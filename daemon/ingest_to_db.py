"""
petal-anamnesis :: ingest_to_db.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Ingests a chatGPT export folder into the petal sqlite db~

usage:
  python daemon/ingest_to_db.py                       # ingest all exports in content/
  python daemon/ingest_to_db.py "Elior - 2025-08-14"  # ingest one by name

idempotent~ rerunning replaces all rows for the given export's slug. content
in reports/ is the source of truth; this script only writes the curation db.

design notes (read once, save you future surprises):
  • we delete + reinsert per export rather than upsert, so removed
    conversations across re-ingests are properly purged
  • branch_kind is precomputed by walking the tree from current_node back
    to root once per conversation (cheap, ~14 nodes avg)
  • text_plain extracts whatever flat text we can from each content_type;
    blobs are always preserved in content_json for everything else
"""

import json
import sqlite3
import sys
from pathlib import Path
from datetime import datetime
from collections import defaultdict
import os
from dotenv import load_dotenv
load_dotenv()


# ── paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
CONTENT_DIR  = PROJECT_ROOT / "content"
DB_PATH      = PROJECT_ROOT / "db" / "petal-anamnesis.db"


# ── allowlist: only these export folders will ever be ingested ─────────────────
# edit this list to add/remove exports~ names must match the folder names in content/
ALLOWED_EXPORTS = [
    x.strip() for x in os.getenv("BASELINE_EXPORT", "").split(",") if x.strip()
]

# ── slug helpers (must match inspect_exports.py) ───────────────────────────────
def export_slug(name: str) -> str:
    return name.replace(" ", "_").replace("-", "_")


# ── content type → text_plain extractors ───────────────────────────────────────
def extract_text(content: dict | None) -> str | None:
    """
    Best-effort flat-text extraction per content_type~ returns None if there's
    nothing meaningfully textual (e.g. pure image attachments, empty parts).
    """
    if not isinstance(content, dict):
        return None
    ct = content.get("content_type")

    if ct == "text":
        parts = content.get("parts") or []
        out = "\n".join(p for p in parts if isinstance(p, str) and p)
        return out or None

    if ct == "multimodal_text":
        # parts is a mix of strings (text) and dicts (image/audio refs)
        parts = content.get("parts") or []
        texts = [p for p in parts if isinstance(p, str) and p]
        return "\n".join(texts) or None

    if ct == "code":
        return content.get("text") or None

    if ct in ("execution_output", "computer_output"):
        return content.get("text") or None

    if ct == "tether_quote":
        # browse tool result; preserve text + url
        parts = [content.get("title"), content.get("text"), content.get("url")]
        joined = "\n".join(p for p in parts if isinstance(p, str) and p)
        return joined or None

    if ct == "tether_browsing_display":
        return content.get("result") or None

    if ct == "user_editable_context":
        # the about_user + about_model archive ~ precious lily material
        parts = [content.get("user_profile"), content.get("user_instructions")]
        joined = "\n\n".join(p for p in parts if isinstance(p, str) and p)
        return joined or None

    if ct == "thoughts":
        # gpt-5 reasoning blocks~ thoughts is a list of {summary, content}
        thoughts = content.get("thoughts") or []
        out = []
        for t in thoughts:
            if isinstance(t, dict):
                out.append(t.get("summary") or "")
                out.append(t.get("content") or "")
        joined = "\n".join(x for x in out if x)
        return joined or None

    if ct == "reasoning_recap":
        return content.get("content") or None

    if ct == "system_error":
        return content.get("text") or content.get("name") or None

    # unknown content_type: try .text, .content, .parts as last resorts
    for k in ("text", "content"):
        v = content.get(k)
        if isinstance(v, str) and v:
            return v
    parts = content.get("parts")
    if isinstance(parts, list):
        s = "\n".join(p for p in parts if isinstance(p, str) and p)
        return s or None

    return None


