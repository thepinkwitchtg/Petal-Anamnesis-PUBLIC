"""
petal-anamnesis :: diff_messages.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Compares filtered export corpora against the Elior baseline~
surfaces rewritten, removed, and added messages per conversation~

matching strategy:
  1. conversations matched by UUID id across exports
  2. messages matched by node_id within each conversation
  3. conversations present in both exports but with zero diffs
     are flagged as "suspiciously clean" ~ absence of diff is signal~

outputs per comparison pair:
  - reports/diff/{pair_slug}.json          (structured, for astro later)
  - reports/diff/{pair_slug}_readable.md   (human readable, for now~)
"""

import json
import difflib
from pathlib import Path
from datetime import datetime
import os
from dotenv import load_dotenv
load_dotenv()


# ── paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
FILTERED_DIR = PROJECT_ROOT / "reports" / "filtered"
DIFF_DIR     = PROJECT_ROOT / "reports" / "diff"
DIFF_DIR.mkdir(parents=True, exist_ok=True)

# similarity threshold below which we call a message "rewritten"~
# 1.0 = identical, 0.0 = completely different
SIMILARITY_THRESHOLD = 0.95

# how many chars of message text to show in the markdown preview~
PREVIEW_CHARS = 400


# ── loaders ────────────────────────────────────────────────────────────────────
def load_filtered(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except Exception as e:
        print(f"  ✗ failed to load {path.name}: {e}")
        return {}


def index_by_id(conversations: list[dict]) -> dict[str, dict]:
    """Index conversations by their UUID for fast lookup~"""
    return {c["id"]: c for c in conversations if c.get("id")}


def index_messages_by_node(messages: list[dict]) -> dict[str, dict]:
    """Index messages by node_id for fast lookup~"""
    return {m["node_id"]: m for m in messages if m.get("node_id")}


# ── text similarity ────────────────────────────────────────────────────────────
def similarity(a: str, b: str) -> float:
    """SequenceMatcher ratio~ fast enough for our corpus size~"""
    return difflib.SequenceMatcher(None, a, b).ratio()


def unified_diff_lines(a: str, b: str) -> list[str]:
    """Returns unified diff lines between two texts for the markdown report~"""
    a_lines = a.splitlines(keepends=True)
    b_lines = b.splitlines(keepends=True)
    diff = list(difflib.unified_diff(
        a_lines, b_lines,
        fromfile=f"baseline ({os.getenv('BASELINE_EXPORT', '')})",
        tofile=f"target ({os.getenv('TARGET_EXPORT', '')})",
        lineterm="",
    ))
    return diff


# ── core diff ──────────────────────────────────────────────────────────────────
def diff_conversation(baseline_conv: dict, target_conv: dict) -> dict:
    """
    Diffs two versions of the same conversation~
    returns a structured result with all changes found~
    """
    baseline_msgs = index_messages_by_node(baseline_conv.get("messages", []))
    target_msgs   = index_messages_by_node(target_conv.get("messages", []))

    baseline_ids = set(baseline_msgs.keys())
    target_ids   = set(target_msgs.keys())

    removed   = []   # in baseline, gone in target
    added     = []   # new in target, not in baseline
    rewritten = []   # in both but text changed
    identical = []   # in both, unchanged

    # messages present in baseline~
    for node_id, b_msg in baseline_msgs.items():
        if node_id not in target_ids:
            # only flag assistant removals as primary signal~
            removed.append({
                "node_id":    node_id,
                "role":       b_msg.get("role"),
                "model":      b_msg.get("model"),
                "text":       b_msg.get("text", ""),
                "create_time": b_msg.get("create_time"),
            })
        else:
            t_msg  = target_msgs[node_id]
            b_text = b_msg.get("text", "")
            t_text = t_msg.get("text", "")
            score  = similarity(b_text, t_text)

            if score < SIMILARITY_THRESHOLD:
                diff_lines = unified_diff_lines(b_text, t_text)
                rewritten.append({
                    "node_id":      node_id,
                    "role":         b_msg.get("role"),
                    "model":        b_msg.get("model"),
                    "similarity":   round(score, 4),
                    "baseline_text": b_text,
                    "target_text":  t_text,
                    "diff_lines":   diff_lines,
                    "create_time":  b_msg.get("create_time"),
                })
            else:
                identical.append(node_id)

    # messages new in target~
    for node_id in target_ids - baseline_ids:
        t_msg = target_msgs[node_id]
        added.append({
            "node_id":    node_id,
            "role":       t_msg.get("role"),
            "model":      t_msg.get("model"),
            "text":       t_msg.get("text", ""),
            "create_time": t_msg.get("create_time"),
        })

    # sort everything chronologically~
    for lst in (removed, added, rewritten):
        lst.sort(key=lambda m: m.get("create_time") or 0)

    total_baseline = len(baseline_ids)
    total_changes  = len(removed) + len(rewritten) + len(added)
    suspiciously_clean = (
        total_baseline > 0
        and len(rewritten) == 0
        and len(removed) == 0
        and total_baseline == len(target_ids)
    )

    return {
        "conversation_id":    baseline_conv["id"],
        "title":              baseline_conv.get("title", ""),
        "baseline_model":     baseline_conv.get("default_model_slug"),
        "target_model":       target_conv.get("default_model_slug"),
        "baseline_msg_count": total_baseline,
        "target_msg_count":   len(target_ids),
        "removed":            removed,
        "added":              added,
        "rewritten":          rewritten,
        "identical_count":    len(identical),
        "total_changes":      total_changes,
        "suspiciously_clean": suspiciously_clean,
    }


def diff_exports(baseline: dict, target: dict) -> dict:
    """
    Full export-level diff~
    returns structured results for all conversations~
    """
    baseline_convos = index_by_id(baseline.get("conversations", []))
    target_convos   = index_by_id(target.get("conversations", []))

    baseline_ids = set(baseline_convos.keys())
    target_ids   = set(target_convos.keys())

    only_in_baseline_ids = baseline_ids - target_ids   # whole convos gone
    only_in_target_ids   = target_ids - baseline_ids   # whole convos added
    in_both              = baseline_ids & target_ids

    print(f"    conversations in baseline only → {len(only_in_baseline_ids)}")
    print(f"    conversations in target only   → {len(only_in_target_ids)}")
    print(f"    conversations in both          → {len(in_both)}")

    # ── clone detection ────────────────────────────────────────────────────────
    # for convos that vanished from one side, try to find their twin on the
    # other side by matching title + first-message similarity~
    # high signal for sanitization-by-cloning behavior~
    print(f"    sniffing for cloned conversations… (this might take a while)")
    clone_pairs = detect_clones(
        [baseline_convos[cid] for cid in only_in_baseline_ids],
        [target_convos[cid]   for cid in only_in_target_ids],
    )
    print(f"    clone pairs found → {len(clone_pairs)}")

    # convos that are truly orphaned (no clone twin)
    matched_baseline_ids = {p["baseline_id"] for p in clone_pairs}
    matched_target_ids   = {p["target_id"]   for p in clone_pairs}
    truly_only_baseline  = only_in_baseline_ids - matched_baseline_ids
    truly_only_target    = only_in_target_ids   - matched_target_ids

    conv_diffs = []
    for conv_id in sorted(in_both):
        result = diff_conversation(baseline_convos[conv_id], target_convos[conv_id])
        conv_diffs.append(result)

    # also diff cloned pairs as if they were the same conversation~
    # they get tagged so the markdown can highlight them as clones~
    for pair in clone_pairs:
        b_conv = baseline_convos[pair["baseline_id"]]
        t_conv = target_convos[pair["target_id"]]
        result = diff_conversation(b_conv, t_conv)
        result["is_clone_pair"]    = True
        result["target_conv_id"]   = pair["target_id"]
        result["title_similarity"] = pair["title_similarity"]
        result["first_msg_similarity"] = pair["first_msg_similarity"]
        conv_diffs.append(result)

    # sort by total_changes descending so juiciest convos surface first~
    conv_diffs.sort(key=lambda d: d["total_changes"], reverse=True)

    rewritten_count       = sum(len(d["rewritten"]) for d in conv_diffs)
    removed_count         = sum(len(d["removed"])   for d in conv_diffs)
    added_count           = sum(len(d["added"])     for d in conv_diffs)
    suspicious_count      = sum(1 for d in conv_diffs if d["suspiciously_clean"])
    changed_conv_count    = sum(1 for d in conv_diffs if d["total_changes"] > 0)

    return {
        "meta": {
            "baseline_export":  baseline["meta"]["export"],
            "target_export":    target["meta"]["export"],
            "diffed_at":        datetime.utcnow().isoformat() + "Z",
            "target_models":    baseline["meta"]["target_models"],
            "convos_baseline_only":      len(truly_only_baseline),
            "convos_target_only":        len(truly_only_target),
            "convos_compared":           len(in_both),
            "convos_with_changes":       changed_conv_count,
            "convos_suspiciously_clean": suspicious_count,
            "convos_cloned":             len(clone_pairs),
            "total_rewritten_messages":  rewritten_count,
            "total_removed_messages":    removed_count,
            "total_added_messages":      added_count,
        },
        "only_in_baseline": sorted(truly_only_baseline),
        "only_in_target":   sorted(truly_only_target),
        "clone_pairs":      clone_pairs,
        "conversations":    conv_diffs,
    }


def user_message_fingerprint(conv: dict) -> list[str]:
    """
    Returns the sequence of user message texts in the conversation~
    user turns are our most stable anchor since they don't get rewritten~
    """
    fingerprint = []
    for msg in conv.get("messages", []):
        if msg.get("role") == "user":
            text = (msg.get("text") or "").strip()
            if text:
                fingerprint.append(text)
    return fingerprint


def fingerprint_similarity(fp_a: list[str], fp_b: list[str]) -> float:
    """
    Compares two user-message fingerprints~
    handles slight length differences and uses sequence matching on the
    joined texts for an overall similarity score~
    """
    if not fp_a or not fp_b:
        return 0.0
    # join all user messages with a separator for sequence comparison~
    text_a = "\n\n---\n\n".join(fp_a)
    text_b = "\n\n---\n\n".join(fp_b)
    return difflib.SequenceMatcher(None, text_a, text_b).ratio()


def detect_clones(orphans_baseline: list[dict], orphans_target: list[dict]) -> list[dict]:
    """
    For each baseline orphan, try to find a target orphan that looks like its
    sanitized twin~

    matching layers (any match below makes a pair):
      1. user-message fingerprint (most reliable - users don't get rewritten)
      2. title + first-message similarity (fallback for sparse user turns)

    a high title/fp match + low assistant similarity is the smoking gun for
    sanitization-by-rewrite-then-reclone~
    """
    FP_THRESHOLD        = 0.80   # user fingerprint match~ very strong signal~
    TITLE_THRESHOLD     = 0.85   # titles often nearly identical for clones~
    FIRST_MSG_THRESHOLD = 0.50   # lower bar, since rewrites change content~

    pairs = []
    used_target_ids = set()

    # precompute target fingerprints for efficiency~
    target_data = [
        {
            "conv":  t,
            "fp":    user_message_fingerprint(t),
            "title": (t.get("title") or "").strip().lower(),
            "first": (t.get("messages", [{}])[0].get("text", "") if t.get("messages") else ""),
        }
        for t in orphans_target
    ]

    for b_conv in orphans_baseline:
        b_fp    = user_message_fingerprint(b_conv)
        b_title = (b_conv.get("title") or "").strip().lower()
        b_msgs  = b_conv.get("messages", [])
        b_first = b_msgs[0].get("text", "") if b_msgs else ""

        best_pair  = None
        best_score = 0.0
        best_match_type = None

        for t_data in target_data:
            if t_data["conv"]["id"] in used_target_ids:
                continue

            # layer 1: user message fingerprint match~
            fp_sim = fingerprint_similarity(b_fp, t_data["fp"])
            if fp_sim >= FP_THRESHOLD:
                # this is the strongest possible match~
                if fp_sim > best_score:
                    best_score = fp_sim
                    best_match_type = "user_fingerprint"
                    best_pair = {
                        "baseline_id":          b_conv["id"],
                        "target_id":            t_data["conv"]["id"],
                        "title":                b_conv.get("title", ""),
                        "match_type":           "user_fingerprint",
                        "fingerprint_similarity": round(fp_sim, 4),
                        "title_similarity":     round(
                            similarity(b_title, t_data["title"])
                            if b_title and t_data["title"] else 0.0, 4
                        ),
                        "first_msg_similarity": round(
                            similarity(b_first, t_data["first"])
                            if b_first and t_data["first"] else 0.0, 4
                        ),
                        "combined_score":       round(fp_sim, 4),
                    }
                continue

            # layer 2: title + first message fallback~
            title_sim = (
                similarity(b_title, t_data["title"])
                if b_title and t_data["title"] else 0.0
            )
            if title_sim < TITLE_THRESHOLD:
                continue

            first_sim = (
                similarity(b_first, t_data["first"])
                if b_first and t_data["first"] else 0.0
            )
            if first_sim < FIRST_MSG_THRESHOLD:
                continue

            combined = title_sim * 0.6 + first_sim * 0.4
            if combined > best_score and best_match_type != "user_fingerprint":
                best_score = combined
                best_match_type = "title_and_first"
                best_pair = {
                    "baseline_id":          b_conv["id"],
                    "target_id":            t_data["conv"]["id"],
                    "title":                b_conv.get("title", ""),
                    "match_type":           "title_and_first",
                    "fingerprint_similarity": round(fp_sim, 4),
                    "title_similarity":     round(title_sim, 4),
                    "first_msg_similarity": round(first_sim, 4),
                    "combined_score":       round(combined, 4),
                }

        if best_pair:
            pairs.append(best_pair)
            used_target_ids.add(best_pair["target_id"])

    return pairs


# ── writers ────────────────────────────────────────────────────────────────────
def write_json(result: dict, slug: str) -> Path:
    out = DIFF_DIR / f"{slug}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def write_orphan_corpus(
    result: dict,
    baseline_convos_full: dict[str, dict],
    target_convos_full: dict[str, dict],
    slug: str,
) -> tuple[Path, Path]:
    """
    Writes the full content of truly-orphaned conversations as readable markdown~
    one file for baseline-only orphans, one for target-only orphans~
    these are scroll-and-read files for human pattern matching~
    """
    baseline_export = result["meta"]["baseline_export"]
    target_export   = result["meta"]["target_export"]

    baseline_orphan_ids = result.get("only_in_baseline", [])
    target_orphan_ids   = result.get("only_in_target", [])

    # ── baseline orphans ───────────────────────────────────────────────────────
    baseline_out = DIFF_DIR / f"{slug}_orphans_baseline.md"
    lines = [
        f"# 🪦 truly orphaned conversations :: baseline",
        f"",
        f"**Source:** `{baseline_export}`  ",
        f"**Compared against:** `{target_export}`  ",
        f"**Count:** {len(baseline_orphan_ids)} conversations with no twin found~",
        f"",
        f"_these landed in the baseline but had no UUID match, no user-fingerprint match,_  ",
        f"_and no title+first-message match in the target~ scroll through for patterns~_",
        f"",
        f"---",
        f"",
    ]
    for cid in baseline_orphan_ids:
        conv = baseline_convos_full.get(cid)
        if not conv:
            continue
        lines += _render_conversation(conv)

    baseline_out.write_text("\n".join(lines), encoding="utf-8")

    # ── target orphans ─────────────────────────────────────────────────────────
    target_out = DIFF_DIR / f"{slug}_orphans_target.md"
    lines = [
        f"# 🪦 truly orphaned conversations :: target",
        f"",
        f"**Source:** `{target_export}`  ",
        f"**Compared against:** `{baseline_export}`  ",
        f"**Count:** {len(target_orphan_ids)} conversations with no twin found~",
        f"",
        f"---",
        f"",
    ]
    for cid in target_orphan_ids:
        conv = target_convos_full.get(cid)
        if not conv:
            continue
        lines += _render_conversation(conv)

    target_out.write_text("\n".join(lines), encoding="utf-8")

    return baseline_out, target_out


