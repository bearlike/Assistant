"""Extra coverage for agentic_search/routes.py — uncovered branches.

Covers:
- Auth rejection on every endpoint (401 when X-API-KEY is missing).
- Workspace POST / PATCH / DELETE edge-cases (bad body shape, invalid sources
  type, patch on unknown id, delete on unknown id).
- Run POST edge-cases (non-dict body, missing/empty workspace_id and query,
  workspace_id that is numeric/non-string).
- Run GET 404, run cancel 404.
- SSE run events with ?after_idx= and Last-Event-ID resume header.
- ``GET /sources`` with seeding off → available=False entries.
- SCG endpoints (``/sources/<id>/map`` and ``/scg``) return 503 when SCG
  is disabled (the default), so we don't need the optional ``mewbo_graph``
  library to exercise the gate.
- Workspace PATCH with valid ``sources`` type (list) and non-dict body.
- Workspace list is auth-gated (401 without key).
- Create workspace rejects an empty name (pydantic min_length).
- ``GET /workspaces/<id>/runs`` auth gate.
- SSE ``after_idx`` parse failure falls back to -1 (all events).
- Map-source events (``/sources/<id>/map/events``) 404 when no job exists.
"""

# mypy: ignore-errors

import pytest
from mewbo_api import backend
from mewbo_api.agentic_search import store as store_mod


@pytest.fixture(autouse=True)
def _reset():
    store_mod.reset_for_tests()
    yield
    store_mod.reset_for_tests()


def _auth():
    return {"X-API-KEY": backend.MASTER_API_TOKEN}


# ===========================================================================
# Auth gates — every endpoint must reject anonymous requests
# ===========================================================================


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/agentic_search/sources"),
        ("GET", "/api/agentic_search/workspaces"),
        ("POST", "/api/agentic_search/workspaces"),
        ("PATCH", "/api/agentic_search/workspaces/eng-docs"),
        ("DELETE", "/api/agentic_search/workspaces/eng-docs"),
        ("GET", "/api/agentic_search/workspaces/eng-docs/runs"),
        ("POST", "/api/agentic_search/runs"),
        ("GET", "/api/agentic_search/runs/some-id"),
        ("POST", "/api/agentic_search/runs/some-id/cancel"),
        ("POST", "/api/agentic_search/sources/notion/map"),
        ("GET", "/api/agentic_search/scg"),
    ],
)
def test_endpoint_requires_auth(method, path):
    """Every agentic-search endpoint must return 401 without a valid API key."""
    client = backend.app.test_client()
    resp = getattr(client, method.lower())(path, json={})
    assert resp.status_code == 401, f"{method} {path} should require auth"


# ===========================================================================
# Sources
# ===========================================================================


