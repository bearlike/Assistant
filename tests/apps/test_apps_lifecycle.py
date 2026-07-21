"""Contract tests for ``AppLifecycle`` — submit, pause/resume, rollback, archive.

Real JSON app store + real JSON trigger store + real ``TriggerPolicy`` (the
policy caps are part of the contract under test); the session backend is the one
faked I/O boundary. Every clock read is an injected fixed NOW.
"""

from __future__ import annotations

import ast
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from mewbo_api.apps.lifecycle import _SCHEDULE_TRIGGER_TTL, AppLifecycle
from mewbo_api.apps.models import (
    AppFrontend,
    AppPolicies,
    AppSpec,
    AppVersion,
    AppVersionSummary,
    AtSchedule,
    CollectionSpec,
    CronSchedule,
    PipelineIssue,
    PipelineSpec,
    WorkspaceRef,
)
from mewbo_api.apps.pipeline_runner import AppPipelineRunner
from mewbo_api.apps.pipeline_tracker import AppPipelineRunTracker
from mewbo_api.apps.store import JsonAppDataStore, JsonAppStore, JsonPipelineRunStore
from mewbo_core.triggers.policy import TriggerPolicy
from mewbo_core.triggers.spec import CronTrigger
from mewbo_core.triggers.store import JsonTriggerStore

NOW = datetime(2026, 7, 17, 9, 0, 0, tzinfo=timezone.utc)


@dataclass
class _FakeResult:
    """Minimal ``PipelineResult`` stand-in the code-fire ledger writer reads."""

    output: Any
    evaluated_at: datetime
    cache: str = "miss"
    docs_written: dict[str, int] = field(default_factory=dict)


class _FakeRunner:
    """Fake ``AppPipelineRunner`` for code-pipeline verify + SEED fires (records + echoes).

    Records ``(app_id, pipeline_name, dry_run)`` per call so a test can tell the
    submit-time verifier's dry run (``dry_run=True``) apart from the go-live/re-arm
    seed fire (``dry_run=False``) — both ride this same runner.
    """

    def __init__(
        self, *, docs_written: dict[str, int] | None = None, raises: Exception | None = None
    ) -> None:
        self.calls: list[tuple[str, str, bool]] = []
        self._docs = docs_written if docs_written is not None else {"c": 1}
        self._raises = raises

    def execute(self, app, pipeline, params, *, now, dry_run=False):  # noqa: ANN001, ANN201
        self.calls.append((app.app_id, pipeline.name, dry_run))
        if self._raises is not None:
            raise self._raises
        return _FakeResult(output={"ok": True}, evaluated_at=now, docs_written=dict(self._docs))

    @staticmethod
    def params_hash(params: dict) -> str:  # noqa: ANN001
        from mewbo_api.apps.pipeline_runner import AppPipelineRunner

        return AppPipelineRunner.params_hash(params)


class _RaisingRunStarter:
    """A run-starter whose ``start_app_run`` raises — proves a seed failure is isolated."""

    def start_app_run(self, session_id: str, message: str) -> str:
        raise RuntimeError("wake exploded")


class FakeRunStarter:
    """Records ``start_app_run(session_id, message)`` calls (the injected run-starter).

    Returns the widened landed signal (default ``"started"``) so it satisfies the
    ``AppRunStarter`` Protocol and lets a fire-seed test read what the wake reported.
    """

    def __init__(self, *, landed: str = "started") -> None:
        self.calls: list[tuple[str, str]] = []
        self._landed = landed

    def start_app_run(self, session_id: str, message: str) -> str:
        self.calls.append((session_id, message))
        return self._landed


class FakeSessions:
    """Records session-backend calls; hands out deterministic session ids."""

    def __init__(self) -> None:
        self._n = 0
        self.tags: dict[str, list[str]] = {}
        self.contexts: dict[str, list[dict]] = {}
        self.events: dict[str, list[dict]] = {}

    def create_session(self) -> str:
        self._n += 1
        return f"session-{self._n}"

    def tag_session(self, session_id: str, tag: str) -> None:
        self.tags.setdefault(session_id, []).append(tag)

    def append_context_event(self, session_id: str, context: dict) -> None:
        self.contexts.setdefault(session_id, []).append(context)

    def append_event(self, session_id: str, event: dict) -> None:
        self.events.setdefault(session_id, []).append(event)


def _frontend() -> AppFrontend:
    return AppFrontend(entrypoint="app.py", files={"app.py": "import streamlit as st\n"})


def _make(tmp_path, *, policy: TriggerPolicy | None = None, run_starter=None):
    app_store = JsonAppStore(root_dir=tmp_path / "apps")
    trigger_store = JsonTriggerStore(data_file=tmp_path / "triggers.json")
    sessions = FakeSessions()
    lifecycle = AppLifecycle(
        app_store=app_store,
        trigger_store=trigger_store,
        trigger_policy=policy or TriggerPolicy(),
        sessions=sessions,
        run_starter=run_starter,
        now_fn=lambda: NOW,
    )
    return lifecycle, app_store, trigger_store, sessions


def _make_seeding(tmp_path, *, runner=None, starter=None, background=None, policy=None):
    """A lifecycle wired to a REAL tracker (the fire seam) for seed/​re-arm tests.

    ``background`` defaults to SYNCHRONOUS (``lambda fn: fn()``) so a code-pipeline
    seed's off-thread fire runs inline — deterministic under test.
    """
    app_store = JsonAppStore(root_dir=tmp_path / "apps")
    run_store = JsonPipelineRunStore(root_dir=tmp_path / "apps")
    trigger_store = JsonTriggerStore(data_file=tmp_path / "triggers.json")
    sessions = FakeSessions()
    lifecycle = AppLifecycle(
        app_store=app_store,
        trigger_store=trigger_store,
        trigger_policy=policy or TriggerPolicy(),
        sessions=sessions,
        run_starter=starter,
        background_runner=background or (lambda fn: fn()),
        now_fn=lambda: NOW,
    )
    tracker = AppPipelineRunTracker(
        run_store=run_store,
        app_store=app_store,
        failure_handler=lifecycle,
        pipeline_runner=runner,
        run_starter=starter,
        now_fn=lambda: NOW,
    )
    lifecycle.tracker = tracker
    return lifecycle, app_store, run_store, trigger_store, sessions


def _real_runner(tmp_path) -> AppPipelineRunner:
    """A REAL ``AppPipelineRunner`` for the submit-time verifier (dry-run, no writes).

    ``execute`` is object-keyed (it takes the resolved app), so the runner's own
    ``app_store`` is irrelevant to the verifier; the workspace resolves empty so a
    dry run reaches ``run(params, ctx)`` without touching a real workspace.
    """
    return AppPipelineRunner(
        app_store=JsonAppStore(root_dir=tmp_path / "apps"),
        app_data=JsonAppDataStore(root_dir=tmp_path / "apps"),
        workspace_resolver=lambda app: None,
        clock=lambda: NOW,
    )


def _draft(app_id: str, *, builder_sid: str, pipelines=None) -> AppSpec:
    return AppSpec(
        app_id=app_id,
        title="Inbox digest",
        summary="Groups email into tasks.",
        owner_session_id=builder_sid,
        workspace_ref=WorkspaceRef(kind="own", key="default"),
        frontend=_frontend(),
        pipelines=pipelines or [],
        status="building",
        created_at=NOW,
        updated_at=NOW,
    )


