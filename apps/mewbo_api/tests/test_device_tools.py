"""Unit tests for ``mewbo_api.device_tools`` (Gitea #179, Phase 1).

Covers ``DevicePendingCalls`` registry mechanics (consumed-once resolve,
token check, opportunistic reaping) and ``ApiDeviceToolDispatcher.dispatch``
in isolation — no Flask, no LLM. Route-level contract tests (the full
POST /query → device_tool_call → POST result round trip) live in
``test_device_tools_routes.py``.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from mewbo_api.device_tools import ApiDeviceToolDispatcher, DevicePendingCalls
from mewbo_core.session_event_bus import (
    get_session_event_bus,
    reset_session_event_bus_for_tests,
)
from mewbo_core.session_runtime import SessionRuntime
from mewbo_core.session_store import SessionStore


@pytest.fixture(autouse=True)
def _fresh_event_bus():
    """Isolate ``has_subscribers`` state per test (process-wide singleton)."""
    reset_session_event_bus_for_tests()
    yield
    reset_session_event_bus_for_tests()


class TestDevicePendingCalls:
    def test_create_then_resolve_ok(self):
        pending = DevicePendingCalls()
        event = pending.create("s1", "c1", "tok", time.time() + 30)
        outcome = pending.resolve("s1", "c1", "tok", {"status": "ok", "result": 1})
        assert outcome == "ok"
        assert event.is_set()
        assert pending.result_for("s1", "c1") == {"status": "ok", "result": 1}

    def test_resolve_unknown_call_is_not_found(self):
        pending = DevicePendingCalls()
        assert pending.resolve("s1", "missing", "tok", {}) == "not_found"

    def test_resolve_wrong_token_is_bad_token(self):
        pending = DevicePendingCalls()
        pending.create("s1", "c1", "correct-token", time.time() + 30)
        assert pending.resolve("s1", "c1", "wrong-token", {}) == "bad_token"

    def test_resolve_twice_conflicts(self):
        pending = DevicePendingCalls()
        pending.create("s1", "c1", "tok", time.time() + 30)
        assert pending.resolve("s1", "c1", "tok", {"a": 1}) == "ok"
        assert pending.resolve("s1", "c1", "tok", {"a": 2}) == "conflict"
        # The first delivered result wins — never overwritten by the conflict.
        assert pending.result_for("s1", "c1") == {"a": 1}

    def test_different_sessions_do_not_collide_on_same_call_id(self):
        pending = DevicePendingCalls()
        pending.create("s1", "c1", "tok-a", time.time() + 30)
        pending.create("s2", "c1", "tok-b", time.time() + 30)
        assert pending.resolve("s1", "c1", "tok-b", {}) == "bad_token"
        assert pending.resolve("s2", "c1", "tok-b", {"x": 1}) == "ok"

    def test_expired_entry_opportunistically_reaped_on_resolve(self):
        pending = DevicePendingCalls()
        pending.create("s1", "c1", "tok", time.time() - 1)  # already past deadline
        assert pending.resolve("s1", "c1", "tok", {}) == "not_found"

    def test_expired_entry_opportunistically_reaped_on_create(self):
        pending = DevicePendingCalls()
        pending.create("s1", "c1", "tok", time.time() - 1)
        pending.create("s1", "c2", "tok2", time.time() + 30)  # triggers a sweep
        assert pending.resolve("s1", "c1", "tok", {}) == "not_found"

    def test_result_for_is_non_destructive(self):
        """A duplicate POST after delivery must still see a live (conflicting)
        entry — reading the result must not itself remove it."""
        pending = DevicePendingCalls()
        pending.create("s1", "c1", "tok", time.time() + 30)
        pending.resolve("s1", "c1", "tok", {"x": 1})
        assert pending.result_for("s1", "c1") == {"x": 1}
        assert pending.result_for("s1", "c1") == {"x": 1}
        assert pending.resolve("s1", "c1", "tok", {"x": 2}) == "conflict"

    def test_result_for_unresolved_call_is_none(self):
        pending = DevicePendingCalls()
        pending.create("s1", "c1", "tok", time.time() + 30)
        assert pending.result_for("s1", "c1") is None

    def test_reap_never_drops_a_consumed_unread_entry_near_deadline(self):
        """F9: a result delivered WITHIN its deadline (resolve succeeds) must
        survive an UNRELATED concurrent reap sweep — even after real
        wall-clock time has since crossed that deadline — until the
        dispatcher actually reads it. Dropping it early would surface a
        spurious internal error and risk a duplicate real-world side effect
        (a model retry re-triggering the device action)."""
        pending = DevicePendingCalls()
        # A short-lived deadline the resolve() call beats...
        pending.create("s1", "c1", "tok", time.time() + 0.05)
        assert pending.resolve("s1", "c1", "tok", {"x": 1}) == "ok"
        # ...but which has since elapsed by the time anyone reads it —
        # reproducing the dispatcher's up-to-0.2s poll gap.
        time.sleep(0.1)

        # An unrelated create/resolve call triggers an opportunistic reap
        # sweep — this must NOT delete the consumed-but-unread "c1" entry.
        pending.create("s1", "other", "tok2", time.time() + 30)

        assert pending.result_for("s1", "c1") == {"x": 1}

    def test_reap_removes_consumed_read_entry_via_unrelated_sweep(self):
        """Once read, an entry is eligible for opportunistic cleanup by a
        LATER unrelated call — no permanent leak."""
        pending = DevicePendingCalls()
        pending.create("s1", "c1", "tok", time.time() + 30)
        pending.resolve("s1", "c1", "tok", {"x": 1})
        pending.result_for("s1", "c1")  # marks it `read`

        pending.create("s1", "other", "tok2", time.time() + 30)  # triggers a sweep

        assert pending.result_for("s1", "c1") is None

    def test_reap_eventually_drops_a_consumed_unread_entry_past_grace(self):
        """A consumed-but-never-read entry is not immortal — it ages out
        past the grace window (a dispatcher that crashed mid-read must not
        leak the registry forever). Backdates the entry's deadline directly
        (white-box) rather than sleeping 60+ real seconds in a test."""
        import mewbo_api.device_tools as device_tools_mod

        pending = DevicePendingCalls()
        pending.create("s1", "c1", "tok", time.time() + 0.05)
        assert pending.resolve("s1", "c1", "tok", {"x": 1}) == "ok"
        with pending._lock:
            pending._pending[("s1", "c1")].expires_at = (
                time.time() - device_tools_mod._CONSUMED_UNREAD_GRACE_S - 1
            )

        pending.create("s1", "other", "tok2", time.time() + 30)  # triggers a sweep

        assert pending.result_for("s1", "c1") is None

    def test_second_post_still_conflicts_after_dispatcher_has_read(self):
        """The round-trip guarantee (a duplicate POST 409s, not 404s) must
        survive the F9 fix: excluding the target key from its OWN resolve()
        sweep, not just the read/unread distinction."""
        pending = DevicePendingCalls()
        pending.create("s1", "c1", "tok", time.time() + 30)
        assert pending.resolve("s1", "c1", "tok", {"x": 1}) == "ok"
        pending.result_for("s1", "c1")  # dispatcher reads it — now reap-eligible
        # The duplicate POST's own resolve() call must not self-sweep the
        # entry it is about to answer for.
        assert pending.resolve("s1", "c1", "tok", {"x": 2}) == "conflict"


@pytest.fixture()
def runtime(tmp_path):
    return SessionRuntime(session_store=SessionStore(root_dir=str(tmp_path)))


class TestApiDeviceToolDispatcher:
    def test_dispatch_appends_device_tool_call_event(self, runtime):
        session_id = runtime.resolve_session()
        get_session_event_bus().subscribe(session_id)  # a client is "attached"
        pending = DevicePendingCalls()
        dispatcher = ApiDeviceToolDispatcher(runtime=runtime, pending=pending)

        async def _drive():
            task = asyncio.create_task(dispatcher.dispatch(session_id, "device_x", {"a": 1}))
            # Give the dispatch coroutine a chance to append the event and
            # start waiting, then resolve it as the client would.
            await asyncio.sleep(0.05)
            events = runtime.load_events(session_id)
            call_events = [e for e in events if e.get("type") == "device_tool_call"]
            assert len(call_events) == 1
            payload = call_events[0]["payload"]
            assert payload["tool_id"] == "device_x"
            assert payload["args"] == {"a": 1}
            assert set(payload) == {"call_id", "call_token", "tool_id", "args", "expires_at"}
            outcome = pending.resolve(
                session_id,
                payload["call_id"],
                payload["call_token"],
                {"status": "ok", "result": {"sent": True}},
            )
            assert outcome == "ok"
            return await task

        result = asyncio.run(_drive())
        assert result == {"status": "ok", "result": {"sent": True}}

    def test_dispatch_times_out_without_a_delivered_result(self, monkeypatch, runtime):
        import mewbo_api.device_tools as device_tools_mod

        monkeypatch.setattr(device_tools_mod, "DEVICE_TOOL_TIMEOUT_S", 0.2)
        session_id = runtime.resolve_session()
        get_session_event_bus().subscribe(session_id)  # a client is "attached"
        pending = DevicePendingCalls()
        dispatcher = ApiDeviceToolDispatcher(runtime=runtime, pending=pending)

        result = asyncio.run(dispatcher.dispatch(session_id, "device_x", {}))
        assert result["status"] == "error"
        assert result["error"]["code"] == "device_timeout"

    def test_dispatch_default_pending_registry_is_process_singleton(self, runtime, monkeypatch):
        """No explicit ``pending=`` falls back to the shared module singleton."""
        import mewbo_api.device_tools as device_tools_mod

        fresh = device_tools_mod.reset_pending_calls_for_tests()
        dispatcher = ApiDeviceToolDispatcher(runtime=runtime)
        assert dispatcher._pending is fresh

    def test_dispatch_with_no_subscriber_returns_device_unavailable_immediately(self, runtime):
        """F10: no client attached to the session's SSE stream → an instant
        honest error, never the full ``DEVICE_TOOL_TIMEOUT_S`` wait."""
        session_id = runtime.resolve_session()
        # Deliberately do NOT subscribe — simulates /message re-engage or
        # /recover with nobody watching the stream.
        dispatcher = ApiDeviceToolDispatcher(runtime=runtime)

        start = time.monotonic()
        result = asyncio.run(dispatcher.dispatch(session_id, "device_x", {}))
        elapsed = time.monotonic() - start

        assert result == {
            "status": "error",
            "error": {
                "code": "device_unavailable",
                "message": (
                    f"No client is attached to session {session_id}'s event "
                    "stream; device tool 'device_x' cannot be delivered."
                ),
            },
        }
        # Well under the default 30s timeout — proves no poll loop ran.
        assert elapsed < 1.0
        # No pending call was ever registered and no event was appended.
        assert runtime.load_events(session_id) == []

    def test_dispatch_with_subscriber_proceeds_normally(self, runtime):
        """Positive control: a subscriber present → normal dispatch, not the
        F10 short-circuit."""
        session_id = runtime.resolve_session()
        get_session_event_bus().subscribe(session_id)
        pending = DevicePendingCalls()
        dispatcher = ApiDeviceToolDispatcher(runtime=runtime, pending=pending)

        async def _drive():
            task = asyncio.create_task(dispatcher.dispatch(session_id, "device_x", {}))
            await asyncio.sleep(0.05)
            events = runtime.load_events(session_id)
            call_events = [e for e in events if e.get("type") == "device_tool_call"]
            assert len(call_events) == 1
            payload = call_events[0]["payload"]
            pending.resolve(
                session_id, payload["call_id"], payload["call_token"], {"status": "ok", "result": 1}
            )
            return await task

        assert asyncio.run(_drive()) == {"status": "ok", "result": 1}
