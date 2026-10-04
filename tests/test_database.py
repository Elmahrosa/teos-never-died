"""database.py engine-management tests.

init_db() catches every exception and falls back to an in-memory dict store.
That is the right behaviour for a bot that must keep answering, but it is
also silent: a misconfigured DATABASE_URL produces a bot that looks fine and
forgets every subscription, tier and usage counter on each restart.

These tests pin the URLs that must NOT silently degrade.
"""
import asyncio
import importlib
import sys

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Re-import database.py against a throwaway DATABASE_URL."""
    def _load(url):
        monkeypatch.setenv("DATABASE_URL", url)
        sys.modules.pop("database", None)
        return importlib.import_module("database")

    yield _load
    sys.modules.pop("database", None)


@pytest.mark.parametrize("url_name", [
    "sqlite_file",        # sqlite+aiosqlite:///<path>
    "sqlite_sync_url",    # sqlite:///<path>  - the natural spelling
    "sqlite_memory",
    "sqlite_sync_memory",
])
def test_init_db_succeeds(db, tmp_path, url_name, request):
    """Every reasonable SQLite URL must produce a real, working database."""
    urls = {
        "sqlite_file": f"sqlite+aiosqlite:///{tmp_path/'a.db'}",
        "sqlite_sync_url": f"sqlite:///{tmp_path/'b.db'}",
        "sqlite_memory": "sqlite+aiosqlite:///:memory:",
        "sqlite_sync_memory": "sqlite:///:memory:",
    }
    database = db(urls[url_name])
    assert asyncio.run(database.init_db()) is True, (
        f"{urls[url_name]} degraded to the non-persistent in-memory store"
    )
    # A real DB answers astats(); the fallback dict also does, so check the
    # engine was actually built.
    assert database._async_engine is not None


def test_sync_url_is_upgraded_to_async_driver(db, tmp_path):
    """`sqlite://` has no async driver of its own and must be rewritten."""
    database = db(f"sqlite:///{tmp_path/'c.db'}")
    assert database._async_url("sqlite:///x.db") == "sqlite+aiosqlite:///x.db"
    assert database._async_url("sqlite:///:memory:") == "sqlite+aiosqlite:///:memory:"
    assert database._async_url("postgresql://h/db") == "postgresql+asyncpg://h/db"
    # Already-correct and unrelated URLs pass through untouched.
    assert database._async_url("sqlite+aiosqlite:///x.db") == "sqlite+aiosqlite:///x.db"
    assert database._async_url("mysql+aiomysql://h/db") == "mysql+aiomysql://h/db"


def test_file_backed_sqlite_is_pooled(db, tmp_path):
    """A file-backed SQLite DB should keep a pool for connection reuse."""
    database = db(f"sqlite+aiosqlite:///{tmp_path/'d.db'}")
    asyncio.run(database.init_db())
    engine = database._async_engine
    assert engine.pool.size() >= 1
    assert engine.pool._max_overflow == 10


def test_in_memory_sqlite_is_not_pooled(db):
    """StaticPool accepts neither pool_size nor max_overflow.

    Passing them raises TypeError from create_engine(), which init_db() turns
    into the in-memory fallback - so this must be left off.
    """
    database = db("sqlite+aiosqlite:///:memory:")
    assert asyncio.run(database.init_db()) is True
    pool = database._async_engine.pool
    # A real, fixed pool - not the file-backed QueuePool configuration.
    assert type(pool).__name__ == "StaticPool", type(pool).__name__


def test_db_available_is_awaitable(db, tmp_path):
    """db_available() is a coroutine; `await database.db_available` is a bug.

    The missing parentheses in webhook_server.py returned a coroutine object
    into a dict, so the status endpoint reported a coroutine as its database
    state instead of a bool.
    """
    database = db(f"sqlite+aiosqlite:///{tmp_path/'e.db'}")
    assert asyncio.run(database.db_available()) is True

    with pytest.raises(TypeError):
        asyncio.run(database.db_available)