class TestCreateDraft:
    def test_mints_builder_session_and_placeholder(self, tmp_path):
        lifecycle, app_store, _, sessions = _make(tmp_path)
        spec = lifecycle.create_draft("Summarize my email", WorkspaceRef(kind="own", key="k"))
        assert spec.status == "building"
        assert spec.owner_session_id == "session-1"
        assert spec.frontend.entrypoint in spec.frontend.files  # placeholder is valid
        # Session tagged app:<id> + advertises the apps capability.
        assert f"app:{spec.app_id}" in sessions.tags["session-1"]
        ctx = sessions.contexts["session-1"][0]
        assert ctx["client_capabilities"] == ["apps"]
        # Persisted, and no version snapshot yet (submit writes v1).
        assert app_store.get(spec.app_id) is not None
        assert app_store.list_versions(spec.app_id) == []

    def test_kicks_off_builder_run_with_the_composed_prompt(self, tmp_path):
        # C1a: create_draft starts the builder run via the injected run-starter,
        # with a kickoff carrying the intent + workspace + the assigned app_id.
        starter = FakeRunStarter()
        lifecycle, _, _, _ = _make(tmp_path, run_starter=starter)
        spec = lifecycle.create_draft(
            "Summarize my unread email", WorkspaceRef(kind="own", key="default")
        )
        assert len(starter.calls) == 1
        session_id, message = starter.calls[0]
        assert session_id == spec.owner_session_id  # the builder session it minted
        assert "Summarize my unread email" in message
        assert spec.app_id in message  # so submit_app reuses the draft's app_id
        assert "app-builder" in message  # the skill delegation contract
        assert "own" in message

    def test_no_run_starter_degrades_to_no_kickoff(self, tmp_path):
        # Unwired run-starter: the draft still persists, just no build starts.
        lifecycle, app_store, _, _ = _make(tmp_path, run_starter=None)
        spec = lifecycle.create_draft("x", WorkspaceRef(kind="own", key="default"))
        assert app_store.get(spec.app_id) is not None


class TestSubmit:
    def test_persists_v1_creates_maintainer_and_goes_live(self, tmp_path):
        lifecycle, app_store, _, sessions = _make(tmp_path)
        draft = _draft("app-x", builder_sid="builder-1")
        live = lifecycle.submit(draft, builder_session_id="builder-1")
        assert live.status == "live"
        assert live.version == 1
        assert live.maintainer_session_id == "session-1"
        # v1 snapshot recorded, authored by the builder.
        versions = app_store.list_versions("app-x")
        assert [v.version for v in versions] == [1]
        assert versions[0].author == "builder"
        # app_ready emitted on the builder session the console streams.
        ready = sessions.events["builder-1"][0]
        assert ready["type"] == "app_ready"
        assert ready["payload"]["app_id"] == "app-x"
        assert ready["payload"]["version"] == 1
        # The payload round-trips (no raw datetime / non-JSON value leaks) — this
        # is now the ONE emission site (the submit_app tool no longer double-fires it).
        assert ast.literal_eval(str(ready["payload"])) == ready["payload"]

    def test_rehomes_pipeline_trigger_onto_maintainer(self, tmp_path):
        lifecycle, _, trigger_store, _ = _make(tmp_path)
        # Builder armed a cron trigger on ITS session during the build.
        builder_trigger = CronTrigger(
            session_id="builder-1", wake_prompt="ingest", cron="0 9 * * *", created_by="agent"
        )
        trigger_store.create(builder_trigger)
        pipeline = PipelineSpec(
            name="ingest", wake_prompt="Ingest new email", trigger_ref=builder_trigger.id
        )
        draft = _draft("app-x", builder_sid="builder-1", pipelines=[pipeline])

        live = lifecycle.submit(draft, builder_session_id="builder-1")
        maintainer = live.maintainer_session_id

        # A fresh trigger is armed on the maintainer with the pipeline's wake prompt.
        armed = trigger_store.list(session_id=maintainer, status="armed")
        assert len(armed) == 1
        assert armed[0].wake_prompt == "Ingest new email"
        assert armed[0].id != builder_trigger.id
        assert armed[0].kind == "time.cron"
        assert armed[0].created_by == "user"
        # The builder's original trigger is cancelled (can't fire into a dead session).
        assert trigger_store.get(builder_trigger.id).status == "cancelled"
        # The persisted pipeline points at the new maintainer trigger.
        assert live.pipelines[0].trigger_ref == armed[0].id

    def test_arms_within_policy_caps(self, tmp_path):
        lifecycle, _, trigger_store, _ = _make(
            tmp_path, policy=TriggerPolicy(max_armed_per_session=1)
        )
        pipelines = []
        for i in range(2):
            trig = CronTrigger(
                session_id="builder-1", wake_prompt=f"p{i}", cron="0 9 * * *", created_by="agent"
            )
            trigger_store.create(trig)
            pipelines.append(
                PipelineSpec(name=f"p{i}", wake_prompt=f"wake {i}", trigger_ref=trig.id)
            )
        draft = _draft("app-x", builder_sid="builder-1", pipelines=pipelines)
        live = lifecycle.submit(draft, builder_session_id="builder-1")
        # Only ONE trigger fits under the cap; the second pipeline stays on-demand.
        armed = trigger_store.list(session_id=live.maintainer_session_id, status="armed")
        assert len(armed) == 1


class TestSubmitPlatformArming:
    """Phase 1: the PLATFORM arms a pipeline's DECLARED schedule at submit.

    Retires builder self-arming — the builder declares a ``schedule`` and never
    touches the trigger store; the lifecycle mints the maintainer-owned trigger.
    """

    def test_declared_cron_schedule_arms_a_maintainer_trigger(self, tmp_path):
        lifecycle, _, trigger_store, _ = _make(tmp_path)
        pipeline = PipelineSpec(
            name="ingest",
            wake_prompt="Ingest new email",
            schedule=CronSchedule(cron="0 9 * * *"),
        )
        draft = _draft("app-x", builder_sid="builder-1", pipelines=[pipeline])

        live = lifecycle.submit(draft, builder_session_id="builder-1")
        maintainer = live.maintainer_session_id

        armed = trigger_store.list(session_id=maintainer, status="armed")
        assert len(armed) == 1
        assert armed[0].kind == "time.cron"
        assert armed[0].cron == "0 9 * * *"
        assert armed[0].wake_prompt == "Ingest new email"
        assert armed[0].created_by == "user"  # platform-armed, not agent-authored
        # The platform stamps the armed id into the (platform-owned) trigger_ref.
        assert live.pipelines[0].trigger_ref == armed[0].id
        # No builder trigger existed — nothing was re-homed / cancelled.
        assert len(trigger_store.list()) == 1

    def test_scheduled_trigger_gets_long_lived_expiry_not_the_7day_default(self, tmp_path):
        # The heartbeat trap: TriggerPolicy stamps a 7-day default_expiry when
        # expires_at is None. A declared-schedule trigger must outlive that, or the
        # app refreshes once and silently dies (app-a2a5f299e0ad, live-verified).
        lifecycle, _, trigger_store, _ = _make(tmp_path, policy=TriggerPolicy())
        pipeline = PipelineSpec(
            name="ingest", wake_prompt="go", schedule=CronSchedule(cron="0 9 * * *")
        )
        live = lifecycle.submit(
            _draft("app-x", builder_sid="b", pipelines=[pipeline]), builder_session_id="b"
        )
        armed = trigger_store.list(session_id=live.maintainer_session_id, status="armed")[0]
        assert armed.expires_at == NOW + _SCHEDULE_TRIGGER_TTL
        # Far beyond the policy's 7-day default — the heartbeat survives.
        assert armed.expires_at > NOW + timedelta(days=7)

    def test_declared_at_schedule_arms_a_one_shot_time_trigger(self, tmp_path):
        lifecycle, _, trigger_store, _ = _make(tmp_path)
        at = NOW + timedelta(days=30)  # past the old 7-day default — must not expire first
        pipeline = PipelineSpec(
            name="reminder", wake_prompt="One-shot", schedule=AtSchedule(at=at)
        )
        live = lifecycle.submit(
            _draft("app-x", builder_sid="b", pipelines=[pipeline]), builder_session_id="b"
        )
        armed = trigger_store.list(session_id=live.maintainer_session_id, status="armed")[0]
        assert armed.kind == "time.at"
        assert armed.at == at
        assert armed.max_fires == 1
        assert armed.expires_at > at  # the one-shot outlives its fire instant

    def test_declared_schedule_over_policy_cap_stays_unscheduled_not_a_failed_submit(
        self, tmp_path
    ):
        lifecycle, _, trigger_store, _ = _make(
            tmp_path, policy=TriggerPolicy(max_armed_per_session=1)
        )
        pipelines = [
            PipelineSpec(name=f"p{i}", wake_prompt=f"w{i}", schedule=CronSchedule(cron="0 9 * * *"))
            for i in range(2)
        ]
        live = lifecycle.submit(
            _draft("app-x", builder_sid="b", pipelines=pipelines), builder_session_id="b"
        )
        # Only one fits under the cap; the second is left unscheduled (submit still
        # succeeds — the app keeps the pipelines that fit).
        armed = trigger_store.list(session_id=live.maintainer_session_id, status="armed")
        assert len(armed) == 1
        assert live.status == "live"
        assert [p.trigger_ref is not None for p in live.pipelines] == [True, False]

    def test_on_demand_pipeline_arms_nothing(self, tmp_path):
        lifecycle, _, trigger_store, _ = _make(tmp_path)
        pipeline = PipelineSpec(name="reindex", wake_prompt="rebuild", on_demand=True)
        live = lifecycle.submit(
            _draft("app-x", builder_sid="b", pipelines=[pipeline]), builder_session_id="b"
        )
        assert trigger_store.list(session_id=live.maintainer_session_id) == []
        assert live.pipelines[0].trigger_ref is None

    def test_platform_armed_trigger_pauses_resumes_and_archives_in_lockstep(self, tmp_path):
        # Pause/resume/archive treat a platform-armed trigger exactly like a
        # re-homed one — it is on the maintainer, so "app == its triggers" holds.
        lifecycle, app_store, trigger_store, _ = _make(tmp_path)
        pipeline = PipelineSpec(
            name="ingest", wake_prompt="go", schedule=CronSchedule(cron="0 9 * * *")
        )
        live = lifecycle.submit(
            _draft("app-x", builder_sid="b", pipelines=[pipeline]), builder_session_id="b"
        )
        maintainer = live.maintainer_session_id

        lifecycle.pause("app-x")
        assert app_store.get("app-x").status == "paused"
        assert trigger_store.list(session_id=maintainer, status="armed") == []
        assert len(trigger_store.list(session_id=maintainer, status="paused")) == 1

        lifecycle.resume("app-x")
        assert app_store.get("app-x").status == "live"
        assert len(trigger_store.list(session_id=maintainer, status="armed")) == 1

        lifecycle.archive("app-x")
        assert all(t.status == "cancelled" for t in trigger_store.list(session_id=maintainer))


