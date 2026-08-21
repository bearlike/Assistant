"""The declared work plan and the observed progress ledger.

A long-running operation is not one unit of work; it is an ordered list of
STEPS with different units, and only some of them can be counted at all. This
module holds the two shapes that say so:

* :class:`StepSpec` — what work EXISTS. Declared as a constant beside the code
  that runs it, before anything runs, so a reader can be shown the outline of an
  operation that has not started.
* :class:`StepRecord` — what HAPPENED to one declared step. Minted from a spec,
  carries its own state, its own clock and its own counter.
* :class:`ProgressLedger` — every record for one operation, in declared order.
  This is the exportable context: one object answering "what is this operation,
  where is it, and how much is left" without a reader re-deriving any of it.

**Why a ledger rather than a progress field.** A single ``(current, total,
unit)`` triple on the operation's record is a shared register with no owner and
no lifetime: whoever wrote last holds it until someone else writes, so a step
that finished at ``921/921`` keeps asserting ``921/921`` through every later
step that reports nothing — and a reader computing ``current/total`` paints a
completed bar over work that has an hour left. Records have an owner and a
lifetime by construction, which is what makes that class of defect
unrepresentable rather than merely fixed.

**The relationships, stated once.**

``StepSpec ──mints──▶ StepRecord ──held in declared order by──▶ ProgressLedger``

* A spec is CODE. A record is STATE. The ledger is the WIRE.
* ``group`` is the coarse bucket a reader collapses steps under — the wiki
  indexer sets it to the phase name, so "phase" stays a rendering concern rather
  than a second axis this module has to know about.
* ``weight`` is the step's share of its operation's cost. It is relative, so a
  caller may declare hand-set defaults now and replace them with measured
  seconds later without any consumer changing.
* A step with ``unit=None`` is UNCOUNTABLE — a single blocking call, not a loop.
  It still has a start time, so "running, 14 minutes" is a first-class state and
  is never confused with a completed one. **The cure for an opaque stretch is an
  open step, not a counter.**

**No I/O, ever.** The clock arrives as an argument to every method that needs
one, which is what lets a test drive a whole operation without sleeping, and
what keeps this module inside the zero-core-import property ``contracts/`` owns
(see ``contracts/CLAUDE.md``).

Cost: every method here is ``O(steps)`` in a plan a human wrote — tens, not
thousands. Nothing in this module may be made to scale with the units a step
counts; a ledger holding one record per file would be ``O(all history)`` on
every snapshot read.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_CFG = ConfigDict(extra="forbid", populate_by_name=True, validate_assignment=True)

# The one timestamp spelling used here. Second precision, explicit Z — the same
# wire format the persisted job records already use, so a consumer parses one
# shape rather than sniffing two.
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Bounds on the two free-text fields. Both carry text from outside this module
# (a file path, an exception message), so they are clamped AT DEFINITION rather
# than at each writer: progress reporting must never be the thing that makes a
# record too large to store or too wide to render.
_DETAIL_MAX = 200
_NOTE_MAX = 500

StepState = Literal["pending", "running", "done", "skipped", "failed"]

#: The states that end a step. A terminal step contributes its full weight to
#: the bar and is never re-entered.
TERMINAL_STATES: frozenset[str] = frozenset({"done", "skipped", "failed"})


def format_stamp(now: datetime) -> str:
    """Render *now* in the one spelling this module writes and reads.

    Cost: ``O(1)``.
    """
    return now.strftime(_TS_FORMAT)


def parse_stamp(stamp: str | None) -> datetime | None:
    """Parse a stamp written by :func:`format_stamp`, or ``None`` if unusable.

    An unreadable stamp answers ``None`` rather than raising: a malformed
    timestamp must cost a derived number, never the operation reporting it.

    Cost: ``O(1)``.
    """
    if not stamp:
        return None
    try:
        return datetime.strptime(stamp, _TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class StepSpec(BaseModel):
    """One declared unit of work — the plan, written beside the code that runs it.

    Specs are constants. They are what a client renders BEFORE the operation
    starts, which is the whole reason the plan is data: an outline showing
    "Resolving symbols · pending" is legible during an hour of silence in a way
    that a frozen counter never is.
    """

    model_config = _CFG

    key: str = Field(
        description="Stable dotted id, e.g. 'graph.resolve_scip_index'. Greppable, "
        "and stable across releases so a measured duration can be keyed on it."
    )
    label: str = Field(description="Human line: 'Resolving Python symbols'.")
    group: str = Field(
        default="",
        description="Coarse bucket a reader collapses steps under (the wiki "
        "indexer sets the phase name).",
    )
    unit: str | None = Field(
        default=None,
        description="Plural noun this step counts ('files', 'nodes'). None "
        "means UNCOUNTABLE — a single blocking call, reported by elapsed time.",
    )
    weight: float = Field(
        default=1.0,
        description="Relative share of the operation's cost. Hand-set at first; "
        "replaced by a measured duration once one exists.",
    )

    @field_validator("key")
    @classmethod
    def _key_is_addressable(cls, value: str) -> str:
        """A key is an identifier, so an empty or padded one is a defect here.

        Validated at definition because every later reader — the ledger's own
        lookup, a persisted duration table, a test asserting plan parity — uses
        it as a dictionary key, where a stray space is a silent miss.
        """
        key = value.strip()
        if not key:
            raise ValueError("step key must be non-empty")
        return key

    @field_validator("weight")
    @classmethod
    def _weight_is_positive(cls, value: float) -> float:
        """A zero or negative weight makes the bar arithmetic meaningless."""
        if not value > 0:
            raise ValueError("step weight must be greater than zero")
        return value

    @model_validator(mode="after")
    def _dotted_key_belongs_to_group(self) -> StepSpec:
        """Keep a dotted plan address in the group a renderer will collapse it under.

        Reject rather than repair a mismatch: silently deriving a group would make
        an authored plan appear to work while placing its progress under a
        different phase. The reporter isolates this programming error from the
        indexing path; stored records remain strict so corrupt durable state is
        not rendered as a plausible lie.
        """
        group, separator, _ = self.key.partition(".")
        if separator and self.group != group:
            raise ValueError(f"dotted step key {self.key!r} must belong to group {group!r}")
        return self


class StepRecord(BaseModel):
    """One declared step's observed state — minted from a :class:`StepSpec`.

    The spec's descriptive fields are COPIED onto the record rather than
    referenced, so a client holding the ledger needs exactly one object to
    render a step. The cost is a few duplicated strings per operation; the
    alternative is every consumer joining two collections to draw a label.
    """

    model_config = _CFG

    key: str
    label: str
    group: str = ""
    unit: str | None = None
    weight: float = 1.0
    state: StepState = "pending"
    # Aliased because this record is nested inside a job snapshot that is dumped
    # ``by_alias=True``: without them one object on the wire would carry two
    # naming conventions, and a client reading ``startedAt`` everywhere else
    # would silently miss the one field spelled differently.
    started_at: str | None = Field(default=None, alias="startedAt")
    ended_at: str | None = Field(default=None, alias="endedAt")
    current: int | None = None
    total: int | None = None
    detail: str = Field(default="", description="Last unit seen — a file path.")
    note: str = Field(default="", description="Why a step failed or was skipped.")

    @field_validator("detail")
    @classmethod
    def _clamp_detail(cls, value: str) -> str:
        """Truncate rather than reject — a long path must not fail a write."""
        return value[:_DETAIL_MAX]

    @field_validator("note")
    @classmethod
    def _clamp_note(cls, value: str) -> str:
        """Truncate rather than reject; an exception message is unbounded."""
        return value[:_NOTE_MAX]

    @model_validator(mode="after")
    def _has_a_coherent_lifecycle(self) -> StepRecord:
        """Reject states that cannot truthfully describe one step.

        A skipped step may have no start because a known-inapplicable declaration
        is closed without running it. Notes are rejected outside skipped/failed:
        retaining a failure reason after a later state change makes healthy work
        look suspect. Strict construction makes invalid reporter calls loud and
        corrupt stored records visible instead of silently rewritten.
        """
        if self.state == "pending":
            if self.started_at is not None or self.ended_at is not None:
                raise ValueError("pending steps must not carry timestamps")
        elif self.state == "running":
            if self.started_at is None or self.ended_at is not None:
                raise ValueError("running steps require started_at and no ended_at")
        elif self.ended_at is None:
            raise ValueError(f"terminal step {self.state!r} requires ended_at")
        elif self.state in ("done", "failed") and self.started_at is None:
            raise ValueError(f"terminal step {self.state!r} requires started_at")

        started = parse_stamp(self.started_at)
        ended = parse_stamp(self.ended_at)
        if started is not None and ended is not None and ended < started:
            raise ValueError("ended_at must not be earlier than started_at")
        if self.current is not None and self.current < 0:
            raise ValueError("current must not be negative")
        if self.current is not None and self.total is not None and self.current > self.total:
            raise ValueError("current must not exceed total")
        if self.note and self.state not in ("skipped", "failed"):
            raise ValueError("note is only valid for skipped or failed steps")
        return self

    def _replace(self, **changes: Any) -> None:
        """Apply a multi-field transition only after its final state validates.

        Assignment validation guards external mutation, but a lifecycle update
        naturally changes several fields. Constructing first avoids exposing an
        impossible intermediate state such as ``running`` without a start stamp.
        """
        replacement = type(self).model_validate({**self.model_dump(), **changes})
        object.__setattr__(self, "__dict__", replacement.__dict__)
        object.__setattr__(self, "__pydantic_fields_set__", replacement.__pydantic_fields_set__)

    @classmethod
    def from_spec(cls, spec: StepSpec) -> StepRecord:
        """Mint a pending record for *spec*. Cost: ``O(1)``."""
        return cls(
            key=spec.key,
            label=spec.label,
            group=spec.group,
            unit=spec.unit,
            weight=spec.weight,
        )

    @property
    def terminal(self) -> bool:
        """True once this step can no longer move."""
        return self.state in TERMINAL_STATES

    @property
    def counted(self) -> bool:
        """True when this step reports a position a reader can turn into a bar.

        A running step with no total is NOT counted, and that is the state the
        whole ledger exists to make renderable: it has a start time and a label,
        so it reads as "running, 14 min" instead of borrowing the last step's
        numbers.
        """
        return self.current is not None and (self.total or 0) > 0

    def fraction(self) -> float | None:
        """How far through this step, or ``None`` when that is unknowable.

        ``None`` is a real answer, not a missing one: an uncountable step's
        progress is genuinely unknown, and reporting it as ``0.0`` would let a
        caller compute an ETA from a number nobody measured.

        Cost: ``O(1)``.
        """
        if self.state in ("done", "skipped"):
            return 1.0
        if self.state in ("pending", "failed"):
            return 0.0 if self.state == "pending" else None
        if not self.counted:
            return None
        current = self.current or 0
        total = self.total or 1
        return max(0.0, min(1.0, current / total))

    def progressed_weight(self) -> float:
        """The share of this step's weight already spent.

        A terminal step contributes its whole weight — including a FAILED one,
        because a failed step is over and the bar must not stall on it. An
        uncountable running step contributes nothing, which is what keeps the
        bar honest rather than optimistic while it runs.

        Cost: ``O(1)``.
        """
        if self.terminal:
            return self.weight
        done = self.fraction()
        return 0.0 if done is None else self.weight * done

    def elapsed_seconds(self, now: datetime) -> float | None:
        """Seconds this step has been running, or ``None`` if it never started.

        The clock is an ARGUMENT — this model is persisted and wired, and a
        model that reads a clock cannot be tested without patching one.
        """
        started = parse_stamp(self.started_at)
        if started is None:
            return None
        ended = parse_stamp(self.ended_at)
        return ((ended or now) - started).total_seconds()

    def enter(self, now: datetime) -> None:
        """Mark this step running and stamp its own origin.

        The per-step origin is the reason an estimate stays sane across a step
        boundary: a rate measured from the OPERATION's start divided by a
        fraction that only describes the CURRENT step explodes as that fraction
        approaches zero, which is exactly when a reader first looks.

        Cost: ``O(1)``.
        """
        if self.terminal:
            raise ValueError(f"cannot re-enter terminal step {self.key!r}")
        self._replace(
            state="running",
            started_at=format_stamp(now),
            ended_at=None,
            note="",
        )

    def advance(
        self,
        current: int | None = None,
        total: int | None = None,
        *,
        detail: str = "",
    ) -> None:
        """Record a position inside this step. Cost: ``O(1)``.

        Every argument is optional because a step legitimately reports a running
        count with no knowable total (an unbounded fan-out), or a detail with no
        count at all (a blocking call naming what it is working on).
        """
        changes: dict[str, Any] = {}
        if current is not None:
            changes["current"] = current
        if total is not None:
            changes["total"] = total
        if detail:
            changes["detail"] = detail[:_DETAIL_MAX]
        if changes:
            self._replace(**changes)

    def finish(
        self, now: datetime, *, state: StepState = "done", note: str = ""
    ) -> None:
        """Close this step in *state*, stamping when it ended.

        A ``done`` step whose counter never reached its total is snapped to it:
        the step is over, and leaving ``900/921`` on a finished step re-creates
        in one record the same lie the shared register told — a number that
        looks live describing work that has stopped.

        Cost: ``O(1)``.
        """
        changes: dict[str, Any] = {
            "state": state,
            "ended_at": format_stamp(now),
            "note": note[:_NOTE_MAX],
        }
        if state == "done" and self.total is not None:
            changes["current"] = self.total
        self._replace(**changes)


class ProgressLedger(BaseModel):
    """Every declared step of one operation, in declared order.

    This is the object a client fetches to learn what an operation IS, not just
    where it is. It is bounded by the number of declared steps — a plan a human
    wrote — so shipping it on every snapshot read is ``O(1)`` in the data the
    operation processes. **Never let a step be minted per unit of work**; that
    turns a constant-size field into one that grows with the repository and puts
    an unbounded document on an interactive path.
    """

    model_config = _CFG

    version: Literal[1] = Field(
        default=1,
        description="Wire version. A consumer that does not recognise it should "
        "fall back rather than guess at the shape.",
    )
    steps: list[StepRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def _has_unique_grouped_steps(self) -> ProgressLedger:
        """Reject ambiguous lookup addresses and group/key disagreements.

        Repairing stored duplicates by retaining one record discards observed
        work, while choosing a group from the dotted key rewrites an authored
        plan. Rejecting makes both programming errors and malformed persisted
        documents loud; the reporter treats that failure as an omitted progress
        update so indexing itself continues.
        """
        seen: set[str] = set()
        for record in self.steps:
            if record.key in seen:
                raise ValueError(f"progress ledger contains duplicate step key {record.key!r}")
            seen.add(record.key)
            group, separator, _ = record.key.partition(".")
            if separator and record.group != group:
                raise ValueError(
                    f"dotted step key {record.key!r} must belong to group {group!r}"
                )
        return self

    # ── Construction ──────────────────────────────────────────────────────────

    @classmethod
    def from_plan(cls, specs: Sequence[StepSpec]) -> ProgressLedger:
        """Mint a pending record for every declared step. Cost: ``O(steps)``."""
        return cls(steps=[StepRecord.from_spec(spec) for spec in specs])

    def extend(self, specs: Iterable[StepSpec]) -> None:
        """Add records for specs not already present, keeping declared order.

        Idempotent, because it is called once per phase on a pipeline whose
        phases can legitimately re-run: a resume re-enters ``clone`` and
        ``scan`` on a job that already reached ``pages``, and re-minting those
        records would discard the history the ledger exists to hold.

        Cost: ``O(steps)``.
        """
        known = {record.key for record in self.steps}
        additions: list[StepRecord] = []
        for spec in specs:
            if spec.key not in known:
                additions.append(StepRecord.from_spec(spec))
                known.add(spec.key)
        if additions:
            self.steps = [*self.steps, *additions]

    # ── Lookup ────────────────────────────────────────────────────────────────

    def find(self, key: str) -> StepRecord | None:
        """The record for *key*, or ``None`` when the step was never declared.

        Answering ``None`` rather than raising is deliberate: a caller reporting
        progress for an undeclared step has a bug in its PLAN, and the cost of
        that bug must be a missing progress line, never a failed operation.
        """
        for record in self.steps:
            if record.key == key:
                return record
        return None

    @property
    def active(self) -> StepRecord | None:
        """The step currently running, or ``None``.

        First rather than only: a fan-out phase can legitimately have more than
        one open step, and a renderer wants something to name either way.
        """
        for record in self.steps:
            if record.state == "running":
                return record
        return None

    def group_keys(self) -> list[str]:
        """Every declared group, in first-appearance order. Cost: ``O(steps)``."""
        seen: list[str] = []
        for record in self.steps:
            if record.group and record.group not in seen:
                seen.append(record.group)
        return seen

    def steps_in(self, group: str) -> list[StepRecord]:
        """Every record in *group*, in declared order. Cost: ``O(steps)``."""
        return [record for record in self.steps if record.group == group]

    def pending_groups(self) -> list[str]:
        """Groups retaining unfinished work, in declaration order.

        Cost: ``O(steps)``. A caller about to announce terminal completion can
        use this as an explicit invariant check instead of mistaking a plausible
        partial fraction for a finished operation.
        """
        return [
            group
            for group in self.group_keys()
            if any(not record.terminal for record in self.steps_in(group))
        ]

    # ── Mutation ──────────────────────────────────────────────────────────────

    def enter(self, key: str, now: datetime) -> StepRecord | None:
        """Open *key* and close nothing — the caller's scope owns the close.

        Cost: ``O(steps)``.
        """
        record = self.find(key)
        if record is not None:
            record.enter(now)
        return record

    def advance(
        self,
        key: str,
        current: int | None = None,
        total: int | None = None,
        *,
        detail: str = "",
    ) -> StepRecord | None:
        """Record a position inside *key*, if it was declared.

        Cost: ``O(steps)``.
        """
        record = self.find(key)
        if record is not None:
            record.advance(current, total, detail=detail)
        return record

    def finish(
        self, key: str, now: datetime, *, state: StepState = "done", note: str = ""
    ) -> StepRecord | None:
        """Close *key* in *state*, if it was declared.

        Cost: ``O(steps)``.
        """
        record = self.find(key)
        if record is not None:
            record.finish(now, state=state, note=note)
        return record

    # ── Derived numbers ───────────────────────────────────────────────────────

    @property
    def total_weight(self) -> float:
        """Declared cost of the whole operation. Cost: ``O(steps)``."""
        return sum(record.weight for record in self.steps) or 1.0

    @property
    def completed_weight(self) -> float:
        """Cost already spent, counting partial progress inside a counted step."""
        return sum(record.progressed_weight() for record in self.steps)

    def fraction(self) -> float:
        """Whole-operation progress in ``[0, 1]``.

        **Monotonic by construction.** A terminal step contributes its full
        weight forever and a pending one contributes nothing, so the only way
        this can retreat is a counter that itself goes backwards. That is the
        property the previous model could not offer: there, a new step starting
        at ``500/11898`` replaced a finished step's ``921/921`` in the same
        register, and the bar walked backwards every time a phase changed unit.

        Cost: ``O(steps)``.
        """
        return max(0.0, min(1.0, self.completed_weight / self.total_weight))

    def started_at_stamp(self) -> str | None:
        """When the operation's first step opened. Cost: ``O(steps)``."""
        stamps = [record.started_at for record in self.steps if record.started_at]
        return min(stamps) if stamps else None

    def elapsed_seconds(self, now: datetime) -> float | None:
        """Seconds since the first step opened, or ``None`` before that."""
        started = parse_stamp(self.started_at_stamp())
        return None if started is None else (now - started).total_seconds()

    def eta_seconds(self, now: datetime) -> float | None:
        """Seconds of work left, measured against this run's own rate.

        The estimate is ``remaining weight ÷ (weight spent ÷ elapsed)`` — a rate
        measured over the WHOLE operation rather than the current step, so
        crossing a step boundary moves both terms continuously and no
        discontinuity exists to blow the number up. It is also self-calibrating:
        the weights only need to be right RELATIVE to one another, so a run on a
        slow machine and a run on a fast one both converge.

        ``None`` means there is genuinely nothing to extrapolate from — nothing
        has started, no weight has been spent, or every step is already
        terminal. Answering ``None`` rather than ``0`` matters: a zero reads as
        "finished", which is the exact misreport the shared register produced
        while an hour of work remained.

        Cost: ``O(steps)``.
        """
        elapsed = self.elapsed_seconds(now)
        if elapsed is None or elapsed <= 0:
            return None
        spent = self.completed_weight
        if spent <= 0:
            return None
        remaining = self.total_weight - spent
        if remaining <= 0:
            return None
        return remaining * (elapsed / spent)

    # ── Export ────────────────────────────────────────────────────────────────

    def export(self, now: datetime) -> dict[str, Any]:
        """The whole operation as one self-describing object.

        Everything a client needs to render an operation it has never seen: the
        declared outline grouped as a reader would collapse it, each step's own
        state and clock, and the derived numbers computed ONCE here rather than
        re-derived by every surface. The console, the CLI and an MCP consumer
        read the same fields, so they cannot disagree about what a step means.

        The clock is an argument, so this is a pure projection — two calls with
        the same *now* return the same object.

        Cost: ``O(steps)``.
        """
        active = self.active
        return {
            "version": self.version,
            "fraction": self.fraction(),
            "etaSeconds": self.eta_seconds(now),
            "elapsedSeconds": self.elapsed_seconds(now),
            "activeKey": active.key if active is not None else None,
            "groups": [
                {
                    "key": group,
                    "steps": [
                        record.model_dump(mode="json", by_alias=True)
                        for record in self.steps_in(group)
                    ],
                }
                for group in self.group_keys()
            ],
            "steps": [
                record.model_dump(mode="json", by_alias=True) for record in self.steps
            ],
        }

    def describe(self, now: datetime) -> str:
        """A one-line human summary of where the operation is.

        For a log line or a CLI status row — the surfaces that have no room for
        the outline but still need an answer better than a bare percentage.

        Cost: ``O(steps)``.
        """
        active = self.active
        pct = round(self.fraction() * 100)
        if active is None:
            return f"{pct}%"
        if active.counted:
            position = f"{active.current} of {active.total} {active.unit or 'units'}"
        else:
            seconds = active.elapsed_seconds(now)
            position = "running" if seconds is None else f"running {int(seconds)}s"
        return f"{pct}% · {active.label} · {position}"
