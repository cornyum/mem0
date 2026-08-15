"""Unit tests for the pure-VDB authority kernel (design v3).

Runs entirely on the in-memory FakeElasticsearch: publish protocol, dedup
lifecycle, CAS races, crash-point recovery, recall fusion, changes/expand
and the HYBRID sidecar replay — the acceptance invariants of §12 that do not
need a live cluster.
"""

import pytest
import sqlalchemy as sa

from mem0.context.errors import (
    CapabilityNotSupportedError,
    ContextValidationError,
    EntryNotFoundError,
    EvidenceExpiredError,
    RevisionConflictError,
)
from mem0.context.models import MemoryCitation
from mem0.context.scope import ScopeIdentity
from mem0.context.vdb.errors import (
    DedupConflictError,
    OperationInProgressError,
    PrimaryUnavailableError,
    PublishedRepairPendingError,
)
from mem0.context.vdb import (
    ElasticsearchMemoryStore,
    HybridSidecar,
    MemoryApplicationService,
    RecoveryReconciler,
    parse_storage_mode,
    validate_vector_store_provider,
)
from tests.context.fake_es import FakeElasticsearch


class FakeEmbedder:
    def __init__(self, fail=False):
        self.fail = fail

    def embed(self, text, action):
        if self.fail:
            raise RuntimeError("embedder down")
        seed = sum(ord(c) for c in text)
        return [((seed % 7) + 1) / 10.0, 0.5, 0.5, ((seed % 11) + 1) / 10.0]


@pytest.fixture
def fake_es():
    return FakeElasticsearch(ik_enabled=True)


@pytest.fixture
def store(fake_es):
    return ElasticsearchMemoryStore(fake_es, prefix="t_mem", dims=4, ensure_indices=True)


@pytest.fixture
def service(store):
    return MemoryApplicationService(store, embedder=FakeEmbedder(), llm=None, storage_mode="ONLY_VDB")


# -- storage config (design §3) -----------------------------------------------------


def test_storage_mode_parsing():
    assert parse_storage_mode(None) == "ONLY_VDB"
    assert parse_storage_mode("") == "ONLY_VDB"
    assert parse_storage_mode(" only_vdb ") == "ONLY_VDB"
    assert parse_storage_mode("Hybrid_Storage") == "HYBRID_STORAGE"
    assert parse_storage_mode("HYBRIRD_STORAGE") == "HYBRID_STORAGE"
    with pytest.raises(ValueError):
        parse_storage_mode("postgres")


def test_vector_store_provider_restriction():
    assert validate_vector_store_provider("Elasticsearch", "ONLY_VDB") == "elasticsearch"
    with pytest.raises(ValueError):
        validate_vector_store_provider("pgvector", "ONLY_VDB")


# -- write protocol: remember --------------------------------------------------------


def test_remember_created_publishes_full_family(service, store):
    result = service.remember("用户偏好深色模式", user_id="u1", kind="preference")
    assert result["results"][0]["outcome"] == "created"
    entry = result["results"][0]["entry"]
    scope_key = ScopeIdentity(user_id="u1").scope_key

    scope = store.get_scope(scope_key)
    assert scope.published_revision == 1
    assert scope.last_event_type == "created"

    head = store.get_head(scope_key, entry["entry_id"])
    assert head["state"] == "active"
    assert head["embedding_status"] == "ready"
    assert head["vector"] is not None

    version = store.get_version(scope_key, entry["entry_id"], 1)
    assert version["text"] == "用户偏好深色模式"

    events = store.list_events(scope_key)
    assert len(events) == 1 and events[0]["event_type"] == "created"


def test_remember_same_content_is_noop(service, store):
    first = service.remember("事实A", user_id="u1")["results"][0]
    second = service.remember("事实A", user_id="u1")["results"][0]
    assert first["outcome"] == "created"
    assert second["outcome"] == "noop"
    assert second["revision"] == first["revision"]
    assert store.get_scope(ScopeIdentity(user_id="u1").scope_key).published_revision == 1


def test_remember_different_scope_no_dedup(service):
    a = service.remember("事实A", user_id="u1")["results"][0]
    b = service.remember("事实A", user_id="u2")["results"][0]
    assert a["outcome"] == "created" and b["outcome"] == "created"
    assert a["entry"]["entry_id"] != b["entry"]["entry_id"]


