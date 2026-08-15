"""PowerMemory end-to-end on null providers (P0 acceptance path ①).

No LLM, no embedding, local qdrant projection store: explicit lifecycle
must be fully functional, with vector projections marked pending_embed.
"""

import pytest

from mem0.context.errors import (
    CapabilityNotSupportedError,
    ContextValidationError,
    EntryNotFoundError,
    EvidenceExpiredError,
)
from mem0.context.models import MemoryCitation
from mem0.context.power_memory import PowerMemory


@pytest.fixture
def power_memory(store, tmp_path):
    config = {
        "llm": {"provider": "null"},
        "embedder": {"provider": "null"},
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": "ctx_e2e",
                "path": str(tmp_path / "qdrant"),
                "embedding_model_dims": 8,
            },
        },
        "history_db_path": str(tmp_path / "history.db"),
    }
    return PowerMemory.from_config(config, ctx_store=store)


def test_remember_without_embedding_marks_pending(power_memory):
    result = power_memory.remember("用户喜欢深色模式", user_id="u1", kind="preference")
    assert result.outcome == "created"
    assert result.pending_embed is True
    assert result.entry.kind == "preference"


def test_remember_is_idempotent(power_memory):
    first = power_memory.remember("事实", user_id="u1")
    second = power_memory.remember("事实", user_id="u1")
    assert second.outcome == "noop"
    assert second.revision == first.revision


def test_retire_reactivate_roundtrip(power_memory):
    created = power_memory.remember("生命周期", user_id="u1")
    entry_id = created.entry.entry_id

    retired = power_memory.retire(entry_id, user_id="u1", reason="过时")
    assert retired.outcome == "updated"
    assert power_memory.reactivate(entry_id, user_id="u1").outcome == "updated"
    assert power_memory.reactivate(entry_id, user_id="u1").outcome == "noop"


def test_changes_and_expand(power_memory):
    created = power_memory.remember("可展开", user_id="u1")
    power_memory.remember("第二条", user_id="u1")

    changes = power_memory.changes(user_id="u1")
    assert len(changes) == 2
    assert changes[0].created_in_revision == 1

    body = power_memory.expand(
        MemoryCitation(
            artifact_id=created.artifact_id,
            entry_id=created.entry.entry_id,
            entry_version_id=created.entry.entry_version_id,
        ),
        user_id="u1",
    )
    assert body.text == "可展开"


def test_expand_detects_tampering(power_memory, store):
    created = power_memory.remember("不可篡改", user_id="u1")
    citation = MemoryCitation(
        artifact_id=created.artifact_id,
        entry_id=created.entry.entry_id,
        entry_version_id=created.entry.entry_version_id,
    )

    from sqlalchemy import update

    versions = store.metadata.tables[store.names["entry_versions"]]
    with store.engine.begin() as conn:
        conn.execute(
            update(versions)
            .where(versions.c.entry_version_id == created.entry.entry_version_id)
            .values(text="被篡改的内容")
        )

    with pytest.raises(EvidenceExpiredError):
        power_memory.expand(citation, user_id="u1")


def test_extract_mode_raises_capability_error(power_memory):
    with pytest.raises(CapabilityNotSupportedError):
        power_memory.remember(mode="extract", messages=[{"role": "user", "content": "x"}], user_id="u1")


def test_auto_without_text_raises_capability_error(power_memory):
    with pytest.raises(CapabilityNotSupportedError):
        power_memory.remember(user_id="u1")


def test_invalid_mode_rejected_not_silently_appended(power_memory):
    with pytest.raises(ContextValidationError):
        power_memory.remember("x", mode="banana", user_id="u1")


def test_text_validation(power_memory):
    with pytest.raises(ContextValidationError):
        power_memory.remember("   ", user_id="u1")
    with pytest.raises(ContextValidationError):
        power_memory.remember("字" * 8193, user_id="u1")


def test_categories_flowing_through(power_memory):
    result = power_memory.remember("带分类", user_id="u1", categories=["偏好", " ", "界面"])
    assert result.entry.categories == ["偏好", "界面"]


class _StubEmbedder:
    """Deterministic embedder swapped in post-construction (the documented
    instance-attribute replacement pattern) so projection behaviour is
    testable without a model provider."""

    def embed(self, text, memory_action=None):
        return [0.1] * 8

    def embed_batch(self, texts, memory_action=None):
        return [[0.1] * 8 for _ in texts]


def _vector_count(memory) -> int:
    listed = memory.vector_store.list(top_k=100)
    # Qdrant's list returns a pagination tuple (rows, next_offset); other
    # adapters return OutputData — normalize both.
    rows = listed[0] if isinstance(listed, tuple) else getattr(listed, "results", listed)
    return len(rows)


