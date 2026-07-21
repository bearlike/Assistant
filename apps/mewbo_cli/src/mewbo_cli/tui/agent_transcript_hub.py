#!/usr/bin/env python3
"""AgentTranscriptHub — the single, order-preserving transcript source.

Mewbo core emits ONE shared, agent-tagged event stream: every event carries
``agent_id`` / ``depth`` (and ``parent_id`` on ``sub_agent`` lifecycle events).
Sub-agents are NOT isolated — they reuse the parent's ``event_logger`` and
``HookManager`` — so ALL agents' events reach the same
:class:`~mewbo_core.session_event_bus.SessionEventBus`. This hub subscribes once
to that bus (plus the per-run ``pre_tool_use`` hook the FleetBridge already uses,
for the tool-start timestamp) and demultiplexes the stream by ``agent_id`` into a
``dict[agent_id -> AgentTranscript]``.

The invariant this class exists to uphold: **within one agent's transcript,
rendered order equals core emission order** — assistant text, tool-call start,
tool-result, and sub-agent spawns interleave in EXACT arrival order, never
bucketed by type. Each :class:`AgentTranscript` is a strictly-ordered append log:

- ``agent_message_delta`` events coalesce into one streamed :class:`TextSpan`
  per step (consecutive deltas merge; a tool/spawn closes the span).
- A tool is ONE mutable :class:`ToolCall` keyed by ``call_key`` — created
  ``running`` on ``pre_tool_use`` (with a start timestamp) and mutated in place
  to ``done``/``error`` (with elapsed) when its ``tool_result`` event arrives.
- ``sub_agent`` lifecycle events append a :class:`Spawn` marker to the PARENT's
  log at arrival position and roll up the child's tokens/status.

The hub is UI-agnostic: state lives here (the testable source of truth); a thin
injected :class:`RootSink` drives the root (``depth == 0``) transcript into the
live ``TranscriptView``. With no sink it is pure state — which is exactly what
the ordering regression test asserts against.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from mewbo_cli.cli_notices import derive_task_outcome
from mewbo_cli.tui.seams import TranscriptItem
from mewbo_cli.tui.status.throughput_meter import ThroughputMeter, ThroughputState
from mewbo_cli.tui.turn_engine import build_tool_payload
from mewbo_cli.tui.widgets.todo_panel import TodoItem, TodoState

# Tool ids whose calls are NOT rendered as transcript cards or counted as tools:
# ``update_todos`` is a meta/progress tool whose authoritative output is the Plan
# dock (the ``todos`` event), so a settled "✓ update_todos" card each call would
# be pure clutter (the agent calls it frequently).
_SUPPRESSED_CARD_TOOLS = frozenset({"update_todos"})

# Map the ``todos`` event's status vocabulary (pending|in_progress|completed) onto
# the TodoPanel's tri-state (pending|in_progress|done). ``completed`` → ``done``.
_TODO_EVENT_TO_PANEL = {"pending": "pending", "in_progress": "in_progress", "completed": "done"}


def _todo_state_from_event_items(items: list[Any]) -> TodoState:
    """Project ``todos``-event items into a :class:`TodoState` for the dock."""
    rows: list[TodoItem] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label", "") or "").strip()
        if not label:
            continue
        status = _TODO_EVENT_TO_PANEL.get(str(entry.get("status", "")), "pending")
        rows.append(TodoItem(label=label, state=status))
    return TodoState.from_items(rows)

# ---------------------------------------------------------------------------
# Ordered log entries (the per-agent transcript is a list of these)
# ---------------------------------------------------------------------------


@dataclass
class TextSpan:
    """Coalesced assistant text for one ``(agent, step)`` — fed by deltas."""

    kind: ClassVar[str] = "text"
    span_id: str
    agent_id: str
    depth: int
    step: int
    text: str = ""


@dataclass
class ToolCall:
    """One tool call, rendered as a single mutable card (running → settled)."""

    kind: ClassVar[str] = "tool"
    call_key: str
    tool_id: str
    operation: str | None
    agent_id: str
    depth: int
    status: str  # "running" | "done" | "error"
    started_at: float
    tool_input: Any = None
    result: Any = None
    is_mcp: bool = False
    elapsed: float | None = None


@dataclass
class Spawn:
    """A sub-agent spawn marker, recorded in the PARENT's ordered log."""

    kind: ClassVar[str] = "spawn"
    child_id: str
    parent_id: str | None
    depth: int
    agent_type: str | None = None
    status: str = "running"


