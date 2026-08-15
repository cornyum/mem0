"""Standalone MCP service entrypoint (design §6.2 deployment form).

The fastmcp transport needs its own ASGI lifespan, so the MCP projection
runs as a dedicated service sharing the same image/env as the REST server:

    uvicorn mcp_standalone:app --host 0.0.0.0 --port 9000
"""

from mcp_server import create_standalone_app

app = create_standalone_app()
