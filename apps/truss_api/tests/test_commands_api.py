"""Integration tests for the slash-command API endpoints."""

# mypy: ignore-errors

from __future__ import annotations

import json
from dataclasses import dataclass

from truss_api import backend
from truss_core.session_store import SessionStore


def _reset_backend(tmp_path):
    """Mirror the helper in test_backend; resets the backend's stores."""
    backend.session_store = SessionStore(root_dir=str(tmp_path))
    backend.runtime = backend.SessionRuntime(session_store=backend.session_store)
    backend.notification_store = backend.NotificationStore(root_dir=str(tmp_path))
    backend.share_store = backend.ShareStore(root_dir=str(tmp_path))
    backend.notification_service = backend.NotificationService(
        backend.notification_store,
        backend.runtime.session_store,
    )


HEADERS = lambda: {  # noqa: E731
    "X-API-KEY": backend.MASTER_API_TOKEN,
    "Content-Type": "application/json",
}


def _create_session(client) -> str:
    response = client.post("/api/sessions", json={}, headers=HEADERS())
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["session_id"]


def test_get_commands_lists_registry(tmp_path):
    """GET /api/commands returns all built-in commands."""
    _reset_backend(tmp_path)
    client = backend.app.test_client()
    response = client.get("/api/commands", headers=HEADERS())
    assert response.status_code == 200, response.get_data(as_text=True)
    payload = response.get_json()
    names = {cmd["name"] for cmd in payload["commands"]}
    assert names >= {"compact", "skills", "tokens", "fork", "tag", "help"}


def test_get_commands_requires_auth(tmp_path):
    """GET /api/commands without a token returns 401."""
    _reset_backend(tmp_path)
    client = backend.app.test_client()
    response = client.get("/api/commands")
    assert response.status_code == 401