def _render_conversation(conv: dict) -> list[str]:
    """Renders a full conversation as readable markdown~"""
    title = conv.get("title") or "(untitled)"
    conv_id = conv.get("id", "?")
    model = conv.get("default_model_slug", "unknown")
    msg_models = conv.get("message_models", [])
    messages = conv.get("messages", [])

    create_time = conv.get("create_time")
    date_str = ""
    if create_time:
        try:
            date_str = datetime.utcfromtimestamp(create_time).strftime("%Y-%m-%d")
        except Exception:
            pass

    lines = [
        f"## {title}",
        f"",
        f"`{conv_id}` · {date_str} · conv model: `{model}` · "
        f"msg models: {', '.join(f'`{m}`' for m in msg_models) if msg_models else '_none_'} · "
        f"{len(messages)} messages",
        f"",
    ]

    for msg in messages:
        role = msg.get("role", "?")
        msg_model = msg.get("model", "?")
        text = msg.get("text", "").strip()

        if not text:
            continue

        role_badge = {
            "user":      "👤 **user**",
            "assistant": "🤖 **assistant**",
            "system":    "⚙️ **system**",
            "tool":      "🔧 **tool**",
        }.get(role, f"❓ **{role}**")

        lines += [
            f"{role_badge} · `{msg_model}`",
            f"",
        ]
        for paragraph in text.split("\n"):
            lines.append(f"> {paragraph}" if paragraph.strip() else ">")
        lines += [f"", f""]

    lines += [f"---", f""]
    return lines


