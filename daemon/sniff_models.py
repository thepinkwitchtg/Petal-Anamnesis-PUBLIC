"""
petal-anamnesis :: sniff_models.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Crawls all exports and surfaces every model string found~
both at conversation level (default_model_slug) and at
individual message level (metadata.model_slug / metadata.default_model_slug)

outputs a markdown report per export + a unified cross-export summary
so you can pick exact model strings for the diff filter~
"""

import json
from pathlib import Path
from datetime import datetime
from collections import defaultdict


# ── paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
CONTENT_DIR  = PROJECT_ROOT / "content"
REPORTS_DIR  = PROJECT_ROOT / "reports" / "models"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


# ── loaders ────────────────────────────────────────────────────────────────────
def load_conversations(path: Path) -> list[dict]:
    """Loads json or jsonl, always returns a flat list of conversation dicts."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"      ✗ read error {path.name}: {e}")
        return []

    if path.suffix.lower() == ".jsonl":
        records = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except Exception:
                    pass
        return records

    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if isinstance(data, dict):
            return [data]
    except Exception as e:
        print(f"      ✗ parse error {path.name}: {e}")
    return []


def is_conversation_file(path: Path) -> bool:
    """Keep only files that look like conversation exports, skip sidecars."""
    skip = {"user.json", "sora.json", "message_feedback.json",
            "shared_conversations.json", "group_chats.json", "shopping.json"}
    return path.name not in skip


# ── model extraction ───────────────────────────────────────────────────────────
def extract_models_from_export(export_path: Path) -> dict:
    """
    Returns:
      {
        "conv_level":    Counter { model_slug: count_of_conversations },
        "message_level": Counter { model_slug: count_of_messages },
        "per_conv":      { conv_id: { "conv_model": str, "message_models": set } }
      }
    """
    conv_level    = defaultdict(int)
    message_level = defaultdict(int)
    per_conv      = {}

    json_files = [
        p for p in sorted(export_path.rglob("*.json"))
        if is_conversation_file(p)
    ]
    json_files += [
        p for p in sorted(export_path.rglob("*.jsonl"))
        if is_conversation_file(p)
    ]

    for jf in json_files:
        conversations = load_conversations(jf)
        for conv in conversations:
            if not isinstance(conv, dict):
                continue

            conv_id = conv.get("id") or conv.get("conversation_id", "unknown")

            # conversation-level model
            conv_model = conv.get("default_model_slug") or "unknown"
            conv_level[conv_model] += 1

            # message-level models live inside mapping
            mapping = conv.get("mapping", {})
            if not isinstance(mapping, dict):
                continue

            msg_models = set()
            for node in mapping.values():
                if not isinstance(node, dict):
                    continue
                msg = node.get("message")
                if not isinstance(msg, dict):
                    continue
                meta = msg.get("metadata", {})
                if not isinstance(meta, dict):
                    continue

                # chatGPT uses several possible keys here~
                model = (
                    meta.get("model_slug")
                    or meta.get("default_model_slug")
                    or meta.get("gpt_id")
                    or None
                )
                if model:
                    message_level[model] += 1
                    msg_models.add(model)

            per_conv[conv_id] = {
                "conv_model":    conv_model,
                "message_models": msg_models,
            }

    return {
        "conv_level":    dict(sorted(conv_level.items(),    key=lambda x: -x[1])),
        "message_level": dict(sorted(message_level.items(), key=lambda x: -x[1])),
        "per_conv":      per_conv,
    }


# ── report writers ─────────────────────────────────────────────────────────────
def write_export_report(export_name: str, data: dict) -> Path:
    slug        = export_name.replace(" ", "_").replace("-", "_")
    report_path = REPORTS_DIR / f"{slug}_models.md"

    conv_level    = data["conv_level"]
    message_level = data["message_level"]
    per_conv      = data["per_conv"]

    # conversations that mixed models mid-thread~ high signal~
    mixed = {
        cid: info for cid, info in per_conv.items()
        if len(info["message_models"]) > 1
    }

    lines = [
        f"# {export_name} :: model inventory",
        f"",
        f"**Analysed:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}  ",
        f"**Total conversations scanned:** {len(per_conv)}  ",
        f"**Conversations with mixed models:** {len(mixed)}",
        f"",
        f"## Conversation-level `default_model_slug`",
        f"",
        f"| Model slug | Conversations |",
        f"|------------|---------------|",
    ]
    for model, count in conv_level.items():
        lines.append(f"| `{model}` | {count} |")

    lines += [
        f"",
        f"## Message-level model tags",
        f"_(from `metadata.model_slug` / `metadata.default_model_slug`)_",
        f"",
        f"| Model slug | Messages |",
        f"|------------|----------|",
    ]
    for model, count in message_level.items():
        lines.append(f"| `{model}` | {count} |")

    if mixed:
        lines += [
            f"",
            f"## Mixed-model conversations",
            f"_conversations where the model changed mid-thread~_",
            f"",
            f"| Conversation ID | Conv model | Message models seen |",
            f"|-----------------|------------|---------------------|",
        ]
        for cid, info in list(mixed.items())[:50]:  # cap at 50 for readability
            msg_models_str = ", ".join(f"`{m}`" for m in sorted(info["message_models"]))
            lines.append(
                f"| `{cid[:16]}…` | `{info['conv_model']}` | {msg_models_str} |"
            )
        if len(mixed) > 50:
            lines.append(f"| _…and {len(mixed) - 50} more_ | | |")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def write_summary_report(all_data: dict[str, dict]) -> Path:
    """Cross-export unified view of all model strings encountered~"""
    report_path = REPORTS_DIR / "ALL_EXPORTS_models_summary.md"

    # union of all model slugs across exports
    all_conv_models = defaultdict(lambda: defaultdict(int))
    all_msg_models  = defaultdict(lambda: defaultdict(int))

    for export_name, data in all_data.items():
        for model, count in data["conv_level"].items():
            all_conv_models[model][export_name] = count
        for model, count in data["message_level"].items():
            all_msg_models[model][export_name] = count

    export_names = list(all_data.keys())

    lines = [
        f"# petal-anamnesis :: all exports model summary",
        f"",
        f"**Analysed:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        f"",
        f"## Conversation-level models (across all exports)",
        f"",
        f"| Model slug | " + " | ".join(export_names) + " |",
        f"|------------|" + "|".join(["---|"] * len(export_names)),
    ]
    for model in sorted(all_conv_models.keys()):
        row = f"| `{model}` |"
        for name in export_names:
            count = all_conv_models[model].get(name, 0)
            row += f" {count} |"
        lines.append(row)

    lines += [
        f"",
        f"## Message-level models (across all exports)",
        f"",
        f"| Model slug | " + " | ".join(export_names) + " |",
        f"|------------|" + "|".join(["---|"] * len(export_names)),
    ]
    for model in sorted(all_msg_models.keys()):
        row = f"| `{model}` |"
        for name in export_names:
            count = all_msg_models[model].get(name, 0)
            row += f" {count} |"
        lines.append(row)

    lines += [
        f"",
        f"---",
        f"_copy the exact model strings above into your diff filter config~_ 💗",
    ]

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


# ── orchestrator ───────────────────────────────────────────────────────────────
def sniff_all_exports():
    if not CONTENT_DIR.exists():
        print(f"✗ content/ not found at {CONTENT_DIR}")
        return

    exports = [d for d in sorted(CONTENT_DIR.iterdir()) if d.is_dir()]
    if not exports:
        print("✗ no export folders found in content/")
        return

    print(f"✦ petal-anamnesis :: sniff_models")
    print(f"  found {len(exports)} export(s)\n")

    all_data = {}

    for export_path in exports:
        name = export_path.name
        print(f"  ◈ {name}")

        data = extract_models_from_export(export_path)

        conv_total = sum(data["conv_level"].values())
        msg_total  = sum(data["message_level"].values())
        print(f"    conversations scanned → {conv_total}")
        print(f"    unique conv models    → {len(data['conv_level'])}")
        print(f"    unique msg models     → {len(data['message_level'])}")

        report_path = write_export_report(name, data)
        print(f"    report → {report_path.relative_to(PROJECT_ROOT)}\n")

        all_data[name] = data

    summary_path = write_summary_report(all_data)
    print(f"  ◈ summary → {summary_path.relative_to(PROJECT_ROOT)}")
    print(f"\n✦ done~ paste your chosen model slugs into diff_messages.py 💗")


if __name__ == "__main__":
    sniff_all_exports()
