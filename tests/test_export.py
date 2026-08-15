"""Tests for the /export router (memory export as JSON/CSV download).

The export router is mounted on a minimal FastAPI app (same pattern as
test_api_keys_router.py) so the tests stay independent of server/main.py route
registration order.

Permission note: require_admin is exercised via dependency_overrides only.
Negative auth cases (401/403) are not asserted here because the CI environment
runs without JWT_SECRET / a users table (AUTH_DISABLED-style setup); the admin
gate itself lives in auth.require_admin and is covered by its own suite.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# conftest.py puts the server/ directory on sys.path, so router modules resolve
# their bare sibling imports (from auth import ...) the same way they do in Docker.
from auth import require_admin  # noqa: E402
from models import User  # noqa: E402
from routers import export as export_router  # noqa: E402

SERIALIZED_FIELDS = {
    "id",
    "memory",
    "user_id",
    "agent_id",
    "run_id",
    "tenant_id",
    "session_id",
    "hash",
    "expiration_date",
    "metadata",
    "created_at",
    "updated_at",
}


def _row(row_id, data="likes pizza", categories=None, user_id="u1"):
    payload = {
        "data": data,
        "user_id": user_id,
        "created_at": "2026-01-02T03:04:05+00:00",
        "updated_at": "2026-01-02T03:04:05+00:00",
    }
    if categories is not None:
        payload["categories"] = categories
    return SimpleNamespace(id=row_id, payload=payload)


@pytest.fixture
def mock_memory():
    memory = MagicMock()
    memory.vector_store.list.return_value = []
    return memory


@pytest.fixture
def client(mock_memory):
    app = FastAPI()
    app.include_router(export_router.router)
    fake_admin = User(id=uuid.uuid4(), name="t", email="t@e.com", password_hash="x", role="admin")
    app.dependency_overrides[require_admin] = lambda: fake_admin
    with patch.object(export_router, "get_memory_instance", return_value=mock_memory):
        yield TestClient(app, raise_server_exceptions=False)


# ===========================================================================
# JSON export
# ===========================================================================


class TestJsonExport:
    def test_structure_meta_and_headers(self, client, mock_memory):
        mock_memory.vector_store.list.side_effect = [[_row("m-1"), _row("m-2")], []]

        resp = client.get("/export")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        assert resp.headers["content-disposition"].startswith('attachment; filename="agentar-memories-')
        assert resp.headers["content-disposition"].endswith('.json"')

        body = resp.json()
        assert set(body.keys()) == {"meta", "memories"}
        assert set(body["meta"].keys()) == {"exported_at", "filters", "total", "truncated"}
        assert body["meta"]["exported_at"]
        assert body["meta"]["filters"] == {}
        assert body["meta"]["total"] == 2
        assert body["meta"]["truncated"] is False
        assert len(body["memories"]) == 2

        first = body["memories"][0]
        assert set(first.keys()) == SERIALIZED_FIELDS
        assert first["id"] == "m-1"
        assert first["memory"] == "likes pizza"
        assert first["user_id"] == "u1"
        assert first["created_at"] == "2026-01-02T03:04:05+00:00"

    def test_empty_store_exports_empty_file(self, client, mock_memory):
        mock_memory.vector_store.list.side_effect = [[]]

        resp = client.get("/export")

        assert resp.status_code == 200
        body = resp.json()
        assert body["meta"]["total"] == 0
        assert body["memories"] == []

    def test_keyset_pagination_advances_until_short_batch(self, client, mock_memory):
        # Full batches drive keyset pagination; a short batch is the final page.
        batch1 = [_row(f"k-{i:04d}") for i in range(500)]
        batch2 = [_row(f"k-{i:04d}") for i in range(500, 502)]
        mock_memory.vector_store.list.side_effect = [batch1, batch2]

        resp = client.get("/export")

        assert resp.status_code == 200
        body = resp.json()
        assert body["meta"]["total"] == 502
        assert body["meta"]["truncated"] is False
        assert mock_memory.vector_store.list.call_count == 2
        # Second call must advance the keyset cursor to the last id of batch 1.
        _, kwargs = mock_memory.vector_store.list.call_args_list[1]
        assert kwargs["after_id"] == "k-0499"
        for _, kwargs in mock_memory.vector_store.list.call_args_list:
            assert kwargs["top_k"] == 500

    def test_stalled_cursor_dedupes_and_flags_truncation(self, client, mock_memory):
        # A pathological store that ignores the cursor and replays the same
        # full batch forever: rows must not duplicate, the loop must stay
        # bounded by the batch budget, and truncation must be signalled.
        batch = [_row(f"s-{i:04d}") for i in range(500)]
        mock_memory.vector_store.list.side_effect = lambda **kwargs: batch

        resp = client.get("/export")

        assert resp.status_code == 200
        body = resp.json()
        assert body["meta"]["total"] == 500  # deduplicated
        assert body["meta"]["truncated"] is True
        assert resp.headers.get("x-agentar-truncated") == "true"
        assert mock_memory.vector_store.list.call_count == export_router.MAX_EXPORT_BATCHES

    def test_scope_filters_forwarded_to_vector_store(self, client, mock_memory):
        mock_memory.vector_store.list.side_effect = [[]]

        resp = client.get("/export?user_id=u1&agent_id=a1")

        assert resp.status_code == 200
        _, kwargs = mock_memory.vector_store.list.call_args
        assert kwargs["filters"] == {"user_id": "u1", "agent_id": "a1"}
        assert kwargs["top_k"] == 500
        assert resp.json()["meta"]["filters"] == {"user_id": "u1", "agent_id": "a1"}

    def test_nested_list_return_shape_unpacked(self, client, mock_memory):
        # Some vector stores return [rows] instead of rows; both must work.
        mock_memory.vector_store.list.side_effect = [[[_row("m-1"), _row("m-2")]], []]

        resp = client.get("/export")

        assert resp.status_code == 200
        assert resp.json()["meta"]["total"] == 2

    def test_no_scope_calls_list_without_filters(self, client, mock_memory):
        mock_memory.vector_store.list.side_effect = [[]]

        resp = client.get("/export")

        assert resp.status_code == 200
        _, kwargs = mock_memory.vector_store.list.call_args
        assert "filters" not in kwargs
        assert resp.json()["meta"]["filters"] == {}


# ===========================================================================
# Category filtering (in-memory, exact match)
# ===========================================================================


class TestCategoryFilter:
    def test_only_matching_category_exported(self, client, mock_memory):
        rows = [
            _row("m-1", data="晨跑五公里", categories=["健康", "饮食"]),
            _row("m-2", data="写周报", categories=["工作"]),
            _row("m-3", data="无分类记忆"),
        ]
        mock_memory.vector_store.list.side_effect = [rows, []]

        resp = client.get("/export?category=健康")

        assert resp.status_code == 200
        body = resp.json()
        assert body["meta"]["total"] == 1
        assert body["memories"][0]["id"] == "m-1"
        assert body["memories"][0]["metadata"]["categories"] == ["健康", "饮食"]
        # category is filtered in memory, never pushed into vector-store filters
        _, kwargs = mock_memory.vector_store.list.call_args
        assert "filters" not in kwargs
        assert body["meta"]["filters"] == {"category": "健康"}

    def test_category_without_match_exports_nothing(self, client, mock_memory):
        mock_memory.vector_store.list.side_effect = [[_row("m-1", categories=["工作"])], []]

        resp = client.get("/export?category=健康")

        assert resp.status_code == 200
        assert resp.json()["meta"]["total"] == 0


# ===========================================================================
# CSV export
# ===========================================================================


class TestCsvExport:
    def test_bom_header_and_rows(self, client, mock_memory):
        rows = [_row("m-1", data="晨跑五公里", categories=["健康", "饮食"]), _row("m-2", data="likes pizza")]
        mock_memory.vector_store.list.side_effect = [rows, []]

        resp = client.get("/export?format=csv")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv; charset=utf-8")
        assert resp.headers["content-disposition"].endswith('.csv"')

        text = resp.content.decode("utf-8")
        assert text.startswith("\ufeff")

        lines = text.lstrip("\ufeff").splitlines()
        assert lines[0] == "id,memory,categories,user_id,agent_id,run_id,tenant_id,session_id,created_at,updated_at"
        assert lines[1] == "m-1,晨跑五公里,健康;饮食,u1,,,,,2026-01-02T03:04:05+00:00,2026-01-02T03:04:05+00:00"
        assert lines[2] == "m-2,likes pizza,,u1,,,,,2026-01-02T03:04:05+00:00,2026-01-02T03:04:05+00:00"

    def test_formula_injection_sanitized(self, client, mock_memory):
        rows = [
            _row("m-1", data="=SUM(A1:A2)"),
            _row("m-2", data="-cmd"),
            _row("m-3", data="+REF(x)"),
            _row("m-4", data="@evil"),
            _row("m-5", data="\ttab-prefixed"),
            _row("m-6", data="plain text"),
        ]
        mock_memory.vector_store.list.side_effect = [rows, []]

        resp = client.get("/export?format=csv")

        assert resp.status_code == 200
        lines = resp.content.decode("utf-8").lstrip("\ufeff").splitlines()
        assert lines[1].startswith("m-1,'=SUM(A1:A2)")
        assert lines[2].startswith("m-2,'-cmd")
        assert lines[3].startswith("m-3,'+REF(x)")
        assert lines[4].startswith("m-4,'@evil")
        assert lines[5].startswith("m-5,'\ttab-prefixed")
        assert lines[6].startswith("m-6,plain text")


# ===========================================================================
# Validation and error mapping
# ===========================================================================


class TestValidationAndErrors:
    def test_invalid_format_returns_400(self, client, mock_memory):
        resp = client.get("/export?format=xml")
        assert resp.status_code == 400
        assert "format" in resp.json()["detail"]

    def test_vector_store_failure_returns_502(self, client, mock_memory):
        mock_memory.vector_store.list.side_effect = RuntimeError("vector store unreachable")
        resp = client.get("/export")
        assert resp.status_code == 502


# ===========================================================================
# Admin dependency wiring
# ===========================================================================


class TestAdminGate:
    def test_endpoint_requires_admin_dependency(self, mock_memory):
        """The route must depend on auth.require_admin. Proven by overriding
        require_admin itself: the override only runs if the very same function
        object is in the route's dependency chain. (Negative 401/403 paths need
        a JWT/users-table environment and are not covered here.)"""
        app = FastAPI()
        app.include_router(export_router.router)
        calls = []

        def _admin():
            calls.append(True)
            return User(id=uuid.uuid4(), name="t", email="t@e.com", password_hash="x", role="admin")

        app.dependency_overrides[require_admin] = _admin
        with patch.object(export_router, "get_memory_instance", return_value=mock_memory):
            client = TestClient(app)
            resp = client.get("/export")

        assert resp.status_code == 200
        assert calls == [True]


class TestCursorPaginationSemantics:
    """Non-mock-assumption coverage for the keyset pagination fix (review B1).

    The fakes here emulate real store behaviour (server-side filtering by
    after_id) instead of scripted side_effect sequences, so the tests assert
    how _collect_rows interacts with an actual paginating store.
    """

    @staticmethod
    def _keyset_store(total, batch_size=500, in_memory=None):
        rows = in_memory if in_memory is not None else [_row(f"r-{i:05d}") for i in range(total)]

        def fake_list(**kwargs):
            after_id = kwargs.get("after_id")
            remaining = rows if after_id is None else [r for r in rows if str(r.id) > after_id]
            return [remaining[: kwargs.get("top_k", batch_size)]]

        return fake_list

    def test_full_collection_reached_beyond_first_batch(self, client, mock_memory):
        # 1200 rows in a paginating store: export must collect all of them
        # (the pre-fix implementation silently stopped at 500).
        mock_memory.vector_store.list.side_effect = self._keyset_store(1200)

        resp = client.get("/export")

        assert resp.status_code == 200
        body = resp.json()
        assert body["meta"]["total"] == 1200
        assert body["meta"]["truncated"] is False
        ids = [m["id"] for m in body["memories"]]
        assert len(set(ids)) == 1200  # no duplicates across keyset pages
        assert mock_memory.vector_store.list.call_count == 3  # 500 + 500 + 200

    def test_store_without_cursor_support_degrades_with_signal(self, client, mock_memory):
        # A store whose list() rejects after_id (TypeError) can only serve the
        # first batch; a full batch must be reported as truncated, never silent.
        rows = [_row(f"n-{i:05d}") for i in range(800)]

        def no_cursor_list(**kwargs):
            if "after_id" in kwargs:
                raise TypeError("list() got an unexpected keyword argument 'after_id'")
            return [rows[: kwargs.get("top_k", 500)]]

        mock_memory.vector_store.list.side_effect = no_cursor_list

        resp = client.get("/export")

        assert resp.status_code == 200
        body = resp.json()
        assert body["meta"]["total"] == 500
        assert body["meta"]["truncated"] is True
        assert resp.headers.get("x-agentar-truncated") == "true"

    def test_batch_budget_exhaustion_flags_truncation(self, client, mock_memory):
        # An endless paginating store: the 200-batch budget caps the export and
        # must signal truncation instead of implying completeness. Rows are
        # generated lazily per call — the "store" is unbounded.
        call_no = {"n": 0}

        def endless_list(**kwargs):
            call_no["n"] += 1
            base = call_no["n"] * 10**6
            return [[_row(f"e-{base + i:09d}") for i in range(kwargs.get("top_k", 500))]]

        mock_memory.vector_store.list.side_effect = endless_list

        resp = client.get("/export")

        assert resp.status_code == 200
        body = resp.json()
        assert body["meta"]["total"] == export_router.MAX_EXPORT_BATCHES * export_router.EXPORT_BATCH_SIZE
        assert body["meta"]["truncated"] is True

    def test_internal_lemmatized_text_not_leaked(self, client, mock_memory):
        row = _row("leak-1")
        row.payload["text_lemmatized"] = "user like pizza eat pizza every day"
        mock_memory.vector_store.list.side_effect = [[row]]

        resp = client.get("/export")

        assert resp.status_code == 200
        metadata = resp.json()["memories"][0]["metadata"]
        assert "text_lemmatized" not in metadata
