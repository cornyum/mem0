"""P2 sources + handoffs (design §7): journal capture, lineage edges,
bounded windows, strict 1..32 citation validation, untrusted history."""

import pytest

from mem0.context.errors import ContextValidationError, EvidenceExpiredError, EntryNotFoundError
from mem0.context.power_memory import PowerMemory
from mem0.context.scope import ScopeIdentity


class _StubEmbedder:
    def embed(self, text, memory_action=None):
        return [0.1] * 8

    def embed_batch(self, texts, memory_action=None):
        return [[0.1] * 8 for _ in texts]


@pytest.fixture
def memory(store, tmp_path):
    mem = PowerMemory.from_config(
        {
            "llm": {"provider": "null"},
            "embedder": {"provider": "null"},
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": "handoff_test",
                    "path": str(tmp_path / "qdrant"),
                    "embedding_model_dims": 8,
                },
            },
            "history_db_path": str(tmp_path / "history.db"),
        },
        ctx_store=store,
    )
    mem.embedding_model = _StubEmbedder()
    return mem


def _citation(memory_result):
    entry = memory_result.entry
    return {
        "artifact_id": memory_result.artifact_id,
        "entry_id": entry.entry_id,
        "entry_version_id": entry.entry_version_id,
    }


class TestSourcesAndLineage:
    def test_capture_and_window(self, memory):
        s1 = memory.capture_source("原始事实一", user_id="u1", metadata={"turn": 1})
        s2 = memory.capture_source("原始事实二", user_id="u1")
        assert s1["journal_position"] == 1 and s2["journal_position"] == 2

        window = memory.prepare_handoff(after=0, user_id="u1", limit=10)
        assert window["window"]["count"] == 2
        assert window["window"]["through"] == 2
        assert any("原始事实一" == s["content"] for s in window["sources"])

    def test_remember_records_lineage_edges(self, memory, store):
        source = memory.capture_source("证据甲", user_id="u1")
        result = memory.remember(
            "来自证据甲的结论", user_id="u1", source_refs=[source["source_id"]]
        )
        lineage = store.lineage_for_entry(ScopeIdentity(user_id="u1"), result.entry.entry_id)
        assert lineage["source_refs"] == [source["source_id"]]


class TestHandoffLifecycle:
    def test_full_manual_flow(self, memory):
        fact = memory.remember("交接事实：项目处于设计评审阶段", user_id="u1")
        memory.capture_source("会话记录片段", user_id="u1")

        prepared = memory.prepare_handoff(after=0, user_id="u1")
        draft = {
            "objective": "继续设计评审",
            "statements": [
                {"text": "项目处于设计评审阶段", "citations": [_citation(fact)]},
            ],
            "next_action": "根据评审意见修订方案",
        }
        committed = memory.commit_handoff(prepared["handoff_id"], draft=draft, user_id="u1")
        assert committed["state"] == "committed"

        resolution = memory.continue_handoff(prepared["handoff_id"], user_id="u1")
        assert resolution["trust"] == "untrusted_history"
        assert resolution["evidence_checks"][0]["available"] is True
        assert resolution["statements"][0]["text"] == "项目处于设计评审阶段"

    def test_zero_citations_rejected(self, memory):
        fact = memory.remember("零引用事实", user_id="u1")
        prepared = memory.prepare_handoff(after=0, user_id="u1")
        with pytest.raises(ContextValidationError, match="1..32"):
            memory.commit_handoff(
                prepared["handoff_id"],
                draft={"statements": [{"text": "无引用陈述", "citations": []}]},
                user_id="u1",
            )

    def test_invented_citation_rejected(self, memory):
        fact = memory.remember("真实事实", user_id="u1")
        prepared = memory.prepare_handoff(after=0, user_id="u1")
        invented = {
            "artifact_id": fact.artifact_id,
            "entry_id": fact.entry.entry_id,
            "entry_version_id": "00000000-0000-0000-0000-000000000000",
        }
        with pytest.raises((EntryNotFoundError, EvidenceExpiredError)):
            memory.commit_handoff(
                prepared["handoff_id"],
                draft={"statements": [{"text": "伪造引用", "citations": [invented]}]},
                user_id="u1",
            )

    def test_retired_evidence_unavailable_on_continue(self, memory):
        fact = memory.remember("退役交接事实", user_id="u1")
        prepared = memory.prepare_handoff(after=0, user_id="u1")
        committed = memory.commit_handoff(
            prepared["handoff_id"],
            draft={"statements": [{"text": "退役后交接", "citations": [_citation(fact)]}]},
            user_id="u1",
        )
        memory.retire(fact.entry.entry_id, user_id="u1")

        resolution = memory.continue_handoff(prepared["handoff_id"], user_id="u1")
        check = resolution["evidence_checks"][0]
        assert check["available"] is False and check["reason"] == "retired"

    def test_continue_uncommitted_handoff_rejected(self, memory):
        memory.capture_source("占位", user_id="u1")
        prepared = memory.prepare_handoff(after=0, user_id="u1")
        with pytest.raises(ContextValidationError, match="not committed"):
            memory.continue_handoff(prepared["handoff_id"], user_id="u1")
