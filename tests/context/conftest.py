"""Shared fixtures for the context-layer test suite.

File-based SQLite (WAL + busy_timeout) rather than :memory: — the
concurrency tests run real writer races across connections, which
per-thread in-memory databases cannot provide (each connection would see
its own empty database).
"""

import pytest
from sqlalchemy import create_engine, event

from mem0.context.store import ContextStore


@pytest.fixture(autouse=True)
def _telemetry_off(monkeypatch):
    # Attribute patches (auto-restored) instead of an env var: env is
    # process-global and would leak into unrelated tests that legitimately
    # exercise the telemetry flows (e.g. test_oss_to_platform_migrate).
    monkeypatch.setattr("mem0.memory.main.MEM0_TELEMETRY", False, raising=False)
    monkeypatch.setattr("mem0.memory.telemetry.MEM0_TELEMETRY", False, raising=False)


@pytest.fixture
def engine(tmp_path):
    db_path = tmp_path / "ctx_store.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA journal_mode=WAL")
        dbapi_conn.execute("PRAGMA busy_timeout=30000")

    yield engine
    engine.dispose()


@pytest.fixture
def store(engine):
    store = ContextStore(engine)
    store.create_tables()
    return store
