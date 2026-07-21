"""Route-level tests for the Agentic Search run lifecycle + SSE stream."""

# mypy: ignore-errors

import pytest
from mewbo_api.agentic_search import store


@pytest.fixture(autouse=True)
def _reset_store():
    """Reset the seeded in-memory store between tests."""
    store.reset_for_tests()
    yield
    store.reset_for_tests()


def _start_run(client, auth_headers, workspace_id="eng-docs", query="fresh query"):
    """POST a run and return the decoded ``run`` payload."""
    response = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": workspace_id, "query": query},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def test_get_run_snapshot_after_post(client, auth_headers):
    """GET /runs/<id> returns the persisted snapshot after a POST /runs."""
    started = _start_run(client, auth_headers)
    run_id = started["run_id"]

    snap = client.get(f"/api/agentic_search/runs/{run_id}", headers=auth_headers)
    assert snap.status_code == 200
    record = snap.get_json()["run"]
    assert record["run_id"] == run_id
    assert record["status"] == "completed"
    assert record["payload"] is not None
    assert record["payload"]["query"] == "fresh query"


def test_get_run_snapshot_is_self_sufficient(client, auth_headers):
    """GET /runs/<id> carries everything a cold deep-link needs to render.

    The shareable ``/search?ws=…&run=…`` URL opens with a single
    ``GET /runs/<id>`` (snapshot) + SSE attach — never a POST — so the snapshot
    must be self-sufficient for a browser with no other context: top-level
    workspace_id / query / tier / status / created_at / session_id plus the
    result/answer payload block. This locks the deep-link contract additively
    (the console reads these top-level; do not move them under ``payload``).
    """
    started = _start_run(client, auth_headers, workspace_id="eng-docs", query="deep link q")
    run_id = started["run_id"]

    snap = client.get(f"/api/agentic_search/runs/{run_id}", headers=auth_headers)
    assert snap.status_code == 200
    record = snap.get_json()["run"]
    # Top-level identity + render context — no second request required.
    assert record["run_id"] == run_id
    assert record["workspace_id"] == "eng-docs"
    assert record["query"] == "deep link q"
    assert record["tier"] in {"fast", "auto", "deep"}
    assert record["status"] == "completed"
    assert record["created_at"]
    # session_id links the URL-addressed run to its auditable session.
    assert record["session_id"]
    # The result/answer payload is present and itself self-describing.
    payload = record["payload"]
    assert payload is not None
    assert payload["workspace_id"] == "eng-docs"
    assert payload["query"] == "deep link q"
    assert payload["session_id"] == record["session_id"]
    assert payload["tier"] == record["tier"]
    assert "answer" in payload
    assert "results" in payload


def test_get_run_survives_cold_store(client, auth_headers):
    """A run snapshot is durable: a fresh store over the same dir still reads it.

    Models an api restart / a second worker — the run store is file/Mongo
    backed, not memory-only, so a shared URL must not 404 after a deploy. We
    drive a real run through the routes, then re-open a brand-new
    ``JsonAgenticSearchStore`` pointed at the SAME root and confirm the terminal
    snapshot (incl. the persisted payload) is intact.
    """
    started = _start_run(client, auth_headers, query="durable q")
    run_id = started["run_id"]

    live = store.get_store()
    cold = store.JsonAgenticSearchStore(root_dir=live.root_dir)
    record = cold.get_run(run_id)
    assert record is not None
    assert record.run_id == run_id
    assert record.query == "durable q"
    assert record.status == "completed"
    assert record.payload is not None
    assert record.payload.query == "durable q"


def test_get_run_no_per_user_scoping(client, auth_headers):
    """The snapshot read has no per-session/per-user scoping (shareable URL).

    Any holder of a valid API key resolves the same run by id — the contract a
    multi-user shared URL relies on. We assert the route resolves a run created
    in one request from an independent request with the same credential and no
    extra context (workspace/session are NOT required in the GET).
    """
    run_id = _start_run(client, auth_headers, workspace_id="eng-docs")["run_id"]
    # A second, independent GET with only the run id + the api key resolves it.
    snap = client.get(f"/api/agentic_search/runs/{run_id}", headers=auth_headers)
    assert snap.status_code == 200
    assert snap.get_json()["run"]["run_id"] == run_id


