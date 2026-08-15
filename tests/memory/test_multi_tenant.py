"""Multi-tenant scoping regression tests.

tenant_id and session_id must behave exactly like user_id/agent_id/run_id
across the memory lifecycle: accepted by add()/delete_all() top-level params,
required-or-rejected consistently, included in metadata payloads and query
filters, encoded in the session scope, and returned as promoted payload keys.
"""

from unittest.mock import MagicMock, patch

import pytest

from mem0.configs.enums import MemoryType  # noqa: F401  (keeps import parity with sibling tests)
from mem0.exceptions import ValidationError as Mem0ValidationError
from mem0.memory.main import (
    ENTITY_PARAMS,
    AsyncMemory,
    Memory,
    _build_filters_and_metadata,
    _build_session_scope,
)


# ---------------------------------------------------------------------------
# _build_filters_and_metadata
# ---------------------------------------------------------------------------

def test_tenant_and_session_flow_into_metadata_and_filters():
    metadata, filters = _build_filters_and_metadata(
        tenant_id="tenant-a", session_id="sess-1", input_metadata={"topic": "hiking"}
    )
    assert metadata["tenant_id"] == "tenant-a"
    assert metadata["session_id"] == "sess-1"
    assert metadata["topic"] == "hiking"
    assert filters == {"tenant_id": "tenant-a", "session_id": "sess-1"}


def test_tenant_alone_satisfies_identifier_requirement():
    metadata, filters = _build_filters_and_metadata(tenant_id="tenant-a")
    assert metadata == {"tenant_id": "tenant-a"}
    assert filters == {"tenant_id": "tenant-a"}


def test_no_identifier_still_raises():
    with pytest.raises(Mem0ValidationError, match="At least one of"):
        _build_filters_and_metadata()


def test_tenant_cannot_be_injected_via_metadata():
    metadata, filters = _build_filters_and_metadata(
        tenant_id="tenant-a", input_metadata={"tenant_id": "tenant-b", "session_id": "sess-evil"}
    )
    # Identity scope comes from the entity params only.
    assert metadata["tenant_id"] == "tenant-a"
    assert filters["tenant_id"] == "tenant-a"


def test_entity_params_include_tenant_and_session():
    assert ENTITY_PARAMS == frozenset(
        {"user_id", "agent_id", "run_id", "tenant_id", "session_id"}
    )


def test_session_scope_encodes_tenant_and_session():
    scope = _build_session_scope({"user_id": "u1", "tenant_id": "t1", "session_id": "s1"})
    # Keys are sorted deterministically.
    assert scope == "session_id=s1&tenant_id=t1&user_id=u1"


# ---------------------------------------------------------------------------
# Memory.add / delete_all plumbing
# ---------------------------------------------------------------------------

def _bare_memory():
    m = Memory.__new__(Memory)
    m.config = MagicMock()
    m.config.llm.config.get.return_value = None  # enable_vision off
    m.api_version = "v1.1"
    return m


def test_sync_add_forwards_tenant_and_session_to_vector_store_pipeline():
    m = _bare_memory()
    captured = {}

    def fake_add_to_vector_store(messages, metadata, filters, infer, prompt=None):
        captured["metadata"] = metadata
        captured["filters"] = filters
        return [{"id": "mem-1", "memory": "likes hiking", "event": "ADD"}]

    with patch.object(m, "_add_to_vector_store", side_effect=fake_add_to_vector_store), patch(
        "mem0.memory.main.parse_vision_messages", lambda msgs, *a, **k: msgs
    ), patch("mem0.memory.main.display_first_run_notice", lambda *a, **k: None):
        result = m.add("I like hiking", tenant_id="t1", session_id="s1")

    assert result["results"][0]["event"] == "ADD"
    assert captured["metadata"]["tenant_id"] == "t1"
    assert captured["metadata"]["session_id"] == "s1"
    assert captured["filters"] == {"tenant_id": "t1", "session_id": "s1"}


