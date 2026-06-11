"""``SourceDescriptorBuilder`` — auto-build a map descriptor from a live source.

``POST /sources/<id>/map`` accepts an optional hand-written ``descriptor``; when
it is omitted for an MCP source this builder produces one from the connector's
**live tool list** so a configured server is mappable with an empty body. The
composition is deliberately app-layer: the SCG engine (``mewbo_graph``) can
never import the MCP transport (``mewbo_tools``) — only an app may combine the
two (root CLAUDE.md layering DAG).

Security stance (spec §6, mirrors ``map_job.py``): the built descriptor is a
SCHEMA only — tool names, descriptions, and input schemas straight off the MCP
handshake. No token, credential, or connection header is ever copied into it;
``auth_scope`` (a redacted descriptor string) stays the caller's concern.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mewbo_core.common import get_logger

if TYPE_CHECKING:
    from mewbo_graph.scg.types import SourceDescriptor

logging = get_logger(name="api.agentic_search.scg.descriptors")


class SourceDescriptorBuilder:
    """Build a schema-only ``SourceDescriptor`` for one configured MCP server.

    State is the source identity plus the project CWD the merged MCP config is
    scoped to; :meth:`build` is the one behavior. Failure vocabulary the map
    route translates to HTTP: :class:`LookupError` — no configured connector
    for *source_id* (a 4xx, the caller must supply a descriptor);
    :class:`RuntimeError` — the optional deps are absent or the live
    introspection failed (a 5xx).
    """

    #: The provider the built descriptor dispatches to; ``raw`` carries the
    #: ``{"tools": [{name, description?, inputSchema?}]}`` shape it parses.
    SOURCE_TYPE = "mcp_tool_list"

    def __init__(self, source_id: str, *, project: str | None = None) -> None:
        """Capture the source identity + the config scope (DI, no I/O yet)."""
        self.source_id = source_id
        self.project = project

    def build(self) -> SourceDescriptor:
        """Return a :class:`SourceDescriptor` built from the live MCP tool list."""
        try:
            from mewbo_graph.scg.types import SourceDescriptor  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "SCG support requires the mewbo-graph library (the `wiki` extra)."
            ) from exc
        tools = self._fetch_tools()
        if not tools:
            raise RuntimeError(
                f"MCP server '{self.source_id}' advertised no tools to map."
            )
        return SourceDescriptor(
            source_id=self.source_id,
            source_type=self.SOURCE_TYPE,
            raw={"tools": tools},
        )

    def _fetch_tools(self) -> list[dict[str, Any]]:
        """List the server's tools through the public ``mewbo_tools`` seam.

        :func:`~mewbo_tools.integration.mcp.list_server_tool_schemas` wraps the
        merged-config read + pool connect + schema extraction. Its
        ``LookupError`` (server not configured) is re-raised with the map
        contract's wording — the route's 422; its ``RuntimeError`` (config
        unreadable / introspection failed) passes through — the route's 503.
        """
        try:
            from mewbo_tools.integration.mcp import (  # noqa: PLC0415
                list_server_tool_schemas,
            )
        except ImportError as exc:
            raise RuntimeError("MCP support is not installed (mewbo-tools).") from exc

        try:
            return list_server_tool_schemas(self.source_id, cwd=self.project)
        except LookupError as exc:
            raise LookupError(
                f"source '{self.source_id}' has no configured MCP connector; "
                "supply a descriptor to map it"
            ) from exc


__all__ = ["SourceDescriptorBuilder"]
