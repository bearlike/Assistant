"""The source catalog + the source→tool scoping rule.

A "source" is one connector a workspace can search across. The catalog resolves
each source's concrete ``tool_ids`` from the **live Source Capability Graph**
(SCG): a capability node (``kind == "capability"``) names exactly one callable
tool a source unlocks. The resolved union is intersected with the live tool
registry via ``filter_specs()`` so a run only ever scopes ``allowed_tools`` to
tools that actually exist.

Before a source is mapped into the SCG, resolution falls back to the
illustrative ``tools`` declared beside the source in :data:`fixtures.SOURCE_CATALOG`
— but **only while demo seeding is enabled** (:func:`store.seeding_enabled`).
A production install (``MEWBO_AGENTIC_SEARCH_SEED=0``) therefore reports an
unmapped source as ``available=False`` ("not yet indexed") rather than serving a
hardcoded guess. The fallback data lives with the source it describes (the mock
catalog), never as a constant in this resolver.

The wire shape (:class:`SourceCatalogEntry`) and the :meth:`SourceCatalog.tools_for`
contract are fixed; only the resolution body lives here. Unconfigured sources are
returned with ``available=False`` (+ ``unavailable_reason``), never omitted, so
the console can grey out a persisted workspace source instead of dropping it.
"""

from __future__ import annotations

from mewbo_core.tool_registry import filter_specs, load_registry

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

    @classmethod
    def _source_tool_ids(cls, source_id: str) -> list[str]:
        """Resolve one source's tool ids: SCG capability nodes, else demo fallback.

        Capability nodes carry the concrete tool id in ``name``. When the SCG
        has no capability nodes for *source_id*, fall back to the fixtures demo
        tools — but only while demo seeding is enabled, so a production install
        reports an unmapped source as having no tools rather than a guess.
        """
        seen: set[str] = set()
        ordered: list[str] = []
        # SCG capability nodes live in the optional ``mewbo_graph`` library. A
        # base (graph-less) install has none, so an absent import is treated the
        # same as an empty SCG — fall through to the demo/empty resolution.
        try:
            from mewbo_graph.scg.store import get_scg_store  # noqa: PLC0415

            nodes = get_scg_store().query_nodes(source_id=source_id, kind="capability")
        except ImportError:
            nodes = []
        for node in nodes:
            if node.name not in seen:
                seen.add(node.name)
                ordered.append(node.name)
        if ordered:
            return ordered
        if seeding_enabled():
            return list(_DEMO_TOOLS.get(source_id, []))
        return []

    @classmethod
    def _available_tool_ids(cls, candidates: list[str], project: str | None) -> set[str]:
        """Intersect *candidates* with the live registry via ``filter_specs()``.

        ``filter_specs(allowed=...)`` keeps only specs whose ``tool_id`` is in
        the candidate union and applies the config denylist — the same scope rule
        the orchestrator and ``spawn_agent`` use. The registry is loaded scoped to
        the project's CWD so project ``.mcp.json`` tools are visible.
        """
        if not candidates:
            return set()
        registry = load_registry(cwd=project)
        specs = filter_specs(registry.list_specs(), allowed=candidates)
        return {spec.tool_id for spec in specs}

    @classmethod
    def entries(cls, project: str | None = None) -> list[SourceCatalogEntry]:
        """Return the catalog, optionally scoped to *project*.

        Each entry's ``tool_ids`` is the source's resolved tools (SCG capability
        nodes, else the demo fallback while seeding is on). A source that resolves
        to **zero** tool ids is returned with ``available=False`` +
        ``unavailable_reason`` rather than omitted, so the console can grey it out
        instead of dropping a persisted workspace source.
        """
        entries: list[SourceCatalogEntry] = []
        for raw in fixtures.SOURCE_CATALOG:
            tool_ids = cls._source_tool_ids(raw["id"])
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
        from its live SCG capability nodes (or the demo fallback while unmapped +
        seeding on), the per-source results are unioned in selection order, then
        intersected with ``filter_specs()`` registry availability. The catalog
        union is the upper bound, not the final grant.
        """
        seen: set[str] = set()
        union: list[str] = []
        for sid in source_ids:
            for tool_id in cls._source_tool_ids(sid):
                if tool_id not in seen:
                    seen.add(tool_id)
                    union.append(tool_id)
        available = cls._available_tool_ids(union, project)
        return [tool_id for tool_id in union if tool_id in available]


__all__ = ["SourceCatalog"]
