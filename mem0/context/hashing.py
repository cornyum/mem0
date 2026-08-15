"""Content hashing for entry identity (design §4).

``entry_content_hash`` covers kind + text + refs + categories — the content
that makes two entries the same fact — and deliberately excludes identity and
version fields, so repeating the same ``remember`` is a natural no-op.

Canonical form: RFC 8785-flavoured JSON (sorted keys, no whitespace,
UTF-8). Reference lists are sorted before serialization so that evidence
sets with different ordering hash identically.
"""

import hashlib

from mem0.context.scope import _canonical_json

CONTENT_HASH_DOMAIN = "agentar:entry-content:v1"

MAX_TEXT_BYTES = 8192


def entry_content_hash(
    *,
    kind: str,
    text: str,
    source_refs: list[str] | tuple[str, ...] = (),
    artifact_refs: list[str] | tuple[str, ...] = (),
    categories: list[str] | tuple[str, ...] = (),
) -> str:
    canonical = _canonical_json(
        {
            "kind": kind,
            "text": text,
            "source_refs": sorted(str(ref) for ref in source_refs),
            "artifact_refs": sorted(str(ref) for ref in artifact_refs),
            "categories": sorted(str(cat) for cat in categories),
        }
    )
    return hashlib.sha256((CONTENT_HASH_DOMAIN + "\0" + canonical).encode("utf-8")).hexdigest()
