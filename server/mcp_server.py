"""MCP projection (design §6.2): a whitelist of context operations exposed
as MCP tools over the SAME in-process memory instance the REST tier uses —
zero semantic fork, no double-counted transport metrics.

Tools mirror the /v1 operationIds: remember / recall / retire / reactivate
/ changes / expand / capture_source / prepare. Auth is enforced by the
HTTP layer that mounts this server; MCP itself is not an authorization
boundary (PowerContext RFC 0050 same caveat) — reviewers must control
endpoint access.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

try:
    from fastmcp import FastMCP
except ImportError:  # optional extra
    FastMCP = None

MCP_TOOLS_ENABLED = (
    "remember",
    "recall",
    "retire",
    "reactivate",
    "changes",
    "expand",
    "capture_source",
    "prepare",
)


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
    ) -> Dict[str, Any]:
        """Explicit idempotent memory write (zero-LLM in append mode)."""
        from server_state import get_memory_instance

        result = get_memory_instance().remember(
            text, mode=mode, kind=kind,
            **{k: v for k, v in dict(user_id=user_id, agent_id=agent_id, run_id=run_id, tenant_id=tenant_id, session_id=session_id).items() if v},
        )
        return result.model_dump(mode="json")

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
    ) -> Dict[str, Any]:
        """Channel-transparent retrieval with freshness checks."""
        from server_state import get_memory_instance

        return get_memory_instance().recall(
            query, limit=limit, mode=mode,
            **{k: v for k, v in dict(user_id=user_id, agent_id=agent_id, run_id=run_id, tenant_id=tenant_id, session_id=session_id).items() if v},
        )

    @mcp.tool
    def retire(entry_id: str, user_id: str, reason: Optional[str] = None) -> Dict[str, Any]:
        """Logically deactivate an entry (history stays queryable)."""
        from server_state import get_memory_instance

        result = get_memory_instance().retire(entry_id, reason=reason, user_id=user_id)
        return result.model_dump(mode="json")

    @mcp.tool
    def reactivate(entry_id: str, user_id: str) -> Dict[str, Any]:
        from server_state import get_memory_instance

        result = get_memory_instance().reactivate(entry_id, user_id=user_id)
        return result.model_dump(mode="json")

    @mcp.tool
    def changes(user_id: str, since_revision: int = 0, limit: int = 200) -> Dict[str, Any]:
        """Version-chain tail for one scope (paginate via next_cursor)."""
        from server_state import get_memory_instance

        records = get_memory_instance().changes(user_id=user_id, since_revision=since_revision, limit=limit)
        return {"changes": [r.model_dump(mode="json") for r in records]}

    @mcp.tool
    def expand(entry_id: str, entry_version_id: str, user_id: str) -> Dict[str, Any]:
        """Version-exact read with hash re-verification."""
        from mem0.context.scope import ScopeIdentity
        from server_state import get_memory_instance

        memory = get_memory_instance()
        artifact_id = memory.ctx_store.get_artifact_id(ScopeIdentity(user_id=user_id))
        body = memory.expand(
            memory._citation_from_dict(
                {"artifact_id": artifact_id, "entry_id": entry_id, "entry_version_id": entry_version_id}
            ),
            user_id=user_id,
        )
        return body.model_dump(mode="json")

    @mcp.tool
    def capture_source(content: str, user_id: str, source_type: str = "content") -> Dict[str, Any]:
        """Append a raw-fact Source to the per-scope journal."""
        from server_state import get_memory_instance

        return get_memory_instance().capture_source(content, source_type=source_type, user_id=user_id)

    @mcp.tool
    def prepare(query: str, user_id: str, budget_bytes: int = 8000) -> Dict[str, Any]:
        """PreparedContext: trust-enveloped, byte-budgeted prompt assembly."""
        from server_state import get_memory_instance

        return get_memory_instance().prepare_context(query, budget_bytes=budget_bytes, user_id=user_id)

    return mcp


def mount_mcp(app):
    """Mount the MCP streamable-HTTP endpoint at /mcp when fastmcp exists."""
    if FastMCP is None:
        logger.info("fastmcp not installed; MCP projection disabled (pip install fastmcp)")
        return None
    try:
        mcp = build_mcp_server()
        app.mount("/mcp", mcp.streamable_http_app())
        logger.info("MCP projection mounted at /mcp (%d tools)", len(MCP_TOOLS_ENABLED))
        return mcp
    except Exception:
        logger.warning("MCP projection failed to mount", exc_info=True)
        return None
