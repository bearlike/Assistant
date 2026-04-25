"""Tests for the Agentic Search mock API namespace."""

# mypy: ignore-errors

import pytest
from mewbo_api import backend
from mewbo_api.agentic_search import store


@pytest.fixture(autouse=True)
def _reset_store():
    """Reset the in-memory workspaces between tests."""
    store.reset_for_tests()
    yield
    store.reset_for_tests()


def _auth():
    return {"X-API-KEY": backend.MASTER_API_TOKEN}


def test_sources_requires_auth():
    """Sources endpoint must reject anonymous calls."""
    client = backend.app.test_client()
    response = client.get("/api/agentic_search/sources")
    assert response.status_code == 401


def test_list_sources():
    """Sources endpoint returns the catalog."""
    client = backend.app.test_client()
    response = client.get("/api/agentic_search/sources", headers=_auth())
    assert response.status_code == 200
    data = response.get_json()
    assert isinstance(data["sources"], list)
    assert len(data["sources"]) >= 8
    notion = next(s for s in data["sources"] if s["id"] == "notion")
    assert notion["glyph"] == "N"


def test_list_workspaces_seeded():
    """Workspaces endpoint returns the seeded demo set."""
    client = backend.app.test_client()
    response = client.get("/api/agentic_search/workspaces", headers=_auth())
    assert response.status_code == 200
    data = response.get_json()
    ids = {w["id"] for w in data["workspaces"]}
    assert {"eng-docs", "product", "research"} <= ids
    eng = next(w for w in data["workspaces"] if w["id"] == "eng-docs")
    # past_queries should be embedded so the FE never needs a second
    # round-trip just to populate autocomplete.
    assert isinstance(eng["past_queries"], list)


def test_create_and_update_workspace():
    """Create + patch round-trip returns the updated workspace shape."""
    client = backend.app.test_client()
    create = client.post(
        "/api/agentic_search/workspaces",
        json={
            "name": "QA",
            "desc": "Quality artifacts",
            "sources": ["notion", "github"],
            "instructions": "be thorough",
        },
        headers=_auth(),
    )
    assert create.status_code == 201
    new_ws = create.get_json()["workspace"]
    assert new_ws["name"] == "QA"
    assert new_ws["sources"] == ["notion", "github"]
    assert new_ws["past_queries"] == []
    new_id = new_ws["id"]

    patched = client.patch(
        f"/api/agentic_search/workspaces/{new_id}",
        json={"name": "QA renamed", "sources": ["notion"]},
        headers=_auth(),
    )
    assert patched.status_code == 200
    body = patched.get_json()["workspace"]
    assert body["name"] == "QA renamed"
    assert body["sources"] == ["notion"]
    # Untouched fields preserved
    assert body["instructions"] == "be thorough"


def test_delete_workspace():
    """Delete removes a workspace and second delete 404s."""
    client = backend.app.test_client()
    delete_first = client.delete(
        "/api/agentic_search/workspaces/eng-docs", headers=_auth()
    )
    assert delete_first.status_code == 200
    delete_again = client.delete(
        "/api/agentic_search/workspaces/eng-docs", headers=_auth()
    )
    assert delete_again.status_code == 404


def test_run_filters_by_workspace_sources():
    """Run payload restricts results and trace to the workspace's sources."""
    client = backend.app.test_client()
    response = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": "research", "query": "what's new"},
        headers=_auth(),
    )
    assert response.status_code == 200
    run = response.get_json()["run"]
    enabled = {"web", "drive", "notion"}
    sources = {r["source"] for r in run["results"]}
    assert sources <= enabled
    trace_sources = {a["source_id"] for a in run["trace"]}
    assert trace_sources <= enabled
    # Citations on bullets must not reference dropped results.
    visible_ids = {r["id"] for r in run["results"]}
    for bullet in run["answer"]["bullets"]:
        for cite in bullet["cites"]:
            assert cite in visible_ids


def test_run_appends_past_query():
    """Successful runs append to the workspace's past_queries history."""
    client = backend.app.test_client()
    before = client.get("/api/agentic_search/workspaces", headers=_auth()).get_json()
    eng_before = next(w for w in before["workspaces"] if w["id"] == "eng-docs")
    initial_count = len(eng_before["past_queries"])

    client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": "eng-docs", "query": "fresh query"},
        headers=_auth(),
    )

    after = client.get("/api/agentic_search/workspaces", headers=_auth()).get_json()
    eng_after = next(w for w in after["workspaces"] if w["id"] == "eng-docs")
    assert len(eng_after["past_queries"]) == initial_count + 1
    assert eng_after["past_queries"][0]["q"] == "fresh query"


def test_run_unknown_workspace_404s():
    """Unknown workspace ids surface a 404, not a 500."""
    client = backend.app.test_client()
    response = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": "does-not-exist", "query": "x"},
        headers=_auth(),
    )
    assert response.status_code == 404


def test_run_validates_inputs():
    """Empty query and missing workspace_id are rejected with 400."""
    client = backend.app.test_client()
    bad_query = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": "eng-docs", "query": "  "},
        headers=_auth(),
    )
    assert bad_query.status_code == 400
    bad_ws = client.post(
        "/api/agentic_search/runs",
        json={"query": "x"},
        headers=_auth(),
    )
    assert bad_ws.status_code == 400
