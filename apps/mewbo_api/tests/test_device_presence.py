#!/usr/bin/env python3
"""Executor-aware presence, and failing fast when the client goes away mid-wait.

Both pin the same incident: an agent drove a phone successfully for
fourteen calls, then launched another app. That launch backgrounded Aura, which
tore down the stream device-tool calls are delivered over — so the tool that
navigates destroyed the transport for the tools after it. The replayed session
shows the signature: a 30s ``device_timeout`` first (the server still counted a
frozen subscriber), then instant ``device_unavailable`` once it was reaped.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from mewbo_api.device_tools import ApiDeviceToolDispatcher, DevicePendingCalls
from mewbo_core.loop.session_runtime import SessionRuntime
from mewbo_core.session.session_event_bus import (
    get_session_event_bus,
    reset_session_event_bus_for_tests,
)
from mewbo_core.session.session_store import SessionStore


@pytest.fixture(autouse=True)
def _fresh_bus():
    reset_session_event_bus_for_tests()
    yield
    reset_session_event_bus_for_tests()


@pytest.fixture()
def runtime(tmp_path):
    return SessionRuntime(session_store=SessionStore(root_dir=str(tmp_path / "sessions")))


class _FakeClock:
    """An injected monotonic clock, so a grace window is tested without sleeping.

    The bus takes its clock as a FIELD for exactly this reason — patching
    ``time.monotonic`` on a module would reach every other module and thread in
    the process (``tests/CLAUDE.md`` → Pitfalls).
    """

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestExecutorAwarePresence:
    """A subscriber is not an executor — the bus must be able to say which."""

    def test_a_plain_reader_does_not_count_as_an_executor(self):
        bus = get_session_event_bus()
        bus.subscribe("s1")

        assert bus.has_subscribers("s1") is True
        assert bus.has_executor("s1") is False

    def test_an_executor_counts_as_both(self):
        bus = get_session_event_bus()
        bus.subscribe("s1", executor=True)

        assert bus.has_subscribers("s1") is True
        assert bus.has_executor("s1") is True

    def test_a_reader_alongside_an_executor_does_not_mask_it(self):
        # The real shape: a console tab watching the same session the phone is
        # driving. The reader must neither create nor destroy executor presence.
        bus = get_session_event_bus()
        bus.subscribe("s1")
        bus.subscribe("s1", executor=True)

        assert bus.has_executor("s1") is True

    def test_losing_the_executor_while_a_reader_stays_reads_as_absent(self):
        # Exactly the incident: the phone goes away, a console tab does not.
        # The old check would have said "present" and burned the full timeout.
        bus = get_session_event_bus()
        bus.subscribe("s1")
        device = bus.subscribe("s1", executor=True)

        bus.unsubscribe("s1", device)

        assert bus.has_subscribers("s1") is True
        assert bus.has_executor("s1") is False

    def test_silence_reads_as_cannot_execute(self):
        # Default False, deliberately: a wrong "yes" costs a 30s stall, a wrong
        # "no" is an instant honest error.
        bus = get_session_event_bus()
        sub = bus.subscribe("s1")
        assert sub.executor is False


class TestTheGraceWindow:
    """A reconnect gap is not an absence — the false-negative of the same shape.

    The subscription is torn down the instant the SSE request ends, and the
    stream self-closes in milliseconds whenever the session is not running. So
    BETWEEN TURNS the bus reads zero executors while the phone is sitting there
    reconnecting. The ask-user dispatcher refuses to ask this question at all for
    the same reason; the device bridge asks it with a window instead, because it
    still needs the fast refusal for a client that genuinely is not there.
    """

    def test_a_detached_executor_stays_reachable_inside_the_window(self):
        from mewbo_core.session.session_event_bus import SessionEventBus

        clock = _FakeClock()
        bus = SessionEventBus(monotonic=clock)
        device = bus.subscribe("s1", executor=True)
        bus.unsubscribe("s1", device)

        assert bus.has_executor("s1", grace_s=5.0) is True
        clock.advance(4.9)
        assert bus.has_executor("s1", grace_s=5.0) is True
        clock.advance(0.2)
        assert bus.has_executor("s1", grace_s=5.0) is False

    def test_no_grace_asked_for_is_no_grace_given(self):
        # The default is the strict liveness question, so a caller that has not
        # thought about a window cannot accidentally inherit one.
        from mewbo_core.session.session_event_bus import SessionEventBus

        bus = SessionEventBus(monotonic=_FakeClock())
        device = bus.subscribe("s1", executor=True)
        bus.unsubscribe("s1", device)

        assert bus.has_executor("s1") is False

    def test_a_session_that_never_had_an_executor_gets_no_window(self):
        # The fast-fail property: a window covers a client that WAS here, never
        # one that might turn up. A reader-only session refuses immediately.
        from mewbo_core.session.session_event_bus import SessionEventBus

        bus = SessionEventBus(monotonic=_FakeClock())
        bus.subscribe("s1")  # reader, not executor

        assert bus.has_executor("s1", grace_s=3600.0) is False

    def test_a_reader_detaching_does_not_open_a_window(self):
        from mewbo_core.session.session_event_bus import SessionEventBus

        bus = SessionEventBus(monotonic=_FakeClock())
        reader = bus.subscribe("s1")
        bus.unsubscribe("s1", reader)

        assert bus.has_executor("s1", grace_s=5.0) is False

    def test_a_live_executor_answers_without_consulting_the_window(self):
        from mewbo_core.session.session_event_bus import SessionEventBus

        clock = _FakeClock()
        bus = SessionEventBus(monotonic=clock)
        first = bus.subscribe("s1", executor=True)
        bus.subscribe("s1", executor=True)  # the reconnect, before the teardown
        bus.unsubscribe("s1", first)
        clock.advance(3600.0)

        assert bus.has_executor("s1") is True


class TestFailFastMidWait:
    def test_a_client_that_vanishes_mid_wait_fails_in_the_WINDOW_not_the_BUDGET(
        self, runtime, monkeypatch
    ):
        """The 30s stall, which is what the user actually experienced.

        The refusal is now bounded by the grace window rather than by one poll
        tick — a phone that is coming back gets those seconds — but it is still
        an order of magnitude short of the call budget, which is the property
        that made the error useful to the model.
        """
        import mewbo_api.device_tools as device_tools_mod

        monkeypatch.setattr(device_tools_mod, "DEVICE_EXECUTOR_GRACE_S", 0.5)
        session_id = runtime.resolve_session()
        bus = get_session_event_bus()
        device = bus.subscribe(session_id, executor=True)
        dispatcher = ApiDeviceToolDispatcher(runtime=runtime, pending=DevicePendingCalls())

        async def _drive():
            task = asyncio.create_task(dispatcher.dispatch(session_id, "device_ui", {}))
            await asyncio.sleep(0.05)
            # The phone backgrounds — this is what launching another app does.
            bus.unsubscribe(session_id, device)
            return await task

        start = time.monotonic()
        result = asyncio.run(_drive())
        elapsed = time.monotonic() - start

        assert result["error"]["code"] == "device_unavailable"
        # The whole point: sub-second, against a 30s budget it used to burn.
        assert elapsed < 2.0, f"took {elapsed:.1f}s — the poll loop is not re-checking presence"
        assert device_tools_mod.DEVICE_TOOL_TIMEOUT_S == 30.0

    def test_a_present_client_still_gets_its_full_budget(self, runtime):
        # The paired positive: fail-fast must not clip a slow-but-live client.
        # Without this, the test above would pass just as well against a
        # dispatcher that refused everything.
        import mewbo_api.device_tools as device_tools_mod

        session_id = runtime.resolve_session()
        get_session_event_bus().subscribe(session_id, executor=True)
        pending = DevicePendingCalls()
        dispatcher = ApiDeviceToolDispatcher(runtime=runtime, pending=pending)

        async def _drive():
            task = asyncio.create_task(dispatcher.dispatch(session_id, "device_ui", {}))
            await asyncio.sleep(0.4)  # slower than several poll ticks
            events = runtime.load_events(session_id)
            payload = next(
                e["payload"] for e in events if e.get("type") == "device_tool_call"
            )
            pending.resolve(
                session_id,
                payload["call_id"],
                payload["call_token"],
                {"status": "ok", "result": {"elements": []}},
            )
            return await task

        assert device_tools_mod.DEVICE_TOOL_TIMEOUT_S == 30.0
        result = asyncio.run(_drive())
        assert result == {"status": "ok", "result": {"elements": []}}

    def test_no_executor_at_entry_is_refused_without_appending_an_event(self, runtime):
        # A reader-only session must not have a call appended to its transcript
        # for a device that was never going to answer.
        session_id = runtime.resolve_session()
        get_session_event_bus().subscribe(session_id)  # reader, not executor
        dispatcher = ApiDeviceToolDispatcher(runtime=runtime, pending=DevicePendingCalls())

        result = asyncio.run(dispatcher.dispatch(session_id, "device_ui", {}))

        assert result["error"]["code"] == "device_unavailable"
        assert runtime.load_events(session_id) == []


class TestABetweenTurnsGapIsNotAnAbsence:
    """The dispatch half of the window: a torn-down stream still has a client.

    The teardown is not a hypothetical — the stream generator picks a blocking
    timeout of ``0.0`` whenever the session is not running, so it closes in
    milliseconds and the subscription goes with it. A call dispatched in the
    first moments of the next turn therefore asks the bus a question whose
    honest answer is "reconnecting", and the bus can only say "no".
    """

    def test_a_call_dispatched_during_the_gap_is_delivered_on_reconnect(
        self, runtime, monkeypatch
    ):
        import mewbo_api.device_tools as device_tools_mod

        monkeypatch.setattr(device_tools_mod, "DEVICE_EXECUTOR_GRACE_S", 5.0)
        session_id = runtime.resolve_session()
        bus = get_session_event_bus()
        # The previous turn's stream, already closed by the time this run starts.
        bus.unsubscribe(session_id, bus.subscribe(session_id, executor=True))
        pending = DevicePendingCalls()
        dispatcher = ApiDeviceToolDispatcher(runtime=runtime, pending=pending)

        async def _drive():
            task = asyncio.create_task(dispatcher.dispatch(session_id, "device_ui", {}))
            await asyncio.sleep(0.3)  # the reconnect delay, several poll ticks
            bus.subscribe(session_id, executor=True)
            payload = next(
                e["payload"]
                for e in runtime.load_events(session_id)
                if e.get("type") == "device_tool_call"
            )
            pending.resolve(
                session_id,
                payload["call_id"],
                payload["call_token"],
                {"status": "ok", "result": {"elements": []}},
            )
            return await task

        assert asyncio.run(_drive()) == {"status": "ok", "result": {"elements": []}}

    def test_a_client_gone_longer_than_the_window_is_still_refused_at_entry(
        self, runtime, monkeypatch
    ):
        # The window is a window, not an amnesty: past it, the refusal is
        # immediate and nothing is appended for a device that will not answer.
        import mewbo_api.device_tools as device_tools_mod

        monkeypatch.setattr(device_tools_mod, "DEVICE_EXECUTOR_GRACE_S", 0.05)
        session_id = runtime.resolve_session()
        bus = get_session_event_bus()
        bus.unsubscribe(session_id, bus.subscribe(session_id, executor=True))
        time.sleep(0.1)
        dispatcher = ApiDeviceToolDispatcher(runtime=runtime, pending=DevicePendingCalls())

        result = asyncio.run(dispatcher.dispatch(session_id, "device_ui", {}))

        assert result["error"]["code"] == "device_unavailable"
        assert runtime.load_events(session_id) == []
