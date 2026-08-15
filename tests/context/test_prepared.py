"""PreparedContext v1 (design §6.4): byte budget, trust envelope,
citations, interleave, per-item cap."""

import json

import pytest

from mem0.context.prepared import (
    BEGIN_MARKER,
    DEFAULT_BUDGET_BYTES,
    END_MARKER,
    MAX_EXPERIENCE_ITEMS,
    MAX_ITEM_BYTES,
    MAX_MEMORY_ITEMS,
    MIN_BUDGET_BYTES,
    SCHEMA,
    TRUST_PREFIX,
    PreparedItem,
    build_prepared_context,
    interleave,
)


def _mem_items(n, prefix="记忆条目"):
    return [PreparedItem("memory", f"{prefix}{i}", {"artifact_id": "a", "entry_id": f"e{i}", "entry_version_id": f"v{i}"}) for i in range(n)]


class TestBuilder:
    def test_envelope_shape(self):
        pc = build_prepared_context(_mem_items(2), budget_bytes=DEFAULT_BUDGET_BYTES)
        assert pc.rendered.startswith(TRUST_PREFIX)
        assert BEGIN_MARKER in pc.rendered and END_MARKER in pc.rendered
        document = pc.rendered.split(BEGIN_MARKER + "\n")[1].split("\n" + END_MARKER)[0]
        body = json.loads(document)
        assert body["schema"] == SCHEMA
        assert len(body["items"]) == 2
        assert body["items"][0]["citation"]["entry_id"] == "e0"

    def test_budget_never_exceeded_even_with_many_items(self):
        items = _mem_items(8, prefix="这是一段比较长的记忆条目内容用于测试字节预算截断行为")
        for budget in (MIN_BUDGET_BYTES, 1500, 4000, DEFAULT_BUDGET_BYTES):
            pc = build_prepared_context(items, budget_bytes=budget)
            assert pc.rendered_bytes <= budget, budget
            assert pc.item_count + pc.dropped == len(items)

    def test_binary_truncation_drops_tail_and_reports(self):
        items = _mem_items(8, prefix="长内容条目" * 20)
        pc = build_prepared_context(items, budget_bytes=1200)
        assert 0 < pc.item_count < 8
        assert pc.dropped == 8 - pc.item_count
        document = pc.rendered.split(BEGIN_MARKER + "\n")[1].split("\n" + END_MARKER)[0]
        body = json.loads(document)
        assert body["dropped"] == pc.dropped
        # The kept prefix is intact — truncation only drops whole items.
        assert body["items"][0]["text"].startswith("长内容条目")

    def test_single_item_capped_at_max_bytes(self):
        huge = "字" * (MAX_ITEM_BYTES + 500)
        pc = build_prepared_context([PreparedItem("memory", huge, None)], budget_bytes=32768)
        document = pc.rendered.split(BEGIN_MARKER + "\n")[1].split("\n" + END_MARKER)[0]
        text = json.loads(document)["items"][0]["text"]
        assert len(text.encode("utf-8")) <= MAX_ITEM_BYTES
        assert text.endswith("...")

    def test_interleave_round_robin_and_caps(self):
        memories = _mem_items(MAX_MEMORY_ITEMS + 3)
        experiences = [PreparedItem("experience", f"经验{i}", None) for i in range(MAX_EXPERIENCE_ITEMS + 2)]
        merged = interleave(memories, experiences)
        assert len(merged) == MAX_MEMORY_ITEMS + MAX_EXPERIENCE_ITEMS
        assert merged[0].type == "memory" and merged[1].type == "experience"

    def test_budget_bounds_validated(self):
        with pytest.raises(ValueError):
            build_prepared_context(_mem_items(1), budget_bytes=MIN_BUDGET_BYTES - 1)
        with pytest.raises(ValueError):
            build_prepared_context(_mem_items(1), budget_bytes=32769)

    def test_empty_items_still_render_envelope(self):
        pc = build_prepared_context([], budget_bytes=MIN_BUDGET_BYTES)
        assert pc.item_count == 0 and pc.rendered_bytes <= MIN_BUDGET_BYTES
        assert TRUST_PREFIX in pc.rendered


class TestPowerMemoryPrepare:
    @pytest.fixture
    def memory(self, store, tmp_path):
        from mem0.context.power_memory import PowerMemory

        mem = PowerMemory.from_config(
            {
                "llm": {"provider": "null"},
                "embedder": {"provider": "null"},
                "vector_store": {
                    "provider": "qdrant",
                    "config": {
                        "collection_name": "prepare_test",
                        "path": str(tmp_path / "qdrant"),
                        "embedding_model_dims": 8,
                    },
                },
                "history_db_path": str(tmp_path / "history.db"),
            },
            ctx_store=store,
        )
        return mem

    def test_prepare_returns_citations_and_budget(self, memory):
        created = memory.remember("准备上下文的记忆内容", user_id="u1")

        result = memory.prepare_context("准备上下文", user_id="u1", budget_bytes=4000)

        assert result["schema"] == SCHEMA
        assert result["rendered_bytes"] <= 4000
        assert result["item_count"] >= 1
        document = result["rendered"].split(BEGIN_MARKER + "\n")[1].split("\n" + END_MARKER)[0]
        item = json.loads(document)["items"][0]
        assert item["citation"]["entry_id"] == created.entry.entry_id
        assert item["citation"]["entry_version_id"] == created.entry.entry_version_id

    def test_prepare_scope_without_binding_returns_empty(self, memory):
        result = memory.prepare_context("任意查询", user_id="never-written")
        assert result["item_count"] == 0
        assert TRUST_PREFIX in result["rendered"]
