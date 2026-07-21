#!/usr/bin/env python3
"""The canonical turn assembler: an event transcript in, conversation turns out.

A session's event log is append-only and flat. Every surface that renders a
conversation has to re-derive turn structure from it — where a turn opens, what
closes it, which markers belong between turns rather than inside one. That
derivation was independently reimplemented per surface, and the copies drifted:
one rendered a plan card, another silently dropped it; one materialised a turn
the next prompt superseded, another discarded its whole body. A reader's
conversation therefore depended on which surface they happened to be looking
at.

This module is the ONE derivation. It is pure: events arrive as an argument and
turns leave as a return value. There is no store, no clock, no config read —
which is also why a test drives every branch below with a literal list of dicts
and no fixtures. Mirrors the discipline :mod:`mewbo_core.triggers.spec` applies
to trigger kinds: the behavior intrinsic to a shape lives ON that shape (see
:meth:`TodoMeta.from_payload`, :meth:`TriggerMeta.from_payload`,
:meth:`RunFailureMeta.from_payload`), and :class:`TranscriptTimeline` walks the
log without a single ``if role ==`` dispatch table of its own.

WHAT A TURN IS. A ``user`` event opens one. The next ``assistant`` event closes
it; a ``completion`` closes it defensively when no assistant event arrived. A
turn still open when the NEXT ``user`` event lands never concluded at all, and
is flushed as an interrupted turn rather than dropped — the body it accumulated
is the only record that the work happened.

THE OPEN-TURN GATE, and what is hoisted above it. Events arriving with no turn
open are ignored: they belong to no conversation. Four kinds are recognized
BEFORE that gate because they legitimately arrive between turns, and gating
them would discard every one of them:

* ``trigger_armed`` / ``trigger_fired`` — a fired trigger wakes an idle session
  BEFORE the re-engagement writes its own ``user`` event.
* ``recovery`` for the two USER-driven actions — recorded after the failed turn
  closed and before the resumed turn opens. The tool-use loop's own
  ``halt_no_progress`` recovery is deliberately NOT hoisted: it falls through to
  the turn's events, where a trace view already renders it. It is engine
  detail, not conversation.
* ``session_terminated`` — a terminate after the last turn closed.
* a ``completion`` for a turn an ``assistant`` event has ALREADY closed — the
  shape every real failure takes, since the orchestrator appends its synthetic
  closure and only then the completion.

THE SYNTHETIC-VS-REAL CLOSURE DISTINCTION. ``done_reason`` decides WHETHER a run
failed — never the closure's text, which is a backend formatting detail and must
not become a client contract. What the text decides is whether the closure is
the orchestrator's disposable placeholder (upgraded in place, so one entry per
turn keeps the turn metadata intact) or prose the model actually returned
(the failure is surfaced BESIDE it). That distinction has no other signal behind
it: a boot sweep appends a terminal completion to an orphaned run, so a session
that died between a REAL assistant event and its completion produces a failure
completion landing on genuine prose. Blanking that would destroy the only copy
of the answer the run produced.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from .run_error import RunError, RunErrorKind

EventRecord = dict[str, Any]

# The classification vocabulary, read off the Literal itself so a kind added to
# ``RunError`` is accepted here without a second list to keep in step.
_RUN_ERROR_KINDS: frozenset[str] = frozenset(get_args(RunErrorKind))

# The shape of the orchestrator's synthetic assistant closure — a lone
# parenthesised "(Run …)" clause and nothing else, covering every variant it
# emits ("(Run interrupted by error: …)", "(Run stopped: …)", "(Run canceled by
# user)", "(Run ended: …)"). A closure matching this carries nothing the failure
# record does not already hold, so replacing it loses nothing. Matched by an
# explicit prefix/suffix pair rather than a regex: anchoring at BOTH ends is the
# load-bearing part (a real answer that merely opens with a parenthetical must
# never be mistaken for a placeholder), and a prefix/suffix test states that
# directly and cannot backtrack.
_SYNTHETIC_CLOSURE_PREFIX = "(Run "
_SYNTHETIC_CLOSURE_SUFFIX = ")"

# The two ``done_reason`` values that mean the run failed. ``interrupted`` is
# absent on purpose — it is synthesised from the ABSENCE of a closure and never
# read off a payload.
_FAILURE_REASONS = frozenset({"error", "max_steps_reached"})

# Recovery actions that are conversation, not engine trace. See the module
# docstring's note on ``halt_no_progress``.
_USER_RECOVERY_ACTIONS = frozenset({"retry", "continue"})

TimelineRole = Literal[
    "user",
    "assistant",
    "run_failed",
    "plan",
    "widget",
    "todos",
    "question",
    "trigger",
    "session_terminated",
    "recovery",
    "compaction",
]


def _read_str(payload: dict[str, Any], key: str) -> str | None:
    """Return ``payload[key]`` when it is a non-empty string, else ``None``.

    A module-level reader rather than a method: it owns no state, reads no
    field of any model here, and every model below needs it during its own
    ``from_payload``. Making it a member of one of them would force the others
    to import through that one arbitrarily.
    """
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _read_int(payload: dict[str, Any], key: str) -> int:
    """Return ``payload[key]`` as an int when it is a real number, else 0."""
    value = payload.get(key)
    if isinstance(value, bool):  # bool is an int subclass — exclude it
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _dict_list(value: Any) -> list[dict[str, Any]]:
    """Coerce an untrusted payload field to a list of dicts, never raising."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


