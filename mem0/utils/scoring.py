"""
Scoring utilities for hybrid retrieval.

Provides:
- **BM25 normalization**: Sigmoid normalization of raw BM25 scores to [0, 1].
- **BM25 parameter selection**: Query-length-adaptive sigmoid parameters.
- **Additive scoring**: Combined scoring with semantic + BM25 + entity boost + recency.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def get_bm25_params(query: str, *, lemmatized: Optional[str] = None) -> tuple:
    """Get BM25 sigmoid parameters based on query length.

    Longer queries tend to have higher raw BM25 scores, so we adjust
    the sigmoid midpoint and steepness accordingly.

    Returns:
        (midpoint, steepness) for sigmoid normalization.
    """
    if lemmatized is None:
        from mem0.utils.lemmatization import lemmatize_for_bm25

        lemmatized = lemmatize_for_bm25(query)
    num_terms = len(lemmatized.split()) if lemmatized else 1

    if num_terms <= 3:
        return 5.0, 0.7
    elif num_terms <= 6:
        return 7.0, 0.6
    elif num_terms <= 9:
        return 9.0, 0.5
    elif num_terms <= 15:
        return 10.0, 0.5
    else:
        return 12.0, 0.5


def normalize_bm25(raw_score: float, midpoint: float, steepness: float) -> float:
    """Normalize BM25 score to [0, 1] using logistic sigmoid.

    Args:
        raw_score: Raw BM25 score (unbounded, typically 0-20+).
        midpoint: Score at which sigmoid outputs 0.5.
        steepness: Controls how quickly sigmoid transitions.

    Returns:
        Normalized score in range [0, 1].
    """
    return 1.0 / (1.0 + math.exp(-steepness * (raw_score - midpoint)))


ENTITY_BOOST_WEIGHT = 0.5

# Recency signal (issue #4956): ADD-only extraction lets contradictory facts
# coexist (e.g. two "current employer" memories). Semantic/BM25/entity scores
# are recency-blind, so the stale fact can outrank the fresh one. Recency is a
# deliberately mild, always-on tie-breaker — it never outweighs relevance.
RECENCY_WEIGHT = 0.25
RECENCY_HALF_LIFE_DAYS = 30.0
# Candidates with no parsable timestamp get the neutral midpoint: with the
# recency term always included in the divisor, 0.5 neither rewards nor unduly
# penalizes legacy/imported payloads that predate timestamped writes.
NEUTRAL_RECENCY = 0.5


def _parse_payload_timestamp(value: Any) -> Optional[datetime]:
    """Parse a payload timestamp into a timezone-aware datetime (UTC assumed when naive).

    Accepts ISO 8601 strings (with or without a trailing ``Z``) and datetime
    objects (some vector stores hand back raw driver values). Returns None for
    missing/unparsable values; never raises.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def recency_factor(
    payload: Optional[Dict[str, Any]], now: Optional[datetime] = None
) -> tuple:
    """Compute the recency factor in [0, 1] for a memory payload.

    Uses ``updated_at`` when present, falling back to ``created_at``. The factor
    decays exponentially with a 30-day half-life: a memory updated today scores
    ~1.0, one 30 days old ~0.5, one 90 days old ~0.125.

    Args:
        payload: Memory payload dict (or None).
        now: Reference time; naive values are treated as UTC. Defaults to the
            current UTC time.

    Returns:
        (recency, age_days). For missing/unparsable timestamps returns
        (NEUTRAL_RECENCY, None).
    """
    if not isinstance(payload, dict):
        return NEUTRAL_RECENCY, None

    timestamp = _parse_payload_timestamp(payload.get("updated_at")) or _parse_payload_timestamp(
        payload.get("created_at")
    )
    if timestamp is None:
        return NEUTRAL_RECENCY, None

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age_days = max((now - timestamp).total_seconds(), 0.0) / 86400.0
    return 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS), age_days


def score_and_rank(
    semantic_results: List[Dict[str, Any]],
    bm25_scores: Dict[str, float],
    entity_boosts: Dict[str, float],
    threshold: float,
    top_k: int,
    explain: bool = False,
) -> List[Dict[str, Any]]:
    """Score candidates additively and return top-k results.

    For each candidate:
        semantic_score is taken from the result's score field.
        combined = (semantic + bm25 + entity_boost + recency_term) / max_possible

    Threshold gates the semantic score BEFORE combining -- candidates
    below the threshold are excluded even if BM25/entity would boost them.

    The divisor adapts based on which signals are active:
        - Semantic only: max_possible = 1.25
        - Semantic + BM25: max_possible = 2.25
        - Semantic + BM25 + entity: max_possible = 2.75
        - Semantic + entity (no BM25): max_possible = 1.75

    The recency term (RECENCY_WEIGHT * recency factor) is always active because
    the scoring divisor must stay identical across candidates; payloads without
    a parsable timestamp contribute the neutral factor.

    Args:
        semantic_results: Candidate memories from vector search.
        bm25_scores: Normalized keyword scores keyed by memory ID.
        entity_boosts: Entity-link boosts keyed by memory ID.
        threshold: Minimum semantic score required before hybrid scoring.
        top_k: Maximum number of results to return.
        explain: Include score_details in each result when true.

    Returns:
        List of scored result dicts sorted by combined score descending.
    """
    has_bm25 = bool(bm25_scores)
    has_entity = bool(entity_boosts)

    max_possible = 1.0 + RECENCY_WEIGHT
    if has_bm25:
        max_possible += 1.0
    if has_entity:
        max_possible += ENTITY_BOOST_WEIGHT

    scored: List[Dict[str, Any]] = []
    # One reference clock per request keeps candidate scores mutually consistent
    # and makes explain output reproducible.
    now = datetime.now(timezone.utc)

    for result in semantic_results:
        mem_id = result.get("id")
        if mem_id is None:
            continue

        semantic_score = result.get("score") or 0.0
        if semantic_score < threshold:
            continue

        mem_id_str = str(mem_id)
        bm25_score = bm25_scores.get(mem_id_str, 0.0)
        entity_boost = entity_boosts.get(mem_id_str, 0.0)

        recency, age_days = recency_factor(result.get("payload"), now=now)
        recency_term = RECENCY_WEIGHT * recency

        raw_combined = semantic_score + bm25_score + entity_boost + recency_term
        combined = min(raw_combined / max_possible, 1.0)

        scored_result = {
            "id": mem_id_str,
            "score": combined,
            "payload": result.get("payload"),
        }
        if explain:
            score_details = {
                "semantic_score": semantic_score,
                "bm25_score": bm25_score,
                "entity_boost": entity_boost,
                "recency_score": recency,
                "recency_term": recency_term,
                "raw_score": raw_combined,
                "max_possible_score": max_possible,
                "final_score": combined,
                "threshold": threshold,
            }
            if age_days is not None:
                score_details["recency_age_days"] = age_days
            scored_result["score_details"] = score_details
        scored.append(scored_result)

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]