def test_post_command_help_returns_dialog_render(tmp_path):
    """POST /command with name=help returns a dialog render payload."""
    _reset_backend(tmp_path)
    client = backend.app.test_client()
    sid = _create_session(client)
    response = client.post(
        f"/api/sessions/{sid}/command",
        data=json.dumps({"name": "help", "args": []}),
        headers=HEADERS(),
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert body["render"] == "dialog"
    assert "/help" in body["body"]


def test_post_command_unknown_returns_404(tmp_path):
    """POST /command with an unknown command name returns 404."""
    _reset_backend(tmp_path)
    client = backend.app.test_client()
    sid = _create_session(client)
    response = client.post(
        f"/api/sessions/{sid}/command",
        data=json.dumps({"name": "definitely-not-a-command", "args": []}),
        headers=HEADERS(),
    )
    assert response.status_code == 404


def test_post_command_bad_args_returns_400(tmp_path):
    """POST /command with missing required args returns 400 bad_args."""
    _reset_backend(tmp_path)
    client = backend.app.test_client()
    sid = _create_session(client)
    response = client.post(
        f"/api/sessions/{sid}/command",
        data=json.dumps({"name": "tag", "args": []}),
        headers=HEADERS(),
    )
    assert response.status_code == 400
    body = response.get_json()
    assert body["error"] == "bad_args"


def test_post_command_tag_emits_notification(tmp_path):
    """POST /command tag stores session tag and emits a notification."""
    _reset_backend(tmp_path)
    client = backend.app.test_client()
    sid = _create_session(client)
    client.post(
        f"/api/sessions/{sid}/command",
        data=json.dumps({"name": "tag", "args": ["smoke-test"]}),
        headers=HEADERS(),
    )
    notif_resp = client.get("/api/notifications", headers=HEADERS())
    titles = [n["title"] for n in notif_resp.get_json()["notifications"]]
    assert "Session tagged" in titles


def _wait_until_idle(client, sid, timeout_s: float = 5.0) -> list[dict]:
    """Poll /events until running flips false; return the final event list."""
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = client.get(f"/api/sessions/{sid}/events", headers=HEADERS())
        assert resp.status_code == 200
        body = resp.get_json()
        if not body.get("running"):
            return body["events"]
        time.sleep(0.05)
    raise AssertionError(f"session {sid} still running after {timeout_s}s")


@dataclass
class _FakeCompactResult:
    summary: str = "transcript-summary"
    tokens_saved: int = 0
    kept_events: list | None = None
    model: str = "fake"
    events_summarized: int = 0

    def __post_init__(self):
        if self.kept_events is None:
            self.kept_events = []


def _patch_compact(monkeypatch):
    async def fake_compact(self, session_id, mode=None, **kwargs):
        self.save_summary(session_id, "transcript-summary")
        return _FakeCompactResult()

    from truss_core.session_store import SessionStoreBase

    monkeypatch.setattr(SessionStoreBase, "compact_session", fake_compact, raising=True)


def test_post_command_compact_returns_202_and_writes_events_in_order(tmp_path, monkeypatch):
    """Compact via /command runs async.

    User event first, completion last — same lifecycle as a regular query
    so the FE polling pipeline drives the UI organically.
    """
    _reset_backend(tmp_path)
    _patch_compact(monkeypatch)

    client = backend.app.test_client()
    sid = _create_session(client)
    response = client.post(
        f"/api/sessions/{sid}/command",
        data=json.dumps({"name": "compact", "args": []}),
        headers=HEADERS(),
    )
    # Async dispatch: 202 Accepted, body advertises transcript render so the
    # FE knows to wake events polling instead of opening a dialog.
    assert response.status_code == 202, response.get_data(as_text=True)
    body = response.get_json()
    assert body["accepted"] is True
    assert body["render"] == "transcript"

    events = _wait_until_idle(client, sid)
    types = [e["type"] for e in events]
    assert "user" in types
    assert "context_compacted" in types
    assert "completion" in types
    # Order matters: bubble appears, work runs, completion closes the turn.
    user_idx = types.index("user")
    compact_idx = types.index("context_compacted")
    completion_idx = types.index("completion")
    assert user_idx < compact_idx < completion_idx, types

    # User event carries the invocation text the FE renders as a chat bubble.
    assert events[user_idx]["payload"]["text"] == "/compact"
    completion_payload = events[completion_idx]["payload"]
    # done=True + done_reason="compacted" mirrors the orchestrator's path so
    # the notification service routes this to a success toast and
    # summarize_session resolves status="completed".
    assert completion_payload["done"] is True
    assert completion_payload["done_reason"] == "compacted"
    assert completion_payload["command"] == "compact"


def test_post_command_compact_marks_session_running(tmp_path, monkeypatch):
    """Server-side running flag stays true for the duration of compaction.

    While the compaction thread is alive ``is_running()`` returns true so the
    FE's events poll keeps streaming and the run indicator stays mounted
    across page refreshes.
    """
    import threading

    _reset_backend(tmp_path)

    gate = threading.Event()
    release = threading.Event()

    @dataclass
    class _Result:
        summary: str = "x"
        tokens_saved: int = 0
        kept_events: list | None = None
        model: str = "fake"
        events_summarized: int = 0

        def __post_init__(self):
            if self.kept_events is None:
                self.kept_events = []

    async def slow_compact(self, session_id, mode=None, **kwargs):
        self.save_summary(session_id, "x")
        gate.set()
        # Hold until the test confirms running==true to avoid a race.
        release.wait(timeout=5.0)
        return _Result()

    from truss_core.session_store import SessionStoreBase

    monkeypatch.setattr(SessionStoreBase, "compact_session", slow_compact, raising=True)

    client = backend.app.test_client()
    sid = _create_session(client)
    response = client.post(
        f"/api/sessions/{sid}/command",
        data=json.dumps({"name": "compact", "args": []}),
        headers=HEADERS(),
    )
    assert response.status_code == 202

    # Worker has entered the handler — running must be true here.
    assert gate.wait(timeout=5.0), "worker thread never started"
    mid_resp = client.get(f"/api/sessions/{sid}/events", headers=HEADERS())
    assert mid_resp.get_json()["running"] is True

    # Bubble must already be visible while the work runs.
    mid_types = [e["type"] for e in mid_resp.get_json()["events"]]
    assert "user" in mid_types
    assert "completion" not in mid_types

    release.set()
    final_events = _wait_until_idle(client, sid)
    assert any(e["type"] == "completion" for e in final_events)


def test_post_command_compact_rejects_when_already_running(tmp_path, monkeypatch):
    """Concurrent transcript commands return 409.

    Same behavior as the /query endpoint when a run is already active.
    """
    import threading

    _reset_backend(tmp_path)
    release = threading.Event()

    @dataclass
    class _Result:
        summary: str = "x"
        tokens_saved: int = 0
        kept_events: list | None = None
        model: str = "fake"
        events_summarized: int = 0

        def __post_init__(self):
            if self.kept_events is None:
                self.kept_events = []

    async def hold_compact(self, session_id, mode=None, **kwargs):
        self.save_summary(session_id, "x")
        release.wait(timeout=5.0)
        return _Result()

    from truss_core.session_store import SessionStoreBase

    monkeypatch.setattr(SessionStoreBase, "compact_session", hold_compact, raising=True)

    client = backend.app.test_client()
    sid = _create_session(client)
    first = client.post(
        f"/api/sessions/{sid}/command",
        data=json.dumps({"name": "compact", "args": []}),
        headers=HEADERS(),
    )
    assert first.status_code == 202

    second = client.post(
        f"/api/sessions/{sid}/command",
        data=json.dumps({"name": "compact", "args": []}),
        headers=HEADERS(),
    )
    assert second.status_code == 409

    release.set()
    _wait_until_idle(client, sid)


def test_post_command_compact_emits_success_notification(tmp_path, monkeypatch):
    """A successful /compact yields a 'completed' toast, not 'failed'."""
    _reset_backend(tmp_path)
    _patch_compact(monkeypatch)

    client = backend.app.test_client()
    sid = _create_session(client)
    response = client.post(
        f"/api/sessions/{sid}/command",
        data=json.dumps({"name": "compact", "args": []}),
        headers=HEADERS(),
    )
    assert response.status_code == 202
    _wait_until_idle(client, sid)

    # /events triggers emit_completion server-side after each poll.
    backend.notification_service.emit_completion(sid)
    notes = backend.notification_store.list(include_dismissed=True)
    completion_notes = [n for n in notes if n.get("event_type") in {"completed", "failed"}]
    assert len(completion_notes) == 1, completion_notes
    note = completion_notes[0]
    assert note["event_type"] == "completed"
    assert note["metadata"]["done_reason"] == "compacted"
    assert "Compaction" in note["message"]


def test_post_command_compact_marks_session_completed(tmp_path, monkeypatch):
    """summarize_session reflects the canonical 'completed' status post-compact."""
    _reset_backend(tmp_path)
    _patch_compact(monkeypatch)

    client = backend.app.test_client()
    sid = _create_session(client)
    response = client.post(
        f"/api/sessions/{sid}/command",
        data=json.dumps({"name": "compact", "args": []}),
        headers=HEADERS(),
    )
    assert response.status_code == 202
    _wait_until_idle(client, sid)

    summary = backend.runtime.summarize_session(sid)
    assert summary["status"] == "completed"
    assert summary["done_reason"] == "compacted"


def test_post_command_compact_failure_emits_failed_notification(tmp_path, monkeypatch):
    """If the handler raises, the notification + status both flip to failed."""
    _reset_backend(tmp_path)

    async def boom_compact(self, session_id, mode=None, **kwargs):
        raise RuntimeError("boom")

    from truss_core.session_store import SessionStoreBase

    monkeypatch.setattr(SessionStoreBase, "compact_session", boom_compact, raising=True)

    client = backend.app.test_client()
    sid = _create_session(client)
    response = client.post(
        f"/api/sessions/{sid}/command",
        data=json.dumps({"name": "compact", "args": []}),
        headers=HEADERS(),
    )
    assert response.status_code == 202
    _wait_until_idle(client, sid)

    backend.notification_service.emit_completion(sid)
    notes = backend.notification_store.list(include_dismissed=True)
    completion_notes = [n for n in notes if n.get("event_type") in {"completed", "failed"}]
    assert len(completion_notes) == 1
    assert completion_notes[0]["event_type"] == "failed"
    assert completion_notes[0]["metadata"]["done_reason"] == "compact_failed"

    summary = backend.runtime.summarize_session(sid)
    assert summary["status"] == "failed"


def test_emit_completion_treats_orchestrator_compacted_as_success(tmp_path, monkeypatch):
    """Pre-existing orchestrator path also writes done_reason='compacted'.

    Same notification routing applies — must surface as success.
    """
    _reset_backend(tmp_path)
    sid = backend.session_store.create_session()
    backend.session_store.append_event(
        sid,
        {
            "type": "completion",
            "payload": {
                "done": True,
                "done_reason": "compacted",
                "task_result": "ok",
            },
        },
    )
    backend.notification_service.emit_completion(sid)
    notes = backend.notification_store.list(include_dismissed=True)
    completion_notes = [n for n in notes if n.get("event_type") in {"completed", "failed"}]
    assert len(completion_notes) == 1
    assert completion_notes[0]["event_type"] == "completed"


def test_emit_completion_skips_transient_done_reasons(tmp_path, monkeypatch):
    """Canceled / awaiting_approval don't generate user-visible toasts."""
    _reset_backend(tmp_path)
    sid = backend.session_store.create_session()
    backend.session_store.append_event(
        sid,
        {
            "type": "completion",
            "payload": {
                "done": True,
                "done_reason": "awaiting_approval",
            },
        },
    )
    backend.notification_service.emit_completion(sid)
    notes = backend.notification_store.list(include_dismissed=True)
    assert all(n.get("event_type") not in {"completed", "failed"} for n in notes)