class TurnTokenUsage(BaseModel):
    """Per-turn token rollup.

    PEAK for input, SUM for output. Within a turn each successive call's
    ``input_tokens`` grows as tool results stack onto the same prompt, so
    summing them double-counts the whole baseline prompt once per step; the peak
    is the real pressure on the context window. Output is genuinely additive.
    """

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=0, ge=0, description="Peak root input (context pressure).")
    output_tokens: int = Field(default=0, ge=0, description="Summed root output.")
    sub_input_tokens: int = Field(
        default=0, ge=0, description="Sum of per-sub-agent PEAK input."
    )
    sub_output_tokens: int = Field(default=0, ge=0, description="Summed sub-agent output.")
    sub_agent_count: int = Field(default=0, ge=0, description="Distinct sub-agent ids seen.")
    cache_creation_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    billed_input_tokens: int = Field(
        default=0, ge=0, description="Cumulative billable input (root sum + sub sum)."
    )

    @classmethod
    def from_events(cls, turn_events: list[EventRecord]) -> TurnTokenUsage | None:
        """Roll up a turn's ``llm_call_end`` events, or ``None`` if there are none.

        Sub-agent input sums per-agent PEAKS rather than every call: each
        sub-agent runs in its own isolated context, so the meaningful number is
        combined pressure across those parallel contexts.
        """
        peak_root_input = 0
        output_tokens = 0
        billed_root_input = 0
        billed_sub_input = 0
        sub_peak_per_agent: dict[str, int] = {}
        sub_output_tokens = 0
        sub_agents: set[str] = set()
        cache_creation = 0
        cache_read = 0
        reasoning = 0

        for event in turn_events:
            if event.get("type") != "llm_call_end":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            depth = _read_int(payload, "depth")
            in_tok = _read_int(payload, "input_tokens")
            out_tok = _read_int(payload, "output_tokens")
            cache_creation += _read_int(payload, "cache_creation_input_tokens")
            cache_read += _read_int(payload, "cache_read_input_tokens")
            reasoning += _read_int(payload, "reasoning_output_tokens")
            if depth == 0:
                peak_root_input = max(peak_root_input, in_tok)
                output_tokens += out_tok
                billed_root_input += in_tok
            else:
                aid = _read_str(payload, "agent_id") or ""
                sub_peak_per_agent[aid] = max(sub_peak_per_agent.get(aid, 0), in_tok)
                sub_output_tokens += out_tok
                billed_sub_input += in_tok
                if aid:
                    sub_agents.add(aid)

        sub_input_tokens = sum(sub_peak_per_agent.values())
        if not (peak_root_input or output_tokens or sub_input_tokens or sub_output_tokens):
            return None
        return cls(
            input_tokens=peak_root_input,
            output_tokens=output_tokens,
            sub_input_tokens=sub_input_tokens,
            sub_output_tokens=sub_output_tokens,
            sub_agent_count=len(sub_agents),
            cache_creation_tokens=cache_creation,
            cache_read_tokens=cache_read,
            reasoning_tokens=reasoning,
            billed_input_tokens=billed_root_input + billed_sub_input,
        )


