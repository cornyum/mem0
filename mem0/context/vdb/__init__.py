"""Pure-VDB memory authority tier (memory-storage-mode-design v3).

ES is the sole memory authority: five document families (scope/head/version/
event/dedup) with a single CAS linearization point on the scope document.
SQL appears only in HYBRID_STORAGE as an async recall sidecar.
"""

from mem0.context.vdb.errors import (
    DedupConflictError,
    OperationInProgressError,
    PrimaryConflictError,
    PrimaryUnavailableError,
    PublishedRepairPendingError,
)
from mem0.context.vdb.es_store import ElasticsearchMemoryStore, build_es_client
from mem0.context.vdb.hybrid import HybridSidecar
from mem0.context.vdb.recall import RecallCoordinator
from mem0.context.vdb.reconcile import EmbeddingReconciler, RecoveryReconciler
from mem0.context.vdb.service import MemoryApplicationService
from mem0.context.vdb.storage_config import (
    MODE_HYBRID_STORAGE,
    MODE_ONLY_VDB,
    parse_storage_mode,
    validate_vector_store_provider,
)
from mem0.context.vdb.write import WriteCoordinator

__all__ = [
    "DedupConflictError",
    "ElasticsearchMemoryStore",
    "EmbeddingReconciler",
    "HybridSidecar",
    "MemoryApplicationService",
    "MODE_HYBRID_STORAGE",
    "MODE_ONLY_VDB",
    "OperationInProgressError",
    "PrimaryConflictError",
    "PrimaryUnavailableError",
    "PublishedRepairPendingError",
    "RecallCoordinator",
    "RecoveryReconciler",
    "WriteCoordinator",
    "build_es_client",
    "parse_storage_mode",
    "validate_vector_store_provider",
]
