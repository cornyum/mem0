"""Null LLM provider: explicit capability absence (design §3.5).

Deployments without a generation model configure ``llm.provider="null"``.
Any generation attempt raises :class:`CapabilityNotSupportedError` so the
context layer can surface a 501 — while explicit lifecycle operations
(remember-append / retire / reactivate / changes / expand) stay available,
matching PowerContext's degraded-mode contract.
"""

from typing import Dict, List, Optional, Union

from mem0.configs.llms.base import BaseLlmConfig
from mem0.llms.base import LLMBase
from mem0.context.errors import CapabilityNotSupportedError

CAPABILITY = "llm"


class NullLLM(LLMBase):
    def __init__(self, config: Optional[Union[BaseLlmConfig, Dict]] = None):
        super().__init__(config)

    def generate_response(
        self,
        messages: List[Dict[str, str]],
        response_format: Optional[Dict] = None,
        tools: Optional[List] = None,
        **kwargs,
    ) -> str:
        raise CapabilityNotSupportedError(CAPABILITY)
