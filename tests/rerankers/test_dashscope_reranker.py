"""Unit tests for the DashScope (Bailian) reranker: request shape, response mapping, and fallbacks."""

from unittest.mock import MagicMock, patch

import pytest

from mem0.configs.rerankers.dashscope import DashScopeRerankerConfig
from mem0.reranker.dashscope_reranker import DashScopeReranker


def _docs():
    return [{"memory": "The capital of China is Beijing."}, {"memory": "Paris is the capital of France."}]


def _fake_response(results):
    response = MagicMock()
    response.json.return_value = {"output": {"results": results}}
    response.raise_for_status.return_value = None
    return response


@pytest.fixture
def mock_http(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "")
    fake_client = MagicMock()
    with patch("mem0.reranker.dashscope_reranker.httpx.Client", return_value=fake_client):
        yield fake_client


class TestDashScopeReranker:
    def test_rerank_orders_by_relevance_and_adds_scores(self, mock_http):
        mock_http.post.return_value = _fake_response(
            [{"index": 1, "relevance_score": 0.91}, {"index": 0, "relevance_score": 0.12}]
        )

        reranker = DashScopeReranker(DashScopeRerankerConfig(api_key="test-key", model="gte-rerank-v2"))
        result = reranker.rerank("What is the capital of France?", _docs())

        assert [d["memory"] for d in result] == ["Paris is the capital of France.", "The capital of China is Beijing."]
        assert result[0]["rerank_score"] == 0.91
        assert result[1]["rerank_score"] == 0.12

    def test_rerank_sends_expected_payload(self, mock_http):
        mock_http.post.return_value = _fake_response([{"index": 0, "relevance_score": 0.5}])

        reranker = DashScopeReranker(DashScopeRerankerConfig(api_key="test-key", model="gte-rerank-v2"))
        reranker.rerank("query", _docs())

        args, kwargs = mock_http.post.call_args
        assert args[0] == "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
        assert kwargs["headers"]["Authorization"] == "Bearer test-key"
        assert kwargs["json"] == {
            "model": "gte-rerank-v2",
            "input": {"query": "query", "documents": ["The capital of China is Beijing.", "Paris is the capital of France."]},
        }

    def test_fallback_keeps_original_order_on_api_error(self, mock_http):
        mock_http.post.side_effect = RuntimeError("API error")

        reranker = DashScopeReranker(DashScopeRerankerConfig(api_key="test-key"))
        result = reranker.rerank("query", _docs())

        assert [d["memory"] for d in result] == ["The capital of China is Beijing.", "Paris is the capital of France."]
        assert all(d["rerank_score"] == 0.0 for d in result)

    def test_fallback_honors_config_top_k(self, mock_http):
        mock_http.post.side_effect = RuntimeError("API error")

        reranker = DashScopeReranker(DashScopeRerankerConfig(api_key="test-key", top_k=1))
        assert len(reranker.rerank("query", _docs())) == 1

    def test_per_call_top_k_overrides_config(self, mock_http):
        mock_http.post.return_value = _fake_response(
            [{"index": 0, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.1}]
        )

        reranker = DashScopeReranker(DashScopeRerankerConfig(api_key="test-key", top_k=2))
        assert len(reranker.rerank("query", _docs(), top_k=1)) == 1

    def test_api_key_from_env_when_not_in_config(self, mock_http, monkeypatch):
        monkeypatch.setenv("DASHSCOPE_API_KEY", "env-key")

        reranker = DashScopeReranker(DashScopeRerankerConfig())
        assert reranker.api_key == "env-key"

    def test_missing_api_key_raises(self, mock_http, monkeypatch):
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

        with pytest.raises(ValueError, match="DashScope API key is required"):
            DashScopeReranker(DashScopeRerankerConfig())

    def test_empty_documents_short_circuits(self, mock_http):
        reranker = DashScopeReranker(DashScopeRerankerConfig(api_key="test-key"))
        assert reranker.rerank("query", []) == []
        mock_http.post.assert_not_called()
