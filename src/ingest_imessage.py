#!/usr/bin/env python3
"""
Ingest iMessage conversations from chat.db into the lore memories table.

Usage:
    uv run src/ingest_imessage.py [options]

Options:
    --contacts-only     Skip conversations with unrecognized contacts (low-value tier)
    --db-path PATH      Path to chat.db (default: ~/Library/Messages/chat.db)
    --pg-dsn DSN        PostgreSQL DSN (default: $LORE_DB or postgresql://localhost/lore)
    --version           Print version and exit
"""

import argparse
import asyncio
import json
import os
import sqlite3
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg

__version__ = "0.1.0"

CHAT_DB_DEFAULT = Path.home() / "Library" / "Messages" / "chat.db"
CONTACTS_FILE = Path(__file__).parent.parent / "contacts.toml"
SOURCE_TYPE = "imessage"
APPLE_EPOCH = 978307200  # seconds between Unix epoch (1970) and Apple epoch (2001)


# ---------------------------------------------------------------------------
# Blob decoding — adapted from read-ashley-messages.py
# ---------------------------------------------------------------------------

def decode_attributed_body(blob: bytes | None) -> str | None:
    """Extract plain text from an NSAttributedString binary plist blob."""
    if not blob:
        return None
    data = bytes(blob)
    ns_idx = data.find(b"NSString")
    if ns_idx == -1:
        return None
    marker_idx = data.find(b"\x01+", ns_idx + 8)
    if marker_idx == -1:
        return None
    pos = marker_idx + 2
    if pos >= len(data):
        return None
    text_len = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        text_len |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
        if shift > 28:
            return None
    text_len = min(text_len, len(data) - pos)
    try:
        return data[pos : pos + text_len].decode("utf-8", errors="replace")
    except Exception:
        return None


def clean_body(text: str) -> str:
    """Strip inline-object metadata at U+FFFC / U+FFFD sentinel characters."""
    first = len(text)
    for sentinel in ("￼", "�"):
        idx = text.find(sentinel)
        if idx != -1:
            first = min(first, idx)
    return text[:first].rstrip()


# ---------------------------------------------------------------------------
# Time conversion
# ---------------------------------------------------------------------------

def apple_to_utc(apple_ns: int) -> datetime:
    """Convert an Apple nanosecond timestamp to a UTC-aware datetime."""
    return datetime.fromtimestamp(apple_ns / 1_000_000_000 + APPLE_EPOCH, tz=timezone.utc)


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

def load_contacts(path: Path) -> dict[str, str]:
    """Load contacts.toml; return {identifier: display_name} or {} if file is absent."""
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return dict(data.get("contacts", {}))


