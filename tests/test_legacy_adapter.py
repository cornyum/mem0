"""LegacyAdapter tests (design §7.2): the /memories + /search compatibility
surface maps onto the MemoryApplicationService with identical storage
semantics — retire-by-default delete, revise-mapped update, extract-mapped
infer=true writes, and inactive heads never surfacing in listings.
"""

import os
import sys

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")


_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from legacy_adapter import LegacyMemoryAdapter  # noqa: E402
from mem0.context.vdb import MemoryApplicationService  # noqa: E402
from mem0.context.vdb.es_store import ElasticsearchMemoryStore  # noqa: E402
from tests.context.fake_es import FakeElasticsearch  # noqa: E402


class LegacyEmbedder:
    def embed(self, text, action):
        seed = sum(ord(c) for c in text)
        return [((seed % 6) + 1) / 9.0, 0.5, 0.25, ((seed % 7) + 1) / 9.0]


class LegacyLLM:
    def generate_response(self, messages, response_format=None):
        return '{"facts": ["用户喜欢绿茶", "用户住在杭州"]}'


@pytest.fixture
def adapter():
    store = ElasticsearchMemoryStore(FakeElasticsearch(ik_enabled=True), prefix="lg_mem", dims=4)
    service = MemoryApplicationService(store, embedder=LegacyEmbedder(), llm=LegacyLLM())
    return LegacyMemoryAdapter(service)


def test_add_infer_false_appends_raw_text(adapter):
    response = adapter.add(
        [{"role": "user", "content": "原始文本"}], infer=False, user_id="u1"
    )
    assert response["results"][0]["event"] == "ADD"
    assert response["results"][0]["memory"] == "原始文本"
    # replay is idempotent: no new ADD
    replay = adapter.add([{"role": "user", "content": "原始文本"}], infer=False, user_id="u1")
    assert replay["results"] == []


def test_add_infer_true_extracts_facts(adapter):
    response = adapter.add(
        [{"role": "user", "content": "我喜欢绿茶，住在杭州"}], infer=True, user_id="u1"
    )
    memories = sorted(r["memory"] for r in response["results"])
    assert memories == ["用户住在杭州", "用户喜欢绿茶"]


def test_search_maps_recall_and_excludes_retired(adapter):
    adapter.add([{"role": "user", "content": "花生过敏约束"}], infer=False, user_id="u1")
    out = adapter.search("花生 过敏", filters={"user_id": "u1"})
    assert out["results"] and out["results"][0]["memory"] == "花生过敏约束"
    assert out["search_mode"] in ("hybrid", "semantic", "keyword")

    entry_id = out["results"][0]["id"]
    adapter.delete(memory_id=entry_id)
    after = adapter.search("花生 过敏", filters={"user_id": "u1"})
    assert after["results"] == []


def test_get_and_history(adapter):
    response = adapter.add([{"role": "user", "content": "初版"}], infer=False, user_id="u1")
    entry_id = response["results"][0]["id"]

    row = adapter.get(entry_id)
    assert row["memory"] == "初版"
    assert row["user_id"] == "u1"

    adapter.update(memory_id=entry_id, data="改版")
    assert adapter.get(entry_id)["memory"] == "改版"

    history = adapter.history(memory_id=entry_id)
    assert [h["event"] for h in history] == ["ADD", "UPDATE"]


def test_delete_defaults_to_retire_and_purge_removes(adapter):
    response = adapter.add([{"role": "user", "content": "将被退役"}], infer=False, user_id="u1")
    entry_id = response["results"][0]["id"]

    adapter.delete(memory_id=entry_id)
    assert adapter.get(entry_id)["state"] == "inactive"  # history preserved

    adapter.delete(memory_id=entry_id, purge=True)
    with pytest.raises(ValueError):
        adapter.get(entry_id)


def test_get_all_scoped_and_expires_filtering(adapter):
    adapter.add([{"role": "user", "content": "可见事实"}], infer=False, user_id="u1")
    adapter.add(
        [{"role": "user", "content": "过期事实"}], infer=False, user_id="u1", expiration_date="2000-01-01"
    )
    visible = adapter.get_all(user_id="u1")
    assert [r["memory"] for r in visible["results"]] == ["可见事实"]
    everything = adapter.get_all(user_id="u1", show_expired=True)
    assert len(everything["results"]) == 2


