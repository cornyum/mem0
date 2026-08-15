"""MCP projection (design §7.3): tools generated from the
MemoryApplicationService — the SAME in-process domain entry point REST /v1
uses, zero semantic fork. Auth is mandatory: the HTTP transport must carry
Bearer / X-API-Key and reuses the server's verify_auth semantics
(acceptance §12.8: no credentials ⇒ 401).
"""

import logging
from typing import Any, Dict, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

try:
    from fastmcp import FastMCP
except ImportError:  # optional extra
    FastMCP = None

MCP_TOOLS_ENABLED = (
    "remember",
    "recall",
    "revise",
    "retire",
    "reactivate",
    "changes",
    "expand",
    "get",
    "prepare",
)


class McpAuthMiddleware(BaseHTTPMiddleware):
    """Service-level MCP authentication (design §7.3): Bearer JWT or
    X-API-Key, same resolution rules as verify_auth. AUTH-disabled
    deployments stay open (local development only)."""

    async def dispatch(self, request, call_next):
        if request.url.path.endswith(("/docs", "/openapi.json")) or request.method == "OPTIONS":
            return await call_next(request)
        try:
            import secrets

            import auth as auth_mod
            from db import SessionLocal

            bearer = request.headers.get("authorization", "")
            token = bearer[7:].strip() if bearer.lower().startswith("bearer ") else None
            api_key = request.headers.get("x-api-key")
            if token is None and api_key is None:
                if auth_mod.AUTH_DISABLED:
                    return await call_next(request)
                return JSONResponse(
                    {"detail": "Authentication required. Provide a Bearer token or X-API-Key header."},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            with SessionLocal() as db:
                if token is not None:
                    auth_mod._resolve_user_from_jwt(token, db)
                else:
                    if auth_mod.ADMIN_API_KEY and secrets.compare_digest(api_key, auth_mod.ADMIN_API_KEY):
                        return await call_next(request)
                    auth_mod._resolve_user_from_api_key(api_key, db)
            return await call_next(request)
        except Exception:
            logger.warning("MCP auth rejected a request", exc_info=True)
            return JSONResponse({"detail": "Invalid credentials"}, status_code=401)


def _ids(user_id, agent_id, run_id, tenant_id, session_id) -> Dict[str, str]:
    return {
        k: v
        for k, v in dict(
            user_id=user_id, agent_id=agent_id, run_id=run_id, tenant_id=tenant_id, session_id=session_id
        ).items()
        if v
    }


def build_mcp_server() -> "FastMCP":
    if FastMCP is None:
        raise RuntimeError("fastmcp is not installed; MCP projection disabled")
    mcp = FastMCP(name="agentar-memory")

    @mcp.tool
    def remember(
        text: str,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        run_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        session_id: Optional[str] = None,
        kind: str = "fact",
        mode: str = "append",
        categories: Optional[list] = None,
        source_refs: Optional[list] = None,
    ) -> Dict[str, Any]:
        """Idempotent memory write into the ES authority (append mode is zero-LLM)."""
        from server_state import get_app_service

        return get_app_service().remember(
            text,
            mode=mode,
            kind=kind,
            categories=categories or [],
            source_refs=source_refs or [],
            **_ids(user_id, agent_id, run_id, tenant_id, session_id),
        )

    @mcp.tool
    def recall(
        query: str,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        run_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        session_id: Optional[str] = None,
        limit: int = 10,
        mode: str = "auto",
        rerank: bool = False,
    ) -> Dict[str, Any]:
        """Channel-transparent retrieval (auto/semantic/keyword, RRF, matched_by)."""
        from server_state import get_app_service

        return get_app_service().recall(
            query, limit=limit, mode=mode, rerank=rerank,
            **_ids(user_id, agent_id, run_id, tenant_id, session_id),
        )

    @mcp.tool
    def revise(
        entry_id: str,
        text: str,
        user_id: str,
        kind: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Explicit revision of one entry (creates a new immutable version)."""
        from server_state import get_app_service

        result = get_app_service().revise(entry_id, text=text, kind=kind, user_id=user_id)
        return result.model_dump(mode="json")

    @mcp.tool
    def retire(entry_id: str, user_id: str, reason: Optional[str] = None) -> Dict[str, Any]:
        """Logically deactivate an entry (history stays queryable)."""
        from server_state import get_app_service

        result = get_app_service().retire(entry_id, reason=reason, user_id=user_id)
        return result.model_dump(mode="json")

    @mcp.tool
    def reactivate(entry_id: str, user_id: str) -> Dict[str, Any]:
        """Reactivate a retired entry."""
        from server_state import get_app_service

        result = get_app_service().reactivate(entry_id, user_id=user_id)
        return result.model_dump(mode="json")

    @mcp.tool
    def changes(user_id: str, since_revision: int = 0, limit: int = 200) -> Dict[str, Any]:
        """Revision-cursor change feed for one scope (retire/reactivate included)."""
        from server_state import get_app_service

        records = get_app_service().changes(user_id=user_id, since_revision=since_revision, limit=limit)
        return {"changes": [r.model_dump(mode="json") for r in records]}

    @mcp.tool
    def expand(entry_id: str, entry_version_id: str, user_id: str) -> Dict[str, Any]:
        """Version-exact read with hash re-verification."""
        from mem0.context.models import MemoryCitation
        from server_state import get_app_service

        from mem0.context.scope import ScopeIdentity

        service = get_app_service()
        scope_doc = service.store.get_scope(ScopeIdentity(user_id=user_id).scope_key)
        body = service.expand(
            MemoryCitation(
                artifact_id=scope_doc.artifact_id,
                entry_id=entry_id,
                entry_version_id=entry_version_id,
            ),
            user_id=user_id,
        )
        return body.model_dump(mode="json")

    @mcp.tool
    def get(entry_id: str, user_id: str) -> Dict[str, Any]:
        """Point read of the current head for one entry."""
        from server_state import get_app_service

        return get_app_service().get(entry_id, user_id=user_id)

    @mcp.tool
    def prepare(query: str, user_id: str, budget_bytes: int = 8000) -> Dict[str, Any]:
        """PreparedContext: trust-enveloped, byte-budgeted prompt assembly."""
        from server_state import get_app_service

        return get_app_service().prepare_context(query, budget_bytes=budget_bytes, user_id=user_id)

    return mcp


def mount_mcp(app):
    """Mount the MCP streamable-HTTP endpoint at /mcp when fastmcp exists.

    NOTE: fastmcp's session manager initializes in the app lifespan, and
    Starlette does NOT run lifespans of mounted sub-apps — mounting under the
    main server leaves the transport unusable. Use :func:`create_standalone_app`
    (its own process/lifespan, e.g. the compose mem0-mcp service) for a working
    endpoint; this mount is kept only for embeddings where the host app adopts
    the lifespan."""
    if FastMCP is None:
        logger.info("fastmcp not installed; MCP projection disabled (pip install fastmcp)")
        return None
    try:
        mcp = build_mcp_server()
        app.mount("/mcp", mcp.http_app(path="/"))
        logger.info("MCP projection mounted at /mcp (%d tools)", len(MCP_TOOLS_ENABLED))
        return mcp
    except Exception:
        logger.warning("MCP projection failed to mount", exc_info=True)
        return None


def create_standalone_app():
    """Standalone MCP service app (own lifespan — the supported deployment:
    `uvicorn mcp_standalone:app` / the compose mem0-mcp service). Same
    whitelist tools, same in-process semantics, service-level auth enforced
    on every request (design §7.3)."""
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    if FastMCP is None:
        raise RuntimeError("fastmcp is not installed")
    mcp = build_mcp_server()
    http_app = mcp.http_app(path="/")

    @asynccontextmanager
    async def lifespan(app):
        # Importing the REST module runs initialize_state(DEFAULT_CONFIG) —
        # the single env-driven construction path — giving this process the
        # same configured service (design §8.2 single-factory rule).
        import main  # noqa: F401

        async with http_app.lifespan(http_app):
            yield

    app = FastAPI(title="Agentar Memory MCP", lifespan=lifespan)
    app.add_middleware(McpAuthMiddleware)
    app.mount("/", http_app)
    return app
