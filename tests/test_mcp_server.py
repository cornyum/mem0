"""MCP projection (design §7.3): tools generated from the
MemoryApplicationService — same semantics as REST /v1 — plus the mandatory
service-level auth (acceptance §12.8: no credentials ⇒ 401)."""

import os
import sys

import pytest

pytest.importorskip("fastmcp", reason="fastmcp not installed")

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import server_state  # noqa: E402
from mcp_server import MCP_TOOLS_ENABLED, McpAuthMiddleware, build_mcp_server  # noqa: E402
from mem0.context.vdb import MemoryApplicationService  # noqa: E402
from mem0.context.vdb.es_store import ElasticsearchMemoryStore  # noqa: E402
from tests.context.fake_es import FakeElasticsearch  # noqa: E402


class McpEmbedder:
    def embed(self, text, action):
        seed = sum(ord(c) for c in text)
        return [((seed % 5) + 1) / 8.0, 0.5, 0.25, ((seed % 9) + 1) / 8.0]


@pytest.fixture(autouse=True)
def _telemetry_off(monkeypatch):
    monkeypatch.setattr("mem0.memory.main.MEM0_TELEMETRY", False, raising=False)
    monkeypatch.setattr("mem0.memory.telemetry.MEM0_TELEMETRY", False, raising=False)


def _body(res) -> dict:
    """Unwrap a fastmcp ToolResult into the underlying dict."""
    import json as _json

    data = getattr(res, "data", None)
    if isinstance(data, dict):
        return data
    return _json.loads(res.content[0].text)


@pytest.fixture
def service():
    store = ElasticsearchMemoryStore(FakeElasticsearch(ik_enabled=True), prefix="mcp_mem", dims=4)
    svc = MemoryApplicationService(store, embedder=McpEmbedder(), llm=None)
    server_state._app_service = svc
    yield svc
    server_state._app_service = None


@pytest.fixture
def mcp(service):
    return build_mcp_server()


@pytest.mark.asyncio
async def test_whitelist_tools_registered(mcp):
    tools = await mcp.list_tools()
    assert set(MCP_TOOLS_ENABLED) <= {t.name for t in tools}


@pytest.mark.asyncio
async def test_remember_recall_roundtrip(mcp):
    remember = _body(await mcp.call_tool("remember", {"text": "MCP 写入事实", "user_id": "u1", "kind": "fact"}))
    assert remember["results"][0]["outcome"] == "created"

    recall = await mcp.call_tool("recall", {"query": "MCP 写入", "user_id": "u1"})
    body = _body(recall)
    assert any("MCP 写入事实" in item["text"] for item in body["results"])


@pytest.mark.asyncio
async def test_revise_retire_changes_get(mcp):
    created = _body(await mcp.call_tool("remember", {"text": "生命周期事实", "user_id": "u1"}))
    entry_id = created["results"][0]["entry"]["entry_id"]

    revised = _body(await mcp.call_tool("revise", {"entry_id": entry_id, "text": "修订后事实", "user_id": "u1"}))
    assert revised["outcome"] == "updated"

    retired = await mcp.call_tool("retire", {"entry_id": entry_id, "user_id": "u1"})
    assert _body(retired)["outcome"] == "updated"

    changes = await mcp.call_tool("changes", {"user_id": "u1"})
    ch = _body(changes)
    assert len(ch["changes"]) == 3  # created + revised + retired

    head = await mcp.call_tool("get", {"entry_id": entry_id, "user_id": "u1"})
    assert _body(head)["state"] == "inactive"


@pytest.mark.asyncio
async def test_prepare(mcp, service):
    service.remember("MCP 准备的事实", user_id="u1")
    prepared = await mcp.call_tool("prepare", {"query": "准备", "user_id": "u1", "budget_bytes": 2000})
    out = _body(prepared)
    assert out["schema"] == "agentar.prepared-context.v1"
    assert out["rendered_bytes"] <= 2000


def test_mcp_auth_middleware_rejects_missing_credentials():
    """Acceptance §12.8: MCP requests without credentials return 401."""
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    async def endpoint(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])])
    app.add_middleware(McpAuthMiddleware)
    client = TestClient(app)

    import auth as auth_mod

    was_disabled = auth_mod.AUTH_DISABLED
    auth_mod.AUTH_DISABLED = False
    try:
        response = client.post("/", json={"x": 1})
        assert response.status_code == 401
        assert response.headers.get("www-authenticate") == "Bearer"
    finally:
        auth_mod.AUTH_DISABLED = was_disabled


def test_mcp_auth_middleware_accepts_admin_api_key(monkeypatch):
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    async def endpoint(request):
        return PlainTextResponse("ok")

    import auth as auth_mod

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])])
    app.add_middleware(McpAuthMiddleware)
    client = TestClient(app)

    monkeypatch.setattr(auth_mod, "ADMIN_API_KEY", "test-admin-key-0123456789")
    monkeypatch.setattr(auth_mod, "AUTH_DISABLED", False)
    response = client.post("/", json={"x": 1}, headers={"X-API-Key": "test-admin-key-0123456789"})
    assert response.status_code == 200
