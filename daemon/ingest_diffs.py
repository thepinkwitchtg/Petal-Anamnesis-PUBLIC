"""
petal-anamnesis :: ingest_diffs.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Ingests the JSON output of diff_messages.py into the queryable flag layer~

usage:
  python daemon/ingest_diffs.py                      # ingest all allowed
  python daemon/ingest_diffs.py path/to/diff.json    # ingest one specific file

idempotent~ rerunning a diff (with same baseline+target) purges the prior
diff_run and re-inserts everything fresh.

design notes:
  • ALLOWED_DIFF_TARGETS filters out diffs that lily considers unreliable
    (e.g. the Feb 2026 export, where post-Jan manual deletions made fake
    orphans). only diff results against allowed targets are ingested.
  • we ingest in transaction-per-diff-file so a failure halfway through
    rolls back cleanly
"""

import json
import sqlite3
import sys
from pathlib import Path
import os
from dotenv import load_dotenv
load_dotenv()


# ── paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
DIFF_DIR     = PROJECT_ROOT / "reports" / "diff"
DB_PATH      = PROJECT_ROOT / "db" / "petal-anamnesis.db"


# ── allowlist: only ingest diffs whose target is in this list ─────────────────
# the Feb 2026 export has unreliable orphan signal due to manual deletions~
# add target slugs here to enable; remove to disable
ALLOWED_DIFF_TARGETS = [
    x.strip() for x in os.getenv("TARGET_EXPORT", "").split(",") if x.strip()
]


# ── slug helpers ──────────────────────────────────────────────────────────────
def export_slug(name: str) -> str:
    return name.replace(" ", "_").replace("-", "_")


