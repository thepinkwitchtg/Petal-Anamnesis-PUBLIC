"""
petal-anamnesis :: inspect_exports.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Structure cartographer ~ reads every export folder in /content,
detects schema version, maps the json + jsonl anatomy, and writes
individual markdown reports into reports/ (sibling of daemon/ and content/)

v2 :: now dumps full sample nodes (one user, one assistant) and probes
v3 UUID dir internal anatomy so downstream consumers don't have to guess
nested shapes~
"""

import json
import os
from pathlib import Path
from datetime import datetime
from collections import defaultdict


# ── paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).parent                # daemon/
PROJECT_ROOT = SCRIPT_DIR.parent                    # petal-anamnesis/
CONTENT_DIR  = PROJECT_ROOT / "content"
REPORTS_DIR  = PROJECT_ROOT / "reports"             # sibling to daemon/ + content/
REPORTS_DIR.mkdir(exist_ok=True)


# ── schema detection ───────────────────────────────────────────────────────────
def detect_schema_version(export_path: Path) -> str:
    """
    Sniffs the export folder and returns a human label for the schema version.

    v1       :: single conversations.json at root
    v2       :: chunked conversations-000.json ... conversations-NNN.json
    v2_jsonl :: chunked conversations-000.jsonl ... (jsonl variant)
    v3       :: per-conversation UUID subdirectories (Elior style)
    """
    files = list(export_path.iterdir())
    names = [f.name for f in files]

    if any(n.startswith("conversations-") and n.endswith(".jsonl") for n in names):
        return "v2_chunked_jsonl"

    if any(n.startswith("conversations-") and n.endswith(".json") for n in names):
        return "v2_chunked_json"

    if any(f.is_dir() and _looks_like_uuid(f.name) for f in files):
        return "v3_uuid_dirs"

    if "conversations.jsonl" in names:
        return "v1_single_jsonl"

    if "conversations.json" in names:
        return "v1_single_json"

    return "unknown"


def _looks_like_uuid(name: str) -> bool:
    parts = name.split("-")
    return len(parts) == 5 and all(p.isalnum() for p in parts)


# ── file collectors ────────────────────────────────────────────────────────────
def collect_conversation_files(export_path: Path, schema: str) -> list[Path]:
    """
    Recursively collects all json/jsonl files in the export folder.
    Schema hint is kept for the report label but collection is always exhaustive~
    """
    found = sorted(export_path.rglob("*.json")) + sorted(export_path.rglob("*.jsonl"))
    seen = set()
    result = []
    for p in found:
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result


