"""``wiki_graph_neighbors`` — directed, kind-filtered, multi-hop graph traversal.

The existing ``wiki_query_graph`` covers "find nodes" (by name, kind,
file) — this complements it with "explore from here" semantics that
the agent can use to answer questions like "what calls X", "what does
X contain", or "what does X import" without chaining a dozen
single-hop lookups.

Atomic class design: ``WikiGraphNeighbors`` owns the per-call state
(slug, store, full edge list) and exposes one entry point ``traverse``;
the ``WikiGraphNeighborsTool`` shim handles registry boilerplate +
argument parsing + ctx resolution and delegates straight to it.
"""
from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Any, Literal

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field

from mewbo_graph.plugins.wiki._base import WikiSessionTool, _err_result
from mewbo_graph.plugins.wiki._ctx import (
    resolve_job_ctx,
    resolve_qa_ctx,
)

if TYPE_CHECKING:
    from mewbo_core.classes import ActionStep

logging = get_logger(name="mewbo_graph.plugins.wiki.graph_neighbors")


Direction = Literal["in", "out", "any"]
EdgeKind = Literal["CONTAINS", "IMPORTS", "CALLS", "EXTENDS", "REFERENCES"]


# ---------------------------------------------------------------------------
# Argument schema
# ---------------------------------------------------------------------------


class WikiGraphNeighborsArgs(BaseModel):
    """Arguments for ``wiki_graph_neighbors``."""

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(
        description="Starting node id (use ``wiki_query_graph`` first to find it).",
    )
    edge_kind: EdgeKind | None = Field(
        default=None,
        description=(
            "Restrict to one edge kind: ``CONTAINS`` (file→class/fn), "
            "``IMPORTS`` (file→file), ``CALLS`` (fn→fn), ``EXTENDS`` "
            "(class→base), ``REFERENCES`` (use site). Omit for all kinds."
        ),
    )
    direction: Direction = Field(
        default="any",
        description=(
            "``out`` = edges where node_id is the source (what does X "
            "call/contain/import). ``in`` = edges where node_id is the "
            "target (who calls/contains/extends X). ``any`` = both."
        ),
    )
    hops: int = Field(
        default=1, ge=1, le=3,
        description="BFS depth from node_id. Default 1 hop; cap 3 to bound fanout.",
    )
    limit: int = Field(
        default=50, ge=1, le=500,
        description="Max distinct neighbour nodes returned. Defaults to 50.",
    )


# ---------------------------------------------------------------------------
# Atomic class
# ---------------------------------------------------------------------------


