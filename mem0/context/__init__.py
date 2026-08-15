"""Context layer: PowerContext-style authoritative memory fused onto mem0.

Public surface (design §5.1/§5.3):

- :class:`PowerMemory` — Memory subclass with remember/retire/reactivate/
  changes/expand
- :class:`ContextStore` — authoritative revision store (ctx table family)
- :class:`ScopeIdentity` — five-id scope model with derived scope_key
- :class:`ReadinessRegistry` — three-state readiness probes
- pydantic wire models and the error taxonomy

Module layout mirrors the design doc: errors/scope/hashing/analyzer are
dependency-free; tables/store need SQLAlchemy; power_memory needs the full
Memory pipeline.
"""

from mem0.context.errors import (
    CapabilityNotSupportedError,
    ContextError,
    ContextValidationError,
    EntryNotFoundError,
    EvidenceExpiredError,
    RevisionConflictError,
)
from mem0.context.models import (
    ChangeRecordBody,
    EntryVersionBody,
    MemoryCitation,
    RememberResult,
    ScopeParams,
)
from mem0.context.scope import SCOPE_FIELDS, ScopeIdentity
from mem0.context.store import ContextStore

__all__ = [
    "CapabilityNotSupportedError",
    "ChangeRecordBody",
    "ContextError",
    "ContextStore",
    "ContextValidationError",
    "EntryNotFoundError",
    "EntryVersionBody",
    "EvidenceExpiredError",
    "MemoryCitation",
    "RememberResult",
    "RevisionConflictError",
    "SCOPE_FIELDS",
    "ScopeIdentity",
    "ScopeParams",
]
