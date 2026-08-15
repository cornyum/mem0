"""Server-side runtime for the context layer (design §2.1 orchestration tier).

Owns the process-wide ContextStore bound to the app DB engine and the
readiness registry wired to the current memory instance. Both are rebuilt
through :func:`server_state._build_memory` so a config hot-reload
(POST /configure) can never leave a stale vector store or a stale probe
behind — the single-factory discipline from the design review (v2 R2).
"""

import logging
import threading

from sqlalchemy import text

import db
from mem0.context.observability import Observability, shared_observability
from mem0.context.readiness import Probe, ReadinessRegistry
from mem0.context.store import ContextStore

logger = logging.getLogger(__name__)

CTX_TABLE_PREFIX = f"{db.TABLE_PREFIX}ctx_"

_lock = threading.Lock()
_ctx_store: ContextStore | None = None
_readiness: ReadinessRegistry | None = None
_obs: Observability | None = None


def get_context_store() -> ContextStore:
    global _ctx_store
    with _lock:
        if _ctx_store is None:
            _ctx_store = ContextStore(db.engine, CTX_TABLE_PREFIX)
        return _ctx_store


def get_observability() -> Observability:
    """One Observability bundle per process (single prometheus
    registration); rebuilt memory instances reuse it."""
    global _obs
    with _lock:
        if _obs is None:
            _obs = shared_observability()
        return _obs


def reset_context_runtime() -> None:
    """Test hook: drop cached singletons (fresh engine / fresh instance)."""
    global _ctx_store, _readiness, _obs
    with _lock:
        _ctx_store = None
        _readiness = None
        _obs = None


CTX_WRITE_MODE_KEY = "ctx_write_mode"


def get_ctx_write_mode() -> str:
    """Dual-write staging switch (design §5.4): "off" (default during P0)
    or "dual" (legacy writes also land in the authoritative store). Read
    from the Settings KV so operators can stage the rollout without a
    restart; storage failure falls back to "off" — the safe default."""
    try:
        session = db.SessionLocal()
        try:
            from models import Settings

            row = session.get(Settings, CTX_WRITE_MODE_KEY)
            value = (row.value if row else None) or "off"
            return value if value in ("off", "dual") else "off"
        finally:
            session.close()
    except Exception:
        logger.warning("ctx_write_mode lookup failed; defaulting to off", exc_info=True)
        return "off"


class _AppDbProbe(Probe):
    def __init__(self):
        super().__init__("app_db", blocking=True)

    def check(self) -> None:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))


class _VectorStoreProbe(Probe):
    def __init__(self, vector_store):
        super().__init__("vector_store", blocking=True)
        self._vector_store = vector_store

    def check(self) -> None:
        self._vector_store.list(top_k=1)


class _ProviderPresenceProbe(Probe):
    """P0 model probe: presence-based (provider configured and non-null).
    P1 upgrades these to active health calls (design §3.5)."""

    def __init__(self, name: str, configured: bool):
        super().__init__(name, blocking=False)
        self._configured = configured

    def check(self) -> None:
        if not self._configured:
            raise RuntimeError("provider is null")


def set_memory_instance(memory) -> None:
    """Rebuild the readiness registry against the current memory instance.
    Called from server_state._build_memory on every (re)build."""
    global _readiness
    with _lock:
        registry = ReadinessRegistry()
        registry.register(_AppDbProbe())
        registry.register(_VectorStoreProbe(memory.vector_store))
        config = memory.config
        registry.register(_ProviderPresenceProbe("llm", config.llm.provider != "null"))
        registry.register(_ProviderPresenceProbe("embedder", config.embedder.provider != "null"))
        registry.register(_ProviderPresenceProbe("reranker", memory.reranker is not None))
        _readiness = registry


def get_readiness():
    if _readiness is None:
        raise RuntimeError("Memory runtime has not been initialized.")
    return _readiness
