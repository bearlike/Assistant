"""ScgRouter — the cheap query→route mechanism over the SCG (spec §6).

Routing is the graph's *only* query-time job: control routing. Given a natural
-language query, the router embeds it, vector-searches seed nodes in the store,
expands one hop along capability/route edges to assemble candidate
:class:`RouteRecipe`s, and ranks them with a deterministic, **zero-LLM** score
(``cosine(seed) + edge weight``). The agentic traversal engine (#19) consumes
the ranked recipes; spending sub-agents is a downstream concern.

This is the lightweight pre-rank, not the full hypothesis search. It mirrors
HippoRAG2's "cheap structural pre-rank before spending agents" stance: route
first, traverse second.

SCALE SEAM — Personalized PageRank
==================================
A query-seeded Personalized PageRank (PPR) over the SCG with hub damping is the
documented upgrade for ranking quality at catalog scale (HippoRAG2
``2502.14802``, PathRAG ``2502.14902``). It lands *behind this same
``route()`` signature* — callers never change. It is **deliberately NOT
implemented now**: the additive ``cosine + weight`` score is cheaper, fully
deterministic, and sufficient for the small catalogs SCG ships with first.
"""

from __future__ import annotations

from typing import Protocol

from .store import ScgStore
from .types import RouteRecipe, ScgEdge, SourceKey


class _QueryEmbedder(Protocol):
    """The single embedder method the router needs (wiki ``Embedder`` satisfies it)."""

    def embed_query(self, text: str) -> list[float]:
        """Return the dense embedding vector for *text*."""
        ...


class ScgRouter:
    """Cheap, deterministic query→route over the Source Capability Graph.

    Dependency-injected with an :class:`ScgStore` and a query embedder (the wiki
    :class:`~mewbo_graph.wiki.embedder.Embedder` by default; tests inject a fake).
    Holds no per-query state — all behaviour is expressed over the injected
    store + embedder, so a single router instance is reusable across queries.
    """

    # Edges traversed when expanding a seed toward a recipe. Capability/route
    # edges connect a capability to what it PRODUCES/CONSUMES and how its
    # fields RESOLVE — the executable pathways the router proposes.
    _EXPAND_KINDS: frozenset[str] = frozenset(
        {"PRODUCES", "CONSUMES", "RESOLVES_TO", "SUPPORTS_QUERY"}
    )

    def __init__(self, *, store: ScgStore, embedder: _QueryEmbedder) -> None:
        """Bind the SCG store + query embedder this router routes against."""
        self.store = store
        self.embedder = embedder

    def route(self, query: str, *, k: int = 5) -> list[RouteRecipe]:
        """Return up to *k* :class:`RouteRecipe`s best matching *query*.

        Embed → vector-search seed nodes → expand one hop along capability/route
        edges → assemble candidate recipes → rank by ``cosine(seed) + edge
        weight`` (zero-LLM). Returns ``[]`` for an empty graph or no match.
        """
        qvec = self.embedder.embed_query(query)
        seeds = self.store.vector_search(qvec, k=k)
        if not seeds:
            return []

        # source_key → recipe, so a vector-search hit (mapped to its graph
        # anchor) can be matched against the recipes reachable from it.
        recipes = {r.source_key: r for r in self.store.list_recipes()}
        # Pre-index inbound capability/route edges by target ONCE per route()
        # (one full edge scan), not once per seed — the inbound expansion below
        # is then an O(1) dict lookup. (PPR is the documented scale upgrade
        # behind this same signature.)
        inbound = self._inbound_index()
        best: dict[SourceKey, float] = {}
        for node_id, sim in seeds:
            node = self.store.get_node(node_id)
            if node is None:
                continue
            for key, weight in self._candidate_keys(node.source_key, inbound):
                if key not in recipes:
                    continue
                score = sim + weight
                if score > best.get(key, float("-inf")):
                    best[key] = score

        ranked = sorted(
            best.items(),
            # Score desc, then source_key asc — a deterministic tie-break.
            key=lambda kv: (-kv[1], kv[0]),
        )
        # Honour the documented bound: one-hop expansion can map the seeds to
        # more than *k* recipe candidates, so slice the ranked result to *k*.
        return [recipes[key] for key, _ in ranked[:k]]

    def _candidate_keys(
        self, seed_key: SourceKey, inbound: dict[SourceKey, list[ScgEdge]]
    ) -> list[tuple[SourceKey, float]]:
        """``(recipe_key, edge_weight)`` candidates reachable from *seed_key*.

        The seed itself is a candidate at weight ``0.0`` (it may anchor a recipe
        directly). Each one-hop capability/route edge contributes its target as
        a candidate carrying the edge's weight, so a seed entity reachable from
        a capability's recipe still routes. ``inbound`` is the pre-built
        target→edges index (see :meth:`_inbound_index`) so this is allocation-
        and scan-free per seed.
        """
        out: list[tuple[SourceKey, float]] = [(seed_key, 0.0)]
        for edge in self.store.neighbors(seed_key):
            if edge.kind in self._EXPAND_KINDS:
                out.append((edge.target, edge.weight))
        # Also expand inbound: an entity seed reaches the capability that
        # PRODUCES it (the recipe lives on the capability, not the entity).
        for edge in inbound.get(seed_key, ()):
            out.append((edge.source, edge.weight))
        return out

    def _inbound_index(self) -> dict[SourceKey, list[ScgEdge]]:
        """Index capability/route edges by ``target`` — one full edge scan.

        Built once per :meth:`route` so the per-seed inbound expansion is an
        O(1) dict lookup instead of a fresh ``list_edges()`` scan per seed.
        """
        index: dict[SourceKey, list[ScgEdge]] = {}
        for edge in self.store.list_edges():
            if edge.kind in self._EXPAND_KINDS:
                index.setdefault(edge.target, []).append(edge)
        return index


__all__ = ["ScgRouter"]
