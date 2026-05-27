"""Configuration for the Mewbo MCP server.

All settings come from environment variables with sane defaults so the
server runs out-of-the-box against a local ``mewbo-api`` (default port
5124). No secrets are baked in — the caller's own Bearer token is what
authenticates every REST call (see ``rest.py``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# The REST API's own default bind port (see apps/mewbo_api/backend.py:main).
_DEFAULT_API_URL = "http://localhost:5124"


@dataclass(frozen=True, slots=True)
class McpConfig:
    """Resolved configuration for the MCP server process."""

    api_url: str
    host: str
    port: int

    @classmethod
    def from_env(cls) -> McpConfig:
        """Build the configuration from environment variables.

        - ``MEWBO_API_URL`` — base URL of the REST API (default
          ``http://localhost:5124``). A trailing slash is stripped so callers
          can join paths uniformly.
        - ``MEWBO_MCP_HOST`` — bind host for the MCP server (default
          ``127.0.0.1``).
        - ``MEWBO_MCP_PORT`` — bind port for the MCP server (default ``5127``).
          Deliberately not ``5125`` — that is the API's gunicorn port in Docker
          (``API_PORT=5125``), so sharing it would clash when both run together.
        """
        api_url = os.environ.get("MEWBO_API_URL", _DEFAULT_API_URL).rstrip("/")
        host = os.environ.get("MEWBO_MCP_HOST", "127.0.0.1")
        port = int(os.environ.get("MEWBO_MCP_PORT", "5127"))
        return cls(api_url=api_url, host=host, port=port)


__all__ = ["McpConfig"]