def test_remember_expected_revision_conflict(service):
    first = service.remember("事实A", user_id="u1")["results"][0]
    with pytest.raises(RevisionConflictError):
        service.remember("事实B", user_id="u1", expected_revision=first["revision"] + 5)


def test_embedder_failure_marks_pending_and_keyword_channel_still_finds(service, store):
    service.embedder = FakeEmbedder(fail=True)
    service.writer.embedder = service.embedder
    result = service.remember("花生严重过敏", user_id="u9")["results"][0]
    assert result["outcome"] == "created"
    assert result["pending_embed"] is True

    # embedding_status=pending head: excluded from semantic, visible to keyword
    out = service.recall("花生 过敏", user_id="u9", mode="semantic")
    assert out["results"] == []
    out = service.recall("花生 过敏", user_id="u9", mode="keyword")
    assert len(out["results"]) == 1
    assert out["results"][0]["matched_by"] == ["keyword"]


def test_published_repair_pending_when_derived_write_fails(store):
    service = MemoryApplicationService(store, embedder=FakeEmbedder(), llm=None)
    es = store.client

    def fail_head(document):
        return PrimaryUnavailableError("head index unavailable")

    es.fail_index = fail_head
    with pytest.raises(PublishedRepairPendingError):
        service.remember("事实A", user_id="u1")
    es.fail_index = None

    # CAS published but head/event/dedup missing → reconciler repairs
    scope_key = ScopeIdentity(user_id="u1").scope_key
    scope = store.get_scope(scope_key)
    assert scope.published_revision == 1  # authoritative publish happened
    counts = service.reconcile()
    assert counts["head_rebuilt"] >= 1 or counts["prepared_completed"] >= 1

    heads = store.list_heads({"user_id": "u1"})
    assert len(heads) == 1
    assert heads[0]["text"] == "事实A"
    # idempotent replay of the same command converges: first retry completes
    # the pending publish (returns the original created outcome with the same
    # entry), the next retry is a clean noop (design §5.2.6)
    replay = service.remember("事实A", user_id="u1")["results"][0]
    assert replay["outcome"] == "created"
    assert replay["entry"]["entry_id"] == heads[0]["entry_id"]
    assert service.remember("事实A", user_id="u1")["results"][0]["outcome"] == "noop"


def test_concurrent_same_content_second_writer_noop(store):
    """Simulate a live prepared claim by another writer: the waiter settles
    into noop once the winner activates the claim (acceptance §12.2)."""
    service = MemoryApplicationService(store, embedder=FakeEmbedder(), llm=None)
    scope = ScopeIdentity(user_id="u1")
    service.remember("事实A", user_id="u1")

    scope_key = scope.scope_key
    content_hash = store.list_heads({"user_id": "u1"})[0]["content_hash"]
    from mem0.context.vdb.write import compute_dedup_key

    dedup_key = compute_dedup_key(scope_key, "fact", content_hash)
    # force the active claim back to prepared as if a new writer crashed mid-way
    claim = store.get_dedup(scope_key, dedup_key)
    store.set_dedup_status(scope_key, dedup_key, "prepared", entry_id=claim["entry_id"], entry_version_id=claim["entry_version_id"])

    other = MemoryApplicationService(store, embedder=FakeEmbedder(), llm=None)
    other.writer.claim_wait_seconds = 0.05
    # background completer flips the claim back to active while we wait
    import threading
    import time

    def settle():
        time.sleep(0.02)
        store.set_dedup_status(
            scope_key, dedup_key, "active", entry_id=claim["entry_id"], entry_version_id=claim["entry_version_id"]
        )

    thread = threading.Thread(target=settle)
    thread.start()
    result = other.remember("事实A", user_id="u1")["results"][0]
    thread.join()
    assert result["outcome"] == "noop"


