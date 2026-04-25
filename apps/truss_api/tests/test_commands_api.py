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


def test_post_command_compact_emits_transcript_events(tmp_path, monkeypatch):
    """Compact through the API endpoint should append user + completion events."""
    _reset_backend(tmp_path)

    @dataclass
    class FakeResult:
        summary: str = "transcript-summary"
        tokens_saved: int = 0
        kept_events: list = None
        model: str = "fake"
        events_summarized: int = 0

        def __post_init__(self):
            if self.kept_events is None:
                self.kept_events = []

    async def fake_compact(self, session_id, mode=None, **kwargs):
        self.save_summary(session_id, "transcript-summary")
        return FakeResult()

    from truss_core.session_store import SessionStoreBase

    monkeypatch.setattr(SessionStoreBase, "compact_session", fake_compact, raising=True)

    client = backend.app.test_client()
    sid = _create_session(client)
    response = client.post(
        f"/api/sessions/{sid}/command",
        data=json.dumps({"name": "compact", "args": []}),
        headers=HEADERS(),
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["render"] == "transcript"

    events_response = client.get(
        f"/api/sessions/{sid}/events", headers=HEADERS()
    )
    assert events_response.status_code == 200
    events = events_response.get_json()["events"]
    types = [e["type"] for e in events]
    assert "user" in types
    assert "completion" in types
