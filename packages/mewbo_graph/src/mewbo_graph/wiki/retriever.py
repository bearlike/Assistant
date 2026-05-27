"""HybridRetriever — BM25 + cosine + RRF fusion + graph/memory expansion.

Operates over two base candidate sets: wiki pages (text bodies) and graph
nodes (name + docstring text). BM25 always runs over both. Vector cosine
only over graph nodes (pages aren't embedded in v1). Final ranking is
reciprocal-rank-fusion (RRF, k=60).

The memory multiplex layer is an additive overlay: with ``memory_expand``,
``MultiplexExpander`` seeds atomic memory notes by cosine, then follows each
note's ``ANCHORS`` edges back to code entities (+ their 1-hop structural
neighbours), additive-fusing a small ``w_ppr`` booster (GAAMA's
``0.1·ppr + 1.0·sim``). Hubs are degree-damped. ``memory_expand=False`` is
byte-for-byte the legacy behaviour.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from .memory_types import MemoryFilter
from .store import WikiStoreBase

if TYPE_CHECKING:
    from .embedder import EmbedderProtocol
    from .memory_types import MemoryNode
    from .structure_provider import StructureProvider
    from .types import GraphNode

_RRF_K = 60           # standard RRF constant — see Robertson & Zaragoza (2009)
_PAGE_BODY_CAP = 4096  # truncate page bodies before BM25 to keep memory bounded
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


@dataclass(frozen=True)
class HybridHit:
    """Single ranked result returned by HybridRetriever.search."""

    kind: Literal["page", "node", "memory"]
    id: str
    score: float
    snippet: str
    metadata: dict[str, Any] = field(default_factory=dict)


class HybridRetriever:
    """Atomic retriever — store + embedder at construction; one public search method."""

    def __init__(
        self,
        *,
        store: WikiStoreBase,
        embedder: EmbedderProtocol,
        expander: MultiplexExpander | None = None,
    ) -> None:
        """Initialise with a store backend, an embedder, and optional expander."""
        self.store = store
        self.embedder = embedder
        self._expander = expander

    def search(
        self,
        slug: str,
        query: str,
        *,
        k: int = 10,
        types: list[str] | None = None,
        graph_expand: bool = False,
        sources: Literal["pages", "graph", "both"] = "both",
        memory_expand: bool = False,
        memory_filters: MemoryFilter | None = None,
    ) -> list[HybridHit]:
        """Return up to *k* results fused from BM25, cosine, graph/memory expansion.

        Args:
            slug: Project slug (e.g. "org/repo").
            query: Free-text search query.
            k: Maximum number of results to return.
            types: Filter graph candidates to these node types (e.g. ["Class"]).
            graph_expand: When True, 1-hop neighbours of top graph hits are added.
            sources: Which candidate pools to include — "pages", "graph", or "both".
            memory_expand: When True, additive-fuse the memory multiplex layer.
            memory_filters: Optional ``MemoryFilter`` applied to memory seeding.
        """
        # 1. Collect candidates.
        page_candidates = (
            self._page_candidates(slug) if sources in {"pages", "both"} else []
        )
        node_candidates = (
            self._graph_candidates(slug, types) if sources in {"graph", "both"} else []
        )

        corpus = page_candidates + node_candidates
        if corpus:
            # 2. BM25 score over the whole corpus (pages + nodes).
            bm25_ranks = _bm25_ranks(query, [c["text"] for c in corpus])

            # 3. Cosine score over graph nodes only (pages aren't embedded in v1).
            cos_ranks: dict[int, int] = {}
            if node_candidates:
                qvec = self.embedder.embed_query(query)
                top_embs = self.store.vector_search(slug, qvec=qvec, k=max(k * 3, 30))
                id_to_rank = {emb.node_id: r for r, emb in enumerate(top_embs)}
                for i, c in enumerate(corpus):
                    if c["kind"] == "node" and c["id"] in id_to_rank:
                        cos_ranks[i] = id_to_rank[c["id"]]

            # 4. RRF fusion — sum 1/(rrf_k + rank) across rankers.
            fused: dict[int, float] = {}
            for i, r in enumerate(bm25_ranks):
                fused[i] = fused.get(i, 0.0) + 1.0 / (_RRF_K + r + 1)
            for i, r in cos_ranks.items():
                fused[i] = fused.get(i, 0.0) + 1.0 / (_RRF_K + r + 1)

            # 5. Sort descending by fused score; take top-k.
            ranked = sorted(fused.items(), key=lambda t: t[1], reverse=True)
            hits = [_to_hit(corpus[i], score=s) for i, s in ranked[:k]]

            # 6. Optional 1-hop graph expansion over top-3 graph hits.
            if graph_expand:
                hits = self._expand_neighbors(slug, hits, k=k)
        else:
            hits = []

        # 7. Optional memory multiplex overlay (additive fusion).
        if memory_expand:
            hits = self._fuse_memory(slug, query, hits, k=k, filt=memory_filters)

        return hits

    def _fuse_memory(
        self,
        slug: str,
        query: str,
        base: list[HybridHit],
        *,
        k: int,
        filt: MemoryFilter | None,
    ) -> list[HybridHit]:
        """Seed memory by cosine, expand to anchored code, additive-fuse into *base*."""
        expander = self._expander
        if expander is None:
            expander = self._expander = MultiplexExpander(store=self.store)
        qvec = self.embedder.embed_query(query)
        extra = expander.expand(slug, qvec, k=k, filt=filt)
        return _merge_hits(base, extra, k=k)

    # -- Private helpers -------------------------------------------------------

    def _page_candidates(self, slug: str) -> list[dict]:
        out = []
        for p in self.store.list_pages(slug):
            text = (p.body or "")[:_PAGE_BODY_CAP]
            snippet = (text[:200] + "…") if len(text) > 200 else text
            out.append({
                "kind": "page",
                "id": p.id,
                "text": text,
                "snippet": snippet,
                "metadata": {"title": p.title},
            })
        return out

    def _graph_candidates(self, slug: str, types: list[str] | None) -> list[dict]:
        if types:
            nodes = []
            for t in types:
                nodes.extend(self.store.query_graph(slug, node_type=t))
        else:
            nodes = self.store.query_graph(slug)
        out = []
        for n in nodes:
            text = (n.name + " " + (n.docstring or "")).strip()
            out.append({
                "kind": "node",
                "id": n.node_id,
                "text": text,
                "snippet": text[:200],
                "metadata": {"type": n.type, "name": n.name, "file": n.file},
            })
        return out

    def _expand_neighbors(
        self, slug: str, hits: list[HybridHit], *, k: int
    ) -> list[HybridHit]:
        """Add 1-hop graph neighbours for the top-3 node hits, with a score bonus."""
        seen: set[tuple[str, str]] = {(h.kind, h.id) for h in hits}
        bonus = (hits[0].score * 0.5) if hits else 0.0
        added: list[HybridHit] = []
        for h in hits[:3]:  # budget: expand only top-3 to avoid fanout
            if h.kind != "node":
                continue
            for n in self.store.query_graph(slug, neighbors_of=h.id):
                key = ("node", n.node_id)
                if key in seen:
                    continue
                text = (n.name + " " + (n.docstring or "")).strip()
                added.append(HybridHit(
                    kind="node",
                    id=n.node_id,
                    score=bonus,
                    snippet=text[:200],
                    metadata={
                        "type": n.type,
                        "name": n.name,
                        "file": n.file,
                        "expanded": True,
                    },
                ))
                seen.add(key)
        combined = hits + added
        combined.sort(key=lambda h: h.score, reverse=True)
        return combined[:k + len(added)]


# -- Memory multiplex expander -------------------------------------------------


class MultiplexExpander:
    """Cross-layer retrieval: memory seeds → anchored code → structural hops.

    Atomic, injectable (store + optional structure provider). Seeds atomic
    memory notes by cosine (``memory_vector_search``, MemoryFilter-aware,
    invalidated-excluded), then for each note follows its ``ANCHORS`` edges
    back to live code entities and their ≤``expansion_hops`` structural
    neighbours, scoring an additive ``w_ppr`` booster and damping hub nodes
    (degree > ``hub_degree``).
    """

    def __init__(
        self,
        *,
        store: WikiStoreBase,
        provider: StructureProvider | None = None,
        w_ppr: float = 0.1,
        hub_degree: int = 50,
        expansion_hops: int = 1,
        rrf_k: int = _RRF_K,
    ) -> None:
        """Inject store + (lazy) structure provider and fusion knobs."""
        self.store = store
        self._provider = provider
        self.w_ppr = w_ppr
        self.hub_degree = hub_degree
        self.expansion_hops = expansion_hops
        self.rrf_k = rrf_k

    @property
    def provider(self) -> StructureProvider:
        """Lazily build the default code structure provider."""
        if self._provider is None:
            from .structure_provider import CodeStructureProvider
            self._provider = CodeStructureProvider(self.store)
        return self._provider

    def expand(
        self,
        slug: str,
        query_vec: list[float],
        *,
        k: int = 10,
        filt: MemoryFilter | None = None,
    ) -> list[HybridHit]:
        """Return memory-seed hits + their anchored/expanded code hits."""
        seeds = self.store.memory_vector_search(
            slug, query_vec, k=k, filt=filt or MemoryFilter()
        )
        # Gather every seed's live ANCHORS edges, then resolve all targets in
        # ONE graph pass (resolve_many) instead of a scan per anchor — bounds
        # expansion to a single O(N) provider hit regardless of seed/anchor fanout.
        seed_nodes: list[tuple[float, MemoryNode]] = []
        anchors_by_seed: dict[str, list[str]] = {}
        for rank, emb in enumerate(seeds):
            node = self.store.get_memory_node(slug, emb.node_id)
            if node is None:
                continue
            seed_nodes.append((1.0 / (self.rrf_k + rank + 1), node))
            anchors_by_seed[node.node_id] = [
                e.target
                for e in self.store.list_memory_edges(slug, node_id=node.node_id)
                if e.type == "ANCHORS"
            ]
        all_targets = {t for targets in anchors_by_seed.values() for t in targets}
        resolved = self.provider.resolve_many(slug, list(all_targets))

        hits: list[HybridHit] = []
        for seed_score, node in seed_nodes:
            hits.append(
                HybridHit(
                    kind="memory",
                    id=node.node_id,
                    score=seed_score,
                    snippet=node.content[:200],
                    metadata={
                        "kind": node.kind,
                        "labels": node.labels,
                        "source": node.provenance.source,
                    },
                )
            )
            for target in anchors_by_seed[node.node_id]:
                code = resolved.get(target)
                if code is None:
                    continue
                damp = self._damp(slug, code.node_id)
                base = self.w_ppr * seed_score * damp
                hits.append(self._node_hit(code, base, via=node.node_id))
                hits.extend(
                    self._expand_neighbours(slug, code.node_id, base * 0.5, node.node_id)
                )
        return hits

    def _expand_neighbours(
        self, slug: str, start_id: str, score: float, via: str
    ) -> list[HybridHit]:
        """Bounded BFS over ≤``expansion_hops`` structural neighbours."""
        out: list[HybridHit] = []
        seen = {start_id}
        frontier = [start_id]
        for _ in range(self.expansion_hops):
            nxt: list[str] = []
            for nid in frontier:
                for nb in self.store.query_graph(slug, neighbors_of=nid):
                    if nb.node_id in seen:
                        continue
                    seen.add(nb.node_id)
                    nxt.append(nb.node_id)
                    out.append(self._node_hit(nb, score, via=via, expanded=True))
            frontier = nxt
        return out

    def _damp(self, slug: str, node_id: str) -> float:
        """Hub damping: 1.0 below threshold, else ``hub_degree / degree``."""
        degree = len(self.store.query_graph(slug, neighbors_of=node_id))
        if degree <= self.hub_degree:
            return 1.0
        return self.hub_degree / degree

    @staticmethod
    def _node_hit(
        node: GraphNode, score: float, *, via: str, expanded: bool = False
    ) -> HybridHit:
        text = (node.name + " " + (node.docstring or "")).strip()
        meta: dict[str, Any] = {
            "type": node.type,
            "name": node.name,
            "file": node.file,
            "via_memory": via,
        }
        if expanded:
            meta["expanded"] = True
        return HybridHit(
            kind="node", id=node.node_id, score=score, snippet=text[:200], metadata=meta
        )


def _merge_hits(
    base: list[HybridHit], extra: list[HybridHit], *, k: int
) -> list[HybridHit]:
    """Additive-fuse two hit lists by ``(kind, id)`` (sum scores); top-k."""
    merged: dict[tuple[str, str], HybridHit] = {}
    for h in base + extra:
        key = (h.kind, h.id)
        prev = merged.get(key)
        if prev is None:
            merged[key] = h
        else:
            merged[key] = HybridHit(
                kind=h.kind,
                id=h.id,
                score=prev.score + h.score,
                snippet=prev.snippet or h.snippet,
                metadata={**h.metadata, **prev.metadata},
            )
    out = sorted(merged.values(), key=lambda h: h.score, reverse=True)
    return out[:k]


# -- Module-level helpers ------------------------------------------------------


def _bm25_ranks(query: str, docs: list[str]) -> list[int]:
    """Return per-doc rank array (0 = highest BM25 score). Lower rank = better."""
    from rank_bm25 import BM25Okapi  # lazy import — optional dep

    if not docs:
        return []
    tokenized = [_tokenize(d) for d in docs]
    bm25 = BM25Okapi(tokenized)
    scores = bm25.get_scores(_tokenize(query))
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    ranks = [0] * len(scores)
    for r, i in enumerate(order):
        ranks[i] = r
    return ranks


def _tokenize(s: str) -> list[str]:
    return [m.group(0).lower() for m in _TOKEN_RE.finditer(s or "")]


def _to_hit(c: dict, *, score: float) -> HybridHit:
    return HybridHit(
        kind=c["kind"],
        id=c["id"],
        score=score,
        snippet=c["snippet"],
        metadata=c["metadata"],
    )


__all__ = ["HybridHit", "HybridRetriever", "MultiplexExpander"]
