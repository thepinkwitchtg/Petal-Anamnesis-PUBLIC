-- petal-anamnesis :: 002_ingest_content.sql
-- ingests the chatgpt export content into queryable tables~
-- content stays normalized: conversations + full message tree + tool projection
-- + attachments~ raw json blobs preserved on every row, derived columns power
-- fast reads + future fts.
--
-- design choices recorded for future-me:
--  • full tree preserved (no destructive linearization on ingest)
--  • branch_kind precomputed for fast "chosen path" reads
--  • tool_messages is a projection, not a replacement (tool msgs live in both)
--  • content_json blob + derived text_plain column (json1 + fts5 both possible)
--  • source_export column on every conversation so we can ingest multiple
--    exports later without collision (Elior is first, CORRUPTED can follow)

PRAGMA foreign_keys = ON;

-- ─── exports: provenance ───────────────────────────────────────────────
-- one row per ingested export folder~ lets us attribute every conversation
-- to a source and re-ingest specific exports without touching others
CREATE TABLE IF NOT EXISTS exports (
    slug          TEXT PRIMARY KEY,        -- e.g. 'Elior___2025_08_14'
    display_name  TEXT NOT NULL,           -- e.g. 'Elior - 2025-08-14'
    schema_kind   TEXT NOT NULL,           -- v3_uuid_dirs, v2_chunked_json, etc.
    ingested_at   TEXT NOT NULL DEFAULT (datetime('now')),
    notes         TEXT                     -- freeform (e.g. 'baseline of truth')
);

