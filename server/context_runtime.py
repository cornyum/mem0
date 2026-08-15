"""Server-side runtime for the memory tiers.

Two regimes (design §3):

- v3 storage modes (``ONLY_VDB`` default, ``HYBRID_STORAGE``): the memory
  authority is the :class:`MemoryApplicationService` over Elasticsearch; SQL
  appears only in HYBRID as the recall sidecar. The legacy ctx/PowerMemory
  machinery is not constructed — no ``ctx_*`` table is created or queried.
- The readiness registry and observability bundle are shared by both regimes
  and rebuilt through the single-construction discipline (server_state).
"""

import logging
import threading
from typing import Optional

from sqlalchemy import text

import db
from mem0.context.observability import Observability, shared_observability
from mem0.context.readiness import Probe, ReadinessRegistry
from mem0.context.vdb import HybridSidecar, parse_storage_mode

logger = logging.getLogger(__name__)

STORAGE_MODE = parse_storage_mode()

_lock = threading.RLock()  # reentrant: register_v3_readiness holds it while building the sidecar
_obs: Optional[Observability] = None
_sidecar: Optional[HybridSidecar] = None


def get_observability() -> Observability:
    """One Observability bundle per process (single prometheus
    registration); rebuilt memory instances reuse this."""
    global _obs
    with _lock:
        if _obs is None:
            _obs = shared_observability()
        return _obs


def get_hybrid_sidecar() -> Optional[HybridSidecar]:
    """HYBRID_STORAGE only: the SQL recall sidecar bound to the app engine.
    ONLY_VDB never constructs, connects, or checks it (design §8)."""
    global _sidecar
    if STORAGE_MODE != "HYBRID_STORAGE":
        return None
    with _lock:
        if _sidecar is None:
            try:
                _sidecar = HybridSidecar(db.engine, table_prefix=db.TABLE_PREFIX, ensure_schema=True)
            except Exception:
                logger.warning("Hybrid sidecar schema init failed; FTS fallback disabled", exc_info=True)
                _sidecar = None
        return _sidecar


def reset_context_runtime() -> None:
    """Test hook: drop cached singletons (fresh engine / fresh instance)."""
    global _obs, _sidecar
    with _lock:
        _obs = None
        _sidecar = None


class _AppDbProbe(Probe):
    """Non-memory app capabilities (auth/settings/audit) still need the app
    DB; memory paths in ONLY_VDB never touch it (design §1)."""

    def __init__(self):
        super().__init__("app_db", blocking=True)

    def check(self) -> None:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))


class _ElasticsearchProbe(Probe):
    """The memory authority health check (design §10): ES down ⇒ not_ready in
    ONLY_VDB; in HYBRID it degrades instead of failing (handled by the SQL
    FTS probe below plus this probe's blocking flag)."""

    def __init__(self, store, *, blocking: bool):
        super().__init__("elasticsearch", blocking=blocking)
        self._store = store

    def check(self) -> None:
        self._store.client.info()


class _SqlFtsProbe(Probe):
    def __init__(self, sidecar: HybridSidecar):
        super().__init__("sql_fts", blocking=False)
        self._sidecar = sidecar

    def check(self) -> None:
        if not self._sidecar.healthy():
            raise RuntimeError("recall sidecar unavailable")


class _ProviderPresenceProbe(Probe):
    """Model providers never gate readiness (design §10): a missing LLM or
    embedder only disables the commands that need it."""

    def __init__(self, name: str, configured: bool):
        super().__init__(name, blocking=False)
        self._configured = configured

    def check(self) -> None:
        if not self._configured:
            raise RuntimeError("provider is null")


def set_memory_instance(memory) -> None:
    """Compatibility shim kept for imports; the v3 builder registers the
    readiness registry itself (see register_v3_readiness)."""
    register_legacy_readiness(memory)


def register_legacy_readiness(memory) -> None:
    global _readiness
    with _lock:
        registry = ReadinessRegistry()
        registry.register(_AppDbProbe())
        from mem0.context.readiness import Probe as _P

        class _VectorProbe(_P):
            def __init__(self):
                super().__init__("vector_store", blocking=True)
                self._vs = memory.vector_store

            def check(self) -> None:
                self._vs.list(top_k=1)

        registry.register(_VectorProbe())
        _readiness = registry


def register_v3_readiness(service) -> None:
    """Readiness per design §10 for the v3 storage modes."""
    global _readiness
    with _lock:
        registry = ReadinessRegistry()
        registry.register(_AppDbProbe())
        # ONLY_VDB: ES is the authority — hard dependency. HYBRID: ES failure
        # degrades (SQL FTS may keep keyword/auto recall alive) but writes
        # still 503, so the probe stays blocking in both modes; the sidecar
        # probe reports the degraded leg.
        registry.register(_ElasticsearchProbe(service.store, blocking=True))
        sidecar = get_hybrid_sidecar()
        if sidecar is not None:
            registry.register(_SqlFtsProbe(sidecar))
        registry.register(_ProviderPresenceProbe("llm", service.llm is not None))
        registry.register(_ProviderPresenceProbe("embedder", service.embedder is not None))
        registry.register(_ProviderPresenceProbe("reranker", service.reranker is not None))
        _readiness = registry


_readiness = None


def get_readiness():
    if _readiness is None:
        raise RuntimeError("Memory runtime has not been initialized.")
    return _readiness
