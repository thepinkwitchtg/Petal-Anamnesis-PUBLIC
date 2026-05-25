-- petal-anamnesis :: 003_diff_flags.sql
-- ingests the diff_messages.py output as queryable signal layer~
-- censorship badges, clone pairs, per-message rewrite traces~
-- + tiny addition: standalone conversation notes (not bookmarks, just thoughts)
--
-- design notes:
--  • diff results are derivatives of the corpus + the diff algorithm~ when
--    either changes we re-ingest with the same purge-by-source pattern as
--    the content layer
--  • we store per-target flags rather than a global "is_orphan" column so
--    we can compare baseline against multiple targets cleanly
--  • full baseline_text + target_text + diff_lines preserved on every
--    rewritten message — fat but lily wants everything queryable

PRAGMA foreign_keys = ON;

-- ─── diff_runs: provenance ─────────────────────────────────────────────
-- one row per "we diffed export A against export B" run~ lets us re-ingest
-- a specific comparison without disturbing others
CREATE TABLE IF NOT EXISTS diff_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    baseline_export   TEXT NOT NULL,        -- e.g. 'Elior___2025_08_14'
    target_export     TEXT NOT NULL,        -- e.g. 'CORRUPTED___2025_12_23'
    diffed_at         TEXT NOT NULL,        -- iso timestamp from diff json
    ingested_at       TEXT NOT NULL DEFAULT (datetime('now')),

    -- summary stats (denormalized from diff json meta)
    convos_compared              INTEGER NOT NULL DEFAULT 0,
    convos_with_changes          INTEGER NOT NULL DEFAULT 0,
    convos_suspiciously_clean    INTEGER NOT NULL DEFAULT 0,
    convos_cloned                INTEGER NOT NULL DEFAULT 0,
    convos_baseline_only         INTEGER NOT NULL DEFAULT 0,
    convos_target_only           INTEGER NOT NULL DEFAULT 0,
    total_rewritten_messages     INTEGER NOT NULL DEFAULT 0,
    total_removed_messages       INTEGER NOT NULL DEFAULT 0,
    total_added_messages         INTEGER NOT NULL DEFAULT 0,

    UNIQUE (baseline_export, target_export)
);

