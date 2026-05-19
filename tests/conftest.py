import subprocess
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

TEST_DB = "lore_test"
TEST_DSN = f"postgresql://localhost/{TEST_DB}"
SCHEMA = Path(__file__).parent.parent / "sql" / "schema.sql"


@pytest.fixture(scope="session", autouse=True)
def lore_test_db():
    """Create lore_test DB with schema once per session; drop it on teardown."""
    subprocess.run(["dropdb", "--if-exists", TEST_DB], check=True)
    subprocess.run(["createdb", TEST_DB], check=True)
    subprocess.run(
        ["psql", "-d", TEST_DB, "-f", str(SCHEMA)],
        check=True,
        capture_output=True,
    )
    yield
    subprocess.run(["dropdb", "--if-exists", TEST_DB], check=True)


@pytest_asyncio.fixture
async def db() -> asyncpg.Connection:
    """Async DB connection with tables truncated before each test."""
    conn = await asyncpg.connect(TEST_DSN)
    await conn.execute("TRUNCATE memories, threads, ingestion_log CASCADE")
    yield conn
    await conn.close()
