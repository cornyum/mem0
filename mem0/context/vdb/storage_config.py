"""Storage-mode configuration (design §3).

``MEMORY_STORAGE_MODE`` selects the memory persistence topology:

- ``ONLY_VDB`` (default): Elasticsearch is the sole memory authority. No SQL
  memory tables are created, queried, or even health-checked on the memory
  path.
- ``HYBRID_STORAGE``: same ES authority plus a minimal SQL recall sidecar
  (``memory_recall_head`` / ``memory_recall_checkpoint``) used only for
  fault-fallback retrieval.

Parsing rules (design §3): missing variable → ONLY_VDB; values are
``strip().upper()``-ed; the historical misspelling ``HYBRIRD_STORAGE`` is
accepted with a single warning; anything else fails startup. The value is
read once at process start — hot switching via /configure is forbidden
(ADR-7).
"""

import logging
import os

logger = logging.getLogger(__name__)

MODE_ONLY_VDB = "ONLY_VDB"
MODE_HYBRID_STORAGE = "HYBRID_STORAGE"

_VALID_MODES = (MODE_ONLY_VDB, MODE_HYBRID_STORAGE)

# Design §3: compatible with the historical misspelling HYBRIRD_STORAGE, warn once
_LEGACY_SPELLINGS = {"HYBRIRD_STORAGE": MODE_HYBRID_STORAGE}


def parse_storage_mode(raw: str | None = None) -> str:
    """Parse and validate the storage mode. Raises ValueError on invalid input."""
    value = (raw if raw is not None else os.environ.get("MEMORY_STORAGE_MODE")) or ""
    normalized = value.strip().upper()
    if not normalized:
        return MODE_ONLY_VDB
    if normalized in _VALID_MODES:
        return normalized
    if normalized in _LEGACY_SPELLINGS:
        logger.warning(
            "MEMORY_STORAGE_MODE=%s is a legacy spelling; treating as %s. Fix the spelling — "
            "support will be removed.",
            normalized,
            _LEGACY_SPELLINGS[normalized],
        )
        return _LEGACY_SPELLINGS[normalized]
    raise ValueError(
        f"Invalid MEMORY_STORAGE_MODE={value!r}: must be one of {', '.join(_VALID_MODES)} "
        f"(or the legacy spelling HYBRIRD_STORAGE). Restart with a valid value."
    )


def validate_vector_store_provider(provider: str, storage_mode: str) -> str:
    """Design §3: the primary provider is fixed to Elasticsearch for both v3
    storage modes; any other VECTOR_STORE_PROVIDER fails startup."""
    normalized = (provider or "").strip().lower()
    if normalized != "elasticsearch":
        raise ValueError(
            f"VECTOR_STORE_PROVIDER={provider!r} is not supported with "
            f"MEMORY_STORAGE_MODE={storage_mode}: only 'elasticsearch' is accepted."
        )
    return normalized