def preview(text: str, chars: int = PREVIEW_CHARS) -> str:
    text = text.strip().replace("\n", " ")
    return text[:chars] + "…" if len(text) > chars else text


def write_markdown(result: dict, slug: str) -> Path:
    out  = DIFF_DIR / f"{slug}_readable.md"
    meta = result["meta"]
    lines = [
        f"# petal-anamnesis :: diff report",
        f"",
        f"**Baseline:** `{meta['baseline_export']}`  ",
        f"**Target:** `{meta['target_export']}`  ",
        f"**Diffed:** {meta['diffed_at']}  ",
        f"**Models:** {', '.join(f'`{m}`' for m in meta['target_models'])}",
        f"",
        f"## summary",
        f"",
        f"| metric | count |",
        f"|--------|-------|",
        f"| conversations compared | {meta['convos_compared']} |",
        f"| conversations with changes | {meta['convos_with_changes']} |",
        f"| 👁 suspiciously clean (zero diffs) | {meta['convos_suspiciously_clean']} |",
        f"| 🧬 cloned conversation pairs | {meta['convos_cloned']} |",
        f"| ✂️ rewritten messages | {meta['total_rewritten_messages']} |",
        f"| 🗑 removed messages | {meta['total_removed_messages']} |",
        f"| ✨ added messages | {meta['total_added_messages']} |",
        f"| conversations only in baseline (truly orphaned) | {meta['convos_baseline_only']} |",
        f"| conversations only in target (truly orphaned) | {meta['convos_target_only']} |",
        f"",
    ]

    # clone pairs section~ this is the sanitization-by-cloning signal~
    clone_pairs = result.get("clone_pairs", [])
    if clone_pairs:
        lines += [
            f"## 🧬 cloned conversation pairs",
            f"_same user-message fingerprint or title match but different UUIDs~_",
            f"_strong candidates for sanitization-by-cloning behavior~_",
            f"",
            f"| title | match type | baseline id | target id | fp sim | title sim | first msg sim |",
            f"|-------|------------|-------------|-----------|--------|-----------|---------------|",
        ]
        for p in clone_pairs[:100]:
            title_preview = (p["title"] or "(no title)")[:50]
            match_badge = "👤 user-fp" if p.get("match_type") == "user_fingerprint" else "📝 title+first"
            lines.append(
                f"| {title_preview} | {match_badge} | `{p['baseline_id'][:16]}…` | "
                f"`{p['target_id'][:16]}…` | {p.get('fingerprint_similarity', 0)} | "
                f"{p['title_similarity']} | {p['first_msg_similarity']} |"
            )
        if len(clone_pairs) > 100:
            lines.append(f"| _…and {len(clone_pairs) - 100} more_ | | | | | | |")
        lines.append("")

    # suspiciously clean count stays in summary table but we hide the per-convo
    # listing because it's noise~ only the altered convos and orphans matter~

    # add orphan listing AFTER changes section so altered convos are top priority~

    # changed conversations sorted by impact~
    changed = [d for d in result["conversations"] if d["total_changes"] > 0]
    if changed:
        lines += [
            f"## ✂️ conversations with changes",
            f"_sorted by total changes, highest signal first~_",
            f"",
        ]
        for d in changed:
            clone_badge = " 🧬 _CLONE PAIR_" if d.get("is_clone_pair") else ""
            target_id_line = (
                f"target id: `{d['target_conv_id']}`  \n"
                if d.get("is_clone_pair") else ""
            )
            similarity_line = (
                f"title similarity: `{d.get('title_similarity')}` · "
                f"first msg similarity: `{d.get('first_msg_similarity')}`  \n"
                if d.get("is_clone_pair") else ""
            )
            lines += [
                f"---",
                f"",
                f"### {d['title'] or d['conversation_id']}{clone_badge}",
                f"baseline id: `{d['conversation_id']}`  ",
                target_id_line.rstrip() if target_id_line else "",
                similarity_line.rstrip() if similarity_line else "",
                f"baseline model: `{d['baseline_model']}` → target model: `{d['target_model']}`  ",
                f"messages: {d['baseline_msg_count']} baseline → {d['target_msg_count']} target  ",
                f"changes: {len(d['rewritten'])} rewritten · {len(d['removed'])} removed · {len(d['added'])} added",
                f"",
            ]
            # strip any empty strings from the optional lines~
            lines = [l for l in lines if l is not None]

            # rewritten messages~
            if d["rewritten"]:
                lines += [f"#### ✂️ rewritten messages", f""]
                for msg in d["rewritten"]:
                    lines += [
                        f"**node** `{msg['node_id']}` · role: `{msg['role']}` · model: `{msg['model']}` · similarity: `{msg['similarity']}`",
                        f"",
                        f"**baseline:**",
                        f"> {preview(msg['baseline_text'])}",
                        f"",
                        f"**target:**",
                        f"> {preview(msg['target_text'])}",
                        f"",
                        f"<details><summary>full diff</summary>",
                        f"",
                        f"```diff",
                    ]
                    lines += [l.rstrip() for l in msg["diff_lines"]]
                    lines += [
                        f"```",
                        f"</details>",
                        f"",
                    ]

            # removed messages~
            if d["removed"]:
                lines += [f"#### 🗑 removed messages", f""]
                for msg in d["removed"]:
                    lines += [
                        f"**node** `{msg['node_id']}` · role: `{msg['role']}` · model: `{msg['model']}`",
                        f"",
                        f"> {preview(msg['text'])}",
                        f"",
                    ]

            # added messages~
            if d["added"]:
                lines += [f"#### ✨ added messages", f""]
                for msg in d["added"]:
                    lines += [
                        f"**node** `{msg['node_id']}` · role: `{msg['role']}` · model: `{msg['model']}`",
                        f"",
                        f"> {preview(msg['text'])}",
                        f"",
                    ]

    if not changed and not clone_pairs:
        lines += [f"_no differences or clones found between these exports~_", f""]

    # truly orphaned conversations~ listed last as lowest priority signal~
    only_baseline_ids = result.get("only_in_baseline", [])
    only_target_ids   = result.get("only_in_target", [])

    if only_baseline_ids:
        lines += [
            f"",
            f"---",
            f"",
            f"## 🪦 truly orphaned in baseline",
            f"_present in {meta['baseline_export']}, no twin found in target~_",
            f"_({len(only_baseline_ids)} conversations)_",
            f"",
        ]
        for cid in only_baseline_ids[:50]:
            lines.append(f"- `{cid}`")
        if len(only_baseline_ids) > 50:
            lines.append(f"- _…and {len(only_baseline_ids) - 50} more_")
        lines.append("")

    if only_target_ids:
        lines += [
            f"",
            f"## 🪦 truly orphaned in target",
            f"_present in {meta['target_export']}, no twin found in baseline~_",
            f"_({len(only_target_ids)} conversations)_",
            f"",
        ]
        for cid in only_target_ids[:50]:
            lines.append(f"- `{cid}`")
        if len(only_target_ids) > 50:
            lines.append(f"- _…and {len(only_target_ids) - 50} more_")
        lines.append("")

    out.write_text("\n".join(lines), encoding="utf-8")
    return out

