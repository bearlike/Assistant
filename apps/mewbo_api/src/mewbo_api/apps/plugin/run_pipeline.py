#!/usr/bin/env python3
"""The ``run_pipeline`` SessionTool — execute one CODE pipeline, on demand.

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
no ``app_id`` argument (there is exactly one app per bound session, so the tool
resolves it by SCOPE alone): the app the SERVER bound this session to —
:meth:`~mewbo_api.apps.staging.AppStagingArea.app_for_session`, the ONE
resolution path ``get_app`` and ``submit_app`` also read — the named pipeline
within it, a refusal for an ``agentic`` pipeline (it runs by being woken via its
``wake_prompt``, never invoked as a tool), and — only then — the registered
:class:`~mewbo_api.apps.plugin.runtime.PipelineRunner`. An unwired runner
degrades to a clean error, never a crash (mirrors ``submit_app``'s "apps runtime
not configured" seam).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from mewbo_core.tooling.session_tools import DEFAULT_SESSION_TOOL_MODES
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mewbo_api.apps.models import (
    PIPELINE_TIMEOUT_CEILING_SECONDS,
    PipelineEvidence,
    PipelineResult,
)
from mewbo_api.apps.plugin.runtime import (
    AppStore,
    PipelineLedger,
    PipelineRunner,
    current_pipeline_ledger,
    current_pipeline_runner,
    session_tags_for,
)
from mewbo_api.apps.staging import AppStagingArea
from mewbo_api.apps.store import get_app_store

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mewbo_core.classes import ActionStep
    from mewbo_core.contracts.types import Event

    from mewbo_api.apps.models import AppSpec, PipelineSpec

logging = get_logger(name="apps.plugin.run_pipeline")

RUN_PIPELINE_TOOL_ID = "run_pipeline"

# Bound the ``output`` text so a runaway pipeline result can't balloon the
# model's context — mirrors the query-limit caps elsewhere in this suite.
_MAX_OUTPUT_CHARS = 4000

# Headroom added to a pipeline's declared ``timeout_seconds`` when declaring this
# tool's outer execution ceiling: the runner also validates params, lints the
# source, resolves the workspace and writes a ledger row, none of which the
# watchdog covers. Generous on purpose — the watchdog is the real bound.
_EXECUTION_TIMEOUT_MARGIN_SECONDS = 30.0

# The widest ceiling any pipeline may actually EXECUTE at —
# :data:`~mewbo_api.apps.models.PIPELINE_TIMEOUT_CEILING_SECONDS`, re-exported as
# a float for the arithmetic below. NOT ``PipelineSpec.timeout_seconds``'s own
# parse bound (``le=600``, deliberately wider — see that field's comment): the
# field stays wide so an already-stored manifest keeps PARSING, while this is
# the number a declared value is CLAMPED to before it ever reaches the watchdog
# (:meth:`RunPipelineTool.execution_timeout` below, and
# ``AppPipelineRunner.execute``'s own clamp). Sharing the one constant is what
# keeps the two clamps from drifting apart — ``tests/apps/test_pipeline_timeout_clamp.py``
# pins the arithmetic against it.
_MAX_PIPELINE_TIMEOUT_SECONDS = float(PIPELINE_TIMEOUT_CEILING_SECONDS)


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

    Satisfies the :class:`~mewbo_core.tooling.session_tools.SessionTool` Protocol via the
    class-shaped ``tool_id``/``schema``/``modes`` attributes plus ``handle`` /
    ``should_terminate_run`` / ``terminal_reason`` (defined explicitly — a
    structural Protocol inherits no bodies). Terminal-free: running a pipeline is
    normal iterative work.
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
        ledger: PipelineLedger | None = None,
        tags_reader: Callable[[str], Sequence[str]] | None = None,
    ) -> None:
        """Bind the owning session + the two collaborators and the tag reader.

        Args:
            session_id: The bound session — the scope boundary. The app is
                resolved as whichever one the SERVER bound this session to (there
                is exactly one; unlike ``app_data`` this tool takes no ``app_id``
                argument, so it can't be pointed at a foreign app).
            event_logger: Accepted for the ``SessionToolRegistry`` manifest
                constructor shape; unused (this tool emits no transcript event).
            app_store: Manifest read store. ``None`` (the plugin path) resolves
                from the process-wide factory (``get_app_store``) at handle time,
                exactly like ``app_data``.
            runner: The code-pipeline executor. ``None`` (the plugin path)
                resolves from the down-only :func:`current_pipeline_runner` seam.
                The runner reads its own clock (the id-keyed seam threads none) —
                see ``AppPipelineRunner.run_pipeline``. Still the executor for a
                ``dry_run`` and the fallback when no ledger is wired.
            ledger: The run-ledger tracker every OTHER execution path already goes
                through. ``None`` (the plugin path) resolves from the down-only
                :func:`current_pipeline_ledger` seam; still ``None`` after that
                degrades to the *runner* path with ``run_key: None``.
            tags_reader: Reads this session's server-stamped tags — the second
                binding tier. Injected rather than reached for, because the
                default (``session_tags_for``) resolves a process-wide session
                store: a test could otherwise never drive the tag tier THROUGH
                this tool, which is why the tier could drift out of three of the
                four app tools without a single test noticing.
        """
        self._session_id = session_id
        self._app_store = app_store
        self._runner = runner
        self._ledger = ledger
        self._tags_reader = tags_reader or session_tags_for

    # -- Protocol surface (defined explicitly — structural Protocol, no inherited bodies) --

    def should_terminate_run(self) -> bool:
        """Never terminates — running a pipeline is normal iterative work."""
        return False

    def terminal_reason(self) -> str:
        """Unused (never terminates); default parity with the Protocol."""
        return "awaiting_approval"

    def execution_timeout(self, tool_input: object) -> float | None:
        """This call's outer ceiling — the PIPELINE's own watchdog plus margin.

        Per the SessionTool declaration law (``mewbo_core/tooling/CLAUDE.md``): an
        undeclared session tool is killed by the loop's flat 120 s
        ``asyncio.wait_for``, so a pipeline declaring the model's own maximum
        ``timeout_seconds`` (600) would have its legitimately long run cut short by
        a ceiling neither it nor the runner knows about. Deriving from
        ``PipelineSpec.timeout_seconds`` keeps the WATCHDOG the only thing that
        ever times a pipeline out — the one bound that also stops the run's writes.

        :data:`_EXECUTION_TIMEOUT_MARGIN_SECONDS` covers the work outside the
        watchdog (params validation, lint, cache fingerprinting, the ledger write).
        Anything unresolvable — a non-dict input, an unknown pipeline, a dead
        store — yields the widest legal ceiling rather than a raise: this hook runs
        BEFORE :meth:`handle` validates, so it must never be the thing that reports
        a bad argument, and a ceiling that is too generous costs nothing (the
        watchdog still fires) while one that is too tight truncates a valid run.

        A resolved pipeline's OWN declared value is clamped to
        :data:`_MAX_PIPELINE_TIMEOUT_SECONDS`: the field itself parses up to 600
        (so an already-stored manifest keeps parsing — see
        ``PipelineSpec.timeout_seconds``), but a value between 241 and 600 must
        never reach the loop's outer wait_for uncapped, or a stored-but-over-
        ceiling pipeline would legitimately outlive the single gunicorn worker.
        Logged once at WARNING when it actually clamps — a silently narrowed
        bound is the fail-open shape this repo forbids, so this declaration-only
        clamp announces itself here too, independent of
        ``AppPipelineRunner.execute``'s own clamp+log at the watchdog itself.
        """
        widest = _MAX_PIPELINE_TIMEOUT_SECONDS + _EXECUTION_TIMEOUT_MARGIN_SECONDS
        try:
            if not isinstance(tool_input, dict):
                return widest
            name = tool_input.get("pipeline")
            if not isinstance(name, str) or not name:
                return widest
            app_store = self._resolve_app_store()
            app = self._resolve_app(app_store) if app_store is not None else None
            pipeline = self._find_pipeline(app, name) if app is not None else None
            if pipeline is None:
                return widest
            declared = min(float(pipeline.timeout_seconds), _MAX_PIPELINE_TIMEOUT_SECONDS)
            if declared != float(pipeline.timeout_seconds):
                logging.warning(
                    "run_pipeline: app {} pipeline {}: declared timeout_seconds={} exceeds "
                    "the {:g}s ceiling; clamping the declared execution_timeout to {:g}s",
                    getattr(app, "app_id", "?"), pipeline.name, pipeline.timeout_seconds,
                    _MAX_PIPELINE_TIMEOUT_SECONDS, declared,
                )
            return declared + _EXECUTION_TIMEOUT_MARGIN_SECONDS
        except Exception as exc:  # noqa: BLE001 - a ceiling must never break the call
            logging.warning("run_pipeline: execution_timeout resolution failed: {}", exc)
            return widest

    # -- dispatch -------------------------------------------------------------

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Validate, resolve the app + pipeline, then execute — through the LEDGER.

        A non-dry invoke goes through :meth:`PipelineLedger.record_code_run`, the
        one home for "run a code pipeline + record its provenance", so the run this
        call performed becomes a readable :class:`PipelineRun` row like every other
        execution path's. Going straight to the runner (as this tool used to) wrote
        no row, so ``get_app``'s ``last_run_status`` still described some earlier
        run and a maintainer asking "did my work land?" read a stale ``succeeded``.
        The arguments follow the ``/fire`` law, not the anti-spam REST-invoke one:
        ``kind="on_request"`` (a model invoke is not a schedule),
        ``dispatch_failure=False`` (a manual fire must never auto-repair or pause
        the app) and ``require_effect=False`` (an explicit fire ALWAYS ledgers).

        **A ``dry_run`` deliberately does NOT ledger** and reports ``run_key: None``:
        it performs no durable write, and ``record_code_run`` has no dry-run mode —
        a row for it would claim a refresh that never happened. It keeps the direct
        runner path, as does a deployment with no ledger wired at all.
        """
        try:
            args = RunPipelineArgs.model_validate(action_step.tool_input or {})
        except ValidationError as exc:
            return self._err("validation", str(exc))

        app_store = self._resolve_app_store()
        if app_store is None:
            return self._err("unavailable", "the apps runtime is not configured")

        app = self._resolve_app(app_store)
        if app is None:
            return self._err(
                "not_found",
                "no app is bound to this session — the server binds an app by its "
                "owner/maintainer session or by a stamped app tag, and this session "
                "carries neither. Call get_app(operation='get') to confirm what this "
                "session can see; if that also reports no app, this session was never "
                "opened against one and no pipeline is invocable from it.",
            )

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

        ledger = self._ledger or self._paired_ledger(runner)
        if ledger is not None and not args.dry_run:
            run, result = ledger.record_code_run(
                app,
                pipeline,
                params=args.params,
                kind="on_request",
                dispatch_failure=False,
                require_effect=False,
            )
            run_key = getattr(run, "run_key", None)
            if result is None:
                code, message = self._bucket(getattr(run, "error", None) or "pipeline failed")
                return self._err(*self._failure(code, message, run_key=run_key), run_key=run_key)
            # The same projection ``AppPipelineRunner.run_pipeline`` performs on its
            # way out of the id-keyed adapter — the ledger seam hands back the
            # richer ``PipelineResult`` instead, so ``_render``'s input is built here.
            outcome = {
                "output": result.output,
                "evaluated_at": result.evaluated_at,
                "docs_written": dict(result.docs_written),
                "evidence": result.evidence.model_dump(mode="json"),
                "cache_hit": result.cache == "hit",
            }
            return self._ok(
                self._render(
                    outcome,
                    pipeline=pipeline,
                    declared_collections=[collection.name for collection in app.collections],
                    dry_run=False,
                    run_key=run_key,
                )
            )

        if ledger is None and not args.dry_run:
            logging.warning(
                "run_pipeline: no pipeline ledger registered — this run will not be "
                "recorded on the app's freshness ledger (session {})",
                self._session_id,
            )
        try:
            outcome = runner.run_pipeline(
                app.app_id, args.pipeline, params=args.params, dry_run=args.dry_run
            )
        except Exception as exc:  # noqa: BLE001 - a pipeline failure is agent-visible feedback
            # Read via ``getattr`` rather than importing PipelineExecutionError —
            # the runner reaches this tool through a Protocol.
            raw_code = getattr(exc, "code", None)
            code = raw_code if isinstance(raw_code, str) and raw_code else "execution"
            return self._err(*self._failure(code, str(exc), run_key=None))

        return self._ok(
            self._render(
                outcome,
                pipeline=pipeline,
                declared_collections=[collection.name for collection in app.collections],
                dry_run=args.dry_run,
                run_key=None,
            )
        )

    @staticmethod
    def _paired_ledger(runner: PipelineRunner) -> PipelineLedger | None:
        """The process-wide ledger, but ONLY when it executes through *runner*.

        A ledger runs the pipeline through the runner IT holds, so using one whose
        runner differs from the one this call resolved would silently execute a
        DIFFERENT executor and discard the resolved one — an explicitly injected
        runner, or a separately re-registered one, would simply stop being called
        with nothing failing. ``init_apps`` registers the runner and then builds the
        tracker around that same instance, so the production pair matches by
        construction and this predicate is transparent there.
        """
        ledger = current_pipeline_ledger()
        if ledger is None:
            return None
        if getattr(ledger, "pipeline_runner", None) is runner:
            return ledger
        logging.warning(
            "run_pipeline: the registered ledger executes through a different runner "
            "than this call resolved — running unledgered (run_key will be None)"
        )
        return None

    # -- failure rendering ----------------------------------------------------

    @staticmethod
    def _bucket(error: str) -> tuple[str, str]:
        """Recover ``(code, message)`` from a ledger row's ``error`` text.

        ``PipelineRun.error`` is ``str(PipelineExecutionError)``, i.e. a clean
        ``"<code>: <message>"``, so the TYPED bucket survives the trip through the
        row — the ledger seam reports a failure by returning ``result is None``
        rather than by raising, so there is no exception left to read ``code`` off.
        A row whose error does not carry a single-token head is not a bucketed
        failure and reads ``execution``, exactly as an un-bucketed raise does.
        """
        head, sep, _rest = error.partition(": ")
        if sep and head and not any(ch.isspace() for ch in head):
            return head, error
        return "execution", error

    @staticmethod
    def _failure(code: str, message: str, *, run_key: str | None) -> tuple[str, str]:
        """Shape a bucketed failure into ``(code, message)`` — the TYPED bucket, kept.

        ``PipelineExecutionError`` already buckets the failure
        (``params``/``lint``/``timeout``/``cap``/``runtime``/…) precisely so a
        caller can act on it; collapsing every one of them into ``execution``
        threw that away and left the model guessing whether to fix its code, its
        params, or nothing at all.

        ``timeout`` additionally names what to do next — mirroring
        ``CollectionCapExceeded``'s "delete a doc or raise the cap" refusal
        (``store.py``). A timed-out run may have written documents BEFORE its
        deadline, so re-running blind is the wrong next move; querying is the
        cheap way to find out what actually landed. When the run was ledgered it
        also names the ``run_key``, since the row recording those partial writes
        is finally readable rather than merely referred to.
        """
        if code != "timeout":
            return code, message
        recorded = f" as run_key={run_key!r}" if run_key else ""
        return code, (
            f"{message} — documents written BEFORE the deadline have landed and are "
            f"recorded on the run{recorded}; writes attempted after it were refused. "
            f"Check what is actually there with app_data(operation='query', "
            f"collection=...) before re-running, then make the pipeline do less "
            f"work per call or raise its timeout_seconds."
        )

    # -- resolution helpers ---------------------------------------------------

    def _resolve_app(self, app_store: AppStore) -> AppSpec | None:
        """The app the SERVER bound this session to, or ``None``.

        Delegates to :meth:`~mewbo_api.apps.staging.AppStagingArea.app_for_session`
        — the two id FIELDS (``maintainer_session_id`` / ``owner_session_id``)
        first, then the server-stamped ``app:<id>`` TAG — so this tool, ``get_app``
        and ``submit_app`` give ONE answer about which app a session may act on.

        Re-deriving the id-field half here was the bug: a tag-bound composer
        session (``AppLifecycle.open_session(fresh=True)``, which is what the
        console mints for a turn against an existing app) resolved for ``get_app``
        and for ``submit_app`` — i.e. it could REPLACE the live app with a new
        version — while this tool told it no app was bound. The safest operation
        in the suite was refused to a session already trusted with the most
        dangerous one, which left a maintainer able to read the source and ship a
        guess but never to dry-run it.
        """
        return AppStagingArea(session_id=self._session_id).app_for_session(
            app_store, session_tags=self._tags_reader(self._session_id)
        )

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
        outcome: dict[str, Any],
        *,
        pipeline: PipelineSpec,
        declared_collections: Sequence[str],
        dry_run: bool,
        run_key: str | None,
    ) -> dict[str, Any]:
        """Shape a bounded pipeline outcome into the agent-facing result envelope.

        ``outcome`` is exactly ``AppPipelineRunner.run_pipeline``'s return shape:
        ``{output, evaluated_at, docs_written, evidence, cache_hit}``. ``output``
        and ``evidence`` are already bounded independently — output by this tool,
        evidence by ``PipelineEvidence`` at the runner — so a diagnostic result
        cannot balloon model context. ``cache`` folds the runner's per-call
        ``cache_hit`` together with the pipeline's declared ``cache_ttl_seconds``.

        A materializing pipeline that misses its explicit ``writes`` contract stays
        execution-successful (the integrity verifier owns health policy), but gets
        a terse, direct diagnostic: which expected collections did not materialize
        and what action separates the usual causes. This is the feedback loop that
        lets the model correct a no-write pipeline before it resubmits it.

        ``run_key`` names the :class:`PipelineRun` row this invoke wrote, so the
        caller can read the run back directly instead of inferring whether its work
        landed from a manifest's ``last_run_status`` — which describes whatever ran
        LAST, frequently a run from long before this call. ``None`` means no row was
        written: a ``dry_run`` (no durable write to record) or a deployment with no
        ledger wired.
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

        docs_written = outcome.get("docs_written") or {}
        evidence = outcome.get("evidence") or {
            "globs": [],
            "read_paths": [],
            "truncated": False,
        }
        # The runner hands this tool a dict via its Protocol. Rebuild the model
        # here rather than trusting a custom/fake runner's arbitrary mapping: the
        # result is model-facing and its evidence must stay structurally bounded.
        result = PipelineResult(
            output=raw_output,
            evaluated_at=(
                evaluated_at
                if isinstance(evaluated_at, datetime)
                else datetime.now(timezone.utc)
            ),
            cache="hit" if bool(outcome.get("cache_hit", False)) else "miss",
            docs_written=docs_written,
            evidence=PipelineEvidence.model_validate(evidence),
        )
        unwritten = result.unwritten_collections(declared_collections)
        missing_expected = result.missing_expected_writes(pipeline.writes)
        attention: dict[str, object] | None = None
        if pipeline.expects_writes() and missing_expected:
            attention = {
                "missing_expected_writes": missing_expected,
                "next_step": (
                    "Inspect evidence.globs for zero matches, then check the source "
                    "filter or collection name before changing the pipeline."
                ),
            }
        # Every glob matching nothing is a DIFFERENT failure from a filter that
        # excluded every row, and it has one overwhelmingly common cause worth
        # naming outright: `ctx` is scoped to the workspace, while the app's
        # BUNDLE files (what `get_app`/`stage` puts on disk, and what a pipeline
        # author is usually looking at while writing the glob) live elsewhere. A
        # local replay against the staged bundle then succeeds while production
        # reads an unrelated, frequently empty directory — a divergence that is
        # invisible in the pipeline source, so the tool has to say it.
        globs = result.evidence.globs
        if globs and not any(glob.match_count for glob in globs):
            attention = dict(attention or {})
            attention["all_globs_matched_nothing"] = True
            attention["workspace"] = result.evidence.workspace
            attention["next_step"] = (
                "Every glob matched zero files. `ctx.glob`/`ctx.read_file` resolve "
                "under `evidence.workspace` — NOT the app bundle that "
                "get_app(operation='stage') writes to disk. A file you can see in "
                "the staged bundle is not visible here unless something puts it in "
                "the workspace. Confirm which directory actually holds the inputs "
                "before editing the pipeline."
            )

        return {
            "pipeline": pipeline.name,
            "output": output_text,
            "output_truncated": truncated,
            "evaluated_at": evaluated_at_str,
            "docs_written": dict(result.docs_written),
            "unwritten_collections": unwritten,
            "evidence": result.evidence.model_dump(mode="json"),
            "attention": attention,
            "cache": {
                "hit": result.cache == "hit",
                "ttl_seconds": pipeline.cache_ttl_seconds,
            },
            "dry_run": dry_run,
            "run_key": run_key,
        }

    @staticmethod
    def _err(code: str, message: str, *, run_key: str | None = None) -> MockSpeaker:
        """Structured-error envelope (``str({"error": {...}})``) — the shared shape.

        ``str(dict)`` not ``json.dumps`` — the loop's envelope parser
        (``_session_tool_error_envelope``) is ``ast.literal_eval``, which reads
        Python repr, not JSON literals. Mirrors ``AppDataTool._err`` verbatim.

        ``run_key`` is added ONLY when the failed run was ledgered, so the model can
        read that row back — a timed-out run's partial writes are recorded on it.
        The extra key is additive by construction: ``_SessionToolError.parse`` reads
        ``error.code`` / ``error.message`` / ``error.permanence`` off the parsed dict
        and ignores every other key, so an envelope carrying it still reclassifies
        the step as a failure exactly as before.
        """
        error: dict[str, str] = {"code": code, "message": message}
        if run_key:
            error["run_key"] = run_key
        return MockSpeaker(content=str({"error": error}))

    @staticmethod
    def _ok(payload: dict[str, object]) -> MockSpeaker:
        """Successful structured payload — same Python-repr shape as :meth:`_err`."""
        return MockSpeaker(content=str(payload))


__all__ = [
    "RUN_PIPELINE_SCHEMA",
    "RunPipelineArgs",
    "RunPipelineTool",
]