class WikiGraphNeighbors:
    """Per-session, slug-scoped graph traversal.

    State: the slug and a single read of every edge in the project
    (graphs are small enough — a few thousand edges — that one pass
    over the full list beats a per-query store call).
    Behaviour: ``traverse(args)`` runs a bounded BFS and returns a
    Cytoscape-ish ``{nodes, edges, hops_reached}`` wire payload.
    Statics: edge direction + kind filters.
    """

    __slots__ = ("slug", "store", "_edges_by_src", "_edges_by_tgt", "_nodes_by_id")

    def __init__(self, slug: str, store: Any) -> None:
        """Initialise with the project slug and a wiki store handle.

        Loads the full node + edge tables once and indexes them for
        O(1) adjacency and node-by-id lookup. The graphs we deal with
        (~thousands of nodes, ~thousands of edges) fit easily; this
        avoids per-traversal store round trips.
        """
        self.slug = slug
        self.store = store
        self._edges_by_src: dict[str, list[Any]] = {}
        self._edges_by_tgt: dict[str, list[Any]] = {}
        for e in store.list_edges(slug):
            self._edges_by_src.setdefault(e.source, []).append(e)
            self._edges_by_tgt.setdefault(e.target, []).append(e)
        self._nodes_by_id: dict[str, Any] = {
            n.node_id: n for n in store.query_graph(slug)
        }

    # ── Construction ────────────────────────────────────────────────

    @classmethod
    def for_session(cls, session_id: str) -> WikiGraphNeighbors | MockSpeaker:
        """Resolve indexer or QA ctx + build a neighbours view.

        Returns a fresh instance on success, or the :func:`_err_result`
        ``MockSpeaker`` the caller can hand back to the LLM.
        """
        runtime = cls._resolve_runtime()
        if runtime is None:
            return _err_result("internal", "runtime not available")
        for resolver in (resolve_job_ctx, resolve_qa_ctx):
            ctx = resolver(session_id, runtime)
            if ctx is not None:
                return cls(slug=ctx.slug, store=ctx.store)
        return _err_result("internal", "wiki ctx not found for this session")

    # ── Behaviour ───────────────────────────────────────────────────

    def traverse(self, args: WikiGraphNeighborsArgs) -> dict[str, Any]:
        """Run a bounded BFS from ``args.node_id``.

        Returns ``{nodes: [...], edges: [...], hops_reached, truncated}``.
        Both the seed node and any traversed neighbours appear in
        ``nodes`` (deduped); ``edges`` contains only the traversed
        edges, in BFS order. ``truncated`` is True when ``limit`` cut
        the walk short of full ``hops``.
        """
        visited: set[str] = {args.node_id}
        kept_edges: list[Any] = []
        # BFS frontier: (node_id, depth_remaining).
        frontier: deque[tuple[str, int]] = deque([(args.node_id, args.hops)])
        truncated = False
        hops_reached = 0

        while frontier:
            current, depth_left = frontier.popleft()
            if depth_left <= 0:
                continue
            for e in self._edges_from(current, args.direction, args.edge_kind):
                other = e.target if e.source == current else e.source
                kept_edges.append(e)
                if other in visited:
                    continue
                if len(visited) >= args.limit:
                    truncated = True
                    break
                visited.add(other)
                hops_reached = max(hops_reached, args.hops - depth_left + 1)
                if depth_left > 1:
                    frontier.append((other, depth_left - 1))
            if truncated:
                break

        nodes = self._hydrate_nodes(visited)
        return {
            "nodes": [self._node_to_wire(n) for n in nodes],
            "edges": [self._edge_to_wire(e) for e in kept_edges],
            "hops_reached": hops_reached,
            "truncated": truncated,
        }

    # ── Helpers ─────────────────────────────────────────────────────

    def _edges_from(
        self,
        node_id: str,
        direction: Direction,
        edge_kind: EdgeKind | None,
    ) -> list[Any]:
        """Return edges incident to *node_id* honouring direction + kind."""
        outgoing = self._edges_by_src.get(node_id, []) if direction in ("out", "any") else []
        incoming = self._edges_by_tgt.get(node_id, []) if direction in ("in", "any") else []
        candidates = outgoing + incoming
        if edge_kind is None:
            return candidates
        return [e for e in candidates if e.type == edge_kind]

    def _hydrate_nodes(self, ids: set[str]) -> list[Any]:
        """Return :class:`GraphNode` records for each id in *ids*.

        Silently skips ids missing from the node table — they appear
        when an edge references a node pruned between indexings (same
        orphan-edge condition as the KG view filters out).
        """
        return [self._nodes_by_id[nid] for nid in ids if nid in self._nodes_by_id]

    @staticmethod
    def _node_to_wire(n: Any) -> dict[str, Any]:
        return {
            "node_id": n.node_id,
            "name": n.name,
            "type": n.type,
            "file": n.file,
            "range": list(n.range),
            "docstring": (n.docstring or "")[:200],
        }

    @staticmethod
    def _edge_to_wire(e: Any) -> dict[str, Any]:
        return {"source": e.source, "target": e.target, "kind": e.type}

    @staticmethod
    def _resolve_runtime() -> Any:
        try:
            from ._ctx import resolve_runtime  # noqa: PLC0415
            return resolve_runtime()
        except ImportError:
            return None


# ---------------------------------------------------------------------------
# Tool shim
# ---------------------------------------------------------------------------


class WikiGraphNeighborsTool(WikiSessionTool):
    """SessionTool: directed, kind-filtered, multi-hop graph traversal."""

    tool_id = "wiki_graph_neighbors"
    args_cls = WikiGraphNeighborsArgs
    schema = pydantic_to_openai_tool(
        WikiGraphNeighborsArgs, name="wiki_graph_neighbors"
    )

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Resolve the per-session graph and delegate to ``traverse``."""
        view = WikiGraphNeighbors.for_session(self._session_id)
        if isinstance(view, MockSpeaker):  # err payload from _err_result
            return view
        args = self._parse_args(WikiGraphNeighborsArgs, action_step)
        if isinstance(args, MockSpeaker):
            return args
        return MockSpeaker(content=str(view.traverse(args)))


__all__ = [
    "WikiGraphNeighbors",
    "WikiGraphNeighborsArgs",
    "WikiGraphNeighborsTool",
]
