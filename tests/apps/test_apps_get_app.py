"""Tests for the get_app SessionTool (mewbo_api.apps.plugin.get_app).

Drives the tool from the caller site with REAL Json stores (JsonAppStore /
JsonAppDataStore / JsonPipelineRunStore under a tmp root) and real
:class:`AppSpec`/:class:`PipelineSpec` instances — stubbing only the clock (an
injected fixed ``NOW``), never patching one. Covers the two operations and the
three guarantees the tool owns:

* SCOPE — the app is resolved by session identity (owner OR maintainer); a
  foreign/unknown session reads a uniform ``not_found`` (no app_id argument
  exists to leak a foreign id).
* ``get`` — the live manifest shape WITHOUT file bodies: version/latest_version,
  per-collection doc counts, per-pipeline mode/schedule/trigger_declared/last-run/freshness
  (from the ledger, via ``PipelineRun.freshness``), and a path+size file list.
* ``stage`` — the full stored bundle (incl. ``mode="code"`` pipeline source)
  re-materialized to disk byte-equal, plus the path-confinement guard.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timedelta, timezone

from mewbo_api.apps import store as store_mod
from mewbo_api.apps.models import (
    AppFrontend,
    AppPolicies,
    AppSpec,
    AppVersion,
    CollectionSpec,
    CronSchedule,
    PipelineRun,
    PipelineSpec,
    WorkspaceRef,
)
from mewbo_api.apps.plugin.get_app import GetAppArgs, GetAppTool
from mewbo_api.apps.store import (
    JsonAppDataStore,
    JsonAppStore,
    JsonPipelineRunStore,
)
from mewbo_core.classes import ActionStep

NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
SESSION_ID = "maint-1"
APP_ID = "app1"

_EMAIL_SCHEMA = {
    "type": "object",
    "properties": {"subject": {"type": "string"}},
    "required": ["subject"],
    "additionalProperties": False,
}

_FILES = {
    "app.py": "import streamlit as st\nimport mewbo_app\n",
    "pages/all.py": "import streamlit as st\n",
    "pipelines/ingest.py": "def run(params, ctx):\n    return {'ok': True}\n",
}


def _app(
    *,
    owner: str = "owner-sess",
    maintainer: str | None = SESSION_ID,
    version: int = 1,
    status: str = "live",
) -> AppSpec:
    return AppSpec(
        app_id=APP_ID,
        title="App One",
        summary="Does a thing.",
        icon="📥",
        owner_session_id=owner,
        maintainer_session_id=maintainer,
        workspace_ref=WorkspaceRef(kind="own", key="k"),
        frontend=AppFrontend(files=dict(_FILES)),
        collections=[
            CollectionSpec(name="emails", json_schema=_EMAIL_SCHEMA),
            CollectionSpec(name="groups", json_schema={"type": "object"}),
        ],
        pipelines=[
            PipelineSpec(
                name="ingest",
                wake_prompt="parse csvs",
                mode="code",
                entrypoint="pipelines/ingest.py",
                schedule=CronSchedule(cron="0 7 * * *"),
                trigger_ref="trig-armed",
            ),
            PipelineSpec(name="digest", wake_prompt="summarize", on_demand=True),
        ],
        policies=AppPolicies(max_docs_per_collection=1000),
        version=version,
        status=status,  # type: ignore[arg-type]
    )


def _stores(tmp_path):
    root = tmp_path / "apps"
    return (
        JsonAppStore(root_dir=root),
        JsonAppDataStore(root_dir=root),
        JsonPipelineRunStore(root_dir=root),
    )


def _tool(app_store, data_store, run_store, *, session_id=SESSION_ID) -> GetAppTool:
    return GetAppTool(
        session_id=session_id,
        app_store=app_store,
        data_store=data_store,
        run_store=run_store,
        now_fn=lambda: NOW,
    )


def _run(tool: GetAppTool, tool_input: dict):
    step = ActionStep(tool_id="get_app", operation="execute", tool_input=tool_input)
    return asyncio.run(tool.handle(step))


def _payload(speaker) -> dict:
    return ast.literal_eval(speaker.content)


def _is_error(speaker) -> bool:
    payload = _payload(speaker)
    return isinstance(payload, dict) and "error" in payload


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


def test_tool_id_modes_and_terminal_free(tmp_path):
    assert GetAppTool.tool_id == "get_app"
    assert GetAppTool.modes == frozenset({"act"})
    tool = _tool(*_stores(tmp_path))
    assert tool.should_terminate_run() is False


# ---------------------------------------------------------------------------
# Arg validation
# ---------------------------------------------------------------------------


def test_default_operation_is_get():
    assert GetAppArgs().operation == "get"


def test_extra_field_rejected(tmp_path):
    tool = _tool(*_stores(tmp_path))
    result = _run(tool, {"bogus": 1})
    assert _payload(result)["error"]["code"] == "validation"


def test_invalid_operation_rejected(tmp_path):
    tool = _tool(*_stores(tmp_path))
    result = _run(tool, {"operation": "delete"})
    assert _payload(result)["error"]["code"] == "validation"


# ---------------------------------------------------------------------------
# Scope — owner (pre-submit builder) OR maintainer (post-submit) resolves;
# foreign/unknown => not_found uniformly (no app_id argument to leak).
# ---------------------------------------------------------------------------


def test_no_app_bound_to_session_is_not_found(tmp_path):
    tool = _tool(*_stores(tmp_path))
    result = _run(tool, {"operation": "get"})
    assert _payload(result)["error"]["code"] == "not_found"


def test_resolves_app_by_maintainer_session(tmp_path):
    app_store, data_store, run_store = _stores(tmp_path)
    app_store.save(_app(maintainer=SESSION_ID))
    tool = _tool(app_store, data_store, run_store)
    result = _run(tool, {"operation": "get"})
    assert not _is_error(result)
    assert _payload(result)["app_id"] == APP_ID


def test_resolves_app_by_owner_session_pre_submit(tmp_path):
    # The builder session (owner_session_id) can read back before any maintainer exists.
    app_store, data_store, run_store = _stores(tmp_path)
    app_store.save(_app(owner=SESSION_ID, maintainer=None))
    tool = _tool(app_store, data_store, run_store)
    result = _run(tool, {"operation": "get"})
    assert not _is_error(result)


def test_foreign_session_app_is_not_found_no_leak(tmp_path):
    # A real app owned+maintained by ANOTHER session reads identically to a missing one.
    app_store, data_store, run_store = _stores(tmp_path)
    app_store.save(_app(owner="someone", maintainer="someone-else"))
    tool = _tool(app_store, data_store, run_store)
    result = _run(tool, {"operation": "get"})
    payload = _payload(result)
    assert payload["error"]["code"] == "not_found"
    assert APP_ID not in payload["error"]["message"]


# ---------------------------------------------------------------------------
# get — the live manifest WITHOUT file bodies
# ---------------------------------------------------------------------------


def test_get_returns_manifest_shape_without_file_bodies(tmp_path):
    app_store, data_store, run_store = _stores(tmp_path)
    app_store.save(_app())
    tool = _tool(app_store, data_store, run_store)

    payload = _payload(_run(tool, {"operation": "get"}))

    assert payload["operation"] == "get"
    assert payload["app_id"] == APP_ID
    assert payload["title"] == "App One"
    assert payload["status"] == "live"
    assert payload["policies"]["max_docs_per_collection"] == 1000

    # collections carry name + doc count (no docs yet).
    by_name = {c["name"]: c for c in payload["collections"]}
    assert by_name["emails"]["doc_count"] == 0
    assert by_name["emails"]["count_capped"] is False

    # pipelines carry the declared tier; the code pipeline's stamped trigger_ref
    # reads as trigger_declared, the on-demand one does not. (trigger_declared,
    # NOT armed — /system owns the live-arming signal; they diverge when paused.)
    pipes = {p["name"]: p for p in payload["pipelines"]}
    assert pipes["ingest"]["mode"] == "code"
    assert pipes["ingest"]["schedule"] == {"kind": "time.cron", "cron": "0 7 * * *"}
    assert pipes["ingest"]["trigger_declared"] is True
    assert "armed" not in pipes["ingest"]  # no collision with /system's key
    assert pipes["ingest"]["on_demand"] is False
    assert pipes["ingest"]["user_writable"] is False
    assert pipes["ingest"]["last_run_status"] is None
    assert pipes["ingest"]["freshness_seconds"] is None
    assert pipes["digest"]["mode"] == "agentic"
    assert pipes["digest"]["trigger_declared"] is False
    assert pipes["digest"]["on_demand"] is True

    # files list is path + byte size ONLY — no bodies anywhere in the payload.
    files = {f["path"]: f["bytes"] for f in payload["files"]}
    assert files["app.py"] == len(_FILES["app.py"].encode("utf-8"))
    # the mode="code" pipeline SOURCE is listed (raw stored files, not the
    # browser-served projection), so a maintainer sees it exists.
    assert "pipelines/ingest.py" in files
    serialized = _run(tool, {"operation": "get"}).content
    for body in _FILES.values():
        assert body not in serialized


def test_get_collection_doc_counts_reflect_stored_docs(tmp_path):
    app_store, data_store, run_store = _stores(tmp_path)
    app_store.save(_app())
    data_store.upsert(APP_ID, "emails", "k1", {"subject": "a"})
    data_store.upsert(APP_ID, "emails", "k2", {"subject": "b"})
    tool = _tool(app_store, data_store, run_store)

    payload = _payload(_run(tool, {"operation": "get"}))
    counts = {c["name"]: c["doc_count"] for c in payload["collections"]}
    assert counts == {"emails": 2, "groups": 0}


def test_get_pipeline_freshness_and_last_status_from_ledger(tmp_path):
    app_store, data_store, run_store = _stores(tmp_path)
    app_store.save(_app())
    ended = NOW - timedelta(hours=2)
    run = PipelineRun.open(
        run_key="r1", app_id=APP_ID, pipeline_name="ingest", now=ended - timedelta(minutes=1)
    )
    run.close(now=ended, status="succeeded")
    run_store.save(run)
    tool = _tool(app_store, data_store, run_store)

    pipes = {p["name"]: p for p in _payload(_run(tool, {"operation": "get"}))["pipelines"]}
    assert pipes["ingest"]["last_run_status"] == "succeeded"
    assert pipes["ingest"]["freshness_seconds"] == 7200.0


def test_get_reports_active_and_latest_version_distinctly(tmp_path):
    # A rolled-back app: active version 1, but two snapshots recorded (latest = 2).
    app_store, data_store, run_store = _stores(tmp_path)
    app = _app(version=1)
    app_store.save(app)
    app_store.save_version(AppVersion(app_id=APP_ID, version=1, spec=app, author="builder"))
    app_store.save_version(AppVersion(app_id=APP_ID, version=2, spec=app, author="repair"))
    tool = _tool(app_store, data_store, run_store)

    payload = _payload(_run(tool, {"operation": "get"}))
    assert payload["version"] == 1
    assert payload["latest_version"] == 2


# ---------------------------------------------------------------------------
# stage — re-materialize the FULL stored bundle to disk
# ---------------------------------------------------------------------------


def test_stage_round_trip_is_byte_equal_incl_pipeline_source(tmp_path, monkeypatch):
    monkeypatch.setenv("MEWBO_APPS_ROOT", str(tmp_path / "staging"))
    app_store, data_store, run_store = _stores(tmp_path)
    app_store.save(_app())
    tool = _tool(app_store, data_store, run_store)

    payload = _payload(_run(tool, {"operation": "stage"}))

    assert payload["operation"] == "stage"
    assert payload["app_id"] == APP_ID
    app_dir = tmp_path / "staging" / SESSION_ID / APP_ID
    assert payload["directory"] == str(app_dir)
    # every stored file (frontend AND the mode="code" pipeline source) round-trips.
    staged = {f["path"] for f in payload["files"]}
    assert staged == set(_FILES)
    for rel, body in _FILES.items():
        assert (app_dir / rel).read_text(encoding="utf-8") == body
    # the pipeline SOURCE specifically is present on disk (the maintainer needs it).
    assert (app_dir / "pipelines" / "ingest.py").read_text(encoding="utf-8") == _FILES[
        "pipelines/ingest.py"
    ]


class _InMemoryAppStore:
    """Yields an app verbatim (no serialize round-trip), so a spec whose files map
    was mutated past ``AppFrontend``'s validator still reaches the tool — the real
    Json store re-validates on load and would drop it (itself a defense), so the
    tool's OWN confinement guard is what this exercises."""

    def __init__(self, app: AppSpec) -> None:
        self._app = app

    def list_apps(self, *, include_archived: bool = False) -> list[AppSpec]:
        return [self._app]


