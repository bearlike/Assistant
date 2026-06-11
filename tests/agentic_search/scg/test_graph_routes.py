"""Route-level tests for the workspace SCG graph view (#79).

Drives the real Flask app through its test client, with the run store + SCG
structure store swapped to fresh JSON backends under tmp dirs. The graph
namespace's captured ``_runtime`` is pointed at a fresh JSON wiki store so the
memory layer assembles for real. NOTHING is mocked at the view seam — the route
exercises the real ``ScgGraphView.for_scope(...).to_wire()`` path (#76).

Covers:

* ``GET /workspaces/<id>/graph`` returns the layer-tagged multiplex scoped to
  the workspace's enabled sources (a node from an out-of-scope source is absent);
* schema-edge endpoints are normalized to node ids (cytoscape-joinable), never
  left as raw ``source_key``s;
* ``auth_scope`` is redacted — the descriptor string never reaches the wire;
* an unmapped workspace source surfaces as a ghost node + in ``stats.unmapped``;
* a disabled SCG degrades to the empty-schema + all-unmapped payload (200, not
  503);
* an unknown workspace 404s.

NEVER spawns a real LLM/session.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mewbo_api import backend
from mewbo_api.agentic_search import graph_routes, store as store_mod
from mewbo_api.agentic_search.routes import ScgConfig
from mewbo_api.agentic_search.schemas import WorkspaceInput
from mewbo_graph.scg import store as scg_store_mod
from mewbo_graph.scg.types import ScgEdge, ScgNode


@pytest.fixture(autouse=True)
def _reset_stores(tmp_path: Path):
    """Reset the run + SCG structure stores; point the graph runtime at a wiki store."""
    store_mod.reset_for_tests()
    scg_store_mod.reset_for_tests()
    # The graph namespace captured backend's runtime at init; give it a fresh
    # JSON wiki store so the memory layer has a real (empty) substrate to read.
    from mewbo_graph.wiki.store import JsonWikiStore

    prev_runtime = graph_routes._runtime
    prev_wiki = getattr(prev_runtime, "wiki_store", None) if prev_runtime else None
    if prev_runtime is not None:
        prev_runtime.wiki_store = JsonWikiStore(root_dir=tmp_path / "wiki")
    yield
    if prev_runtime is not None:
        prev_runtime.wiki_store = prev_wiki
    store_mod.reset_for_tests()
    scg_store_mod.reset_for_tests()


@pytest.fixture
def _scg_on(monkeypatch):
    """Force the ``scg.enabled`` gate ON."""
    monkeypatch.setattr(ScgConfig, "enabled", staticmethod(lambda: True))


def _auth() -> dict[str, str]:
    return {"X-API-KEY": backend.MASTER_API_TOKEN}


def _seed_two_source_scg() -> None:
    """A github (in-scope) + slack (out-of-scope) SCG, mirroring test_graph_view."""
    scg = scg_store_mod.get_scg_store()
    scg.upsert_nodes(
        [
            ScgNode(
                source_key="github#Repo",
                kind="entity_type",
                source_id="github",
                name="Repo",
                doc="A repository.",
                auth_scope="oauth:repo",
            ),
            ScgNode(
                source_key="github#search",
                kind="capability",
                source_id="github",
                name="search",
            ),
            ScgNode(
                source_key="slack#Channel",
                kind="entity_type",
                source_id="slack",
                name="Channel",
            ),
        ]
    )
    scg.upsert_edges(
        [
            ScgEdge(source="github#search", target="github#Repo", kind="PRODUCES"),
            ScgEdge(source="github#Repo", target="slack#Channel", kind="RESOLVES_TO"),
        ]
    )


def _make_workspace(sources: list[str]) -> str:
    """Create a workspace with *sources* selected; return its id."""
    store = store_mod.get_store()
    ws = store.create_workspace(
        WorkspaceInput(
            name="Eng", desc="engineering", sources=sources, instructions=""
        )
    )
    return ws.id


# ── happy path ───────────────────────────────────────────────────────────────


def test_graph_scoped_to_workspace_sources(_scg_on):
    """A github-only workspace surfaces github schema nodes, not slack's."""
    _seed_two_source_scg()
    ws_id = _make_workspace(["github"])

    client = backend.app.test_client()
    resp = client.get(f"/api/agentic_search/workspaces/{ws_id}/graph", headers=_auth())
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()

    assert body["scope"] == ["github"]
    schema_labels = {
        n["data"]["label"]
        for n in body["nodes"]
        if n["data"].get("layer") == "schema" and not n["data"].get("unmapped")
    }
    assert schema_labels == {"Repo", "search"}
    # slack is out of the github-only scope — its Channel node never appears.
    assert "Channel" not in schema_labels


