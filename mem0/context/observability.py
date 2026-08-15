"""Observability layer (design §8, RFC 0046 discipline).

Prometheus metrics + OpenTelemetry spans for the context tier, applied by
wrapping the model/vector-store collaborators at the single construction
point (server_state._build_memory) — a config hot-rebuild can never leave
an unwrapped instance behind.

Hard data policy (design §8.1):
- Labels are bounded: operation/component/outcome vocabularies only. No
  request ids, scope ids, memory ids, paths, or any content-derived value.
- Spans and logs never carry prompts, texts, queries, vectors or
  credentials (include_content=False equivalent everywhere).
- Instrumentation failures are fully isolated: ``suppress``-style guard,
  a broken exporter must not change responses or readiness.

Dependencies are optional by design (metrics need prometheus_client, spans
need opentelemetry); when absent the layer degrades to zero-overhead
no-ops rather than failing imports.
"""

import contextlib
import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"
OUTCOME_NOOP = "noop"

_METRIC_PREFIX = "agentar_mem_"

try:  # optional extra: metrics
    import prometheus_client

    _PROM = True
except ImportError:  # pragma: no cover - environment without the extra
    _PROM = False

try:  # optional extra: tracing
    from opentelemetry import trace as _otel_trace

    _OTEL = True
except ImportError:  # pragma: no cover
    _OTEL = False


def _no_span():
    return contextlib.nullcontext()


class Meter:
    """Bounded Prometheus counters/histograms for one deployment. Pass a
    private ``registry`` in tests; the process-wide REGISTRY is the default."""

    def __init__(self, registry=None):
        self.enabled = _PROM
        self._registry = registry
        if not self.enabled:
            return
        reg = registry if registry is not None else prometheus_client.REGISTRY
        kw = {"registry": reg}

        def counter(name, doc):
            return prometheus_client.Counter(f"{_METRIC_PREFIX}{name}", doc, labelnames=["operation", "outcome"], **kw)

        def histogram(name, doc):
            return prometheus_client.Histogram(
                f"{_METRIC_PREFIX}{name}", doc, labelnames=["operation"], **kw
            )

        self.application_total = counter("application_operations_total", "Context-tier operations by outcome (noop included).")
        self.application_seconds = histogram("application_operation_duration_seconds", "Context-tier operation latency.")
        self.inference_total = counter("inference_calls_total", "Model-tier calls (llm/embedder) by outcome.")
        self.inference_seconds = histogram("inference_call_duration_seconds", "Model-tier call latency.")
        self.transport_total = counter("transport_requests_total", "Transport requests by outcome.")
        self.transport_seconds = histogram("transport_request_duration_seconds", "Transport request latency.")
        self.keyword_none_total = counter(
            "keyword_none_total", "keyword_search invocations that returned no channel (store unsupports or errored)."
        )
        self.runtime_ready = prometheus_client.Gauge(
            f"{_METRIC_PREFIX}runtime_ready", "1 = ready, 0.5 = degraded, 0 = not_ready", **kw
        )

    @contextlib.contextmanager
    def observe(self, family: str, operation: str):
        """Inference/transport operation scope: duration + success/failure,
        with the content-free exception guard (RFC 0046)."""
        if not self.enabled:
            yield
            return
        start = time.perf_counter()
        outcome = OUTCOME_SUCCESS
        try:
            yield
        except Exception:
            outcome = OUTCOME_FAILURE
            raise
        finally:
            self._record(family, operation, outcome, time.perf_counter() - start)

    @contextlib.contextmanager
    def application(self, operation: str):
        """Application-tier operation scope. The caller sets ``op.outcome``
        to the designed outcome vocabulary ("created"/"updated"/"noop"/...)
        on success; unhandled exceptions record failure and propagate."""
        if not self.enabled:
            yield _OpOutcome()
            return
        marker = _OpOutcome()
        start = time.perf_counter()
        try:
            yield marker
        except Exception:
            self._record("application", operation, OUTCOME_FAILURE, time.perf_counter() - start)
            raise
        else:
            self._record("application", operation, marker.outcome, time.perf_counter() - start)

    def _record(self, family: str, operation: str, outcome: str, seconds: float) -> None:
        try:
            getattr(self, f"{family}_total").labels(operation=operation, outcome=outcome).inc()
            getattr(self, f"{family}_seconds").labels(operation=operation).observe(seconds)
        except Exception:
            logger.debug("metrics recording failed", exc_info=True)

    def set_ready(self, state_code: float) -> None:
        if not self.enabled:
            return
        try:
            self.runtime_ready.set(state_code)
        except Exception:
            logger.debug("readiness gauge failed", exc_info=True)


class _OpOutcome:
    """Mutable marker yielded by Meter.application."""

    def __init__(self):
        self.outcome = OUTCOME_SUCCESS


