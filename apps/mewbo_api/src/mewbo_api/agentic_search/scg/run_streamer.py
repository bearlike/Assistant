"""RunEventStreamer — project a live session transcript onto the run event log.

The root-cause fix for "the console sits on *Starting search…* for the whole
run" (#77): the orchestrated runner used to drive ``run_sync`` to completion and
then ``_settle`` batch-replayed EVERY ``agent_*`` event at the end, so a 2m42s
run emitted a single ``run_started`` followed by 53 events in one burst.

The mechanism reuses the SideStage streaming seam verbatim — the core
``SessionEventBus`` (``session_event_bus.py``), the same in-process per-session
pub/sub the realtime ``/v1/draft/stream`` and the console SSE generator already
ride. No new transport: the streamer *subscribes* to the backing session before
the drive starts, drains the subscription on a daemon thread, and projects each
``sub_agent`` lifecycle event onto ``store.append_run_event`` AS it happens
(``agent_start`` → ``agent_line`` → ``agent_done``). The run's own SSE generator
(``RunSseGenerator``) tails that log, so the console reveals each probe live.

Settle is reduced to terminal reconciliation: the synthesis typewriter
(``answer_delta*`` → ``answer_ready``) + ``run_done`` / ``error``, plus a
back-stop that flushes any trace agent the live stream missed (a fast run whose
``completion`` lands before the consumer drains, or a bus drop). Each ``run_id``
is consulted for what it has already streamed, so a reconciled agent is never
double-emitted.

This is a *transport* concern (it writes the api run store), so it lives here in
the api glue, not in the core engine or the graph library.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from mewbo_core.common import get_logger
from mewbo_core.session_event_bus import SessionEventBus, Subscription
from mewbo_core.types import EventRecord

from .. import events
from ..schemas import TraceAgent, TraceLine

logging = get_logger(name="api.agentic_search.scg.run_streamer")


@dataclass
class _LaneState:
    """Per-probe streaming state shared between the consumer + the settle worker.

    ``slot`` is the lane's stable first-seen ordinal (the console lays lanes out
    on it); ``lines`` counts trace lines emitted so far; ``done`` flips once on
    the probe's ``stop`` so a duplicate ``stop`` never emits a second
    ``agent_done``. Mutated only under :attr:`RunEventStreamer._lock`.
    """

    slot: int
    lines: int = 0
    done: bool = False

# A probe brief (the ``start`` event ``detail``) can be the whole task block —
# we surface only its first substantive line as the lane's opening trace line so
# the console shows the pathway/sub-query, not the system-prompt boilerplate.
_PROBE_LINE_CAP = 160


class ProbeTrace:
    """Pure projection of a ``sub_agent`` event into trace fields (atomic, DRY).

    Both the LIVE streamer (:class:`RunEventStreamer`) and the settle-time
    :meth:`OrchestratedSearchRunner._build_trace` reconciliation render a
    ``sub_agent`` event the SAME way through these statics — so a reconciled
    lane is byte-identical to the one that streamed live, and the
    "every lane shows just the header + completed" projection bug is fixed in
    one place: a ``start`` carries the probe's pathway/sub-query brief (its first
    substantive line), a ``stop`` carries the real outcome detail rather than a
    bare ``done_reason``.
    """

    @staticmethod
    def lane_name(payload: dict[str, Any]) -> str:
        """The lane's display name — the probe's agent kind (e.g. ``scg-path-probe``)."""
        return str(payload.get("model") or "scg-path-probe")

    @staticmethod
    def source_id(payload: dict[str, Any]) -> str:
        """The spawning parent's id (the lane's grouping key in the console)."""
        return str(payload.get("parent_id") or "")

    @staticmethod
    def line(payload: dict[str, Any]) -> TraceLine:
        """Render one ``sub_agent`` lifecycle event into a :class:`TraceLine`.

        A ``start`` brief is condensed to its first substantive line (the
        pathway/sub-query), so the lane opens with the probe's actual target
        rather than the multi-line task header; a ``stop`` keeps its real
        outcome detail; intermediate ``message`` lines pass through verbatim.
        """
        action = str(payload.get("action") or "")
        raw = str(payload.get("detail") or action)
        is_stop = action == "stop"
        text = ProbeTrace._first_substantive(raw) if action == "start" else raw
        return TraceLine(
            t_ms=0,
            glyph="✓" if is_stop else "·",
            text=text,
            done=is_stop,
        )

    @staticmethod
    def _first_substantive(brief: str) -> str:
        """First non-empty line of a probe brief, capped — the pathway/sub-query."""
        for line in brief.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped[:_PROBE_LINE_CAP]
        return (brief.strip() or "probe")[:_PROBE_LINE_CAP]


class RunEventStreamer:
    """Live transcript→run-event projector for one search/structured run.

    One instance per drive. Holds the run id + store + the per-agent streaming
    state (which probes have opened a lane, how many lines each has emitted) so
    the live consumer and the settle reconciliation agree on what is already on
    the wire. Subscribe → :meth:`start` the consumer thread → drive → settle →
    :meth:`stop`. Thread-safe: a single lock guards the per-agent state shared
    between the consumer thread and the settling worker thread.
    """

    def __init__(self, *, run_id: str, store: Any, bus: SessionEventBus) -> None:
        """Bind the run + store + the (already-resolved) ``SessionEventBus``.

        ``store`` is the agentic-search run store (typed ``Any`` only because the
        dual JSON/Mongo base is injected by the caller); ``bus`` is the core
        per-session pub/sub the SideStage streaming seam already uses.
        """
        self._run_id = run_id
        self._store = store
        self._bus = bus
        self._lock = threading.Lock()
        self._agents: dict[str, _LaneState] = {}
        self._order: list[str] = []
        self._subscription: Subscription | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def subscribe(self, session_id: str) -> None:
        """Subscribe to *session_id* BEFORE the drive so no early event is missed.

        Subscribing up-front (not at consume time) closes the race where a fast
        first probe spawns before the consumer thread is scheduled.
        """
        self._subscription = self._bus.subscribe(session_id)

    def start(self) -> None:
        """Spin up the daemon consumer thread draining the subscription."""
        if self._subscription is None:
            return
        self._thread = threading.Thread(
            target=self._consume, name=f"run-streamer-{self._run_id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the consumer to drain-and-exit; join briefly.

        Called after the drive returns (the transcript is complete). The
        consumer drains whatever is still queued, then exits on the next empty
        poll — so a probe event that landed between the last drain and the drive
        return is still projected before settle reconciles.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._subscription is not None:
            try:
                self._bus.unsubscribe(self._subscription.session_id, self._subscription)
            except Exception as exc:  # noqa: BLE001 — best-effort teardown
                logging.debug("run streamer unsubscribe failed: {}", exc)

    # -- live consume ------------------------------------------------------

    def _consume(self) -> None:
        """Drain the subscription queue, projecting each event until stopped.

        Blocks on ``queue.get(timeout=...)`` so a published event wakes it
        immediately (no busy poll). Exits once :meth:`stop` is signalled AND the
        queue is drained — guaranteeing the tail events are projected.
        """
        import queue as _queue

        sub = self._subscription
        if sub is None:
            return
        while True:
            try:
                record = sub.queue.get(timeout=0.2)
            except _queue.Empty:
                if self._stop.is_set():
                    return
                continue
            try:
                self._project(record)
            except Exception as exc:  # noqa: BLE001 — a bad event never stalls the stream
                logging.debug("run streamer projection failed: {}", exc)

    def _project(self, record: EventRecord) -> None:
        """Project one transcript event onto the run event log (live)."""
        if record.get("type") != "sub_agent":
            return
        raw = record.get("payload")
        payload: dict[str, Any] = raw if isinstance(raw, dict) else {}
        agent_id = str(payload.get("agent_id") or "")
        if not agent_id:
            return
        is_stop = str(payload.get("action") or "") == "stop"

        with self._lock:
            state = self._agents.get(agent_id)
            opened = state is not None
            if state is None:
                state = _LaneState(slot=len(self._order))
                self._order.append(agent_id)
                self._agents[agent_id] = state
            already_done = state.done

        if not opened:
            self._store.append_run_event(
                self._run_id,
                events.agent_start(
                    agent_id=agent_id,
                    source_id=ProbeTrace.source_id(payload),
                    name=ProbeTrace.lane_name(payload),
                    slot=state.slot,
                ),
            )

        # Every lifecycle line becomes a trace line; ``stop`` marks it done.
        self._store.append_run_event(
            self._run_id,
            events.agent_line(agent_id=agent_id, line=ProbeTrace.line(payload)),
        )
        with self._lock:
            state.lines += 1
            if is_stop and not already_done:
                state.done = True

        if is_stop and not already_done:
            self._store.append_run_event(
                self._run_id,
                events.agent_done(agent_id=agent_id, results_count=0, empty=False),
            )

    # -- settle reconciliation --------------------------------------------

    def streamed_agent_ids(self) -> set[str]:
        """The agent ids that have already been opened on the wire (for settle)."""
        with self._lock:
            return set(self._agents)

    def reconcile_missing(self, trace: list[TraceAgent]) -> None:
        """Flush any trace agent the live stream did not already emit.

        The settle path builds the full trace from the finished transcript; this
        back-stops a fast run whose ``sub_agent`` events were never drained
        live (the ``completion`` landed first) or a bus drop. Each missing agent
        gets the full ``agent_start`` → ``agent_line*`` → ``agent_done`` it would
        have streamed, so the snapshot is always complete even if the live path
        was bypassed. Already-streamed agents are left untouched — no duplicates.
        """
        streamed = self.streamed_agent_ids()
        for agent in trace:
            if agent.agent_id in streamed:
                continue
            self._store.append_run_event(
                self._run_id,
                events.agent_start(
                    agent_id=agent.agent_id,
                    source_id=agent.source_id,
                    name=agent.name,
                    slot=agent.slot,
                ),
            )
            for line in agent.lines:
                self._store.append_run_event(
                    self._run_id,
                    events.agent_line(agent_id=agent.agent_id, line=line),
                )
            self._store.append_run_event(
                self._run_id,
                events.agent_done(
                    agent_id=agent.agent_id, results_count=0, empty=not agent.lines
                ),
            )


__all__ = ["RunEventStreamer"]
