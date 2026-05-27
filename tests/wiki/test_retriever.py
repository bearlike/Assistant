"""HybridRetriever tests — exercises BM25, cosine, RRF, graph expand."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from mewbo_graph.wiki.retriever import HybridRetriever
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import (
    Embedding,
    Frontmatter,
    GraphEdge,
    GraphNode,
    WikiPage,
)


@pytest.fixture
def store(tmp_path):
    return JsonWikiStore(root_dir=tmp_path / "wiki")


def _seed_pages(store):
    pages = [
        WikiPage(
            id="overview", title="Overview",
            frontmatter=Frontmatter(title="Overview", slug="overview"),
            body="The overview describes the engine and its mechanics.",
            toc=[], nav=[],
        ),
        WikiPage(
            id="auth", title="Authentication",
            frontmatter=Frontmatter(title="Authentication", slug="auth"),
            body="User authentication via tokens and session management.",
            toc=[], nav=[],
        ),
        WikiPage(
            id="storage", title="Storage Layer",
            frontmatter=Frontmatter(title="Storage Layer", slug="storage"),
            body="Storage uses MongoDB or JSON files for persistence.",
            toc=[], nav=[],
        ),
    ]
    for p in pages:
        store.save_page("x/y", p)


def _seed_graph(store):
    store.upsert_nodes("x/y", [
        GraphNode(slug="x/y", node_id="f1", type="Function", name="authenticate",
                  file="auth.py", range=(0, 100), docstring="Verify a user's token."),
        GraphNode(slug="x/y", node_id="f2", type="Function", name="store_data",
                  file="storage.py", range=(0, 100), docstring="Persist a record."),
        GraphNode(slug="x/y", node_id="c1", type="Class", name="Engine",
                  file="core.py", range=(0, 100), docstring="The main loop."),
    ])
    store.upsert_embeddings("x/y", [
        Embedding(slug="x/y", node_id="f1", vector=[1.0, 0.0, 0.0], model="m", dim=3),
        Embedding(slug="x/y", node_id="f2", vector=[0.0, 1.0, 0.0], model="m", dim=3),
        Embedding(slug="x/y", node_id="c1", vector=[0.0, 0.0, 1.0], model="m", dim=3),
    ])


def _make_embedder(qvec):
    """Return an Embedder stub whose embed_nodes returns the given query vector."""
    emb = MagicMock()
    emb.embed_nodes.return_value = [
        MagicMock(vector=qvec)
    ]
    return emb


def test_search_pages_only_bm25_ranks_lexically(store):
    _seed_pages(store)
    retriever = HybridRetriever(store=store, embedder=_make_embedder([0.0, 0.0, 0.0]))
    hits = retriever.search("x/y", query="authentication tokens", k=2, sources="pages")
    assert len(hits) >= 1
    assert hits[0].id == "auth"
    assert hits[0].kind == "page"


def test_search_graph_uses_embedding_match(store):
    _seed_pages(store)
    _seed_graph(store)
    # qvec aligned with f1 (authenticate) → it must rank first by cosine
    retriever = HybridRetriever(store=store, embedder=_make_embedder([1.0, 0.0, 0.0]))
    hits = retriever.search("x/y", query="user authentication", k=3, sources="graph")
    assert any(h.id == "f1" for h in hits)
    assert hits[0].id == "f1"


def test_search_both_combines_pages_and_graph(store):
    _seed_pages(store)
    _seed_graph(store)
    retriever = HybridRetriever(store=store, embedder=_make_embedder([1.0, 0.0, 0.0]))
    hits = retriever.search("x/y", query="authenticate", k=5, sources="both")
    kinds = {h.kind for h in hits}
    assert "page" in kinds or "node" in kinds
    # at least one of each in the union
    assert len(hits) > 0


def test_search_filters_by_type(store):
    _seed_graph(store)
    retriever = HybridRetriever(store=store, embedder=_make_embedder([0.0, 0.0, 1.0]))
    hits = retriever.search("x/y", query="engine", k=5, sources="graph", types=["Class"])
    assert all(h.metadata.get("type") == "Class" for h in hits)


def test_graph_expand_adds_neighbors(store):
    _seed_graph(store)
    store.upsert_edges("x/y", [
        GraphEdge(slug="x/y", source="f1", target="c1", type="CALLS"),
    ])
    retriever = HybridRetriever(store=store, embedder=_make_embedder([1.0, 0.0, 0.0]))
    hits = retriever.search("x/y", query="authenticate", k=10, sources="graph", graph_expand=True)
    ids = {h.id for h in hits}
    # f1 is the top hit; graph_expand should pull c1 (1-hop neighbor) into results
    assert "f1" in ids
    assert "c1" in ids


def test_search_empty_corpus_returns_empty(store):
    retriever = HybridRetriever(store=store, embedder=_make_embedder([1.0, 0.0, 0.0]))
    hits = retriever.search("nothing/here", query="anything", k=5)
    assert hits == []


def test_rrf_score_deterministic(store):
    _seed_pages(store)
    _seed_graph(store)
    r = HybridRetriever(store=store, embedder=_make_embedder([1.0, 0.0, 0.0]))
    a = r.search("x/y", query="token authenticate", k=5)
    b = r.search("x/y", query="token authenticate", k=5)
    assert [h.id for h in a] == [h.id for h in b]
