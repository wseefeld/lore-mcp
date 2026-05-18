# lore-mcp — Design Notes

---

## Core Philosophy

Everything is a conversation. A text message, a YouTube video, a journal entry, a dream, a therapy session — all are instances of someone saying something to someone at some point in time. This system models that universal pattern.

If it can be expressed in words, or described after the fact in words, it's a candidate for indexing.

---

## What This Is

A personal semantic memory layer over the user's entire communication and content history. A local, private, queryable archive of everything significant that was ever said — by the user, to the user, or consumed by the user.

The primary interface is an MCP tool server (lore-mcp), so any MCP-capable AI client (Claude Desktop, LibreChat + local models) can query it as a first-class tool. The archive doesn't care who's asking.

---

## Schema Summary

Three tables in PostgreSQL + pgvector:

- **memories** — one row per message, transcript chunk, article chunk, or content unit
- **threads** — one row per conversation / video / article / journal entry (thread-level metadata)
- **ingestion_log** — tracks what has been imported and when (idempotency)

See [schema.sql](../sql/schema.sql) for full DDL with comments.


### Key design decisions

**Everything is a chunk.** Short messages are single-chunk (chunk_index=0, chunk_count=1). Long content (video transcripts, articles, journal entries) is split into overlapping segments, each with its own embedding. Chunk overlap ensures context isn't lost at boundaries.

**`direction` encodes the relationship to the user:**
- `sent` — the user wrote or said it
- `received` — someone sent it to the user
- `consumed` — the user read, watched, or listened (no direct reply)
- `internal` — the user to themselves (journal, voice memo, dream)
- `recalled` — reconstructed after the fact from memory

**`participants` is a JSONB array** with `{name, identifier, role}` objects. Roles include: `self`, `contact`, `creator`, `host`, `ai`, `therapist`. Handles any source type.

**`metadata` is a JSONB escape hatch** for source-specific fields that don't belong in core schema (video ID, channel name, book ISBN, dream vividness, etc.).

**Embedding model:** nomic-embed-text via Ollama (already available). Produces 768-dimensional vectors. Full-text index (GIN on `to_tsvector`) complements semantic search for exact-keyword queries.

**Source data and embeddings stored separately** (credit: OB1 pattern). An `embedding_model` column records which model produced each vector, so re-indexing with a better model never requires touching source data.

---

## Planned Source Types

### Already have data / tooling

| source_type | Data available | Notes |
|-------------|---------------|-------|
| `imessage` | chat.db (Aug 2020–present) | existing iMessage parser exists; generalize |
| `android_sms` | 1.6GB XML on Google Drive | Pre-2025; pre-dates iPhone |
| `gmail` | Google Takeout | Explored; key threads catalogued |
| `journal` | Obsidian vault | 18+ months processed; ready to index |
| `course_transcript` | MacWhisper output in vault | Bootcamp sessions already transcribed |
| `voice_memo` | MacWhisper transcriptions | Voice-first writing methodology in use |

### Near-term additions

| source_type | Source | Notes |
|-------------|--------|-------|
| `youtube` | YouTube watch history + transcripts | yt-dlp can pull transcripts |
| `podcast` | Overcast/Pocket Casts export | Episode transcripts via Whisper |
| `claude_conversation` | Anthropic conversation export | Rich; deeply useful for ADHD recall |
| `book_highlight` | Kindle/Readwise export | Highlights + notes |

### Future / aspirational

| source_type | Notes |
|-------------|-------|
| `dream` | Described after the fact; direction='internal' |
| `recalled_conversation` | In-person or phone conversations reconstructed from memory |
| `article` / `medium` / `news` | Saved articles; Pocket export or browser history |
| `skool` | Community posts/replies if exportable |
| `therapy_note` | Session notes; direction='internal' or 'received' |

---

## MCP Tool Interface (planned)

Three tools exposed to MCP clients:

```
search_memory(query, sources?, date_range?, direction?, limit?)
  → Returns ranked results with thread_title, author, created_at, body excerpt, similarity score

get_thread_context(thread_id, around_id?, window?)
  → Returns surrounding messages/chunks for a flagged result

list_sources()
  → Returns ingested source types with row counts and latest indexed_at
```

### MCP server