@pytest.mark.asyncio
async def test_async_add_forwards_tenant_and_session():
    m = AsyncMemory.__new__(AsyncMemory)
    m.config = MagicMock()
    m.config.llm.config.get.return_value = None  # enable_vision off
    m.api_version = "v1.1"
    captured = {}

    async def fake_add_to_vector_store(messages, metadata, filters, infer, prompt=None):
        captured["metadata"] = metadata
        captured["filters"] = filters
        return [{"id": "mem-1", "memory": "likes hiking", "event": "ADD"}]

    with patch.object(m, "_add_to_vector_store", side_effect=fake_add_to_vector_store), patch(
        "mem0.memory.main.parse_vision_messages", lambda msgs, *a, **k: msgs
    ), patch("mem0.memory.main.display_first_run_notice_async", _noop_async):
        await m.add("I like hiking", tenant_id="t2", session_id="s2")

    assert captured["metadata"]["tenant_id"] == "t2"
    assert captured["metadata"]["session_id"] == "s2"
    assert captured["filters"] == {"tenant_id": "t2", "session_id": "s2"}


async def _noop_async(*args, **kwargs):
    return None


def _memory_with_mocked_vector_store():
    m = _bare_memory()
    m.vector_store = MagicMock()
    m.vector_store.list.return_value = [[]]  # empty first batch -> delete loop exits
    with patch("mem0.memory.main.display_first_run_notice", lambda *a, **k: None):
        pass
    return m


def test_delete_all_accepts_tenant_and_session():
    m = _memory_with_mocked_vector_store()
    with patch("mem0.memory.main.display_first_run_notice", lambda *a, **k: None):
        result = m.delete_all(tenant_id="t1", session_id="s1")
    assert result["message"].startswith("Memories deleted")
    _, kwargs = m.vector_store.list.call_args
    assert kwargs["filters"] == {"tenant_id": "t1", "session_id": "s1"}


@pytest.mark.asyncio
async def test_async_delete_all_accepts_tenant():
    m = AsyncMemory.__new__(AsyncMemory)
    m.vector_store = MagicMock()
    m.vector_store.list.return_value = [[]]
    m._entity_store = None
    with patch("mem0.memory.main.display_first_run_notice_async", _noop_async):
        result = await m.delete_all(tenant_id="t9")
    assert result["message"].startswith("Memories deleted")
    _, kwargs = m.vector_store.list.call_args
    assert kwargs["filters"] == {"tenant_id": "t9"}


# ---------------------------------------------------------------------------
# get_all / search validation with tenant/session filters
# ---------------------------------------------------------------------------

def test_get_all_accepts_tenant_only_filters():
    m = _memory_with_mocked_vector_store()
    m.vector_store.list.return_value = []
    with patch("mem0.memory.main.display_first_run_notice", lambda *a, **k: None):
        result = m.get_all(filters={"tenant_id": "t1"})
    assert result == {"results": []}
    _, kwargs = m.vector_store.list.call_args
    assert kwargs["filters"] == {"tenant_id": "t1"}


def test_search_rejects_top_level_tenant_param():
    m = _bare_memory()
    with pytest.raises(ValueError, match="Top-level entity parameters"):
        m.search("hiking", tenant_id="t1")


def test_search_accepts_tenant_filters_after_validation():
    m = _bare_memory()
    m.vector_store = MagicMock()
    m.vector_store.keyword_search.return_value = []
    m._entity_store = None
    m.reranker = None
    with patch.object(m, "_search_vector_store", return_value=[]) as fake_search, patch(
        "mem0.memory.main.display_first_run_notice", lambda *a, **k: None
    ):
        result = m.search("hiking", filters={"tenant_id": "t1", "session_id": "s1"})
    assert result == {"results": []}
    _, args, kwargs = fake_search.mock_calls[0]
    assert args[1] == {"tenant_id": "t1", "session_id": "s1"}



