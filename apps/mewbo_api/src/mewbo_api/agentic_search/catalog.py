"""The source catalog + the source→tool scoping rule.

A "source" is one connector a workspace can search across. The catalog is the
**live configured MCP servers** (the same merged ``configs/mcp.json`` +
project ``.mcp.json`` chain every other Mewbo surface uses, read through the
tool registry) merged with the illustrative demo fixtures — the latter **only
while demo seeding is enabled** (:func:`store.seeding_enabled`). A production
install (``MEWBO_AGENTIC_SEARCH_SEED=0``) therefore lists exactly what is
really configured, never a mock.

Each source's concrete ``tool_ids`` resolve from the **live Source Capability
Graph** (SCG): a capability node (``kind == "capability"``) names exactly one
callable tool a source unlocks. Before a source is mapped into the SCG, a live
MCP server resolves to its registry tool ids (``mcp_<server>_*``) and a demo
fixture falls back to the ``tools`` declared beside it in
:data:`fixtures.SOURCE_CATALOG`. The resolved union is intersected with the
live tool registry via ``filter_specs()`` so a run only ever scopes
``allowed_tools`` to tools that actually exist.

The wire shape (:class:`SourceCatalogEntry`) and the :meth:`SourceCatalog.tools_for`
contract are fixed; only the resolution body lives here. A **configured** server
whose discovery failed stays listed ``available=False`` (+ ``unavailable_reason``),
never omitted, so the console can grey it out; a source that is neither configured
nor a demo fixture (seeding off) is simply not listed.
"""

from __future__ import annotations

from mewbo_core.config import get_merged_mcp_config
from mewbo_core.tool_registry import ToolRegistry, filter_specs, load_registry

from . import fixtures
from .schemas import SourceCatalogEntry
from .store import seeding_enabled

# Demo tool ids per source, projected from the fixtures catalog (the mock-data
# module) — the single source of truth, indexed here only for O(1) lookup.
_DEMO_TOOLS: dict[str, list[str]] = {
    row["id"]: list(row.get("tools", [])) for row in fixtures.SOURCE_CATALOG
}