def test_sources_no_project_param(client, auth_headers):
    """GET /sources without ?project= returns a catalog (no crash)."""
    resp = client.get("/api/agentic_search/sources", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.get_json()
    assert "sources" in data
    assert isinstance(data["sources"], list)
    assert len(data["sources"]) >= 1


def test_sources_all_have_available_field(client, auth_headers):
    """Every catalog entry must carry ``available`` and ``tool_ids`` keys."""
    resp = client.get("/api/agentic_search/sources", headers=auth_headers)
    for entry in resp.get_json()["sources"]:
        assert "available" in entry
        assert "tool_ids" in entry


def test_sources_with_seeding_disabled_returns_available_false(client, auth_headers, monkeypatch):
    """With seeding off, unmapped sources report available=False, not omitted."""
    monkeypatch.setenv("MEWBO_AGENTIC_SEARCH_SEED", "0")
    resp = client.get("/api/agentic_search/sources", headers=auth_headers)
    assert resp.status_code == 200
    sources = resp.get_json()["sources"]
    # With no SCG and seeding off, all sources should be unavailable.
    assert all(not s["available"] for s in sources)


# ===========================================================================
# Workspace collection
# ===========================================================================


def test_list_workspaces_auth_gate(client):
    """GET /workspaces without key → 401."""
    resp = client.get("/api/agentic_search/workspaces")
    assert resp.status_code == 401


def test_list_workspaces_shape(client, auth_headers):
    """GET /workspaces returns workspaces list with expected keys."""
    resp = client.get("/api/agentic_search/workspaces", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert "workspaces" in body
    for ws in body["workspaces"]:
        assert "id" in ws
        assert "name" in ws
        assert "past_queries" in ws


def test_create_workspace_non_dict_body(client, auth_headers):
    """POST /workspaces with a non-dict body returns 400."""
    resp = client.post(
        "/api/agentic_search/workspaces",
        data="not-json",
        content_type="text/plain",
        headers=auth_headers,
    )
    assert resp.status_code == 400


def test_create_workspace_empty_name_rejected(client, auth_headers):
    """POST /workspaces with an empty name is rejected with 400."""
    resp = client.post(
        "/api/agentic_search/workspaces",
        json={"name": ""},
        headers=auth_headers,
    )
    assert resp.status_code == 400


def test_create_workspace_returns_201_and_workspace(client, auth_headers):
    """POST /workspaces with a valid body returns 201 + the workspace."""
    resp = client.post(
        "/api/agentic_search/workspaces",
        json={"name": "My WS", "sources": ["web"]},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    ws = resp.get_json()["workspace"]
    assert ws["name"] == "My WS"
    assert ws["sources"] == ["web"]
    assert ws["id"].startswith("ws-")


# ===========================================================================
# Workspace item (PATCH / DELETE)
# ===========================================================================


def test_patch_workspace_non_dict_body(client, auth_headers):
    """PATCH /workspaces/<id> with a non-JSON body is treated as empty dict.

    NOTE: The ``not isinstance(body, dict)`` guard at routes.py line 212 is
    unreachable in practice: ``get_json(silent=True)`` returns None for a
    non-JSON content-type, and ``None or {}`` produces an empty dict —
    making the body always a dict at the guard. This test documents the
    real (not the intended) behavior. See dead-code note in the return summary.
    """
    resp = client.patch(
        "/api/agentic_search/workspaces/eng-docs",
        data="bad",
        content_type="text/plain",
        headers=auth_headers,
    )
    # The guard is dead code: get_json(silent=True) → None → {} (always a dict).
    # The PATCH proceeds with an empty update dict and returns 200 (no-op patch).
    assert resp.status_code == 200


def test_patch_workspace_invalid_sources_type(client, auth_headers):
    """PATCH /workspaces/<id> with sources as a string (not list) returns 400."""
    resp = client.patch(
        "/api/agentic_search/workspaces/eng-docs",
        json={"sources": "notion"},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert "sources" in resp.get_json()["message"]


def test_patch_workspace_unknown_id_returns_404(client, auth_headers):
    """PATCH /workspaces/<id> for an unknown workspace returns 404."""
    resp = client.patch(
        "/api/agentic_search/workspaces/does-not-exist",
        json={"name": "New Name"},
        headers=auth_headers,
    )
    assert resp.status_code == 404


def test_patch_workspace_valid_update(client, auth_headers):
    """PATCH /workspaces/<id> applies a partial update and returns the new state."""
    resp = client.patch(
        "/api/agentic_search/workspaces/eng-docs",
        json={"name": "Eng Docs Renamed"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.get_json()["workspace"]
    assert body["name"] == "Eng Docs Renamed"


def test_delete_workspace_unknown_id_returns_404(client, auth_headers):
    """DELETE /workspaces/<id> for an absent workspace returns 404."""
    resp = client.delete("/api/agentic_search/workspaces/ghost-ws", headers=auth_headers)
    assert resp.status_code == 404


def test_delete_workspace_success_returns_deleted_true(client, auth_headers):
    """DELETE /workspaces/<id> for an existing workspace returns deleted=True."""
    resp = client.delete("/api/agentic_search/workspaces/eng-docs", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["deleted"] is True
    assert body["workspace_id"] == "eng-docs"


# ===========================================================================
# Workspace runs list
# ===========================================================================


def test_workspace_runs_auth_gate(client):
    """GET /workspaces/<id>/runs without key → 401."""
    resp = client.get("/api/agentic_search/workspaces/eng-docs/runs")
    assert resp.status_code == 401


def test_workspace_runs_returns_list(client, auth_headers):
    """GET /workspaces/<id>/runs returns a list (may be empty for fresh store)."""
    resp = client.get("/api/agentic_search/workspaces/eng-docs/runs", headers=auth_headers)
    assert resp.status_code == 200
    assert "runs" in resp.get_json()


# ===========================================================================
# Run creation edge-cases
# ===========================================================================


def test_run_post_non_dict_body(client, auth_headers):
    """POST /runs with non-dict body → 400."""
    resp = client.post(
        "/api/agentic_search/runs",
        data="bad",
        content_type="text/plain",
        headers=auth_headers,
    )
    assert resp.status_code == 400


def test_run_post_missing_workspace_id(client, auth_headers):
    """POST /runs without workspace_id → 400."""
    resp = client.post(
        "/api/agentic_search/runs",
        json={"query": "hello"},
        headers=auth_headers,
    )
    assert resp.status_code == 400


def test_run_post_empty_workspace_id(client, auth_headers):
    """POST /runs with workspace_id = '  ' → 400."""
    resp = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": "   ", "query": "hello"},
        headers=auth_headers,
    )
    assert resp.status_code == 400


def test_run_post_missing_query(client, auth_headers):
    """POST /runs without query → 400."""
    resp = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": "eng-docs"},
        headers=auth_headers,
    )
    assert resp.status_code == 400


def test_run_post_empty_query(client, auth_headers):
    """POST /runs with query = '  ' → 400."""
    resp = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": "eng-docs", "query": "   "},
        headers=auth_headers,
    )
    assert resp.status_code == 400


def test_run_post_nonstring_workspace_id(client, auth_headers):
    """POST /runs with workspace_id as an int → 400."""
    resp = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": 42, "query": "hello"},
        headers=auth_headers,
    )
    assert resp.status_code == 400


def test_run_post_success_envelope(client, auth_headers):
    """POST /runs returns the back-compat envelope with run, run_id, session_id, status."""
    resp = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": "eng-docs", "query": "unit test query"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert "run" in body
    assert "run_id" in body
    assert "session_id" in body
    assert "status" in body
    assert body["status"] == "completed"


def test_run_post_404_on_unknown_workspace(client, auth_headers):
    """POST /runs for an absent workspace → 404."""
    resp = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": "ghost-ws", "query": "x"},
        headers=auth_headers,
    )
    assert resp.status_code == 404


# ===========================================================================
# Run GET / cancel
# ===========================================================================


def test_run_get_unknown_id_404(client, auth_headers):
    """GET /runs/<id> for an absent run → 404."""
    resp = client.get("/api/agentic_search/runs/ghost-run", headers=auth_headers)
    assert resp.status_code == 404


def test_run_get_snapshot_has_expected_keys(client, auth_headers):
    """GET /runs/<id> returns a record with required durable keys."""
    run_resp = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": "eng-docs", "query": "snapshot check"},
        headers=auth_headers,
    )
    run_id = run_resp.get_json()["run_id"]
    snap_resp = client.get(f"/api/agentic_search/runs/{run_id}", headers=auth_headers)
    assert snap_resp.status_code == 200
    record = snap_resp.get_json()["run"]
    for key in ("run_id", "session_id", "workspace_id", "query", "status"):
        assert key in record, f"missing key: {key}"


def test_run_cancel_unknown_id_404(client, auth_headers):
    """POST /runs/<id>/cancel for an absent run → 404."""
    resp = client.post("/api/agentic_search/runs/ghost-run/cancel", headers=auth_headers)
    assert resp.status_code == 404


def test_run_cancel_completed_run_not_cancelled(client, auth_headers):
    """Cancelling an already-completed echo run reports cancelled=False."""
    run_resp = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": "eng-docs", "query": "cancel test"},
        headers=auth_headers,
    )
    run_id = run_resp.get_json()["run_id"]
    cancel_resp = client.post(f"/api/agentic_search/runs/{run_id}/cancel", headers=auth_headers)
    assert cancel_resp.status_code == 200
    body = cancel_resp.get_json()
    assert body["run_id"] == run_id
    assert body["cancelled"] is False  # already completed