# ── loaders (json vs jsonl) ────────────────────────────────────────────────────
def load_file(path: Path) -> tuple[bool, list | dict | None, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        return False, None, str(e)

    if path.suffix.lower() == ".jsonl":
        records = []
        for i, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                return False, None, f"line {i}: {e}"
        return True, records, None

    try:
        return True, json.loads(text), None
    except json.JSONDecodeError as e:
        return False, None, str(e)


# ── full sample harvesting (NEW in v2) ─────────────────────────────────────────
def _harvest_sample_nodes(conversation: dict) -> dict:
    """
    Walks a conversation's mapping and returns one example of each role
    we encounter (user, assistant, system, tool)~ preserves full nested
    structure so downstream consumers see the real shape of content/author/metadata.
    """
    samples = {}
    mapping = conversation.get("mapping")
    if not isinstance(mapping, dict):
        return samples

    for node_id, node in mapping.items():
        if not isinstance(node, dict):
            continue
        msg = node.get("message")
        if not isinstance(msg, dict):
            continue
        author = msg.get("author")
        if not isinstance(author, dict):
            continue
        role = author.get("role")
        if not role or role in samples:
            continue
        samples[role] = {
            "node_id": node_id,
            "parent":  node.get("parent"),
            "children": node.get("children"),
            "message": msg,
        }

    return samples


def _harvest_conversation_sample(conversations: list) -> dict:
    """
    Finds the first conversation that has both a user and an assistant message
    and returns its sample nodes + top-level metadata (without the heavy mapping).
    """
    for conv in conversations:
        if not isinstance(conv, dict):
            continue
        samples = _harvest_sample_nodes(conv)
        if "user" in samples and "assistant" in samples:
            meta = {k: v for k, v in conv.items() if k != "mapping"}
            return {
                "conversation_meta": meta,
                "mapping_size": len(conv.get("mapping") or {}),
                "samples": samples,
            }
    return {}


def _scan_role_distribution(conversations: list) -> dict:
    """Counts roles seen across all messages in all conversations."""
    counts = defaultdict(int)
    content_types = defaultdict(int)
    model_slugs = defaultdict(int)
    recipients = defaultdict(int)

    for conv in conversations:
        if not isinstance(conv, dict):
            continue
        mapping = conv.get("mapping")
        if not isinstance(mapping, dict):
            continue
        for node in mapping.values():
            if not isinstance(node, dict):
                continue
            msg = node.get("message")
            if not isinstance(msg, dict):
                continue
            author = msg.get("author") or {}
            counts[author.get("role") or "(none)"] += 1
            content = msg.get("content") or {}
            if isinstance(content, dict):
                content_types[content.get("content_type") or "(none)"] += 1
            md = msg.get("metadata") or {}
            if isinstance(md, dict):
                ms = md.get("model_slug")
                if ms:
                    model_slugs[ms] += 1
            r = msg.get("recipient")
            if r:
                recipients[r] += 1

    return {
        "roles": dict(counts),
        "content_types": dict(content_types),
        "model_slugs": dict(sorted(model_slugs.items(), key=lambda kv: -kv[1])),
        "recipients": dict(recipients),
    }


# ── anatomy dissector ──────────────────────────────────────────────────────────
def dissect_file(path: Path, deep: bool = False) -> dict:
    """
    Loads a json or jsonl file and returns metadata + structural fingerprint.
    deep=True also harvests full sample nodes and scans role/content/model
    distributions~ used for the primary conversations.json so we get the
    full anatomy without re-parsing later.
    """
    result = {
        "file":               path.name,
        "format":             path.suffix.lower().lstrip("."),
        "size_kb":            round(path.stat().st_size / 1024, 2),
        "parse_ok":           False,
        "top_level_type":     None,
        "item_count":         None,
        "sample_keys":        [],
        "message_sample_keys": [],
        "date_range":         {"earliest": None, "latest": None},
        "errors":             [],
        # deep-only fields
        "sample_conversation": None,
        "distribution":        None,
    }

    ok, data, err = load_file(path)
    if not ok:
        result["errors"].append(err)
        return result

    result["parse_ok"]       = True
    result["top_level_type"] = type(data).__name__

    if isinstance(data, list):
        result["item_count"] = len(data)
        if data:
            first = data[0]
            if isinstance(first, dict):
                result["sample_keys"] = list(first.keys())
                msgs = _find_messages(first)
                if msgs:
                    result["message_sample_keys"] = list(msgs[0].keys())
                stamps = _harvest_timestamps(data)
                if stamps:
                    result["date_range"]["earliest"] = _fmt_ts(min(stamps))
                    result["date_range"]["latest"]   = _fmt_ts(max(stamps))

                if deep:
                    result["sample_conversation"] = _harvest_conversation_sample(data)
                    result["distribution"]        = _scan_role_distribution(data)

    elif isinstance(data, dict):
        result["sample_keys"] = list(data.keys())
        result["item_count"]  = 1

    return result


def _find_messages(conversation: dict) -> list:
    for key in ("mapping", "messages", "content"):
        if key in conversation:
            val = conversation[key]
            if isinstance(val, list) and val:
                return [v for v in val if isinstance(v, dict)]
            if isinstance(val, dict):
                nodes = list(val.values())
                return [
                    n.get("message") for n in nodes
                    if isinstance(n, dict) and isinstance(n.get("message"), dict)
                ]
    return []


def _harvest_timestamps(conversations: list) -> list[float]:
    stamps = []
    for conv in conversations:
        if not isinstance(conv, dict):
            continue
        for key in ("create_time", "update_time"):
            val = conv.get(key)
            if isinstance(val, (int, float)) and val > 0:
                stamps.append(float(val))
    return stamps


def _fmt_ts(ts: float) -> str:
    try:
        return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(ts)


# ── UUID dir probing (NEW in v2) ───────────────────────────────────────────────
def probe_uuid_dirs(export_path: Path, max_samples: int = 2) -> dict:
    """
    For v3_uuid_dirs schemas, peeks inside up to max_samples conversation UUID
    folders and reports the file layout~ so we know if the dirs contain a
    full conversation re-encoding or just attachments.
    """
    uuid_dirs = [d for d in export_path.iterdir() if d.is_dir() and _looks_like_uuid(d.name)]
    total = len(uuid_dirs)

    samples = []
    for d in uuid_dirs[:max_samples]:
        entries = sorted(d.iterdir())
        layout = []
        for e in entries:
            entry = {
                "name": e.name,
                "type": "dir" if e.is_dir() else "file",
                "size_kb": round(e.stat().st_size / 1024, 2) if e.is_file() else None,
            }
            # if it's a json file, peek at top-level shape
            if e.is_file() and e.suffix.lower() == ".json":
                ok, data, _ = load_file(e)
                if ok:
                    if isinstance(data, dict):
                        entry["json_top_level"] = "dict"
                        entry["json_keys"] = list(data.keys())
                    elif isinstance(data, list):
                        entry["json_top_level"] = "list"
                        entry["json_item_count"] = len(data)
                        if data and isinstance(data[0], dict):
                            entry["json_first_item_keys"] = list(data[0].keys())
            layout.append(entry)
        samples.append({"uuid": d.name, "entries": layout})

    return {"total_uuid_dirs": total, "samples": samples}


# ── file inventory ─────────────────────────────────────────────────────────────
def inventory_files(export_path: Path) -> dict:
    counts = defaultdict(int)
    for f in export_path.rglob("*"):
        if f.is_file():
            counts[f.suffix.lower() or "(no ext)"] += 1
    return dict(sorted(counts.items()))


# ── pretty json printer for the report ─────────────────────────────────────────
def _fmt_json(obj, indent: int = 2, max_chars: int = 6000) -> str:
    """
    Pretty-print JSON for inclusion in markdown~ truncates long string values
    so giant assistant responses don't bloat the report~ structure is preserved.
    """
    def _trunc(v):
        if isinstance(v, str) and len(v) > 400:
            return v[:400] + f"... [+{len(v) - 400} chars]"
        if isinstance(v, dict):
            return {k: _trunc(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_trunc(x) for x in v]
        return v

    text = json.dumps(_trunc(obj), indent=indent, ensure_ascii=False)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... [+{len(text) - max_chars} chars truncated]"
    return text


# ── markdown report writer ─────────────────────────────────────────────────────
def write_report(
    export_name: str,
    schema: str,
    file_analyses: list[dict],
    inventory: dict,
    uuid_probe: dict | None = None,
) -> Path:
    slug        = export_name.replace(" ", "_").replace("-", "_")
    report_path = REPORTS_DIR / f"{slug}.md"

    total_convos = sum(
        a["item_count"] for a in file_analyses
        if a["parse_ok"] and a["item_count"] and a["top_level_type"] == "list"
    )

    lines = [
        f"# {export_name}",
        f"",
        f"**Schema version:** `{schema}`  ",
        f"**Analysed:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}  ",
        f"**Total conversations detected:** {total_convos}",
        f"",
        f"## File inventory",
        f"",
        f"| Extension | Count |",
        f"|-----------|-------|",
    ]
    for ext, count in inventory.items():
        lines.append(f"| `{ext}` | {count} |")

    # ── UUID dir probe section (v3 only) ────────────────────────────────
    if uuid_probe and uuid_probe.get("total_uuid_dirs"):
        lines += [
            "",
            "## UUID directory anatomy",
            "",
            f"**Total UUID dirs:** {uuid_probe['total_uuid_dirs']}",
            "",
        ]
        for s in uuid_probe["samples"]:
            lines.append(f"### `{s['uuid']}/`")
            lines.append("")
            lines.append("| Entry | Type | Size (KB) | Notes |")
            lines.append("|-------|------|-----------|-------|")
            for e in s["entries"]:
                notes = []
                if "json_top_level" in e:
                    notes.append(f"json:`{e['json_top_level']}`")
                if "json_keys" in e:
                    notes.append(f"keys: {', '.join(f'`{k}`' for k in e['json_keys'][:8])}")
                if "json_item_count" in e:
                    notes.append(f"items:{e['json_item_count']}")
                if "json_first_item_keys" in e:
                    notes.append(f"first-item keys: {', '.join(f'`{k}`' for k in e['json_first_item_keys'][:8])}")
                lines.append(
                    f"| `{e['name']}` | {e['type']} | "
                    f"{e['size_kb'] if e['size_kb'] is not None else '—'} | "
                    f"{' / '.join(notes) if notes else ''} |"
                )
            lines.append("")

    lines += ["", "## Conversation file analysis", ""]

    for a in file_analyses:
        lines.append(f"### `{a['file']}` ({a['format'].upper()})")
        lines.append(f"- **Size:** {a['size_kb']} KB")
        lines.append(f"- **Parsed OK:** {a['parse_ok']}")

        if a["errors"]:
            lines.append(f"- **Errors:** {'; '.join(a['errors'])}")

        if a["parse_ok"]:
            lines.append(f"- **Top-level type:** `{a['top_level_type']}`")
            lines.append(f"- **Item count:** {a['item_count']}")

            if a["sample_keys"]:
                keys_str = ", ".join(f"`{k}`" for k in a["sample_keys"])
                lines.append(f"- **Conversation fields:** {keys_str}")

            if a["message_sample_keys"]:
                mkeys_str = ", ".join(f"`{k}`" for k in a["message_sample_keys"])
                lines.append(f"- **Message node fields:** {mkeys_str}")

            dr = a["date_range"]
            if dr["earliest"]:
                lines.append(f"- **Date range:** {dr['earliest']} → {dr['latest']}")

            # ── deep sections ──────────────────────────────────────────
            dist = a.get("distribution")
            if dist:
                lines += ["", "#### Role / content distribution", ""]
                if dist["roles"]:
                    lines.append("**Roles:**")
                    for role, cnt in sorted(dist["roles"].items(), key=lambda kv: -kv[1]):
                        lines.append(f"- `{role}` :: {cnt}")
                    lines.append("")
                if dist["content_types"]:
                    lines.append("**Content types:**")
                    for ct, cnt in sorted(dist["content_types"].items(), key=lambda kv: -kv[1]):
                        lines.append(f"- `{ct}` :: {cnt}")
                    lines.append("")
                if dist["recipients"]:
                    lines.append("**Recipients (top 15):**")
                    for r, cnt in list(sorted(dist["recipients"].items(), key=lambda kv: -kv[1]))[:15]:
                        lines.append(f"- `{r}` :: {cnt}")
                    lines.append("")
                if dist["model_slugs"]:
                    lines.append("**Model slugs (all):**")
                    for ms, cnt in dist["model_slugs"].items():
                        lines.append(f"- `{ms}` :: {cnt}")
                    lines.append("")

            sample = a.get("sample_conversation")
            if sample:
                lines += ["", "#### Sample conversation anatomy", ""]
                lines.append(f"**Mapping size of sampled conversation:** {sample['mapping_size']} nodes")
                lines.append("")
                lines.append("**Conversation top-level (minus mapping):**")
                lines.append("```json")
                lines.append(_fmt_json(sample["conversation_meta"], max_chars=3000))
                lines.append("```")
                lines.append("")
                for role, node in sample["samples"].items():
                    lines.append(f"**Sample `{role}` node:**")
                    lines.append("```json")
                    lines.append(_fmt_json(node, max_chars=5000))
                    lines.append("```")
                    lines.append("")

        lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


# ── orchestrator ───────────────────────────────────────────────────────────────
def inspect_all_exports():
    if not CONTENT_DIR.exists():
        print(f"✗ content/ not found at {CONTENT_DIR}")
        return

    exports = [d for d in sorted(CONTENT_DIR.iterdir()) if d.is_dir()]

    if not exports:
        print("✗ no export folders found in content/")
        return

    print(f"✦ petal-anamnesis :: inspect_exports (v2)")
    print(f"  found {len(exports)} export(s)\n")

    for export_path in exports:
        name = export_path.name
        print(f"  ◈ {name}")

        schema = detect_schema_version(export_path)
        print(f"    schema    → {schema}")

        conv_files = collect_conversation_files(export_path, schema)
        print(f"    files     → {len(conv_files)}")

        # deep-dissect the largest json file (the actual conversations dump)
        # shallow-dissect the rest~
        analyses = []
        if conv_files:
            biggest = max(conv_files, key=lambda p: p.stat().st_size)
            for f in conv_files:
                deep = (f == biggest) or f.name.startswith("conversations")
                analyses.append(dissect_file(f, deep=deep))

        inventory = inventory_files(export_path)

        uuid_probe = None
        if schema == "v3_uuid_dirs":
            uuid_probe = probe_uuid_dirs(export_path, max_samples=2)
            print(f"    uuid dirs → {uuid_probe['total_uuid_dirs']} (sampled {len(uuid_probe['samples'])})")

        report_path = write_report(name, schema, analyses, inventory, uuid_probe)
        print(f"    report    → {report_path.relative_to(PROJECT_ROOT)}\n")

    print(f"✦ reports written to {REPORTS_DIR.relative_to(PROJECT_ROOT)}/")


if __name__ == "__main__":
    inspect_all_exports()