def determine_tier(participant_ids: list[str], contacts: dict[str, str]) -> str:
    """Return 'high_value' if any participant is a known contact, else 'low_value'."""
    return "high_value" if any(pid in contacts for pid in participant_ids) else "low_value"


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def fetch_chats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return all chats with their participant handle identifiers."""
    chats = conn.execute(
        "SELECT rowid, guid, chat_identifier, display_name, service_name "
        "FROM chat ORDER BY rowid"
    ).fetchall()
    result = []
    for rowid, guid, chat_identifier, display_name, service_name in chats:
        participants = conn.execute(
            """
            SELECT h.id, h.service FROM handle h
            JOIN chat_handle_join chj ON chj.handle_id = h.rowid
            WHERE chj.chat_id = ?
            """,
            (rowid,),
        ).fetchall()
        result.append({
            "rowid": rowid,
            "guid": guid,
            "chat_identifier": chat_identifier or "",
            "display_name": display_name or "",
            "service_name": service_name or "",
            "participants": [{"id": p[0], "service": p[1]} for p in participants],
        })
    return result


def fetch_messages(conn: sqlite3.Connection, chat_rowid: int) -> list[dict[str, Any]]:
    """Return all messages for a chat, oldest first."""
    rows = conn.execute(
        """
        SELECT m.guid, m.text, m.attributedBody, m.is_from_me, m.date,
               h.id AS handle_id, h.service AS handle_service
        FROM message m
        LEFT JOIN handle h ON m.handle_id = h.rowid
        JOIN chat_message_join cmj ON cmj.message_id = m.rowid
        WHERE cmj.chat_id = ? AND m.date > 0
        ORDER BY m.date ASC
        """,
        (chat_rowid,),
    ).fetchall()
    return [
        {
            "guid": r[0],
            "text": r[1],
            "attributed_body": r[2],
            "is_from_me": bool(r[3]),
            "date": r[4],
            "handle_id": r[5],
            "handle_service": r[6],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------

def chat_title(chat: dict[str, Any], contacts: dict[str, str]) -> str:
    """Derive a human-readable title for a chat."""
    if chat["display_name"]:
        return chat["display_name"]
    participant_ids = [p["id"] for p in chat["participants"]]
    if len(participant_ids) == 1:
        pid = participant_ids[0]
        return contacts.get(pid, pid)
    return ", ".join(contacts.get(p, p) for p in participant_ids)


def build_participants_json(
    chat: dict[str, Any], contacts: dict[str, str]
) -> list[dict[str, Any]]:
    """Build the participants JSONB array for a chat."""
    result: list[dict[str, Any]] = [{"name": "Me", "role": "self"}]
    for p in chat["participants"]:
        name = contacts.get(p["id"], p["id"])
        result.append({"name": name, "identifier": p["id"], "role": "contact"})
    return result


def build_memory_row(
    msg: dict[str, Any],
    chat: dict[str, Any],
    tier: str,
    title: str,
    participants: list[dict[str, Any]],
    contacts: dict[str, str],
) -> dict[str, Any] | None:
    """Build a memories table row; return None if body cannot be resolved."""
    body = msg["text"]
    if not body and msg["attributed_body"]:
        body = decode_attributed_body(msg["attributed_body"])
    if not body:
        return None
    body = clean_body(body)
    if not body:
        return None

    if msg["is_from_me"]:
        author, author_id, direction = "Me", None, "sent"
    else:
        handle = msg["handle_id"]
        if handle:
            author = contacts.get(handle, handle)
            author_id = handle
        else:
            author, author_id = "Unknown", None
        direction = "received"

    return {
        "source_type": SOURCE_TYPE,
        "source_id": msg["guid"],
        "thread_id": chat["guid"],
        "thread_title": title,
        "author": author,
        "author_id": author_id,
        "direction": direction,
        "participants": json.dumps(participants),
        "body": body,
        "chunk_index": 0,
        "chunk_count": 1,
        "created_at": apple_to_utc(msg["date"]),
        "metadata": json.dumps({
            "tier": tier,
            "service": msg["handle_service"] or chat["service_name"],
            "chat_identifier": chat["chat_identifier"],
        }),
    }


def build_thread_row(
    chat: dict[str, Any],
    messages: list[dict[str, Any]],
    tier: str,
    title: str,
    participants: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a threads table row from a chat and its messages."""
    dated = [m for m in messages if m["date"] > 0]
    return {
        "thread_id": chat["guid"],
        "source_type": SOURCE_TYPE,
        "title": title,
        "participants": json.dumps(participants),
        "started_at": apple_to_utc(dated[0]["date"]) if dated else None,
        "ended_at": apple_to_utc(dated[-1]["date"]) if dated else None,
        "message_count": len(messages),
        "chunk_count": len(messages),
        "metadata": json.dumps({"tier": tier, "service": chat["service_name"]}),
    }


# ---------------------------------------------------------------------------
# PostgreSQL helpers
# ---------------------------------------------------------------------------

async def upsert_thread(pg: asyncpg.Connection, row: dict[str, Any]) -> None:
    """Insert or update a thread row."""
    await pg.execute(
        """
        INSERT INTO threads
            (thread_id, source_type, title, participants, started_at,
             ended_at, message_count, chunk_count, metadata)
        VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9::jsonb)
        ON CONFLICT (thread_id) DO UPDATE SET
            title         = EXCLUDED.title,
            participants  = EXCLUDED.participants,
            started_at    = EXCLUDED.started_at,
            ended_at      = EXCLUDED.ended_at,
            message_count = EXCLUDED.message_count,
            chunk_count   = EXCLUDED.chunk_count,
            metadata      = EXCLUDED.metadata
        """,
        row["thread_id"], row["source_type"], row["title"],
        row["participants"], row["started_at"], row["ended_at"],
        row["message_count"], row["chunk_count"], row["metadata"],
    )


