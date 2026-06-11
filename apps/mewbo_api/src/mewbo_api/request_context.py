"""Per-request client-surface helper — the single ``X-Mewbo-Surface`` reader.

A leaf module (imports only Flask) so every route module AND ``backend`` can share
one implementation without a back-edge: ``backend`` imports the route modules, so
the route modules cannot import ``backend``; this neutral home breaks that cycle.
The value rides into the trace via ``source_platform`` → ``TraceProvenance``.
"""
from __future__ import annotations

from flask import request


def request_surface() -> str:
    """Originating client surface from the ``X-Mewbo-Surface`` header.

    HTTP clients (console, MCP, Home Assistant) send this on EVERY request, so
    reading it at each run-start keeps followups/recovery tagged, not just the
    first turn. Defaults to ``"api"`` for a raw caller that sends no header; the
    MCP client sends ``"mcp"``.
    """
    return request.headers.get("X-Mewbo-Surface", "").strip() or "api"


__all__ = ["request_surface"]
