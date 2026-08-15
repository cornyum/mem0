"""Behavioural contract tests for search mode + matched_by (design §6.1).

Covers the channel-selection semantics of ``Memory.search(mode=...)`` and
``_search_vector_store``: auto fallback without an embedder, keyword never
embedding, semantic skipping BM25, hard errors instead of silent downgrades,
and per-result channel attribution.
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import mem0.memory.main as memory_main  # noqa: E402
from mem0.context.errors import CapabilityNotSupportedError  # noqa: E402
from mem0.memory.main import AsyncMemory, Memory  # noqa: E402


def _mem(mem_id, score, data):
    return SimpleNamespace(id=mem_id, score=score, payload={"data": data})


class _NullEmbedder:
    def embed(self, text, memory_action=None):
        raise CapabilityNotSupportedError("embedding")

    def embed_batch(self, texts, memory_action=None):
        raise CapabilityNotSupportedError("embedding")


def _make_memory(embedder=None, semantic=None, keyword=None):
    memory = Memory.__new__(Memory)
    memory.api_version = "v1.1"
    memory.reranker = None
    memory.embedding_model = embedder if embedder is not None else _NullEmbedder()
    memory.vector_store = MagicMock()
    memory.vector_store.search = MagicMock(return_value=semantic or [])
    memory.vector_store.keyword_search = MagicMock(return_value=keyword or [])
    memory.config = SimpleNamespace(vector_store=SimpleNamespace(provider="test_store"))
    return memory


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(memory_main, "capture_event", MagicMock())
    monkeypatch.setattr(memory_main, "display_first_run_notice", lambda *a, **k: None)
    monkeypatch.setattr(
        memory_main, "display_first_run_notice_async", AsyncMock(return_value=None)
    )


class TestSyncSearchModes:
    def test_auto_hybrid_runs_both_channels_and_reports_them(self):
        memory = _make_memory(
            embedder=MagicMock(embed=MagicMock(return_value=[0.1] * 8)),
            semantic=[_mem("s1", 0.9, "语义命中"), _mem("s2", 0.8, "双通道命中")],
            keyword=[_mem("s2", 5.0, "双通道命中")],
        )
        result = memory.search("查询", filters={"user_id": "u1"}, top_k=5)

        assert result["search_mode"] == "hybrid"
        by_id = {item["id"]: item for item in result["results"]}
        assert by_id["s1"]["matched_by"] == ["semantic"]
        assert by_id["s2"]["matched_by"] == ["semantic", "keyword"]

    def test_auto_without_embedder_falls_back_to_keyword(self):
        memory = _make_memory(keyword=[_mem("k1", 7.5, "关键词命中")])
        result = memory.search("查询", filters={"user_id": "u1"}, top_k=5)

        memory.vector_store.search.assert_not_called()
        assert result["search_mode"] == "keyword"
        assert [item["id"] for item in result["results"]] == ["k1"]
        assert result["results"][0]["matched_by"] == ["keyword"]

    def test_keyword_mode_never_embeds_even_with_embedder(self):
        embedder = MagicMock()
        embedder.embed.return_value = [0.1] * 8
        memory = _make_memory(embedder=embedder, semantic=[_mem("s1", 0.9, "x")], keyword=[_mem("k1", 5.0, "关键词命中")])
        result = memory.search("查询", filters={"user_id": "u1"}, mode="keyword")

        embedder.embed.assert_not_called()
        memory.vector_store.search.assert_not_called()
        assert result["search_mode"] == "keyword"
        assert [item["id"] for item in result["results"]] == ["k1"]

    def test_semantic_mode_skips_keyword_channel(self):
        memory = _make_memory(
            embedder=MagicMock(embed=MagicMock(return_value=[0.1] * 8)),
            semantic=[_mem("s1", 0.9, "语义")],
            keyword=[_mem("k1", 5.0, "关键词")],
        )
        result = memory.search("查询", filters={"user_id": "u1"}, mode="semantic")

        memory.vector_store.keyword_search.assert_not_called()
        assert result["search_mode"] == "semantic"
        assert [item["id"] for item in result["results"]] == ["s1"]

    def test_semantic_mode_without_embedder_is_a_hard_error(self):
        memory = _make_memory(keyword=[_mem("k1", 5.0, "x")])
        with pytest.raises(CapabilityNotSupportedError):
            memory.search("查询", filters={"user_id": "u1"}, mode="semantic")

    def test_invalid_mode_rejected(self):
        memory = _make_memory()
        with pytest.raises(ValueError, match="mode must be one of"):
            memory.search("查询", filters={"user_id": "u1"}, mode="banana")

    def test_no_channel_available_returns_empty_with_keyword_mode(self):
        memory = _make_memory()
        memory.vector_store.keyword_search = MagicMock(return_value=None)
        result = memory.search("查询", filters={"user_id": "u1"})
        assert result == {"results": [], "search_mode": "keyword"}

    def test_default_call_matches_auto(self):
        embedder = MagicMock()
        embedder.embed.return_value = [0.1] * 8
        memory = _make_memory(
            embedder=embedder,
            semantic=[_mem("s1", 0.9, "语义")],
            keyword=[_mem("s1", 5.0, "语义")],
        )
        legacy = memory.search("查询", filters={"user_id": "u1"})
        explicit = memory.search("查询", filters={"user_id": "u1"}, mode="auto")
        assert legacy == explicit

    def test_entity_channel_appears_in_matched_by(self, monkeypatch):
        embedder = MagicMock()
        embedder.embed.return_value = [0.1] * 8
        memory = _make_memory(embedder=embedder, semantic=[_mem("s1", 0.9, "实体相关")])
        memory.vector_store.keyword_search = MagicMock(return_value=[])
        monkeypatch.setattr(memory_main, "extract_entities", lambda q: [("PERSON", "Caroline")])
        memory._compute_entity_boosts = MagicMock(return_value={"s1": 0.3})
        result = memory.search("查询 Caroline", filters={"user_id": "u1"})
        assert result["results"][0]["matched_by"] == ["semantic", "entity"]


class TestAsyncSearchModes:
    def _make_async_memory(self, **kwargs):
        memory = AsyncMemory.__new__(AsyncMemory)
        memory.api_version = "v1.1"
        memory.reranker = None
        memory.embedding_model = kwargs.get("embedder", _NullEmbedder())
        memory.vector_store = MagicMock()
        memory.vector_store.search = MagicMock(return_value=kwargs.get("semantic") or [])
        memory.vector_store.keyword_search = MagicMock(return_value=kwargs.get("keyword") or [])
        memory.config = SimpleNamespace(vector_store=SimpleNamespace(provider="test_store"))
        return memory

    @pytest.mark.asyncio
    async def test_auto_without_embedder_falls_back_to_keyword(self):
        memory = self._make_async_memory(keyword=[_mem("k1", 6.0, "关键词命中")])
        result = await memory.search("查询", filters={"user_id": "u1"}, top_k=5)

        memory.vector_store.search.assert_not_called()
        assert result["search_mode"] == "keyword"
        assert result["results"][0]["matched_by"] == ["keyword"]

    @pytest.mark.asyncio
    async def test_keyword_mode_never_embeds(self):
        embedder = MagicMock()
        embedder.embed.return_value = [0.1] * 8
        memory = self._make_async_memory(embedder=embedder, keyword=[_mem("k1", 6.0, "关键词命中")])
        result = await memory.search("查询", filters={"user_id": "u1"}, mode="keyword")
        embedder.embed.assert_not_called()
        assert result["search_mode"] == "keyword"

    @pytest.mark.asyncio
    async def test_semantic_mode_without_embedder_is_a_hard_error(self):
        memory = self._make_async_memory()
        with pytest.raises(CapabilityNotSupportedError):
            await memory.search("查询", filters={"user_id": "u1"}, mode="semantic")
