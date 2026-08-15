from typing import Optional
from pydantic import Field

from mem0.configs.rerankers.base import BaseRerankerConfig


class DashScopeRerankerConfig(BaseRerankerConfig):
    """
    Configuration class for Alibaba Cloud DashScope (Bailian) reranker-specific parameters.
    Inherits from BaseRerankerConfig and adds DashScope-specific settings.
    """

    model: Optional[str] = Field(default="gte-rerank-v2", description="The DashScope rerank model to use")
    base_url: Optional[str] = Field(
        default="https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank",
        description="The DashScope text-rerank API endpoint",
    )
