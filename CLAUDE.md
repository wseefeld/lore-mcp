# lore-mcp

A personal semantic memory archive exposed as an MCP server. Ingests
communication and content history from any source expressible in words,
stores it in PostgreSQL + pgvector, and makes it searchable by any
MCP-compatible AI client (Claude Desktop, LibreChat, etc.).

## Design document
Full architecture, philosophy, source-type catalog, and key decisions:
docs/Personal-Memory-Archive-Design.md

Read this before making architectural decisions. The design doc is authoritative;
do not override schema, source model, or tech stack choices without consulting it.

## Schema
sql/schema.sql — read before writing any database-touching code.

## Tech stack
- Python 3.14 via uv
- PostgreSQL 17 + pgvector 0.8.2
- Database name: lore (localhost, default port 5432)
- Embeddings: nomic-embed-text via Ollama (768-dim vectors)
- DB access: asyncpg
- MCP server: mcp library

## iMessage contact tiering
- Contact list: `contacts.toml` (gitignored) — maps phone numbers to display names
- All conversations ingested by default; listed contacts → `metadata.tier = "high_value"`, others → `"low_value"`
- `--contacts-only` flag skips low-value tier at ingest time
- `search_memory` excludes low-value memories by default; `include_low_value=true` to override

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
