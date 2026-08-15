"""Readiness registry: three-state aggregation and TTL caching."""

import time

from mem0.context.readiness import (
    DEGRADED,
    NOT_READY,
    READY,
    Probe,
    ReadinessRegistry,
)


class _StaticProbe(Probe):
    def __init__(self, name, blocking, healthy_fn):
        super().__init__(name, blocking=blocking)
        self._healthy_fn = healthy_fn
        self.calls = 0

    def check(self):
        self.calls += 1
        if not self._healthy_fn():
            raise RuntimeError("unhealthy")


def test_all_healthy_is_ready():
    registry = ReadinessRegistry()
    registry.register(_StaticProbe("app_db", True, lambda: True))
    registry.register(_StaticProbe("llm", False, lambda: True))
    assert registry.evaluate().state == READY


def test_nonblocking_failure_is_degraded_not_not_ready():
    registry = ReadinessRegistry()
    registry.register(_StaticProbe("app_db", True, lambda: True))
    registry.register(_StaticProbe("llm", False, lambda: False))
    report = registry.evaluate()
    assert report.state == DEGRADED
    assert {c.name for c in report.checks if c.state == "unavailable"} == {"llm"}


def test_blocking_failure_is_not_ready():
    registry = ReadinessRegistry()
    registry.register(_StaticProbe("app_db", True, lambda: False))
    report = registry.evaluate()
    assert report.state == NOT_READY


def test_results_are_cached_per_ttl():
    probe = _StaticProbe("app_db", True, lambda: True)
    registry = ReadinessRegistry()
    registry.register(probe)
    registry.evaluate()
    registry.evaluate()
    assert probe.calls == 1  # second read served from the success cache


def test_failed_probe_rechecked_after_fail_ttl(monkeypatch):
    probe = _StaticProbe("app_db", True, lambda: False)
    registry = ReadinessRegistry()
    registry.register(probe)
    registry.evaluate()
    assert probe.calls == 1

    # Advance past the fail TTL: the probe must be re-run.
    import mem0.context.readiness as readiness

    cached = registry._probes[0]
    monkeypatch.setattr(cached, "_checked_at", time.monotonic() - (readiness.FAIL_TTL_SECONDS + 1))
    registry.evaluate()
    assert probe.calls == 2


def test_slow_probe_times_out():

    class _SlowProbe(Probe):
        def __init__(self):
            super().__init__("slow", blocking=True)

        def check(self):
            time.sleep(5)

    registry = ReadinessRegistry()
    registry.register(_SlowProbe())
    report = registry.evaluate()
    assert report.state == NOT_READY
