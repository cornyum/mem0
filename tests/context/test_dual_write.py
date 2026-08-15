"""Dual-write staging + backfill (design §5.4).

The server-side hooks (_dual_write_adopt/_dual_write_sync) are thin
(mode check + loop over events) and are exercised on the live stack; the
semantic core — adopt/revise/retire/backfill — lives in PowerMemory and
is covered here."""

import uuid

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
                    "collection_name": "dual_test",
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


def _insert_legacy_row(memory, text, **ids):
    """Write a row the way the legacy add pipeline does — direct vector
    insert with identity payload, no ctx involvement."""
    vector_id = str(uuid.uuid4())
    payload = {"data": text, "hash": "legacy", **ids}
    memory.vector_store.insert(vectors=[[0.2] * 8], ids=[vector_id], payloads=[payload])
    return vector_id


def test_adopt_binds_without_reembedding_and_dedups(power_memory):
    vector_id = _insert_legacy_row(power_memory, "存量事实甲", user_id="u9")

    first = power_memory.adopt_legacy(memory_id=vector_id, text="存量事实甲", payload={"kind": "fact"}, user_id="u9")
    second = power_memory.adopt_legacy(memory_id=vector_id, text="存量事实甲", payload={"kind": "fact"}, user_id="u9")

    assert first.outcome == "created" and second.outcome == "noop"
    head = power_memory.ctx_store.get_head(ScopeIdentity(user_id="u9"), first.entry.entry_id)
    assert head["vector_id"] == vector_id and head["pending_embed"] is False
    assert power_memory.ctx_store.iter_pending_embed() == []


def test_backfill_adopts_legacy_rows_idempotently(power_memory):
    _insert_legacy_row(power_memory, "存量事实乙", user_id="u9")
    _insert_legacy_row(power_memory, "存量事实丙", user_id="u9")

    first_pass = power_memory.backfill(batch_size=500)
    assert first_pass["scanned"] == 2 and first_pass["created"] == 2
    assert first_pass["truncated"] is False

    second_pass = power_memory.backfill(batch_size=500)
    assert second_pass["scanned"] == 2 and second_pass["noop"] == 2 and second_pass["created"] == 0


def test_backfill_skips_rows_without_identity_or_data(power_memory):
    memory_id = str(uuid.uuid4())
    power_memory.vector_store.insert(
        vectors=[[0.3] * 8], ids=[memory_id], payloads=[{"data": "无身份行"}]
    )
    summary = power_memory.backfill(batch_size=500)
    assert summary["scanned"] == 0


def test_revise_bound_bumps_version_and_keeps_binding(power_memory):
    vector_id = _insert_legacy_row(power_memory, "初稿内容", user_id="u9")
    adopted = power_memory.adopt_legacy(memory_id=vector_id, text="初稿内容", user_id="u9")

    revised = power_memory.revise_bound(vector_id, "修订内容")

    assert revised.outcome == "created" and revised.entry.version == 2
    head = power_memory.ctx_store.get_head(ScopeIdentity(user_id="u9"), adopted.entry.entry_id)
    assert head["vector_id"] == vector_id
    assert head["entry_version_id"] == revised.entry.entry_version_id
    assert head["pending_embed"] is False


def test_retire_bound_tombstones_and_clears_binding(power_memory):
    vector_id = _insert_legacy_row(power_memory, "将删除内容", user_id="u9")
    adopted = power_memory.adopt_legacy(memory_id=vector_id, text="将删除内容", user_id="u9")

    result = power_memory.retire_bound(vector_id)

    assert result.outcome == "updated"
    head = power_memory.ctx_store.get_head(ScopeIdentity(user_id="u9"), adopted.entry.entry_id)
    assert head["state"] == "inactive" and head["vector_id"] is None


def test_bound_helpers_are_none_for_unadopted_rows(power_memory):
    vector_id = _insert_legacy_row(power_memory, "未采纳行", user_id="u9")
    assert power_memory.revise_bound(vector_id, "x") is None
    assert power_memory.retire_bound(vector_id) is None


def test_ctx_write_mode_reads_settings_kv(store, monkeypatch):
    import sys
    from pathlib import Path

    server_dir = str(Path(__file__).resolve().parents[2] / "server")
    if server_dir not in sys.path:
        sys.path.insert(0, server_dir)

    import context_runtime
    from sqlalchemy.orm import Session, sessionmaker

    import models

    # Point the runtime's app-DB handles at this test's engine.
    monkeypatch.setattr(context_runtime.db, "engine", store.engine, raising=False)
    monkeypatch.setattr(context_runtime.db, "SessionLocal", sessionmaker(bind=store.engine))
    models.Settings.__table__.create(store.engine, checkfirst=True)

    with Session(store.engine) as session:
        session.add(models.Settings(key="ctx_write_mode", value="dual"))
        session.commit()

    assert context_runtime.get_ctx_write_mode() == "dual"

    with Session(store.engine) as session:
        session.query(models.Settings).filter(models.Settings.key == "ctx_write_mode").delete()
        session.commit()

    assert context_runtime.get_ctx_write_mode() == "off"
