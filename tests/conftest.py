import logging
import sys
from pathlib import Path

# Server modules (server/main.py et al.) import their siblings top-level
# (`import telemetry`, `import db`), so the server directory itself must be on
# sys.path — regardless of which test file runs first.
_SERVER_DIR = str(Path(__file__).resolve().parent.parent / "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)


# Server tests run without an app database: every TestClient request makes the
# request-log middleware attempt (and fail) a DB write. server/main.py already
# circuit-breaks and rate-limits those failures, but each test case reloads the
# module and resets that state — silence the known noise on the root logger
# (root filters survive module reloads) so real errors stay visible.
class _SilenceRequestLogPersist(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "Failed to persist request log" not in record.getMessage()


logging.getLogger().addFilter(_SilenceRequestLogPersist())
