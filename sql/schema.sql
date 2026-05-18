-- =============================================================================
-- lore-mcp — PostgreSQL + pgvector Schema
-- GitHub: open-lore/lore-mcp
-- =============================================================================
-- Philosophy: Everything is a conversation.
--   A text message, a YouTube video, a journal entry, a dream — all are
--   instances of someone saying something to someone at some point in time.
--   This schema models that universal pattern, with enough flexibility to
--   accommodate any source that can be expressed in words.
--
-- Core table: memories
--   One row per message, chunk, highlight, or turn.
--   Long content (articles, transcripts) is split into overlapping chunks;
--   all chunks from one piece share a thread_id.
--
-- Companion tables:
--   threads         — one row per conversation/video/article/etc.
--   ingestion_log   — tracks what has been imported and when (idempotency)
--
-- MCP tool interface (planned — see lore-mcp server):
--   search_memory(query, sources?, date_range?, limit?)
--   get_thread_context(thread_id, around_id?)
--   list_sources()
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";


-- -----------------------------------------------------------------------------
-- memories — the core table
-- -----------------------------------------------------------------------------

CREATE TABLE memories (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- -------------------------------------------------------------------------
    -- Source identification
    -- -------------------------------------------------------------------------
    source_type     TEXT NOT NULL,
    -- Controlled vocabulary (extensible — add new values freely):
    --   Messaging:            'imessage', 'android_sms', 'gmail'
    --   Social / community:   'skool', 'discord', 'reddit'
    --   Web content:          'youtube', 'podcast', 'medium', 'article', 'news'
    --   Books / reading:      'book_highlight', 'book_note'
    --   Personal capture:     'journal', 'voice_memo', 'dream', 'therapy_note'
    --   Course material:      'course_transcript', 'course_note'
    --   AI conversations:     'claude_conversation', 'chatgpt_conversation'
    --   Recalled / reconstructed: 'recalled_conversation'

    source_id       TEXT,           -- ID in the originating system
                                    --   iMessage: message GUID
                                    --   YouTube:  video ID
                                    --   Gmail:    message ID
                                    --   etc.
    source_url      TEXT,           -- Canonical URL if applicable

    -- -------------------------------------------------------------------------
    -- Thread grouping
    -- Every memory belongs to a thread. A thread is one coherent conversation
    -- or piece of content: an SMS exchange, an email thread, a YouTube video
    -- plus its comments, a journal entry, a book chapter's highlights, etc.
    -- -------------------------------------------------------------------------
    thread_id       TEXT NOT NULL,  -- stable identifier; used to reconstruct
                                    -- context around a search result
    thread_title    TEXT,           -- email subject, video title, article
                                    -- headline, journal date, etc.

    -- -------------------------------------------------------------------------
    -- Authorship and direction
    -- -------------------------------------------------------------------------
    author          TEXT,           -- display name of sender / creator / speaker
                                    --   'Walt', 'Ashley', 'Lex Fridman',
                                    --   'The Atlantic', 'Claude', etc.
    author_id       TEXT,           -- stable identifier for the author
                                    --   phone number, email address, channel ID
    direction       TEXT,           -- 'sent'      — Walt wrote/said it
                                    -- 'received'  — someone sent it to Walt
                                    -- 'consumed'  — Walt read/watched/listened
                                    -- 'internal'  — Walt to himself
                                    --               (journal, voice memo, dream)
                                    -- 'recalled'  — reconstructed after the fact
    participants    JSONB,          -- All parties in the thread.
                                    -- Array of: {name, identifier, role}
                                    -- role: 'self', 'contact', 'creator',
                                    --       'host', 'ai', 'therapist', etc.
                                    --
                                    -- Example (text message thread):
                                    --   [
                                    --     {"name":"Walt","identifier":"+19015550100","role":"self"},
                                    --     {"name":"Ashley","identifier":"+16622997344","role":"contact"}
                                    --   ]
                                    -- Example (YouTube video):
                                    --   [
                                    --     {"name":"Lex Fridman","identifier":"UC...","role":"creator"},
                                    --     {"name":"Walt","role":"self"}
                                    --   ]
                                    -- Example (journal entry):
                                    --   [{"name":"Walt","role":"self"}]

    -- -------------------------------------------------------------------------
    -- Content
    -- -------------------------------------------------------------------------
    body            TEXT NOT NULL,          -- The actual text of this memory
    body_embedding  vector(768),            -- nomic-embed-text produces 768-dim vectors
                                            -- NULL until embedding pass is run

    -- -------------------------------------------------------------------------
    -- Chunking
    -- Short messages: chunk_index=0, chunk_count=1 (no chunking needed)
    -- Long content (articles, transcripts): split into overlapping segments.
    --   Each segment is one row; all share the same thread_id.
    --   chunk_overlap: how many tokens this chunk shares with its neighbor,
    --   so retrieval can reconstruct context across chunk boundaries.
    -- -------------------------------------------------------------------------
    chunk_index     INTEGER NOT NULL DEFAULT 0,
    chunk_count     INTEGER NOT NULL DEFAULT 1,
    chunk_overlap   INTEGER,                -- tokens overlapped with prior chunk

    -- -------------------------------------------------------------------------
    -- Temporal
    -- -------------------------------------------------------------------------
    created_at      TIMESTAMPTZ,            -- when the original event occurred
                                            -- NULL if unknown (reconstructed, etc.)
    indexed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- -------------------------------------------------------------------------
    -- Flexible metadata
    -- Source-specific fields that don't belong in the core schema.
    -- Query these with PostgreSQL's JSONB operators.
    --
    -- Examples:
    --   YouTube:      {"video_id":"abc123","channel":"Lex Fridman","duration_s":7200}
    --   Podcast:      {"episode_num":412,"feed_url":"...","show":"Huberman Lab"}
    --   SMS:          {"contact_number":"+16622997344","read":true}
    --   Journal:      {"mood":"reflective","tags":["shame","pillar06"]}
    --   Book:         {"isbn":"...","chapter":"3","highlight_color":"yellow"}
    --   Dream:        {"lucid":false,"vividness":4}
    --   Recalled:     {"confidence":"medium","occasion":"therapy 2024-03"}
    --   Claude conv:  {"model":"claude-sonnet-4-6","project":"Hey Me Writing"}
    -- -------------------------------------------------------------------------
    metadata        JSONB
);


