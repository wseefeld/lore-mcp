import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest

from ingest_imessage import (
    apple_to_utc,
    build_memory_row,
    build_participants_json,
    build_thread_row,
    chat_title,
    clean_body,
    decode_attributed_body,
    determine_tier,
    fetch_chats,
    fetch_messages,
    load_contacts,
    log_run,
    upsert_memory,
    upsert_thread,
    ingest,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CHAT_DB_SCHEMA = """
CREATE TABLE handle (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT,
    service TEXT
);
CREATE TABLE chat (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    guid TEXT,
    chat_identifier TEXT,
    display_name TEXT,
    service_name TEXT
);
CREATE TABLE message (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    guid TEXT,
    text TEXT,
    attributedBody BLOB,
    is_from_me INTEGER DEFAULT 0,
    date INTEGER DEFAULT 0,
    handle_id INTEGER
);
CREATE TABLE chat_handle_join (
    chat_id INTEGER,
    handle_id INTEGER
);
CREATE TABLE chat_message_join (
    chat_id INTEGER,
    message_id INTEGER
);
"""


def make_chat_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(CHAT_DB_SCHEMA)
    return conn


def make_attributed_body(text: str) -> bytes:
    """Construct a minimal NSAttributedString blob containing the given text."""
    encoded = text.encode("utf-8")
    n = len(encoded)
    varint = bytes([n]) if n < 128 else bytes([n & 0x7F | 0x80, n >> 7])
    return b"NSString\x01+" + varint + encoded


def sample_chat(display_name: str = "") -> dict:
    return {
        "rowid": 1,
        "guid": "chat-guid-1",
        "chat_identifier": "+15551234567",
        "display_name": display_name,
        "service_name": "iMessage",
        "participants": [{"id": "+15551234567", "service": "iMessage"}],
    }


def sample_message(is_from_me: bool = False, text: str = "Hello") -> dict:
    return {
        "guid": "msg-guid-1",
        "text": text,
        "attributed_body": None,
        "is_from_me": is_from_me,
        "date": 700_000_000_000_000_000,  # some valid Apple timestamp
        "handle_id": "+15551234567",
        "handle_service": "iMessage",
    }


CONTACTS = {"+15551234567": "Alice"}

# ---------------------------------------------------------------------------
# decode_attributed_body
# ---------------------------------------------------------------------------

def test_decode_attributed_body_valid():
    blob = make_attributed_body("hello world")
    assert decode_attributed_body(blob) == "hello world"


def test_decode_attributed_body_valid_long():
    text = "x" * 200
    blob = make_attributed_body(text)
    assert decode_attributed_body(blob) == text


def test_decode_attributed_body_none():
    assert decode_attributed_body(None) is None


def test_decode_attributed_body_empty():
    assert decode_attributed_body(b"") is None


def test_decode_attributed_body_no_nsstring():
    assert decode_attributed_body(b"garbage data without the marker") is None


def test_decode_attributed_body_no_separator():
    assert decode_attributed_body(b"NSString but no separator after") is None


# ---------------------------------------------------------------------------
# clean_body
# ---------------------------------------------------------------------------

def test_clean_body_plain():
    assert clean_body("hello") == "hello"


def test_clean_body_trailing_whitespace():
    assert clean_body("hello   ") == "hello"


def test_clean_body_fffc_sentinel():
    assert clean_body("hello￼ metadata") == "hello"


def test_clean_body_fffd_sentinel():
    assert clean_body("hello� garbage") == "hello"


def test_clean_body_earliest_sentinel():
    assert clean_body("hi� middle ￼ end") == "hi"


# ---------------------------------------------------------------------------
# apple_to_utc
# ---------------------------------------------------------------------------

def test_apple_to_utc_epoch():
    # Apple epoch (0 ns) should map to 2001-01-01 00:00:00 UTC
    result = apple_to_utc(0)
    assert result == datetime(2001, 1, 1, tzinfo=timezone.utc)


def test_apple_to_utc_one_second():
    result = apple_to_utc(1_000_000_000)
    assert result == datetime(2001, 1, 1, 0, 0, 1, tzinfo=timezone.utc)


def test_apple_to_utc_is_utc_aware():
    result = apple_to_utc(0)
    assert result.tzinfo is not None


# ---------------------------------------------------------------------------
# load_contacts
# ---------------------------------------------------------------------------

def test_load_contacts_valid(tmp_path: Path):
    f = tmp_path / "contacts.toml"
    f.write_text('[contacts]\n"+15551234567" = "Alice"\n')
    assert load_contacts(f) == {"+15551234567": "Alice"}


def test_load_contacts_missing(tmp_path: Path):
    assert load_contacts(tmp_path / "nonexistent.toml") == {}


def test_load_contacts_empty_section(tmp_path: Path):
    f = tmp_path / "contacts.toml"
    f.write_text("[contacts]\n")
    assert load_contacts(f) == {}


def test_load_contacts_no_contacts_key(tmp_path: Path):
    f = tmp_path / "contacts.toml"
    f.write_text("[other]\nfoo = 'bar'\n")
    assert load_contacts(f) == {}


# ---------------------------------------------------------------------------
# determine_tier
# ---------------------------------------------------------------------------

def test_determine_tier_known_contact():
    assert determine_tier(["+15551234567"], {"+15551234567": "Alice"}) == "high_value"


def test_determine_tier_one_of_many():
    contacts = {"+15551234567": "Alice"}
    assert determine_tier(["+19990000000", "+15551234567"], contacts) == "high_value"


def test_determine_tier_unknown():
    assert determine_tier(["+19990000000"], {"+15551234567": "Alice"}) == "low_value"


def test_determine_tier_empty_participants():
    assert determine_tier([], {"+15551234567": "Alice"}) == "low_value"


def test_determine_tier_empty_contacts():
    assert determine_tier(["+15551234567"], {}) == "low_value"


# ---------------------------------------------------------------------------
# fetch_chats
# ---------------------------------------------------------------------------

def test_fetch_chats_single(make_db):
    chats = fetch_chats(make_db)
    assert len(chats) == 1
    assert chats[0]["guid"] == "chat-guid-1"
    assert chats[0]["chat_identifier"] == "+15551234567"
    assert len(chats[0]["participants"]) == 1
    assert chats[0]["participants"][0]["id"] == "+15551234567"


def test_fetch_chats_empty():
    conn = make_chat_db()
    assert fetch_chats(conn) == []


def test_fetch_chats_no_participants(make_db_no_participants):
    chats = fetch_chats(make_db_no_participants)
    assert len(chats) == 1
    assert chats[0]["participants"] == []


# ---------------------------------------------------------------------------
# fetch_messages
# ---------------------------------------------------------------------------

def test_fetch_messages_returns_message(make_db):
    msgs = fetch_messages(make_db, 1)
    assert len(msgs) == 1
    assert msgs[0]["guid"] == "msg-guid-1"
    assert msgs[0]["text"] == "Hello"
    assert msgs[0]["is_from_me"] is False


def test_fetch_messages_sent(make_db_sent):
    msgs = fetch_messages(make_db_sent, 1)
    assert msgs[0]["is_from_me"] is True


def test_fetch_messages_skips_zero_date():
    conn = make_chat_db()
    conn.execute("INSERT INTO chat VALUES (1,'chat-1','+1','','iMessage')")
    conn.execute("INSERT INTO handle VALUES (1,'+1','iMessage')")
    conn.execute("INSERT INTO message VALUES (1,'guid','Hi',NULL,0,0,1)")
    conn.execute("INSERT INTO chat_handle_join VALUES (1,1)")
    conn.execute("INSERT INTO chat_message_join VALUES (1,1)")
    assert fetch_messages(conn, 1) == []


def test_fetch_messages_empty_chat():
    conn = make_chat_db()
    conn.execute("INSERT INTO chat VALUES (1,'chat-1','+1','','iMessage')")
    assert fetch_messages(conn, 1) == []


# ---------------------------------------------------------------------------
# chat_title
# ---------------------------------------------------------------------------

def test_chat_title_display_name():
    chat = sample_chat(display_name="Family Group")
    assert chat_title(chat, CONTACTS) == "Family Group"


def test_chat_title_known_contact():
    chat = sample_chat()
    assert chat_title(chat, CONTACTS) == "Alice"


def test_chat_title_unknown_contact():
    chat = sample_chat()
    assert chat_title(chat, {}) == "+15551234567"


def test_chat_title_group_no_display_name():
    chat = sample_chat()
    chat["participants"] = [
        {"id": "+15551234567", "service": "iMessage"},
        {"id": "+19990000000", "service": "iMessage"},
    ]
    assert chat_title(chat, CONTACTS) == "Alice, +19990000000"


# ---------------------------------------------------------------------------
# build_participants_json
# ---------------------------------------------------------------------------

def test_build_participants_json_includes_self():
    result = build_participants_json(sample_chat(), CONTACTS)
    roles = [p["role"] for p in result]
    assert "self" in roles


def test_build_participants_json_known_name():
    result = build_participants_json(sample_chat(), CONTACTS)
    contact = next(p for p in result if p["role"] == "contact")
    assert contact["name"] == "Alice"
    assert contact["identifier"] == "+15551234567"


def test_build_participants_json_unknown_name():
    result = build_participants_json(sample_chat(), {})
    contact = next(p for p in result if p["role"] == "contact")
    assert contact["name"] == "+15551234567"


# ---------------------------------------------------------------------------
# build_memory_row
# ---------------------------------------------------------------------------

def _row(msg=None, chat=None, tier="high_value"):
    return build_memory_row(
        msg or sample_message(),
        chat or sample_chat(),
        tier,
        "Alice",
        build_participants_json(sample_chat(), CONTACTS),
        CONTACTS,
    )


def test_build_memory_row_received():
    row = _row()
    assert row is not None
    assert row["direction"] == "received"
    assert row["author"] == "Alice"
    assert row["author_id"] == "+15551234567"


def test_build_memory_row_sent():
    row = _row(msg=sample_message(is_from_me=True))
    assert row["direction"] == "sent"
    assert row["author"] == "Me"
    assert row["author_id"] is None


def test_build_memory_row_no_body():
    msg = sample_message(text="")
    msg["attributed_body"] = None
    assert _row(msg=msg) is None


def test_build_memory_row_attributed_body_fallback():
    msg = sample_message(text="")
    msg["attributed_body"] = make_attributed_body("from blob")
    row = _row(msg=msg)
    assert row is not None
    assert row["body"] == "from blob"


def test_build_memory_row_tier_in_metadata():
    row = _row(tier="low_value")
    assert row is not None
    meta = json.loads(row["metadata"])
    assert meta["tier"] == "low_value"


def test_build_memory_row_source_fields():
    row = _row()
    assert row["source_type"] == "imessage"
    assert row["source_id"] == "msg-guid-1"
    assert row["thread_id"] == "chat-guid-1"


def test_build_memory_row_unknown_handle():
    msg = sample_message()
    msg["handle_id"] = None
    row = _row(msg=msg)
    assert row["author"] == "Unknown"
    assert row["author_id"] is None


# ---------------------------------------------------------------------------
# build_thread_row
# ---------------------------------------------------------------------------

def test_build_thread_row_fields():
    msgs = [sample_message()]
    row = build_thread_row(
        sample_chat(), msgs, "high_value", "Alice",
        build_participants_json(sample_chat(), CONTACTS),
    )
    assert row["thread_id"] == "chat-guid-1"
    assert row["source_type"] == "imessage"
    assert row["message_count"] == 1
    assert row["started_at"] is not None
    assert row["ended_at"] is not None


def test_build_thread_row_no_messages():
    row = build_thread_row(
        sample_chat(), [], "low_value", "Alice",
        build_participants_json(sample_chat(), CONTACTS),
    )
    assert row["started_at"] is None
    assert row["ended_at"] is None


# ---------------------------------------------------------------------------
# upsert_thread (async, uses DB fixture)
# ---------------------------------------------------------------------------

async def test_upsert_thread_inserts(db: asyncpg.Connection):
    msgs = [sample_message()]
    row = build_thread_row(
        sample_chat(), msgs, "high_value", "Alice",
        build_participants_json(sample_chat(), CONTACTS),
    )
    await upsert_thread(db, row)
    result = await db.fetchrow(
        "SELECT * FROM threads WHERE thread_id = $1", "chat-guid-1"
    )
    assert result is not None
    assert result["title"] == "Alice"


async def test_upsert_thread_updates_on_conflict(db: asyncpg.Connection):
    msgs = [sample_message()]
    row = build_thread_row(
        sample_chat(), msgs, "high_value", "Alice",
        build_participants_json(sample_chat(), CONTACTS),
    )
    await upsert_thread(db, row)
    row["title"] = "Alice (updated)"
    await upsert_thread(db, row)
    count = await db.fetchval("SELECT COUNT(*) FROM threads")
    assert count == 1
    title = await db.fetchval(
        "SELECT title FROM threads WHERE thread_id = $1", "chat-guid-1"
    )
    assert title == "Alice (updated)"


# ---------------------------------------------------------------------------
# upsert_memory (async, uses DB fixture)
# ---------------------------------------------------------------------------

async def test_upsert_memory_inserts(db: asyncpg.Connection):
    row = _row()
    assert row is not None
    inserted = await upsert_memory(db, row)
    assert inserted is True
    count = await db.fetchval("SELECT COUNT(*) FROM memories")
    assert count == 1


async def test_upsert_memory_duplicate_skipped(db: asyncpg.Connection):
    row = _row()
    assert row is not None
    await upsert_memory(db, row)
    inserted = await upsert_memory(db, row)
    assert inserted is False
    count = await db.fetchval("SELECT COUNT(*) FROM memories")
    assert count == 1


async def test_upsert_memory_body_stored(db: asyncpg.Connection):
    row = _row()
    assert row is not None
    await upsert_memory(db, row)
    body = await db.fetchval("SELECT body FROM memories WHERE source_id = $1", "msg-guid-1")
    assert body == "Hello"


# ---------------------------------------------------------------------------
# log_run (async, uses DB fixture)
# ---------------------------------------------------------------------------

async def test_log_run_inserts(db: asyncpg.Connection):
    await log_run(db, Path("/fake/chat.db"), 42, 3, "complete", None)
    row = await db.fetchrow("SELECT * FROM ingestion_log")
    assert row is not None
    assert row["record_count"] == 42
    assert row["thread_count"] == 3
    assert row["status"] == "complete"


async def test_log_run_with_notes(db: asyncpg.Connection):
    await log_run(db, Path("/fake/chat.db"), 0, 0, "failed", "connection refused")
    notes = await db.fetchval("SELECT notes FROM ingestion_log")
    assert notes == "connection refused"


# ---------------------------------------------------------------------------
# ingest end-to-end (async, uses DB fixture + temp SQLite file)
# ---------------------------------------------------------------------------

async def test_ingest_full(db: asyncpg.Connection, tmp_path: Path):
    db_file = tmp_path / "chat.db"
    conn = sqlite3.connect(str(db_file))
    conn.executescript(CHAT_DB_SCHEMA)
    conn.execute("INSERT INTO chat VALUES (1,'chat-guid-1','+15551234567','','iMessage')")
    conn.execute("INSERT INTO handle VALUES (1,'+15551234567','iMessage')")
    conn.execute("INSERT INTO message VALUES (1,'msg-guid-1','Hello',NULL,0,1000000000000000000,1)")
    conn.execute("INSERT INTO chat_handle_join VALUES (1,1)")
    conn.execute("INSERT INTO chat_message_join VALUES (1,1)")
    conn.commit()
    conn.close()

    inserted, upserted = await ingest(db, db_file, CONTACTS, contacts_only=False)
    assert inserted == 1
    assert upserted == 1


async def test_ingest_idempotent(db: asyncpg.Connection, tmp_path: Path):
    db_file = tmp_path / "chat.db"
    conn = sqlite3.connect(str(db_file))
    conn.executescript(CHAT_DB_SCHEMA)
    conn.execute("INSERT INTO chat VALUES (1,'chat-guid-1','+15551234567','','iMessage')")
    conn.execute("INSERT INTO handle VALUES (1,'+15551234567','iMessage')")
    conn.execute("INSERT INTO message VALUES (1,'msg-guid-1','Hello',NULL,0,1000000000000000000,1)")
    conn.execute("INSERT INTO chat_handle_join VALUES (1,1)")
    conn.execute("INSERT INTO chat_message_join VALUES (1,1)")
    conn.commit()
    conn.close()

    await ingest(db, db_file, CONTACTS, contacts_only=False)
    inserted2, _ = await ingest(db, db_file, CONTACTS, contacts_only=False)
    assert inserted2 == 0


async def test_ingest_contacts_only_skips_low_value(db: asyncpg.Connection, tmp_path: Path):
    db_file = tmp_path / "chat.db"
    conn = sqlite3.connect(str(db_file))
    conn.executescript(CHAT_DB_SCHEMA)
    conn.execute("INSERT INTO chat VALUES (1,'chat-guid-1','+19990000000','','iMessage')")
    conn.execute("INSERT INTO handle VALUES (1,'+19990000000','iMessage')")
    conn.execute("INSERT INTO message VALUES (1,'msg-guid-1','Hello',NULL,0,1000000000000000000,1)")
    conn.execute("INSERT INTO chat_handle_join VALUES (1,1)")
    conn.execute("INSERT INTO chat_message_join VALUES (1,1)")
    conn.commit()
    conn.close()

    inserted, upserted = await ingest(db, db_file, CONTACTS, contacts_only=True)
    assert inserted == 0
    assert upserted == 0


# ---------------------------------------------------------------------------
# Fixtures for SQLite helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def make_db() -> sqlite3.Connection:
    conn = make_chat_db()
    conn.execute("INSERT INTO chat VALUES (1,'chat-guid-1','+15551234567','','iMessage')")
    conn.execute("INSERT INTO handle VALUES (1,'+15551234567','iMessage')")
    conn.execute("INSERT INTO message VALUES (1,'msg-guid-1','Hello',NULL,0,1000000000000000000,1)")
    conn.execute("INSERT INTO chat_handle_join VALUES (1,1)")
    conn.execute("INSERT INTO chat_message_join VALUES (1,1)")
    conn.commit()
    return conn


@pytest.fixture
def make_db_sent() -> sqlite3.Connection:
    conn = make_chat_db()
    conn.execute("INSERT INTO chat VALUES (1,'chat-guid-1','+15551234567','','iMessage')")
    conn.execute("INSERT INTO handle VALUES (1,'+15551234567','iMessage')")
    conn.execute("INSERT INTO message VALUES (1,'msg-guid-1','Hi',NULL,1,1000000000000000000,1)")
    conn.execute("INSERT INTO chat_handle_join VALUES (1,1)")
    conn.execute("INSERT INTO chat_message_join VALUES (1,1)")
    conn.commit()
    return conn


@pytest.fixture
def make_db_no_participants() -> sqlite3.Connection:
    conn = make_chat_db()
    conn.execute("INSERT INTO chat VALUES (1,'chat-guid-1','+15551234567','','iMessage')")
    conn.commit()
    return conn
