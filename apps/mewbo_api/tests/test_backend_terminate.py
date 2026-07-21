"""HTTP contract tests for permanent session termination (WP2).

Covers the terminate endpoint (200 shape, idempotency, 404) and the 410 Gone
guard on every mutating entry point, plus proof that reads keep working after a
session is terminated (terminated is not deleted). Stubs only the run I/O
boundary; the real Flask routes + serialisation run intact.
"""

# mypy: ignore-errors

from mewbo_api import backend
from mewbo_api.responses import ApiResponseKit
from mewbo_core.session_store import SessionStore

# The canonical terminated 410 body now lives on the response kit (the ONE home
# both backend.py and triggers/routes.py import with no cycle — errors.py was
# folded in).
TERMINATED_ENVELOPE = ApiResponseKit.TERMINATED_ERROR_BODY


def _reset_backend(tmp_path):
    """Swap module-level stores to temp-dir-backed stores (isolated per test)."""
    backend.session_store = SessionStore(root_dir=str(tmp_path))
    backend.runtime = backend.SessionRuntime(session_store=backend.session_store)
    backend.notification_store = backend.NotificationStore(root_dir=str(tmp_path))
    backend.share_store = backend.ShareStore(root_dir=str(tmp_path))
    backend.notification_service = backend.NotificationService(
        backend.notification_store,
        backend.runtime.session_store,
    )


def _seed_session(*, terminated: bool = False) -> str:
    """Create a session with one completed turn; optionally terminate it."""
    session_id = backend.runtime.resolve_session()
    backend.session_store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})
    backend.session_store.append_event(
        session_id,
        {"type": "completion", "payload": {"done": True, "done_reason": "completed"}},
    )
    if terminated:
        backend.runtime.terminate_session(session_id)
    return session_id


# ---------------------------------------------------------------------------
# Terminate endpoint
# ---------------------------------------------------------------------------


