"""Extraction-to-payload passthrough for LLM-assigned categories.

The SDK stays business-agnostic about categories: when an extraction output
item carries a "categories" field, _add_to_vector_store cleans it (stringify,
strip, drop empties, cap at 10) and writes it into the memory payload.
Anything that is not a list is silently ignored, and a missing or empty field
leaves the payload without a categories key at all.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from mem0.memory.main import AsyncMemory, Memory

_MESSAGES = [{"role": "user", "content": "I drink green tea and jog every morning"}]
_METADATA = {"user_id": "u-cat"}
_FILTERS = {"user_id": "u-cat"}


def _sync_pipeline_memory():
    m = Memory.__new__(Memory)
    m.config = MagicMock()
    m.config.llm.config.get.return_value = None  # enable_vision off
    m.api_version = "v1.1"
    m.custom_instructions = None
    m.db = MagicMock()
    m.embedding_model = MagicMock()
    m.embedding_model.embed.return_value = [0.0, 0.0]
    m.embedding_model.embed_batch.side_effect = lambda texts, mode: [[0.1]] * len(texts)
    m.vector_store = MagicMock()
    m.vector_store.search.return_value = []
    m._entity_store = None  # entity_store is a lazy property; Phase 7 is neutralized below
    return m


def _async_pipeline_memory():
    m = AsyncMemory.__new__(AsyncMemory)
    m.config = MagicMock()
    m.config.llm.config.get.return_value = None  # enable_vision off
    m.api_version = "v1.1"
    m.custom_instructions = None
    m.db = MagicMock()
    m.embedding_model = MagicMock()
    m.embedding_model.embed.return_value = [0.0, 0.0]
    m.embedding_model.embed_batch.side_effect = lambda texts, mode: [[0.1]] * len(texts)
    m.vector_store = MagicMock()
    m.vector_store.search.return_value = []
    m._entity_store = None  # entity_store is a lazy property; Phase 7 is neutralized below
    return m


def _run_sync_extraction(llm_items):
    m = _sync_pipeline_memory()
    m.llm = MagicMock()
    m.llm.generate_response.return_value = json.dumps({"memory": llm_items})
    with patch("mem0.memory.main.lemmatize_for_bm25", lambda text: text), patch(
        "mem0.memory.main.extract_entities_batch", lambda texts: [[] for _ in texts]
    ):
        m._add_to_vector_store(_MESSAGES, _METADATA, _FILTERS, infer=True)
    _, kwargs = m.vector_store.insert.call_args
    return kwargs["payloads"]


async def _run_async_extraction(llm_items):
    m = _async_pipeline_memory()
    m.llm = MagicMock()
    m.llm.generate_response.return_value = json.dumps({"memory": llm_items})
    with patch("mem0.memory.main.lemmatize_for_bm25", lambda text: text), patch(
        "mem0.memory.main.extract_entities_batch", lambda texts: [[] for _ in texts]
    ):
        await m._add_to_vector_store(_MESSAGES, _METADATA, _FILTERS, infer=True)
    _, kwargs = m.vector_store.insert.call_args
    return kwargs["payloads"]


def test_valid_categories_land_in_payload():
    payloads = _run_sync_extraction(
        [{"text": "User drinks green tea daily", "categories": ["健康", "  Lifestyle  "]}]
    )
    assert payloads[0]["categories"] == ["健康", "Lifestyle"]


def test_sync_missing_categories_leaves_no_key():
    payloads = _run_sync_extraction([{"text": "User drinks green tea daily"}])
    assert "categories" not in payloads[0]


def test_sync_non_list_categories_silently_ignored():
    payloads = _run_sync_extraction([{"text": "User drinks green tea daily", "categories": "健康"}])
    assert "categories" not in payloads[0]


def test_sync_mixed_types_are_stringified_and_empties_dropped():
    payloads = _run_sync_extraction(
        [{"text": "User drinks green tea daily", "categories": ["健康", 42, "", "   "]}]
    )
    assert payloads[0]["categories"] == ["健康", "42"]


def test_sync_long_list_capped_at_ten():
    payloads = _run_sync_extraction(
        [{"text": "User drinks green tea daily", "categories": [f"c{i}" for i in range(15)]}]
    )
    assert payloads[0]["categories"] == [f"c{i}" for i in range(10)]


def test_sync_empty_list_leaves_no_key():
    payloads = _run_sync_extraction([{"text": "User drinks green tea daily", "categories": []}])
    assert "categories" not in payloads[0]


def test_sync_per_memory_categories():
    payloads = _run_sync_extraction(
        [
            {"text": "User drinks green tea daily", "categories": ["健康"]},
            {"text": "User jogs every morning", "categories": []},
            {"text": "User lives in Berlin"},
        ]
    )
    assert payloads[0]["categories"] == ["健康"]
    assert "categories" not in payloads[1]
    assert "categories" not in payloads[2]


@pytest.mark.asyncio
async def test_async_valid_categories_land_in_payload():
    payloads = await _run_async_extraction(
        [{"text": "User drinks green tea daily", "categories": ["健康", "Fitness"]}]
    )
    assert payloads[0]["categories"] == ["健康", "Fitness"]


@pytest.mark.asyncio
async def test_async_non_list_categories_silently_ignored():
    payloads = await _run_async_extraction([{"text": "User drinks green tea daily", "categories": {"a": 1}}])
    assert "categories" not in payloads[0]


@pytest.mark.asyncio
async def test_async_missing_and_capped_categories():
    payloads = await _run_async_extraction(
        [
            {"text": "User drinks green tea daily"},
            {"text": "User jogs every morning", "categories": [f"c{i}" for i in range(12)]},
        ]
    )
    assert "categories" not in payloads[0]
    assert payloads[1]["categories"] == [f"c{i}" for i in range(10)]
