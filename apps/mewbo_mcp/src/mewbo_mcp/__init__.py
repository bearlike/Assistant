"""Mewbo MCP — a standalone MCP server wrapping the Mewbo REST API.

Exposes Mewbo *above* its REST API so external agents can create/control
sessions, read session history (tiered), and query the Agentic Wiki. The
server speaks streamable-HTTP MCP and forwards the caller's own Bearer token
to the REST API as ``X-API-Key`` (token pass-through — no privileged service
identity).
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.0.13"
