"""Scope identity model (design §3.3, D6).

mem0's retrieval semantics: whichever of the five identity ids are supplied at
write time are stored, and any *subset* of them matches at query time. A pure
hash scope key would break that (hashes cannot be enumerated into subsets), so
the authoritative tables keep the five entity columns and derive ``scope_key``
only as the dedup/CAS locus: the canonical fingerprint of the id set that was
present at write time.

Encoding spec (design §3.3): JCS key order tenant_id < user_id < agent_id <
run_id < session_id, UTF-8 values, absent fields omitted, at least one id
required.
"""

import hashlib
import json
import unicodedata

from mem0.context.errors import ContextValidationError

SCOPE_FIELDS = ("tenant_id", "user_id", "agent_id", "run_id", "session_id")

_SCOPE_DOMAIN = "agentar:scope:v1"
_MAX_ID_LENGTH = 256


def _canonical_json(mapping: dict) -> str:
    """RFC 8785-flavoured canonical JSON for flat string maps: sorted keys,
    no whitespace, non-ASCII preserved (JCS escapes only control characters,
    which id validation rejects up front)."""
    return json.dumps({k: mapping[k] for k in sorted(mapping)}, ensure_ascii=False, separators=(",", ":"))


class ScopeIdentity:
    """The set of identity ids supplied on one write, plus its derived key."""

    __slots__ = ("fields", "scope_key")

    def __init__(self, **ids: str | None):
        cleaned: dict[str, str] = {}
        for field in SCOPE_FIELDS:
            value = ids.get(field)
            if value is None:
                continue
            if not isinstance(value, str):
                raise ContextValidationError(f"{field} must be a string, got {type(value).__name__}")
            value = unicodedata.normalize("NFC", value).strip()
            if not value:
                continue
            if len(value) > _MAX_ID_LENGTH:
                raise ContextValidationError(f"{field} exceeds {_MAX_ID_LENGTH} characters")
            if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
                raise ContextValidationError(f"{field} contains control characters")
            cleaned[field] = value
        if not cleaned:
            raise ContextValidationError(
                "At least one scope id is required (tenant_id/user_id/agent_id/run_id/session_id)"
            )
        self.fields = cleaned
        self.scope_key = hashlib.sha256((_SCOPE_DOMAIN + "\0" + _canonical_json(cleaned)).encode("utf-8")).hexdigest()

    @classmethod
    def from_filters(cls, filters: dict) -> "ScopeIdentity":
        return cls(**{field: filters.get(field) for field in SCOPE_FIELDS})

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"ScopeIdentity({self.fields!r})"