@dataclass
class AgentTranscript:
    """One agent's strictly-ordered append log + live rollups."""

    agent_id: str
    parent_id: str | None = None
    depth: int = 0
    model: str | None = None
    status: str = "running"
    entries: list[Any] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    # The size of the MOST RECENT prompt (``llm_call_end``'s plain, per-call
    # ``input_tokens`` — never the cumulative counter above) — what the model
    # currently has in its context window. This is the number the context-%
    # gauge needs; ``input_tokens`` (cumulative) is for cost/billing rollups.
    last_input_tokens: int = 0
    tool_count: int = 0
    # The spawned AgentDef name (e.g. ``scg-path-probe``) — only children carry
    # one; ``None`` for the root or an ad-hoc spawn. Drives the fleet row label.
    agent_type: str | None = None
    # Lifecycle timestamps (monotonic): ``started_at`` stamped first-seen,
    # ``stopped_at`` on the terminal transition — together they give elapsed.
    started_at: float = 0.0
    stopped_at: float | None = None
    # The authoritative live todo list this agent last emitted (``todos`` event);
    # None until it calls ``update_todos`` (todos are never fabricated).
    todos: TodoState | None = None
    # Live phase / tok-s / stall meter, fed from the ingest points. Set
    # once in ``_get`` from the hub's meter factory.
    meter: ThroughputMeter | None = None
    # internal: the currently-open text span (None when no span is streaming)
    open_span: TextSpan | None = None
    # internal: running tool cards awaiting their result, keyed by call_key
    running: dict[str, list[ToolCall]] = field(default_factory=dict)


# Terminal-state set (mirrors the hypervisor's 4 terminal states).
_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "rejected"})


@dataclass(frozen=True)
class FleetRow:
    """A one-glance fleet summary projected from an :class:`AgentTranscript`.

    Pure data read straight off the hub's rollups (never recomputed): the
    fleet panel renders ``<glyph> <label> · <model> · <N tools> · <elapsed> ·
    <in→out>`` from these fields and computes *elapsed* at render time from
    ``started_at`` / ``stopped_at`` (the only time-relative facet).
    """

    agent_id: str
    parent_id: str | None
    depth: int
    model: str | None
    status: str
    tool_count: int
    input_tokens: int
    output_tokens: int
    started_at: float
    stopped_at: float | None
    agent_type: str | None
    is_root: bool
    # Live throughput snapshot (phase / tok-s / stall) for this agent, or None
    # when the agent has no meter yet. The fleet row renders a terse facet from
    # it — chiefly a STALL, the sub-agent-hang case.
    throughput: ThroughputState | None = None

    @property
    def tokens(self) -> int:
        """Total tokens (input + output) — the REAL rollup, never a placeholder."""
        return max(0, self.input_tokens) + max(0, self.output_tokens)

    @property
    def label(self) -> str:
        """The human label: ``root`` for depth 0, else the agent type / id."""
        if self.is_root or self.depth == 0:
            return "root"
        return self.agent_type or self.agent_id[:8]


# ---------------------------------------------------------------------------
# Root sink — the live rendering seam (depth == 0 only)
# ---------------------------------------------------------------------------


class RootSink(Protocol):
    """Drives root (``depth == 0``) transcript changes into the live UI.

    Every method runs on the engine worker thread; an implementation backed by
    the ``TranscriptView`` marshals to the UI thread via ``call_from_thread``.
    A misbehaving sink must never break ingest — the hub guards each call.
    """

    def stream_delta(self, stream_id: str, delta: str) -> None:
        """Append a streamed-markdown delta (opens the slot on first call)."""
        ...

    def stream_end(self, stream_id: str) -> None:
        """Finalise a streaming slot."""
        ...

    def upsert_tool(self, card_id: str, item: TranscriptItem) -> None:
        """Mount-or-update a tool card keyed by ``card_id`` (in place)."""
        ...

    def spawn(self, item: TranscriptItem) -> None:
        """Append a sub-agent spawn marker."""
        ...

    def set_status(self, label: str) -> None:
        """Update the foot activity label to reflect the live step."""
        ...


class _NullSink:
    """No-op sink: the hub is pure state (the unit-test default)."""

    def stream_delta(self, stream_id: str, delta: str) -> None:
        """Discard the delta."""

    def stream_end(self, stream_id: str) -> None:
        """Discard the close."""

    def upsert_tool(self, card_id: str, item: TranscriptItem) -> None:
        """Discard the tool card."""

    def spawn(self, item: TranscriptItem) -> None:
        """Discard the spawn marker."""

    def set_status(self, label: str) -> None:
        """Discard the status label."""


