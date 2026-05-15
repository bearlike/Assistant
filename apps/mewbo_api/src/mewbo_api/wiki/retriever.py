"""HybridRetriever — BM25 + cosine + RRF fusion + 1-hop graph expansion.

Operates over two candidate sets: wiki pages (text bodies) and graph
nodes (name + docstring text). BM25 always runs over both. Vector
cosine only over graph nodes (pages aren't embedded in v1). Final
ranking is reciprocal-rank-fusion (RRF, k=60).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from .store import WikiStoreBase

_RRF_K = 60           # standard RRF constant — see Robertson & Zaragoza (2009)
_PAGE_BODY_CAP = 4096  # truncate page bodies before BM25 to keep memory bounded
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


@dataclass(frozen=True)
class HybridHit:
    """Single ranked result returned by HybridRetriever.search."""

    kind: Literal["page", "node"]
    id: str
    score: float
    snippet: str
    metadata: dict[str, Any] = field(default_factory=dict)


class HybridRetriever:
    """Atomic retriever — store + embedder at construction; one public search method."""

    def __init__(self, *, store: WikiStoreBase, embedder: Any) -> None:
        """Initialise with a store backend and an embedder instance."""
        self.store = store
        self.embedder = embedder

    def search(
        self,
        slug: str,
        query: str,
        *,
        k: int = 10,
        types: list[str] | None = None,
        graph_expand: bool = False,
        sources: Literal["pages", "graph", "both"] = "both",
    ) -> list[HybridHit]:
        """Return up to *k* results fused from BM25, cosine, and optional graph expansion.

        Args:
            slug: Project slug (e.g. "org/repo").
            query: Free-text search query.
            k: Maximum number of results to return.
            types: Filter graph candidates to these node types (e.g. ["Class"]).
            graph_expand: When True, 1-hop neighbours of top graph hits are added.
            sources: Which candidate pools to include — "pages", "graph", or "both".
        """
        # 1. Collect candidates.
        page_candidates = (
            self._page_candidates(slug) if sources in {"pages", "both"} else []
        )
        node_candidates = (
            self._graph_candidates(slug, types) if sources in {"graph", "both"} else []
        )

        corpus = page_candidates + node_candidates
        if not corpus:
            return []

        # 2. BM25 score over the whole corpus (pages + nodes).
        bm25_ranks = _bm25_ranks(query, [c["text"] for c in corpus])

        # 3. Cosine score over graph nodes only (pages aren't embedded in v1).
        cos_ranks: dict[int, int] = {}
        if node_candidates:
            qvec = self.embedder.embed_nodes([("query", query)])[0].vector
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

        return hits

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


__all__ = ["HybridHit", "HybridRetriever"]
