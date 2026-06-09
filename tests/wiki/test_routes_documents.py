"""Route tests for POST /v1/wiki/projects/<slug>/documents (catalog ingest).

Proves the issue #49 REST acceptance: a non-git project is created AND
populated purely over HTTP, and the existing retrieval layer grounds over it.
Reuses the route harness from ``test_routes`` (shared store + stub runtime).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

API_KEY = "test-key-123"


@pytest.fixture()
def store(tmp_path: Path):
    from mewbo_graph.wiki.store import JsonWikiStore

    return JsonWikiStore(root_dir=tmp_path / "wiki")


@pytest.fixture()
def runtime_stub(store):
    rt = MagicMock()
    rt.wiki_store = store
    return rt


@pytest.fixture()
def client(tmp_path: Path, monkeypatch, store, runtime_stub):
    monkeypatch.setenv("MASTER_API_TOKEN", API_KEY)
    monkeypatch.setattr("mewbo_api.backend.MASTER_API_TOKEN", API_KEY, raising=False)
    # No real embedder — force the BM25 fallback path so the route never calls
    # a proxy (deterministic, offline). make_embedder_or_none returns None.
    monkeypatch.setattr(
        "mewbo_graph.wiki.embedder.make_embedder_or_none", lambda: None, raising=True
    )

    import mewbo_api.wiki.routes as routes_mod
    from flask import Flask
    from mewbo_api.wiki.routes import register

    app = Flask(__name__)
    app.config["TESTING"] = True
    register(app, runtime_stub)
    yield app.test_client(), store
    routes_mod._runtime = None


_DOCS = {
    "documents": [
        {
            "id": "sku-1",
            "title": "Aurora Standing Desk",
            "text": "A height-adjustable standing desk with a bamboo top.",
            "metadata": {"category": "furniture"},
        },
        {
            "id": "sku-2",
            "title": "Nimbus Office Chair",
            "text": "An ergonomic mesh office chair with lumbar support.",
            "metadata": {"category": "furniture"},
        },
    ]
}


def test_documents_requires_auth(client):
    c, _ = client
    resp = c.post("/v1/wiki/projects/acme%2Fcatalog/documents", json=_DOCS)
    assert resp.status_code == 401


def test_post_documents_creates_and_populates_non_git_project(client):
    c, store = client
    # Project does not exist beforehand — no git URL was ever submitted.
    assert store.get_project("acme/catalog") is None

    resp = c.post(
        "/v1/wiki/projects/acme%2Fcatalog/documents",
        json=_DOCS,
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["slug"] == "acme/catalog"
    assert data["ingested"] == 2
    assert data["totalDocuments"] == 2
    assert data["bm25Only"] is True  # no embedder in this harness
    assert data["landingPageId"]

    # Project now exists, complete, with a populated graph.
    project = store.get_project("acme/catalog")
    assert project is not None
    assert project.landing_page_id == data["landingPageId"]
    assert store.query_graph("acme/catalog")


def test_post_documents_grounds_via_retriever(client):
    c, store = client
    c.post(
        "/v1/wiki/projects/acme%2Fcatalog/documents",
        json=_DOCS,
        headers={"X-Api-Key": API_KEY},
    )
    from unittest.mock import MagicMock as MM

    from mewbo_graph.wiki.retriever import HybridRetriever

    retriever = HybridRetriever(store=store, embedder=MM())
    hits = retriever.search("acme/catalog", "ergonomic chair", k=5, sources="pages")
    assert hits
    assert hits[0].metadata.get("title") == "Nimbus Office Chair"


def test_post_documents_reingest_upserts(client):
    c, store = client
    headers = {"X-Api-Key": API_KEY}
    c.post("/v1/wiki/projects/acme%2Fcatalog/documents", json=_DOCS, headers=headers)
    resp = c.post("/v1/wiki/projects/acme%2Fcatalog/documents", json=_DOCS, headers=headers)
    assert resp.status_code == 201
    assert resp.get_json()["totalDocuments"] == 2  # not 4
    assert len(store.query_graph("acme/catalog")) == 2


def test_post_documents_empty_list_is_validation_error(client):
    c, _ = client
    resp = c.post(
        "/v1/wiki/projects/acme%2Fcatalog/documents",
        json={"documents": []},
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code in (400, 422)
    assert resp.get_json()["code"] == "validation"


def test_post_documents_malformed_doc_is_validation_error(client):
    c, _ = client
    resp = c.post(
        "/v1/wiki/projects/acme%2Fcatalog/documents",
        json={"documents": [{"id": "x", "title": "no text"}]},  # missing required 'text'
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code in (400, 422)
    assert resp.get_json()["code"] == "validation"


def test_refresh_rejects_catalog_project(client):
    # A catalog (non-git) project has no repo_url → the git refresh path would
    # synthesize ``git clone <slug>`` and fail. The route must reject it cleanly.
    c, store = client
    c.post(
        "/v1/wiki/projects/acme%2Fcatalog/documents",
        json=_DOCS,
        headers={"X-Api-Key": API_KEY},
    )
    assert store.get_project("acme/catalog").repo_url is None

    resp = c.post(
        "/v1/wiki/projects/acme%2Fcatalog/refresh", headers={"X-Api-Key": API_KEY}
    )
    assert resp.status_code in (400, 422)
    body = resp.get_json()
    assert body["code"] == "validation"
    assert "documents" in body["message"]
