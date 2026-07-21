"""Contract tests for the declared-schedule union + the PipelineSpec floor (Phase 1).

Covers the ``CronSchedule``/``AtSchedule`` discriminated union (validation at
definition, discriminator parsing of the plugin's dumped shape), the
``to_trigger_spec`` strategy-on-model (it builds the core ``TriggerSpec`` the
lifecycle arms), and the ``PipelineSpec`` submit-time floor that makes the
silent-unscheduled state unrepresentable. Every clock read is an injected NOW.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from mewbo_api.apps.models import (
    AtSchedule,
    CronSchedule,
    PipelineSpec,
)
from mewbo_core.triggers.spec import CronTrigger, TimeAtTrigger
from pydantic import ValidationError

NOW = datetime(2026, 7, 17, 9, 0, 0, tzinfo=timezone.utc)
AWARE_AT = datetime(2026, 7, 20, 9, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# CronSchedule / AtSchedule — validation at definition + extra="forbid"
# ---------------------------------------------------------------------------


def test_cron_schedule_accepts_valid_expression():
    sched = CronSchedule(cron="0 7 * * *")
    assert sched.kind == "time.cron"
    assert sched.cron == "0 7 * * *"


def test_cron_schedule_rejects_invalid_expression_at_definition():
    with pytest.raises(ValidationError):
        CronSchedule(cron="not a cron")


def test_cron_schedule_extra_field_forbidden():
    with pytest.raises(ValidationError):
        CronSchedule(cron="0 7 * * *", at=AWARE_AT)  # `at` is a foreign field here


def test_at_schedule_accepts_aware_instant():
    sched = AtSchedule(at=AWARE_AT)
    assert sched.kind == "time.at"
    assert sched.at == AWARE_AT


def test_at_schedule_rejects_naive_instant():
    with pytest.raises(ValidationError):
        AtSchedule(at=datetime(2026, 7, 20, 9, 0, 0))


def test_at_schedule_extra_field_forbidden():
    with pytest.raises(ValidationError):
        AtSchedule(at=AWARE_AT, cron="0 7 * * *")


# ---------------------------------------------------------------------------
# Discriminated-union parsing (on the real host, PipelineSpec) — the plugin dumps
# the flat schedule with ``exclude_none``, so the unused sibling is dropped and
# the right member parses on the ``kind`` discriminator.
# ---------------------------------------------------------------------------


def test_union_parses_cron_from_plugin_dumped_dict():
    ps = PipelineSpec.model_validate(
        {"name": "p", "wake_prompt": "go", "schedule": {"kind": "time.cron", "cron": "0 7 * * *"}}
    )
    assert isinstance(ps.schedule, CronSchedule)
    assert ps.schedule.cron == "0 7 * * *"


def test_union_parses_at_from_plugin_dumped_dict():
    ps = PipelineSpec.model_validate(
        {
            "name": "p",
            "wake_prompt": "go",
            "schedule": {"kind": "time.at", "at": "2026-07-20T09:00:00+00:00"},
        }
    )
    assert isinstance(ps.schedule, AtSchedule)
    assert ps.schedule.at == AWARE_AT


def test_union_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        PipelineSpec.model_validate(
            {"name": "p", "wake_prompt": "go", "schedule": {"kind": "webhook"}}
        )


# ---------------------------------------------------------------------------
# to_trigger_spec — strategy-on-model builds the core TriggerSpec (NOW injected)
# ---------------------------------------------------------------------------


def test_cron_schedule_to_trigger_spec_builds_user_owned_cron_trigger():
    trig = CronSchedule(cron="0 7 * * *").to_trigger_spec(
        wake_prompt="Fetch email", session_id="maint-1", now=NOW
    )
    assert isinstance(trig, CronTrigger)
    assert trig.kind == "time.cron"
    assert trig.cron == "0 7 * * *"
    assert trig.session_id == "maint-1"
    assert trig.wake_prompt == "Fetch email"
    assert trig.created_by == "user"  # platform-armed, never agent-authored
    assert trig.created_at == NOW  # deterministic cron anchoring
    assert trig.status == "armed"


def test_at_schedule_to_trigger_spec_builds_one_shot_time_trigger():
    trig = AtSchedule(at=AWARE_AT).to_trigger_spec(
        wake_prompt="One-shot", session_id="maint-1", now=NOW
    )
    assert isinstance(trig, TimeAtTrigger)
    assert trig.kind == "time.at"
    assert trig.at == AWARE_AT
    assert trig.created_by == "user"
    assert trig.max_fires == 1  # TimeAtTrigger forces a single fire


# ---------------------------------------------------------------------------
# PipelineSpec floor — enforced at the SUBMIT boundary, never at parse
# (the app store is append-only: pre-existing version snapshots hold pipelines
# with no schedule/on_demand/trigger_ref and MUST keep parsing forever —
# a parse-time floor 500'd every detail read of app-a2a5f299e0ad live)
# ---------------------------------------------------------------------------


def test_pipeline_spec_parses_bare_legacy_shape_but_is_not_wakeable():
    # Parsing succeeds (history must load) …
    ps = PipelineSpec(name="ingest", wake_prompt="go")
    assert ps.schedule is None and not ps.on_demand and ps.trigger_ref is None
    # … but the submit boundary refuses it.
    with pytest.raises(ValueError, match="declare a `schedule`"):
        ps.ensure_wakeable()


def test_pre_phase1_version_snapshot_parses(  # the exact production 500 shape
):
    legacy_pipeline = {
        "name": "refresh-trackers",
        "wake_prompt": "refresh",
        "trigger_ref": None,
        "tools_allowlist": ["app_data", "read_file"],
        "cursor": {},
    }
    ps = PipelineSpec.model_validate(legacy_pipeline)
    ps_roundtrip = PipelineSpec.model_validate(ps.model_dump(mode="json"))
    assert ps_roundtrip.name == "refresh-trackers"


def test_pipeline_spec_accepts_declared_schedule():
    ps = PipelineSpec(
        name="ingest", wake_prompt="go", schedule=CronSchedule(cron="0 7 * * *")
    )
    assert isinstance(ps.schedule, CronSchedule)
    assert ps.on_demand is False
    assert ps.trigger_ref is None  # platform-owned output, unset at declaration


def test_pipeline_spec_accepts_on_demand():
    ps = PipelineSpec(name="reindex", wake_prompt="rebuild", on_demand=True)
    assert ps.on_demand is True
    assert ps.schedule is None


def test_pipeline_spec_accepts_legacy_trigger_ref_without_schedule():
    # The deprecated re-home path: a builder-armed trigger_ref is itself a
    # scheduling declaration, so it satisfies the floor (kept working, marked legacy).
    ps = PipelineSpec(name="ingest", wake_prompt="go", trigger_ref="trig-abc")
    assert ps.trigger_ref == "trig-abc"
    assert ps.schedule is None
    assert ps.on_demand is False


def test_pipeline_spec_parses_plugin_dumped_scheduled_pipeline():
    # The exact dict shape the landed submit_app tool produces
    # (model_dump(mode="json", exclude_none=True) — no trigger_ref, sibling dropped).
    ps = PipelineSpec.model_validate(
        {
            "name": "morning-organize",
            "wake_prompt": "Fetch new emails and regroup tasks",
            "schedule": {"kind": "time.cron", "cron": "0 7 * * *"},
            "on_demand": False,
            "tools_allowlist": ["app_data"],
            "cursor": {},
        }
    )
    assert isinstance(ps.schedule, CronSchedule)
    assert ps.tools_allowlist == ["app_data"]
