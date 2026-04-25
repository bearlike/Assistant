"""Tests for the Truss API backend."""

# mypy: ignore-errors
import hashlib
import hmac as hmac_mod
import io
import json
import os
import time
from unittest.mock import patch

from truss_api import backend
from truss_core.session_store import SessionStore


class DummyQueue:
    """Minimal task queue stub for API responses."""

    def __init__(self, result: str) -> None:
        """Initialize the dummy queue with a single action result."""
        self.task_result = result
        self.plan_steps = [
            {
                "title": "Say hello",
                "description": "Respond to the user.",
            }
        ]
        self.action_steps = [
            {
                "tool_id": "home_assistant_tool",
                "operation": "get",
                "tool_input": "say",
                "result": result,
            }
        ]

    def dict(self):
        """Return a serialized representation of the queue."""
        return {
            "task_result": self.task_result,
            "plan_steps": list(self.plan_steps),
            "action_steps": list(self.action_steps),
        }


def _make_task_queue(result: str) -> DummyQueue:
    return DummyQueue(result)


def test_query_requires_api_key(monkeypatch):
    """Require authentication headers for query requests."""
    client = backend.app.test_client()
    response = client.post("/api/query", json={"query": "hello"})
    assert response.status_code == 401


def test_query_invalid_input(monkeypatch):
    """Reject empty payloads without a query value."""
    client = backend.app.test_client()
    response = client.post(
        "/api/query",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        data=json.dumps({}),
        content_type="application/json",
    )
    assert response.status_code == 400


