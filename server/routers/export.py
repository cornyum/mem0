import csv
import io
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from auth import require_admin
from errors import upstream_error
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from serialize import serialize_memory as _serialize_memory
from server_state import get_memory_instance

router = APIRouter(prefix="/export", tags=["export"])

EXPORT_BATCH_SIZE = 500
MAX_EXPORT_BATCHES = 200  # 200 batches x 500 rows = 100k memories hard cap.

CSV_COLUMNS = [
    "id",
    "memory",
    "categories",
    "user_id",
    "agent_id",
    "run_id",
    "tenant_id",
    "session_id",
    "created_at",
    "updated_at",
]


def _unpack_rows(results: Any) -> List[Any]:
    # vector_store.list() may return [rows] (nested) or the rows directly.
    if results and isinstance(results, list) and isinstance(results[0], list):
        return results[0]
    return results or []


def _list_batch(vector_store, filters: Dict[str, str], cursor: Optional[str], use_cursor: bool):
    kwargs: Dict[str, Any] = {"top_k": EXPORT_BATCH_SIZE}
    if filters:
        kwargs["filters"] = filters
    if use_cursor and cursor is not None:
        kwargs["after_id"] = cursor
    try:
        return vector_store.list(**kwargs), True
    except TypeError:
        # Vector store without keyset-pagination support (after_id kwarg):
        # fall back to a single-batch export and report truncation.
        kwargs.pop("after_id", None)
        return vector_store.list(**kwargs), False


def _collect_rows(filters: Dict[str, str]) -> Tuple[List[Any], bool]:
    """Collect export rows; returns (rows, truncated).

    Keyset pagination via `after_id` when the store supports it (pgvector):
    batches advance by id so every row is reachable. Stores without cursor
    support can only ever return the first batch — a full batch there means
    the export may be incomplete, which is signalled via truncated=True.
    """
    vector_store = get_memory_instance().vector_store
    collected: List[Any] = []
    seen_ids: set = set()
    cursor: Optional[str] = None
    use_cursor = True
    truncated = False

    for batch_no in range(MAX_EXPORT_BATCHES):
        results, use_cursor = _list_batch(vector_store, filters, cursor, use_cursor)
        rows = _unpack_rows(results)
        if not rows:
            return collected, truncated

        new_rows = [row for row in rows if getattr(row, "id", None) not in seen_ids]
        for row in new_rows:
            seen_ids.add(getattr(row, "id", None))
        collected.extend(new_rows)

        if use_cursor:
            if len(rows) < EXPORT_BATCH_SIZE:
                return collected, truncated  # short batch = final page
            cursor = str(rows[-1].id)
        else:
            # No pagination support: everything beyond this batch is unreachable.
            truncated = len(rows) >= EXPORT_BATCH_SIZE
            return collected, truncated

    # Batch budget exhausted before the store ran out of rows.
    return collected, True


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if text.startswith(("=", "+", "-", "@", "\t")):
        return "'" + text
    return text