def test_get_run_unknown_404s(client, auth_headers):
    """GET /runs/<id> for an unknown id is a clean 404 envelope, not a 500."""
    resp = client.get("/api/agentic_search/runs/run-does-not-exist", headers=auth_headers)
    assert resp.status_code == 404
    body = resp.get_json()
    # A structured JSON body (never a raw Werkzeug HTML 500/404 page).
    assert isinstance(body, dict)
    assert "message" in body


def test_cancel_completed_run_returns_cancelled_false(client, auth_headers):
    """Cancelling an already-completed echo run reports cancelled=False."""
    run_id = _start_run(client, auth_headers)["run_id"]
    resp = client.post(f"/api/agentic_search/runs/{run_id}/cancel", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["run_id"] == run_id
    # Echo runs complete synchronously, so cancel is a no-op (terminal already).
    assert body["cancelled"] is False


def test_cancel_unknown_run_404s(client, auth_headers):
    """Cancelling an unknown run is a 404."""
    resp = client.post(
        "/api/agentic_search/runs/run-missing/cancel", headers=auth_headers
    )
    assert resp.status_code == 404


def test_sources_carry_available_and_tool_ids(client, auth_headers):
    """GET /sources?project=x returns entries with available + tool_ids keys."""
    resp = client.get("/api/agentic_search/sources?project=demo", headers=auth_headers)
    assert resp.status_code == 200
    sources = resp.get_json()["sources"]
    assert sources
    for entry in sources:
        assert "available" in entry
        assert "tool_ids" in entry
        assert isinstance(entry["tool_ids"], list)
    notion = next(s for s in sources if s["id"] == "notion")
    assert notion["available"] is True
    assert notion["tool_ids"] == ["notion_search", "notion_fetch"]


def test_run_events_stream_is_event_stream(client, auth_headers, monkeypatch):
    """GET /runs/<id>/events streams text/event-stream from run_started..run_done."""
    # Keep the idle loop tiny so the generator can never hang in CI.
    monkeypatch.setenv("MEWBO_AGENTIC_SSE_SLEEP", "0.01")
    monkeypatch.setenv("MEWBO_AGENTIC_SSE_MAX_IDLE", "3")

    run_id = _start_run(client, auth_headers)["run_id"]

    resp = client.get(
        f"/api/agentic_search/runs/{run_id}/events", headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    body = resp.get_data(as_text=True)
    assert "event: run_started" in body
    assert "event: run_done" in body
    # Terminal event closes the stream, so run_done is the final search event.
    assert body.index("event: run_started") < body.index("event: run_done")


def test_run_events_unknown_run_404s(client, auth_headers, monkeypatch):
    """SSE endpoint 404s for an unknown run id before opening a stream."""
    monkeypatch.setenv("MEWBO_AGENTIC_SSE_SLEEP", "0.01")
    monkeypatch.setenv("MEWBO_AGENTIC_SSE_MAX_IDLE", "3")
    resp = client.get("/api/agentic_search/runs/ghost/events", headers=auth_headers)
    assert resp.status_code == 404


def test_workspace_runs_lists_run_after_post(client, auth_headers):
    """GET /workspaces/<id>/runs lists the run created by a POST /runs."""
    run_id = _start_run(client, auth_headers, workspace_id="eng-docs")["run_id"]

    resp = client.get(
        "/api/agentic_search/workspaces/eng-docs/runs", headers=auth_headers
    )
    assert resp.status_code == 200
    runs = resp.get_json()["runs"]
    run_ids = {r["run_id"] for r in runs}
    assert run_id in run_ids
    listed = next(r for r in runs if r["run_id"] == run_id)
    assert listed["workspace_id"] == "eng-docs"


# ── GET /runs?limit= — cross-workspace recent runs ───────────────────────────


def test_recent_runs_newest_first_across_workspaces(client, auth_headers):
    """GET /runs lists runs from every workspace, newest first, projected to
    the contract fields (run_id/workspace_id/query/created_at/status)."""
    first = _start_run(client, auth_headers, workspace_id="eng-docs", query="first q")
    second = _start_run(client, auth_headers, workspace_id="product", query="second q")

    resp = client.get("/api/agentic_search/runs", headers=auth_headers)
    assert resp.status_code == 200
    runs = resp.get_json()["runs"]
    run_ids = [r["run_id"] for r in runs]
    # Newest (second) run sorts before the older (first) one.
    assert run_ids.index(second["run_id"]) < run_ids.index(first["run_id"])

    listed = next(r for r in runs if r["run_id"] == second["run_id"])
    assert listed == {
        "run_id": second["run_id"],
        "workspace_id": "product",
        "query": "second q",
        "created_at": listed["created_at"],
        "status": "completed",
    }


def test_recent_runs_respects_limit_query_param(client, auth_headers):
    """?limit= caps the count returned."""
    for i in range(3):
        _start_run(client, auth_headers, workspace_id="eng-docs", query=f"q{i}")

    resp = client.get("/api/agentic_search/runs?limit=2", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.get_json()["runs"]) == 2


def _spy_on_list_recent_runs(monkeypatch) -> dict:
    """Patch the live store's ``list_recent_runs`` to record the *limit* it
    was called with, while still delegating to the real implementation.

    A response-shape assertion alone can't distinguish "clamped to 100" from
    "unclamped, only N runs exist" — this proves what the store actually
    received.
    """
    captured: dict = {}
    live_store = store.get_store()
    store_cls = type(live_store)
    original = store_cls.list_recent_runs

    def _spy(self, limit):
        captured["limit"] = limit
        return original(self, limit)

    monkeypatch.setattr(store_cls, "list_recent_runs", _spy)
    return captured


def test_recent_runs_limit_is_capped_sanely(client, auth_headers, monkeypatch):
    """An oversized ?limit= is clamped to the max BEFORE reaching the store."""
    from mewbo_api.agentic_search import routes

    captured = _spy_on_list_recent_runs(monkeypatch)

    resp = client.get("/api/agentic_search/runs?limit=999999", headers=auth_headers)
    assert resp.status_code == 200
    assert captured["limit"] == routes._RECENT_RUNS_MAX_LIMIT == 100


def test_recent_runs_default_limit_reaches_the_store(client, auth_headers, monkeypatch):
    """A bare GET /runs (no ?limit=) hands the store the configured default."""
    from mewbo_api.agentic_search import routes

    captured = _spy_on_list_recent_runs(monkeypatch)

    resp = client.get("/api/agentic_search/runs", headers=auth_headers)
    assert resp.status_code == 200
    assert captured["limit"] == routes._RECENT_RUNS_DEFAULT_LIMIT == 30


def test_recent_runs_defaults_when_no_limit_given(client, auth_headers):
    """A bare GET /runs (no ?limit=) still returns newest-first summaries."""
    run_id = _start_run(client, auth_headers, workspace_id="eng-docs")["run_id"]

    resp = client.get("/api/agentic_search/runs", headers=auth_headers)
    assert resp.status_code == 200
    assert any(r["run_id"] == run_id for r in resp.get_json()["runs"])


def test_recent_runs_requires_auth(client):
    """No API key → 401, matching every other route on the namespace."""
    resp = client.get("/api/agentic_search/runs")
    assert resp.status_code == 401


def test_post_runs_still_works_beside_get_runs(client, auth_headers):
    """POST /runs (create) and GET /runs (list) coexist on the same path."""
    started = _start_run(client, auth_headers, workspace_id="eng-docs")
    assert started["status"] == "completed"
    resp = client.get("/api/agentic_search/runs", headers=auth_headers)
    assert resp.status_code == 200


def test_swagger_runs_path_lists_both_verbs(client):
    """/swagger.json must document BOTH verbs on /runs — regression guard.

    Flask-RESTX's swagger builder keys ``paths`` by URL: two ``Resource``
    classes routed at the same path (``@ns.route("/runs")`` twice) dispatch
    correctly at the Werkzeug level, but the second-registered class silently
    OVERWRITES the first in the emitted spec, dropping its doc models from
    ``docs/openapi.json`` / the Scalar reference with no error anywhere. GET
    and POST must live on ONE ``Resource`` class (see ``RunsResource``); this
    pins the swagger output so that trap can't return unnoticed.
    """
    spec = client.get("/swagger.json").get_json()
    path = spec["paths"].get("/api/agentic_search/runs")
    assert path is not None, "swagger spec is missing /api/agentic_search/runs entirely"
    assert set(path.keys()) == {"get", "post"}
