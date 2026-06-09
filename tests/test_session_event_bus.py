"""Tests for the in-process SessionEventBus pub/sub choke-point.

The bus is the universal fan-out at the event-append hot path: it wakes SSE
waiters (subscriptions) and notifies observers (e.g. the on_event hook bridge)
on every appended event. ``publish`` must never block the caller — a slow
subscriber drops its oldest event rather than stalling the append.
"""

from __future__ import annotations

import queue

from mewbo_core.session_event_bus import (
    SessionEventBus,
    get_session_event_bus,
    reset_session_event_bus_for_tests,
    set_session_event_bus,
)
from mewbo_core.types import EventRecord


def _event(text: str = "hi") -> EventRecord:
    return {"ts": "2026-06-07T00:00:00Z", "type": "user", "payload": {"text": text}}


# -- Subscribe / publish ----------------------------------------------------


class TestSubscribePublish:
    """A published event reaches every live subscriber for that session."""

    def test_single_subscriber_receives(self):
        bus = SessionEventBus()
        sub = bus.subscribe("s1")
        bus.publish("s1", _event())
        got = sub.queue.get_nowait()
        assert got == _event()

    def test_multiple_subscribers_each_receive(self):
        bus = SessionEventBus()
        sub_a = bus.subscribe("s1")
        sub_b = bus.subscribe("s1")
        ev = _event("broadcast")
        bus.publish("s1", ev)
        assert sub_a.queue.get_nowait() == ev
        assert sub_b.queue.get_nowait() == ev

    def test_publish_is_session_scoped(self):
        bus = SessionEventBus()
        sub = bus.subscribe("s1")
        bus.publish("s2", _event())
        assert sub.queue.empty()

    def test_publish_to_no_subscribers_is_noop(self):
        bus = SessionEventBus()
        # Must not raise even with zero subscribers / observers.
        bus.publish("nobody", _event())

    def test_unsubscribe_stops_delivery(self):
        bus = SessionEventBus()
        sub = bus.subscribe("s1")
        bus.unsubscribe("s1", sub)
        bus.publish("s1", _event())
        assert sub.queue.empty()

    def test_unsubscribe_cleans_empty_session_set(self):
        bus = SessionEventBus()
        sub = bus.subscribe("s1")
        bus.unsubscribe("s1", sub)
        # The internal set is pruned when the last sub leaves.
        assert "s1" not in bus._subs


# -- Observers --------------------------------------------------------------


class TestObservers:
    """Observers are invoked with (session_id, event) on every publish."""

    def test_observer_invoked_with_args(self):
        bus = SessionEventBus()
        seen: list[tuple[str, EventRecord]] = []
        bus.register_observer(lambda sid, ev: seen.append((sid, ev)))
        ev = _event("observed")
        bus.publish("s1", ev)
        assert seen == [("s1", ev)]

    def test_failing_observer_isolated(self):
        bus = SessionEventBus()
        good_seen: list[str] = []

        def bad(sid: str, ev: EventRecord) -> None:
            raise RuntimeError("observer boom")

        bus.register_observer(bad)
        bus.register_observer(lambda sid, ev: good_seen.append(sid))
        sub = bus.subscribe("s1")
        # A raising observer must not break publish, the other observer, or
        # subscriber delivery.
        bus.publish("s1", _event())
        assert good_seen == ["s1"]
        assert not sub.queue.empty()


# -- Bounded queue / non-blocking publish -----------------------------------


class TestBoundedQueueOverflow:
    """A full subscriber queue drops its OLDEST event; publish never blocks."""

    def test_overflow_drops_oldest(self):
        bus = SessionEventBus()
        sub = bus.subscribe("s1", maxsize=3)
        for i in range(5):
            bus.publish("s1", _event(str(i)))
        # Queue holds the 3 most-recent (oldest two dropped), publish returned.
        drained = []
        while True:
            try:
                drained.append(sub.queue.get_nowait()["payload"]["text"])
            except queue.Empty:
                break
        assert drained == ["2", "3", "4"]

    def test_publish_never_blocks_on_full_queue(self):
        bus = SessionEventBus()
        sub = bus.subscribe("s1", maxsize=1)
        # Far more than maxsize — if publish blocked this would hang.
        for i in range(50):
            bus.publish("s1", _event(str(i)))
        assert sub.queue.qsize() == 1


# -- Process singleton ------------------------------------------------------


class TestSingleton:
    """get/set/reset mirror the wiki-store singleton idiom."""

    def test_get_lazily_creates_and_is_stable(self):
        reset_session_event_bus_for_tests()
        a = get_session_event_bus()
        b = get_session_event_bus()
        assert a is b
        assert isinstance(a, SessionEventBus)

    def test_set_pins_instance(self):
        custom = SessionEventBus()
        set_session_event_bus(custom)
        assert get_session_event_bus() is custom

    def test_reset_swaps_fresh(self):
        first = get_session_event_bus()
        second = reset_session_event_bus_for_tests()
        assert second is not first
        assert get_session_event_bus() is second
