"""Tests for the categories router (custom memory taxonomy management).

Covers the PRD 3.1 contract: GET empty/defined/corrupt states, PUT create and
replace with the frozen validation rules (400 on >50 items, empty/oversized
names, oversized descriptions, duplicate names after strip), the persisted
Settings payload shape (key=memory_categories), the dialect-aware upsert
(mysql on_duplicate_key_update vs postgresql on_conflict_do_update), and the
admin-only guard on PUT.
"""

import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.dialects import mysql as mysql_dialect  # noqa: E402
from sqlalchemy.dialects import postgresql as pg_dialect  # noqa: E402

# server/ modules use bare imports (from auth import ...), so the server
# directory itself must be importable, mirroring how it runs in Docker.
_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from auth import require_admin, verify_auth  # noqa: E402
from db import get_db  # noqa: E402
from models import Settings, User  # noqa: E402
from routers import categories as categories_router  # noqa: E402

CATEGORIES_SETTINGS_KEY = "memory_categories"


class _FakeBind:
    def __init__(self, name):
        self.dialect = SimpleNamespace(name=name)


class _FakeSession:
    """In-memory stand-in for a Session bound to the Settings table.

    Records executed statements so tests can inspect the upsert shape and the
    serialized payload without a live database.
    """

    def __init__(self, stored=None, dialect="postgresql"):
        self._stored = dict(stored or {})
        self.bind = _FakeBind(dialect)
        self.executed = []
        self.committed = 0

    def get(self, _model, ident):
        return self._stored.get(ident)

    def execute(self, stmt):
        self.executed.append(stmt)

    def commit(self):
        self.committed += 1


def _admin_user():
    return User(id=uuid.uuid4(), name="t", email="t@e.com", password_hash="x", role="admin")


def _inserted_value(session):
    """Extract the serialized value from the single upsert the router executed."""
    assert len(session.executed) == 1
    params = session.executed[0].compile(dialect=pg_dialect.dialect()).params
    assert params["key"] == CATEGORIES_SETTINGS_KEY
    return params["value"]


@pytest.fixture
def session():
    return _FakeSession()


@pytest.fixture
def client(session):
    app = FastAPI()
    app.include_router(categories_router.router)
    app.dependency_overrides[verify_auth] = lambda: _admin_user()
    app.dependency_overrides[require_admin] = lambda: _admin_user()
    app.dependency_overrides[get_db] = lambda: session
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# GET /categories
# ---------------------------------------------------------------------------


def test_get_empty_returns_empty_state(client):
    resp = client.get("/categories")
    assert resp.status_code == 200
    assert resp.json() == {"categories": [], "updated_at": None}