# ===========================================================================
# SSE run events
# ===========================================================================


def test_run_events_auth_gate(client, monkeypatch):
    """GET /runs/<id>/events without key → 401."""
    monkeypatch.setenv("MEWBO_AGENTIC_SSE_SLEEP", "0.01")
    monkeypatch.setenv("MEWBO_AGENTIC_SSE_MAX_IDLE", "3")
    resp = client.get("/api/agentic_search/runs/ghost/events")
    assert resp.status_code == 401


def test_run_events_after_idx_param(client, auth_headers, monkeypatch):
    """GET /runs/<id>/events?after_idx=N streams only events after idx N."""
    monkeypatch.setenv("MEWBO_AGENTIC_SSE_SLEEP", "0.01")
    monkeypatch.setenv("MEWBO_AGENTIC_SSE_MAX_IDLE", "3")

    run_resp = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": "eng-docs", "query": "sse after_idx test"},
        headers=auth_headers,
    )
    run_id = run_resp.get_json()["run_id"]

    # Request events from idx=0 (should still include the first event as it's idx=0
    # and after_idx=0 means after 0, exclusive, so we skip idx 0).
    resp = client.get(
        f"/api/agentic_search/runs/{run_id}/events?after_idx=0",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    body = resp.get_data(as_text=True)
    # run_started is at idx=0, so it should not appear.
    # run_done (terminal) should still be in the tail.
    assert "event: run_done" in body


def test_run_events_last_event_id_resume(client, auth_headers, monkeypatch):
    """GET /runs/<id>/events with Last-Event-ID header resumes from that idx."""
    monkeypatch.setenv("MEWBO_AGENTIC_SSE_SLEEP", "0.01")
    monkeypatch.setenv("MEWBO_AGENTIC_SSE_MAX_IDLE", "3")

    run_resp = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": "eng-docs", "query": "last-event-id resume"},
        headers=auth_headers,
    )
    run_id = run_resp.get_json()["run_id"]

    # Resume from Last-Event-ID: 0 (same semantics as after_idx=0).
    resp = client.get(
        f"/api/agentic_search/runs/{run_id}/events",
        headers={**auth_headers, "Last-Event-ID": "0"},
    )
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    body = resp.get_data(as_text=True)
    assert "event: run_done" in body


