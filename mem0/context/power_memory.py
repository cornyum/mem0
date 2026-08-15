"""PowerMemory: explicit lifecycle facade over Memory (design §5.1).

Subclassing keeps the whole mem0 pipeline (embedder/vector store/history)
available for projection writes, while every authoritative decision goes to
the injected :class:`~mem0.context.store.ContextStore`. The trust boundary
(design D5) is structural here: no method on this class lets an LLM produce
ids or write the authoritative store directly.

Write order is authoritative-first (design D3): the ctx transaction commits
before the vector projection is attempted; a failed projection leaves
``pending_embed`` for reconciliation instead of failing the request.
"""

import hashlib
import logging
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from pydantic import ValidationError

from mem0.configs.base import MemoryConfig
from mem0.memory.main import Memory

from mem0.context import analyzer
from mem0.context.errors import (
    CapabilityNotSupportedError,
    ContextValidationError,
    EntryNotFoundError,
    EvidenceExpiredError,
)
from mem0.context.hashing import MAX_TEXT_BYTES, entry_content_hash
from mem0.context.models import (
    OUTCOME_NOOP,
    ChangeRecordBody,
    EntryVersionBody,
    MemoryCitation,
    RememberResult,
)
from mem0.context.scope import ScopeIdentity
from mem0.context.store import ACTIVE, ContextStore, EntryVersionView, RememberOutcome

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PowerMemory(Memory):
    """Memory with an authoritative revision store behind it.

    Construct with a normal :class:`MemoryConfig` plus a
    :class:`ContextStore` bound to the deployment's app DB engine.
    """

    def __init__(self, config: MemoryConfig = MemoryConfig(), *, ctx_store: ContextStore):
        super().__init__(config)
        self.ctx_store = ctx_store

    @classmethod
    def from_config(cls, config_dict: Dict[str, Any], *, ctx_store: ContextStore) -> "PowerMemory":
        try:
            config = MemoryConfig(**config_dict)
        except ValidationError as e:
            raise ContextValidationError(f"Invalid memory config: {e}") from e
        return cls(config, ctx_store=ctx_store)

    # -- explicit lifecycle ----------------------------------------------------

    def remember(
        self,
        text: Optional[str] = None,
        *,
        mode: str = "auto",
        kind: str = "fact",
        categories: Optional[list[str] | tuple[str, ...]] = None,
        source_refs: tuple[str, ...] | list[str] = (),
        artifact_refs: tuple[str, ...] | list[str] = (),
        expected_revision: Optional[int] = None,
        **ids: Optional[str],
    ) -> RememberResult:
        """Explicit memory write (design §5.1).

        mode="append" is zero-LLM and idempotent: same content active in the
        scope returns ``outcome="noop"`` without bumping the revision.
        mode="extract" (LLM candidate generation from evidence) arrives with
        the P2 Source Store and raises CapabilityNotSupportedError today.
        """
        if mode not in ("auto", "append", "extract"):
            raise ContextValidationError(f"mode must be auto|append|extract, got {mode!r}")
        if mode == "extract" or (mode == "auto" and text is None):
            raise CapabilityNotSupportedError("extract")
        if text is None:
            raise ContextValidationError("text is required for mode=append/auto")

        scope = ScopeIdentity(**ids)
        normalized = self._normalize_text(text)
        cats = [str(c).strip() for c in (categories or ()) if str(c).strip()]

        outcome = self.ctx_store.remember_entry(
            scope,
            kind=kind,
            text=normalized,
            categories=cats,
            source_refs=source_refs,
            artifact_refs=artifact_refs,
            expected_revision=expected_revision,
        )
        # Only content writes project to the vector store; state flips
        # (retire/reactivate) mirror onto the existing projection instead.
        return self._finalize_outcome(scope, outcome, project=True)

    def retire(
        self,
        entry_id: str,
        *,
        reason: Optional[str] = None,
        expected_revision: Optional[int] = None,
        **ids: Optional[str],
    ) -> RememberResult:
        """Logical deactivation (design ADR-5): history stays queryable, the
        content claim is released so the same fact can be remembered anew."""
        scope = ScopeIdentity(**ids)
        if reason:
            logger.info("Retiring entry %s: %s", entry_id, reason)
        outcome = self.ctx_store.set_entry_state(scope, entry_id, active=False, expected_revision=expected_revision)
        self._sync_vector_state_with_authority(scope, entry_id)
        return self._finalize_outcome(scope, outcome, project=False)

    def reactivate(
        self,
        entry_id: str,
        *,
        reason: Optional[str] = None,
        expected_revision: Optional[int] = None,
        **ids: Optional[str],
    ) -> RememberResult:
        scope = ScopeIdentity(**ids)
        outcome = self.ctx_store.set_entry_state(scope, entry_id, active=True, expected_revision=expected_revision)
        self._sync_vector_state_with_authority(scope, entry_id)
        return self._finalize_outcome(scope, outcome, project=False)

    def _sync_vector_state_with_authority(self, scope: ScopeIdentity, entry_id: str) -> None:
        """Sync the vector payload from the authoritative head state — never
        from the requested target: an owner-noop (reactivating retired
        content now owned by another entry) leaves the target inactive, and
        mirroring the request would invert the two sources of truth."""
        head = self.ctx_store.get_head(scope, entry_id)
        self._sync_vector_state(scope, entry_id, head["state"])

    def changes(
        self,
        *,
        since_revision: int = 0,
        limit: int = 200,
        cursor: Optional[int] = None,
        **ids: Optional[str],
    ) -> list[ChangeRecordBody]:
        scope = ScopeIdentity(**ids)
        return [
            ChangeRecordBody(
                entry_id=r.entry_id,
                entry_version_id=r.entry_version_id,
                version=r.version,
                kind=r.kind,
                entry_content_hash=r.entry_content_hash,
                created_in_revision=r.created_in_revision,
                provenance=r.provenance,
                created_at=r.created_at,
                next_cursor=r.next_cursor,
            )
            for r in self.ctx_store.list_changes(scope, since_revision=since_revision, limit=limit, cursor=cursor)
        ]

    def expand(self, citation: MemoryCitation, **ids: Optional[str]) -> EntryVersionBody:
        """Version-exact read with hash re-verification (design §3.2): the
        stored entry version is re-hashed; a mismatch means the evidence
        changed underneath the citation and raises EvidenceExpiredError —
        never a silent nearest-latest fallback."""
        scope = ScopeIdentity(**ids)
        if citation.artifact_id != self.ctx_store.get_artifact_id(scope):
            raise EntryNotFoundError(f"Citation artifact {citation.artifact_id} does not resolve in this scope")
        entry = self.ctx_store.get_entry_version(scope, citation.entry_id, citation.entry_version_id)
        recomputed = entry_content_hash(
            kind=entry.kind,
            text=entry.text,
            source_refs=entry.source_refs,
            artifact_refs=entry.artifact_refs,
            categories=entry.categories,
        )
        if recomputed != entry.entry_content_hash:
            raise EvidenceExpiredError(f"Citation {citation.entry_version_id} failed hash verification")
        return _entry_body(entry)

    # -- internals ----------------------------------------------------------------

    @staticmethod
    def _normalize_text(text: str) -> str:
        normalized = unicodedata.normalize("NFC", text).strip()
        if not normalized:
            raise ContextValidationError("text is empty after normalization")
        if len(normalized.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ContextValidationError(f"text exceeds {MAX_TEXT_BYTES} UTF-8 bytes")
        return normalized

    def _finalize_outcome(self, scope: ScopeIdentity, outcome: RememberOutcome, *, project: bool) -> RememberResult:
        pending_embed = False
        if project and outcome.outcome != OUTCOME_NOOP and outcome.entry is not None:
            pending_embed = self._project_to_vector_store(scope, outcome.entry)
        return RememberResult(
            outcome=outcome.outcome,
            artifact_id=outcome.artifact_id,
            revision=outcome.revision,
            pending_embed=pending_embed,
            entry=_entry_body(outcome.entry) if outcome.entry else None,
        )

    def _project_to_vector_store(self, scope: ScopeIdentity, entry: EntryVersionView) -> bool:
        """Best-effort projection after the authoritative commit (design D3).
        Any failure — embedder absent, embedder transiently down, vector
        store down — leaves the authoritative fact durable with
        ``pending_embed`` set for reconciliation; the request itself never
        fails on projection problems."""
        payload: Dict[str, Any] = {
            "data": entry.text,
            "hash": hashlib.md5(entry.text.encode()).hexdigest(),
            "text_lemmatized": analyzer.analyze(entry.text),
            "created_at": _utcnow_iso(),
            "updated_at": _utcnow_iso(),
            "kind": entry.kind,
            "state": ACTIVE,
            "entry_id": entry.entry_id,
            "entry_version_id": entry.entry_version_id,
            "entry_content_hash": entry.entry_content_hash,
            **scope.fields,
        }
        if entry.categories:
            payload["categories"] = entry.categories

        try:
            vector = self.embedding_model.embed(entry.text, "add")
            vector_id = str(uuid.uuid4())
            self.vector_store.insert(vectors=[vector], ids=[vector_id], payloads=[payload])
            self.db.add_history(
                vector_id,
                None,
                entry.text,
                "ADD",
                created_at=payload["created_at"],
                updated_at=payload["updated_at"],
            )
        except Exception:
            logger.warning(
                "Vector projection failed for entry %s; marked pending_embed for reconciliation",
                entry.entry_id,
                exc_info=True,
            )
            self.ctx_store.bind_vector(
                scope,
                entry.entry_id,
                vector_id=None,
                pending_embed=True,
                expected_entry_version_id=entry.entry_version_id,
            )
            return True

        self.ctx_store.bind_vector(
            scope,
            entry.entry_id,
            vector_id=vector_id,
            pending_embed=False,
            expected_entry_version_id=entry.entry_version_id,
        )
        return False

    def _sync_vector_state(self, scope: ScopeIdentity, entry_id: str, state: str) -> None:
        """Mirror a retire/reactivate onto the vector payload so retrieval
        filters see it immediately (double insurance, design §5.1)."""
        head = self.ctx_store.get_head(scope, entry_id)
        if not head.get("vector_id"):
            return
        try:
            self.vector_store.update(head["vector_id"], payload={"state": state})
        except Exception:
            logger.warning(
                "Vector state sync failed for entry %s; authoritative state already flipped",
                entry_id,
                exc_info=True,
            )


def _entry_body(entry: Optional[EntryVersionView]) -> Optional[EntryVersionBody]:
    if entry is None:
        return None
    return EntryVersionBody(
        entry_id=entry.entry_id,
        entry_version_id=entry.entry_version_id,
        version=entry.version,
        previous_version_id=entry.previous_version_id,
        kind=entry.kind,
        text=entry.text,
        categories=entry.categories,
        source_refs=entry.source_refs,
        artifact_refs=entry.artifact_refs,
        entry_content_hash=entry.entry_content_hash,
        created_in_revision=entry.created_in_revision,
        provenance=entry.provenance,
        created_at=entry.created_at,
    )