def test_projection_writes_vector_and_retire_flips_state(power_memory):
    from mem0.context.scope import ScopeIdentity

    power_memory.embedding_model = _StubEmbedder()

    created = power_memory.remember("真实嵌入事实", user_id="u1")
    assert created.pending_embed is False
    head = power_memory.ctx_store.get_head(ScopeIdentity(user_id="u1"), created.entry.entry_id)
    assert head["vector_id"] is not None
    assert _vector_count(power_memory) == 1

    vector = power_memory.vector_store.get(head["vector_id"])
    assert vector.payload["state"] == "active"
    assert vector.payload["entry_version_id"] == created.entry.entry_version_id

    retired = power_memory.retire(created.entry.entry_id, user_id="u1")
    assert retired.pending_embed is False
    # A state flip must never insert a new projection row.
    assert _vector_count(power_memory) == 1
    assert power_memory.vector_store.get(head["vector_id"]).payload["state"] == "inactive"

    power_memory.reactivate(created.entry.entry_id, user_id="u1")
    assert _vector_count(power_memory) == 1
    assert power_memory.vector_store.get(head["vector_id"]).payload["state"] == "active"


def test_embed_transient_failure_keeps_request_successful(power_memory):
    class _FlakyEmbedder:
        def __init__(self):
            self.calls = 0

        def embed(self, text, memory_action=None):
            self.calls += 1
            raise ConnectionError("embedder rate-limited")

        def embed_batch(self, texts, memory_action=None):
            raise ConnectionError("embedder rate-limited")

    power_memory.embedding_model = _FlakyEmbedder()
    # Projection is best-effort after the authoritative commit (design D3):
    # a transient embedder failure must not fail the remember request.
    result = power_memory.remember("瞬时故障事实", user_id="u1")
    assert result.outcome == "created"
    assert result.pending_embed is True

    from mem0.context.scope import ScopeIdentity

    head = power_memory.ctx_store.get_head(ScopeIdentity(user_id="u1"), result.entry.entry_id)
    assert head["pending_embed"] is True
    assert head["vector_id"] is None


def test_reactivate_owner_noop_keeps_target_vector_inactive(power_memory):
    from mem0.context.scope import ScopeIdentity

    power_memory.embedding_model = _StubEmbedder()
    scope = ScopeIdentity(user_id="u1")

    original = power_memory.remember("重叠事实", user_id="u1")
    original_vector = power_memory.ctx_store.get_head(scope, original.entry.entry_id)["vector_id"]

    power_memory.retire(original.entry.entry_id, user_id="u1")
    replacement = power_memory.remember("重叠事实", user_id="u1")

    # Reactivating the retired entry converges to the current owner as a
    # no-op: the ORIGINAL entry stays inactive — including its vector
    # payload (review Major-1 regression test).
    outcome = power_memory.reactivate(original.entry.entry_id, user_id="u1")
    assert outcome.outcome == "noop"
    assert outcome.entry.entry_id == replacement.entry.entry_id
    assert power_memory.vector_store.get(original_vector).payload["state"] == "inactive"
    assert power_memory.ctx_store.get_head(scope, original.entry.entry_id)["state"] == "inactive"


def test_expand_rejects_foreign_artifact_id(power_memory):
    created = power_memory.remember("有主事实", user_id="u1")
    with pytest.raises(EntryNotFoundError):
        power_memory.expand(
            MemoryCitation(
                artifact_id="not-the-artifact",
                entry_id=created.entry.entry_id,
                entry_version_id=created.entry.entry_version_id,
            ),
            user_id="u1",
        )


def test_retire_sync_merges_full_vector_payload(power_memory):
    """VectorStoreBase.update REPLACES the payload (pgvector/ES alike), so a
    state flip must merge the current payload back — flipping only `state`
    would wipe data/identity/citation fields on every backend."""
    from mem0.context.scope import ScopeIdentity

    power_memory.embedding_model = _StubEmbedder()
    created = power_memory.remember("不可清除字段", user_id="u1", categories=["关键"])
    vector_id = power_memory.ctx_store.get_head(
        ScopeIdentity(user_id="u1"), created.entry.entry_id
    )["vector_id"]

    power_memory.retire(created.entry.entry_id, user_id="u1")

    payload = power_memory.vector_store.get(vector_id).payload
    assert payload["state"] == "inactive"
    assert payload["data"] == "不可清除字段"
    assert payload["entry_version_id"] == created.entry.entry_version_id
    assert payload["categories"] == ["关键"]
    assert payload["user_id"] == "u1"
