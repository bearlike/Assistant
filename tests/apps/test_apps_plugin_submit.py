"""Tests for the submit_app SessionTool (mewbo_api.apps.plugin.submit_app).

Drives the tool from the caller site with a FAKE ``AppSubmitter`` (a Protocol
satisfier — no real lifecycle), the frontend written to a real temp app root.
Covers: the terminal happy path (read files off disk -> submit -> terminate;
``app_ready`` is AppLifecycle.submit's job, not this tool's — see
test_apps_lifecycle.py), bounded reask on invalid args / lint failure / missing
entrypoint / lifecycle rejection, the give-up-at-cap terminate, the
runtime-not-configured degrade, and the down-only provider resolution.
"""

from __future__ import annotations

import asyncio

import pytest
from mewbo_api.apps.models import AppSpec, AppVersion, AppVersionSummary
from mewbo_api.apps.plugin import runtime as runtime_mod
from mewbo_api.apps.plugin.runtime import register_app_submitter
from mewbo_api.apps.plugin.submit_app import (
    PipelineSchedule,
    SubmitAppArgs,
    SubmitAppTool,
    SubmitPipelineArgs,
)
from mewbo_api.apps.store import JsonAppStore, set_stores_for_tests
from mewbo_core.classes import ActionStep
from pydantic import ValidationError

SESSION_ID = "s1"
APP_ID = "my-app"
CLEAN_APP_PY = "import streamlit as st\nimport mewbo_app\nst.title('x')\n"


class FakeSubmitter:
    """A Protocol-satisfying ``AppSubmitter`` — records the draft, returns it live."""

    def __init__(self, *, raises: Exception | None = None, version: int = 1) -> None:
        self.calls: list[tuple[AppSpec, str]] = []
        self._raises = raises
        self._version = version

    def submit(self, draft: AppSpec, *, builder_session_id: str) -> AppSpec:
        self.calls.append((draft, builder_session_id))
        if self._raises is not None:
            raise self._raises
        return draft.model_copy(
            update={"version": self._version, "status": "live", "maintainer_session_id": "maint-1"}
        )


class _PersistingSubmitter:
    """A submitter that also writes the version row — like the real lifecycle.

    ``submit_app`` reads the persisted :class:`AppVersion` back (via the process-wide
    store factory) to echo the change summary + verification verdicts, so this stand-
    in persists a row carrying them into the store the tool reads.
    """

    def __init__(self, store, *, version, summary=None, verification=None) -> None:
        self._store = store
        self._version = version
        self._summary = summary
        self._verification = verification
        self.calls: list[tuple[AppSpec, str]] = []

    def submit(self, draft: AppSpec, *, builder_session_id: str) -> AppSpec:
        self.calls.append((draft, builder_session_id))
        live = draft.model_copy(
            update={"version": self._version, "status": "live", "maintainer_session_id": "maint-1"}
        )
        self._store.save(live)
        self._store.save_version(
            AppVersion(
                app_id=live.app_id,
                version=self._version,
                spec=live,
                author="repair",
                summary=self._summary,
                verification=self._verification,
            )
        )
        return live


