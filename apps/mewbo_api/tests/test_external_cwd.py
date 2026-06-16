"""Tests for the external-cwd feature (Gitea #91).

Covers:
1. Flag off (default) + cwd in body → 403 structured error, no session started.
2. Flag on + valid dir → session created, start_async receives that cwd,
   context event reports it.
3. Flag on + nonexistent path / file-not-dir → 400 structured error.
4. Re-engagement: /message on the session keeps resolving the same cwd.
5. No cwd → behaviour identical to before (existing tests unaffected).

Stubs: ``SessionRuntime.start_async`` / ``run_sync`` — the I/O boundary.
Guard: tests read the route module's own runtime via ``backend.runtime``
after ``_reset_backend`` so the ``test_backend._reset_backend`` plain-
assignment leak does not cross-contaminate (see tests/CLAUDE.md pitfall).
"""

# mypy: ignore-errors

import pytest
from mewbo_api import backend
from mewbo_core.session_store import SessionStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_backend(tmp_path, monkeypatch):
    """Swap module-level stores to fresh temp-dir-backed stores."""
    backend.session_store = SessionStore(root_dir=str(tmp_path))
    backend.runtime = backend.SessionRuntime(session_store=backend.session_store)
    backend.notification_store = backend.NotificationStore(root_dir=str(tmp_path))
    backend.share_store = backend.ShareStore(root_dir=str(tmp_path))
    backend.notification_service = backend.NotificationService(
        backend.notification_store,
        backend.runtime.session_store,
    )


def _fake_start_async(*args, **kwargs):
    """Stub that records kwargs and pretends it started a run."""
    _fake_start_async.last_kwargs = kwargs
    return "fakesession:r0"


