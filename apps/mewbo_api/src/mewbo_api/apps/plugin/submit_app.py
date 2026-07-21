#!/usr/bin/env python3
"""The ``submit_app`` SessionTool — the app-builder's terminal, validated submit.

The builder sub-agent designs an app's data model + frontend + pipelines under
``${MEWBO_APPS_ROOT:-/tmp/mewbo/apps}/${SESSION_ID}/<app_id>/`` and then calls
``submit_app`` with the manifest METADATA (title, workspace, collections,
pipelines, policies) — NOT the frontend file contents, which the tool reads off
disk exactly like ``submit_widget`` reads ``app.py``/``data.json``. This keeps a
multi-file bundle out of the tool arguments and off the model's context.

``handle()`` is a validate-or-reask loop bounded by :data:`_MAX_SUBMIT_FAILURES`
(the ``EmitStructuredResponseTool`` pattern): a malformed argument, a missing
entrypoint, a lint finding, or a lifecycle rejection each comes back as a
"fix these and resubmit" tool result the builder can act on; the run only
TERMINATES on a clean submit (``terminal_reason() == "completed"``) or once the
failure budget is spent.

TRAP (the submit_widget post-mortem, `packages/mewbo_core/CLAUDE.md`):
``SessionTool`` is a STRUCTURAL Protocol — a standalone class inherits NO default
method bodies, so ``should_terminate_run`` / ``terminal_reason`` are defined
explicitly below. This tool DOES terminate (on success), unlike ``submit_widget``
which is terminal-free.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from croniter import croniter  # type: ignore[import-untyped]  # no stubs published
from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from mewbo_core.session_tools import DEFAULT_SESSION_TOOL_MODES
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from mewbo_api.apps.models import (
    PIPELINE_ALLOWED_EXEC,
    AppFrontend,
    AppPolicies,
    AppSpec,
    CollectionSpec,
    WorkspaceRef,
)
from mewbo_api.apps.pipeline_runner import lint_pipeline
from mewbo_api.apps.plugin.linter import ALLOWED_MODULES, format_findings, lint_app
from mewbo_api.apps.plugin.runtime import AppSubmitter, current_app_submitter
from mewbo_api.apps.store import get_app_store

if TYPE_CHECKING:
    from collections.abc import Callable

    from mewbo_core.classes import ActionStep
    from mewbo_core.types import Event

logging = get_logger(name="apps.plugin.submit_app")

SUBMIT_APP_TOOL_ID = "submit_app"

# The builder gets this many "not ready" tool results (bad args / missing files /
# lint findings / a lifecycle rejection) before the run gives up. Higher than the
# structured-response default (3): a multi-file app legitimately needs a few
# iterations to satisfy the schema + the per-file lint gate.
_MAX_SUBMIT_FAILURES = 6

# Bundle caps — a frontend map is read off disk, so bound it (a runaway write
# can't balloon the AppSpec or the persisted manifest).
_MAX_FILES = 100
_MAX_TOTAL_BYTES = 2 * 1024 * 1024
# Injected at RENDER time by the console/host, never authored by the builder —
# skip it if it is on disk so a builder can't smuggle a token file into the spec.
_INJECTED_CONTEXT_FILE = "_app_context.json"
_SKIP_DIRS = frozenset({"__pycache__", ".git"})

_DEFAULT_APPS_ROOT = "/tmp/mewbo/apps"


def _apps_root() -> str:
    """Base directory for builder app workspaces (env-driven, no config coupling).

    ``MEWBO_APPS_ROOT`` with a ``/tmp/mewbo/apps`` fallback — the AgentDef prompt
    uses the same ``${MEWBO_APPS_ROOT:-/tmp/mewbo/apps}`` pattern so writer and
    reader agree without config plumbing. Empty string is treated as unset (avoid
    ``Path("")`` resolving to CWD), mirroring ``submit_widget``'s ``_widget_root``.
    """
    return os.environ.get("MEWBO_APPS_ROOT") or _DEFAULT_APPS_ROOT


# ------------------------------------------------------------------
# Tool args — Pydantic IS the schema + the validator (spec §3 AppSpec draft)
# ------------------------------------------------------------------


class PipelineSchedule(BaseModel):
    """A platform-armed wake for a pipeline — declare it, the platform arms it.

    You never call ``schedule_trigger`` for an app pipeline; that arming
    happens on the maintainer session once the app goes live (Phase 1 —
    the builder that hand-armed its own trigger and shipped a null
    `trigger_ref` when its session died mid-build is exactly the failure this
    replaces). `kind="time.cron"` needs `cron` (a 5-field cron expression,
    evaluated in UTC); `kind="time.at"` needs `at` (a tz-aware ISO-8601
    instant — a bare date/time with no UTC offset is rejected).
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["time.cron", "time.at"] = Field(
        description="Which schedule shape follows — determines which field below is required."
    )
    cron: str | None = Field(
        default=None,
        description=(
            '`kind=time.cron`: a 5-field cron expression, e.g. "0 7 * * *" (daily 07:00 UTC).'
        ),
    )
    at: datetime | None = Field(
        default=None,
        description=(
            '`kind=time.at`: a tz-aware ISO-8601 instant, e.g. "2026-07-20T09:00:00Z" '
            "(a one-shot wake — fires once, then the trigger completes)."
        ),
    )

    @model_validator(mode="after")
    def _check_kind_fields(self) -> PipelineSchedule:
        if self.kind == "time.cron":
            if not self.cron:
                raise ValueError("kind=time.cron requires `cron`")
            if not croniter.is_valid(self.cron):
                raise ValueError(f"invalid cron expression: {self.cron!r}")
        elif self.at is None:
            raise ValueError("kind=time.at requires `at`")
        elif self.at.tzinfo is None or self.at.utcoffset() is None:
            raise ValueError(
                "`at` must include a timezone offset (e.g. Z or +05:30), "
                f"got {self.at.isoformat()!r}"
            )
        return self