class TestSubmitReconciliation:
    """C1b: submit reconciles the existing ``building`` draft row (identity + guard)."""

    def test_submit_preserves_draft_row_identity(self, tmp_path):
        starter = FakeRunStarter()
        lifecycle, app_store, _, _ = _make(tmp_path, run_starter=starter)
        # The gallery-create flow writes a building row (owner/created_at/workspace).
        draft_row = lifecycle.create_draft(
            "Inbox digest", WorkspaceRef(kind="shared", key="acme")
        )
        app_id = draft_row.app_id
        builder_sid = draft_row.owner_session_id

        # The builder submits a rebuilt draft carrying the SAME app_id but a fresh
        # owner/workspace/created_at (submit_app defaults) — identity must NOT drift.
        rebuilt = AppSpec(
            app_id=app_id,
            title="Inbox digest v1",
            owner_session_id="some-sub-agent-session",
            workspace_ref=WorkspaceRef(kind="own", key="wrong"),
            frontend=_frontend(),
            status="building",
        )
        live = lifecycle.submit(rebuilt, builder_session_id=builder_sid)

        assert live.status == "live"
        assert live.version == 1
        assert live.owner_session_id == builder_sid  # preserved from the draft row
        assert live.workspace_ref.key == "acme"  # preserved (not the rebuilt "wrong")
        assert live.created_at == draft_row.created_at  # preserved
        assert live.title == "Inbox digest v1"  # content replaced from the rebuilt draft
        assert live.maintainer_session_id is not None
        # The stored row is the live one.
        assert app_store.get(app_id).status == "live"

    def test_submit_refuses_overwriting_a_live_app(self, tmp_path):
        lifecycle, app_store, _, _ = _make(tmp_path)
        live = lifecycle.submit(_draft("app-x", builder_sid="b"), builder_session_id="b")
        assert live.status == "live"
        # A second submit for the same (now-live) app_id is refused — the
        # double-submit / live-overwrite guard (data-loss vector).
        with pytest.raises(ValueError, match="refusing to overwrite"):
            lifecycle.submit(_draft("app-x", builder_sid="b"), builder_session_id="b")
        # The live app is untouched.
        assert app_store.get("app-x").status == "live"

    def test_submit_refuses_a_pipeline_that_would_never_wake(self, tmp_path):
        # The wakeability floor moved from model parse (where it 500'd every
        # detail read of pre-existing version history) to the submit boundary:
        # the bare shape is CONSTRUCTIBLE, but not SUBMITTABLE.
        lifecycle, app_store, _, _ = _make(tmp_path)
        bare = PipelineSpec(name="ingest", wake_prompt="go")
        with pytest.raises(ValueError, match="declare a `schedule`"):
            lifecycle.submit(
                _draft("app-nowake", builder_sid="b", pipelines=[bare]),
                builder_session_id="b",
            )
        assert app_store.get("app-nowake") is None  # nothing persisted


class TestSubmitMaintainerResubmit:
    """The app-repair resubmit path: 'resubmit the same app_id to ship a new
    version' (app-repair.md) is the app's OWN maintainer resubmitting, not the
    chat-builder collision the live-overwrite guard exists to catch.
    """

    def test_maintainer_resubmit_on_live_app_bumps_version_and_preserves_identity(
        self, tmp_path
    ):
        lifecycle, app_store, _, sessions = _make(tmp_path)
        draft = _draft("app-x", builder_sid="builder-1")
        live = lifecycle.submit(draft, builder_session_id="builder-1")
        maintainer = live.maintainer_session_id

        # The app-repair AgentDef calls submit_app from ON the maintainer session.
        fix = _draft("app-x", builder_sid=maintainer).model_copy(
            update={"title": "Inbox digest (fixed)"}
        )
        repaired = lifecycle.submit(fix, builder_session_id=maintainer)

        assert repaired.status == "live"
        assert repaired.version == 2  # bumped, never reset to 1
        assert repaired.maintainer_session_id == maintainer  # SAME session reused
        assert repaired.owner_session_id == live.owner_session_id  # identity preserved
        assert repaired.created_at == live.created_at
        assert repaired.workspace_ref == live.workspace_ref
        assert repaired.title == "Inbox digest (fixed)"  # content replaced
        assert app_store.get("app-x").version == 2
        versions = app_store.list_versions("app-x")
        assert [v.version for v in versions] == [1, 2]
        assert versions[1].author == "repair"
        assert versions[1].note == "repair resubmit"
        # app_ready fired again on the resubmitting (maintainer) session.
        ready = sessions.events[maintainer][-1]
        assert ready["type"] == "app_ready"
        assert ready["payload"]["version"] == 2

    def test_maintainer_resubmit_replaces_scheduled_triggers_with_no_leak(self, tmp_path):
        # The trigger-leak critical: a repair resubmit re-arms from the fresh draft;
        # without cancelling the maintainer's prior triggers, each resubmit would
        # leave the OLD armed trigger firing its stale wake_prompt forever (an
        # orphan whose trigger_id no longer matches any pipeline), accreting against
        # the policy cap. Exactly ONE armed trigger must remain after a resubmit.
        lifecycle, _, trigger_store, _ = _make(tmp_path)
        v1_pipeline = PipelineSpec(
            name="ingest", wake_prompt="v1 wake", schedule=CronSchedule(cron="0 9 * * *")
        )
        live = lifecycle.submit(
            _draft("app-x", builder_sid="builder-1", pipelines=[v1_pipeline]),
            builder_session_id="builder-1",
        )
        maintainer = live.maintainer_session_id
        first_trigger_id = live.pipelines[0].trigger_ref
        assert len(trigger_store.list(session_id=maintainer, status="armed")) == 1

        # app-repair resubmits a v2 with the same pipeline schedule FROM the maintainer.
        fix = _draft(
            "app-x",
            builder_sid=maintainer,
            pipelines=[
                PipelineSpec(
                    name="ingest", wake_prompt="v2 wake", schedule=CronSchedule(cron="0 9 * * *")
                )
            ],
        )
        repaired = lifecycle.submit(fix, builder_session_id=maintainer)

        armed = trigger_store.list(session_id=maintainer, status="armed")
        assert len(armed) == 1  # no leak — the v1 trigger was cancelled, not orphaned
        assert armed[0].id != first_trigger_id  # freshly minted from the v2 draft
        assert armed[0].wake_prompt == "v2 wake"
        assert repaired.pipelines[0].trigger_ref == armed[0].id
        # The prior trigger is terminal (cancelled), so it can never fire again.
        assert trigger_store.get(first_trigger_id).status == "cancelled"

    def test_maintainer_resubmit_on_paused_app_brings_it_back_live(self, tmp_path):
        lifecycle, app_store, _, _ = _make(tmp_path)
        draft = _draft("app-x", builder_sid="builder-1")
        live = lifecycle.submit(draft, builder_session_id="builder-1")
        maintainer = live.maintainer_session_id
        lifecycle.pause("app-x")
        assert app_store.get("app-x").status == "paused"

        repaired = lifecycle.submit(
            _draft("app-x", builder_sid=maintainer), builder_session_id=maintainer
        )
        assert repaired.status == "live"
        assert repaired.version == 2

    def test_foreign_builder_session_is_still_refused_on_a_live_app(self, tmp_path):
        lifecycle, app_store, _, _ = _make(tmp_path)
        lifecycle.submit(_draft("app-x", builder_sid="builder-1"), builder_session_id="builder-1")
        # Some OTHER session (not the maintainer) reusing the app_id is the
        # chat-builder collision this guard exists to catch — still refused.
        with pytest.raises(ValueError, match="refusing to overwrite"):
            lifecycle.submit(
                _draft("app-x", builder_sid="some-other-session"),
                builder_session_id="some-other-session",
            )
        assert app_store.get("app-x").status == "live"
        assert app_store.get("app-x").version == 1  # untouched