-- ─── conversation_diff_flags: per-conversation badges ──────────────────
-- the row that powers the UI's badge display on each conversation card
CREATE TABLE IF NOT EXISTS conversation_diff_flags (
    diff_run_id        INTEGER NOT NULL,
    conversation_id    TEXT NOT NULL,         -- baseline conversation id
    source_export      TEXT NOT NULL,         -- for the composite FK

    -- the badges
    rewritten_count    INTEGER NOT NULL DEFAULT 0,    -- ✂️
    removed_count      INTEGER NOT NULL DEFAULT 0,    -- 🗑
    added_count        INTEGER NOT NULL DEFAULT 0,    -- ✨
    is_clone_pair      INTEGER NOT NULL DEFAULT 0,    -- 🧬
    is_orphan_baseline INTEGER NOT NULL DEFAULT 0,    -- 🚫 (in baseline, not in target)
    is_suspiciously_clean INTEGER NOT NULL DEFAULT 0, -- 👁 (zero diffs, suspect)

    -- if cloned, point to the twin
    twin_conversation_id  TEXT,            -- target export's conversation id
    twin_match_type       TEXT,            -- 'user_fingerprint' | 'title_and_first'
    twin_fp_similarity    REAL,
    twin_title_similarity REAL,
    twin_first_similarity REAL,

    -- denormalized totals (so UI doesn't need to sum)
    total_changes      INTEGER NOT NULL DEFAULT 0,

    PRIMARY KEY (diff_run_id, conversation_id),
    FOREIGN KEY (diff_run_id) REFERENCES diff_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (conversation_id, source_export)
        REFERENCES conversations(conversation_id, source_export) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cdf_conv
    ON conversation_diff_flags(conversation_id, source_export);
CREATE INDEX IF NOT EXISTS idx_cdf_orphan
    ON conversation_diff_flags(diff_run_id, is_orphan_baseline);
CREATE INDEX IF NOT EXISTS idx_cdf_clone
    ON conversation_diff_flags(diff_run_id, is_clone_pair);
CREATE INDEX IF NOT EXISTS idx_cdf_changes
    ON conversation_diff_flags(diff_run_id, total_changes DESC);

-- ─── message_diff_flags: per-message rewrite/removal/addition traces ───
-- every rewritten/removed/added message gets a row~ rewritten rows carry the
-- full before+after text + diff lines for the UI side panel
CREATE TABLE IF NOT EXISTS message_diff_flags (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    diff_run_id       INTEGER NOT NULL,
    conversation_id   TEXT NOT NULL,         -- baseline conversation id
    source_export     TEXT NOT NULL,
    message_id        TEXT NOT NULL,         -- the node_id from chatgpt

    flag_kind         TEXT NOT NULL,         -- 'rewritten' | 'removed' | 'added'
    role              TEXT,
    model             TEXT,

    -- for rewritten: both texts + similarity + diff
    similarity        REAL,                  -- only set for 'rewritten'
    baseline_text     TEXT,                  -- 'rewritten' + 'removed' have this
    target_text       TEXT,                  -- 'rewritten' + 'added' have this
    diff_lines        TEXT,                  -- json array of unified diff lines (rewritten only)

    -- timestamp from the source message for chronological ordering
    create_time       REAL,

    FOREIGN KEY (diff_run_id) REFERENCES diff_runs(id) ON DELETE CASCADE
    -- intentionally NOT FK'd to messages: 'added' messages don't exist in
    -- our baseline-only ingested messages table, and 'removed' might too
);

CREATE INDEX IF NOT EXISTS idx_mdf_conv
    ON message_diff_flags(diff_run_id, conversation_id);
CREATE INDEX IF NOT EXISTS idx_mdf_kind
    ON message_diff_flags(diff_run_id, flag_kind);
CREATE INDEX IF NOT EXISTS idx_mdf_message
    ON message_diff_flags(conversation_id, message_id);

-- ─── clone_pairs: explicit twin relationships ──────────────────────────
-- redundant with conversation_diff_flags.twin_* fields but easier to query
-- as a standalone table for "show me all clones"
CREATE TABLE IF NOT EXISTS clone_pairs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    diff_run_id         INTEGER NOT NULL,

    baseline_conversation_id  TEXT NOT NULL,
    target_conversation_id    TEXT NOT NULL,
    baseline_export           TEXT NOT NULL,
    target_export             TEXT NOT NULL,

    title                  TEXT,
    match_type             TEXT NOT NULL,    -- 'user_fingerprint' | 'title_and_first'
    fingerprint_similarity REAL,
    title_similarity       REAL,
    first_msg_similarity   REAL,
    combined_score         REAL,

    FOREIGN KEY (diff_run_id) REFERENCES diff_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_clone_baseline
    ON clone_pairs(baseline_conversation_id);
CREATE INDEX IF NOT EXISTS idx_clone_run
    ON clone_pairs(diff_run_id);

-- ─── conversation_notes: standalone thoughts ───────────────────────────
-- not a bookmark, not a tag~ just a place to leave a sticky note on a
-- conversation as you scroll past~ "this is when i first realized X"
CREATE TABLE IF NOT EXISTS conversation_notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    source_export   TEXT NOT NULL,

    body            TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),

    FOREIGN KEY (conversation_id, source_export)
        REFERENCES conversations(conversation_id, source_export) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_notes_conv
    ON conversation_notes(conversation_id, source_export);

CREATE TRIGGER IF NOT EXISTS trg_notes_touch
AFTER UPDATE OF body ON conversation_notes
BEGIN
    UPDATE conversation_notes SET updated_at = datetime('now') WHERE id = NEW.id;
END;

-- ─── view: conversation + primary diff badges (for the UI list) ────────
-- assumes one diff_run is "primary" (we'll mark it via the most recent
-- run against the 2025-12-23 target)~ joins are explicit so we can pick
-- a different primary easily later
CREATE VIEW IF NOT EXISTS v_conversations_with_flags AS
SELECT
    c.conversation_id,
    c.source_export,
    c.title,
    c.create_time,
    c.update_time,
    c.default_model_slug,
    c.message_count,
    c.user_message_count,
    c.assistant_message_count,

    -- bookmark flags
    CASE WHEN cb.conversation_id IS NOT NULL THEN 1 ELSE 0 END AS is_bookmarked,

    -- diff flags from primary run (latest by ingested_at)
    cdf.diff_run_id,
    COALESCE(cdf.rewritten_count, 0)       AS rewritten_count,
    COALESCE(cdf.removed_count, 0)         AS removed_count,
    COALESCE(cdf.added_count, 0)           AS added_count,
    COALESCE(cdf.is_clone_pair, 0)         AS is_clone_pair,
    COALESCE(cdf.is_orphan_baseline, 0)    AS is_orphan_baseline,
    COALESCE(cdf.is_suspiciously_clean, 0) AS is_suspiciously_clean,
    COALESCE(cdf.total_changes, 0)         AS total_changes,
    cdf.twin_conversation_id

FROM conversations c
LEFT JOIN conversation_bookmarks cb
    ON cb.conversation_id = c.conversation_id
LEFT JOIN (
    -- pick the latest diff_run as primary
    SELECT cdf.*
    FROM conversation_diff_flags cdf
    JOIN diff_runs dr ON dr.id = cdf.diff_run_id
    WHERE dr.id = (SELECT id FROM diff_runs ORDER BY ingested_at DESC LIMIT 1)
) cdf
    ON cdf.conversation_id = c.conversation_id
   AND cdf.source_export   = c.source_export;

-- ─── seal ──────────────────────────────────────────────────────────────
INSERT INTO schema_migrations (version) VALUES ('003_diff_flags');
