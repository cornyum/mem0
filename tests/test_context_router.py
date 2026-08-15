"""Context router contract tests (design §5.3): /v1/memory/* endpoints,
three-state /health/ready, /v1/capabilities, and the ContextError → HTTP
status mapping (409/501/422).
"""

import os
import sys

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import context_runtime  # noqa: E402
import server_state  # noqa: E402
from auth import verify_auth  # noqa: E402
from mem0.context.power_memory import PowerMemory  # noqa: E402
from mem0.context.readiness import ReadinessRegistry  # noqa: E402
from mem0.context.store import ContextStore  # noqa: E402
from routers import context_router  # noqa: E402


@pytest.fixture(autouse=True)
def _telemetry_off(monkeypatch):
    monkeypatch.setattr("mem0.memory.main.MEM0_TELEMETRY", False, raising=False)
    monkeypatch.setattr("mem0.memory.telemetry.MEM0_TELEMETRY", False, raising=False)


@pytest.fixture
def client(tmp_path, monkeypatch):
    import db

    engine = create_engine(f"sqlite:///{tmp_path / 'router.db'}", connect_args={"timeout": 30})
    # The app-db readiness probe reads db.engine at check time; point it at
    # the test database so /health/ready exercises the real probe path.
    monkeypatch.setattr(db, "engine", engine)
    store = ContextStore(engine)
    store.create_tables()

    memory = PowerMemory.from_config(
        {
            "llm": {"provider": "null"},
            "embedder": {"provider": "null"},
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": "router_test",
                    "path": str(tmp_path / "qdrant"),
                    "embedding_model_dims": 8,
                },
            },
            "history_db_path": str(tmp_path / "history.db"),
        },
        ctx_store=store,
    )
    server_state._memory_instance = memory
    context_runtime.set_memory_instance(memory)

    app = FastAPI()
    app.include_router(context_router.router)
    app.dependency_overrides[verify_auth] = lambda: None
    with TestClient(app) as test_client:
        yield test_client

    server_state._memory_instance = None
    context_runtime.reset_context_runtime()


def test_remember_noop_and_created(client):
    body = {"user_id": "u1", "text": "路由层事实", "kind": "fact", "mode": "append"}
    first = client.post("/v1/memory/remember", json=body)
    assert first.status_code == 200
    assert first.json()["outcome"] == "created"
    assert first.json()["pending_embed"] is True

    second = client.post("/v1/memory/remember", json=body)
    assert second.status_code == 200
    assert second.json()["outcome"] == "noop"


def test_remember_validation_error_is_422(client):
    response = client.post("/v1/memory/remember", json={"user_id": "u1", "text": "x", "kind": ""})
    assert response.status_code == 422


def test_scope_required(client):
    response = client.post("/v1/memory/remember", json={"text": "无身份"})
    # scope passes pydantic (all None) but ScopeIdentity rejects at runtime
    assert response.status_code == 422


def test_extract_capability_error_is_501(client):
    response = client.post(
        "/v1/memory/remember",
        json={"user_id": "u1", "text": "x", "mode": "extract"},
    )
    assert response.status_code == 501
    assert "extract" in response.json()["detail"]


def test_cas_conflict_is_409(client):
    client.post("/v1/memory/remember", json={"user_id": "u1", "text": "第一条"})
    response = client.post(
        "/v1/memory/remember",
        json={"user_id": "u1", "text": "第二条", "expected_revision": 0},
    )
    assert response.status_code == 409


def test_retire_reactivate_changes_expand_cycle(client):
    created = client.post("/v1/memory/remember", json={"user_id": "u1", "text": "全周期"}).json()
    entry = created["entry"]

    retired = client.post(
        "/v1/memory/retire",
        json={"user_id": "u1", "entry_id": entry["entry_id"], "reason": "过时"},
    )
    assert retired.status_code == 200
    assert retired.json()["outcome"] == "updated"

    reactivated = client.post("/v1/memory/reactivate", json={"user_id": "u1", "entry_id": entry["entry_id"]})
    assert reactivated.json()["outcome"] == "updated"

    changes = client.post("/v1/memory/changes", json={"user_id": "u1"})
    assert changes.status_code == 200
    assert len(changes.json()["changes"]) == 1

    expanded = client.post(
        "/v1/memory/expand",
        json={
            "user_id": "u1",
            "citation": {
                "artifact_id": created["artifact_id"],
                "entry_id": entry["entry_id"],
                "entry_version_id": entry["entry_version_id"],
            },
        },
    )
    assert expanded.status_code == 200
    assert expanded.json()["text"] == "全周期"


