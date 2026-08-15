import sys
from pathlib import Path

# Server modules (server/main.py et al.) import their siblings top-level
# (`import telemetry`, `import db`), so the server directory itself must be on
# sys.path — regardless of which test file runs first.
_SERVER_DIR = str(Path(__file__).resolve().parent.parent / "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

# server/main.py refuses to import without a JWT secret unless auth is
# explicitly disabled; local runs of the router suites get a harmless default
# so the import succeeds (deployments still must set a real secret).
import os as _os  # noqa: E402

_os.environ.setdefault("JWT_SECRET", "unit-test-secret-not-for-production")
