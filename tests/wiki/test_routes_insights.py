"""Route tests for POST /v1/wiki/projects/<slug>/insights."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

API_KEY = "test-key-123"


@pytest.fixture()
def store(tmp_path: Path):
    from mewbo_graph.wiki.store import JsonWikiStore
    from mewbo_graph.wiki.types import GraphNode, Project

    s = JsonWikiStore(root_dir=tmp_path / "wiki")
    s.create_project(
        Project(slug="org/repo", source="github", lang="Python", indexed_at="t", pages=1, desc="d")
    )
    code = GraphNode(
        slug="org/repo", node_id="cA", type="Class", name="AuthService",
        file="auth.py", range=(0, 9),
    )
    s.upsert_nodes("org/repo", [code])
    return s


@pytest.fixture()
def client(monkeypatch, store):
    monkeypatch.setenv("MASTER_API_TOKEN", API_KEY)
    monkeypatch.setattr("mewbo_api.backend.MASTER_API_TOKEN", API_KEY, raising=False)

    # No real embedder/LLM in tests → BM25-only, no condense.
    import mewbo_api.wiki.routes as routes_mod
    import mewbo_graph.wiki.embedder as embedder_mod
    from flask import Flask
    from mewbo_api.wiki.routes import register

    monkeypatch.setattr(embedder_mod, "make_embedder_or_none", lambda: None)
    monkeypatch.setattr(routes_mod, "_make_insight_llm", lambda: None)

    app = Flask(__name__)
    app.config["TESTING"] = True
    register(app, SimpleNamespace(wiki_store=store))
    yield app.test_client(), store, routes_mod
    routes_mod._runtime = None


def _hdr():
    return {"X-API-Key": API_KEY}


def test_post_insight_stores_memory(client) -> None:
    c, store, _ = client
    resp = c.post(
        "/v1/wiki/projects/org/repo/insights",
        json={"content": "AuthService verifies bearer tokens", "anchors": ["auth.py#AuthService"]},
        headers=_hdr(),
    )
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["claims"][0]["action"] == "created"
    node_id = data["claims"][0]["node_id"]
    node = store.get_memory_node("org/repo", node_id)
    assert node is not None and node.provenance.source == "on_demand"
    edges = store.list_memory_edges("org/repo", node_id=node_id)
    anchors = [e.target for e in edges if e.type == "ANCHORS"]
    assert anchors == ["auth.py#AuthService"]


def test_post_insight_unknown_project_404(client) -> None:
    c, _, _ = client
    resp = c.post("/v1/wiki/projects/ghost/repo/insights", json={"content": "x"}, headers=_hdr())
    assert resp.status_code == 404


def test_post_insight_missing_content_400(client) -> None:
    c, _, _ = client
    resp = c.post("/v1/wiki/projects/org/repo/insights", json={}, headers=_hdr())
    assert resp.status_code == 400


def test_post_insight_invalid_kind_400(client) -> None:
    c, _, _ = client
    resp = c.post(
        "/v1/wiki/projects/org/repo/insights",
        json={"content": "x", "kind": "bogus"},
        headers=_hdr(),
    )
    assert resp.status_code == 400


def test_post_insight_requires_auth(client) -> None:
    c, _, _ = client
    resp = c.post("/v1/wiki/projects/org/repo/insights", json={"content": "x"})
    assert resp.status_code == 401


def test_post_insight_overlong_content_rejected_200(client) -> None:
    c, store, _ = client
    resp = c.post(
        "/v1/wiki/projects/org/repo/insights",
        json={"content": "x" * 250},
        headers=_hdr(),
    )
    # well-formed request, but the claim is rejected → 200 with ok:false
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["claims"][0]["action"] == "rejected"
    assert store.query_memory("org/repo") == []


def test_post_insight_condense_with_fake_llm(client, monkeypatch) -> None:
    c, store, routes_mod = client
    from types import SimpleNamespace as NS

    class FakeLLM:
        def invoke(self, prompt):  # noqa: ARG002
            return NS(content="- AuthService verifies tokens\n- Tokens expire after one hour")

    monkeypatch.setattr(routes_mod, "_make_insight_llm", lambda: FakeLLM())
    resp = c.post(
        "/v1/wiki/projects/org/repo/insights",
        json={"raw": "auth notes blob", "condense": True},
        headers=_hdr(),
    )
    assert resp.status_code == 201
    assert len(resp.get_json()["claims"]) == 2
    assert len(store.query_memory("org/repo")) == 2
