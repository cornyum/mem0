"""Wire API models for the context layer (design §5.3).

These are the single source of truth shared by the SDK results and the
``/v1/*`` REST contract — the router serializes them directly.
"""

from datetime import datetime, timezone
from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, Field

from mem0.context.scope import SCOPE_FIELDS

OUTCOME_CREATED = "created"
OUTCOME_UPDATED = "updated"
OUTCOME_NOOP = "noop"

DEFAULT_KIND = "fact"

# Individual reference/category strings are bounded so the 8 KiB text limit
# cannot be sidestepped through oversized list items.
RefItem = Annotated[str, Field(min_length=1, max_length=256)]


class ScopeParams(BaseModel):
    """Identity ids accepted on every context endpoint — mirrors mem0's
    five first-class scope ids; at least one must be present (validated by
    ScopeIdentity, which also canonicalizes and derives scope_key)."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: Optional[str] = None
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    run_id: Optional[str] = None
    session_id: Optional[str] = None

    def identity_kwargs(self) -> dict:
        return {field: getattr(self, field) for field in SCOPE_FIELDS}


class MemoryCitation(BaseModel):
    """Version-exact reference (design §3.2): the triple identifies one
    immutable entry version, and expand() re-verifies its content hash."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(min_length=1, max_length=64)
    entry_id: str = Field(min_length=1, max_length=64)
    entry_version_id: str = Field(min_length=1, max_length=64)


class EntryVersionBody(BaseModel):
    entry_id: str
    entry_version_id: str
    version: int
    previous_version_id: Optional[str] = None
    kind: str
    text: str
    categories: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    entry_content_hash: str
    created_in_revision: int
    provenance: str
    created_at: datetime

    def model_post_init(self, _ctx) -> None:
        # Dialect reality: MySQL DATETIME / SQLite return naive datetimes,
        # PostgreSQL returns aware ones. Outbound timestamps are always
        # UTC-normalized so the API contract does not depend on the backend.
        if self.created_at.tzinfo is None:
            object.__setattr__(self, "created_at", self.created_at.replace(tzinfo=timezone.utc))


class RememberResult(BaseModel):
    outcome: str  # created | updated | noop
    artifact_id: str
    revision: int
    pending_embed: bool = False
    entry: Optional[EntryVersionBody] = None


class RetireRequest(ScopeParams):
    entry_id: str = Field(min_length=1, max_length=64)
    reason: Optional[str] = Field(default=None, max_length=512)
    expected_revision: Optional[int] = Field(default=None, ge=0)


class ReactivateRequest(ScopeParams):
    entry_id: str = Field(min_length=1, max_length=64)
    reason: Optional[str] = Field(default=None, max_length=512)
    expected_revision: Optional[int] = Field(default=None, ge=0)


class ExpandRequest(ScopeParams):
    citation: MemoryCitation


class ChangesRequest(ScopeParams):
    since_revision: int = Field(default=0, ge=0)
    limit: int = Field(default=200, ge=1, le=1000)
    cursor: Optional[int] = Field(default=None, ge=0)


class RememberRequest(ScopeParams):
    text: str = Field(min_length=1)
    kind: str = Field(default=DEFAULT_KIND, min_length=1, max_length=64)
    categories: list[RefItem] = Field(default_factory=list, max_length=10)
    source_refs: list[RefItem] = Field(default_factory=list, max_length=32)
    artifact_refs: list[RefItem] = Field(default_factory=list, max_length=32)
    mode: str = Field(default="auto", pattern="^(auto|append|extract)$")
    expected_revision: Optional[int] = Field(default=None, ge=0)


class ChangeRecordBody(BaseModel):
    entry_id: str
    entry_version_id: str
    version: int
    kind: str
    entry_content_hash: str
    created_in_revision: int
    provenance: str
    created_at: datetime
    next_cursor: Optional[int] = None


class RecallRequest(ScopeParams):
    """POST /v1/memory/recall — channel-transparent retrieval with
    authoritative freshness checks (design §5.3/§6.1)."""

    query: str = Field(min_length=1, max_length=8192)
    limit: int = Field(default=10, ge=1, le=50)
    mode: str = Field(default="auto", pattern="^(auto|semantic|keyword)$")
    rerank: bool = Field(default=False)
    threshold: Optional[float] = Field(default=None, ge=0, le=1)


class PrepareContextRequest(ScopeParams):
    """POST /v1/context/prepare (design §6.4)."""

    query: str = Field(min_length=1, max_length=8192)
    budget_bytes: int = Field(default=8000, ge=512, le=32768)
    mode: str = Field(default="auto", pattern="^(auto|semantic|keyword)$")


class CaptureSourceRequest(ScopeParams):
    """POST /v1/sources/content (design §7)."""

    content: str = Field(min_length=1, max_length=65536)
    metadata: Optional[dict] = Field(default=None)
    source_type: str = Field(default="content", max_length=32)


class HandoffPrepareRequest(ScopeParams):
    after: int = Field(default=0, ge=0)
    through: Optional[int] = Field(default=None, ge=0)
    limit: int = Field(default=50, ge=1, le=200)


class HandoffStatement(BaseModel):
    text: str = Field(min_length=1, max_length=8192)
    citations: list[MemoryCitation] = Field(min_length=1, max_length=32)


class HandoffDraft(BaseModel):
    objective: Optional[str] = Field(default=None, max_length=8192)
    statements: list[HandoffStatement] = Field(min_length=1, max_length=64)
    next_action: Optional[str] = Field(default=None, max_length=8192)


class HandoffCommitRequest(ScopeParams):
    handoff_id: str = Field(min_length=1, max_length=64)
    draft: HandoffDraft


class HandoffContinueRequest(ScopeParams):
    handoff_id: str = Field(min_length=1, max_length=64)


class CandidateProposeRequest(ScopeParams):
    family: str = Field(pattern="^(experience|skill)$")
    proposal: dict
    source_refs: list[RefItem] = Field(min_length=1, max_length=32)
    reason: Optional[str] = Field(default=None, max_length=2000)


class CandidateReviseRequest(CandidateProposeRequest):
    candidate_id: str = Field(min_length=1, max_length=64)
    expected_version: Optional[int] = Field(default=None, ge=1)


class CandidateDecideRequest(ScopeParams):
    candidate_id: str = Field(min_length=1, max_length=64)
    expected_version: Optional[int] = Field(default=None, ge=1)
    decision_reason: Optional[str] = Field(default=None, max_length=512)


class CandidateListRequest(ScopeParams):
    status: Optional[str] = Field(default="pending", pattern="^(pending|approved|rejected|all)$")
    limit: int = Field(default=50, ge=1, le=100)
