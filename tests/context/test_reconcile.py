"""Reconciliation (design §5.2): claim-flip discipline, idempotent, and
no duplicate vectors across passes or workers."""

import pytest

from mem0.context.power_memory import PowerMemory
from mem0.context.scope import ScopeIdentity


class _StubEmbedder:
    def embed(self, text, memory_action=None):
        return [0.1] * 8

    def embed_batch(self, texts, memory_action=None):
        return [[0.1] * 8 for _ in texts]


class _DownEmbedder:
    def embed(self, text, memory_action=None):
        raise ConnectionError("still down")

    def embed_batch(self, texts, memory_action=None):
        raise ConnectionError("still down")


@pytest.fixture
def power_memory(store, tmp_path):
    memory = PowerMemory.from_config(
        {
            "llm": {"provider": "null"},
            "embedder": {"provider": "null"},
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": "reconcile_test",
                    "path": str(tmp_path / "qdrant"),
                    "embedding_model_dims": 8,
                },
            },
            "history_db_path": str(tmp_path / "history.db"),
        },
        ctx_store=store,
    )
    return memory


def _make_pending(memory, text):
    from mem0.context.errors import CapabilityNotSupportedError

    class _Null:
        def embed(self, t, memory_action=None):
            raise CapabilityNotSupportedError("embedding")

        def embed_batch(self, ts, memory_action=None):
            raise CapabilityNotSupportedError("embedding")

    original = memory.embedding_model
    memory.embedding_model = _Null()
    result = memory.remember(text, user_id="u1")
    memory.embedding_model = original
    assert result.pending_embed is True
    return result


def _vector_count(memory) -> int:
    listed = memory.vector_store.list(top_k=100)
    rows = listed[0] if isinstance(listed, tuple) else getattr(listed, "results", listed)
    return len(rows)


def test_reconcile_projects_pending_and_binds(power_memory):
    pending = _make_pending(power_memory, "待对账事实一")

    power_memory.embedding_model = _StubEmbedder()
    summary = power_memory.reconcile_projections()

    assert summary["claimed"] == 1 and summary["projected"] == 1
    head = power_memory.ctx_store.get_head(ScopeIdentity(user_id="u1"), pending.entry.entry_id)
    assert head["pending_embed"] is False and head["vector_id"]
    assert power_memory.ctx_store.iter_pending_embed() == []


def test_reconcile_is_idempotent_and_creates_no_duplicates(power_memory):
    pending = _make_pending(power_memory, "待对账事实二")
    power_memory.embedding_model = _StubEmbedder()

    power_memory.reconcile_projections()
    head = power_memory.ctx_store.get_head(ScopeIdentity(user_id="u1"), pending.entry.entry_id)
    first_vector = head["vector_id"]

    # A second pass (e.g. after a claimed-but-crashed run) must update in
    # place, never insert a second vector for the same entry.
    power_memory.ctx_store.bind_vector(
        ScopeIdentity(user_id="u1"), pending.entry.entry_id,
        vector_id=first_vector, pending_embed=True,
        expected_entry_version_id=pending.entry.entry_version_id,
    )
    summary = power_memory.reconcile_projections()
    assert summary["projected"] == 1
    assert _vector_count(power_memory) == 1
    assert (
        power_memory.ctx_store.get_head(ScopeIdentity(user_id="u1"), pending.entry.entry_id)["vector_id"]
        == first_vector
    )


def test_reconcile_releases_claim_when_embedder_still_down(power_memory):
    _make_pending(power_memory, "仍不可用事实")

    power_memory.embedding_model = _DownEmbedder()
    summary = power_memory.reconcile_projections()

    assert summary["claimed"] == 1 and summary["released"] == 1
    assert len(power_memory.ctx_store.iter_pending_embed()) == 1


def test_claim_pending_loses_for_second_claimer(power_memory, store):
    pending = _make_pending(power_memory, "认领竞争事实")
    scope = ScopeIdentity(user_id="u1")

    assert store.claim_pending(scope, pending.entry.entry_id, pending.entry.entry_version_id) is True
    # The second worker's conditional UPDATE matches zero rows.
    assert store.claim_pending(scope, pending.entry.entry_id, pending.entry.entry_version_id) is False


def test_reconcile_claim_respects_version_guard(power_memory, store):
    pending = _make_pending(power_memory, "版本守卫事实")
    scope = ScopeIdentity(user_id="u1")

    # A stale claim (entry has since been revised) matches nothing.
    assert store.claim_pending(scope, pending.entry.entry_id, "stale-version-id") is False
    head = store.get_head(scope, pending.entry.entry_id)
    assert head["pending_embed"] is True
