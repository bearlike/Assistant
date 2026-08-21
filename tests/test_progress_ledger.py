"""Contract tests for the declared-step progress ledger.

The clock is always supplied by the caller: these cases exercise progress across
long-running boundaries without sleeps or a wall-clock dependency.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from mewbo_core.contracts.progress import ProgressLedger, StepRecord, StepSpec
from pydantic import ValidationError

T0 = datetime.fromtimestamp(0, tz=timezone.utc)


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _spec(
    key: str,
    *,
    label: str | None = None,
    group: str = "work",
    unit: str | None = "items",
    weight: float = 1.0,
) -> StepSpec:
    return StepSpec(
        key=key,
        label=label or key.replace(".", " ").title(),
        group=group,
        unit=unit,
        weight=weight,
    )


def test_a_terminal_count_cannot_read_as_whole_operation_completion() -> None:
    """A completed record keeps only its own weight while later work remains pending."""
    ledger = ProgressLedger.from_plan([_spec("clone"), _spec("scan")])
    ledger.enter("clone", T0)
    ledger.advance("clone", 100, 100)
    ledger.finish("clone", _at(60))

    assert ledger.fraction() == pytest.approx(0.5)
    assert ledger.eta_seconds(_at(60)) not in (None, 0.0)


def test_an_uncountable_running_step_reports_elapsed_never_a_fraction() -> None:
    """An open blocking call has a named elapsed state, not invented counter progress."""
    ledger = ProgressLedger.from_plan([_spec("publish", label="Publishing", unit=None)])
    record = ledger.enter("publish", T0)

    assert record is not None
    assert record.fraction() is None
    assert record.counted is False
    assert record.progressed_weight() == 0.0
    assert record.elapsed_seconds(_at(42)) == 42.0
    assert ledger.describe(_at(42)) == "0% · Publishing · running 42s"


def test_the_bar_is_monotonic_across_a_step_boundary() -> None:
    """A completed record retains its weight when the next unit starts from zero."""
    ledger = ProgressLedger.from_plan([_spec("clone"), _spec("scan")])
    fractions = [ledger.fraction()]

    ledger.enter("clone", T0)
    ledger.advance("clone", 30, 100)
    fractions.append(ledger.fraction())
    ledger.advance("clone", 100, 100)
    fractions.append(ledger.fraction())
    ledger.finish("clone", _at(30))
    fractions.append(ledger.fraction())
    ledger.enter("scan", _at(31))
    fractions.append(ledger.fraction())
    ledger.advance("scan", 10, 100)
    fractions.append(ledger.fraction())

    assert fractions == sorted(fractions)


def test_the_estimate_does_not_explode_at_a_step_boundary() -> None:
    """A twofold bound permits one second of boundary overhead, not an order jump.

    The whole-operation rate makes a fully counted first step and a just-started
    second step nearly continuous, so an ETA after the boundary must remain within
    twice the value immediately before it.
    """
    ledger = ProgressLedger.from_plan([_spec("clone"), _spec("scan")])
    ledger.enter("clone", T0)
    ledger.advance("clone", 100, 100)
    before = ledger.eta_seconds(_at(100))
    ledger.finish("clone", _at(100))
    ledger.enter("scan", _at(101))
    ledger.advance("scan", 1, 100)
    after = ledger.eta_seconds(_at(101))

    assert before is not None
    assert after is not None
    assert after <= before * 2


def test_an_eta_of_zero_is_never_reported_while_work_remains() -> None:
    """No baseline answers None; measured partial work with a pending step is positive."""
    ledger = ProgressLedger.from_plan([_spec("clone"), _spec("scan")])

    assert ledger.eta_seconds(_at(1)) is None
    ledger.enter("clone", T0)
    assert ledger.eta_seconds(_at(1)) is None

    ledger.advance("clone", 100, 100)
    assert ledger.eta_seconds(_at(10)) not in (None, 0.0)


def test_a_failed_step_contributes_its_full_weight_without_stalling_the_bar() -> None:
    """Failure ends the declared unit, leaving subsequent work able to move the bar."""
    ledger = ProgressLedger.from_plan([_spec("clone"), _spec("scan")])
    ledger.enter("clone", T0)
    ledger.finish("clone", _at(5), state="failed", note="unavailable")
    after_failure = ledger.fraction()
    ledger.enter("scan", _at(6))
    ledger.advance("scan", 50, 100)

    assert after_failure == pytest.approx(0.5)
    assert ledger.fraction() == pytest.approx(0.75)


def test_extend_is_idempotent_and_preserves_existing_history() -> None:
    """Re-entering a resumable phase must not mint a second record or erase its past."""
    plan = [_spec("clone"), _spec("scan")]
    ledger = ProgressLedger()
    ledger.extend(plan)
    ledger.enter("clone", T0)
    ledger.advance("clone", 3, 3, detail="repository")
    ledger.finish("clone", _at(3))
    finished = ledger.find("clone")

    assert finished is not None
    history = finished.model_dump(mode="json")
    ledger.extend(plan)

    assert len(ledger.steps) == 2
    assert ledger.find("clone") is finished
    assert ledger.find("clone").model_dump(mode="json") == history


def test_undeclared_progress_operations_answer_none_and_leave_the_ledger_alone() -> None:
    """A plan mismatch costs a missing progress update rather than the operation itself."""
    ledger = ProgressLedger.from_plan([_spec("clone")])
    before = ledger.model_dump(mode="json")

    assert ledger.find("undeclared") is None
    assert ledger.advance("undeclared", 1, 2) is None
    assert ledger.finish("undeclared", _at(1)) is None
    assert ledger.model_dump(mode="json") == before


def test_definition_clamps_text_and_refuses_non_positive_weights_or_blank_keys() -> None:
    """External detail and note text are bounded where records are defined."""
    record = StepRecord(
        key="clone",
        label="Cloning",
        state="skipped",
        ended_at="1970-01-01T00:00:01Z",
        detail="d" * 201,
        note="n" * 501,
    )

    assert len(record.detail) == 200
    assert len(record.note) == 500
    for weight in (0.0, -1.0):
        with pytest.raises(ValidationError):
            _spec("clone", weight=weight)
    for key in ("", " \t "):
        with pytest.raises(ValidationError):
            _spec(key)


def test_export_is_a_pure_projection_with_consistent_grouped_and_flat_steps() -> None:
    """Every reader receives the same records whether it renders groups or a flat list."""
    ledger = ProgressLedger.from_plan([
        _spec("clone", group="prepare"),
        _spec("scan", group="index"),
    ])
    ledger.enter("clone", T0)
    ledger.finish("clone", _at(2))
    ledger.enter("scan", _at(3))
    ledger.advance("scan", 1, 4)

    first = ledger.export(_at(4))
    second = ledger.export(_at(4))
    grouped_steps = [step for group in first["groups"] for step in group["steps"]]

    assert first == second
    assert grouped_steps == first["steps"]
    assert first["activeKey"] == "scan"


def test_a_pending_step_carries_no_timestamps() -> None:
    """A step that never opened cannot truthfully carry either lifecycle stamp."""
    with pytest.raises(ValidationError, match="pending steps"):
        StepRecord(key="clone", label="Cloning", started_at="1970-01-01T00:00:00Z")


def test_a_running_step_requires_only_its_start_stamp() -> None:
    """An open step has a known origin and no false terminal time."""
    for fields in ({}, {"ended_at": "1970-01-01T00:00:01Z"}):
        with pytest.raises(ValidationError, match="running steps"):
            StepRecord(key="clone", label="Cloning", state="running", **fields)


def test_terminal_steps_require_an_end_and_started_work_except_skips() -> None:
    """Known-inapplicable work may close without ever entering running."""
    with pytest.raises(ValidationError, match="requires ended_at"):
        StepRecord(key="clone", label="Cloning", state="done")
    with pytest.raises(ValidationError, match="requires started_at"):
        StepRecord(key="clone", label="Cloning", state="failed", ended_at="1970-01-01T00:00:01Z")

    skipped = StepRecord(
        key="clone",
        label="Cloning",
        state="skipped",
        ended_at="1970-01-01T00:00:01Z",
    )

    assert skipped.started_at is None


def test_a_step_cannot_end_before_it_starts() -> None:
    """Elapsed work cannot be negative even when a stored record is malformed."""
    with pytest.raises(ValidationError, match="must not be earlier"):
        StepRecord(
            key="clone",
            label="Cloning",
            state="done",
            started_at="1970-01-01T00:00:02Z",
            ended_at="1970-01-01T00:00:01Z",
        )


def test_current_is_non_negative_and_never_exceeds_a_known_total() -> None:
    """A position remains meaningful even when its denominator is unknowable."""
    for fields in ({"current": -1}, {"current": 2, "total": 1}):
        with pytest.raises(ValidationError):
            StepRecord(key="clone", label="Cloning", **fields)

    unbounded = StepRecord(key="clone", label="Cloning", unit="files", current=2)

    assert unbounded.total is None


def test_notes_are_rejected_outside_terminal_exception_states() -> None:
    """A stale reason must not make a pending or healthy step look unhealthy."""
    with pytest.raises(ValidationError, match="only valid"):
        StepRecord(key="clone", label="Cloning", note="not applicable")


def test_dotted_step_keys_must_match_their_group() -> None:
    """A renderer can trust each dotted step's phase grouping."""
    with pytest.raises(ValidationError, match="must belong to group 'clone'"):
        StepSpec(key="clone.acquire", label="Acquiring", group="scan")