class TestPipelineFailurePolicy:
    """I4: on a failed pipeline run, dispatch ``policies.on_pipeline_failure``."""

    def _live_app(self, lifecycle, policy: str):
        trig = CronTrigger(
            session_id="builder-1", wake_prompt="p", cron="0 9 * * *", created_by="agent"
        )
        lifecycle.trigger_store.create(trig)
        draft = _draft(
            "app-x",
            builder_sid="builder-1",
            pipelines=[PipelineSpec(name="p", wake_prompt="wake", trigger_ref=trig.id)],
        )
        draft = draft.model_copy(update={"policies": AppPolicies(on_pipeline_failure=policy)})
        return lifecycle.submit(draft, builder_session_id="builder-1")

    def test_repair_starts_a_repair_run_on_the_maintainer(self, tmp_path):
        starter = FakeRunStarter()
        lifecycle, *_ = _make(tmp_path, run_starter=starter)
        live = self._live_app(lifecycle, "repair")
        starter.calls.clear()  # ignore any earlier kickoff (none here — no create_draft)

        lifecycle.handle_pipeline_failure(
            live, PipelineIssue.run_failed("p", "collection 'tasks' rejected the doc")
        )
        assert len(starter.calls) == 1
        session_id, message = starter.calls[0]
        assert session_id == live.maintainer_session_id
        assert "app-repair" in message
        assert live.app_id in message
        assert "collection 'tasks' rejected the doc" in message

    def test_pause_pauses_the_app_and_its_triggers(self, tmp_path):
        lifecycle, app_store, trigger_store, _ = _make(tmp_path)
        live = self._live_app(lifecycle, "pause")

        lifecycle.handle_pipeline_failure(live, PipelineIssue.run_failed("p", "boom"))
        assert app_store.get("app-x").status == "paused"
        assert trigger_store.list(session_id=live.maintainer_session_id, status="armed") == []

    def test_notify_emits_an_app_issue_event_only(self, tmp_path):
        lifecycle, app_store, _, sessions = _make(tmp_path)
        live = self._live_app(lifecycle, "notify")

        lifecycle.handle_pipeline_failure(live, PipelineIssue.run_failed("p", "boom"))
        # An app_issue event landed on the maintainer session; nothing else changed.
        issue = sessions.events[live.maintainer_session_id][-1]
        assert issue["type"] == "app_issue"
        assert issue["payload"] == {
            "app_id": "app-x",
            "error": "boom",  # a failure's line stays the raw error, verbatim
            "kind": "run_failed",
            "pipeline": "p",
            "collections": [],
        }
        assert app_store.get("app-x").status == "live"  # NOT paused


class TestPauseResume:
    def test_pause_then_resume_toggles_app_and_triggers(self, tmp_path):
        lifecycle, app_store, trigger_store, _ = _make(tmp_path)
        trig = CronTrigger(
            session_id="builder-1", wake_prompt="p", cron="0 9 * * *", created_by="agent"
        )
        trigger_store.create(trig)
        pipeline = PipelineSpec(name="p", wake_prompt="wake", trigger_ref=trig.id)
        live = lifecycle.submit(
            _draft("app-x", builder_sid="builder-1", pipelines=[pipeline]),
            builder_session_id="builder-1",
        )
        maintainer = live.maintainer_session_id

        paused = lifecycle.pause("app-x")
        assert paused.status == "paused"
        assert trigger_store.list(session_id=maintainer, status="armed") == []
        assert len(trigger_store.list(session_id=maintainer, status="paused")) == 1

        resumed = lifecycle.resume("app-x")
        assert resumed.status == "live"
        assert len(trigger_store.list(session_id=maintainer, status="armed")) == 1

    def test_pause_unknown_app_is_none(self, tmp_path):
        lifecycle, *_ = _make(tmp_path)
        assert lifecycle.pause("nope") is None


class TestRollback:
    def test_rollback_repoints_as_new_version(self, tmp_path):
        lifecycle, app_store, _, sessions = _make(tmp_path)
        live = lifecycle.submit(_draft("app-x", builder_sid="builder-1"), builder_session_id="b")
        maintainer = live.maintainer_session_id
        # Simulate a v2 edit with a changed title.
        v2 = live.model_copy(update={"version": 2, "title": "Edited"})
        app_store.save(v2)
        app_store.save_version(AppVersion(app_id="app-x", version=2, spec=v2, author="user"))

        rolled = lifecycle.rollback("app-x", 1)
        assert rolled.version == 3  # append-only: latest+1
        assert rolled.title == "Inbox digest"  # v1's design
        assert rolled.maintainer_session_id == maintainer  # live wiring preserved
        assert app_store.get("app-x").version == 3
        # app_updated emitted on the maintainer session.
        updated = sessions.events[maintainer][-1]
        assert updated["type"] == "app_updated"
        assert updated["payload"]["version"] == 3

    def test_rollback_unknown_version_is_none(self, tmp_path):
        lifecycle, *_ = _make(tmp_path)
        lifecycle.submit(_draft("app-x", builder_sid="b"), builder_session_id="b")
        assert lifecycle.rollback("app-x", 9) is None

    def test_rollback_stamps_a_reverse_diff_summary(self, tmp_path):
        # F2: the rollback-authored version carries a summary of the REVERSE diff
        # (current live -> the restored snapshot); verification stays None (the
        # snapshot was verified at its original submit, not re-executed here).
        lifecycle, app_store, _, _ = _make(tmp_path)
        lifecycle.submit(_draft("app-x", builder_sid="b"), builder_session_id="b")  # v1: no pipes
        live = app_store.get("app-x")
        v2 = live.model_copy(
            update={
                "version": 2,
                "pipelines": [PipelineSpec(name="digest", wake_prompt="w", on_demand=True)],
            }
        )
        app_store.save(v2)
        app_store.save_version(AppVersion(app_id="app-x", version=2, spec=v2, author="user"))

        rolled = lifecycle.rollback("app-x", 1)  # back to the no-pipeline design
        row = app_store.get_version("app-x", rolled.version)
        assert row.summary is not None
        assert row.summary.pipelines_removed == ["digest"]  # the reverse diff drops it
        assert row.verification is None


