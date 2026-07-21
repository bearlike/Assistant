#!/usr/bin/env python3
"""Route tests for GET /api/sessions/<id>/timeline — the assembled transcript.

Drives the real Flask backend test client against a temp-store runtime so the
route reads events it genuinely appended (no over-mocking); only auth + storage
root are swapped. The runtime is monkeypatched (auto-restored) so the rebind
never leaks into the shared suite (see tests/CLAUDE.md).

The assembly rules themselves are covered by ``tests/test_transcript_timeline``;
what is asserted here is the WIRE contract — auth, the unknown-session 404, the
readability of a terminated session, and the fields the response does and does
not carry.
"""

from __future__ import annotations

import pytest

API_KEY = "test-master-token-timeline"


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


def _seed(rt, session_id: str, events: list[dict]) -> None:
    rt.ensure_session(session_id)
    for event in events:
        rt.append_event(session_id, event)


def test_timeline_requires_auth(client):
    c, rt = client
    _seed(rt, "s1", [{"type": "user", "payload": {"text": "hi"}}])
    assert c.get("/api/sessions/s1/timeline").status_code == 401


def test_unknown_session_is_404_not_an_empty_conversation(client):
    """An empty-but-successful body would read as "this session said nothing"."""
    c, _rt = client
    resp = c.get("/api/sessions/ghost/timeline", headers=_headers())
    assert resp.status_code == 404
    assert resp.get_json()["error"]["code"] == 404


def test_timeline_returns_assembled_rows(client):
    c, rt = client
    _seed(
        rt,
        "s1",
        [
            {"type": "user", "ts": "t0", "payload": {"text": "hi"}},
            {"type": "tool_result", "ts": "t1", "payload": {"tool_id": "shell"}},
            {"type": "assistant", "ts": "t2", "payload": {"text": "hello"}},
        ],
    )
    resp = c.get("/api/sessions/s1/timeline", headers=_headers())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["session_id"] == "s1"
    assert [e["role"] for e in body["entries"]] == ["user", "assistant"]
    assert body["entries"][1]["content"] == "hello"
    assert body["entries"][1]["turn"]["id"] == "turn-1"
    assert body["open_turn"] is None


def test_turn_bodies_are_not_inlined(client):
    """Inlining a turn's events repeats the whole event log once per turn."""
    c, rt = client
    _seed(
        rt,
        "s1",
        [
            {"type": "user", "ts": "t0", "payload": {"text": "hi"}},
            {"type": "assistant", "ts": "t2", "payload": {"text": "hello"}},
        ],
    )
    body = c.get("/api/sessions/s1/timeline", headers=_headers()).get_json()
    assert "events" not in body["entries"][1]["turn"]


def test_open_turn_is_reported_separately_from_the_rows(client):
    """A running turn has no closing row, so it cannot ride `entries`."""
    c, rt = client
    _seed(
        rt,
        "s1",
        [
            {"type": "user", "ts": "t0", "payload": {"text": "still working"}},
            {"type": "tool_result", "ts": "t1", "payload": {"tool_id": "shell"}},
        ],
    )
    body = c.get("/api/sessions/s1/timeline", headers=_headers()).get_json()
    assert [e["role"] for e in body["entries"]] == ["user"]
    assert body["open_turn"] is not None
    assert body["open_turn"]["id"] == "turn-1"
    assert "events" not in body["open_turn"]


def test_terminated_session_is_still_readable(client):
    """Termination ends a session's run; it does not withdraw its transcript."""
    c, rt = client
    _seed(
        rt,
        "s1",
        [
            {"type": "user", "ts": "t0", "payload": {"text": "hi"}},
            {"type": "assistant", "ts": "t1", "payload": {"text": "hello"}},
        ],
    )
    # The real path: it stamps the store AND appends the transcript marker.
    # Appending the marker alone would leave the stored flag unset and prove
    # nothing about how a genuinely terminated session reads.
    rt.terminate_session("s1")
    resp = c.get("/api/sessions/s1/timeline", headers=_headers())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["terminated"] is True
    assert "session_terminated" in [e["role"] for e in body["entries"]]


def test_question_rows_never_carry_the_answer_credential(client):
    """The call_token is a bearer secret; answering goes through its own route."""
    c, rt = client
    _seed(
        rt,
        "s1",
        [
            {"type": "user", "ts": "t0", "payload": {"text": "go"}},
            {
                "type": "user_question",
                "ts": "t1",
                "payload": {
                    "call_id": "c1",
                    "call_token": "secret-token",
                    "questions": [{"header": "Scope", "question": "How far?"}],
                },
            },
        ],
    )
    raw = c.get("/api/sessions/s1/timeline", headers=_headers()).get_data(as_text=True)
    assert "secret-token" not in raw
    assert "call_token" not in raw


def test_failure_rows_carry_the_classified_reason(client):
    c, rt = client
    _seed(
        rt,
        "s1",
        [
            {"type": "user", "ts": "t0", "payload": {"text": "go"}},
            {
                "type": "completion",
                "ts": "t1",
                "payload": {"done_reason": "error", "error": "upstream died"},
            },
        ],
    )
    body = c.get("/api/sessions/s1/timeline", headers=_headers()).get_json()
    failed = [e for e in body["entries"] if e["role"] == "run_failed"]
    assert len(failed) == 1
    assert failed[0]["run_failure"]["reason"] == "error"
    assert failed[0]["run_failure"]["text"] == "upstream died"


def test_timeline_is_documented_on_the_swagger_surface(client):
    """Two Resources on one path silently drop verbs; assert the GET survived."""
    c, _rt = client
    spec = c.get("/swagger.json", headers=_headers()).get_json()
    assert "get" in spec["paths"]["/api/sessions/{session_id}/timeline"]
