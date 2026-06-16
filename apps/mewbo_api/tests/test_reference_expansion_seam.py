"""Integration: @<ref> expansion is wired at the API submit seam, pre-LLM."""

# mypy: ignore-errors
import subprocess

from mewbo_api import backend
from mewbo_core.session_store import SessionStore


def _reset_backend(tmp_path):
    backend.session_store = SessionStore(root_dir=str(tmp_path))
    backend.runtime = backend.SessionRuntime(session_store=backend.session_store)
    backend.notification_store = backend.NotificationStore(root_dir=str(tmp_path))
    backend.share_store = backend.ShareStore(root_dir=str(tmp_path))
    backend.notification_service = backend.NotificationService(
        backend.notification_store,
        backend.runtime.session_store,
    )


def test_query_seam_expands_file_ref(monkeypatch, tmp_path):
    """A @<file> ref in the query body is expanded before start_async sees it."""
    _reset_backend(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "hello.py").write_text("ANSWER = 42\n")

    # Bind the session's cwd to the workspace and capture what the runtime runs.
    monkeypatch.setattr(backend, "_resolve_project_cwd", lambda data: str(workspace))
    captured = {}

    def fake_start_async(*args, **kwargs):
        captured["user_query"] = kwargs.get("user_query")
        return "sess:r1"

    client = backend.app.test_client()
    resp = client.post(
        "/api/sessions", json={}, headers={"X-API-KEY": backend.MASTER_API_TOKEN}
    )
    session_id = resp.get_json()["session_id"]
    monkeypatch.setattr(backend.runtime, "start_async", fake_start_async)

    resp = client.post(
        f"/api/sessions/{session_id}/query",
        json={"query": "explain @hello.py"},
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
    )
    assert resp.status_code == 202
    assert "ANSWER = 42" in captured["user_query"]
    assert "--- @hello.py ---" in captured["user_query"]


def test_query_seam_leaves_bogus_ref_literal(monkeypatch, tmp_path):
    """An unresolved @ref is passed through verbatim, not stripped."""
    _reset_backend(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(backend, "_resolve_project_cwd", lambda data: str(workspace))
    captured = {}

    def fake_start_async(*args, **kwargs):
        captured["user_query"] = kwargs.get("user_query")
        return "sess:r1"

    client = backend.app.test_client()
    resp = client.post(
        "/api/sessions", json={}, headers={"X-API-KEY": backend.MASTER_API_TOKEN}
    )
    session_id = resp.get_json()["session_id"]
    monkeypatch.setattr(backend.runtime, "start_async", fake_start_async)

    resp = client.post(
        f"/api/sessions/{session_id}/query",
        json={"query": "look at @nope/missing.py here"},
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
    )
    assert resp.status_code == 202
    assert captured["user_query"] == "look at @nope/missing.py here"


def test_files_endpoint_lists_git_index(monkeypatch, tmp_path):
    """GET /files?session=<id> returns the git-indexed project files."""
    _reset_backend(tmp_path)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "-C", str(workspace), "init", "-q"], check=True)
    (workspace / "main.py").write_text("x = 1\n")
    (workspace / "README.md").write_text("# hi\n")
    (workspace / ".gitignore").write_text("secret.txt\n")
    (workspace / "secret.txt").write_text("nope\n")

    client = backend.app.test_client()
    resp = client.post(
        "/api/sessions", json={}, headers={"X-API-KEY": backend.MASTER_API_TOKEN}
    )
    session_id = resp.get_json()["session_id"]
    # Bind the session cwd via a context event (what a real query would persist).
    backend.runtime.append_context_event(session_id, {"cwd": str(workspace)})

    resp = client.get(
        f"/api/files?session={session_id}",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
    )
    assert resp.status_code == 200
    files = resp.get_json()["files"]
    assert "main.py" in files
    assert "README.md" in files
    assert "secret.txt" not in files  # gitignored → out of scope

    # Substring filter.
    resp = client.get(
        f"/api/files?session={session_id}&q=read",
        headers={"X-API-KEY": backend.MASTER_API_TOKEN},
    )
    assert resp.get_json()["files"] == ["README.md"]
