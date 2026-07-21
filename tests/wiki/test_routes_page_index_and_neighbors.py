"""HTTP contract for the two read/navigate wiki routes the MCP facade needs.

``GET /v1/wiki/projects/<slug>/pages`` (the page INDEX — previously there was no
way to discover a page id) and ``GET /v1/wiki/projects/<slug>/graph/neighbors``
(traversal over the stored graph, no model call). Both drive the real Flask
routes over a temp ``JsonWikiStore``.

The property under test in both is the same one: a bound applied at the QUERY
returns a complete, small, correct answer — never a severed prefix of a large one.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from mewbo_graph.wiki.types import Frontmatter, Project, WikiPage

API_KEY = "test-pages-key"
SLUG = "github.com/org/repo"


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path: Path):
    from mewbo_graph.wiki.store import JsonWikiStore

    return JsonWikiStore(root_dir=tmp_path / "wiki")


@pytest.fixture()
def client(monkeypatch, store):
    monkeypatch.setenv("MASTER_API_TOKEN", API_KEY)
    monkeypatch.setattr("mewbo_api.backend.MASTER_API_TOKEN", API_KEY, raising=False)

    import mewbo_api.wiki.routes as routes_mod
    from flask import Flask

    runtime = MagicMock()
    runtime.wiki_store = store

    app = Flask(__name__)
    app.config["TESTING"] = True
    routes_mod.register(app, runtime)

    yield app.test_client(), store

    routes_mod._runtime = None


def _seed_project(store) -> None:
    store.create_project(
        Project(
            slug=SLUG, source="github", lang="en", indexedAt="2026-01-01T00:00:00Z",
            pages=0, desc="a repo", repoUrl=f"https://{SLUG}",
        )
    )


def _seed_pages(store, titles: list[str]) -> None:
    for i, title in enumerate(titles):
        store.save_page(
            SLUG,
            WikiPage(
                id=f"page-{i}",
                title=title,
                frontmatter=Frontmatter(title=title, slug=f"page-{i}"),
                body=f"# {title}",
                toc=[],
                nav=[],
            ),
        )


def _get(client, path: str, **params):
    return client.get(path, headers={"X-API-Key": API_KEY}, query_string=params)


# ── GET /projects/<slug>/pages ────────────────────────────────────────────────


def test_page_index_lists_id_and_title_sorted(client):
    api, store = client
    _seed_project(store)
    _seed_pages(store, ["Zeta", "Alpha", "Mid"])

    resp = _get(api, f"/v1/wiki/projects/{SLUG}/pages")

    assert resp.status_code == 200
    body = resp.get_json()
    assert [p["title"] for p in body["pages"]] == ["Alpha", "Mid", "Zeta"]
    assert all(set(p) == {"id", "title"} for p in body["pages"])
    assert body["total"] == 3
    assert body["truncated"] is False
    assert body["nextOffset"] is None


def test_page_index_pages_forward_instead_of_cutting(client):
    """A bounded reply is COMPLETE for its window and hands back a continuation."""
    api, store = client
    _seed_project(store)
    _seed_pages(store, ["Alpha", "Beta", "Gamma", "Delta"])

    first = _get(api, f"/v1/wiki/projects/{SLUG}/pages", limit=2).get_json()
    assert [p["title"] for p in first["pages"]] == ["Alpha", "Beta"]
    assert first["count"] == 2
    assert first["total"] == 4
    assert first["truncated"] is True
    assert first["nextOffset"] == 2

    second = _get(
        api, f"/v1/wiki/projects/{SLUG}/pages", limit=2, offset=first["nextOffset"]
    ).get_json()
    assert [p["title"] for p in second["pages"]] == ["Delta", "Gamma"]
    assert second["truncated"] is False
    assert second["nextOffset"] is None


def test_page_index_filters_by_title_substring_case_insensitively(client):
    api, store = client
    _seed_project(store)
    _seed_pages(store, ["Authentication", "Routing", "AUTH tokens"])

    body = _get(api, f"/v1/wiki/projects/{SLUG}/pages", titleContains="auth").get_json()

    assert [p["title"] for p in body["pages"]] == ["AUTH tokens", "Authentication"]
    assert body["total"] == 2  # total counts the FILTERED set, not the whole wiki


def test_page_index_rejects_an_out_of_range_limit(client):
    """The ceiling is enforced at the boundary, not silently clamped."""
    api, store = client
    _seed_project(store)

    resp = _get(api, f"/v1/wiki/projects/{SLUG}/pages", limit=5000)

    assert resp.status_code == 400
    assert resp.get_json()["code"] == "validation"
    assert "limit" in resp.get_json()["fields"]


def test_page_index_rejects_an_unknown_query_param(client):
    """`extra="forbid"` — a typo'd param is a clean 400, never a silent no-op."""
    api, store = client
    _seed_project(store)

    resp = _get(api, f"/v1/wiki/projects/{SLUG}/pages", titelContains="auth")

    assert resp.status_code == 400
    assert resp.get_json()["code"] == "validation"


def test_page_index_tolerates_the_query_param_api_key(client):
    """`api_key` is auth transport, not a query argument — it must not 400."""
    api, store = client
    _seed_project(store)
    _seed_pages(store, ["Alpha"])

    resp = api.get(f"/v1/wiki/projects/{SLUG}/pages", query_string={"api_key": API_KEY})

    assert resp.status_code == 200
    assert [p["title"] for p in resp.get_json()["pages"]] == ["Alpha"]


