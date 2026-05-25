-- petal-anamnesis :: 001_init.sql
-- foundational schema for the curation layer~
-- content (conversations + messages) lives in reports/filtered/*.json
-- this db only carries the curatorial overlay: bookmarks, tags, corpora, notes

PRAGMA foreign_keys = ON;

-- ─── migrations bookkeeping ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─── bookmarks: conversation-level ─────────────────────────────────────
-- the "i wanna come back to this whole thread" mark
CREATE TABLE IF NOT EXISTS conversation_bookmarks (
    conversation_id  TEXT PRIMARY KEY,    -- chatgpt UUID
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    note             TEXT                 -- optional freeform why-i-saved
);

-- ─── bookmarks: pair-level ─────────────────────────────────────────────
-- the corpus-bearing mark~ pair = user msg + its assistant response
-- the user_message_id is the anchor (assistant_message_id derived at read time
-- by walking the next assistant node in the JSON)
CREATE TABLE IF NOT EXISTS pair_bookmarks (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id       TEXT NOT NULL,
    user_message_id       TEXT NOT NULL,
    assistant_message_id  TEXT,           -- nullable: we cache it but can rederive
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    note                  TEXT,
    UNIQUE(conversation_id, user_message_id)
);

CREATE INDEX IF NOT EXISTS idx_pair_bookmarks_conv
    ON pair_bookmarks(conversation_id);

-- ─── tags: normalized + dedup-by-canonical-name ────────────────────────
-- canonical_name is the lowercased+trimmed dedup key
-- display_name is what lily typed (preserves caps, emoji, etc.)
-- emoji is optional decoration extracted/assigned for ui badges
CREATE TABLE IF NOT EXISTS tags (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name  TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    emoji           TEXT,
    color           TEXT,                  -- optional hex/rgba for ui swatch
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    use_count       INTEGER NOT NULL DEFAULT 0  -- denormalized for sort-by-popularity
);

-- ─── tag joins: pair ↔ tag (many-to-many) ──────────────────────────────
CREATE TABLE IF NOT EXISTS pair_bookmark_tags (
    pair_bookmark_id  INTEGER NOT NULL,
    tag_id            INTEGER NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (pair_bookmark_id, tag_id),
    FOREIGN KEY (pair_bookmark_id) REFERENCES pair_bookmarks(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id)           REFERENCES tags(id)           ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_pair_tags_tag ON pair_bookmark_tags(tag_id);

-- ─── tag joins: conversation ↔ tag (many-to-many) ──────────────────────
-- same dialect for whole-conversation tagging (vibes, themes)
CREATE TABLE IF NOT EXISTS conversation_bookmark_tags (
    conversation_id  TEXT NOT NULL,
    tag_id           INTEGER NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (conversation_id, tag_id),
    FOREIGN KEY (conversation_id) REFERENCES conversation_bookmarks(conversation_id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id)          REFERENCES tags(id)                                ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_conv_tags_tag ON conversation_bookmark_tags(tag_id);

-- ─── corpora: named collections ────────────────────────────────────────
-- a corpus is a named selection of pair_bookmarks (export-ready)
CREATE TABLE IF NOT EXISTS corpora (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    slug           TEXT NOT NULL UNIQUE,   -- url-safe identifier
    name           TEXT NOT NULL,          -- display name
    description    TEXT,
    kind           TEXT,                   -- freeform: 'calibration', 'gen_prompts', 'product_pages', etc.
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─── corpus membership: ordered pair_bookmarks ─────────────────────────
-- position lets lily reorder pairs within a corpus for export sequencing
CREATE TABLE IF NOT EXISTS corpus_pairs (
    corpus_id         INTEGER NOT NULL,
    pair_bookmark_id  INTEGER NOT NULL,
    position          INTEGER NOT NULL,
    added_at          TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (corpus_id, pair_bookmark_id),
    FOREIGN KEY (corpus_id)        REFERENCES corpora(id)        ON DELETE CASCADE,
    FOREIGN KEY (pair_bookmark_id) REFERENCES pair_bookmarks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_corpus_pairs_pos
    ON corpus_pairs(corpus_id, position);

-- ─── tag use_count triggers ────────────────────────────────────────────
-- keep denormalized counts in sync automatically
CREATE TRIGGER IF NOT EXISTS trg_pair_tag_insert_count
AFTER INSERT ON pair_bookmark_tags
BEGIN
    UPDATE tags SET use_count = use_count + 1 WHERE id = NEW.tag_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_pair_tag_delete_count
AFTER DELETE ON pair_bookmark_tags
BEGIN
    UPDATE tags SET use_count = use_count - 1 WHERE id = OLD.tag_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_conv_tag_insert_count
AFTER INSERT ON conversation_bookmark_tags
BEGIN
    UPDATE tags SET use_count = use_count + 1 WHERE id = NEW.tag_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_conv_tag_delete_count
AFTER DELETE ON conversation_bookmark_tags
BEGIN
    UPDATE tags SET use_count = use_count - 1 WHERE id = OLD.tag_id;
END;

-- ─── corpus updated_at trigger ─────────────────────────────────────────
CREATE TRIGGER IF NOT EXISTS trg_corpus_pairs_touch
AFTER INSERT ON corpus_pairs
BEGIN
    UPDATE corpora SET updated_at = datetime('now') WHERE id = NEW.corpus_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_corpus_pairs_untouch
AFTER DELETE ON corpus_pairs
BEGIN
    UPDATE corpora SET updated_at = datetime('now') WHERE id = OLD.corpus_id;
END;

-- ─── seal the migration ────────────────────────────────────────────────
INSERT INTO schema_migrations (version) VALUES ('001_init');
