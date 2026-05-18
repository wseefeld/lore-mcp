# lore-mcp

A personal semantic memory archive, exposed as an MCP server.

Ingests your communication and content history — text messages, email, journals,
voice memos, transcripts, articles, and more — into a local PostgreSQL + pgvector
database. Any MCP-compatible AI client (Claude Desktop, LibreChat, and others)
can query it as a tool, making your full personal history searchable by meaning,
not just by keyword.

**Philosophy:** Everything is a conversation. If it can be expressed in words,
it belongs here.

## Status

Early development. Schema and ingestion pipeline in progress.

## Stack

- Python 3.14 / [uv](https://docs.astral.sh/uv/)
- PostgreSQL 17 + [pgvector](https://github.com/pgvector/pgvector)
- [nomic-embed-text](https://ollama.com/library/nomic-embed-text) via [Ollama](https://ollama.com)
- [MCP](https://modelcontextprotocol.io) (Model Context Protocol)

## License

Apache 2.0