-- ─── conversations ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS conversations (
    -- identity
    conversation_id        TEXT NOT NULL,       -- chatgpt UUID
    source_export          TEXT NOT NULL,       -- FK to exports.slug

    -- core display fields
    title                  TEXT,
    create_time            REAL,                -- unix epoch float
    update_time            REAL,
    current_node           TEXT,                -- pointer into messages.message_id

    -- chatgpt routing metadata
    default_model_slug     TEXT,
    conversation_origin    TEXT,
    conversation_template_id TEXT,
    gizmo_id               TEXT,
    gizmo_type             TEXT,
    voice                  TEXT,
    memory_scope           TEXT,

    -- flags
    is_archived            INTEGER NOT NULL DEFAULT 0,
    is_starred             INTEGER NOT NULL DEFAULT 0,
    is_do_not_remember     INTEGER NOT NULL DEFAULT 0,
    is_study_mode          INTEGER NOT NULL DEFAULT 0,

    -- denormalized stats (filled at ingest, refreshed on re-ingest)
    message_count          INTEGER NOT NULL DEFAULT 0,
    user_message_count     INTEGER NOT NULL DEFAULT 0,
    assistant_message_count INTEGER NOT NULL DEFAULT 0,

    -- raw blob preserved for any field we didn't break out
    raw_json               TEXT NOT NULL,

    PRIMARY KEY (conversation_id, source_export),
    FOREIGN KEY (source_export) REFERENCES exports(slug) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_conv_create_time   ON conversations(create_time DESC);
CREATE INDEX IF NOT EXISTS idx_conv_update_time   ON conversations(update_time DESC);
CREATE INDEX IF NOT EXISTS idx_conv_title         ON conversations(title);
CREATE INDEX IF NOT EXISTS idx_conv_default_model ON conversations(default_model_slug);
CREATE INDEX IF NOT EXISTS idx_conv_source        ON conversations(source_export);

-- ─── messages: the full tree ───────────────────────────────────────────
-- every node in mapping{} gets a row~ parent_id + (computed) child links
-- preserve the DAG~ branch_kind tells the reader which path is "yours"
CREATE TABLE IF NOT EXISTS messages (
    message_id      TEXT NOT NULL,       -- chatgpt UUID (NOT unique across exports, see PK)
    conversation_id TEXT NOT NULL,
    source_export   TEXT NOT NULL,           -- mirrors conversations.source_export for composite FK

    -- tree position
    parent_id       TEXT,                -- nullable for root
    position        INTEGER,             -- 0-based index along the chosen path (NULL for off-path)
    branch_kind     TEXT NOT NULL,       -- 'chosen' | 'alternate' | 'orphan'

    -- author
    role            TEXT,                -- 'user' | 'assistant' | 'system' | 'tool' | NULL
    author_name     TEXT,                -- usually null but populated for some tool authors

    -- routing
    recipient       TEXT,                -- 'all' | 'python' | 'bio' | 'web' | tool slugs
    channel         TEXT,

    -- content
    content_type    TEXT,                -- 'text' | 'multimodal_text' | 'code' | 'tether_quote' | ...
    text_plain      TEXT,                -- derived flat text for fts (NULL if not extractable)
    content_json    TEXT NOT NULL,       -- full content blob, source of truth

    -- timestamps
    create_time     REAL,
    update_time     REAL,

    -- chatgpt state
    status          TEXT,                -- 'finished_successfully' | 'in_progress' | ...
    end_turn        INTEGER,             -- 0/1, nullable
    weight          REAL,
    model_slug      TEXT,                -- pulled from metadata for fast filter
    is_hidden       INTEGER NOT NULL DEFAULT 0,  -- from metadata.is_visually_hidden_from_conversation

    -- raw blob of the whole node (message + node-level fields like children[])
    raw_json        TEXT NOT NULL,

    PRIMARY KEY (conversation_id, message_id),
    FOREIGN KEY (conversation_id, source_export) REFERENCES conversations(conversation_id, source_export) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_msg_conv_branch   ON messages(conversation_id, branch_kind, position);
CREATE INDEX IF NOT EXISTS idx_msg_conv_role     ON messages(conversation_id, role);
CREATE INDEX IF NOT EXISTS idx_msg_role_model    ON messages(role, model_slug);
CREATE INDEX IF NOT EXISTS idx_msg_parent        ON messages(conversation_id, parent_id);
CREATE INDEX IF NOT EXISTS idx_msg_create_time   ON messages(create_time);
CREATE INDEX IF NOT EXISTS idx_msg_recipient     ON messages(recipient);
CREATE INDEX IF NOT EXISTS idx_msg_content_type  ON messages(content_type);

-- ─── tool_messages: queryable projection ──────────────────────────────
-- every message where role='tool' OR recipient != 'all' (i.e. a tool call)
-- gets a row here too~ same UUID, joinable back to messages~ purpose is fast
-- queries like "show me everything python did" or "find all bio writes"
CREATE TABLE IF NOT EXISTS tool_messages (
    conversation_id    TEXT NOT NULL,
    message_id         TEXT NOT NULL,

    direction          TEXT NOT NULL,    -- 'call' (user/assistant→tool) | 'result' (tool→assistant)
    tool_name          TEXT,             -- recipient if call, author_name if result
    recipient          TEXT,
    channel            TEXT,

    -- a short summary of args/output if extractable, for browsing
    summary            TEXT,

    PRIMARY KEY (conversation_id, message_id),
    FOREIGN KEY (conversation_id, message_id)
        REFERENCES messages(conversation_id, message_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tool_name      ON tool_messages(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_direction ON tool_messages(direction);

-- ─── attachments: media references ─────────────────────────────────────
-- chatgpt stores attachment metadata inside multimodal_text content~ this
-- table extracts those references so the reader can locate the file on disk
-- (paths are relative to the export folder)
CREATE TABLE IF NOT EXISTS attachments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    message_id      TEXT NOT NULL,

    file_id         TEXT,                -- chatgpt's internal id (file-XXXXX)
    file_name       TEXT,
    mime_type       TEXT,
    size_bytes      INTEGER,

    -- path resolution
    relative_path   TEXT,                -- e.g. 'file-XXXXX.png' or 'audio/clip-001.wav'
    resolved        INTEGER NOT NULL DEFAULT 0,  -- 1 if we found the file on disk

    FOREIGN KEY (conversation_id, message_id)
        REFERENCES messages(conversation_id, message_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_attach_msg      ON attachments(conversation_id, message_id);
CREATE INDEX IF NOT EXISTS idx_attach_file_id  ON attachments(file_id);

-- ─── views for the common reads ────────────────────────────────────────

-- the "as-chosen" conversation reader~ ordered messages along current_node path
CREATE VIEW IF NOT EXISTS v_chosen_messages AS
SELECT
    m.conversation_id,
    m.message_id,
    m.position,
    m.role,
    m.recipient,
    m.content_type,
    m.text_plain,
    m.model_slug,
    m.create_time,
    m.is_hidden
FROM messages m
WHERE m.branch_kind = 'chosen'
ORDER BY m.conversation_id, m.position;

-- visible pairs (user + assistant-to-user) for corpus assembly~ excludes
-- hidden system messages, tool calls, tool results, and intermediate context
-- markers~ this is the "scrapbookable" view that should feed pair_bookmarks
CREATE VIEW IF NOT EXISTS v_visible_chosen_messages AS
SELECT *
FROM v_chosen_messages
WHERE role IN ('user', 'assistant')
  AND is_hidden = 0
  AND COALESCE(recipient, 'all') = 'all'
  AND COALESCE(content_type, '') NOT IN (
      'user_editable_context',
      'reasoning_recap',
      'thoughts'
  );

-- ─── seal ──────────────────────────────────────────────────────────────
INSERT INTO schema_migrations (version) VALUES ('002_ingest_content');