"""WriteCoordinator: the ES publish protocol (design §5.2/§5.3).

Every write follows the same six steps: claim dedup → read CAS baseline →
write immutable version → CAS-publish the scope doc (the single linearization
point) → rebuildable derived writes (head/event/dedup) → respond. A lost CAS
race without an explicit ``expected_revision`` retries with a fresh baseline;
a derived-write failure after a successful CAS surfaces as
``PublishedRepairPendingError`` (503) and is repaired by the
RecoveryReconciler — clients converge by retrying the same command.

Dedup claim states (§5.3): ``active`` hit → noop without advancing the
revision; ``prepared`` → idempotent recovery when the CAS already published,
a bounded wait while a concurrent writer finishes (so concurrent duplicates
naturally become noop, acceptance §12.2), takeover after the TTL, else 409
``operation_in_progress``; ``released`` → re-claim and publish fresh.
"""

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from mem0.context.errors import (
    ContextError,
    EntryNotFoundError,
    RevisionConflictError,
)
from mem0.context.hashing import entry_content_hash
from mem0.context.scope import ScopeIdentity
from mem0.context.vdb.errors import (
    DedupConflictError,
    OperationInProgressError,
    PrimaryUnavailableError,
    PublishedRepairPendingError,
    classify_elasticsearch_error,
)
from mem0.context.vdb.es_store import (
    ElasticsearchMemoryStore,
    EsCasConflict,
    ScopeDoc,
    utcnow,
)
from mem0.utils.lemmatization import lemmatize_for_bm25

logger = logging.getLogger(__name__)

DEDUP_DOMAIN = "agentar:dedup:v1"
ACTIVE = "active"
INACTIVE = "inactive"

EVENT_CREATED = "created"
EVENT_REVISED = "revised"
EVENT_RETIRED = "retired"
EVENT_REACTIVATED = "reactivated"
EVENT_PURGED = "purged"

CAS_RETRIES = 3
DERIVED_WRITE_RETRIES = 2
CLAIM_POLL_INTERVAL = 0.25


def compute_dedup_key(scope_key: str, kind: str, content_hash: str) -> str:
    canonical = "\0".join((DEDUP_DOMAIN, scope_key, kind, content_hash))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class WriteOutcome:
    outcome: str  # created | updated | noop
    event_type: Optional[str]
    scope_key: str
    artifact_id: str
    revision: int
    entry_id: Optional[str] = None
    entry_version_id: Optional[str] = None
    version: Optional[int] = None
    kind: Optional[str] = None
    text: Optional[str] = None
    content_hash: Optional[str] = None
    state_after: Optional[str] = None
    pending_embed: bool = False