async def upsert_memory(pg: asyncpg.Connection, row: dict[str, Any]) -> bool:
    """Insert a memory row; return True if inserted, False if already existed."""
    result = await pg.fetchval(
        """
        INSERT INTO memories
            (source_type, source_id, thread_id, thread_title, author, author_id,
             direction, participants, body, chunk_index, chunk_count,
             created_at, metadata)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11, $12, $13::jsonb)
        ON CONFLICT (source_type, source_id) WHERE source_id IS NOT NULL
        DO NOTHING
        RETURNING id
        """,
        row["source_type"], row["source_id"], row["thread_id"], row["thread_title"],
        row["author"], row["author_id"], row["direction"], row["participants"],
        row["body"], row["chunk_index"], row["chunk_count"],
        row["created_at"], row["metadata"],
    )
    return result is not None


async def log_run(
    pg: asyncpg.Connection,
    source_path: Path,
    record_count: int,
    thread_count: int,
    status: str,
    notes: str | None,
) -> None:
    """Record an ingestion run in ingestion_log."""
    await pg.execute(
        """
        INSERT INTO ingestion_log
            (source_type, source_path, record_count, thread_count, status, notes)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        SOURCE_TYPE, str(source_path), record_count, thread_count, status, notes,
    )


# ---------------------------------------------------------------------------
# Main ingestion logic
# ---------------------------------------------------------------------------

async def ingest(
    pg: asyncpg.Connection,
    chat_db_path: Path,
    contacts: dict[str, str],
    contacts_only: bool,
) -> tuple[int, int]:
    """
    Ingest all iMessage conversations into lore.
    Returns (memories_inserted, threads_upserted).
    """
    conn = sqlite3.connect(f"file:{chat_db_path}?mode=ro", uri=True)
    memories_inserted = 0
    threads_upserted = 0
    try:
        chats = fetch_chats(conn)
        for chat in chats:
            participant_ids = [p["id"] for p in chat["participants"]]
            tier = determine_tier(participant_ids, contacts)
            if contacts_only and tier == "low_value":
                continue
            messages = fetch_messages(conn, chat["rowid"])
            if not messages:
                continue
            title = chat_title(chat, contacts)
            participants = build_participants_json(chat, contacts)
            await upsert_thread(
                pg, build_thread_row(chat, messages, tier, title, participants)
            )
            threads_upserted += 1
            for msg in messages:
                memory_row = build_memory_row(
                    msg, chat, tier, title, participants, contacts
                )
                if memory_row and await upsert_memory(pg, memory_row):
                    memories_inserted += 1
    finally:
        conn.close()
    return memories_inserted, threads_upserted


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest iMessage conversations into the lore memory archive."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--contacts-only",
        action="store_true",
        help="Skip conversations with unrecognized contacts (low-value tier)",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=CHAT_DB_DEFAULT,
        help="Path to chat.db (default: ~/Library/Messages/chat.db)",
    )
    parser.add_argument(
        "--pg-dsn",
        default=os.environ.get("LORE_DB", "postgresql://localhost/lore"),
        help="PostgreSQL DSN (default: $LORE_DB or postgresql://localhost/lore)",
    )
    args = parser.parse_args()

    async def run() -> None:
        contacts = load_contacts(CONTACTS_FILE)
        pg = await asyncpg.connect(args.pg_dsn)
        try:
            inserted, upserted = await ingest(pg, args.db_path, contacts, args.contacts_only)
            await log_run(pg, args.db_path, inserted, upserted, "complete", None)
            print(f"Done: {inserted} memories inserted, {upserted} threads upserted.")
        except Exception as e:
            await log_run(pg, args.db_path, 0, 0, "failed", str(e))
            raise
        finally:
            await pg.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
