# lore-mcp

A personal semantic memory archive exposed as an MCP server. Ingests
communication and content history from any source expressible in words,
stores it in PostgreSQL + pgvector, and makes it searchable by any
MCP-compatible AI client (Claude Desktop, LibreChat, etc.).

## Design document
Full architecture, philosophy, and source-type catalog:
/Users/walt/ObsidianVaults/Walt_Personal/Claude.AI/Active-Projects/Personal-Memory-Archive-Design.md

Read this before making architectural decisions.

## Schema
sql/schema.sql — read before writing any database-touching code.

## Tech stack
- Python 3.14 via uv
- PostgreSQL 17 + pgvector 0.8.2
- Database name: lore (localhost, default port 5432)
- Embeddings: nomic-embed-text via Ollama (768-dim vectors)
- DB access: asyncpg
- MCP server: mcp library

## Key decisions — do not re-litigate
- Three tables: memories, threads, ingestion_log
- Source data and embeddings stored separately; embedding_model column
  records which model produced each vector (enables re-indexing)
- chunk_index / chunk_count pattern for long content (articles, transcripts)
- direction field values: sent / received / consumed / internal / recalled
- metadata JSONB column for source-specific fields (no pre-specified schema)
- Full-text GIN index alongside semantic ivfflat index
- MCP server binds to localhost only

## Build order
1. sql/schema.sql — apply to lore database (already written)
2. src/ingest_imessage.py — normalize iMessage chat.db → memories schema
3. src/embed.py — embedding pass over un-embedded rows via Ollama API
4. src/search.py — CLI semantic + keyword search for local testing
5. src/server.py — MCP server (tools: search_memory, get_thread_context, list_sources)

## Coding conventions
- Async throughout (asyncpg, httpx for Ollama API calls)
- Type hints on all function signatures
- Ingestion scripts are idempotent — always check ingestion_log before importing
- Keep ingestion scripts independent of each other (one file per source type)

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

### Implementation notes
- Shared DB fixture lives in `tests/conftest.py` so all test files can reuse it
- Mock Ollama via `httpx.MockTransport` (not `unittest.mock` patching)
