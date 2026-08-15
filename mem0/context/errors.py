"""Error taxonomy for the context layer (design §5.3).

Every failure mode maps to exactly one exception class with an explicit
capability name; the REST layer translates them to the documented status
codes (409 / 501 / 410 / 422 / 503). No silent fallbacks: a missing
capability raises, an expired citation raises, a lost CAS race raises.
"""

from typing import Optional


class ContextError(Exception):
    """Base class for all context-layer errors."""

    default_status_code = 500

    def __init__(self, message: str, *, capability: Optional[str] = None):
        super().__init__(message)
        self.capability = capability


class ContextValidationError(ContextError):
    """Invalid input (oversized text, malformed scope, bad citation)."""

    default_status_code = 422


class CapabilityNotSupportedError(ContextError):
    """A capability (e.g. embedding, extraction) is not configured."""

    default_status_code = 501

    def __init__(self, capability: str):
        super().__init__(f"Capability not supported in this deployment: {capability}", capability=capability)


class RevisionConflictError(ContextError):
    """CAS failure: the expected revision no longer matches the head."""

    default_status_code = 409

    def __init__(
        self,
        *,
        scope_key: str,
        artifact_id: str,
        expected_revision: Optional[int],
        current_revision: Optional[int] = None,
    ):
        detail = (
            f"Revision conflict for artifact {artifact_id}: expected {expected_revision}, current {current_revision}"
        )
        super().__init__(detail)
        self.scope_key = scope_key
        self.artifact_id = artifact_id
        self.expected_revision = expected_revision
        self.current_revision = current_revision


class EvidenceExpiredError(ContextError):
    """Citation hash mismatch on expand: the evidence changed."""

    default_status_code = 410


class EntryNotFoundError(ContextError):
    """Referenced entry/version does not exist in the authoritative store."""

    default_status_code = 404