class TurnMeta(BaseModel):
    """The run that answered one prompt: its events and their rollups.

    ``duration_ms`` is the raw span, not a rendered string — formatting is a
    surface concern, and a core model that pre-formatted it would force every
    consumer to parse the label back out to do arithmetic on it.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    events: list[EventRecord] = Field(default_factory=list)
    duration_ms: int | None = Field(
        default=None, ge=0, description="Span from the opening prompt to the closing event."
    )
    model: str | None = None
    token_usage: TurnTokenUsage | None = None
    done_reason: str | None = Field(
        default=None,
        description=(
            "The closing completion's reason; None when an assistant event closed it."
        ),
    )


class PlanMeta(BaseModel):
    """A proposed plan and its approval state.

    ``status`` starts ``pending`` and is settled in place by the matching
    ``plan_approved``/``plan_rejected`` event — the same pending→settled fold
    :class:`QuestionMeta` uses.
    """

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(default=0, ge=0)
    status: Literal["pending", "approved", "rejected"] = "pending"
    plan_path: str | None = None
    plan_content: str = ""
    plan_summary: str | None = None
    timestamp: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any], ts: str | None) -> PlanMeta:
        """Read a ``plan_proposed`` payload; every field degrades, none raises."""
        return cls(
            revision=_read_int(payload, "revision"),
            status="pending",
            plan_path=_read_str(payload, "plan_path"),
            plan_content=_read_str(payload, "content") or "",
            plan_summary=_read_str(payload, "summary"),
            timestamp=ts,
        )


class TodoItem(BaseModel):
    """One checklist row. ``label`` is required — a label-less row renders blank."""

    model_config = ConfigDict(extra="forbid")

    label: str
    status: Literal["pending", "in_progress", "completed"] = "pending"


class TodoMeta(BaseModel):
    """The live todo/plan checklist carried by a ``todos`` event."""

    model_config = ConfigDict(extra="forbid")

    items: list[TodoItem] = Field(default_factory=list)
    source: Literal["plan", "agent"] | None = None
    agent_id: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TodoMeta | None:
        """Parse a ``todos`` payload, or ``None`` when no usable row remains.

        Returning ``None`` for an empty list is what stops a surface rendering a
        fabricated empty checklist. The CLI's ``done`` spelling is normalized to
        ``completed`` so either producer yields one shape.
        """
        items: list[TodoItem] = []
        for entry in _dict_list(payload.get("items")):
            label = (_read_str(entry, "label") or "").strip()
            if not label:
                continue
            raw_status = _read_str(entry, "status") or "pending"
            if raw_status in ("completed", "done"):
                status: Literal["pending", "in_progress", "completed"] = "completed"
            elif raw_status == "in_progress":
                status = "in_progress"
            else:
                status = "pending"
            items.append(TodoItem(label=label, status=status))
        if not items:
            return None
        raw_source = _read_str(payload, "source")
        source: Literal["plan", "agent"] | None = (
            raw_source if raw_source in ("plan", "agent") else None  # type: ignore[assignment]
        )
        return cls(items=items, source=source, agent_id=_read_str(payload, "agent_id"))


class QuestionMeta(BaseModel):
    """A pending or settled ask-user-question group, keyed by ``call_id``.

    Deliberately carries NO ``call_token``. That token is the single-use bearer
    secret the answering surface POSTs back; an assembled transcript is a READ
    projection that travels to readers who are not the answerer, so minting it
    into the transcript would hand the answer credential to every one of them.
    A surface that must answer holds the token from the live event stream.
    """

    model_config = ConfigDict(extra="forbid")

    call_id: str
    questions: list[dict[str, Any]] = Field(default_factory=list)
    status: Literal[
        "pending", "answered", "declined", "interrupted", "cancelled"
    ] = "pending"
    answers: list[dict[str, Any]] | None = None
    answered_via: str | None = None


class TriggerMeta(BaseModel):
    """A ``trigger_armed`` / ``trigger_fired`` marker."""

    model_config = ConfigDict(extra="forbid")

    trigger_id: str | None = None
    kind: str = "trigger"
    action: Literal["armed", "fired"]
    summary: str | None = None

    @classmethod
    def from_payload(
        cls, payload: dict[str, Any], action: Literal["armed", "fired"]
    ) -> TriggerMeta:
        """Read a trigger payload.

        The two event kinds name the summary field differently — ``armed``
        writes ``summary``, ``fired`` writes ``payload_summary`` — so reading
        the wrong one yields a marker with no explanation of why the session
        woke. ``kind`` falls back to a generic label so an unknown or absent
        kind still renders a sensible marker.
        """
        raw = payload.get("summary") if action == "armed" else payload.get("payload_summary")
        summary = raw.strip() if isinstance(raw, str) and raw.strip() else None
        return cls(
            trigger_id=_read_str(payload, "trigger_id"),
            kind=_read_str(payload, "kind") or "trigger",
            action=action,
            summary=summary,
        )


class RecoveryMeta(BaseModel):
    """A user-driven retry/continue on a failed run."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["retry", "continue"]


