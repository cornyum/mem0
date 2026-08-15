import logging
import os
from typing import Any, Dict, List

import httpx

from mem0.reranker.base import BaseReranker

logger = logging.getLogger(__name__)


class DashScopeReranker(BaseReranker):
    """Alibaba Cloud DashScope (Bailian) reranker implementation, using the
    native text-rerank API (e.g. the gte-rerank model family)."""

    def __init__(self, config):
        """
        Initialize DashScope reranker.

        Args:
            config: DashScopeRerankerConfig object with configuration parameters
        """
        self.config = config
        self.api_key = config.api_key or os.getenv("DASHSCOPE_API_KEY")
        if not self.api_key:
            raise ValueError(
                "DashScope API key is required. Set DASHSCOPE_API_KEY environment variable or pass api_key in config."
            )

        self.model = config.model
        self.base_url = getattr(config, "base_url", None) or (
            "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
        )
        self.client = httpx.Client(timeout=60.0)

    def rerank(self, query: str, documents: List[Dict[str, Any]], top_k: int = None) -> List[Dict[str, Any]]:
        """
        Rerank documents using the DashScope rerank API.

        Args:
            query: The search query
            documents: List of documents to rerank
            top_k: Number of top documents to return

        Returns:
            List of reranked documents with rerank_score
        """
        if not documents:
            return documents

        # Extract text content for reranking
        doc_texts = []
        for doc in documents:
            if 'memory' in doc:
                doc_texts.append(doc['memory'])
            elif 'text' in doc:
                doc_texts.append(doc['text'])
            elif 'content' in doc:
                doc_texts.append(doc['content'])
            else:
                doc_texts.append(str(doc))

        try:
            # Call DashScope rerank API
            response = self.client.post(
                self.base_url,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={
                    "model": self.model,
                    "input": {"query": query, "documents": doc_texts},
                },
            )
            response.raise_for_status()
            results = response.json()["output"]["results"]

            # Create reranked results
            reranked_docs = []
            for result in results:
                original_doc = documents[result["index"]].copy()
                original_doc['rerank_score'] = result["relevance_score"]
                reranked_docs.append(original_doc)

            final_top_k = top_k or self.config.top_k
            return reranked_docs[:final_top_k] if final_top_k else reranked_docs

        except Exception as e:
            # Fallback to original order if reranking fails
            logger.warning("DashScope reranking failed, falling back to original order: %s", e)
            fallback_docs = []
            for doc in documents:
                fallback_doc = doc.copy()
                fallback_doc['rerank_score'] = 0.0
                fallback_docs.append(fallback_doc)
            final_top_k = top_k or self.config.top_k
            return fallback_docs[:final_top_k] if final_top_k else fallback_docs
