"""Null embedding provider: explicit capability absence (design §3.5).

Deployments without an embedding model configure ``embedder.provider="null"``.
The instance constructs successfully (Memory pipelines can boot), but any
embed attempt raises :class:`CapabilityNotSupportedError` — never zeros or
stale vectors. The context layer catches that signal and takes the designed
degraded path (authoritative write + FTS projection, ``pending_embed=True``).
"""

from typing import Literal, Optional

from mem0.configs.embeddings.base import BaseEmbedderConfig
from mem0.embeddings.base import EmbeddingBase
from mem0.context.errors import CapabilityNotSupportedError

CAPABILITY = "embedding"


class NullEmbedding(EmbeddingBase):
    def __init__(self, config: Optional[BaseEmbedderConfig] = None):
        super().__init__(config)

    def embed(self, text, memory_action: Optional[Literal["add", "search", "update"]] = None):
        raise CapabilityNotSupportedError(CAPABILITY)

    def embed_batch(self, texts, memory_action=None):
        raise CapabilityNotSupportedError(CAPABILITY)
