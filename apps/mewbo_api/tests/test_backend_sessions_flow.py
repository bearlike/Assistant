"""Integration tests for session-lifecycle HTTP endpoints in backend.py.

Covers: create/list sessions (with tag, capability header, project context),
query (run, 409 already-running, /status, /terminate slash commands),
events?after=, message (steering + 404 when idle), interrupt (+ 404),
agents tree, archive/unarchive (+ 404), fork_from / fork endpoint,
attachments (image/text/vision-gate), share + /api/share/<token>, export,
session-level usage, recovery, plan-approve, and session-tag on /api/query.

Stubs ONLY the I/O boundary (run_sync / start_async / enqueue_message /
interrupt_step / resolve_recovery_query etc.) while keeping the real Flask
routes and serialisation logic intact.
"""

# mypy: ignore-errors

import io
import time

from mewbo_api import backend
from mewbo_core.session_store import SessionStore

# ---------------------------------------------------------------------------
# Helpers shared across this module
# ---------------------------------------------------------------------------


class DummyQueue:
    """Minimal TaskQueue stub for sync /api/query responses."""

    def __init__(self, result: str = "ok") -> None:
        self.task_result = result
        self.plan_steps = [{"title": "T", "description": "D"}]
        self.action_steps = [
            {"tool_id": "shell", "operation": "run", "tool_input": "ls", "result": result}
        ]

    def dict(self):
        return {
            "task_result": self.task_result,
            "plan_steps": list(self.plan_steps),
            "action_steps": list(self.action_steps),
        }


def _reset_backend(tmp_path, monkeypatch):
    """Swap module-level stores to temp-dir-backed in-memory stores."""
    backend.session_store = SessionStore(root_dir=str(tmp_path))
    backend.runtime = backend.SessionRuntime(session_store=backend.session_store)
    backend.notification_store = backend.NotificationStore(root_dir=str(tmp_path))
    backend.share_store = backend.ShareStore(root_dir=str(tmp_path))
    backend.notification_service = backend.NotificationService(
        backend.notification_store,
        backend.runtime.session_store,
    )


def _fake_run_sync(*, session_id: str, user_query: str, should_cancel=None, **_kwargs):
    """Write events as the real runtime would so the event-polling assertions work."""
    backend.session_store.append_event(
        session_id, {"type": "user", "payload": {"text": user_query}}
    )
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


# ---------------------------------------------------------------------------
# Session create
# ---------------------------------------------------------------------------