class SubmitPipelineArgs(BaseModel):
    """One data pipeline: a wake prompt, and how it gets invoked.

    Declare a `schedule` for periodic/one-shot refresh — the PLATFORM arms it
    at submit time, re-homed onto the maintainer session; you never arm a
    pipeline's trigger yourself. Set `on_demand: true` only when a pipeline
    should have NO armed wake at all (it runs solely from a repair or a
    future manual re-invocation). At least one of the two is required —
    submit_app refuses a pipeline with neither, since it would never run.

    `mode="code"` (Phase 2) is the DEFAULT choice for a deterministic
    transform (file parsing, CSV ingestion, filtering) — the platform EXECUTES
    your `entrypoint` file directly via `run_pipeline`/the schedule, no LLM call
    involved. Reserve `mode="agentic"` (the default, for backwards compatibility)
    for flows that genuinely need judgment; a deterministic pipeline running
    agentically burns a full LLM turn for work a function could do.

    Cron example (daily refresh, code pipeline):
        {"name": "morning-organize", "wake_prompt": "Ingest new emails.",
         "mode": "code", "entrypoint": "pipelines/morning_organize.py",
         "schedule": {"kind": "time.cron", "cron": "0 7 * * *"}}

    On-demand example (manual only, no schedule, agentic):
        {"name": "reindex", "wake_prompt": "Rebuild the search index from ...",
         "on_demand": true, "tools_allowlist": ["app_data"]}
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    wake_prompt: str
    schedule: PipelineSchedule | None = Field(
        default=None,
        description=(
            "Periodic/one-shot wake the PLATFORM arms at submit — you never "
            "arm this yourself with schedule_trigger. Leave null only when "
            "`on_demand: true`."
        ),
    )
    on_demand: bool = Field(
        default=False,
        description=(
            "True ONLY when this pipeline should run purely on manual/repair "
            "re-invocation, with no armed schedule. Prefer a `schedule` for any "
            "pipeline that should keep the app's data fresh on its own."
        ),
    )
    tools_allowlist: list[str] = Field(
        default_factory=list,
        description=(
            "`mode=agentic` only: tool ids this pipeline may use unattended — "
            "least privilege (e.g. ['app_data'])."
        ),
    )
    cursor: dict[str, Any] = Field(
        default_factory=dict,
        description="Opaque incremental-fetch bookmark this pipeline may read/write across runs.",
    )
    mode: Literal["agentic", "code"] = Field(
        default="agentic",
        description=(
            "`code` (prefer this for deterministic transforms): the platform "
            "EXECUTES `entrypoint` directly, no LLM call. `agentic` (default): "
            "your maintainer session acts on `wake_prompt` when woken — reserve "
            "for flows that need judgment."
        ),
    )
    entrypoint: str | None = Field(
        default=None,
        description=(
            "`mode=code` only, REQUIRED: bundle-relative path to the pipeline's "
            "Python file (e.g. 'pipelines/refresh.py'), defining "
            "`def run(params: dict, ctx) -> Any`. Must exist among your submitted files."
        ),
    )
    params_schema: dict[str, Any] | None = Field(
        default=None,
        description=(
            "`mode=code` only (optional): a JSON Schema `run_pipeline`'s `params` "
            "argument is validated against before your code runs."
        ),
    )
    cache_ttl_seconds: int = Field(
        default=0,
        ge=0,
        description=(
            "`mode=code` only: seconds a successful result may be served from "
            "cache instead of re-executing. 0 (default) = never cache."
        ),
    )
    timeout_seconds: int = Field(
        default=10,
        ge=1,
        le=600,
        description=(
            "`mode=code` only: wall-clock seconds ONE run of `entrypoint` may take "
            "before the watchdog stops it. A deterministic transform finishes well "
            "under the 10 default; raise it (e.g. 120-300) for a pipeline that calls "
            "`ctx.llm`, so the model round-trip(s) have headroom."
        ),
    )
    user_writable: bool = Field(
        default=False,
        description=(
            "`mode=code` only: set true when this pipeline's `params` are USER "
            "INPUT the frontend submits (a form). It makes the app request a "
            "write-scoped render token so the served frontend can call "
            "`app.pipelines.submit(name, params)` — its params flow through this "
            "pipeline's `params_schema` validation into your `ctx.collection` "
            "writes. Leave false for a pipeline only the schedule/maintainer "
            "invokes: this flag gates ONLY the POST/form write path — a code "
            "pipeline stays GET-invocable read-only by the served app regardless, "
            "so `false` means 'the browser may not POST params to it', not "
            "'unreachable'."
        ),
    )
    allow_exec: list[str] = Field(
        default_factory=list,
        description=(
            "`mode=code` only, OPT-IN (default empty = no subprocess at all): "
            f"platform-vetted binaries {sorted(PIPELINE_ALLOWED_EXEC)} this pipeline's "
            "`ctx.exec(argv)` may run — a CLI-plumbed sync (git log/diff, tea "
            "issues/PRs) belongs HERE as mode='code', not as mode='agentic'. "
            "Declare only what you actually call; `ctx.exec` refuses any other "
            "binary at runtime even if it were somehow importable."
        ),
    )
    allow_egress: list[str] = Field(
        default_factory=list,
        description=(
            "`mode=code` only: bare hostnames (no scheme, e.g. "
            "'git.example.com', 'github.com') `ctx.exec` may reach — checked "
            "against every URL/scp-shaped argv token. A host-less invocation "
            "(e.g. `git log` inside an already-cloned workspace) needs no entry."
        ),
    )

    @field_validator("entrypoint")
    @classmethod
    def _entrypoint_is_relative(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parts = value.replace("\\", "/").split("/")
        if not value or value.startswith("/") or value.startswith("\\") or any(
            p in ("", ".", "..") for p in parts
        ):
            raise ValueError(
                f"entrypoint must be a relative path with no '.'/'..' segments, got {value!r}"
            )
        return value

    # No separate `params_schema` validator: the `dict[str, Any] | None` type
    # annotation ALREADY enforces "a dict if present" at pydantic's own field
    # level — a `field_validator` for that check would be unreachable dead
    # code, since pydantic never invokes it once the underlying type coercion
    # (not a dict) has already failed.

    @model_validator(mode="after")
    def _check_schedule_or_on_demand(self) -> SubmitPipelineArgs:
        if self.schedule is None and not self.on_demand:
            raise ValueError(
                f"pipeline '{self.name}' needs either a `schedule` (time.cron/time.at) "
                f"or `on_demand: true` — a pipeline with neither would never run"
            )
        return self

    @model_validator(mode="after")
    def _check_entrypoint_required_iff_code(self) -> SubmitPipelineArgs:
        if self.mode == "code" and not self.entrypoint:
            raise ValueError(
                f"pipeline '{self.name}' has mode='code' but no `entrypoint` — "
                f"set it to the pipeline's Python file"
            )
        if self.mode == "agentic" and self.entrypoint is not None:
            raise ValueError(
                f"pipeline '{self.name}' has mode='agentic' but sets `entrypoint` — "
                f"entrypoint only applies to mode='code'"
            )
        return self

    @model_validator(mode="after")
    def _check_user_writable_requires_code(self) -> SubmitPipelineArgs:
        # user_writable declares that the frontend may SUBMIT this pipeline's
        # params through the write-scoped POST path — only a mode="code" pipeline
        # executes synchronously on an invoke, so a user-writable agentic pipeline
        # would be a form the platform can never run.
        if self.user_writable and self.mode != "code":
            raise ValueError(
                f"pipeline '{self.name}' sets `user_writable` but is not mode='code' — "
                f"only a code pipeline can accept user-submitted params (set mode='code')"
            )
        return self


class SubmitAppArgs(BaseModel):
    """Submit a finished app for the platform to persist and bring live.

    Call this ONCE, after you have written every frontend file under your app
    directory and designed the data model. Pass the app's METADATA here — the
    frontend file CONTENTS are read from disk, so do NOT put them in arguments.
    On success the app goes live and this run ends; on a validation or lint
    failure you get the specific problem back and can fix it and resubmit.
    """

    model_config = ConfigDict(extra="forbid")

    app_id: str = Field(
        description=(
            "Your app's id — the SAME slug you named its directory "
            "(`${MEWBO_APPS_ROOT}/${SESSION_ID}/<app_id>/`). No path separators."
        ),
    )
    title: str = Field(description="Human-facing app name shown in the gallery + chrome.")
    summary: str = Field(
        default="",
        description="One-line description of what the app does.",
    )
    icon: str = Field(
        default="🧩",
        description="A single emoji used as the app's glyph.",
    )
    workspace_ref: WorkspaceRef = Field(
        description=(
            "Which workspace the app's agents anchor to — `kind` 'own' (a private "
            "workspace for this app) or 'shared' (an existing one), plus its `key`. "
            "Use the workspace choice you were handed."
        ),
    )
    entrypoint: str = Field(
        default="app.py",
        description="The frontend file stlite mounts first — must exist in your app directory.",
    )
    requirements: list[str] = Field(
        default_factory=list,
        description="Pure-Python packages the frontend imports (e.g. ['pandas']).",
    )
    collections: list[CollectionSpec] = Field(
        default_factory=list,
        description=(
            "The app's data collections — each a name + a JSON Schema every stored "
            "document is validated against. Design these FIRST; pipelines write into "
            "them and the frontend reads them via the SDK."
        ),
    )
    pipelines: list[SubmitPipelineArgs] = Field(
        default_factory=list,
        description=(
            "The data pipelines that keep the app fresh — each a name, a `wake_prompt` "
            "(what you'll be told to do when it fires), a `schedule` the PLATFORM arms "
            "for you (or `on_demand: true` for no schedule), and a `tools_allowlist` "
            "bounding what it may do unattended. See the pipeline schema for cron and "
            "on-demand examples."
        ),
    )
    policies: AppPolicies = Field(
        default_factory=AppPolicies,
        description=(
            "Declarative reactions: `on_pipeline_failure` (repair/pause/notify), "
            "`retention_days`, `max_docs_per_collection`."
        ),
    )

    @field_validator("app_id")
    @classmethod
    def _no_path_traversal(cls, value: str) -> str:
        # ':' is rejected because the render token packs `<app_id>:<exp>:<nonce>:<sig>`
        # and recovers app_id via split(":") — a colon-bearing app_id makes every
        # token unverifiable (self-DoS). Caught here at the tool boundary so the
        # agent gets an immediate, actionable reask (the model-side AppSpec.app_id
        # validator is the durable enforcement).
        if (
            not value
            or "/" in value
            or "\\" in value
            or ".." in value
            or ":" in value
            or value.startswith(".")
        ):
            raise ValueError(
                "app_id must be a plain identifier (no '/', '\\', '..', ':', or leading '.')"
            )
        return value


SUBMIT_APP_SCHEMA: dict[str, object] = pydantic_to_openai_tool(
    SubmitAppArgs, name=SUBMIT_APP_TOOL_ID
)


# ------------------------------------------------------------------
# The SessionTool
# ------------------------------------------------------------------


class SubmitAppTool:
    """Handles ``submit_app`` — validate a draft, read its frontend, submit it live.

    Satisfies the :class:`~mewbo_core.session_tools.SessionTool` Protocol via the
    class-shaped ``tool_id``/``schema``/``modes`` attributes plus ``handle`` /
    ``should_terminate_run`` / ``terminal_reason``. The ``submitter`` collaborator
    is resolved from the down-only :func:`current_app_submitter` seam when not
    passed explicitly (the plugin-manifest build path supplies only ``session_id``
    + ``event_logger``); a test injects a fake ``submitter`` directly.
    """

    tool_id: str = SUBMIT_APP_TOOL_ID
    schema: dict[str, object] = SUBMIT_APP_SCHEMA
    modes: frozenset[str] = DEFAULT_SESSION_TOOL_MODES

    def __init__(
        self,
        *,
        session_id: str,
        event_logger: Callable[[Event], None] | None = None,
        submitter: AppSubmitter | None = None,
        max_failures: int = _MAX_SUBMIT_FAILURES,
    ) -> None:
        """Bind the owning session + the submit collaborator.

        Args:
            session_id: Session id; resolves the builder's app directory and is
                passed as ``builder_session_id`` to the submitter.
            event_logger: Accepted for the ``SessionToolRegistry`` manifest
                constructor shape (``session_id=``, ``event_logger=``) — unused
                here. ``app_ready`` is emitted by ``AppLifecycle.submit`` itself
                (the one place an app goes live), not by this tool; emitting it a
                second time here duplicated the event on every submit.
            submitter: The app lifecycle's submit surface. ``None`` (the plugin
                path) means resolve it from ``current_app_submitter`` at handle time.
            max_failures: Failure budget before the run gives up (the Nth failure
                terminates, so the builder gets N-1 reasks).
        """
        self._session_id = session_id
        self._submitter = submitter
        self._max_failures = max_failures
        self._attempts = 0
        self._terminate_pending = False

    # -- Protocol surface (defined explicitly — structural Protocol, no inherited bodies) --

    def should_terminate_run(self) -> bool:
        """Return True once (consuming the flag) after a clean submit or give-up."""
        if self._terminate_pending:
            self._terminate_pending = False
            return True
        return False

    def terminal_reason(self) -> str:
        """A successful submit terminates the build run with ``"completed"``."""
        return "completed"

    # -- dispatch -----------------------------------------------------------

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Validate the draft, read + lint its frontend, and submit it live."""
        raw = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        try:
            args = SubmitAppArgs.model_validate(raw)
        except ValidationError as exc:
            return self._reask(f"invalid submit_app arguments: {exc}")

        try:
            files = self._read_frontend_files(args.app_id, args.entrypoint)
        except _FrontendError as exc:
            return self._reask(str(exc))

        missing = self._missing_pipeline_entrypoints(args, files)
        if missing:
            return self._reask(
                f"pipeline entrypoint(s) not found among your submitted files: {missing} — "
                f"write the file(s) or fix `entrypoint`"
            )

        pipeline_entrypoints = frozenset(
            p.entrypoint for p in args.pipelines if p.mode == "code" and p.entrypoint
        )
        findings = self._lint_frontend(files, pipeline_entrypoints=pipeline_entrypoints)
        if findings:
            return self._reask(
                f"frontend lint failed:\n{findings}\n\nAllowed modules: {sorted(ALLOWED_MODULES)}"
            )

        try:
            draft = self._build_spec(args, files)
        except ValidationError as exc:
            return self._reask(f"the app manifest is invalid: {exc}")

        submitter = self._resolve_submitter()
        if submitter is None:
            # Ops failure, not the builder's fault and not agent-fixable — surface
            # it plainly WITHOUT consuming the reask budget or terminating.
            logging.error("submit_app: no app runtime registered for session {}", self._session_id)
            return MockSpeaker(
                content="ERROR: the apps runtime is not configured on this deployment."
            )

        try:
            persisted = submitter.submit(draft, builder_session_id=self._session_id)
        except Exception as exc:  # noqa: BLE001 — a lifecycle rejection is agent-visible feedback
            return self._reask(f"the platform rejected the app: {exc}")

        # app_ready is emitted by AppLifecycle.submit itself, not here — see the
        # event_logger docstring above (a second emission here was a double-fire).
        self._terminate_pending = True
        logging.info("submit_app: app {} submitted (v{})", persisted.app_id, persisted.version)
        return MockSpeaker(
            content=(
                f"App '{persisted.title}' is live (id {persisted.app_id}, v{persisted.version})."
                f"{self._submission_detail(persisted.app_id, persisted.version)}"
                " The build is complete."
            )
        )

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _submission_detail(app_id: str, version: int) -> str:
        """The version's change summary + non-``pass`` verification verdicts for the echo.

        Reads back the :class:`AppVersion` ``AppLifecycle.submit`` just persisted —
        via the process-wide store factory, the SAME resolution the ``app_data``
        tool uses — so the agent transcript and the user timeline carry what THIS
        version changed (``summary.describe()``) and how its code pipelines verified
        (any ``skipped``/``fail`` verdict; an all-``pass`` run adds nothing). Degrades
        to an empty string when the row isn't resolvable (an unconfigured store, or a
        pre-transparency snapshot), never raising into a successful submit's result.
        """
        try:
            version_row = get_app_store().get_version(app_id, version)
        except Exception:  # noqa: BLE001 - a read-back failure must never sink a successful submit
            return ""
        if version_row is None:
            return ""
        parts: list[str] = []
        if version_row.summary is not None:
            parts.append(f" Changes: {version_row.summary.describe()}.")
        flagged = sorted(
            (name, verdict)
            for name, verdict in (version_row.verification or {}).items()
            if verdict != "pass"
        )
        if flagged:
            rendered = ", ".join(f"{name}={verdict}" for name, verdict in flagged)
            parts.append(f" Pipeline verification: {rendered}.")
        return "".join(parts)

    def _read_frontend_files(self, app_id: str, entrypoint: str) -> dict[str, str]:
        """Read the builder's frontend bundle off disk into a ``{relpath: source}`` map.

        Path-guarded to ``${MEWBO_APPS_ROOT}/${SESSION_ID}/<app_id>/`` (a resolved
        path escaping the root is refused), skips the render-injected
        ``_app_context.json`` and cache dirs, and enforces file-count / total-size
        caps. Raises :class:`_FrontendError` (mapped to a reask) on any problem the
        builder can fix — a missing directory, an absent entrypoint, an oversized
        bundle, an unreadable file.
        """
        base = Path(_apps_root()).resolve()
        app_dir = (base / self._session_id / app_id).resolve()
        try:
            app_dir.relative_to(base)
        except ValueError:
            raise _FrontendError(f"app_id '{app_id}' escapes the apps root") from None
        if not app_dir.is_dir():
            raise _FrontendError(
                f"no app directory at {app_dir} — write your frontend files there first"
            )

        files: dict[str, str] = {}
        total = 0
        for path in sorted(app_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(app_dir)
            if any(part in _SKIP_DIRS for part in rel.parts) or path.suffix == ".pyc":
                continue
            rel_posix = rel.as_posix()
            if rel_posix == _INJECTED_CONTEXT_FILE:
                continue
            if len(files) >= _MAX_FILES:
                raise _FrontendError(f"too many files (> {_MAX_FILES}) — trim the bundle")
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise _FrontendError(f"could not read {rel_posix}: {exc}") from None
            total += len(content.encode("utf-8"))
            if total > _MAX_TOTAL_BYTES:
                raise _FrontendError(
                    f"frontend bundle exceeds {_MAX_TOTAL_BYTES // (1024 * 1024)} MiB"
                )
            files[rel_posix] = content

        if not files:
            raise _FrontendError(f"app directory {app_dir} is empty")
        if entrypoint not in files:
            raise _FrontendError(
                f"entrypoint '{entrypoint}' not found among {sorted(files)} — "
                f"write it or set a different entrypoint"
            )
        return files

    @staticmethod
    def _missing_pipeline_entrypoints(
        args: SubmitAppArgs, files: dict[str, str]
    ) -> list[str]:
        """Every ``mode="code"`` pipeline's ``entrypoint`` that isn't among the submitted files.

        A code pipeline the runner can't find at execution time is a submit-time
        bug, not a runtime surprise — catch it at the same trust boundary the
        frontend's own missing-entrypoint check already guards.
        """
        return sorted(
            {
                p.entrypoint
                for p in args.pipelines
                if p.mode == "code" and p.entrypoint and p.entrypoint not in files
            }
        )

    @staticmethod
    def _lint_frontend(
        files: dict[str, str], *, pipeline_entrypoints: frozenset[str] = frozenset()
    ) -> str:
        """Lint every ``.py`` file; return a formatted, filename-tagged findings block or ``""``.

        Two DIFFERENT rule sets over the SAME directory scan, from two DIFFERENT
        owners: a ``mode="code"`` pipeline's ``entrypoint`` runs server-side under
        ``pipeline_runner.py:AppPipelineRunner``, never in stlite/pyodide, so the
        browser import allowlist (``st``/``pandas``/``mewbo_app`` and nothing
        else) does not apply to it and would reject ordinary stdlib
        data-processing imports (``csv``/``hashlib``) with a false positive.
        Pipeline files are routed to :func:`lint_pipeline` (imported FROM
        ``pipeline_runner.py`` — that module owns the ONE canonical pipeline lint,
        since it also gates actual execution via a guarded ``__import__``; this
        function never re-implements a second, independently-drifting copy).
        Every OTHER ``.py`` file is genuine frontend and keeps the full
        ``lint_app`` (the authoritative stlite/pyodide gate — an agent-side ruff
        can't see the allowlist).
        """
        blocks: list[str] = []
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            findings = (
                lint_pipeline(files[name])
                if name in pipeline_entrypoints
                else lint_app(files[name])
            )
            if findings:
                blocks.append(f"{name}:\n{format_findings(findings)}")
        return "\n\n".join(blocks)

    def _build_spec(self, args: SubmitAppArgs, files: dict[str, str]) -> AppSpec:
        """Assemble the draft :class:`AppSpec` handed to the submitter.

        This carries the builder-authored CONTENT (frontend, collections,
        pipelines, policies, presentation). It stamps ``owner_session_id`` with
        this builder session and ``status="building"``, but the lifecycle owns
        the app's durable IDENTITY: when a ``building`` draft row already exists
        for ``app_id`` (the gallery-create flow), ``AppLifecycle.submit``
        preserves that row's ``owner_session_id`` / ``created_at`` /
        ``workspace_ref`` and only replaces the content, then transitions
        ``building → live`` and attaches the ``maintainer_session_id`` — so the
        ``owner_session_id`` set here is used only when NO draft row exists (the
        chat-builder flow).

        Pipelines are dumped to plain dicts (``exclude_none`` drops the unset
        `time.cron`/`time.at` sibling field so the model-side ``PipelineSpec``
        validates them fresh — this tool never constructs one directly, so a
        schedule kind mismatch or an added field never has two owners to drift
        apart) rather than passed as ``SubmitPipelineArgs`` instances: this
        plugin's args shape is deliberately decoupled from the model's
        ``PipelineSpec`` (Phase 1) — `trigger_ref` is PLATFORM-owned and
        never appears here at all; the lifecycle mints it from `schedule` at
        submit time.
        """
        frontend = AppFrontend(
            entrypoint=args.entrypoint,
            files=files,
            requirements=list(args.requirements),
        )
        return AppSpec(
            app_id=args.app_id,
            title=args.title,
            summary=args.summary,
            icon=args.icon,
            owner_session_id=self._session_id,
            workspace_ref=args.workspace_ref,
            frontend=frontend,
            collections=list(args.collections),
            pipelines=[p.model_dump(mode="json", exclude_none=True) for p in args.pipelines],
            policies=args.policies,
            status="building",
        )

    def _resolve_submitter(self) -> AppSubmitter | None:
        """Explicit constructor injection wins; else the down-only submitter seam."""
        if self._submitter is not None:
            return self._submitter
        return current_app_submitter()

    def _reask(self, detail: str) -> MockSpeaker:
        """Return a bounded "fix and resubmit" result; terminate once the budget is spent."""
        self._attempts += 1
        if self._attempts >= self._max_failures:
            self._terminate_pending = True
            logging.warning(
                "submit_app: giving up after {} failed attempts for session {}",
                self._attempts,
                self._session_id,
            )
            return MockSpeaker(
                content=(
                    f"ERROR: {detail}\n\nThat was attempt {self._attempts} of "
                    f"{self._max_failures}; the build is stopping. Report what blocked you."
                )
            )
        return MockSpeaker(
            content=f"NOT SUBMITTED: {detail}\n\nFix this and call submit_app again."
        )


class _FrontendError(Exception):
    """Internal: a builder-fixable problem reading the frontend bundle (mapped to a reask)."""


__all__ = [
    "SUBMIT_APP_SCHEMA",
    "PipelineSchedule",
    "SubmitAppArgs",
    "SubmitAppTool",
    "SubmitPipelineArgs",
]
