"""Route-level tests for the MCP gold-standard backend slice (Gitea #43/#44).

Covers the six fixes:
1. Global JSON 404 handler (no raw Werkzeug HTML leak).
2. /events returns authoritative status/done_reason/title.
3. /agents token rollup includes root (depth==0) tokens.
4. Idle semantics: interrupt → 200 no-op; message → re-engage 200.
5. /api/projects enriches each project with repo identity + aliases.
6. _resolve_repo_or_404 matches a key against a project's repo aliases.

Stubs only the I/O boundary (start_async / interrupt_step / git remotes).
"""

# mypy: ignore-errors

from __future__ import annotations

from mewbo_api import backend
from mewbo_api.repo_identity import RepoIdentity
from mewbo_core.config import get_config
from mewbo_core.session_store import SessionStore


def _reset_backend(tmp_path, monkeypatch):
    # Isolate the file-backed JsonProjectStore to this test's tmp dir. Without
    # this it reads/writes the real ~/.mewbo/virtual_projects.json, so projects
    # created by ANY test leak across the suite (and pollute real user data) —
    # which makes the alias-resolution tests (asserting a single candidate)
    # fragile to test ordering. Pointing config_dir at tmp_path gives each test
    # a fresh, empty project store.
    monkeypatch.setattr(get_config().runtime, "config_dir", str(tmp_path), raising=False)
    backend.session_store = SessionStore(root_dir=str(tmp_path))
    backend.runtime = backend.SessionRuntime(session_store=backend.session_store)
    backend.notification_store = backend.NotificationStore(root_dir=str(tmp_path))
    backend.share_store = backend.ShareStore(root_dir=str(tmp_path))
    backend.notification_service = backend.NotificationService(
        backend.notification_store,
        backend.runtime.session_store,
    )
    backend.project_store = backend.create_project_store()


# ---------------------------------------------------------------------------
# 1. Global JSON 404 handler
# ---------------------------------------------------------------------------


class TestGlobalJson404:
    def test_unmatched_route_returns_json(self, client):
        resp = client.get("/api/this/does/not/exist")
        assert resp.status_code == 404
        assert resp.is_json
        body = resp.get_json()
        assert body["error"]["code"] == 404
        assert isinstance(body["error"]["reason"], str)

    def test_no_html_leak(self, client):
        resp = client.get("/totally/unknown")
        assert resp.status_code == 404
        # The raw Werkzeug HTML page starts with "<!doctype html>".
        assert "text/html" not in resp.content_type
        assert resp.is_json


# ---------------------------------------------------------------------------
# 2. /events status/done_reason/title
# ---------------------------------------------------------------------------