class WriteCoordinator:
    def __init__(
        self,
        store: ElasticsearchMemoryStore,
        *,
        embedder=None,
        claim_wait_seconds: float = 3.0,
        prepared_ttl_seconds: float = 300.0,
    ):
        self.store = store
        self.embedder = embedder
        self.claim_wait_seconds = claim_wait_seconds
        self.prepared_ttl_seconds = prepared_ttl_seconds

    # -- remember ----------------------------------------------------------------

    def remember(
        self,
        scope: ScopeIdentity,
        *,
        kind: str,
        text: str,
        categories: List[str],
        source_refs: List[str],
        artifact_refs: List[str],
        metadata: Optional[Dict[str, Any]] = None,
        expires_at: Optional[str] = None,
        expected_revision: Optional[int] = None,
        provenance: str = "api",
    ) -> WriteOutcome:
        content_hash = entry_content_hash(
            kind=kind, text=text, source_refs=source_refs, artifact_refs=artifact_refs, categories=categories
        )
        dedup_key = compute_dedup_key(scope.scope_key, kind, content_hash)

        scope_doc = self._ensure_scope(scope)
        if expected_revision is not None and expected_revision != scope_doc.published_revision:
            raise RevisionConflictError(
                scope_key=scope.scope_key,
                artifact_id=scope_doc.artifact_id,
                expected_revision=expected_revision,
                current_revision=scope_doc.published_revision,
            )

        entry_id = uuid.uuid4().hex
        entry_version_id = uuid.uuid4().hex

        claimed, noop = self._acquire_claim(
            scope, scope_doc, dedup_key, entry_id, entry_version_id, content_hash=content_hash
        )
        if not claimed:
            return noop

        return self._publish_content(
            scope,
            scope_doc,
            event_type=EVENT_CREATED,
            entry_id=entry_id,
            entry_version_id=entry_version_id,
            next_version=1,
            kind=kind,
            text=text,
            categories=categories,
            source_refs=source_refs,
            artifact_refs=artifact_refs,
            content_hash=content_hash,
            dedup_key=dedup_key,
            old_dedup_key=None,
            metadata=metadata,
            expires_at=expires_at,
            expected_revision=expected_revision,
            provenance=provenance,
        )

    # -- revise --------------------------------------------------------------------

    def revise(
        self,
        scope: ScopeIdentity,
        entry_id: str,
        *,
        kind: Optional[str] = None,
        text: Optional[str] = None,
        categories: Optional[List[str]] = None,
        source_refs: Optional[List[str]] = None,
        artifact_refs: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        expires_at: Optional[str] = None,
        expected_revision: Optional[int] = None,
    ) -> WriteOutcome:
        scope_doc = self._ensure_scope(scope)
        head = self.store.get_head(scope.scope_key, entry_id)
        if head is None:
            raise EntryNotFoundError(f"Entry {entry_id} not found in scope")
        if head.get("state") != ACTIVE:
            raise ContextError("Entry is not active; reactivate before revising")

        next_kind = kind if kind is not None else head["kind"]
        next_text = text if text is not None else head["text"]
        next_categories = list(categories) if categories is not None else list(head.get("categories") or [])
        next_source_refs = list(source_refs) if source_refs is not None else list(head.get("source_refs") or [])
        next_artifact_refs = list(artifact_refs) if artifact_refs is not None else list(head.get("artifact_refs") or [])

        content_hash = entry_content_hash(
            kind=next_kind,
            text=next_text,
            source_refs=next_source_refs,
            artifact_refs=next_artifact_refs,
            categories=next_categories,
        )
        if content_hash == head.get("content_hash"):
            return WriteOutcome(
                outcome="noop",
                event_type=None,
                scope_key=scope.scope_key,
                artifact_id=scope_doc.artifact_id,
                revision=scope_doc.published_revision,
                entry_id=entry_id,
                entry_version_id=head.get("entry_version_id"),
                version=int(head.get("version", 0)),
                kind=next_kind,
                text=next_text,
                content_hash=content_hash,
                state_after=ACTIVE,
            )

        dedup_key = compute_dedup_key(scope.scope_key, next_kind, content_hash)
        old_dedup_key = compute_dedup_key(scope.scope_key, head["kind"], head["content_hash"])

        new_version_id = uuid.uuid4().hex
        existing = self.store.try_claim_dedup(
            scope.scope_key,
            dedup_key,
            entry_id=entry_id,
            entry_version_id=new_version_id,
            scope_revision=scope_doc.published_revision + 1,
        )
        if existing is not None:
            if existing.get("status") == ACTIVE:
                if existing.get("entry_id") == entry_id:
                    # Same entry converging onto identical content: no-op.
                    return WriteOutcome(
                        outcome="noop",
                        event_type=None,
                        scope_key=scope.scope_key,
                        artifact_id=scope_doc.artifact_id,
                        revision=scope_doc.published_revision,
                        entry_id=entry_id,
                        entry_version_id=head.get("entry_version_id"),
                        version=int(head.get("version", 0)),
                        kind=next_kind,
                        text=next_text,
                        content_hash=content_hash,
                        state_after=ACTIVE,
                    )
                raise DedupConflictError(
                    f"Same content already active in entry {existing.get('entry_id')}"
                )
            if existing.get("status") == "prepared" and not self._claim_expired(existing):
                raise OperationInProgressError(
                    "Another revise of the same content is in flight; retry the same command"
                )
            # released or expired prepared: take over the claim
            self.store.set_dedup_status(
                scope.scope_key,
                dedup_key,
                "prepared",
                entry_id=entry_id,
                entry_version_id=new_version_id,
                scope_revision=scope_doc.published_revision + 1,
            )

        return self._publish_content(
            scope,
            scope_doc,
            event_type=EVENT_REVISED,
            entry_id=entry_id,
            entry_version_id=new_version_id,
            next_version=int(head.get("version", 0)) + 1,
            kind=next_kind,
            text=next_text,
            categories=next_categories,
            source_refs=next_source_refs,
            artifact_refs=next_artifact_refs,
            content_hash=content_hash,
            dedup_key=dedup_key,
            old_dedup_key=old_dedup_key,
            metadata=metadata if metadata is not None else head.get("metadata"),
            expires_at=expires_at if expires_at is not None else head.get("expires_at"),
            expected_revision=expected_revision,
        )

    # -- retire / reactivate / purge -------------------------------------------------

    def retire(
        self,
        scope: ScopeIdentity,
        entry_id: str,
        *,
        reason: Optional[str] = None,
        expected_revision: Optional[int] = None,
    ) -> WriteOutcome:
        head, scope_doc = self._require_head(scope, entry_id)
        if head.get("state") != ACTIVE:
            return self._noop_state(scope, scope_doc, head, INACTIVE)
        # retire releases the content claim so the same fact can come back
        # with a new entry identity (design §5.3)
        dedup_key = compute_dedup_key(scope.scope_key, head["kind"], head["content_hash"])
        return self._state_flip(
            scope, scope_doc, head, EVENT_RETIRED, INACTIVE, reason, expected_revision, dedup_key=dedup_key
        )

    def reactivate(
        self,
        scope: ScopeIdentity,
        entry_id: str,
        *,
        reason: Optional[str] = None,
        expected_revision: Optional[int] = None,
    ) -> WriteOutcome:
        head, scope_doc = self._require_head(scope, entry_id)
        if head.get("state") == ACTIVE:
            return self._noop_state(scope, scope_doc, head, ACTIVE)
        dedup_key = compute_dedup_key(scope.scope_key, head["kind"], head["content_hash"])
        self._claim_for_reactivate(scope, dedup_key, head, scope_doc)
        return self._state_flip(
            scope, scope_doc, head, EVENT_REACTIVATED, ACTIVE, reason, expected_revision, dedup_key=dedup_key
        )

    def purge(
        self,
        scope: ScopeIdentity,
        entry_id: str,
        *,
        reason: Optional[str] = None,
        expected_revision: Optional[int] = None,
    ) -> WriteOutcome:
        """Physical delete (legacy DELETE purge=true): head + versions are
        removed, an audit event remains, the dedup claim is released."""
        head, scope_doc = self._require_head(scope, entry_id)
        dedup_key = compute_dedup_key(scope.scope_key, head["kind"], head["content_hash"])
        outcome = self._state_flip(
            scope, scope_doc, head, EVENT_PURGED, "purged", reason, expected_revision, dedup_key=dedup_key
        )
        self._retry_derived(lambda: self.store.delete_head(scope.scope_key, entry_id))
        self._retry_derived(lambda: self.store.delete_entry_versions(scope.scope_key, entry_id))
        self._refresh()
        return outcome

    # -- claim acquisition --------------------------------------------------------

    def _acquire_claim(
        self,
        scope: ScopeIdentity,
        scope_doc: ScopeDoc,
        dedup_key: str,
        entry_id: str,
        entry_version_id: str,
        *,
        content_hash: str,
    ) -> Tuple[bool, Optional[WriteOutcome]]:
        """Returns (claimed, noop_outcome). When claimed is False the caller
        must return the noop outcome untouched."""
        existing = self.store.try_claim_dedup(
            scope.scope_key,
            dedup_key,
            entry_id=entry_id,
            entry_version_id=entry_version_id,
            scope_revision=scope_doc.published_revision + 1,
        )
        if existing is None:
            return True, None

        status = existing.get("status")
        if status == ACTIVE:
            head = self.store.get_head(scope.scope_key, existing.get("entry_id"))
            if head is not None and head.get("content_hash") == content_hash:
                return False, self._noop_from_head(scope, scope_doc, head)
            # active claim whose head diverged (repair pending / stale): re-claim
            self._reclaim(scope, dedup_key, entry_id, entry_version_id, scope_doc)
            return True, None

        if status == "prepared":
            recovered = self._try_complete_prepared(scope, scope_doc, existing)
            if recovered is not None:
                return False, recovered
            if self._claim_expired(existing):
                self._reclaim(scope, dedup_key, entry_id, entry_version_id, scope_doc)
                return True, None
            settled = self._await_claim(scope, dedup_key)
            if settled is not None:
                if settled.get("status") == ACTIVE:
                    head = self.store.get_head(scope.scope_key, settled.get("entry_id"))
                    if head is not None and head.get("content_hash") == content_hash:
                        return False, self._noop_from_head(scope, scope_doc, head)
                if settled.get("status") == "released":
                    self._reclaim(scope, dedup_key, entry_id, entry_version_id, scope_doc)
                    return True, None
            raise OperationInProgressError(
                "Another writer is completing the same content claim; retry the same command"
            )

        # released → fresh claim with our identity
        self._reclaim(scope, dedup_key, entry_id, entry_version_id, scope_doc)
        return True, None

    def _reclaim(
        self, scope: ScopeIdentity, dedup_key: str, entry_id: str, entry_version_id: str, scope_doc: ScopeDoc
    ) -> None:
        self.store.set_dedup_status(
            scope.scope_key,
            dedup_key,
            "prepared",
            entry_id=entry_id,
            entry_version_id=entry_version_id,
            scope_revision=scope_doc.published_revision + 1,
        )

    def _try_complete_prepared(
        self, scope: ScopeIdentity, scope_doc: ScopeDoc, claim: dict
    ) -> Optional[WriteOutcome]:
        """The CAS may already have published this claim; finish its derived
        writes and report the original outcome (idempotent recovery, §5.2)."""
        entry_id = claim.get("entry_id")
        entry_version_id = claim.get("entry_version_id")
        revision = int(claim.get("scope_revision", 0))
        if not (
            scope_doc.published_revision >= revision
            and scope_doc.last_entry_version_id == entry_version_id
        ):
            return None
        event = self.store.get_event(scope.scope_key, revision)
        if event is None:
            return None
        head = self.store.get_head(scope.scope_key, entry_id)
        if head is None:
            version_doc = self.store.find_version(entry_version_id)
            if version_doc is None:
                return None
            head = self._build_head(scope, version_doc, state=ACTIVE)
            self._retry_derived(lambda: self.store.put_head(head))
        self._retry_derived(
            lambda: self.store.set_dedup_status(
                scope.scope_key, claim["dedup_key"], ACTIVE, entry_id=entry_id, entry_version_id=entry_version_id
            )
        )
        self._refresh()
        return WriteOutcome(
            outcome="created" if event.get("event_type") == EVENT_CREATED else "updated",
            event_type=event.get("event_type"),
            scope_key=scope.scope_key,
            artifact_id=scope_doc.artifact_id,
            revision=scope_doc.published_revision,
            entry_id=entry_id,
            entry_version_id=entry_version_id,
            version=int(head.get("version", 1)),
            kind=head.get("kind"),
            text=head.get("text"),
            content_hash=head.get("content_hash"),
            state_after=ACTIVE,
        )

    def _claim_expired(self, claim: dict) -> bool:
        updated = claim.get("updated_at")
        if not updated:
            return True
        try:
            parsed = datetime.fromisoformat(updated)
            return (datetime.now(timezone.utc) - parsed).total_seconds() > self.prepared_ttl_seconds
        except ValueError:
            return True

    def _await_claim(self, scope: ScopeIdentity, dedup_key: str) -> Optional[dict]:
        """Bounded wait for a concurrent same-content writer to settle, so the
        natural result is a noop instead of a 409 (acceptance §12.2)."""
        deadline = time.monotonic() + self.claim_wait_seconds
        while time.monotonic() < deadline:
            time.sleep(CLAIM_POLL_INTERVAL)
            refreshed = self.store.get_dedup(scope.scope_key, dedup_key)
            if refreshed is None or refreshed.get("status") != "prepared":
                return refreshed
        return None

    def _claim_for_reactivate(self, scope, dedup_key: str, head: dict, scope_doc: ScopeDoc) -> None:
        """Reactivate re-acquires the dedup claim; another active owner is a 409."""
        existing = self.store.try_claim_dedup(
            scope.scope_key,
            dedup_key,
            entry_id=head["entry_id"],
            entry_version_id=head.get("entry_version_id"),
            scope_revision=scope_doc.published_revision + 1,
        )
        if existing is None:
            return
        if existing.get("status") == ACTIVE:
            if existing.get("entry_id") == head["entry_id"]:
                return
            raise DedupConflictError(
                f"Same content already active in entry {existing.get('entry_id')}"
            )
        if existing.get("status") == "prepared" and existing.get("entry_id") == head["entry_id"]:
            return  # our own in-flight claim
        # released or foreign expired claim: take it over deterministically
        self._reclaim(scope, dedup_key, head["entry_id"], head.get("entry_version_id"), scope_doc)

    # -- publish ----------------------------------------------------------------

    def _publish_content(
        self,
        scope: ScopeIdentity,
        scope_doc: ScopeDoc,
        *,
        event_type: str,
        entry_id: str,
        entry_version_id: str,
        next_version: int,
        kind: str,
        text: str,
        categories: List[str],
        source_refs: List[str],
        artifact_refs: List[str],
        content_hash: str,
        dedup_key: str,
        old_dedup_key: Optional[str],
        metadata: Optional[Dict[str, Any]] = None,
        expires_at: Optional[str] = None,
        expected_revision: Optional[int] = None,
        provenance: str = "api",
    ) -> WriteOutcome:
        version_doc = {
            "scope_key": scope.scope_key,
            **scope.fields,
            "entry_id": entry_id,
            "entry_version_id": entry_version_id,
            "version": next_version,
            "kind": kind,
            "content_hash": content_hash,
            "text": text,
            "source_refs": source_refs,
            "artifact_refs": artifact_refs,
            "categories": categories,
            "scope_revision": 0,  # filled per CAS attempt
            "provenance": provenance,
            "legacy_ids": [entry_id],
            "created_at": utcnow(),
        }

        last_error: Optional[Exception] = None
        for _ in range(CAS_RETRIES):
            baseline = self.store.get_scope(scope.scope_key)
            if baseline is None:
                raise PrimaryUnavailableError("Scope document vanished mid-publish")
            if expected_revision is not None and expected_revision != baseline.published_revision:
                raise RevisionConflictError(
                    scope_key=scope.scope_key,
                    artifact_id=baseline.artifact_id,
                    expected_revision=expected_revision,
                    current_revision=baseline.published_revision,
                )
            new_revision = baseline.published_revision + 1
            version_doc["scope_revision"] = new_revision
            self.store.put_version(version_doc)
            try:
                self.store.cas_publish(
                    scope.scope_key,
                    if_seq_no=baseline.seq_no,
                    if_primary_term=baseline.primary_term,
                    published_revision=new_revision,
                    last_event_type=event_type,
                    last_entry_id=entry_id,
                    last_entry_version_id=entry_version_id,
                )
            except EsCasConflict as exc:
                last_error = exc
                if expected_revision is not None:
                    raise RevisionConflictError(
                        scope_key=scope.scope_key,
                        artifact_id=baseline.artifact_id,
                        expected_revision=expected_revision,
                        current_revision=baseline.published_revision + 1,
                    ) from exc
                continue  # fresh baseline, bounded retry
            return self._finish_publish(
                scope,
                baseline,
                event_type=event_type,
                entry_id=entry_id,
                entry_version_id=entry_version_id,
                version=next_version,
                version_doc=version_doc,
                dedup_key=dedup_key,
                old_dedup_key=old_dedup_key,
                metadata=metadata,
                expires_at=expires_at,
            )
        raise RevisionConflictError(
            scope_key=scope.scope_key,
            artifact_id=scope_doc.artifact_id,
            expected_revision=expected_revision,
            current_revision=None,
        ) from last_error

    def _finish_publish(
        self,
        scope: ScopeIdentity,
        baseline: ScopeDoc,
        *,
        event_type: str,
        entry_id: str,
        entry_version_id: str,
        version: int,
        version_doc: dict,
        dedup_key: str,
        old_dedup_key: Optional[str],
        metadata: Optional[Dict[str, Any]],
        expires_at: Optional[str],
    ) -> WriteOutcome:
        """Design §5.2 step 5: derived, rebuildable writes after the CAS.
        Failures surface as PublishedRepairPendingError (503)."""
        revision = baseline.published_revision + 1
        vector, embedding_status, pending = self._embed(version_doc["text"])
        head = {
            "scope_key": scope.scope_key,
            **scope.fields,
            "entry_id": entry_id,
            "entry_version_id": entry_version_id,
            "version": version,
            "kind": version_doc["kind"],
            "state": ACTIVE,
            "content_hash": version_doc["content_hash"],
            "scope_revision": revision,
            "text": version_doc["text"],
            "searchable_text": lemmatize_for_bm25(version_doc["text"]),
            "categories": version_doc.get("categories") or [],
            "source_refs": version_doc.get("source_refs") or [],
            "artifact_refs": version_doc.get("artifact_refs") or [],
            "embedding_status": embedding_status,
            "legacy_ids": version_doc.get("legacy_ids") or [entry_id],
            "created_at": utcnow(),
            "updated_at": utcnow(),
        }
        if vector is not None:
            head["vector"] = vector
        if metadata:
            head["metadata"] = metadata
        if expires_at:
            head["expires_at"] = expires_at
        event = {
            "scope_key": scope.scope_key,
            "scope_revision": revision,
            "event_type": event_type,
            "entry_id": entry_id,
            "entry_version_id": entry_version_id,
            "version": version,
            "kind": version_doc["kind"],
            "provenance": version_doc.get("provenance") or "api",
            "content_hash": version_doc["content_hash"],
            "state_after": ACTIVE,
            "created_at": utcnow(),
        }
        try:
            self._retry_derived(lambda: self.store.put_head(head))
            self._retry_derived(lambda: self.store.put_event(event))
            self._retry_derived(
                lambda: self.store.set_dedup_status(
                    scope.scope_key, dedup_key, ACTIVE, entry_id=entry_id, entry_version_id=entry_version_id
                )
            )
            if old_dedup_key and old_dedup_key != dedup_key:
                self._retry_derived(
                    lambda: self.store.set_dedup_status(scope.scope_key, old_dedup_key, "released")
                )
            self._refresh()
        except Exception as exc:
            logger.warning(
                "Derived writes pending for %s revision %d: %s", scope.scope_key, revision, exc
            )
            raise PublishedRepairPendingError(
                "Memory published; derived documents are being repaired — retry the same command",
                scope_key=scope.scope_key,
                revision=revision,
                entry_id=entry_id,
            ) from exc
        outcome = "created" if event_type == EVENT_CREATED else "updated"
        return WriteOutcome(
            outcome=outcome,
            event_type=event_type,
            scope_key=scope.scope_key,
            artifact_id=baseline.artifact_id,
            revision=revision,
            entry_id=entry_id,
            entry_version_id=entry_version_id,
            version=version,
            kind=version_doc["kind"],
            text=version_doc["text"],
            content_hash=version_doc["content_hash"],
            state_after=ACTIVE,
            pending_embed=pending,
        )

    def _state_flip(
        self,
        scope: ScopeIdentity,
        scope_doc: ScopeDoc,
        head: dict,
        event_type: str,
        state_after: str,
        reason: Optional[str],
        expected_revision: Optional[int],
        *,
        dedup_key: Optional[str] = None,
    ) -> WriteOutcome:
        """retire/reactivate/purge: no version write; CAS then head/event/dedup."""
        entry_id = head["entry_id"]
        entry_version_id = head.get("entry_version_id")
        last_error: Optional[Exception] = None
        for _ in range(CAS_RETRIES):
            baseline = self.store.get_scope(scope.scope_key)
            if baseline is None:
                raise PrimaryUnavailableError("Scope document vanished mid-publish")
            if expected_revision is not None and expected_revision != baseline.published_revision:
                raise RevisionConflictError(
                    scope_key=scope.scope_key,
                    artifact_id=baseline.artifact_id,
                    expected_revision=expected_revision,
                    current_revision=baseline.published_revision,
                )
            revision = baseline.published_revision + 1
            event = {
                "scope_key": scope.scope_key,
                "scope_revision": revision,
                "event_type": event_type,
                "entry_id": entry_id,
                "entry_version_id": entry_version_id,
                "version": int(head.get("version", 0)),
                "kind": head.get("kind"),
                "provenance": "api",
                "content_hash": head.get("content_hash"),
                "state_after": state_after,
                "reason": reason,
                "created_at": utcnow(),
            }
            try:
                self.store.cas_publish(
                    scope.scope_key,
                    if_seq_no=baseline.seq_no,
                    if_primary_term=baseline.primary_term,
                    published_revision=revision,
                    last_event_type=event_type,
                    last_entry_id=entry_id,
                    last_entry_version_id=entry_version_id,
                )
            except EsCasConflict as exc:
                last_error = exc
                if expected_revision is not None:
                    raise RevisionConflictError(
                        scope_key=scope.scope_key,
                        artifact_id=baseline.artifact_id,
                        expected_revision=expected_revision,
                        current_revision=baseline.published_revision + 1,
                    ) from exc
                continue
            try:
                if event_type != EVENT_PURGED:
                    next_head = dict(head)
                    next_head["state"] = state_after
                    next_head["scope_revision"] = revision
                    next_head["updated_at"] = utcnow()
                    if state_after == ACTIVE and "vector" not in next_head:
                        next_head["embedding_status"] = "pending"
                    self._retry_derived(lambda: self.store.put_head(next_head))
                self._retry_derived(lambda: self.store.put_event(event))
                if dedup_key:
                    if state_after == ACTIVE:
                        self._retry_derived(
                            lambda: self.store.set_dedup_status(
                                scope.scope_key,
                                dedup_key,
                                ACTIVE,
                                entry_id=entry_id,
                                entry_version_id=entry_version_id,
                            )
                        )
                    else:
                        self._retry_derived(
                            lambda: self.store.set_dedup_status(scope.scope_key, dedup_key, "released")
                        )
                self._refresh()
            except Exception as exc:
                logger.warning(
                    "Derived writes pending for %s revision %d: %s", scope.scope_key, revision, exc
                )
                raise PublishedRepairPendingError(
                    "Memory published; derived documents are being repaired — retry the same command",
                    scope_key=scope.scope_key,
                    revision=revision,
                    entry_id=entry_id,
                ) from exc
            return WriteOutcome(
                outcome="updated",
                event_type=event_type,
                scope_key=scope.scope_key,
                artifact_id=baseline.artifact_id,
                revision=revision,
                entry_id=entry_id,
                entry_version_id=entry_version_id,
                version=int(head.get("version", 0)),
                kind=head.get("kind"),
                text=head.get("text"),
                content_hash=head.get("content_hash"),
                state_after=state_after,
            )
        raise RevisionConflictError(
            scope_key=scope.scope_key,
            artifact_id=scope_doc.artifact_id,
            expected_revision=expected_revision,
            current_revision=None,
        ) from last_error

    # -- helpers ----------------------------------------------------------------

    def _ensure_scope(self, scope: ScopeIdentity) -> ScopeDoc:
        scope_doc = self.store.get_scope(scope.scope_key)
        if scope_doc is None:
            self.store.create_scope(scope.scope_key, scope.fields)
            scope_doc = self.store.get_scope(scope.scope_key)
        if scope_doc is None:  # ES unavailable between calls
            raise PrimaryUnavailableError("Scope document unavailable after creation")
        return scope_doc

    def _require_head(self, scope: ScopeIdentity, entry_id: str):
        scope_doc = self._ensure_scope(scope)
        head = self.store.get_head(scope.scope_key, entry_id)
        if head is None:
            raise EntryNotFoundError(f"Entry {entry_id} not found in scope")
        return head, scope_doc

    def _noop_from_head(self, scope, scope_doc, head) -> WriteOutcome:
        return WriteOutcome(
            outcome="noop",
            event_type=None,
            scope_key=scope.scope_key,
            artifact_id=scope_doc.artifact_id,
            revision=scope_doc.published_revision,
            entry_id=head.get("entry_id"),
            entry_version_id=head.get("entry_version_id"),
            version=int(head.get("version", 0)),
            kind=head.get("kind"),
            text=head.get("text"),
            content_hash=head.get("content_hash"),
            state_after=head.get("state"),
        )

    def _noop_state(self, scope, scope_doc, head, state) -> WriteOutcome:
        outcome = self._noop_from_head(scope, scope_doc, head)
        outcome.state_after = state
        return outcome

    def _embed(self, text: str):
        if self.embedder is None:
            return None, "pending", True
        try:
            vector = self.embedder.embed(text, "memory")
            return vector, "ready", False
        except Exception as exc:
            logger.warning("Embedding failed; head stays embedding_status=pending (%s)", exc)
            return None, "pending", True

    def _build_head(self, scope, version_doc: dict, *, state: str) -> dict:
        head = {
            "scope_key": scope.scope_key,
            **scope.fields,
            "entry_id": version_doc["entry_id"],
            "entry_version_id": version_doc["entry_version_id"],
            "version": int(version_doc.get("version", 1)),
            "kind": version_doc["kind"],
            "state": state,
            "content_hash": version_doc["content_hash"],
            "scope_revision": int(version_doc.get("scope_revision", 0)),
            "text": version_doc["text"],
            "searchable_text": lemmatize_for_bm25(version_doc["text"]),
            "categories": version_doc.get("categories") or [],
            "source_refs": version_doc.get("source_refs") or [],
            "artifact_refs": version_doc.get("artifact_refs") or [],
            "embedding_status": "pending",
            "legacy_ids": version_doc.get("legacy_ids") or [version_doc["entry_id"]],
            "created_at": utcnow(),
            "updated_at": utcnow(),
        }
        if version_doc.get("expires_at"):
            head["expires_at"] = version_doc["expires_at"]
        return head

    def _retry_derived(self, fn) -> None:
        last: Optional[Exception] = None
        for _ in range(DERIVED_WRITE_RETRIES):
            try:
                fn()
                return
            except Exception as exc:
                cls = classify_elasticsearch_error(exc)
                if cls in ("UNAVAILABLE", "TIMEOUT", "THROTTLED"):
                    last = exc
                    time.sleep(0.2)
                    continue
                raise
        raise last or PrimaryUnavailableError("derived write failed")

    def _refresh(self) -> None:
        """refresh=wait_for equivalent (design §5.2 step 5): make every
        derived document — head AND event AND the scope watermark itself —
        visible to SEARCH before the caller sees success. By-id GETs are
        realtime, but the reconciler's scans and the /changes feed go through
        _search, which only sees refreshed segments."""
        for family in ("scope", "head", "event", "version"):
            try:
                self.store.client.indices.refresh(index=self.store.alias(family))
            except Exception:
                logger.debug("post-publish refresh failed for %s", family, exc_info=True)