def test_expand_unknown_version_is_404(client):
    created = client.post("/v1/memory/remember", json={"user_id": "u1", "text": "锚"}).json()
    response = client.post(
        "/v1/memory/expand",
        json={
            "user_id": "u1",
            "citation": {
                "artifact_id": created["artifact_id"],
                "entry_id": created["entry"]["entry_id"],
                "entry_version_id": "missing-version",
            },
        },
    )
    assert response.status_code == 404


def test_expand_tampered_evidence_is_410(client):
    from sqlalchemy import update

    created = client.post(
        "/v1/memory/remember", json={"user_id": "u1", "text": "防篡改锚点"}
    ).json()
    store = server_state._memory_instance.ctx_store
    versions = store.metadata.tables[store.names["entry_versions"]]
    with store.engine.begin() as conn:
        conn.execute(
            update(versions)
            .where(versions.c.entry_version_id == created["entry"]["entry_version_id"])
            .values(text="被篡改")
        )

    response = client.post(
        "/v1/memory/expand",
        json={
            "user_id": "u1",
            "citation": {
                "artifact_id": created["artifact_id"],
                "entry_id": created["entry"]["entry_id"],
                "entry_version_id": created["entry"]["entry_version_id"],
            },
        },
    )
    assert response.status_code == 410


def test_health_ready_reports_state(client):
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] in ("ready", "degraded")
    names = {c["name"] for c in body["checks"]}
    assert {"app_db", "vector_store", "llm", "embedder", "reranker"} <= names


def test_health_not_ready_is_503(client):
    from mem0.context.readiness import Probe

    class _Down(Probe):
        def __init__(self):
            super().__init__("app_db", blocking=True)

        def check(self):
            raise RuntimeError("down")

    registry = ReadinessRegistry()
    registry.register(_Down())
    context_runtime._readiness = registry
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["state"] == "not_ready"
    assert response.headers["X-Readiness"] == "not_ready"


def test_capabilities_reflect_null_providers(client):
    response = client.get("/v1/capabilities")
    assert response.status_code == 200
    caps = response.json()["memory"]
    assert caps["explicit_lifecycle"] is True
    assert caps["extraction"] is False  # llm provider = null
    assert caps["embedding"] is False
    assert caps["rerank"] is False
    assert caps["keyword_search"] is True  # qdrant implements keyword_search


def test_recall_endpoint_contract(client):
    """Router fixture runs the null embedder: remember lands authoritative
    (pending), recall falls back to the keyword/FTS path and merges the
    pending entry as stale — the read-your-writes contract end to end."""
    created = client.post(
        "/v1/memory/remember", json={"user_id": "u1", "text": "路由召回验证事实"}
    ).json()
    assert created["pending_embed"] is True

    response = client.post(
        "/v1/memory/recall",
        json={"user_id": "u1", "query": "召回验证", "mode": "keyword", "limit": 5},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["search_mode"] == "keyword"
    assert isinstance(body["results"], list)
    ids = {(item.get("metadata") or {}).get("entry_id") for item in body["results"]}
    assert created["entry"]["entry_id"] in ids
    merged = next(
        item for item in body["results"]
        if (item.get("metadata") or {}).get("entry_id") == created["entry"]["entry_id"]
    )
    assert merged["stale"] is True
    assert merged["matched_by"] == ["fts_sidecar"]


def test_recall_validation_errors(client):
    missing_scope = client.post("/v1/memory/recall", json={"query": "x"})
    assert missing_scope.status_code == 422
    bad_mode = client.post(
        "/v1/memory/recall", json={"user_id": "u1", "query": "x", "mode": "banana"}
    )
    assert bad_mode.status_code == 422
    semantic_without_embedder = client.post(
        "/v1/memory/recall", json={"user_id": "u1", "query": "x", "mode": "semantic"}
    )
    assert semantic_without_embedder.status_code == 501
