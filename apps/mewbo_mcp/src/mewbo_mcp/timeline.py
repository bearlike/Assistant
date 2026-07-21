"""The MCP projection of a session transcript.

This module used to be a hand-maintained Python PORT of the console's
``buildTimeline``, kept in step with it by convention. Convention lost: the port
rendered no plan, todos, widget, run-failure or question rows, so an MCP reader
and a console reader looking at the same session saw different conversations —
and neither could tell.

Turn reconstruction now lives ONCE, in
:class:`mewbo_core.transcript_timeline.TranscriptTimeline`. Everything below is
a projection of that assembler's output into the shapes MCP callers already
consume. No boundary rule is decided here; when a rule changes it changes in
core and every surface moves together.

WHY THE PROJECTION STILL EXISTS. The canonical assembler emits RENDERING rows —
one per user prompt, closure, marker or card. MCP's tiers (overview / turns /
steps / full) address a conversation by TURN, so this module regroups those rows
into one :class:`Turn` per prompt and lifts the standalone markers into the flat
lists the tools iterate. It is a reshaping, not a second derivation.

WHAT THE PROJECTION DELIBERATELY DROPS. :class:`QuestionMarker` carries no
``call_token``. That is the single-use bearer secret the answering surface POSTs
back; an MCP reader is read-only and must never receive the answer credential —
the same rule ``SearchTools`` applies to workspace ``instructions``. The
canonical :class:`~mewbo_core.transcript_timeline.QuestionMeta` never mints it
either, so the guarantee now holds at the source rather than at this seam.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mewbo_core.hooks import OutcomeAssertion
from mewbo_core.transcript_timeline import (
    EventRecord,
    TimelineEntry,
    TranscriptTimeline,
    TurnTokenUsage as CoreTurnTokenUsage,
)
from mewbo_core.types import BLOCKED_CODES
from pydantic import ValidationError

# ``BLOCKED_CODES`` is core's canonical set of tool-envelope codes that make a
# stopped run USER-ACTIONABLE (a credential, a network path, a permission, a
# quota). ``OutcomeAssertion`` is the model a session-end hook returns to
# CONTRADICT a clean terminal (the wiki-index-never-completed case). Both are
# imported rather than re-derived so this projection can never drift from the
# ``blocked`` / ``unmet_goal`` statuses ``summarize_session`` derives against the
# same events — a second copy is exactly the cross-surface divergence this closes.

__all__ = [
    "EventRecord",
    "QuestionMarker",
    "RecoveryMarker",
    "TerminationMarker",
    "TriggerMarker",
    "Turn",
    "TurnTokenUsage",
    "build_timeline",
    "compute_turn_token_usage",
    "extract_question_events",
    "extract_recovery_events",
    "extract_trigger_events",
]


@dataclass(slots=True)
class TurnTokenUsage:
    """Per-turn token rollup, in the snake_case shape MCP tools read.

    A thin view over the canonical
    :class:`~mewbo_core.transcript_timeline.TurnTokenUsage`; the PEAK-input /
    SUM-output reasoning lives there.
    """

    input_tokens: int = 0  # PEAK root input (context pressure)
    output_tokens: int = 0  # SUM root output (additive)
    sub_input_tokens: int = 0  # SUM of per-sub-agent peak input
    sub_output_tokens: int = 0
    sub_agent_count: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    reasoning_tokens: int = 0
    billed_input_tokens: int = 0  # cumulative billable (root sum + sub sum)

    @classmethod
    def _from_core(cls, usage: CoreTurnTokenUsage) -> TurnTokenUsage:
        return cls(**usage.model_dump())

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

    index: int  # 1-based, matches the canonical ``turn-<index>`` id
    turn_id: str
    user_text: str
    user_ts: str | None
    events: list[EventRecord] = field(default_factory=list)
    assistant_text: str = ""
    done_reason: str | None = None
    model: str | None = None
    closed: bool = False
    # True when the NEXT ``user`` event superseded this turn before anything
    # concluded it — no ``assistant``, no ``completion``, no ``done_reason``.
    # Distinguishes a turn that will never conclude from the trailing open turn
    # of a session that is simply still running: both have ``closed=False``, but
    # only the former is terminal.
    interrupted: bool = False
    # The failure record's classified detail when the run that answered this
    # turn failed, else ``""``. Bounded by the caller that renders it.
    error: str = ""
    # The user-actionable wall the run hit (``repo_access``/``network``/
    # ``forbidden``/``quota_exceeded``), else ``""``. The loop leaves
    # ``done_reason`` at ``"completed"`` for such a run and carries the real fact
    # HERE — so a reader that trusts ``done_reason`` alone treats a blocked run
    # as a success. When set, ``done_reason`` is overridden to ``"blocked"``.
    blocked_code: str = ""
    # The product token from a session-end hook's ``outcome_assertion`` that a
    # CLEAN completion (``done_reason: "completed"``) never met its purpose — the
    # majority unmet-goal case, a wiki index that ended without completing. The
    # loop can't see it (every signal it owns says success), so the assertion is
    # the only evidence. When set, ``done_reason`` is overridden to
    # ``"unmet_goal"`` and the assertion detail rides ``error``.
    unmet_goal_reason: str = ""
    # The opening ``user`` event's ``attachments`` (AttachmentDescriptor dicts),
    # or ``[]`` when the turn carried none.
    attachments: list[EventRecord] = field(default_factory=list)

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


def compute_turn_token_usage(turn_events: list[EventRecord]) -> TurnTokenUsage | None:
    """Roll up *turn_events*, or ``None`` when the turn had no token activity."""
    usage = CoreTurnTokenUsage.from_events(turn_events)
    return TurnTokenUsage._from_core(usage) if usage is not None else None


def build_timeline(events: list[EventRecord]) -> list[Turn]:
    """Reconstruct turns from a transcript.

    Regroups the canonical assembler's rows one-per-prompt. A turn is ``closed``
    when an ``assistant`` or ``completion`` row concluded it, ``interrupted``
    when the next prompt superseded it first, and neither while it is still
    running.

    ``done_reason`` is resolved from three sources, weakest first, because no
    single one covers every turn. The canonical ``TurnMeta`` carries a reason
    only when a ``completion`` closed the turn ITSELF, which is the rare shape:
    the ordinary run writes its final ``assistant`` event first, so the
    completion lands on an already-closed turn and its reason is dropped. The
    failure record covers the outcomes the assembler models as failures, and the
    ``completion`` event covers the rest — including every successful turn, and
    the halted/verification/budget outcomes that are not failures to the
    assembler but are certainly not "no outcome". Reading only the first left
    this tier reporting ``null`` for every turn of a session the overview
    correctly reported as failed.
    """
    transcript = TranscriptTimeline.build(events)
    outcomes, assertions = _completion_outcomes(events)
    turns: list[Turn] = []
    by_turn_id: dict[str, Turn] = {}

    for entry in transcript.entries:
        if entry.role == "user":
            turn = Turn(
                index=len(turns) + 1,
                turn_id=entry.turn_id,
                user_text=entry.content,
                user_ts=entry.ts,
                attachments=list(entry.attachments or []),
            )
            turns.append(turn)
            by_turn_id[entry.turn_id] = turn
            continue
        owner = by_turn_id.get(entry.turn_id)
        if owner is not None and entry.run_failure is not None:
            # Read BEFORE the turn-metadata gate below: when the closure was
            # real prose the assembler appends the failure as its own row, which
            # carries no turn metadata at all and would be skipped entirely.
            owner.done_reason = entry.run_failure.reason
            owner.error = entry.run_failure.text
        # Only a row that CARRIES turn metadata concluded a turn; a marker or a
        # card row shares the turn id without ending anything.
        if entry.turn is None:
            continue
        if owner is None:
            continue
        turn = owner
        turn.events = entry.turn.events
        turn.model = entry.turn.model
        if entry.turn.done_reason is not None:
            turn.done_reason = entry.turn.done_reason
        if entry.run_failure is not None and entry.run_failure.reason == "interrupted":
            # Synthesised from the ABSENCE of a closure: terminal, but never
            # "closed", and carrying no text because none was ever produced.
            turn.interrupted = True
            continue
        turn.closed = True
        if entry.turn.done_reason is None:
            # An assistant event closed it, so the content is the model's own
            # final text. A completion closure also carries role "assistant",
            # so the role alone cannot tell the two apart — only the absence of
            # a done_reason can, and a placeholder like "(run ended)" must never
            # be projected as something the model returned.
            turn.assistant_text = entry.content
        elif entry.turn.done_reason == "command":
            # A slash-command completion carries its rendered body verbatim.
            turn.assistant_text = entry.content

    # The trailing turn of a live session concluded with nothing at all, so it
    # owns no metadata row — its events come from the walk's leftover state.
    if transcript.open_turn is not None:
        running = by_turn_id.get(transcript.open_turn.id)
        if running is not None:
            running.events = transcript.open_turn.events
            running.model = transcript.open_turn.model

    for turn in turns:
        # The run's own terminal record wins over anything derived from the
        # rendered rows — it is the same value the overview tier reports.
        reason, blocked_code = outcomes.get(turn.index, (None, None))
        if reason:
            turn.done_reason = reason
        if blocked_code:
            # A blocked run's ``done_reason`` is a laundered ``"completed"`` /
            # ``"error"``; the derived ``blocked`` status is the actionable
            # truth, so it overrides — matching what the session ``status`` (and
            # core's ``_completion_status``) reports for the same completion.
            turn.blocked_code = blocked_code
            turn.done_reason = "blocked"
            if not turn.error:
                turn.error = blocked_code
        elif turn.index in assertions and turn.done_reason == "completed":
            # A CLEAN completion an ``outcome_assertion`` contradicts: the run
            # ended but never met its purpose. Only a ``"completed"`` turn is
            # promoted — a failed/halted one is already honest — mirroring
            # ``summarize_session``'s ``status == "completed"`` gate, and blocked
            # (checked first) outranks it exactly as it does in core.
            assertion = assertions[turn.index]
            turn.done_reason = "unmet_goal"
            turn.unmet_goal_reason = assertion.reason
            if not turn.error:
                turn.error = assertion.detail or assertion.reason
    return turns


def _completion_outcomes(
    events: list[EventRecord],
) -> tuple[dict[int, tuple[str | None, str | None]], dict[int, OutcomeAssertion]]:
    """Map 1-based turn index → its completion outcome and any unmet-goal assertion.

    Returns ``(outcomes, assertions)`` where ``outcomes[i]`` is
    ``(done_reason, blocked_code)`` and ``assertions[i]`` is the
    ``OutcomeAssertion`` a session-end hook appended for turn ``i``.

    Attribution uses the one rule this projection already stands on — a ``user``
    event opens a turn — so no boundary rule is decided here. A completion
    belongs to the most recently opened turn, which covers both shapes: closing
    an open turn, and landing after an ``assistant`` event already closed it.
    Last write wins, so a turn that somehow recorded two terminal events reports
    the later one rather than an arbitrary one. ``blocked_code`` is validated
    against core's canonical set, so an unrecognised value can never widen the
    outcome vocabulary — the same guard ``_completion_status`` applies.

    An ``outcome_assertion`` is attached ONLY when it follows the turn's
    completion (the shape the hook always produces — it describes a terminal that
    already happened), which mirrors ``summarize_session`` resetting its pending
    assertion on every completion: a stray assertion before the terminal is
    ignored. It is validated through core's own ``OutcomeAssertion`` model, so a
    malformed record is dropped rather than breaking the projection.
    """
    outcomes: dict[int, tuple[str | None, str | None]] = {}
    assertions: dict[int, OutcomeAssertion] = {}
    completed: set[int] = set()
    index = 0
    for event in events:
        etype = event.get("type")
        if etype == "user":
            index += 1
        elif etype == "completion" and index:
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            reason = payload.get("done_reason")
            reason = reason if isinstance(reason, str) and reason else None
            raw_code = payload.get("blocked_code")
            code = raw_code if isinstance(raw_code, str) and raw_code in BLOCKED_CODES else None
            outcomes[index] = (reason, code)
            completed.add(index)
        elif etype == "outcome_assertion" and index in completed:
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            try:
                assertions[index] = OutcomeAssertion.model_validate(payload)
            except ValidationError:
                continue
    return outcomes, assertions


@dataclass(slots=True)
class TriggerMarker:
    """A ``trigger_armed`` / ``trigger_fired`` transcript marker.

    ``turn_index`` is the 1-based index of the turn that was OPEN when the event
    was recorded, or ``None`` when it arrived with no open turn — the common case
    for ``trigger_fired``, which typically wakes a session BETWEEN turns, before
    the re-engagement's own ``user`` event opens the next one.
    """

    action: str  # "armed" | "fired"
    ts: str | None
    turn_index: int | None
    trigger_id: str | None
    kind: str
    summary: str | None


@dataclass(slots=True)
class TerminationMarker:
    """A ``session_terminated`` transcript marker.

    Always turn-independent — termination never attaches to a turn — so it
    carries only ``ts``.
    """

    ts: str | None


@dataclass(slots=True)
class RecoveryMarker:
    """A session-level recovery the user triggered on a failed run.

    Only the two user-driven actions become markers. The tool-use loop's own
    ``halt_no_progress`` recovery is engine trace, not conversation, and the
    canonical assembler leaves it on the turn's events.
    """

    action: str  # "retry" | "continue"
    ts: str | None
    turn_index: int | None


@dataclass(slots=True)
class QuestionMarker:
    """A pending/settled ask-user-question group.

    ``turn_index`` is the 1-based index of the turn OPEN when the question was
    asked (the run blocks mid-turn). Deliberately carries no ``call_token`` —
    see the module docstring.
    """

    call_id: str
    ts: str | None
    turn_index: int
    questions: list[EventRecord]
    status: str  # pending | answered | declined | interrupted | cancelled
    answers: list[EventRecord] | None
    answered_via: str | None


def _turn_index(entry: TimelineEntry) -> int | None:
    """Read the 1-based turn index off an entry's ``turn-<n>`` id.

    A marker recorded with no turn open carries one of the standalone bucket ids
    (``triggers``, ``recovery``) instead, which yields ``None``.
    """
    prefix = "turn-"
    if not entry.turn_id.startswith(prefix):
        return None
    suffix = entry.turn_id[len(prefix) :]
    return int(suffix) if suffix.isdigit() else None


def extract_trigger_events(
    events: list[EventRecord],
) -> tuple[list[TriggerMarker], TerminationMarker | None]:
    """Project the trigger and termination markers out of a transcript.

    Returns ``(triggers, terminated)``. ``terminated`` is the LAST
    ``session_terminated`` marker seen — termination is a one-way absorbing
    state, so a transcript carries at most one in practice; last-wins is a
    defensive pick if it ever carried more.
    """
    triggers: list[TriggerMarker] = []
    terminated: TerminationMarker | None = None
    for entry in TranscriptTimeline.assemble(events):
        if entry.role == "trigger" and entry.trigger is not None:
            triggers.append(
                TriggerMarker(
                    action=entry.trigger.action,
                    ts=entry.ts,
                    turn_index=_turn_index(entry),
                    trigger_id=entry.trigger.trigger_id,
                    kind=entry.trigger.kind,
                    summary=entry.trigger.summary,
                )
            )
        elif entry.role == "session_terminated":
            terminated = TerminationMarker(ts=entry.ts)
    return triggers, terminated


def extract_recovery_events(events: list[EventRecord]) -> list[RecoveryMarker]:
    """Project the user-driven recovery markers out of a transcript."""
    return [
        RecoveryMarker(action=entry.recovery.action, ts=entry.ts, turn_index=_turn_index(entry))
        for entry in TranscriptTimeline.assemble(events)
        if entry.role == "recovery" and entry.recovery is not None
    ]


def extract_question_events(events: list[EventRecord]) -> list[QuestionMarker]:
    """Project the ask-user-question groups out of a transcript, settled in place."""
    markers: list[QuestionMarker] = []
    for entry in TranscriptTimeline.assemble(events):
        if entry.role != "question" or entry.question is None:
            continue
        index = _turn_index(entry)
        if index is None:
            continue
        markers.append(
            QuestionMarker(
                call_id=entry.question.call_id,
                ts=entry.ts,
                turn_index=index,
                questions=list(entry.question.questions),
                status=entry.question.status,
                answers=list(entry.question.answers) if entry.question.answers else None,
                answered_via=entry.question.answered_via,
            )
        )
    return markers
