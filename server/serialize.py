"""Shared memory-row serialization for the REST layer.

Single source of truth for how a raw vector-store row is rendered in
`GET /memories`, `GET /memories/{id}` and `GET /export`, so the admin listing,
single-fetch and export output never drift apart.
"""

from typing import Any, Dict

# Payload keys promoted to top-level fields; everything else lands in `metadata`.
# `text_lemmatized` is internal BM25 state (near full-length duplicate text) and
# is excluded to match the SDK's get/get_all/search output shape.
RESERVED_PAYLOAD_KEYS = frozenset(
    {
        "data",
        "user_id",
        "agent_id",
        "run_id",
        "tenant_id",
        "session_id",
        "hash",
        "created_at",
        "updated_at",
        "expiration_date",
        "text_lemmatized",
    }
)


def serialize_memory(row: Any) -> Dict[str, Any]:
    payload = getattr(row, "payload", None) or {}
    return {
        "id": getattr(row, "id", None),
        "memory": payload.get("data"),
        "user_id": payload.get("user_id"),
        "agent_id": payload.get("agent_id"),
        "run_id": payload.get("run_id"),
        "tenant_id": payload.get("tenant_id"),
        "session_id": payload.get("session_id"),
        "hash": payload.get("hash"),
        "expiration_date": payload.get("expiration_date"),
        "metadata": {k: v for k, v in payload.items() if k not in RESERVED_PAYLOAD_KEYS},
        "created_at": payload.get("created_at"),
        "updated_at": payload.get("updated_at"),
    }