class TestWorkspaceScope:
    """workspace_ref → the agent session's project/MCP scope (spec §2.3)."""

    def test_shared_workspace_binds_project_on_builder_and_maintainer(self, tmp_path):
        lifecycle, _, _, sessions = _make(tmp_path)
        shared = WorkspaceRef(kind="shared", key="acme-monorepo")

        draft = lifecycle.create_draft("Ship it", shared)
        # The builder session inherits the referenced project scope (agent side).
        builder_ctx = sessions.contexts[draft.owner_session_id][0]
        assert builder_ctx["project"] == "acme-monorepo"
        assert builder_ctx["client_capabilities"] == ["apps"]
        assert builder_ctx["app_id"] == draft.app_id

        live = lifecycle.submit(draft, builder_session_id=draft.owner_session_id)
        # The maintainer (the durable, trigger-woken session) carries the same scope,
        # so a later fire resolves its cwd/MCP scope from `project` (convention).
        maint_ctx = sessions.contexts[live.maintainer_session_id][0]
        assert maint_ctx["project"] == "acme-monorepo"
        assert maint_ctx["client_capabilities"] == ["apps"]

    def test_own_workspace_is_isolated_default_scope_no_project(self, tmp_path):
        lifecycle, _, _, sessions = _make(tmp_path)
        own = WorkspaceRef(kind="own", key="default")

        draft = lifecycle.create_draft("Private app", own)
        builder_ctx = sessions.contexts[draft.owner_session_id][0]
        # v1 own-kind = isolated default scope tagged to the app: NO project key,
        # no new workspace entity — just the app tag + capability.
        assert "project" not in builder_ctx
        assert builder_ctx["app_id"] == draft.app_id

        live = lifecycle.submit(draft, builder_session_id=draft.owner_session_id)
        assert "project" not in sessions.contexts[live.maintainer_session_id][0]


class TestArchive:
    def test_archive_cancels_triggers_and_absorbs(self, tmp_path):
        lifecycle, app_store, trigger_store, _ = _make(tmp_path)
        trig = CronTrigger(
            session_id="builder-1", wake_prompt="p", cron="0 9 * * *", created_by="agent"
        )
        trigger_store.create(trig)
        pipeline = PipelineSpec(name="p", wake_prompt="wake", trigger_ref=trig.id)
        live = lifecycle.submit(
            _draft("app-x", builder_sid="builder-1", pipelines=[pipeline]),
            builder_session_id="builder-1",
        )
        maintainer = live.maintainer_session_id

        archived = lifecycle.archive("app-x")
        assert archived.status == "archived"
        # Every maintainer trigger is cancelled.
        remaining = trigger_store.list(session_id=maintainer)
        assert all(t.status == "cancelled" for t in remaining)
        # Archived is absorbing — a re-archive is a no-op, not an error.
        assert lifecycle.archive("app-x").status == "archived"


class TestGoLiveSeed:
    """Submit seeds a first run of every pipeline so freshness isn't born 'Never'."""

    def test_submit_seeds_an_agentic_pipeline(self, tmp_path):
        starter = FakeRunStarter()
        lifecycle, _, run_store, _, _ = _make_seeding(tmp_path, starter=starter)
        pipeline = PipelineSpec(name="digest", wake_prompt="Refresh the digest", on_demand=True)
        live = lifecycle.submit(
            _draft("app-x", builder_sid="b", pipelines=[pipeline]), builder_session_id="b"
        )
        maintainer = live.maintainer_session_id
        # The seed opened a running on_request row (no trigger) + woke the maintainer
        # with the pipeline's OWN wake_prompt (never a trigger's stale copy).
        run = run_store.get_open("app-x", "digest")
        assert run is not None and run.status == "running"
        assert run.kind == "on_request" and run.trigger_id is None
        assert (maintainer, "Refresh the digest") in starter.calls

    def test_submit_seeds_a_code_pipeline_writing_a_closed_row(self, tmp_path):
        runner = _FakeRunner(docs_written={"tasks": 3})
        lifecycle, _, run_store, _, _ = _make_seeding(tmp_path, runner=runner)
        entrypoint = "pipelines/report.py"
        pipeline = PipelineSpec(
            name="report", wake_prompt="unused", on_demand=True, mode="code", entrypoint=entrypoint
        )
        draft = _draft("app-x", builder_sid="b", pipelines=[pipeline])
        frontend = draft.frontend.model_copy(
            update={"files": {**draft.frontend.files, entrypoint: "def run(p, c):\n    return p\n"}}
        )
        lifecycle.submit(draft.model_copy(update={"frontend": frontend}), builder_session_id="b")
        runs = run_store.list_runs("app-x")
        assert len(runs) == 1
        assert runs[0].kind == "on_request" and runs[0].status == "succeeded"
        assert runs[0].docs_written == {"tasks": 3}  # an explicit fire ALWAYS ledgers
        # The runner is hit twice: the submit-time verifier's dry run (leaves no
        # ledger row) then the go-live seed's real fire (the one row asserted above).
        assert runner.calls == [("app-x", "report", True), ("app-x", "report", False)]

    def test_a_seed_failure_never_fails_submit(self, tmp_path):
        # An agentic seed whose wake raises must not sink the submit — the app is
        # already durably live before the (best-effort) seed runs.
        lifecycle, app_store, _, _, _ = _make_seeding(tmp_path, starter=_RaisingRunStarter())
        pipeline = PipelineSpec(name="digest", wake_prompt="go", on_demand=True)
        live = lifecycle.submit(
            _draft("app-x", builder_sid="b", pipelines=[pipeline]), builder_session_id="b"
        )
        assert live.status == "live"
        assert app_store.get("app-x").status == "live"

    def test_no_tracker_wired_leaves_submit_unchanged(self, tmp_path):
        # The default lifecycle (no tracker) does NOT seed — the pre-existing submit
        # behavior every other test in this file relies on.
        starter = FakeRunStarter()
        lifecycle, app_store, _, _ = _make(tmp_path, run_starter=starter)
        pipeline = PipelineSpec(name="digest", wake_prompt="go", on_demand=True)
        live = lifecycle.submit(
            _draft("app-x", builder_sid="b", pipelines=[pipeline]), builder_session_id="b"
        )
        assert live.status == "live"
        assert starter.calls == []  # no seed wake without a tracker

    def test_agentic_seed_refused_wake_does_not_strand_a_row(self, tmp_path):
        # A seed whose wake is refused must close its row (not strand it) and not
        # fail the submit — the row is settled failed, get_open is clear.
        lifecycle, _, run_store, _, _ = _make_seeding(
            tmp_path, starter=FakeRunStarter(landed="refused")
        )
        pipeline = PipelineSpec(name="digest", wake_prompt="go", on_demand=True)
        live = lifecycle.submit(
            _draft("app-x", builder_sid="b", pipelines=[pipeline]), builder_session_id="b"
        )
        assert live.status == "live"
        assert run_store.get_open("app-x", "digest") is None  # closed, not stranded