def test_a_ledger_rejects_duplicate_step_keys() -> None:
    """Every lookup address resolves to one declared record."""
    with pytest.raises(ValidationError, match="duplicate step key"):
        ProgressLedger(steps=[
            StepRecord(key="clone", label="Cloning"),
            StepRecord(key="clone", label="Cloning again"),
        ])


def test_a_ledger_rejects_a_dotted_key_in_another_group() -> None:
    """Stored records cannot silently render under a phase their key denies."""
    with pytest.raises(ValidationError, match="must belong to group 'clone'"):
        ProgressLedger.model_validate({
            "steps": [{"key": "clone.acquire", "label": "Acquiring", "group": "scan"}]
        })


def test_mutation_methods_apply_lifecycle_updates_without_invalid_intermediate_states() -> None:
    """Assignment validation guards external writes while methods commit whole transitions."""
    record = StepRecord(key="clone", label="Cloning")

    record.enter(T0)
    record.advance(current=1, total=1)
    record.finish(_at(1))

    assert record.state == "done"
    assert record.current == record.total == 1


def test_pending_groups_make_an_incomplete_terminal_plan_observable() -> None:
    """A finalizer can detect a declared phase that no run path settled."""
    ledger = ProgressLedger.from_plan([
        _spec("clone.acquire", group="clone"),
        _spec("scan.files", group="scan"),
    ])
    ledger.finish("clone.acquire", T0, state="skipped", note="reused")

    assert ledger.pending_groups() == ["scan"]

    ledger.finish("scan.files", _at(1), state="skipped", note="reused")
    assert ledger.pending_groups() == []


def test_a_terminal_step_cannot_be_reentered() -> None:
    """A completed aggregate cannot silently restart for the next fan-out unit."""
    ledger = ProgressLedger.from_plan([_spec("pages.write", group="pages")])
    ledger.enter("pages.write", T0)
    ledger.finish("pages.write", _at(1))

    with pytest.raises(ValueError, match="cannot re-enter terminal step"):
        ledger.enter("pages.write", _at(2))


def test_an_unparseable_stamp_costs_a_derived_number_not_an_exception() -> None:
    """Legacy or corrupt timestamp text leaves elapsed time unknown without breaking status."""
    record = StepRecord.model_validate({
        "key": "clone",
        "label": "Cloning",
        "state": "running",
        "started_at": "not-a-stamp",
    })

    assert record.elapsed_seconds(_at(1)) is None
