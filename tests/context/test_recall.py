"""recall: channel transparency + authoritative freshness semantics.

Covers the three design §2.2/§6.1 guarantees:
1. projected entries surface with their channel attribution;
2. authoritative-but-pending entries merge as stale (read-your-writes);
3. retired / superseded projections are dropped even when the vector
   payload lags the authoritative head.
"""

import pytest

from mem0.context.power_memory import PowerMemory
from mem0.context.scope import ScopeIdentity


class _StubEmbedder:
    """Deterministic DISTINCT vectors so semantic ranking is meaningful in
    tests: the query embeds through the same rule as the documents."""

    def _vec(self, text):
        if "深色" in text:
            return [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        if "浅色" in text:
            # Acute angle with the 深色 query vector so the projected entry
            # clears the score threshold without being an exact match.
            return [1.0, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        return [0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]

    def embed(self, text, memory_action=None):
        return self._vec(text)

    def embed_batch(self, texts, memory_action=None):
        return [self._vec(t) for t in texts]


@pytest.fixture
def power_memory(store, tmp_path):
    memory = PowerMemory.from_config(
        {
            "llm": {"provider": "null"},
            "embedder": {"provider": "null"},
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": "recall_test",
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


def test_recall_reports_channels_for_projected_entries(power_memory):
    a = power_memory.remember("用户偏好深色模式", user_id="u1")
    b = power_memory.remember("用户喜欢马拉松训练", user_id="u1")
    assert not a.pending_embed and not b.pending_embed

    out = power_memory.recall("深色模式", user_id="u1", limit=5)
    assert out["search_mode"] == "hybrid"
    top = out["results"][0]
    assert "深色模式" in top["memory"]
    assert top["matched_by"]
    assert all("stale" not in item or item["stale"] is False for item in out["results"])


def test_recall_merges_pending_entry_as_stale(power_memory):
    projected = power_memory.remember("用户偏好浅色界面", user_id="u1")

    # Simulate a projection that never landed: write while the embedder is
    # down (authoritative commit + pending_embed, no vector row).
    from mem0.context.errors import CapabilityNotSupportedError

    class _Down:
        def embed(self, text, memory_action=None):
            raise CapabilityNotSupportedError("embedding")

        def embed_batch(self, texts, memory_action=None):
            raise CapabilityNotSupportedError("embedding")

    power_memory.embedding_model = _Down()
    pending = power_memory.remember("用户偏好深色模式", user_id="u1")
    assert pending.pending_embed is True
    power_memory.embedding_model = _StubEmbedder()

    out = power_memory.recall("深色模式", user_id="u1", limit=5)
    by_flag = {(item.get("metadata") or {}).get("entry_id"): item for item in out["results"]}

    stale_item = by_flag[pending.entry.entry_id]
    assert stale_item["stale"] is True
    assert stale_item["matched_by"] == ["fts_sidecar"]
    assert "深色模式" in stale_item["memory"]

    projected_item = by_flag[projected.entry.entry_id]
    assert not projected_item.get("stale")


def test_recall_drops_retired_even_when_vector_payload_lags(power_memory):
    created = power_memory.remember("过期的事实陈述", user_id="u1")

    # Flip the authoritative head WITHOUT syncing the vector payload —
    # the payload still says active; recall must trust the authority.
    power_memory.ctx_store.set_entry_state(
        ScopeIdentity(user_id="u1"), created.entry.entry_id, active=False
    )

    out = power_memory.recall("过期的事实", user_id="u1", limit=5)
    ids = {(item.get("metadata") or {}).get("entry_id") for item in out["results"]}
    assert created.entry.entry_id not in ids


def test_recall_drops_superseded_projection_and_merges_new_version(power_memory):
    created = power_memory.remember("初版内容是苹果", user_id="u1")

    # Revise authoritatively without re-projecting: the vector still serves
    # v1 while the head is v2 (pending).
    power_memory.ctx_store.revise_entry(
        ScopeIdentity(user_id="u1"),
        created.entry.entry_id,
        kind="fact",
        text="修订后的内容是香蕉",
    )

    out = power_memory.recall("内容", user_id="u1", limit=5)
    ids_and_versions = {
        ((item.get("metadata") or {}).get("entry_id"), (item.get("metadata") or {}).get("entry_version_id"))
        for item in out["results"]
    }
    assert (created.entry.entry_id, created.entry.entry_version_id) not in ids_and_versions

    stale_texts = [item["memory"] for item in out["results"] if item.get("stale")]
    assert any("香蕉" in t for t in stale_texts)


def test_recall_legacy_entries_pass_through(power_memory, monkeypatch):
    monkeypatch.setattr(
        power_memory,
        "search",
        lambda *a, **k: {
            "search_mode": "hybrid",
            "results": [
                {"id": "legacy-1", "memory": "老条目", "score": 0.5, "matched_by": ["semantic"], "metadata": {}}
            ],
        },
    )
    out = power_memory.recall("老条目", user_id="u1")
    assert len(out["results"]) == 1
    assert out["results"][0]["memory"] == "老条目"


def test_recall_mode_passthrough(power_memory, monkeypatch):
    captured = {}

    def fake_search(query, **kwargs):
        captured.update(kwargs)
        return {"search_mode": "keyword", "results": []}

    monkeypatch.setattr(power_memory, "search", fake_search)
    power_memory.recall("查询", user_id="u1", mode="keyword", limit=7, rerank=True, threshold=0.2)
    assert captured["mode"] == "keyword"
    assert captured["top_k"] == 7
    assert captured["rerank"] is True
    assert captured["threshold"] == 0.2
    assert captured["filters"] == {"user_id": "u1"}