class SourceCatalog:
    """Read-side façade resolving sources → tool ids over the live SCG."""

    @staticmethod
    def _configured_servers(project: str | None) -> list[str]:
        """Names of the MCP servers configured for *project* (config state).

        Reads the same merged global + subtree + CWD ``.mcp.json`` chain the
        registry builds from; tolerates the legacy ``mcpServers`` key. A config
        read failure degrades to an empty list, never an error.
        """
        try:
            merged = get_merged_mcp_config(project)
        except Exception:
            return []
        servers = merged.get("servers") or merged.get("mcpServers") or {}
        return list(servers) if isinstance(servers, dict) else []

    @staticmethod
    def _registry_servers(
        registry: ToolRegistry,
    ) -> tuple[dict[str, list[str]], dict[str, str]]:
        """Group the registry's MCP specs by server → (live tool ids, failure).

        Enabled specs feed the live tool-id map; a server whose specs are all
        disabled (the manifest keeps them with a ``disabled_reason`` when
        discovery fails) contributes only an ``unavailable_reason``.
        """
        live: dict[str, list[str]] = {}
        reasons: dict[str, str] = {}
        for spec in registry.list_specs(include_disabled=True):
            if spec.kind != "mcp":
                continue
            server = spec.metadata.get("server")
            if not isinstance(server, str) or not server:
                continue
            if spec.enabled:
                live.setdefault(server, []).append(spec.tool_id)
            else:
                reason = spec.metadata.get("disabled_reason")
                if isinstance(reason, str) and reason:
                    reasons.setdefault(server, reason)
        return live, reasons

    @classmethod
    def _scg_tool_ids(cls, source_id: str) -> list[str]:
        """Resolve one source's tool ids from its live SCG capability nodes.

        Capability nodes carry the concrete tool id in ``name``. SCG capability
        nodes live in the optional ``mewbo_graph`` library. A base (graph-less)
        install has none, so an absent import is treated the same as an empty
        SCG — the caller falls through to the live/demo resolution.
        """
        try:
            from mewbo_graph.scg.store import get_scg_store  # noqa: PLC0415

            nodes = get_scg_store().query_nodes(source_id=source_id, kind="capability")
        except ImportError:
            nodes = []
        seen: set[str] = set()
        ordered: list[str] = []
        for node in nodes:
            if node.name not in seen:
                seen.add(node.name)
                ordered.append(node.name)
        return ordered

    @classmethod
    def _source_tool_ids(
        cls,
        source_id: str,
        live: dict[str, list[str]],
        *,
        demo_fallback: bool = True,
    ) -> list[str]:
        """Resolve one source's tool ids: SCG nodes, else live server, else demo.

        The ONE resolution rule both ``entries`` and ``tools_for`` apply, so a
        source never resolves differently between the catalog and a run grant.
        When the SCG has no capability nodes for *source_id*, a live configured
        MCP server resolves to its registry tool ids; otherwise fall back to the
        fixtures demo tools — but only while demo seeding is enabled AND
        *demo_fallback* is on. Callers pass ``demo_fallback=False`` for a
        **configured** server id: a configured-but-discovery-failed server must
        report no tools (greyed out / no grant), never a demo fixture that
        happens to share its id.
        """
        ordered = cls._scg_tool_ids(source_id)
        if ordered:
            return ordered
        if source_id in live:
            return list(live[source_id])
        if demo_fallback and seeding_enabled():
            return list(_DEMO_TOOLS.get(source_id, []))
        return []

    @classmethod
    def entries(cls, project: str | None = None) -> list[SourceCatalogEntry]:
        """Return the catalog, optionally scoped to *project*.

        Live configured MCP servers come first (``id`` = server name,
        ``source_type`` = the MCP descriptor kind); the demo fixtures are merged
        after them **only while demo seeding is on**, skipping any id a live
        server already claims. A source that resolves to **zero** tool ids is
        returned with ``available=False`` + ``unavailable_reason`` rather than
        omitted, so the console can grey it out instead of dropping a persisted
        workspace source.
        """
        registry = load_registry(cwd=project)
        live, reasons = cls._registry_servers(registry)
        entries: list[SourceCatalogEntry] = []
        seen: set[str] = set()
        for server in [*cls._configured_servers(project), *live]:
            if server in seen:
                continue
            seen.add(server)
            tool_ids = cls._source_tool_ids(server, live, demo_fallback=False)
            available = bool(tool_ids)
            entries.append(
                SourceCatalogEntry(
                    id=server,
                    name=server,
                    glyph=(server[:1].upper() or "?"),
                    desc="Configured MCP server.",
                    source_type="mcp_tool_list",
                    available=available,
                    unavailable_reason=(
                        None
                        if available
                        else reasons.get(
                            server, "MCP server configured but no tools discovered."
                        )
                    ),
                    tool_ids=tool_ids,
                )
            )
        if not seeding_enabled():
            return entries
        for raw in fixtures.SOURCE_CATALOG:
            if raw["id"] in seen:
                continue
            tool_ids = cls._source_tool_ids(raw["id"], live)
            available = bool(tool_ids)
            entries.append(
                SourceCatalogEntry(
                    id=raw["id"],
                    name=raw["name"],
                    color=raw.get("color", "#ffffff"),
                    bg=raw.get("bg", "#191919"),
                    glyph=raw.get("glyph", "?"),
                    desc=raw.get("desc", ""),
                    available=available,
                    unavailable_reason=(
                        None if available else "No capabilities indexed for this source."
                    ),
                    tool_ids=tool_ids,
                )
            )
        return entries

    @classmethod
    def tools_for(cls, source_ids: list[str], project: str | None = None) -> list[str]:
        """Return the de-duplicated union of tool ids *source_ids* unlock.

        The rule a run applies to scope ``allowed_tools``: each source resolves
        through :meth:`_source_tool_ids` (SCG capability nodes, else the live
        MCP server's registry tool ids, else — for a *non-configured* id only —
        the demo fallback while seeding is on), the per-source
        results are unioned in selection order, then intersected with
        ``filter_specs()`` registry availability. The catalog union is the upper
        bound, not the final grant.
        """
        registry = load_registry(cwd=project)
        live, _ = cls._registry_servers(registry)
        configured = set(cls._configured_servers(project))
        seen: set[str] = set()
        union: list[str] = []
        for sid in source_ids:
            for tool_id in cls._source_tool_ids(
                sid, live, demo_fallback=sid not in configured
            ):
                if tool_id not in seen:
                    seen.add(tool_id)
                    union.append(tool_id)
        if not union:
            return []
        specs = filter_specs(registry.list_specs(), allowed=union)
        available = {spec.tool_id for spec in specs}
        return [tool_id for tool_id in union if tool_id in available]


__all__ = ["SourceCatalog"]
