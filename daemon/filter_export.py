"""
petal-anamnesis :: filter_export.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Filters conversations from all exports by model slug~
produces clean flat JSON files per export ready for diffing~

matching strategy: conversation included if EITHER
  - its default_model_slug matches
  - ANY of its messages carry a matching model tag
  (because chatGPT lies at conv level sometimes~)

outputs land in reports/filtered/ as sibling to daemon/
"""

import json
from pathlib import Path
from datetime import datetime


# ── config ~ edit these to your chosen slugs ───────────────────────────────────
TARGET_MODELS = [
    "gpt-4o",
    "gpt-4-5",
    "unknown",
]

# models that only count when seen on specific roles~
# unknown at user role is just old schema behavior, not concealed routing~
# we only want unknown when the assistant is the one with no disclosed model~
ROLE_RESTRICTED_MODELS: dict[str, set[str]] = {
    "unknown": {"assistant"},
}

# ── paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR    = Path(__file__).parent
PROJECT_ROOT  = SCRIPT_DIR.parent
CONTENT_DIR   = PROJECT_ROOT / "content"
FILTERED_DIR  = PROJECT_ROOT / "reports" / "filtered"
FILTERED_DIR.mkdir(parents=True, exist_ok=True)

# sidecar files we never want to treat as conversation sources~
SKIP_FILES = {
    "user.json", "sora.json", "message_feedback.json",
    "shared_conversations.json", "group_chats.json", "shopping.json",
}


# ── loaders ────────────────────────────────────────────────────────────────────
def load_conversations(path: Path) -> list[dict]:
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


# ── message model extractor ────────────────────────────────────────────────────
def get_message_models(conv: dict, role_restricted: dict[str, set[str]] | None = None) -> set[str]:
    """
    Pulls every model slug found in any message node's metadata~
    role_restricted: models that only count when seen on specific roles~
    e.g. {"unknown": {"assistant"}} means unknown only counts on assistant messages~
    """
    models = set()
    mapping = conv.get("mapping", {})
    if not isinstance(mapping, dict):
        return models

    for node in mapping.values():
        if not isinstance(node, dict):
            continue
        msg = node.get("message")
        if not isinstance(msg, dict):
            continue

        # get role first so we can gate restricted models~
        author = msg.get("author", {})
        role   = author.get("role", "unknown") if isinstance(author, dict) else "unknown"

        meta = msg.get("metadata", {})
        if not isinstance(meta, dict):
            continue

        for key in ("model_slug", "default_model_slug", "gpt_id"):
            val = meta.get(key)
            if not val:
                continue
            # check role restriction if applicable~
            if role_restricted and val in role_restricted:
                if role not in role_restricted[val]:
                    continue
            models.add(val)

    return models


# ── message tree flattener ─────────────────────────────────────────────────────
def flatten_messages(conv: dict) -> list[dict]:
    """
    Walks the mapping tree in chronological order and returns
    a clean flat list of messages with role + content + model + timestamps~
    follows the current_node pointer to get the canonical thread~
    """
    mapping = conv.get("mapping", {})
    if not isinstance(mapping, dict):
        return []

    # build parent→children index
    children_of: dict[str, list[str]] = {nid: [] for nid in mapping}
    root_id = None
    for nid, node in mapping.items():
        parent = node.get("parent")
        if parent and parent in children_of:
            children_of[parent].append(nid)
        if parent is None or parent not in mapping:
            root_id = nid

    if root_id is None:
        return []

    # walk depth-first following current_node lineage where possible
    current_node = conv.get("current_node")
    canonical_ids: set[str] = set()

    if current_node and current_node in mapping:
        # trace back from current_node to root to get the canonical path
        node_id = current_node
        while node_id:
            canonical_ids.add(node_id)
            node_id = mapping.get(node_id, {}).get("parent")
    else:
        # fallback: include everything
        canonical_ids = set(mapping.keys())

    messages = []
    for nid in mapping:
        if nid not in canonical_ids:
            continue
        node = mapping[nid]
        msg  = node.get("message")
        if not isinstance(msg, dict):
            continue

        author  = msg.get("author", {})
        role    = author.get("role", "unknown") if isinstance(author, dict) else "unknown"
        content = msg.get("content", {})
        meta    = msg.get("metadata", {}) or {}

        # extract text from content parts~
        text = ""
        if isinstance(content, dict):
            parts = content.get("parts", [])
            text  = " ".join(str(p) for p in parts if p and isinstance(p, str))
        elif isinstance(content, str):
            text = content

        if not text.strip():
            continue

        messages.append({
            "node_id":    nid,
            "role":       role,
            "model":      meta.get("model_slug") or meta.get("default_model_slug") or "unknown",
            "text":       text,
            "create_time": msg.get("create_time"),
            "status":     msg.get("status"),
        })

    # sort by create_time so the thread reads chronologically~
    messages.sort(key=lambda m: m["create_time"] or 0)
    return messages