# ---------------------------------------------------------------------------
# The hub
# ---------------------------------------------------------------------------


class AgentTranscriptHub:
    """Demultiplex the shared event stream into per-agent ordered transcripts.

    Atomic class: state attrs + ingest methods + DI (the sink + clock). All
    ingest happens on the engine worker thread (the bus observer and the
    ``pre_tool_use`` hook both fire there, synchronously), so arrival order
    equals core emission order; a single lock guards the bookkeeping so the UI
    thread can read snapshots safely.

    **Deferred-sink law.** The sink is NEVER invoked while ``_lock`` is held: a
    sink method marshals to the UI thread (``call_from_thread``) and the UI
    callback re-enters the hub's read API (``root_activity_label`` acquires
    ``_lock``) — calling it under the lock cross-thread self-deadlocks (``_lock``
    is an ``RLock``, so per-thread reentrancy does not help). Ingest therefore
    COLLECTS sink actions into a FIFO under ``_lock`` and FLUSHES them after the
    lock is released; a single-drainer guard keeps delivery in strict enqueue
    order even under concurrent ``observe``. See ``_enqueue_sink``/``_flush_sink``.
    """

    def __init__(
        self,
        *,
        sink: RootSink | None = None,
        clock: Callable[[], float] = time.monotonic,
        meter_factory: Callable[[], ThroughputMeter] | None = None,
    ) -> None:
        """Bind the optional live :class:`RootSink`, clock, and meter factory.

        ``meter_factory`` builds one :class:`ThroughputMeter` per agent; the
        default threads the hub's own ``clock`` into each meter so a ``snapshot``
        taken without an explicit ``now`` shares the hub's time base (crucial for
        deterministic tests under a fake clock).
        """
        self._lock = threading.RLock()
        self._sink: RootSink = sink or _NullSink()
        self._clock = clock
        self._meter_factory = meter_factory or (lambda: ThroughputMeter(clock=clock))
        self._agents: dict[str, AgentTranscript] = {}
        self._active_session: str | None = None
        self._current_agent_id: str | None = None
        self._current_depth: int = 0
        self._root_id: str | None = None
        # The most recent authoritative todo list (any agent) — the Plan dock's
        # source. Re-emit-each-update means the latest event is the whole truth.
        self._latest_todos: TodoState | None = None
        # call_key -> FIFO of pending start timestamps (from pre_tool_use), popped
        # when the matching tool_result arrives to compute elapsed.
        self._pending_starts: dict[str, list[float]] = {}
        # Deferred-sink machinery (the deadlock law, see the class docstring):
        # ingest COLLECTS sink actions into this FIFO under ``_lock`` and flushes
        # them AFTER the lock is released. ``_drain_lock`` guards BOTH the queue
        # and the single-drainer ``_draining`` flag, so actions reach the sink in
        # strict enqueue order even under concurrent ``observe`` (lock order is
        # always ``_lock`` → ``_drain_lock``; the drainer takes ``_drain_lock``
        # alone and never ``_lock``, so there is no cycle).
        self._sink_queue: deque[Callable[[], None]] = deque()
        self._drain_lock = threading.Lock()
        self._draining = False

    # -- public read API --------------------------------------------------

    def set_active_session(self, session_id: str | None) -> None:
        """Scope the bus observer to ``session_id`` (others are ignored)."""
        with self._lock:
            self._active_session = session_id

    def transcript(self, agent_id: str) -> AgentTranscript | None:
        """Return the ordered transcript for ``agent_id`` (or ``None``)."""
        with self._lock:
            return self._agents.get(agent_id)

    def agents(self) -> list[AgentTranscript]:
        """Return all tracked transcripts (insertion order)."""
        with self._lock:
            return list(self._agents.values())

    def fleet_rows(self) -> list[FleetRow]:
        """Project every tracked agent into a :class:`FleetRow` (insertion order).

        The fleet panel's data source: one immutable summary per agent (root +
        every sub-agent), read straight off the per-agent rollups so nothing is
        recomputed in the UI.
        """
        with self._lock:
            return [
                FleetRow(
                    agent_id=a.agent_id,
                    parent_id=a.parent_id,
                    depth=a.depth,
                    model=a.model,
                    status=a.status,
                    tool_count=a.tool_count,
                    input_tokens=a.input_tokens,
                    output_tokens=a.output_tokens,
                    started_at=a.started_at,
                    stopped_at=a.stopped_at,
                    agent_type=a.agent_type,
                    is_root=a.agent_id == self._root_id,
                    throughput=(
                        a.meter.snapshot()
                        if a.meter is not None and a.status == "running"
                        else None
                    ),
                )
                for a in self._agents.values()
            ]

    def root_todos(self) -> TodoState:
        """The authoritative live todo list for the Plan dock (never fabricated).

        Returns the most recent ``todos`` event's list (any agent — in practice
        the root, the only agent granted ``update_todos``); an empty
        :class:`TodoState` when nothing has been emitted yet.
        """
        with self._lock:
            return self._latest_todos or TodoState()

    def root_last_input_tokens(self) -> int:
        """The root's live context size (the last prompt's ``input_tokens``).

        This is what the ctx-% gauge should show: how full the model's context
        window is RIGHT NOW, not how many tokens the session has billed in
        total. Mirrors core's ``token_budget.read_last_input_tokens``. ``0``
        when the root has not completed an ``llm_call_end`` yet.
        """
        with self._lock:
            root = self._agents.get(self._root_id) if self._root_id else None
            return root.last_input_tokens if root is not None else 0

    def root_activity_label(self) -> str:
        """The live foot-activity label from the ROOT meter (``streaming ↓… tok/s``).

        Pulled on the transcript's 10 Hz spinner tick so the phase/rate/stall
        stay live even when no new event arrives (a hung agent). Falls back to
        ``"working"`` when there is no root meter yet.
        """
        with self._lock:
            root = self._agents.get(self._root_id) if self._root_id else None
            meter = root.meter if root is not None else None
            if meter is None:
                return "working"
            return ThroughputMeter.format_label(meter.snapshot())

    def throughput_for(self, agent_id: str) -> ThroughputState | None:
        """The live :class:`ThroughputState` for ``agent_id`` (or ``None``)."""
        with self._lock:
            agent = self._agents.get(agent_id)
            meter = agent.meter if agent is not None else None
            return meter.snapshot() if meter is not None else None

    def items_for(self, agent_id: str) -> list[TranscriptItem]:
        """Project one agent's ordered log into renderable :class:`TranscriptItem`s.

        The drill-in view feeds these straight to the shared transcript
        renderers (the same ones the live root uses), so a child's history
        renders identically to the root — text, settled tool cards, and spawn
        markers in EXACT emission order.
        """
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                return []
            items: list[TranscriptItem] = []
            for entry in agent.entries:
                if isinstance(entry, TextSpan):
                    if entry.text:
                        items.append(
                            TranscriptItem("assistant", {"text": entry.text, "depth": entry.depth})
                        )
                elif isinstance(entry, ToolCall):
                    items.append(self._tool_item(entry))
                elif isinstance(entry, Spawn):
                    label = entry.agent_type or entry.child_id
                    items.append(
                        TranscriptItem(
                            "notice",
                            {"text": f"⇣ spawned agent {label}", "depth": entry.depth + 1},
                        )
                    )
            return items

    # -- bus observer -----------------------------------------------------

    def observe(self, session_id: str, record: dict[str, Any]) -> None:
        """Ingest one appended event from the shared :class:`SessionEventBus`.

        Registered once via ``bus.register_observer``; filters to the active
        session and dispatches by event ``type``. Best-effort — never raises
        into the publish hot path.
        """
        try:
            with self._lock:
                self._dispatch(session_id, record)
        except Exception:  # noqa: BLE001 — ingest must never break the publish path
            pass
        # Sink actions collected under ``_lock`` are flushed HERE, after the lock
        # is released — never invoke the sink under ``_lock`` (the deadlock law).
        self._flush_sink()

    def _dispatch(self, session_id: str, record: dict[str, Any]) -> None:
        """Route one event to its per-type handler (caller holds ``_lock``)."""
        if self._active_session is not None and session_id != self._active_session:
            return
        etype = record.get("type")
        payload = record.get("payload") or {}
        if etype == "agent_message_delta":
            self._on_delta(payload)
        elif etype == "agent_message":
            self._on_message(payload)
        elif etype == "llm_call_start":
            self._on_llm_start(payload)
        elif etype == "llm_call_end":
            self._on_llm_end(payload)
        elif etype == "tool_result":
            self._on_tool_result(payload)
        elif etype == "sub_agent":
            self._on_sub_agent(payload)
        elif etype == "todos":
            self._on_todos(payload)
        elif etype == "completion":
            self._on_completion(payload)

    # -- pre_tool_use hook (the FleetBridge seam) -------------------------

    def tool_started(self, action_step: Any) -> Any:
        """Record a tool START (``pre_tool_use``): stamp the time + running card.

        Attributed to the *current* agent (the last to emit a delta / llm_call /
        result) since ``pre_tool_use`` carries no ``agent_id``. Stamps a start
        timestamp keyed by ``call_key`` so the matching ``tool_result`` can
        compute elapsed, and — for the root — renders an in-place ``running``
        card that the result mutates to settled. Returns the step unchanged so
        it composes as a hook.
        """
        try:
            with self._lock:
                self._ingest_tool_started(action_step)
        except Exception:  # noqa: BLE001 — a tracking hook must never break a run
            pass
        # Flush OFF the lock, on every path (incl. the early ``agent_id is None``
        # return, which still enqueued a ``set_status``) — the deadlock law.
        self._flush_sink()
        return action_step

    def _ingest_tool_started(self, action_step: Any) -> None:
        """Record a tool START into state (caller holds ``_lock``)."""
        tool_id = getattr(action_step, "tool_id", "") or ""
        # ``update_todos`` is a meta/progress tool — its output is the Plan dock
        # (the ``todos`` event), never a transcript card. Skip it entirely so it
        # makes no card, no tool-count, no tool phase.
        if tool_id in _SUPPRESSED_CARD_TOOLS:
            return
        tool_input = getattr(action_step, "tool_input", None)
        call_key = self._call_key(tool_id, tool_input)
        self._pending_starts.setdefault(call_key, []).append(self._clock())
        self._sink_set_status(f"running {tool_id}" if tool_id else "running tool")
        agent_id = self._current_agent_id
        if agent_id is None:
            return
        agent = self._get(agent_id, self._current_depth)
        if agent.meter is not None:
            agent.meter.mark_tool_start(tool_id, now=self._clock())
        self._close_span(agent)
        entry = ToolCall(
            call_key=call_key,
            tool_id=tool_id,
            operation=getattr(action_step, "operation", None),
            agent_id=agent_id,
            depth=agent.depth,
            status="running",
            started_at=self._clock(),
            tool_input=tool_input,
        )
        agent.entries.append(entry)
        agent.tool_count += 1
        agent.running.setdefault(call_key, []).append(entry)
        if agent.depth == 0:
            self._sink_upsert_tool(entry)

    # -- per-type ingest (lock held by callers) ---------------------------

    def _on_llm_start(self, payload: dict[str, Any]) -> None:
        agent_id = payload.get("agent_id")
        if not isinstance(agent_id, str):
            return
        depth = int(payload.get("depth", 0) or 0)
        agent = self._get(agent_id, depth)
        model = payload.get("model")
        if isinstance(model, str):
            agent.model = model
        self._set_current(agent_id, depth)
        if agent.meter is not None:
            agent.meter.mark_llm_start(agent.model, now=self._clock())
        self._sink_set_status("thinking")

    def _on_llm_end(self, payload: dict[str, Any]) -> None:
        # Roll up per-agent token totals from the CUMULATIVE counts the loop
        # stamps on each ``llm_call_end`` (authoritative for every agent incl.
        # the root, which has no ``sub_agent`` event to carry tokens).
        agent_id = payload.get("agent_id")
        if not isinstance(agent_id, str):
            return
        depth = int(payload.get("depth", 0) or 0)
        agent = self._get(agent_id, depth)
        cum_in = payload.get("cumulative_input_tokens")
        cum_out = payload.get("cumulative_output_tokens")
        if isinstance(cum_in, (int, float)):
            agent.input_tokens = max(agent.input_tokens, int(cum_in))
        if isinstance(cum_out, (int, float)):
            agent.output_tokens = max(agent.output_tokens, int(cum_out))
        # Plain (non-cumulative) per-call size — REPLACE, never max: unlike the
        # cumulative billing counter this can legitimately shrink (a compaction
        # resets what the next prompt carries), so the latest call always wins.
        last_in = payload.get("input_tokens")
        if isinstance(last_in, (int, float)):
            agent.last_input_tokens = int(last_in)
        model = payload.get("model")
        if isinstance(model, str):
            agent.model = model
        # Reconcile the live len/4 estimate to the authoritative per-call output.
        if agent.meter is not None:
            out = payload.get("output_tokens")
            agent.meter.mark_llm_end(
                output_tokens=int(out) if isinstance(out, (int, float)) else None,
                now=self._clock(),
            )

    def _on_delta(self, payload: dict[str, Any]) -> None:
        agent_id = payload.get("agent_id")
        text = payload.get("text")
        if not isinstance(agent_id, str) or not text:
            return
        depth = int(payload.get("depth", 0) or 0)
        step = int(payload.get("step", 0) or 0)
        agent = self._get(agent_id, depth)
        self._set_current(agent_id, depth)
        if agent.meter is not None:
            agent.meter.mark_delta(str(text), now=self._clock())
        span = agent.open_span
        if span is not None and span.step != step:
            self._close_span(agent)
            span = None
        if span is None:
            span = TextSpan(
                span_id=f"{agent_id}:{step}", agent_id=agent_id, depth=depth, step=step
            )
            agent.entries.append(span)
            agent.open_span = span
        span.text += str(text)
        if depth == 0:
            self._sink_stream_delta(span.span_id, str(text))

    def _on_message(self, payload: dict[str, Any]) -> None:
        # The per-step assistant text. If deltas streamed it, close the span
        # (commit). If NONE streamed (a non-streaming model), render the full
        # text now via the same span path so the assistant turn still appears.
        agent_id = payload.get("agent_id")
        if not isinstance(agent_id, str):
            return
        depth = int(payload.get("depth", 0) or 0)
        agent = self._get(agent_id, depth)
        self._set_current(agent_id, depth)
        if agent.open_span is None:
            text = payload.get("text")
            if text:
                self._on_delta(
                    {"agent_id": agent_id, "depth": depth, "step": -1, "text": text}
                )
        self._close_span(agent)

    def _on_tool_result(self, payload: dict[str, Any]) -> None:
        agent_id = payload.get("agent_id")
        if not isinstance(agent_id, str):
            return
        depth = int(payload.get("depth", 0) or 0)
        tool_id = str(payload.get("tool_id", "") or "")
        # ``update_todos`` never renders a card (its output is the Plan dock) and
        # never entered a tool phase in ``tool_started`` — skip it symmetrically.
        if tool_id in _SUPPRESSED_CARD_TOOLS:
            return
        tool_input = payload.get("tool_input")
        call_key = self._call_key(tool_id, tool_input)
        starts = self._pending_starts.get(call_key)
        start_ts = starts.pop(0) if starts else None
        elapsed = (self._clock() - start_ts) if start_ts is not None else None

        agent = self._get(agent_id, depth)
        self._set_current(agent_id, depth)
        if agent.meter is not None:
            agent.meter.mark_tool_end(now=self._clock())
        self._close_span(agent)
        success = bool(payload.get("success", True))
        status = "done" if success else "error"
        result = payload.get("result")
        if not result and payload.get("error"):
            result = payload.get("error")

        running = agent.running.get(call_key)
        entry = running.pop(0) if running else None
        if entry is None:
            # No running card (error path with no pre_tool_use, or a tool first
            # seen at its result) — append a freshly-settled card in place.
            entry = ToolCall(
                call_key=call_key,
                tool_id=tool_id,
                operation=payload.get("operation"),
                agent_id=agent_id,
                depth=depth,
                status=status,
                started_at=start_ts if start_ts is not None else self._clock(),
                tool_input=tool_input,
            )
            agent.entries.append(entry)
            agent.tool_count += 1
        entry.status = status
        entry.result = result
        entry.elapsed = elapsed
        entry.is_mcp = bool(payload.get("is_mcp", entry.is_mcp))
        if depth == 0:
            self._sink_upsert_tool(entry)

    def _on_sub_agent(self, payload: dict[str, Any]) -> None:
        child_id = payload.get("agent_id")
        if not isinstance(child_id, str):
            return
        depth = int(payload.get("depth", 0) or 0)
        parent_id = payload.get("parent_id")
        parent_id = parent_id if isinstance(parent_id, str) else None
        is_new = child_id not in self._agents
        child = self._get(child_id, depth)
        child.parent_id = parent_id or child.parent_id
        model = payload.get("model")
        if isinstance(model, str):
            child.model = model
        status = payload.get("status")
        if isinstance(status, str):
            child.status = status
            if status in _TERMINAL_STATES and child.stopped_at is None:
                child.stopped_at = self._clock()
        agent_type = payload.get("agent_type")
        if isinstance(agent_type, str) and not child.agent_type:
            child.agent_type = agent_type
        child.input_tokens = int(payload.get("input_tokens", child.input_tokens) or 0)
        child.output_tokens = int(payload.get("output_tokens", child.output_tokens) or 0)

        if is_new:
            agent_type = payload.get("agent_type")
            marker = Spawn(
                child_id=child_id,
                parent_id=parent_id,
                depth=depth,
                agent_type=agent_type if isinstance(agent_type, str) else None,
                status=child.status,
            )
            target = self._agents.get(parent_id) if parent_id else None
            if target is None and self._current_agent_id:
                target = self._agents.get(self._current_agent_id)
            if target is not None:
                self._close_span(target)
                target.entries.append(marker)
                if target.depth == 0:
                    self._sink_spawn(marker)

    def _on_todos(self, payload: dict[str, Any]) -> None:
        # The authoritative live todo list (``update_todos`` → ``todos`` event).
        # Re-emitted in FULL each call, so the latest event fully replaces the
        # displayed list — store it per-agent AND as the dock's latest source.
        items = payload.get("items")
        if not isinstance(items, list):
            return
        state = _todo_state_from_event_items(items)
        agent_id = payload.get("agent_id")
        if isinstance(agent_id, str):
            agent = self._get(agent_id, int(payload.get("depth", 0) or 0))
            agent.todos = state
        self._latest_todos = state

    def _on_completion(self, payload: dict[str, Any]) -> None:
        # Close any open root span and mark the root settled. Fallback: if the
        # root produced NO assistant text this turn but a task_result exists
        # (e.g. a model that never streamed and emitted no agent_message), render
        # it so the turn is never silently empty.
        root_id = self._root_id
        if root_id is None:
            return
        root = self._agents.get(root_id)
        if root is None:
            return
        done_reason = payload.get("done_reason")
        if isinstance(done_reason, str):
            # Honest derivation, not a passthrough: a run that hit an
            # unrecovered repo/network/permission/quota wall still completes
            # with ``done_reason == "completed"`` and carries the wall
            # separately as ``blocked_code`` — reading ``done_reason`` alone
            # rendered that as a green success. ``derive_task_outcome`` folds
            # in ``blocked_code`` and maps halt/verification-failure reasons
            # onto the same ``unmet_goal`` the console shows.
            root.status = derive_task_outcome(done_reason, payload.get("blocked_code"))
            # A completion event fires exactly once per turn's true end
            # (including a park like ``awaiting_approval``), so it always
            # marks this stamp — not just when the result happens to land in
            # the hypervisor's 4-state vocabulary (``_TERMINAL_STATES``),
            # which ``root.status`` no longer speaks now that it carries the
            # session-status vocabulary instead.
            if root.stopped_at is None:
                root.stopped_at = self._clock()
        if root.open_span is None and not any(
            isinstance(e, TextSpan) for e in root.entries
        ):
            task_result = payload.get("task_result")
            if task_result:
                self._on_delta(
                    {
                        "agent_id": root_id,
                        "depth": 0,
                        "step": -2,
                        "text": str(task_result),
                    }
                )
        self._close_span(root)

    # -- internals --------------------------------------------------------

    def _get(self, agent_id: str, depth: int) -> AgentTranscript:
        agent = self._agents.get(agent_id)
        if agent is None:
            agent = AgentTranscript(
                agent_id=agent_id,
                depth=depth,
                started_at=self._clock(),
                meter=self._meter_factory(),
            )
            self._agents[agent_id] = agent
        if depth == 0 and self._root_id is None:
            self._root_id = agent_id
        return agent

    def _set_current(self, agent_id: str, depth: int) -> None:
        self._current_agent_id = agent_id
        self._current_depth = depth
        if depth == 0:
            self._root_id = agent_id

    def _close_span(self, agent: AgentTranscript) -> None:
        span = agent.open_span
        if span is None:
            return
        agent.open_span = None
        if agent.depth == 0:
            self._sink_stream_end(span.span_id)

    @staticmethod
    def _call_key(tool_id: str, tool_input: Any) -> str:
        """A stable correlation key for a tool call (tool_id + its input).

        Correlates a ``pre_tool_use`` start with its later ``tool_result`` across
        the two seams without relying on object identity or arrival order — keyed
        by content so identical concurrent calls still FIFO-match cleanly.
        """
        try:
            if isinstance(tool_input, str):
                rendered = tool_input
            elif isinstance(tool_input, dict):
                rendered = json.dumps(tool_input, sort_keys=True, default=str)
            else:
                dumped = getattr(tool_input, "model_dump", None)
                if callable(dumped):
                    rendered = json.dumps(dumped(), sort_keys=True, default=str)
                else:
                    rendered = repr(tool_input)
        except Exception:  # noqa: BLE001 — key derivation must never raise
            rendered = repr(tool_input)
        return f"{tool_id}::{rendered}"

    def _tool_item(self, entry: ToolCall) -> TranscriptItem:
        """Project a :class:`ToolCall` to a ``tool`` TranscriptItem for rendering."""
        payload = build_tool_payload(
            tool_id=entry.tool_id,
            operation=entry.operation,
            tool_input=entry.tool_input,
            result=entry.result,
            is_mcp=entry.is_mcp,
        )
        payload["status"] = entry.status
        payload["depth"] = entry.depth
        if entry.elapsed is not None:
            payload["elapsed"] = entry.elapsed
        return TranscriptItem("tool", payload)

    @staticmethod
    def _card_id(entry: ToolCall) -> str:
        return f"c{id(entry)}"

    # -- deferred sink calls ---------------------------------------------
    #
    # Each ``_sink_*`` helper runs UNDER ``_lock`` (ingest holds it), so it may
    # NOT touch the sink directly — it SNAPSHOTS its render payload now (while
    # the mutable state is coherent, e.g. a ``ToolCall`` before it settles) and
    # enqueues a zero-arg closure. ``_flush_sink`` (called off the lock) delivers
    # them. See the class docstring — the deadlock law.

    def _sink_stream_delta(self, stream_id: str, delta: str) -> None:
        self._enqueue_sink(lambda: self._sink.stream_delta(stream_id, delta))

    def _sink_stream_end(self, stream_id: str) -> None:
        self._enqueue_sink(lambda: self._sink.stream_end(stream_id))

    def _sink_upsert_tool(self, entry: ToolCall) -> None:
        # Snapshot the card NOW: the entry is mutated in place (running→settled),
        # so a deferred re-read would deliver the wrong status.
        card_id = self._card_id(entry)
        item = self._tool_item(entry)
        self._enqueue_sink(lambda: self._sink.upsert_tool(card_id, item))

    def _sink_spawn(self, marker: Spawn) -> None:
        label = marker.agent_type or marker.child_id
        item = TranscriptItem(
            "notice", {"text": f"⇣ spawned agent {label}", "depth": marker.depth + 1}
        )
        self._enqueue_sink(lambda: self._sink.spawn(item))

    def _sink_set_status(self, label: str) -> None:
        self._enqueue_sink(lambda: self._sink.set_status(label))

    def _enqueue_sink(self, action: Callable[[], None]) -> None:
        """Queue ONE deferred sink action (caller holds ``_lock``).

        Appended under ``_drain_lock`` so the FIFO stays consistent with the
        drainer; lock order is always ``_lock`` → ``_drain_lock``.
        """
        with self._drain_lock:
            self._sink_queue.append(action)

    def _flush_sink(self) -> None:
        """Deliver queued sink actions in FIFO order — call OFF ``_lock``.

        Single-drainer: the first thread in becomes the drainer and delivers
        every queued action (including ones other threads enqueue meanwhile);
        concurrent callers see ``_draining`` and return, so actions always reach
        the sink in strict enqueue order. The sink is invoked with NO hub lock
        held, so a sink callback may freely re-enter the hub's read API. A
        misbehaving sink never breaks ingest (each call is guarded).
        """
        with self._drain_lock:
            if self._draining:
                return
            self._draining = True
        try:
            while True:
                with self._drain_lock:
                    if not self._sink_queue:
                        # Clear the flag ATOMICALLY with the empty check so a
                        # concurrent enqueue+flush can never be lost (it will
                        # either see the item here or become the next drainer).
                        self._draining = False
                        return
                    action = self._sink_queue.popleft()
                try:
                    action()
                except Exception:  # noqa: BLE001 — a misbehaving sink never breaks ingest
                    pass
        except BaseException:
            # Never leave the drainer flag stuck if something abnormal escapes.
            with self._drain_lock:
                self._draining = False
            raise


__all__ = [
    "AgentTranscript",
    "AgentTranscriptHub",
    "FleetRow",
    "RootSink",
    "Spawn",
    "TextSpan",
    "ToolCall",
]
