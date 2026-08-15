from datetime import datetime, timedelta, timezone

import pytest

from mem0.utils.scoring import (
    get_bm25_params,
    normalize_bm25,
    recency_factor,
    score_and_rank,
    ENTITY_BOOST_WEIGHT,
    RECENCY_WEIGHT,
)


class TestGetBm25Params:
    def test_short_query(self):
        midpoint, steepness = get_bm25_params("hello world", lemmatized="hello world")
        assert midpoint == 5.0
        assert steepness == 0.7

    def test_medium_query(self):
        midpoint, steepness = get_bm25_params("x", lemmatized="one two three four five")
        assert midpoint == 7.0
        assert steepness == 0.6

    def test_long_query(self):
        words = " ".join(f"word{i}" for i in range(20))
        midpoint, steepness = get_bm25_params("x", lemmatized=words)
        assert midpoint == 12.0
        assert steepness == 0.5

    def test_empty_lemmatized(self):
        midpoint, steepness = get_bm25_params("test", lemmatized="")
        # Empty string -> 1 term -> short query params
        assert midpoint == 5.0


class TestNormalizeBm25:
    def test_at_midpoint(self):
        score = normalize_bm25(5.0, 5.0, 0.7)
        assert abs(score - 0.5) < 0.01  # Should be ~0.5 at midpoint

    def test_high_score(self):
        score = normalize_bm25(20.0, 5.0, 0.7)
        assert score > 0.99  # Well above midpoint

    def test_low_score(self):
        score = normalize_bm25(0.0, 5.0, 0.7)
        assert score < 0.05  # Well below midpoint

    def test_range(self):
        for raw in [0, 1, 5, 10, 20, 50]:
            score = normalize_bm25(float(raw), 5.0, 0.7)
            assert 0.0 <= score <= 1.0


class TestScoreAndRank:
    def test_semantic_only(self):
        results = [
            {"id": "a", "score": 0.9, "payload": {"data": "mem a"}},
            {"id": "b", "score": 0.5, "payload": {"data": "mem b"}},
        ]
        scored = score_and_rank(results, {}, {}, threshold=0.1, top_k=10)
        assert len(scored) == 2
        # No BM25/entity; recency is neutral (0.5) for timestamp-less payloads.
        # max_possible = 1.25; a: (0.9 + 0.125) / 1.25 = 0.82
        assert scored[0]["score"] == pytest.approx(0.82)
        assert scored[1]["score"] == pytest.approx(0.5)

    def test_semantic_plus_bm25(self):
        results = [
            {"id": "a", "score": 0.8, "payload": {"data": "mem a"}},
            {"id": "b", "score": 0.6, "payload": {"data": "mem b"}},
        ]
        bm25 = {"a": 0.3, "b": 0.9}
        scored = score_and_rank(results, bm25, {}, threshold=0.1, top_k=10)
        # max_possible = 2.25 (semantic + bm25 + recency)
        # a: (0.8 + 0.3 + 0.125) / 2.25 = 0.5444
        # b: (0.6 + 0.9 + 0.125) / 2.25 = 0.7222
        assert scored[0]["id"] == "b"  # b should rank higher due to BM25
        assert scored[0]["score"] == pytest.approx(1.625 / 2.25)
        assert scored[1]["id"] == "a"
        assert scored[1]["score"] == pytest.approx(1.225 / 2.25)

    def test_all_three_signals(self):
        results = [{"id": "a", "score": 0.8, "payload": {"data": "mem a"}}]
        bm25 = {"a": 0.6}
        entity = {"a": 0.3}
        scored = score_and_rank(results, bm25, entity, threshold=0.1, top_k=10)
        # max_possible = 2.75
        expected = (0.8 + 0.6 + 0.3 + 0.125) / 2.75
        assert scored[0]["score"] == pytest.approx(expected)

    def test_threshold_gates_on_semantic(self):
        results = [
            {"id": "a", "score": 0.05, "payload": {"data": "mem a"}},  # Below threshold
            {"id": "b", "score": 0.5, "payload": {"data": "mem b"}},
        ]
        bm25 = {"a": 0.99}  # High BM25 shouldn't save it
        scored = score_and_rank(results, bm25, {}, threshold=0.1, top_k=10)
        assert len(scored) == 1
        assert scored[0]["id"] == "b"

    def test_top_k_limit(self):
        results = [{"id": str(i), "score": 0.5, "payload": {}} for i in range(20)]
        scored = score_and_rank(results, {}, {}, threshold=0.1, top_k=5)
        assert len(scored) == 5

    def test_adaptive_divisor_semantic_only(self):
        results = [{"id": "a", "score": 0.8, "payload": {}}]
        scored = score_and_rank(results, {}, {}, threshold=0.1, top_k=10)
        # max_possible = 1.25 (semantic + recency)
        assert scored[0]["score"] == pytest.approx((0.8 + 0.125) / 1.25)

    def test_adaptive_divisor_semantic_plus_entity(self):
        results = [{"id": "a", "score": 0.8, "payload": {}}]
        entity = {"a": 0.3}
        scored = score_and_rank(results, {}, entity, threshold=0.1, top_k=10)
        # max_possible = 1.75 (semantic + entity + recency)
        expected = (0.8 + 0.3 + 0.125) / 1.75
        assert scored[0]["score"] == pytest.approx(expected)

    def test_empty_results(self):
        scored = score_and_rank([], {}, {}, threshold=0.1, top_k=10)
        assert scored == []

    def test_none_score_treated_as_zero(self):
        """Defensive: score=None must not crash on None < threshold comparison."""
        results = [{"id": "a", "score": None, "payload": {"data": "mem a"}}]
        # Should not raise TypeError; None score is treated as 0.0 and filtered out
        scored = score_and_rank(results, {}, {}, threshold=0.1, top_k=10)
        assert scored == []

    def test_score_clamped_to_1(self):
        results = [{"id": "a", "score": 1.0, "payload": {}}]
        bm25 = {"a": 1.0}
        entity = {"a": 0.5}
        scored = score_and_rank(results, bm25, entity, threshold=0.1, top_k=10)
        assert scored[0]["score"] <= 1.0

    def test_explain_includes_score_details(self):
        results = [{"id": "a", "score": 0.8, "payload": {"data": "mem a"}}]
        bm25 = {"a": 0.6}
        entity = {"a": 0.3}
        scored = score_and_rank(results, bm25, entity, threshold=0.1, top_k=10, explain=True)

        details = scored[0]["score_details"]
        assert details == {
            "semantic_score": 0.8,
            "bm25_score": 0.6,
            "entity_boost": 0.3,
            "recency_score": 0.5,  # neutral: no timestamp in payload
            "recency_term": pytest.approx(0.125),
            "raw_score": pytest.approx(1.825),
            "max_possible_score": 2.75,
            "final_score": pytest.approx(1.825 / 2.75),
            "threshold": 0.1,
        }
        # No parsable timestamp -> no age reported
        assert "recency_age_days" not in details

    def test_score_details_are_omitted_by_default(self):
        results = [{"id": "a", "score": 0.8, "payload": {"data": "mem a"}}]
        scored = score_and_rank(results, {}, {}, threshold=0.1, top_k=10)
        assert "score_details" not in scored[0]


