"""Tests for the trigger domain model (mewbo_core.triggers.spec).

Covers lifecycle transitions, record_fire/max_fires completion, per-kind
validators, matches()/verify() truth tables, cron due-ness math, and
discriminated-union parse_trigger() round-tripping.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import pytest
from mewbo_core.triggers.spec import (
    CiWorkflowTrigger,
    CronTrigger,
    ForgePrTrigger,
    TimeAtTrigger,
    TriggerSpec,
    WebhookTrigger,
    parse_trigger,
)
from pydantic import ValidationError

NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


def _time_at(**overrides):
    defaults = dict(
        session_id="s1",
        wake_prompt="wake up",
        created_by="user",
        at=NOW + timedelta(hours=1),
    )
    defaults.update(overrides)
    return TimeAtTrigger(**defaults)


# ---------------------------------------------------------------------------
# Base — required fields, extra=forbid
# ---------------------------------------------------------------------------


def test_trigger_spec_requires_kind():
    """Bare TriggerSpec is not meaningfully instantiable — kind is required."""
    with pytest.raises(ValidationError):
        TriggerSpec(session_id="s1", wake_prompt="w", created_by="user")


def test_extra_fields_forbidden():
    """extra='forbid' rejects an unknown field on any concrete kind."""
    with pytest.raises(ValidationError):
        TimeAtTrigger(
            session_id="s1",
            wake_prompt="w",
            created_by="user",
            at=NOW,
            bogus="nope",
        )


# ---------------------------------------------------------------------------
# Lifecycle: transition()
# ---------------------------------------------------------------------------


def test_transition_resting_pair():
    """armed <-> paused is legal in both directions."""
    t = _time_at()
    t.transition("paused")
    assert t.status == "paused"
    t.transition("armed")
    assert t.status == "armed"


def test_transition_noop_same_status():
    """Transitioning to the current status is a silent success."""
    t = _time_at()
    t.transition("armed")
    assert t.status == "armed"


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled", "expired"])
def test_transition_resting_to_terminal(terminal):
    """A resting trigger may move directly into any terminal status."""
    t = _time_at()
    t.transition(terminal)
    assert t.status == terminal
    assert t.is_terminal


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled", "expired"])
def test_transition_out_of_terminal_raises(terminal):
    """A terminal status is absorbing — any further transition raises."""
    t = _time_at()
    t.transition(terminal)
    with pytest.raises(ValueError):
        t.transition("armed")


def test_transition_illegal_value_raises():
    """An unrecognised status string is rejected, not silently accepted."""
    t = _time_at()
    with pytest.raises(ValueError):
        t.transition("bogus")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# record_fire / max_fires completion
# ---------------------------------------------------------------------------


def test_record_fire_increments_and_stamps():
    """record_fire bumps fires and stamps last_fired_at/last_error."""
    t = CronTrigger(
        session_id="s1", wake_prompt="w", created_by="agent", cron="*/5 * * * *", max_fires=3
    )
    t.record_fire(NOW)
    assert t.fires == 1
    assert t.last_fired_at == NOW
    assert t.last_error is None
    assert t.status == "armed"


def test_record_fire_completes_at_max_fires():
    """Reaching max_fires auto-transitions to completed."""
    t = CronTrigger(
        session_id="s1", wake_prompt="w", created_by="agent", cron="*/5 * * * *", max_fires=2
    )
    t.record_fire(NOW)
    assert t.status == "armed"
    t.record_fire(NOW + timedelta(minutes=5))
    assert t.status == "completed"
    assert t.fires == 2


def test_record_fire_with_error_still_counts():
    """An errored fire still increments fires and records last_error."""
    t = CronTrigger(
        session_id="s1", wake_prompt="w", created_by="agent", cron="*/5 * * * *", max_fires=5
    )
    t.record_fire(NOW, error="boom")
    assert t.fires == 1
    assert t.last_error == "boom"
    assert t.status == "armed"


def test_time_at_forces_single_fire_and_auto_completes():
    """TimeAtTrigger forces max_fires=1 regardless of input, then auto-completes."""
    t = _time_at(max_fires=99)
    assert t.max_fires == 1
    t.record_fire(NOW + timedelta(hours=2))
    assert t.status == "completed"
    assert t.fires == 1


# ---------------------------------------------------------------------------
# is_due / next_fire_at
# ---------------------------------------------------------------------------


def test_time_at_next_fire_at_and_is_due():
    """TimeAtTrigger is due once now passes `at`, and never again after firing."""
    at = NOW + timedelta(hours=1)
    t = _time_at(at=at)
    assert not t.is_due(NOW)
    assert t.is_due(at)
    assert t.is_due(at + timedelta(minutes=1))
    t.record_fire(at)
    assert t.next_fire_at(at + timedelta(hours=5)) is None


def test_is_due_false_when_not_armed():
    """A paused or terminal trigger never polls due, even past its fire time."""
    t = _time_at(at=NOW - timedelta(hours=1))
    t.transition("paused")
    assert not t.is_due(NOW)


def test_default_next_fire_at_is_none_for_event_kinds():
    """ci.workflow/forge.pr/webhook never poll due by clock — they fire via matches()/verify()."""
    ci = CiWorkflowTrigger(
        session_id="s1", wake_prompt="w", created_by="agent", repo="o/r", workflow="build"
    )
    assert ci.next_fire_at(NOW) is None
    assert not ci.is_due(NOW)


def test_is_expired():
    """is_expired is true once now passes expires_at on a still-resting trigger."""
    t = _time_at(expires_at=NOW + timedelta(minutes=30))
    assert not t.is_expired(NOW)
    assert t.is_expired(NOW + timedelta(hours=1))
    t.transition("cancelled")
    assert not t.is_expired(NOW + timedelta(hours=1))  # terminal — no longer "expired"


# ---------------------------------------------------------------------------
# Timezone-aware datetime validation (naive datetimes are rejected, not coerced)
# ---------------------------------------------------------------------------


def test_time_at_rejects_naive_datetime():
    """A naive `at` (no tzinfo) fails validation instead of being silently assumed UTC."""
    with pytest.raises(ValidationError):
        _time_at(at=datetime(2026, 7, 13, 12, 0, 0))


def test_expires_at_rejects_naive_datetime():
    """A naive `expires_at` fails validation — the base-class guard applies to every kind."""
    with pytest.raises(ValidationError):
        _time_at(expires_at=datetime(2026, 7, 13, 12, 0, 0))


def test_aware_at_and_expires_at_still_accepted():
    """Aware datetimes (any offset, not just UTC) pass validation unchanged."""
    plus_530 = timezone(timedelta(hours=5, minutes=30))
    t = _time_at(
        at=datetime(2026, 7, 13, 18, 0, 0, tzinfo=plus_530),
        expires_at=datetime(2026, 7, 14, 0, 0, 0, tzinfo=plus_530),
    )
    assert t.at.tzinfo is not None
    assert t.expires_at is not None and t.expires_at.tzinfo is not None


def test_naive_datetime_error_message_is_informative():
    """The validation error names the offense and offers a fix, not a bare traceback."""
    with pytest.raises(ValidationError) as excinfo:
        _time_at(at=datetime(2026, 7, 13, 12, 0, 0))
    message = str(excinfo.value)
    assert "timezone offset" in message
    assert "Z" in message or "+05:30" in message


# ---------------------------------------------------------------------------
# CronTrigger — validator + next_fire_at math
# ---------------------------------------------------------------------------


def test_cron_validator_rejects_bad_expression():
    """An unparseable cron string fails validation."""
    with pytest.raises(ValidationError):
        CronTrigger(session_id="s1", wake_prompt="w", created_by="agent", cron="not a cron")


def test_cron_next_fire_at_anchors_on_last_fired_or_created():
    """next_fire_at is strictly after last_fired_at (or created_at pre-first-fire)."""
    created = NOW
    t = CronTrigger(
        session_id="s1",
        wake_prompt="w",
        created_by="agent",
        cron="0 * * * *",  # top of every hour
        created_at=created,
    )
    nxt = t.next_fire_at(created)
    assert nxt is not None
    assert nxt > created
    assert nxt.minute == 0

    t.record_fire(nxt)
    nxt2 = t.next_fire_at(nxt)
    assert nxt2 > nxt


def test_cron_is_due_gates_on_now():
    """is_due only fires once `now` has actually reached the computed next occurrence."""
    t = CronTrigger(
        session_id="s1", wake_prompt="w", created_by="agent", cron="0 * * * *", created_at=NOW
    )
    nxt = t.next_fire_at(NOW)
    assert not t.is_due(nxt - timedelta(minutes=1))
    assert t.is_due(nxt)


# ---------------------------------------------------------------------------
# CiWorkflowTrigger — validators + matches() truth table
# ---------------------------------------------------------------------------


def test_ci_workflow_requires_exactly_one_selector():
    """Neither or both of run_id/workflow set is a validation error."""
    with pytest.raises(ValidationError):
        CiWorkflowTrigger(session_id="s1", wake_prompt="w", created_by="agent", repo="o/r")
    with pytest.raises(ValidationError):
        CiWorkflowTrigger(
            session_id="s1",
            wake_prompt="w",
            created_by="agent",
            repo="o/r",
            run_id=1,
            workflow="build",
        )


def test_ci_workflow_repo_must_be_owner_slash_name():
    """repo must be 'owner/name'."""
    with pytest.raises(ValidationError):
        CiWorkflowTrigger(
            session_id="s1", wake_prompt="w", created_by="agent", repo="not-a-repo", run_id=1
        )


@pytest.mark.parametrize(
    "payload,expected",
    [
        (
            {"repo": "o/r", "workflow": "build", "status": "completed", "conclusion": "success"},
            True,
        ),
        (
            {"repo": "o/r", "workflow": "build", "status": "completed", "conclusion": "failure"},
            False,
        ),
        (
            {"repo": "o/r", "workflow": "build", "status": "in_progress", "conclusion": "success"},
            False,
        ),
        (
            {
                "repo": "other/r",
                "workflow": "build",
                "status": "completed",
                "conclusion": "success",
            },
            False,
        ),
        (
            {"repo": "o/r", "workflow": "other", "status": "completed", "conclusion": "success"},
            False,
        ),
    ],
)
def test_ci_workflow_matches_truth_table(payload, expected):
    """matches() evaluates repo/workflow/status/conclusion_filter."""
    trigger = CiWorkflowTrigger(
        session_id="s1",
        wake_prompt="w",
        created_by="agent",
        repo="o/r",
        workflow="build",
        conclusion_filter=["success"],
    )
    assert trigger.matches(payload) is expected


def test_ci_workflow_matches_by_run_id():
    """The run_id selector variant matches on run_id instead of workflow."""
    trigger = CiWorkflowTrigger(
        session_id="s1", wake_prompt="w", created_by="agent", repo="o/r", run_id=42
    )
    assert trigger.matches({"repo": "o/r", "run_id": 42, "status": "completed"})
    assert not trigger.matches({"repo": "o/r", "run_id": 99, "status": "completed"})


# ---------------------------------------------------------------------------
# ForgePrTrigger — validators + matches() truth table
# ---------------------------------------------------------------------------


def test_forge_pr_events_must_be_non_empty():
    """An empty events list fails validation."""
    with pytest.raises(ValidationError):
        ForgePrTrigger(
            session_id="s1", wake_prompt="w", created_by="agent", repo="o/r", number=1, events=[]
        )


def test_forge_pr_repo_must_be_owner_slash_name():
    """repo must be 'owner/name'."""
    with pytest.raises(ValidationError):
        ForgePrTrigger(
            session_id="s1",
            wake_prompt="w",
            created_by="agent",
            repo="bad",
            number=1,
            events=["merged"],
        )


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"repo": "o/r", "number": 7, "event": "merged"}, True),
        ({"repo": "o/r", "number": 7, "event": "comment"}, False),
        ({"repo": "o/r", "number": 8, "event": "merged"}, False),
        ({"repo": "other/r", "number": 7, "event": "merged"}, False),
    ],
)
def test_forge_pr_matches_truth_table(payload, expected):
    """matches() evaluates repo/number/event membership."""
    trigger = ForgePrTrigger(
        session_id="s1",
        wake_prompt="w",
        created_by="agent",
        repo="o/r",
        number=7,
        events=["merged", "ci_status"],
    )
    assert trigger.matches(payload) is expected


# ---------------------------------------------------------------------------
# WebhookTrigger — verify()
# ---------------------------------------------------------------------------


def test_webhook_secret_defaults_to_128_bit_hex():
    """The default secret is a 128-bit (32 hex char) token."""
    t = WebhookTrigger(session_id="s1", wake_prompt="w", created_by="agent")
    assert len(t.secret) == 32
    int(t.secret, 16)  # valid hex


def test_webhook_verify_no_hmac_header_configured():
    """Without hmac_header, only the secret is checked."""
    t = WebhookTrigger(session_id="s1", wake_prompt="w", created_by="agent")
    assert t.verify(t.secret, {}, b"anything")
    assert not t.verify("wrong-secret", {}, b"anything")


def test_webhook_verify_good_and_bad_hmac():
    """With hmac_header configured, the HMAC-SHA256 over body must also match."""
    t = WebhookTrigger(session_id="s1", wake_prompt="w", created_by="agent", hmac_header="X-Sig")
    body = b'{"hello":"world"}'
    good_sig = hmac.new(t.secret.encode(), body, hashlib.sha256).hexdigest()

    assert t.verify(t.secret, {"X-Sig": f"sha256={good_sig}"}, body)
    assert not t.verify(t.secret, {"X-Sig": "sha256=deadbeef"}, body)
    assert not t.verify(t.secret, {}, body)  # header missing entirely
    assert not t.verify("wrong-secret", {"X-Sig": f"sha256={good_sig}"}, body)


def test_webhook_verify_hmac_without_prefix():
    """A header value with no 'algo=' prefix is compared as-is."""
    t = WebhookTrigger(session_id="s1", wake_prompt="w", created_by="agent", hmac_header="X-Sig")
    body = b"payload"
    sig = hmac.new(t.secret.encode(), body, hashlib.sha256).hexdigest()
    assert t.verify(t.secret, {"X-Sig": sig}, body)


# ---------------------------------------------------------------------------
# Discriminated union round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "trigger",
    [
        _time_at(),
        CronTrigger(session_id="s1", wake_prompt="w", created_by="agent", cron="*/5 * * * *"),
        CiWorkflowTrigger(
            session_id="s1", wake_prompt="w", created_by="agent", repo="o/r", workflow="build"
        ),
        ForgePrTrigger(
            session_id="s1",
            wake_prompt="w",
            created_by="agent",
            repo="o/r",
            number=1,
            events=["merged"],
        ),
        WebhookTrigger(session_id="s1", wake_prompt="w", created_by="agent"),
    ],
)
def test_parse_trigger_round_trip(trigger):
    """model_dump(mode='json') -> parse_trigger() recovers the exact concrete kind."""
    data = trigger.model_dump(mode="json")
    parsed = parse_trigger(data)
    assert type(parsed) is type(trigger)
    assert parsed.model_dump(mode="json") == data


def test_parse_trigger_dispatches_on_kind_field():
    """parse_trigger picks the right subclass purely from the 'kind' discriminator."""
    data = {
        "kind": "webhook",
        "session_id": "s1",
        "wake_prompt": "w",
        "created_by": "user",
    }
    parsed = parse_trigger(data)
    assert isinstance(parsed, WebhookTrigger)