def _build_csv(memories: List[Dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for mem in memories:
        categories = (mem.get("metadata") or {}).get("categories")
        if not isinstance(categories, list):
            categories = []
        writer.writerow(
            [
                _csv_cell(mem.get("id")),
                _csv_cell(mem.get("memory")),
                _csv_cell(";".join(str(c) for c in categories)),
                _csv_cell(mem.get("user_id")),
                _csv_cell(mem.get("agent_id")),
                _csv_cell(mem.get("run_id")),
                _csv_cell(mem.get("tenant_id")),
                _csv_cell(mem.get("session_id")),
                _csv_cell(mem.get("created_at")),
                _csv_cell(mem.get("updated_at")),
            ]
        )
    return "\ufeff" + buffer.getvalue()


@router.get("", summary="Export memories")
def export_memories(
    format: str = Query("json", description="Export format: json or csv."),
    mode: str = Query("projection", description="projection (vector rows) or authoritative (full revision history + lineage + sources)."),
    user_id: Optional[str] = Query(None, description="Filter by user_id."),
    agent_id: Optional[str] = Query(None, description="Filter by agent_id."),
    run_id: Optional[str] = Query(None, description="Filter by run_id."),
    tenant_id: Optional[str] = Query(None, description="Filter by tenant_id."),
    session_id: Optional[str] = Query(None, description="Filter by session_id."),
    category: Optional[str] = Query(None, description="Filter by memory category (exact match)."),
    _auth=Depends(require_admin),
):
    """Export memories as a downloadable JSON or CSV file (admin only).

    mode=authoritative streams the ctx store itself — every immutable
    version with its citation triple and lineage edges — the audit-grade
    export (design §9); CSV is projection-only by construction.
    """
    fmt = (format or "json").lower()
    if fmt not in {"json", "csv"}:
        raise HTTPException(status_code=400, detail="Invalid format. Supported formats: json, csv.")
    export_mode = (mode or "projection").lower()
    if export_mode not in {"projection", "authoritative"}:
        raise HTTPException(status_code=400, detail="Invalid mode. Supported modes: projection, authoritative.")
    if export_mode == "authoritative" and fmt != "json":
        raise HTTPException(status_code=400, detail="authoritative export is JSON only.")

    scope_filters = {
        k: v
        for k, v in {
            "user_id": user_id,
            "agent_id": agent_id,
            "run_id": run_id,
            "tenant_id": tenant_id,
            "session_id": session_id,
        }.items()
        if v
    }

    if export_mode == "authoritative":
        return _export_authoritative(scope_filters)

    meta_filters = {**scope_filters, **({"category": category} if category else {})}

    try:
        rows, truncated = _collect_rows(scope_filters)
        if category:
            rows = [
                row for row in rows if category in ((getattr(row, "payload", None) or {}).get("categories") or [])
            ]
        memories = [_serialize_memory(row) for row in rows]
    except HTTPException:
        raise
    except Exception:
        raise upstream_error()

    exported_at = datetime.now(timezone.utc)
    stamp = exported_at.strftime("%Y%m%d-%H%M%S")
    ext = "csv" if fmt == "csv" else "json"
    headers = {"Content-Disposition": f'attachment; filename="agentar-memories-{stamp}.{ext}"'}

    if fmt == "csv":
        if truncated:
            headers["X-Agentar-Truncated"] = "true"
        return Response(
            content=_build_csv(memories),
            media_type="text/csv; charset=utf-8",
            headers=headers,
        )

    content = json.dumps(
        {
            "meta": {
                "exported_at": exported_at.isoformat(),
                "filters": meta_filters,
                "total": len(memories),
                "truncated": truncated,
            },
            "memories": memories,
        },
        ensure_ascii=False,
        default=str,
    )
    if truncated:
        headers["X-Agentar-Truncated"] = "true"
    return Response(
        content=content,
        media_type="application/json",
        headers=headers,
    )


def _export_authoritative(scope_filters: dict) -> Response:
    """Audit-grade export (design §9): every immutable entry version with
    its citation triple, head state, and lineage edges, plus the source
    journal — straight from the ctx store via keyset pagination."""
    import json as _json
    from datetime import datetime, timezone

    from context_runtime import get_context_store
    from mem0.context.scope import ScopeIdentity, SCOPE_FIELDS

    store = get_context_store()
    scope = ScopeIdentity(**{f: scope_filters.get(f) for f in SCOPE_FIELDS if scope_filters.get(f)}) if scope_filters else None

    entries = []
    after = None
    while True:
        filter_kwargs = {"state": None}
        heads = store.iter_heads(limit=200, after=after, **filter_kwargs)
        if not heads:
            break
        for head in heads:
            if scope is not None:
                head_ids = {f: head.get(f) for f in SCOPE_FIELDS if head.get(f)}
                if not all(head_ids.get(f) == v for f, v in scope.fields.items()):
                    after = (head["scope_key"], head["entry_id"])
                    continue
            after = (head["scope_key"], head["entry_id"])
            head_scope = ScopeIdentity(**{f: head.get(f) for f in SCOPE_FIELDS if head.get(f)})
            lineage = store.lineage_for_entry(head_scope, head["entry_id"])
            entries.append(
                {
                    "scope": {f: head.get(f) for f in SCOPE_FIELDS if head.get(f)},
                    "artifact_id": head["artifact_id"],
                    "entry_id": head["entry_id"],
                    "head": {
                        "entry_version_id": head["entry_version_id"],
                        "state": head["state"],
                        "entry_content_hash": head["entry_content_hash"],
                        "head_revision": head["head_revision"],
                    },
                    "lineage": lineage,
                }
            )

    sources = []
    if scope is not None:
        sources = store.read_source_window(scope, after=0, limit=100000)

    payload = {
        "schema": "agentar.authoritative-export.v1",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "entries": entries,
        "sources": sources,
    }
    stamp = payload["exported_at"].replace(":", "").replace("-", "")[:15]
    return Response(
        content=_json.dumps(payload, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="agentar-authoritative-{stamp}.json"'},
    )
