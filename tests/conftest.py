import sys
from pathlib import Path

# Server modules (server/main.py et al.) import their siblings top-level
# (`import telemetry`, `import db`), so the server directory itself must be on
# sys.path — regardless of which test file runs first.
_SERVER_DIR = str(Path(__file__).resolve().parent.parent / "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)
