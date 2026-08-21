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
import time
from collections.abc import Callable

from mewbo_core.common import get_logger
from mewbo_core.contracts.types import EventRecord

logger = get_logger(name="core.session_event_bus")

# Per-subscriber queue depth. Generous enough to absorb a burst of tool events
# while a slow client catches up; overflow drops the oldest (see ``publish``).
_DEFAULT_QUEUE_MAXSIZE = 2000

# How long a detached executor's timestamp is kept at all. It bounds the memory
# the recollection costs — an entry older than this can no longer satisfy any
# plausible grace window, so keeping it would only grow a map that nothing
# prunes. Comfortably above every caller's window (the device bridge asks for
# 5s); raise it here, not at a call site, if one ever needs longer.
_EXECUTOR_SEEN_RETENTION_S = 60.0

EventObserver = Callable[[str, EventRecord], None]


class Subscription:
    """A single SSE consumer's bounded mailbox on the bus.

    Holds the session it follows plus a bounded ``queue.Queue`` the bus pushes
    events into. The SSE generator blocks on ``queue.get(timeout=...)`` so a
    published event wakes it immediately (no polling).
    """

    __slots__ = ("session_id", "queue", "executor")

    def __init__(
        self,
        session_id: str,
        maxsize: int = _DEFAULT_QUEUE_MAXSIZE,
        *,
        executor: bool = False,
    ) -> None:
        """Create a subscription with a bounded mailbox queue.

        *executor* marks a consumer that can FULFIL a client-declared call, not
        merely read the stream. Default ``False`` — a plain reader is the
        common case, and a consumer that has not said it can execute must never
        be counted as one.
        """
        self.session_id = session_id
        self.queue: queue.Queue[EventRecord] = queue.Queue(maxsize=maxsize)
        self.executor = executor


class SessionEventBus:
    """In-process per-session pub/sub. The append-time fan-out choke-point.

    A single lock guards mutation of the subscriber map and observer list;
    ``publish`` snapshots under the lock then does its work outside it so a slow
    observer never holds the lock against concurrent subscribes.
    """

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        """Initialize empty subscriber and observer registries.

        *monotonic* is the clock the executor grace window measures against,
        injected as a FIELD so a test drives a window with no sleeping and
        without patching a stdlib module every other thread in the process
        shares.
        """
        self._lock = threading.Lock()
        self._subs: dict[str, set[Subscription]] = {}
        self._observers: list[EventObserver] = []
        self._monotonic = monotonic
        # session_id -> when its last executor subscription detached. Bounded by
        # ``_EXECUTOR_SEEN_RETENTION_S``; see ``_stamp_executor_detach_locked``.
        self._executor_seen: dict[str, float] = {}

    # -- subscription lifecycle --------------------------------------------

    def subscribe(
        self,
        session_id: str,
        maxsize: int = _DEFAULT_QUEUE_MAXSIZE,
        *,
        executor: bool = False,
    ) -> Subscription:
        """Register a new subscriber for *session_id* and return its handle.

        Pass ``executor=True`` only for a consumer that can fulfil a
        client-declared tool call (the device client). Server-internal
        consumers and read-only viewers leave it ``False``.
        """
        sub = Subscription(session_id, maxsize=maxsize, executor=executor)
        with self._lock:
            self._subs.setdefault(session_id, set()).add(sub)
        return sub

    def unsubscribe(self, session_id: str, sub: Subscription) -> None:
        """Remove *sub*; prune the session's set once it is empty.

        Removing an EXECUTOR also stamps when it left, which is the whole basis
        of :meth:`has_executor`'s grace window — the departure is the only moment
        the bus can record, since a subscription that is gone leaves nothing to
        ask afterwards.
        """
        with self._lock:
            subs = self._subs.get(session_id)
            if subs is None:
                return
            if sub in subs and sub.executor:
                self._stamp_executor_detach_locked(session_id)
            subs.discard(sub)
            if not subs:
                self._subs.pop(session_id, None)

    def _stamp_executor_detach_locked(self, session_id: str) -> None:
        """Record an executor's departure and drop the stamps nothing can use.

        Caller MUST hold ``_lock``. ``O(stamped sessions)``, and that set is what
        the sweep bounds: without it the map would keep one entry per session
        that ever ran a device tool, for the life of the process.
        """
        now = self._monotonic()
        self._executor_seen = {
            sid: seen
            for sid, seen in self._executor_seen.items()
            if now - seen <= _EXECUTOR_SEEN_RETENTION_S
        }
        self._executor_seen[session_id] = now

    def register_observer(self, callback: EventObserver) -> None:
        """Register a best-effort observer invoked on every publish."""
        with self._lock:
            self._observers.append(callback)

    def has_subscribers(self, session_id: str) -> bool:
        """True when any live SSE subscriber is attached to *session_id*.

        "Is anyone reading" — never "can anyone answer". A consumer that can
        FULFIL a client-declared call is a different and narrower question, and
        it has its own method: keeping it a flag on this one produced two
        spellings of one rule, and the answers must not be able to drift.
        """
        with self._lock:
            return bool(self._subs.get(session_id))

    def has_executor(self, session_id: str, *, grace_s: float = 0.0) -> bool:
        """True when a consumer that can FULFIL a call is attached — or just was.

        **A subscriber is not an executor, and that distinction was a real
        30-second stall, twice over.** The old answer counted any SSE consumer:
        a read-only console tab, a server-internal run streamer, or an assist
        overlay that renders a run without servicing its tools. Presence passed,
        the call was appended, nobody answered, and the dispatcher burned its
        whole budget. A consumer must SAY it can execute; silence reads as
        "cannot".

        **And a reconnect gap is not an absence.** The flag rides one SSE
        request, so the subscription dies with it — and these streams are
        deliberately short-lived (the generator gives its slot back the moment a
        session stops running). So a strict liveness read says "no executor"
        while the client is between connections, which is the false negative that
        makes an honest refusal a wrong one. *grace_s* admits an executor that
        DETACHED that recently; the caller owns the number, because only it knows
        what its own clients' reconnect ladders cost.

        Two bounds, both load-bearing: the window covers only a session that
        genuinely HAD an executor (one that never attached is refused with no
        delay), and it is remembered for at most
        ``_EXECUTOR_SEEN_RETENTION_S``. ``O(subscribers of one session)``.
        """
        with self._lock:
            subs = self._subs.get(session_id)
            if subs and any(sub.executor for sub in subs):
                return True
            if grace_s <= 0.0:
                return False
            seen = self._executor_seen.get(session_id)
            return seen is not None and (self._monotonic() - seen) <= grace_s

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