# ── load + ingest one diff json ───────────────────────────────────────────────
def ingest_diff_file(conn: sqlite3.Connection, path: Path) -> dict:
    """Ingests a single diff JSON file. Returns stats dict."""
    print(f"  ◈ {path.name}")
    with path.open(encoding="utf-8") as f:
        data = json.load(f)

    meta = data.get("meta", {})
    baseline_export = export_slug(meta.get("baseline_export", ""))
    target_export   = export_slug(meta.get("target_export", ""))

    print(f"    baseline  → {baseline_export}")
    print(f"    target    → {target_export}")

    if target_export not in ALLOWED_DIFF_TARGETS:
        print(f"    ⊘ target not in ALLOWED_DIFF_TARGETS, skipping")
        return {"skipped": True}

    cur = conn.cursor()

    # purge any existing diff_run for this baseline+target pair
    cur.execute("""
        DELETE FROM diff_runs WHERE baseline_export = ? AND target_export = ?
    """, (baseline_export, target_export))

    # insert the new diff_run
    cur.execute("""
        INSERT INTO diff_runs (
            baseline_export, target_export, diffed_at,
            convos_compared, convos_with_changes, convos_suspiciously_clean,
            convos_cloned, convos_baseline_only, convos_target_only,
            total_rewritten_messages, total_removed_messages, total_added_messages
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        baseline_export, target_export, meta.get("diffed_at", ""),
        meta.get("convos_compared", 0),
        meta.get("convos_with_changes", 0),
        meta.get("convos_suspiciously_clean", 0),
        meta.get("convos_cloned", 0),
        meta.get("convos_baseline_only", 0),
        meta.get("convos_target_only", 0),
        meta.get("total_rewritten_messages", 0),
        meta.get("total_removed_messages", 0),
        meta.get("total_added_messages", 0),
    ))
    diff_run_id = cur.lastrowid

    # ── conversation_diff_flags ────────────────────────────────────────
    conv_flag_rows = []
    msg_flag_rows = []

    # set of conversation IDs that are clone pairs (so we can mark them)
    # build a map: baseline_id → clone metadata
    clone_map = {}
    for cp in data.get("clone_pairs", []):
        clone_map[cp["baseline_id"]] = cp

    # all conversations that appeared in the diff (in_both + clone_pairs)
    for conv_diff in data.get("conversations", []):
        cid = conv_diff["conversation_id"]
        is_clone = bool(conv_diff.get("is_clone_pair"))

        # clone twin info either from the conv_diff itself (is_clone branch)
        # or absent for non-clone in-both conversations
        twin_cid = conv_diff.get("target_conv_id")
        clone_meta = clone_map.get(cid, {})

        conv_flag_rows.append((
            diff_run_id,
            cid,
            baseline_export,
            len(conv_diff.get("rewritten", [])),
            len(conv_diff.get("removed", [])),
            len(conv_diff.get("added", [])),
            1 if is_clone else 0,
            0,  # is_orphan_baseline (we'll set these in a second loop below)
            1 if conv_diff.get("suspiciously_clean") else 0,
            twin_cid,
            clone_meta.get("match_type"),
            clone_meta.get("fingerprint_similarity"),
            clone_meta.get("title_similarity"),
            clone_meta.get("first_msg_similarity"),
            conv_diff.get("total_changes", 0),
        ))

        # ── message-level flags ────────────────────────────────────────
        for msg in conv_diff.get("rewritten", []):
            msg_flag_rows.append((
                diff_run_id, cid, baseline_export, msg["node_id"],
                "rewritten", msg.get("role"), msg.get("model"),
                msg.get("similarity"),
                msg.get("baseline_text"), msg.get("target_text"),
                json.dumps(msg.get("diff_lines", []), ensure_ascii=False),
                msg.get("create_time"),
            ))
        for msg in conv_diff.get("removed", []):
            msg_flag_rows.append((
                diff_run_id, cid, baseline_export, msg["node_id"],
                "removed", msg.get("role"), msg.get("model"),
                None,  # no similarity for fully removed
                msg.get("text"), None, None,
                msg.get("create_time"),
            ))
        for msg in conv_diff.get("added", []):
            msg_flag_rows.append((
                diff_run_id, cid, baseline_export, msg["node_id"],
                "added", msg.get("role"), msg.get("model"),
                None,
                None, msg.get("text"), None,
                msg.get("create_time"),
            ))

    # ── orphan flags: conversations in baseline but not in target ──────
    # these are in only_in_baseline list, not in conversations[] (since they
    # have no twin to diff against). create flag rows for them.
    orphan_ids = data.get("only_in_baseline", [])
    for orphan_id in orphan_ids:
        conv_flag_rows.append((
            diff_run_id,
            orphan_id,
            baseline_export,
            0, 0, 0,        # no rewrites/removes/adds (nothing to compare)
            0,              # is_clone_pair
            1,              # is_orphan_baseline ← the badge
            0,              # is_suspiciously_clean
            None, None, None, None, None,  # no twin
            0,              # total_changes
        ))

    # bulk insert
    cur.executemany("""
        INSERT INTO conversation_diff_flags (
            diff_run_id, conversation_id, source_export,
            rewritten_count, removed_count, added_count,
            is_clone_pair, is_orphan_baseline, is_suspiciously_clean,
            twin_conversation_id, twin_match_type,
            twin_fp_similarity, twin_title_similarity, twin_first_similarity,
            total_changes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, conv_flag_rows)

    cur.executemany("""
        INSERT INTO message_diff_flags (
            diff_run_id, conversation_id, source_export, message_id,
            flag_kind, role, model, similarity,
            baseline_text, target_text, diff_lines, create_time
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, msg_flag_rows)

    # ── clone_pairs ────────────────────────────────────────────────────
    clone_rows = []
    for cp in data.get("clone_pairs", []):
        clone_rows.append((
            diff_run_id,
            cp["baseline_id"], cp["target_id"],
            baseline_export, target_export,
            cp.get("title"),
            cp.get("match_type"),
            cp.get("fingerprint_similarity"),
            cp.get("title_similarity"),
            cp.get("first_msg_similarity"),
            cp.get("combined_score"),
        ))

    cur.executemany("""
        INSERT INTO clone_pairs (
            diff_run_id,
            baseline_conversation_id, target_conversation_id,
            baseline_export, target_export,
            title, match_type,
            fingerprint_similarity, title_similarity,
            first_msg_similarity, combined_score
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, clone_rows)

    conn.commit()

    print(f"    ✓ ingested:")
    print(f"      conv flags     → {len(conv_flag_rows)}")
    print(f"      msg flags      → {len(msg_flag_rows)}")
    print(f"      clone pairs    → {len(clone_rows)}")
    print(f"      orphans flagged→ {len(orphan_ids)}")

    return {
        "diff_run_id": diff_run_id,
        "conversation_flags": len(conv_flag_rows),
        "message_flags": len(msg_flag_rows),
        "clone_pairs": len(clone_rows),
        "orphans": len(orphan_ids),
    }


# ── orchestrator ───────────────────────────────────────────────────────────────
def main():
    if not DB_PATH.exists():
        print(f"✗ db not found at {DB_PATH}")
        print(f"  run: node db/migrate.mjs")
        sys.exit(1)

    if not DIFF_DIR.exists():
        print(f"✗ {DIFF_DIR} not found")
        sys.exit(1)

    # specific file or all
    target_file = sys.argv[1] if len(sys.argv) > 1 else None

    if target_file:
        diff_files = [Path(target_file)]
        if not diff_files[0].exists():
            print(f"✗ file not found: {target_file}")
            sys.exit(1)
    else:
        # only the structured json files (skip _readable.md, _orphans_*.md, etc.)
        diff_files = sorted(
            f for f in DIFF_DIR.glob("*.json")
            if "__vs__" in f.name
        )

    if not diff_files:
        print(f"✗ no diff json files found in {DIFF_DIR}")
        sys.exit(1)

    print(f"✦ petal-anamnesis :: ingest_diffs")
    print(f"  db        → {DB_PATH.relative_to(PROJECT_ROOT)}")
    print(f"  diff json → {len(diff_files)} file(s)\n")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")

    results = []
    for f in diff_files:
        results.append(ingest_diff_file(conn, f))
        print()

    conn.close()

    ingested = [r for r in results if not r.get("skipped")]
    print(f"✦ done~")
    print(f"  ingested → {len(ingested)} diff run(s)")
    for r in ingested:
        print(f"    diff_run #{r['diff_run_id']}: "
              f"{r['conversation_flags']} conv flags, "
              f"{r['message_flags']} msg flags, "
              f"{r['clone_pairs']} clone pairs, "
              f"{r['orphans']} orphans")


if __name__ == "__main__":
    main()
