#!/usr/bin/env python3
"""In-process per-session pub/sub fan-out for appended events.

``SessionEventBus`` is the single choke-point that wakes SSE waiters and
notifies observers (e.g. the ``on_event`` hook bridge) on **every** appended
event. It is driven from ``SessionStore.append_event`` — the true universal
funnel, which catches in-loop emissions *and* out-of-loop appends (user
enqueues, context events).

Design notes
------------
* **``publish`` must never block the caller.** It runs on the event-append hot
  path, so a slow SSE client must not stall a durable write. Each subscriber
  has a *bounded* queue; on overflow the oldest event is dropped (the SSE
  consumer reloads the backlog on reconnect, so a dropped live event is not
  data loss).
* **Single-process is correct under ``--workers 1``.** The API serves with one
  gunicorn worker + a thread pool, so an in-memory bus reaches every SSE thread.
  The documented seam for a future multi-worker deployment is a
  ``RedisSessionEventBus`` subclass overriding ``publish``/``subscribe`` — do
  NOT build it speculatively; the abstraction boundary is exactly those two
  methods.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable

from mewbo_core.common import get_logger
from mewbo_core.types import EventRecord

logger = get_logger(name="core.session_event_bus")

# Per-subscriber queue depth. Generous enough to absorb a burst of tool events
# while a slow client catches up; overflow drops the oldest (see ``publish``).
_DEFAULT_QUEUE_MAXSIZE = 2000

EventObserver = Callable[[str, EventRecord], None]


class Subscription:
    """A single SSE consumer's bounded mailbox on the bus.

    Holds the session it follows plus a bounded ``queue.Queue`` the bus pushes
    events into. The SSE generator blocks on ``queue.get(timeout=...)`` so a
    published event wakes it immediately (no polling).
    """

    __slots__ = ("session_id", "queue")

    def __init__(self, session_id: str, maxsize: int = _DEFAULT_QUEUE_MAXSIZE) -> None:
        """Create a subscription with a bounded mailbox queue."""
        self.session_id = session_id
        self.queue: queue.Queue[EventRecord] = queue.Queue(maxsize=maxsize)


class SessionEventBus:
    """In-process per-session pub/sub. The append-time fan-out choke-point.

    A single lock guards mutation of the subscriber map and observer list;
    ``publish`` snapshots under the lock then does its work outside it so a slow
    observer never holds the lock against concurrent subscribes.
    """

    def __init__(self) -> None:
        """Initialize empty subscriber and observer registries."""
        self._lock = threading.Lock()
        self._subs: dict[str, set[Subscription]] = {}
        self._observers: list[EventObserver] = []

    # -- subscription lifecycle --------------------------------------------

    def subscribe(self, session_id: str, maxsize: int = _DEFAULT_QUEUE_MAXSIZE) -> Subscription:
        """Register a new subscriber for *session_id* and return its handle."""
        sub = Subscription(session_id, maxsize=maxsize)
        with self._lock:
            self._subs.setdefault(session_id, set()).add(sub)
        return sub

    def unsubscribe(self, session_id: str, sub: Subscription) -> None:
        """Remove *sub*; prune the session's set once it is empty."""
        with self._lock:
            subs = self._subs.get(session_id)
            if subs is None:
                return
            subs.discard(sub)
            if not subs:
                self._subs.pop(session_id, None)

    def register_observer(self, callback: EventObserver) -> None:
        """Register a best-effort observer invoked on every publish."""
        with self._lock:
            self._observers.append(callback)

    # -- fan-out ------------------------------------------------------------

    def publish(self, session_id: str, event: EventRecord) -> None:
        """Fan *event* out to subscribers + observers without blocking.

        Snapshots the subscriber set and observer list under the lock, then:
        * non-blocking ``put`` into each subscriber queue; on ``Full`` drop the
          oldest (``get_nowait`` then ``put_nowait``) so a slow client never
          stalls this (hot-path) caller;
        * run each observer best-effort (try/except) — an observer raising must
          not break publish, sibling observers, or the durable append.
        """
        with self._lock:
            subs = list(self._subs.get(session_id, ()))
            observers = list(self._observers)

        for sub in subs:
            try:
                sub.queue.put_nowait(event)
            except queue.Full:
                # Drop the oldest to make room; never block the publisher.
                try:
                    sub.queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    sub.queue.put_nowait(event)
                except queue.Full:
                    # A concurrent consumer refilled it — fine, drop this event.
                    pass

        for observer in observers:
            try:
                observer(session_id, event)
            except Exception:
                logger.warning("Session event observer failed", exc_info=True)


# ---------------------------------------------------------------------------
# Process-wide singleton (DI seam shared by the store + the API)
# ---------------------------------------------------------------------------

_SESSION_EVENT_BUS: SessionEventBus | None = None


def get_session_event_bus() -> SessionEventBus:
    """Return the process-wide session event bus, creating it on first use.

    The store publishes through this singleton and the API both subscribes
    (SSE) and registers the hook observer against it — the same
    singleton+factory+``reset_for_tests`` shape as the wiki store, kept in core
    so both the store (down) and the app (up) reach it through one seam.
    """
    global _SESSION_EVENT_BUS
    if _SESSION_EVENT_BUS is None:
        _SESSION_EVENT_BUS = SessionEventBus()
    return _SESSION_EVENT_BUS


def set_session_event_bus(bus: SessionEventBus | None) -> None:
    """Pin the process-wide bus (API startup wiring / test injection)."""
    global _SESSION_EVENT_BUS
    _SESSION_EVENT_BUS = bus


def reset_session_event_bus_for_tests() -> SessionEventBus:
    """Swap in a fresh bus for test isolation and return it."""
    bus = SessionEventBus()
    set_session_event_bus(bus)
    return bus


__all__ = [
    "Subscription",
    "SessionEventBus",
    "get_session_event_bus",
    "set_session_event_bus",
    "reset_session_event_bus_for_tests",
]
