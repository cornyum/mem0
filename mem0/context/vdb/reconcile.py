"""RecoveryReconciler + EmbeddingReconciler (design §5.4).

The RecoveryReconciler repairs publishes whose CAS succeeded but whose
derived writes (head/event/dedup) did not complete, releases stale prepared
claims, and sweeps orphan versions — all through deterministic-``_id``
upserts so multiple instances may run concurrently (§5.4). The
EmbeddingReconciler backfills ``embedding_status=pending`` head vectors so
pending heads eventually join the semantic channel (§6.4).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict

from mem0.context.vdb.es_store import ElasticsearchMemoryStore, utcnow
from mem0.context.vdb.write import (
    ACTIVE,
    EVENT_CREATED,
    EVENT_REACTIVATED,
    EVENT_REVISED,
)

logger = logging.getLogger(__name__)

# Scopes touched within this window get a head/event existence check.
RECENT_SCOPE_WINDOW = timedelta(minutes=30)


class RecoveryReconciler:
    def __init__(self, store: ElasticsearchMemoryStore, *, prepared_ttl_seconds: float = 300.0):
        self.store = store
        self.prepared_ttl_seconds = prepared_ttl_seconds

    def reconcile(self, *, limit: int = 200) -> Dict[str, int]:
        counts = {
            "prepared_completed": 0,
            "prepared_released": 0,
            "head_rebuilt": 0,
            "event_rebuilt": 0,
            "orphan_versions_deleted": 0,
        }
        self._repair_prepared_claims(counts, limit=limit)
        self._verify_recent_scopes(counts, limit=limit)
        self._sweep_orphan_versions(counts, limit=limit)
        if any(counts.values()):
            logger.info("RecoveryReconciler pass: %s", counts)
        return counts

    # -- step 1: prepared claims (§5.4.1-2) -------------------------------------

    def _repair_prepared_claims(self, counts: Dict[str, int], *, limit: int) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=self.prepared_ttl_seconds)).isoformat()
        claims = self.store.scan_prepared_dedups(older_than_iso=cutoff, limit=limit)
        for claim in claims:
            scope_key = claim.get("scope_key")
            scope = self.store.get_scope(scope_key) if scope_key else None
            if scope is None:
                # scope gone entirely (rebuild in progress): release the claim
                self.store.set_dedup_status(scope_key, claim["dedup_key"], "released")
                counts["prepared_released"] += 1
                continue
            claim_revision = int(claim.get("scope_revision", 0))
            published_here = (
                scope.published_revision >= claim_revision
                and scope.last_entry_version_id == claim.get("entry_version_id")
            )
            if published_here:
                if self._complete_publish(scope_key, scope, claim):
                    counts["prepared_completed"] += 1
                continue
            if scope.published_revision >= claim_revision:
                # published past the claim without matching it: the claim lost
                # the race — release so future writers can retry (§5.4.2).
                self.store.set_dedup_status(scope_key, claim["dedup_key"], "released")
                counts["prepared_released"] += 1
                continue
            # never published and past TTL: rollback (§5.4.2 "否则回滚")
            self.store.set_dedup_status(scope_key, claim["dedup_key"], "released")
            counts["prepared_released"] += 1

    def _complete_publish(self, scope_key: str, scope, claim: dict) -> bool:
        entry_id = claim.get("entry_id")
        revision = int(claim.get("scope_revision", 0))
        event = self.store.get_event(scope_key, revision)
        if event is None:
            version_doc = self.store.find_version(claim.get("entry_version_id"))
            if version_doc is None:
                logger.warning(
                    "Cannot complete claim %s: neither event nor version exists", claim.get("dedup_key")
                )
                return False
            event = {
                "scope_key": scope_key,
                "scope_revision": revision,
                "event_type": scope.last_event_type or EVENT_CREATED,
                "entry_id": entry_id,
                "entry_version_id": claim.get("entry_version_id"),
                "version": int(version_doc.get("version", 1)),
                "kind": version_doc.get("kind"),
                "provenance": version_doc.get("provenance") or "api",
                "content_hash": version_doc.get("content_hash"),
                "state_after": ACTIVE,
                "created_at": utcnow(),
            }
            self.store.put_event(event)
        head = self.store.get_head(scope_key, entry_id)
        if head is None and event.get("event_type") in (EVENT_CREATED, EVENT_REVISED, EVENT_REACTIVATED):
            version_doc = self.store.find_version(claim.get("entry_version_id"))
            if version_doc is None:
                return False
            head = self._head_from_version(scope, version_doc, event)
            if head is not None:
                self.store.put_head(head)
        self.store.set_dedup_status(
            scope_key,
            claim["dedup_key"],
            ACTIVE,
            entry_id=entry_id,
            entry_version_id=claim.get("entry_version_id"),
        )
        return True

    def _head_from_version(self, scope, version_doc: dict, event: dict):
        from mem0.utils.lemmatization import lemmatize_for_bm25

        return {
            "scope_key": scope.scope_key,
            **scope.fields,
            "entry_id": version_doc["entry_id"],
            "entry_version_id": version_doc["entry_version_id"],
            "version": int(version_doc.get("version", 1)),
            "kind": version_doc.get("kind"),
            "state": event.get("state_after") or ACTIVE,
            "content_hash": version_doc.get("content_hash"),
            "scope_revision": int(version_doc.get("scope_revision", 0)),
            "text": version_doc.get("text"),
            "searchable_text": lemmatize_for_bm25(version_doc.get("text") or ""),
            "categories": version_doc.get("categories") or [],
            "source_refs": version_doc.get("source_refs") or [],
            "artifact_refs": version_doc.get("artifact_refs") or [],
            "embedding_status": "pending",
            "legacy_ids": version_doc.get("legacy_ids") or [version_doc["entry_id"]],
            "created_at": utcnow(),
            "updated_at": utcnow(),
        }

    # -- step 2: recent scope last_* consistency (§5.4.3) ------------------------

    def _verify_recent_scopes(self, counts: Dict[str, int], *, limit: int) -> None:
        cutoff = (datetime.now(timezone.utc) - RECENT_SCOPE_WINDOW).isoformat()
        scopes = self.store.scan_recent_scopes(updated_after=cutoff, limit=limit)
        for scope in scopes:
            revision = scope.published_revision
            if revision <= 0 or not scope.last_entry_id:
                continue
            event = self.store.get_event(scope.scope_key, revision)
            head = self.store.get_head(scope.scope_key, scope.last_entry_id)
            if event is None:
                version_doc = self.store.find_version(scope.last_entry_version_id)
                event = {
                    "scope_key": scope.scope_key,
                    "scope_revision": revision,
                    "event_type": scope.last_event_type or EVENT_CREATED,
                    "entry_id": scope.last_entry_id,
                    "entry_version_id": scope.last_entry_version_id,
                    "version": int((version_doc or {}).get("version", 0) or 0),
                    "kind": (version_doc or head or {}).get("kind"),
                    "provenance": (version_doc or {}).get("provenance") or "api",
                    "content_hash": (version_doc or head or {}).get("content_hash"),
                    "state_after": (head or {}).get("state") or ACTIVE,
                    "created_at": utcnow(),
                }
                self.store.put_event(event)
                counts["event_rebuilt"] += 1
            if head is None and scope.last_event_type in (
                EVENT_CREATED,
                EVENT_REVISED,
                EVENT_REACTIVATED,
            ):
                version_doc = self.store.find_version(scope.last_entry_version_id)
                if version_doc is not None:
                    rebuilt = self._head_from_version(scope, version_doc, event or {})
                    self.store.put_head(rebuilt)
                    counts["head_rebuilt"] += 1

    # -- step 3: orphan sweep (§5.4.4) --------------------------------------------

    def _sweep_orphan_versions(self, counts: Dict[str, int], *, limit: int) -> None:
        orphans = self.store.scan_orphan_versions(limit=limit)
        for version_doc in orphans:
            self.store.delete_entry_versions(version_doc["scope_key"], version_doc["entry_id"])
            counts["orphan_versions_deleted"] += 1


class EmbeddingReconciler:
    """Fills ``embedding_status=pending`` head vectors (design §6.4)."""

    def __init__(self, store: ElasticsearchMemoryStore, embedder, *, limit: int = 200):
        self.store = store
        self.embedder = embedder
        self.limit = limit

    def reconcile(self) -> Dict[str, int]:
        embedded = 0
        failed = 0
        if self.embedder is None:
            return {"embedded": 0, "failed": 0, "skipped": self.limit}
        heads = self.store.scan_heads_by_embedding_status("pending", limit=self.limit)
        for head in heads:
            try:
                vector = self.embedder.embed(head.get("text") or "", "memory")
                self.store.set_head_vector(head["scope_key"], head["entry_id"], vector)
                embedded += 1
            except Exception as exc:
                logger.warning(
                    "Embedding backfill failed for entry %s: %s", head.get("entry_id"), exc
                )
                failed += 1
        if embedded or failed:
            logger.info("EmbeddingReconciler pass: embedded=%d failed=%d", embedded, failed)
        return {"embedded": embedded, "failed": failed}
