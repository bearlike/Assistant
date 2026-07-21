#!/usr/bin/env python3
"""The ``run_pipeline`` SessionTool — execute one CODE pipeline, on demand (Phase 2).

This is the "test your pipeline" phase made EXECUTABLE: a builder or maintainer
writes a ``pipelines/<name>.py`` file (``def run(params: dict, ctx) -> Any``),
then calls ``run_pipeline(pipeline=..., dry_run=True)`` to exercise it against
the real workspace-scoped ``ctx`` before ever wiring a schedule to it — schemas
enforce what prose instructs (spec), and this tool is that enforcement made
callable. It also serves the maintainer's own manual re-invocation of a
declared pipeline outside its schedule.

Unlike ``submit_app`` this tool is **terminal-free** (the ``app_data`` /
``update_todos`` shape): invoking a pipeline is normal iterative work, never a
run exit — a builder calls it, inspects ``docs_written``/``output``, fixes the
code, and calls it again, all within the same build turn.

Resolution mirrors ``app_data``'s three guarantees, adapted to a session with
no ``app_id`` argument (there is exactly one app per maintainer/builder
session, so the tool resolves it by SCOPE alone — the same query
``AppPipelineRunTracker._app_for_session`` already uses at the trigger-fire
seam): the app whose ``maintainer_session_id`` OR ``owner_session_id`` equals
this session, the named pipeline within it, a refusal for an ``agentic``
pipeline (it runs by being woken via its ``wake_prompt``, never invoked as a
tool), and — only then — the registered :class:`~mewbo_api.apps.plugin.runtime.PipelineRunner`.
An unwired runner degrades to a clean error, never a crash (mirrors
``submit_app``'s "apps runtime not configured" seam).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from mewbo_core.session_tools import DEFAULT_SESSION_TOOL_MODES
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mewbo_api.apps.plugin.runtime import (
    AppStore,
    PipelineRunner,
    current_pipeline_runner,
)
from mewbo_api.apps.store import get_app_store

if TYPE_CHECKING:
    from collections.abc import Callable

    from mewbo_core.classes import ActionStep
    from mewbo_core.types import Event

    from mewbo_api.apps.models import AppSpec, PipelineSpec

logging = get_logger(name="apps.plugin.run_pipeline")

RUN_PIPELINE_TOOL_ID = "run_pipeline"

# Bound the ``output`` text so a runaway pipeline result can't balloon the
# model's context — mirrors the query-limit caps elsewhere in this suite.
_MAX_OUTPUT_CHARS = 4000


# ------------------------------------------------------------------
# Tool args
# ------------------------------------------------------------------


class RunPipelineArgs(BaseModel):
    """Execute one of your app's declared CODE pipelines and inspect the result.

    Use `dry_run=true` while developing a pipeline: it exercises the SAME code
    path (schema validation, file resolution, `ctx` construction) WITHOUT any
    durable write, so you can see `docs_written`/`output` before it can affect
    live data. Only `mode="code"` pipelines are executable this way — an
    `agentic` pipeline runs by being woken via its `wake_prompt`, never through
    this tool.
    """

    model_config = ConfigDict(extra="forbid")

    pipeline: str = Field(description="The name of one of your app's declared pipelines.")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Arguments passed to the pipeline's `run(params, ctx)`, validated "
            "against its `params_schema` if declared."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "True to exercise the pipeline without any durable collection "
            "write — inspect before you ship."
        ),
    )


RUN_PIPELINE_SCHEMA: dict[str, object] = pydantic_to_openai_tool(
    RunPipelineArgs, name=RUN_PIPELINE_TOOL_ID
)


# ------------------------------------------------------------------
# The SessionTool
# ------------------------------------------------------------------


class RunPipelineTool:
    """Handles ``run_pipeline`` — resolve the app + pipeline, execute via the registered runner.

    Satisfies the :class:`~mewbo_core.session_tools.SessionTool` Protocol via the
    class-shaped ``tool_id``/``schema``/``modes`` attributes plus ``handle`` /
    ``should_terminate_run`` / ``terminal_reason`` (defined explicitly — structural
    Protocol, no inherited bodies; see the ``submit_widget`` post-mortem in core's
    CLAUDE.md). Terminal-free: running a pipeline is normal iterative work.
    """

    tool_id: str = RUN_PIPELINE_TOOL_ID
    schema: dict[str, object] = RUN_PIPELINE_SCHEMA
    modes: frozenset[str] = DEFAULT_SESSION_TOOL_MODES

    def __init__(
        self,
        *,
        session_id: str,
        event_logger: Callable[[Event], None] | None = None,
        app_store: AppStore | None = None,
        runner: PipelineRunner | None = None,
    ) -> None:
        """Bind the owning session + the two collaborators.

        Args:
            session_id: The builder/maintainer session — the scope boundary. The
                app is resolved as whichever one this session owns or maintains
                (there is exactly one; unlike ``app_data`` this tool takes no
                ``app_id`` argument, so it can't be pointed at a foreign app).
            event_logger: Accepted for the ``SessionToolRegistry`` manifest
                constructor shape; unused (this tool emits no transcript event).
            app_store: Manifest read store. ``None`` (the plugin path) resolves
                from the process-wide factory (``get_app_store``) at handle time,
                exactly like ``app_data``.
            runner: The code-pipeline executor. ``None`` (the plugin path)
                resolves from the down-only :func:`current_pipeline_runner` seam.
                The runner reads its own clock (the id-keyed seam threads none) —
                see ``AppPipelineRunner.run_pipeline``.
        """
        self._session_id = session_id
        self._app_store = app_store
        self._runner = runner

    # -- Protocol surface (defined explicitly — structural Protocol, no inherited bodies) --

    def should_terminate_run(self) -> bool:
        """Never terminates — running a pipeline is normal iterative work."""
        return False

    def terminal_reason(self) -> str:
        """Unused (never terminates); default parity with the Protocol."""
        return "awaiting_approval"

    # -- dispatch -------------------------------------------------------------

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Validate, resolve the app + pipeline, then execute via the runner."""
        try:
            args = RunPipelineArgs.model_validate(action_step.tool_input or {})
        except ValidationError as exc:
            return self._err("validation", str(exc))

        app_store = self._resolve_app_store()
        if app_store is None:
            return self._err("unavailable", "the apps runtime is not configured")

        app = self._resolve_app(app_store)
        if app is None:
            return self._err("not_found", "no app is bound to this session")

        pipeline = self._find_pipeline(app, args.pipeline)
        if pipeline is None:
            declared = [p.name for p in app.pipelines]
            return self._err(
                "validation", f"unknown pipeline {args.pipeline!r}; declared: {declared}"
            )

        if pipeline.mode != "code":
            return self._err(
                "not_executable",
                f"pipeline {args.pipeline!r} is agentic (mode={pipeline.mode!r}) — "
                f"run_pipeline only executes code pipelines. An agentic pipeline runs "
                f"by being woken via its wake_prompt, not by calling a tool.",
            )

        runner = self._runner or current_pipeline_runner()
        if runner is None:
            logging.error(
                "run_pipeline: no pipeline runner registered for session {}", self._session_id
            )
            return self._err("unavailable", "pipeline execution not configured")

        try:
            outcome = runner.run_pipeline(
                app.app_id, args.pipeline, params=args.params, dry_run=args.dry_run
            )
        except Exception as exc:  # noqa: BLE001 - a pipeline failure is agent-visible feedback
            return self._err("execution", str(exc))

        return self._ok(self._render(outcome, pipeline=pipeline, dry_run=args.dry_run))

    # -- resolution helpers ---------------------------------------------------

    def _resolve_app(self, app_store: AppStore) -> AppSpec | None:
        """The app this session owns or maintains.

        Mirrors ``AppPipelineRunTracker._app_for_session``. ``run_pipeline`` has
        no ``app_id`` argument, so scope is derived purely from the session:
        the builder session (pre-submit, ``owner_session_id``) or the
        maintainer session (post-submit re-invocation, ``maintainer_session_id``)
        — either binds this session to exactly one app.
        """
        for app in app_store.list_apps(include_archived=True):
            if self._session_id in (app.maintainer_session_id, app.owner_session_id):
                return app
        return None

    @staticmethod
    def _find_pipeline(app: AppSpec, name: str) -> PipelineSpec | None:
        return next((p for p in app.pipelines if p.name == name), None)

    def _resolve_app_store(self) -> AppStore | None:
        """Explicit constructor injection wins; else the process-wide store factory."""
        if self._app_store is not None:
            return self._app_store
        try:
            return get_app_store()
        except Exception as exc:  # noqa: BLE001 - a store-init failure must not crash the agent
            logging.error("run_pipeline: store resolution failed: {}", exc)
            return None

    # -- rendering --------------------------------------------------------------

    @staticmethod
    def _render(
        outcome: dict[str, Any], *, pipeline: PipelineSpec, dry_run: bool
    ) -> dict[str, Any]:
        """Shape the runner's outcome dict into the agent-facing result envelope.

        ``outcome`` is exactly ``AppPipelineRunner.run_pipeline``'s return shape:
        ``{output, evaluated_at, docs_written, cache_hit}``. ``output`` is
        serialized to text and truncated to :data:`_MAX_OUTPUT_CHARS` with a note
        — a runaway pipeline result can't balloon the model's context. ``cache``
        folds the runner's per-call ``cache_hit`` together with the pipeline's
        declared ``cache_ttl_seconds`` (data this tool already has from the
        resolved spec — the runner doesn't need to report it back).
        """
        raw_output = outcome.get("output")
        try:
            output_text = (
                raw_output if isinstance(raw_output, str) else json.dumps(raw_output, default=str)
            )
        except TypeError:
            output_text = str(raw_output)
        truncated = len(output_text) > _MAX_OUTPUT_CHARS
        if truncated:
            output_text = output_text[:_MAX_OUTPUT_CHARS]

        evaluated_at = outcome.get("evaluated_at")
        evaluated_at_str = (
            evaluated_at.isoformat() if isinstance(evaluated_at, datetime) else evaluated_at
        )

        return {
            "pipeline": pipeline.name,
            "output": output_text,
            "output_truncated": truncated,
            "evaluated_at": evaluated_at_str,
            "docs_written": outcome.get("docs_written") or {},
            "cache": {
                "hit": bool(outcome.get("cache_hit", False)),
                "ttl_seconds": pipeline.cache_ttl_seconds,
            },
            "dry_run": dry_run,
        }

    @staticmethod
    def _err(code: str, message: str) -> MockSpeaker:
        """Structured-error envelope (``str({"error": {...}})``) — the shared shape.

        ``str(dict)`` not ``json.dumps`` — the loop's envelope parser
        (``_session_tool_error_envelope``) is ``ast.literal_eval``, which reads
        Python repr, not JSON literals. Mirrors ``AppDataTool._err`` verbatim.
        """
        return MockSpeaker(content=str({"error": {"code": code, "message": message}}))

    @staticmethod
    def _ok(payload: dict[str, object]) -> MockSpeaker:
        """Successful structured payload — same Python-repr shape as :meth:`_err`."""
        return MockSpeaker(content=str(payload))


__all__ = [
    "RUN_PIPELINE_SCHEMA",
    "RunPipelineArgs",
    "RunPipelineTool",
]