-- -----------------------------------------------------------------------------
-- Indexes
-- -----------------------------------------------------------------------------

-- Semantic similarity search (primary search mode)
-- ivfflat is appropriate for this dataset size; adjust `lists` as corpus grows:
--   ~100 for <1M rows, ~1000 for 1M–10M rows
CREATE INDEX idx_memories_embedding
    ON memories USING ivfflat (body_embedding vector_cosine_ops)
    WITH (lists = 100);

-- Full-text keyword search (exact-word complement to semantic search)
CREATE INDEX idx_memories_fts
    ON memories USING gin (to_tsvector('english', body));

-- Filtering indexes
CREATE INDEX idx_memories_source_type ON memories (source_type);
CREATE INDEX idx_memories_direction   ON memories (direction);
CREATE INDEX idx_memories_thread_id   ON memories (thread_id);
CREATE INDEX idx_memories_author_id   ON memories (author_id);
CREATE INDEX idx_memories_created_at  ON memories (created_at DESC);

-- JSONB metadata queries
CREATE INDEX idx_memories_metadata    ON memories USING gin (metadata);


-- -----------------------------------------------------------------------------
-- threads — thread-level metadata
-- One row per conversation / video / article / journal entry / etc.
-- Populated and kept in sync by the ingestion pipeline.
-- -----------------------------------------------------------------------------

CREATE TABLE threads (
    thread_id       TEXT PRIMARY KEY,
    source_type     TEXT NOT NULL,
    title           TEXT,
    participants    JSONB,
    started_at      TIMESTAMPTZ,
    ended_at        TIMESTAMPTZ,
    message_count   INTEGER,
    chunk_count     INTEGER,        -- total embedded chunks across all messages
    source_url      TEXT,
    metadata        JSONB
);

CREATE INDEX idx_threads_source_type ON threads (source_type);
CREATE INDEX idx_threads_started_at  ON threads (started_at DESC);


-- -----------------------------------------------------------------------------
-- ingestion_log — what has been imported (idempotency + audit trail)
-- Before ingesting a source file, check here by source_hash.
-- If a row exists with status='complete' and same hash, skip.
-- -----------------------------------------------------------------------------

CREATE TABLE ingestion_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type     TEXT NOT NULL,
    source_path     TEXT,           -- file path or API endpoint ingested
    source_hash     TEXT,           -- SHA256 of source file (change detection)
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    record_count    INTEGER,        -- rows written to memories
    thread_count    INTEGER,        -- threads created or updated
    status          TEXT NOT NULL,  -- 'complete', 'partial', 'failed'
    notes           TEXT            -- error messages, partial-run details, etc.
);

CREATE INDEX idx_ingestion_log_source_type ON ingestion_log (source_type);
CREATE INDEX idx_ingestion_log_ingested_at ON ingestion_log (ingested_at DESC);


-- =============================================================================
-- Reference: Example queries
-- =============================================================================

-- Semantic search (embed the query string first in application layer):
--
--   SELECT id, source_type, author, created_at, thread_title, body,
--          1 - (body_embedding <=> $1::vector) AS similarity
--   FROM   memories
--   WHERE  body_embedding IS NOT NULL
--   ORDER  BY body_embedding <=> $1::vector
--   LIMIT  20;

-- Filtered to one source type:
--   ... WHERE source_type = 'imessage' AND body_embedding IS NOT NULL ...

-- Filtered to Walt's own sent messages:
--   ... WHERE direction = 'sent' ORDER BY body_embedding <=> $1::vector LIMIT 20;

-- Keyword search (no embedding needed):
--   SELECT * FROM memories
--   WHERE  to_tsvector('english', body) @@ plainto_tsquery('english', 'fiction writing')
--   ORDER  BY created_at DESC;

-- Reconstruct full thread context around a result:
--   SELECT * FROM memories
--   WHERE  thread_id = $1
--   ORDER  BY chunk_index;

-- Combined: semantic search with keyword pre-filter (faster on large corpus):
--   SELECT id, source_type, author, created_at, body,
--          1 - (body_embedding <=> $1::vector) AS similarity
--   FROM   memories
--   WHERE  to_tsvector('english', body) @@ plainto_tsquery('english', $2)
--     AND  body_embedding IS NOT NULL
--   ORDER  BY body_embedding <=> $1::vector
--   LIMIT  20;
