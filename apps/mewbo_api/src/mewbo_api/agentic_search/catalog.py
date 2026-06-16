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
from mewbo_core.tool_registry import (
    ToolRegistry,
    filter_specs,
    load_registry,
    mcp_tool_id,
)

from . import fixtures
from .schemas import SourceCatalogEntry
from .store import seeding_enabled

# Demo tool ids per source, projected from the fixtures catalog (the mock-data
# module) — the single source of truth, indexed here only for O(1) lookup.
_DEMO_TOOLS: dict[str, list[str]] = {
    row["id"]: list(row.get("tools", [])) for row in fixtures.SOURCE_CATALOG
}

# Write-capable connector verbs a SEARCH grant must NOT bind. The EVIDENCE: a
# failed-map source fell through to the live branch and bound ALL ~51 raw
# registry tools — incl. ``create_repo`` / ``delete_branch`` / ``wiki_write``.
# A search run reads; it never mutates a connector. This is a conservative
# NAME-VERB filter dropping the obvious mutators while keeping every read verb
# (``get_`` / ``list_`` / ``search_`` / ``read_`` / ``fetch_``). An UNKNOWN
# verb is KEPT (default-allow on read surfaces) — the filter only removes verbs
# it is confident are writes, never a maybe.
#
# A verb at the FRONT (``create_repo``) is the established action position, so
# the leading-token set is broad. A verb at the BACK (``wiki_write``,
# ``file_delete``) is also a write, but the trailing-token set is kept STRICT —
# only the unambiguous action words — because many read tools END in a noun
# that doubles as a verb (``get_latest_release`` → ``release``; ``list_tags`` →
# ``tags``) and must NOT be dropped.
_WRITE_VERB_PREFIXES: frozenset[str] = frozenset(
    {
        "create",
        "update",
        "delete",
        "write",
        "edit",
        "remove",
        "add",
        "set",
        "put",
        "patch",
        "post",
        "push",
        "merge",
        "fork",
        "cancel",
        "trigger",
        "rename",
        "move",
        "upload",
        "send",
        "publish",
        "archive",
        "revert",
        "reset",
        "drop",
        "destroy",
        "insert",
        "modify",
        "dispatch",
        "approve",
        "dismiss",
    }
)
# Unambiguous action words that signal a write even in trailing position.
_WRITE_VERB_SUFFIXES: frozenset[str] = frozenset(
    {"write", "delete", "create", "update", "remove", "edit", "publish", "upload"}
)


