"""Tests for the push-based (#46) SSE session stream generator.

The generator must:
- load the backlog exactly ONCE (no per-event full-transcript re-read),
- emit each backlog event, then live events pushed via the SessionEventBus,
- de-duplicate the subscribe/backlog race-window overlap by content key,
- emit ``stream_end`` when the run is no longer running and the queue drains.
"""

from __future__ import annotations

import queue
from types import SimpleNamespace

from mewbo_api.backend import SessionStream
from mewbo_core.session_event_bus import SessionEventBus
from mewbo_core.types import EventRecord


def _ev(text: str) -> EventRecord:
    return {"ts": f"2026-06-07T00:00:0{text}Z", "type": "user", "payload": {"text": text}}


def _fake_runtime(backlog: list[EventRecord], running_flags: list[bool]):
    """Build a fake runtime whose is_running() returns the flags in sequence.

    ``load_transcript`` records its call count so the test can assert it is
    invoked exactly once (proves the per-event re-read is gone).
    """
    calls = {"load_transcript": 0}

    def load_transcript(session_id: str) -> list[EventRecord]:
        calls["load_transcript"] += 1
        return list(backlog)

    flags = iter(running_flags)

    def is_running(session_id: str) -> bool:
        try:
            return next(flags)
        except StopIteration:
            return False

    store = SimpleNamespace(load_transcript=load_transcript)
    runtime = SimpleNamespace(session_store=store, is_running=is_running)
    return runtime, calls


def test_backlog_then_live_then_stream_end():
    """Backlog flushes, one live event arrives once, then stream_end on stop."""
    bus = SessionEventBus()
    backlog = [_ev("0"), _ev("1")]
    # is_running: True (still running, queue empty heartbeat path not needed
    # because we publish a live event first), then False to close.
    runtime, calls = _fake_runtime(backlog, running_flags=[False])

    live = _ev("2")

    def gen():
        # Pre-seed the live event so the first queue.get returns it without a
        # heartbeat timeout, keeping the test fast and deterministic.
        sub = bus.subscribe("s1")
        sub.queue.put(live)
        yield from SessionStream._stream_events(
            "s1", runtime, bus, heartbeat_s=0.05, idle_close_s=0.2, _sub=sub
        )

    chunks = list(gen())
    # Backlog (2) + live (1) + stream_end (1).
    assert chunks[0] == f"data: {_json(backlog[0])}\n\n"
    assert chunks[1] == f"data: {_json(backlog[1])}\n\n"
    assert chunks[2] == f"data: {_json(live)}\n\n"
    assert chunks[3] == 'data: {"type": "stream_end"}\n\n'
    # The critical assertion: backlog loaded exactly once.
    assert calls["load_transcript"] == 1


def test_pending_event_drained_before_stream_end():
    """A terminal event published in the close race is delivered, not dropped.

    Exercises the empty-timeout→not-running branch with a PENDING queue item:
    the run thread publishes its completion right before is_running flips False,
    so the loop must drain the queue (with backlog dedup) before stream_end.
    The first blocking get() must time out (queue momentarily empty) for the
    drain branch to be reached — a plain pre-seeded queue would short-circuit
    via the happy get() path and never exercise the race (the bug this guards).
    """
    bus = SessionEventBus()
    runtime, _ = _fake_runtime([], running_flags=[False])

    terminal = _ev("9")

    class _BlockingThenItemQueue:
        """A queue whose blocking get() times out once, then the item is drained.

        Models the real race: the first heartbeat get() finds nothing, the run
        finishes (is_running False), and the terminal event is already enqueued
        for the non-blocking drain loop to pick up.
        """

        def __init__(self) -> None:
            self._q: queue.Queue = queue.Queue()
            self._first_get = True

        def get(self, timeout=None):
            if self._first_get:
                self._first_get = False
                raise queue.Empty
            return self._q.get(timeout=timeout)

        def get_nowait(self):
            return self._q.get_nowait()

        def put(self, item) -> None:
            self._q.put(item)

    def gen():
        sub = bus.subscribe("s1")
        sub.queue = _BlockingThenItemQueue()  # type: ignore[assignment]
        sub.queue.put(terminal)
        yield from SessionStream._stream_events(
            "s1", runtime, bus, heartbeat_s=0.01, idle_close_s=5.0, _sub=sub
        )

    chunks = list(gen())
    # The terminal event MUST be delivered, and BEFORE stream_end.
    assert chunks == [
        f"data: {_json(terminal)}\n\n",
        'data: {"type": "stream_end"}\n\n',
    ]


def test_dedup_drops_backlog_overlap():
    """An event already in the backlog arriving on the queue is dropped once."""
    bus = SessionEventBus()
    dup = _ev("0")
    backlog = [dup]
    runtime, calls = _fake_runtime(backlog, running_flags=[False])

    def gen():
        sub = bus.subscribe("s1")
        # Simulate the race: the same event landed on the queue (publish fired
        # between subscribe and backlog load) AND is in the backlog.
        sub.queue.put(dup)
        yield from SessionStream._stream_events(
            "s1", runtime, bus, heartbeat_s=0.05, idle_close_s=0.2, _sub=sub
        )

    chunks = list(gen())
    data_frames = [c for c in chunks if c.startswith("data: ")]
    # Backlog yields it once; the queued duplicate is skipped; then stream_end.
    assert data_frames == [
        f"data: {_json(dup)}\n\n",
        'data: {"type": "stream_end"}\n\n',
    ]


def test_heartbeat_emitted_while_running_and_idle():
    """While running with an empty queue, a heartbeat comment is emitted."""
    bus = SessionEventBus()
    # First is_running check (empty queue) -> True -> heartbeat; second -> False.
    runtime, _ = _fake_runtime([], running_flags=[True, False])

    def gen():
        sub = bus.subscribe("s1")
        yield from SessionStream._stream_events(
            "s1", runtime, bus, heartbeat_s=0.01, idle_close_s=5.0, _sub=sub
        )

    chunks = list(gen())
    assert ": heartbeat\n\n" in chunks
    assert chunks[-1] == 'data: {"type": "stream_end"}\n\n'


def test_idle_close_breaks_without_stream_end_when_still_running():
    """Idle past idle_close_s closes the stream even while nominally running."""
    bus = SessionEventBus()
    # Always running -> never emits stream_end -> must break on idle timeout.
    runtime, _ = _fake_runtime([], running_flags=[True] * 100)

    def gen():
        sub = bus.subscribe("s1")
        yield from SessionStream._stream_events(
            "s1", runtime, bus, heartbeat_s=0.01, idle_close_s=0.03, _sub=sub
        )

    chunks = list(gen())
    # Idle close path: no stream_end (run still active), just terminates.
    assert all(c == ": heartbeat\n\n" for c in chunks)


def _json(ev: EventRecord) -> str:
    import json

    return json.dumps(ev)