def test_prepared_claim_unrecoverable_returns_409(store):
    service = MemoryApplicationService(store, embedder=FakeEmbedder(), llm=None)
    service.remember("事实A", user_id="u1")
    scope_key = ScopeIdentity(user_id="u1").scope_key
    content_hash = store.list_heads({"user_id": "u1"})[0]["content_hash"]
    from mem0.context.vdb.write import compute_dedup_key

    dedup_key = compute_dedup_key(scope_key, "fact", content_hash)
    store.set_dedup_status(scope_key, dedup_key, "prepared", entry_id="ghost", entry_version_id="ghost")
    service.writer.claim_wait_seconds = 0
    with pytest.raises(OperationInProgressError):
        service.remember("事实A", user_id="u1")


def test_released_claim_is_reclaimed_after_retire(service, store):
    first = service.remember("事实A", user_id="u1")["results"][0]
    entry_id = first["entry"]["entry_id"]
    service.retire(entry_id, user_id="u1", reason="stale")
    # retire releases the claim → same content can be remembered anew (§5.3)
    second = service.remember("事实A", user_id="u1")["results"][0]
    assert second["outcome"] == "created"
    assert second["entry"]["entry_id"] != entry_id


def test_scope_lifecycle_revision_counter(service, store):
    scope_key = ScopeIdentity(user_id="u1").scope_key
    a = service.remember("事实A", user_id="u1")["results"][0]
    b = service.remember("事实B", user_id="u1")["results"][0]
    assert (a["revision"], b["revision"]) == (1, 2)
    assert store.get_scope(scope_key).published_revision == 2


# -- revise ---------------------------------------------------------------------------


def test_revise_creates_new_version_and_rotates_dedup(service, store):
    first = service.remember("初始内容", user_id="u1")["results"][0]
    entry_id = first["entry"]["entry_id"]
    result = service.revise(entry_id, text="修订内容", user_id="u1")
    assert result.outcome == "updated"
    assert result.entry.version == 2

    scope_key = ScopeIdentity(user_id="u1").scope_key
    head = store.get_head(scope_key, entry_id)
    assert head["text"] == "修订内容"
    assert head["version"] == 2
    assert store.get_version(scope_key, entry_id, 1)["text"] == "初始内容"
    assert store.get_version(scope_key, entry_id, 2)["text"] == "修订内容"


def test_revise_same_content_noop(service):
    first = service.remember("内容", user_id="u1")["results"][0]
    result = service.revise(first["entry"]["entry_id"], text="内容", user_id="u1")
    assert result.outcome == "noop"


def test_revise_missing_entry_404(service):
    with pytest.raises(EntryNotFoundError):
        service.revise("nope", text="x", user_id="u1")


def test_revise_conflicting_content_owned_elsewhere_409(service):
    a = service.remember("内容A", user_id="u1")["results"][0]
    service.remember("内容B", user_id="u1")
    with pytest.raises(DedupConflictError):
        service.revise(a["entry"]["entry_id"], text="内容B", user_id="u1")


# -- retire / reactivate / purge ---------------------------------------------------------


def test_retire_reactivate_roundtrip(service, store):
    first = service.remember("事实", user_id="u1")["results"][0]
    entry_id = first["entry"]["entry_id"]
    scope_key = ScopeIdentity(user_id="u1").scope_key

    retired = service.retire(entry_id, user_id="u1", reason="gone")
    assert retired.outcome == "updated"
    assert store.get_head(scope_key, entry_id)["state"] == "inactive"
    # retired heads never surface in recall (acceptance §12.8)
    assert service.recall("事实", user_id="u1")["results"] == []
    # double retire → noop
    assert service.retire(entry_id, user_id="u1").outcome == "noop"

    activated = service.reactivate(entry_id, user_id="u1")
    assert activated.outcome == "updated"
    assert store.get_head(scope_key, entry_id)["state"] == "active"
    assert service.recall("事实", user_id="u1")["results"]


def test_reactivate_conflict_when_dedup_owned(service):
    first = service.remember("事实", user_id="u1")["results"][0]
    entry_id = first["entry"]["entry_id"]
    service.retire(entry_id, user_id="u1")
    # same content now owned by a new active entry
    service.remember("事实", user_id="u1")
    with pytest.raises(DedupConflictError):
        service.reactivate(entry_id, user_id="u1")


