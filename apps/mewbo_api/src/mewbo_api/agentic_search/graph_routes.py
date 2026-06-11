"""Flask-RESTX namespace for the workspace SCG graph view (#79).

Endpoint under ``/api/agentic_search``:

- ``GET /workspaces/<id>/graph`` — the layer-tagged nodes/edges projection of
  the workspace-scoped SCG multiplex (schema + memory + entity layers), wire
  shape mirroring the wiki ``/v1/wiki/projects/<slug>/graph`` endpoint so the
  console reuses the same ``KnowledgeGraphRenderer`` mechanism.

The view assembler is :class:`~mewbo_graph.scg.graph_view.ScgGraphView` (the #76
multiplex twin of the wiki ``KnowledgeGraphView``). This module is the thin
**typed** transport wrapper around it — every payload is a Pydantic wire model
(:class:`WorkspaceGraphWire` and its node/edge/stats parts), mirroring the
console ``WorkspaceGraph`` type 1:1; the only ``dict`` boundary is parsing the
view's self-contained ``to_wire()`` output, which is immediately validated into
these models.

The wrapper resolves the workspace's enabled-source scope (the #75 grant
semantics: ``WorkspaceMcpConfig.attached_server_names`` first, falling back to
``Workspace.sources``) and adds the two FE affordances the view is intentionally
agnostic about:

* **Edge-endpoint normalization.** ``ScgGraphView``'s *schema* edges address
  their endpoints by ``source_key`` (the SCG edge addressing) while every node's
  cytoscape ``id`` is its ``node_id``. The console renderer joins edges to nodes
  by ``id``, so each schema edge's ``source``/``target`` is remapped from
  ``source_key`` → the owning node's ``node_id`` here, dropping any edge whose
  endpoint isn't a real node in the payload (no dangling edges). Memory/cross
  edges already address by ``node_id`` and pass through untouched.
* **Unmapped-source ghost nodes.** A workspace source with NO SCG schema nodes
  (never mapped) is surfaced as a single ``unmapped`` ghost node so the FE can
  render a "map this source" hint instead of silently omitting it.

Degrades gracefully — an unmapped workspace, a disabled SCG, or an absent graph
library yields the schema layer empty + every source listed as ``unmapped``,
NEVER a 500/503. Only a missing workspace 404s.

Security (projection contract): ``auth_scope`` is already redacted off the wire
by ``ScgGraphView``; this wrapper never reads secrets — the schema layer carries
only redacted descriptors, and nodes/edges expose no token, credential, or
record value.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from flask_restx import Namespace, Resource
from mewbo_core.common import get_logger
from pydantic import BaseModel, ConfigDict, Field

from . import store as store_mod
from .mcp_config import WorkspaceMcpConfig
from .scg.config import ScgConfig

if TYPE_CHECKING:
    from mewbo_graph.scg.store import ScgStore

    from .schemas import Workspace
    from .store import AgenticSearchStoreBase

logging = get_logger(name="api.agentic_search.graph_routes")

AuthResult = tuple[dict[str, Any], int] | None
AuthGuard = Callable[[], AuthResult]

GraphLayer = Literal["schema", "memory", "entity", "cross"]


def _no_auth() -> AuthResult:
    return None


_require_api_key: AuthGuard = _no_auth
_runtime: Any = None  # populated by init_agentic_search_graph; carries wiki_store


# ── Typed wire models (mirror the console ``WorkspaceGraph`` 1:1) ───────────


class _GraphWire(BaseModel):
    """Lenient-in / strict-shape base — ``to_wire()`` carries known keys only.

    ``extra="ignore"`` so a future additive view field never breaks parsing of
    the ``ScgGraphView.to_wire()`` dict; the models still pin the keys this
    route reads + re-emits.
    """

    model_config = ConfigDict(extra="ignore")


class GraphNodeData(_GraphWire):
    """The ``data`` payload of one cytoscape node (schema | memory | ghost)."""

    id: str
    label: str
    kind: str
    layer: GraphLayer
    source_id: str | None = Field(default=None, alias="sourceId")
    source_key: str | None = Field(default=None, alias="sourceKey")
    doc: str | None = None
    snippet: str | None = None
    labels: list[str] | None = None
    unmapped: bool | None = None

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class GraphNode(_GraphWire):
    """One cytoscape node element."""

    data: GraphNodeData


class GraphEdgeData(_GraphWire):
    """The ``data`` payload of one cytoscape edge."""

    id: str
    source: str
    target: str
    kind: str
    layer: GraphLayer
    weight: float | None = None


class GraphEdge(_GraphWire):
    """One cytoscape edge element."""

    data: GraphEdgeData


class PerLayer(_GraphWire):
    """Per-layer node tallies (mirrors ``ScgGraphView`` stats)."""

    schema_: int = Field(default=0, alias="schema")
    memory: int = 0
    entity: int = 0

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class GraphStats(_GraphWire):
    """Aggregate stats + the unmapped-source list the FE renders as ghosts."""

    total_nodes: int = Field(default=0, alias="totalNodes")
    total_edges: int = Field(default=0, alias="totalEdges")
    kinds: dict[str, int] = Field(default_factory=dict)
    per_layer: PerLayer = Field(default_factory=PerLayer, alias="perLayer")
    unmapped: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class WorkspaceGraphWire(_GraphWire):
    """The full ``GET /workspaces/<id>/graph`` response model."""

    scope: list[str]
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    stats: GraphStats

    def dump(self) -> dict[str, Any]:
        """Serialise to the camelCase wire dict the console consumes."""
        return self.model_dump(by_alias=True, exclude_none=True)


# ── Wiring ─────────────────────────────────────────────────────────────────


graph_ns = Namespace(
    "agentic_search_graph",
    description="Agentic Search — workspace SCG multiplex graph view.",
)


def init_agentic_search_graph(
    api: object, require_api_key: AuthGuard, runtime: Any = None
) -> None:
    """Wire the graph namespace + capture the auth guard and session runtime.

    Shares the ``/api/agentic_search`` path prefix with the main namespace;
    ``runtime`` carries the wiki memory store (``runtime.wiki_store``) the
    multiplex assembler reads for the memory layer.
    """
    global _require_api_key, _runtime
    _require_api_key = require_api_key
    _runtime = runtime
    api.add_namespace(graph_ns, path="/api/agentic_search")  # type: ignore[attr-defined]


def _scope_for_workspace(
    store: AgenticSearchStoreBase, workspace: Workspace
) -> list[str]:
    """Resolve the workspace's enabled-source scope (#75 grant semantics).

    The persisted virtual MCP config's attached server names win when one
    exists; otherwise fall back to the workspace's raw ``sources`` (the current
    global behavior). Mirrors ``SearchRun.start`` so the graph view is scoped to
    exactly what a run on this workspace may reach.
    """
    return (
        WorkspaceMcpConfig.attached_server_names(store, workspace.id)
        or list(workspace.sources)
    )


# ── Payload assembly (typed end to end) ─────────────────────────────────────


def _ghost_node(source_id: str) -> GraphNode:
    """A synthetic ``unmapped`` node for a workspace source with no SCG graph.

    The FE renders it as a ghost with a "map this source" hint (the map action
    already exists on the Sources flow). Carries the ``schema`` layer tag so it
    rides the schema toggle, and a stable id so re-fetches are idempotent.
    """
    return GraphNode(
        data=GraphNodeData(
            id=f"unmapped:{source_id}",
            label=source_id,
            kind="unmapped",
            layer="schema",
            sourceId=source_id,
            unmapped=True,
        )
    )


def _empty_wire(scope: list[str]) -> WorkspaceGraphWire:
    """The graceful-degradation payload: no schema, every source unmapped."""
    ordered = sorted(set(scope))
    return WorkspaceGraphWire(
        scope=ordered,
        nodes=[_ghost_node(sid) for sid in ordered],
        edges=[],
        stats=GraphStats(unmapped=list(ordered)),
    )


def _normalize_and_ghost(
    parsed: WorkspaceGraphWire, scope: list[str]
) -> WorkspaceGraphWire:
    """Remap schema-edge endpoints to node ids + append unmapped ghost nodes.

    ``ScgGraphView`` emits schema edges addressed by ``source_key`` and schema
    nodes carrying both ``id`` (= node_id) and ``source_key``; the renderer joins
    by ``id``. We build a ``source_key → node_id`` index over the schema nodes
    and re-point each schema edge, dropping any whose endpoint is unknown.
    Memory + cross edges already use ``node_id`` and are kept only when both
    endpoints are real nodes. Finally a ghost node is appended for every scoped
    source that produced zero schema nodes.
    """
    key_to_id: dict[str, str] = {}
    mapped_sources: set[str] = set()
    node_ids: set[str] = set()
    for node in parsed.nodes:
        node_ids.add(node.data.id)
        if node.data.source_key is not None:
            key_to_id[node.data.source_key] = node.data.id
        if node.data.source_id is not None and node.data.layer == "schema":
            mapped_sources.add(node.data.source_id)

    edges: list[GraphEdge] = []
    for edge in parsed.edges:
        data = edge.data
        if data.layer == "schema":
            src = key_to_id.get(data.source)
            tgt = key_to_id.get(data.target)
            if src is None or tgt is None:
                continue  # endpoint not a real node in the payload — drop
            edges.append(
                GraphEdge(data=data.model_copy(update={"source": src, "target": tgt}))
            )
        elif data.source in node_ids and data.target in node_ids:
            # memory/cross edges already address by node_id (defensive check).
            edges.append(edge)

    unmapped = [sid for sid in sorted(set(scope)) if sid not in mapped_sources]
    nodes = [*parsed.nodes, *(_ghost_node(sid) for sid in unmapped)]

    stats = parsed.stats.model_copy(update={"unmapped": unmapped})
    return WorkspaceGraphWire(
        scope=parsed.scope or sorted(set(scope)),
        nodes=nodes,
        edges=edges,
        stats=stats,
    )


def _schema_only_wire(scg_store: ScgStore, scope: list[str]) -> dict[str, Any]:
    """Schema-only ``to_wire()`` when the wiki memory store is absent.

    Reuses ``ScgGraphView``'s own ``to_wire`` formatters (constructs the frozen
    view with empty memory tuples) so the wire shape is byte-identical to the
    full assembler minus the memory layer.
    """
    from mewbo_graph.scg.graph_view import ScgGraphView

    ordered = sorted(set(scope))
    schema_nodes = [
        n for sid in ordered for n in scg_store.query_nodes(source_id=sid)
    ]
    node_keys = {n.source_key for n in schema_nodes}
    schema_edges = [
        e
        for e in scg_store.list_edges()
        if e.source in node_keys and e.target in node_keys
    ]
    view = ScgGraphView(
        scope=tuple(ordered),
        schema_nodes=tuple(schema_nodes),
        schema_edges=tuple(schema_edges),
        memory_nodes=(),
        memory_edges=(),
        cross_edges=(),
    )
    return view.to_wire()


def _build_graph_payload(scope: list[str]) -> WorkspaceGraphWire:
    """Assemble + normalize the workspace-scoped multiplex wire payload.

    Returns the empty-schema shape (every source ``unmapped``) when SCG is
    disabled or the graph library is unavailable — never raises for those.
    """
    if not ScgConfig.enabled():
        return _empty_wire(scope)
    try:
        from mewbo_graph.scg.graph_view import ScgGraphView
        from mewbo_graph.scg.store import get_scg_store
    except ImportError:
        # Graph library absent (no ``wiki``/``retrieval`` extra) — schema layer
        # is empty; the FE renders every source as an unmapped ghost.
        return _empty_wire(scope)

    scg_store = get_scg_store()
    wiki_store = getattr(_runtime, "wiki_store", None)
    if wiki_store is None:
        # The memory layer needs the shared wiki store; without it (graph-less
        # boot) degrade to the schema layer alone.
        raw = _schema_only_wire(scg_store, scope)
    else:
        raw = ScgGraphView.for_scope(scg_store, wiki_store, list(scope)).to_wire()

    parsed = WorkspaceGraphWire.model_validate(raw)
    return _normalize_and_ghost(parsed, scope)


@graph_ns.route("/workspaces/<string:workspace_id>/graph")
class WorkspaceGraphResource(Resource):
    """The workspace-scoped SCG multiplex graph (schema + memory + entity)."""

    @graph_ns.doc(
        "get_workspace_graph",
        params={
            "workspace_id": "Workspace id returned by "
            "POST /api/agentic_search/workspaces.",
        },
    )
    @graph_ns.response(200, "The workspace graph.")
    @graph_ns.response(401, "Missing or invalid API key.")
    @graph_ns.response(404, "Workspace not found.")
    def get(self, workspace_id: str) -> tuple[dict[str, Any], int]:
        """Get the workspace graph.

        Returns the capability graph scoped to the workspace's enabled
        sources, as cytoscape-style `nodes` and `edges` plus `stats` and the
        resolved `scope`. Every element is tagged with a layer (`schema`,
        `memory`, `entity` or `cross`) so clients can toggle layers
        independently. A source that has never been mapped appears as one
        ghost node flagged `unmapped`. The endpoint degrades gracefully: a
        disabled or unavailable graph backend still returns 200 with an empty
        schema layer and every source listed as unmapped. Only an unknown
        workspace returns 404. No node or edge ever carries a credential.
        """
        if (auth := _require_api_key()) is not None:
            return auth
        store = store_mod.get_store()
        workspace = store.get_workspace(workspace_id)
        if workspace is None:
            return {"message": "workspace not found"}, 404
        scope = _scope_for_workspace(store, workspace)
        try:
            payload = _build_graph_payload(scope)
        except Exception as exc:  # noqa: BLE001 — never 500 the viewer
            logging.warning(
                "workspace graph assembly failed for %s: %s", workspace_id, exc
            )
            payload = _empty_wire(scope)
        return payload.dump(), 200


__all__ = ["WorkspaceGraphWire", "graph_ns", "init_agentic_search_graph"]
