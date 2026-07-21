"""Tests for the trigger admission policy (mewbo_core.triggers.policy).

Covers TriggerPolicy.admit(): default-expiry stamping, the armed-count
ceiling, the max_fires cap, and the cron minimum-interval gate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from mewbo_core.triggers.policy import TriggerPolicy
from mewbo_core.triggers.spec import CronTrigger, TimeAtTrigger

NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


def _time_at(**overrides):
    defaults = dict(
        session_id="s1",
        wake_prompt="wake up",
        created_by="agent",
        at=NOW + timedelta(hours=1),
        created_at=NOW,
    )
    defaults.update(overrides)
    return TimeAtTrigger(**defaults)


def _cron(**overrides):
    defaults = dict(
        session_id="s1",
        wake_prompt="w",
        created_by="agent",
        cron="*/5 * * * *",
        created_at=NOW,
    )
    defaults.update(overrides)
    return CronTrigger(**defaults)


def test_policy_defaults():
    """Sane, documented defaults — the values every deployment inherits."""
    policy = TriggerPolicy()
    assert policy.max_armed_per_session == 20
    assert policy.max_fires_cap == 100
    assert policy.default_expiry == timedelta(days=7)
    assert policy.cron_min_interval_seconds == 60
    assert policy.webhook_payload_max_bytes == 200_000


def test_extra_fields_forbidden():
    """extra='forbid' rejects an unknown knob — no silent typo'd config key."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TriggerPolicy(bogus=1)


# ---------------------------------------------------------------------------
# default_expiry stamping
# ---------------------------------------------------------------------------


def test_admit_stamps_default_expiry_when_missing():
    """A spec with no expires_at gets created_at + default_expiry stamped."""
    policy = TriggerPolicy(default_expiry=timedelta(days=3))
    spec = _time_at(expires_at=None)
    admitted = policy.admit(spec, armed_count=0)
    assert admitted.expires_at == spec.created_at + timedelta(days=3)


def test_admit_preserves_explicit_expires_at():
    """An explicit expires_at is never overridden by the policy default."""
    policy = TriggerPolicy()
    explicit = NOW + timedelta(hours=2)
    spec = _time_at(expires_at=explicit)
    admitted = policy.admit(spec, armed_count=0)
    assert admitted.expires_at == explicit


def test_admit_does_not_mutate_original_spec():
    """admit() returns a NEW instance via model_copy — the caller's object is untouched."""
    policy = TriggerPolicy()
    spec = _time_at(expires_at=None)
    admitted = policy.admit(spec, armed_count=0)
    assert admitted is not spec
    assert spec.expires_at is None


# ---------------------------------------------------------------------------
# max_armed_per_session ceiling
# ---------------------------------------------------------------------------


def test_admit_rejects_at_armed_ceiling():
    """armed_count == max_armed_per_session is already at the ceiling — rejected."""
    policy = TriggerPolicy(max_armed_per_session=2)
    spec = _time_at()
    with pytest.raises(ValueError, match="max_armed_per_session"):
        policy.admit(spec, armed_count=2)


def test_admit_allows_below_armed_ceiling():
    policy = TriggerPolicy(max_armed_per_session=2)
    spec = _time_at()
    admitted = policy.admit(spec, armed_count=1)
    assert admitted.id == spec.id


# ---------------------------------------------------------------------------
# max_fires cap
# ---------------------------------------------------------------------------


def test_admit_rejects_max_fires_over_cap():
    policy = TriggerPolicy(max_fires_cap=5)
    spec = _cron(max_fires=10)
    with pytest.raises(ValueError, match="max_fires_cap"):
        policy.admit(spec, armed_count=0)


def test_admit_allows_max_fires_within_cap():
    policy = TriggerPolicy(max_fires_cap=5)
    spec = _cron(max_fires=5)
    admitted = policy.admit(spec, armed_count=0)
    assert admitted.max_fires == 5


def test_admit_allows_unlimited_max_fires():
    """max_fires=None (unlimited) never trips the cap."""
    policy = TriggerPolicy(max_fires_cap=1)
    spec = _cron(max_fires=None)
    admitted = policy.admit(spec, armed_count=0)
    assert admitted.max_fires is None


def test_time_at_forced_single_fire_always_within_cap():
    """TimeAtTrigger forces max_fires=1 — even a cap of 1 admits it."""
    policy = TriggerPolicy(max_fires_cap=1)
    spec = _time_at()
    assert spec.max_fires == 1
    admitted = policy.admit(spec, armed_count=0)
    assert admitted.max_fires == 1


# ---------------------------------------------------------------------------
# cron minimum interval
# ---------------------------------------------------------------------------


def test_admit_rejects_cron_below_min_interval():
    """Every-minute cron fires every 60s; a 1-hour policy floor rejects it."""
    policy = TriggerPolicy(cron_min_interval_seconds=3600)
    spec = _cron(cron="* * * * *")
    with pytest.raises(ValueError, match="cron_min_interval_seconds"):
        policy.admit(spec, armed_count=0)


def test_admit_allows_cron_at_exactly_min_interval():
    """An hourly cron against a 1-hour floor is allowed (not-less-than, not strict)."""
    policy = TriggerPolicy(cron_min_interval_seconds=3600)
    spec = _cron(cron="0 * * * *")
    admitted = policy.admit(spec, armed_count=0)
    assert admitted.cron == "0 * * * *"


def test_admit_allows_cron_above_min_interval():
    policy = TriggerPolicy(cron_min_interval_seconds=60)
    spec = _cron(cron="*/5 * * * *")
    admitted = policy.admit(spec, armed_count=0)
    assert admitted.id == spec.id


def test_non_cron_spec_skips_cron_check():
    """A TimeAtTrigger never runs the cron-interval gate at all."""
    policy = TriggerPolicy(cron_min_interval_seconds=999_999_999)
    spec = _time_at()
    admitted = policy.admit(spec, armed_count=0)
    assert admitted.id == spec.id