def test_purge_removes_physical_documents(service, store):
    first = service.remember("将被清除", user_id="u1")["results"][0]
    entry_id = first["entry"]["entry_id"]
    scope_key = ScopeIdentity(user_id="u1").scope_key
    service.purge(entry_id, user_id="u1")
    assert store.get_head(scope_key, entry_id) is None
    assert store.list_versions(scope_key, entry_id) == []
    events = store.list_events(scope_key)
    assert events[-1]["event_type"] == "purged"


# -- recall (design §6.2) ------------------------------------------------------------------


def _seed_recall_corpus(service):
    service.remember("用户对花生严重过敏", user_id="u1", kind="fact")
    service.remember("用户每天早上七点晨跑", user_id="u1", kind="habit")
    service.remember("MySQL 数据库是核心存储", user_id="u1", kind="fact")


def test_recall_auto_unions_channels_with_rrf(service):
    _seed_recall_corpus(service)
    out = service.recall("花生 过敏", user_id="u1", limit=5)
    assert out["search_mode"] == "hybrid"
    assert out["storage_source"] == "elasticsearch"
    assert out["degraded"] is False
    top = out["results"][0]
    assert "花生" in top["text"]
    assert set(top["matched_by"]) >= {"semantic", "keyword"}


def test_recall_keyword_only_match_included(service):
    """A keyword-exclusive hit must survive the union (acceptance §12.5)."""
    service.embedder = FakeEmbedder(fail=False)
    service.remember("独特关键词XYZQW", user_id="u1")
    out = service.recall("XYZQW", user_id="u1", limit=5)
    assert any("XYZQW" in r["text"] for r in out["results"])


def test_recall_threshold_gates_semantic_channel(service):
    _seed_recall_corpus(service)
    out = service.recall("花生", user_id="u1", mode="semantic", threshold=0.99)
    assert out["results"] == []
    out = service.recall("花生", user_id="u1", mode="semantic", threshold=0.0)
    assert out["results"]


def test_recall_keyword_with_threshold_is_422(service):
    with pytest.raises(ContextValidationError):
        service.recall("q", user_id="u1", mode="keyword", threshold=0.5)


def test_recall_semantic_without_embedder_is_501(store):
    service = MemoryApplicationService(store, embedder=None, llm=None)
    with pytest.raises(CapabilityNotSupportedError):
        service.recall("q", user_id="u1", mode="semantic")
    # auto degrades to keyword
    service.remember("文本", user_id="u1")
    out = service.recall("文本", user_id="u1", mode="auto")
    assert out["search_mode"] == "keyword"
    assert out["degraded_channels"] == ["semantic"]


def test_recall_es_down_only_vdb_is_503(service, store):
    _seed_recall_corpus(service)
    from elasticsearch import ConnectionError as EsConnectionError

    store.client.fail_search = lambda index: EsConnectionError("cluster down")
    with pytest.raises(PrimaryUnavailableError):
        service.recall("花生", user_id="u1")
    with pytest.raises(PrimaryUnavailableError):
        service.recall("花生", user_id="u1", mode="keyword")
    store.client.fail_search = None


def test_recall_normal_empty_results_never_fallback(service, store):
    out = service.recall("完全不存在的内容词组", user_id="ghost_user")
    assert out["results"] == []
    assert out["storage_source"] == "elasticsearch"


def test_recall_rerank_failure_keeps_rrf_order(service, store):
    _seed_recall_corpus(service)

    class BrokenReranker:
        def rerank(self, query, documents, top_k):
            raise RuntimeError("reranker down")

    service.recaller.reranker = BrokenReranker()
    out = service.recall("花生", user_id="u1", rerank=True)
    assert out["rerank_status"] == "fallback"
    assert out["results"]


def test_recall_rerank_applied(service):
    _seed_recall_corpus(service)

    class StaticReranker:
        def rerank(self, query, documents, top_k):
            return list(reversed(documents))[:top_k]

    service.recaller.reranker = StaticReranker()
    out = service.recall("花生", user_id="u1", rerank=True)
    assert out["rerank_status"] == "applied"
    assert "rerank" in out["results"][0]["matched_by"]


def test_recall_scope_isolation(service):
    service.remember("用户A的秘密", user_id="alice")
    service.remember("用户B的秘密", user_id="bob")
    out = service.recall("秘密", user_id="alice")
    assert out["results"]
    assert all(r.get("user_id") == "alice" and "用户A" in r["text"] for r in out["results"])