class TestTerminateEndpoint:
    def test_terminate_returns_shape(self, client, auth_headers, tmp_path):
        _reset_backend(tmp_path)
        sid = _seed_session()
        resp = client.post(f"/api/sessions/{sid}/terminate", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["session_id"] == sid
        assert body["status"] == "terminated"
        assert body["terminated_at"]
        assert body["cancelled_triggers"] == 0
        assert backend.runtime.is_terminated(sid) is True

    def test_terminate_is_idempotent(self, client, auth_headers, tmp_path):
        _reset_backend(tmp_path)
        sid = _seed_session()
        first = client.post(f"/api/sessions/{sid}/terminate", headers=auth_headers).get_json()
        second_resp = client.post(f"/api/sessions/{sid}/terminate", headers=auth_headers)
        assert second_resp.status_code == 200
        second = second_resp.get_json()
        assert second["terminated_at"] == first["terminated_at"]
        assert second["cancelled_triggers"] == 0
        markers = [
            e
            for e in backend.session_store.load_transcript(sid)
            if e.get("type") == "session_terminated"
        ]
        assert len(markers) == 1

    def test_terminate_unknown_session_404(self, client, auth_headers, tmp_path):
        _reset_backend(tmp_path)
        resp = client.post("/api/sessions/does-not-exist/terminate", headers=auth_headers)
        assert resp.status_code == 404
        assert resp.get_json()["error"]["code"] == 404

    def test_terminate_requires_auth(self, client, tmp_path):
        _reset_backend(tmp_path)
        sid = _seed_session()
        assert client.post(f"/api/sessions/{sid}/terminate").status_code == 401


# ---------------------------------------------------------------------------
# 410 guard on every mutating entry point
# ---------------------------------------------------------------------------


class TestTerminatedGuards:
    def _assert_410(self, resp):
        assert resp.status_code == 410
        assert resp.get_json()["error"] == TERMINATED_ENVELOPE

    def test_query_rejected(self, client, auth_headers, tmp_path):
        _reset_backend(tmp_path)
        sid = _seed_session(terminated=True)
        self._assert_410(
            client.post(f"/api/sessions/{sid}/query", headers=auth_headers, json={"query": "go"})
        )

    def test_message_rejected(self, client, auth_headers, tmp_path):
        _reset_backend(tmp_path)
        sid = _seed_session(terminated=True)
        self._assert_410(
            client.post(f"/api/sessions/{sid}/message", headers=auth_headers, json={"text": "go"})
        )

    def test_interrupt_rejected(self, client, auth_headers, tmp_path):
        _reset_backend(tmp_path)
        sid = _seed_session(terminated=True)
        self._assert_410(client.post(f"/api/sessions/{sid}/interrupt", headers=auth_headers))

    def test_recover_rejected(self, client, auth_headers, tmp_path):
        _reset_backend(tmp_path)
        sid = _seed_session(terminated=True)
        self._assert_410(
            client.post(
                f"/api/sessions/{sid}/recover", headers=auth_headers, json={"action": "continue"}
            )
        )

    def test_fork_rejected(self, client, auth_headers, tmp_path):
        _reset_backend(tmp_path)
        sid = _seed_session(terminated=True)
        self._assert_410(client.post(f"/api/sessions/{sid}/fork", headers=auth_headers, json={}))

    def test_plan_approve_rejected(self, client, auth_headers, tmp_path):
        _reset_backend(tmp_path)
        sid = _seed_session(terminated=True)
        self._assert_410(
            client.post(
                f"/api/sessions/{sid}/plan/approve",
                headers=auth_headers,
                json={"approved": True},
            )
        )

    def test_command_fork_rejected_creates_no_session(self, client, auth_headers, tmp_path):
        """The /command route closes the fork-resurrection hole.

        ``fork`` dispatches into ``mewbo_core.commands`` which copies the
        transcript store-directly, bypassing the ``resolve_session`` kill switch —
        so the API-layer terminated guard is the ONLY thing stopping an agent
        from laundering a terminated session into a fresh one. It must 410 AND
        leave the session set unchanged.
        """
        _reset_backend(tmp_path)
        sid = _seed_session(terminated=True)
        before = set(backend.session_store.list_sessions())
        self._assert_410(
            client.post(
                f"/api/sessions/{sid}/command",
                headers=auth_headers,
                json={"name": "fork", "args": []},
            )
        )
        assert set(backend.session_store.list_sessions()) == before

    def test_command_compact_rejected(self, client, auth_headers, tmp_path):
        """``compact`` would mutate a frozen transcript — the guard 410s it too."""
        _reset_backend(tmp_path)
        sid = _seed_session(terminated=True)
        self._assert_410(
            client.post(
                f"/api/sessions/{sid}/command",
                headers=auth_headers,
                json={"name": "compact", "args": []},
            )
        )

    def test_device_tool_result_rejected(self, client, auth_headers, tmp_path):
        """Delivering a device-tool result advances a run — rejected on 410."""
        _reset_backend(tmp_path)
        sid = _seed_session(terminated=True)
        self._assert_410(
            client.post(
                f"/api/sessions/{sid}/device_tools/some-call/result",
                headers=auth_headers,
                json={"call_token": "tok", "status": "ok", "result": {}},
            )
        )

    def test_sync_query_rejected_for_terminated_target(self, client, auth_headers, tmp_path):
        _reset_backend(tmp_path)
        sid = _seed_session(terminated=True)
        self._assert_410(
            client.post(
                "/api/query", headers=auth_headers, json={"query": "go", "session_id": sid}
            )
        )

    def test_sync_query_fork_from_terminated_source_rejected(
        self, client, auth_headers, tmp_path
    ):
        """Fork-resurrection is blocked: the kill switch covers `fork_from` too.

        Copying a terminated transcript into a fresh session would let an agent
        launder its way around termination — the core seam
        (``resolve_session`` raising ``SessionTerminatedError``) rejects the
        SOURCE before any fork happens, and no new session may be created.
        """
        _reset_backend(tmp_path)
        sid = _seed_session(terminated=True)
        before = set(backend.session_store.list_sessions())
        self._assert_410(
            client.post(
                "/api/query", headers=auth_headers, json={"query": "go", "fork_from": sid}
            )
        )
        assert set(backend.session_store.list_sessions()) == before

    def test_status_slash_command_still_works(self, client, auth_headers, tmp_path):
        """/status is a read routed through /query — a terminated session still answers."""
        _reset_backend(tmp_path)
        sid = _seed_session(terminated=True)
        resp = client.post(
            f"/api/sessions/{sid}/query", headers=auth_headers, json={"query": "/status"}
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "terminated"


# ---------------------------------------------------------------------------
# Reads keep working after termination (terminated != deleted)
# ---------------------------------------------------------------------------


class TestTerminatedReadsStillWork:
    def test_events_readable(self, client, auth_headers, tmp_path):
        _reset_backend(tmp_path)
        sid = _seed_session(terminated=True)
        resp = client.get(f"/api/sessions/{sid}/events", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "terminated"
        assert body["recoverable"] is False
        # The events route forwards the permanent-termination signal so a
        # downstream projection (MCP) can surface it without re-reading the
        # transcript for the terminal marker.
        assert body["terminated"] is True
        assert body["terminated_at"]
        # The transcript (incl. the terminal marker) is still served.
        assert any(e.get("type") == "session_terminated" for e in body["events"])

    def test_list_surfaces_terminated_flag(self, client, auth_headers, tmp_path):
        _reset_backend(tmp_path)
        sid = _seed_session(terminated=True)
        resp = client.get("/api/sessions", headers=auth_headers)
        assert resp.status_code == 200
        rows = resp.get_json()["sessions"]
        row = next((r for r in rows if r["session_id"] == sid), None)
        assert row is not None
        assert row["terminated"] is True
        assert row["status"] == "terminated"
