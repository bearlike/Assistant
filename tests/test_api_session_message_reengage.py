#!/usr/bin/env python3
"""Route tests for POST /api/sessions/<id>/message idle re-engagement.

Pins the context-inheritance contract (Gitea #177): a re-engaged run must
inherit the session's persisted context (model / mode / mcp_tools allowlist)
exactly like /query and /recover — never clobber it with the config default.

Drives the real Flask backend test client against a temp-store runtime; only
auth, storage root, and the run dispatch seam (start_async) are swapped.
"""

from __future__ import annotations

import pytest

API_KEY = "test-master-token-177"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Backend test client bound to a temp SessionStore runtime + known token."""
    from mewbo_core.session_event_bus import reset_session_event_bus_for_tests
    from mewbo_core.session_runtime import SessionRuntime
    from mewbo_core.session_store import SessionStore

    reset_session_event_bus_for_tests()

    import mewbo_api.backend as backend

    monkeypatch.setattr(backend, "MASTER_API_TOKEN", API_KEY, raising=False)
    store = SessionStore(root_dir=str(tmp_path / "sessions"))
    rt = SessionRuntime(session_store=store)
    monkeypatch.setattr(backend, "runtime", rt, raising=False)
    backend.app.config["TESTING"] = True
    return backend.app.test_client(), rt


def _headers() -> dict:
    return {"X-Api-Key": API_KEY}


def test_message_reengage_inherits_persisted_context(client, monkeypatch):
    """Idle /message starts a run with the session's model/mode/mcp_tools."""
    c, rt = client
    captured: dict = {}

    def fake_start_async(**kwargs):
        captured.update(kwargs)
        return f"{kwargs['session_id']}:r1"

    monkeypatch.setattr(rt, "start_async", fake_start_async)

    resp = c.post(
        "/api/sessions",
        json={
            "context": {
                "model": "claude-sonnet-5",
                "mode": "plan",
                "mcp_tools": ["searxng_web_search"],
            }
        },
        headers=_headers(),
    )
    assert resp.status_code == 200
    sid = resp.get_json()["session_id"]

    resp = c.post(
        f"/api/sessions/{sid}/message",
        json={"text": "hello again"},
        headers=_headers(),
    )
    assert resp.status_code == 200
    assert resp.get_json()["run_id"] == f"{sid}:r1"
    assert captured["model_name"] == "claude-sonnet-5"
    assert captured["mode"] == "plan"
    assert captured["allowed_tools"] == ["searxng_web_search"]
    # The re-engage path must not append a context event that would shadow
    # the persisted model (the old behavior wrote the config default here).
    models = [
        e["payload"].get("model")
        for e in rt.load_events(sid)
        if e.get("type") == "context"
    ]
    assert models == ["claude-sonnet-5"]


def test_message_reengage_matches_created_context(client, monkeypatch):
    """Bare create → re-engage runs on whatever model create persisted.

    The create route fills context.model itself; the contract here is that
    /message reads THAT persisted value back instead of doing a fresh config
    lookup at re-engage time.
    """
    c, rt = client
    captured: dict = {}

    def fake_start_async(**kwargs):
        captured.update(kwargs)
        return f"{kwargs['session_id']}:r1"

    monkeypatch.setattr(rt, "start_async", fake_start_async)

    resp = c.post("/api/sessions", json={}, headers=_headers())
    sid = resp.get_json()["session_id"]
    persisted = [
        e["payload"].get("model")
        for e in rt.load_events(sid)
        if e.get("type") == "context" and isinstance(e.get("payload"), dict)
    ]

    resp = c.post(
        f"/api/sessions/{sid}/message",
        json={"text": "hi"},
        headers=_headers(),
    )
    assert resp.status_code == 200
    assert captured["model_name"] == (persisted[-1] if persisted else None)
    assert captured["allowed_tools"] is None