_fake_start_async.last_kwargs = {}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExternalCwdFlagOff:
    """With api.allow_external_cwd=False (default), any explicit cwd is rejected."""

    def test_create_session_with_cwd_rejected(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """POST /api/sessions with cwd → 403 when flag is off."""
        _reset_backend(tmp_path, monkeypatch)
        valid_dir = str(tmp_path)
        resp = client.post(
            "/api/sessions",
            headers=auth_headers,
            json={"cwd": valid_dir},
        )
        assert resp.status_code == 403
        body = resp.get_json()
        assert "error" in body
        assert body["error"]["code"] == 403
        assert "allow_external_cwd" in body["error"]["reason"]

    def test_query_with_cwd_rejected(self, client, auth_headers, tmp_path, monkeypatch):
        """POST /api/sessions/{id}/query with cwd → 403 when flag is off."""
        _reset_backend(tmp_path, monkeypatch)
        session_id = backend.session_store.create_session()
        valid_dir = str(tmp_path)
        resp = client.post(
            f"/api/sessions/{session_id}/query",
            headers=auth_headers,
            json={"query": "hello", "cwd": valid_dir},
        )
        assert resp.status_code == 403
        body = resp.get_json()
        assert "error" in body
        assert body["error"]["code"] == 403

    def test_context_cwd_rejected(self, client, auth_headers, tmp_path, monkeypatch):
        """cwd nested in context dict is also rejected when flag is off."""
        _reset_backend(tmp_path, monkeypatch)
        valid_dir = str(tmp_path)
        resp = client.post(
            "/api/sessions",
            headers=auth_headers,
            json={"context": {"cwd": valid_dir}},
        )
        assert resp.status_code == 403

    def test_no_cwd_still_works(self, client, auth_headers, tmp_path, monkeypatch):
        """POST /api/sessions without cwd succeeds even with flag off."""
        _reset_backend(tmp_path, monkeypatch)
        resp = client.post("/api/sessions", headers=auth_headers, json={})
        assert resp.status_code == 200
        assert "session_id" in resp.get_json()


class TestExternalCwdFlagOn:
    """With api.allow_external_cwd=True, valid paths work; invalid paths are rejected."""

    @pytest.fixture(autouse=True)
    def _enable_flag(self, monkeypatch):
        """Patch get_config() to return a config with allow_external_cwd=True."""
        from mewbo_core.config import get_config

        cfg = get_config()
        patched_api = cfg.api.model_copy(update={"allow_external_cwd": True})
        patched_cfg = cfg.model_copy(update={"api": patched_api})
        monkeypatch.setattr("mewbo_api.backend.get_config", lambda: patched_cfg)

    def test_valid_dir_create_session(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """POST /api/sessions with a valid cwd dir → session created, cwd persisted."""
        _reset_backend(tmp_path, monkeypatch)
        valid_dir = str(tmp_path)
        resp = client.post(
            "/api/sessions",
            headers=auth_headers,
            json={"cwd": valid_dir},
        )
        assert resp.status_code == 200
        session_id = resp.get_json()["session_id"]

        # Verify the cwd was persisted in a context event.
        events = backend.session_store.load_transcript(session_id)
        context_events = [e for e in events if e.get("type") == "context"]
        assert context_events, "Expected at least one context event"
        ctx_payload = context_events[-1].get("payload", {})
        assert ctx_payload.get("cwd") == valid_dir

    def test_valid_dir_query_passes_cwd_to_runtime(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """POST /api/sessions/{id}/query with valid cwd → start_async receives that cwd."""
        _reset_backend(tmp_path, monkeypatch)
        session_id = backend.session_store.create_session()
        valid_dir = str(tmp_path)
        captured = {}

        def fake_start_async(*args, **kwargs):
            captured.update(kwargs)
            return "sid:r0"

        monkeypatch.setattr(backend.runtime, "start_async", fake_start_async)
        resp = client.post(
            f"/api/sessions/{session_id}/query",
            headers=auth_headers,
            json={"query": "hello", "cwd": valid_dir},
        )
        assert resp.status_code == 202
        assert captured.get("cwd") == valid_dir

    def test_valid_dir_context_cwd(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """cwd in context dict also works when flag is on."""
        _reset_backend(tmp_path, monkeypatch)
        session_id = backend.session_store.create_session()
        valid_dir = str(tmp_path)
        captured = {}

        def fake_start_async(*args, **kwargs):
            captured.update(kwargs)
            return "sid:r0"

        monkeypatch.setattr(backend.runtime, "start_async", fake_start_async)
        resp = client.post(
            f"/api/sessions/{session_id}/query",
            headers=auth_headers,
            json={"query": "hello", "context": {"cwd": valid_dir}},
        )
        assert resp.status_code == 202
        assert captured.get("cwd") == valid_dir

    def test_nonexistent_path_rejected(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """A cwd that does not exist on disk → 400 structured error."""
        _reset_backend(tmp_path, monkeypatch)
        nonexistent = str(tmp_path / "does_not_exist")
        resp = client.post(
            "/api/sessions",
            headers=auth_headers,
            json={"cwd": nonexistent},
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert "error" in body
        assert body["error"]["code"] == 400
        assert "does not exist" in body["error"]["reason"]

    def test_file_not_dir_rejected(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """A cwd that is a file, not a directory → 400 structured error."""
        _reset_backend(tmp_path, monkeypatch)
        file_path = tmp_path / "some_file.txt"
        file_path.write_text("content")
        resp = client.post(
            "/api/sessions",
            headers=auth_headers,
            json={"cwd": str(file_path)},
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert "error" in body
        assert body["error"]["code"] == 400
        assert "not a directory" in body["error"]["reason"]

    def test_query_nonexistent_path_rejected(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """Query endpoint: nonexistent cwd → 400."""
        _reset_backend(tmp_path, monkeypatch)
        session_id = backend.session_store.create_session()
        nonexistent = str(tmp_path / "ghost")
        resp = client.post(
            f"/api/sessions/{session_id}/query",
            headers=auth_headers,
            json={"query": "hi", "cwd": nonexistent},
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert "error" in body

    def test_no_cwd_falls_back_to_project(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """When no cwd is given the existing project resolution is unchanged."""
        _reset_backend(tmp_path, monkeypatch)
        captured = {}

        def fake_start_async(*args, **kwargs):
            captured.update(kwargs)
            return "sid:r0"

        monkeypatch.setattr(backend.runtime, "start_async", fake_start_async)
        session_id = backend.session_store.create_session()
        resp = client.post(
            f"/api/sessions/{session_id}/query",
            headers=auth_headers,
            json={"query": "hello"},
        )
        assert resp.status_code == 202
        # No cwd in body → cwd falls back to session_temp_dir (not None)
        assert captured.get("cwd") is not None


class TestExternalCwdReengagement:
    """Re-engagement via /message keeps the persisted cwd."""

    @pytest.fixture(autouse=True)
    def _enable_flag(self, monkeypatch):
        from mewbo_core.config import get_config

        cfg = get_config()
        patched_api = cfg.api.model_copy(update={"allow_external_cwd": True})
        patched_cfg = cfg.model_copy(update={"api": patched_api})
        monkeypatch.setattr("mewbo_api.backend.get_config", lambda: patched_cfg)

    def test_message_reengagement_uses_persisted_cwd(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """POST /api/sessions/{id}/message on idle session → start_async uses persisted cwd."""
        _reset_backend(tmp_path, monkeypatch)
        valid_dir = str(tmp_path)

        # Create session with a cwd persisted.
        create_resp = client.post(
            "/api/sessions",
            headers=auth_headers,
            json={"cwd": valid_dir},
        )
        assert create_resp.status_code == 200
        session_id = create_resp.get_json()["session_id"]

        # Now re-engage via /message (idle session → start_async path).
        captured = {}

        def fake_start_async(*args, **kwargs):
            captured.update(kwargs)
            return f"{session_id}:r1"

        # Also stub enqueue_message to return False (no active run) so the
        # re-engagement path is taken.
        monkeypatch.setattr(backend.runtime, "enqueue_message", lambda *a, **k: False)
        monkeypatch.setattr(backend.runtime, "start_async", fake_start_async)

        msg_resp = client.post(
            f"/api/sessions/{session_id}/message",
            headers=auth_headers,
            json={"text": "continue"},
        )
        assert msg_resp.status_code == 200
        assert captured.get("cwd") == valid_dir
