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
import json
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
from mem0.context.observability import Observability, application_op, shared_observability
from mem0.context.prepared import DEFAULT_BUDGET_BYTES
from mem0.context.scope import SCOPE_FIELDS, ScopeIdentity
from mem0.context.store import ACTIVE, INACTIVE, ContextStore, EntryVersionView, RememberOutcome

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PowerMemory(Memory):
    """Memory with an authoritative revision store behind it.

    Construct with a normal :class:`MemoryConfig` plus a
    :class:`ContextStore` bound to the deployment's app DB engine.
    """

    def __init__(
        self,
        config: MemoryConfig = MemoryConfig(),
        *,
        ctx_store: ContextStore,
        obs: Optional["Observability"] = None,
    ):
        super().__init__(config)
        self.ctx_store = ctx_store
        self._obs = obs if obs is not None else shared_observability()

    @classmethod
    def from_config(
        cls, config_dict: Dict[str, Any], *, ctx_store: ContextStore, obs: "Observability | None" = None
    ) -> "PowerMemory":
        try:
            config = MemoryConfig(**config_dict)
        except ValidationError as e:
            raise ContextValidationError(f"Invalid memory config: {e}") from e
        return cls(config, ctx_store=ctx_store, obs=obs)

    # -- explicit lifecycle ----------------------------------------------------

    @application_op("remember")
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

    @application_op("retire")
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

    @application_op("reactivate")
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

    @application_op("changes")
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

    @application_op("expand")
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

    @application_op("recall")
    def recall(
        self,
        query: str,
        *,
        limit: int = 10,
        mode: str = "auto",
        rerank: bool = False,
        threshold: float = 0.1,
        **ids: Optional[str],
    ) -> Dict[str, Any]:
        """Channel-transparent retrieval with authoritative freshness
        checks (design §2.2/§6.1).

        1. vector channels (mode-aware, matched_by attribution);
        2. every ctx-managed hit is validated against the authoritative
           head: retired entries and superseded projections are dropped
           even if the vector payload lags;
        3. pending entries (authoritative but not yet projected) are merged
           from the authoritative store via token match and marked
           ``stale: True`` — the read-your-writes guarantee.
        """
        scope = ScopeIdentity(**ids)
        response = self.search(
            query,
            top_k=limit,
            filters={k: v for k, v in ids.items() if v},
            threshold=threshold,
            rerank=rerank,
            mode=mode,
        )
        results = self._validate_against_heads(scope, response["results"])
        seen = {
            (item.get("metadata") or {}).get("entry_id") or item.get("id")
            for item in results
        }
        results.extend(
            self._merge_pending(scope, query, budget=limit - len(results), seen=seen)
        )
        return {"search_mode": response["search_mode"], "results": results}

    def _validate_against_heads(self, scope: ScopeIdentity, results: list) -> list:
        """Drop ctx-managed hits whose authoritative head disagrees with the
        vector projection (retired, or revised past this projection). Legacy
        entries without an authoritative record pass through unchanged."""
        heads = {
            h["entry_id"]: h
            for h in self.ctx_store.find_heads(scope, state=None, limit=1000)
        }
        kept = []
        for item in results:
            meta = item.get("metadata") or {}
            entry_id = meta.get("entry_id")
            if not entry_id:
                kept.append(item)
                continue
            head = heads.get(entry_id)
            if (
                head is None
                or head["state"] != ACTIVE
                or head["entry_version_id"] != meta.get("entry_version_id")
            ):
                logger.debug(
                    "recall dropped stale projection for entry %s (retired or superseded)", entry_id
                )
                continue
            kept.append(item)
        return kept

    def _merge_pending(self, scope: ScopeIdentity, query: str, *, budget: int, seen: set) -> list:
        """Read-your-writes: authoritative-but-unprojected entries matching
        the query tokens are merged as ``stale`` results via the
        fts_sidecar channel (design §2.2)."""
        if budget <= 0:
            return []
        query_tokens = set(analyzer.analyze(query).split())
        merged = []
        for row in self.ctx_store.pending_entries_with_text(scope):
            if row["entry_id"] in seen:
                continue
            if not query_tokens & set(row["searchable_text"].split()):
                continue
            merged.append(
                {
                    "id": row["entry_id"],
                    "memory": row["text"],
                    "score": 0.0,
                    "matched_by": ["fts_sidecar"],
                    "stale": True,
                    "metadata": {
                        "kind": row["kind"],
                        "categories": json.loads(row["categories"]),
                        "entry_id": row["entry_id"],
                        "entry_version_id": row["entry_version_id"],
                        "entry_content_hash": row["entry_content_hash"],
                    },
                }
            )
            if len(merged) >= budget:
                break
        return merged

    @application_op("revise_bound")
    def revise_bound(self, memory_id: str, text: str) -> Optional[RememberResult]:
        """Dual-write sync for legacy PUT (design §5.4): revise the
        authoritative entry bound to this vector row. Returns None when the
        row was never adopted (legacy-only entry)."""
        head = self.ctx_store.get_head_by_vector_id(memory_id)
        if head is None:
            return None
        scope = ScopeIdentity(**{f: head.get(f) for f in SCOPE_FIELDS if head.get(f)})
        current = self.ctx_store.get_entry_version(scope, head["entry_id"], head["entry_version_id"])
        outcome = self.ctx_store.revise_entry(
            scope, head["entry_id"], kind=current.kind, text=text
        )
        # The legacy update already rewrote the vector row in place; keep
        # the binding authoritative for the new version.
        if outcome.entry is not None and outcome.outcome != OUTCOME_NOOP:
            self.ctx_store.bind_vector(
                scope, head["entry_id"],
                vector_id=memory_id, pending_embed=False,
                expected_entry_version_id=outcome.entry.entry_version_id,
            )
        return self._finalize_outcome(scope, outcome, project=False)

    @application_op("retire_bound")
    def retire_bound(self, memory_id: str, *, keep_projection: bool = False) -> Optional[RememberResult]:
        """Tombstone for legacy DELETE (design §5.4): retire the
        authoritative entry bound to this vector row. In dual mode the
        legacy path already deleted the vector (binding cleared); in
        authoritative mode (keep_projection=True) the vector row stays and
        only its payload state flips to inactive."""
        head = self.ctx_store.get_head_by_vector_id(memory_id)
        if head is None:
            return None
        scope = ScopeIdentity(**{f: head.get(f) for f in SCOPE_FIELDS if head.get(f)})
        outcome = self.ctx_store.set_entry_state(scope, head["entry_id"], active=False)
        if outcome.outcome != OUTCOME_NOOP:
            if keep_projection:
                self._sync_vector_state(scope, head["entry_id"], INACTIVE)
            else:
                self.ctx_store.bind_vector(
                    scope, head["entry_id"],
                    vector_id=None, pending_embed=False,
                    expected_entry_version_id=head["entry_version_id"],
                )
        return self._finalize_outcome(scope, outcome, project=False)

    @application_op("adopt")
    def adopt_legacy(
        self,
        *,
        memory_id: str,
        text: str,
        payload: Optional[Dict[str, Any]] = None,
        **ids: Optional[str],
    ) -> RememberResult:
        """Dual-write adoption (design §5.4): bind a vector row written by
        the legacy add pipeline to the authoritative store WITHOUT
        re-embedding — the projection already exists. Hash-dedup makes
        re-adoption (and backfill reruns) a no-op."""
        scope = ScopeIdentity(**ids)
        payload = payload or {}
        outcome = self.ctx_store.remember_entry(
            scope,
            kind=str(payload.get("kind") or "fact"),
            text=text,
            categories=payload.get("categories") or (),
            source_refs=(),
            artifact_refs=(),
        )
        if outcome.outcome != OUTCOME_NOOP and outcome.entry is not None:
            self.ctx_store.bind_vector(
                scope,
                outcome.entry.entry_id,
                vector_id=memory_id,
                pending_embed=False,
                expected_entry_version_id=outcome.entry.entry_version_id,
            )
        return self._finalize_outcome(scope, outcome, project=False)

    @application_op("backfill")
    def backfill(self, *, batch_size: int = 500) -> Dict[str, Any]:
        """Adopt existing vector rows into the authoritative store (design
        §5.4). Idempotent: adopted entries hash-match to no-op on rerun.
        Keyset pagination is used where the adapter supports ``after_id``;
        otherwise a single bounded batch is adopted per call and
        ``truncated`` reports whether more rows remain."""
        summary = {"scanned": 0, "created": 0, "noop": 0, "truncated": False}
        rows = self._list_projection_rows(batch_size + 1)
        if len(rows) > batch_size:
            summary["truncated"] = True
            rows = rows[:batch_size]
        for row in rows:
            payload = getattr(row, "payload", None) or {}
            text = payload.get("data")
            if not text:
                continue
            ids = {f: payload.get(f) for f in SCOPE_FIELDS if payload.get(f)}
            if not ids:
                continue
            summary["scanned"] += 1
            result = self.adopt_legacy(memory_id=str(row.id), text=text, payload=payload, **ids)
            summary[result.outcome] = summary.get(result.outcome, 0) + 1
        return summary

    def _list_projection_rows(self, limit: int):
        """Normalize the adapter zoo of ``list()`` return shapes: qdrant and
        pgvector paginate with ``(rows, offset)`` tuples, ES wraps rows in a
        ``[rows]`` list, others return OutputData or a bare list."""
        listed = self.vector_store.list(top_k=limit)
        if isinstance(listed, tuple):
            return listed[0]
        if isinstance(listed, list) and listed and isinstance(listed[0], list):
            return listed[0]
        return getattr(listed, "results", listed)

    @application_op("prepare")
    def prepare_context(
        self,
        query: str,
        *,
        budget_bytes: int = DEFAULT_BUDGET_BYTES,
        mode: str = "auto",
        **ids: Optional[str],
    ) -> Dict[str, Any]:
        """PreparedContext assembly (design §6.4): recall → ≤8 memory items,
        each with its version-exact citation, rendered into the
        trust-prefixed byte-budgeted envelope (schema
        agentar.prepared-context.v1)."""
        from mem0.context.prepared import MAX_MEMORY_ITEMS, PreparedItem, build_prepared_context

        scope = ScopeIdentity(**ids)
        recall = self.recall(query, limit=MAX_MEMORY_ITEMS, mode=mode, **ids)
        try:
            artifact_id = self.ctx_store.get_artifact_id(scope)
        except EntryNotFoundError:
            artifact_id = None

        items = []
        for result in recall["results"]:
            meta = result.get("metadata") or {}
            citation = None
            if artifact_id and meta.get("entry_id"):
                citation = {
                    "artifact_id": artifact_id,
                    "entry_id": meta["entry_id"],
                    "entry_version_id": meta.get("entry_version_id"),
                }
            items.append(PreparedItem(type="memory", text=result["memory"], citation=citation))

        prepared = build_prepared_context(items, budget_bytes=budget_bytes)
        return {
            "schema": prepared.schema,
            "rendered": prepared.rendered,
            "item_count": prepared.item_count,
            "dropped": prepared.dropped,
            "budget_bytes": prepared.budget_bytes,
            "rendered_bytes": prepared.rendered_bytes,
            "search_mode": recall["search_mode"],
        }

    @staticmethod
    def _citation_from_dict(data: Dict[str, Any]) -> "MemoryCitation":
        from mem0.context.models import MemoryCitation as _MC

        return _MC(
            artifact_id=data["artifact_id"],
            entry_id=data["entry_id"],
            entry_version_id=data["entry_version_id"],
        )


    # -- P2: Review Inbox (design §7, RFC 0050) ----------------------------------

    @application_op("candidate_propose")
    def propose_candidate(
        self,
        *,
        family: str,
        proposal: Dict[str, Any],
        source_refs: tuple[str, ...] | list[str],
        reason: Optional[str] = None,
        **ids: Optional[str],
    ) -> Dict[str, Any]:
        """Submit an experience/skill candidate: persisted, untrusted, out
        of retrieval until approved. Evidence refs must exist in this
        scope's source journal — LLMs may draft, never invent evidence."""
        scope = ScopeIdentity(**ids)
        return self.ctx_store.propose_candidate(
            scope, family=family, proposal=proposal, source_refs=source_refs, reason=reason
        )

    @application_op("candidate_list")
    def list_candidates(
        self, *, status: Optional[str] = "pending", limit: int = 50, **ids: Optional[str]
    ) -> Dict[str, Any]:
        scope = ScopeIdentity(**ids)
        return {"candidates": self.ctx_store.list_candidates(scope, status=status, limit=limit)}

    @application_op("candidate_revise")
    def revise_candidate(
        self,
        candidate_id: str,
        *,
        proposal: Dict[str, Any],
        source_refs: tuple[str, ...] | list[str],
        reason: Optional[str] = None,
        expected_version: Optional[int] = None,
        **ids: Optional[str],
    ) -> Dict[str, Any]:
        scope = ScopeIdentity(**ids)
        return self.ctx_store.revise_candidate(
            scope, candidate_id, proposal=proposal, source_refs=source_refs,
            reason=reason, expected_version=expected_version,
        )

    @application_op("candidate_decide")
    def decide_candidate(
        self,
        candidate_id: str,
        *,
        approve: bool,
        expected_version: Optional[int] = None,
        decision_reason: Optional[str] = None,
        **ids: Optional[str],
    ) -> Dict[str, Any]:
        """Approve (atomic entry creation + terminal state) or reject.
        Approved entries surface via recall immediately (stale merge) and
        project to the vector store through reconciliation."""
        scope = ScopeIdentity(**ids)
        result = self.ctx_store.decide_candidate(
            scope, candidate_id, approve=approve,
            expected_version=expected_version, decision_reason=decision_reason,
        )
        if approve and result.get("result_entry_version_id"):
            # Approved head is pending_embed=True; project now best-effort.
            for pending in self.ctx_store.iter_pending_embed(limit=10):
                if pending["entry_version_id"] == result["result_entry_version_id"]:
                    pend_scope = ScopeIdentity(
                        **{f: pending.get(f) for f in SCOPE_FIELDS if pending.get(f)}
                    )
                    self._project_to_vector_store(
                        pend_scope,
                        self.ctx_store.get_entry_version(
                            pend_scope, pending["entry_id"], pending["entry_version_id"]
                        ),
                    )
                    self.ctx_store.claim_pending(
                        pend_scope, pending["entry_id"], pending["entry_version_id"]
                    )
                    break
        return result

    # -- P2: sources & handoffs (design §7) --------------------------------------

    @application_op("capture_source")
    def capture_source(
        self,
        content: str,
        *,
        metadata: Optional[Dict[str, Any]] = None,
        source_type: str = "content",
        **ids: Optional[str],
    ) -> Dict[str, Any]:
        """Capture a raw-fact Source into the per-scope journal (design §7).
        Sources are the un-truncated evidence layer — the answer to the
        rolling-window messages table losing original facts."""
        scope = ScopeIdentity(**ids)
        return self.ctx_store.capture_source(
            scope, content=content, metadata=metadata, source_type=source_type
        )

    @application_op("handoff_prepare")
    def prepare_handoff(
        self,
        *,
        after: int = 0,
        through: Optional[int] = None,
        limit: int = 50,
        **ids: Optional[str],
    ) -> Dict[str, Any]:
        """Bound a Source window and open a handoff artifact (design §7).
        Without an LLM the draft is produced manually — the window and the
        handoff record are still created (degraded-mode contract)."""
        scope = ScopeIdentity(**ids)
        window = self.ctx_store.read_source_window(scope, after=after, through=through, limit=limit)
        effective_through = window[-1]["journal_position"] if window else self.ctx_store.journal_position(scope)
        handoff_id = self.ctx_store.create_handoff(
            scope, window_after=after, window_through=effective_through, draft="{}"
        )
        return {
            "handoff_id": handoff_id,
            "window": {"after": after, "through": effective_through, "count": len(window)},
            "sources": window,
            "draft_hint": "provide draft JSON on commit; LLM drafting requires an LLM provider",
        }

    @application_op("handoff_commit")
    def commit_handoff(
        self, handoff_id: str, *, draft: Dict[str, Any], **ids: Optional[str]
    ) -> Dict[str, Any]:
        """Commit a handoff draft with strict citation validation (design
        §7): every state/next_action statement carries 1..32 citations and
        each citation must resolve to a live, hash-verified entry version —
        the LLM may draft text but can never invent citations."""
        scope = ScopeIdentity(**ids)
        validated = self._validate_handoff_draft(scope, draft)
        self.ctx_store.commit_handoff(scope, handoff_id, draft=json.dumps(validated, ensure_ascii=False))
        return {"handoff_id": handoff_id, "state": "committed", "statements": len(validated["statements"])}

    @application_op("handoff_continue")
    def continue_handoff(self, handoff_id: str, **ids: Optional[str]) -> Dict[str, Any]:
        """Resolve a committed handoff for the next session (design §7):
        history is returned as explicitly untrusted with per-citation
        evidence checks — the anti context-poisoning guarantee."""
        scope = ScopeIdentity(**ids)
        row = self.ctx_store.get_handoff(scope, handoff_id)
        if row["state"] != "committed":
            raise ContextValidationError(f"Handoff {handoff_id} is not committed")
        draft = json.loads(row["draft"])
        evidence_checks = []
        for statement in draft.get("statements", []):
            for citation in statement.get("citations", []):
                check = {"citation": citation, "available": False, "reason": None}
                try:
                    entry = self.ctx_store.get_entry_version(
                        scope, citation["entry_id"], citation["entry_version_id"]
                    )
                    recomputed = entry_content_hash(
                        kind=entry.kind, text=entry.text,
                        source_refs=entry.source_refs, artifact_refs=entry.artifact_refs,
                        categories=entry.categories,
                    )
                    if recomputed != entry.entry_content_hash:
                        check["reason"] = "hash_mismatch"
                    else:
                        head = self.ctx_store.get_head(scope, citation["entry_id"])
                        check["available"] = head["state"] == ACTIVE
                        if not check["available"]:
                            check["reason"] = "retired"
                except EntryNotFoundError:
                    check["reason"] = "missing"
                evidence_checks.append(check)
        return {
            "handoff_id": handoff_id,
            "trust": "untrusted_history",
            "statements": draft.get("statements", []),
            "evidence_checks": evidence_checks,
        }

    def _validate_handoff_draft(self, scope: ScopeIdentity, draft: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(draft, dict) or not draft.get("statements"):
            raise ContextValidationError("draft.statements must be a non-empty list")
        artifact_id = self.ctx_store.get_artifact_id(scope)
        validated_statements = []
        for statement in draft["statements"]:
            text = (statement.get("text") or "").strip()
            if not text:
                raise ContextValidationError("every statement needs text")
            citations = statement.get("citations") or []
            if not 1 <= len(citations) <= 32:
                raise ContextValidationError(
                    f"statement citations must be 1..32, got {len(citations)}"
                )
            checked = []
            for citation in citations:
                citation = dict(citation)
                citation["artifact_id"] = artifact_id
                # Resolution + hash re-verification reuse expand's semantics.
                self.expand(self._citation_from_dict(citation), **scope.fields)
                checked.append(citation)
            validated_statements.append({"text": text, "citations": checked})
        result = dict(draft)
        result["statements"] = validated_statements
        return result

    @application_op("rebuild")
    def rebuild_projections(self, *, batch_size: int = 200) -> Dict[str, Any]:
        """Rebuild the whole vector projection from the authoritative store
        (design §3.2, P2 acceptance ④: RPO=0 — the authority is the entire
        truth). Every ACTIVE head gets a fresh vector row (old bindings are
        replaced and their stale rows deleted where the adapter supports
        it); inactive heads keep no projection. Operator-run maintenance:
        pause writers for a clean cut, then run once and finish with a
        reconcile pass for anything written during the rebuild."""
        summary = {"rebuilt": 0, "cleared": 0, "failed": 0}
        after = None
        while True:
            heads = self.ctx_store.iter_heads(state=ACTIVE, limit=batch_size, after=after)
            if not heads:
                break
            for head in heads:
                after = (head["scope_key"], head["entry_id"])
                scope = ScopeIdentity(**{f: head.get(f) for f in SCOPE_FIELDS if head.get(f)})
                try:
                    entry = self.ctx_store.get_entry_version(
                        scope, head["entry_id"], head["entry_version_id"]
                    )
                    payload = self._projection_payload(scope, entry)
                    vector = self.embedding_model.embed(entry.text, "add")
                    new_id = str(uuid.uuid4())
                    self.vector_store.insert(vectors=[vector], ids=[new_id], payloads=[payload])
                    old_id = head.get("vector_id")
                    self.ctx_store.bind_vector(
                        scope, head["entry_id"],
                        vector_id=new_id, pending_embed=False,
                        expected_entry_version_id=head["entry_version_id"],
                    )
                    if old_id and old_id != new_id:
                        try:
                            self.vector_store.delete(old_id)
                        except Exception:
                            logger.debug("stale projection row %s left behind", old_id)
                    summary["rebuilt"] += 1
                except Exception:
                    summary["failed"] += 1
                    logger.warning("rebuild failed for entry %s", head["entry_id"], exc_info=True)
        summary["scanned_until"] = after
        return summary

    @application_op("reconcile")
    def reconcile_projections(self, *, limit: int = 100) -> Dict[str, Any]:
        """Drain pending projections (design §5.2, multi-worker safe):

        - claim: conditional UPDATE flips ``pending_embed`` — exactly one
          worker wins per entry (same CAS discipline as every write);
        - project: entries with an existing ``vector_id`` are updated
          IN PLACE (no duplicate vectors can accumulate across runs);
        - release: on failure the claim is released so a later pass
          retries; other entries are unaffected.
        """
        summary = {"claimed": 0, "projected": 0, "skipped": 0, "released": 0}
        for head in self.ctx_store.iter_pending_embed(limit=limit):
            scope = ScopeIdentity(
                **{f: head.get(f) for f in SCOPE_FIELDS if head.get(f)}
            )
            if not self.ctx_store.claim_pending(scope, head["entry_id"], head["entry_version_id"]):
                summary["skipped"] += 1
                continue
            summary["claimed"] += 1
            try:
                entry = self.ctx_store.get_entry_version(
                    scope, head["entry_id"], head["entry_version_id"]
                )
                payload = self._projection_payload(scope, entry)
                vector = self.embedding_model.embed(entry.text, "add")
                if head.get("vector_id"):
                    self.vector_store.update(head["vector_id"], vector=vector, payload=payload)
                    vector_id = head["vector_id"]
                else:
                    vector_id = str(uuid.uuid4())
                    self.vector_store.insert(vectors=[vector], ids=[vector_id], payloads=[payload])
                    self.db.add_history(
                        vector_id, None, entry.text, "ADD",
                        created_at=payload["created_at"], updated_at=payload["updated_at"],
                    )
                self.ctx_store.bind_vector(
                    scope, entry.entry_id,
                    vector_id=vector_id, pending_embed=False,
                    expected_entry_version_id=entry.entry_version_id,
                )
                summary["projected"] += 1
            except Exception:
                self.ctx_store.bind_vector(
                    scope, head["entry_id"],
                    vector_id=head.get("vector_id"), pending_embed=True,
                    expected_entry_version_id=head["entry_version_id"],
                )
                summary["released"] += 1
                logger.warning(
                    "Reconciliation released entry %s for retry", head["entry_id"], exc_info=True
                )
        return summary

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

    def _projection_payload(self, scope: ScopeIdentity, entry: EntryVersionView) -> Dict[str, Any]:
        """Single source of the vector projection document — shared by the
        write path and reconciliation so both produce identical payloads."""
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
        return payload

    def _project_to_vector_store(self, scope: ScopeIdentity, entry: EntryVersionView) -> bool:
        """Best-effort projection after the authoritative commit (design D3).
        Any failure — embedder absent, embedder transiently down, vector
        store down — leaves the authoritative fact durable with
        ``pending_embed`` set for reconciliation; the request itself never
        fails on projection problems."""
        payload = self._projection_payload(scope, entry)

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
        filters see it immediately (double insurance, design §5.1).

        VectorStoreBase.update REPLACES the payload (pgvector and ES both
        do), so the merged full payload is written back — flipping only
        ``state`` would wipe every other field on every backend."""
        head = self.ctx_store.get_head(scope, entry_id)
        vector_id = head.get("vector_id")
        if not vector_id:
            return
        try:
            current = self.vector_store.get(vector_id)
            payload = dict(getattr(current, "payload", None) or {})
            payload["state"] = state
            self.vector_store.update(vector_id, payload=payload)
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