class TestEventsStatus:
    def test_events_includes_status_done_reason_title(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.session_store.save_title(sid, "My Session")
        backend.session_store.append_event(sid, {"type": "user", "payload": {"text": "hi"}})
        backend.session_store.append_event(
            sid,
            {
                "type": "completion",
                "payload": {"done": True, "done_reason": "completed"},
            },
        )
        resp = client.get(f"/api/sessions/{sid}/events", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        # Existing keys preserved.
        assert body["session_id"] == sid
        assert "events" in body
        assert body["running"] is False
        # New authoritative fields.
        assert body["status"] == "completed"
        assert body["done_reason"] == "completed"
        assert body["title"] == "My Session"

    def test_events_title_falls_back_when_unset(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.session_store.append_event(sid, {"type": "user", "payload": {"text": "hello"}})
        resp = client.get(f"/api/sessions/{sid}/events", headers=auth_headers)
        body = resp.get_json()
        # status idle for a session with no completion event.
        assert body["status"] == "idle"
        assert body["done_reason"] is None
        assert body["title"]  # non-empty fallback


# ---------------------------------------------------------------------------
# 3. /agents token rollup includes root tokens
# ---------------------------------------------------------------------------


class TestAgentsRootTokens:
    def test_root_only_session_reports_root_tokens(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        # Root-only run: an llm_call_end at depth 0, NO sub_agent stop events.
        backend.session_store.append_event(
            sid,
            {
                "type": "llm_call_end",
                "payload": {"depth": 0, "input_tokens": 1234, "output_tokens": 567},
            },
        )
        resp = client.get(f"/api/sessions/{sid}/agents", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        # Previously this was 0 (only summed sub_agent stop events).
        assert body["total_input_tokens"] == 1234
        assert body["total_output_tokens"] == 567

    def test_root_plus_sub_tokens_combined(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.session_store.append_event(
            sid,
            {
                "type": "llm_call_end",
                "payload": {"depth": 0, "input_tokens": 1000, "output_tokens": 100},
            },
        )
        backend.session_store.append_event(
            sid,
            {
                "type": "llm_call_end",
                "payload": {
                    "depth": 1,
                    "agent_id": "child-1",
                    "input_tokens": 500,
                    "output_tokens": 50,
                },
            },
        )
        resp = client.get(f"/api/sessions/{sid}/agents", headers=auth_headers)
        body = resp.get_json()
        assert body["total_input_tokens"] == 1500
        assert body["total_output_tokens"] == 150


# ---------------------------------------------------------------------------
# 4. Idle semantics — interrupt no-op, message re-engage
# ---------------------------------------------------------------------------


class TestIdleInterrupt:
    def test_interrupt_idle_returns_200_noop(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        monkeypatch.setattr(backend.runtime, "interrupt_step", lambda sid: False)
        sid = backend.session_store.create_session()
        resp = client.post(f"/api/sessions/{sid}/interrupt", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["interrupted"] is False

    def test_interrupt_running_still_202(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        monkeypatch.setattr(backend.runtime, "interrupt_step", lambda sid: True)
        sid = backend.session_store.create_session()
        resp = client.post(f"/api/sessions/{sid}/interrupt", headers=auth_headers)
        assert resp.status_code == 202
        assert resp.get_json()["interrupted"] is True


class TestIdleMessageReengage:
    def test_message_running_enqueues(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        monkeypatch.setattr(backend.runtime, "enqueue_message", lambda sid, text: True)
        sid = backend.session_store.create_session()
        resp = client.post(
            f"/api/sessions/{sid}/message",
            headers=auth_headers,
            json={"text": "steer me"},
        )
        assert resp.status_code == 202
        assert resp.get_json()["enqueued"] is True

    def test_message_idle_reengages_with_new_run(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        # No active run → enqueue fails → re-engage via start_async.
        monkeypatch.setattr(backend.runtime, "enqueue_message", lambda sid, text: False)
        monkeypatch.setattr(backend.runtime, "is_running", lambda sid: False)
        monkeypatch.setattr(backend.runtime, "append_context_event", lambda sid, p: None)
        started = {}

        def fake_start_async(**kw):
            started.update(kw)
            return f"{kw['session_id']}:r1"

        monkeypatch.setattr(backend.runtime, "start_async", fake_start_async)
        sid = backend.session_store.create_session()
        resp = client.post(
            f"/api/sessions/{sid}/message",
            headers=auth_headers,
            json={"text": "continue please"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["enqueued"] is True
        assert body["run_id"] == f"{sid}:r1"
        # The message text was forwarded as the new run's query.
        assert started["user_query"] == "continue please"

    def test_message_missing_text_still_400(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.post(f"/api/sessions/{sid}/message", headers=auth_headers, json={})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 5. /api/projects repo identity enrichment
# ---------------------------------------------------------------------------


class TestProjectsRepoIdentity:
    def test_managed_project_gets_repo_and_aliases(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        proj = backend.project_store.create_project(
            name="Assistant", description="", path=str(tmp_path)
        )

        def fake_aliases_for_path(path):
            if path == proj.path:
                return [
                    "github.com/bearlike/Assistant",
                    "bearlike/Assistant",
                    "Assistant",
                ]
            return []

        def fake_for_path(path):
            if path == proj.path:
                return [RepoIdentity(host="github.com", owner="bearlike", repo="Assistant")]
            return []

        monkeypatch.setattr(RepoIdentity, "aliases_for_path", staticmethod(fake_aliases_for_path))
        monkeypatch.setattr(RepoIdentity, "for_path", staticmethod(fake_for_path))

        resp = client.get("/api/projects", headers=auth_headers)
        assert resp.status_code == 200
        projects = resp.get_json()["projects"]
        managed = [p for p in projects if p.get("project_id") == proj.project_id]
        assert managed, "managed project should be present"
        entry = managed[0]
        assert entry["repo"] == {
            "host": "github.com",
            "owner": "bearlike",
            "name": "Assistant",
        }
        assert "github.com/bearlike/Assistant" in entry["aliases"]
        assert "Assistant" in entry["aliases"]

    def test_project_without_remotes_omits_repo(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        backend.project_store.create_project(name="NoGit", description="", path=str(tmp_path))
        monkeypatch.setattr(RepoIdentity, "aliases_for_path", staticmethod(lambda path: []))
        monkeypatch.setattr(RepoIdentity, "for_path", staticmethod(lambda path: []))
        resp = client.get("/api/projects", headers=auth_headers)
        projects = resp.get_json()["projects"]
        nogit = [p for p in projects if p.get("name") == "NoGit"]
        assert nogit
        # No repo identity → keys absent (not present-but-null).
        assert "repo" not in nogit[0]
        assert "aliases" not in nogit[0]


# ---------------------------------------------------------------------------
# 6. _resolve_repo_or_404 matches by alias
# ---------------------------------------------------------------------------


class TestResolveByAlias:
    def test_resolve_by_canonical_alias(self, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        proj = backend.project_store.create_project(
            name="my-assistant", description="", path=str(tmp_path)
        )
        monkeypatch.setattr(
            RepoIdentity,
            "for_path",
            staticmethod(
                lambda path: [RepoIdentity(host="github.com", owner="bearlike", repo="Assistant")]
                if path == proj.path
                else []
            ),
        )
        # The incoming key matches the canonical git identity, NOT the
        # project name — must still resolve.
        target, err = backend._resolve_repo_or_404("github.com/bearlike/Assistant")
        assert err is None
        assert target is not None
        assert target.project_id == proj.project_id

    def test_resolve_by_bare_name_alias(self, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        proj = backend.project_store.create_project(
            name="my-assistant", description="", path=str(tmp_path)
        )
        monkeypatch.setattr(
            RepoIdentity,
            "for_path",
            staticmethod(
                lambda path: [RepoIdentity(host="github.com", owner="bearlike", repo="Assistant")]
                if path == proj.path
                else []
            ),
        )
        target, err = backend._resolve_repo_or_404("Assistant")
        assert err is None
        assert target is not None
        assert target.project_id == proj.project_id

    def test_ambiguous_bare_name_raises_candidates_error(self, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        p1 = backend.project_store.create_project(
            name="proj-a", description="", path=str(tmp_path / "a")
        )
        p2 = backend.project_store.create_project(
            name="proj-b", description="", path=str(tmp_path / "b")
        )

        def fake_for_path(path):
            if path == p1.path:
                return [RepoIdentity(host="github.com", owner="bearlike", repo="Assistant")]
            if path == p2.path:
                return [RepoIdentity(host="git.hurricane.home", owner="kk", repo="Assistant")]
            return []

        monkeypatch.setattr(RepoIdentity, "for_path", staticmethod(fake_for_path))
        target, err = backend._resolve_repo_or_404("Assistant")
        assert target is None
        assert err is not None
        body, status = err
        assert status == 409
        assert "candidates" in str(body).lower() or "ambiguous" in str(body).lower()

    def test_config_name_still_resolves(self, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        # A managed project resolves by its own UUID/name before any alias path.
        proj = backend.project_store.create_project(
            name="exact-name", description="", path=str(tmp_path)
        )
        monkeypatch.setattr(RepoIdentity, "for_path", staticmethod(lambda path: []))
        target, err = backend._resolve_repo_or_404(proj.project_id)
        assert err is None
        assert target.project_id == proj.project_id
