#!/usr/bin/env python3
"""ThroughputMeter — live phase + token-throughput for one agent.

One atomic class (mirrors :class:`ContextMeter`) that turns the event stream the
:class:`~mewbo_cli.tui.agent_transcript_hub.AgentTranscriptHub` ALREADY ingests
into the honest "what is this agent doing *right now*" signal the old 2-state
label (``thinking`` / ``running {tool}``) could not express:

- a **phase** — waiting for the first token (``uploading``), a reasoning model
  thinking with no deltas (``reasoning``), tokens flowing (``streaming``),
  a tool running (``running_tool``), idle, or **stalled** (nothing for a while),
- an **EWMA output tok/s** during streaming (live ``len/4`` estimate, the honest
  count is reconciled from the authoritative ``llm_call_end`` usage),
- **time-to-first-token** (TTFT) for the last call,
- a **stall timer** off a monotonic last-event timestamp — a hung agent and a
  fast-streaming one no longer look identical.

APP-LAYER, ZERO CORE CHANGE: the meter is fed from the hub's existing ingest
points (``_on_llm_start`` / ``_on_delta`` / ``tool_started`` / ``_on_tool_result``
/ ``_on_llm_end``); the client stamps arrival timestamps (``time.monotonic``), so
core emits nothing new. Pure state + methods + DI (clock + a reasoning-model
predicate) — trivially unit-testable with a fake clock.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


class Phase(str, Enum):
    """The mutually-exclusive activity phases the meter tracks."""

    IDLE = "idle"
    UPLOADING = "uploading"  # request sent, awaiting the first token (TTFT window)
    REASONING = "reasoning"  # awaiting first token on a reasoning model (legit silence)
    STREAMING = "streaming"  # output tokens flowing
    RUNNING_TOOL = "running_tool"  # a tool call is executing
    STALLED = "stalled"  # DERIVED at snapshot: no event for too long


# A char is ~4 tokens — the standard live estimate while streaming (the exact
# count is reconciled from ``llm_call_end`` usage).
_CHARS_PER_TOKEN = 4.0
# EWMA smoothing for the instantaneous tok/s (higher = more responsive).
_EWMA_ALPHA = 0.3
# A non-reasoning phase silent for this long reads as stalled. Reasoning phases
# never flip to stalled — a reasoning model legitimately emits nothing while it
# thinks; its rising ``phase_elapsed`` is the signal instead.
_STALL_AFTER_SECONDS = 12.0


def _default_is_reasoning_model(model: str | None) -> bool:
    """Best-effort: does ``model`` support reasoning (extended thinking)?

    Reuses core's classifier (down-only app→core import) so the "is this silence
    legitimate thinking?" decision is DRY with the rest of the engine. Never
    raises — an unknown model degrades to ``False`` (treated as a normal call).
    """
    try:
        from mewbo_core.llm import model_supports_reasoning_effort

        return bool(model_supports_reasoning_effort(model))
    except Exception:  # noqa: BLE001 — a classifier miss must never break the meter
        return False


@dataclass(frozen=True)
class ThroughputState:
    """A single computed snapshot of an agent's live throughput.

    ``phase`` already accounts for stalling (it is :attr:`Phase.STALLED` when the
    base phase has gone silent past the threshold). ``phase_elapsed`` is seconds
    in the current base phase; ``stall_seconds`` is seconds since the last event
    (meaningful when stalled).
    """

    phase: Phase
    tok_per_s: float
    ttft: float | None
    phase_elapsed: float
    stall_seconds: float
    tool_id: str | None
    reasoning: bool

    @property
    def is_active(self) -> bool:
        """Whether the agent is doing anything (not idle)."""
        return self.phase is not Phase.IDLE


class ThroughputMeter:
    """Track one agent's phase, output tok/s, TTFT and stall timer.

    All ``mark_*`` methods take an explicit ``now`` (monotonic seconds, stamped by
    the hub at arrival) so the meter is deterministic under a fake clock; they
    default to the injected ``clock``. :meth:`snapshot` derives the stalled state
    and returns an immutable :class:`ThroughputState`.
    """

    #: Public for callers/tests that want to reason about the threshold.
    STALL_AFTER_SECONDS: float = _STALL_AFTER_SECONDS

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        is_reasoning_model: Callable[[str | None], bool] | None = None,
    ) -> None:
        """Bind the monotonic clock and the reasoning-model predicate (both DI)."""
        self._clock = clock
        self._is_reasoning_model = is_reasoning_model or _default_is_reasoning_model
        self._phase = Phase.IDLE
        self._phase_started = 0.0
        self._last_event = 0.0
        self._tok_per_s = 0.0
        self._ttft: float | None = None
        self._llm_started: float | None = None
        self._saw_first_token = False
        self._last_delta_ts: float | None = None
        self._reasoning = False
        self._tool_id: str | None = None
        # Live output-token estimate for the CURRENT call (len/4), reconciled to
        # the authoritative count at ``llm_call_end``.
        self._stream_tokens = 0.0
        self._last_output_tokens = 0

    # -- feed (called from the hub's ingest points) -----------------------

    def _enter(self, phase: Phase, now: float) -> None:
        if phase is not self._phase:
            self._phase = phase
            self._phase_started = now
        self._last_event = now

    def mark_llm_start(self, model: str | None = None, now: float | None = None) -> None:
        """A model call started — enter the TTFT window (uploading/reasoning)."""
        now = self._clock() if now is None else now
        self._llm_started = now
        self._saw_first_token = False
        self._last_delta_ts = None
        self._stream_tokens = 0.0
        self._reasoning = self._is_reasoning_model(model)
        self._enter(Phase.REASONING if self._reasoning else Phase.UPLOADING, now)

    def mark_delta(self, text: str, now: float | None = None) -> None:
        """A streamed text delta arrived — record TTFT + fold the EWMA tok/s."""
        now = self._clock() if now is None else now
        tokens = max(0.0, len(text) / _CHARS_PER_TOKEN)
        if not self._saw_first_token:
            self._saw_first_token = True
            if self._llm_started is not None:
                self._ttft = max(0.0, now - self._llm_started)
        elif self._last_delta_ts is not None:
            dt = now - self._last_delta_ts
            if dt > 0:
                instant = tokens / dt
                self._tok_per_s = (
                    _EWMA_ALPHA * instant + (1 - _EWMA_ALPHA) * self._tok_per_s
                )
        self._last_delta_ts = now
        self._stream_tokens += tokens
        self._enter(Phase.STREAMING, now)

    def mark_tool_start(self, tool_id: str | None = None, now: float | None = None) -> None:
        """A tool call began executing."""
        now = self._clock() if now is None else now
        self._tool_id = tool_id or None
        self._enter(Phase.RUNNING_TOOL, now)

    def mark_tool_end(self, now: float | None = None) -> None:
        """A tool call finished — briefly idle until the next model call."""
        now = self._clock() if now is None else now
        self._tool_id = None
        self._enter(Phase.IDLE, now)

    def mark_llm_end(self, output_tokens: int | None = None, now: float | None = None) -> None:
        """The model call finished — reconcile the live estimate to the truth."""
        now = self._clock() if now is None else now
        if output_tokens is not None and output_tokens >= 0:
            self._last_output_tokens = int(output_tokens)
        self._stream_tokens = 0.0
        # Stay idle; the next ``mark_tool_start`` / ``mark_llm_start`` re-enters.
        self._enter(Phase.IDLE, now)

    def reset(self, now: float | None = None) -> None:
        """Clear all state (a new session / a hard turn boundary)."""
        now = self._clock() if now is None else now
        self._phase = Phase.IDLE
        self._phase_started = now
        self._last_event = now
        self._tok_per_s = 0.0
        self._ttft = None
        self._llm_started = None
        self._saw_first_token = False
        self._last_delta_ts = None
        self._reasoning = False
        self._tool_id = None
        self._stream_tokens = 0.0
        self._last_output_tokens = 0

    # -- read -------------------------------------------------------------

    def snapshot(self, now: float | None = None) -> ThroughputState:
        """Compute the current :class:`ThroughputState` (derives stalled)."""
        now = self._clock() if now is None else now
        base = self._phase
        stall_seconds = max(0.0, now - self._last_event)
        phase_elapsed = max(0.0, now - self._phase_started)
        # Reasoning silence is legitimate; only the active work phases stall.
        stalled = base in (
            Phase.UPLOADING,
            Phase.STREAMING,
            Phase.RUNNING_TOOL,
        ) and stall_seconds >= _STALL_AFTER_SECONDS
        phase = Phase.STALLED if stalled else base
        return ThroughputState(
            phase=phase,
            tok_per_s=max(0.0, self._tok_per_s),
            ttft=self._ttft,
            phase_elapsed=phase_elapsed,
            stall_seconds=stall_seconds,
            tool_id=self._tool_id,
            reasoning=self._reasoning,
        )

    # -- formatting (pure) ------------------------------------------------

    @staticmethod
    def format_label(state: ThroughputState) -> str:
        """Render a compact activity label from a snapshot (footer/status use).

        Examples: ``streaming ↓82 tok/s``, ``uploading…``, ``reasoning… (8s)``,
        ``running bash… (12s)``, ``⠿ stalled 30s``. Returns ``"working"`` for the
        neutral idle-mid-turn gap so the activity line is never blank.
        """
        phase = state.phase
        if phase is Phase.STREAMING:
            return f"streaming ↓{state.tok_per_s:.0f} tok/s"
        if phase is Phase.UPLOADING:
            return "uploading…"
        if phase is Phase.REASONING:
            return f"reasoning… ({state.phase_elapsed:.0f}s)"
        if phase is Phase.RUNNING_TOOL:
            tool = state.tool_id or "tool"
            return f"running {tool}… ({state.phase_elapsed:.0f}s)"
        if phase is Phase.STALLED:
            # No glyph — the transcript activity line prepends its own spinner.
            return f"stalled {state.stall_seconds:.0f}s"
        return "working"

    @staticmethod
    def format_facet(state: ThroughputState) -> str:
        """Render a terse per-agent fleet-row facet (empty when nothing to show).

        The fleet already shows tool count + elapsed; this adds only the volatile
        signal a row otherwise hides — the streaming rate and, crucially, a
        **stall** (a sub-agent hang is the hard case). Idle/uploading add nothing.
        """
        phase = state.phase
        if phase is Phase.STALLED:
            return f"⠿ stalled {state.stall_seconds:.0f}s"
        if phase is Phase.STREAMING:
            return f"↓{state.tok_per_s:.0f} tok/s"
        if phase is Phase.REASONING:
            return "reasoning…"
        return ""


__all__ = ["Phase", "ThroughputMeter", "ThroughputState"]
