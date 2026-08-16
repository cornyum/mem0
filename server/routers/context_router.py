"""Context layer REST surface (design v3 §7).

/v1/memory/remember|revise|retire|reactivate|recall|expand|changes|get,
/v1/context/prepare, /health/ready (three-state), /v1/capabilities, and the
admin namespace /v1/admin/memory/* (design §7.6.2). SQL-dependent extras
(sources/handoff/artifact-candidates) are 501 in the v3 storage modes until
they migrate (§7.6.4).

Every route delegates to server_state.get_app_service() — no route touches ES
or SQL directly (design §2 forbidden). Errors return { code, message,
request_id } with the §7.4 status codes.
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from auth import require_admin, verify_auth
from context_runtime import get_readiness
from errors import request_id_var
from mem0.context.errors import ContextError
from mem0.context.models import (
    ChangesRequest,
    ExpandRequest,
    GetRequest,
    PrepareContextRequest,
    ReactivateRequest,
    RecallRequest,
    RememberRequest,
    RetireRequest,
    ReviseRequest,
)
from mem0.context.readiness import NOT_READY
from server_state import get_app_service

logger = logging.getLogger(__name__)

_ERROR_CODES = {
    "ContextValidationError": ("validation_error", 422),
    "EntryNotActiveError": ("entry_not_active", 409),
    "CapabilityNotSupportedError": ("capability_not_supported", 501),
    "RevisionConflictError": ("revision_conflict", 409),
    "EvidenceExpiredError": ("evidence_expired", 410),
    "EntryNotFoundError": ("entry_not_found", 404),
    "OperationInProgressError": ("operation_in_progress", 409),
    "DedupConflictError": ("dedup_conflict", 409),
    "PrimaryUnavailableError": ("primary_unavailable", 503),
    "PrimaryConflictError": ("primary_conflict", 503),
    "PublishedRepairPendingError": ("memory_published_repair_pending", 503),
}


class _ContextRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def context_aware_handler(request):
            try:
                return await original(request)
            except ContextError as exc:
                code, status = _ERROR_CODES.get(type(exc).__name__, (None, exc.default_status_code))
                rid = request_id_var.get("")
                return JSONResponse(
                    status_code=status,
                    content={"code": code or "context_error", "message": str(exc), "request_id": rid},
                )

        return context_aware_handler


router = APIRouter(route_class=_ContextRoute)


def _sql_feature_unavailable(feature: str):
    from mem0.context.errors import CapabilityNotSupportedError

    raise CapabilityNotSupportedError(
        f"{feature} depends on the legacy SQL store and is unavailable in the v3 storage modes; "
        "it returns after its ES migration (design §7.6.4)"
    )


@router.post("/v1/memory/remember")
def remember(req: RememberRequest, _auth=Depends(verify_auth)):
    service = get_app_service()
    return service.remember(
        req.text,
        messages=req.messages,
        mode=req.mode,
        kind=req.kind,
        categories=req.categories,
        source_refs=req.source_refs,
        artifact_refs=req.artifact_refs,
        metadata=req.metadata,
        expires_at=req.expires_at,
        prompt=req.prompt,
        timestamp=req.timestamp,
        timezone=req.timezone,
        expected_revision=req.expected_revision,
        **req.identity_kwargs(),
    )


@router.post("/v1/memory/revise")
def revise(req: ReviseRequest, _auth=Depends(verify_auth)):
    """Explicit revision of one entry (design §7.6.1); Legacy PUT
    /memories/{id} maps onto this same command."""
    service = get_app_service()
    result = service.revise(
        req.entry_id,
        text=req.text,
        kind=req.kind,
        categories=req.categories,
        source_refs=req.source_refs,
        artifact_refs=req.artifact_refs,
        metadata=req.metadata,
        expires_at=req.expires_at,
        expected_revision=req.expected_revision,
        **req.identity_kwargs(),
    )
    return result.model_dump(mode="json")


@router.post("/v1/memory/retire")
def retire(req: RetireRequest, _auth=Depends(verify_auth)):
    service = get_app_service()
    result = service.retire(
        req.entry_id,
        reason=req.reason,
        expected_revision=req.expected_revision,
        **req.identity_kwargs(),
    )
    return result.model_dump(mode="json")


@router.post("/v1/memory/reactivate")
def reactivate(req: ReactivateRequest, _auth=Depends(verify_auth)):
    service = get_app_service()
    result = service.reactivate(
        req.entry_id,
        reason=req.reason,
        expected_revision=req.expected_revision,
        **req.identity_kwargs(),
    )
    return result.model_dump(mode="json")


@router.post("/v1/memory/recall")
def recall(req: RecallRequest, _auth=Depends(verify_auth)):
    service = get_app_service()
    kwargs = {}
    if req.threshold is not None:
        kwargs["threshold"] = req.threshold
    return service.recall(
        req.query,
        limit=req.limit,
        mode=req.mode,
        rerank=req.rerank,
        **kwargs,
        **req.identity_kwargs(),
    )


@router.post("/v1/memory/expand")
def expand(req: ExpandRequest, _auth=Depends(verify_auth)):
    service = get_app_service()
    body = service.expand(req.citation, **req.identity_kwargs())
    return body.model_dump(mode="json")


@router.post("/v1/memory/changes")
def changes(req: ChangesRequest, _auth=Depends(verify_auth)):
    service = get_app_service()
    records = service.changes(
        since_revision=req.since_revision,
        limit=req.limit,
        cursor=req.cursor,
        **req.identity_kwargs(),
    )
    return {"changes": [r.model_dump(mode="json") for r in records]}


@router.post("/v1/memory/get")
def get(req: GetRequest, _auth=Depends(verify_auth)):
    """Point read of the current head by entry_id (design §7.6.1)."""
    service = get_app_service()
    return service.get(req.entry_id, **req.identity_kwargs())


@router.post("/v1/context/prepare")
def prepare_context(req: PrepareContextRequest, _auth=Depends(verify_auth)):
    """Deterministic, byte-budgeted prompt assembly with the trust
    envelope (design §6.4)."""
    service = get_app_service()
    return service.prepare_context(
        req.query,
        budget_bytes=req.budget_bytes,
        mode=req.mode,
        **req.identity_kwargs(),
    )


# -- SQL-dependent extras: 501 until migrated (design §7.6.4) -----------------


@router.post("/v1/sources/content")
def capture_source( _auth=Depends(verify_auth)):
    _sql_feature_unavailable("sources")


@router.post("/v1/handoff/prepare")
def handoff_prepare( _auth=Depends(verify_auth)):
    _sql_feature_unavailable("handoff")


@router.post("/v1/handoff/commit")
def handoff_commit( _auth=Depends(verify_auth)):
    _sql_feature_unavailable("handoff")


@router.post("/v1/handoff/continue")
def handoff_continue( _auth=Depends(verify_auth)):
    _sql_feature_unavailable("handoff")


@router.post("/v1/artifact-candidates/propose")
def candidate_propose( _auth=Depends(verify_auth)):
    _sql_feature_unavailable("artifact-candidates")


@router.post("/v1/artifact-candidates/list")
def candidate_list( _auth=Depends(verify_auth)):
    _sql_feature_unavailable("artifact-candidates")


@router.post("/v1/artifact-candidates/revise")
def candidate_revise( _auth=Depends(verify_auth)):
    _sql_feature_unavailable("artifact-candidates")


@router.post("/v1/artifact-candidates/approve")
def candidate_approve( _admin=Depends(require_admin)):
    _sql_feature_unavailable("artifact-candidates")


@router.post("/v1/artifact-candidates/reject")
def candidate_reject( _admin=Depends(require_admin)):
    _sql_feature_unavailable("artifact-candidates")


# -- admin namespace (design §7.6.2) -------------------------------------------


@router.post("/v1/admin/memory/reconcile")
def admin_reconcile(_admin=Depends(require_admin)):
    """Trigger RecoveryReconciler / EmbeddingReconciler (design §5.4)."""
    service = get_app_service()
    counts = service.reconcile()
    sidecar_counts = _sync_hybrid_sidecar(service)
    if sidecar_counts:
        counts["hybrid_sidecar"] = sidecar_counts
    return counts


@router.post("/v1/admin/memory/rebuild")
def admin_rebuild(_admin=Depends(require_admin)):
    """Rebuild heads from version/event (design §7.6.2)."""
    service = get_app_service()
    return service.rebuild_heads()


@router.post("/v1/admin/memory/backfill")
def admin_backfill(_admin=Depends(require_admin)):
    """Legacy vector-row import — superseded by /v1/admin/memory/migrate for
    the ES authority; kept for the one-version deprecation window."""
    _sql_feature_unavailable("backfill")


_MIGRATION_JOBS: dict = {}


@router.post("/v1/admin/memory/migrate")
def admin_migrate(background_tasks: BackgroundTasks, dry_run: bool = True, _admin=Depends(require_admin)):
    """SQL ctx → ES authority migration task (design §7.6.2/§9). Read-only
    unless dry_run=false. Runs as a background job (full table scans must not
    hold an HTTP worker); poll /v1/admin/memory/migrate?job_id=... for the
    report. The same tool is available as server/scripts/migrate_ctx_to_es.py."""
    import secrets as _secrets

    from scripts.migrate_ctx_to_es import migrate_ctx_to_es

    service = get_app_service()
    job_id = _secrets.token_hex(8)
    _MIGRATION_JOBS[job_id] = {"status": "running", "dry_run": dry_run}

    def _run():
        try:
            _MIGRATION_JOBS[job_id]["result"] = migrate_ctx_to_es(service.store, dry_run=dry_run)
            _MIGRATION_JOBS[job_id]["status"] = "done"
        except Exception as exc:
            _MIGRATION_JOBS[job_id]["status"] = "failed"
            _MIGRATION_JOBS[job_id]["error"] = str(exc)

    background_tasks.add_task(_run)
    return {"job_id": job_id, "status": "running"}


@router.get("/v1/admin/memory/migrate")
def admin_migrate_status(job_id: str, _admin=Depends(require_admin)):
    if job_id not in _MIGRATION_JOBS:
        raise HTTPException(status_code=404, detail="Unknown migration job")
    return _MIGRATION_JOBS[job_id]


def _sync_hybrid_sidecar(service):
    import context_runtime

    sidecar = context_runtime.get_hybrid_sidecar()
    if sidecar is None:
        return None
    try:
        return sidecar.sync_all(service.store)
    except Exception:
        logger.warning("Hybrid sidecar sync failed", exc_info=True)
        return {"error": "sync_failed"}


# -- deprecated legacy paths (design §7.6.2: one version, then removal) --------


def _deprecated(response: JSONResponse) -> JSONResponse:
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = "Sat, 31 Dec 2027 00:00:00 GMT"  # RFC 8594
    return response


@router.post("/v1/memory/reconcile")
def reconcile(_admin=Depends(require_admin)):
    result = admin_reconcile(_admin)
    return _deprecated(JSONResponse(result))


@router.post("/v1/memory/rebuild")
def rebuild(_admin=Depends(require_admin)):
    result = admin_rebuild(_admin)
    return _deprecated(JSONResponse(result))


@router.post("/v1/memory/backfill")
def backfill(_admin=Depends(require_admin)):
    _sql_feature_unavailable("backfill")


# -- health / metrics / capabilities ---------------------------------------------


@router.get("/health/ready")
def health_ready():
    report = get_readiness().evaluate()
    # degraded stays in rotation by design (design §10): only a blocking
    # failure pulls the server out. The state is visible in body + header.
    body = {
        "state": report.state,
        "checks": [{"name": c.name, "blocking": c.blocking, "state": c.state} for c in report.checks],
    }
    status = 503 if report.state == NOT_READY else 200
    _set_ready_gauge(report.state)
    return JSONResponse(body, status_code=status, headers={"X-Readiness": report.state})


def _set_ready_gauge(state: str) -> None:
    from context_runtime import get_observability

    code = {"ready": 1.0, "degraded": 0.5, "not_ready": 0.0}.get(state, 0.0)
    get_observability().meter.set_ready(code)


@router.get("/metrics")
def metrics(_auth=Depends(verify_auth)):
    """Prometheus exposition (design §7.6.2/§8.1): admin/内网 surface —
    authenticated by default; content-free by construction (bounded label
    vocabularies only)."""
    try:
        from fastapi.responses import Response
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
    except ImportError:
        return JSONResponse({"detail": "prometheus_client not installed"}, status_code=501)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/v1/capabilities")
def capabilities(_auth=Depends(verify_auth)):
    """Real probes only (design §10): extraction/semantic/keyword/sql fallback
    reflect what actually works, never the config file."""
    service = get_app_service()
    caps = service.capabilities()
    caps["memory"]["prepare_context"] = True
    caps["sources"] = False
    caps["handoff"] = False
    caps["review_inbox"] = False
    return caps
