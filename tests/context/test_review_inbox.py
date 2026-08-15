"""Review Inbox (design §7, RFC 0050): untrusted candidates stay out of
retrieval until an atomic admin approve; evidence cannot be invented."""

import pytest

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
                    "collection_name": "inbox_test",
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


@pytest.fixture
def evidence(memory):
    return memory.capture_source("任务结果：发布成功但出现两条告警", user_id="u1")


def _proposal():
    return {
        "situation": "夜间发布出现告警",
        "action": "先核查告警再继续发布",
        "outcome": "避免了回滚",
        "lesson": "告警优先于进度",
    }


class TestReviewInbox:
    def test_candidate_excluded_from_recall_until_approved(self, memory, evidence):
        proposed = memory.propose_candidate(
            family="experience", proposal=_proposal(),
            source_refs=[evidence["source_id"]], user_id="u1",
        )
        assert proposed["status"] == "pending"

        recall = memory.recall("发布 告警", user_id="u1", limit=10)
        texts = [item["memory"] for item in recall["results"]]
        assert not any("告警优先于进度" in t for t in texts)

        decided = memory.decide_candidate(
            proposed["candidate_id"], approve=True, decision_reason="有效", user_id="u1"
        )
        assert decided["status"] == "approved" and decided["result_entry_version_id"]

        recall_after = memory.recall("发布 告警", user_id="u1", limit=10)
        texts_after = [item["memory"] for item in recall_after["results"]]
        assert any("告警优先于进度" in t for t in texts_after)

    def test_approve_projects_to_vector(self, memory, evidence):
        proposed = memory.propose_candidate(
            family="experience", proposal=_proposal(),
            source_refs=[evidence["source_id"]], user_id="u1",
        )
        memory.decide_candidate(proposed["candidate_id"], approve=True, user_id="u1")
        assert memory.ctx_store.iter_pending_embed() == []

    def test_invented_evidence_rejected(self, memory):
        with pytest.raises(ValueError, match="source journal"):
            memory.propose_candidate(
                family="experience", proposal=_proposal(),
                source_refs=["00000000-0000-0000-0000-000000000000"], user_id="u1",
            )

    def test_reject_is_terminal_with_reason(self, memory, evidence):
        proposed = memory.propose_candidate(
            family="skill", proposal={"description": "发布检查清单"},
            source_refs=[evidence["source_id"]], user_id="u1",
        )
        rejected = memory.decide_candidate(
            proposed["candidate_id"], approve=False, decision_reason="证据不足", user_id="u1"
        )
        assert rejected["status"] == "rejected"
        with pytest.raises(ValueError, match="terminal"):
            memory.decide_candidate(proposed["candidate_id"], approve=True, user_id="u1")
        assert memory.ctx_store.find_heads(ScopeIdentity(user_id="u1")) == []

    def test_revise_replaces_pending_immutably(self, memory, evidence):
        proposed = memory.propose_candidate(
            family="experience", proposal=_proposal(),
            source_refs=[evidence["source_id"]], user_id="u1",
        )
        revised = memory.revise_candidate(
            proposed["candidate_id"],
            proposal={**_proposal(), "lesson": "修订后的教训"},
            source_refs=[evidence["source_id"]], user_id="u1",
        )
        assert revised["version"] == 2
        candidates = memory.list_candidates(user_id="u1")["candidates"]
        assert candidates[0]["proposal"]["lesson"] == "修订后的教训"
        assert candidates[0]["version"] == 2

    def test_expected_version_conflict(self, memory, evidence):
        proposed = memory.propose_candidate(
            family="experience", proposal=_proposal(),
            source_refs=[evidence["source_id"]], user_id="u1",
        )
        memory.revise_candidate(
            proposed["candidate_id"], proposal=_proposal(),
            source_refs=[evidence["source_id"]], user_id="u1",
        )
        from mem0.context.errors import RevisionConflictError

        with pytest.raises(RevisionConflictError):
            memory.decide_candidate(
                proposed["candidate_id"], approve=True, expected_version=1, user_id="u1"
            )
