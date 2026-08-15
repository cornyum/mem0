"""SQL ctx → ES authority migration (design §9).

Reads the legacy ``ctx_*`` family (bindings / entry_versions / entry_heads /
memory_heads) plus the legacy vector projection index and rebuilds the five
ES document families. The old tables are never modified; everything lands in
the NEW authority indices (``{prefix}_scope_v1`` etc., distinct names from
the legacy single collection).

Order per §9.2: scope → version → head → revision-events → dedup. Legacy
vector rows that never bound to ctx synthesize ``provenance=legacy_backfill``
entries. Verification (§9.4): scope/head/version/active-dedup counts plus
sampled hash re-verification, run after the copy in both modes.

CLI:
    python server/scripts/migrate_ctx_to_es.py --dry-run   # report only
    python server/scripts/migrate_ctx_to_es.py             # migrate + verify
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("migrate_ctx_to_es")

CTX_PREFIX = "agentar_mem_ctx_"
IDENTITY = ("tenant_id", "user_id", "agent_id", "run_id", "session_id")


def _iso(value) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()
    return str(value)


def migrate_ctx_to_es(
    store,
    *,
    engine=None,
    ctx_prefix: str = CTX_PREFIX,
    legacy_collection: Optional[str] = "agentar_mem0",
    dry_run: bool = True,
    sample_size: int = 10,
) -> Dict[str, Any]:
    """One-shot migration + verification. ``store`` is the v3
    ElasticsearchMemoryStore; ``engine`` points at the legacy ctx DB."""
    from sqlalchemy import text

    if engine is None:
        import db as app_db

        engine = app_db.engine

    t_bind = f"{ctx_prefix}memory_bindings"
    t_versions = f"{ctx_prefix}entry_versions"
    t_heads = f"{ctx_prefix}entry_heads"
    t_memory = f"{ctx_prefix}memory_heads"

    with engine.connect() as conn:
        try:
            bindings = conn.execute(text(f"SELECT * FROM {t_bind}")).mappings().all()
            versions = conn.execute(text(f"SELECT * FROM {t_versions}")).mappings().all()
            heads = conn.execute(text(f"SELECT * FROM {t_heads}")).mappings().all()
            memory_heads = conn.execute(text(f"SELECT * FROM {t_memory}")).mappings().all()
        except Exception as exc:
            return {"status": "no_legacy_schema", "error": str(exc)}

    revision_by_scope = {row["scope_key"]: int(row["revision"]) for row in memory_heads}
    bound_vector_ids = {row["vector_id"] for row in heads if row.get("vector_id")}

    counts = {"scopes": 0, "versions": 0, "heads": 0, "events": 0, "dedup_active": 0, "legacy_backfill": 0}
    if dry_run:
        counts["scopes"] = len(bindings)
        counts["versions"] = len(versions)
        counts["heads"] = len(heads)
        counts["dedup_active"] = sum(1 for h in heads if h["state"] == "active")
        logger.info("dry-run: would migrate %s", counts)
        return {"status": "dry_run", "counts": counts}

    from mem0.context.vdb.write import compute_dedup_key
    from mem0.utils.lemmatization import lemmatize_for_bm25

    versions_by_scope: Dict[str, List[dict]] = {}
    for row in versions:
        versions_by_scope.setdefault(row["scope_key"], []).append(dict(row))
    heads_by_scope: Dict[str, List[dict]] = {}
    for row in heads:
        heads_by_scope.setdefault(row["scope_key"], []).append(dict(row))

    def _legacy_vector(vector_id: Optional[str]) -> Optional[List[float]]:
        if not vector_id or not legacy_collection:
            return None
        try:
            resp = store.client.get(index=legacy_collection, id=vector_id)
            return resp["_source"].get("vector")
        except Exception:
            return None

    def _parse_list(value) -> list:
        if isinstance(value, list):
            return value
        try:
            return json.loads(value or "[]")
        except (json.JSONDecodeError, TypeError):
            return []

    for binding in bindings:
        scope_key = binding["scope_key"]
        identity = {k: binding.get(k) for k in IDENTITY if binding.get(k)}
        artifact_id = binding["artifact_id"]
        published_revision = revision_by_scope.get(scope_key, 0)

        # 1. scope doc (overwrite semantics: migration is the authority cut-over)
        store.client.index(
            index=store.alias("scope"),
            id=f"s:{scope_key}",
            routing=scope_key,
            document={
                "scope_key": scope_key,
                **identity,
                "artifact_id": artifact_id,
                "published_revision": published_revision,
                "last_event_type": "migrated",
                "created_at": _iso(binding.get("created_at")),
                "updated_at": _iso(None),
            },
        )
        counts["scopes"] += 1

        # 2. versions + events
        for row in sorted(versions_by_scope.get(scope_key, []), key=lambda r: int(r["created_in_revision"])):
            version_doc = {
                "scope_key": scope_key,
                **identity,
                "entry_id": row["entry_id"],
                "entry_version_id": row["entry_version_id"],
                "version": int(row["version"]),
                "kind": row["kind"],
                "content_hash": row["entry_content_hash"],
                "text": row["text"],
                "source_refs": _parse_list(row.get("source_refs")),
                "artifact_refs": _parse_list(row.get("artifact_refs")),
                "categories": _parse_list(row.get("categories")),
                "scope_revision": int(row["created_in_revision"]),
                "provenance": row.get("provenance") or "native",
                "legacy_ids": [row["entry_id"]],
                "created_at": _iso(row.get("created_at")),
            }
            store.put_version(version_doc)
            counts["versions"] += 1
            revision = int(row["created_in_revision"])
            store.put_event(
                {
                    "scope_key": scope_key,
                    "scope_revision": revision,
                    "event_type": "created" if int(row["version"]) == 1 else "revised",
                    "entry_id": row["entry_id"],
                    "entry_version_id": row["entry_version_id"],
                    "version": int(row["version"]),
                    "kind": row["kind"],
                    "provenance": row.get("provenance") or "native",
                    "content_hash": row["entry_content_hash"],
                    "state_after": "active",
                    "created_at": _iso(row.get("created_at")),
                }
            )
            counts["events"] += 1

        # 3. heads (+ terminal retire events) and dedup claims
        for row in heads_by_scope.get(scope_key, []):
            vector = _legacy_vector(row.get("vector_id"))
            head_doc = {
                "scope_key": scope_key,
                **identity,
                "entry_id": row["entry_id"],
                "entry_version_id": row["entry_version_id"],
                "version": _latest_version_number(versions_by_scope.get(scope_key, []), row["entry_id"]),
                "kind": _latest_kind(versions_by_scope.get(scope_key, []), row["entry_id"]),
                "state": row["state"],
                "content_hash": row["entry_content_hash"],
                "scope_revision": int(row["head_revision"]),
                "text": _latest_text(versions_by_scope.get(scope_key, []), row["entry_id"]) or "",
                "searchable_text": lemmatize_for_bm25(
                    _latest_text(versions_by_scope.get(scope_key, []), row["entry_id"]) or ""
                ),
                "categories": _latest_categories(versions_by_scope.get(scope_key, []), row["entry_id"]),
                "source_refs": [],
                "artifact_refs": [],
                "embedding_status": "ready" if vector is not None else "pending",
                "legacy_ids": [row["entry_id"]],
                "created_at": _iso(row.get("created_at")),
                "updated_at": _iso(row.get("updated_at")),
            }
            if vector is not None:
                head_doc["vector"] = vector
            store.put_head(head_doc)
            counts["heads"] += 1

            dedup_key = compute_dedup_key(scope_key, head_doc["kind"] or "fact", row["entry_content_hash"])
            status = "active" if row["state"] == "active" else "released"
            store.set_dedup_status(
                scope_key,
                dedup_key,
                status,
                entry_id=row["entry_id"],
                entry_version_id=row["entry_version_id"],
                scope_revision=int(row["head_revision"]),
            )
            if status == "active":
                counts["dedup_active"] += 1
            else:
                # terminal retire/reactivation state lands as an audit event
                flip_revision = int(row["head_revision"])
                if store.get_event(scope_key, flip_revision) is None:
                    store.put_event(
                        {
                            "scope_key": scope_key,
                            "scope_revision": flip_revision,
                            "event_type": "retired",
                            "entry_id": row["entry_id"],
                            "entry_version_id": row["entry_version_id"],
                            "version": head_doc["version"],
                            "kind": head_doc["kind"],
                            "provenance": "api",
                            "content_hash": row["entry_content_hash"],
                            "state_after": "inactive",
                            "reason": "migrated",
                            "created_at": _iso(row.get("updated_at")),
                        }
                    )
                    counts["events"] += 1

        store.client.indices.refresh(index=store.alias("head"))

    # 4. unbound legacy vector rows → provenance=legacy_backfill (§9.3)
    if legacy_collection:
        try:
            scanned = 0
            backfilled = 0
            failed_ids: list = []
            search_after = None
            while True:
                body = {"size": 500, "query": {"match_all": {}}, "sort": [{"_id": {"order": "asc"}}]}
                if search_after is not None:
                    body["search_after"] = [search_after]
                resp = store.client.search(index=legacy_collection, **body)
                hits = resp["hits"]["hits"]
                if not hits:
                    break
                scanned += len(hits)
                for hit in hits:
                    search_after = hit.get("sort", [hit["_id"]])[0]
                    try:
                        if _backfill_legacy_row(store, hit, bound_vector_ids, counts):
                            backfilled += 1
                    except Exception:
                        failed_ids.append(hit["_id"])
                        logger.warning("backfill row %s failed", hit["_id"], exc_info=True)
                if len(hits) < 500:
                    break
            logger.info("legacy backfill scan: %d rows scanned, %d backfilled, %d failed",
                        scanned, backfilled, len(failed_ids))
            if failed_ids:
                counts["legacy_backfill_failed"] = len(failed_ids)
        except Exception:
            logger.warning("legacy vector backfill skipped", exc_info=True)

    # 5. verification (§9.4)
    verification = verify_migration(store, engine, ctx_prefix, sample_size)
    return {"status": "migrated", "counts": counts, "verification": verification}


def _backfill_legacy_row(store, hit, bound_vector_ids, counts) -> bool:
    """Synthesize one legacy_backfill entry (§9.3). Returns True when a row
    was written; raises on per-row failure so the caller can continue."""
    from mem0.context.hashing import entry_content_hash
    from mem0.context.vdb.write import compute_dedup_key
    from mem0.utils.lemmatization import lemmatize_for_bm25

    if hit["_id"] in bound_vector_ids:
        return False
    payload = (hit["_source"] or {}).get("metadata") or {}
    text_value = payload.get("data") or (hit["_source"] or {}).get("text")
    if not text_value:
        return False
    identity = {k: payload.get(k) for k in IDENTITY if payload.get(k)}
    if not identity:
        return False
    from mem0.context.scope import ScopeIdentity

    scope = ScopeIdentity(**identity)
    scope_doc = store.get_scope(scope.scope_key)
    if scope_doc is None:
        store.create_scope(scope.scope_key, scope.fields)
        scope_doc = store.get_scope(scope.scope_key)
    revision = scope_doc.published_revision + 1
    entry_id = hit["_id"]
    entry_version_id = f"{entry_id}-v1"
    kind = payload.get("memory_type") or "fact"
    content_hash = entry_content_hash(kind=kind, text=text_value)
    store.put_version(
        {
            "scope_key": scope.scope_key,
            **scope.fields,
            "entry_id": entry_id,
            "entry_version_id": entry_version_id,
            "version": 1,
            "kind": kind,
            "content_hash": content_hash,
            "text": text_value,
            "source_refs": [],
            "artifact_refs": [],
            "categories": payload.get("categories") or [],
            "scope_revision": revision,
            "provenance": "legacy_backfill",
            "legacy_ids": [entry_id],
            "created_at": _iso(payload.get("created_at")),
        }
    )
    head_doc = {
        "scope_key": scope.scope_key,
        **scope.fields,
        "entry_id": entry_id,
        "entry_version_id": entry_version_id,
        "version": 1,
        "kind": kind,
        "state": "active",
        "content_hash": content_hash,
        "scope_revision": revision,
        "text": text_value,
        "searchable_text": lemmatize_for_bm25(text_value),
        "categories": payload.get("categories") or [],
        "source_refs": [],
        "artifact_refs": [],
        "embedding_status": "ready" if (hit["_source"] or {}).get("vector") else "pending",
        "legacy_ids": [entry_id],
        "created_at": _iso(payload.get("created_at")),
        "updated_at": _iso(payload.get("updated_at")),
    }
    if (hit["_source"] or {}).get("vector"):
        head_doc["vector"] = hit["_source"]["vector"]
    store.put_head(head_doc)
    store.put_event(
        {
            "scope_key": scope.scope_key,
            "scope_revision": revision,
            "event_type": "created",
            "entry_id": entry_id,
            "entry_version_id": entry_version_id,
            "version": 1,
            "kind": kind,
            "provenance": "legacy_backfill",
            "content_hash": content_hash,
            "state_after": "active",
            "created_at": _iso(payload.get("created_at")),
        }
    )
    # single scope fetch for a consistent CAS pair (review #20b)
    scope_doc = store.get_scope(scope.scope_key)
    store.cas_publish(
        scope.scope_key,
        if_seq_no=scope_doc.seq_no,
        if_primary_term=scope_doc.primary_term,
        published_revision=revision,
        last_event_type="created",
        last_entry_id=entry_id,
        last_entry_version_id=entry_version_id,
    )
    store.set_dedup_status(
        scope.scope_key,
        compute_dedup_key(scope.scope_key, kind, content_hash),
        "active",
        entry_id=entry_id,
        entry_version_id=entry_version_id,
        scope_revision=revision,
    )
    counts["legacy_backfill"] += 1
    counts["versions"] += 1
    counts["heads"] += 1
    counts["events"] += 1
    counts["dedup_active"] += 1
    return True


def _rows_for(rows, entry_id):
    return [r for r in rows if r["entry_id"] == entry_id]


def _latest(rows, entry_id, field, default=None):
    scoped = sorted(_rows_for(rows, entry_id), key=lambda r: int(r["version"]))
    return scoped[-1].get(field) if scoped else default


def _latest_version_number(rows, entry_id) -> int:
    return int(_latest(rows, entry_id, "version", 1) or 1)


def _latest_kind(rows, entry_id):
    return _latest(rows, entry_id, "kind", "fact") or "fact"


def _latest_text(rows, entry_id):
    return _latest(rows, entry_id, "text", "")


def _latest_categories(rows, entry_id):
    value = _latest(rows, entry_id, "categories", "[]")
    if isinstance(value, list):
        return value
    try:
        return json.loads(value or "[]")
    except (json.JSONDecodeError, TypeError):
        return []


def verify_migration(store, engine, ctx_prefix: str, sample_size: int = 10) -> Dict[str, Any]:
    """Count reconciliation + sampled hash re-verification (§9.4)."""
    from sqlalchemy import text

    report: Dict[str, Any] = {}
    scopes = store.scan_all_scopes(limit=10000)
    report["es_scopes"] = len(scopes)
    try:
        with engine.connect() as conn:
            for name, table, es_count_key in (
                ("ctx_bindings", f"{ctx_prefix}memory_bindings", "es_scopes"),
                ("ctx_heads", f"{ctx_prefix}entry_heads", "es_heads"),
                ("ctx_versions", f"{ctx_prefix}entry_versions", "es_versions"),
            ):
                count = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
                report[name] = int(count)
    except Exception as exc:
        report["legacy_counts_error"] = str(exc)

    # refresh every index first: verification must read published state, not
    # pre-refresh segments; and count with track_total_hits (ES caps at 10k)
    for family in ("scope", "head", "version", "event", "dedup"):
        try:
            store.client.indices.refresh(index=store.alias(family))
        except Exception:
            pass

    es_heads = store.list_heads(limit=10000, active_only=False)
    report["es_heads"] = len(es_heads)
    resp = store._search(
        "version", {"size": 0, "track_total_hits": True, "query": {"match_all": {}}}
    )
    report["es_versions"] = int(resp["hits"]["total"]["value"])
    resp = store._search(
        "dedup", {"size": 0, "track_total_hits": True, "query": {"term": {"status": "active"}}}
    )
    report["es_dedup_active"] = int(resp["hits"]["total"]["value"])

    # The authority may legitimately hold MORE than the legacy store (live
    # traffic after cutover, or a re-run migration); it must never hold LESS —
    # a shortfall means rows were dropped (§9.4).
    count_mismatches = []
    if "ctx_bindings" in report and report["es_scopes"] < report["ctx_bindings"]:
        count_mismatches.append(
            f"scopes: es={report['es_scopes']} < ctx={report['ctx_bindings']}"
        )
    if "ctx_heads" in report and report["es_heads"] < report["ctx_heads"]:
        count_mismatches.append(f"heads: es={report['es_heads']} < ctx={report['ctx_heads']}")
    if "ctx_versions" in report and report["es_versions"] < report["ctx_versions"]:
        count_mismatches.append(
            f"versions: es={report['es_versions']} < ctx={report['ctx_versions']}"
        )
    report["count_mismatches"] = count_mismatches

    mismatches = []
    from mem0.context.hashing import entry_content_hash

    for head in es_heads[:sample_size]:
        version_doc = store.get_version(
            head["scope_key"], head["entry_id"], int(head.get("version") or 1)
        )
        if version_doc is None:
            mismatches.append({"entry_id": head["entry_id"], "reason": "version_missing"})
            continue
        recomputed = entry_content_hash(
            kind=version_doc["kind"],
            text=version_doc["text"],
            source_refs=version_doc.get("source_refs") or [],
            artifact_refs=version_doc.get("artifact_refs") or [],
            categories=version_doc.get("categories") or [],
        )
        if recomputed != version_doc.get("content_hash"):
            mismatches.append({"entry_id": head["entry_id"], "reason": "hash_mismatch"})
    report["sampled"] = min(sample_size, len(es_heads))
    report["sample_mismatches"] = mismatches
    report["ok"] = not mismatches and not count_mismatches
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--ctx-prefix", default=CTX_PREFIX)
    parser.add_argument("--legacy-collection", default="agentar_mem0")
    args = parser.parse_args()

    sys.path.insert(0, ".")
    import os

    import db
    from mem0.context.vdb import ElasticsearchMemoryStore, build_es_client

    client = build_es_client(
        host=os.environ.get("ES_HOST", "elasticsearch"),
        port=int(os.environ.get("ES_PORT", "9200")),
        user=os.environ.get("ES_USER"),
        password=os.environ.get("ES_PASSWORD"),
        use_ssl=(os.environ.get("ES_USE_SSL", "false").lower() == "true"),
    )
    store = ElasticsearchMemoryStore(
        client, prefix=os.environ.get("ES_COLLECTION_NAME", "agentar_mem0"), dims=1024
    )
    result = migrate_ctx_to_es(
        store,
        engine=db.engine,
        ctx_prefix=args.ctx_prefix,
        legacy_collection=args.legacy_collection,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status") in ("migrated", "dry_run") else 1


if __name__ == "__main__":
    raise SystemExit(main())
