"""Context router contract tests (design v3 §7): /v1/memory/* endpoints,
/v1/context/prepare, three-state /health/ready, /v1/capabilities, the
ContextError → {code,message,request_id} mapping (§7.6.5), the admin
namespace, and the 501 SQL-feature boundary (§7.6.4).

The v3 router delegates to server_state.get_app_service(); the fixture
injects a MemoryApplicationService over the in-memory FakeElasticsearch so
the full HTTP contract runs without a live cluster.
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
from auth import require_admin, verify_auth  # noqa: E402
from mem0.context.vdb import MemoryApplicationService  # noqa: E402
from mem0.context.vdb.es_store import ElasticsearchMemoryStore  # noqa: E402
from routers import context_router  # noqa: E402
from tests.context.fake_es import FakeElasticsearch  # noqa: E402


class RouterEmbedder:
    def embed(self, text, action):
        seed = sum(ord(c) for c in text)
        return [((seed % 5) + 1) / 8.0, 0.5, 0.25, ((seed % 9) + 1) / 8.0]


class RouterLLM:
    """Deterministic extraction stub returning one fact."""

    def generate_response(self, messages, response_format=None):
        return '{"facts": ["用户喜欢简洁的回复"]}'


@pytest.fixture(autouse=True)
def _telemetry_off(monkeypatch):
    monkeypatch.setattr("mem0.memory.main.MEM0_TELEMETRY", False, raising=False)
    monkeypatch.setattr("mem0.memory.telemetry.MEM0_TELEMETRY", False, raising=False)


@pytest.fixture
def client(tmp_path, monkeypatch):
    import db

    engine = create_engine(f"sqlite:///{tmp_path / 'router.db'}", connect_args={"timeout": 30})
    monkeypatch.setattr(db, "engine", engine)

    store = ElasticsearchMemoryStore(FakeElasticsearch(ik_enabled=True), prefix="rt_mem", dims=4)
    service = MemoryApplicationService(store, embedder=RouterEmbedder(), llm=RouterLLM())
    server_state._app_service = service
    server_state._memory_instance = None
    context_runtime.register_v3_readiness(service)

    app = FastAPI()
    app.include_router(context_router.router)
    app.dependency_overrides[verify_auth] = lambda: None
    app.dependency_overrides[require_admin] = lambda: None
    with TestClient(app) as test_client:
        yield test_client

    server_state._app_service = None
    server_state._memory_instance = None
    context_runtime.reset_context_runtime()


def _remember(client, text, **extra):
    body = {"user_id": "u1", "text": text, "mode": "append", **extra}
    return client.post("/v1/memory/remember", json=body)


# -- remember ----------------------------------------------------------------------


def test_remember_created_and_noop(client):
    first = _remember(client, "路由层事实")
    assert first.status_code == 200
    payload = first.json()["results"][0]
    assert payload["outcome"] == "created"
    assert payload["entry"]["text"] == "路由层事实"

    second = _remember(client, "路由层事实")
    assert second.status_code == 200
    assert second.json()["results"][0]["outcome"] == "noop"


def test_remember_validation_error_is_422(client):
    response = client.post("/v1/memory/remember", json={"user_id": "u1", "text": "x", "kind": ""})
    assert response.status_code == 422  # pydantic


def test_scope_required_maps_to_validation_error(client):
    response = client.post("/v1/memory/remember", json={"text": "无身份", "mode": "append"})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert "request_id" in body


def test_extract_via_messages(client):
    response = client.post(
        "/v1/memory/remember",
        json={
            "user_id": "u1",
            "mode": "extract",
            "messages": [{"role": "user", "content": "我喜欢简洁回复"}],
        },
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 1
    assert results[0]["entry"]["text"] == "用户喜欢简洁的回复"


def test_extract_forwards_timestamp_and_prompt(client, monkeypatch):
    """The router must not drop the temporal anchor or per-call prompt."""
    captured = {}

    class StubService:
        def remember(self, *args, **kwargs):
            captured.update(kwargs)
            return {"results": []}

    monkeypatch.setattr("routers.context_router.get_app_service", lambda: StubService())
    response = client.post(
        "/v1/memory/remember",
        json={
            "user_id": "u1",
            "mode": "extract",
            "messages": [{"role": "user", "content": "I ran a marathon last week"}],
            "timestamp": "2023-05-08",
            "prompt": "Only extract temporal facts",
        },
    )
    assert response.status_code == 200
    assert captured["timestamp"] == "2023-05-08"
    assert captured["prompt"] == "Only extract temporal facts"


def test_cas_conflict_is_409_with_code(client):
    _remember(client, "第一条")
    response = client.post(
        "/v1/memory/remember",
        json={"user_id": "u1", "text": "第二条", "mode": "append", "expected_revision": 99},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "revision_conflict"


# -- revise / retire / reactivate / get -----------------------------------------------


def test_revise_endpoint(client):
    entry_id = _remember(client, "初始版本").json()["results"][0]["entry"]["entry_id"]
    response = client.post(
        "/v1/memory/revise", json={"user_id": "u1", "entry_id": entry_id, "text": "修订版本"}
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "updated"
    assert response.json()["entry"]["version"] == 2


def test_get_endpoint(client):
    entry_id = _remember(client, "点查内容").json()["results"][0]["entry"]["entry_id"]
    response = client.post("/v1/memory/get", json={"user_id": "u1", "entry_id": entry_id})
    assert response.status_code == 200
    assert response.json()["text"] == "点查内容"
    assert response.json()["state"] == "active"

    missing = client.post("/v1/memory/get", json={"user_id": "u1", "entry_id": "ghost"})
    assert missing.status_code == 404


def test_retire_reactivate_roundtrip(client):
    entry_id = _remember(client, "生命周期").json()["results"][0]["entry"]["entry_id"]
    retired = client.post("/v1/memory/retire", json={"user_id": "u1", "entry_id": entry_id})
    assert retired.json()["outcome"] == "updated"
    assert client.post("/v1/memory/get", json={"user_id": "u1", "entry_id": entry_id}).json()["state"] == "inactive"

    activated = client.post("/v1/memory/reactivate", json={"user_id": "u1", "entry_id": entry_id})
    assert activated.json()["outcome"] == "updated"
    assert client.post("/v1/memory/get", json={"user_id": "u1", "entry_id": entry_id}).json()["state"] == "active"


# -- recall / expand / changes -----------------------------------------------------------


def test_recall_endpoint_envelope(client):
    _remember(client, "用户对花生过敏")
    response = client.post(
        "/v1/memory/recall", json={"user_id": "u1", "query": "花生 过敏", "limit": 5}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["search_mode"] in ("hybrid", "semantic", "keyword")
    assert body["storage_source"] == "elasticsearch"
    assert body["degraded"] is False
    assert body["rerank_status"] == "off"
    assert body["results"]
    assert body["results"][0]["matched_by"]


def test_recall_keyword_threshold_422(client):
    response = client.post(
        "/v1/memory/recall",
        json={"user_id": "u1", "query": "q", "mode": "keyword", "threshold": 0.5},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_expand_endpoint_and_hash_guard(client):
    created = _remember(client, "可展开事实").json()["results"][0]
    citation = {
        "artifact_id": created["artifact_id"],
        "entry_id": created["entry"]["entry_id"],
        "entry_version_id": created["entry"]["entry_version_id"],
    }
    response = client.post("/v1/memory/expand", json={"user_id": "u1", "citation": citation})
    assert response.status_code == 200
    assert response.json()["text"] == "可展开事实"

    bad = dict(citation, artifact_id="wrong")
    missing = client.post("/v1/memory/expand", json={"user_id": "u1", "citation": bad})
    assert missing.status_code == 404


def test_changes_endpoint_paginates(client):
    entry_id = _remember(client, "变化事实A").json()["results"][0]["entry"]["entry_id"]
    _remember(client, "变化事实B")
    client.post("/v1/memory/retire", json={"user_id": "u1", "entry_id": entry_id})

    response = client.post("/v1/memory/changes", json={"user_id": "u1"})
    changes = response.json()["changes"]
    assert len(changes) == 3
    assert changes[-1]["entry_id"] == entry_id

    page = client.post("/v1/memory/changes", json={"user_id": "u1", "limit": 2})
    assert len(page.json()["changes"]) == 2


# -- prepare / capabilities / health --------------------------------------------------------


def test_prepare_context_endpoint(client):
    _remember(client, "准备上下文的事实")
    response = client.post(
        "/v1/context/prepare", json={"user_id": "u1", "query": "准备 上下文", "budget_bytes": 2048}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "agentar.prepared-context.v1"
    assert "BEGIN AGENTAR PREPARED CONTEXT" in body["rendered"]


def test_capabilities_v3_shape(client):
    response = client.get("/v1/capabilities")
    assert response.status_code == 200
    caps = response.json()
    assert caps["storage"]["mode"] == "only_vdb"
    assert caps["storage"]["primary_provider"] == "elasticsearch"
    assert caps["storage"]["sql_fallback_enabled"] is False
    assert caps["memory"]["extraction"] is True
    assert caps["memory"]["semantic_search"] is True
    assert caps["memory"]["keyword_search"] is True
    assert caps["sources"] is False
    assert caps["handoff"] is False


def test_health_ready_reports_es_probe(client):
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] in ("ready", "degraded")
    names = {check["name"] for check in body["checks"]}
    assert "elasticsearch" in names


# -- 501 boundary + admin namespace (§7.6.2/§7.6.4) --------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/v1/sources/content",
        "/v1/handoff/prepare",
        "/v1/handoff/commit",
        "/v1/handoff/continue",
        "/v1/artifact-candidates/propose",
        "/v1/artifact-candidates/list",
        "/v1/artifact-candidates/revise",
        "/v1/artifact-candidates/approve",
        "/v1/artifact-candidates/reject",
    ],
)
def test_sql_features_return_501(client, path):
    response = client.post(path, json={})
    assert response.status_code == 501
    assert response.json()["code"] == "capability_not_supported"


def test_admin_reconcile(client):
    _remember(client, "待对账")
    response = client.post("/v1/admin/memory/reconcile")
    assert response.status_code == 200
    assert "embedding_embedded" in response.json()


def test_admin_rebuild(client):
    _remember(client, "重建目标")
    response = client.post("/v1/admin/memory/rebuild")
    assert response.status_code == 200
    assert response.json()["heads_rebuilt"] >= 0


def test_deprecated_legacy_paths_carry_header(client):
    response = client.post("/v1/memory/reconcile")
    assert response.status_code == 200
    assert response.headers.get("deprecation") == "true"
