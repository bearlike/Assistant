#!/usr/bin/env python3
"""Route tests for POST /api/sessions/<id>/events — the CLI transcript-mirror
ingest seam.

Drives the real Flask backend test client against a temp-store runtime so the
append actually lands (no over-mocking); only auth + storage root are swapped.
The runtime is monkeypatched (auto-restored) so the rebind never leaks into the
shared suite (see tests/CLAUDE.md).
"""

from __future__ import annotations

import pytest

API_KEY = "test-master-token-171"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Backend test client bound to a temp SessionStore runtime + known token."""
    from mewbo_core.session_event_bus import reset_session_event_bus_for_tests
    from mewbo_core.session_runtime import SessionRuntime
    from mewbo_core.session_store import SessionStore

    reset_session_event_bus_for_tests()  # isolate from leaked observers

    import mewbo_api.backend as backend

    monkeypatch.setattr(backend, "MASTER_API_TOKEN", API_KEY, raising=False)
    store = SessionStore(root_dir=str(tmp_path / "sessions"))
    rt = SessionRuntime(session_store=store)
    monkeypatch.setattr(backend, "runtime", rt, raising=False)
    backend.app.config["TESTING"] = True
    return backend.app.test_client(), rt


def _headers() -> dict:
    return {"X-Api-Key": API_KEY}


def test_ingest_batch_materializes_session_and_appends(client):
    """A batch POST creates the session idempotently and appends every record."""
    c, rt = client
    sid = "cli-session-batch"
    resp = c.post(
        f"/api/sessions/{sid}/events",
        json={
            "records": [
                {"type": "user", "payload": {"text": "hi"}},
                {"type": "completion", "payload": {"done_reason": "completed"}},
            ]
        },
        headers=_headers(),
    )
    assert resp.status_code == 202
    body = resp.get_json()
    assert body == {"session_id": sid, "appended": 2}
    events = rt.load_events(sid)
    assert [e["type"] for e in events] == ["user", "completion"]
    # The mirrored session is a real record (visible to list_sessions).
    assert sid in rt.session_store.list_sessions()


def test_ingest_single_record(client):
    """The single-record shape (`{record}`) appends one event."""
    c, rt = client
    sid = "cli-session-single"
    resp = c.post(
        f"/api/sessions/{sid}/events",
        json={"record": {"type": "tool_result", "payload": {"tool_id": "shell"}}},
        headers=_headers(),
    )
    assert resp.status_code == 202
    assert resp.get_json()["appended"] == 1
    assert [e["type"] for e in rt.load_events(sid)] == ["tool_result"]


def test_ingest_preserves_original_timestamp(client):
    """A record carrying its own ``ts`` keeps it (mirror fidelity, not re-stamped)."""
    c, rt = client
    sid = "cli-session-ts"
    ts = "2026-07-01T10:00:00.000000+00:00"
    c.post(
        f"/api/sessions/{sid}/events",
        json={"record": {"type": "user", "ts": ts, "payload": {}}},
        headers=_headers(),
    )
    events = rt.load_events(sid)
    assert events[0]["ts"] == ts


def test_ingest_requires_auth(client):
    """No credential → 401, nothing stored."""
    c, rt = client
    resp = c.post(
        "/api/sessions/nope/events",
        json={"record": {"type": "user", "payload": {}}},
    )
    assert resp.status_code == 401
    assert rt.load_events("nope") == []


def test_ingest_rejects_empty_body(client):
    """A body without record/records is a 400."""
    c, _ = client
    resp = c.post("/api/sessions/s/events", json={}, headers=_headers())
    assert resp.status_code == 400


def test_ingest_rejects_records_without_type(client):
    """Records missing a ``type`` are rejected (no phantom events)."""
    c, rt = client
    resp = c.post(
        "/api/sessions/s/events",
        json={"records": [{"payload": {"x": 1}}]},
        headers=_headers(),
    )
    assert resp.status_code == 400
    assert rt.load_events("s") == []