def test_recall_candidate_limit_formula():
    from mem0.context.vdb.recall import candidate_limit

    assert candidate_limit(1) == 50
    assert candidate_limit(10) == 50
    assert candidate_limit(30) == 120
    assert candidate_limit(100) == 200


# -- changes / expand / get (design §7) ---------------------------------------------------


def test_changes_includes_lifecycle_events_and_pagination(service, store):
    first = service.remember("事实A", user_id="u1")["results"][0]
    entry_id = first["entry"]["entry_id"]
    service.retire(entry_id, user_id="u1")
    service.reactivate(entry_id, user_id="u1")

    changes = service.changes(user_id="u1")
    assert [c.entry_id for c in changes] == [entry_id] * 3
    # full window: no cursor needed
    assert [c.next_cursor for c in changes] == [None, None, None]

    page1 = service.changes(user_id="u1", limit=2)
    assert len(page1) == 2 and page1[-1].next_cursor == 2
    page2 = service.changes(user_id="u1", limit=2, cursor=page1[-1].next_cursor)
    assert len(page2) == 1 and page2[0].created_in_revision == 3


def test_expand_verifies_hash_and_artifact(service, store):
    first = service.remember("可扩展的事实", user_id="u1")["results"][0]
    entry = first["entry"]
    citation = MemoryCitation(
        artifact_id=first["artifact_id"],
        entry_id=entry["entry_id"],
        entry_version_id=entry["entry_version_id"],
    )
    body = service.expand(citation, user_id="u1")
    assert body.text == "可扩展的事实"

    scope_key = ScopeIdentity(user_id="u1").scope_key
    tampered = store.get_version(scope_key, entry["entry_id"], 1)
    tampered["text"] = "被篡改的内容"
    store.put_version(tampered)
    with pytest.raises(EvidenceExpiredError):
        service.expand(citation, user_id="u1")

    bad = MemoryCitation(
        artifact_id="wrong", entry_id=entry["entry_id"], entry_version_id=entry["entry_version_id"]
    )
    with pytest.raises(EntryNotFoundError):
        service.expand(bad, user_id="u1")


def test_expand_locates_version_without_scope(service):
    first = service.remember("跨scope定位", user_id="u2")["results"][0]
    entry = first["entry"]
    citation = MemoryCitation(
        artifact_id=first["artifact_id"],
        entry_id=entry["entry_id"],
        entry_version_id=entry["entry_version_id"],
    )
    assert service.expand(citation).text == "跨scope定位"


def test_get_returns_current_head(service):
    first = service.remember("当前内容", user_id="u1")["results"][0]
    entry_id = first["entry"]["entry_id"]
    head = service.get(entry_id, user_id="u1")
    assert head["text"] == "当前内容"
    service.revise(entry_id, text="新内容", user_id="u1")
    head = service.get(entry_id, user_id="u1")
    assert head["text"] == "新内容" and head["version"] == 2
    with pytest.raises(EntryNotFoundError):
        service.get("missing", user_id="u1")


# -- recovery reconciler (design §5.4) --------------------------------------------------------


def test_recovery_reconciler_releases_stale_claim(service, store):
    service.remember("事实A", user_id="u1")
    scope_key = ScopeIdentity(user_id="u1").scope_key
    content_hash = store.list_heads({"user_id": "u1"})[0]["content_hash"]
    from mem0.context.vdb.write import compute_dedup_key

    dedup_key = compute_dedup_key(scope_key, "fact", content_hash)
    # a prepared claim that will never publish (revision far in the future)
    store.try_claim_dedup(
        scope_key, dedup_key + "x", entry_id="ghost", entry_version_id="ghost", scope_revision=999
    )
    reconciler = RecoveryReconciler(store, prepared_ttl_seconds=0)
    counts = reconciler.reconcile()
    assert counts["prepared_released"] >= 1
    assert store.get_dedup(scope_key, dedup_key + "x")["status"] == "released"