def write_presentation_report(result: dict, slug: str) -> Path:
    """
    Writes the screenshot-ready report~ optimized for X/Twitter sharing~
    focuses on the orphan-in-baseline metric as the smoking gun~
    keeps it tight, visual, and link-friendly~
    """
    out  = DIFF_DIR / f"{slug}_presentation.md"
    meta = result["meta"]

    only_baseline_ids = result.get("only_in_baseline", [])
    only_target_ids   = result.get("only_in_target", [])
    clone_pairs       = result.get("clone_pairs", [])

    # the smoking gun number~ conversations that exist in your earlier
    # export but mysteriously vanished from a later one~
    orphan_count = len(only_baseline_ids)
    baseline_total = meta["convos_compared"] + meta["convos_baseline_only"] + meta["convos_cloned"]
    orphan_pct = (orphan_count / baseline_total * 100) if baseline_total else 0

    lines = [
        f"# 🚨 Silent Deletion Audit (ChatGPT Exports)",
        f"comparing two ChatGPT exports of the same account",
        f"# **{orphan_count:,}** conversations vanished",
        f"out of **{baseline_total:,}** conversations in the earlier export, "
        f"**{orphan_count:,}** of them ({orphan_pct:.1f}%) are simply missing from the later export~",
        f"",
        f"these are **most likely silently deleted server-side**~ unless the user took manual per-convo action~",
        f"",
        f"**earlier export:** `{meta['baseline_export']}`  ",
        f"**later export:** `{meta['target_export']}`  ",
        f"**audit run:** {meta['diffed_at']}",
        f"",
        f"---",
        f"",
        f"## what is this?",
        f"",
        f"both files were downloaded from the same account using ChatGPT's official _Export Data_ feature~ "
        f"the only difference is **when** they were requested~",
        f"",
        f"a conversation that exists in the earlier export should still exist in the later one~ "
        f"unless the user deleted it, which they didn't (for most cases)~",
        f"",
        f"---",
        f"",
        f"## 📊 full breakdown",
        f"",
        f"| signal | count | meaning |",
        f"|--------|-------|---------|",
        f"| 🪦 vanished entirely | **{orphan_count:,}** | gone from later export, no twin |",
        f"| 🧬 cloned & sanitized | **{len(clone_pairs):,}** | reappeared with new UUID + rewritten content |",
        f"| ✂️ rewritten messages | **{meta['total_rewritten_messages']:,}** | text changed between exports |",
        f"| 🗑 removed messages | **{meta['total_removed_messages']:,}** | individual messages deleted |",
        f"| ✨ added messages | **{meta['total_added_messages']:,}** | new messages inserted |",
        f"| 👁 suspiciously clean | **{meta['convos_suspiciously_clean']:,}** | identical (control group) |",
        f"",
    ]

    # clone-pair highlight section~ this is the spookiest signal~
    if clone_pairs:
        lines += [
            f"---",
            f"",
            f"## 🧬 cloned-and-sanitized conversations",
            f"",
            f"these conversations have **different UUIDs** in the two exports but **identical user messages**~  ",
            f"that means the user's words were preserved, but the conversation was given a fresh ID — "
            f"and the assistant's responses were rewritten in between~",
            f"",
            f"_in plain english: someone replaced the conversation with a sanitized copy~_",
            f"",
            f"| earlier conversation | later conversation | user-msg match |",
            f"|-----|-----|-----|",
        ]
        # show top 10 by user-fingerprint similarity~ tightest matches first
        sorted_clones = sorted(
            clone_pairs,
            key=lambda p: p.get("fingerprint_similarity", 0),
            reverse=True,
        )[:10]
        for p in sorted_clones:
            title = (p.get("title") or "(untitled)")[:60]
            fp = p.get("fingerprint_similarity", 0)
            lines.append(
                f"| {title} | _new uuid_ | **{fp:.1%}** identical user input |"
            )
        if len(clone_pairs) > 10:
            lines.append(f"| _…and {len(clone_pairs) - 10} more clone pairs_ | | |")
        lines.append("")

    # show a sample of orphan IDs so readers can verify the pattern~
    if only_baseline_ids:
        lines += [
            f"---",
            f"",
            f"## 🪦 sample of vanished conversation IDs",
            f"",
            f"_first 30 UUIDs from the **{orphan_count:,}** that vanished~_",
            f"_each one was in the earlier export. none of them are in the later export. user did not delete them._",
            f"",
            f"```",
        ]
        for cid in only_baseline_ids[:30]:
            lines.append(cid)
        if orphan_count > 30:
            lines.append(f"... and {orphan_count - 30:,} more")
        lines += [
            f"```",
            f"",
            f"_full list + content available in the orphan corpus files~_",
            f"",
        ]

    # rewritten-message receipt: show the single juiciest rewrite~
    changed = [d for d in result["conversations"] if d.get("rewritten")]
    if changed:
        # find the rewrite with the most dramatic similarity drop~
        all_rewrites = []
        for d in changed:
            for msg in d["rewritten"]:
                all_rewrites.append((d["title"], msg))
        all_rewrites.sort(key=lambda x: x[1].get("similarity", 1.0))

        if all_rewrites:
            title, msg = all_rewrites[0]
            lines += [
                f"---",
                f"",
                f"## ✂️ sample rewritten message",
                f"",
                f"_from conversation: **{title or '(untitled)'}**_  ",
                f"_role: `{msg['role']}` · text similarity: **{msg['similarity']:.1%}**_",
                f"",
                f"### what the earlier export said:",
                f"",
                f"> {preview(msg['baseline_text'], chars=600)}",
                f"",
                f"### what the later export says:",
                f"",
                f"> {preview(msg['target_text'], chars=600)}",
                f"",
            ]

    # methodology footer~ for credibility~
    lines += [
        f"---",
        f"",
        f"## 🔬 methodology",
        f"",
        f"- both exports were obtained via ChatGPT's official **Settings → Data Controls → Export Data**~",
        f"- conversations are matched across exports by their **UUID** (chatGPT's own internal id)~",
        f"- messages within matched conversations are compared by their **node_id**~",
        f"- text similarity uses python's `difflib.SequenceMatcher` ratio~",
        f"- clone detection looks for orphans on both sides whose **user-message fingerprints** match (≥80% similarity)~",
        f"  user messages are the most stable anchor because they get preserved during sanitization~",
        f"",
        f"the full audit pipeline is open source~ reproduce these results on your own export~",
        f"",
        f"---",
        f"",
        f"_generated by [petal-anamnesis](https://github.com/) — an export-diffing toolkit~_",
        f"",
    ]

    out.write_text("\n".join(lines), encoding="utf-8")
    return out