class TestRearm:
    """Operator re-arm of a live app whose declared schedules never took."""

    @staticmethod
    def _live_scheduled(lifecycle):
        pipeline = PipelineSpec(
            name="ingest", wake_prompt="Ingest", schedule=CronSchedule(cron="0 9 * * *")
        )
        return lifecycle.submit(
            _draft("app-x", builder_sid="b", pipelines=[pipeline]), builder_session_id="b"
        )

    def test_rearms_a_dead_trigger(self, tmp_path):
        lifecycle, app_store, _, trigger_store, _ = _make_seeding(
            tmp_path, starter=FakeRunStarter()
        )
        live = self._live_scheduled(lifecycle)
        maintainer = live.maintainer_session_id
        old_ref = live.pipelines[0].trigger_ref
        trigger_store.cancel_for_session(maintainer)  # the heartbeat lapsed
        assert trigger_store.list(session_id=maintainer, status="armed") == []

        result = lifecycle.rearm(app_store.get("app-x"), seed=False, now=NOW)

        assert [a["pipeline"] for a in result["armed"]] == ["ingest"]
        new_ref = result["armed"][0]["trigger_id"]
        assert new_ref != old_ref
        armed = trigger_store.list(session_id=maintainer, status="armed")
        assert len(armed) == 1 and armed[0].id == new_ref
        # The new platform-owned trigger_ref is persisted onto the pipeline.
        assert app_store.get("app-x").pipelines[0].trigger_ref == new_ref
        assert result["seeded"] == []  # seed defaulted false

    def test_rearms_a_null_trigger_ref(self, tmp_path):
        lifecycle, app_store, _, trigger_store, _ = _make_seeding(
            tmp_path, starter=FakeRunStarter()
        )
        # A live app (via an on_demand pipeline) whose scheduled pipeline never armed.
        live = lifecycle.submit(
            _draft(
                "app-x", builder_sid="b",
                pipelines=[PipelineSpec(name="ingest", wake_prompt="go", on_demand=True)],
            ),
            builder_session_id="b",
        )
        maintainer = live.maintainer_session_id
        scheduled = PipelineSpec(
            name="ingest", wake_prompt="go", schedule=CronSchedule(cron="0 9 * * *")
        )  # trigger_ref is None
        app_store.save(app_store.get("app-x").model_copy(update={"pipelines": [scheduled]}))

        result = lifecycle.rearm(app_store.get("app-x"), seed=False, now=NOW)
        assert len(result["armed"]) == 1
        assert len(trigger_store.list(session_id=maintainer, status="armed")) == 1

    def test_already_armed_pipeline_is_unchanged(self, tmp_path):
        lifecycle, app_store, _, _, _ = _make_seeding(tmp_path, starter=FakeRunStarter())
        self._live_scheduled(lifecycle)
        result = lifecycle.rearm(app_store.get("app-x"), seed=False, now=NOW)
        assert result["armed"] == []
        assert result["unchanged"] == ["ingest"]

    def test_seed_true_fires_each_rearmed_pipeline(self, tmp_path):
        # A code pipeline seed re-runs deterministically (no already-running guard,
        # unlike an agentic fire whose go-live-seeded row would still be open here).
        runner = _FakeRunner()
        lifecycle, app_store, _, trigger_store, _ = _make_seeding(tmp_path, runner=runner)
        entrypoint = "pipelines/report.py"
        pipeline = PipelineSpec(
            name="report", wake_prompt="unused", mode="code", entrypoint=entrypoint,
            schedule=CronSchedule(cron="0 9 * * *"),
        )
        draft = _draft("app-x", builder_sid="b", pipelines=[pipeline])
        frontend = draft.frontend.model_copy(
            update={"files": {**draft.frontend.files, entrypoint: "def run(p, c):\n    return p\n"}}
        )
        live = lifecycle.submit(
            draft.model_copy(update={"frontend": frontend}), builder_session_id="b"
        )
        maintainer = live.maintainer_session_id
        trigger_store.cancel_for_session(maintainer)
        runner.calls.clear()  # ignore the go-live seed fire

        result = lifecycle.rearm(app_store.get("app-x"), seed=True, now=NOW)
        assert result["seeded"] == ["report"]
        # Re-arm has no verifier (only submit does), so its seed is the sole call.
        assert runner.calls == [("app-x", "report", False)]  # the re-armed pipeline fired once

    def test_cancels_a_stale_paused_trigger_before_rearming(self, tmp_path):
        # A trigger paused via the generic triggers route (app stays live) isn't
        # "armed", so rearm re-mints — but must CANCEL the stale one, else an app
        # pause/resume cycle later revives BOTH (double-fire).
        lifecycle, app_store, _, trigger_store, _ = _make_seeding(
            tmp_path, starter=FakeRunStarter()
        )
        live = self._live_scheduled(lifecycle)
        maintainer = live.maintainer_session_id
        old_ref = live.pipelines[0].trigger_ref
        stale = trigger_store.get(old_ref)
        stale.transition("paused")
        trigger_store.update(stale)  # paused, but the app is still live

        result = lifecycle.rearm(app_store.get("app-x"), seed=False, now=NOW)

        assert [a["pipeline"] for a in result["armed"]] == ["ingest"]
        # The stale paused trigger is now terminal (cancelled) — resume can't revive it.
        assert trigger_store.get(old_ref).status == "cancelled"
        non_terminal = [t for t in trigger_store.list(session_id=maintainer) if not t.is_terminal]
        assert len(non_terminal) == 1 and non_terminal[0].id != old_ref

    def test_concurrent_rearm_mints_exactly_one_replacement(self, tmp_path):
        # The per-app lock + re-read makes concurrent re-arms idempotent: exactly one
        # mints a replacement; the rest see it already armed. No orphaned double-armed
        # trigger (there is no sweep for triggers, so a leak would fire forever).
        lifecycle, app_store, _, trigger_store, _ = _make_seeding(
            tmp_path, starter=FakeRunStarter()
        )
        live = self._live_scheduled(lifecycle)
        maintainer = live.maintainer_session_id
        trigger_store.cancel_for_session(maintainer)  # the heartbeat lapsed
        results: list = []
        collect_lock = threading.Lock()

        def _rearm() -> None:
            r = lifecycle.rearm(app_store.get("app-x"), seed=False, now=NOW)
            with collect_lock:
                results.append(r)

        threads = [threading.Thread(target=_rearm) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(len(r["armed"]) for r in results) == 1  # only one minted
        non_terminal = [t for t in trigger_store.list(session_id=maintainer) if not t.is_terminal]
        assert len(non_terminal) == 1  # no orphaned double-armed trigger


class TestSubmitVerifier:
    """The submit-time pipeline verifier: dry-run every code pipeline via the SAME
    runner the fire plane uses, BEFORE anything persists/arms. A failure refuses the
    submit; verdicts land on the version row."""

    def _code_draft(self, app_id: str, source: str, *, name: str = "p", **pipe_kwargs) -> AppSpec:
        entrypoint = f"pipelines/{name}.py"
        draft = _draft(
            app_id,
            builder_sid="builder-1",
            pipelines=[
                PipelineSpec(
                    name=name,
                    wake_prompt="w",
                    on_demand=True,
                    mode="code",
                    entrypoint=entrypoint,
                    **pipe_kwargs,
                )
            ],
        )
        frontend = draft.frontend.model_copy(
            update={"files": {**draft.frontend.files, entrypoint: source}}
        )
        return draft.model_copy(update={"frontend": frontend})

    def test_pass_persists_the_verdict_on_the_version(self, tmp_path):
        lifecycle, app_store, *_ = _make_seeding(tmp_path, runner=_real_runner(tmp_path))
        draft = self._code_draft("app-x", "def run(params, ctx):\n    return {'n': 1}\n")
        live = lifecycle.submit(draft, builder_session_id="builder-1")
        assert live.status == "live"
        assert app_store.get_version("app-x", 1).verification == {"p": "pass"}

    def test_dry_run_failure_refuses_submit_naming_the_pipeline(self, tmp_path):
        lifecycle, app_store, *_ = _make_seeding(tmp_path, runner=_real_runner(tmp_path))
        draft = self._code_draft("app-x", "def run(params, ctx):\n    raise ValueError('boom')\n")
        with pytest.raises(ValueError, match="'p' failed verification"):
            lifecycle.submit(draft, builder_session_id="builder-1")
        # Nothing persisted, armed, or ledgered — the verifier runs before all of it.
        assert app_store.get("app-x") is None
        assert app_store.list_versions("app-x") == []

    def test_lint_failure_refuses_submit(self, tmp_path):
        # A banned import is caught by the runner's own lint at dry-run time.
        lifecycle, app_store, *_ = _make_seeding(tmp_path, runner=_real_runner(tmp_path))
        draft = self._code_draft("app-x", "import os\ndef run(params, ctx):\n    return {}\n")
        with pytest.raises(ValueError, match="failed verification"):
            lifecycle.submit(draft, builder_session_id="builder-1")
        assert app_store.get("app-x") is None

    def test_unwired_runner_skips_and_submit_proceeds(self, tmp_path):
        lifecycle, app_store, *_ = _make_seeding(tmp_path, runner=None)
        draft = self._code_draft("app-x", "def run(params, ctx):\n    return {}\n")
        live = lifecycle.submit(draft, builder_session_id="builder-1")
        assert live.status == "live"  # unwired-tolerant — never a crash
        assert app_store.get_version("app-x", 1).verification == {"p": "skipped"}

    def test_no_tracker_skips_verification(self, tmp_path):
        # The default lifecycle (no tracker at all) can't reach a runner -> skipped.
        lifecycle, app_store, _, _ = _make(tmp_path)
        draft = self._code_draft("app-x", "def run(params, ctx):\n    return {}\n")
        live = lifecycle.submit(draft, builder_session_id="builder-1")
        assert live.status == "live"
        assert app_store.get_version("app-x", 1).verification == {"p": "skipped"}

    def test_agentic_pipeline_is_skipped(self, tmp_path):
        lifecycle, app_store, *_ = _make_seeding(tmp_path, runner=_real_runner(tmp_path))
        draft = _draft(
            "app-x",
            builder_sid="builder-1",
            pipelines=[PipelineSpec(name="a", wake_prompt="w", on_demand=True)],
        )
        lifecycle.submit(draft, builder_session_id="builder-1")
        assert app_store.get_version("app-x", 1).verification == {"a": "skipped"}

    def test_read_file_pipeline_on_unbound_workspace_is_skipped(self, tmp_path):
        # At submit time no maintainer session (hence no workspace) exists yet, so an
        # unconditional ctx.read_file raises code="workspace" — a verifier artifact,
        # NOT a pipeline defect. It must be "skipped", never a spurious refusal.
        lifecycle, app_store, *_ = _make_seeding(tmp_path, runner=_real_runner(tmp_path))
        source = "def run(params, ctx):\n    return {'body': ctx.read_file('data.csv')}\n"
        draft = self._code_draft("app-x", source)
        live = lifecycle.submit(draft, builder_session_id="builder-1")
        assert live.status == "live"  # went live, no refusal
        assert app_store.get_version("app-x", 1).verification == {"p": "skipped"}

    def test_exec_pipeline_is_skipped_not_failed(self, tmp_path):
        # ctx.exec REFUSES under a dry run, and the submit-time verifier IS a dry
        # run — so a CLI-plumbed pipeline is unverifiable here by construction.
        # That must be "skipped" (a verifier artifact), never a refused submit,
        # or declaring allow_exec would make an app unshippable.
        #
        # The workspace resolver must return a REAL directory: ctx.exec checks the
        # workspace BEFORE dry_run, so the shared `_real_runner` (which resolves
        # None) would produce "skipped" for the wrong reason and prove nothing.
        workspace = tmp_path / "ws"
        workspace.mkdir()
        runner = AppPipelineRunner(
            app_store=JsonAppStore(root_dir=tmp_path / "apps"),
            app_data=JsonAppDataStore(root_dir=tmp_path / "apps"),
            workspace_resolver=lambda app: str(workspace),
        )
        lifecycle, app_store, *_ = _make_seeding(tmp_path, runner=runner)
        source = "def run(params, ctx):\n    return ctx.exec(['git', 'status'])\n"
        draft = self._code_draft("app-x", source, allow_exec=["git"])

        live = lifecycle.submit(draft, builder_session_id="builder-1")

        assert live.status == "live"  # went live, no refusal
        assert app_store.get_version("app-x", 1).verification == {"p": "skipped"}

    def test_params_required_pipeline_is_skipped_not_failed(self, tmp_path):
        # A user_writable form pipeline REQUIRES params — a params={} smoke can't
        # represent it, so it is unverifiable ("skipped"), never a false "fail".
        lifecycle, app_store, *_ = _make_seeding(tmp_path, runner=_real_runner(tmp_path))
        schema = {"type": "object", "properties": {"t": {"type": "string"}}, "required": ["t"]}
        draft = self._code_draft(
            "app-x",
            "def run(params, ctx):\n    return params\n",
            name="form",
            params_schema=schema,
            user_writable=True,
        )
        live = lifecycle.submit(draft, builder_session_id="builder-1")
        assert live.status == "live"
        assert app_store.get_version("app-x", 1).verification == {"form": "skipped"}

    def test_mixed_verdicts_land_together(self, tmp_path):
        # One passing code pipeline + one agentic pipeline -> both verdicts recorded.
        lifecycle, app_store, *_ = _make_seeding(tmp_path, runner=_real_runner(tmp_path))
        entrypoint = "pipelines/code.py"
        draft = _draft(
            "app-x",
            builder_sid="builder-1",
            pipelines=[
                PipelineSpec(
                    name="code",
                    wake_prompt="w",
                    on_demand=True,
                    mode="code",
                    entrypoint=entrypoint,
                ),
                PipelineSpec(name="think", wake_prompt="w", on_demand=True),
            ],
        )
        code_src = "def run(p, c):\n    return {}\n"
        frontend = draft.frontend.model_copy(
            update={"files": {**draft.frontend.files, entrypoint: code_src}}
        )
        lifecycle.submit(
            draft.model_copy(update={"frontend": frontend}), builder_session_id="builder-1"
        )
        assert app_store.get_version("app-x", 1).verification == {
            "code": "pass",
            "think": "skipped",
        }

    def test_duplicate_pipeline_names_refuse_submit(self, tmp_path):
        lifecycle, app_store, _, _ = _make(tmp_path)
        dup = [
            PipelineSpec(name="d", wake_prompt="w", on_demand=True),
            PipelineSpec(name="d", wake_prompt="w", on_demand=True),
        ]
        with pytest.raises(ValueError, match="duplicate pipeline name"):
            lifecycle.submit(
                _draft("app-x", builder_sid="builder-1", pipelines=dup),
                builder_session_id="builder-1",
            )
        assert app_store.get("app-x") is None


class TestVersionSummary:
    """``AppVersionSummary.compute`` — the pure strategy-on-model version diff."""

    def _spec(self, files, *, pipes=(), cols=()) -> AppSpec:
        return AppSpec(
            app_id="a",
            title="t",
            owner_session_id="o",
            workspace_ref=WorkspaceRef(kind="own", key="k"),
            frontend=AppFrontend(entrypoint="app.py", files=files),
            pipelines=list(pipes),
            collections=list(cols),
        )

    def test_v1_none_prev_is_all_added(self):
        new = self._spec(
            {"app.py": "x", "pages/p.py": "y"},
            pipes=[PipelineSpec(name="ingest", wake_prompt="w", on_demand=True)],
            cols=[CollectionSpec(name="emails", json_schema={"type": "object"})],
        )
        s = AppVersionSummary.compute(None, new)
        assert (s.files_added, s.files_changed, s.files_removed) == (2, 0, 0)
        assert s.pipelines_added == ["ingest"] and s.pipelines_removed == []
        assert s.collections_added == ["emails"]
        described = s.describe()
        assert "+2 files" in described and "ingest" in described and "emails" in described

    def test_file_add_change_remove_counts(self):
        prev = self._spec({"app.py": "x", "old.py": "gone", "keep.py": "same"})
        new = self._spec({"app.py": "x", "keep.py": "CHANGED", "new.py": "z"})
        s = AppVersionSummary.compute(prev, new)
        assert (s.files_added, s.files_changed, s.files_removed) == (1, 1, 1)

    def test_pipeline_add_remove_and_schedule_change(self):
        prev = self._spec(
            {"app.py": "x"},
            pipes=[
                PipelineSpec(name="keep", wake_prompt="w", on_demand=True),
                PipelineSpec(name="gone", wake_prompt="w", on_demand=True),
            ],
        )
        new = self._spec(
            {"app.py": "x"},
            pipes=[
                PipelineSpec(name="keep", wake_prompt="w", schedule=CronSchedule(cron="0 9 * * *")),
                PipelineSpec(name="fresh", wake_prompt="w", on_demand=True),
            ],
        )
        s = AppVersionSummary.compute(prev, new)
        assert s.pipelines_added == ["fresh"]
        assert s.pipelines_removed == ["gone"]
        assert s.pipelines_changed == ["keep"]  # on_demand -> scheduled

    def test_trigger_ref_restamp_is_not_a_pipeline_change(self):
        # trigger_ref is platform-owned output, re-minted every submit — it must NOT
        # read as a builder-authored change.
        prev = self._spec(
            {"app.py": "x"},
            pipes=[
                PipelineSpec(
                    name="p",
                    wake_prompt="w",
                    schedule=CronSchedule(cron="0 9 * * *"),
                    trigger_ref="t-old",
                )
            ],
        )
        new = self._spec(
            {"app.py": "x"},
            pipes=[
                PipelineSpec(
                    name="p",
                    wake_prompt="w",
                    schedule=CronSchedule(cron="0 9 * * *"),
                    trigger_ref="t-new",
                )
            ],
        )
        assert AppVersionSummary.compute(prev, new).pipelines_changed == []

    def test_collection_add_and_remove(self):
        prev = self._spec(
            {"app.py": "x"}, cols=[CollectionSpec(name="old", json_schema={"type": "object"})]
        )
        new = self._spec(
            {"app.py": "x"}, cols=[CollectionSpec(name="new", json_schema={"type": "object"})]
        )
        s = AppVersionSummary.compute(prev, new)
        assert s.collections_added == ["new"] and s.collections_removed == ["old"]

    def test_describe_no_changes(self):
        prev = self._spec({"app.py": "x"})
        assert AppVersionSummary.compute(prev, prev).describe() == "no changes"

    def test_submit_records_a_diff_summary_on_resubmit(self, tmp_path):
        # End-to-end: v2 (maintainer resubmit) records a summary diffing against v1.
        lifecycle, app_store, _, _ = _make(tmp_path)
        live = lifecycle.submit(
            _draft("app-x", builder_sid="builder-1"), builder_session_id="builder-1"
        )
        maintainer = live.maintainer_session_id
        fix = _draft(
            "app-x",
            builder_sid=maintainer,
            pipelines=[PipelineSpec(name="digest", wake_prompt="w", on_demand=True)],
        )
        lifecycle.submit(fix, builder_session_id=maintainer)
        summary = app_store.get_version("app-x", 2).summary
        assert summary is not None
        assert summary.pipelines_added == ["digest"]
        # v1 also carries a summary (everything added, prev=None).
        assert app_store.get_version("app-x", 1).summary is not None


class TestIntegrityIssuePolicy:
    """Each ``on_pipeline_failure`` value, driven by an INTEGRITY issue.

    The policy means the same thing for both issue kinds — the operator declared
    one reaction to "this pipeline is not doing its job". The one addition is that
    an integrity issue ALSO emits ``app_issue`` under every policy, because its
    ledger row is a green ``succeeded`` and would otherwise leave no trace a user
    could see.
    """

    @staticmethod
    def _live_app(lifecycle, policy):
        trig = CronTrigger(
            session_id="builder-1", wake_prompt="p", cron="0 9 * * *", created_by="agent"
        )
        lifecycle.trigger_store.create(trig)
        draft = _draft(
            "app-x",
            builder_sid="builder-1",
            pipelines=[PipelineSpec(name="p", wake_prompt="wake", trigger_ref=trig.id)],
        )
        draft = draft.model_copy(update={"policies": AppPolicies(on_pipeline_failure=policy)})
        return lifecycle.submit(draft, builder_session_id="builder-1")

    @staticmethod
    def _issue():
        return PipelineIssue.unwritten("p", ["records"])

    def test_repair_starts_a_repair_run_naming_the_empty_collections(self, tmp_path):
        starter = FakeRunStarter()
        lifecycle, *_ = _make(tmp_path, run_starter=starter)
        live = self._live_app(lifecycle, "repair")
        starter.calls.clear()

        lifecycle.handle_pipeline_failure(live, self._issue())

        assert len(starter.calls) == 1
        _, message = starter.calls[0]
        assert "app-repair" in message
        assert "records" in message
        # The prompt must NOT send the agent hunting for an exception that never
        # happened — the run succeeded, and the prompt has to say so.
        assert "SUCCEEDED" in message
        assert "raised no exception" in message

    def test_the_repair_prompt_names_the_surfaces_the_maintainer_holds(self, tmp_path):
        # A repair wakes in a FRESH context holding no memory of the app and no
        # staged files. A prompt that names only the problem burns its first turns
        # rediscovering its own toolbox, so every surface is named explicitly.
        starter = FakeRunStarter()
        lifecycle, *_ = _make(tmp_path, run_starter=starter)
        live = self._live_app(lifecycle, "repair")
        starter.calls.clear()

        lifecycle.handle_pipeline_failure(live, self._issue())
        _, message = starter.calls[0]

        for surface in ("get_app", "stage", "run_pipeline", "dry_run", "app_data",
                        "submit_app", "spawn_agent"):
            assert surface in message, surface
        # Staging is ephemeral and is the ONLY route back to a code pipeline's
        # source — the agent cannot edit what broke without it.
        assert "ephemeral" in message

    def test_the_repair_prompt_defers_depth_to_the_agent_def(self, tmp_path):
        # DRY: the hypothesis set lives in the app-repair AgentDef (it is the same
        # advice when a USER reports the symptom and no dispatched prompt exists).
        # The prompt must POINT at it, not carry a second copy that can drift.
        starter = FakeRunStarter()
        lifecycle, *_ = _make(tmp_path, run_starter=starter)
        live = self._live_app(lifecycle, "repair")
        starter.calls.clear()

        lifecycle.handle_pipeline_failure(live, self._issue())
        _, message = starter.calls[0]

        assert "its own instructions" in message.lower()
        assert "swallowed" not in message.lower()  # that depth is the AgentDef's

    def test_every_policy_emission_names_the_offending_collections(self, tmp_path):
        # A violation that silently self-repairs and leaves no trace is still a
        # swallowed error from the operator's side, so the emission must be
        # actionable under EVERY policy — not just `notify`.
        for policy in ("repair", "pause", "notify"):
            lifecycle, _, _, sessions = _make(
                tmp_path / f"p-{policy}", run_starter=FakeRunStarter()
            )
            live = self._live_app(lifecycle, policy)

            lifecycle.handle_pipeline_failure(live, self._issue())

            emitted = [
                e for e in sessions.events.get(live.maintainer_session_id, [])
                if e["type"] == "app_issue"
            ]
            assert len(emitted) == 1, policy
            payload = emitted[0]["payload"]
            assert payload["collections"] == ["records"], policy
            assert "records" in payload["error"], policy

    def test_pause_pauses_the_app_for_an_integrity_issue_too(self, tmp_path):
        lifecycle, app_store, _, _ = _make(tmp_path)
        live = self._live_app(lifecycle, "pause")

        lifecycle.handle_pipeline_failure(live, self._issue())

        assert app_store.get("app-x").status == "paused"

    def test_notify_emits_the_structured_app_issue_event(self, tmp_path):
        lifecycle, app_store, _, sessions = _make(tmp_path)
        live = self._live_app(lifecycle, "notify")

        lifecycle.handle_pipeline_failure(live, self._issue())

        event = sessions.events[live.maintainer_session_id][-1]
        assert event["type"] == "app_issue"
        assert event["payload"]["kind"] == "unwritten_collections"
        assert event["payload"]["pipeline"] == "p"
        assert event["payload"]["collections"] == ["records"]
        assert app_store.get("app-x").status == "live"  # NOT paused

    def test_an_integrity_issue_is_visible_under_repair_and_pause_too(self, tmp_path):
        # Without this the app would silently pause (or spawn a repair run) on a
        # signal the user was never shown — the run row stays green either way.
        for policy in ("repair", "pause"):
            lifecycle, _, _, sessions = _make(tmp_path / policy, run_starter=FakeRunStarter())
            live = self._live_app(lifecycle, policy)

            lifecycle.handle_pipeline_failure(live, self._issue())

            kinds = [e["type"] for e in sessions.events.get(live.maintainer_session_id, [])]
            assert "app_issue" in kinds, policy

    def test_a_run_failure_does_not_emit_an_extra_event_under_repair(self, tmp_path):
        # Unchanged behavior for the failure kind: its failed ledger row is the
        # user-visible artifact, so `repair` does not also notify.
        lifecycle, _, _, sessions = _make(tmp_path, run_starter=FakeRunStarter())
        live = self._live_app(lifecycle, "repair")

        lifecycle.handle_pipeline_failure(live, PipelineIssue.run_failed("p", "boom"))

        kinds = [e["type"] for e in sessions.events.get(live.maintainer_session_id, [])]
        assert "app_issue" not in kinds
