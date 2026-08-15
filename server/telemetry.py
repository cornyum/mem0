"""Local-only deployment marker (privatized build).

The upstream build sent two anonymous onboarding events to a hosted PostHog
project. This privatized deployment must not talk to any public endpoint, so
all network reporting has been removed. The public function names are kept so
callers in auth routers keep working; they now only record the event locally
in the telemetry state file for troubleshooting purposes.

Events recorded locally (never sent off-box):
- `admin_registered` — when the first admin account is created.
- `onboarding_completed` — when the setup wizard reaches its final success state.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

STATE_PATH = Path(os.environ.get("MEM0_TELEMETRY_STATE_PATH", "/app/history/telemetry.json"))

_lock = Lock()
_dashboard_nudge_logged = False


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state))
    except OSError:
        logging.exception("telemetry: failed to persist state")


def _record_once(email: str, event: str, state_key: str, extra: dict[str, Any] | None = None) -> None:
    with _lock:
        state = _load_state()
        if state.get(state_key):
            return
        state[state_key] = datetime.now(timezone.utc).isoformat()
        _save_state(state)


def log_status() -> None:
    logging.info("telemetry: privatized build — no events leave this machine.")


def capture_admin_registered(email: str) -> None:
    _record_once(email, "admin_registered", "admin_registered_sent_at")


def capture_onboarding_completed(email: str, use_case: str) -> None:
    _record_once(email, "onboarding_completed", "onboarding_sent_at", {"use_case": use_case})


def log_dashboard_nudge_once(dashboard_url: str) -> None:
    """Log a hint pointing the operator to the web dashboard the first time a memory
    is stored. LOCAL console log only — sends nothing off-box.
    """
    global _dashboard_nudge_logged
    if _dashboard_nudge_logged:
        return
    _dashboard_nudge_logged = True
    logging.info(
        "First memory stored. Open the dashboard at %s to view and manage your memories.",
        dashboard_url,
    )