def test_schema_edge_endpoints_are_node_ids(_scg_on):
    """Schema edges are remapped from source_key → node_id so the FE can join them."""
    _seed_two_source_scg()
    ws_id = _make_workspace(["github"])

    client = backend.app.test_client()
    body = client.get(
        f"/api/agentic_search/workspaces/{ws_id}/graph", headers=_auth()
    ).get_json()

    node_ids = {n["data"]["id"] for n in body["nodes"]}
    schema_edges = [e for e in body["edges"] if e["data"].get("layer") == "schema"]
    assert schema_edges, "expected the in-scope PRODUCES edge"
    for e in schema_edges:
        # No raw source_keys ('#') leaked; both endpoints are real payload nodes.
        assert "#" not in e["data"]["source"]
        assert "#" not in e["data"]["target"]
        assert e["data"]["source"] in node_ids
        assert e["data"]["target"] in node_ids


def test_auth_scope_redacted_off_wire(_scg_on):
    """The redacted auth descriptor never appears anywhere in the payload."""
    _seed_two_source_scg()
    ws_id = _make_workspace(["github"])

    client = backend.app.test_client()
    raw = client.get(
        f"/api/agentic_search/workspaces/{ws_id}/graph", headers=_auth()
    ).get_data(as_text=True)
    assert "oauth:repo" not in raw


# ── ghost / unmapped state ───────────────────────────────────────────────────


def test_unmapped_source_is_a_ghost_node(_scg_on):
    """A selected source with no SCG nodes surfaces as an unmapped ghost node."""
    _seed_two_source_scg()
    # 'notion' is selected but never mapped into the SCG.
    ws_id = _make_workspace(["github", "notion"])

    client = backend.app.test_client()
    body = client.get(
        f"/api/agentic_search/workspaces/{ws_id}/graph", headers=_auth()
    ).get_json()

    assert "notion" in body["stats"]["unmapped"]
    ghosts = [n for n in body["nodes"] if n["data"].get("unmapped")]
    ghost_sources = {n["data"]["sourceId"] for n in ghosts}
    assert "notion" in ghost_sources
    # github IS mapped, so it is NOT a ghost.
    assert "github" not in body["stats"]["unmapped"]


def test_disabled_scg_degrades_to_all_unmapped(monkeypatch):
    """SCG off → empty schema + every source unmapped (200, never 503)."""
    monkeypatch.setattr(ScgConfig, "enabled", staticmethod(lambda: False))
    ws_id = _make_workspace(["github", "slack"])

    client = backend.app.test_client()
    resp = client.get(
        f"/api/agentic_search/workspaces/{ws_id}/graph", headers=_auth()
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert sorted(body["stats"]["unmapped"]) == ["github", "slack"]
    # No schema nodes — every node is an unmapped ghost.
    assert all(n["data"].get("unmapped") for n in body["nodes"])
    assert body["edges"] == []


# ── 404 ──────────────────────────────────────────────────────────────────────


def test_unknown_workspace_404s(_scg_on):
    """An unknown workspace id is a clean 404, not a 500."""
    client = backend.app.test_client()
    resp = client.get(
        "/api/agentic_search/workspaces/does-not-exist/graph", headers=_auth()
    )
    assert resp.status_code == 404
