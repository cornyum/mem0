"""Observability layer: metrics vocabulary, wrapper semantics, content-free
discipline (design §8 / RFC 0046)."""

from unittest.mock import MagicMock

import pytest

from mem0.context.observability import (
    Meter,
    Observability,
    ObservableEmbedder,
    ObservableLLM,
    ObservableVectorStore,
    OUTCOME_FAILURE,
    OUTCOME_NOOP,
    wrap_memory_for_observation,
)

prometheus_client = pytest.importorskip("prometheus_client")


@pytest.fixture
def meter():
    return Meter(registry=prometheus_client.CollectorRegistry())


@pytest.fixture
def obs(meter):
    return Observability(meter=meter)


def _samples(meter, name):
    return {
        tuple(s.labels.values()): s.value
        for s in getattr(meter, name).collect()[0].samples
        if s.name.endswith("_total") and not s.name.endswith("_created")
    }


class TestMeter:
    def test_application_records_noop_outcome(self, meter):
        with meter.application("remember") as marker:
            marker.outcome = OUTCOME_NOOP
        assert _samples(meter, "application_total")[("remember", "noop")] == 1.0

    def test_application_failure_records_and_reraises(self, meter):
        with pytest.raises(RuntimeError):
            with meter.application("recall"):
                raise RuntimeError("x")
        assert _samples(meter, "application_total")[("recall", OUTCOME_FAILURE)] == 1.0

    def test_inference_success_and_failure(self, meter):
        with meter.observe("inference", "embed"):
            pass
        with pytest.raises(ValueError):
            with meter.observe("inference", "embed"):
                raise ValueError("x")
        samples = _samples(meter, "inference_total")
        assert samples[("embed", "success")] == 1.0
        assert samples[("embed", OUTCOME_FAILURE)] == 1.0


class TestWrappers:
    def test_llm_wrapper_delegates_and_records(self, obs, meter):
        inner = MagicMock()
        inner.generate_response.return_value = "ok"
        wrapped = ObservableLLM(inner, obs)
        assert wrapped.generate_response(messages=[]) == "ok"
        # attribute delegation for everything else
        inner.config.model = "m1"
        assert wrapped.config.model == "m1"
        assert _samples(meter, "inference_total")[("llm_generate", "success")] == 1.0

    def test_embedder_failure_propagates_with_counter(self, obs, meter):
        inner = MagicMock()
        inner.embed.side_effect = ConnectionError("down")
        wrapped = ObservableEmbedder(inner, obs)
        with pytest.raises(ConnectionError):
            wrapped.embed("text", "add")
        assert _samples(meter, "inference_total")[("embed", OUTCOME_FAILURE)] == 1.0

    def test_vector_store_keyword_none_counted(self, obs, meter):
        inner = MagicMock()
        inner.keyword_search.return_value = None
        wrapped = ObservableVectorStore(inner, obs)
        assert wrapped.keyword_search("q", top_k=5) is None
        assert _samples(meter, "application_total")[("keyword_none", "success")] == 1.0

    def test_wrap_memory_applies_all_three(self, obs):
        memory = MagicMock()
        wrap_memory_for_observation(memory, obs)
        assert isinstance(memory.llm, ObservableLLM)
        assert isinstance(memory.embedding_model, ObservableEmbedder)
        assert isinstance(memory.vector_store, ObservableVectorStore)


class TestPowerMemoryIntegration:
    @pytest.fixture
    def memory(self, store, tmp_path, obs):
        from mem0.context.power_memory import PowerMemory

        return PowerMemory.from_config(
            {
                "llm": {"provider": "null"},
                "embedder": {"provider": "null"},
                "vector_store": {
                    "provider": "qdrant",
                    "config": {
                        "collection_name": "obs_test",
                        "path": str(tmp_path / "qdrant"),
                        "embedding_model_dims": 8,
                    },
                },
                "history_db_path": str(tmp_path / "history.db"),
            },
            ctx_store=store,
            obs=obs,
        )

    def test_remember_noop_visible_in_metrics(self, memory, meter):
        first = memory.remember("重复事实", user_id="u1")
        second = memory.remember("重复事实", user_id="u1")
        assert first.outcome == "created" and second.outcome == "noop"
        samples = _samples(meter, "application_total")
        assert samples[("remember", "created")] == 1.0
        assert samples[("remember", "noop")] == 1.0

    def test_labels_are_content_free(self, memory, meter):
        memory.remember("绝密内容不应出现在标签", user_id="u1")
        for collector in meter.application_total.collect():
            for sample in collector.samples:
                for label_value in (sample.labels or {}).values():
                    assert "绝密" not in label_value
                    assert "u1" not in label_value


class TestNoDependenciesMode:
    def test_meter_without_prometheus_is_noop(self, monkeypatch):
        import mem0.context.observability as mod

        monkeypatch.setattr(mod, "_PROM", False)
        meter = Meter()
        assert meter.enabled is False
        with meter.application("remember") as marker:
            marker.outcome = "noop"  # must not raise