# ── conversation matcher ───────────────────────────────────────────────────────
def matches_target(conv: dict, targets: set[str]) -> bool:
    """True if conv or any of its messages touch a target model~
    respects role restrictions so unknown user messages stay filtered out~"""
    conv_model = conv.get("default_model_slug", "unknown")
    # for conv-level unknown we can't check role, so we let message-level decide~
    if conv_model in targets and conv_model not in ROLE_RESTRICTED_MODELS:
        return True
    return bool(get_message_models(conv, ROLE_RESTRICTED_MODELS) & targets)


# ── core filter ────────────────────────────────────────────────────────────────
def filter_export(export_path: Path, targets: set[str]) -> list[dict]:
    """
    Loads all conversation files in an export, filters by target models,
    and returns a flat list of enriched conversation dicts~
    """
    all_files = [
        p for p in sorted(export_path.rglob("*.json"))
        if p.name not in SKIP_FILES
    ] + [
        p for p in sorted(export_path.rglob("*.jsonl"))
        if p.name not in SKIP_FILES
    ]

    seen_ids  = set()
    results   = []

    for jf in all_files:
        convos = load_conversations(jf)
        for conv in convos:
            conv_id = conv.get("id") or conv.get("conversation_id")
            if not conv_id or conv_id in seen_ids:
                continue
            if not matches_target(conv, targets):
                continue

            seen_ids.add(conv_id)
            results.append({
                "id":                conv_id,
                "title":             conv.get("title", ""),
                "create_time":       conv.get("create_time"),
                "update_time":       conv.get("update_time"),
                "default_model_slug": conv.get("default_model_slug", "unknown"),
                "message_models":    sorted(get_message_models(conv, ROLE_RESTRICTED_MODELS)),
                "messages":          flatten_messages(conv),
                "source_file":       str(jf.relative_to(export_path)),
            })

    # sort by create_time so corpus reads chronologically~
    results.sort(key=lambda c: c["create_time"] or 0)
    return results


# ── writer ─────────────────────────────────────────────────────────────────────
def write_filtered(export_name: str, conversations: list[dict], targets: list[str]) -> Path:
    slug         = export_name.replace(" ", "_").replace("-", "_")
    models_tag   = "_".join(t.replace("-", "") for t in sorted(targets))
    output_path  = FILTERED_DIR / f"{slug}__{models_tag}.json"

    payload = {
        "meta": {
            "export":        export_name,
            "target_models": targets,
            "filtered_at":   datetime.utcnow().isoformat() + "Z",
            "total_conversations": len(conversations),
            "total_messages": sum(len(c["messages"]) for c in conversations),
        },
        "conversations": conversations,
    }

    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output_path


# ── orchestrator ───────────────────────────────────────────────────────────────
def filter_all_exports():
    if not CONTENT_DIR.exists():
        print(f"✗ content/ not found at {CONTENT_DIR}")
        return

    exports = [d for d in sorted(CONTENT_DIR.iterdir()) if d.is_dir()]
    if not exports:
        print("✗ no export folders found in content/")
        return

    targets = set(TARGET_MODELS)

    print(f"✦ petal-anamnesis :: filter_export")
    print(f"  target models: {sorted(targets)}")
    print(f"  found {len(exports)} export(s)\n")

    for export_path in exports:
        name = export_path.name
        print(f"  ◈ {name}")

        conversations = filter_export(export_path, targets)
        total_msgs    = sum(len(c["messages"]) for c in conversations)

        print(f"    conversations matched → {len(conversations)}")
        print(f"    messages extracted   → {total_msgs}")

        if conversations:
            out = write_filtered(name, conversations, TARGET_MODELS)
            print(f"    output → {out.relative_to(PROJECT_ROOT)}")
        else:
            print(f"    (nothing matched, skipping write)")

        print()

    print(f"✦ filtered corpora ready in reports/filtered/")
    print(f"  feed these into diff_messages.py next~ 🔮")


if __name__ == "__main__":
    filter_all_exports()