class CompactionMeta(BaseModel):
    """A context-compaction boundary: where the model's horizon moved.

    The runtime replaced older transcript events with a summary so the next call
    stays within budget (:class:`~mewbo_core.context.ContextBuilder` slices the
    transcript forward past this marker). The persisted events rendered ABOVE
    the marker are untouched — only what the model receives going forward
    narrows, which is why this is a marker in the conversation rather than a
    truncation of it.
    """

    model_config = ConfigDict(extra="forbid")

    mode: str = "auto"
    tokens_saved: int | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> CompactionMeta:
        """Read a ``context_compacted`` payload.

        ``mode`` defaults to ``auto`` for an event recorded before the field
        existed. Field names mirror the trace panel's fuller rendering of the
        same event so the two surfaces cannot drift on what the payload means.
        """
        saved = payload.get("tokens_saved")
        usable = isinstance(saved, (int, float)) and not isinstance(saved, bool)
        return cls(
            mode=_read_str(payload, "mode") or "auto",
            tokens_saved=int(saved) if usable else None,  # type: ignore[arg-type]
        )


class RunFailureMeta(BaseModel):
    """A failed run, normalized once so every surface renders the same record.

    ``detail`` reuses :class:`~mewbo_core.run_error.RunError` rather than
    restating its fields: that model already owns the length clamps and the
    markup guard that keep an upstream error page out of a rendered card, and a
    second parallel shape here would be a second place for those caps to be
    forgotten.
    """

    model_config = ConfigDict(extra="forbid")

    reason: Literal["error", "max_steps_reached", "interrupted"]
    text: str = Field(default="", description="Classified detail, else the legacy error string.")
    detail: RunError | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> RunFailureMeta | None:
        """Normalize a ``completion`` payload, or ``None`` when the run did not fail.

        The verdict comes from ``done_reason`` alone. Degrades for events
        persisted before ``error_detail`` shipped: the body falls back to the
        capped ``error``/``last_error`` string and no classified record is
        attached.
        """
        reason = (_read_str(payload, "done_reason") or "").lower()
        if reason not in _FAILURE_REASONS:
            return None
        detail = cls._parse_detail(payload.get("error_detail"))
        legacy = _read_str(payload, "error") or _read_str(payload, "last_error") or ""
        return cls(
            reason="error" if reason == "error" else "max_steps_reached",
            text=(detail.detail if detail else "") or legacy,
            detail=detail,
        )

    @staticmethod
    def _parse_detail(raw: Any) -> RunError | None:
        """Narrow an untrusted ``error_detail`` into a :class:`RunError`, or ``None``.

        A stored payload is not a trusted construction site: it may predate a
        field, carry an unrecognized ``kind``, or (after a schema change) carry
        a key ``RunError`` forbids. None of those should cost a reader the whole
        transcript, so the keys are selected explicitly and an unknown ``kind``
        degrades to ``unknown`` — the same leniency the rest of this module
        applies to every payload read.
        """
        if not isinstance(raw, dict):
            return None
        kind = _read_str(raw, "kind") or ""
        selected: dict[str, Any] = {
            "kind": kind if kind in _RUN_ERROR_KINDS else "unknown",
            "title": _read_str(raw, "title") or "",
            "provider": _read_str(raw, "provider"),
            "detail": _read_str(raw, "detail") or "",
        }
        supplied_chars = raw.get("detail_chars")
        if isinstance(supplied_chars, int) and not isinstance(supplied_chars, bool):
            selected["detail_chars"] = supplied_chars
        return RunError.model_validate(selected)