# ── tree walker ────────────────────────────────────────────────────────────────
def compute_branch_kinds(mapping: dict, current_node: str | None) -> tuple[dict, dict]:
    """
    Walks back from current_node to root, marking 'chosen' with positions~
    everything else reachable from root is 'alternate'~ everything else 'orphan'.

    Returns:
      branch_kinds: {message_id: 'chosen' | 'alternate' | 'orphan'}
      positions:    {message_id: int_position}  (chosen path only)
    """
    branch_kinds: dict[str, str] = {}
    positions: dict[str, int] = {}

    # walk current_node → root
    chosen_path_reversed = []
    cursor = current_node
    seen_walk = set()
    while cursor and cursor in mapping and cursor not in seen_walk:
        seen_walk.add(cursor)
        chosen_path_reversed.append(cursor)
        node = mapping[cursor]
        cursor = node.get("parent") if isinstance(node, dict) else None

    chosen_path = list(reversed(chosen_path_reversed))
    for i, nid in enumerate(chosen_path):
        branch_kinds[nid] = "chosen"
        positions[nid] = i

    # find roots (nodes with no parent OR parent not in mapping)
    roots = [
        nid for nid, node in mapping.items()
        if isinstance(node, dict)
        and (not node.get("parent") or node.get("parent") not in mapping)
    ]

    # BFS from roots to mark anything reachable as at least 'alternate'
    reachable = set()
    queue = list(roots)
    while queue:
        nid = queue.pop()
        if nid in reachable or nid not in mapping:
            continue
        reachable.add(nid)
        node = mapping[nid]
        if isinstance(node, dict):
            for child in node.get("children") or []:
                queue.append(child)

    for nid in mapping:
        if nid not in branch_kinds:
            branch_kinds[nid] = "alternate" if nid in reachable else "orphan"

    return branch_kinds, positions


