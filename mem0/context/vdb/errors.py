"""Error taxonomy extensions for the pure-VDB authority tier (design §7.4/§7.5).

The context-layer base classes stay in :mod:`mem0.context.errors`; this module
adds the ES-specific failures mapped to their documented status codes:

- ``PrimaryUnavailableError``      503  ES runtime failure (ONLY_VDB or semantic)
- ``PrimaryConflictError``         503  unexpected ES 409 (internal conflict)
- ``OperationInProgressError``     409  same dedup claim is being completed
- ``PublishedRepairPendingError``  503  CAS published, derived writes pending
- ``DedupConflictError``           409  dedup key owned by another active entry
"""

from typing import Optional

from mem0.context.errors import ContextError


class PrimaryUnavailableError(ContextError):
    """The ES primary is unreachable/timing out/throttling."""

    default_status_code = 503

    def __init__(self, message: str, *, error_class: Optional[str] = None):
        super().__init__(message)
        self.error_class = error_class


class PrimaryConflictError(ContextError):
    """An ES 409 that is neither a scope CAS race nor a dedup create."""

    default_status_code = 503


class OperationInProgressError(ContextError):
    """A prepared dedup claim is in flight and cannot be recovered now."""

    default_status_code = 409


class PublishedRepairPendingError(ContextError):
    """The scope CAS succeeded but head/event/dedup are still being repaired."""

    default_status_code = 503

    def __init__(self, message: str, *, scope_key: str, revision: int, entry_id: str):
        super().__init__(message)
        self.scope_key = scope_key
        self.revision = revision
        self.entry_id = entry_id


class DedupConflictError(ContextError):
    """The dedup key is held by a different active entry."""

    default_status_code = 409


# ES error classification (design §7.5). Only UNAVAILABLE/TIMEOUT/THROTTLED
# may trigger the HYBRID SQL fallback.
ES_UNAVAILABLE = "UNAVAILABLE"
ES_TIMEOUT = "TIMEOUT"
ES_THROTTLED = "THROTTLED"
ES_CONFLICT = "CONFLICT"
ES_BAD_REQUEST = "BAD_REQUEST"
ES_NOT_FOUND = "NOT_FOUND"
ES_AUTH = "AUTH"
ES_UNKNOWN = "UNKNOWN"

FALLBACK_ELIGIBLE_CLASSES = frozenset({ES_UNAVAILABLE, ES_TIMEOUT, ES_THROTTLED})


def classify_elasticsearch_error(exc: Exception) -> str:
    """Classify an elasticsearch client exception into the §7.5 vocabulary."""
    try:
        from elasticsearch import AuthenticationException, AuthorizationException
        from elasticsearch import BadRequestError
        from elasticsearch import ConflictError as EsConflictError
        from elasticsearch import ConnectionError as EsConnectionError
        from elasticsearch import ConnectionTimeout
        from elasticsearch import NotFoundError
        from elasticsearch import TransportError

        if isinstance(exc, AuthenticationException):
            return ES_AUTH
        if isinstance(exc, AuthorizationException):
            return ES_AUTH
        if isinstance(exc, ConnectionTimeout):
            return ES_TIMEOUT
        if isinstance(exc, EsConnectionError):
            return ES_UNAVAILABLE
        if isinstance(exc, EsConflictError):
            return ES_CONFLICT
        if isinstance(exc, NotFoundError):
            return ES_NOT_FOUND
        if isinstance(exc, BadRequestError):
            return ES_BAD_REQUEST
        if isinstance(exc, TransportError):
            status = getattr(exc, "status_code", None)
            if status == 429:
                return ES_THROTTLED
            if status in (502, 503, 504):
                return ES_UNAVAILABLE
            if status == 408:
                return ES_TIMEOUT
            if status == 409:
                return ES_CONFLICT
            if status == 404:
                return ES_NOT_FOUND
            if status in (400, 422):
                return ES_BAD_REQUEST
            if status in (401, 403):
                return ES_AUTH
            return ES_UNKNOWN
    except ImportError:  # elasticsearch extra missing — caller decides
        pass
    return ES_UNKNOWN