def test_stage_confines_writes_and_rejects_an_escaping_path(tmp_path, monkeypatch):
    monkeypatch.setenv("MEWBO_APPS_ROOT", str(tmp_path / "staging"))
    _, data_store, run_store = _stores(tmp_path)
    app = _app()
    # Bypass AppFrontend's traversal validator by mutating the files dict in place
    # (no validate_assignment) — exercises the tool's own confinement guard.
    app.frontend.files["../escape.py"] = "boom"
    tool = _tool(_InMemoryAppStore(app), data_store, run_store)

    result = _run(tool, {"operation": "stage"})

    payload = _payload(result)
    assert payload["error"]["code"] == "stage"
    assert "escape" in payload["error"]["message"].lower()
    # nothing was written outside the app directory.
    assert not (tmp_path / "staging" / SESSION_ID / "escape.py").exists()


def test_stage_rejects_a_hostile_app_id_escaping_the_session_dir(tmp_path, monkeypatch):
    # Defense-in-depth: the model's app_id validator only bans ':', not '/'/'..',
    # so a stored app_id could relocate within the apps root. Mutate app_id past
    # the validator and confirm the tightened session-dir guard refuses it.
    monkeypatch.setenv("MEWBO_APPS_ROOT", str(tmp_path / "staging"))
    _, data_store, run_store = _stores(tmp_path)
    app = _app()
    app.app_id = "../evil"  # no validate_assignment -> bypasses the field validator
    tool = _tool(_InMemoryAppStore(app), data_store, run_store)

    result = _run(tool, {"operation": "stage"})

    payload = _payload(result)
    assert payload["error"]["code"] == "stage"
    assert "session directory" in payload["error"]["message"]
    assert not (tmp_path / "staging" / "evil").exists()


# ---------------------------------------------------------------------------
# Store resolution — factory fallback + clean unavailable degrade
# ---------------------------------------------------------------------------


def test_factory_resolves_stores_when_not_injected(tmp_path):
    app_store, data_store, run_store = _stores(tmp_path)
    app_store.save(_app())
    store_mod.set_stores_for_tests(
        app_store=app_store, data_store=data_store, run_store=run_store
    )
    try:
        tool = GetAppTool(session_id=SESSION_ID, now_fn=lambda: NOW)  # no explicit stores
        result = _run(tool, {"operation": "get"})
        assert not _is_error(result)
        assert _payload(result)["app_id"] == APP_ID
    finally:
        store_mod.set_stores_for_tests(app_store=None, data_store=None, run_store=None)


def test_store_resolution_failure_is_clean_error(monkeypatch):
    def _boom():
        raise RuntimeError("store backend unreachable")

    monkeypatch.setattr("mewbo_api.apps.plugin.get_app.get_app_store", _boom)
    tool = GetAppTool(session_id=SESSION_ID)  # no injected stores -> factory -> raises
    result = _run(tool, {"operation": "get"})
    assert _payload(result)["error"]["code"] == "unavailable"
