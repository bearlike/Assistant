"""Tests for the run_pipeline SessionTool (mewbo_api.apps.plugin.run_pipeline).

Drives the tool from the caller site with a FAKE ``PipelineRunner`` (a Protocol
satisfier — no real ``pipeline_runner.py:AppPipelineRunner``), using real
:class:`AppSpec`/:class:`PipelineSpec` instances (mewbo_api.apps.models). The
fake mirrors ``AppPipelineRunner.run_pipeline``'s id-keyed adapter shape
exactly: ``run_pipeline(app_id, pipeline_name, *, params, dry_run) ->
{output, evaluated_at, docs_written, cache_hit}`` (see
``plugin/runtime.py:PipelineRunner`` — the runner's OTHER method, ``execute``,
is the object-keyed collaborator ``apps/routes.py``'s REST invoke route uses
instead; both funnel into the same engine).

Covers: arg validation (extra="forbid", defaults), app resolution (owner OR
maintainer session, not_found otherwise), pipeline resolution (unknown name
lists declared pipelines), the agentic-mode refusal, the unwired-store /
unwired-runner clean-error degrades, the happy path (dry_run pass-through,
docs_written, cache composition, output truncation), a runner exception
surfacing as an agent-visible error, and the terminal-free contract.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone

import pytest
from mewbo_api.apps.models import (
    AppFrontend,
    AppSpec,
    CollectionSpec,
    PipelineSpec,
    WorkspaceRef,
)
from mewbo_api.apps.plugin import runtime as runtime_mod
from mewbo_api.apps.plugin.run_pipeline import RunPipelineArgs, RunPipelineTool
from mewbo_api.apps.plugin.runtime import register_pipeline_runner
from mewbo_core.classes import ActionStep
from pydantic import ValidationError

SESSION_ID = "maint-1"
APP_ID = "app1"
NOW = datetime(2026, 7, 18, 9, 0, 0, tzinfo=timezone.utc)


def _app(
    *,
    owner: str | None = None,
    maintainer: str | None = None,
    pipelines: list | None = None,
    collections: list | None = None,
) -> AppSpec:
    return AppSpec(
        app_id=APP_ID,
        title="App One",
        owner_session_id=owner or "someone-else",
        maintainer_session_id=maintainer,
        workspace_ref=WorkspaceRef(kind="own", key="k"),
        frontend=AppFrontend(files={"app.py": "import streamlit as st"}),
        collections=collections or [],
        pipelines=pipelines or [],
    )


def _code_pipeline(
    name: str = "ingest", *, cache_ttl_seconds: int = 0, writes: tuple[str, ...] = ()
) -> PipelineSpec:
    return PipelineSpec(
        name=name,
        wake_prompt="parse csvs",
        mode="code",
        entrypoint=f"pipelines/{name}.py",
        cache_ttl_seconds=cache_ttl_seconds,
        on_demand=True,
        writes=writes,
    )


def _agentic_pipeline(name: str = "morning-organize") -> PipelineSpec:
    return PipelineSpec(name=name, wake_prompt="do the thing", on_demand=True)


class FakeAppStore:
    def __init__(self, *apps: AppSpec) -> None:
        self._apps = list(apps)

    def get(self, app_id):  # pragma: no cover - unused by run_pipeline, present for Protocol parity
        return next((a for a in self._apps if a.app_id == app_id), None)

    def list_apps(self, *, include_archived: bool = False) -> list:
        return list(self._apps)


class FakeRunner:
    """Records calls; returns a canned outcome dict or raises."""

    def __init__(self, *, outcome: dict | None = None, raises: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self._outcome = outcome if outcome is not None else {
            "output": {"rows": 3},
            "evaluated_at": NOW,
            "docs_written": {"emails": 3},
            "cache_hit": False,
        }
        self._raises = raises

    def run_pipeline(self, app_id: str, pipeline_name: str, *, params: dict, dry_run: bool) -> dict:
        self.calls.append((app_id, pipeline_name, params, dry_run))
        if self._raises is not None:
            raise self._raises
        return self._outcome


def _tool(app_store, runner, *, session_id=SESSION_ID) -> RunPipelineTool:
    return RunPipelineTool(session_id=session_id, app_store=app_store, runner=runner)


def _run(tool: RunPipelineTool, tool_input: dict):
    step = ActionStep(tool_id="run_pipeline", operation="execute", tool_input=tool_input)
    return asyncio.run(tool.handle(step))


def _payload(speaker) -> dict:
    return ast.literal_eval(speaker.content)


def _is_error(speaker) -> bool:
    payload = _payload(speaker)
    return isinstance(payload, dict) and "error" in payload


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


def test_tool_id_modes_and_terminal_free():
    assert RunPipelineTool.tool_id == "run_pipeline"
    assert RunPipelineTool.modes == frozenset({"act"})
    tool = _tool(FakeAppStore(), FakeRunner())
    assert tool.should_terminate_run() is False


# ---------------------------------------------------------------------------
# Arg validation
# ---------------------------------------------------------------------------


def test_args_defaults():
    args = RunPipelineArgs(pipeline="ingest")
    assert args.params == {}
    assert args.dry_run is False


def test_args_extra_field_rejected():
    tool = _tool(FakeAppStore(), FakeRunner())
    result = _run(tool, {"pipeline": "ingest", "bogus": 1})
    assert _payload(result)["error"]["code"] == "validation"


def test_args_missing_pipeline_rejected():
    with pytest.raises(ValidationError):
        RunPipelineArgs()


# ---------------------------------------------------------------------------
# App resolution — owner (pre-submit builder) OR maintainer (post-submit) session
# ---------------------------------------------------------------------------


def test_no_app_bound_to_session_is_not_found():
    tool = _tool(FakeAppStore(), FakeRunner())
    result = _run(tool, {"pipeline": "ingest"})
    assert _payload(result)["error"]["code"] == "not_found"


def test_resolves_app_by_maintainer_session():
    app = _app(maintainer=SESSION_ID, pipelines=[_code_pipeline()])
    runner = FakeRunner()
    tool = _tool(FakeAppStore(app), runner)
    result = _run(tool, {"pipeline": "ingest"})
    assert not _is_error(result)
    assert runner.calls[0][0] == APP_ID


def test_resolves_app_by_owner_session_pre_submit():
    # The builder session (owner_session_id) can dry-run test before any
    # maintainer session exists yet.
    app = _app(owner=SESSION_ID, maintainer=None, pipelines=[_code_pipeline()])
    runner = FakeRunner()
    tool = _tool(FakeAppStore(app), runner)
    result = _run(tool, {"pipeline": "ingest", "dry_run": True})
    assert not _is_error(result)


def test_foreign_session_app_is_not_found():
    app = _app(maintainer="someone-else-entirely", pipelines=[_code_pipeline()])
    tool = _tool(FakeAppStore(app), FakeRunner())
    result = _run(tool, {"pipeline": "ingest"})
    assert _payload(result)["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# Pipeline resolution
# ---------------------------------------------------------------------------


def test_unknown_pipeline_lists_declared_names():
    app = _app(
        maintainer=SESSION_ID,
        pipelines=[_code_pipeline("ingest"), _code_pipeline("cleanup")],
    )
    tool = _tool(FakeAppStore(app), FakeRunner())
    result = _run(tool, {"pipeline": "ghost"})
    payload = _payload(result)
    assert payload["error"]["code"] == "validation"
    assert "ingest" in payload["error"]["message"]
    assert "cleanup" in payload["error"]["message"]


# ---------------------------------------------------------------------------
# Agentic refusal — run_pipeline only executes mode="code"
# ---------------------------------------------------------------------------


def test_agentic_pipeline_is_refused_with_actionable_message():
    app = _app(maintainer=SESSION_ID, pipelines=[_agentic_pipeline()])
    runner = FakeRunner()
    tool = _tool(FakeAppStore(app), runner)

    result = _run(tool, {"pipeline": "morning-organize"})

    payload = _payload(result)
    assert payload["error"]["code"] == "not_executable"
    assert "agentic" in payload["error"]["message"]
    assert "wake_prompt" in payload["error"]["message"]
    assert runner.calls == []  # never reached the runner


# ---------------------------------------------------------------------------
# Unwired degrades — clean errors, never a crash
# ---------------------------------------------------------------------------


def test_unwired_app_store_is_a_clean_error(monkeypatch):
    def _boom():
        raise RuntimeError("store backend unreachable")

    monkeypatch.setattr("mewbo_api.apps.plugin.run_pipeline.get_app_store", _boom)
    tool = RunPipelineTool(session_id=SESSION_ID)  # no injected store -> factory -> raises
    result = _run(tool, {"pipeline": "ingest"})
    assert _payload(result)["error"]["code"] == "unavailable"


def test_unwired_runner_is_a_clean_error_not_a_crash(monkeypatch):
    monkeypatch.setattr(runtime_mod, "_PIPELINE_RUNNER", None)
    app = _app(maintainer=SESSION_ID, pipelines=[_code_pipeline()])
    # no runner, no provider registered
    tool = RunPipelineTool(session_id=SESSION_ID, app_store=FakeAppStore(app))

    result = _run(tool, {"pipeline": "ingest"})

    payload = _payload(result)
    assert payload["error"]["code"] == "unavailable"
    assert "not configured" in payload["error"]["message"]


def test_runner_seam_resolves_runner(monkeypatch):
    monkeypatch.setattr(runtime_mod, "_PIPELINE_RUNNER", None)
    app = _app(maintainer=SESSION_ID, pipelines=[_code_pipeline()])
    runner = FakeRunner()
    register_pipeline_runner(runner)
    # no explicit runner -> resolves through the pushed seam
    tool = RunPipelineTool(session_id=SESSION_ID, app_store=FakeAppStore(app))

    result = _run(tool, {"pipeline": "ingest"})

    assert not _is_error(result)
    assert len(runner.calls) == 1


# ---------------------------------------------------------------------------
# Happy path — dry_run pass-through, docs_written, cache composition
# ---------------------------------------------------------------------------


def test_happy_path_executes_and_reports_outcome():
    app = _app(maintainer=SESSION_ID, pipelines=[_code_pipeline(cache_ttl_seconds=300)])
    runner = FakeRunner(outcome={
        "output": {"rows": 5},
        "evaluated_at": NOW,
        "docs_written": {"emails": 5},
        "cache_hit": True,
    })
    tool = _tool(FakeAppStore(app), runner)

    result = _run(tool, {"pipeline": "ingest", "params": {"since": "2026-01-01"}})

    assert runner.calls == [(APP_ID, "ingest", {"since": "2026-01-01"}, False)]
    payload = _payload(result)
    assert payload["pipeline"] == "ingest"
    assert payload["docs_written"] == {"emails": 5}
    assert payload["evaluated_at"] == NOW.isoformat()
    assert payload["cache"] == {"hit": True, "ttl_seconds": 300}
    assert payload["dry_run"] is False
    assert payload["output_truncated"] is False


def test_every_glob_matching_nothing_names_the_bundle_versus_workspace_trap():
    """The failure that actually shipped: globs resolved against the wrong directory.

    A pipeline's data files were stored in the app BUNDLE, while `ctx.glob`
    resolves under the WORKSPACE. Every pattern matched zero files, the pipeline
    wrote nothing, and the run reported success — while an offline replay against
    the staged bundle reproduced perfectly, because there the files existed. Two
    directories both meaning "the app's files", and nothing named the difference,
    so the divergence was invisible in the pipeline source.

    The envelope has to say it, since no amount of reading the code reveals which
    directory was actually searched.
    """
    pipeline = _code_pipeline(writes=("gateway",))
    app = _app(
        maintainer=SESSION_ID,
        pipelines=[pipeline],
        collections=[CollectionSpec(name="gateway", json_schema={"type": "object"})],
    )
    runner = FakeRunner(outcome={
        "output": {},
        "evaluated_at": NOW,
        "docs_written": {},
        "evidence": {
            "globs": [
                {"pattern": "modelsnap/gateway.lzw", "match_count": 0, "paths": []},
                {"pattern": "modelsnap/providers*.json", "match_count": 0, "paths": []},
            ],
            "read_paths": [],
            "truncated": False,
            "workspace": "/tmp/mewbo/sessions/abc123",
        },
        "cache_hit": False,
    })

    payload = _payload(_run(_tool(FakeAppStore(app), runner), {
        "pipeline": "ingest", "dry_run": True,
    }))

    assert payload["attention"]["all_globs_matched_nothing"] is True
    assert payload["attention"]["workspace"] == "/tmp/mewbo/sessions/abc123"
    assert "not the app bundle" in payload["attention"]["next_step"].lower()
    assert payload["evidence"]["workspace"] == "/tmp/mewbo/sessions/abc123"


def test_one_matching_glob_is_not_flagged_as_the_workspace_trap():
    """The control: a partial match is a filter problem, not a wrong-directory one."""
    pipeline = _code_pipeline(writes=("gateway",))
    app = _app(
        maintainer=SESSION_ID,
        pipelines=[pipeline],
        collections=[CollectionSpec(name="gateway", json_schema={"type": "object"})],
    )
    runner = FakeRunner(outcome={
        "output": {},
        "evaluated_at": NOW,
        "docs_written": {},
        "evidence": {
            "globs": [
                {"pattern": "a/*.json", "match_count": 0, "paths": []},
                {"pattern": "b/*.json", "match_count": 2, "paths": ["b/x.json", "b/y.json"]},
            ],
            "read_paths": [],
            "truncated": False,
            "workspace": "/tmp/ws",
        },
        "cache_hit": False,
    })

    payload = _payload(_run(_tool(FakeAppStore(app), runner), {
        "pipeline": "ingest", "dry_run": True,
    }))

    assert "all_globs_matched_nothing" not in payload["attention"]
    # The declared-writes miss is still reported — this control narrows the
    # workspace claim only, it does not silence the contract violation.
    assert payload["attention"]["missing_expected_writes"] == ["gateway"]


def test_zero_write_materialization_surfaces_evidence_and_next_step():
    pipeline = _code_pipeline(writes=("gateway",))
    app = _app(
        maintainer=SESSION_ID,
        pipelines=[pipeline],
        collections=[
            CollectionSpec(name="gateway", json_schema={"type": "object"}),
            CollectionSpec(name="models", json_schema={"type": "object"}),
        ],
    )
    runner = FakeRunner(outcome={
        "output": {"gateway_rows": 0},
        "evaluated_at": NOW,
        "docs_written": {},
        "evidence": {
            "globs": [
                {"pattern": "modelsnap/gateway.lzw", "match_count": 0, "paths": []}
            ],
            "read_paths": [],
            "truncated": False,
        },
        "cache_hit": False,
    })

    payload = _payload(_run(_tool(FakeAppStore(app), runner), {
        "pipeline": "ingest", "dry_run": True,
    }))

    assert payload["evidence"]["globs"] == [
        {"pattern": "modelsnap/gateway.lzw", "match_count": 0, "paths": []}
    ]
    assert payload["unwritten_collections"] == ["gateway", "models"]
    assert payload["attention"]["missing_expected_writes"] == ["gateway"]
    # This fixture's only glob matched nothing, so the envelope ALSO raises the
    # wrong-directory case — the more specific diagnosis wins the next step.
    assert payload["attention"]["all_globs_matched_nothing"] is True


def test_dry_run_passes_through_to_the_runner():
    app = _app(maintainer=SESSION_ID, pipelines=[_code_pipeline()])
    runner = FakeRunner()
    tool = _tool(FakeAppStore(app), runner)

    result = _run(tool, {"pipeline": "ingest", "dry_run": True})

    assert runner.calls == [(APP_ID, "ingest", {}, True)]
    assert _payload(result)["dry_run"] is True


def test_output_truncated_with_note():
    app = _app(maintainer=SESSION_ID, pipelines=[_code_pipeline()])
    huge = {"rows": list(range(10_000))}  # serializes far past the char cap
    runner = FakeRunner(outcome={
        "output": huge, "evaluated_at": NOW, "docs_written": {}, "cache_hit": False,
    })
    tool = _tool(FakeAppStore(app), runner)

    result = _run(tool, {"pipeline": "ingest"})

    payload = _payload(result)
    assert payload["output_truncated"] is True
    assert len(payload["output"]) <= 4000


def test_string_output_passes_through_unmodified_when_short():
    app = _app(maintainer=SESSION_ID, pipelines=[_code_pipeline()])
    runner = FakeRunner(outcome={
        "output": "plain text result", "evaluated_at": NOW, "docs_written": {}, "cache_hit": False,
    })
    tool = _tool(FakeAppStore(app), runner)

    result = _run(tool, {"pipeline": "ingest"})

    payload = _payload(result)
    assert payload["output"] == "plain text result"
    assert payload["output_truncated"] is False


# ---------------------------------------------------------------------------
# Runner failure — agent-visible, never a crash
# ---------------------------------------------------------------------------


def test_runner_exception_surfaces_as_execution_error():
    app = _app(maintainer=SESSION_ID, pipelines=[_code_pipeline()])
    runner = FakeRunner(raises=ValueError("bad csv row at line 12"))
    tool = _tool(FakeAppStore(app), runner)

    result = _run(tool, {"pipeline": "ingest"})

    payload = _payload(result)
    assert payload["error"]["code"] == "execution"
    assert "bad csv row at line 12" in payload["error"]["message"]
