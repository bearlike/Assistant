"""MCP-tool-list structure provider — pure dict parsing, never a network.

Parses an MCP server's advertised tool list
(``descriptor.raw["tools"]`` — a list of ``{name, description, inputSchema,
outputSchema?}`` dicts, the shape ``MultiServerMCPClient`` discovers) into the
SCG fabric. A flat tool list has no entity hierarchy, so each tool maps to one
``capability`` node keyed ``<source_id>#<tool_name>`` (spec §16-1):

* ``inputSchema`` properties → :class:`CapabilityBinding` (in ``required`` ⇒
  ``bound``, else ``optional``) + a ``SUPPORTS_QUERY`` edge per field,
* ``outputSchema`` properties → ``PRODUCES`` edges so the parser's In-N-Out
  producer→consumer join can wire cross-capability param edges later.

Each capability is linked to the source via ``HAS_ENTITY`` so the router can
reach it. No argument *values* are touched — only schema field names + docs.
"""

from __future__ import annotations

from ..types import (
    CapabilityBinding,
    ScgEdge,
    ScgNode,
    SourceDescriptor,
    StructureGraph,
)

# Coarse operator assumption for an MCP input field (no in:query distinction).
_TOOL_OPERATORS = ["eq"]


class McpToolListStructureProvider:
    """Parse an MCP tool-list ``descriptor.raw`` into a StructureGraph."""

    source_type = "mcp_tool_list"

    def build_structure(self, descriptor: SourceDescriptor) -> StructureGraph:
        """Build a capability-per-tool subgraph for one MCP server source."""
        source_id = descriptor.source_id
        nodes: list[ScgNode] = [
            ScgNode(
                source_key=source_id,
                kind="source",
                source_id=source_id,
                name=source_id,
            )
        ]
        edges: list[ScgEdge] = []

        for tool in self._tools(descriptor.raw):
            cap_node, cap_edges = self._capability(source_id, tool)
            if cap_node is None:
                continue
            nodes.append(cap_node)
            edges.extend(cap_edges)

        return StructureGraph(nodes=nodes, edges=edges)

    # -- capability ---------------------------------------------------------

    @classmethod
    def _capability(
        cls, source_id: str, tool: dict[str, object]
    ) -> tuple[ScgNode | None, list[ScgEdge]]:
        """Build (capability node, edges) for one tool, or (None, []) if unnamed."""
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            return None, []
        cap_key = f"{source_id}#{name}"

        bindings: list[CapabilityBinding] = []
        edges: list[ScgEdge] = [
            ScgEdge(source=source_id, target=cap_key, kind="HAS_ENTITY")
        ]

        input_props, required = cls._schema_props(tool.get("inputSchema"))
        for field_name in input_props:
            field_key = f"{cap_key}.{field_name}"
            bindings.append(
                CapabilityBinding(
                    field_key=field_key,
                    mode="bound" if field_name in required else "optional",
                    operators=list(_TOOL_OPERATORS),
                )
            )
            edges.append(
                ScgEdge(source=cap_key, target=field_key, kind="SUPPORTS_QUERY")
            )

        output_props, _ = cls._schema_props(tool.get("outputSchema"))
        for field_name in output_props:
            edges.append(
                ScgEdge(
                    source=cap_key,
                    target=f"{cap_key}.{field_name}",
                    kind="PRODUCES",
                )
            )

        return (
            ScgNode(
                source_key=cap_key,
                kind="capability",
                source_id=source_id,
                name=name,
                doc=cls._doc(tool),
                bindings=bindings,
            ),
            edges,
        )

    # -- raw-dict accessors -------------------------------------------------

    @staticmethod
    def _tools(raw: dict[str, object]) -> list[dict[str, object]]:
        """Return the tool dicts under ``raw["tools"]`` (skips malformed entries)."""
        tools = raw.get("tools")
        if not isinstance(tools, list):
            return []
        return [t for t in tools if isinstance(t, dict)]

    @staticmethod
    def _schema_props(schema: object) -> tuple[list[str], set[str]]:
        """Return (property names in declared order, required-name set)."""
        if not isinstance(schema, dict):
            return [], set()
        props = schema.get("properties")
        names = [str(k) for k in props] if isinstance(props, dict) else []
        req = schema.get("required")
        required = {str(r) for r in req} if isinstance(req, list) else set()
        return names, required

    @staticmethod
    def _doc(tool: dict[str, object]) -> str:
        """Return the tool's description string, or ''."""
        desc = tool.get("description")
        return desc if isinstance(desc, str) else ""


__all__ = ["McpToolListStructureProvider"]