def test_page_index_404s_for_an_unknown_project(client):
    api, _store = client

    resp = _get(api, "/v1/wiki/projects/github.com/org/ghost/pages")

    assert resp.status_code == 404
    assert resp.get_json()["code"] == "not_found"


def test_page_index_requires_auth(client):
    api, store = client
    _seed_project(store)

    assert api.get(f"/v1/wiki/projects/{SLUG}/pages").status_code == 401


def test_page_index_of_a_project_with_no_pages_is_empty_not_an_error(client):
    """A graph-only project has no pages — that is a valid answer, not a 409."""
    api, store = client
    _seed_project(store)

    body = _get(api, f"/v1/wiki/projects/{SLUG}/pages").get_json()

    assert body == {
        "pages": [],
        "count": 0,
        "total": 0,
        "truncated": False,
        "nextOffset": None,
    }


# ── GET /projects/<slug>/graph/neighbors ──────────────────────────────────────


def _seed_graph(store) -> None:
    """A tiny call graph: root → mid → leaf, plus an unrelated island."""
    from mewbo_graph.wiki.types import GraphEdge, make_graph_node

    nodes = [
        make_graph_node(
            node_id=nid, slug=SLUG, type="Function", name=nid, file="app.py", range=(0, 1)
        )
        for nid in ("root", "mid", "leaf", "island")
    ]
    store.upsert_nodes(SLUG, nodes)
    store.upsert_edges(
        SLUG,
        [
            GraphEdge(slug=SLUG, source="root", target="mid", type="CALLS"),
            GraphEdge(slug=SLUG, source="mid", target="leaf", type="CALLS"),
            GraphEdge(slug=SLUG, source="root", target="mid", type="REFERENCES"),
        ],
    )


def test_neighbors_walks_one_hop_by_default(client):
    api, store = client
    _seed_project(store)
    _seed_graph(store)

    body = _get(api, f"/v1/wiki/projects/{SLUG}/graph/neighbors", node_id="root").get_json()

    assert {n["node_id"] for n in body["nodes"]} == {"root", "mid"}
    assert body["hops_reached"] == 1
    assert body["truncated"] is False


def test_neighbors_honours_hops_direction_and_edge_kind(client):
    api, store = client
    _seed_project(store)
    _seed_graph(store)

    two_hop = _get(
        api,
        f"/v1/wiki/projects/{SLUG}/graph/neighbors",
        node_id="root",
        hops=2,
        direction="out",
        edge_kind="CALLS",
    ).get_json()
    assert {n["node_id"] for n in two_hop["nodes"]} == {"root", "mid", "leaf"}

    inbound = _get(
        api,
        f"/v1/wiki/projects/{SLUG}/graph/neighbors",
        node_id="leaf",
        direction="in",
    ).get_json()
    assert {n["node_id"] for n in inbound["nodes"]} == {"leaf", "mid"}
    assert "island" not in {n["node_id"] for n in inbound["nodes"]}


def test_neighbors_reports_truncation_rather_than_overrunning_the_limit(client):
    api, store = client
    _seed_project(store)
    _seed_graph(store)

    body = _get(
        api, f"/v1/wiki/projects/{SLUG}/graph/neighbors", node_id="root", hops=3, limit=1
    ).get_json()

    assert body["truncated"] is True
    assert len(body["nodes"]) <= 2  # the seed plus at most the one it was allowed


def test_neighbors_reuses_the_shared_args_bounds(client):
    """`hops<=3` / `limit<=500` are enforced because the model IS the contract.

    Restating them route-side is exactly the drift this reuse exists to prevent,
    so the assertion is that the SHARED model's bound is what rejects the request.
    """
    from mewbo_graph.plugins.wiki.graph_neighbors import WikiGraphNeighborsArgs

    api, store = client
    _seed_project(store)
    _seed_graph(store)

    assert WikiGraphNeighborsArgs.model_fields["hops"].default == 1
    assert WikiGraphNeighborsArgs.model_fields["limit"].default == 50

    for params in ({"hops": 9}, {"limit": 9000}, {"direction": "sideways"}):
        resp = _get(
            api, f"/v1/wiki/projects/{SLUG}/graph/neighbors", node_id="root", **params
        )
        assert resp.status_code == 400, params
        assert resp.get_json()["code"] == "validation"


def test_neighbors_rejects_an_unknown_query_param(client):
    """`extra="forbid"` on the shared model reaches the HTTP boundary for free."""
    api, store = client
    _seed_project(store)
    _seed_graph(store)

    resp = _get(
        api, f"/v1/wiki/projects/{SLUG}/graph/neighbors", node_id="root", depth=2
    )

    assert resp.status_code == 400
    assert resp.get_json()["code"] == "validation"


def test_neighbors_requires_a_node_id(client):
    api, store = client
    _seed_project(store)
    _seed_graph(store)

    resp = _get(api, f"/v1/wiki/projects/{SLUG}/graph/neighbors")

    assert resp.status_code == 400
    assert "node_id" in resp.get_json()["fields"]


def test_neighbors_404s_for_an_unknown_project(client):
    api, _store = client

    resp = _get(api, "/v1/wiki/projects/github.com/org/ghost/graph/neighbors", node_id="x")

    assert resp.status_code == 404
    assert resp.get_json()["code"] == "not_found"


def test_neighbors_requires_auth(client):
    api, store = client
    _seed_project(store)

    resp = api.get(f"/v1/wiki/projects/{SLUG}/graph/neighbors", query_string={"node_id": "x"})

    assert resp.status_code == 401
