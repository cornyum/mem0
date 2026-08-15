"""Context layer REST surface (design §5.3).

POST /v1/memory/remember|recall|retire|reactivate|expand|changes,
GET /health/ready (three-state), GET /v1/capabilities.

All context operations are POST + JSON (PowerContext contract style) so
scope ids never land in access-log query strings. The /v1 namespace is
deliberately separate from the legacy no-prefix routes: context endpoints
are additive and never change legacy behaviour (ctx_write_mode=off during
P0). ContextError subclasses translate to their documented status codes
(409/501/410/422/503) via the route class — one place, no per-endpoint
try/except.
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from auth import require_admin, verify_auth
from context_runtime import get_readiness
from mem0.context.errors import ContextError
from mem0.context.models import (
    ChangesRequest,
    ExpandRequest,
    PrepareContextRequest,
    ReactivateRequest,
    RecallRequest,
    RememberRequest,
    RetireRequest,
)
from mem0.context.readiness import NOT_READY
from mem0.vector_stores.base import VectorStoreBase
from server_state import get_memory_instance


class _ContextRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def context_aware_handler(request):
            try:
                return await original(request)
            except ContextError as exc:
                raise HTTPException(status_code=exc.default_status_code, detail=str(exc))

        return context_aware_handler


router = APIRouter(route_class=_ContextRoute)


@router.post("/v1/memory/remember")
def remember(req: RememberRequest, _auth=Depends(verify_auth)):
    memory = get_memory_instance()
    result = memory.remember(
        req.text,
        mode=req.mode,
        kind=req.kind,
        categories=req.categories,
        source_refs=tuple(req.source_refs),
        artifact_refs=tuple(req.artifact_refs),
        expected_revision=req.expected_revision,
        **req.identity_kwargs(),
    )
    return result.model_dump(mode="json")


@router.post("/v1/memory/recall")
def recall(req: RecallRequest, _auth=Depends(verify_auth)):
    memory = get_memory_instance()
    kwargs = {}
    if req.threshold is not None:
        kwargs["threshold"] = req.threshold
    return memory.recall(
        req.query,
        limit=req.limit,
        mode=req.mode,
        rerank=req.rerank,
        **kwargs,
        **req.identity_kwargs(),
    )


@router.post("/v1/memory/reconcile")
def reconcile(_admin=Depends(require_admin)):
    """Drain pending vector projections (maintenance, design §5.2)."""
    memory = get_memory_instance()
    return memory.reconcile_projections()


@router.post("/v1/memory/backfill")
def backfill(batch_size: int = 500, _admin=Depends(require_admin)):
    """Adopt existing vector rows into the authoritative store (design
    §5.4). Idempotent — rerun until truncated=False."""
    if batch_size < 1 or batch_size > 5000:
        raise HTTPException(status_code=422, detail="batch_size must be 1..5000")
    memory = get_memory_instance()
    return memory.backfill(batch_size=batch_size)


@router.post("/v1/memory/rebuild")
def rebuild(batch_size: int = 200, _admin=Depends(require_admin)):
    """Rebuild the entire vector projection from the authoritative store
    (design §3.2, P2 acceptance ④). Operator maintenance — pause writers
    for a clean cut, then run reconcile afterwards."""
    if batch_size < 1 or batch_size > 1000:
        raise HTTPException(status_code=422, detail="batch_size must be 1..1000")
    memory = get_memory_instance()
    return memory.rebuild_projections(batch_size=batch_size)


@router.post("/v1/context/prepare")
def prepare_context(req: PrepareContextRequest, _auth=Depends(verify_auth)):
    """Deterministic, byte-budgeted prompt assembly with the trust
    envelope (design §6.4)."""
    memory = get_memory_instance()
    return memory.prepare_context(
        req.query,
        budget_bytes=req.budget_bytes,
        mode=req.mode,
        **req.identity_kwargs(),
    )


@router.post("/v1/memory/retire")
def retire(req: RetireRequest, _auth=Depends(verify_auth)):
    memory = get_memory_instance()
    result = memory.retire(
        req.entry_id,
        reason=req.reason,
        expected_revision=req.expected_revision,
        **req.identity_kwargs(),
    )
    return result.model_dump(mode="json")


@router.post("/v1/memory/reactivate")
def reactivate(req: ReactivateRequest, _auth=Depends(verify_auth)):
    memory = get_memory_instance()
    result = memory.reactivate(
        req.entry_id,
        reason=req.reason,
        expected_revision=req.expected_revision,
        **req.identity_kwargs(),
    )
    return result.model_dump(mode="json")


@router.post("/v1/memory/expand")
def expand(req: ExpandRequest, _auth=Depends(verify_auth)):
    memory = get_memory_instance()
    body = memory.expand(req.citation, **req.identity_kwargs())
    return body.model_dump(mode="json")


@router.post("/v1/memory/changes")
def changes(req: ChangesRequest, _auth=Depends(verify_auth)):
    memory = get_memory_instance()
    records = memory.changes(
        since_revision=req.since_revision,
        limit=req.limit,
        cursor=req.cursor,
        **req.identity_kwargs(),
    )
    return {"changes": [r.model_dump(mode="json") for r in records]}


@router.get("/health/ready")
def health_ready():
    report = get_readiness().evaluate()
    # degraded stays in rotation by design (design §3.5): only a blocking
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
def metrics():
    """Prometheus exposition (design §8.1). Content-free by construction:
    labels are bounded vocabularies only."""
    try:
        from fastapi.responses import Response
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
    except ImportError:
        return JSONResponse({"detail": "prometheus_client not installed"}, status_code=501)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/v1/capabilities")
def capabilities(_auth=Depends(verify_auth)):
    memory = get_memory_instance()
    keyword_supported = getattr(type(memory.vector_store), "keyword_search", None) is not VectorStoreBase.keyword_search
    return {
        "memory": {
            "explicit_lifecycle": True,
            "extraction": memory.config.llm.provider != "null",
            "embedding": memory.config.embedder.provider != "null",
            "rerank": memory.reranker is not None,
            "keyword_search": keyword_supported,
        },
        "handoff": False,  # P2 (design §7)
        "review_inbox": False,  # P2 (design §7)
    }