def test_query_success(monkeypatch):
    """Return a task result payload when authorized."""
    client = backend.app.test_client()
    captured = {}

    def fake_run_sync(*args, **kwargs):
        captured["mode"] = kwargs.get("mode")
        return _make_task_queue("ok")

    monkeypatch.setattr(backend.runtime, "run_sync", fake_run_sync)
    response = client.post(
        "/api/query",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={"query": "hello"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["task_result"] == "ok"
    assert payload["session_id"]
    assert payload["plan_steps"]
    assert payload["action_steps"]
    assert captured["mode"] is None


def test_query_with_mode(monkeypatch):
    """Pass through orchestration mode when provided."""
    client = backend.app.test_client()
    captured = {}

    def fake_run_sync(*args, **kwargs):
        captured["mode"] = kwargs.get("mode")
        return _make_task_queue("ok")

    monkeypatch.setattr(backend.runtime, "run_sync", fake_run_sync)
    response = client.post(
        "/api/query",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={"query": "hello", "mode": "plan"},
    )
    assert response.status_code == 200
    assert captured["mode"] == "plan"


def test_api_auto_approves_permissions(monkeypatch, tmp_path):
    """API requests should always use auto-approve callback."""
    _reset_backend(tmp_path, monkeypatch)
    client = backend.app.test_client()
    session_id = backend.session_store.create_session()

    captured = {}

    def fake_start_async(*_args, **kwargs):
        captured["approval_callback"] = kwargs.get("approval_callback")
        return True

    monkeypatch.setattr(backend.runtime, "start_async", fake_start_async)
    response = client.post(
        f"/api/sessions/{session_id}/query",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={"query": "hello"},
    )
    assert response.status_code == 202
    assert captured["approval_callback"] is backend.auto_approve

    captured.clear()

    def fake_run_sync(*_args, **kwargs):
        captured["approval_callback"] = kwargs.get("approval_callback")
        return _make_task_queue("ok")

    monkeypatch.setattr(backend.runtime, "run_sync", fake_run_sync)
    response = client.post(
        "/api/query",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={"query": "hello"},
    )
    assert response.status_code == 200
    assert captured["approval_callback"] is backend.auto_approve


def test_query_with_session_tag(monkeypatch, tmp_path):
    """Create or reuse a tagged session and pass it into orchestration."""
    _reset_backend(tmp_path, monkeypatch)
    client = backend.app.test_client()
    captured = {}

    def fake_run_sync(*args, **kwargs):
        captured["session_id"] = kwargs.get("session_id")
        return _make_task_queue("ok")

    monkeypatch.setattr(backend.runtime, "run_sync", fake_run_sync)
    response = client.post(
        "/api/query",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={"query": "hello", "session_tag": "primary"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["session_id"] == captured["session_id"]
    assert backend.session_store.resolve_tag("primary") == captured["session_id"]


def test_query_fork_from(monkeypatch, tmp_path):
    """Fork a session when requested and pass the fork into orchestration."""
    _reset_backend(tmp_path, monkeypatch)
    source_session = backend.session_store.create_session()
    client = backend.app.test_client()
    captured = {}

    def fake_run_sync(*args, **kwargs):
        captured["session_id"] = kwargs.get("session_id")
        return _make_task_queue("ok")

    monkeypatch.setattr(backend.runtime, "run_sync", fake_run_sync)
    response = client.post(
        "/api/query",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={"query": "hello", "fork_from": source_session},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["session_id"] == captured["session_id"]
    assert payload["session_id"] != source_session


def _reset_backend(tmp_path, monkeypatch):
    backend.session_store = SessionStore(root_dir=str(tmp_path))
    backend.runtime = backend.SessionRuntime(session_store=backend.session_store)
    backend.notification_store = backend.NotificationStore(root_dir=str(tmp_path))
    backend.share_store = backend.ShareStore(root_dir=str(tmp_path))
    backend.notification_service = backend.NotificationService(
        backend.notification_store,
        backend.runtime.session_store,
    )


def _fake_run_sync(*, session_id: str, user_query: str, should_cancel=None, **_kwargs):
    backend.session_store.append_event(
        session_id, {"type": "user", "payload": {"text": user_query}}
    )
    if should_cancel and should_cancel():
        backend.session_store.append_event(
            session_id,
            {
                "type": "completion",
                "payload": {"done": True, "done_reason": "canceled", "task_result": None},
            },
        )
        return
    backend.session_store.append_event(session_id, {"type": "assistant", "payload": {"text": "ok"}})
    backend.session_store.append_event(
        session_id,
        {
            "type": "completion",
            "payload": {"done": True, "done_reason": "completed", "task_result": "ok"},
        },
    )


def _wait_for_run(session_id: str, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not backend.runtime.is_running(session_id):
            return
        time.sleep(0.01)
    raise AssertionError("Run did not finish in time.")


def test_sessions_create_list_and_events(monkeypatch, tmp_path):
    """Create a session, run, and assert list/events output."""
    _reset_backend(tmp_path, monkeypatch)
    monkeypatch.setattr(backend.runtime, "run_sync", _fake_run_sync)
    client = backend.app.test_client()

    create = client.post(
        "/api/sessions",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={"context": {"repo": "acme/web", "branch": "main"}},
    )
    assert create.status_code == 200
    session_id = create.get_json()["session_id"]

    enqueue = client.post(
        f"/api/sessions/{session_id}/query",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={"query": "hello"},
    )
    assert enqueue.status_code == 202
    _wait_for_run(session_id)

    events = client.get(
        f"/api/sessions/{session_id}/events",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
    )
    payload = events.get_json()
    assert events.status_code == 200
    assert payload["session_id"] == session_id
    assert payload["events"]
    assert payload["running"] is False

    listing = client.get(
        "/api/sessions",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
    )
    assert listing.status_code == 200
    sessions = listing.get_json()["sessions"]
    assert any(item["session_id"] == session_id for item in sessions)


def test_sessions_list_skips_empty(monkeypatch, tmp_path):
    """Do not list sessions without transcript events."""
    _reset_backend(tmp_path, monkeypatch)
    empty_session = backend.session_store.create_session()
    client = backend.app.test_client()

    listing = client.get(
        "/api/sessions",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
    )
    assert listing.status_code == 200
    sessions = listing.get_json()["sessions"]
    assert all(item["session_id"] != empty_session for item in sessions)


def test_sessions_create_hidden_until_user_event(monkeypatch, tmp_path):
    """Hide sessions that only contain session-created events."""
    _reset_backend(tmp_path, monkeypatch)
    client = backend.app.test_client()
    create = client.post(
        "/api/sessions",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={"context": {"repo": "acme/web"}},
    )
    assert create.status_code == 200
    session_id = create.get_json()["session_id"]
    listing = client.get(
        "/api/sessions",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
    )
    sessions = listing.get_json()["sessions"]
    assert all(item["session_id"] != session_id for item in sessions)

    backend.session_store.append_event(session_id, {"type": "user", "payload": {"text": "hello"}})
    listing = client.get(
        "/api/sessions",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
    )
    sessions = listing.get_json()["sessions"]
    assert any(item["session_id"] == session_id for item in sessions)


def test_sessions_archive_and_list(monkeypatch, tmp_path):
    """Archive sessions and include them when requested."""
    _reset_backend(tmp_path, monkeypatch)
    client = backend.app.test_client()
    session_id = backend.session_store.create_session()
    backend.session_store.append_event(session_id, {"type": "user", "payload": {"text": "hello"}})

    archive = client.post(
        f"/api/sessions/{session_id}/archive",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
    )
    assert archive.status_code == 200
    assert archive.get_json()["archived"] is True

    listing = client.get(
        "/api/sessions",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
    )
    sessions = listing.get_json()["sessions"]
    assert all(item["session_id"] != session_id for item in sessions)

    listing = client.get(
        "/api/sessions?include_archived=1",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
    )
    sessions = listing.get_json()["sessions"]
    assert any(item["session_id"] == session_id for item in sessions)
    archived_entry = next(item for item in sessions if item["session_id"] == session_id)
    assert archived_entry.get("archived") is True

    unarchive = client.delete(
        f"/api/sessions/{session_id}/archive",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
    )
    assert unarchive.status_code == 200
    assert unarchive.get_json()["archived"] is False


def test_update_session_title(monkeypatch, tmp_path):
    """PATCH /sessions/<id>/title persists user-edited titles."""
    _reset_backend(tmp_path, monkeypatch)
    client = backend.app.test_client()
    session_id = backend.session_store.create_session()
    backend.session_store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})

    # Successful update
    response = client.patch(
        f"/api/sessions/{session_id}/title",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={"title": "Edited title"},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body == {"session_id": session_id, "title": "Edited title"}
    assert backend.session_store.load_title(session_id) == "Edited title"

    # Title over 120 chars is truncated
    long = "a" * 300
    response = client.patch(
        f"/api/sessions/{session_id}/title",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={"title": long},
    )
    assert response.status_code == 200
    assert len(response.get_json()["title"]) == 120

    # Empty after strip → 400
    response = client.patch(
        f"/api/sessions/{session_id}/title",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={"title": "   "},
    )
    assert response.status_code == 400

    # Non-string payload → 400
    response = client.patch(
        f"/api/sessions/{session_id}/title",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={"title": 123},
    )
    assert response.status_code == 400

    # Unknown session → 404
    response = client.patch(
        "/api/sessions/does-not-exist/title",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={"title": "anything"},
    )
    assert response.status_code == 404

    # Missing auth → 401
    response = client.patch(
        f"/api/sessions/{session_id}/title",
        json={"title": "no auth"},
    )
    assert response.status_code == 401


def test_regenerate_session_title(monkeypatch, tmp_path):
    """POST /sessions/<id>/title regenerates title via AI."""
    _reset_backend(tmp_path, monkeypatch)
    client = backend.app.test_client()
    session_id = backend.session_store.create_session()
    backend.session_store.append_event(
        session_id, {"type": "user", "payload": {"text": "Debug CI"}}
    )
    backend.session_store.append_event(
        session_id, {"type": "assistant", "payload": {"text": "Checking..."}}
    )

    async def fake_gen(_events):
        return "Debug CI Pipeline"

    with patch("truss_core.title_generator.generate_session_title", fake_gen):
        resp = client.post(
            f"/api/sessions/{session_id}/title",
            headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["title"] == "Debug CI Pipeline"
    assert backend.session_store.load_title(session_id) == "Debug CI Pipeline"
    # title_update event emitted
    events = backend.session_store.load_transcript(session_id)
    assert any(e.get("type") == "title_update" for e in events)

    # Returns 422 when generator returns None
    async def null_gen(_events):
        return None

    with patch("truss_core.title_generator.generate_session_title", null_gen):
        resp = client.post(
            f"/api/sessions/{session_id}/title",
            headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        )
    assert resp.status_code == 422

    # Unknown session → 404
    resp = client.post(
        "/api/sessions/does-not-exist/title",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
    )
    assert resp.status_code == 404


def test_notifications_endpoints(monkeypatch, tmp_path):
    """Create, dismiss, and clear notifications."""
    _reset_backend(tmp_path, monkeypatch)
    client = backend.app.test_client()
    create = client.post(
        "/api/sessions",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={},
    )
    assert create.status_code == 200
    listing = client.get(
        "/api/notifications",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
    )
    assert listing.status_code == 200
    notifications = listing.get_json()["notifications"]
    assert notifications
    first_id = notifications[0]["id"]

    dismiss = client.post(
        "/api/notifications/dismiss",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={"id": first_id},
    )
    assert dismiss.status_code == 200
    assert dismiss.get_json()["dismissed"] == 1

    listing = client.get(
        "/api/notifications?include_dismissed=1",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
    )
    notifications = listing.get_json()["notifications"]
    dismissed = next(item for item in notifications if item["id"] == first_id)
    assert dismissed.get("dismissed") is True

    cleared = client.post(
        "/api/notifications/clear",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={},
    )
    assert cleared.status_code == 200
    listing = client.get(
        "/api/notifications?include_dismissed=1",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
    )
    notifications = listing.get_json()["notifications"]
    assert all(item["id"] != first_id for item in notifications)


def test_notifications_require_api_key(monkeypatch, tmp_path):
    """Require authentication headers for notification endpoints."""
    _reset_backend(tmp_path, monkeypatch)
    client = backend.app.test_client()
    assert client.get("/api/notifications").status_code == 401
    assert client.post("/api/notifications/dismiss").status_code == 401
    assert client.post("/api/notifications/clear").status_code == 401


def test_notifications_dismiss_ids_and_clear_all(monkeypatch, tmp_path):
    """Dismiss multiple ids and clear all notifications."""
    _reset_backend(tmp_path, monkeypatch)
    client = backend.app.test_client()
    first = backend.notification_store.add(title="one", message="first")
    second = backend.notification_store.add(title="two", message="second")

    dismiss = client.post(
        "/api/notifications/dismiss",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={"ids": [first["id"], second["id"]]},
    )
    assert dismiss.status_code == 200
    assert dismiss.get_json()["dismissed"] == 2

    cleared = client.post(
        "/api/notifications/clear",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={"clear_all": "true"},
    )
    assert cleared.status_code == 200
    assert cleared.get_json()["cleared"] == 2


def test_notification_service_skips_invalid_completion_payload(monkeypatch, tmp_path):
    """Skip completion notifications with invalid payloads."""
    _reset_backend(tmp_path, monkeypatch)
    session_id = backend.session_store.create_session()

    monkeypatch.setattr(
        backend.session_store,
        "load_recent_events",
        lambda *_args, **_kwargs: [{"type": "completion", "payload": "bad", "ts": "1"}],
    )
    backend.notification_service.emit_completion(session_id)
    assert backend.notification_store.list(include_dismissed=True) == []


def test_notification_service_skips_missing_timestamp(monkeypatch, tmp_path):
    """Skip completion notifications without timestamps."""
    _reset_backend(tmp_path, monkeypatch)
    session_id = backend.session_store.create_session()

    monkeypatch.setattr(
        backend.session_store,
        "load_recent_events",
        lambda *_args, **_kwargs: [{"type": "completion", "payload": {"done": True}}],
    )
    backend.notification_service.emit_completion(session_id)
    assert backend.notification_store.list(include_dismissed=True) == []


def test_notification_service_avoids_duplicate_completion(monkeypatch, tmp_path):
    """Avoid duplicate completion notifications for the same timestamp."""
    _reset_backend(tmp_path, monkeypatch)
    session_id = backend.session_store.create_session()
    other_session = backend.session_store.create_session()
    backend.notification_store.add(
        title="Other session",
        message="Other complete",
        session_id=other_session,
        event_type="completed",
        metadata={"completion_ts": "other"},
    )
    backend.session_store.append_event(
        session_id,
        {
            "type": "completion",
            "payload": {"done": True, "done_reason": "completed", "task_result": "ok"},
        },
    )
    backend.notification_service.emit_completion(session_id)
    backend.notification_service.emit_completion(session_id)
    notifications = backend.notification_store.list(include_dismissed=True)
    session_notifications = [item for item in notifications if item.get("session_id") == session_id]
    assert len(session_notifications) == 1


def test_attachments_upload(monkeypatch, tmp_path):
    """Upload attachments and return metadata."""
    _reset_backend(tmp_path, monkeypatch)
    client = backend.app.test_client()
    create = client.post(
        "/api/sessions",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={},
    )
    session_id = create.get_json()["session_id"]
    data = {"file": (io.BytesIO(b"hello"), "note.txt")}
    response = client.post(
        f"/api/sessions/{session_id}/attachments",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        data=data,
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    attachments = response.get_json()["attachments"]
    assert attachments
    stored_name = attachments[0]["stored_name"]
    path = tmp_path / session_id / "attachments" / stored_name
    assert path.exists()


def test_attachments_errors(monkeypatch, tmp_path):
    """Return validation errors for attachment uploads."""
    _reset_backend(tmp_path, monkeypatch)
    client = backend.app.test_client()
    session_id = backend.session_store.create_session()

    unauthorized = client.post(f"/api/sessions/{session_id}/attachments")
    assert unauthorized.status_code == 401

    missing = client.post(
        "/api/sessions/missing/attachments",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        data={},
        content_type="multipart/form-data",
    )
    assert missing.status_code == 404

    no_files = client.post(
        f"/api/sessions/{session_id}/attachments",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        data={},
        content_type="multipart/form-data",
    )
    assert no_files.status_code == 400

    invalid_name = client.post(
        f"/api/sessions/{session_id}/attachments",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        data={"file": (io.BytesIO(b"data"), "")},
        content_type="multipart/form-data",
    )
    assert invalid_name.status_code == 400


def test_share_and_export(monkeypatch, tmp_path):
    """Create share tokens and export session transcripts."""
    _reset_backend(tmp_path, monkeypatch)
    client = backend.app.test_client()
    create = client.post(
        "/api/sessions",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={},
    )
    session_id = create.get_json()["session_id"]
    share = client.post(
        f"/api/sessions/{session_id}/share",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
    )
    assert share.status_code == 200
    token = share.get_json()["token"]

    export = client.get(
        f"/api/sessions/{session_id}/export",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
    )
    assert export.status_code == 200
    assert export.get_json()["session_id"] == session_id

    shared = client.get(f"/api/share/{token}")
    assert shared.status_code == 200
    payload = shared.get_json()
    assert payload["session_id"] == session_id


def test_share_and_export_errors(monkeypatch, tmp_path):
    """Return error responses for missing share/export inputs."""
    _reset_backend(tmp_path, monkeypatch)
    client = backend.app.test_client()

    unauthorized_share = client.post("/api/sessions/missing/share")
    assert unauthorized_share.status_code == 401

    missing_share = client.post(
        "/api/sessions/missing/share",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
    )
    assert missing_share.status_code == 404

    unauthorized_export = client.get("/api/sessions/missing/export")
    assert unauthorized_export.status_code == 401

    missing_export = client.get(
        "/api/sessions/missing/export",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
    )
    assert missing_export.status_code == 404

    missing_token = client.get("/api/share/does-not-exist")
    assert missing_token.status_code == 404


def test_slash_command_terminate(monkeypatch, tmp_path):
    """Terminate a running session via slash command."""
    _reset_backend(tmp_path, monkeypatch)

    def slow_run_sync(*, session_id: str, user_query: str, should_cancel=None, **_kwargs):
        backend.session_store.append_event(
            session_id, {"type": "user", "payload": {"text": user_query}}
        )
        while should_cancel and not should_cancel():
            time.sleep(0.01)
        backend.session_store.append_event(
            session_id,
            {
                "type": "completion",
                "payload": {"done": True, "done_reason": "canceled", "task_result": None},
            },
        )

    monkeypatch.setattr(backend.runtime, "run_sync", slow_run_sync)
    client = backend.app.test_client()
    session_id = backend.session_store.create_session()

    enqueue = client.post(
        f"/api/sessions/{session_id}/query",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={"query": "long running"},
    )
    assert enqueue.status_code == 202

    terminate = client.post(
        f"/api/sessions/{session_id}/query",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={"query": "/terminate"},
    )
    assert terminate.status_code == 202
    _wait_for_run(session_id)

    events = client.get(
        f"/api/sessions/{session_id}/events",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
    )
    payload = events.get_json()
    assert payload["events"][-1]["type"] == "completion"
    assert payload["events"][-1]["payload"]["done_reason"] == "canceled"


def test_query_appends_context_payload(monkeypatch, tmp_path):
    """Append context/attachments payload to query events."""
    _reset_backend(tmp_path, monkeypatch)
    client = backend.app.test_client()
    captured = []

    def fake_append_context_event(session_id, payload):
        captured.append((session_id, payload))

    monkeypatch.setattr(backend.runtime, "append_context_event", fake_append_context_event)
    monkeypatch.setattr(backend.runtime, "start_async", lambda **_kwargs: True)

    session_id = backend.session_store.create_session()
    response = client.post(
        f"/api/sessions/{session_id}/query",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={
            "query": "hello",
            "context": {"repo": "acme/app"},
            "attachments": [{"id": "file-1", "filename": "note.txt"}],
        },
    )
    assert response.status_code == 202
    assert captured[0][1]["attachments"]

    captured.clear()

    def fake_run_sync(*_args, **_kwargs):
        return _make_task_queue("ok")

    monkeypatch.setattr(backend.runtime, "run_sync", fake_run_sync)
    response = client.post(
        "/api/query",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={
            "query": "hello",
            "attachments": [{"id": "file-1", "filename": "note.txt"}],
        },
    )
    assert response.status_code == 200
    assert captured


def test_parse_mode_invalid_value():
    """Return None for invalid mode inputs."""
    assert backend._parse_mode("invalid") is None


def test_query_persists_mode_in_context_event(monkeypatch, tmp_path):
    """Top-level mode is mirrored into the context event payload.

    This is what lets the console rehydrate the plan/act toggle when the
    user re-opens a past session — summarize_session surfaces the last
    context event's payload as SessionSummary.context, which InputBar
    reads to initialize queryMode.
    """
    _reset_backend(tmp_path, monkeypatch)
    client = backend.app.test_client()
    captured: list[tuple[str, dict]] = []

    def fake_append_context_event(session_id, payload):
        captured.append((session_id, payload))

    monkeypatch.setattr(backend.runtime, "append_context_event", fake_append_context_event)
    monkeypatch.setattr(backend.runtime, "start_async", lambda **_kwargs: True)

    session_id = backend.session_store.create_session()
    response = client.post(
        f"/api/sessions/{session_id}/query",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={"query": "hello", "mode": "plan"},
    )
    assert response.status_code == 202
    assert captured, "context event was not appended"
    assert captured[0][1].get("mode") == "plan"


def test_query_omits_mode_when_invalid(monkeypatch, tmp_path):
    """Invalid/missing mode values do not pollute the context payload."""
    _reset_backend(tmp_path, monkeypatch)
    client = backend.app.test_client()
    captured: list[tuple[str, dict]] = []

    def fake_append_context_event(session_id, payload):
        captured.append((session_id, payload))

    monkeypatch.setattr(backend.runtime, "append_context_event", fake_append_context_event)
    monkeypatch.setattr(backend.runtime, "start_async", lambda **_kwargs: True)

    session_id = backend.session_store.create_session()
    response = client.post(
        f"/api/sessions/{session_id}/query",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={"query": "hello", "mode": "bogus", "context": {"model": "x"}},
    )
    assert response.status_code == 202
    assert captured
    assert "mode" not in captured[0][1]


def test_tools_list(monkeypatch, tmp_path):
    """Return tool metadata for the MCP picker."""
    _reset_backend(tmp_path, monkeypatch)
    client = backend.app.test_client()
    response = client.get(
        "/api/tools",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert "tools" in payload


# ------------------------------------------------------------------
# Channel webhook route tests
# ------------------------------------------------------------------


_NC_SECRET = "test-bot-secret-at-least-40-characters-long!"


def _nc_sign(body: str, random: str = "testrandom") -> dict:
    """Build valid Nextcloud Talk webhook headers."""
    digest = hmac_mod.new(
        _NC_SECRET.encode(),
        (random + body).encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Nextcloud-Talk-Signature": digest,
        "X-Nextcloud-Talk-Random": random,
        "X-Nextcloud-Talk-Backend": "https://nc.test",
        "Content-Type": "application/json",
    }


def _nc_payload(
    message: str = "Hello bot",
    message_id: str = "100",
    thread_id: int | None = None,
) -> str:
    obj = {
        "type": "Note",
        "id": message_id,
        "name": "message",
        "content": json.dumps({"message": message, "parameters": {}}),
        "mediaType": "text/markdown",
    }
    if thread_id is not None:
        obj["threadId"] = thread_id
    return json.dumps(
        {
            "type": "Create",
            "actor": {"type": "Person", "id": "users/alice", "name": "Alice"},
            "object": obj,
            "target": {"type": "Collection", "id": "room1", "name": "General"},
        }
    )


def _setup_nc_channel(tmp_path, monkeypatch):
    """Reset backend and register a Nextcloud Talk adapter."""
    _reset_backend(tmp_path, monkeypatch)
    from truss_api.channels import routes as ch_routes
    from truss_api.channels.nextcloud_talk import NextcloudTalkAdapter

    adapter = NextcloudTalkAdapter(
        bot_secret=_NC_SECRET,
        nextcloud_url="https://nc.test",
    )
    ch_routes._runtime = backend.runtime
    ch_routes._hook_manager = backend._hook_manager
    ch_routes._registry.register(adapter)


def test_webhook_unknown_platform(monkeypatch, tmp_path):
    """Unknown platform returns 404."""
    _setup_nc_channel(tmp_path, monkeypatch)
    client = backend.app.test_client()
    resp = client.post("/api/webhooks/nonexistent", data=b"{}")
    assert resp.status_code == 404


def test_webhook_invalid_signature(monkeypatch, tmp_path):
    """Bad HMAC signature returns 401."""
    _setup_nc_channel(tmp_path, monkeypatch)
    client = backend.app.test_client()
    body = _nc_payload()
    headers = {
        "X-Nextcloud-Talk-Signature": "bad" * 20,
        "X-Nextcloud-Talk-Random": "xyz",
        "X-Nextcloud-Talk-Backend": "https://nc.test",
        "Content-Type": "application/json",
    }
    resp = client.post("/api/webhooks/nextcloud-talk", data=body, headers=headers)
    assert resp.status_code == 401


def test_webhook_creates_session(monkeypatch, tmp_path):
    """Valid webhook with @mention creates a session and starts a run."""
    _setup_nc_channel(tmp_path, monkeypatch)
    client = backend.app.test_client()

    captured = {}

    def fake_start_async(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(backend.runtime, "start_async", fake_start_async)

    body = _nc_payload(message="@Truss help me", message_id="200")
    headers = _nc_sign(body)
    resp = client.post("/api/webhooks/nextcloud-talk", data=body, headers=headers)
    assert resp.status_code == 200

    # Session was created with room-scoped tag
    session_id = captured.get("session_id")
    assert session_id is not None
    assert backend.session_store.resolve_tag("nextcloud-talk:room:room1") == session_id

    # Trigger keyword is stripped from user_query
    assert captured["user_query"] == "help me"

    # Client system context injected via skill_instructions
    si = captured.get("skill_instructions", "")
    assert "Nextcloud Talk" in si
    assert "/help" in si

    # Context event was injected
    events = backend.session_store.load_transcript(session_id)
    ctx = next(e for e in events if e.get("type") == "context" and "sender" in e.get("payload", {}))
    assert ctx["payload"]["source_platform"] == "nextcloud-talk"
    assert ctx["payload"]["channel_id"] == "room1"
    assert ctx["payload"]["sender"] == "Alice"


def test_webhook_room_scoped_session_continuity(monkeypatch, tmp_path):
    """Multiple messages in the same room map to the same session."""
    _setup_nc_channel(tmp_path, monkeypatch)
    client = backend.app.test_client()

    sessions = []

    def fake_start_async(**kwargs):
        sessions.append(kwargs["session_id"])
        return True

    monkeypatch.setattr(backend.runtime, "start_async", fake_start_async)

    # First @mention (unique message_id to avoid dedup)
    body1 = _nc_payload(message="@Truss first", message_id="300")
    client.post("/api/webhooks/nextcloud-talk", data=body1, headers=_nc_sign(body1))

    # Second @mention in same room (different message_id, no threadId)
    body2 = _nc_payload(message="@Truss follow-up", message_id="301")
    client.post("/api/webhooks/nextcloud-talk", data=body2, headers=_nc_sign(body2))

    # Both should target the same room-scoped session
    assert len(sessions) == 2
    assert sessions[0] == sessions[1]


def test_webhook_thread_scoped_session(monkeypatch, tmp_path):
    """Messages with threadId get a separate thread-scoped session."""
    _setup_nc_channel(tmp_path, monkeypatch)
    client = backend.app.test_client()

    sessions = []

    def fake_start_async(**kwargs):
        sessions.append(kwargs["session_id"])
        return True

    monkeypatch.setattr(backend.runtime, "start_async", fake_start_async)

    # Message in main room
    body1 = _nc_payload(message="@Truss main", message_id="400")
    client.post("/api/webhooks/nextcloud-talk", data=body1, headers=_nc_sign(body1))

    # Message in a thread
    body2 = _nc_payload(message="@Truss thread", message_id="401", thread_id=99)
    client.post("/api/webhooks/nextcloud-talk", data=body2, headers=_nc_sign(body2))

    # Different sessions (room vs thread)
    assert len(sessions) == 2
    assert sessions[0] != sessions[1]


def test_webhook_steers_running_session(monkeypatch, tmp_path):
    """Follow-up @mention to a running session uses enqueue_message."""
    _setup_nc_channel(tmp_path, monkeypatch)
    client = backend.app.test_client()

    started = []
    steered = []

    def fake_start_async(**kwargs):
        started.append(kwargs["session_id"])
        return True

    def fake_is_running(sid):
        return sid in started  # "running" after first start

    def fake_enqueue(sid, text):
        steered.append((sid, text))
        return True

    monkeypatch.setattr(backend.runtime, "start_async", fake_start_async)
    monkeypatch.setattr(backend.runtime, "is_running", fake_is_running)
    monkeypatch.setattr(backend.runtime, "enqueue_message", fake_enqueue)

    # First @mention — starts session
    body1 = _nc_payload(message="@Truss start", message_id="500")
    client.post("/api/webhooks/nextcloud-talk", data=body1, headers=_nc_sign(body1))

    # Second @mention in same room while session is running
    body2 = _nc_payload(message="@Truss more context", message_id="501")
    resp2 = client.post("/api/webhooks/nextcloud-talk", data=body2, headers=_nc_sign(body2))
    assert resp2.status_code == 200

    assert len(started) == 1
    assert len(steered) == 1
    assert steered[0][1] == "more context"  # trigger keyword stripped


def test_webhook_no_mention_ignored(monkeypatch, tmp_path):
    """Messages without @mention are silently ignored — no session, no events."""
    _setup_nc_channel(tmp_path, monkeypatch)
    client = backend.app.test_client()

    started = []
    monkeypatch.setattr(backend.runtime, "start_async", lambda **kw: started.append(1) or True)

    body = _nc_payload(message="Just chatting", message_id="600")
    resp = client.post("/api/webhooks/nextcloud-talk", data=body, headers=_nc_sign(body))
    assert resp.status_code == 200

    # No LLM run and no session created
    assert len(started) == 0
    assert backend.session_store.resolve_tag("nextcloud-talk:room:room1") is None


def _send_command(client, monkeypatch, message, message_id="610"):
    """Send a command via webhook and capture the bot's response text."""
    responses = []
    from truss_api.channels import routes as ch_routes

    adapter = ch_routes._registry.get("nextcloud-talk")

    def capture_send(channel_id, text, **kw):
        responses.append(text)
        return "sent"

    monkeypatch.setattr(adapter, "send_response", capture_send)
    started = []
    monkeypatch.setattr(backend.runtime, "start_async", lambda **kw: started.append(1) or True)

    body = _nc_payload(message=message, message_id=message_id)
    resp = client.post("/api/webhooks/nextcloud-talk", data=body, headers=_nc_sign(body))
    return resp, responses, started


def test_webhook_help_command(monkeypatch, tmp_path):
    """/help returns auto-generated command list without invoking the LLM."""
    _setup_nc_channel(tmp_path, monkeypatch)
    client = backend.app.test_client()

    resp, responses, started = _send_command(client, monkeypatch, "@Truss /help")
    assert resp.status_code == 200
    assert len(started) == 0
    assert len(responses) == 1
    text = responses[0]
    assert "/help" in text
    assert "/usage" in text
    assert "/new" in text
    assert "/switch-project" in text


def test_webhook_usage_command(monkeypatch, tmp_path):
    """/usage returns token budget stats without invoking the LLM."""
    _setup_nc_channel(tmp_path, monkeypatch)
    client = backend.app.test_client()

    resp, responses, started = _send_command(
        client, monkeypatch, "@Truss /usage", message_id="611"
    )
    assert resp.status_code == 200
    assert len(started) == 0
    assert len(responses) == 1
    text = responses[0]
    assert "Events:" in text
    assert "Tokens used:" in text
    assert "Utilization:" in text


def test_webhook_new_command(monkeypatch, tmp_path):
    """/new creates a fresh session, replacing the tag mapping."""
    _setup_nc_channel(tmp_path, monkeypatch)
    client = backend.app.test_client()

    monkeypatch.setattr(backend.runtime, "start_async", lambda **kw: True)

    # First message creates session A
    body1 = _nc_payload(message="@Truss hello", message_id="620")
    client.post("/api/webhooks/nextcloud-talk", data=body1, headers=_nc_sign(body1))
    session_a = backend.session_store.resolve_tag("nextcloud-talk:room:room1")
    assert session_a is not None

    # /new creates session B — capture the response
    resp, responses, _ = _send_command(client, monkeypatch, "@Truss /new", message_id="621")
    assert resp.status_code == 200
    assert len(responses) == 1
    assert "Fresh conversation" in responses[0]
    session_b = backend.session_store.resolve_tag("nextcloud-talk:room:room1")
    assert session_b is not None
    assert session_b != session_a


def test_webhook_switch_project_valid(monkeypatch, tmp_path):
    """/switch-project stores active project and passes cwd to start_async."""
    _setup_nc_channel(tmp_path, monkeypatch)
    client = backend.app.test_client()
    from truss_api.channels import routes as ch_routes
    from truss_core.config import ProjectConfig

    project_dir = str(tmp_path / "my-project")
    os.makedirs(project_dir, exist_ok=True)
    monkeypatch.setattr(
        ch_routes,
        "get_config",
        lambda: type(
            "Cfg",
            (),
            {
                "projects": {
                    "test-proj": ProjectConfig(path=project_dir, description="Test"),
                }
            },
        )(),
    )

    # Switch to the project
    body = _nc_payload(message="@Truss /switch-project test-proj", message_id="630")
    resp = client.post("/api/webhooks/nextcloud-talk", data=body, headers=_nc_sign(body))
    assert resp.status_code == 200

    # Verify context event was written
    session_id = backend.session_store.resolve_tag("nextcloud-talk:room:room1")
    events = backend.session_store.load_transcript(session_id)
    project_ctx = [
        e
        for e in events
        if e.get("type") == "context" and "active_project_cwd" in e.get("payload", {})
    ]
    assert len(project_ctx) == 1
    assert project_ctx[0]["payload"]["active_project_cwd"] == project_dir

    # Now send a real query — cwd should be read from the context event
    captured = {}
    monkeypatch.setattr(backend.runtime, "start_async", lambda **kw: captured.update(kw) or True)

    body2 = _nc_payload(message="@Truss list files", message_id="631")
    client.post("/api/webhooks/nextcloud-talk", data=body2, headers=_nc_sign(body2))
    assert captured.get("cwd") == project_dir


def test_webhook_switch_project_invalid(monkeypatch, tmp_path):
    """/switch-project with unknown name returns project list."""
    _setup_nc_channel(tmp_path, monkeypatch)
    client = backend.app.test_client()

    resp, responses, started = _send_command(
        client, monkeypatch, "@Truss /switch-project nonexistent", message_id="640"
    )
    assert resp.status_code == 200
    assert len(started) == 0
    assert len(responses) == 1
    assert "Unknown project" in responses[0]
    assert "Available projects" in responses[0]


def test_webhook_non_create_event_acknowledged(monkeypatch, tmp_path):
    """Update/Delete events return 200 without creating sessions."""
    _setup_nc_channel(tmp_path, monkeypatch)
    client = backend.app.test_client()

    body = json.dumps(
        {
            "type": "Delete",
            "actor": {"type": "Person", "id": "users/alice", "name": "Alice"},
            "object": {
                "type": "Note",
                "id": "500",
                "name": "msg",
                "content": "{}",
                "mediaType": "text/plain",
            },
            "target": {"type": "Collection", "id": "room1", "name": "General"},
        }
    )
    headers = _nc_sign(body)
    resp = client.post("/api/webhooks/nextcloud-talk", data=body, headers=headers)
    assert resp.status_code == 200

    # No session should have been created
    assert backend.session_store.resolve_tag("nextcloud-talk:500") is None


def test_webhook_dedup_replayed_message(monkeypatch, tmp_path):
    """Replayed webhooks are silently acknowledged."""
    _setup_nc_channel(tmp_path, monkeypatch)
    client = backend.app.test_client()

    start_count = []

    def fake_start_async(**kwargs):
        start_count.append(1)
        return True

    monkeypatch.setattr(backend.runtime, "start_async", fake_start_async)

    body = _nc_payload(message="@Truss replay test", message_id="700")
    headers = _nc_sign(body)

    # Send same message twice
    resp1 = client.post("/api/webhooks/nextcloud-talk", data=body, headers=headers)
    resp2 = client.post("/api/webhooks/nextcloud-talk", data=body, headers=headers)
    assert resp1.status_code == 200
    assert resp2.status_code == 200

    # Only one LLM run should have been started (second is dedup'd)
    assert len(start_count) == 1


def test_webhook_session_accessible_via_events_api(monkeypatch, tmp_path):
    """Sessions created by webhooks are accessible via standard event polling."""
    _setup_nc_channel(tmp_path, monkeypatch)
    client = backend.app.test_client()

    monkeypatch.setattr(backend.runtime, "start_async", lambda **kw: True)

    body = _nc_payload(message="@Truss API visibility test", message_id="800")
    client.post(
        "/api/webhooks/nextcloud-talk",
        data=body,
        headers=_nc_sign(body),
    )

    # Resolve session from room-scoped tag
    session_id = backend.session_store.resolve_tag("nextcloud-talk:room:room1")
    assert session_id is not None

    # Events should be accessible via standard API
    resp = client.get(
        f"/api/sessions/{session_id}/events",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
    )
    assert resp.status_code == 200
    events = resp.get_json()["events"]
    ctx_events = [e for e in events if e.get("type") == "context"]
    assert len(ctx_events) >= 1
    assert ctx_events[0]["payload"]["source_platform"] == "nextcloud-talk"