def test_get_returns_stored_categories(client, session):
    session._stored[CATEGORIES_SETTINGS_KEY] = Settings(
        key=CATEGORIES_SETTINGS_KEY,
        value=json.dumps(
            {"categories": [{"name": "健康", "description": "饮食与运动"}, {"name": "工作", "description": ""}],
             "updated_at": "2026-01-01T00:00:00+00:00"},
            ensure_ascii=False,
        ),
    )
    resp = client.get("/categories")
    assert resp.status_code == 200
    assert resp.json() == {
        "categories": [{"name": "健康", "description": "饮食与运动"}, {"name": "工作", "description": ""}],
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


def test_get_corrupt_json_treated_as_undefined(client, session):
    session._stored[CATEGORIES_SETTINGS_KEY] = Settings(key=CATEGORIES_SETTINGS_KEY, value="not-json{{{")
    resp = client.get("/categories")
    assert resp.status_code == 200
    assert resp.json() == {"categories": [], "updated_at": None}


# ---------------------------------------------------------------------------
# PUT /categories: create / replace
# ---------------------------------------------------------------------------


def test_put_creates_and_persists_taxonomy(client, session):
    import unittest.mock as mock

    with mock.patch("server_state.update_config"):
        resp = client.put(
            "/categories",
            json={"categories": [{"name": "  健康  ", "description": "饮食与运动"}, {"name": "工作", "description": None}]},
        )
    assert resp.status_code == 200
    assert resp.json() == {"message": "分类已更新", "count": 2}
    assert session.committed == 1

    stored = json.loads(_inserted_value(session))
    # Names are stripped; missing descriptions normalize to empty string.
    assert stored["categories"] == [
        {"name": "健康", "description": "饮食与运动"},
        {"name": "工作", "description": ""},
    ]
    assert stored["updated_at"]


def test_put_round_trip_via_get(client, session):
    resp = client.put("/categories", json={"categories": [{"name": "健康", "description": "饮食与运动"}]})
    assert resp.status_code == 200

    value = _inserted_value(session)
    read_session = _FakeSession({CATEGORIES_SETTINGS_KEY: Settings(key=CATEGORIES_SETTINGS_KEY, value=value)})
    app = FastAPI()
    app.include_router(categories_router.router)
    app.dependency_overrides[verify_auth] = lambda: _admin_user()
    app.dependency_overrides[get_db] = lambda: read_session
    resp = TestClient(app).get("/categories")
    assert resp.status_code == 200
    body = resp.json()
    assert body["categories"] == [{"name": "健康", "description": "饮食与运动"}]
    assert body["updated_at"]


def test_put_replaces_existing_taxonomy(client, session):
    session._stored[CATEGORIES_SETTINGS_KEY] = Settings(
        key=CATEGORIES_SETTINGS_KEY,
        value=json.dumps({"categories": [{"name": "旧分类", "description": ""}], "updated_at": "2025-01-01T00:00:00+00:00"}),
    )
    resp = client.put("/categories", json={"categories": [{"name": "新分类", "description": "d"}]})
    assert resp.status_code == 200
    assert resp.json()["count"] == 1
    stored = json.loads(_inserted_value(session))
    assert stored["categories"] == [{"name": "新分类", "description": "d"}]


def test_put_empty_list_clears_taxonomy(client, session):
    import unittest.mock as mock

    with mock.patch("server_state.update_config"):
        resp = client.put("/categories", json={"categories": []})
    assert resp.status_code == 200
    assert resp.json() == {"message": "分类已更新", "count": 0}
    stored = json.loads(_inserted_value(session))
    assert stored["categories"] == []


def test_put_upsert_uses_postgres_on_conflict(client, session):
    resp = client.put("/categories", json={"categories": [{"name": "a", "description": ""}]})
    assert resp.status_code == 200
    sql = str(session.executed[0].compile(dialect=pg_dialect.dialect()))
    assert "ON CONFLICT" in sql
    assert "DO UPDATE" in sql


def test_put_upsert_uses_mysql_on_duplicate_key():
    mysql_session = _FakeSession(dialect="mysql")
    app = FastAPI()
    app.include_router(categories_router.router)
    app.dependency_overrides[verify_auth] = lambda: _admin_user()
    app.dependency_overrides[require_admin] = lambda: _admin_user()
    app.dependency_overrides[get_db] = lambda: mysql_session
    resp = TestClient(app).put("/categories", json={"categories": [{"name": "a", "description": ""}]})
    assert resp.status_code == 200
    sql = str(mysql_session.executed[0].compile(dialect=mysql_dialect.dialect()))
    assert "ON DUPLICATE KEY UPDATE" in sql


# ---------------------------------------------------------------------------
# PUT /categories: validation (400)
# ---------------------------------------------------------------------------


def test_put_rejects_non_list(client, session):
    resp = client.put("/categories", json={"categories": "oops"})
    assert resp.status_code == 400
    assert session.executed == []


def test_put_rejects_more_than_50(client, session):
    resp = client.put(
        "/categories", json={"categories": [{"name": f"c{i}", "description": ""} for i in range(51)]}
    )
    assert resp.status_code == 400
    assert session.executed == []


def test_put_accepts_exactly_50(client):
    resp = client.put(
        "/categories", json={"categories": [{"name": f"c{i}", "description": ""} for i in range(50)]}
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 50


def test_put_rejects_empty_or_whitespace_name(client, session):
    for bad_name in ("", "   "):
        resp = client.put("/categories", json={"categories": [{"name": bad_name, "description": ""}]})
        assert resp.status_code == 400
    resp = client.put("/categories", json={"categories": [{"description": "no name"}]})
    assert resp.status_code == 400
    assert session.executed == []


def test_put_rejects_oversized_name(client, session):
    resp = client.put("/categories", json={"categories": [{"name": "x" * 65, "description": ""}]})
    assert resp.status_code == 400
    assert session.executed == []


def test_put_rejects_oversized_description(client, session):
    resp = client.put("/categories", json={"categories": [{"name": "ok", "description": "d" * 201}]})
    assert resp.status_code == 400
    assert session.executed == []


def test_put_rejects_duplicate_names_after_strip(client, session):
    resp = client.put(
        "/categories",
        json={"categories": [{"name": "健康", "description": ""}, {"name": "  健康  ", "description": "d"}]},
    )
    assert resp.status_code == 400
    assert session.executed == []


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_put_non_admin_gets_403(session):
    def _forbidden():
        raise HTTPException(status_code=403, detail="Admin role required.")

    app = FastAPI()
    app.include_router(categories_router.router)
    app.dependency_overrides[verify_auth] = lambda: _admin_user()
    app.dependency_overrides[require_admin] = _forbidden
    app.dependency_overrides[get_db] = lambda: session
    resp = TestClient(app).put("/categories", json={"categories": [{"name": "a", "description": ""}]})
    assert resp.status_code == 403
    assert session.executed == []


def test_get_unauthenticated_gets_401(session):
    def _unauthorized():
        raise HTTPException(status_code=401, detail="Authentication required.")

    app = FastAPI()
    app.include_router(categories_router.router)
    app.dependency_overrides[verify_auth] = _unauthorized
    app.dependency_overrides[get_db] = lambda: session
    resp = TestClient(app).get("/categories")
    assert resp.status_code == 401


def test_put_refreshes_memory_config(client, session):
    """PUT must rebuild the Memory instance so the taxonomy takes effect without a restart."""
    import unittest.mock as mock

    body = {"categories": [{"name": "工作", "description": "职业相关信息"}]}
    with mock.patch("server_state.update_config") as refresh:
        resp = client.put("/categories", json=body)
    assert resp.status_code == 200
    refresh.assert_called_once_with({})