class TestSessionCreate:
    def test_create_returns_session_id(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.post("/api/sessions", headers=auth_headers, json={})
        assert resp.status_code == 200
        body = resp.get_json()
        assert "session_id" in body
        assert body["session_id"]

    def test_create_requires_auth(self, client, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.post("/api/sessions", json={})
        assert resp.status_code == 401

    def test_create_with_context(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.post(
            "/api/sessions",
            headers=auth_headers,
            json={"context": {"repo": "acme/web", "branch": "main"}},
        )
        assert resp.status_code == 200
        sid = resp.get_json()["session_id"]
        events = backend.session_store.load_transcript(sid)
        ctx = next((e for e in events if e.get("type") == "context"), None)
        assert ctx is not None
        assert ctx["payload"].get("repo") == "acme/web"

    def test_create_with_session_tag(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.post(
            "/api/sessions",
            headers=auth_headers,
            json={"session_tag": "my-tag"},
        )
        assert resp.status_code == 200
        sid = resp.get_json()["session_id"]
        assert backend.session_store.resolve_tag("my-tag") == sid

    def test_create_with_capabilities_header(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        headers = {**auth_headers, "X-Mewbo-Capabilities": "wiki,stlite"}
        resp = client.post("/api/sessions", headers=headers, json={})
        assert resp.status_code == 200
        sid = resp.get_json()["session_id"]
        events = backend.session_store.load_transcript(sid)
        ctx = next((e for e in events if e.get("type") == "context"), None)
        assert ctx is not None
        caps = ctx["payload"].get("client_capabilities")
        assert "wiki" in caps
        assert "stlite" in caps

    def test_create_emits_notification(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.post("/api/sessions", headers=auth_headers, json={})
        assert resp.status_code == 200
        notes = backend.notification_store.list(include_dismissed=False)
        assert any(n.get("event_type") == "created" for n in notes)


# ---------------------------------------------------------------------------
# Session list
# ---------------------------------------------------------------------------


class TestSessionList:
    def test_list_requires_auth(self, client, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.get("/api/sessions")
        assert resp.status_code == 401

    def test_list_returns_sessions_with_events(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.session_store.append_event(sid, {"type": "user", "payload": {"text": "hi"}})
        resp = client.get("/api/sessions", headers=auth_headers)
        assert resp.status_code == 200
        sessions = resp.get_json()["sessions"]
        assert any(s["session_id"] == sid for s in sessions)

    def test_list_skips_sessions_with_no_events(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        empty_sid = backend.session_store.create_session()
        resp = client.get("/api/sessions", headers=auth_headers)
        sessions = resp.get_json()["sessions"]
        assert all(s["session_id"] != empty_sid for s in sessions)

    def test_list_surfaces_recoverable_flag(self, client, auth_headers, tmp_path, monkeypatch):
        """F2: a session that died without a completion event is recoverable=True
        on the /api/sessions wire; a completed one is False.
        """
        _reset_backend(tmp_path, monkeypatch)
        dead = backend.session_store.create_session()
        backend.session_store.append_event(dead, {"type": "user", "payload": {"text": "q"}})
        # No completion event — process died mid-call.
        done = backend.session_store.create_session()
        backend.session_store.append_event(done, {"type": "user", "payload": {"text": "q"}})
        backend.session_store.append_event(
            done,
            {"type": "completion", "payload": {"done": True, "done_reason": "completed"}},
        )
        resp = client.get("/api/sessions", headers=auth_headers)
        sessions = {s["session_id"]: s for s in resp.get_json()["sessions"]}
        assert sessions[dead]["recoverable"] is True
        assert sessions[done]["recoverable"] is False

    def test_events_surfaces_recoverable_flag(self, client, auth_headers, tmp_path, monkeypatch):
        """F2: the per-session /events poll carries ``recoverable``."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.session_store.append_event(sid, {"type": "user", "payload": {"text": "q"}})
        backend.session_store.append_event(
            sid, {"type": "completion", "payload": {"done": True, "done_reason": "error"}}
        )
        resp = client.get(f"/api/sessions/{sid}/events", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["recoverable"] is True

    def test_list_hides_archived_by_default(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.session_store.append_event(sid, {"type": "user", "payload": {"text": "hi"}})
        backend.session_store.archive_session(sid)
        resp = client.get("/api/sessions", headers=auth_headers)
        sessions = resp.get_json()["sessions"]
        assert all(s["session_id"] != sid for s in sessions)

    def test_list_includes_archived_when_requested(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.session_store.append_event(sid, {"type": "user", "payload": {"text": "hi"}})
        backend.session_store.archive_session(sid)
        resp = client.get("/api/sessions?include_archived=1", headers=auth_headers)
        sessions = resp.get_json()["sessions"]
        assert any(s["session_id"] == sid for s in sessions)


# ---------------------------------------------------------------------------
# Session query (async)
# ---------------------------------------------------------------------------


class TestSessionQuery:
    def test_query_requires_auth(self, client, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.post(f"/api/sessions/{sid}/query", json={"query": "hi"})
        assert resp.status_code == 401

    def test_query_missing_query_field(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.post(f"/api/sessions/{sid}/query", headers=auth_headers, json={})
        assert resp.status_code == 400

    def test_query_enqueues_run(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        monkeypatch.setattr(backend.runtime, "run_sync", _fake_run_sync)
        sid = backend.session_store.create_session()
        resp = client.post(
            f"/api/sessions/{sid}/query",
            headers=auth_headers,
            json={"query": "hello"},
        )
        assert resp.status_code == 202
        body = resp.get_json()
        assert body["session_id"] == sid
        assert body["accepted"] is True

    def test_query_409_when_already_running(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        monkeypatch.setattr(backend.runtime, "is_running", lambda sid: True)
        monkeypatch.setattr(backend.runtime, "start_async", lambda **kw: True)
        sid = backend.session_store.create_session()
        resp = client.post(
            f"/api/sessions/{sid}/query",
            headers=auth_headers,
            json={"query": "hello"},
        )
        assert resp.status_code == 409

    def test_slash_terminate_cancels_run(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        canceled_ids = []
        monkeypatch.setattr(backend.runtime, "cancel", lambda sid: canceled_ids.append(sid) or True)
        sid = backend.session_store.create_session()
        resp = client.post(
            f"/api/sessions/{sid}/query",
            headers=auth_headers,
            json={"query": "/terminate"},
        )
        assert resp.status_code == 202
        body = resp.get_json()
        assert body["canceled"] is True
        assert sid in canceled_ids

    def test_slash_status_returns_summary(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        fake_summary = {"status": "idle", "events": 2}
        monkeypatch.setattr(backend.runtime, "summarize_session", lambda sid: fake_summary)
        sid = backend.session_store.create_session()
        resp = client.post(
            f"/api/sessions/{sid}/query",
            headers=auth_headers,
            json={"query": "/status"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["session_id"] == sid
        assert body["status"] == "idle"

    def test_query_with_capabilities_header(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        captured = {}
        monkeypatch.setattr(
            backend.runtime,
            "start_async",
            lambda **kw: captured.update(kw) or True,
        )
        monkeypatch.setattr(backend.runtime, "append_context_event", lambda sid, p: None)
        sid = backend.session_store.create_session()
        headers = {**auth_headers, "X-Mewbo-Capabilities": "wiki"}
        resp = client.post(
            f"/api/sessions/{sid}/query",
            headers=headers,
            json={"query": "hello"},
        )
        assert resp.status_code == 202

    def test_query_invalid_project_returns_400(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        monkeypatch.setattr(backend.runtime, "start_async", lambda **kw: True)
        sid = backend.session_store.create_session()
        # "managed:nonexistent-uuid" will fail lookup in project_store
        resp = client.post(
            f"/api/sessions/{sid}/query",
            headers=auth_headers,
            json={"query": "hello", "project": "managed:nonexistent-uuid-xyz"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Session events polling
# ---------------------------------------------------------------------------


class TestSessionEvents:
    def test_events_requires_auth(self, client, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.get(f"/api/sessions/{sid}/events")
        assert resp.status_code == 401

    def test_events_returns_transcript(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        monkeypatch.setattr(backend.runtime, "run_sync", _fake_run_sync)
        sid = backend.session_store.create_session()
        client.post(
            f"/api/sessions/{sid}/query",
            headers=auth_headers,
            json={"query": "hi"},
        )
        _wait_for_run(sid)
        resp = client.get(f"/api/sessions/{sid}/events", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["session_id"] == sid
        assert body["running"] is False
        assert body["events"]

    def test_events_after_filter(self, client, auth_headers, tmp_path, monkeypatch):
        """?after= param is accepted and the route returns 200 with events + running flag."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.session_store.append_event(sid, {"type": "user", "payload": {"text": "first"}})
        backend.session_store.append_event(
            sid, {"type": "assistant", "payload": {"text": "second"}}
        )
        # Base request — get all events
        resp = client.get(f"/api/sessions/{sid}/events", headers=auth_headers)
        all_events = resp.get_json()["events"]
        assert len(all_events) >= 2

        # Use a far-future timestamp so the filter returns no events.
        # Note: the + in UTC timezone must be URL-encoded as %2B for the
        # query string to parse correctly — raw + becomes a space and
        # fromisoformat fails, returning all events as the safe fallback.
        far_future = "9999-12-31T23:59:59%2B00:00"
        resp2 = client.get(f"/api/sessions/{sid}/events?after={far_future}", headers=auth_headers)
        assert resp2.status_code == 200
        body2 = resp2.get_json()
        assert body2["session_id"] == sid
        # Far-future cutoff: no events after 9999-12-31 — expect empty list
        assert len(body2["events"]) == 0

    def test_events_unknown_session_404(self, client, auth_headers, tmp_path, monkeypatch):
        """#64: an unknown id must 404 — never synthesize a phantom idle 200."""
        _reset_backend(tmp_path, monkeypatch)
        resp = client.get("/api/sessions/does-not-exist/events", headers=auth_headers)
        assert resp.status_code == 404
        body = resp.get_json()
        assert body["error"]["code"] == 404
        # The phantom-idle placeholder must NOT leak through the 404.
        assert "title" not in body
        assert "status" not in body

    def test_events_known_empty_session_returns_200(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """#64: the guard distinguishes 'exists-but-empty' from 'unknown' —
        a real session with no events still polls 200 (with the placeholder)."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()  # exists, zero events
        resp = client.get(f"/api/sessions/{sid}/events", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["session_id"] == sid
        assert body["events"] == []
        # The titleless-but-real placeholder is still correct here.
        assert "status" in body

    def test_events_truncate_is_opt_in_default_preserves_full_result(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """#42: default GET returns the FULL result (console renders it) — no cap,
        no _truncated flag; ?truncate=1 caps free-text fields and flags them, but
        leaves the already-capped ``summary`` untouched."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        big = "x" * 50_000
        big_cmd = "c" * 50_000
        backend.session_store.append_event(
            sid,
            {
                "type": "tool_result",
                "payload": {
                    "tool_id": "shell",
                    "result": big,
                    "tool_input": {"cmd": big_cmd},
                    "summary": "short-summary",
                },
            },
        )

        # Default — full result preserved, no truncation flags.
        resp = client.get(f"/api/sessions/{sid}/events", headers=auth_headers)
        assert resp.status_code == 200
        ev = next(e for e in resp.get_json()["events"] if e["type"] == "tool_result")
        assert len(ev["payload"]["result"]) == 50_000
        assert "result_truncated" not in ev["payload"]
        assert "tool_input_truncated" not in ev["payload"]

        # ?truncate=1 — result capped at 2000 + flagged; tool_input (dict) flagged;
        # the upstream-capped ``summary`` is left alone.
        resp2 = client.get(f"/api/sessions/{sid}/events?truncate=1", headers=auth_headers)
        assert resp2.status_code == 200
        ev2 = next(e for e in resp2.get_json()["events"] if e["type"] == "tool_result")
        assert len(ev2["payload"]["result"]) == 2000
        assert ev2["payload"]["result_truncated"] is True
        assert ev2["payload"]["tool_input_truncated"] is True
        assert ev2["payload"]["summary"] == "short-summary"


# ---------------------------------------------------------------------------
# Session message (steering) and interrupt
# ---------------------------------------------------------------------------


class TestSessionMessage:
    def test_message_requires_auth(self, client, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.post(f"/api/sessions/{sid}/message", json={"text": "hello"})
        assert resp.status_code == 401

    def test_message_missing_text_returns_400(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.post(f"/api/sessions/{sid}/message", headers=auth_headers, json={})
        assert resp.status_code == 400

    def test_message_enqueues_when_running(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        enqueued = []
        monkeypatch.setattr(
            backend.runtime,
            "enqueue_message",
            lambda sid, text: enqueued.append((sid, text)) or True,
        )
        sid = backend.session_store.create_session()
        resp = client.post(
            f"/api/sessions/{sid}/message",
            headers=auth_headers,
            json={"text": "steer me"},
        )
        assert resp.status_code == 202
        assert resp.get_json()["enqueued"] is True
        assert enqueued[0][1] == "steer me"

    def test_message_idle_reengages_with_new_run(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """Idle session: a message starts a fresh run instead of 404 (#44.5)."""
        _reset_backend(tmp_path, monkeypatch)
        monkeypatch.setattr(backend.runtime, "enqueue_message", lambda sid, text: False)
        monkeypatch.setattr(backend.runtime, "is_running", lambda sid: False)
        monkeypatch.setattr(backend.runtime, "append_context_event", lambda sid, p: None)
        monkeypatch.setattr(
            backend.runtime, "start_async", lambda **kw: f"{kw['session_id']}:r1"
        )
        sid = backend.session_store.create_session()
        resp = client.post(
            f"/api/sessions/{sid}/message",
            headers=auth_headers,
            json={"text": "steer me"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["enqueued"] is True
        assert body["run_id"] == f"{sid}:r1"


class TestSessionInterrupt:
    def test_interrupt_requires_auth(self, client, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.post(f"/api/sessions/{sid}/interrupt")
        assert resp.status_code == 401

    def test_interrupt_ok_when_running(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        interrupted = []
        monkeypatch.setattr(
            backend.runtime,
            "interrupt_step",
            lambda sid: interrupted.append(sid) or True,
        )
        sid = backend.session_store.create_session()
        resp = client.post(f"/api/sessions/{sid}/interrupt", headers=auth_headers)
        assert resp.status_code == 202
        assert resp.get_json()["interrupted"] is True
        assert sid in interrupted

    def test_interrupt_idle_is_graceful_noop(self, client, auth_headers, tmp_path, monkeypatch):
        """Idle session: interrupt is an idempotent no-op (200), not a 404 (#44.5)."""
        _reset_backend(tmp_path, monkeypatch)
        monkeypatch.setattr(backend.runtime, "interrupt_step", lambda sid: False)
        sid = backend.session_store.create_session()
        resp = client.post(f"/api/sessions/{sid}/interrupt", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["interrupted"] is False


# ---------------------------------------------------------------------------
# Session agents tree
# ---------------------------------------------------------------------------


class TestSessionAgents:
    def test_agents_requires_auth(self, client, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.get(f"/api/sessions/{sid}/agents")
        assert resp.status_code == 401

    def test_agents_returns_tree_structure(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        # Inject a sub_agent event to simulate an agent spawn
        backend.session_store.append_event(
            sid,
            {
                "type": "sub_agent",
                "payload": {
                    "agent_id": "child-1",
                    "parent_id": "root",
                    "depth": 1,
                    "action": "start",
                    "status": "running",
                    "steps_completed": 0,
                    "input_tokens": 100,
                    "output_tokens": 50,
                },
            },
        )
        backend.session_store.append_event(
            sid,
            {"type": "tool_result", "payload": {"tool_id": "shell", "result": "ok"}},
        )
        resp = client.get(f"/api/sessions/{sid}/agents", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert "agents" in body
        assert "total_steps" in body
        assert body["total_steps"] == 1
        assert len(body["agents"]) == 1
        assert body["agents"][0]["agent_id"] == "child-1"

    def test_agents_token_totals_include_root(self, client, auth_headers, tmp_path, monkeypatch):
        """Token rollup delegates to build_usage_numbers — root (depth==0)
        tokens are counted, so a root-only session no longer reports 0 (#44.3)."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.session_store.append_event(
            sid,
            {
                "type": "llm_call_end",
                "payload": {"depth": 0, "input_tokens": 200, "output_tokens": 100},
            },
        )
        resp = client.get(f"/api/sessions/{sid}/agents", headers=auth_headers)
        body = resp.get_json()
        assert body["total_input_tokens"] == 200
        assert body["total_output_tokens"] == 100

    def test_agents_total_input_tokens_is_peak_not_billed(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """total_input_tokens reports PEAK context pressure, not the cumulative billed sum.

        Within a session the prompt GROWS as tool results accumulate, so summing
        input_tokens across calls double-counts the baseline (the billed sum is
        ~2× the real peak for a long session).  The /agents endpoint must match
        the peak figure the history overview + console badge show — not the billed
        sum that is only correct for cost accounting.

        Fixture mirrors the docstring example in build_usage_numbers:
          call 1: 13 000 input tokens  →  billed = 13 000, peak = 13 000
          call 2: 27 000 input tokens  →  billed = 40 000, peak = 27 000
        """
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        for input_tokens in (13_000, 27_000):
            backend.session_store.append_event(
                sid,
                {
                    "type": "llm_call_end",
                    "payload": {"depth": 0, "input_tokens": input_tokens, "output_tokens": 50},
                },
            )
        resp = client.get(f"/api/sessions/{sid}/agents", headers=auth_headers)
        body = resp.get_json()
        # Peak — the figure that reflects real context pressure.
        assert body["total_input_tokens"] == 27_000
        # Billed sum — exposed separately for cost accounting; must NOT equal peak.
        assert body["total_input_tokens_billed"] == 40_000
        assert body["total_input_tokens"] != body["total_input_tokens_billed"]


# ---------------------------------------------------------------------------
# Archive / unarchive
# ---------------------------------------------------------------------------


class TestSessionArchive:
    def test_archive_requires_auth(self, client, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.post(f"/api/sessions/{sid}/archive")
        assert resp.status_code == 401

    def test_archive_session(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.post(f"/api/sessions/{sid}/archive", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["archived"] is True

    def test_archive_unknown_session_404(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.post("/api/sessions/no-such-id/archive", headers=auth_headers)
        assert resp.status_code == 404

    def test_unarchive_session(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.session_store.archive_session(sid)
        resp = client.delete(f"/api/sessions/{sid}/archive", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["archived"] is False

    def test_unarchive_unknown_session_404(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.delete("/api/sessions/no-such-id/archive", headers=auth_headers)
        assert resp.status_code == 404

    def test_archive_unarchive_requires_auth(self, client, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        assert client.delete(f"/api/sessions/{sid}/archive").status_code == 401


# ---------------------------------------------------------------------------
# Fork session endpoint
# ---------------------------------------------------------------------------


class TestSessionFork:
    def test_fork_requires_auth(self, client, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.post(f"/api/sessions/{sid}/fork", json={})
        assert resp.status_code == 401

    def test_fork_creates_new_session(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.session_store.append_event(sid, {"type": "user", "payload": {"text": "hello"}})
        resp = client.post(f"/api/sessions/{sid}/fork", headers=auth_headers, json={})
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["forked_from"] == sid
        new_sid = body["session_id"]
        assert new_sid != sid

    def test_fork_409_when_running(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        monkeypatch.setattr(backend.runtime, "is_running", lambda sid: True)
        sid = backend.session_store.create_session()
        resp = client.post(f"/api/sessions/{sid}/fork", headers=auth_headers, json={})
        assert resp.status_code == 409

    def test_fork_with_tag(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.session_store.append_event(sid, {"type": "user", "payload": {"text": "hello"}})
        resp = client.post(
            f"/api/sessions/{sid}/fork",
            headers=auth_headers,
            json={"tag": "forked-tag"},
        )
        assert resp.status_code == 201
        new_sid = resp.get_json()["session_id"]
        assert backend.session_store.resolve_tag("forked-tag") == new_sid


# ---------------------------------------------------------------------------
# Attachments upload
# ---------------------------------------------------------------------------


class TestSessionAttachments:
    def test_attachments_upload_text_file(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        data = {"file": (io.BytesIO(b"hello world"), "readme.txt")}
        resp = client.post(
            f"/api/sessions/{sid}/attachments",
            headers=auth_headers,
            data=data,
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        attachments = resp.get_json()["attachments"]
        assert len(attachments) == 1
        assert attachments[0]["filename"] == "readme.txt"

    def test_attachments_unknown_session_404(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        data = {"file": (io.BytesIO(b"data"), "f.txt")}
        resp = client.post(
            "/api/sessions/missing-sid/attachments",
            headers=auth_headers,
            data=data,
            content_type="multipart/form-data",
        )
        assert resp.status_code == 404

    def test_attachments_requires_auth(self, client, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.post(f"/api/sessions/{sid}/attachments")
        assert resp.status_code == 401

    def test_attachments_no_files_400(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.post(
            f"/api/sessions/{sid}/attachments",
            headers=auth_headers,
            data={},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400

    def test_attachments_rejects_unsupported_type(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        data = {"file": (io.BytesIO(b"binary"), "evil.exe", "application/x-msdownload")}
        resp = client.post(
            f"/api/sessions/{sid}/attachments",
            headers=auth_headers,
            data=data,
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        assert "Unsupported" in resp.get_json()["message"]

    def test_attachments_rejects_image_on_non_vision_model(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        monkeypatch.setattr(backend, "model_supports_vision", lambda _: False)
        sid = backend.session_store.create_session()
        data = {
            "model": "text-only",
            "file": (io.BytesIO(b"\x89PNG\r\n\x1a\n"), "pic.png", "image/png"),
        }
        resp = client.post(
            f"/api/sessions/{sid}/attachments",
            headers=auth_headers,
            data=data,
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        assert "vision" in resp.get_json()["message"].lower()

    def test_attachments_invalid_filename_400(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        data = {"file": (io.BytesIO(b"data"), "")}
        resp = client.post(
            f"/api/sessions/{sid}/attachments",
            headers=auth_headers,
            data=data,
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Share + export
# ---------------------------------------------------------------------------


class TestShareAndExport:
    def test_share_creates_token(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.post(f"/api/sessions/{sid}/share", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert "token" in body

    def test_share_requires_auth(self, client, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.post(f"/api/sessions/{sid}/share")
        assert resp.status_code == 401

    def test_share_unknown_session_404(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.post("/api/sessions/missing-sid/share", headers=auth_headers)
        assert resp.status_code == 404

    def test_share_token_resolves(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        share_resp = client.post(f"/api/sessions/{sid}/share", headers=auth_headers)
        token = share_resp.get_json()["token"]
        resp = client.get(f"/api/share/{token}")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["session_id"] == sid

    def test_share_missing_token_404(self, client, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.get("/api/share/does-not-exist")
        assert resp.status_code == 404

    def test_export_returns_transcript(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.session_store.append_event(sid, {"type": "user", "payload": {"text": "hi"}})
        resp = client.get(f"/api/sessions/{sid}/export", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["session_id"] == sid
        assert body["events"]

    def test_export_requires_auth(self, client, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.get(f"/api/sessions/{sid}/export")
        assert resp.status_code == 401

    def test_export_unknown_session_404(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.get("/api/sessions/missing-sid/export", headers=auth_headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Session recovery endpoint
# ---------------------------------------------------------------------------


class TestSessionRecovery:
    def test_recovery_requires_auth(self, client, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.post(f"/api/sessions/{sid}/recover", json={"action": "retry"})
        assert resp.status_code == 401

    def test_recovery_invalid_action_400(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.post(
            f"/api/sessions/{sid}/recover",
            headers=auth_headers,
            json={"action": "invalid"},
        )
        assert resp.status_code == 400

    def test_recovery_409_when_running(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        monkeypatch.setattr(backend.runtime, "is_running", lambda sid: True)
        sid = backend.session_store.create_session()
        resp = client.post(
            f"/api/sessions/{sid}/recover",
            headers=auth_headers,
            json={"action": "retry"},
        )
        assert resp.status_code == 409

    def test_recovery_retry_success(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.session_store.append_event(
            sid, {"type": "user", "payload": {"text": "original query"}}
        )

        def fake_resolve(session_id, action, from_ts=None, replacement_text=None):
            return "original query"

        monkeypatch.setattr(backend.runtime, "resolve_recovery_query", fake_resolve)
        monkeypatch.setattr(backend.runtime, "start_async", lambda **kw: f"{sid}:r2")

        resp = client.post(
            f"/api/sessions/{sid}/recover",
            headers=auth_headers,
            json={"action": "retry"},
        )
        assert resp.status_code == 202
        body = resp.get_json()
        assert body["action"] == "retry"
        assert body["accepted"] is True
        # F3: the recover response carries the run_id minted by start_async.
        assert body["run_id"] == f"{sid}:r2"

    def test_recovery_value_error_returns_400(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()

        def fail_resolve(*args, **kwargs):
            raise ValueError("No prior user message")

        monkeypatch.setattr(backend.runtime, "resolve_recovery_query", fail_resolve)

        resp = client.post(
            f"/api/sessions/{sid}/recover",
            headers=auth_headers,
            json={"action": "retry"},
        )
        assert resp.status_code == 400

    def test_recovery_runtime_error_returns_409(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()

        def fail_resolve(*args, **kwargs):
            raise RuntimeError("session conflict")

        monkeypatch.setattr(backend.runtime, "resolve_recovery_query", fail_resolve)

        resp = client.post(
            f"/api/sessions/{sid}/recover",
            headers=auth_headers,
            json={"action": "retry"},
        )
        assert resp.status_code == 409

    def test_recovery_reinjects_capability_context(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """F1: a recovered session whose context declared a capability re-emits
        it as the most-recent context event before the run starts.
        """
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.runtime.append_context_event(sid, {"client_capabilities": ["wiki"]})
        backend.session_store.append_event(
            sid, {"type": "user", "payload": {"text": "q"}}
        )

        monkeypatch.setattr(
            backend.runtime, "resolve_recovery_query", lambda *a, **k: "q"
        )
        # start_async must observe the re-injected capability as the latest context.
        observed = {}

        def fake_start_async(**kw):
            events = backend.session_store.load_transcript(sid)
            last_ctx = next(
                (e for e in reversed(events) if e.get("type") == "context"), None
            )
            observed["caps"] = last_ctx["payload"].get("client_capabilities") if last_ctx else None
            return f"{sid}:r2"

        monkeypatch.setattr(backend.runtime, "start_async", fake_start_async)
        resp = client.post(
            f"/api/sessions/{sid}/recover",
            headers=auth_headers,
            json={"action": "continue"},
        )
        assert resp.status_code == 202
        assert observed["caps"] == ["wiki"]

    def test_recovery_inherits_fallback_models(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """F5: the generic recovery path must NOT strip the auto-heal chain.

        It passes ``fallback_models`` unset (None) so the resolved config policy
        applies — never an explicit empty tuple that would disable fallback.
        """
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.session_store.append_event(
            sid, {"type": "user", "payload": {"text": "q"}}
        )
        monkeypatch.setattr(
            backend.runtime, "resolve_recovery_query", lambda *a, **k: "q"
        )
        captured = {}

        def fake_start_async(**kw):
            captured.update(kw)
            return f"{sid}:r2"

        monkeypatch.setattr(backend.runtime, "start_async", fake_start_async)
        resp = client.post(
            f"/api/sessions/{sid}/recover",
            headers=auth_headers,
            json={"action": "retry"},
        )
        assert resp.status_code == 202
        # Not stripped to () — defers to config default (None) so auto-heal stays.
        assert captured.get("fallback_models") in (None,)
        assert captured.get("fallback_models") != ()


class _FakeJob:
    def __init__(self, status: str = "interrupted", slug: str = "org/repo") -> None:
        self.status = status
        self.slug = slug


class _FakeWikiStore:
    """Minimal wiki store exposing only the dispatch lookups."""

    def __init__(self, *, job_id, job):
        self._job_id = job_id
        self._job = job

    def find_job_by_session(self, session_id):
        return self._job_id

    def get_job(self, job_id):
        return self._job if job_id == self._job_id else None


class TestSessionRecoveryWikiDispatch:
    """F4: origin-aware dispatch — a recoverable wiki INDEXING job routes to
    WikiResume; a plain user session uses the generic path.
    """

    def test_indexing_session_routes_to_wiki_resume(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        job = _FakeJob(status="interrupted")
        backend.runtime.wiki_store = _FakeWikiStore(job_id="job-1", job=job)

        import mewbo_api.wiki.resume as resume_mod

        called = {}

        class FakeWikiResume:
            @staticmethod
            def is_resumable(j):
                return j.status not in {"complete", "cancelled"}

            @classmethod
            def resume(
                cls, store, runtime, job_id, *,
                hook_manager=None, user_initiated=True, restart=False,
            ):
                called["job_id"] = job_id
                called["restart"] = restart
                return {"job_id": job_id, "session_id": sid, "status": "scanning"}

        monkeypatch.setattr(resume_mod, "WikiResume", FakeWikiResume)
        # If dispatch leaked to the generic path, this would blow up the test.
        monkeypatch.setattr(
            backend.runtime,
            "resolve_recovery_query",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("generic path used")),
        )

        resp = client.post(
            f"/api/sessions/{sid}/recover",
            headers=auth_headers,
            json={"action": "continue"},
        )
        assert resp.status_code == 202
        body = resp.get_json()
        assert called["job_id"] == "job-1"
        assert called["restart"] is False  # continue == checkpoint resume
        assert body["job_id"] == "job-1"
        assert body["status"] == "scanning"
        assert body["action"] == "continue"  # request action preserved, not hardcoded
        assert body["slug"] == "org/repo"  # lets the client deep-link the indexing screen
        assert body["accepted"] is True
        # WikiResume rebinds the deps used by _reset_backend; restore for isolation.
        backend.runtime.wiki_store = None

    def test_indexing_retry_forces_restart(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """A wiki indexing session's ``retry`` (Restart) forces a no-skip rebuild
        (``restart=True``) rather than being silently down-graded to a continue."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        job = _FakeJob(status="failed")
        backend.runtime.wiki_store = _FakeWikiStore(job_id="job-r", job=job)

        import mewbo_api.wiki.resume as resume_mod

        called = {}

        class FakeWikiResume:
            @staticmethod
            def is_resumable(j):
                return j.status not in {"complete", "cancelled"}

            @classmethod
            def resume(
                cls, store, runtime, job_id, *,
                hook_manager=None, user_initiated=True, restart=False,
            ):
                called["restart"] = restart
                return {"job_id": job_id, "session_id": sid, "status": "scanning"}

        monkeypatch.setattr(resume_mod, "WikiResume", FakeWikiResume)
        resp = client.post(
            f"/api/sessions/{sid}/recover",
            headers=auth_headers,
            json={"action": "retry"},
        )
        assert resp.status_code == 202
        assert called["restart"] is True
        assert resp.get_json()["action"] == "retry"
        backend.runtime.wiki_store = None

    def test_complete_indexing_job_uses_generic_path(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """A non-resumable (complete) job falls through to the generic path."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.session_store.append_event(
            sid, {"type": "user", "payload": {"text": "q"}}
        )
        job = _FakeJob(status="complete")
        backend.runtime.wiki_store = _FakeWikiStore(job_id="job-2", job=job)

        import mewbo_api.wiki.resume as resume_mod

        class FakeWikiResume:
            @staticmethod
            def is_resumable(j):
                return j.status not in {"complete", "cancelled"}

            @classmethod
            def resume(cls, *a, **k):  # pragma: no cover - must not run
                raise AssertionError("should not resume a complete job")

        monkeypatch.setattr(resume_mod, "WikiResume", FakeWikiResume)
        monkeypatch.setattr(
            backend.runtime, "resolve_recovery_query", lambda *a, **k: "q"
        )
        monkeypatch.setattr(backend.runtime, "start_async", lambda **kw: f"{sid}:r2")

        resp = client.post(
            f"/api/sessions/{sid}/recover",
            headers=auth_headers,
            json={"action": "continue"},
        )
        assert resp.status_code == 202
        body = resp.get_json()
        # Generic path response shape — no job_id key.
        assert "job_id" not in body
        assert body["run_id"] == f"{sid}:r2"
        backend.runtime.wiki_store = None

    def test_plain_user_session_uses_generic_path(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """No wiki_store / no indexing job ⇒ generic path (wiki Q&A re-runs here too)."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.session_store.append_event(
            sid, {"type": "user", "payload": {"text": "q"}}
        )
        # No wiki_store attribute at all (graph-less install / non-wiki session).
        monkeypatch.setattr(
            backend.runtime, "resolve_recovery_query", lambda *a, **k: "q"
        )
        monkeypatch.setattr(backend.runtime, "start_async", lambda **kw: f"{sid}:r2")

        resp = client.post(
            f"/api/sessions/{sid}/recover",
            headers=auth_headers,
            json={"action": "retry"},
        )
        assert resp.status_code == 202
        body = resp.get_json()
        assert "job_id" not in body
        assert body["run_id"] == f"{sid}:r2"


# ---------------------------------------------------------------------------
# Plan approve / reject
# ---------------------------------------------------------------------------


class TestPlanApprove:
    def test_plan_approve_requires_auth(self, client, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.post(f"/api/sessions/{sid}/plan/approve", json={"approved": True})
        assert resp.status_code == 401

    def test_plan_approve_invalid_payload_400(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.post(
            f"/api/sessions/{sid}/plan/approve",
            headers=auth_headers,
            json={"approved": "yes"},  # not a bool
        )
        assert resp.status_code == 400

    def test_plan_approve_no_pending_plan_404(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        monkeypatch.setattr(backend.runtime, "approve_plan", lambda sid: False)
        sid = backend.session_store.create_session()
        resp = client.post(
            f"/api/sessions/{sid}/plan/approve",
            headers=auth_headers,
            json={"approved": True},
        )
        assert resp.status_code == 404

    def test_plan_approve_success(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        monkeypatch.setattr(backend.runtime, "approve_plan", lambda sid: True)
        monkeypatch.setattr(backend.runtime, "start_async", lambda **kw: True)
        sid = backend.session_store.create_session()
        resp = client.post(
            f"/api/sessions/{sid}/plan/approve",
            headers=auth_headers,
            json={"approved": True},
        )
        assert resp.status_code == 200
        assert resp.get_json()["approved"] is True

    def test_plan_reject_success(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        monkeypatch.setattr(backend.runtime, "reject_plan", lambda sid: True)
        sid = backend.session_store.create_session()
        resp = client.post(
            f"/api/sessions/{sid}/plan/approve",
            headers=auth_headers,
            json={"approved": False},
        )
        assert resp.status_code == 200
        assert resp.get_json()["approved"] is False

    def test_plan_reject_no_pending_404(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        monkeypatch.setattr(backend.runtime, "reject_plan", lambda sid: False)
        sid = backend.session_store.create_session()
        resp = client.post(
            f"/api/sessions/{sid}/plan/approve",
            headers=auth_headers,
            json={"approved": False},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/query (legacy sync endpoint) — session_tag + fork_from paths
# ---------------------------------------------------------------------------


class TestLegacySyncQuerySessionResolution:
    def test_query_with_session_tag_resolves(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        captured = {}

        def fake_run_sync(*args, **kwargs):
            captured["session_id"] = kwargs.get("session_id")
            return DummyQueue()

        monkeypatch.setattr(backend.runtime, "run_sync", fake_run_sync)
        resp = client.post(
            "/api/query",
            headers=auth_headers,
            json={"query": "hello", "session_tag": "test-tag"},
        )
        assert resp.status_code == 200
        resolved = backend.session_store.resolve_tag("test-tag")
        assert resolved == captured["session_id"]

    def test_query_with_fork_from_creates_new_session(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        source_sid = backend.session_store.create_session()
        captured = {}

        def fake_run_sync(*args, **kwargs):
            captured["session_id"] = kwargs.get("session_id")
            return DummyQueue()

        monkeypatch.setattr(backend.runtime, "run_sync", fake_run_sync)
        resp = client.post(
            "/api/query",
            headers=auth_headers,
            json={"query": "hello", "fork_from": source_sid},
        )
        assert resp.status_code == 200
        assert captured["session_id"] != source_sid

    def test_query_invalid_project_returns_400(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.post(
            "/api/query",
            headers=auth_headers,
            json={"query": "hello", "project": "managed:nonexistent-uuid-abc"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Notification service: success vs failure completion notifications
# ---------------------------------------------------------------------------


class TestNotificationServiceCompletion:
    def test_failure_completion_emits_warning_notification(self, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.session_store.append_event(
            sid,
            {
                "type": "completion",
                "payload": {
                    "done": True,
                    "done_reason": "error",
                    "task_result": None,
                },
            },
        )
        backend.notification_service.emit_completion(sid)
        notes = backend.notification_store.list(include_dismissed=True)
        session_notes = [n for n in notes if n.get("session_id") == sid]
        assert len(session_notes) == 1
        assert session_notes[0]["event_type"] == "failed"
        assert session_notes[0]["level"] == "warning"

    def test_success_completion_emits_info_notification(self, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.session_store.append_event(
            sid,
            {
                "type": "completion",
                "payload": {
                    "done": True,
                    "done_reason": "completed",
                    "task_result": "ok",
                },
            },
        )
        backend.notification_service.emit_completion(sid)
        notes = backend.notification_store.list(include_dismissed=True)
        session_notes = [n for n in notes if n.get("session_id") == sid]
        assert len(session_notes) == 1
        assert session_notes[0]["event_type"] == "completed"

    def test_transient_completion_emits_no_notification(self, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.session_store.append_event(
            sid,
            {
                "type": "completion",
                "payload": {
                    "done": True,
                    "done_reason": "canceled",
                    "task_result": None,
                },
            },
        )
        backend.notification_service.emit_completion(sid)
        notes = backend.notification_store.list(include_dismissed=True)
        session_notes = [n for n in notes if n.get("session_id") == sid]
        assert len(session_notes) == 0

    def test_session_label_uses_stored_title(self, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.session_store.save_title(sid, "My Session")
        label = backend.notification_service._session_label(sid)
        assert label == "My Session"

    def test_session_label_fallback_to_short_id(self, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        label = backend.notification_service._session_label(sid)
        assert label.startswith("Session ")
        assert sid[:8] in label

    def test_success_message_compacted(self):
        assert backend._success_message("compacted") == "Compaction finished."

    def test_success_message_command_prefix(self):
        msg = backend._success_message("command:compact")
        assert "compact" in msg
        assert "finished" in msg.lower()

    def test_success_message_generic(self):
        msg = backend._success_message("completed")
        # The generic path returns this exact literal (see backend.py:_success_message).
        assert msg == "Turn finished successfully."

    def test_classify_done_reason_transient(self):
        assert backend._classify_done_reason("canceled") is None
        assert backend._classify_done_reason("awaiting_approval") is None

    def test_classify_done_reason_failure(self):
        assert backend._classify_done_reason("error") == "failure"
        assert backend._classify_done_reason("command_failed:compact") == "failure"

    def test_classify_done_reason_success(self):
        assert backend._classify_done_reason("completed") == "success"
        assert backend._classify_done_reason("compacted") == "success"


# ---------------------------------------------------------------------------
# SessionPlanFile — /api/sessions/<id>/plan.md
# ---------------------------------------------------------------------------


class TestSessionPlanFile:
    def test_plan_file_requires_auth(self, client, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.get(f"/api/sessions/{sid}/plan.md")
        assert resp.status_code == 401

    def test_plan_file_returns_404_when_absent(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        # No plan.md file exists for this session
        resp = client.get(f"/api/sessions/{sid}/plan.md", headers=auth_headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Plan approve — start_async returns False (500 branch)
# ---------------------------------------------------------------------------


class TestPlanApproveStartFails:
    def test_plan_approve_start_async_fails_500(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        monkeypatch.setattr(backend.runtime, "approve_plan", lambda sid: True)
        monkeypatch.setattr(backend.runtime, "start_async", lambda **kw: False)
        sid = backend.session_store.create_session()
        resp = client.post(
            f"/api/sessions/{sid}/plan/approve",
            headers=auth_headers,
            json={"approved": True},
        )
        assert resp.status_code == 500
        body = resp.get_json()
        assert body["approved"] is True
        assert "could not start" in body["message"].lower()


# ---------------------------------------------------------------------------
# SessionGitDiff — no_project path
# ---------------------------------------------------------------------------


class TestSessionGitDiff:
    def test_git_diff_requires_auth(self, client, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.get(f"/api/sessions/{sid}/git-diff")
        assert resp.status_code == 401

    def test_git_diff_unknown_session_404(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.get("/api/sessions/no-such-id/git-diff", headers=auth_headers)
        assert resp.status_code == 404

    def test_git_diff_no_project_returns_no_project(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        # No context event with project — _resolve_session_cwd returns None
        resp = client.get(f"/api/sessions/{sid}/git-diff", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["git_repo"] is False
        assert body["reason"] == "no_project"

    def test_git_diff_invalid_scope_400(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.get(f"/api/sessions/{sid}/git-diff?scope=bad", headers=auth_headers)
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# SessionCommand — DIALOG render type (synchronous execution)
# ---------------------------------------------------------------------------


class TestSessionCommandDialog:
    def test_command_help_dialog_returns_200(self, client, auth_headers, tmp_path, monkeypatch):
        """'help' is a DIALOG-type command — executed synchronously and returned inline."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.post(
            f"/api/sessions/{sid}/command",
            headers=auth_headers,
            json={"name": "help", "args": []},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert "render" in body
        assert body["render"] == "dialog"
        assert "body" in body

    def test_command_skills_dialog_returns_200(self, client, auth_headers, tmp_path, monkeypatch):
        """'skills' is also DIALOG-type."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.post(
            f"/api/sessions/{sid}/command",
            headers=auth_headers,
            json={"name": "skills", "args": []},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["render"] == "dialog"

    def test_command_tokens_dialog_returns_200(self, client, auth_headers, tmp_path, monkeypatch):
        """'tokens' is DIALOG-type — returns usage numbers inline."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.post(
            f"/api/sessions/{sid}/command",
            headers=auth_headers,
            json={"name": "tokens", "args": []},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["render"] == "dialog"


# ---------------------------------------------------------------------------
# SessionUsage — with model context event
# ---------------------------------------------------------------------------


class TestSessionUsageWithModel:
    def test_usage_with_model_context(self, client, auth_headers, tmp_path, monkeypatch):
        """Usage endpoint reads model from the context event."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.session_store.append_event(
            sid,
            {"type": "context", "payload": {"model": "claude-opus-4"}},
        )
        resp = client.get(f"/api/sessions/{sid}/usage", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert isinstance(body, dict)
        # The root_model should be picked up from the context event.
        # build_usage_numbers always returns these keys (see token_budget.py).
        assert "root_model" in body
        assert body["root_model"] == "claude-opus-4"

    def test_usage_without_model_context(self, client, auth_headers, tmp_path, monkeypatch):
        """Usage endpoint falls back to config default when no context model."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.get(f"/api/sessions/{sid}/usage", headers=auth_headers)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Recovery with model_override
# ---------------------------------------------------------------------------


class TestSessionRecoveryModelOverride:
    def test_recovery_with_model_override(self, client, auth_headers, tmp_path, monkeypatch):
        """Recovery endpoint accepts an optional model override."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        context_appended = []

        def fake_resolve(session_id, action, from_ts=None, replacement_text=None):
            return "retry query"

        monkeypatch.setattr(backend.runtime, "resolve_recovery_query", fake_resolve)
        monkeypatch.setattr(backend.runtime, "start_async", lambda **kw: True)

        _orig_append = backend.runtime.append_context_event

        def capture_append(sid, payload):
            context_appended.append(payload)

        monkeypatch.setattr(backend.runtime, "append_context_event", capture_append)

        resp = client.post(
            f"/api/sessions/{sid}/recover",
            headers=auth_headers,
            json={"action": "retry", "model": "new-model-override"},
        )
        assert resp.status_code == 202
        # Context event with model override should have been appended
        assert any("model" in p for p in context_appended)
