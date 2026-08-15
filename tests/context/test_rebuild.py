"""rebuild_projections (design §3.2, P2 acceptance ④): the authoritative
store alone can reconstruct the entire vector projection — RPO=0."""


import pytest

from mem0.context.power_memory import PowerMemory
from mem0.context.scope import ScopeIdentity


class _StubEmbedder:
    def embed(self, text, memory_action=None):
        return [0.1] * 8

    def embed_batch(self, texts, memory_action=None):
        return [[0.1] * 8 for _ in texts]


@pytest.fixture
def power_memory(store, tmp_path):
    memory = PowerMemory.from_config(
        {
            "llm": {"provider": "null"},
            "embedder": {"provider": "null"},
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": "rebuild_test",
                    "path": str(tmp_path / "qdrant"),
                    "embedding_model_dims": 8,
                },
            },
            "history_db_path": str(tmp_path / "history.db"),
        },
        ctx_store=store,
    )
    memory.embedding_model = _StubEmbedder()
    return memory


def _vector_ids(memory):
    listed = memory.vector_store.list(top_k=1000)
    rows = listed[0] if isinstance(listed, tuple) else (
        listed[0] if isinstance(listed, list) and listed and isinstance(listed[0], list) else listed
    )
    return {str(r.id) for r in rows}


def test_rebuild_restores_projection_after_total_loss(power_memory):
    a = power_memory.remember("重建事实甲", user_id="u1")
    b = power_memory.remember("重建事实乙", user_id="u2")
    retired = power_memory.remember("退役事实", user_id="u1")
    power_memory.retire(retired.entry.entry_id, user_id="u1")

    before = _vector_ids(power_memory)
    assert len(before) == 3

    # Total projection loss: wipe the vector store (the exact P2 acceptance
    # scenario — index deleted, authority intact). delete() is singular per
    # VectorStoreBase.
    for vector_id in before:
        power_memory.vector_store.delete(vector_id)
    assert _vector_ids(power_memory) == set()

    summary = power_memory.rebuild_projections()

    assert summary["rebuilt"] == 2 and summary["failed"] == 0  # retired stays out
    after = _vector_ids(power_memory)
    assert len(after) == 2

    head_a = power_memory.ctx_store.get_head(ScopeIdentity(user_id="u1"), a.entry.entry_id)
    head_b = power_memory.ctx_store.get_head(ScopeIdentity(user_id="u2"), b.entry.entry_id)
    assert head_a["vector_id"] in after and head_b["vector_id"] in after
    assert power_memory.ctx_store.iter_pending_embed() == []

    # Recalled content survives the wipe — RPO=0.
    recall = power_memory.recall("重建事实", user_id="u1", limit=5)
    assert any("重建事实甲" in item["memory"] for item in recall["results"])


def test_rebuild_replaces_old_rows_no_orphans(power_memory):
    created = power_memory.remember("无孤儿事实", user_id="u1")
    old_id = power_memory.ctx_store.get_head(ScopeIdentity(user_id="u1"), created.entry.entry_id)["vector_id"]

    power_memory.rebuild_projections()

    assert len(_vector_ids(power_memory)) == 1  # old row deleted, exactly one remains
    assert old_id not in _vector_ids(power_memory)


def test_iter_heads_keyset_pagination(store, power_memory):
    for i in range(5):
        power_memory.remember(f"分页事实{i}", user_id=f"u{i}")
    seen = []
    after = None
    while True:
        page = store.iter_heads(state="active", limit=2, after=after)
        if not page:
            break
        seen.extend(h["entry_id"] for h in page)
        after = (page[-1]["scope_key"], page[-1]["entry_id"])
    assert len(seen) == len(set(seen)) == 5