class TimelineEntry(BaseModel):
    """One rendered row of a conversation.

    ``role`` says which of the optional meta fields is populated. They are
    separate optional fields rather than a discriminated union because an entry
    is a RENDERING row, not a variant with its own behavior — the assembler
    settles several of them in place after the fact (a plan's status, a
    question's answers, a synthetic closure upgraded to a failure), which a
    union of frozen variants would make a rebuild rather than a field write.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    role: TimelineRole
    content: str = ""
    turn_id: str
    ts: str | None = None
    turn: TurnMeta | None = None
    plan: PlanMeta | None = None
    widget: dict[str, Any] | None = None
    todos: TodoMeta | None = None
    question: QuestionMeta | None = None
    trigger: TriggerMeta | None = None
    recovery: RecoveryMeta | None = None
    compaction: CompactionMeta | None = None
    run_failure: RunFailureMeta | None = None
    attachments: list[dict[str, Any]] | None = None


class Transcript(BaseModel):
    """A whole assembled conversation: settled rows, plus the turn still running.

    ``open_turn`` is the trailing turn no closure has concluded yet — the
    in-flight turn of a live session. It is NOT among ``entries``: an unconcluded
    turn has no closing row to hang metadata on, and synthesising one would make
    a running turn indistinguishable from a finished one. It is also NOT the
    interrupted case, which HAS a following prompt proving it will never
    conclude and therefore is flushed into ``entries``.
    """

    model_config = ConfigDict(extra="forbid")

    entries: list[TimelineEntry] = Field(default_factory=list)
    open_turn: TurnMeta | None = None


class TranscriptTimeline:
    """Walks an event log once and emits :class:`TimelineEntry` rows.

    The walk state (which turn is open, what it has accumulated, where the
    pending closure sits) lives on the instance rather than in a pile of
    closure variables, so each rule below reads and writes named fields instead
    of threading a tuple through helpers. Construct via :meth:`assemble`; the
    instance is single-use.
    """

    __slots__ = (
        "_entries",
        "_turn_index",
        "_current_turn_id",
        "_turn_events",
        "_turn_start",
        "_last_model",
        "_turn_model",
        "_last_attachments",
        "_closure_index",
    )

    def __init__(self) -> None:
        """Start an empty walk; see :meth:`assemble` for the entry point."""
        self._entries: list[TimelineEntry] = []
        self._turn_index = 0
        self._current_turn_id: str | None = None
        self._turn_events: list[EventRecord] = []
        self._turn_start: str | None = None
        self._last_model: str | None = None
        self._turn_model: str | None = None
        # Fallback source for older sessions where attachment descriptors were
        # only ever written onto the ``context`` event, never the ``user`` event.
        self._last_attachments: list[dict[str, Any]] | None = None
        # Index of the assistant entry that closed the most recent turn, while
        # that turn's ``completion`` is still pending. The orchestrator appends
        # its synthetic closure BEFORE the completion, so a failure has to reach
        # BACK and upgrade that entry rather than push its own. Cleared the
        # moment the completion is seen or a new turn opens, so a failure can
        # never reach past a turn that has already settled.
        self._closure_index: int | None = None

    @classmethod
    def build(cls, events: list[EventRecord]) -> Transcript:
        """Assemble the whole conversation, including any still-running turn.

        ONE walk produces both halves. The console derives the open turn from a
        SECOND walker over the same log, which is a standing drift risk: the two
        close a turn at different points, so a boundary rule fixed in one can be
        missed in the other. Here the open turn is simply the walk's leftover
        state, and cannot disagree with the rows by construction.
        """
        walker = cls()
        entries = walker._run(events)
        open_turn = walker._build_turn_meta(None) if walker._current_turn_id else None
        return Transcript(entries=entries, open_turn=open_turn)

    @classmethod
    def assemble(cls, events: list[EventRecord]) -> list[TimelineEntry]:
        """Reconstruct the settled conversation rows for *events*, in order."""
        return cls()._run(events)

    @classmethod
    def turns(cls, events: list[EventRecord]) -> list[TurnMeta]:
        """The turn metadata alone, for callers that need rollups and not rows."""
        return [entry.turn for entry in cls.assemble(events) if entry.turn is not None]

    def _run(self, events: list[EventRecord]) -> list[TimelineEntry]:
        for event in events:
            etype = event.get("type")
            payload = event.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            ts = event.get("ts")
            ts = ts if isinstance(ts, str) else None

            if etype == "context":
                self._absorb_context(payload)
                continue
            if etype == "user":
                self._open_turn(event, payload, ts)
                continue
            # --- hoisted above the open-turn gate (see the module docstring) --
            if etype in ("trigger_armed", "trigger_fired"):
                action: Literal["armed", "fired"] = (
                    "armed" if etype == "trigger_armed" else "fired"
                )
                self._entries.append(
                    TimelineEntry(
                        id=f"trigger-{action}-{ts}",
                        role="trigger",
                        turn_id=self._current_turn_id or "triggers",
                        ts=ts,
                        trigger=TriggerMeta.from_payload(payload, action),
                    )
                )
                continue
            if etype == "recovery":
                action_raw = _read_str(payload, "action")
                if action_raw in _USER_RECOVERY_ACTIONS:
                    self._entries.append(
                        TimelineEntry(
                            id=f"recovery-{ts}",
                            role="recovery",
                            turn_id=self._current_turn_id or "recovery",
                            ts=ts,
                            recovery=RecoveryMeta(action=action_raw),  # type: ignore[arg-type]
                        )
                    )
                    continue
                # An engine ``halt_no_progress`` deliberately falls THROUGH to
                # the gate below, joining the turn's events as trace detail.
            if etype == "context_compacted":
                # Handled above the gate like ``recovery``, but NOT restricted
                # to arriving between turns: the auto/user-triggered path fires
                # after a turn settles, while a reactive mid-loop compaction
                # fires with a turn still OPEN. Both render where the horizon
                # actually moved rather than being dropped for arriving
                # off-schedule.
                #
                # DEPTH 0 ONLY. A sub-agent compacts its own isolated context,
                # which narrows nothing about what THIS conversation's model
                # receives; a trace view already surfaces that one against its
                # agent id. Rendering it here would attribute a sub-context's
                # narrowing to the thread being read. The skip is total — a
                # deeper compaction is consumed, never folded into the turn's
                # events, so widening this filter is the only way it can
                # reappear.
                if _read_int(payload, "depth") == 0:
                    self._entries.append(
                        TimelineEntry(
                            id=f"compaction-{ts}",
                            role="compaction",
                            turn_id=self._current_turn_id or "compaction",
                            ts=ts,
                            compaction=CompactionMeta.from_payload(payload),
                        )
                    )
                continue
            if etype == "session_terminated":
                self._entries.append(
                    TimelineEntry(
                        id=f"session-terminated-{ts}",
                        role="session_terminated",
                        turn_id="session-terminated",
                        ts=ts,
                    )
                )
                continue
            if etype == "completion" and not self._current_turn_id:
                self._settle_pending_closure(payload, ts)
                continue
            # --- the open-turn gate -----------------------------------------
            if not self._current_turn_id:
                continue

            self._turn_events.append(event)
            if etype == "plan_proposed":
                self._entries.append(
                    TimelineEntry(
                        id=f"plan-{_read_int(payload, 'revision')}",
                        role="plan",
                        turn_id=self._current_turn_id,
                        plan=PlanMeta.from_payload(payload, ts),
                    )
                )
            elif etype == "todos":
                self._upsert_todos(payload, ts)
            elif etype == "widget_ready":
                self._entries.append(
                    TimelineEntry(
                        id=f"widget-{ts}",
                        role="widget",
                        turn_id=self._current_turn_id,
                        ts=ts,
                        widget=dict(payload),
                    )
                )
            elif etype in ("plan_approved", "plan_rejected"):
                self._settle_plan(payload, "approved" if etype == "plan_approved" else "rejected")
            elif etype == "user_question":
                self._open_question(payload, ts)
            elif etype == "user_question_answered":
                self._settle_question(payload)
            elif etype == "assistant":
                self._close_turn_with_assistant(payload, ts)
            elif etype == "completion":
                self._close_turn_with_completion(payload, ts)
        return self._entries

    # ------------------------------------------------------------------
    # Turn boundaries
    # ------------------------------------------------------------------

    def _absorb_context(self, payload: dict[str, Any]) -> None:
        """Track the model and attachments the NEXT opened turn inherits."""
        model = _read_str(payload, "model")
        if model:
            self._last_model = model
        attachments = _dict_list(payload.get("attachments"))
        if attachments:
            self._last_attachments = attachments

    def _open_turn(self, event: EventRecord, payload: dict[str, Any], ts: str | None) -> None:
        """Flush any unconcluded turn, then open a new one for this prompt."""
        self._flush_interrupted()
        self._turn_index += 1
        self._current_turn_id = f"turn-{self._turn_index}"
        self._turn_events = [event]
        self._turn_start = ts
        self._turn_model = self._last_model
        self._closure_index = None
        # Primary source is the persisted ``user`` event's own attachments;
        # the last ``context`` event is the fallback for sessions recorded
        # before that field existed.
        attachments = _dict_list(payload.get("attachments")) or self._last_attachments
        self._entries.append(
            TimelineEntry(
                id=f"user-{self._turn_index}",
                role="user",
                content=str(payload.get("text") or ""),
                turn_id=self._current_turn_id,
                ts=ts,
                attachments=attachments or None,
            )
        )
        # A ``context`` event's attachments belong to the ONE turn it precedes —
        # cleared so a later attachment-less turn never inherits a stale set.
        self._last_attachments = None

    def _flush_interrupted(self) -> None:
        """Materialise a turn that neither an assistant nor a completion concluded.

        Without this the reset in :meth:`_open_turn` drops every body event
        accumulated since the last prompt and erases the turn from the
        transcript entirely. Deliberately NOT applied to the trailing open turn:
        that one has no following ``user`` event, so it stays open and keeps its
        in-flight rendering.

        The outcome is synthesised from the ABSENCE of a closure, so there is no
        ``done_reason`` to read and no text to show. Inventing prose for a turn
        that produced none would put words in the model's mouth.
        """
        if not self._current_turn_id:
            return
        last_ts = self._turn_events[-1].get("ts") if self._turn_events else None
        last_ts = last_ts if isinstance(last_ts, str) else None
        self._entries.append(
            TimelineEntry(
                id=f"interrupted-{self._turn_index}",
                role="run_failed",
                turn_id=self._current_turn_id,
                ts=last_ts,
                turn=self._build_turn_meta(last_ts),
                run_failure=RunFailureMeta(reason="interrupted", text=""),
            )
        )

    def _build_turn_meta(self, end_ts: str | None, done_reason: str | None = None) -> TurnMeta:
        """Snapshot the open turn's events and rollups."""
        return TurnMeta(
            id=self._current_turn_id or "",
            events=list(self._turn_events),
            duration_ms=self._span_ms(self._turn_start, end_ts),
            model=self._turn_model,
            token_usage=TurnTokenUsage.from_events(self._turn_events),
            done_reason=done_reason,
        )

    def _reset_turn(self) -> None:
        self._current_turn_id = None
        self._turn_events = []
        self._turn_start = None

    def _close_turn_with_assistant(self, payload: dict[str, Any], ts: str | None) -> None:
        """Close the open turn on the model's final text for it."""
        self._entries.append(
            TimelineEntry(
                id=f"assistant-{self._turn_index}",
                role="assistant",
                content=str(payload.get("text") or ""),
                turn_id=self._current_turn_id or "",
                turn=self._build_turn_meta(ts),
            )
        )
        self._closure_index = len(self._entries) - 1
        self._reset_turn()

    def _close_turn_with_completion(self, payload: dict[str, Any], ts: str | None) -> None:
        """Close the open turn on a completion no assistant event preceded.

        Covers legacy sessions and any race where a run terminates without
        writing a final assistant event. Without it the turn's tool results stay
        orphaned and are discarded when the next prompt resets state.
        """
        reason = str(payload.get("done_reason") or "")
        turn = self._build_turn_meta(ts, done_reason=reason)
        # A slash-command completion carries its rendered body in ``text`` —
        # surfaced directly so the conversation shows the result rather than a
        # generic placeholder.
        if reason == "command":
            content = str(payload.get("text") or "")
        elif reason in ("canceled", "cancelled"):
            content = "(run canceled)"
        else:
            content = "(run ended)"
        # A failed run gets its own role rather than an assistant bubble reading
        # "(run interrupted — see logs)": the failure record travels with the
        # entry, in place and in order, so nothing has to be reconstructed from
        # a trace view to find out what happened.
        run_failure = RunFailureMeta.from_payload(payload)
        self._entries.append(
            TimelineEntry(
                id=f"completion-{self._turn_index}",
                role="run_failed" if run_failure else "assistant",
                content="" if run_failure else content,
                turn_id=self._current_turn_id or "",
                ts=ts,
                turn=turn,
                run_failure=run_failure,
            )
        )
        self._closure_index = None
        self._reset_turn()

    def _settle_pending_closure(self, payload: dict[str, Any], ts: str | None) -> None:
        """Apply a failure completion to the closure that already shut its turn.

        See the module docstring on the synthetic-vs-real distinction: a
        placeholder is upgraded in place (one entry per turn, so the turn
        metadata stays intact) while genuine prose keeps its entry and the
        failure is surfaced beside it.
        """
        if self._closure_index is None:
            return
        run_failure = RunFailureMeta.from_payload(payload)
        if run_failure:
            closure = self._entries[self._closure_index]
            text = closure.content
            if text.startswith(_SYNTHETIC_CLOSURE_PREFIX) and text.endswith(
                _SYNTHETIC_CLOSURE_SUFFIX
            ):
                closure.role = "run_failed"
                closure.content = ""
                closure.run_failure = run_failure
                closure.ts = ts
            else:
                self._entries.append(
                    TimelineEntry(
                        id=f"run-failed-{ts}",
                        role="run_failed",
                        turn_id=closure.turn_id,
                        ts=ts,
                        run_failure=run_failure,
                    )
                )
        self._closure_index = None

    # ------------------------------------------------------------------
    # In-turn folds
    # ------------------------------------------------------------------

    def _upsert_todos(self, payload: dict[str, Any], ts: str | None) -> None:
        """Keep ONE checklist per turn, updated in place by each snapshot.

        Successive ``todos`` events are snapshots of the same list, so pushing
        one row per emission would stack a dozen near-identical checklists onto
        a single turn.
        """
        todos = TodoMeta.from_payload(payload)
        if todos is None:
            return
        for entry in self._entries:
            if entry.role == "todos" and entry.turn_id == self._current_turn_id:
                entry.todos = todos
                return
        self._entries.append(
            TimelineEntry(
                id=f"todos-{self._current_turn_id}",
                role="todos",
                turn_id=self._current_turn_id or "",
                ts=ts,
                todos=todos,
            )
        )

    def _settle_plan(
        self, payload: dict[str, Any], status: Literal["approved", "rejected"]
    ) -> None:
        """Settle the most recent plan entry carrying this revision."""
        revision = _read_int(payload, "revision")
        for entry in reversed(self._entries):
            if entry.role == "plan" and entry.plan is not None and entry.plan.revision == revision:
                entry.plan.status = status
                return

    def _open_question(self, payload: dict[str, Any], ts: str | None) -> None:
        """Push a pending question card keyed by ``call_id``."""
        call_id = _read_str(payload, "call_id")
        if not call_id:
            return
        self._entries.append(
            TimelineEntry(
                id=f"question-{call_id}",
                role="question",
                turn_id=self._current_turn_id or "",
                ts=ts,
                question=QuestionMeta(
                    call_id=call_id,
                    questions=_dict_list(payload.get("questions")),
                    status="pending",
                ),
            )
        )

    def _settle_question(self, payload: dict[str, Any]) -> None:
        """Settle the pending question card in place, whatever the outcome."""
        call_id = _read_str(payload, "call_id")
        if not call_id:
            return
        outcome = _read_str(payload, "outcome") or "pending"
        if outcome not in ("pending", "answered", "declined", "interrupted", "cancelled"):
            return
        for entry in reversed(self._entries):
            if entry.role == "question" and entry.question is not None:
                if entry.question.call_id != call_id:
                    continue
                entry.question.status = outcome  # type: ignore[assignment]
                entry.question.answers = (
                    _dict_list(payload.get("answers")) if outcome == "answered" else None
                )
                entry.question.answered_via = _read_str(payload, "answered_via")
                return

    # ------------------------------------------------------------------

    @staticmethod
    def _span_ms(start: str | None, end: str | None) -> int | None:
        """Milliseconds between two ISO timestamps, or ``None`` if not measurable.

        A non-positive or unparseable span yields ``None`` rather than 0 so a
        surface can tell "no measurable duration" from "instantaneous", and so a
        fixture using opaque markers (``t0``, ``t1``) simply reports nothing
        instead of raising.
        """
        if not start or not end:
            return None
        try:
            start_dt = datetime.fromisoformat(start)
            end_dt = datetime.fromisoformat(end)
        except ValueError:
            return None
        delta = (end_dt - start_dt).total_seconds() * 1000
        return int(delta) if delta > 0 else None


__all__ = [
    "CompactionMeta",
    "EventRecord",
    "PlanMeta",
    "QuestionMeta",
    "RecoveryMeta",
    "RunFailureMeta",
    "TimelineEntry",
    "TimelineRole",
    "TodoItem",
    "TodoMeta",
    "Transcript",
    "TranscriptTimeline",
    "TriggerMeta",
    "TurnMeta",
    "TurnTokenUsage",
]
