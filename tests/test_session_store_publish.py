"""append_event publishes to the SessionEventBus (the choke-point fan-out).

The published dict MUST be the same shape ``load_transcript`` returns (the
persisted record incl. ``ts``) so an SSE live event is byte-identical to the
same event read back from the backlog.
"""

from __future__ import annotations

from mewbo_core.session_event_bus import (
    SessionEventBus,
    reset_session_event_bus_for_tests,
    set_session_event_bus,
)
from mewbo_core.session_store import SessionStore


def test_append_event_publishes_record(tmp_path):
    """Subscribing then appending delivers the persisted record on the queue."""
    bus = SessionEventBus()
    set_session_event_bus(bus)
    try:
        store = SessionStore(root_dir=str(tmp_path))
        session_id = store.create_session()
        sub = bus.subscribe(session_id)

        store.append_event(session_id, {"type": "user", "payload": {"text": "hello"}})

        published = sub.queue.get_nowait()
        # Same shape as load_transcript: includes the persisted ts.
        persisted = store.load_transcript(session_id)[0]
        assert published == persisted
        assert published["type"] == "user"
        assert "ts" in published
    finally:
        reset_session_event_bus_for_tests()


def test_append_event_publish_failure_does_not_break_append(tmp_path, monkeypatch):
    """A bus.publish that raises must never break the (durable) append."""
    bus = SessionEventBus()

    def boom(session_id, event):
        raise RuntimeError("bus down")

    monkeypatch.setattr(bus, "publish", boom)
    set_session_event_bus(bus)
    try:
        store = SessionStore(root_dir=str(tmp_path))
        session_id = store.create_session()
        # Append must still persist despite the publish failure.
        store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})
        assert store.load_transcript(session_id)[0]["type"] == "user"
    finally:
        reset_session_event_bus_for_tests()
