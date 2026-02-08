"""Tests for the Meeseeks API backend."""

# mypy: ignore-errors
import json
import time

from meeseeks_api import backend


class DummyQueue:
    """Minimal task queue stub for API responses."""

    def __init__(self, result: str) -> None:
        """Initialize the dummy queue with a single action result."""
        self.task_result = result
        self.action_steps = [
            {
                "action_consumer": "home_assistant_tool",
                "action_type": "get",
                "action_argument": "say",
                "result": result,
            }
        ]

    def dict(self):
        """Return a serialized representation of the queue."""
        return {
            "task_result": self.task_result,
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

    def fake_run_sync(*args, **kwargs):
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
    assert payload["action_steps"]


def test_query_with_session_tag(monkeypatch, tmp_path):
    """Create or reuse a tagged session and pass it into orchestration."""
    backend.session_store = backend.SessionStore(root_dir=str(tmp_path))
    backend.runtime = backend.SessionRuntime(session_store=backend.session_store)
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
    backend.session_store = backend.SessionStore(root_dir=str(tmp_path))
    backend.runtime = backend.SessionRuntime(session_store=backend.session_store)
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
    backend.session_store = backend.SessionStore(root_dir=str(tmp_path))
    backend.runtime = backend.SessionRuntime(session_store=backend.session_store)


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
