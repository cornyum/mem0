"""ContextStore lifecycle + distributed-concurrency guarantees (design §3.2/§5.2).

The concurrency tests run real writer races across threads and connections —
the same optimistic-CAS code path that serializes multi-process writers on
MySQL/PostgreSQL.
"""

import threading

import pytest

from mem0.context.errors import EntryNotFoundError, RevisionConflictError
from mem0.context.scope import ScopeIdentity
from mem0.context.store import ACTIVE, INACTIVE, BACKFILL

SCOPE = {"tenant_id": "t1", "user_id": "u1"}


def test_remember_creates_first_revision(store):
    outcome = store.remember_entry(ScopeIdentity(**SCOPE), kind="fact", text="用户偏好深色模式")
    assert outcome.outcome == "created"
    assert outcome.revision == 1
    assert outcome.entry.version == 1
    assert outcome.entry.created_in_revision == 1
    assert outcome.entry.provenance == "native"


def test_duplicate_content_is_noop_without_revision_bump(store):
    scope = ScopeIdentity(**SCOPE)
    first = store.remember_entry(scope, kind="fact", text="事实A")
    second = store.remember_entry(scope, kind="fact", text="事实A")
    assert second.outcome == "noop"
    assert second.revision == first.revision
    assert second.entry.entry_id == first.entry.entry_id


def test_categories_participate_in_dedup_identity(store):
    scope = ScopeIdentity(**SCOPE)
    a = store.remember_entry(scope, kind="fact", text="事实", categories=("偏好",))
    b = store.remember_entry(scope, kind="fact", text="事实", categories=("工作",))
    assert b.outcome == "created"
    assert b.entry.entry_id != a.entry.entry_id


def test_revise_creates_next_version_and_keeps_history(store):
    scope = ScopeIdentity(**SCOPE)
    first = store.remember_entry(scope, kind="fact", text="v1 内容")
    revised = store.revise_entry(scope, first.entry.entry_id, kind="fact", text="v2 内容")
    assert revised.outcome == "created"
    assert revised.entry.version == 2
    assert revised.entry.previous_version_id == first.entry.entry_version_id
    # The old version stays readable forever (immutability).
    old = store.get_entry_version(scope, first.entry.entry_id, first.entry.entry_version_id)
    assert old.text == "v1 内容"


def test_revise_to_identical_content_is_noop(store):
    scope = ScopeIdentity(**SCOPE)
    first = store.remember_entry(scope, kind="fact", text="same")
    same = store.revise_entry(scope, first.entry.entry_id, kind="fact", text="same")
    assert same.outcome == "noop"


def test_revise_to_content_owned_by_another_entry_conflicts(store):
    scope = ScopeIdentity(**SCOPE)
    a = store.remember_entry(scope, kind="fact", text="A")
    b = store.remember_entry(scope, kind="fact", text="B")
    with pytest.raises(RevisionConflictError):
        store.revise_entry(scope, b.entry.entry_id, kind="fact", text="A")
    # b unchanged
    assert store.get_head(scope, b.entry.entry_id)["entry_content_hash"] != a.entry.entry_content_hash


def test_retire_and_reactivate(store):
    scope = ScopeIdentity(**SCOPE)
    created = store.remember_entry(scope, kind="fact", text="事实")
    entry_id = created.entry.entry_id

    retired = store.set_entry_state(scope, entry_id, active=False)
    assert retired.outcome == "updated"
    assert store.get_head(scope, entry_id)["state"] == INACTIVE
    # retire never creates a content version
    assert retired.entry.version == 1

    again = store.set_entry_state(scope, entry_id, active=False)
    assert again.outcome == "noop"

    reactivated = store.set_entry_state(scope, entry_id, active=True)
    assert reactivated.outcome == "updated"
    assert store.get_head(scope, entry_id)["state"] == ACTIVE


def test_retire_releases_content_claim_for_remember(store):
    scope = ScopeIdentity(**SCOPE)
    first = store.remember_entry(scope, kind="fact", text="可退役事实")
    store.set_entry_state(scope, first.entry.entry_id, active=False)
    second = store.remember_entry(scope, kind="fact", text="可退役事实")
    assert second.outcome == "created"
    assert second.entry.entry_id != first.entry.entry_id


def test_reactivate_after_remember_same_content_noops_to_owner(store):
    scope = ScopeIdentity(**SCOPE)
    first = store.remember_entry(scope, kind="fact", text="重叠事实")
    store.set_entry_state(scope, first.entry.entry_id, active=False)
    second = store.remember_entry(scope, kind="fact", text="重叠事实")
    outcome = store.set_entry_state(scope, first.entry.entry_id, active=True)
    assert outcome.outcome == "noop"
    assert outcome.entry.entry_id == second.entry.entry_id


def test_explicit_cas_expectation_surfaces_conflict(store):
    scope = ScopeIdentity(**SCOPE)
    store.remember_entry(scope, kind="fact", text="第一条")
    with pytest.raises(RevisionConflictError) as excinfo:
        store.remember_entry(scope, kind="fact", text="第二条", expected_revision=0)
    assert excinfo.value.expected_revision == 0


def test_missing_entry_raises(store):
    scope = ScopeIdentity(**SCOPE)
    store.remember_entry(scope, kind="fact", text="锚点")
    with pytest.raises(EntryNotFoundError):
        store.set_entry_state(scope, "nonexistent", active=False)


def test_changes_pagination(store):
    scope = ScopeIdentity(**SCOPE)
    for i in range(5):
        store.remember_entry(scope, kind="fact", text=f"事实-{i}")

    page1 = store.list_changes(scope, since_revision=0, limit=2)
    assert [r.created_in_revision for r in page1] == [1, 2]
    assert page1[-1].next_cursor == 2

    page2 = store.list_changes(scope, since_revision=0, cursor=page1[-1].next_cursor)
    assert [r.created_in_revision for r in page2] == [3, 4, 5]
    assert page2[-1].next_cursor is None