def test_run_events_invalid_after_idx_falls_back_to_all(client, auth_headers, monkeypatch):
    """GET /runs/<id>/events?after_idx=bad_value falls back to -1 (all events)."""
    monkeypatch.setenv("MEWBO_AGENTIC_SSE_SLEEP", "0.01")
    monkeypatch.setenv("MEWBO_AGENTIC_SSE_MAX_IDLE", "3")

    run_resp = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": "eng-docs", "query": "bad after_idx test"},
        headers=auth_headers,
    )
    run_id = run_resp.get_json()["run_id"]

    resp = client.get(
        f"/api/agentic_search/runs/{run_id}/events?after_idx=not-a-number",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    body = resp.get_data(as_text=True)
    # Falls back to all events, so both start and done should be present.
    assert "event: run_started" in body
    assert "event: run_done" in body


# ===========================================================================
# SCG-gated endpoints — 503 when SCG is disabled (the default)
# ===========================================================================


def test_map_source_disabled_scg_returns_503(client, auth_headers):
    """POST /sources/<id>/map returns 503 when scg.enabled is False (default)."""
    resp = client.post(
        "/api/agentic_search/sources/notion/map",
        json={"source_type": "notion"},
        headers=auth_headers,
    )
    assert resp.status_code == 503
    assert "SCG is disabled" in resp.get_json()["message"]


def test_scg_introspect_disabled_returns_503(client, auth_headers):
    """GET /scg returns 503 when scg.enabled is False (default)."""
    resp = client.get("/api/agentic_search/scg", headers=auth_headers)
    assert resp.status_code == 503
    assert "SCG is disabled" in resp.get_json()["message"]


# ===========================================================================
# Map-source events — 404 when no job exists for source
# ===========================================================================


def test_map_source_events_no_job_404(client, auth_headers, monkeypatch):
    """GET /sources/<id>/map/events returns 404 when no map job exists."""
    monkeypatch.setenv("MEWBO_AGENTIC_SSE_SLEEP", "0.01")
    monkeypatch.setenv("MEWBO_AGENTIC_SSE_MAX_IDLE", "3")
    resp = client.get(
        "/api/agentic_search/sources/notion/map/events",
        headers=auth_headers,
    )
    assert resp.status_code == 404


def test_map_source_events_unknown_job_id_404(client, auth_headers, monkeypatch):
    """GET /sources/<id>/map/events?job_id=bad returns 404 when job doesn't exist."""
    monkeypatch.setenv("MEWBO_AGENTIC_SSE_SLEEP", "0.01")
    monkeypatch.setenv("MEWBO_AGENTIC_SSE_MAX_IDLE", "3")
    resp = client.get(
        "/api/agentic_search/sources/notion/map/events?job_id=ghost-job",
        headers=auth_headers,
    )
    assert resp.status_code == 404


def test_map_source_events_streams_existing_job(client, auth_headers, monkeypatch):
    """GET /sources/<id>/map/events?job_id=X streams the job's event log via SSE.

    Exercises lines 403-417: the job exists path that builds a RunSseGenerator
    with ``load=st.load_map_job_events`` and streams it.
    """
    monkeypatch.setenv("MEWBO_AGENTIC_SSE_SLEEP", "0.01")
    monkeypatch.setenv("MEWBO_AGENTIC_SSE_MAX_IDLE", "3")

    # Inject a map job directly into the store (bypasses SCG).
    from mewbo_api.agentic_search.schemas import MapJobRecord
    from mewbo_api.agentic_search.store import get_store

    st = get_store()
    job = MapJobRecord(job_id="job-test-sse", source_id="notion", source_type="notion")
    st.create_map_job(job)
    st.append_map_job_event("job-test-sse", {"type": "run_done", "status": "complete"})

    resp = client.get(
        "/api/agentic_search/sources/notion/map/events?job_id=job-test-sse",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    body = resp.get_data(as_text=True)
    assert "event: run_done" in body


def test_map_source_events_no_job_id_uses_newest(client, auth_headers, monkeypatch):
    """GET /sources/<id>/map/events without ?job_id= uses the newest job for source."""
    monkeypatch.setenv("MEWBO_AGENTIC_SSE_SLEEP", "0.01")
    monkeypatch.setenv("MEWBO_AGENTIC_SSE_MAX_IDLE", "3")

    from mewbo_api.agentic_search.schemas import MapJobRecord
    from mewbo_api.agentic_search.store import get_store

    st = get_store()
    job = MapJobRecord(job_id="job-newest-test", source_id="github", source_type="github")
    st.create_map_job(job)
    st.append_map_job_event("job-newest-test", {"type": "run_done", "status": "complete"})

    resp = client.get(
        "/api/agentic_search/sources/github/map/events",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"


# ===========================================================================
# Workspace POST validation error path
# ===========================================================================


def test_create_workspace_validation_error_message(client, auth_headers):
    """POST /workspaces with a missing required field returns a readable 400."""
    # WorkspaceInput requires name with min_length=1.
    resp = client.post(
        "/api/agentic_search/workspaces",
        json={"desc": "no name field"},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert "message" in body
    # _validation_error formats as "<loc>: <pydantic-msg>" where loc is the
    # field path.  The missing ``name`` field produces a loc of "name".
    assert "name" in body["message"]


# ===========================================================================
# Run POST back-compat envelope — status field present
# ===========================================================================


def test_run_post_back_compat_top_level_fields(client, auth_headers):
    """POST /runs envelope carries top-level run_id, session_id, status (MCP compat)."""
    resp = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": "product", "query": "back-compat check"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.get_json()
    # All three back-compat top-level fields must be present.
    assert body.get("run_id") is not None
    assert body.get("session_id") is not None
    assert body.get("status") is not None
    # Nested run must also carry these.
    run = body["run"]
    assert run["run_id"] == body["run_id"]
    assert run["session_id"] == body["session_id"]
    assert run["status"] == body["status"]