# ── ingest one conversation ────────────────────────────────────────────────────
def ingest_conversation(cur: sqlite3.Cursor, conv: dict, source_export: str) -> dict:
    """Returns stats dict for the conversation just ingested."""
    conv_id = conv.get("conversation_id") or conv.get("id")
    if not conv_id:
        return {"skipped": True, "reason": "no conversation_id"}

    mapping = conv.get("mapping") or {}
    current_node = conv.get("current_node")
    branch_kinds, positions = compute_branch_kinds(mapping, current_node)

    # walk mapping once to gather stats + insert messages
    msg_rows = []
    tool_rows = []
    attach_rows = []
    role_counts = defaultdict(int)

    for node_id, node in mapping.items():
        if not isinstance(node, dict):
            continue
        msg = node.get("message") or {}
        if not isinstance(msg, dict):
            # nodes without messages still exist in the tree (client-created-root)
            # we record them anyway to preserve structure
            msg = {}

        author = msg.get("author") or {}
        role = author.get("role")
        author_name = author.get("name")

        content = msg.get("content") or {}
        content_type = content.get("content_type") if isinstance(content, dict) else None
        text_plain = extract_text(content)

        metadata = msg.get("metadata") or {}
        model_slug = metadata.get("model_slug") if isinstance(metadata, dict) else None
        is_hidden = 1 if (isinstance(metadata, dict)
                          and metadata.get("is_visually_hidden_from_conversation")) else 0

        role_counts[role] += 1

        msg_rows.append((
            node_id,                                    # message_id
            conv_id,                                    # conversation_id
            source_export,                              # source_export
            node.get("parent"),                         # parent_id
            positions.get(node_id),                     # position (NULL if off-path)
            branch_kinds.get(node_id, "orphan"),        # branch_kind
            role,
            author_name,
            msg.get("recipient"),
            msg.get("channel"),
            content_type,
            text_plain,
            json.dumps(content, ensure_ascii=False) if content else "{}",
            msg.get("create_time"),
            msg.get("update_time"),
            msg.get("status"),
            1 if msg.get("end_turn") else (0 if msg.get("end_turn") is False else None),
            msg.get("weight"),
            model_slug,
            is_hidden,
            json.dumps(node, ensure_ascii=False),       # raw_json (whole node)
        ))

        # tool projection: anything where role='tool' OR recipient != 'all'
        recipient = msg.get("recipient")
        is_tool_call = bool(recipient) and recipient != "all"
        is_tool_result = role == "tool"

        if is_tool_call or is_tool_result:
            direction = "result" if is_tool_result else "call"
            tool_name = author_name if is_tool_result else recipient
            # summary: first 200 chars of text_plain if any
            summary = (text_plain[:200] if text_plain else None)
            tool_rows.append((
                conv_id, node_id, direction, tool_name,
                recipient, msg.get("channel"), summary,
            ))

        # attachments: extracted from multimodal_text or metadata.attachments
        if isinstance(metadata, dict):
            for att in (metadata.get("attachments") or []):
                if isinstance(att, dict):
                    attach_rows.append((
                        conv_id, node_id,
                        att.get("id"),
                        att.get("name"),
                        att.get("mimeType") or att.get("mime_type"),
                        att.get("size"),
                        att.get("name"),  # relative_path guess
                        0,                # resolved (we don't check fs here)
                    ))
        # also scan multimodal_text parts for image refs
        if content_type == "multimodal_text" and isinstance(content, dict):
            for part in content.get("parts") or []:
                if isinstance(part, dict):
                    aid = part.get("asset_pointer") or part.get("file_id")
                    if aid:
                        attach_rows.append((
                            conv_id, node_id,
                            aid,
                            None,
                            part.get("content_type"),
                            part.get("size_bytes"),
                            None,
                            0,
                        ))

    # insert order matters: conversation first (FK target), then messages,
    # then tool_messages + attachments (both FK to messages)
    cur.execute("""
        INSERT INTO conversations (
            conversation_id, source_export, title, create_time, update_time,
            current_node, default_model_slug, conversation_origin,
            conversation_template_id, gizmo_id, gizmo_type, voice, memory_scope,
            is_archived, is_starred, is_do_not_remember, is_study_mode,
            message_count, user_message_count, assistant_message_count,
            raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        conv_id, source_export, conv.get("title"),
        conv.get("create_time"), conv.get("update_time"),
        current_node, conv.get("default_model_slug"),
        conv.get("conversation_origin"), conv.get("conversation_template_id"),
        conv.get("gizmo_id"), conv.get("gizmo_type"),
        conv.get("voice"), conv.get("memory_scope"),
        1 if conv.get("is_archived") else 0,
        1 if conv.get("is_starred") else 0,
        1 if conv.get("is_do_not_remember") else 0,
        1 if conv.get("is_study_mode") else 0,
        sum(role_counts.values()),
        role_counts.get("user", 0),
        role_counts.get("assistant", 0),
        # raw_json: store everything except mapping (which is in messages already)
        json.dumps({k: v for k, v in conv.items() if k != "mapping"},
                   ensure_ascii=False),
    ))

    # now the messages (FK to conversation, now safe)
    cur.executemany("""
        INSERT INTO messages (
            message_id, conversation_id, source_export, parent_id, position, branch_kind,
            role, author_name, recipient, channel,
            content_type, text_plain, content_json,
            create_time, update_time, status, end_turn, weight,
            model_slug, is_hidden, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, msg_rows)

    # then projections (FK to messages, now safe)
    cur.executemany("""
        INSERT INTO tool_messages (
            conversation_id, message_id, direction, tool_name,
            recipient, channel, summary
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, tool_rows)

    cur.executemany("""
        INSERT INTO attachments (
            conversation_id, message_id, file_id, file_name,
            mime_type, size_bytes, relative_path, resolved
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, attach_rows)

    return {
        "conversation_id": conv_id,
        "messages": sum(role_counts.values()),
        "tool_rows": len(tool_rows),
        "attachments": len(attach_rows),
    }


# ── export-level ingest ────────────────────────────────────────────────────────
def ingest_export(conn: sqlite3.Connection, export_dir: Path) -> dict:
    """Ingests a single export folder~ idempotent: purges existing rows first."""
    slug = export_slug(export_dir.name)
    print(f"  ◈ {export_dir.name}")
    print(f"    slug      → {slug}")

    # locate the conversations.json (v3) or first conversations-*.json (v2)
    candidates = sorted(export_dir.glob("conversations.json"))
    if not candidates:
        candidates = sorted(export_dir.glob("conversations-*.json"))
    if not candidates:
        print(f"    ✗ no conversations json found, skipping")
        return {"skipped": True}

    cur = conn.cursor()

    # idempotent purge: cascade through messages/tool/attachments via FK
    print(f"    purging existing rows for slug...")
    cur.execute("DELETE FROM exports WHERE slug = ?", (slug,))

    # detect schema_kind (cheap re-check)
    schema_kind = "v3_uuid_dirs" if (export_dir / "conversations.json").exists() else "v2_chunked_json"

    # record the export
    cur.execute("""
        INSERT INTO exports (slug, display_name, schema_kind)
        VALUES (?, ?, ?)
    """, (slug, export_dir.name, schema_kind))

    total_convos = 0
    total_messages = 0
    total_tools = 0
    total_attachments = 0

    for path in candidates:
        print(f"    reading   → {path.name} ({round(path.stat().st_size / 1024 / 1024, 1)} MB)")
        with path.open(encoding="utf-8") as f:
            convs = json.load(f)
        if not isinstance(convs, list):
            print(f"    ✗ top-level not a list, skipping")
            continue

        print(f"    ingesting → {len(convs)} conversations")
        for i, conv in enumerate(convs):
            if not isinstance(conv, dict):
                continue
            stats = ingest_conversation(cur, conv, slug)
            if "messages" in stats:
                total_convos += 1
                total_messages += stats["messages"]
                total_tools += stats["tool_rows"]
                total_attachments += stats["attachments"]
            if (i + 1) % 200 == 0:
                conn.commit()  # periodic checkpoint to keep WAL bounded
                print(f"      ... {i+1}/{len(convs)}")

    conn.commit()
    print(f"    ✓ ingested {total_convos} conversations "
          f"({total_messages} msgs, {total_tools} tool rows, {total_attachments} attachments)")

    return {
        "slug": slug,
        "conversations": total_convos,
        "messages": total_messages,
        "tool_messages": total_tools,
        "attachments": total_attachments,
    }


# ── orchestrator ───────────────────────────────────────────────────────────────
def main():
    if not DB_PATH.exists():
        print(f"✗ db not found at {DB_PATH}")
        print(f"  run: node db/migrate.mjs")
        sys.exit(1)

    if not CONTENT_DIR.exists():
        print(f"✗ content/ not found at {CONTENT_DIR}")
        sys.exit(1)

    # filter by name if provided; always constrained to ALLOWED_EXPORTS
    target = sys.argv[1] if len(sys.argv) > 1 else None

    all_exports = [d for d in sorted(CONTENT_DIR.iterdir()) if d.is_dir()]

    if target:
        if target not in ALLOWED_EXPORTS:
            print(f"✗ '{target}' is not in ALLOWED_EXPORTS~ edit the script to add it")
            sys.exit(1)
        exports = [d for d in all_exports if d.name == target]
        if not exports:
            print(f"✗ no export named '{target}' found in content/")
            sys.exit(1)
    else:
        exports = [d for d in all_exports if d.name in ALLOWED_EXPORTS]
        skipped = [d.name for d in all_exports if d.name not in ALLOWED_EXPORTS]
        if skipped:
            print(f"  skipping  → {chr(39).join(skipped)} (not in ALLOWED_EXPORTS)")
        if not exports:
            print(f"✗ no allowed exports found in content/")
            print(f"  ALLOWED_EXPORTS = {ALLOWED_EXPORTS}")
            sys.exit(1)


    print(f"✦ petal-anamnesis :: ingest_to_db")
    print(f"  db        → {DB_PATH.relative_to(PROJECT_ROOT)}")
    print(f"  exports   → {len(exports)}\n")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")

    results = []
    for export_dir in exports:
        results.append(ingest_export(conn, export_dir))
        print()

    conn.close()

    print(f"✦ done~")
    for r in results:
        if r.get("skipped"):
            continue
        print(f"  {r['slug']}: {r['conversations']} convos, {r['messages']} messages")


if __name__ == "__main__":
    main()