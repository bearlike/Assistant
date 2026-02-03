"""Tests for the Meeseeks API backend."""
# mypy: ignore-errors
import json
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

import backend  # noqa: E402


class DummyQueue:
    """Minimal task queue stub for API responses."""
    def __init__(self, result: str) -> None:
        """Initialize the dummy queue with a single action result."""
        self.task_result = result
        self.action_steps = [
            {
                "action_consumer": "talk_to_user_tool",
                "action_type": "set",
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

    def fake_orchestrate(*args, **kwargs):
        return _make_task_queue("ok")

    monkeypatch.setattr(backend, "orchestrate_session", fake_orchestrate)
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
    client = backend.app.test_client()
    captured = {}

    def fake_orchestrate(*args, **kwargs):
        captured["session_id"] = kwargs.get("session_id")
        return _make_task_queue("ok")

    monkeypatch.setattr(backend, "orchestrate_session", fake_orchestrate)
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
    source_session = backend.session_store.create_session()
    client = backend.app.test_client()
    captured = {}

    def fake_orchestrate(*args, **kwargs):
        captured["session_id"] = kwargs.get("session_id")
        return _make_task_queue("ok")

    monkeypatch.setattr(backend, "orchestrate_session", fake_orchestrate)
    response = client.post(
        "/api/query",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
        json={"query": "hello", "fork_from": source_session},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["session_id"] == captured["session_id"]
    assert payload["session_id"] != source_session