def test_embedding_reconciler_backfills_vectors(store):
    service = MemoryApplicationService(store, embedder=FakeEmbedder(fail=True), llm=None)
    first = service.remember("待补向量", user_id="u1")["results"][0]
    scope_key = ScopeIdentity(user_id="u1").scope_key
    head = store.get_head(scope_key, first["entry"]["entry_id"])
    assert head["embedding_status"] == "pending"

    service.embedder = FakeEmbedder()
    service.embedder_reconciler.embedder = service.embedder
    counts = service.embedder_reconciler.reconcile()
    assert counts["embedded"] == 1
    head = store.get_head(scope_key, first["entry"]["entry_id"])
    assert head["embedding_status"] == "ready"
    assert head["vector"] is not None
    # now the semantic channel can find it
    out = service.recall("待补向量", user_id="u1")
    assert out["results"]


def test_capabilities_report_real_probes(service):
    caps = service.capabilities()
    assert caps["storage"]["mode"] == "only_vdb"
    assert caps["storage"]["sql_fallback_enabled"] is False
    assert caps["memory"]["semantic_search"] is True
    assert caps["memory"]["keyword_search"] is True
    assert caps["memory"]["extraction"] is False  # no LLM configured


# -- HYBRID sidecar (design §8) ----------------------------------------------------------------


@pytest.fixture
def sqlite_engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path/'sidecar.db'}")
    yield engine
    engine.dispose()


def test_hybrid_sidecar_replays_events_and_fts(sqlite_engine, store):
    sidecar = HybridSidecar(sqlite_engine, table_prefix="t_", ensure_schema=True)
    service = MemoryApplicationService(store, embedder=FakeEmbedder(), llm=None)
    first = service.remember("用户住在杭州滨江", user_id="u1")["results"][0]
    entry_id = first["entry"]["entry_id"]
    service.revise(entry_id, text="用户住在杭州西湖区", user_id="u1")
    service.retire(entry_id, user_id="u1")

    counts = sidecar.sync_all(store)
    assert counts["events_applied"] == 3
    assert counts["scopes"] == 1

    scope_key = ScopeIdentity(user_id="u1").scope_key
    assert sidecar._get_checkpoint(scope_key) == 3

    with sqlite_engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT state, text FROM t_memory_recall_head WHERE scope_key = :k"),
            {"k": scope_key},
        ).fetchone()
    assert row[0] == "inactive"
    assert row[1] == "用户住在杭州西湖区"

    # idempotent replay
    again = sidecar.sync_all(store)
    assert again["events_applied"] == 0


def test_hybrid_sidecar_purge_removes_row(sqlite_engine, store):
    sidecar = HybridSidecar(sqlite_engine, table_prefix="t_", ensure_schema=True)
    service = MemoryApplicationService(store, embedder=FakeEmbedder(), llm=None)
    first = service.remember("将被物理删除", user_id="u1")["results"][0]
    entry_id = first["entry"]["entry_id"]
    sidecar.sync_all(store)
    service.purge(entry_id, user_id="u1")
    sidecar.sync_all(store)
    with sqlite_engine.connect() as conn:
        count = conn.execute(
            sa.text("SELECT COUNT(*) FROM t_memory_recall_head WHERE entry_id = :e"), {"e": entry_id}
        ).scalar()
    assert count == 0


def test_recall_falls_back_to_sidecar_when_es_down(sqlite_engine, store):
    sidecar = HybridSidecar(sqlite_engine, table_prefix="t_", ensure_schema=True)
    service = MemoryApplicationService(
        store, embedder=FakeEmbedder(), llm=None, storage_mode="HYBRID_STORAGE", hybrid_sidecar=sidecar
    )
    service.remember("花生过敏是关键约束", user_id="u1")
    assert sidecar.sync_all(store)["events_applied"] == 1

    from elasticsearch import ConnectionError as EsConnectionError

    store.client.fail_search = lambda index: EsConnectionError("cluster down")
    out = service.recall("花生", user_id="u1", mode="auto")
    store.client.fail_search = None
    assert out["storage_source"] == "sql_fts"
    assert out["search_mode"] == "fts_sidecar"
    assert out["degraded"] is True
    assert out["as_of_revision"] == 1
    assert out["results"] and out["results"][0]["matched_by"] == ["fts_sidecar"]