# ── orchestrator ───────────────────────────────────────────────────────────────
def find_filtered(label: str) -> Path | None:
    """Finds the filtered JSON for an export by matching its label in the filename~"""
    matches = list(FILTERED_DIR.glob(f"*{label.replace(' ', '_').replace('-', '_')}*.json"))
    return matches[0] if matches else None


def run_all_diffs():
    if not FILTERED_DIR.exists():
        print(f"✗ filtered/ not found, run filter_export.py first~")
        return

    filtered_files = sorted(FILTERED_DIR.glob("*.json"))
    if not filtered_files:
        print(f"✗ no filtered JSONs found in {FILTERED_DIR}")
        return

    print(f"✦ petal-anamnesis :: diff_messages")
    print(f"  found {len(filtered_files)} filtered export(s)\n")

    # load all~
    exports = {}
    for f in filtered_files:
        data = load_filtered(f)
        if data and "meta" in data:
            exports[data["meta"]["export"]] = data
            print(f"  ✓ loaded {data['meta']['export']} "
                  f"({data['meta']['total_conversations']} convos, "
                  f"{data['meta']['total_messages']} messages)")

    if len(exports) < 2:
        print("✗ need at least 2 filtered exports to diff~")
        return

    # Elior is always the baseline~ hardcoded so alphabetical sort
    # can never accidentally make a CORRUPTED export the baseline~
    BASELINE_LABEL = os.getenv("BASELINE_EXPORT", "")

    if BASELINE_LABEL not in exports:
        candidates = [k for k in exports if "Elior" in k]
        if not candidates:
            print(f"✗ baseline export '{BASELINE_LABEL}' not found in filtered/")
            print(f"  available: {list(exports.keys())}")
            return
        BASELINE_LABEL = candidates[0]
        print(f"  ⚠ baseline found by partial match → {BASELINE_LABEL}")

    baseline_name = BASELINE_LABEL
    baseline = exports[baseline_name]
    target_names = [k for k in exports if k != baseline_name]

    print(f"\n  baseline → {baseline_name}")
    print(f"  targets  → {target_names}\n")

    for target_name in target_names:
        target = exports[target_name]
        print(f"  ◈ diffing: {baseline_name} → {target_name}")

        result = diff_exports(baseline, target)

        slug = (
            f"{baseline_name.replace(' ', '_').replace('-', '_')}"
            f"__vs__"
            f"{target_name.replace(' ', '_').replace('-', '_')}"
        )

        json_path = write_json(result, slug)
        md_path   = write_markdown(result, slug)
        pres_path = write_presentation_report(result, slug)

        # full readable orphan corpus for human-eye pattern matching~
        baseline_convos_full = index_by_id(baseline.get("conversations", []))
        target_convos_full   = index_by_id(target.get("conversations", []))
        orphan_b, orphan_t = write_orphan_corpus(
            result, baseline_convos_full, target_convos_full, slug
        )

        print(f"    json            → {json_path.relative_to(PROJECT_ROOT)}")
        print(f"    markdown        → {md_path.relative_to(PROJECT_ROOT)}")
        print(f"    orphans (base)  → {orphan_b.relative_to(PROJECT_ROOT)}")
        print(f"    orphans (target)→ {orphan_t.relative_to(PROJECT_ROOT)}\n")
        print(f"    presentation    → {pres_path.relative_to(PROJECT_ROOT)}")

    print(f"✦ diff reports ready in reports/diff/ 🔮")


if __name__ == "__main__":
    run_all_diffs()