class Observability:
    """Bundle handed to wrappers and application code: Meter + tracer."""

    def __init__(self, meter: Optional[Meter] = None, tracer_name: str = "agentar"):
        self.meter = meter if meter is not None else Meter()
        self._tracer_name = tracer_name

    def span(self, name: str, **attributes):
        """Start an INTERNAL span. Attributes must already be
        content-free (bounded vocabularies enforced by callers)."""
        if not _OTEL:
            return _no_span()
        tracer = _otel_trace.get_tracer(self._tracer_name)
        span_cm = tracer.start_as_current_span(name)
        return _SpanWithAttrs(span_cm, attributes)


class _SpanWithAttrs:
    def __init__(self, span_cm, attributes):
        self._span_cm = span_cm
        self._attributes = attributes

    def __enter__(self):
        self._span = self._span_cm.__enter__()
        try:
            for key, value in self._attributes.items():
                self._span.set_attribute(key, value)
        except Exception:
            logger.debug("span attribute set failed", exc_info=True)
        return self._span

    def __exit__(self, *exc):
        try:
            if exc[0] is not None:
                self._span.set_status(_otel_trace.Status(_otel_trace.StatusCode.ERROR))
                self._span.set_attribute("operation.outcome", OUTCOME_FAILURE)
            else:
                self._span.set_attribute("operation.outcome", OUTCOME_SUCCESS)
        except Exception:
            logger.debug("span close failed", exc_info=True)
        return self._span_cm.__exit__(*exc)


def _record_outcome(meter: Meter, family: str, operation: str, outcome: str) -> None:
    meter._record(family, operation, outcome, 0.0)


class ObservableLLM:
    """Wraps a mem0 LLM collaborator: latency + outcome per call, one
    content-free span. Never inspects prompts or responses."""

    def __init__(self, inner: Any, obs: Observability):
        self._inner = inner
        self._obs = obs

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def generate_response(self, *args, **kwargs):
        with self._obs.meter.observe("inference", "llm_generate"):
            with self._obs.span("agentar llm.generate", component="llm"):
                return self._inner.generate_response(*args, **kwargs)


class ObservableEmbedder:
    def __init__(self, inner: Any, obs: Observability):
        self._inner = inner
        self._obs = obs

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def embed(self, *args, **kwargs):
        with self._obs.meter.observe("inference", "embed"):
            with self._obs.span("agentar embedder.embed", component="embedder"):
                return self._inner.embed(*args, **kwargs)

    def embed_batch(self, *args, **kwargs):
        with self._obs.meter.observe("inference", "embed_batch"):
            with self._obs.span("agentar embedder.embed_batch", component="embedder"):
                return self._inner.embed_batch(*args, **kwargs)


class ObservableVectorStore:
    def __init__(self, inner: Any, obs: Observability):
        self._inner = inner
        self._obs = obs

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def search(self, *args, **kwargs):
        with self._obs.meter.observe("inference", "vector_search"):
            with self._obs.span("agentar vector.search", component="vector_store"):
                return self._inner.search(*args, **kwargs)

    def keyword_search(self, *args, **kwargs):
        with self._obs.meter.observe("inference", "vector_keyword_search"):
            with self._obs.span("agentar vector.keyword_search", component="vector_store"):
                result = self._inner.keyword_search(*args, **kwargs)
        if result is None:
            _record_outcome(self._obs.meter, "application", "keyword_none", OUTCOME_SUCCESS)
        return result

    def insert(self, *args, **kwargs):
        with self._obs.meter.observe("inference", "vector_insert"):
            with self._obs.span("agentar vector.insert", component="vector_store"):
                return self._inner.insert(*args, **kwargs)

    def update(self, *args, **kwargs):
        with self._obs.meter.observe("inference", "vector_update"):
            with self._obs.span("agentar vector.update", component="vector_store"):
                return self._inner.update(*args, **kwargs)


def wrap_memory_for_observation(memory: Any, obs: Observability) -> Any:
    """Construction-point injection (design §8.2): wrap the three external
    collaborators. Callers must do this inside the single instance factory
    so hot rebuilds re-apply the wrappers."""
    memory.llm = ObservableLLM(memory.llm, obs)
    memory.embedding_model = ObservableEmbedder(memory.embedding_model, obs)
    memory.vector_store = ObservableVectorStore(memory.vector_store, obs)
    return memory


def application_op(operation: str):
    """Decorate PowerMemory lifecycle methods: application metric with the
    designed outcome vocabulary (RememberResult.outcome: created/updated/
    noop; anything else counts as success) plus one INTERNAL span."""

    def decorator(fn):
        import functools

        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            meter = self._obs.meter
            span_cm = self._obs.span(f"agentar {operation}", operation=operation)
            with span_cm:
                with meter.application(operation) as marker:
                    result = fn(self, *args, **kwargs)
                    marker.outcome = getattr(result, "outcome", "success") or "success"
                    return result

        return wrapper

    return decorator


_shared: Optional[Observability] = None
_shared_lock = threading.Lock()


def shared_observability() -> Observability:
    """Process-wide default (one prometheus registration per metric name).
    Tests and multi-registry deployments construct their own Observability
    with a private Meter instead."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = Observability()
        return _shared
