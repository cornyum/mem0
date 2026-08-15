"""RecallCoordinator: channel-transparent retrieval over the ES head index
(design §6).

Channels: ES kNN (semantic) and ES BM25 (keyword — text_zh takes the original
query, searchable_text takes lemmatized tokens). ``auto`` unions both and
fuses with RRF (k=60, equal weights); a failed channel degrades to the other.
``semantic`` never falls back; ES runtime failures are 503 in ONLY_VDB and —
for auto/keyword only — the HYBRID SQL FTS sidecar when the ES error class is
UNAVAILABLE/TIMEOUT/THROTTLED. Normal empty results never trigger fallback.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from mem0.context.errors import CapabilityNotSupportedError, ContextValidationError
from mem0.context.vdb.errors import (
    FALLBACK_ELIGIBLE_CLASSES,
    PrimaryUnavailableError,
    classify_elasticsearch_error,
)
from mem0.utils.entity_extraction import extract_entities
from mem0.utils.lemmatization import lemmatize_for_bm25

logger = logging.getLogger(__name__)

MODES = ("auto", "semantic", "keyword")
RRF_K = 60
ES_UNKNOWN_LABEL = "UNKNOWN"


def _error_class(exc: Optional[Exception]) -> str:
    """Classify a channel failure, unwrapping the store's PrimaryUnavailableError
    wrapper so its original ES class (§7.5) survives for fallback decisions."""
    if exc is None:
        return ES_UNKNOWN_LABEL
    if isinstance(exc, PrimaryUnavailableError) and exc.error_class:
        return exc.error_class
    return classify_elasticsearch_error(exc)

MATCHED_SEMANTIC = "semantic"
MATCHED_KEYWORD = "keyword"
MATCHED_ENTITY = "entity"
MATCHED_RERANK = "rerank"
MATCHED_FTS = "fts_sidecar"

RERANK_APPLIED = "applied"
RERANK_FALLBACK = "fallback"
RERANK_OFF = "off"


def candidate_limit(limit: int) -> int:
    """Per-channel over-fetch window (design §6.2): min(max(limit*4, 50), 200)."""
    return min(max(limit * 4, 50), 200)


@dataclass
class RecallCandidate:
    entry_id: str
    doc: Dict[str, Any]
    matched_by: List[str] = field(default_factory=list)
    semantic_score: Optional[float] = None
    rrf_score: float = 0.0


class RecallCoordinator:
    def __init__(
        self,
        store,
        *,
        embedder=None,
        reranker=None,
        fts_fallback: Optional[Callable[..., Any]] = None,
    ):
        self.store = store
        self.embedder = embedder
        self.reranker = reranker
        self.fts_fallback = fts_fallback  # HYBRID sidecar hook (design §6.3)

    def recall(
        self,
        query: str,
        *,
        identity_filters: Dict[str, str],
        limit: int = 10,
        mode: str = "auto",
        threshold: Optional[float] = None,
        rerank: bool = False,
    ) -> Dict[str, Any]:
        if mode not in MODES:
            raise ContextValidationError(f"mode must be one of {MODES}, got {mode!r}")
        if not (query or "").strip():
            raise ContextValidationError("query must be a non-empty string")
        if mode == "keyword" and threshold is not None:
            raise ContextValidationError("threshold is not supported with mode=keyword")

        per_channel = candidate_limit(limit)
        lemmatized = lemmatize_for_bm25(query)

        semantic_hits: List[Dict[str, Any]] = []
        keyword_hits: List[Dict[str, Any]] = []
        degraded_channels: List[str] = []
        es_failures: List[Exception] = []

        # -- semantic channel --------------------------------------------------
        semantic_wanted = mode != "keyword"
        semantic_ran = False
        if semantic_wanted:
            if self.embedder is None:
                if mode == "semantic":
                    raise CapabilityNotSupportedError("semantic")
                degraded_channels.append("semantic")
            else:
                try:
                    vector = self.embedder.embed(query, "search")
                    semantic_hits = self.store.knn_search(
                        vector, identity_filters=identity_filters, limit=per_channel
                    )
                    semantic_ran = True
                except CapabilityNotSupportedError:
                    if mode == "semantic":
                        raise
                    degraded_channels.append("semantic")
                except Exception as exc:
                    if mode == "semantic":
                        raise  # semantic never falls back (design §6.1)
                    es_failures.append(exc)
                    degraded_channels.append("semantic")

        # -- keyword channel ---------------------------------------------------
        keyword_wanted = mode != "semantic"
        keyword_ran = False
        if keyword_wanted and (mode == "keyword" or semantic_ran or "semantic" in degraded_channels):
            # Run the keyword channel whenever it is the requested mode or the
            # semantic channel is missing (auto must union; a dead semantic
            # channel degrades to keyword-only rather than failing, §6.1).
            try:
                keyword_hits = self.store.keyword_search(
                    query, lemmatized, identity_filters=identity_filters, limit=per_channel
                )
                keyword_ran = True
            except Exception as exc:
                es_failures.append(exc)
                degraded_channels.append("keyword")

        # -- availability contract (§6.1 / acceptance §12.5-6) ------------------
        if not semantic_ran and not keyword_ran:
            # Fallback eligibility requires EVERY observed failure to be an
            # outage class — one 400/401-class failure means a config error the
            # sidecar must not paper over (design §6.3, review #11)
            classes = [_error_class(exc) for exc in es_failures] or [ES_UNKNOWN_LABEL]
            all_eligible = bool(es_failures) and all(c in FALLBACK_ELIGIBLE_CLASSES for c in classes)
            if self.fts_fallback is not None and all_eligible:
                return self._sql_fts_fallback(
                    query, identity_filters=identity_filters, limit=limit
                )
            cls = classes[0] if len(classes) == 1 else ",".join(classes)
            raise PrimaryUnavailableError(
                f"No recall channel available (es_error={cls})", error_class=classes[0]
            )

        if mode == "semantic":
            search_mode = "semantic"
        elif semantic_ran and keyword_ran:
            search_mode = "hybrid"
        elif semantic_ran:
            search_mode = "semantic"
        else:
            search_mode = "keyword"

        # -- union + RRF (§6.2) --------------------------------------------------
        candidates: Dict[str, RecallCandidate] = {}
        if semantic_hits:
            rank = 0
            for hit in semantic_hits:
                doc = hit["doc"]
                if threshold is not None and (hit.get("score") or 0.0) < threshold:
                    continue  # threshold gates the semantic channel's raw score only
                rank += 1
                entry_id = doc["entry_id"]
                cand = candidates.setdefault(
                    entry_id, RecallCandidate(entry_id=entry_id, doc=doc)
                )
                cand.matched_by.append(MATCHED_SEMANTIC)
                cand.semantic_score = hit.get("score")
                cand.rrf_score += 1.0 / (RRF_K + rank)
        if keyword_hits:
            rank = 0
            for hit in keyword_hits:
                rank += 1
                doc = hit["doc"]
                entry_id = doc["entry_id"]
                cand = candidates.setdefault(
                    entry_id, RecallCandidate(entry_id=entry_id, doc=doc)
                )
                if MATCHED_KEYWORD not in cand.matched_by:
                    cand.matched_by.append(MATCHED_KEYWORD)
                    cand.rrf_score += 1.0 / (RRF_K + rank)

        if not candidates:
            return self._envelope([], search_mode, degraded_channels, RERANK_OFF)

        ranked = sorted(candidates.values(), key=lambda c: c.rrf_score, reverse=True)

        # -- entity marking (§6.2): annotate existing candidates only ----------
        entities = extract_entities(query)
        if entities:
            lowered = {e.lower() for e in entities}
            for cand in ranked:
                text = (cand.doc.get("text") or "").lower()
                if any(ent in text for ent in lowered):
                    cand.matched_by.append(MATCHED_ENTITY)

        # -- rerank (§6.2): order-only, failure keeps RRF order -----------------
        rerank_status = RERANK_OFF
        if rerank and self.reranker is not None:
            rerank_status = self._apply_rerank(query, ranked, limit)

        results = [
            self._format_hit(cand, search_mode) for cand in ranked[:limit]
        ]
        return self._envelope(results, search_mode, degraded_channels, rerank_status)

    # -- internals ---------------------------------------------------------------

    def _apply_rerank(self, query: str, ranked: List[RecallCandidate], limit: int) -> str:
        try:
            documents = [
                {"memory": cand.doc.get("text") or "", "entry_id": cand.entry_id}
                for cand in ranked
            ]
            reranked = self.reranker.rerank(query, documents, limit)
            if not reranked:
                return RERANK_FALLBACK
            order = {}
            for position, doc in enumerate(reranked):
                entry_id = doc.get("entry_id") or doc.get("id")
                if entry_id:
                    order[entry_id] = position
            if not order:
                return RERANK_FALLBACK

            def sort_key(cand: RecallCandidate):
                return order.get(cand.entry_id, len(order) + 1)

            ranked.sort(key=sort_key)
            reranked_ids = set(order)
            for cand in ranked:
                if cand.entry_id in reranked_ids:
                    if MATCHED_RERANK not in cand.matched_by:
                        cand.matched_by.append(MATCHED_RERANK)
            return RERANK_APPLIED
        except Exception as exc:
            logger.warning("Rerank failed; keeping RRF order (%s)", exc)
            return RERANK_FALLBACK

    def _sql_fts_fallback(
        self, query: str, *, identity_filters: Dict[str, str], limit: int
    ) -> Dict[str, Any]:
        try:
            hits, as_of_revision = self.fts_fallback(
                query, identity_filters=identity_filters, limit=limit
            )
        except Exception as exc:
            raise PrimaryUnavailableError(
                f"SQL FTS fallback failed: {exc}", error_class=classify_elasticsearch_error(exc)
            ) from exc
        return {
            "results": hits,
            "search_mode": "fts_sidecar",
            "storage_source": "sql_fts",
            "degraded": True,
            "degraded_channels": ["semantic", "keyword"],
            "rerank_status": RERANK_OFF,
            "as_of_revision": as_of_revision,
        }

    @staticmethod
    def _envelope(results, search_mode, degraded_channels, rerank_status) -> Dict[str, Any]:
        envelope: Dict[str, Any] = {
            "results": results,
            "search_mode": search_mode,
            "storage_source": "elasticsearch",
            "degraded": bool(degraded_channels),
            "rerank_status": rerank_status,
        }
        if degraded_channels:
            envelope["degraded_channels"] = degraded_channels
        return envelope

    @staticmethod
    def _format_hit(cand: RecallCandidate, search_mode: str) -> Dict[str, Any]:
        doc = cand.doc
        hit = {
            "entry_id": cand.entry_id,
            "entry_version_id": doc.get("entry_version_id"),
            "version": doc.get("version"),
            "kind": doc.get("kind"),
            "text": doc.get("text"),
            "score": round(cand.rrf_score, 6),
            "matched_by": list(dict.fromkeys(cand.matched_by)),
            "categories": doc.get("categories") or [],
            "source_refs": doc.get("source_refs") or [],
            "artifact_refs": doc.get("artifact_refs") or [],
            "scope_key": doc.get("scope_key"),
            "created_at": doc.get("created_at"),
            "updated_at": doc.get("updated_at"),
        }
        if cand.semantic_score is not None:
            hit["semantic_score"] = cand.semantic_score
        for key in ("tenant_id", "user_id", "agent_id", "run_id", "session_id"):
            if doc.get(key):
                hit[key] = doc[key]
        if isinstance(doc.get("metadata"), dict):
            hit["metadata"] = doc["metadata"]
        return hit
