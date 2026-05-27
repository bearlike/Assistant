"""Turn reconstruction from a Mewbo session transcript.

DRY note: this is a Python port of ``buildTimeline`` /
``computeTurnTokenUsage`` from
``apps/mewbo_console/src/utils/timeline.ts``. The two implementations MUST
stay behaviorally in sync — the console renders the same turns visually that
this module exposes to MCP callers. Parity is enforced by
``apps/mewbo_mcp/tests/test_timeline.py`` against shared fixtures. When you
change turn-boundary or token-usage logic in either file, update both and the
parity test.

What a *turn* is (unchanged from the TS): a ``user`` event opens a turn; the
next ``assistant`` event (or, defensively, a ``completion`` event) closes it.
A *step* is a single ``tool_result`` event inside a turn. Per-turn token
totals are derived from ``llm_call_end`` events: PEAK input (context
pressure — summing double-counts the growing prompt) and SUM output
(additive).

This port intentionally omits the console's diff/widget/plan rendering — the
MCP tiers (overview / turns / steps / full) only need turn boundaries, the
turn's events, the closing assistant text, the done_reason, and token
totals. Everything else stays in the TS layer where it is rendered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

EventRecord = dict[str, Any]


@dataclass(slots=True)
class TurnTokenUsage:
    """Per-turn token rollup, mirroring the TS ``TurnTokenUsage`` shape."""

    input_tokens: int = 0  # PEAK root input (context pressure)
    output_tokens: int = 0  # SUM root output (additive)
    sub_input_tokens: int = 0  # SUM of per-sub-agent peak input
    sub_output_tokens: int = 0
    sub_agent_count: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    reasoning_tokens: int = 0
    billed_input_tokens: int = 0  # cumulative billable (root sum + sub sum)

    def to_dict(self) -> dict[str, int]:
        """Return the JSON-friendly camelCase dict (matches the wire shape)."""
        return {
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "subInputTokens": self.sub_input_tokens,
            "subOutputTokens": self.sub_output_tokens,
            "subAgentCount": self.sub_agent_count,
            "cacheCreationTokens": self.cache_creation_tokens,
            "cacheReadTokens": self.cache_read_tokens,
            "reasoningTokens": self.reasoning_tokens,
            "billedInputTokens": self.billed_input_tokens,
        }


@dataclass(slots=True)
class Turn:
    """A reconstructed conversation turn (user prompt + the run that answered it)."""

    index: int  # 1-based, matches the TS ``turn-<index>`` id
    turn_id: str
    user_text: str
    user_ts: str | None
    events: list[EventRecord] = field(default_factory=list)
    assistant_text: str = ""
    done_reason: str | None = None
    model: str | None = None
    closed: bool = False

    @property
    def steps(self) -> list[EventRecord]:
        """Return this turn's ``tool_result`` events (one step each)."""
        return [e for e in self.events if e.get("type") == "tool_result"]

    @property
    def step_count(self) -> int:
        """Number of steps (``tool_result`` events) in this turn."""
        return len(self.steps)

    def token_usage(self) -> TurnTokenUsage | None:
        """Compute this turn's token rollup, or ``None`` if there is none."""
        return compute_turn_token_usage(self.events)


