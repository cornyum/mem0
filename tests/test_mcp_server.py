"""MCP projection (design §6.2): whitelist tools over the same in-process
memory instance — semantics identical to the REST tier."""

import os
import sys

import pytest

pytest.importorskip("fastmcp", reason="fastmcp not installed")

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import server_state  # noqa: E402
from mcp_server import MCP_TOOLS_ENABLED, build_mcp_server  # noqa: E402
from mem0.context.power_memory import PowerMemory  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from mem0.context.store import ContextStore  # noqa: E402


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
def memory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'mcp.db'}", connect_args={"timeout": 30})
    store = ContextStore(engine)
    store.create_tables()
    mem = PowerMemory.from_config(
        {
            "llm": {"provider": "null"},
            "embedder": {"provider": "null"},
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": "mcp_test",
                    "path": str(tmp_path / "qdrant"),
                    "embedding_model_dims": 8,
                },
            },
            "history_db_path": str(tmp_path / "history.db"),
        },
        ctx_store=store,
    )
    server_state._memory_instance = mem
    yield mem
    server_state._memory_instance = None


@pytest.fixture
def mcp(memory):
    return build_mcp_server()


@pytest.mark.asyncio
async def test_whitelist_tools_registered(mcp):
    tools = await mcp.list_tools()
    assert set(MCP_TOOLS_ENABLED) <= {t.name for t in tools}


@pytest.mark.asyncio
async def test_remember_recall_roundtrip(mcp):
    remember = _body(await mcp.call_tool("remember", {"text": "MCP 写入事实", "user_id": "u1", "kind": "fact"}))
    assert remember["outcome"] == "created"

    recall = await mcp.call_tool("recall", {"query": "MCP 写入", "user_id": "u1"})
    body = _body(recall)
    assert any("MCP 写入事实" in item["memory"] for item in body["results"])


@pytest.mark.asyncio
async def test_retire_and_changes(mcp):
    created = _body(await mcp.call_tool("remember", {"text": "生命周期事实", "user_id": "u1"}))
    entry_id = created["entry"]["entry_id"]

    retired = await mcp.call_tool("retire", {"entry_id": entry_id, "user_id": "u1"})
    body = _body(retired)
    assert body["outcome"] == "updated"

    changes = await mcp.call_tool("changes", {"user_id": "u1"})
    ch = _body(changes)
    assert len(ch["changes"]) == 1


@pytest.mark.asyncio
async def test_capture_and_prepare(mcp):
    src = await mcp.call_tool("capture_source", {"content": "原始证据", "user_id": "u1"})
    body = _body(src)
    assert body["journal_position"] == 1

    prepared = await mcp.call_tool("prepare", {"query": "证据", "user_id": "u1", "budget_bytes": 2000})
    out = _body(prepared)
    assert out["schema"] == "agentar.prepared-context.v1"
    assert out["rendered_bytes"] <= 2000