@pytest.fixture
def apps_root(tmp_path, monkeypatch):
    monkeypatch.setenv("MEWBO_APPS_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _isolate_app_store(tmp_path):
    """Point the process-wide app store at a fresh per-test JSON store.

    ``submit_app`` now reads the persisted ``AppVersion`` back through
    ``get_app_store()`` for its success echo (the same store resolution the
    ``app_data`` tool uses), so isolate that singleton per test — no cross-test bleed,
    no real-FS default store — and reset it afterward.
    """
    store = JsonAppStore(root_dir=tmp_path / "store")
    set_stores_for_tests(app_store=store)
    yield store
    set_stores_for_tests()


def _write_app(apps_root, files: dict[str, str], *, session_id=SESSION_ID, app_id=APP_ID) -> None:
    app_dir = apps_root / session_id / app_id
    for rel, content in files.items():
        path = app_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _args(**overrides) -> dict:
    base = {
        "app_id": APP_ID,
        "title": "My App",
        "summary": "does things",
        "icon": "📥",
        "workspace_ref": {"kind": "own", "key": "my-app"},
        "collections": [
            {
                "name": "emails",
                "json_schema": {"type": "object", "properties": {"s": {"type": "string"}}},
            }
        ],
        "pipelines": [
            {
                "name": "daily",
                "wake_prompt": "fetch and upsert",
                "tools_allowlist": ["app_data"],
                "on_demand": True,
            }
        ],
    }
    base.update(overrides)
    return base


def _run(tool: SubmitAppTool, tool_input: dict):
    step = ActionStep(tool_id="submit_app", operation="execute", tool_input=tool_input)
    return asyncio.run(tool.handle(step))


# ---------------------------------------------------------------------------
# Protocol / contract
# ---------------------------------------------------------------------------


def test_tool_id_modes_and_terminal_reason():
    assert SubmitAppTool.tool_id == "submit_app"
    assert SubmitAppTool.modes == frozenset({"act"})
    assert SubmitAppTool(session_id=SESSION_ID).terminal_reason() == "completed"


# ---------------------------------------------------------------------------
# Happy path — files on disk -> submit -> app_ready + terminate
# ---------------------------------------------------------------------------


def test_happy_path_submits_and_terminates(apps_root):
    _write_app(apps_root, {"app.py": CLEAN_APP_PY, "pages/inbox.py": CLEAN_APP_PY})
    events: list = []
    submitter = FakeSubmitter(version=1)
    tool = SubmitAppTool(session_id=SESSION_ID, event_logger=events.append, submitter=submitter)

    result = _run(tool, _args())

    # submitter received the draft + builder session id
    assert len(submitter.calls) == 1
    draft, builder_session_id = submitter.calls[0]
    assert builder_session_id == SESSION_ID
    assert draft.owner_session_id == SESSION_ID
    assert draft.app_id == APP_ID
    assert draft.status == "building"
    # frontend read off disk (both files, injected context skipped)
    assert set(draft.frontend.files) == {"app.py", "pages/inbox.py"}
    # app_ready is AppLifecycle.submit's job, not the tool's — emitting it here
    # too would double the event (the bug this test now guards against).
    assert events == []
    # terminates once, then resets
    assert tool.should_terminate_run() is True
    assert tool.should_terminate_run() is False
    assert "live" in result.content


def test_success_echo_includes_change_summary_and_verdicts(apps_root, _isolate_app_store):
    # submit_app reads the persisted version back and echoes its describe() line +
    # any non-pass verification verdicts, so the transcript carries the verifiable fact.
    _write_app(apps_root, {"app.py": CLEAN_APP_PY})
    summary = AppVersionSummary(files_changed=2, pipelines_added=["meetings"])
    submitter = _PersistingSubmitter(
        _isolate_app_store, version=2, summary=summary, verification={"daily": "skipped"}
    )
    tool = SubmitAppTool(session_id=SESSION_ID, submitter=submitter)

    result = _run(tool, _args())

    assert "v2" in result.content
    assert "Changes: " in result.content
    assert "2 files changed" in result.content and "meetings" in result.content
    assert "Pipeline verification: daily=skipped" in result.content


def test_success_echo_omits_verdict_line_when_all_pass(apps_root, _isolate_app_store):
    _write_app(apps_root, {"app.py": CLEAN_APP_PY})
    summary = AppVersionSummary(files_added=1)
    submitter = _PersistingSubmitter(
        _isolate_app_store, version=1, summary=summary, verification={"p": "pass"}
    )
    tool = SubmitAppTool(session_id=SESSION_ID, submitter=submitter)

    result = _run(tool, _args())

    assert "Changes: " in result.content
    assert "verification" not in result.content.lower()  # an all-pass run adds nothing


def test_success_echo_degrades_when_version_not_readable(apps_root):
    # A FakeSubmitter that persists nothing (the store read-back finds no row) still
    # yields a clean success message — the detail simply omits, never crashes.
    _write_app(apps_root, {"app.py": CLEAN_APP_PY})
    tool = SubmitAppTool(session_id=SESSION_ID, submitter=FakeSubmitter(version=1))

    result = _run(tool, _args())

    assert "live" in result.content and "Changes:" not in result.content


def test_pipeline_schedule_passes_through_to_submitter_verbatim(apps_root):
    """schedule/on_demand reach the submitted draft's pipeline entry unchanged.

    Round-trips through the real AppSpec/PipelineSpec (mewbo_api.apps.models):
    this plugin dumps its own (flat) `SubmitPipelineArgs.schedule` with
    `exclude_none` so only the relevant `cron`/`at` sibling survives, and the
    model side's `CronSchedule`/`AtSchedule` discriminated union parses it back
    into a real strategy-bearing object — not a bare dict.
    """
    _write_app(apps_root, {"app.py": CLEAN_APP_PY})
    submitter = FakeSubmitter()
    tool = SubmitAppTool(session_id=SESSION_ID, submitter=submitter)

    result = _run(
        tool,
        _args(
            pipelines=[
                {
                    "name": "morning-organize",
                    "wake_prompt": "fetch and upsert",
                    "schedule": {"kind": "time.cron", "cron": "0 7 * * *"},
                    "tools_allowlist": ["app_data"],
                }
            ]
        ),
    )

    assert len(submitter.calls) == 1, result.content
    draft, _ = submitter.calls[0]
    (pipeline,) = draft.pipelines
    assert pipeline.schedule.kind == "time.cron"
    assert pipeline.schedule.cron == "0 7 * * *"
    assert pipeline.on_demand is False
    assert pipeline.trigger_ref is None  # platform-owned — the agent never sets it


def test_code_pipeline_full_round_trip_through_appspec(apps_root):
    """A `mode="code"` pipeline reaches the real AppSpec/PipelineSpec, submitted live.

    End-to-end through `_build_spec`'s AppSpec(...) construction — proves
    SubmitPipelineArgs' new fields (mode/entrypoint/params_schema/
    cache_ttl_seconds) round-trip into the model's PipelineSpec cleanly (both
    sides' validators converged independently on the same shape).
    """
    _write_app(
        apps_root,
        {
            "app.py": CLEAN_APP_PY,
            "pipelines/ingest.py": "import csv\n\ndef run(params, ctx):\n    return {}\n",
        },
    )
    submitter = FakeSubmitter()
    tool = SubmitAppTool(session_id=SESSION_ID, submitter=submitter)
    schema = {"type": "object", "properties": {"since": {"type": "string"}}}

    result = _run(
        tool,
        _args(
            pipelines=[
                {
                    "name": "ingest",
                    "wake_prompt": "parse csvs into expenses",
                    "mode": "code",
                    "entrypoint": "pipelines/ingest.py",
                    "params_schema": schema,
                    "cache_ttl_seconds": 300,
                    "schedule": {"kind": "time.cron", "cron": "0 */6 * * *"},
                }
            ]
        ),
    )

    assert len(submitter.calls) == 1, result.content
    draft, _ = submitter.calls[0]
    (pipeline,) = draft.pipelines
    assert pipeline.mode == "code"
    assert pipeline.entrypoint == "pipelines/ingest.py"
    assert pipeline.params_schema == schema
    assert pipeline.cache_ttl_seconds == 300


def test_injected_context_file_is_never_read_into_the_spec(apps_root):
    _write_app(apps_root, {"app.py": CLEAN_APP_PY, "_app_context.json": '{"token": "secret"}'})
    submitter = FakeSubmitter()
    tool = SubmitAppTool(session_id=SESSION_ID, submitter=submitter)

    _run(tool, _args())

    draft, _ = submitter.calls[0]
    assert "_app_context.json" not in draft.frontend.files


# ---------------------------------------------------------------------------
# Pipeline schedule / on_demand — Phase 1 (platform-armed, agent never
# sets trigger_ref). Unit-level: exercises SubmitPipelineArgs/PipelineSchedule
# directly, independent of AppSpec/PipelineSpec (the models agent's surface).
# ---------------------------------------------------------------------------


def test_pipeline_requires_schedule_or_on_demand():
    with pytest.raises(ValidationError, match="schedule.*on_demand"):
        SubmitPipelineArgs(name="daily", wake_prompt="fetch and upsert")


def test_pipeline_cron_schedule_accepted():
    args = SubmitPipelineArgs(
        name="daily",
        wake_prompt="fetch and upsert",
        schedule={"kind": "time.cron", "cron": "0 7 * * *"},
    )
    assert args.schedule is not None
    assert args.schedule.kind == "time.cron"
    assert args.schedule.cron == "0 7 * * *"
    assert args.on_demand is False


def test_pipeline_time_at_schedule_accepted():
    args = SubmitPipelineArgs(
        name="reminder",
        wake_prompt="send it",
        schedule={"kind": "time.at", "at": "2026-07-20T09:00:00Z"},
    )
    assert args.schedule is not None
    assert args.schedule.kind == "time.at"
    assert args.schedule.at is not None
    assert args.schedule.at.tzinfo is not None


def test_pipeline_on_demand_accepted_with_no_schedule():
    args = SubmitPipelineArgs(name="reindex", wake_prompt="rebuild the index", on_demand=True)
    assert args.schedule is None
    assert args.on_demand is True


def test_pipeline_bad_cron_shape_rejected():
    with pytest.raises(ValidationError, match="invalid cron expression"):
        SubmitPipelineArgs(
            name="daily",
            wake_prompt="fetch and upsert",
            schedule={"kind": "time.cron", "cron": "not a cron"},
        )


def test_pipeline_naive_at_rejected():
    with pytest.raises(ValidationError, match="timezone offset"):
        SubmitPipelineArgs(
            name="reminder",
            wake_prompt="send it",
            schedule={"kind": "time.at", "at": "2026-07-20T09:00:00"},
        )


def test_pipeline_unknown_schedule_kind_rejected():
    with pytest.raises(ValidationError):
        SubmitPipelineArgs(
            name="daily",
            wake_prompt="fetch and upsert",
            schedule={"kind": "weekly", "cron": "0 7 * * *"},
        )


def test_pipeline_schedule_extra_key_rejected():
    with pytest.raises(ValidationError):
        SubmitPipelineArgs(
            name="daily",
            wake_prompt="fetch and upsert",
            schedule={"kind": "time.cron", "cron": "0 7 * * *", "trigger_ref": "t1"},
        )


def test_pipeline_schedule_missing_kind_rejected():
    with pytest.raises(ValidationError):
        SubmitPipelineArgs(
            name="daily",
            wake_prompt="fetch and upsert",
            schedule={"cron": "0 7 * * *"},
        )


def test_submit_app_rejects_colon_in_app_id():
    # A colon breaks render-token parsing (`<app_id>:<exp>:<nonce>:<sig>`) — rejected
    # at the tool boundary so the agent gets an immediate reask (the model-side
    # AppSpec.app_id validator is the durable enforcement).
    with pytest.raises(ValidationError):
        SubmitAppArgs.model_validate(_args(app_id="app:evil"))


def test_submit_app_accepts_plain_app_id():
    assert SubmitAppArgs.model_validate(_args(app_id="app-1a2b3c4d")).app_id == "app-1a2b3c4d"


def test_pipeline_schedule_is_a_bare_pydantic_model():
    """The nested schedule validates independently too (kind Literal + model_validator)."""
    schedule = PipelineSchedule(kind="time.cron", cron="*/15 * * * *")
    assert schedule.at is None


def test_pipeline_mode_defaults_to_agentic():
    args = SubmitPipelineArgs(name="daily", wake_prompt="fetch and upsert", on_demand=True)
    assert args.mode == "agentic"
    assert args.entrypoint is None
    assert args.params_schema is None
    assert args.cache_ttl_seconds == 0


def test_pipeline_code_mode_with_entrypoint_accepted():
    args = SubmitPipelineArgs(
        name="ingest",
        wake_prompt="parse csvs",
        mode="code",
        entrypoint="pipelines/ingest.py",
        on_demand=True,
    )
    assert args.mode == "code"
    assert args.entrypoint == "pipelines/ingest.py"


def test_pipeline_code_mode_without_entrypoint_rejected():
    with pytest.raises(ValidationError, match="mode='code' but no `entrypoint`"):
        SubmitPipelineArgs(name="ingest", wake_prompt="parse csvs", mode="code", on_demand=True)


def test_pipeline_agentic_mode_with_entrypoint_rejected():
    with pytest.raises(ValidationError, match="mode='agentic' but sets `entrypoint`"):
        SubmitPipelineArgs(
            name="daily",
            wake_prompt="fetch and upsert",
            entrypoint="pipelines/x.py",
            on_demand=True,
        )


def test_pipeline_user_writable_defaults_false():
    args = SubmitPipelineArgs(name="reindex", wake_prompt="rebuild", on_demand=True)
    assert args.user_writable is False


def test_pipeline_user_writable_on_code_pipeline_accepted():
    args = SubmitPipelineArgs(
        name="add-note",
        wake_prompt="append a submitted note",
        mode="code",
        entrypoint="pipelines/add_note.py",
        user_writable=True,
        on_demand=True,
    )
    assert args.user_writable is True


def test_pipeline_user_writable_requires_code_mode():
    # user_writable declares the frontend may SUBMIT this pipeline's params; only a
    # mode="code" pipeline executes synchronously on that invoke, so an agentic one
    # is rejected at the tool boundary.
    with pytest.raises(ValidationError, match="user_writable.*not mode='code'"):
        SubmitPipelineArgs(
            name="daily",
            wake_prompt="fetch and upsert",
            user_writable=True,
            on_demand=True,
        )


@pytest.mark.parametrize("bad_entrypoint", ["/etc/passwd", "../x.py", "a/../b.py", ""])
def test_pipeline_entrypoint_rejects_non_relative_paths(bad_entrypoint):
    with pytest.raises(ValidationError):
        SubmitPipelineArgs(
            name="ingest",
            wake_prompt="parse csvs",
            mode="code",
            entrypoint=bad_entrypoint,
            on_demand=True,
        )


def test_pipeline_params_schema_must_be_a_dict():
    # Enforced by the `dict[str, Any] | None` type annotation itself (no
    # separate validator needed — see submit_app.py's comment on that field).
    with pytest.raises(ValidationError, match="params_schema"):
        SubmitPipelineArgs(
            name="ingest",
            wake_prompt="parse csvs",
            mode="code",
            entrypoint="pipelines/ingest.py",
            params_schema="not-a-dict",
            on_demand=True,
        )


def test_pipeline_params_schema_dict_accepted():
    schema = {"type": "object", "properties": {"since": {"type": "string"}}}
    args = SubmitPipelineArgs(
        name="ingest",
        wake_prompt="parse csvs",
        mode="code",
        entrypoint="pipelines/ingest.py",
        params_schema=schema,
        on_demand=True,
    )
    assert args.params_schema == schema


def test_pipeline_cache_ttl_seconds_defaults_zero():
    args = SubmitPipelineArgs(
        name="ingest",
        wake_prompt="parse csvs",
        mode="code",
        entrypoint="pipelines/ingest.py",
        on_demand=True,
    )
    assert args.cache_ttl_seconds == 0


def test_pipeline_cache_ttl_seconds_negative_rejected():
    with pytest.raises(ValidationError):
        SubmitPipelineArgs(
            name="ingest",
            wake_prompt="parse csvs",
            mode="code",
            entrypoint="pipelines/ingest.py",
            cache_ttl_seconds=-1,
            on_demand=True,
        )


def test_pipeline_cache_ttl_seconds_positive_accepted():
    args = SubmitPipelineArgs(
        name="ingest",
        wake_prompt="parse csvs",
        mode="code",
        entrypoint="pipelines/ingest.py",
        cache_ttl_seconds=300,
        on_demand=True,
    )
    assert args.cache_ttl_seconds == 300


def test_pipeline_timeout_seconds_defaults_and_bounds():
    default = SubmitPipelineArgs(
        name="ingest", wake_prompt="parse csvs", mode="code",
        entrypoint="pipelines/ingest.py", on_demand=True,
    )
    assert default.timeout_seconds == 10
    # A raised ceiling for an llm pipeline passes through; 0 / >600 are rejected.
    raised = SubmitPipelineArgs(
        name="triage", wake_prompt="classify", mode="code",
        entrypoint="pipelines/triage.py", timeout_seconds=240, on_demand=True,
    )
    assert raised.timeout_seconds == 240
    with pytest.raises(ValidationError):
        SubmitPipelineArgs(
            name="ingest", wake_prompt="parse csvs", mode="code",
            entrypoint="pipelines/ingest.py", timeout_seconds=0, on_demand=True,
        )
    with pytest.raises(ValidationError):
        SubmitPipelineArgs(
            name="ingest", wake_prompt="parse csvs", mode="code",
            entrypoint="pipelines/ingest.py", timeout_seconds=601, on_demand=True,
        )


# ---------------------------------------------------------------------------
# Code-pipeline entrypoint existence + lint routing (Phase 2) — exercised
# at the tool level BEFORE AppSpec/PipelineSpec construction, since that model
# (workstream C) doesn't carry mode/entrypoint/params_schema/cache_ttl_seconds
# yet — see SubmitAppTool._missing_pipeline_entrypoints / _lint_frontend.
# ---------------------------------------------------------------------------


def test_missing_pipeline_entrypoint_reask(apps_root):
    _write_app(apps_root, {"app.py": CLEAN_APP_PY})  # no pipelines/ingest.py written
    submitter = FakeSubmitter()
    tool = SubmitAppTool(session_id=SESSION_ID, submitter=submitter)

    result = _run(
        tool,
        _args(
            pipelines=[
                {
                    "name": "ingest",
                    "wake_prompt": "parse csvs",
                    "mode": "code",
                    "entrypoint": "pipelines/ingest.py",
                    "on_demand": True,
                }
            ]
        ),
    )

    assert "not found among your submitted files" in result.content
    assert submitter.calls == []


def test_pipeline_entrypoint_present_passes_the_missing_check(apps_root):
    _write_app(
        apps_root,
        {"app.py": CLEAN_APP_PY, "pipelines/ingest.py": "def run(params, ctx):\n    return {}\n"},
    )
    args = SubmitAppArgs.model_validate(
        _args(
            pipelines=[
                {
                    "name": "ingest",
                    "wake_prompt": "parse csvs",
                    "mode": "code",
                    "entrypoint": "pipelines/ingest.py",
                    "on_demand": True,
                }
            ]
        )
    )
    files = {
        "app.py": CLEAN_APP_PY,
        "pipelines/ingest.py": "def run(params, ctx):\n    return {}\n",
    }
    assert SubmitAppTool._missing_pipeline_entrypoints(args, files) == []


def test_missing_pipeline_entrypoints_helper_reports_unwritten_files():
    args = SubmitAppArgs.model_validate(
        _args(
            pipelines=[
                {
                    "name": "ingest",
                    "wake_prompt": "parse csvs",
                    "mode": "code",
                    "entrypoint": "pipelines/ingest.py",
                    "on_demand": True,
                }
            ]
        )
    )
    assert SubmitAppTool._missing_pipeline_entrypoints(args, {"app.py": CLEAN_APP_PY}) == [
        "pipelines/ingest.py"
    ]


def test_lint_frontend_routes_pipeline_files_to_the_lighter_rule_set():
    """A pipeline file importing plain stdlib (csv) is NOT flagged as unsupported-import
    when routed as a pipeline entrypoint, but IS flagged when treated as a frontend file —
    proving the routing (not just the rule table) is what fixes the false positive.
    """
    files = {
        "app.py": CLEAN_APP_PY,
        "pipelines/ingest.py": "import csv\n\ndef run(params, ctx):\n    return {}\n",
    }
    # Routed as a pipeline entrypoint: csv is fine, no findings.
    assert SubmitAppTool._lint_frontend(
        files, pipeline_entrypoints=frozenset({"pipelines/ingest.py"})
    ) == ""
    # NOT routed (treated as ordinary frontend): csv is outside ALLOWED_MODULES.
    findings = SubmitAppTool._lint_frontend(files, pipeline_entrypoints=frozenset())
    assert "pipelines/ingest.py" in findings
    assert "unsupported-import" in findings.lower() or "csv" in findings


def test_lint_frontend_still_bans_dynamic_exec_in_pipeline_files():
    files = {
        "app.py": CLEAN_APP_PY,
        "pipelines/bad.py": "def run(params, ctx):\n    return eval('1+1')\n",
    }
    findings = SubmitAppTool._lint_frontend(
        files, pipeline_entrypoints=frozenset({"pipelines/bad.py"})
    )
    assert "pipelines/bad.py" in findings
    assert "forbidden-dynamic-exec" in findings.lower() or "eval" in findings.lower()


def test_submit_app_args_pipeline_neither_schedule_nor_on_demand_reask(apps_root):
    """The check fires at SubmitAppArgs validation — before any file I/O or AppSpec build."""
    _write_app(apps_root, {"app.py": CLEAN_APP_PY})
    submitter = FakeSubmitter()
    tool = SubmitAppTool(session_id=SESSION_ID, submitter=submitter)

    result = _run(
        tool,
        _args(
            pipelines=[
                {"name": "daily", "wake_prompt": "fetch and upsert", "tools_allowlist": []}
            ]
        ),
    )

    assert "NOT SUBMITTED" in result.content
    assert submitter.calls == []


# ---------------------------------------------------------------------------
# Bounded reask — invalid args / lint / missing entrypoint / lifecycle reject
# ---------------------------------------------------------------------------


def test_invalid_args_reask_without_submitting(apps_root):
    _write_app(apps_root, {"app.py": CLEAN_APP_PY})
    submitter = FakeSubmitter()
    tool = SubmitAppTool(session_id=SESSION_ID, submitter=submitter)

    # extra="forbid": an unknown field is a clean reask
    result = _run(tool, _args(bogus="x"))

    assert "NOT SUBMITTED" in result.content
    assert submitter.calls == []
    assert tool.should_terminate_run() is False


def test_lint_failure_reask(apps_root):
    _write_app(apps_root, {"app.py": "import requests\nrequests.get('http://x')\n"})
    submitter = FakeSubmitter()
    tool = SubmitAppTool(session_id=SESSION_ID, submitter=submitter)

    result = _run(tool, _args())

    assert "NOT SUBMITTED" in result.content
    assert "lint" in result.content.lower()
    assert submitter.calls == []
    assert tool.should_terminate_run() is False


def test_missing_entrypoint_reask(apps_root):
    _write_app(apps_root, {"main.py": CLEAN_APP_PY})  # no app.py, default entrypoint
    submitter = FakeSubmitter()
    tool = SubmitAppTool(session_id=SESSION_ID, submitter=submitter)

    result = _run(tool, _args())

    assert "NOT SUBMITTED" in result.content
    assert submitter.calls == []


def test_missing_app_directory_reask(apps_root):
    submitter = FakeSubmitter()
    tool = SubmitAppTool(session_id=SESSION_ID, submitter=submitter)

    result = _run(tool, _args())

    assert "NOT SUBMITTED" in result.content
    assert submitter.calls == []


def test_lifecycle_rejection_reask(apps_root):
    _write_app(apps_root, {"app.py": CLEAN_APP_PY})
    submitter = FakeSubmitter(raises=ValueError("too many armed triggers"))
    tool = SubmitAppTool(session_id=SESSION_ID, submitter=submitter)

    result = _run(tool, _args())

    assert "NOT SUBMITTED" in result.content
    assert "too many armed triggers" in result.content
    assert len(submitter.calls) == 1  # it was attempted
    assert tool.should_terminate_run() is False


def test_gives_up_and_terminates_at_failure_cap(apps_root):
    _write_app(apps_root, {"app.py": CLEAN_APP_PY})
    submitter = FakeSubmitter()
    tool = SubmitAppTool(session_id=SESSION_ID, submitter=submitter, max_failures=3)

    r1 = _run(tool, _args(bogus="x"))
    assert "NOT SUBMITTED" in r1.content
    assert tool.should_terminate_run() is False

    r2 = _run(tool, _args(bogus="x"))
    assert "NOT SUBMITTED" in r2.content
    assert tool.should_terminate_run() is False

    r3 = _run(tool, _args(bogus="x"))
    assert "stopping" in r3.content
    assert tool.should_terminate_run() is True  # the 3rd failure terminated the run


# ---------------------------------------------------------------------------
# Runtime resolution — constructor override, provider seam, unconfigured
# ---------------------------------------------------------------------------


def test_runtime_not_configured_is_a_clean_error_not_a_crash(apps_root, monkeypatch):
    monkeypatch.setattr(runtime_mod, "_SUBMITTER", None)
    _write_app(apps_root, {"app.py": CLEAN_APP_PY})
    tool = SubmitAppTool(session_id=SESSION_ID)  # no submitter, no provider

    result = _run(tool, _args())

    assert "not configured" in result.content
    # An ops failure does not consume the reask budget or terminate.
    assert tool.should_terminate_run() is False


def test_submitter_seam_resolves_submitter(apps_root, monkeypatch):
    monkeypatch.setattr(runtime_mod, "_SUBMITTER", None)
    _write_app(apps_root, {"app.py": CLEAN_APP_PY})
    submitter = FakeSubmitter(version=2)
    register_app_submitter(submitter)
    tool = SubmitAppTool(session_id=SESSION_ID)  # no explicit submitter -> pushed seam

    result = _run(tool, _args())

    assert len(submitter.calls) == 1
    assert "v2" in result.content