class TestRecencyFactor:
    """Issue #4956: the hybrid score must include a time/recency signal so that
    contradictory facts written by the ADD-only v3 pipeline rank fresh-first."""

    def _iso(self, delta: timedelta) -> str:
        return (datetime.now(timezone.utc) + delta).isoformat()

    def test_newer_memory_outranks_older(self):
        old = self._iso(timedelta(days=-90))
        new = self._iso(timedelta(days=-1))
        results = [
            {"id": "old", "score": 0.8, "payload": {"data": "works at A", "updated_at": old}},
            {"id": "new", "score": 0.8, "payload": {"data": "works at B", "updated_at": new}},
        ]
        scored = score_and_rank(results, {}, {}, threshold=0.1, top_k=10)
        assert scored[0]["id"] == "new"
        assert scored[1]["id"] == "old"

    def test_issue_4956_scenario_stale_employer_loses(self):
        # Same semantic/BM25 signal for both employers; the fresh fact must win.
        results = [
            {
                "id": "company_a",
                "score": 0.82,
                "payload": {"data": "Works at Company A", "created_at": self._iso(timedelta(days=-90))},
            },
            {
                "id": "company_b",
                "score": 0.80,
                "payload": {"data": "Works at Company B", "created_at": self._iso(timedelta(seconds=-1))},
            },
        ]
        bm25 = {"company_a": 0.5, "company_b": 0.5}
        scored = score_and_rank(results, bm25, {}, threshold=0.1, top_k=10)
        assert scored[0]["id"] == "company_b"

    def test_recency_is_tiebreak_not_override(self):
        # A clearly more relevant old memory must still outrank a fresh weak match.
        results = [
            {"id": "old_relevant", "score": 0.95, "payload": {"updated_at": self._iso(timedelta(days=-365))}},
            {"id": "new_weak", "score": 0.5, "payload": {"updated_at": self._iso(timedelta(seconds=-1))}},
        ]
        scored = score_and_rank(results, {}, {}, threshold=0.1, top_k=10)
        assert scored[0]["id"] == "old_relevant"

    def test_half_life_decay_value(self):
        now = datetime.now(timezone.utc)
        payload = {"updated_at": (now - timedelta(days=30)).isoformat()}
        recency, age_days = recency_factor(payload, now=now)
        assert recency == pytest.approx(0.5, abs=0.01)
        assert age_days == pytest.approx(30.0, abs=0.01)

    def test_created_at_fallback(self):
        now = datetime.now(timezone.utc)
        payload = {"created_at": (now - timedelta(days=1)).isoformat()}
        recency, _ = recency_factor(payload, now=now)
        assert recency > 0.9

    def test_updated_at_preferred_over_created_at(self):
        now = datetime.now(timezone.utc)
        payload = {
            "created_at": (now - timedelta(days=400)).isoformat(),
            "updated_at": (now - timedelta(days=2)).isoformat(),
        }
        recency, _ = recency_factor(payload, now=now)
        assert recency > 0.9

    def test_missing_timestamp_is_neutral(self):
        recency, age_days = recency_factor({"data": "no timestamps"})
        assert recency == 0.5
        assert age_days is None
        recency_none, _ = recency_factor(None)
        assert recency_none == 0.5

    def test_future_timestamp_clamped(self):
        now = datetime.now(timezone.utc)
        payload = {"updated_at": (now + timedelta(days=10)).isoformat()}
        recency, age_days = recency_factor(payload, now=now)
        assert age_days == 0.0
        assert recency == pytest.approx(1.0)

    def test_unparsable_timestamp_is_neutral(self):
        recency, age_days = recency_factor({"updated_at": "not-a-date"})
        assert recency == 0.5
        assert age_days is None

    def test_naive_timestamp_treated_as_utc(self):
        now = datetime.now(timezone.utc)
        naive = (now - timedelta(days=30)).replace(tzinfo=None).isoformat()
        recency, _ = recency_factor({"updated_at": naive}, now=now)
        assert recency == pytest.approx(0.5, abs=0.05)

    def test_z_suffix_timestamp_parsed(self):
        now = datetime.now(timezone.utc)
        z_suffix = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        recency, _ = recency_factor({"updated_at": z_suffix}, now=now)
        assert recency == pytest.approx(0.5, abs=0.05)

    def test_blank_timestamp_is_neutral(self):
        for blank in ("", "   "):
            recency, age_days = recency_factor({"updated_at": blank})
            assert recency == 0.5
            assert age_days is None

    def test_datetime_object_timestamp_accepted(self):
        now = datetime.now(timezone.utc)
        recency, _ = recency_factor({"updated_at": now - timedelta(days=30)}, now=now)
        assert recency == pytest.approx(0.5, abs=0.01)
        naive_recency, _ = recency_factor({"updated_at": (now - timedelta(days=30)).replace(tzinfo=None)}, now=now)
        assert naive_recency == pytest.approx(0.5, abs=0.01)

    def test_naive_now_parameter_treated_as_utc(self):
        now = datetime.now(timezone.utc)
        payload = {"updated_at": (now - timedelta(days=30)).isoformat()}
        recency, _ = recency_factor(payload, now=now.replace(tzinfo=None))
        assert recency == pytest.approx(0.5, abs=0.05)

    def test_fresh_candidate_outranks_timestamp_less_tie(self):
        # Equal semantic/BM25 signal: a fresh timestamp beats no timestamp
        # (neutral midpoint anchor), which in turn beats a 90-day-old one.
        results = [
            {"id": "no_ts", "score": 0.8, "payload": {"data": "legacy"}},
            {
                "id": "fresh",
                "score": 0.8,
                "payload": {"data": "fresh", "updated_at": self._iso(timedelta(seconds=-1))},
            },
        ]
        scored = score_and_rank(results, {}, {}, threshold=0.1, top_k=10)
        assert scored[0]["id"] == "fresh"
        assert scored[1]["id"] == "no_ts"

    def test_explain_includes_recency_fields(self):
        results = [{"id": "a", "score": 0.8, "payload": {"updated_at": self._iso(timedelta(seconds=-1))}}]
        scored = score_and_rank(results, {}, {}, threshold=0.1, top_k=10, explain=True)
        details = scored[0]["score_details"]
        assert details["recency_score"] == pytest.approx(1.0, abs=0.01)
        assert details["recency_term"] == pytest.approx(RECENCY_WEIGHT, abs=0.01)
        assert "recency_age_days" in details


class TestEntityBoostWeight:
    def test_weight_value(self):
        assert ENTITY_BOOST_WEIGHT == 0.5


class TestRecencyWeight:
    def test_weight_value(self):
        assert RECENCY_WEIGHT == 0.25