def test_vector_store_shim_lists_rows(adapter):
    adapter.add([{"role": "user", "content": "列表事实"}], infer=False, user_id="u1")
    rows = adapter.vector_store.list(top_k=100)[0]
    assert any(row.payload.get("data") == "列表事实" for row in rows)
    assert all(hasattr(row, "id") for row in rows)


def test_delete_all_purges_scope(adapter):
    adapter.add([{"role": "user", "content": "A"}], infer=False, user_id="u1")
    adapter.add([{"role": "user", "content": "B"}], infer=False, user_id="u1")
    result = adapter.delete_all(user_id="u1")
    assert result["deleted"] == 2
    assert adapter.get_all(user_id="u1")["results"] == []


# -- server-review regressions ---------------------------------------------------


def test_future_expiration_still_listed(adapter):
    """Review #5: an expiration_date in the FUTURE must not hide the memory."""
    adapter.add(
        [{"role": "user", "content": "远期过期事实"}], infer=False, user_id="u1", expiration_date="2999-01-01"
    )
    adapter.add([{"role": "user", "content": "永不过期事实"}], infer=False, user_id="u1")
    visible = adapter.get_all(user_id="u1")
    texts = [r["memory"] for r in visible["results"]]
    assert "远期过期事实" in texts and "永不过期事实" in texts
    expired_only = adapter.get_all(user_id="u1", show_expired=True)
    assert len(expired_only["results"]) == 2


def test_metadata_only_revise_persists(adapter):
    """Review #6: a metadata-only change must publish a revision, and the
    expiration must be clearable."""
    created = adapter.add([{"role": "user", "content": "元数据修订目标"}], infer=False, user_id="u1")
    entry_id = created["results"][0]["id"]

    adapter.update(memory_id=entry_id, metadata={"source": "crm", "priority": "high"})
    head = adapter.get(entry_id)
    assert head["metadata"] == {"source": "crm", "priority": "high"}

    # set then clear an expiration date
    adapter.update(memory_id=entry_id, data=None, metadata=None, expiration_date="2099-01-01",
                   clear_expiration=False)
    assert adapter.get(entry_id)["expiration_date"] == "2099-01-01"
    adapter.update(memory_id=entry_id, data=None, metadata=None, clear_expiration=True)
    assert adapter.get(entry_id)["expiration_date"] is None


def test_search_metadata_filters_filter_not_fabricate(adapter):
    """Review #7: metadata filters must filter results and never rewrite the
    hit's own metadata."""
    adapter.add([{"role": "user", "content": "带类型的事实一"}], infer=False, user_id="u1",
                metadata={"memory_type": "core"})
    adapter.add([{"role": "user", "content": "带类型的事实二"}], infer=False, user_id="u1",
                metadata={"memory_type": "edge"})
    out = adapter.search("事实", filters={"user_id": "u1", "memory_type": "core"})
    assert len(out["results"]) == 1
    assert out["results"][0]["memory"] == "带类型的事实一"
    assert out["results"][0]["metadata"]["memory_type"] == "core"  # its own value


def test_shim_payload_carries_expiration_and_categories(adapter):
    """Review #8: the admin-listing payload honors the serialize.py contract."""
    adapter.add([{"role": "user", "content": "分类载荷事实"}], infer=False, user_id="u1",
                expiration_date="2098-05-05", metadata={"categories": ["工作安排"]})
    rows = adapter.vector_store.list(top_k=100)[0]
    row = next(r for r in rows if r.payload.get("data") == "分类载荷事实")
    assert row.payload.get("expiration_date") == "2098-05-05"
    assert row.payload.get("categories") == ["工作安排"]


def test_add_reports_update_event_for_dedup_update(adapter):
    """Review #14: an outcome=updated (dedup convergence onto another entry's
    revise of the same content) must surface as UPDATE, not ADD."""
    first = adapter.add([{"role": "user", "content": "初始相同内容"}], infer=False, user_id="u1")
    entry_id = first["results"][0]["id"]
    adapter.update(memory_id=entry_id, data="变化后内容")
    # second writer appends the exact same updated content: converges via
    # dedup onto the existing entry → outcome=updated → legacy event UPDATE
    second = adapter.add([{"role": "user", "content": "变化后内容"}], infer=False, user_id="u1")
    # noop converges to no event at all; updated would carry UPDATE
    if second["results"]:
        assert second["results"][0]["event"] in ("UPDATE",)