def _num(payload: dict[str, Any], key: str) -> int:
    """Return ``payload[key]`` as an int when it is a real number, else 0."""
    value = payload.get(key)
    if isinstance(value, bool):  # bool is an int subclass — exclude it
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def compute_turn_token_usage(turn_events: list[EventRecord]) -> TurnTokenUsage | None:
    """Port of ``computeTurnTokenUsage``.

    PEAK for input (context pressure), SUM for output (additive). Sub-agent
    input is summed across per-agent peaks (combined parallel pressure).
    Returns ``None`` when the turn had no measurable token activity.
    """
    peak_root_input = 0
    output_tokens = 0
    billed_root_input = 0
    billed_sub_input = 0
    sub_peak_per_agent: dict[str, int] = {}
    sub_output_tokens = 0
    sub_agents: set[str] = set()
    cache_creation_tokens = 0
    cache_read_tokens = 0
    reasoning_tokens = 0

    for event in turn_events:
        if event.get("type") != "llm_call_end":
            continue
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        depth = _num(payload, "depth")
        in_tok = _num(payload, "input_tokens")
        out_tok = _num(payload, "output_tokens")
        cache_creation_tokens += _num(payload, "cache_creation_input_tokens")
        cache_read_tokens += _num(payload, "cache_read_input_tokens")
        reasoning_tokens += _num(payload, "reasoning_output_tokens")
        if depth == 0:
            if in_tok > peak_root_input:
                peak_root_input = in_tok
            output_tokens += out_tok
            billed_root_input += in_tok
        else:
            aid_raw = payload.get("agent_id")
            aid = aid_raw if isinstance(aid_raw, str) else ""
            prev = sub_peak_per_agent.get(aid, 0)
            if in_tok > prev:
                sub_peak_per_agent[aid] = in_tok
            sub_output_tokens += out_tok
            billed_sub_input += in_tok
            if aid:
                sub_agents.add(aid)

    sub_input_tokens = sum(sub_peak_per_agent.values())
    if (
        not peak_root_input
        and not output_tokens
        and not sub_input_tokens
        and not sub_output_tokens
    ):
        return None
    return TurnTokenUsage(
        input_tokens=peak_root_input,
        output_tokens=output_tokens,
        sub_input_tokens=sub_input_tokens,
        sub_output_tokens=sub_output_tokens,
        sub_agent_count=len(sub_agents),
        cache_creation_tokens=cache_creation_tokens,
        cache_read_tokens=cache_read_tokens,
        reasoning_tokens=reasoning_tokens,
        billed_input_tokens=billed_root_input + billed_sub_input,
    )


def build_timeline(events: list[EventRecord]) -> list[Turn]:
    """Port of ``buildTimeline`` — reconstruct turns from a transcript.

    A ``user`` event opens a new turn; the next ``assistant`` event closes it
    with the assistant's text. A ``completion`` event also closes an
    open turn (defensive fallback for runs that ended without a final
    ``assistant`` event) — carrying the done_reason. ``context`` events update
    the "current model" that the next opened turn inherits.
    """
    turns: list[Turn] = []
    turn_index = 0
    current: Turn | None = None
    last_model: str | None = None

    for event in events:
        etype = event.get("type")
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}

        if etype == "context":
            model = payload.get("model")
            if isinstance(model, str) and model:
                last_model = model
            continue

        if etype == "user":
            turn_index += 1
            text = payload.get("text")
            current = Turn(
                index=turn_index,
                turn_id=f"turn-{turn_index}",
                user_text=str(text) if text is not None else "",
                user_ts=event.get("ts"),
                events=[event],
                model=last_model,
            )
            turns.append(current)
            continue

        if current is None or current.closed:
            # No open turn — events before the first user message (or between
            # a closed turn and the next user) are ignored, matching the TS.
            continue

        current.events.append(event)

        if etype == "assistant":
            text = payload.get("text")
            current.assistant_text = str(text) if text is not None else ""
            current.closed = True
            current = None
            continue

        # Defensive: a completion closes an open turn when no assistant event
        # has. The done_reason is preserved so callers can classify the turn.
        if etype == "completion":
            reason = payload.get("done_reason")
            current.done_reason = str(reason) if reason is not None else ""
            text = payload.get("text")
            # Slash-command completions carry the rendered body in payload.text.
            if current.done_reason == "command" and text is not None:
                current.assistant_text = str(text)
            current.closed = True
            current = None

    return turns


__all__ = [
    "EventRecord",
    "Turn",
    "TurnTokenUsage",
    "build_timeline",
    "compute_turn_token_usage",
]
