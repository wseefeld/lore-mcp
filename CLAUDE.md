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