def test_find_heads_subset_semantics(store):
    # Write under (tenant, user, session) and (tenant, user): a recall
    # filtered on the subset (tenant, user) must see both (design D6).
    wide = ScopeIdentity(tenant_id="t1", user_id="u1", session_id="s1")
    narrow = ScopeIdentity(tenant_id="t1", user_id="u1")
    store.remember_entry(wide, kind="fact", text="会话内事实")
    store.remember_entry(narrow, kind="fact", text="用户级事实")

    seen = store.find_heads(ScopeIdentity(tenant_id="t1", user_id="u1"))
    texts = {row["searchable_text"] for row in seen}
    assert len(seen) == 2
    assert any("会话" in t or "u4f1a" in t for t in texts)

    session_only = store.find_heads(ScopeIdentity(tenant_id="t1", user_id="u1", session_id="s1"))
    assert len(session_only) == 1


def test_scopes_are_isolated(store):
    a = ScopeIdentity(tenant_id="t1", user_id="u1")
    b = ScopeIdentity(tenant_id="t2", user_id="u1")
    store.remember_entry(a, kind="fact", text="租户一事实")
    store.remember_entry(b, kind="fact", text="租户二事实")
    # Same content, different tenant: separate artifacts, both created.
    store.remember_entry(b, kind="fact", text="租户一事实")
    assert len(store.find_heads(a)) == 1
    assert len(store.find_heads(b)) == 2


def test_backfill_provenance_roundtrip(store):
    scope = ScopeIdentity(**SCOPE)
    outcome = store.remember_entry(scope, kind="fact", text="存量", provenance=BACKFILL)
    assert outcome.entry.provenance == BACKFILL


def test_text_and_kind_limits_enforced(store):
    scope = ScopeIdentity(**SCOPE)
    with pytest.raises(ValueError):
        store.remember_entry(scope, kind="", text="x")
    with pytest.raises(ValueError):
        store.remember_entry(scope, kind="k" * 65, text="x")
    with pytest.raises(ValueError):
        store.remember_entry(scope, kind="fact", text="字" * 8193)


# -- distributed concurrency -------------------------------------------------


def _run_threads(target, count):
    """Start ``count`` workers released simultaneously by a barrier so the
    race window is forced open rather than left to scheduler luck."""
    barrier = threading.Barrier(count)

    def _gated(i):
        barrier.wait()
        target(i)

    threads = [threading.Thread(target=_gated, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_concurrent_distinct_writes_all_land(store):
    scope = ScopeIdentity(**SCOPE)
    outcomes = []
    lock = threading.Lock()

    def worker(i):
        # The caller-side retry loop mirrors a production client: unconditional
        # writes that lose a CAS race re-issue; dedup guarantees convergence.
        for _ in range(10):
            try:
                outcome = store.remember_entry(scope, kind="fact", text=f"并发事实-{i}").outcome
                with lock:
                    outcomes.append(outcome)
                return
            except RevisionConflictError:
                continue
        with lock:
            outcomes.append("exhausted")

    _run_threads(worker, 8)
    assert outcomes.count("created") == 8, outcomes
    assert len(store.find_heads(scope)) == 8


def test_concurrent_same_content_converges_to_single_created(store):
    scope = ScopeIdentity(**SCOPE)
    outcomes = []
    lock = threading.Lock()

    def worker(_i):
        try:
            result = store.remember_entry(scope, kind="fact", text="相同并发事实").outcome
            with lock:
                outcomes.append(result)
        except RevisionConflictError:
            with lock:
                outcomes.append("conflict")

    _run_threads(worker, 6)
    # Idempotent remember contract (design §5.1): with every writer pushing
    # the same content, the designed single retry converges each loser to
    # see the winner's committed head — exactly one created, all others noop.
    assert outcomes.count("created") == 1, outcomes
    assert outcomes.count("noop") == 5, outcomes
    assert len(store.find_heads(scope)) == 1


def test_concurrent_retire_and_remember_same_content(store):
    scope = ScopeIdentity(**SCOPE)
    created = store.remember_entry(scope, kind="fact", text="竞态事实")
    outcomes = []

    def retires(_i):
        try:
            outcomes.append(store.set_entry_state(scope, created.entry.entry_id, active=False).outcome)
        except RevisionConflictError:
            outcomes.append("conflict")

    def remembers(_i):
        try:
            outcomes.append(store.remember_entry(scope, kind="fact", text="竞态事实").outcome)
        except RevisionConflictError:
            outcomes.append("conflict")

    threads = [threading.Thread(target=retires, args=(i,)) for i in range(3)]
    threads += [threading.Thread(target=remembers, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    active = store.find_heads(scope)
    # Invariant: at most one active entry per content hash per artifact.
    hashes = [row["entry_content_hash"] for row in active]
    assert len(hashes) == len(set(hashes))
    assert all(row["state"] == ACTIVE for row in active)


def test_bind_vector_projection_state(store):
    scope = ScopeIdentity(**SCOPE)
    created = store.remember_entry(scope, kind="fact", text="投影")
    assert store.get_head(scope, created.entry.entry_id)["pending_embed"] is True
    store.bind_vector(scope, created.entry.entry_id, vector_id="vec-1", pending_embed=False)
    head = store.get_head(scope, created.entry.entry_id)
    assert head["vector_id"] == "vec-1"
    assert head["pending_embed"] is False
    assert store.iter_pending_embed() == []