- Custom Python MCP server (`lore-mcp`) — `asyncpg` + Ollama API for query embedding
- Examine OB1's MCP server implementation before writing from scratch — may be adaptable
- Binds to localhost only; accessed by Claude Desktop and LibreChat on the same machine
- Implements `--version` CLI arg for checking that it executes and is the expected version

### Client compatibility

| Client | MCP support | Notes |
|--------|-------------|-------|
| Claude Desktop | ✅ Native | Add server to `claude_desktop_config.json` |
| LibreChat | ✅ Yes (added ~2024) | Configure via LibreChat MCP settings |
| Claude Code | ✅ Via CLAUDE.md or config | Command-line tool |

---

## Infrastructure

- PostgreSQL@17 (upgrading from deprecated @14)
- pgvector extension
- nomic-embed-text pulled in Ollama
- Python 3.14.5 via Homebrew, managed with uv
- LibreChat running in Docker/Colima

**Database:** new database named `lore` on the host PostgreSQL@17 instance. LibreChat's own PostgreSQL (if it uses one) is separate from this.

LibreChat runs in an isolated Docker/Colima environment. The lore-mcp server runs on the host and is reachable by both LibreChat (via host networking or bridge) and Claude Desktop natively.

---

## Implementation Phases

### Phase 0 — Infrastructure
- [x] Upgrade PostgreSQL@14 → @17; install pgvector on @17
- [x] Reinstall uv via standalone installer; verify `uv self update` works
- [x] Create `lore` database
- [x] Run schema.sql
- [x] Create lore-mcp GitHub repo under wseefeld account
- [x] Move schema.sql to repo; update this doc with repo pointer

### Phase 1 — Foundation
- [ ] Write `ingest_imessage.py` — generalizes existing iMessage parser to all contacts
- [ ] Write embedding pass script — iterates unembedded rows, calls Ollama nomic-embed-text, stores vectors
- [ ] Write `search.py` — CLI semantic + keyword search for testing

### Phase 2 — More sources
- [ ] `ingest_android_sms.py` — parse Google Takeout XML to same row format
- [ ] `ingest_gmail.py` — Google Takeout mbox format
- [ ] `ingest_journal.py` — walk Obsidian vault journal directories

### Phase 3 — MCP server
- [ ] Write lore-mcp MCP server (Python, `mcp` library)
- [ ] Register with Claude Desktop
- [ ] Configure LibreChat MCP integration
- [ ] Test both clients

### Phase 4 — Rich sources
- [ ] YouTube transcript ingestion (yt-dlp)
- [ ] Claude conversation export ingestion
- [ ] Readwise / Kindle highlights

---

## Testing conventions

### Framework
pytest + pytest-asyncio for all tests.

### Rules
- Every module gets a test file when the module is created (`src/foo.py` → `tests/test_foo.py`)
- Every function in a module has at least one test
- A feature or logic change is not complete until its tests are updated

### Two-tier structure

**Unit/system tests** (`tests/`)
Isolated — no dependency on external services or persistent state. Database
tests use a throwaway DB (`lore_test`) created and dropped per session by a
pytest fixture. Mock external services (Ollama, iMessage chat.db) at this tier.
Run with: `uv run pytest`

**Integration tests** (`tests/integration/`)
Full end-to-end against real external services. Use the `lore_integration` DB —
never the production `lore` DB. Neither tier may depend on or affect the other's
database state.
Run with: `uv run pytest tests/integration`

### Database naming
- `lore` — production (no suffix)
- `lore_test` — throwaway, created/dropped by unit/system test fixture
- `lore_integration` — persistent, for integration tests only

---

## Notes and Open Questions

- **Chunk size:** 512 tokens with 64-token overlap is a reasonable starting point for long content. Tune based on retrieval quality.
- **Re-embedding:** `embedding_model` column makes this tractable — query rows where embedding_model != target model, re-embed, update.
- **Privacy:** This is entirely local. Nothing leaves the machine. lore-mcp binds to localhost only.
- **First ingestion target:** All iMessage conversations (not just listed contacts) — contact filtering becomes a query-time operation on an already-indexed corpus.
- **OB1 relationship:** Complementary, not duplicative. OB1 is working/forward memory; lore-mcp is bulk historical ingestion and episodic memory. Worth studying OB1's MCP server code before writing our own.