def _is_write_tool(tool_id: str) -> bool:
    """True when *tool_id* names an obviously write-capable connector verb.

    The action word in the ``mcp_<server>_<verb>_<obj>`` convention is usually
    the FIRST token after the ``mcp_<server>_`` prefix (``mcp_gitea_create_branch``
    → ``create``) but some connectors put it LAST (``mcp_gitea_wiki_write`` →
    ``write``). The leading token is matched against the broad write set; the
    trailing token only against the strict, unambiguous suffix set (so a read
    tool ending in a verb-noun like ``get_latest_release`` is NOT dropped).
    Conservative: a read-or-ambiguous tool is never denied a search.
    """
    body = tool_id
    if body.startswith("mcp_"):
        # Strip ``mcp_`` then the server segment — the verb is what's left.
        rest = body[4:]
        parts = rest.split("_", 1)
        body = parts[1] if len(parts) == 2 else rest
    tokens = body.lower().split("_")
    if not tokens:
        return False
    return tokens[0] in _WRITE_VERB_PREFIXES or tokens[-1] in _WRITE_VERB_SUFFIXES


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
    def _scg_tool_ids(
        cls, source_id: str, *, available: set[str] | None = None
    ) -> list[str]:
        """Resolve one source's tool ids from its live SCG capability nodes.

        A capability node's ``name`` is the connector's RAW MCP tool name (e.g.
        ``search_repos``), NOT the registry id (``mcp_<server>_<tool>`` minted
        by ``mcp_tool_id``). The GRANT-INVERSION bug (run-797097e4b1): returning
        the raw name made the ``tools_for`` ∩ ``filter_specs`` intersection
        DELETE every successfully-mapped source's tools, while a failed-map
        source fell through to the live branch and bound the full raw registry.
        The identical translation already shipped for probe spawns in
        ``plugins/scg/route.py`` (``mcp_tool_id(sid, name)``) — mirror it here so
        the catalog and the route agree.

        Each node yields the canonical registry id ``mcp_tool_id(source_id,
        name)``. When an *available* set is supplied (the registry's live ids)
        the minted id is preferred only if it actually exists; otherwise the raw
        ``name`` is kept when IT exists (a node already carrying a built-in /
        full registry id, the shape the existing fixtures use); with neither
        present the minted id is the best-effort grant the ``filter_specs``
        intersection then drops. Ordering is preserved.

        SCG capability nodes live in the optional ``mewbo_graph`` library. A
        base (graph-less) install has none, so an absent import is treated the
        same as an empty SCG — the caller falls through to live/demo resolution.
        """
        try:
            from mewbo_graph.scg.store import get_scg_store  # noqa: PLC0415

            nodes = get_scg_store().query_nodes(source_id=source_id, kind="capability")
        except ImportError:
            nodes = []
        seen: set[str] = set()
        ordered: list[str] = []
        for node in nodes:
            tool_id = cls._registry_id_for(source_id, node.name, available)
            if tool_id not in seen:
                seen.add(tool_id)
                ordered.append(tool_id)
        return ordered

    @staticmethod
    def _registry_id_for(
        source_id: str, node_name: str, available: set[str] | None
    ) -> str:
        """Map a capability node ``name`` onto its canonical registry tool id.

        Prefers ``mcp_tool_id(source_id, node_name)`` (the core id convention) —
        the GRANT-INVERSION fix. When the *available* registry-id set is known,
        the raw ``node_name`` is kept instead iff the minted id is absent but the
        raw name IS a real id (a node already carrying a built-in / full registry
        id — the existing fixture shape, e.g. ``read_file``). With no *available*
        set the minted id is returned unconditionally (the ``filter_specs``
        intersection downstream resolves it).
        """
        minted = mcp_tool_id(source_id, node_name)
        if available is not None and minted not in available and node_name in available:
            return node_name
        return minted

    @classmethod
    def _source_tool_ids(
        cls,
        source_id: str,
        live: dict[str, list[str]],
        *,
        demo_fallback: bool = True,
        available: set[str] | None = None,
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
        happens to share its id. *available* (the registry's live ids) lets the
        SCG resolution pick the registry-valid form of a capability id.
        """
        ordered = cls._scg_tool_ids(source_id, available=available)
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
        avail_ids = {spec.tool_id for spec in registry.list_specs()}
        entries: list[SourceCatalogEntry] = []
        seen: set[str] = set()
        for server in [*cls._configured_servers(project), *live]:
            if server in seen:
                continue
            seen.add(server)
            tool_ids = cls._source_tool_ids(
                server, live, demo_fallback=False, available=avail_ids
            )
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
            tool_ids = cls._source_tool_ids(raw["id"], live, available=avail_ids)
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
        """Return the de-duplicated union of READ tool ids *source_ids* unlock.

        The rule a run applies to scope ``allowed_tools``: each source resolves
        through :meth:`_source_tool_ids` (SCG capability nodes minted onto
        registry ids, else the live MCP server's registry tool ids, else — for a
        *non-configured* id only — the demo fallback while seeding is on), the
        per-source results are unioned in selection order, then intersected with
        ``filter_specs()`` registry availability. The catalog union is the upper
        bound, not the final grant.

        SEARCH grants are READ-ONLY: the union is filtered through
        :func:`_is_write_tool` so an obviously write-capable connector verb
        (``create_*`` / ``delete_*`` / ``push_*`` …) is dropped — a search run
        reads a connector, it never mutates it (the EVIDENCE: a failed-map
        source bound ``create_repo`` / ``delete_branch``). The traversal verbs a
        graph drive also needs are unioned later by ``WorkspaceGraphBinding``,
        not here; this seam stays the pure connector read-grant.
        """
        registry = load_registry(cwd=project)
        live, _ = cls._registry_servers(registry)
        avail_ids = {spec.tool_id for spec in registry.list_specs()}
        configured = set(cls._configured_servers(project))
        seen: set[str] = set()
        union: list[str] = []
        for sid in source_ids:
            for tool_id in cls._source_tool_ids(
                sid, live, demo_fallback=sid not in configured, available=avail_ids
            ):
                if tool_id not in seen:
                    seen.add(tool_id)
                    union.append(tool_id)
        if not union:
            return []
        specs = filter_specs(registry.list_specs(), allowed=union)
        available = {spec.tool_id for spec in specs}
        return [
            tool_id
            for tool_id in union
            if tool_id in available and not _is_write_tool(tool_id)
        ]


__all__ = ["SourceCatalog"]
