"""CatalogIngestor tests — non-git document ingestion → grounded retrieval.

Proves the issue #49 acceptance at the engine layer:

- ingest N catalog docs → :class:`HybridRetriever` returns relevant cited hits
  over BOTH the page corpus (BM25) and the graph-node corpus (embeddings),
- re-ingesting the same doc ids upserts (no duplicate pages/nodes),
- an absent embedder degrades to BM25-only without crashing,
- the Project is created + marked complete with a non-empty graph (so the
  same finalize-honesty gate ``_graph_is_populated`` is satisfied honestly).

I/O boundary stubbed: the embedder (a fake returning deterministic vectors);
the store is a real :class:`JsonWikiStore` under ``tmp_path``.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from mewbo_graph.wiki.catalog import CatalogIngestor
from mewbo_graph.wiki.retriever import HybridRetriever
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import CatalogDocument, Embedding

# ── Fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path):
    return JsonWikiStore(root_dir=tmp_path / "wiki")


def _docs() -> list[CatalogDocument]:
    return [
        CatalogDocument(
            id="sku-100",
            title="Aurora Standing Desk",
            text="A height-adjustable standing desk with an electric motor and bamboo top.",
            metadata={"category": "furniture", "price": "499"},
        ),
        CatalogDocument(
            id="sku-200",
            title="Nimbus Office Chair",
            text="An ergonomic mesh office chair with lumbar support and adjustable armrests.",
            metadata={"category": "furniture", "price": "299"},
        ),
        CatalogDocument(
            id="sku-300",
            title="Lumen Desk Lamp",
            text="A dimmable LED desk lamp with warm and cool color temperatures.",
            metadata={"category": "lighting", "price": "59"},
        ),
    ]


class _FakeEmbedder:
    """Deterministic embedder: one orthonormal-ish vector per node, by order.

    ``embed_nodes`` returns Embedding rows so the store's vector_search works;
    ``embed_query`` returns the vector aligned with the doc whose title token
    appears in the query (cheap keyword routing so the cosine ranker is
    testable without a real model).
    """

    def __init__(self) -> None:
        self._by_token = {
            "desk": [1.0, 0.0, 0.0],
            "chair": [0.0, 1.0, 0.0],
            "lamp": [0.0, 0.0, 1.0],
        }

    def embed_nodes(self, items, *, slug=""):
        out = []
        for node_id, text in items:
            low = text.lower()
            vec = next(
                (v for tok, v in self._by_token.items() if tok in low),
                [0.0, 0.0, 0.0],
            )
            out.append(Embedding(slug=slug, node_id=node_id, vector=vec, model="fake", dim=3))
        return out

    def embed_query(self, text):
        low = text.lower()
        return next((v for tok, v in self._by_token.items() if tok in low), [0.0, 0.0, 0.0])


# ── Tests ───────────────────────────────────────────────────────────────────


def test_ingest_creates_complete_project_with_populated_graph(store):
    report = CatalogIngestor(store=store, embedder=_FakeEmbedder()).ingest("acme/catalog", _docs())

    assert report.ingested == 3
    assert report.total_documents == 3
    assert report.bm25_only is False

    project = store.get_project("acme/catalog")
    assert project is not None
    # 3 doc pages + the synthetic landing index page.
    assert project.pages == 4
    assert project.landing_page_id == report.landing_page_id
    # Non-empty graph → the finalize honesty gate is satisfied honestly.
    assert len(store.query_graph("acme/catalog")) == 3


def test_ingest_writes_pages_and_graph_nodes_for_each_doc(store):
    CatalogIngestor(store=store, embedder=_FakeEmbedder()).ingest("acme/catalog", _docs())

    pages = store.list_pages("acme/catalog")
    page_ids = {p.id for p in pages}
    titles = {p.title for p in pages}
    for doc in _docs():
        # Each doc lands at its content-addressed page id, with its title intact.
        assert CatalogIngestor._doc_id("acme/catalog", doc.id) in page_ids
        assert doc.title in titles
    # one graph node per doc, carrying searchable text
    nodes = store.query_graph("acme/catalog")
    names = {n.name for n in nodes}
    assert "Aurora Standing Desk" in names
    assert any("ergonomic" in (n.docstring or "") for n in nodes)


def test_retriever_grounds_over_ingested_catalog_pages(store):
    CatalogIngestor(store=store, embedder=_FakeEmbedder()).ingest("acme/catalog", _docs())
    retriever = HybridRetriever(store=store, embedder=_FakeEmbedder())

    hits = retriever.search("acme/catalog", "ergonomic office chair", k=5, sources="pages")
    assert hits, "expected page hits over the ingested catalog"
    assert hits[0].metadata.get("title") == "Nimbus Office Chair"


def test_retriever_grounds_over_graph_nodes_via_embeddings(store):
    CatalogIngestor(store=store, embedder=_FakeEmbedder()).ingest("acme/catalog", _docs())
    retriever = HybridRetriever(store=store, embedder=_FakeEmbedder())

    hits = retriever.search("acme/catalog", "standing desk", k=5, sources="graph")
    assert hits
    # the "desk" query vector aligns with the standing-desk node
    assert any(h.metadata.get("name") == "Aurora Standing Desk" for h in hits)


def test_reingest_same_ids_upserts_no_duplicates(store):
    ingestor = CatalogIngestor(store=store, embedder=_FakeEmbedder())
    ingestor.ingest("acme/catalog", _docs())
    # re-ingest with one edited doc and the same ids
    edited = _docs()
    edited[0] = CatalogDocument(
        id="sku-100",
        title="Aurora Standing Desk (v2)",
        text="An upgraded height-adjustable standing desk with a wider bamboo top.",
        metadata={"category": "furniture", "price": "549"},
    )
    report = ingestor.ingest("acme/catalog", edited)

    assert report.total_documents == 3  # not 6
    # 3 doc pages + 1 landing page (no churn on re-ingest).
    assert len(store.list_pages("acme/catalog")) == 4
    assert len(store.query_graph("acme/catalog")) == 3
    titles = {p.title for p in store.list_pages("acme/catalog")}
    assert "Aurora Standing Desk (v2)" in titles
    assert "Aurora Standing Desk" not in titles  # the old body was replaced


def test_embed_absent_falls_back_to_bm25_without_crashing(store):
    # An embedder whose embed_nodes raises (proxy with no embedding model).
    broken = MagicMock()
    broken.embed_nodes.side_effect = RuntimeError("no embedding deployment")

    report = CatalogIngestor(store=store, embedder=broken).ingest("acme/catalog", _docs())

    assert report.ingested == 3
    assert report.bm25_only is True
    project = store.get_project("acme/catalog")
    assert project is not None  # project still created + complete
    # BM25 grounding still works (pages are not embedded anyway).
    retriever = HybridRetriever(store=store, embedder=_FakeEmbedder())
    hits = retriever.search("acme/catalog", "led desk lamp", k=5, sources="pages")
    assert hits
    assert hits[0].metadata.get("title") == "Lumen Desk Lamp"


def test_ingest_without_embedder_is_bm25_only(store):
    # No embedder injected at all → engine builds one or degrades; force the
    # null path by passing embedder=None and letting make_embedder_or_none fail.
    report = CatalogIngestor(store=store, embedder=None).ingest("acme/catalog", _docs())
    assert report.ingested == 3
    # project complete regardless of embedding availability
    assert store.get_project("acme/catalog") is not None


def test_slug_colliding_ids_do_not_overwrite_each_other(store):
    # Two DISTINCT doc ids that would slugify to the same string ("foo bar" vs
    # "foo-bar") must remain distinct pages — the content-addressed id keeps the
    # page and node in lockstep instead of silently overwriting one page.
    docs = [
        CatalogDocument(id="foo bar", title="Foo Bar (space)", text="The spaced variant."),
        CatalogDocument(id="foo-bar", title="Foo Bar (dash)", text="The dashed variant."),
    ]
    report = CatalogIngestor(store=store, embedder=_FakeEmbedder()).ingest("acme/catalog", docs)

    assert report.total_documents == 2
    # 2 distinct doc pages + landing page; 2 distinct graph nodes.
    assert len(store.query_graph("acme/catalog")) == 2
    titles = {p.title for p in store.list_pages("acme/catalog")}
    assert {"Foo Bar (space)", "Foo Bar (dash)"} <= titles
    # The two content-addressed ids are genuinely different.
    assert CatalogIngestor._doc_id("acme/catalog", "foo bar") != CatalogIngestor._doc_id(
        "acme/catalog", "foo-bar"
    )


def test_doc_total_excludes_git_ast_file_nodes(store):
    # If the same slug was ever git-indexed, plain File nodes exist. The catalog
    # doc count must exclude them (count only ``catalog/``-prefixed nodes).
    from mewbo_graph.wiki.types import GraphNode

    store.upsert_nodes("acme/catalog", [
        GraphNode(slug="acme/catalog", node_id="ast1", type="File", name="main.py",
                  file="src/main.py", range=(0, 10), docstring=None),
    ])
    report = CatalogIngestor(store=store, embedder=_FakeEmbedder()).ingest("acme/catalog", _docs())
    # 3 catalog docs — the pre-existing git File node is NOT counted.
    assert report.total_documents == 3
