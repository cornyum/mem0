"""Three-state readiness (design §3.5).

ready        — all probes healthy
degraded     — a non-blocking probe (model tier) is down; memory operations
               that need it report capability errors, everything else serves
not_ready    — a blocking probe (authoritative store / vector tier) is down;
               the server should be pulled from traffic

Probe results are cached: success for ``OK_TTL`` seconds, failure for
``FAIL_TTL`` — a flapping dependency does not translate into a request-time
probe storm, and no request path ever performs a probe inline. Details of
probe failures stay in logs; they are never serialized into API responses.
"""

import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_SECONDS = 2.0
OK_TTL_SECONDS = 300.0
FAIL_TTL_SECONDS = 30.0

READY = "ready"
DEGRADED = "degraded"
NOT_READY = "not_ready"


@dataclass(frozen=True)
class ProbeStatus:
    name: str
    blocking: bool
    state: str  # ready | unavailable


@dataclass(frozen=True)
class ReadinessReport:
    state: str  # ready | degraded | not_ready
    checks: list[ProbeStatus]


class Probe(ABC):
    """One dependency check. ``check`` raises on failure.

    ``run`` enforces the timeout with a daemon thread: a hung database
    call must never hang /health/ready along with it, and the daemon flag
    keeps a wedged probe from blocking interpreter shutdown."""

    def __init__(self, name: str, *, blocking: bool):
        self.name = name
        self.blocking = blocking

    @abstractmethod
    def check(self) -> None:
        raise NotImplementedError

    def run(self) -> bool:
        outcome: dict[str, bool] = {}

        def _target():
            try:
                self.check()
                outcome["ok"] = True
            except Exception:
                logger.warning("Readiness probe %s failed", self.name, exc_info=True)
                outcome["ok"] = False

        thread = threading.Thread(target=_target, daemon=True, name=f"readiness-{self.name}")
        thread.start()
        thread.join(PROBE_TIMEOUT_SECONDS)
        if thread.is_alive():
            logger.warning("Readiness probe %s timed out after %.1fs", self.name, PROBE_TIMEOUT_SECONDS)
            return False
        return outcome.get("ok", False)


class CachedProbe:
    """TTL cache around a Probe — per-process, refreshed lazily on read."""

    def __init__(self, probe: Probe):
        self.probe = probe
        self._cached: Optional[bool] = None
        self._checked_at: float = 0.0

    def healthy(self) -> bool:
        now = time.monotonic()
        ttl = OK_TTL_SECONDS if self._cached else FAIL_TTL_SECONDS
        if self._cached is not None and (now - self._checked_at) < ttl:
            return self._cached
        self._cached = self.probe.run()
        self._checked_at = now
        return self._cached


class ReadinessRegistry:
    """Aggregates cached probes into the three-state verdict."""

    def __init__(self):
        self._probes: list[CachedProbe] = []

    def register(self, probe: Probe) -> None:
        self._probes.append(CachedProbe(probe))

    def evaluate(self) -> ReadinessReport:
        checks = [
            ProbeStatus(name=p.probe.name, blocking=p.probe.blocking, state="ready" if p.healthy() else "unavailable")
            for p in self._probes
        ]
        if any(c.blocking and c.state != "ready" for c in checks):
            state = NOT_READY
        elif any(c.state != "ready" for c in checks):
            state = DEGRADED
        else:
            state = READY
        return ReadinessReport(state=state, checks=checks)
