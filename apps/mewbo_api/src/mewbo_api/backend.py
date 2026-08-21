#!/usr/bin/env python3
"""Mewbo API.

Single-user REST API with session-based orchestration and event polling.
"""

# OpenAPI operation summaries are the first docstring line of each HTTP method
# and deliberately omit trailing punctuation (Stripe-style reference docs).
# ruff: noqa: D415

from __future__ import annotations

import hmac
import json
import os
import queue
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, ClassVar

from flask import Flask, Request, Response, g, request, stream_with_context
from flask_restx import Api, Resource, fields
from mewbo_core.capabilities import (
    CAPABILITY_HEADER,
    DEVICE_CONTROL_CAPABILITY,
    parse_capability_header,
)
from mewbo_core.classes import TaskQueue
from mewbo_core.common import get_logger
from mewbo_core.config import (
    AppConfig,
    ConfigWriteAccess,
    ConfigWriteError,
    _deep_merge,
    _load_json,
    get_app_config_path,
    get_config,
    get_config_value,
    get_mcp_config_path,
    get_version,
    reset_config,
    start_preflight,
)
from mewbo_core.contracts.secret_redaction import redact_mapping, redact_text
from mewbo_core.contracts.types import EventRecord
from mewbo_core.llm.llm_resilience import RetryStrategy
from mewbo_core.loop.session_runtime import (
    SessionRuntime,
    SessionTerminatedError,
    parse_core_command,
)
from mewbo_core.permissions import auto_approve
from mewbo_core.secrets.key_store import KeyScopes, KeyStoreBase, PublicKeyRecord, create_key_store
from mewbo_core.session.attachments import (
    is_image,
    is_supported,
    model_supports_vision,
    parse_to_markdown,
    parsed_sidecar_path,
)
from mewbo_core.session.context import _iter_attachments
from mewbo_core.session.event_cursor import EventCursor
from mewbo_core.session.notifications import NotificationStore
from mewbo_core.session.session_provenance import (
    MOBILE_TAG_PREFIX,
    SessionOrigin,
    is_mobile_surface,
)

# Aliased: ``SessionQuery`` is already the name of the ``/sessions/<id>/query``
# Resource below, and that class name is what flask-restx publishes as the
# operation id. The listing predicate takes the alias so neither has to move.
from mewbo_core.session.session_query import SessionQuery as SessionListQuery
from mewbo_core.session.session_store import SessionStoreBase, create_session_store
from mewbo_core.session.share_store import ShareStore
from mewbo_core.session.transcript_timeline import TranscriptTimeline
from mewbo_core.tooling.ask_user import (
    ASK_USER_CAPABILITY,
    MAX_QUESTION_NOTES_CHARS,
    AskUserQuestionTool,
    QuestionAnswerItem,
    QuestionDispatcher,
)
from mewbo_core.tooling.client_tools import ClientDeclaredTool, DeviceToolDispatcher
from mewbo_core.tooling.exit_plan_mode import PLAN_DIR_ROOT, plan_file_for, session_temp_dir
from mewbo_core.tooling.session_tools import SessionTool
from mewbo_core.tooling.tool_registry import (
    classify_tool_scope,
    get_or_build_registry,
    load_registry,
)
from mewbo_core.workspaces.project_catalog import (
    AUTO_PROJECT,
    MANAGED_PREFIX,
    ProjectCatalog,
    ProjectResolutionError,
    is_auto_project,
)
from mewbo_core.workspaces.project_store import (
    ProjectStoreBase,
    VirtualProject,
    create_project_store,
)
from mewbo_core.workspaces.repository_store import create_repository_store
from mewbo_core.workspaces.worktree import WorktreeBranchInUseError, WorktreeManager
from mewbo_tools.integration.file_catalog import FileCatalog
from mewbo_tools.integration.reference_expansion import expand_references
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from werkzeug.exceptions import NotFound
from werkzeug.utils import secure_filename

from mewbo_api.config_view import ConfigSchemaView
from mewbo_api.contracts import ApiResponse
from mewbo_api.errors import (
    RequestInvalid,
    StreamCapacityExhausted,
    register_api_error_handler,
)
from mewbo_api.repo_identity import RepoIdentity
from mewbo_api.request_context import request_surface
from mewbo_api.responses import ApiResponseKit
from mewbo_api.session_spec import (
    SessionSpec,
    SessionSpecOverrides,
    SessionSpecStore,
)
from mewbo_api.stream_capacity import LeasedStream, StreamCapacity

# The canonical 410-Gone body for a permanently terminated session lives on the
# response kit — the ONE home both this module and triggers/routes.py import
# with no cycle. This thin alias keeps the guard call sites below terse while
# the envelope stays single-sourced.
_terminated_response = ApiResponseKit.terminated_response

# ``done_reason`` taxonomy — the orchestrator and /command paths share these
# canonical values so every consumer (notifications, status badge,
# summarize_session, FE recovery card) classifies a terminal turn the same way.
# Anything not listed here is treated as an unrecognized success — better to
# under-warn than spuriously cry "failed" at users.
_FAILURE_REASONS = {
    "error",
    "max_steps_reached",
    "max_iterations_reached",
    "compact_failed",
}
_FAILURE_PREFIXES = ("command_failed:",)
_TRANSIENT_REASONS = {"canceled", "awaiting_approval"}


def _classify_done_reason(done_reason: str) -> str | None:
    """Classify a ``done_reason`` for notification routing.

    Returns ``"success"``, ``"failure"``, or ``None`` for transient states
    that should not produce a user-visible toast (e.g. mid-flow approval
    gates, user-initiated cancels).
    """
    reason = done_reason.lower()
    if reason in _TRANSIENT_REASONS:
        return None
    if reason in _FAILURE_REASONS or any(reason.startswith(p) for p in _FAILURE_PREFIXES):
        return "failure"
    return "success"


def _success_message(done_reason: str) -> str:
    """Render the body of a success toast.

    /compact and other slash commands deserve specific phrasing so the
    notification reads naturally in the panel; everything else falls back
    to the generic completion line.
    """
    reason = done_reason.lower()
    if reason == "compacted":
        return "Compaction finished."
    if reason.startswith("command:"):
        return f"Command {reason.split(':', 1)[1]} finished."
    return "Turn finished successfully."


class NotificationService:
    """Emit session lifecycle notifications for the API."""

    def __init__(self, store: NotificationStore, session_store: SessionStoreBase) -> None:
        """Initialize with notification and session stores."""
        self._store = store
        self._session_store = session_store

    def notify(
        self,
        *,
        title: str,
        message: str,
        level: str = "info",
        session_id: str | None = None,
        event_type: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        """Persist a notification record."""
        self._store.add(
            title=title,
            message=message,
            level=level,
            session_id=session_id,
            event_type=event_type,
            metadata=metadata,
        )

    def _session_label(self, session_id: str) -> str:
        """User-facing label for a session: stored title, else short-id fallback."""
        title = self._session_store.load_title(session_id)
        if title:
            return title
        return f"Session {session_id[:8]}"

    def emit_session_created(self, session_id: str) -> None:
        """Append a session-created event and notify."""
        self._session_store.append_event(
            session_id,
            {"type": "session", "payload": {"event": "created"}},
        )
        label = self._session_label(session_id)
        self.notify(
            title="New session",
            message=f"Started '{label}'.",
            session_id=session_id,
            event_type="created",
        )

    def emit_completion(self, session_id: str) -> None:
        """Emit a completion notification based on the latest completion event."""
        events = self._session_store.load_recent_events(
            session_id,
            limit=1,
            include_types={"completion"},
        )
        if not events:
            return
        event = events[-1]
        completion_ts = event.get("ts")
        if not completion_ts or self._completion_exists(session_id, completion_ts):
            return
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        if not bool(payload.get("done")):
            # Not terminal yet (mid-run snapshot) — wait for the real close.
            return

        done_reason = str(payload.get("done_reason") or "")
        label = self._session_label(session_id)
        metadata = {"completion_ts": completion_ts, "done_reason": done_reason}

        outcome = _classify_done_reason(done_reason)
        if outcome is None:
            # Transient / intermediate (canceled, awaiting_approval) — no
            # success/failure semantic, so no toast.
            return
        if outcome == "success":
            self.notify(
                title=f"'{label}' completed",
                message=_success_message(done_reason),
                session_id=session_id,
                event_type="completed",
                metadata=metadata,
            )
            return
        self.notify(
            title=f"'{label}' failed",
            message=f"Reason: {done_reason or 'unknown'}.",
            level="warning",
            session_id=session_id,
            event_type="failed",
            metadata=metadata,
        )

    def _completion_exists(self, session_id: str, completion_ts: str) -> bool:
        for item in self._store.list(include_dismissed=True):
            if item.get("session_id") != session_id:
                continue
            if item.get("event_type") not in {"completed", "failed"}:
                continue
            metadata = item.get("metadata") or {}
            if metadata.get("completion_ts") == completion_ts:
                return True
        return False


MASTER_API_TOKEN = os.environ.get("MEWBO_MASTER_API_TOKEN") or get_config_value(
    "api", "master_token", default="msk-strong-password"
)

logging = get_logger(name="mewbo-api")
logging.info("Starting Mewbo API server.")
logging.debug("API master token configured: {}", "yes" if MASTER_API_TOKEN else "no")

_config = get_config()
if _config.runtime.preflight_enabled:
    start_preflight(_config)

# Load hooks from config so API sessions run with configured hooks
from mewbo_core.hooks import HookManager as _HookManager  # noqa: E402
from mewbo_core.session.session_event_bus import (  # noqa: E402
    SessionEventBus,
    Subscription,
    get_session_event_bus,
)

_hook_manager = _HookManager.load_from_config(_config.hooks)
# Bridge the append-time event bus to the hook manager: every appended event
# fires the configured ``on_event`` hooks. This is the ONLY place the bus and
# hook manager connect (the SSE stream subscribes to the same bus directly).
get_session_event_bus().register_observer(_hook_manager.run_on_event)

app = Flask(__name__)
session_store = create_session_store()
key_store: KeyStoreBase = create_key_store()
project_store = create_project_store()
repository_store = create_repository_store()


class ManagedCheckoutLocator:
    """Answers "which managed project is a checkout of this repository slug?".

    The ONE fact :class:`ProjectCatalog` cannot compute for itself, which is why
    it takes the join as an injected callable: matching a repository identity
    against a project's git REMOTES needs ``RepoIdentity``, which lives in this
    app, above core in the DAG. The matching RULE is the alias-set rule
    ``GitRepositoriesController`` already reports ``usage.tasks`` from, so the
    catalog and that projection cannot disagree about which checkout a slug
    belongs to — including its known limitation, the last-two-segments alias
    form that misses a subgroup-nested remote.

    **Why the per-path memo is load-bearing.** ``RepoIdentity.aliases_for_path``
    SHELLS OUT once per project, and the catalog asks for this join on every
    resolution — which now includes the session-create and query paths, where
    nothing spawned a subprocess before. A long-lived store accretes promoted
    parents (a dev box held several hundred, nearly all dead temp paths), so an
    unmemoized join would put that many process spawns on every request. Two
    cheap filters do most of the work: a project whose path is not a git working
    tree is skipped without spawning anything, and a path's alias set is read
    once per process. The project LIST is re-read every call, so a checkout
    created moments ago costs exactly one new ``git`` call and is resolvable
    immediately — the memo bounds the cost, never the freshness.
    """

    def __init__(self, project_store: ProjectStoreBase) -> None:
        """Bind the store whose projects are candidate checkouts."""
        self.project_store = project_store
        self._aliases_by_path: dict[str, list[str]] = {}

    def locate(self, slug: str) -> str | None:
        """Return the checkout directory holding *slug*, or ``None``.

        Worktrees are skipped for the same reason the usage projection skips
        them: a worktree is a child of a parent that is itself a candidate, so
        including it would report one repository as several checkouts.
        """
        try:
            projects = self.project_store.list_projects()
        except Exception:  # noqa: BLE001 - an unreadable store means "no checkout known"
            return None
        for project in projects:
            if project.is_worktree or not project.path:
                continue
            if slug in self._aliases_for(project.path):
                return project.path
        return None

    def _aliases_for(self, path: str) -> list[str]:
        """Every alias form the remote(s) at *path* are addressable by, memoized."""
        cached = self._aliases_by_path.get(path)
        if cached is not None:
            return cached
        # ``_is_git_repo`` is defined further down this module; it resolves at
        # call time. Reusing it is what keeps a dead or non-git project path
        # from costing a process spawn.
        aliases = RepoIdentity.aliases_for_path(path) if _is_git_repo(path) else []
        self._aliases_by_path[path] = aliases
        return aliases


_checkout_locator = ManagedCheckoutLocator(project_store)

# The ONE catalog: every "project name → directory" decision in this app runs
# through it. Held as a composition-root handle rather than rebuilt per call so
# the memo above survives, and re-pointed by ``_catalog()`` because two of its
# four sources legitimately move under a running process.
_project_catalog = ProjectCatalog(
    configured=_config.projects,
    project_store=project_store,
    repository_store=repository_store,
    checkout_locator=_checkout_locator.locate,
)


def _catalog() -> ProjectCatalog:
    """The one catalog, re-pointed at the sources this process currently holds.

    ``get_config()`` is re-read on every resolution today (a ``PATCH /api/config``
    can add or drop a project mid-process) and ``project_store`` is a module
    global the suite legitimately rebinds to a temp-dir store per test. A catalog
    that captured either at import would keep answering from the config or the
    store that existed then — the same late-binding reason ``_session_specs``
    takes lambdas rather than bound methods.
    """
    _project_catalog.configured = get_config().projects
    _project_catalog.project_store = project_store
    _checkout_locator.project_store = project_store
    return _project_catalog


def _auto_cleanup_worktree_on_session_end(session_id: str, error: str | None) -> None:
    """Auto-remove a worktree-backed session's worktree if it is clean.

    When a worktree-bound session ends and leaves no uncommitted changes /
    unpushed commits behind, drop the worktree. Otherwise keep it so the
    user can resume or recover work.

    After reaping the child worktree, also reaps the auto-promoted parent if it
    now has no remaining worktree children (the orphan-parent symptom). An
    auto-promoted parent is identified by ``path_source == "provided"`` — it was
    lifted from a config project and is system-owned, not user-created.

    Failures are swallowed — this is best-effort housekeeping, never blocking.
    """
    # The newest context event NAMING a project, not the newest context event:
    # context events merge key-by-key, so a session's latest one need not mention
    # a project at all. Type-bounded, so this is one indexed read, not a scan.
    try:
        event = session_store.latest_event_of_type(session_id, "context", payload_key="project")
    except Exception:
        return
    payload = (event.get("payload") if event else None) or {}
    candidate = payload.get("project") if isinstance(payload, dict) else None
    project_name = candidate if isinstance(candidate, str) and candidate else None
    if not project_name or not project_name.startswith("managed:"):
        return
    vpid = project_name[len("managed:") :]
    proj = project_store.get_project(vpid)
    if proj is None or not proj.is_worktree:
        return
    parent_project_id = proj.parent_project_id
    try:
        if WorktreeManager.is_clean(proj.path):
            project_store.delete_worktree(vpid)
            # Reap the orphan auto-promoted parent when it has no remaining
            # worktree children. ``path_source == "provided"`` distinguishes
            # system-promoted parents (reapable) from user-created managed
            # projects (keep). Never raises — best-effort only.
            if parent_project_id:
                parent = project_store.get_project(parent_project_id)
                if (
                    parent is not None
                    and not parent.is_worktree
                    and parent.path_source == "provided"
                    and not project_store.list_worktrees(parent_project_id)
                ):
                    project_store.delete_project(parent_project_id)
    except Exception:
        # Never let auto-cleanup raise from the hook chain.
        pass


_hook_manager.on_session_end.append(_auto_cleanup_worktree_on_session_end)


runtime = SessionRuntime(session_store=session_store)
notification_store = NotificationStore(root_dir=session_store.root_dir)
share_store = ShareStore(root_dir=session_store.root_dir)

# Settle session runs orphaned by a process restart (deploy / OOM kill): a
# worker that died mid-turn leaves a transcript ending on run activity with no
# terminal ``completion``, so ``summarize_session`` reads ``idle`` and the
# console's recovery card never renders. The session-side peer of the apps
# ledger's ``sweep_orphaned_runs`` appends a synthetic error completion so the
# status flips to ``failed`` and recovery surfaces. Store-only + best-effort: a
# failure here must never break startup. NOTE this WRITES synthetic completions
# to the configured store at import — and in this app import IS startup, so any
# test importing this module triggers it (idempotent, but a write; see
# apps/mewbo_api/CLAUDE.md → the /variables prime-at-boot lesson).
# ``MEWBO_BOOT_RUN_SWEEP=0`` opts out entirely.
try:
    from mewbo_api.run_sweep import SessionRunSweeper  # noqa: PLC0415

    _session_runs_settled = SessionRunSweeper(
        session_store, now=lambda: datetime.now(timezone.utc)
    ).sweep()
    if _session_runs_settled:
        logging.warning(
            "Settled {} orphaned session run(s) as interrupted at startup",
            _session_runs_settled,
        )
except Exception:
    logging.warning("Session-run startup sweep failed", exc_info=True)

# Report a runtime the harness denials cannot actually cover. Every one of them
# denies by path PREFIX, and a bind mount gives the same inodes a second name
# that ``realpath`` does not collapse — so mounting the source over the runtime
# leaves the harness readable and writable through its project path while the
# denial on the installed path reports healthy. Measured on a real deployment,
# for write, against the turn loop's source and the credential file.
#
# Read-only and best-effort: it cannot fix the topology and does not try, since
# widening the denial to inode identity would deny a legitimate project mount.
# All it buys is that the bypass stops being silent. ``MEWBO_BOOT_ALIAS_PROBE=0``
# opts out for a deployment that has accepted the aliasing and wants its log back.
try:
    if os.environ.get("MEWBO_BOOT_ALIAS_PROBE", "1") != "0":
        from mewbo_tools.integration.runtime_aliasing import RuntimeAliasProbe  # noqa: PLC0415

        RuntimeAliasProbe.for_deployment().report()
except Exception:
    logging.warning("Runtime aliasing probe failed", exc_info=True)

# Client-declared device tools: register the concrete
# dispatcher into the core seam, mirroring how the api registers
# RunStoreSearchLauncher for the agentic-search SessionTool. Unconditional
# (no feature flag) — a session simply never advertises `device_tools` when
# the feature isn't in use.
from mewbo_api.device_tools import (  # noqa: E402
    ApiDeviceToolDispatcher,
    DeviceToolBinding,
    get_pending_calls,
)

DeviceToolDispatcher.register(ApiDeviceToolDispatcher(runtime=runtime))

# Ask-user questions: same down-only registration, same no-flag rationale — a
# session simply never advertises the `ask_user` capability when no interactive
# client is attached, so the tool (and the wait it blocks on, which is unbounded
# unless the call names its own `timeout_seconds`) never exists for it.
from mewbo_api.ask_user import (  # noqa: E402
    ApiQuestionDispatcher,
    QuestionAnswerRouter,
    TurnDelivery,
    get_pending_questions,
)

QuestionDispatcher.register(ApiQuestionDispatcher(runtime=runtime))

authorizations = {"apikey": {"type": "apiKey", "in": "header", "name": "X-API-KEY"}}
VERSION = get_version()
api = Api(
    app,
    version=VERSION,
    title="Mewbo API",
    description="Interact with Mewbo through a REST API",
    doc="/swagger-ui/",
    authorizations=authorizations,
    security="apikey",
)

ns = api.namespace("api", description="Mewbo operations")

# One DRY home for the error-response half of the OpenAPI contract on this
# namespace. ``kit.errors(...)`` / ``kit.auth_error()`` attach example-bearing
# error bodies (envelope or ``{"message"}`` shape) per route — see
# ``responses.py``. Wire it once here so import-time decorators can see it.
kit = ApiResponseKit(ns, prefix="Api")


# Renders a raised ``ApiError`` on BOTH surfaces, and both are required: a plain
# Flask errorhandler does not cover a flask-restx ``Resource``, because RESTX
# installs its own error_router ahead of Flask's dispatch and would otherwise
# render a generic 500 for any route it owns. Registered here, beside the
# ``Api(app, …)`` setup, so the taxonomy works the first time a Resource raises.
register_api_error_handler(app, api)


@app.errorhandler(NotFound)
def _handle_not_found(exc: NotFound) -> tuple[dict, int]:
    """Return JSON for any unmatched route (no raw Werkzeug HTML leak).

    A request that matches no route (e.g. a ``project`` containing a ``/``
    that splits the path) would otherwise render Werkzeug's HTML 404 page.
    One app-level handler keeps every endpoint's 404 a JSON contract.
    """
    return {"error": {"code": 404, "reason": exc.description}}, 404


def _session_not_found(session_id: str) -> tuple[dict, int]:
    """Canonical JSON 404 envelope for an unknown session id.

    Matches the ``@app.errorhandler(NotFound)`` shape so the MCP ``_enveloped``
    not-found mapping reads it identically whether the route or Werkzeug raised.
    """
    return {"error": {"code": 404, "reason": f"session {session_id} not found"}}, 404


def _session_exists(session_id: str) -> bool:
    """True iff *session_id* is a real stored session (the canonical guard)."""
    return session_id in runtime.session_store.list_sessions()


def _terminated_guard(session_id: str) -> tuple[dict, int] | None:
    """Return a 410 Gone envelope iff *session_id* is permanently terminated.

    The single guard every MUTATING entry point calls (query/message/interrupt/
    recover/fork/sync-query). Reads — events, stream, history — deliberately do
    NOT call it: a terminated session stays fully inspectable (terminated ≠
    deleted). The ``code`` is the semantic ``session_terminated`` token and
    ``retryable`` is false; a non-terminated (or unknown) session returns
    ``None`` so callers fall through (WP2).
    """
    if runtime.is_terminated(session_id):
        return _terminated_response()
    return None


# Free-text payload fields that can carry full prompts / tool dumps (uncapped
# upstream). ``summary`` is already capped at the source (``max_result_chars``),
# so it is deliberately NOT in this set.
_EVENT_FREETEXT_FIELDS = ("result", "tool_input", "detail", "error")
_EVENT_FIELD_CAP = 2000


def _cap_freetext(value: object) -> object:
    """Cap a single free-text string at ``_EVENT_FIELD_CAP``; pass through others."""
    if isinstance(value, str) and len(value) > _EVENT_FIELD_CAP:
        return value[:_EVENT_FIELD_CAP]
    return value


def _truncate_event_freetext(events: list[dict]) -> list[dict]:
    """Cap large free-text payload fields (full prompts / tool dumps).

    Opt-in via ?truncate=1 so the console's full-result view is unaffected.
    """
    out = []
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            out.append(event)
            continue
        new_payload = dict(payload)
        for field in _EVENT_FREETEXT_FIELDS:
            value = new_payload.get(field)
            if isinstance(value, str) and len(value) > _EVENT_FIELD_CAP:
                new_payload[field] = value[:_EVENT_FIELD_CAP]
                new_payload[f"{field}_truncated"] = True
            elif isinstance(value, (dict, list)):
                blob = json.dumps(value)
                if len(blob) > _EVENT_FIELD_CAP:
                    new_payload[field] = blob[:_EVENT_FIELD_CAP]
                    new_payload[f"{field}_truncated"] = True
        out.append({**event, "payload": new_payload})
    return out


# Web IDE (code-server) namespace. Actually initialized further down, once
# ``_require_api_key`` is defined.
_ide_manager = None

task_queue_model = api.model(
    "TaskQueue",
    {
        "plan_steps": fields.List(
            fields.Nested(
                api.model(
                    "PlanStep",
                    {
                        "title": fields.String(
                            required=True,
                            description="Short title for the plan step",
                        ),
                        "description": fields.String(
                            required=True,
                            description="Brief description of the step",
                        ),
                    },
                )
            )
        ),
        "session_id": fields.String(
            required=False, description="Session identifier for transcript storage"
        ),
        "human_message": fields.String(required=True, description="The original user query"),
        "task_result": fields.String(
            required=True, description="Combined response of all action steps"
        ),
        "action_steps": fields.List(
            fields.Nested(
                api.model(
                    "ActionStep",
                    {
                        "tool_id": fields.String(
                            required=True,
                            description="The tool responsible for executing the action",
                        ),
                        "operation": fields.String(
                            required=True,
                            description="The type of action to be performed (get/set)",
                        ),
                        "tool_input": fields.Raw(
                            required=True, description="Arguments for the tool invocation"
                        ),
                        "result": fields.String(description="The result of the executed action"),
                    },
                )
            )
        ),
    },
)


# How much of a request body the debug log will look at. Generous enough that a
# real payload is readable in full, small enough that no caller can turn this
# hook into a workload. A debug line is a diagnostic, not an archive.
_LOG_BODY_LIMIT = 8192


@app.before_request
def log_request_info() -> None:
    """Log request metadata for debugging.

    Headers (``Authorization``/``X-API-KEY``/``Cookie``) and the body can carry
    credentials, so both are scrubbed through the secret redactor before they
    reach any sink — the record-level patcher would also catch known shapes, but
    redacting the structured payload here catches arbitrary-shaped secrets under
    a named key too.

    Cost: ``O(_LOG_BODY_LIMIT)``, NOT ``O(request body)`` — and that bound is the
    point. This is a ``before_request`` hook, so it runs on every request ahead
    of routing and authentication, and loguru defers FORMATTING but Python
    evaluates the argument eagerly, so the redaction runs at any log level. A
    caller with no credentials and no valid route therefore spends whatever this
    line costs, on a single-worker process where ``re`` holds the GIL.

    The body is TRUNCATED BEFORE redaction, never redacted past a cap and then
    cut. Getting that order backwards would log a prefix nothing had scrubbed —
    trading a CPU defect for a credential disclosure, which is the worse of the
    two by a distance.
    """
    logging.debug("Endpoint: {}", request.endpoint)
    logging.debug("Headers: {}", redact_mapping(dict(request.headers)))
    body = request.get_data(as_text=True)[:_LOG_BODY_LIMIT]
    logging.debug("Body: {}", redact_text(body))


_CORS_ORIGIN = os.environ.get("CORS_ORIGIN", "*")


@app.after_request
def _add_cors_headers(response: Response) -> Response:
    """Allow cross-origin requests. Set CORS_ORIGIN env var to restrict."""
    response.headers["Access-Control-Allow-Origin"] = _CORS_ORIGIN
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, X-API-Key, X-Mewbo-Capabilities, X-Mewbo-Surface, "
        "X-Mewbo-App-Token, Authorization"
    )
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
    # Cookie auth is cross-origin-credentialed; a wildcard origin cannot carry
    # credentials, so send Allow-Credentials only for an exact origin.
    if _CORS_ORIGIN != "*":
        response.headers["Access-Control-Allow-Credentials"] = "true"
    return response


def _request_credential(req: Request | None = None) -> str | None:
    """Return the presented credential (header preferred, query param for SSE).

    ``req`` defaults to the in-flight Flask ``request``; the AuthKit passes an
    explicit request so principal resolution reads the same credential contract.
    """
    source = req if req is not None else request
    return source.headers.get("X-API-Key") or source.args.get("api_key")


def _token_matches_master(token: str) -> bool:
    """Constant-time compare *token* against the master token.

    Uses ``hmac.compare_digest`` so the master credential cannot be recovered
    via a byte-by-byte timing side-channel — the same primitive the KeyStore
    already uses to verify hashed keys.
    """
    return hmac.compare_digest(token, MASTER_API_TOKEN)


# -- Identity & access management -----------------------------------------
# ONE AuthKit resolves every request to a Principal. With no ``api.auth`` block
# (the default) it is DISABLED: ``resolve`` returns the full-power
# principal, the guards below behave exactly as they did before IAM existed, no
# IAM store is created, and no ``iam_*.json`` is written. Constructed here (this
# app builds at import) so an ENABLED deployment fails LOUD at boot on invalid
# auth config or a missing authenticator driver. The credential reader and the
# master compare are injected so the kit reuses the one documented contract.
from mewbo_api.auth import AuthKit  # noqa: E402
from mewbo_api.auth.guard_registry import guard, guard_registry  # noqa: E402

_auth_kit = AuthKit.from_config(
    _config,
    # A provider, not the instance: resolved per request off this module, so a
    # caller that substitutes the store after boot is actually honored.
    key_store=lambda: key_store,
    credential_reader=_request_credential,
    master_matcher=_token_matches_master,
)

# A browser-login deployment cannot sit behind a wildcard CORS origin: the
# credentialed cookie request the console makes is invalid against "*". Fail at
# boot rather than at the user's first login attempt.
_auth_kit.validate_deployment(_CORS_ORIGIN)

# THE composition root for the declarative route guard. Route modules import the
# process-wide guard and decorate at module scope, which runs long before this
# line; every one of those bindings enforces against nothing until the live kit
# is bound here. It must therefore stay AHEAD of the first route import below —
# an unbound guard raises rather than failing open, so a missing bind is a 500
# on every decorated route, not a silently unguarded one.
guard_registry.bind(_auth_kit)


@app.before_request
def _resolve_principal() -> None:
    """Store the caller's resolved identity on ``g.principal`` (never rejects).

    Additive to the request outcome: rejection stays in the per-route guards, so
    the deliberately-public routes (share links, trigger hooks, channel
    webhooks, CORS preflight) are untouched. A resolution error while auth is
    enabled fails CLOSED to ``None`` — the guards then reject — and never 500s.
    """
    try:
        g.principal = _auth_kit.resolve(request)
    except Exception:  # noqa: BLE001 - resolution must never break the request
        g.principal = None
        logging.warning("principal resolution failed; treating as unauthenticated", exc_info=True)


def _require_api_key() -> tuple[dict, int] | None:
    """Authorize a protected route — thin wrapper over the one AuthKit.

    Preserves the exact wire contract every call site depends on: same accepted
    credentials (``X-API-Key`` header / ``api_key`` query, the latter for SSE),
    same ``{"message": ...}`` bodies and statuses. See ``AuthKit.require_api_key``.
    """
    return _auth_kit.require_api_key()


def _require_master_token() -> tuple[dict, int] | None:
    """Authorize a master-token-only route — thin wrapper over the one AuthKit.

    Issued keys are deliberately rejected here: a leaked key must not be able to
    mint or revoke keys. See ``AuthKit.require_master_token``.
    """
    return _auth_kit.require_master_token()


# A permission-guard FACTORY over the resolved ``g.principal``:
# ``_require_permission("wiki.read")`` returns a zero-arg guard with the same
# ``(body, status) | None`` contract as the two guards above, so it COMPOSES with
# them rather than replacing them —
# ``_require_api_key() or _require_permission("sessions.read")()`` authenticates
# first, then authorizes. Order matters: an unauthenticated caller must get the
# 401 the key guard has always returned, never a 403 about a role it was never
# asked to present. With auth disabled every request resolves to the admin
# principal, which bypasses every check — so the composed form is byte-identical
# to the bare key guard in a default deployment.
#
# Route modules receive the factory the same way they receive the key guard (an
# ``init_*`` parameter or a controller field), never by importing it from here.
_require_permission = _auth_kit.require_permission

# Browser login + identity surface. The controller reads the kit's shared OIDC
# runtime (one JWKS/discovery cache for both request resolution and the routes);
# with auth or OIDC off the runtime is None and the routes answer "not
# configured" without touching a store.
from mewbo_api.auth import AuthRoutesController, init_auth_routes  # noqa: E402

init_auth_routes(app, AuthRoutesController.from_kit(_auth_kit))

from mewbo_api.auth.saml_routes import SamlRoutesController, init_saml_routes  # noqa: E402

init_saml_routes(app, SamlRoutesController.from_kit(_auth_kit))


def _deprovision_scim_subject(subject: str) -> None:
    """Revoke every credential the deprovisioned *subject* owns.

    Session termination is not covered yet: sessions carry no owner stamp and
    the store has no owner-scoped bulk terminate, so a disabled user's live runs
    survive until the ownership sweep lands. Best-effort by contract — a store
    failure is logged, never raised into the SCIM request path.
    """
    try:
        for record in key_store.list_keys():
            if record.get("owner_subject") == subject:
                key_store.revoke_key(record["id"])
    except Exception:  # noqa: BLE001 - provisioning must not break on store errors
        logging.warning("key revocation failed for deprovisioned subject", exc_info=True)


from mewbo_api.scim import init_scim  # noqa: E402

init_scim(app, settings=_auth_kit.settings, deprovision=_deprovision_scim_subject)

# The administration surface over the same stores. It takes the SAME
# deprovision callback SCIM does, so an account disabled from the console loses
# its credentials exactly as one disabled by an identity provider does.
from mewbo_api.iam import init_iam_routes  # noqa: E402

init_iam_routes(
    app,
    settings=_auth_kit.settings,
    deprovision=_deprovision_scim_subject,
)

# Imported down HERE, not at the top of the file: the ``AuthKit`` constructed
# above has already pulled the kernel in, so this costs nothing, while a
# top-of-file import would load ``mewbo_iam`` before the config seam this
# module builds against. Used for its ``subject`` law at the key-mint boundary
# and for annotations.
from mewbo_iam import Principal  # noqa: E402

# ONE resolver turns the resolved principal's role into a per-run SessionScope
# (tool ceiling + permission policy + approval callback) that every
# ``start_async``/``run_sync`` site threads into the runtime. Reuses the
# AuthKit's already-parsed settings, so the disabled/admin path is a pure
# passthrough and the run stays byte-identical.
from mewbo_api.auth import current_principal  # noqa: E402
from mewbo_api.auth.session_scope import (  # noqa: E402
    SessionScope,
    SessionScopeResolver,
    principal_from_authority,
)


def _baseline_tool_ids() -> frozenset[str]:
    """Built-in (non-MCP) tool ids a role-bounded session keeps under strict scope.

    Read from the LIVE registry (cached) so a newly registered built-in flows in
    automatically — never a hardcoded list. The built-in set is cwd-invariant
    (only MCP tools vary by project), so the ``cwd=None`` registry is the right
    source. ``always_load`` specs (``tool_search``) are included harmlessly —
    ``filter_specs`` exempts them from the allowlist gate regardless.
    """
    return frozenset(
        spec.tool_id
        for spec in get_or_build_registry(cwd=None).list_specs()
        if spec.kind != "mcp"
    )


_session_scope = SessionScopeResolver(
    settings=_auth_kit.settings,
    baseline_tool_ids=_baseline_tool_ids,
)


def _run_scope(
    *,
    allowed_tools: list[str] | None,
    client_capabilities: Sequence[str] | None = None,
    strict_tool_scope: bool = False,
) -> SessionScope:
    """Resolve the in-flight caller's role into a run scope for a start/run call.

    The single seam every request-driven ``start_async``/``run_sync`` site uses:
    reads ``current_principal()`` and hands the resolver the caller's requested
    grants. Auth disabled or an admin caller ⇒ a byte-identical passthrough scope
    (permissive, ``capability_mode="all"``, no policy, ``auto_approve``).
    """
    return _session_scope.resolve(
        current_principal(),
        requested_allowed_tools=allowed_tools,
        requested_capabilities=client_capabilities,
        requested_strict_tool_scope=strict_tool_scope,
    )


def _stamp_principal_subject(context_payload: dict[str, object]) -> None:
    """Record the caller's subject on a fresh context payload (auth-enabled only).

    Additive provenance: which principal a session was created/run under. Gated
    on auth being enabled AND a resolved principal, so a disabled deployment
    never writes the key and the persisted context stays byte-identical.
    """
    if not _auth_kit.enabled:
        return
    principal = current_principal()
    if principal is not None:
        context_payload["principal_subject"] = principal.subject


# -- Web IDE (code-server) namespace --------------------------------------
# Wire up only when enabled and only if the session store exposes a Mongo
# database (the feature needs a real Mongo backend for the IdeStore).
_web_ide_cfg = _config.agent.web_ide
if _web_ide_cfg is not None and _web_ide_cfg.enabled:
    _mongo_db = getattr(session_store, "_db", None)
    if _mongo_db is None:
        logging.warning(
            "web_ide enabled but session store has no MongoDB backend; "
            "IDE namespace will not be registered."
        )
    else:
        try:
            from mewbo_api.ide import (
                BrokerContainerBackend,
                DockerContainerBackend,
                IdeContainerBackend,
                IdeManager,
                IdeStore,
            )
            from mewbo_api.ide_broker import IdeBrokerClient
            from mewbo_api.ide_routes import ide_ns, init_ide

            _ide_store = IdeStore(_mongo_db)
            # The broker is selected only when BOTH coordinates are present. Its
            # shared secret is read from the environment and never from
            # app.json, which GET /api/config serves. Log the choice at INFO:
            # this is how an operator tells whether the API process still holds
            # the docker socket.
            _ide_broker_url = _web_ide_cfg.broker_url.strip()
            _ide_broker_token = os.environ.get("MEWBO_IDE_BROKER_TOKEN", "").strip()
            _ide_backend: IdeContainerBackend
            if _ide_broker_url and _ide_broker_token:
                _ide_backend = BrokerContainerBackend(
                    IdeBrokerClient(_ide_broker_url, _ide_broker_token)
                )
                logging.info(
                    "web_ide: container operations delegated to the IDE broker at {}; "
                    "this process needs no docker socket",
                    _ide_broker_url,
                )
            else:
                _ide_backend = DockerContainerBackend(_web_ide_cfg)
                logging.info(
                    "web_ide: driving the docker daemon directly from the API process "
                    "(no broker configured — set agent.web_ide.broker_url and "
                    "MEWBO_IDE_BROKER_TOKEN to move that privilege out)"
                )
            _ide_manager = IdeManager(_web_ide_cfg, _ide_store, backend=_ide_backend)
            # ``_catalog`` (the accessor, not its result) so the mount tier
            # resolves against the config and project store the process holds at
            # request time — the same late-binding reason every other call site
            # here goes through it.
            init_ide(_ide_manager, runtime, _catalog)
            api.add_namespace(ide_ns, path="/api")
            logging.info("web_ide namespace registered at /api")
        except Exception as exc:  # pragma: no cover - startup fail-soft
            logging.warning("web_ide namespace failed to initialize: {}", exc)
            _ide_manager = None


# -- Agentic Search namespace ---------------------------------------------
# Persistent workspaces + runs (JSON/Mongo via the store) and a run lifecycle
# driven by the per-run resolved SearchRunner (echo replay, or the orchestrated
# SCG runner once scg.enabled is on and a source is mapped).
from mewbo_api.agentic_search import (  # noqa: E402
    init_agentic_search,
    store as agentic_search_store,
)
from mewbo_api.agentic_search.runs import SearchRun  # noqa: E402

init_agentic_search(api, runtime=runtime)
logging.info("agentic_search namespace registered at /api")

# A run backed by a real (orchestrated) session settles ``failed`` when its
# session dies mid-flight; if that session is later recovered (``/recover``,
# a ``/message`` re-engage) and genuinely finishes, the run record is stuck
# wrong forever unless something re-visits it. Every session end already
# knows a session reached a terminal state, so this checks whether the
# ending session backs a search run — via its ``agentic_search:run:<id>``
# tag, the orchestrated runner's own tag (``scg/orchestrated_runner.py:
# _seed_session``) — and offers it to ``SearchRun.reconcile_after_recovery``.
# That call refuses unless the run is currently ``failed`` AND the session
# now summarises ``completed``, so firing it on every ordinary settle
# (recovered or not) is a safe no-op.
_AGENTIC_SEARCH_RUN_TAG_PREFIX = "agentic_search:run:"


def _reconcile_agentic_search_after_recovery(session_id: str, error: str | None) -> None:
    """Amend a ``failed`` search run whose backing session went on to complete."""
    for tag in session_store.tags_for_session(session_id):
        if tag.startswith(_AGENTIC_SEARCH_RUN_TAG_PREFIX):
            run_id = tag[len(_AGENTIC_SEARCH_RUN_TAG_PREFIX) :]
            SearchRun.reconcile_after_recovery(
                run_id, store=agentic_search_store.get_store(), runtime=runtime
            )
            return


_hook_manager.on_session_end.append(_reconcile_agentic_search_after_recovery)


# -- Structured-response namespace ----------------------------------------
# Schema-constrained synthesis over the core StructuredResponder (down-only
# compose). POST /v1/structured returns a JSON-Schema-validated object — the
# default 'agentic' mode after a bounded session, or an inline no-loop
# 'synthesis' mode (the former /v1/structured/fast lane, folded in).
from mewbo_api.structured import init_structured  # noqa: E402

init_structured(api, runtime=runtime)
logging.info("structured namespace registered at /v1/structured")

# Token-streaming draft synthesis (POST /v1/draft/stream).
from mewbo_api.realtime import init_realtime  # noqa: E402

init_realtime(api, runtime=runtime)
logging.info("realtime draft-stream endpoint registered at /v1/draft/stream")

# -- VCS automation namespace ----------------------------------------------
# Agent pickup for GitHub/Gitea Actions: assigning or @mentioning the bot on
# an issue/PR posts here; the endpoint binds a session to the right branch
# worktree and starts/continues the run.
from mewbo_api.vcs_pickup import init_vcs_pickup, vcs_ns  # noqa: E402

init_vcs_pickup(
    runtime,
    # Late-bound: _resolve_repo_or_404 is defined further down this module.
    lambda key, promote=False: _resolve_repo_or_404(key, promote=promote),
    project_store,
    _hook_manager,
)
api.add_namespace(vcs_ns, path="/api")
logging.info("vcs automation namespace registered at /api/automation")


def _handle_slash_command(session_id: str, user_query: str) -> tuple[dict, int] | None:
    """Handle session slash commands like /terminate and /status."""
    command = parse_core_command(user_query)
    if command == "/terminate":
        canceled = runtime.cancel(session_id)
        return {"session_id": session_id, "canceled": canceled}, 202
    if command == "/status":
        return {"session_id": session_id, **runtime.summarize_session(session_id)}, 200
    return None


def _parse_bool(value: str | None) -> bool:
    """Interpret a query param or payload value as a boolean."""
    if value is None:
        return False
    lowered = value.strip().lower()
    if not lowered:
        return False
    return lowered not in {"0", "false", "no", "off"}


def _parse_mode(value: object | None) -> str | None:
    """Normalize orchestration mode values to 'plan' or 'act'."""
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if lowered in {"plan", "act"}:
        return lowered
    return None


def _request_surface() -> str:
    """Originating client surface from ``X-Mewbo-Surface`` (shared seam).

    Thin alias for ``request_context.request_surface`` — the one implementation
    shared with the structured/realtime route modules (a back-edge-free leaf, see
    that module). Distinct from channel/vcs callers, which stamp their own
    platform/forge.
    """
    return request_surface()


def _utc_now() -> str:
    """Return current UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat()


def _build_context_payload(request_data: dict[str, object]) -> dict[str, object]:
    """Merge context and attachments into a single payload.

    Also mirrors the top-level ``mode`` ("plan"/"act") into the context
    payload so ``summarize_session`` can surface it as part of each
    session's trailing state for the console to rehydrate its plan/act
    toggle. The orchestrator still reads ``mode`` directly from the
    top-level request — this is an additional persistence path, not a
    behavioural change to the orchestration run.
    """
    payload: dict[str, object] = {}
    context = request_data.get("context")
    if isinstance(context, dict):
        payload.update(context)
    attachments = request_data.get("attachments")
    if isinstance(attachments, list):
        payload["attachments"] = attachments
    mode = _parse_mode(request_data.get("mode"))
    if mode is not None:
        payload["mode"] = mode
    return payload


def _extract_attachments(request_data: dict[str, object]) -> list[dict] | None:
    """Return the request's top-level ``attachments`` list, or ``None``.

    Same shape/validation as the ``attachments`` half of
    ``_build_context_payload`` — reused here so the orchestration call
    (``start_async``/``run_sync``) can thread the identical descriptor
    dicts onto the persisted ``user`` event (additive; the sibling
    ``context`` event keeps carrying them for LLM vision input, unchanged).
    """
    attachments = request_data.get("attachments")
    return attachments if isinstance(attachments, list) else None


def _session_attachment_map(session_id: str) -> dict[str, str]:
    """Map a session's attachment display names → the best path to render.

    Lets a user reference an uploaded file inline as ``@<filename>`` even
    though it lives in the session's attachment store (outside the project
    tree). Prefers the parsed-Markdown sidecar written at upload time, falling
    back to the raw file. Returns ``{}`` when the session has no attachments.
    """
    try:
        events = runtime.session_store.load_transcript(session_id)
    except Exception:  # noqa: BLE001 - never block expansion on a store read
        return {}
    attachments_dir = os.path.join(
        runtime.session_store.root_dir, session_id, "attachments"
    )
    mapping: dict[str, str] = {}
    for att in _iter_attachments(events):
        stored_name = att.get("stored_name")
        filename = att.get("filename") or stored_name
        if not stored_name or not filename:
            continue
        raw = os.path.join(attachments_dir, str(stored_name))
        sidecar = parsed_sidecar_path(raw)
        path = sidecar if os.path.isfile(sidecar) else raw
        if os.path.isfile(path):
            mapping[str(filename)] = path
    return mapping


def _extract_allowed_tools(context_payload: dict[str, object]) -> list[str] | None:
    """Extract MCP tool allowlist from context payload, if present.

    Three-state, and the empty case is PRESERVED rather than normalized to
    ``None``: a client that persisted ``mcp_tools: []`` advertised no MCP tools,
    which is a real ceiling, not an absent one. Collapsing it re-bound every MCP
    tool in the registry to a session that declared none. Under the permissive
    scope this path feeds, built-ins are unioned back in downstream
    (``Orchestrator.run``), so an empty list narrows MCP tools only — it never
    strands a session with no tools at all.
    """
    if not context_payload:
        return None
    mcp_tools = context_payload.get("mcp_tools")
    if isinstance(mcp_tools, list):
        return [str(t) for t in mcp_tools if t]
    return None


def _extract_denied_tools(context_payload: dict[str, object]) -> list[str] | None:
    """Extract a client-declared session/registry tool denylist, if present.

    NOT three-state, unlike :func:`_extract_allowed_tools`: deny is purely
    subtractive, so an absent key and an empty list mean the same "nothing
    denied" thing — there is no distinct ceiling to preserve by returning ``[]``
    verbatim. Mirrors ``SessionSpec.denied_tools`` (`session_spec.py`), which
    persists the same key under the same wire name.
    """
    if not context_payload:
        return None
    denied = context_payload.get("denied_tools")
    if isinstance(denied, list):
        cleaned = [str(t) for t in denied if t]
        return cleaned or None
    return None


def _extract_strict_tool_scope(context_payload: dict[str, object]) -> bool:
    """Extract the persisted ``strict_tool_scope`` flag from context, if present.

    Mirrors ``_extract_allowed_tools`` — a re-engage site (``/message``,
    ``/recover``) that derives its tool grants from persisted context should
    re-apply the SAME scoping the originating ``start_async`` call used,
    not silently default to unscoped. Absent ⇒ ``False``, i.e. unscoped, which
    is correct for every session that never asked for a tool ceiling.
    """
    return bool(context_payload.get("strict_tool_scope", False))


def _persisted_client_capabilities(context_payload: dict[str, object]) -> list[str] | None:
    """The advertised capabilities persisted on a session's context, if any.

    Read at a re-engage site so the resolved run scope sees the SAME capability
    set the session was created with (a viewer re-engaging a wiki session still
    reasons about ``wiki``). Absent ⇒ ``None``.
    """
    caps = context_payload.get("client_capabilities")
    if isinstance(caps, list):
        return [str(c) for c in caps if str(c).strip()]
    return None


def _extract_skill_instructions(context_payload: dict[str, object]) -> str | None:
    """Extract a persisted ``skill_instructions`` playbook from context, if present.

    A caller that started a session with a non-default ``skill_instructions``
    (e.g. the wiki-qa hypervisor playbook) can persist it here so a re-engage
    re-applies the SAME playbook instead of silently dropping it.
    """
    val = context_payload.get("skill_instructions")
    return str(val) if isinstance(val, str) and val else None


def _extract_session_step_budget(context_payload: dict[str, object]) -> int:
    """Persisted per-session step budget override, else the configured default.

    A caller that started a session with a narrower budget than the config
    default (e.g. the wiki-qa read-only run's cost backstop) can persist it
    here so a re-engage doesn't silently widen back to the generic default.
    """
    val = context_payload.get("session_step_budget")
    if isinstance(val, int) and val > 0:
        return val
    return int(get_config_value("agent", "session_step_budget", default=0))


# Reverse-invocation trigger subsystem (WP3). Populated by
# ``init_triggers`` at module scope; None/False until then so the read sites
# below no-op on an unconfigured (or disabled) deployment.
_trigger_service = None
_trigger_store = None
_trigger_policy = None
_triggers_enabled = False

# Mewbo Apps pipeline-run ledger tracker. Populated by ``init_apps`` (below,
# module scope), read at trigger-fire time by ``_trigger_deliver``; None until
# then so the trigger path no-ops on a deployment without the apps sub-product.
_apps_pipeline_tracker = None
# Mewbo Apps code-pipeline executor. Populated by ``init_apps``;
# also pushed to the plugin's run_pipeline seam via ``register_pipeline_runner``.
# The tracker holds it for the fire seam; this module handle lets the REST run
# endpoint reach it too (``current_pipeline_runner()`` is the plugin's path).
_apps_pipeline_runner = None


def _ask_user_tools(session_id: str, context_payload: dict[str, object]) -> list[SessionTool]:
    """The ``ask_user_question`` SessionTool, iff the client advertised for it.

    The gate is the ``ask_user`` entry in ``context.client_capabilities``
    (``X-Mewbo-Capabilities``) — the client's promise that a human is on the
    other end to render the question card and POST the answer. Headless
    drives (triggers, wiki, search, channels) never advertise it, so the
    block-until-answered tool never exists for them. Bound via
    ``extra_session_tools`` ⇒ structurally root-only (children never inherit
    that seam).
    """
    caps = context_payload.get("client_capabilities")
    if isinstance(caps, list) and ASK_USER_CAPABILITY in caps:
        return [AskUserQuestionTool(session_id)]
    return []


def _derive_tool_grants(
    session_id: str, context_payload: dict[str, object]
) -> tuple[list[str] | None, list[SessionTool]]:
    """Single seam: ``(allowed_tools, extra_session_tools)`` from a context payload.

    STRICT — raises ``ValueError`` (caller maps to 400) when ``device_tools``
    contains a malformed entry. Use this at a site that has NOT YET persisted
    *context_payload* (``POST /query``, sync ``POST /api/query``): validate
    before ``append_context_event`` so a malformed declaration 400s without
    ever poisoning the session's context. Re-drive sites that read
    ALREADY-persisted context (``/message`` re-engage, ``/recover``) must use
    :func:`_derive_tool_grants_tolerant` instead — see its docstring.

    ``O(1)`` when *context_payload* declares device tools (every ordinary
    ``/query``), else one narrowed store read — ``_device_tools`` resolves a
    payload that is SILENT about them from the session's record rather than
    reading the silence as a revocation.
    """
    allowed_tools = _extract_allowed_tools(context_payload)
    device_specs = _device_tools.specs_for(session_id, context_payload)
    extra_session_tools: list[SessionTool] = [
        ClientDeclaredTool(session_id, spec) for spec in device_specs
    ]
    # schedule_trigger is NOT injected here — it rides the ordinary
    # SessionToolRegistry (wired in init_triggers) so spawned sub-agents can
    # bind it too. ask_user_question stays root-only on this seam.
    extra_session_tools.extend(_ask_user_tools(session_id, context_payload))
    return allowed_tools, extra_session_tools


def _derive_tool_grants_tolerant(
    session_id: str, context_payload: dict[str, object]
) -> tuple[list[str] | None, list[SessionTool]]:
    """Self-healing sibling of :func:`_derive_tool_grants` for RE-DRIVES.

    ``/message`` re-engage and ``/recover`` derive grants from the session's
    PERSISTED declaration, not a fresh request body they could 400 on
    behalf of. If that persisted context was ever poisoned by a malformed
    ``device_tools`` declaration — a stale write from before the
    validate-before-persist ordering existed, or any future write path that
    doesn't validate — a hard 400 here would brick the session: every
    re-engage/recover attempt re-reads the same stored poison and 400s
    forever, with no request-body fix the client can offer (this call's body
    doesn't even carry ``device_tools``). So a malformed persisted
    declaration is DROPPED (bind zero device tools) with a logged warning,
    and the run proceeds; ``allowed_tools`` is derived independently since an
    ``mcp_tools`` failure is a different failure mode entirely.
    """
    try:
        return _derive_tool_grants(session_id, context_payload)
    except ValueError as exc:
        logging.warning(
            "Dropping malformed persisted device_tools for session {}: {}",
            session_id,
            exc,
        )
        return _extract_allowed_tools(context_payload), [
            *_ask_user_tools(session_id, context_payload),
        ]


def _deliver_user_turn(session_id: str, text: str) -> TurnDelivery:
    """Put *text* into a session as a user turn: steer a live run, or start one.

    The ONE delivery seam. ``POST .../message`` is its HTTP face, and the
    late-answer router (``ask_user.py``) is its second caller — an answer that
    arrives after its waiter departed has to reach the model exactly the way a
    typed message would, and re-deriving "re-engage an idle session" a second
    time is how the two would drift on the next persisted-context field.

    Re-engagement inherits the session's PERSISTED context (model, mode, tool
    allowlist, capabilities, cwd, budget) rather than the config defaults — a
    picker-selected model must survive it. The caller owns the terminated
    guard: refusing a dead session is an HTTP concern and both callers already
    answer it with the shared 410 envelope before reaching here.
    """
    if runtime.enqueue_message(session_id, text):
        return TurnDelivery(outcome="steered")
    last_context = _load_last_context(session_id)
    # Resolve cwd from session context (honours persisted external cwd) or
    # fall back to the per-session temp dir for sessions without a project.
    session_cwd = _resolve_session_cwd(session_id) or session_temp_dir(session_id)
    # The SPEC, not `last_context`: a session that auto-selected keeps `auto` in
    # its binding while its newest context event names the project it settled
    # on, so reading the loose key here would silently drop auto mode on the
    # first re-engage after a switch.
    reengage_spec = _session_specs.load(session_id)
    project_autoselect = is_auto_project(reengage_spec.project)
    # The SPEC for the same reason, and this one was measured. ``last_context``
    # is the NEWEST context event, not a merge, so the loose ``model`` key is
    # present only when that particular event happened to carry it — and the
    # capability re-write this seam's own callers perform does not. A session
    # created on one model then answered on another two hours later, with no
    # fallback and nothing logged, because the newest event was
    # ``{"client_capabilities": [...]}`` and an absent key reads as "no model
    # chosen" and falls through to ``llm.default_model``.
    #
    # ``SessionSpecStore.load`` is narrowed to the newest event CARRYING the
    # typed mirror, which is exactly the read that cannot miss this way. The
    # loose key stays as the fallback for a transcript written before the mirror
    # existed.
    model_name = reengage_spec.model or str(last_context.get("model", "")) or None
    budget = _extract_session_step_budget(last_context)
    max_iters = int(get_config_value("agent", "max_iters", default=30))
    # Tolerant: re-engagement reads PERSISTED context it can't 400 on
    # behalf of — a poisoned prior write self-heals (drops device tools,
    # keeps going) instead of bricking the session (review, F6).
    allowed_tools, extra_session_tools = _derive_tool_grants_tolerant(session_id, last_context)
    # RBAC applies to whoever drives this re-engage: resolve the CURRENT
    # caller's role, carrying the session's persisted tool scope as the
    # requested grants.
    scope = _run_scope(
        allowed_tools=allowed_tools,
        # The SPEC unioned with the advertisement, for the same reason the
        # ``project_autoselect`` line above reads the spec: ``last_context`` is the
        # newest context event, and a client that stamps a fixed rendering set on
        # every request has already buried the session's own capability there.
        client_capabilities=reengage_spec.run_capabilities(
            _persisted_client_capabilities(last_context)
        ),
        strict_tool_scope=_extract_strict_tool_scope(last_context),
    )
    run_id = runtime.start_async(
        session_id=session_id,
        user_query=text,
        model_name=model_name,
        approval_callback=scope.approval_callback,
        permission_policy=scope.permission_policy,
        hook_manager=_hook_manager,
        mode=_parse_mode(last_context.get("mode")),
        allowed_tools=scope.allowed_tools,
        # A client-declared deny is a session fact, not an RBAC grant, so it
        # rides straight off the persisted context rather than through
        # ``SessionScope`` — re-engagement must not silently forget it.
        denied_tools=_extract_denied_tools(last_context),
        # Re-apply persisted scope instead of silently
        # widening back to the unscoped default on re-engage — e.g. a
        # wiki-qa session's ``strict_tool_scope``/playbook survive a
        # follow-up driven through this generic endpoint too, not just
        # through ``WikiQaSession.follow_up``.
        strict_tool_scope=scope.strict_tool_scope,
        capability_mode=scope.capability_mode,
        skill_instructions=_extract_skill_instructions(last_context),
        cwd=session_cwd,
        max_iters=max_iters,
        session_step_budget=budget,
        source_platform=_request_surface(),
        extra_session_tools=extra_session_tools,
        project_autoselect=project_autoselect,
    )
    if not run_id:
        return TurnDelivery(outcome="refused")
    return TurnDelivery(outcome="started", run_id=run_id)


def _load_last_context(session_id: str) -> dict[str, object]:
    """Most-recent persisted ``context`` event payload for a session ({} if none).

    ``O(1)`` on the Mongo driver, ``O(one session)`` on the base store. Bounded by
    the TYPE, never by a count: this sits beside the spec load on ``/query``,
    ``/message``, ``/recover`` and every unattended fire, and the newest context
    event sits arbitrarily far back after a long run — a window that missed it
    would report a session with no model, no tool ceiling and no playbook, which
    is a wrong ANSWER rather than a slow one.

    Returns a COPY, because callers merge into it (``setdefault``) before
    persisting the result as the next context event; handing back the store's own
    mapping would let one caller's carry-forward mutate a cached document.
    """
    event = runtime.session_store.latest_event_of_type(session_id, "context")
    payload = event.get("payload") if event else None
    return dict(payload) if isinstance(payload, dict) else {}


def _speech_model_ids() -> frozenset[str]:
    """Every model id that is a speech route, in either direction.

    The chat picker subtracts these, and it asks the SPEECH namespace rather
    than deriving its own answer: that module already discovers the gateway's
    modes and caches the result, so a second derivation here would be a second
    thing to keep true. It falls back to the configured ids when the namespace
    is absent (a deployment without the extra) or discovery is unavailable.

    Cost class: ``O(1)`` — a cached lookup, or a handful of configured names.
    """
    try:
        from mewbo_api.speech import speech_model_ids

        discovered = speech_model_ids()
    except Exception:  # noqa: BLE001 — the model list must never fail on speech
        discovered = frozenset()
    try:
        speech = get_config().speech
    except Exception:  # noqa: BLE001 — nor on config
        return discovered
    configured = {speech.tts.model.strip(), speech.stt.model.strip()}
    return discovered | frozenset(name for name in configured if name)


def _extract_fallback_models(context_payload: dict[str, object]) -> tuple[str, ...] | None:
    """Read an opt-in fallback model list from the request context.

    ``None`` defers to the configured fallback policy; a non-empty list opts
    this run into cross-model fallback in the given order.
    """
    if not context_payload:
        return None
    raw = context_payload.get("fallback_models")
    if isinstance(raw, list):
        models = tuple(str(m).strip() for m in raw if str(m).strip())
        return models or None
    return None


# The ONE reader/writer of a session's durable purpose binding. All four
# collaborators are LATE-BOUND lambdas rather than bound methods captured at
# import: the test suite swaps ``runtime`` wholesale for a temp-dir store, and a
# bound ``runtime.session_store.load_transcript`` captured here would keep serving
# the store that existed at import — a spec read that silently answers from the
# wrong session. Same late-binding reason as the ``_resolve_repo_or_404`` lambda
# above. ``load_tags`` is what lets the store classify a spec-less session by the
# signal the classifier trusts most, on BOTH the creation and reconstruction legs.
# ``latest_event_of_type`` is what keeps the binding read — the first thing every
# ``/query``, ``/message``, ``/recover`` and unattended fire does — bounded to the
# one context event it needs; without it the store degrades to the transcript
# scan, which is correct and costs the whole session.
_session_specs = SessionSpecStore(
    load_transcript=lambda session_id: runtime.session_store.load_transcript(session_id),
    append_context_event=lambda session_id, payload: runtime.append_context_event(
        session_id, payload
    ),
    load_tags=lambda session_id: runtime.session_store.tags_for_session(session_id),
    latest_event_of_type=lambda session_id, event_type, payload_key: (
        runtime.session_store.latest_event_of_type(
            session_id, event_type, payload_key=payload_key
        )
    ),
)

# The ONE reader of a session's client-declared device tools, late-bound to the
# store for the same reason ``_session_specs`` is. It reads the newest context
# event that CARRIES the declaration rather than the newest context event, so a
# writer with no reason to know device tools exist — an approved plan's
# ``{"mode": "act"}``, a recovery re-inject, a fork's provenance stamp — cannot
# de-register them by staying silent.
_device_tools = DeviceToolBinding(
    latest_event_of_type=lambda session_id, event_type, payload_key: (
        runtime.session_store.latest_event_of_type(
            session_id, event_type, payload_key=payload_key
        )
    ),
)


def _populate_worktree_context(project_name: str, context_payload: dict) -> None:
    """If *project_name* refers to a managed worktree, set ``repo``/``branch``.

    No-op for config-defined or non-worktree managed projects. Mutates
    ``context_payload`` in place.
    """
    if not project_name.startswith("managed:"):
        return
    vpid = project_name[len("managed:") :]
    proj = project_store.get_project(vpid)
    if proj is None or not proj.is_worktree:
        return
    parent = (
        project_store.get_project(proj.parent_project_id)
        if proj.parent_project_id
        else None
    )
    if proj.branch:
        context_payload.setdefault("branch", proj.branch)
    if parent is not None:
        context_payload.setdefault("repo", parent.name)


def _requested_project(request_data: dict[str, object]) -> str | None:
    """The project key a request names, top-level or under ``context``.

    The ONE reader of that wire position, so a caller cannot be reading
    ``request["project"]`` while its sibling reads ``request["context"]["project"]``.
    """
    for candidate in (request_data.get("project"), _request_context(request_data).get("project")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _request_context(request_data: dict[str, object]) -> dict[str, object]:
    """The request's ``context`` object, or an empty one."""
    ctx = request_data.get("context")
    return ctx if isinstance(ctx, dict) else {}


def _resolve_project_cwd(request_data: dict[str, object]) -> str | None:
    """Resolve a project from the request to its filesystem path.

    Delegates the whole name→directory decision to the ONE
    :class:`ProjectCatalog`, so a configured name, a ``managed:<project_id>``,
    a worktree and a registered repository slug all resolve here by the same
    rule this app applies everywhere else.

    Returns ``None`` when no project is named AND when the named project is the
    ``auto`` sentinel: that is not a directory, it is a declaration that one has
    not been chosen yet, so the caller falls through to the session temp dir
    exactly as an absent project does. Raises ``ValueError`` (a
    :class:`ProjectResolutionError`, which IS one) when a project identifier is
    given but cannot be turned into a usable directory — every call site already
    branches on ``ValueError`` and keeps doing so.
    """
    project_name = _requested_project(request_data)
    if project_name is None or is_auto_project(project_name):
        return None
    catalog = _catalog()
    try:
        return catalog.resolve(project_name)
    except ProjectResolutionError as exc:
        if exc.code != "unavailable":
            raise
        # A MANAGED project's directory is Mewbo's own to create — the store row
        # is the authority, not the filesystem — so "the directory is missing"
        # is a directory we simply make. A configured or repository path belongs
        # to the operator and stays a refusal.
        entry = catalog.find(project_name)
        if entry is None or entry.kind not in {"managed", "worktree"} or not entry.path:
            raise
        os.makedirs(entry.path, exist_ok=True)
        return entry.path


def _scoping_cwd(project_key: str | None) -> str | None:
    """Best-effort directory for a read-only endpoint's ``?project=`` parameter.

    The catalog-listing endpoints scope to a project and have always IGNORED an
    unresolvable one rather than refusing — a browsable list is more useful
    narrow-or-wide than 400'd. What they silently ignored, though, included every
    ``managed:<project_id>``: they resolved CONFIGURED projects only, so scoping
    either list to a worktree-backed session fell back to the unscoped list with
    no error to read. Delegating fixes that; a genuinely unknown key still
    resolves to no scope rather than an error.

    Deliberately NOT :func:`_resolve_project_cwd`: that one creates a managed
    project's missing directory, which a GET has no business doing.
    """
    if not project_key:
        return None
    entry = _catalog().find(project_key)
    return entry.path if entry is not None and entry.runnable else None


class ExternalCwdPolicy:
    """Gate and validate caller-supplied host paths for session working directories.

    Resolution order (applied by :meth:`resolve`):
    1. Explicit ``cwd`` from the request (top-level or ``context.cwd``) wins over
       a project-derived path when ``api.allow_external_cwd`` is on, or when the
       server already knows the directory through the session binding or catalog.
       The catalog leg is reached only while the flag is off and the path is not
       the session's own, so currently-working requests retain their O(1) path;
       catalog ownership is O(collection) and these handlers are budgeted O(1).
    2. Project-derived path via :func:`_resolve_project_cwd` — unchanged
       existing behaviour.
    3. ``None`` — caller falls back to ``session_temp_dir``.

    The flag and validation are co-located here so the route functions stay
    thin; DI with ``AppConfig`` keeps this testable without touching module
    globals.
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        catalog: Callable[[], ProjectCatalog] | None = None,
    ) -> None:
        """Initialise with the config and a live catalog accessor when available."""
        self._enabled: bool = config.api.allow_external_cwd
        self._catalog: Callable[[], ProjectCatalog] | None = catalog

    @staticmethod
    def _extract_cwd(request_data: dict[str, object]) -> str | None:
        """Extract the caller-supplied ``cwd`` from request body or context."""
        raw = request_data.get("cwd")
        if not raw or not isinstance(raw, str):
            ctx = request_data.get("context")
            if isinstance(ctx, dict):
                raw = ctx.get("cwd")
        if not raw or not isinstance(raw, str):
            return None
        return raw.strip() or None

    def _server_knows(self, raw_cwd: str, bound_cwd: str | None) -> bool:
        """Whether *raw_cwd* is a directory this server already issued or owns."""
        # The binding is O(1), needs no store, and distinguishes a session's echoed
        # directory from a new host-path claim before the catalog's O(collection) walk.
        if bound_cwd and ProjectCatalog.same_dir_key(raw_cwd) == ProjectCatalog.same_dir_key(
            bound_cwd
        ):
            return True
        if self._catalog is None:
            return False
        try:
            return self._catalog().owns_path(raw_cwd)
        except Exception:
            # A filter that cannot be applied must refuse rather than fail open: a
            # dead project store costs a refusal, never an unchecked host path.
            return False

    def resolve(
        self,
        request_data: dict[str, object],
        *,
        bound_cwd: str | None = None,
    ) -> tuple[str | None, tuple[dict, int] | None]:
        """Resolve a caller-supplied working directory when one is present.

        Returns ``(cwd, None)`` on success or ``(None, error_response)`` when
        the caller provided a ``cwd`` that failed validation.
        On success ``cwd`` may be ``None`` — the caller should fall back to
        ``session_temp_dir`` or another default.
        """
        raw_cwd = self._extract_cwd(request_data)
        if raw_cwd is None:
            # No explicit cwd → delegate to project resolution.
            return None, None
        if not self._enabled and not self._server_knows(raw_cwd, bound_cwd):
            return None, (
                {
                    "error": {
                        "code": 403,
                        "reason": (
                            "api.allow_external_cwd is disabled; "
                            "explicit cwd is not permitted."
                        ),
                    }
                },
                403,
            )
        # A server-known project can still have been reaped, so validate every
        # accepted path rather than handing a vanished directory to the run.
        if not os.path.exists(raw_cwd):
            return None, (
                {
                    "error": {
                        "code": 400,
                        "reason": f"cwd path does not exist: {raw_cwd}",
                    }
                },
                400,
            )
        if not os.path.isdir(raw_cwd):
            return None, (
                {
                    "error": {
                        "code": 400,
                        "reason": f"cwd path is not a directory: {raw_cwd}",
                    }
                },
                400,
            )
        # Root-derivation seam. This validated external cwd is the session's
        # working directory, and thus (once workspace enforcement is enabled) the
        # workspace-containment ROOT: it flows unchanged to ``ToolUseLoop.cwd``,
        # which builds the ``WorkspaceContainment(root=cwd)`` for the run. External
        # workspace managers anchor sessions at their worktree root, so the anchored
        # cwd already IS the project root — no separate enclosing-project derivation
        # is needed in v1. The D5 "enclosing project root" refinement (git-toplevel /
        # RepoIdentity) would plug in HERE, narrowing the returned path before it
        # becomes the root.
        return raw_cwd, None


class RunReadinessGate:
    """Refuses a run this process is not yet able to serve, instead of accepting it.

    A restarting worker can answer HTTP before its configuration has finished
    resolving, so runs were accepted seconds before the first model call died. The
    run was already persisted by then, so the failure read as a credential defect
    rather than as the restart window it actually was — a burned session per
    request, and a misleading diagnosis on top.

    **What this deliberately does NOT do: guess at credentials.** An empty
    ``llm.api_key`` is indistinguishable from the outside between "not loaded yet"
    (transient, retry) and "never configured" (permanent, fix your config), and a
    gate that refuses the second while SAYING "retry shortly" is actively lying
    about a misconfiguration. Credentials also legitimately arrive by routes this
    process cannot enumerate — a proxy at ``llm.api_base`` that authenticates on
    the caller's behalf, a provider env var, an instance role. So the only signal
    used is the one that is unambiguous and process-local: whether the config
    LOADS at all. That is narrower than "predict whether this run will succeed",
    and narrow is the point — a run that fails for a configured reason must fail
    with that reason, not behind a readiness message that misdirects.

    Two properties make it safe in front of every accept:

    * **It latches.** Readiness is monotonic within a process, so only the first
      request after start pays for a probe.
    * **Only a config-load failure refuses.** Every other error inside the probe
      reports READY — a gate whose own bug can refuse traffic is worse than the
      window it closes.

    The config accessor is injected so a test drives both arms without touching
    global config.
    """

    def __init__(self, config_reader: Callable[[], AppConfig] = get_config) -> None:
        """Bind the config accessor; readiness is probed lazily on first use."""
        self._config_reader = config_reader
        self._ready = False

    def check(self) -> tuple[dict, int] | None:
        """``None`` when the process can serve a run, else a retryable 503 to return.

        The structured envelope carries ``retryable: true`` — the caller SHOULD come
        back. Refusing up front preserves that distinction; accepting the run and
        failing it a few seconds later destroys it.
        """
        if self._is_ready():
            return None
        return (
            {
                "error": {
                    "code": 503,
                    "reason": (
                        "Server is still starting up and cannot accept runs yet; retry shortly."
                    ),
                    "retryable": True,
                }
            },
            503,
        )

    def _is_ready(self) -> bool:
        """Probe once, then latch. Only an unloadable config reports not-ready."""
        if self._ready:
            return True
        try:
            config = self._config_reader()
        except Exception:  # noqa: BLE001 - THE signal: config has not resolved yet
            return False
        try:
            self._ready = bool((config.llm.default_model or "").strip())
        except Exception:  # noqa: BLE001 - a probe bug must never refuse traffic
            self._ready = True
        return self._ready


_run_readiness = RunReadinessGate()


def _resolve_skill_instructions(
    request_data: dict[str, object],
    user_query: str,
    context_payload: dict[str, object] | None = None,
) -> str | None:
    """Resolve skill instructions from request payload or context.

    Checks (in order):
    1. Top-level ``"skill"`` field in request body.
    2. ``"skill"`` field inside the ``context`` object.
    3. Falls back to None (orchestrator can still detect ``/skill-name`` queries).
    """
    from mewbo_core.tooling.skills import SkillRegistry, activate_skill

    skill_name = request_data.get("skill")

    # Check context.skill (from web console SessionContext).
    if not skill_name and context_payload:
        ctx = request_data.get("context")
        if isinstance(ctx, dict):
            skill_name = ctx.get("skill")

    if isinstance(skill_name, str) and skill_name.strip():
        registry = SkillRegistry()
        registry.load()
        skill = registry.get(skill_name.strip())
        if skill is not None:
            args = str(request_data.get("skill_args", ""))
            instructions, _ = activate_skill(skill, args)
            return instructions

    return None


notification_service = NotificationService(notification_store, runtime.session_store)

# Channel adapters (Nextcloud Talk, etc.) — no-ops if none configured
from mewbo_api.channels.routes import init_channels  # noqa: E402

init_channels(
    app,
    runtime,
    _hook_manager,
    _config,
    # Late-bound for the same reason ``_session_specs``' collaborators are: the
    # suite swaps ``runtime`` and ``project_store`` wholesale, so a catalog or a
    # bound resolver captured at import would answer from stores this process no
    # longer holds.
    project_catalog=_catalog,
    resolve_session_cwd=lambda session_id: _resolve_session_cwd(session_id),
)

# Wiki backend (opt-in via mewbo-api[wiki] extras).
from mewbo_api.wiki import init_wiki  # noqa: E402

# Pass the shared hook manager so the wiki-qa hypervisor's session-end finalizer
# can emit the terminal ``complete`` event + reconcile the answer snapshot
# (the QA counterpart to indexing's wiki_finalize tool).
init_wiki(app, runtime, hook_manager=_hook_manager)

# Speech backend (opt-in via the mewbo-speech package). Mounts /api/speech* when
# the optional capability library resolves and returns False silently otherwise,
# so the mount's own presence IS the availability signal a client reads — there
# is no second "speech is enabled" registry to keep in step with reality.
from mewbo_api.speech import init_speech  # noqa: E402

init_speech(api)

# Product-wide repository registry. Registered HERE rather than from
# ``init_wiki`` deliberately: that function returns early on an install without
# the ``wiki`` extra, and agentic tasks run on exactly such a base install — a
# registry mounted from there would be absent precisely where it is needed. Its
# optional wiki/credential usage projection degrades to null instead.
from mewbo_api.git_repositories_routes import init_git_repositories  # noqa: E402

init_git_repositories(app, runtime, project_store=project_store)


# -- Reverse-invocation triggers (WP3) -------------------------
# The durable peer of the AgentHypervisor: a background watcher that fires
# time/cron/CI/PR/webhook triggers and re-invokes the sessions that armed them.
from mewbo_api.triggers import (  # noqa: E402
    TriggerFireContext,
    TriggerService,
    build_forge_client_factory,
    init_trigger_routes,
)


def _reengage_idle_session(
    session_id: str,
    message: str,
    *,
    source_platform: str,
    allowed_tools_override: list[str] | None = None,
    strict_scope_override: bool | None = None,
    scope: SessionScope | None = None,
) -> str:
    """Start a fresh run on an IDLE session inheriting its persisted context.

    The shared idle-start idiom for a fired trigger AND an app kick-off (builder /
    repair): re-inject capability-gating context (so a gated session doesn't wake
    TOOLS-MISSING), derive tool grants from the last persisted context, and
    ``start_async``. The two overrides let a scoped pipeline fire replace the
    permissive grants with its ``tools_allowlist``.

    ``scope`` carries the arming principal's role ceiling for a fired trigger
    (see :func:`_trigger_fire_scope`): its ``approval_callback`` /
    ``capability_mode`` / ``permission_policy`` layer over whatever tool allowlist
    the pipeline/persisted context resolved. ``None`` (an app kick-off, or a
    trigger armed with no captured authority) keeps today's ambient full power.
    Returns the ``run_id`` (``""`` when the runtime refused, e.g. a run is live).
    """
    # Read the binding BEFORE ``reinject_recovery_context``, which appends an event
    # carrying ONLY the gating keys — reading after it returns a payload with no
    # model, no tool ceiling and no playbook, which is how an unattended wake used
    # to silently drop everything but the capabilities.
    spec = _session_specs.load(session_id)
    runtime.reinject_recovery_context(session_id)
    gating = _load_last_context(session_id)

    # Per-fire capability re-derivation: ``allowed_tools`` was already recomputed
    # per fire, capabilities were not — so ONE interactive turn that advertised
    # ``ask_user`` left every later scheduled fire able to bind a tool that BLOCKS
    # until a human answers, with no human attached. Re-derive from the purpose and
    # make that the newest context event, carrying any non-spec gating key
    # (``structured_workspace``) forward so the reinject above is not undone.
    fire_context = spec.to_context_payload(capabilities=spec.unattended_capabilities())
    for key, value in gating.items():
        if key not in SessionSpec.SPEC_OWNED_CONTEXT_KEYS:
            fire_context.setdefault(key, value)
    runtime.append_context_event(session_id, fire_context)

    allowed_tools, extra_session_tools = _derive_tool_grants_tolerant(session_id, fire_context)
    strict_tool_scope = spec.strict_tool_scope
    if allowed_tools_override is not None:
        allowed_tools = allowed_tools_override
    if strict_scope_override is not None:
        strict_tool_scope = strict_scope_override
    return (
        runtime.start_async(
            session_id=session_id,
            user_query=message,
            model_name=spec.model,
            # An opted-in ladder is part of the binding: reverting to config policy
            # here left every unattended wake as defenceless as the run that armed it.
            fallback_models=spec.fallback_models,
            approval_callback=scope.approval_callback if scope else auto_approve,
            permission_policy=scope.permission_policy if scope else None,
            hook_manager=_hook_manager,
            mode=spec.mode,
            allowed_tools=allowed_tools,
            strict_tool_scope=strict_tool_scope,
            capability_mode=scope.capability_mode if scope else "all",
            skill_instructions=spec.skill_instructions,
            cwd=spec.cwd or _resolve_session_cwd(session_id) or session_temp_dir(session_id),
            max_iters=int(get_config_value("agent", "max_iters", default=30)),
            session_step_budget=spec.session_step_budget
            or int(get_config_value("agent", "session_step_budget", default=0)),
            source_platform=source_platform,
            extra_session_tools=extra_session_tools,
        )
        or ""
    )


def _trigger_fire_scope(trigger_id: str) -> SessionScope | None:
    """Resolve a firing trigger's captured authority into a run scope, or ``None``.

    A fired trigger re-engages its session out-of-band, so it must inherit the
    arming principal's role ceiling rather than ambient full power. Reads the
    trigger's optional ``authority`` snapshot and resolves it through the SAME
    :class:`SessionScopeResolver` a live request uses. ``None`` — no trigger
    store, an unknown trigger, or a trigger armed with no captured authority
    (auth disabled, or armed by an admin) — leaves the fire path on today's
    ambient behavior, byte-identical.
    """
    if _trigger_store is None or not trigger_id:
        return None
    try:
        trigger = _trigger_store.get(trigger_id)
    except Exception:  # noqa: BLE001 — a store read must never break trigger delivery
        return None
    if trigger is None or trigger.authority is None:
        return None
    return _session_scope.resolve(
        principal_from_authority(trigger.authority), requested_allowed_tools=None
    )


def _trigger_deliver(ctx: TriggerFireContext) -> bool:
    """Deliver a fired trigger's wake message into its session (the app seam).

    Encapsulates the re-engage decision the ``TriggerService`` delegates so the
    service stays free of backend-private helpers (no import cycle). Mirrors the
    ``/message`` re-engage path — a running session is steered (``message``) or
    left for the next tick (``start`` can't open a 2nd concurrent run); an idle
    session starts a fresh run inheriting its persisted context. Returns True
    when delivered, False when the session was busy (the service re-arms).

    ``ctx.trigger_id`` lets the Mewbo Apps ledger attribute the fire: when the
    firing trigger belongs to an app pipeline, the tracker OPENS a
    ``PipelineRun`` on a successful delivery and scopes an idle-start run to the
    pipeline's ``tools_allowlist`` (least privilege). A non-app fire
    is a no-op. (The tracker still consumes ``trigger_id`` positionally, so the
    apps package needs no change for the structured-payload switch.)
    """
    session_id, wake, action, trigger_id = (
        ctx.session_id,
        ctx.wake,
        ctx.action,
        ctx.trigger_id,
    )
    # A fired ``mode="code"`` pipeline runs its ENGINE synchronously
    # (deterministic, no LLM call) and is fully handled here — the maintainer
    # session is never woken. Checked FIRST so a code fire short-circuits before any
    # re-engage decision; a non-code / non-app fire returns False and falls through
    # to the unchanged agentic path below.
    if _apps_pipeline_tracker is not None and _apps_pipeline_tracker.run_code_pipeline_fire(
        trigger_id, now=datetime.now(timezone.utc)
    ):
        return True
    if runtime.is_running(session_id):
        if action != "message":
            return False  # one-live-run-per-session: action="start" can't stack a run
        delivered = runtime.enqueue_message(session_id, wake)
        if delivered and _apps_pipeline_tracker is not None:
            _apps_pipeline_tracker.open_run(session_id, trigger_id)
        return delivered
    # Idle → re-engage. I3: an app pipeline fire with a non-empty tools_allowlist
    # runs under that authoritative least-privilege scope (+ app_data); an empty
    # allowlist / non-app fire keeps the session's derived grants.
    override_allow: list[str] | None = None
    override_strict: bool | None = None
    if _apps_pipeline_tracker is not None:
        pipeline_scope = _apps_pipeline_tracker.pipeline_scope(trigger_id)
        if pipeline_scope is not None:
            override_allow, override_strict = pipeline_scope
    run_id = _reengage_idle_session(
        session_id,
        wake,
        source_platform="trigger",
        allowed_tools_override=override_allow,
        strict_scope_override=override_strict,
        # RBAC ceiling of whoever armed the trigger (None ⇒ ambient, today's
        # behavior): layers over the pipeline's least-privilege allowlist above.
        scope=_trigger_fire_scope(trigger_id),
    )
    if run_id and _apps_pipeline_tracker is not None:
        _apps_pipeline_tracker.open_run(session_id, trigger_id)
    return bool(run_id)


def init_triggers(app_, runtime_: SessionRuntime, config) -> None:
    """Wire the trigger subsystem (mirrors ``init_channels``, called once).

    Management routes + the terminate cascade are ALWAYS registered so the
    console can list/arm/pause/cancel; the firing watcher starts only when
    ``triggers.enabled`` (a disabled deployment can hold armed triggers that
    wait for the watcher to be turned on).
    """
    global _trigger_service, _trigger_store, _trigger_policy, _triggers_enabled  # noqa: PLW0603
    tcfg = config.triggers
    from mewbo_core.triggers.session_tool import register_schedule_trigger_provider
    from mewbo_core.triggers.store import create_trigger_store

    store = create_trigger_store()
    policy = tcfg.to_policy()
    service = TriggerService(
        runtime=runtime_,
        store=store,
        policy=policy,
        config=tcfg,
        forge_client_factory=build_forge_client_factory(config),
        deliver=_trigger_deliver,
    )
    _trigger_service = service
    _trigger_store = store
    _trigger_policy = policy
    _triggers_enabled = bool(tcfg.enabled)
    init_trigger_routes(
        api,
        service=service,
        store=store,
        policy=policy,
        runtime=runtime_,
    )
    # Cascade: terminating a session cancels every trigger still waiting to wake
    # it. The returned count is summed into the /terminate response's
    # ``cancelled_triggers``.
    runtime_.register_on_terminate(lambda sid: store.cancel_for_session(sid))
    if tcfg.enabled:
        # Down-only push: hand the store+policy to core so every
        # Orchestrator registers the schedule_trigger SessionToolRegistry
        # factory, rather than a root-only extra_session_tools injection, so a
        # spawned sub-agent (the app-builder) whose allowlist names it can bind
        # it too. Gated on triggers.enabled: arming a trigger no watcher will
        # ever fire would only mislead the agent.
        register_schedule_trigger_provider(store, policy)
        service.start()
        logging.info("Trigger watcher started (triggers.enabled=true)")
    else:
        logging.info("Trigger routes registered; watcher idle (triggers.enabled=false)")


init_triggers(app, runtime, _config)


# -- Custom system instructions ----------------------------------------------
# Operator-authored Jinja template appended to every session's system prompt,
# branching on client/surface (InstructionContext). REST + API key only —
# deliberately no agent-facing SessionTool/MCP surface (see routes.py).
from mewbo_api.system_instructions import (  # noqa: E402
    InstructionValueSources,
    init_system_instructions_routes,
)


def init_system_instructions() -> None:
    """Wire the custom-system-instructions subsystem (mirrors ``init_triggers``).

    Two injected collaborators: the singleton document store, and the
    ``InstructionValueSources`` edge that resolves what this deployment's
    tools/capabilities/projects/models actually ARE (it takes the same managed
    ``project_store`` ``GET /api/projects`` lists; its other sources — config,
    tool registry, plugin fan-out — default to the live accessors).
    """
    from mewbo_core.system_instructions import create_system_instructions_store

    init_system_instructions_routes(
        api,
        store=create_system_instructions_store(),
        value_sources=InstructionValueSources(project_store=project_store),
    )


init_system_instructions()


# -- Mewbo Apps (LLM-built, trigger-maintained mini apps) --------------------
# API-only sub-product (nothing importable from core/CLI paths): manifest +
# version + pipeline-run + data stores, the app lifecycle, and the REST surface,
# plus the agent-side plugin root pushed at startup. Wired after triggers because
# the lifecycle arms pipeline triggers via the trigger store + policy that
# ``init_triggers`` built above.
from mewbo_api.apps.lifecycle import (  # noqa: E402
    AppLifecycle,
    RuntimeSessionBackend,
)
from mewbo_api.apps.models import AppSpec  # noqa: E402
from mewbo_api.apps.pipeline_runner import AppPipelineRunner  # noqa: E402
from mewbo_api.apps.pipeline_tracker import AppPipelineRunTracker  # noqa: E402
from mewbo_api.apps.plugin import PLUGIN_ROOT as _APPS_PLUGIN_ROOT  # noqa: E402
from mewbo_api.apps.plugin.runtime import (  # noqa: E402
    register_app_submitter,
    register_pipeline_ledger,
    register_pipeline_runner,
)
from mewbo_api.apps.routes import (  # noqa: E402
    AppsRoutesController,
    init_apps_routes,
)
from mewbo_api.apps.staging import AppStagingArea, AppStagingError  # noqa: E402
from mewbo_api.apps.store import (  # noqa: E402
    get_app_data_store,
    get_app_store,
    get_pipeline_run_store,
)
from mewbo_api.apps.tokens import AppReadTokenSigner  # noqa: E402


def _build_apps_token_signer(configured_secret: str, master_token: str) -> AppReadTokenSigner:
    """Build the apps render-token signer from config, falling back to the master token.

    A dedicated ``api.apps_token_secret`` lets an operator sign (and rotate) the
    short-lived app render tokens independently of the master token. When it is
    unset the signer falls back to the master token — with ONE startup warning, so
    a deployment that never rotates is honest about sharing the secret rather than
    silently doing so. Pure + injected (no module reads) so the fallback branch is
    unit-testable without touching global config.
    """
    secret = (configured_secret or "").strip()
    if secret:
        return AppReadTokenSigner(secret=secret)
    logging.warning(
        "apps token secret falls back to master token — set api.apps_token_secret "
        "to rotate app render-token signing independently"
    )
    return AppReadTokenSigner(secret=master_token)


class _RuntimeAppRunStarter:
    """Adapts the runtime idle-start idiom to the ``AppRunStarter`` Protocol.

    Starts a fresh run on an idle builder/maintainer session (builder kick-off at
    ``create_draft``, repair run on a failed pipeline); if a run is already live
    (a kick-off racing an in-flight run) the message is steered in instead.
    Best-effort — a start failure is logged, never raised into the lifecycle /
    tracker that called it.
    """

    def start_app_run(self, session_id: str, message: str) -> str:
        """Start (or steer) a run on *session_id*; return the landed signal.

        ``"steered"`` (a run was live, so the message was enqueued), ``"started"``
        (an idle session began a fresh run), or ``"refused"`` (the runtime declined
        the start, or a start/steer failure) — so the ``/fire`` route reports what
        actually happened. Best-effort: a raised exception logs and reports
        ``"refused"``, never propagating into the create/close/​fire path.
        """
        try:
            if runtime.is_running(session_id):
                return "steered" if runtime.enqueue_message(session_id, message) else "refused"
            return "started" if _reengage_idle_session(
                session_id, message, source_platform="apps"
            ) else "refused"
        except Exception:  # noqa: BLE001 — a kick-off failure must not break create/close
            logging.warning("Apps run start failed for session {}", session_id, exc_info=True)
            return "refused"


def _resolve_app_workspace_cwd(app: AppSpec) -> str | None:
    """The filesystem cwd a code pipeline's ``ctx`` (glob/read_file) is scoped to.

    Reuses the maintainer session's OWN cwd resolution (``_resolve_session_cwd``):
    a ``shared`` workspace wrote a ``project`` context event on the maintainer, so
    this resolves the SAME project cwd a trigger re-engage would.

    **The fallback is the app's STAGING directory, not the session temp dir.** An
    ``own``-scoped app has no project, and so did a ``shared`` one whose project
    stopped resolving — both landed in ``session_temp_dir``, which is the wrong
    directory twice over:

    * **Nothing ever puts anything there.** An agentic capture stage told to
      "write snapshots into the app workspace" writes them where the app's files
      demonstrably ARE — the staging directory ``get_app``/``stage`` materializes
      into. A two-stage app (agentic captures files, code pipeline ingests them)
      therefore wrote to one directory and read from another, every glob matched
      nothing, and the run reported success. That is not a hypothetical: it is
      how a live app served an empty collection while holding 7 MB of fresh
      snapshots on disk.
    * **It does not survive a restart.** ``/tmp/mewbo/sessions`` is container
      local while the staging root is a volume, so even a correct handoff was
      destroyed by the next deploy — and the two stages are deliberately minutes
      apart.

    Pointing at staging makes the durable directory the one pipelines read, and
    makes ``ctx`` see the same files the maintainer edits. Note the consequence:
    ``submit_app`` reads that whole directory, so a pipeline writing data files
    beside its source will carry them into the next version's bundle.

    ``None`` when the app has no maintainer yet (a still-building draft) — the
    runner then treats the workspace as empty (``glob`` → ``[]``, ``read_file`` →
    a clean error), never reaching outside a scope. The directory is NOT created
    or materialized here: a missing one globs empty, and re-materializing the
    bundle on every run would overwrite a freshly captured file with the older
    copy stored in the manifest.
    """
    maintainer = app.maintainer_session_id
    if not maintainer:
        return None
    project_cwd = _resolve_session_cwd(maintainer)
    if project_cwd:
        return project_cwd
    try:
        return str(AppStagingArea(session_id=maintainer).directory_for(app.app_id))
    except AppStagingError as exc:
        # A stored app_id that escapes its session dir — refused rather than
        # silently relocated. An empty workspace is the honest degradation.
        logging.warning(
            "apps: staging workspace refused for app {} ({}); pipelines run with no workspace",
            app.app_id,
            exc,
        )
        return None


# The X-Mewbo-Surface value stamped on a code-pipeline's ctx.llm run so its
# session/Langfuse provenance is filterable as apps-pipeline LLM spend (a distinct
# source_platform) while reusing the structured:fast recording machinery.
_APPS_PIPELINE_LLM_SURFACE = "apps-pipeline"


class BoundedSyncRetry:
    """Bounded retry for a SYNCHRONOUS one-shot model call, on the shared classifier.

    The apps ``ctx.llm`` step is the one LLM call in the product with no
    :class:`ToolUseLoop` behind it — one no-loop ``StructuredSynthesizer``
    round-trip — so it inherited no retry of any kind and a transient 502 killed
    the whole pipeline run. (The runner's own single retry is a SCHEMA reask; it
    never re-issues a call that failed in transport.)

    This deliberately reuses ``RetryStrategy`` for the two parts that carry the
    decision — ``classify`` (a pure staticmethod over the exception) and
    ``backoff`` (full-jitter over the configured knobs) — rather than its
    ``run()``. That method is an ASYNC state machine typed to chat ``AIMessage``
    turns and needs ``emit``/``compact`` callbacks over a message history, none of
    which exist for a synchronous schema round-trip that returns a dict. Driving
    the taxonomy from one place is what matters; wrapping a chat-turn machine
    around a non-chat call would have meant faking three collaborators.

    Only ``RETRY_SAME`` is retried. ``SWITCH_MODEL`` has nowhere to switch to (the
    synthesis seam takes no ladder) and ``FATAL`` must never be retried, so both
    re-raise immediately rather than burning the budget on a decided failure.
    """

    def __init__(self, strategy_factory: Callable[[], Any] | None = None) -> None:
        """Bind the strategy factory; the strategy itself is built per call.

        Built per call so the retry knobs are hot-read from config exactly as every
        other retry site reads them, and so one pipeline's transient failures never
        consume a budget shared with the next.
        """
        self._strategy_factory = strategy_factory or RetryStrategy.from_config

    def call(self, invoke: Callable[[], Any]) -> Any:
        """Run *invoke*, retrying transient failures; re-raise anything decided."""
        strategy = self._strategy_factory()
        attempts = max(1, int(strategy.primary_retries))
        for attempt in range(attempts):
            try:
                return invoke()
            except Exception as exc:  # noqa: BLE001 - re-raised unless transient
                decision = RetryStrategy.classify(exc)
                if not decision.retryable or attempt == attempts - 1:
                    raise
                delay = strategy.backoff(attempt, decision.retry_after)
                logging.warning(
                    "apps ctx.llm call failed ({}, {}); retrying in {:.1f}s",
                    decision.error_type,
                    decision.reason,
                    delay,
                )
                time.sleep(delay)
        raise RuntimeError("unreachable: bounded retry exhausted without raising")


_apps_llm_retry = BoundedSyncRetry()


def _apps_llm_invoke(prompt: str, output_schema: dict, max_tokens: int) -> dict:
    """The bounded ``ctx.llm`` step's model round-trip (Wave 5), session-recorded.

    Reuses the DEDICATED structured-output surface the ``/v1/structured`` 'synthesis'
    mode rides — the in-process :class:`~mewbo_api.structured.synthesis.SynthesisRunner`,
    which drives ONE no-loop ``StructuredSynthesizer`` round-trip (an arbitrary
    JSON-schema dict in → a schema-validated dict out, no ``ToolUseLoop``) AND wraps
    it in the write-behind :class:`RealtimeSessionRecorder`. So each ``ctx.llm`` call
    becomes a session-backed, Langfuse-traced structured run: a code pipeline's LLM
    spend gets the SAME observability + provenance an interactive synthesis run has,
    for free. Called DIRECTLY in-process (never HTTP-to-self, never a fresh
    ``build_chat_model`` client).

    ``surface="apps-pipeline"`` stamps a distinct ``source_platform`` on the run so
    pipeline LLM spend is filterable — WITHOUT the surgery a custom ``session_type``
    tag would need (``SynthesisRunner`` hardcodes the ``structured:fast`` base tag,
    and a new tag prefix would also have to be taught to core's provenance
    classifier, else it reclassifies to the ``user`` origin fallback). Returns the
    model's validated dict (the runner re-checks it against ``output_schema`` and
    owns the one-retry / budget / cache). ``max_tokens`` drives the runner's budget
    accounting ONLY — the synthesis seam accepts no per-call token cap (documented,
    not silently dropped). ``runtime is None`` degrades to trace-only, never a crash.
    """
    from mewbo_api.structured.synthesis import SynthesisRunner  # noqa: PLC0415 - edge dep

    result = _apps_llm_retry.call(
        lambda: SynthesisRunner(runtime=runtime).run(
            query=prompt,
            schema=output_schema,
            workspace=None,  # un-grounded: a pipeline prompt carries its own context
            model=None,  # the configured default synthesis model
            surface=_APPS_PIPELINE_LLM_SURFACE,
        )
    )
    output = result["output"]
    return output if isinstance(output, dict) else {"result": output}


def init_apps() -> None:
    """Wire the Mewbo Apps sub-product (mirrors ``init_triggers``, called once).

    Registers the agent-side plugin root (down-only push, discovered by
    ``load_all_plugin_components`` alongside core's own suites) and the REST
    namespace. The read-token signer is keyed by the API master token — the one
    server secret already governing this deployment.
    """
    from mewbo_core.tooling.plugins import (  # noqa: PLC0415
        discover_builtin_plugins,
        register_builtin_root,
    )

    # ``discover_builtin_plugins`` scans a root's immediate subdirectories for a
    # ``<suite>/.claude-plugin/plugin.json``. The agent-side suite ships its
    # manifest at ``plugin/.claude-plugin/plugin.json`` (``plugin/`` IS the
    # suite), so the discovery ROOT is its parent — the ``mewbo_api.apps`` package
    # dir — inside which ``plugin/`` is the one suite it finds. Guard against a
    # silent layout drift (a manifest move breaks discovery with no error): log
    # loudly rather than ship an apps deployment with no app-builder AgentDef.
    plugin_root = _APPS_PLUGIN_ROOT.parent
    register_builtin_root(plugin_root)
    if not discover_builtin_plugins(plugin_root):
        logging.warning(
            "Mewbo Apps agent plugin not discovered under %s — the app-builder / "
            "app-repair AgentDefs will be unavailable (check the suite's "
            ".claude-plugin/plugin.json location).",
            plugin_root,
        )
    global _apps_pipeline_tracker, _apps_pipeline_runner  # noqa: PLW0603 - composition-root handles
    app_store = get_app_store()
    run_store = get_pipeline_run_store()
    data_store = get_app_data_store()
    # ONE run-starter, shared by the lifecycle (builder/repair kick-off + agentic
    # seed) and the tracker (the agentic /fire wake) — both wake a session the same
    # way through the _trigger_deliver idle-start idiom.
    apps_run_starter = _RuntimeAppRunStarter()
    lifecycle = AppLifecycle(
        app_store=app_store,
        trigger_store=_trigger_store,
        trigger_policy=_trigger_policy,
        sessions=RuntimeSessionBackend(runtime),
        # Starts the builder run at create_draft + the repair run on a failed
        # pipeline; mirrors the _trigger_deliver idle-start idiom.
        run_starter=apps_run_starter,
        # The ONE catalog, so a submitted ``workspace_ref`` key is checked against
        # exactly what the maintainer session will later resolve it through. The
        # singleton HANDLE is injected (not a snapshot of its sources): ``_catalog()``
        # re-points that same object's config/store fields, so the lifecycle sees
        # every later re-pointing.
        project_catalog=_catalog(),
        # The deployment's ceiling on what a pipeline may shell out to. A pipeline
        # still DECLARES the binaries it needs; this only bounds what it is allowed
        # to declare, so widening the reachable set stays an operator act rather
        # than something an app can grant itself.
        allowed_exec_binaries=frozenset(_config.api.apps_exec_binaries),
    )
    # Code-pipeline executor: runs a ``mode="code"`` pipeline's
    # entrypoint deterministically (no LLM call) at the fire seam + on demand. The
    # workspace resolver reuses the SAME session-cwd resolution a trigger re-engage
    # would (the maintainer session already carries the app's project/own-scope
    # context), so glob/read_file see exactly the workspace the agentic path would.
    _apps_pipeline_runner = AppPipelineRunner(
        app_store=app_store,
        app_data=data_store,
        workspace_resolver=_resolve_app_workspace_cwd,
        # The bounded ctx.llm() step (Wave 5): a thin adapter over the SAME
        # structured-synthesis seam /v1/structured 'synthesis' mode uses (no new LLM
        # client). Unwired ⇒ ctx.llm raises a clean "not configured"; a pipeline must
        # still DECLARE a positive llm_budget_tokens to reach it.
        llm_invoke=_apps_llm_invoke,
        # Same ceiling the lifecycle admits against, read from the same setting —
        # a submit-time refusal and an execution-time refusal that disagreed would
        # let an app pass admission and then fail on every fire.
        allowed_exec_binaries=frozenset(_config.api.apps_exec_binaries),
    )
    # Push it to the plugin's run_pipeline seam (mirrors register_app_submitter);
    # unwired ⇒ run_pipeline degrades to a clean "not configured" error.
    register_pipeline_runner(_apps_pipeline_runner)
    # Pipeline-run ledger tracker: opens a PipelineRun when a fired
    # pipeline trigger re-engages a maintainer (read at fire time by
    # ``_trigger_deliver`` via the module handle) and closes it at the run's end
    # via the session-end hook — the seam that actually OBSERVES run completion
    # (``start_async`` returns a run_id immediately, so the deliver closure never
    # does). A failed close dispatches the lifecycle's on_pipeline_failure policy.
    # The runner is handed to it so a fired ``mode="code"`` pipeline runs the engine
    # synchronously (no maintainer wake) at the ``_trigger_deliver`` seam.
    _apps_pipeline_tracker = AppPipelineRunTracker(
        run_store=run_store,
        app_store=app_store,
        failure_handler=lifecycle,
        pipeline_runner=_apps_pipeline_runner,
        # The agentic /fire wake + seed rides the SAME idle-start idiom the builder
        # and repair kick-offs do.
        run_starter=apps_run_starter,
    )
    # Push the tracker to the plugin's run_pipeline LEDGER seam, so a model-driven
    # invoke records a PipelineRun like every other execution path (unwired ⇒ the
    # tool still runs, reporting run_key: None).
    register_pipeline_ledger(_apps_pipeline_tracker)
    # Close the lifecycle<->tracker cycle: the lifecycle drives the fire seam to SEED
    # a first run of every pipeline at go-live (so freshness is never born "Never
    # refreshed") and to seed a re-armed pipeline. The tracker is built after the
    # lifecycle (it takes the lifecycle as its failure_handler), so this is an
    # assign-after-construction wiring — a reference cycle, never an import one.
    lifecycle.tracker = _apps_pipeline_tracker
    # A process death (deploy/restart) strands in-flight runs as `running`
    # forever — close them honestly before any new fire can open a run. An
    # unswept open run has zero trace and blocks the failure policy from firing.
    swept = _apps_pipeline_tracker.sweep_orphaned_runs(datetime.now(timezone.utc))
    if swept:
        logging.warning("Closed {} orphaned running pipeline run(s) at startup", swept)
    _hook_manager.on_session_end.append(_apps_pipeline_tracker.close_runs)
    # Push the concrete lifecycle to the plugin's submitter seam so ``submit_app``
    # (built through the manifest path, which feeds it only session_id+event_logger)
    # resolves its ``AppSubmitter``. The stores self-wire via their process-wide
    # factories, so this is the ONLY runtime wiring the agent plugin needs; without
    # it ``submit_app`` degrades to a clean "apps runtime not configured" error.
    register_app_submitter(lifecycle)
    controller = AppsRoutesController(
        lifecycle=lifecycle,
        app_store=app_store,
        run_store=run_store,
        data_store=data_store,
        trigger_store=_trigger_store,
        token_signer=_build_apps_token_signer(_config.api.apps_token_secret, MASTER_API_TOKEN),
        require_api_key=_require_api_key,
        # Minting a WRITE-scoped app token is master-key-only — a
        # SEPARATE guard from require_api_key, mirroring _require_master_token's
        # existing use for key-management routes: an issued key must never be
        # able to escalate a served app's pipeline surface to invocable.
        require_master_token=_require_master_token,
        require_permission=_require_permission,
        sdk_files=_load_app_sdk_files(),
        # A pipeline executes synchronously and holds a request thread for its whole
        # life, so this is a slice of the worker's total concurrency rather than a
        # per-app knob. Past it a caller is refused with a retryable 429, which is
        # diagnosable; queueing behind the thread pool wedges every other endpoint.
        apps_max_concurrent_pipelines=_config.api.apps_max_concurrent_pipelines,
        # The SAME code-pipeline engine the fire seam + run_pipeline tool use
        # — GET/POST .../pipelines/<name> executes for real
        # instead of 503ing "pipeline execution not configured".
        runner=_apps_pipeline_runner,
        # The SAME ledger tracker the fire seam uses — an on-demand
        # invoke that writes data or genuinely fails is
        # ledgered kind="on_request"; a cache hit or a no-write success mints no
        # row. See AppsRoutesController.invoke_pipeline / record_code_run.
        tracker=_apps_pipeline_tracker,
    )
    init_apps_routes(api, controller)


def _load_app_sdk_files() -> dict[str, str]:
    """Read the agent SDK once at startup for server-side injection into rendered apps.

    The stlite frontend imports ``mewbo_app`` (the sanctioned network path); the
    backend injects the SDK source into the rendered detail response's
    ``frontend.files`` rather than the console/Aura bundling it, so both clients
    stay SDK-free and the stored :class:`AppSpec` is never polluted. A missing SDK
    file logs loudly and degrades to no injection (served apps then fail their
    ``import mewbo_app`` — visible, never a silent server crash).
    """
    sdk_path = _APPS_PLUGIN_ROOT / "sdk" / "mewbo_app.py"
    try:
        return {"mewbo_app.py": sdk_path.read_text(encoding="utf-8")}
    except OSError as exc:
        logging.warning(
            "Mewbo Apps SDK not readable at %s (%s) — rendered apps will fail "
            "`import mewbo_app`; check the plugin's sdk/ directory.",
            sdk_path,
            exc,
        )
        return {}


init_apps()


# ---------------------------------------------------------------------------
# Request body models. Documentation only: request validation is not enabled,
# so these shape the OpenAPI spec without changing runtime behavior.
# ---------------------------------------------------------------------------

key_mint_model = ns.model(
    "KeyMintRequest",
    {
        "label": fields.String(
            required=True,
            description="Human-readable label for the key, shown in key listings.",
            example="ci-deploy",
        ),
    },
)

project_create_model = ns.model(
    "ProjectCreateRequest",
    {
        "name": fields.String(
            required=True,
            description="Display name for the project.",
            example="my-service",
        ),
        "description": fields.String(
            required=False,
            description="Optional free-text description.",
            example="Payments service monorepo",
        ),
        "path": fields.String(
            required=False,
            description=(
                "Absolute filesystem path to an existing checkout. When omitted, "
                "the server provisions a folder for the project."
            ),
            example="/srv/repos/my-service",
        ),
    },
)

project_patch_model = ns.model(
    "ProjectPatchRequest",
    {
        "name": fields.String(
            required=False,
            description="New display name. Omit to keep the current one.",
            example="my-service",
        ),
        "description": fields.String(
            required=False,
            description="New description. Omit to keep the current one.",
        ),
    },
)

worktree_create_model = ns.model(
    "WorktreeCreateRequest",
    {
        "branch": fields.String(
            required=True,
            description=(
                "Branch to check out in the new worktree. Must already exist "
                "unless `base` is provided."
            ),
            example="feature/checkout-flow",
        ),
        "base": fields.String(
            required=False,
            description=(
                "Optional base ref. When set, a fresh `branch` is created from "
                "this ref instead of requiring the branch to exist."
            ),
            example="main",
        ),
    },
)

session_create_model = ns.model(
    "SessionCreateRequest",
    {
        "session_tag": fields.String(
            required=False,
            description="Optional stable tag for looking the session up later.",
            example="nightly-report",
        ),
        "project": fields.String(
            required=False,
            description=(
                "Project to bind the session to: a configured project name, or "
                "`managed:<project_id>` for a managed project or worktree."
            ),
            example="Assistant",
        ),
        "mode": fields.String(
            required=False,
            description="Orchestration mode. Either `plan` or `act`.",
            example="act",
        ),
        "context": fields.Raw(
            required=False,
            description=(
                "Free-form context object persisted with the session. Recognized "
                "keys include `project`, `model`, `mcp_tools` (tool allowlist), "
                "`denied_tools` (tool denylist — purely subtractive, applies over "
                "every other gate), `skill`, and `fallback_models`."
            ),
        ),
        "attachments": fields.List(
            fields.Raw,
            required=False,
            description="Attachment descriptors returned by the attachments upload endpoint.",
        ),
    },
)

session_query_model = ns.model(
    "SessionQueryRequest",
    {
        "query": fields.String(
            required=True,
            description=(
                "The user message to run, or a slash command such as `/status` "
                "or `/terminate`."
            ),
            example="Summarize the open pull requests.",
        ),
        "mode": fields.String(
            required=False,
            description="Orchestration mode. Either `plan` or `act`.",
            example="act",
        ),
        "project": fields.String(
            required=False,
            description=(
                "Project whose directory the run executes in: a configured project "
                "name or `managed:<project_id>`."
            ),
            example="Assistant",
        ),
        "context": fields.Raw(
            required=False,
            description=(
                "Free-form context object persisted with the session. Recognized "
                "keys include `project`, `model`, `mcp_tools` (tool allowlist), "
                "`denied_tools` (tool denylist — purely subtractive, applies over "
                "every other gate), `skill`, and `fallback_models`."
            ),
        ),
        "attachments": fields.List(
            fields.Raw,
            required=False,
            description="Attachment descriptors returned by the attachments upload endpoint.",
        ),
        "skill": fields.String(
            required=False,
            description="Name of a skill to activate for this run.",
            example="deep-research",
        ),
        "skill_args": fields.String(
            required=False,
            description="Arguments passed to the activated skill.",
        ),
    },
)

session_message_model = ns.model(
    "SessionMessageRequest",
    {
        "text": fields.String(
            required=True,
            description=(
                "Message text. Steers the active run, or re-engages an idle "
                "session as a new query."
            ),
            example="Focus on the failing tests first.",
        ),
    },
)

session_recover_model = ns.model(
    "SessionRecoverRequest",
    {
        "action": fields.String(
            required=True,
            description=(
                "`retry` re-runs the last user query; `continue` resumes from "
                "where the failed run stopped."
            ),
            example="retry",
        ),
        "from_ts": fields.String(
            required=False,
            description=(
                "Timestamp of the user message to recover from. Defaults to the "
                "most recent one."
            ),
        ),
        "edited_text": fields.String(
            required=False,
            description="Replacement text for the recovered query.",
        ),
        "model": fields.String(
            required=False,
            description="Model override for the recovered run.",
            example="anthropic/claude-sonnet-4-6",
        ),
    },
)

session_fork_model = ns.model(
    "SessionForkRequest",
    {
        "from_ts": fields.String(
            required=False,
            description=(
                "Fork point: copy events up to this timestamp. Omit to fork the "
                "full transcript."
            ),
        ),
        "model": fields.String(
            required=False,
            description="Model override recorded on the new session.",
            example="anthropic/claude-sonnet-4-6",
        ),
        "compact": fields.String(
            required=False,
            description=(
                "Set to `true` to compact the forked transcript in the background "
                "after the fork."
            ),
            example="true",
        ),
        "tag": fields.String(
            required=False,
            description="Optional tag applied to the new session.",
            example="experiment-2",
        ),
    },
)

plan_approve_model = ns.model(
    "PlanApproveRequest",
    {
        "approved": fields.Boolean(
            required=True,
            description="True to approve the pending plan, false to reject it.",
            example=True,
        ),
    },
)

title_patch_model = ns.model(
    "TitlePatchRequest",
    {
        "title": fields.String(
            required=True,
            description="New display title. Trimmed and capped at 120 characters.",
            example="Refactor the billing pipeline",
        ),
    },
)

session_command_model = ns.model(
    "SessionCommandRequest",
    {
        "name": fields.String(
            required=True,
            description="Command name without the leading slash.",
            example="compact",
        ),
        "args": fields.List(
            fields.String,
            required=False,
            description="Positional arguments for the command.",
        ),
    },
)

notification_dismiss_model = ns.model(
    "NotificationDismissRequest",
    {
        "ids": fields.List(
            fields.String,
            required=False,
            description="Notification ids to dismiss.",
        ),
        "id": fields.String(
            required=False,
            description="Single notification id. Ignored when `ids` is present.",
        ),
    },
)

notification_clear_model = ns.model(
    "NotificationClearRequest",
    {
        "clear_all": fields.Boolean(
            required=False,
            description=(
                "When true, clear every notification. Defaults to clearing only "
                "dismissed ones."
            ),
            example=False,
        ),
    },
)

config_patch_model = ns.model(
    "ConfigPatchRequest",
    {
        "*": fields.Wildcard(
            fields.Raw,
            description=(
                "Partial configuration subtree, deep-merged into the stored "
                "configuration. Mirrors the shape served by GET /api/config/schema."
            ),
        ),
    },
)

plugin_install_model = ns.model(
    "PluginInstallRequest",
    {
        "name": fields.String(
            required=True,
            description="Plugin name as listed by GET /api/plugins/marketplace.",
            example="code-review",
        ),
        "marketplace": fields.String(
            required=True,
            description="Marketplace the plugin is published in.",
            example="official",
        ),
    },
)

sync_query_model = ns.model(
    "SyncQueryRequest",
    {
        "query": fields.String(
            required=True,
            description="The user query to run to completion.",
            example="What changed in the last release?",
        ),
        "session_id": fields.String(
            required=False,
            description="Existing session id to continue.",
        ),
        "session_tag": fields.String(
            required=False,
            description="Human-friendly tag resolving to a session (created if new).",
            example="cli",
        ),
        "fork_from": fields.String(
            required=False,
            description="Session id or tag to fork the new session from.",
        ),
        "mode": fields.String(
            required=False,
            description="Orchestration mode. Either `plan` or `act`.",
            example="act",
        ),
        "project": fields.String(
            required=False,
            description=(
                "Project whose directory the run executes in: a configured project "
                "name or `managed:<project_id>`."
            ),
            example="Assistant",
        ),
        "context": fields.Raw(
            required=False,
            description=(
                "Free-form context object persisted with the session. Recognized "
                "keys include `project`, `model`, `mcp_tools` (tool allowlist), and "
                "`denied_tools` (tool denylist — purely subtractive, applies over "
                "every other gate)."
            ),
        ),
        "attachments": fields.List(
            fields.Raw,
            required=False,
            description="Attachment descriptors returned by the attachments upload endpoint.",
        ),
    },
)


# ---------------------------------------------------------------------------
# Response (success) models. Documentation only — never attached via
# ``marshal_with`` (which would filter the real body), only via ``@api.response``
# so Scalar synthesizes a sample body from the ``example=`` values. Field names
# and examples mirror what the handlers actually return; reused across endpoints
# that share a shape (DRY).
# ---------------------------------------------------------------------------

key_mint_response_model = ns.model(
    "KeyMintResponse",
    {
        "id": fields.String(example="k_7f3a9c21", description="Stable key id; use it to revoke."),
        "label": fields.String(example="ci-deploy", description="Label supplied at mint time."),
        "key": fields.String(
            example="msk-2f9a1c7e4b8d3a6f0e5c2b1a9d8e7f60",
            description="The plaintext API key — shown ONCE, never retrievable again.",
        ),
        "created_at": fields.String(
            example="2026-06-15T18:24:05.412903+00:00",
            description="ISO-8601 UTC creation timestamp.",
        ),
    },
)

key_record_model = ns.model(
    "KeyRecord",
    {
        "id": fields.String(example="k_7f3a9c21"),
        "label": fields.String(example="ci-deploy"),
        "created_at": fields.String(example="2026-06-15T18:24:05.412903+00:00"),
        "revoked": fields.Boolean(example=False, description="True once the key has been revoked."),
    },
)

keys_list_model = ns.model(
    "KeysListResponse",
    {
        "keys": fields.List(
            fields.Nested(key_record_model),
            description="Metadata for every key; hashes and plaintext are never included.",
        )
    },
)

key_revoke_model = ns.model(
    "KeyRevokeResponse",
    {
        "id": fields.String(example="k_7f3a9c21"),
        "revoked": fields.Boolean(example=True),
    },
)

model_capability_model = ns.model(
    "ModelCapability",
    {
        "supports_vision": fields.Boolean(
            example=True, description="Whether the model accepts image attachments."
        ),
    },
)

models_list_model = ns.model(
    "ModelsListResponse",
    {
        "models": fields.List(
            fields.String,
            example=["anthropic/claude-opus-4-8", "openai/gpt-5.4-nano"],
            description="Model names served by the configured LLM proxy.",
        ),
        "default": fields.String(
            example="anthropic/claude-opus-4-8", description="The default model name."
        ),
        "capabilities": fields.Raw(
            example={
                "anthropic/claude-opus-4-8": {"supports_vision": True},
                "openai/gpt-5.4-nano": {"supports_vision": False},
            },
            description="Per-model capability map keyed by model name.",
        ),
    },
)

repo_identity_model = ns.model(
    "RepoIdentity",
    {
        "host": fields.String(example="github.com"),
        "owner": fields.String(example="bearlike"),
        "name": fields.String(example="Assistant"),
    },
)

project_model = ns.model(
    "Project",
    {
        "name": fields.String(example="Assistant"),
        "path": fields.String(example="/srv/repos/Assistant"),
        "description": fields.String(example="Mewbo monorepo"),
        "available": fields.Boolean(
            example=True, description="Whether the project path exists on disk."
        ),
        "source": fields.String(
            example="config", description="`config` (static) or `managed` (server-owned)."
        ),
        "project_id": fields.String(
            example="6f1c2d3e4a5b", description="Managed-project id (managed entries only)."
        ),
        "is_worktree": fields.Boolean(example=False),
        "parent_project_id": fields.String(example=None),
        "branch": fields.String(example=None),
        "repo": fields.Nested(
            repo_identity_model,
            allow_null=True,
            description="Canonical git identity (git checkouts only).",
        ),
        "aliases": fields.List(
            fields.String,
            example=["github.com/bearlike/Assistant", "bearlike/Assistant", "Assistant"],
            description="Addressable aliases for the same repository.",
        ),
    },
)

projects_list_model = ns.model(
    "ProjectsListResponse",
    {"projects": fields.List(fields.Nested(project_model))},
)

vproject_model = ns.model(
    "ManagedProject",
    {
        "project_id": fields.String(example="6f1c2d3e4a5b"),
        "name": fields.String(example="my-service"),
        "description": fields.String(example="Payments service monorepo"),
        "parent_project_id": fields.String(example=None),
        "branch": fields.String(example=None),
        "is_worktree": fields.Boolean(example=False),
        "path": fields.String(example="/app/data/projects/6f1c2d3e4a5b"),
        "path_source": fields.String(
            example="created",
            description="`provided` (caller path) or `created` (server-provisioned).",
        ),
        "folder_created": fields.Boolean(example=True),
        "created_at": fields.String(example="2026-06-15T18:24:05.412903+00:00"),
        "updated_at": fields.String(example="2026-06-15T18:24:05.412903+00:00"),
    },
)

worktree_model = ns.model(
    "Worktree",
    {
        "project_id": fields.String(
            example="wt:6f1c2d3e4a5b:feature-checkout-flow",
            description="The worktree's own managed id (null for unmanaged on-disk worktrees).",
        ),
        "name": fields.String(example="feature/checkout-flow"),
        "branch": fields.String(example="feature/checkout-flow"),
        "path": fields.String(example="/app/data/worktrees/wt-feature-checkout-flow"),
        "managed": fields.Boolean(
            example=True, description="True for API-created worktrees, false for plain-git ones."
        ),
        "is_worktree": fields.Boolean(example=True),
        "parent_project_id": fields.String(example="6f1c2d3e4a5b"),
        "parent_path": fields.String(example="/srv/repos/my-service"),
        "clean": fields.Boolean(
            example=True, description="True when the worktree has no uncommitted changes."
        ),
    },
)

worktrees_list_model = ns.model(
    "WorktreesListResponse",
    {"worktrees": fields.List(fields.Nested(worktree_model))},
)

branches_list_model = ns.model(
    "BranchesListResponse",
    {
        "branches": fields.List(
            fields.String, example=["main", "feature/checkout-flow"]
        ),
        "current_branch": fields.String(
            example="main", description="The checked-out branch, or null when HEAD is detached."
        ),
        "branches_in_use": fields.List(
            fields.String,
            example=["feature/checkout-flow"],
            description="Branches already checked out by the parent repo or another worktree.",
        ),
        "git_repo": fields.Boolean(
            example=True,
            description="False (with a `reason`) when the path is missing or not a git repo.",
        ),
        "reason": fields.String(
            example="not_git",
            description="Why `git_repo` is false: `missing_path` or `not_git`.",
        ),
    },
)

session_diff_stat_model = ns.model(
    "SessionDiffStat",
    {
        "additions": fields.Integer(
            example=128, description="Lines the session's edits added, summed."
        ),
        "deletions": fields.Integer(
            example=34, description="Lines the session's edits removed, summed."
        ),
    },
)

session_summary_model = ns.model(
    "SessionSummary",
    {
        "session_id": fields.String(example="9e2d47c1a0b34f12"),
        "title": fields.String(example="Refactor the billing pipeline"),
        "status": fields.String(
            example="completed",
            description="`idle`, `running`, `completed`, `incomplete`, or `terminated`.",
        ),
        "done_reason": fields.String(example="completed"),
        # The enum is READ OFF core's SessionOrigin rather than spelled out here:
        # the hand-written prose list this replaced had gone stale by four members,
        # and it is published — it renders into the REST reference from
        # docs/openapi.json. Deriving it means a new origin documents itself.
        "origin": fields.String(
            example="user",
            enum=[member.value for member in SessionOrigin],
            description=(
                "What created the session, derived from its tags and first context event."
            ),
        ),
        "recoverable": fields.Boolean(example=False),
        "terminated": fields.Boolean(
            example=False, description="True once the session is permanently terminated."
        ),
        "terminated_at": fields.String(
            example=None, description="ISO termination time, or null if the session is live."
        ),
        "created_at": fields.String(example="2026-06-15T18:24:05.412903+00:00"),
        "updated_at": fields.String(example="2026-06-15T18:31:42.108551+00:00"),
        "pinned": fields.Boolean(
            example=False,
            description=(
                "Present (`true`) only on a pinned session — an unpinned row omits "
                "both this key and `pinned_at` rather than carrying `false`."
            ),
        ),
        "pinned_at": fields.String(
            example="2026-06-15T18:24:05.412903+00:00",
            description="When the session was pinned. Absent on an unpinned session.",
        ),
        "projects": fields.List(
            fields.String,
            example=["Assistant"],
            description=(
                "Every project identity the session's context has ever bound to — "
                "an auto-select session that switched mid-task carries each one it "
                "moved through, not just its current binding. Absent when the "
                "session has bound to no project."
            ),
        ),
        "diff_stat": fields.Nested(
            session_diff_stat_model,
            allow_null=True,
            skip_none=True,
            description=(
                "Line counts summed over the session's file edits. "
                "Absent when the session changed no files."
            ),
        ),
    },
)

sessions_list_model = ns.model(
    "SessionsListResponse",
    {
        "sessions": fields.List(fields.Nested(session_summary_model)),
        "limit": fields.Integer(
            example=50,
            description="Echoed back only when the request supplied `limit`.",
        ),
        "offset": fields.Integer(
            example=0,
            description="Echoed back only when the request supplied `limit`.",
        ),
    },
)

session_create_response_model = ns.model(
    "SessionCreateResponse",
    {"session_id": fields.String(example="9e2d47c1a0b34f12")},
)

session_query_accepted_model = ns.model(
    "SessionQueryAccepted",
    {
        "session_id": fields.String(example="9e2d47c1a0b34f12"),
        "accepted": fields.Boolean(example=True),
    },
)

session_status_model = ns.model(
    "SessionStatusResponse",
    {
        "session_id": fields.String(example="9e2d47c1a0b34f12"),
        "status": fields.String(example="running"),
        "done_reason": fields.String(example=""),
        "title": fields.String(example="Refactor the billing pipeline"),
        "recoverable": fields.Boolean(example=False),
    },
)

session_event_model = ns.model(
    "SessionEvent",
    {
        "type": fields.String(
            example="tool_result",
            description="Event kind (`user`, `tool_result`, `completion`, …).",
        ),
        "ts": fields.String(example="2026-06-15T18:24:06.001234+00:00"),
        "payload": fields.Raw(
            example={"tool_id": "shell", "operation": "get", "result": "ok"},
            description="Event-specific payload.",
        ),
    },
)

session_events_model = ns.model(
    "SessionEventsResponse",
    {
        "session_id": fields.String(example="9e2d47c1a0b34f12"),
        "events": fields.List(fields.Nested(session_event_model)),
        "running": fields.Boolean(example=False),
        "status": fields.String(example="completed"),
        "done_reason": fields.String(example="completed"),
        "title": fields.String(example="Refactor the billing pipeline"),
        "recoverable": fields.Boolean(example=False),
    },
)

timeline_token_usage_model = ns.model(
    "TimelineTokenUsage",
    {
        "input_tokens": fields.Integer(
            example=24110, description="Peak root input — context pressure, not a sum."
        ),
        "output_tokens": fields.Integer(example=5102),
        "sub_input_tokens": fields.Integer(
            example=18200, description="Sum of per-sub-agent peak input."
        ),
        "sub_output_tokens": fields.Integer(example=1200),
        "sub_agent_count": fields.Integer(example=3),
        "cache_creation_tokens": fields.Integer(example=800),
        "cache_read_tokens": fields.Integer(example=19400),
        "reasoning_tokens": fields.Integer(example=640),
        "billed_input_tokens": fields.Integer(
            example=88120, description="Cumulative billable input (root sum + sub sum)."
        ),
    },
)

timeline_turn_model = ns.model(
    "TimelineTurn",
    {
        "id": fields.String(example="turn-3"),
        "duration_ms": fields.Integer(
            example=18400, description="Prompt-to-closure span; absent when not measurable."
        ),
        "model": fields.String(example="some-model"),
        "done_reason": fields.String(
            example="completed",
            description=(
                "The closing completion's reason; absent when an assistant "
                "event closed the turn."
            ),
        ),
        "token_usage": fields.Nested(timeline_token_usage_model, allow_null=True),
    },
)

timeline_entry_model = ns.model(
    "TimelineEntry",
    {
        "id": fields.String(example="assistant-3"),
        "role": fields.String(
            example="assistant",
            description=(
                "One of: user, assistant, run_failed, plan, widget, todos, "
                "question, trigger, session_terminated, recovery."
            ),
        ),
        "content": fields.String(example="Here is the summary you asked for."),
        "turn_id": fields.String(example="turn-3"),
        "ts": fields.String(example="2026-06-15T18:24:10.882001+00:00"),
        # Turn metadata rides only the row that CLOSED a turn, so a client can
        # render one footer per turn without de-duplicating.
        "turn": fields.Nested(timeline_turn_model, allow_null=True),
        "plan": fields.Raw(description="Present on `plan` rows: revision, status, content."),
        "widget": fields.Raw(description="Present on `widget` rows: the widget_ready payload."),
        "todos": fields.Raw(description="Present on `todos` rows: the checklist items."),
        "question": fields.Raw(
            description=(
                "Present on `question` rows. Never carries the answer credential — "
                "answering goes through the questions endpoint."
            )
        ),
        "trigger": fields.Raw(description="Present on `trigger` rows: kind, action, summary."),
        "recovery": fields.Raw(description="Present on `recovery` rows: retry or continue."),
        "run_failure": fields.Raw(
            description="Present on `run_failed` rows: reason plus the classified error detail."
        ),
        "attachments": fields.Raw(description="Descriptors for files sent with a `user` row."),
    },
)

session_timeline_model = ns.model(
    "SessionTimelineResponse",
    {
        "session_id": fields.String(example="9e2d47c1a0b34f12"),
        "entries": fields.List(fields.Nested(timeline_entry_model)),
        "open_turn": fields.Nested(
            timeline_turn_model,
            allow_null=True,
            description="The still-running turn, absent once every turn has concluded.",
        ),
        "running": fields.Boolean(example=False),
        "status": fields.String(example="completed"),
        "terminated": fields.Boolean(example=False),
    },
)

session_spec_binding_model = ns.model(
    "SessionSpecBinding",
    {
        "origin": fields.String(
            example="wiki",
            description="Provenance of the creating surface: user/wiki/search/channel/"
            "mobile/structured/draft/apps.",
        ),
        "surface": fields.String(
            example="console", description="Client surface the session was created from."
        ),
        "purpose_bound": fields.Boolean(
            example=True,
            description=(
                "True when the session was created FOR a narrower purpose than open "
                "chat — a product surface created it, or its tool scope is "
                "authoritative. A bound session refuses tool/scope overrides."
            ),
        ),
        "project": fields.String(example="Assistant", description="Bound project name."),
        "slug": fields.String(example="acme/beacon", description="Product-scoped identifier."),
        "cwd": fields.String(example="/srv/projects/assistant"),
        "model": fields.String(example="openai/claude-sonnet-5"),
        "fallback_models": fields.List(
            fields.String,
            description="Opted-in fallback ladder; null defers to the configured policy.",
        ),
        "allowed_tools": fields.List(
            fields.String,
            description=(
                "MCP tool ceiling. THREE-STATE: null unrestricted, [] grants no MCP "
                "tool, non-empty grants exactly those."
            ),
        ),
        "strict_tool_scope": fields.Boolean(example=True),
        "capabilities": fields.List(
            fields.String, description="Capabilities the purpose requires."
        ),
        "skill_instructions_present": fields.Boolean(
            example=True,
            description="Whether a playbook is bound. The text itself is not projected.",
        ),
        "session_step_budget": fields.Integer(example=50),
        "mode": fields.String(example="act"),
    },
)

session_spec_model = ns.model(
    "SessionSpecResponse",
    {
        "session_id": fields.String(example="9e2d47c1a0b34f12"),
        "spec": fields.Nested(session_spec_binding_model),
        "editable": fields.Raw(
            description=(
                "Server-declared per-field modifiability, keyed by the binding's field "
                "names. Fail-closed and authoritative: a false field is refused "
                "server-side rather than silently ignored."
            ),
            example={
                "model": True,
                "fallback_models": True,
                "mode": True,
                "project": False,
                "slug": False,
                "cwd": False,
                "allowed_tools": False,
                "strict_tool_scope": False,
                "skill_instructions": False,
                "session_step_budget": False,
            },
        ),
        "source": fields.String(
            example="spec",
            description=(
                "`spec` when a durable binding was recorded, `legacy_context` when it "
                "was reconstructed from a session predating them."
            ),
        ),
    },
)

session_project_rebind_model = ns.model(
    "SessionProjectRebindRequest",
    {
        "project": fields.String(
            required=True,
            example="Assistant",
            description=(
                "Name of the project to bind the session to — a configured project, "
                "or `managed:<project_id>` for a server-managed one. Its directory is "
                "resolved server-side and stored with it, so the next turn runs there. "
                "This is the only accepted field; any other, `cwd` included, is a 400."
            ),
        ),
    },
)

session_events_ingest_request_model = ns.model(
    "SessionEventsIngestRequest",
    {
        "record": fields.Nested(
            session_event_model,
            description="A single event record to append (use this OR `records`).",
        ),
        "records": fields.List(
            fields.Nested(session_event_model),
            description="A batch of event records to append (use this OR `record`).",
        ),
    },
)

session_events_ingest_model = ns.model(
    "SessionEventsIngestResponse",
    {
        "session_id": fields.String(example="9e2d47c1a0b34f12"),
        "appended": fields.Integer(example=3, description="Number of events appended."),
    },
)

session_message_enqueued_model = ns.model(
    "SessionMessageEnqueued",
    {
        "session_id": fields.String(example="9e2d47c1a0b34f12"),
        "enqueued": fields.Boolean(example=True),
        "run_id": fields.String(
            example="9e2d47c1a0b34f12:r2",
            description="New run id (present only when an idle session was re-engaged).",
        ),
    },
)

session_interrupt_model = ns.model(
    "SessionInterruptResponse",
    {
        "session_id": fields.String(example="9e2d47c1a0b34f12"),
        "interrupted": fields.Boolean(example=True),
    },
)

device_tool_error_model = ns.model(
    "DeviceToolError",
    {
        "code": fields.String(example="permission_denied"),
        "message": fields.String(example="User denied the SMS permission."),
    },
)

device_tool_result_model = ns.model(
    "DeviceToolResultRequest",
    {
        "call_token": fields.String(
            required=True,
            description="Single-use token carried on the `device_tool_call` event this answers.",
            example="Q1sT9x...redacted",
        ),
        "status": fields.String(
            required=True,
            description="`ok` or `error`.",
            example="ok",
        ),
        "result": fields.Raw(
            required=False,
            description="The tool's return value. Present when `status` is `ok`.",
        ),
        "error": fields.Nested(
            device_tool_error_model,
            required=False,
            description="Structured error. Present when `status` is `error`.",
        ),
    },
)

device_tool_resolved_model = ns.model(
    "DeviceToolResolved",
    {"resolved": fields.Boolean(example=True)},
)

question_answer_item_model = ns.model(
    "QuestionAnswerItem",
    {
        "selected_indexes": fields.List(
            fields.Integer,
            required=False,
            description="0-based indexes into the question's options (XOR `text`).",
            example=[0],
        ),
        "text": fields.String(
            required=False,
            description=(
                "Free-text answer — always accepted, even with options "
                "(XOR `selected_indexes`)."
            ),
            example="Use the staging cluster instead",
        ),
    },
)

question_answer_model = ns.model(
    "QuestionAnswerRequest",
    {
        "call_token": fields.String(
            required=True,
            description="Single-use token carried on the `user_question` event this answers.",
            example="Q1sT9x...redacted",
        ),
        "answers": fields.List(
            fields.Nested(question_answer_item_model),
            required=True,
            description="One item per question, in the question order.",
        ),
        "notes": fields.String(
            required=False,
            description=(
                "Optional free text the user typed alongside the selections "
                f"(at most {MAX_QUESTION_NOTES_CHARS} characters). Answers no "
                "single question; carries whatever the event's "
                "`notes_placeholder` invited."
            ),
            example="The staging cluster is mid-migration until Friday.",
        ),
    },
)

question_answered_model = ns.model(
    "QuestionAnswered",
    {
        "resolved": fields.Boolean(example=True),
        "delivery": fields.String(
            example="run",
            enum=["run", "message"],
            description=(
                "Where the answer landed: `run` — the blocked "
                "`ask_user_question` call resolved with it; `message` — the "
                "wait had ended, so it arrived as a new user turn."
            ),
        ),
    },
)

session_recover_response_model = ns.model(
    "SessionRecoverResponse",
    {
        "session_id": fields.String(example="9e2d47c1a0b34f12"),
        "action": fields.String(example="retry"),
        "accepted": fields.Boolean(example=True),
        "run_id": fields.String(
            example="9e2d47c1a0b34f12:r3",
            description="Generic recovery run id; absent for wiki-indexing recovery.",
        ),
        "job_id": fields.String(
            example=None,
            description="Wiki-indexing job id (returned instead of `run_id` for indexing).",
        ),
        "slug": fields.String(
            example=None, description="Wiki repo slug (wiki-indexing recovery only)."
        ),
        "status": fields.String(example=None),
    },
)

session_fork_response_model = ns.model(
    "SessionForkResponse",
    {
        "session_id": fields.String(
            example="a1b2c3d4e5f60718", description="The newly forked session id."
        ),
        "forked_from": fields.String(example="9e2d47c1a0b34f12"),
        "forked_at": fields.String(
            example=None, description="Fork-point timestamp, or null for a full-history fork."
        ),
    },
)

plan_decision_model = ns.model(
    "PlanDecisionResponse",
    {
        "session_id": fields.String(example="9e2d47c1a0b34f12"),
        "approved": fields.Boolean(example=True),
    },
)

session_title_model = ns.model(
    "SessionTitleResponse",
    {
        "session_id": fields.String(example="9e2d47c1a0b34f12"),
        "title": fields.String(example="Refactor the billing pipeline"),
    },
)

session_archive_model = ns.model(
    "SessionArchiveResponse",
    {
        "session_id": fields.String(example="9e2d47c1a0b34f12"),
        "archived": fields.Boolean(example=True),
    },
)

session_pin_model = ns.model(
    "SessionPinResponse",
    {
        "session_id": fields.String(example="9e2d47c1a0b34f12"),
        "pinned": fields.Boolean(example=True),
        "pinned_at": fields.String(
            example="2026-06-15T18:24:05.412903+00:00",
            description="ISO pin time, or null once unpinned.",
        ),
    },
)

session_terminate_model = ns.model(
    "SessionTerminateResponse",
    {
        "session_id": fields.String(example="9e2d47c1a0b34f12"),
        "status": fields.String(example="terminated"),
        "terminated_at": fields.String(example="2026-07-13T18:24:10.882001+00:00"),
        "cancelled_triggers": fields.Integer(
            example=0,
            description="Downstream artifacts (e.g. scheduled triggers) cancelled by this call.",
        ),
    },
)

# A terminated session rejects every mutating call with this 410 envelope. The
# ``code`` is the SEMANTIC token ``session_terminated`` (not the HTTP status) so
# a small agent can branch on it; ``retryable`` is always false — the state is
# permanent. Distinct from the generic ``kit`` envelope (whose ``code`` is the
# int status), so it is documented with a bespoke model.
session_terminated_error_model = ns.model(
    "SessionTerminatedError",
    {
        "error": fields.Nested(
            ns.model(
                "SessionTerminatedErrorBody",
                {
                    "code": fields.String(example="session_terminated"),
                    "reason": fields.String(example="Session is permanently terminated"),
                    "retryable": fields.Boolean(example=False),
                },
            )
        )
    },
)

agent_node_model = ns.model(
    "AgentNode",
    {
        "agent_id": fields.String(example="sub-1a2b"),
        "parent_id": fields.String(example="root"),
        "depth": fields.Integer(example=1),
        "model": fields.String(example="anthropic/claude-opus-4-8"),
        "action": fields.String(example="spawn"),
        "detail": fields.String(example="explore the auth module"),
        "status": fields.String(example="completed"),
        "steps_completed": fields.Integer(example=4),
        "input_tokens": fields.Integer(example=18234),
        "output_tokens": fields.Integer(example=2041),
        "ts": fields.String(example="2026-06-15T18:24:10.882001+00:00"),
    },
)

agents_tree_model = ns.model(
    "AgentTreeResponse",
    {
        "agents": fields.List(fields.Nested(agent_node_model)),
        "running": fields.Boolean(example=False),
        "total_steps": fields.Integer(example=12),
        "total_input_tokens": fields.Integer(
            example=42310, description="Peak context pressure (root peak + sum of per-agent peaks)."
        ),
        "total_input_tokens_billed": fields.Integer(
            example=88120, description="Cumulative billed input tokens."
        ),
        "total_output_tokens": fields.Integer(example=5102),
    },
)

usage_model = ns.model(
    "UsageResponse",
    {
        "root_peak_input_tokens": fields.Integer(example=24110),
        "sub_peak_input_tokens": fields.Integer(example=18200),
        "total_input_tokens_billed": fields.Integer(example=88120),
        "total_output_tokens": fields.Integer(example=5102),
    },
)

share_record_model = ns.model(
    "ShareRecord",
    {
        "token": fields.String(example="shr_4f9a2c7e1b8d"),
        "session_id": fields.String(example="9e2d47c1a0b34f12"),
        "created_at": fields.String(example="2026-06-15T18:24:05.412903+00:00"),
    },
)

session_export_model = ns.model(
    "SessionExportResponse",
    {
        "session_id": fields.String(example="9e2d47c1a0b34f12"),
        "events": fields.List(fields.Nested(session_event_model)),
        "summary": fields.Raw(
            example={"status": "completed", "title": "Refactor the billing pipeline"},
            description="The stored session summary.",
        ),
    },
)

share_lookup_model = ns.model(
    "ShareLookupResponse",
    {
        "token": fields.String(example="shr_4f9a2c7e1b8d"),
        "session_id": fields.String(example="9e2d47c1a0b34f12"),
        "created_at": fields.String(example="2026-06-15T18:24:05.412903+00:00"),
        "events": fields.List(fields.Nested(session_event_model)),
        "summary": fields.Raw(
            example={"status": "completed", "title": "Refactor the billing pipeline"}
        ),
    },
)

files_list_model = ns.model(
    "FilesListResponse",
    {
        "files": fields.List(
            fields.String,
            example=["src/app.py", "README.md", "tests/test_app.py"],
            description="Git-indexed project files the composer can reference.",
        ),
        "attachments": fields.List(
            fields.String,
            example=["spec.pdf"],
            description="Session attachment display names (session-scoped queries only).",
        ),
    },
)

git_diff_model = ns.model(
    "GitDiffResponse",
    {
        "git_repo": fields.Boolean(
            example=True, description="False (with a `reason`) when there is no git project."
        ),
        "diff": fields.String(
            example="diff --git a/src/app.py b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n",
            description="Unified diff (present when `git_repo` is true).",
        ),
        "reason": fields.String(
            example="no_project",
            description="Why no diff: `no_project`, `not_git`, or `git_error`.",
        ),
    },
)

command_spec_model = ns.model(
    "CommandSpec",
    {
        "name": fields.String(example="compact"),
        "args": fields.List(fields.String, example=[]),
        "render": fields.String(
            example="transcript", description="`transcript`, `dialog`, or `notification`."
        ),
    },
)

commands_list_model = ns.model(
    "CommandsListResponse",
    {"commands": fields.List(fields.Nested(command_spec_model))},
)

command_inline_model = ns.model(
    "CommandInlineResult",
    {
        "render": fields.String(example="dialog"),
        "title": fields.String(example="Token usage"),
        "body": fields.String(example="Root: 24,110 tokens; sub-agents: 18,200 tokens."),
        "metadata": fields.Raw(example={"total": 42310}),
    },
)

command_accepted_model = ns.model(
    "CommandAccepted",
    {
        "session_id": fields.String(example="9e2d47c1a0b34f12"),
        "accepted": fields.Boolean(example=True),
        "render": fields.String(example="transcript"),
    },
)

notification_model = ns.model(
    "Notification",
    {
        "id": fields.String(example="ntf_8a1c2d3e"),
        "title": fields.String(example="'Refactor the billing pipeline' completed"),
        "message": fields.String(example="Turn finished successfully."),
        "level": fields.String(example="info", description="`info` or `warning`."),
        "session_id": fields.String(example="9e2d47c1a0b34f12"),
        "event_type": fields.String(example="completed"),
        "dismissed": fields.Boolean(example=False),
        "metadata": fields.Raw(example={"done_reason": "completed"}),
    },
)

notifications_list_model = ns.model(
    "NotificationsListResponse",
    {"notifications": fields.List(fields.Nested(notification_model))},
)

notification_dismiss_response_model = ns.model(
    "NotificationDismissResponse",
    {"dismissed": fields.Integer(example=2, description="Number of notifications dismissed.")},
)

notification_clear_response_model = ns.model(
    "NotificationClearResponse",
    {"cleared": fields.Integer(example=5, description="Number of notifications cleared.")},
)

tool_spec_model = ns.model(
    "ToolSpec",
    {
        "tool_id": fields.String(example="shell"),
        "name": fields.String(example="Shell"),
        "kind": fields.String(example="builtin", description="`builtin` or `mcp`."),
        "enabled": fields.Boolean(example=True),
        "description": fields.String(example="Run a shell command in the project directory."),
        "disabled_reason": fields.String(example=None),
        "server": fields.String(
            example=None,
            description=(
                "Originating MCP server (MCP tools only), OR the product-tool group "
                "name (`Wiki`, `Agentic Search`) for a capability-gated internal tool."
            ),
        ),
        "scope": fields.String(
            example="builtin", description="`builtin`, `project`, `system`, or `plugin`."
        ),
        "requires_capability": fields.String(
            example=None,
            description=(
                "Set only on capability-gated internal/product tools (wiki_*, scg_*, "
                "agentic_search): the session capability that must be granted for the "
                "tool to actually bind. Selecting the tool via a session's `mcp_tools` "
                "allowlist grants this capability for that request."
            ),
        ),
    },
)

tools_list_model = ns.model(
    "ToolsListResponse",
    {"tools": fields.List(fields.Nested(tool_spec_model))},
)

skill_spec_model = ns.model(
    "SkillSpec",
    {
        "name": fields.String(example="deep-research"),
        "description": fields.String(example="Fan-out web research with cited synthesis."),
        "allowed_tools": fields.List(fields.String, example=["web_search", "web_url_read"]),
        "user_invocable": fields.Boolean(example=True),
        "disable_model_invocation": fields.Boolean(example=False),
        "context": fields.String(example=None),
        "source": fields.String(example="builtin", description="`builtin` or `plugin:<name>`."),
    },
)

skills_list_model = ns.model(
    "SkillsListResponse",
    {"skills": fields.List(fields.Nested(skill_spec_model))},
)

config_validation_error_model = ns.model(
    "ConfigValidationError",
    {
        "message": fields.String(example="Validation failed"),
        "errors": fields.List(
            fields.Raw,
            example=[{"loc": ["llm", "default_model"], "msg": "field required", "type": "missing"}],
            description="Pydantic validation errors; nothing was saved.",
        ),
    },
)

# A PATCH whose merged configuration is valid but cannot be written to disk
# (read-only mount, wrong ownership, full disk) returns this 500 body instead
# of an unhandled traceback. See ``ApiResponseKit.config_write_error_response``
# / ``mewbo_core.config.ConfigWriteError`` — the server filesystem path is
# deliberately never part of this body.
config_write_error_model = ns.model(
    "ConfigWriteError",
    {
        "message": fields.String(
            example="The configuration file lives on a read-only mount.",
            description="Human-readable, actionable reason the write failed.",
        ),
        "code": fields.String(
            example="read_only",
            description=(
                "Machine-readable failure reason: `read_only`, `permission_denied`, "
                "`no_space`, or `io_error`."
            ),
        ),
    },
)

# Reported on GET /api/config so a settings UI can warn a user BEFORE they
# edit and save, instead of only discovering the store is unwritable from a
# failed PATCH. Mirrors ``mewbo_core.config.ConfigWriteAccess``.
config_write_access_model = ns.model(
    "ConfigWriteAccess",
    {
        "writable": fields.Boolean(
            example=True,
            description="Whether the server can currently persist configuration changes.",
        ),
        "code": fields.String(
            example="read_only",
            description="Machine-readable reason the store is unwritable, absent when writable.",
        ),
        "reason": fields.String(
            example="The configuration file lives on a read-only mount.",
            description="Human-readable reason the store is unwritable, absent when writable.",
        ),
    },
)

config_response_model = ns.model(
    "ConfigResponse",
    {
        "config": fields.Raw(
            example={"llm": {"default_model": "anthropic/claude-opus-4-8"}},
            description="Configuration values with protected/secret values stripped.",
        ),
        "secrets": fields.Raw(
            example={"llm.api_key": True, "langfuse.public_key": False},
            description="Is-set map for secret fields (never the values themselves).",
        ),
        "storage": fields.Nested(
            config_write_access_model,
            description="Whether the configuration store is currently writable.",
        ),
    },
)

class PluginListItem(ApiResponse):
    """One plugin's stable identity, availability, and contribution summary."""

    name: str
    display_name: str
    description: str
    version: str
    marketplace: str
    scope: str
    enabled: bool
    skills: int
    agents: int
    commands: int
    mcp_servers: int
    has_hooks: bool


class PluginsListResponse(ApiResponse):
    """The bounded plugin availability projection for the configured installation."""

    plugins: list[PluginListItem]


plugin_model = ns.model(
    "Plugin",
    {
        "name": fields.String(example="code-review"),
        "display_name": fields.String(
            example="Code Review",
            description=(
                "Human-readable label. Falls back to `name` for a plugin whose "
                "manifest sets no `display_name`."
            ),
        ),
        "description": fields.String(example="Multi-agent code review."),
        "version": fields.String(example="1.2.0"),
        "marketplace": fields.String(example="official"),
        "scope": fields.String(example="user"),
        "enabled": fields.Boolean(
            example=True,
            description="Whether the configured plugin selection makes this plugin available.",
        ),
        "skills": fields.Integer(example=1),
        "agents": fields.Integer(example=0),
        "commands": fields.Integer(example=1),
        "mcp_servers": fields.Integer(example=0),
        "has_hooks": fields.Boolean(example=False),
    },
)

plugins_list_model = ns.model(
    "PluginsListResponse",
    {"plugins": fields.List(fields.Nested(plugin_model))},
)

marketplace_plugins_model = ns.model(
    "MarketplacePluginsResponse",
    {
        "plugins": fields.List(
            fields.Raw,
            example=[{"name": "code-review", "marketplace": "official", "version": "1.2.0"}],
            description="Plugins available to install.",
        )
    },
)

plugin_install_response_model = ns.model(
    "PluginInstallResponse",
    {
        "installed": fields.String(example="code-review"),
        "version": fields.String(example="1.2.0"),
    },
)

# The plugin install/uninstall routes return a bare ``{"error": "..."}`` shape
# (an ``error`` string, not the kit's ``{"error": {code, reason}}`` envelope).
plugin_error_model = ns.model(
    "PluginError",
    {"error": fields.String(example="name and marketplace required")},
)

plugin_uninstall_model = ns.model(
    "PluginUninstallResponse",
    {"uninstalled": fields.String(example="code-review")},
)

attachment_descriptor_model = ns.model(
    "AttachmentDescriptor",
    {
        "id": fields.String(example="a1b2c3d4e5f6"),
        "filename": fields.String(example="spec.pdf"),
        "stored_name": fields.String(example="a1b2c3d4e5f6_spec.pdf"),
        "content_type": fields.String(example="application/pdf"),
        "size_bytes": fields.Integer(example=20481),
        "uploaded_at": fields.String(example="2026-06-15T18:24:05.412903+00:00"),
        "parsed": fields.Boolean(
            example=True, description="True when a Markdown sidecar was written at upload time."
        ),
    },
)

attachments_response_model = ns.model(
    "AttachmentsResponse",
    {"attachments": fields.List(fields.Nested(attachment_descriptor_model))},
)

worktree_absent_model = ns.model(
    "WorktreeAbsentResponse",
    {
        "status": fields.String(
            example="already_absent",
            description="Idempotent marker: the worktree was already gone.",
        )
    },
)

# The /command route returns its own ``{error, message?, name?}`` error shape
# (distinct from both kit wire-shapes), so it is documented with bespoke models.
command_error_model = ns.model(
    "CommandError",
    {
        "error": fields.String(example="bad_args", description="Machine-readable error code."),
        "message": fields.String(example="name and args[] required"),
    },
)

command_unknown_model = ns.model(
    "CommandUnknown",
    {
        "error": fields.String(example="unknown_command"),
        "name": fields.String(example="frobnicate", description="The unrecognized command name."),
    },
)

command_running_model = ns.model(
    "CommandSessionRunning",
    {"message": fields.String(example="Session is already running.")},
)


class SelfMintRefused(Exception):
    """A self-service mint asked for more authority than the caller holds."""


@dataclass(frozen=True)
class SelfMintGrant:
    """The exact identity fields a self-service mint may write to the store."""

    roles: list[str]
    scopes: list[str] | None
    expires_at: str | None


class SelfMintAuthority:
    """The ceiling a self-service key mint may never exceed.

    **Why this is a class and not three inline conditionals.** The three fields a
    self-minting caller can influence are each TRI-STATE on the wire — omitted,
    an explicit empty value, or an explicit non-empty value — and the key store
    already honours all three correctly (``_mint_scoped_record`` writes a field
    only when it ``is not None``, so ``[]`` persists as ``[]`` while an ABSENT
    field reads back as unrestricted). The route is the only place the law can
    be broken, and one coalesce breaks it: ``list(payload.get("roles") or ()) or
    None`` folds omitted/``null``/``[]`` into ``None``, the store records ABSENT,
    and :meth:`AuthKit._principal_from_record` reads that back as
    ``(ADMIN_ROLE,)`` — full admin from a ``member`` key that posted
    ``{"label": "x"}``.

    The rule this class exists to make unmissable: **omitted INHERITS the
    caller's own value; an explicit value is checked against it; nothing here
    can widen.** ``is None`` is load-bearing in every branch — a truthiness test
    would read an explicitly-empty list as omitted and inherit the caller's
    authority, so there is deliberately not one anywhere below.

    The structural half matters as much as the checks: :meth:`attenuate` always
    returns a CONCRETE ``roles`` list (the caller's own when omitted), so this
    path never produces a record with an absent ``roles`` field at all. The
    store's absent-field default stays correct for records that already lack one
    and becomes unreachable from client input — a stronger guarantee than a
    conditional that must keep being written correctly.
    """

    __slots__ = ("_expires_at", "_roles", "_scopes")

    def __init__(
        self, *, roles: tuple[str, ...], scopes: tuple[str, ...] | None, expires_at: str | None
    ) -> None:
        """Capture the caller's own authority — the ceiling every mint attenuates to."""
        self._roles = roles
        self._scopes = scopes
        self._expires_at = expires_at

    @classmethod
    def for_request(
        cls, principal: Principal, *, key_store: KeyStoreBase, credential: str | None
    ) -> SelfMintAuthority:
        """Build the ceiling from the in-flight caller.

        Roles and scopes come off the resolved principal; the EXPIRY has to come
        from the caller's own key record, because a principal carries no expiry
        (it is a property of the credential, not of the identity behind it).
        A caller whose record cannot be re-resolved is treated as unexpiring.
        """
        record = key_store.resolve_key(credential) if credential else None
        return cls(
            roles=tuple(principal.roles),
            scopes=None if principal.scopes is None else tuple(principal.scopes),
            expires_at=(record or {}).get("expires_at"),
        )

    def attenuate(self, payload: dict) -> SelfMintGrant:
        """Resolve *payload*'s requested authority against this ceiling."""
        return SelfMintGrant(
            roles=self._roles_for(payload.get("roles")),
            scopes=self._scopes_for(payload.get("scopes")),
            expires_at=self._expires_at_for(payload.get("expires_at")),
        )

    def _roles_for(self, requested: object) -> list[str]:
        """The minted roles: the caller's own when omitted, else a subset of them."""
        if requested is None:
            return list(self._roles)
        if not isinstance(requested, (list, tuple)) or not set(requested) <= set(self._roles):
            raise SelfMintRefused("requested roles exceed the caller's own")
        return [str(role) for role in requested]

    def _scopes_for(self, requested: object) -> list[str] | None:
        """The minted scopes, preserving the three-state law in both directions."""
        if requested is None:
            return None if self._scopes is None else list(self._scopes)
        if not isinstance(requested, (list, tuple)):
            raise SelfMintRefused("requested scopes exceed the caller's own")
        caller = KeyScopes(self._scopes)
        if not all(caller.matches(str(scope)) for scope in requested):
            raise SelfMintRefused("requested scopes exceed the caller's own")
        return [str(scope) for scope in requested]

    def _expires_at_for(self, requested: object) -> str | None:
        """The minted expiry — a self-minted key must never outlive its parent."""
        if requested is None:
            return self._expires_at
        moment = self._parse_iso(requested)
        if moment is None:
            raise SelfMintRefused("expires_at must be an ISO-8601 timestamp")
        ceiling = self._parse_iso(self._expires_at)
        if ceiling is not None and moment > ceiling:
            raise SelfMintRefused("requested expiry exceeds the caller's own")
        return str(requested)

    @staticmethod
    def _parse_iso(raw: object) -> datetime | None:
        """Parse an ISO-8601 stamp to an aware datetime, mirroring the key store.

        Naive values are read as UTC exactly as ``key_store._record_expired``
        does, so the comparison here and the expiry check that later enforces it
        can never disagree about what a stored stamp means.
        """
        if not isinstance(raw, str) or not raw:
            return None
        try:
            moment = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


def _key_mint_response(record: PublicKeyRecord, plaintext: str) -> dict:
    """Shape a minted/rotated key into the wire response.

    Optional identity fields appear only when the record carries them, so a
    record with no owner mints a body with no owner key.
    """
    body: dict = {
        "id": record["id"],
        "label": record["label"],
        "key": plaintext,
        "created_at": record["created_at"],
    }
    for field in ("owner_subject", "roles", "scopes", "team_id", "expires_at"):
        if field in record:
            body[field] = record[field]
    return body


def _self_mint_denied() -> tuple[dict, int] | None:
    """Return the refusal when the caller may not mint keys for itself.

    Self-service exists only once identity does: with auth disabled there is no
    subject to own a key, so the master-token contract stands alone.
    """
    if not _auth_kit.settings.enabled:
        return {"message": "Unauthorized"}, 401
    return _require_permission("keys.mint_own")()


@ns.route("/keys/<string:key_id>/rotate")
class ApiKeyRotate(Resource):
    """Mint a replacement key carrying the original's authority, then revoke it."""

    @api.doc(
        security="apikey",
        params={"key_id": "Key id returned by POST /api/keys."},
        description=(
            "Mint a replacement carrying the same owner, roles, scopes, team and "
            "expiry as `key_id`, and revoke the original in the same call. The new "
            "plaintext key is returned exactly once — store it immediately. "
            "Requires the **master** token, or `keys.mint_own` on a key the caller owns."
        ),
    )
    @ns.response(200, "Key rotated. The new plaintext key is in the body.", key_mint_response_model)
    @kit.errors(404, shape="message", descriptions={404: "No key with that id exists."})
    @kit.auth_error()
    @guard.dual_channel(
        "keys.admin",
        primary="master",
        channel="self_service",
        alternate_permissions=("keys.mint_own",),
        enforced_by="ApiKeyRotate.post (owner check against list_keys_for_owner)",
    )
    def post(self, key_id: str) -> tuple[dict, int]:
        """Rotate an API key

        Mints a replacement carrying the same authority as the original and
        revokes the original. The new plaintext key is returned exactly once.
        """
        admin_error = _require_master_token() or _require_permission("keys.admin")()
        if admin_error is not None:
            if _self_mint_denied() is not None:
                return admin_error
            principal = current_principal()
            if principal is None:
                return admin_error
            owned = key_store.list_keys_for_owner(principal.subject)
            if not any(record["id"] == key_id for record in owned):
                # Uniform not-found: a foreign key's id must not be probeable.
                return {"message": f"Key '{key_id}' not found"}, 404

        rotated = key_store.rotate_key(key_id)
        if rotated is None:
            return {"message": f"Key '{key_id}' not found"}, 404
        plaintext, record = rotated
        return _key_mint_response(record, plaintext), 200


@ns.route("/keys")
class ApiKeys(Resource):
    """Mint and list API keys (master-token-only)."""

    @api.doc(
        security="apikey",
        description=(
            "Mint a new API key for the `X-API-Key` header. The plaintext key "
            "is returned exactly once in the `key` field — store it immediately, "
            "it cannot be retrieved again. Requires the **master** token; keys "
            "minted here cannot manage other keys.\n\n"
            "`curl -XPOST -H 'X-API-Key: <master>' -d '{\"label\":\"ci-deploy\"}' "
            "<base>/api/keys`"
        ),
    )
    @ns.response(
        201, "Key created. The plaintext key is in the response body.", key_mint_response_model
    )
    @kit.errors(400, shape="message", descriptions={400: "The `label` field is missing or empty."})
    @kit.auth_error()
    @ns.expect(key_mint_model)
    @guard.dual_channel(
        "keys.admin",
        primary="master",
        channel="self_service",
        alternate_permissions=("keys.mint_own",),
        enforced_by="ApiKeys.post (owner forced to the caller's subject, roles/scopes subset)",
    )
    def post(self) -> tuple[dict, int]:
        """Mint an API key

        Creates a new API key for use in the `X-API-Key` header. The plaintext
        key is returned exactly once in this response and cannot be retrieved
        again, so store it securely. Requires the master token; keys minted
        here cannot manage other keys.
        """
        payload = request.get_json(silent=True) or {}
        label = str(payload.get("label", "")).strip()
        if not label:
            return {"message": "Invalid input: 'label' is required"}, 400

        admin_error = _require_master_token() or _require_permission("keys.admin")()
        if admin_error is None:
            # An admin may name ANY owner, but not a malformed one: the store
            # takes the string verbatim and only `Principal` enforces the
            # `user:`/`svc:` law, so an unvalidated value mints happily and then
            # raises at every AUTH attempt — a key that exists but can never be
            # used, failing on the wrong request. Same law, checked where the
            # mistake is made. `is None` and not truthiness: the subject is
            # OPTIONAL (an ownerless key), while `""` is a bad value.
            owner_subject = payload.get("owner_subject")
            if owner_subject is not None:
                try:
                    owner_subject = Principal.validate_subject(str(owner_subject))
                except ValueError as exc:
                    return {"message": str(exc)}, 400
            plaintext, record = key_store.create_scoped_key(
                label,
                owner_subject=owner_subject,
                roles=payload.get("roles"),
                scopes=payload.get("scopes"),
                team_id=payload.get("team_id"),
                expires_at=payload.get("expires_at"),
            )
            _auth_kit.record_key_minted(
                key_id=str(record.get("id", "")),
                subject=str(owner_subject or record.get("id", "")),
                label=label,
            )
            return _key_mint_response(record, plaintext), 201

        self_error = _self_mint_denied()
        if self_error is not None:
            # Report the ORIGINAL admin failure: a caller who cannot self-mint
            # should not learn that a self-mint tier exists.
            return admin_error
        principal = current_principal()
        if principal is None:
            return admin_error
        authority = SelfMintAuthority.for_request(
            principal, key_store=key_store, credential=_request_credential()
        )
        try:
            grant = authority.attenuate(payload)
        except SelfMintRefused as exc:
            return {"message": str(exc)}, 400
        plaintext, record = key_store.create_scoped_key(
            label,
            owner_subject=principal.subject,  # forced: a self-mint cannot name another owner
            roles=grant.roles,
            scopes=grant.scopes,
            # team_id is deliberately NOT narrowed here, and that is safe only
            # because _principal_from_record never projects it into team
            # memberships. Anything that starts reading a key's team as authority
            # must narrow it against the caller's own teams FIRST — otherwise a
            # self-mint names any team it likes and this becomes an escalation.
            team_id=payload.get("team_id"),
            expires_at=grant.expires_at,
        )
        _auth_kit.record_key_minted(
            key_id=str(record.get("id", "")), subject=principal.subject, label=label
        )
        return _key_mint_response(record, plaintext), 201

    @api.doc(
        security="apikey",
        description=(
            "List metadata for every API key — id, label, creation time, and "
            "revocation state. Hashes and plaintext values are never returned. "
            "Requires the **master** token. Use a key's `id` with "
            "`DELETE /api/keys/{key_id}` to revoke it."
        ),
    )
    @ns.response(200, "Key metadata list.", keys_list_model)
    @kit.auth_error()
    @guard.dual_channel(
        "keys.admin",
        primary="master",
        channel="self_service",
        alternate_permissions=("keys.mint_own",),
        enforced_by="ApiKeys.get (owner filter pinned to the caller's subject)",
    )
    def get(self) -> tuple[dict, int]:
        """List API keys

        Returns metadata for every key: id, label, creation time, and
        revocation state. Hashes and plaintext key values are never included.
        Requires the master token.
        """
        owner = request.args.get("owner")
        admin_error = _require_master_token() or _require_permission("keys.admin")()
        if admin_error is None:
            keys = key_store.list_keys_for_owner(owner) if owner else key_store.list_keys()
            return {"keys": keys}, 200

        if _self_mint_denied() is not None:
            return admin_error
        principal = current_principal()
        if principal is None:
            return admin_error
        if owner is not None and owner != principal.subject:
            return {"message": "insufficient role"}, 403
        return {"keys": key_store.list_keys_for_owner(principal.subject)}, 200


@ns.route("/keys/<string:key_id>")
class ApiKey(Resource):
    """Revoke an API key (master-token-only)."""

    @api.doc(
        security="apikey",
        params={"key_id": "Key id returned by POST /api/keys."},
        description=(
            "Permanently revoke a key by its `id`. Any request presenting a "
            "revoked key is rejected with 401 from that point on. Requires the "
            "**master** token. Revocation is irreversible — mint a new key to "
            "replace it."
        ),
    )
    @ns.response(200, "Key revoked.", key_revoke_model)
    @kit.errors(404, shape="message", descriptions={404: "No key with that id exists."})
    @kit.auth_error()
    @guard.requires_master("keys.admin")
    def delete(self, key_id: str) -> tuple[dict, int]:
        """Revoke an API key

        Permanently revokes the key. Requests presenting a revoked key are
        rejected with 401 from that point on. Requires the master token.
        """
        # Read the owner BEFORE revoking: the audit entry names whose access was
        # withdrawn, and only the pre-revoke record still says who that was.
        owner = next(
            (
                str(k.get("owner_subject") or "")
                for k in key_store.list_keys()
                if k.get("id") == key_id
            ),
            "",
        )
        if not key_store.revoke_key(key_id):
            return {"message": f"Key '{key_id}' not found"}, 404
        _auth_kit.record_key_revoked(key_id=key_id, subject=owner or key_id)
        return {"id": key_id, "revoked": True}, 200


@ns.route("/models")
class Models(Resource):
    """List available LLM models."""

    @api.doc(
        security="apikey",
        description=(
            "List the model names served by the configured LLM proxy, the "
            "default model, and a per-model capability map. Read "
            "`capabilities[name].supports_vision` to decide whether image "
            "attachments can be sent to a given model before uploading them."
        ),
    )
    @ns.response(200, "Model names, default model, and capability map.", models_list_model)
    @kit.auth_error()
    @guard.requires("projects.read")
    def get(self) -> tuple[dict, int]:
        """List available models

        Returns the model names served by the configured LLM proxy, the
        default model, and a per-model capability map. Use
        `capabilities[name].supports_vision` to decide whether image
        attachments can be sent to a given model.
        """
        default_model = get_config_value("llm", "default_model", default="unknown")
        try:
            models = get_config().llm.list_models()
        except ValueError:
            models = [default_model] if default_model != "unknown" else []
        # A speech route is not a chat model, and the gateway's listing cannot
        # say so — it returns bare ids, and the route carrying each one's mode is
        # closed to the runtime key. So the ids the operator has already NAMED as
        # speech routes are removed here. Without this the answer picker offers
        # every text-to-speech and transcription model the gateway serves, and
        # choosing one fails only later, when the turn runs.
        models = [name for name in models if name not in _speech_model_ids()]
        # Per-model capability map. Frontend uses ``supports_vision`` to
        # gate image attachments at file-selection time (Q5 option B
        # complement — backend still rejects on upload as a safety net).
        capabilities = {
            name: {"supports_vision": bool(model_supports_vision(name))}
            for name in models
        }
        return {
            "models": models,
            "default": default_model,
            "capabilities": capabilities,
        }, 200


def _enrich_project_identity(entry: dict) -> dict:
    """Add ``repo`` + ``aliases`` to a project dict from its git remotes.

    Mutates and returns *entry*. The canonical ``{host, owner, name}`` comes
    from the first remote; ``aliases`` unions every remote's addressable forms
    (``host/owner/repo``, host-less ``owner/repo``, bare ``repo``). A project
    with no git remotes is left untouched (keys absent, not present-but-null).
    """
    path = entry.get("path")
    if not isinstance(path, str) or not path:
        return entry
    identities = RepoIdentity.for_path(path)
    if not identities:
        return entry
    primary = identities[0]
    entry["repo"] = {"host": primary.host, "owner": primary.owner, "name": primary.repo}
    entry["aliases"] = RepoIdentity.aliases_for_path(path)
    return entry


@ns.route("/projects")
class Projects(Resource):
    """List all projects (config-defined + managed)."""

    @api.doc(
        security="apikey",
        description=(
            "List configuration-defined and managed projects in one array. Each "
            "entry carries an `available` flag (does its path exist on disk) and, "
            "for git checkouts, a `repo` identity plus `aliases` (e.g. "
            "`owner/repo`) that address the same project elsewhere in the API. "
            "Managed worktrees appear as child entries with `is_worktree` set."
        ),
    )
    @ns.response(200, "Unified project list.", projects_list_model)
    @kit.auth_error()
    @guard.requires("projects.read")
    def get(self) -> tuple[dict, int]:
        """List projects

        Returns configuration-defined and managed projects in one list. Each
        entry carries an `available` flag (whether its path exists on disk)
        and, for git checkouts, a `repo` identity plus `aliases` such as
        `owner/repo` that address the same project elsewhere in the API.
        Managed worktrees appear as child entries with `is_worktree` set.
        """
        # The union (config first, then managed/worktree) and the `available`
        # rule are the catalog's; this method owns only the WIRE PROJECTION.
        #
        # Registered repositories are deliberately NOT listed here even though
        # the catalog carries them: this payload is a closed contract three
        # clients decode (`contracts.ts:ProjectSummary`, Aura's `ProjectDto`,
        # the CLI), and a repository with no checkout has no `path` for any of
        # them to anchor to. `/v1/git/repositories` is where that list lives.
        result: list[dict] = []
        for entry in _catalog().entries():
            if entry.kind == "configured":
                result.append(
                    _enrich_project_identity(
                        {
                            "name": entry.name,
                            "path": entry.path,
                            "description": entry.description,
                            "available": entry.available,
                            "source": "config",
                        }
                    )
                )
            elif entry.kind in {"managed", "worktree"}:
                parent_key = entry.parent_key
                result.append(
                    _enrich_project_identity(
                        {
                            "name": entry.name,
                            "project_id": entry.key.removeprefix(MANAGED_PREFIX),
                            "path": entry.path,
                            "description": entry.description,
                            "available": entry.available,
                            "source": "managed",
                            "is_worktree": entry.kind == "worktree",
                            "parent_project_id": (
                                parent_key.removeprefix(MANAGED_PREFIX) if parent_key else None
                            ),
                            "branch": entry.branch,
                        }
                    )
                )
        return {"projects": result}, 200


def _vproject_to_dict(p: VirtualProject) -> dict:
    return {
        "project_id": p.project_id,
        "name": p.name,
        "description": p.description,
        "parent_project_id": p.parent_project_id,
        "branch": p.branch,
        "is_worktree": p.is_worktree,
        "path": p.path,
        "path_source": p.path_source,
        "folder_created": p.folder_created,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


@ns.route("/v_projects")
class VirtualProjects(Resource):
    """Create managed projects."""

    @api.doc(
        security="apikey",
        description=(
            "Register a server-managed project (as opposed to a static config "
            "one). When `path` is omitted the server provisions a folder. Use "
            "the returned `project_id` with the other `/api/v_projects` endpoints "
            "and as `managed:<project_id>` when creating sessions."
        ),
    )
    @ns.response(201, "Project created.", vproject_model)
    @kit.errors(400, shape="message", descriptions={400: "The `name` field is missing or empty."})
    @kit.auth_error()
    @ns.expect(project_create_model)
    @guard.requires("projects.write")
    def post(self) -> tuple[dict, int]:
        """Create a managed project

        Registers a project managed by the server, as opposed to one defined
        in static configuration. When `path` is omitted the server provisions
        a folder for it. Use the returned `project_id` with the other
        `/api/v_projects` endpoints, and as `managed:<project_id>` when
        creating sessions.
        """
        payload = request.get_json(silent=True) or {}
        name = payload.get("name", "").strip()
        if not name:
            return {"message": "Invalid input: 'name' is required"}, 400
        description = payload.get("description", "").strip()
        path = payload.get("path", "").strip() or None
        proj = project_store.create_project(name=name, description=description, path=path)
        return _vproject_to_dict(proj), 201


@ns.route("/v_projects/<string:project_id>")
class VirtualProject_(Resource):
    """Get, update, or delete a single virtual project."""

    @api.doc(
        security="apikey",
        params={"project_id": "Managed project id returned by POST /api/v_projects."},
        description=(
            "Fetch a managed project's full record: filesystem path, worktree "
            "linkage (`is_worktree`, `parent_project_id`, `branch`), and "
            "timestamps. Only **managed** ids are accepted here; configured "
            "projects are listed via GET /api/projects."
        ),
    )
    @ns.response(200, "Project record.", vproject_model)
    @kit.errors(404, shape="message", descriptions={404: "No managed project with that id exists."})
    @kit.auth_error()
    @guard.requires("projects.read")
    def get(self, project_id: str) -> tuple[dict, int]:
        """Get a managed project

        Returns the full project record, including its filesystem path,
        worktree linkage (`is_worktree`, `parent_project_id`, `branch`), and
        timestamps. Only managed project ids are accepted here; configured
        projects are listed via GET /api/projects.
        """
        proj = project_store.get_project(project_id)
        if proj is None:
            return {"message": f"Project '{project_id}' not found"}, 404
        return _vproject_to_dict(proj), 200

    @api.doc(
        security="apikey",
        params={"project_id": "Managed project id returned by POST /api/v_projects."},
        description=(
            "Update a managed project's `name` and/or `description`. Fields "
            "omitted from the body are left unchanged. A project's path and "
            "worktree linkage are immutable after creation."
        ),
    )
    @ns.response(200, "Updated project record.", vproject_model)
    @kit.errors(404, shape="message", descriptions={404: "No managed project with that id exists."})
    @kit.auth_error()
    @ns.expect(project_patch_model)
    @guard.requires("projects.write")
    def patch(self, project_id: str) -> tuple[dict, int]:
        """Update a managed project

        Updates the name and/or description. Fields omitted from the body are
        left unchanged. The path and worktree linkage of a project cannot be
        changed after creation.
        """
        payload = request.get_json(silent=True) or {}
        name = payload.get("name")
        description = payload.get("description")
        try:
            proj = project_store.update_project(project_id, name=name, description=description)
        except KeyError:
            return {"message": f"Project '{project_id}' not found"}, 404
        return _vproject_to_dict(proj), 200

    @api.doc(
        security="apikey",
        params={"project_id": "Managed project id returned by POST /api/v_projects."},
        description=(
            "Remove a managed project record. Returns 204 with an empty body on "
            "success. Deleting a project that has worktrees removes only the "
            "project record itself."
        ),
    )
    @api.response(204, "Project deleted (empty body).")
    @kit.errors(404, shape="message", descriptions={404: "No managed project with that id exists."})
    @kit.auth_error()
    @guard.requires("projects.admin")
    def delete(self, project_id: str) -> tuple[dict, int]:
        """Delete a managed project

        Removes the managed project record. Returns 204 with an empty body on
        success.
        """
        proj = project_store.get_project(project_id)
        if proj is None:
            return {"message": f"Project '{project_id}' not found"}, 404
        project_store.delete_project(project_id)
        return {}, 204


# ---------------------------------------------------------------------------
# Worktree routes
#
# A worktree is a child VirtualProject (is_worktree=True) bound to a single
# branch. Identity is deterministic: project_id == "wt:<parent_id>:<slug>".
# These endpoints accept either a managed VirtualProject UUID or a configured
# project name (from ``configs/app.json``). Configured projects are auto-
# promoted to a managed VirtualProject on first worktree creation so the
# existing worktree machinery can take over.
# ---------------------------------------------------------------------------


@dataclass
class _RepoTarget:
    """Resolved view of a project that the worktree routes can operate on."""

    project_id: str | None  # managed UUID, or promoted UUID once created
    name: str
    path: str
    source: str  # "managed" | "config"


def _find_promoted_for_path(path: str) -> VirtualProject | None:
    """Return the managed VirtualProject promoted from this config path, if any.

    Auto-promotion picks the first non-worktree managed project whose
    ``path_source == "provided"`` and whose ``path`` matches. Multiple
    matches are unexpected — first wins, deterministic.
    """
    target = os.path.realpath(path)
    for vp in project_store.list_projects():
        if vp.is_worktree:
            continue
        if vp.path_source != "provided":
            continue
        try:
            if os.path.realpath(vp.path) == target:
                return vp
        except OSError:
            continue
    return None


def _promote_config_project(name: str, path: str, description: str) -> VirtualProject:
    """Create a managed VirtualProject pointing at the existing config path.

    Idempotent: if a managed project already maps to the same path, returns
    that one unchanged. The promoted project becomes the parent of any
    worktrees the user creates.
    """
    existing = _find_promoted_for_path(path)
    if existing is not None:
        return existing
    return project_store.create_project(name=name, description=description, path=path)


def _resolve_repo_by_identity(
    project_key: str,
) -> tuple[_RepoTarget | None, tuple[dict, int] | None]:
    """Match *project_key* against every managed project's git identity.

    Returns ``(target, None)`` on a unique alias match, ``(None, error)`` when
    a bare name is ambiguous (≥2 repos share it), or ``(None, None)`` when no
    project's canonical identity / alias set contains the key.
    """
    matches: list[VirtualProject] = []
    candidates: list[str] = []
    for vp in project_store.list_projects():
        if vp.is_worktree:
            continue
        aliases = RepoIdentity.aliases_for_path(vp.path) if vp.path else []
        if project_key in aliases:
            matches.append(vp)
            for identity in RepoIdentity.for_path(vp.path):
                candidates.append(identity.canonical())
    if not matches:
        return None, None
    if len(matches) > 1:
        return None, (
            {
                "message": (
                    f"Ambiguous project '{project_key}' matches multiple "
                    "repositories. Disambiguate with a full host/owner/repo key."
                ),
                "candidates": sorted(set(candidates)),
            },
            409,
        )
    vp = matches[0]
    return _RepoTarget(
        project_id=vp.project_id,
        name=vp.name,
        path=vp.path,
        source="managed",
    ), None


def _resolve_repo_or_404(
    project_key: str, *, promote: bool = False
) -> tuple[_RepoTarget | None, tuple[dict, int] | None]:
    """Resolve a managed UUID, a configured name, OR a git identity to a target.

    Resolution order: managed project_id/name → configured project name →
    git repo identity/alias (the canonical ``host/owner/repo`` or any of its
    ``owner/repo`` / bare-``repo`` aliases). An ambiguous bare name that maps
    to two different repos raises a clear candidates error (409), never a
    silent wrong match.

    When ``promote=True`` and the key is a config-defined project, ensures a
    managed VirtualProject exists for the path so worktree creation can
    proceed. Returns a ``(target, None)`` on success or ``(None, response)``
    on error.

    The managed and configured lookups delegate to the catalog; the git-identity
    fallback and the promote arm below are this function's own, because neither
    is a name→directory question.
    """
    entry = _catalog().find(project_key)
    # Route paths carry a BARE project_id, a session's ``project`` field carries
    # ``managed:<id>``; the catalog knows the second spelling, the store the
    # first, so accepting both costs one lookup and removes a grammar that
    # resolved on one surface and 404'd on another.
    proj = project_store.get_project(project_key)
    if proj is None and entry is not None and entry.kind in {"managed", "worktree"}:
        proj = project_store.get_project(entry.key.removeprefix(MANAGED_PREFIX))
    if proj is not None:
        if proj.is_worktree:
            return None, ({"message": "Cannot manage worktrees of a worktree."}, 400)
        return _RepoTarget(
            project_id=proj.project_id,
            name=proj.name,
            path=proj.path,
            source="managed",
        ), None

    if entry is None or entry.kind != "configured" or not entry.path:
        # Fall back to git-identity matching before declaring a miss.
        target, err = _resolve_repo_by_identity(project_key)
        if target is not None or err is not None:
            return target, err
        return None, ({"message": f"Project '{project_key}' not found"}, 404)

    if not promote:
        return _RepoTarget(
            project_id=None,
            name=project_key,
            path=entry.path,
            source="config",
        ), None

    promoted = _promote_config_project(
        name=project_key, path=entry.path, description=entry.description
    )
    return _RepoTarget(
        project_id=promoted.project_id,
        name=promoted.name,
        path=promoted.path,
        source="managed",
    ), None


def _is_git_repo(path: str) -> bool:
    """Return ``True`` if *path* is a git working tree.

    Worktrees and submodules use a ``.git`` *file* (a gitlink), so we accept
    both a directory and a regular file at that location.
    """
    if not os.path.isdir(path):
        return False
    return os.path.exists(os.path.join(path, ".git"))


@ns.route("/v_projects/<string:project_id>/branches")
class VirtualProjectBranches(Resource):
    """List git branches and the current HEAD for a project's repository."""

    @api.doc(
        security="apikey",
        params={
            "project_id": (
                "Managed project id, configured project name, or git identity "
                "such as `owner/repo` (any alias of the repository resolves)."
            ),
        },
        description=(
            "List `branches`, the `current_branch` (null when HEAD is detached), "
            "and `branches_in_use` — branches already checked out by the parent "
            "repo or another worktree, which `git worktree add` would refuse. "
            "When the path is missing or not a git repo the call still returns "
            "200 with `git_repo` false and a `reason`."
        ),
    )
    @ns.response(200, "Branch listing, or `git_repo: false` with a reason.", branches_list_model)
    @kit.errors(
        404,
        409,
        shape="message",
        descriptions={
            404: "No project resolves from this id, name, or git identity.",
            409: "A bare repo name matched multiple repositories; use host/owner/repo.",
        },
    )
    @kit.auth_error()
    @guard.requires("projects.read")
    def get(self, project_id: str) -> tuple[dict, int]:
        """List branches

        Returns `branches`, the `current_branch` (null when HEAD is detached),
        and `branches_in_use`, the branches already checked out by the parent
        repository or another worktree. UIs should disable in-use entries,
        since creating a worktree for them fails. When the project path is
        missing or not a git repository the call still returns 200 with
        `git_repo` false and a `reason`.
        """
        target, err = _resolve_repo_or_404(project_id)
        if err:
            return err
        assert target is not None
        if not os.path.isdir(target.path):
            return {
                "branches": [],
                "current_branch": None,
                "git_repo": False,
                "reason": "missing_path",
            }, 200
        if not _is_git_repo(target.path):
            return {
                "branches": [],
                "current_branch": None,
                "git_repo": False,
                "reason": "not_git",
            }, 200
        return {
            "branches": WorktreeManager.list_branches(target.path),
            "current_branch": WorktreeManager.current_branch(target.path),
            # Branches that ``git worktree add`` will refuse — the UI uses
            # this to disable "use existing branch" entries already checked
            # out by the parent repo or another worktree (the original RCA
            # of the "already checked out" 500-class error).
            "branches_in_use": sorted(WorktreeManager.branches_in_use(target.path)),
            "git_repo": True,
        }, 200


def _merged_worktree_listing(target: _RepoTarget) -> list[dict]:
    """Merge managed VirtualProject worktrees with on-disk git worktrees.

    Each entry carries a ``managed`` flag — managed worktrees expose their
    ``project_id`` so callers can pin them to sessions; user-created ones
    only carry ``branch`` and ``path`` until adopted (``managed: false``).
    The parent repo's own working tree is intentionally excluded; it is the
    parent, not a sibling.
    """
    entries: list[dict] = []
    seen_paths: set[str] = set()

    parent_path_real = os.path.realpath(target.path)

    if target.project_id and target.source == "managed":
        for wt in project_store.list_worktrees(target.project_id):
            entry = _vproject_to_dict(wt)
            entry["clean"] = WorktreeManager.is_clean(wt.path)
            entry["managed"] = True
            entry["parent_path"] = target.path
            entries.append(entry)
            try:
                seen_paths.add(os.path.realpath(wt.path))
            except OSError:
                seen_paths.add(wt.path)

    if _is_git_repo(target.path):
        for wt in WorktreeManager.list_worktrees(target.path):
            wt_path = wt.get("path", "")
            if not wt_path:
                continue
            try:
                real = os.path.realpath(wt_path)
            except OSError:
                real = wt_path
            if real == parent_path_real or real in seen_paths:
                continue
            seen_paths.add(real)
            entries.append(
                {
                    "project_id": None,
                    "name": wt.get("branch") or os.path.basename(wt_path),
                    "branch": wt.get("branch") or None,
                    "path": wt_path,
                    "head": wt.get("head") or None,
                    "managed": False,
                    "is_worktree": True,
                    "parent_project_id": target.project_id,
                    "parent_path": target.path,
                    "clean": WorktreeManager.is_clean(wt_path),
                }
            )
    return entries


@ns.route("/v_projects/<string:project_id>/worktrees")
class VirtualProjectWorktrees(Resource):
    """List or create worktrees for a managed or configured project."""

    @api.doc(
        security="apikey",
        params={
            "project_id": (
                "Managed project id, configured project name, or git identity "
                "such as `owner/repo` (any alias of the repository resolves)."
            ),
        },
        description=(
            "List the union of worktrees created through this API and worktrees "
            "added on disk with plain git. Each entry has a `managed` flag — "
            "managed entries carry a `project_id` sessions can be pinned to — and "
            "a `clean` flag indicating it has no uncommitted changes."
        ),
    )
    @ns.response(200, "Worktree list.", worktrees_list_model)
    @kit.errors(
        404,
        409,
        shape="message",
        descriptions={
            404: "No project resolves from this id, name, or git identity.",
            409: "A bare repo name matched multiple repositories; use host/owner/repo.",
        },
    )
    @kit.auth_error()
    @guard.requires("projects.read")
    def get(self, project_id: str) -> tuple[dict, int]:
        """List worktrees

        Returns the union of worktrees created through this API and worktrees
        added on disk with plain git. Each entry has a `managed` flag; managed
        entries carry a `project_id` that sessions can be pinned to. Every
        entry includes a `clean` flag indicating it has no uncommitted
        changes.
        """
        target, err = _resolve_repo_or_404(project_id)
        if err:
            return err
        assert target is not None
        return {"worktrees": _merged_worktree_listing(target)}, 200

    @api.doc(
        security="apikey",
        params={
            "project_id": (
                "Managed project id, configured project name, or git identity "
                "such as `owner/repo` (any alias of the repository resolves)."
            ),
        },
        description=(
            "Check out `branch` in a new worktree folder and register it as a "
            "child managed project. Pass `base` to create a fresh branch from "
            "that ref instead of requiring `branch` to exist. Configured projects "
            "are promoted to managed projects automatically. Worktree lifecycle "
            "is system-owned: a clean worktree is removed when its session ends."
        ),
    )
    @ns.response(201, "Worktree created.", vproject_model)
    @kit.errors(
        400,
        404,
        409,
        shape="message",
        descriptions={
            400: "The `branch` field is missing, or the project is not a git repository.",
            404: "No project resolves from this id, name, or git identity.",
            409: "The branch is already checked out elsewhere, or the worktree path exists.",
        },
    )
    @kit.auth_error()
    @ns.expect(worktree_create_model)
    @guard.requires("projects.write")
    def post(self, project_id: str) -> tuple[dict, int]:
        """Create a worktree

        Checks out `branch` in a new worktree folder and registers it as a
        child managed project. Pass `base` to create a fresh branch from that
        ref instead of requiring `branch` to exist. Configured projects are
        promoted to managed projects automatically so the worktree gets a
        stable parent. Worktree lifecycle is system owned: a clean worktree is
        removed automatically when its session ends.
        """
        payload = request.get_json(silent=True) or {}
        branch = str(payload.get("branch", "")).strip()
        # Optional ``base`` — when provided, the backend creates a fresh
        # branch from <base> via ``git worktree add -b <branch> <path> <base>``.
        # When absent, ``branch`` must already exist locally / as a remote
        # tracking ref for the per-session worktree workflow.
        base_raw = payload.get("base")
        base = str(base_raw).strip() if base_raw else None
        if not branch:
            return {"message": "Invalid input: 'branch' is required"}, 400
        target, err = _resolve_repo_or_404(project_id, promote=True)
        if err:
            return err
        assert target is not None and target.project_id is not None
        if not _is_git_repo(target.path):
            return {
                "message": (
                    f"Project '{target.name}' is not a git repository — "
                    "worktrees require a git working tree."
                )
            }, 400
        try:
            wt = project_store.create_worktree(
                target.project_id, branch, base=base
            )
        except ValueError as exc:
            return {"message": str(exc)}, 400
        except FileExistsError as exc:
            return {"message": str(exc)}, 409
        except WorktreeBranchInUseError as exc:
            # Surface the actionable "already checked out" case as a 409 so
            # the UI can render it as a constraint violation rather than a
            # generic 400 — the user just needs to pick a different branch.
            return {"message": str(exc)}, 409
        except RuntimeError as exc:
            return {"message": str(exc)}, 400
        return _vproject_to_dict(wt), 201


@ns.route("/v_projects/<string:project_id>/worktrees/<string:worktree_id>")
class VirtualProjectWorktree(Resource):
    """Manage a single worktree."""

    @api.doc(
        security="apikey",
        params={
            "project_id": (
                "Parent project: managed project id, configured project name, or "
                "git identity such as `owner/repo`."
            ),
            "worktree_id": "The worktree's own `project_id` from the worktree listing.",
            "force": {
                "description": "Set to true to remove a worktree with uncommitted changes.",
                "in": "query",
                "type": "boolean",
            },
        },
        description=(
            "Remove a managed worktree. Idempotent: deleting one that is already "
            "gone returns 200 with status `already_absent`. A worktree with "
            "uncommitted changes is refused with 409 unless `force=true`. "
            "Worktrees created outside this API must be removed with git directly."
        ),
    )
    @api.response(204, "Worktree removed (empty body).")
    @ns.response(200, "Worktree already absent; nothing to do.", worktree_absent_model)
    @kit.errors(
        400,
        409,
        shape="message",
        descriptions={
            400: "The worktree does not belong to this project.",
            409: "The worktree has uncommitted changes and `force` was not set.",
        },
    )
    @kit.auth_error()
    @guard.requires("projects.admin")
    def delete(self, project_id: str, worktree_id: str) -> tuple[dict, int]:
        """Remove a worktree

        Removes a managed worktree. The call is idempotent: deleting a
        worktree that is already gone returns 200 with status
        `already_absent`. A worktree with uncommitted changes is refused with
        409 unless `force=true`. Worktrees created outside this API must be
        removed with git directly.
        """
        force = _parse_bool(request.args.get("force"))
        wt = project_store.get_project(worktree_id)
        if wt is None or not wt.is_worktree:
            # Idempotent: if already absent return 200 instead of 404 so
            # callers (on_session_end hook, FE) can safely call delete multiple
            # times without treating a second call as an error.
            return {"status": "already_absent"}, 200
        # Allow either the managed parent UUID or a configured project name
        # whose promoted parent matches the worktree's parent_project_id.
        owns = wt.parent_project_id == project_id
        if not owns:
            target, err = _resolve_repo_or_404(project_id)
            if err is None and target is not None:
                owns = target.project_id == wt.parent_project_id
        if not owns:
            return {"message": "Worktree does not belong to this project."}, 400
        try:
            project_store.delete_worktree(worktree_id, force=force)
        except RuntimeError as exc:
            return {"message": str(exc)}, 409
        return {}, 204


@ns.route("/sessions")
class Sessions(Resource):
    """List and create sessions."""

    @api.doc(
        security="apikey",
        params={
            "include_archived": {
                "description": "Set to true to include archived sessions.",
                "in": "query",
                "type": "boolean",
            },
            "project": {
                "description": (
                    "Repeat to widen: `?project=a&project=b` lists sessions that "
                    "worked in EITHER. A session accumulates every project it "
                    "binds to, so an auto-select session that switched mid-task "
                    "matches each one."
                ),
                "in": "query",
                "type": "string",
            },
            "pinned": {
                "description": (
                    "Set to true for pinned sessions only, false for unpinned "
                    "only. Omit to list both — pinning is normally an ordering, "
                    "not a filter."
                ),
                "in": "query",
                "type": "boolean",
            },
            "limit": {
                "description": (
                    "Cap the number of most-recently-active sessions examined. "
                    "Omit to examine every candidate. Applied AFTER "
                    "`pinned`/`project`/`include_archived` have narrowed the "
                    "candidate set, so a filtered page is a page of the filtered "
                    "sessions. Still bounds candidates rather than guaranteeing a "
                    "row count — an examined session with no visible turn counts "
                    "against `limit` without producing a row."
                ),
                "in": "query",
                "type": "integer",
            },
            "offset": {
                "description": (
                    "Candidates to skip before applying `limit`. Ignored without `limit`."
                ),
                "in": "query",
                "type": "integer",
            },
        },
        description=(
            "List one summary per session — status, title, timestamps, and an "
            "`origin` describing what created it (the `SessionSummary` model "
            "carries the full set of values). Pinned sessions sort first, then "
            "newest first. Archived sessions are hidden unless "
            "`include_archived=true`. Unpaginated by default; pass `limit` to "
            "page through the most recently active sessions."
        ),
    )
    @ns.response(200, "Session summaries.", sessions_list_model)
    @kit.auth_error()
    @guard.requires("sessions.read_all")
    def get(self) -> tuple[dict, int]:
        """List sessions

        Returns one summary per session with status, title, timestamps, and an
        `origin` field describing what created it (the `SessionSummary` model
        carries the full set of values). Pinned sessions sort first, then newest
        first. Archived sessions are hidden unless `include_archived=true`.
        Unpaginated (returns the whole store) unless `limit` is given, matching
        every consumer written before pagination existed; passing `limit`
        narrows both the response AND, on the MongoDB driver, the store read
        behind it.

        `project` is REPEATABLE rather than comma-separated — a project identity
        is an opaque string this route must not re-split, and Werkzeug already
        gives the multi-value read for free. `pinned` is absent-vs-false
        tri-state, so it reads the raw arg before `_parse_bool` (which cannot
        tell "omitted" from "false").

        The filters and the page compose in ONE order: every `SessionListQuery`
        predicate is decided at the store, and only what it admitted is paged.
        Paging first would make `?pinned=true&limit=50` return the pinned
        sessions among the newest 50 candidates rather than the newest 50 pinned
        sessions.
        """
        pinned_arg = request.args.get("pinned")
        limit = request.args.get("limit", type=int)
        offset = request.args.get("offset", type=int) or 0
        if limit is not None:
            limit = max(limit, 1)
        offset = max(offset, 0)
        sessions = runtime.list_sessions(
            SessionListQuery(
                include_archived=_parse_bool(request.args.get("include_archived")),
                pinned=None if pinned_arg is None else _parse_bool(pinned_arg),
                projects=request.args.getlist("project"),
            ),
            limit=limit,
            offset=offset,
        )
        body: dict[str, object] = {"sessions": sessions}
        if limit is not None:
            body["limit"] = limit
            body["offset"] = offset
        return body, 200

    @api.doc(
        security="apikey",
        description=(
            "Create an empty session and return its `session_id`. Optionally bind "
            "a project, apply a lookup `session_tag`, and persist initial context "
            "(e.g. the model). Clients may declare capabilities via the "
            "`X-Mewbo-Capabilities` header (comma separated). Run queries with "
            "POST /api/sessions/{session_id}/query. `api.allow_external_cwd` governs "
            "only a path the server does not already own; configured, managed, "
            "worktree, repository-checkout, and session-bound directories are accepted."
        ),
    )
    @ns.response(
        200,
        "Session created; body carries the new `session_id`.",
        session_create_response_model,
    )
    @kit.errors(
        400,
        403,
        descriptions={
            400: "An explicit `cwd` was supplied but the path does not exist / is not a dir.",
            403: (
                "`api.allow_external_cwd` rejects only a supplied `cwd` the server does not "
                "already own; configured, managed, worktree, repository-checkout, and "
                "session-bound directories are accepted."
            ),
        },
    )
    @kit.auth_error()
    @ns.expect(session_create_model)
    @guard.requires("sessions.create")
    def post(self) -> tuple[dict, int]:
        """Create a session

        Creates an empty session and returns its `session_id`. Optionally
        binds a project, applies a lookup tag, and persists initial context
        such as the model to use. Clients may declare capabilities via the
        `X-Mewbo-Capabilities` header (comma separated). Run queries against
        the session with POST /api/sessions/{session_id}/query.
        """
        payload = request.get_json(silent=True) or {}
        session_id = runtime.session_store.create_session()
        notification_service.emit_session_created(session_id)
        session_tag = payload.get("session_tag")
        if session_tag:
            runtime.tag_session(session_id, session_tag)
        # Mobile clients (Aura) declare their surface via X-Mewbo-Surface;
        # tag the session additively so SessionOrigin.classify() reports the
        # MOBILE origin. Never clobbers an explicit session_tag above.
        surface = _request_surface()
        if surface and is_mobile_surface(surface):
            runtime.tag_session(session_id, f"{MOBILE_TAG_PREFIX}{surface.lower()}")
        context_payload = _build_context_payload(payload)
        # Capability header — clients may declare supported features (e.g. "stlite"
        # for the widget builder). Parse comma-separated values and persist in the
        # session context so the Orchestrator can conditionally enable agent types.
        client_capabilities = parse_capability_header(
            request.headers.get(CAPABILITY_HEADER, "")
        )
        if client_capabilities:
            context_payload["client_capabilities"] = list(client_capabilities)
        # External cwd (external workspace managers): explicit cwd wins.
        ext_policy = ExternalCwdPolicy(get_config(), catalog=_catalog)
        ext_cwd, ext_err = ext_policy.resolve(payload)
        if ext_err is not None:
            return ext_err
        if ext_cwd is not None:
            context_payload["cwd"] = ext_cwd
        if ext_cwd is None:
            project_name = _requested_project(payload)
            if project_name is not None and is_auto_project(project_name):
                # The sentinel IS the binding, so it persists with NO cwd. It is
                # not a directory — it records that one has not been chosen yet,
                # which is what keeps the session in auto mode for every later
                # turn. Resolving it to the scratch directory here would make
                # "never chose" and "chose the scratch dir" indistinguishable.
                context_payload["project"] = project_name
            else:
                try:
                    project_cwd = _resolve_project_cwd(payload)
                except ValueError:
                    project_cwd = None
                if project_cwd and project_name:
                    context_payload["project"] = project_name
                    # Persist the DIRECTORY beside the name. Resolving it and
                    # keeping only the name left every project-bound session with
                    # a null cwd, so each re-engage path had to re-derive the path
                    # from the name — and the one path with no such rung ran the
                    # turn in an empty per-session temp dir instead of the project.
                    context_payload["cwd"] = project_cwd
                    _populate_worktree_context(project_name, context_payload)
        if "model" not in context_payload:
            context_payload["model"] = get_config_value("llm", "default_model", default="unknown")
        # Bind the session's PURPOSE at creation — the one moment the creating
        # surface's intent is unambiguous. Every later turn through any surface
        # reads this instead of re-deriving from whoever happens to be calling.
        # Persisted as a full context event (typed mirror + the loose keys every
        # existing reader still consumes), so nothing downstream changes shape.
        spec = SessionSpec.from_context(
            context_payload,
            origin=_session_specs.origin_for(session_id, context_payload),
            surface=surface or None,
        )
        runtime.append_context_event(session_id, spec.to_context_payload() | context_payload)
        return {"session_id": session_id}, 200


@ns.route("/sessions/<string:session_id>/query")
class SessionQuery(Resource):
    """Enqueue a query or process slash commands for a session."""

    @api.doc(
        security="apikey",
        params={"session_id": "Session id returned by POST /api/sessions."},
        description=(
            "Start an asynchronous run for `query` and return 202 immediately; "
            "follow progress via the events or stream endpoints. The slash "
            "commands `/terminate` and `/status` are handled inline without "
            "starting a run (`/status` returns 200 with the session state). A "
            "session runs one turn at a time, so a second call while one is "
            "active returns 409. `@file`/`@dir`/`@diff`/`@url` references in the "
            "query are expanded before the run."
        ),
    )
    @ns.response(
        202,
        "Run started; poll the events endpoint or open the stream.",
        session_query_accepted_model,
    )
    @ns.response(200, "Slash command handled inline (`/status`).", session_status_model)
    @ns.response(
        410, "Session is permanently terminated.", session_terminated_error_model
    )
    @kit.errors(
        400,
        shape="message",
        descriptions={400: "The `query` field is missing, or the named project is invalid."},
    )
    @kit.errors(
        403,
        descriptions={
            403: (
                "`api.allow_external_cwd` rejects only a supplied `cwd` the server does not "
                "already own; configured, managed, worktree, repository-checkout, and "
                "session-bound directories are accepted."
            )
        },
    )
    @kit.errors(
        409, shape="message", descriptions={409: "A run is already active for this session."}
    )
    @kit.auth_error()
    @ns.expect(session_query_model)
    @guard.requires("sessions.interact")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Run a session query

        Starts an asynchronous run for `query` on the session and returns 202
        immediately; follow progress via the events or stream endpoints. The
        slash commands `/terminate` and `/status` are handled inline without
        starting a run. A session executes one run at a time, so a second
        call while one is active returns 409.
        """
        request_data = request.get_json(silent=True) or {}
        user_query = request_data.get("query")
        if not user_query:
            return {"message": "Invalid input: 'query' is required"}, 400

        command_response = _handle_slash_command(session_id, user_query)
        if command_response is not None:
            return command_response

        # A terminated session rejects new runs. Slash commands above stay
        # allowed: /status is a read; the /terminate COMMAND is a run-cancel
        # (not this endpoint's permanent kill) and no-ops when nothing runs —
        # same verb, different permanence. 410 Gone below.
        terminated = _terminated_guard(session_id)
        if terminated is not None:
            return terminated

        if runtime.is_running(session_id):
            return {"message": "Session is already running."}, 409

        # Refuse before the run is persisted, not after it dies: a worker still
        # resolving its provider credentials must answer "retry" rather than
        # accept a turn it cannot serve.
        not_ready = _run_readiness.check()
        if not_ready is not None:
            return not_ready

        request_context = _build_context_payload(request_data)
        # Capability header — same parsing as Sessions.post() for per-query declarations.
        # Falls back to the body's `context.client_capabilities` when no header is
        # sent, mirroring Sessions.post()'s header-wins-else-body precedence: this
        # is the interactive turn's OWN advertisement (run_capabilities is
        # turn-scoped, never persisted to the spec), not a spec override, so a
        # body-only "ask_user" declaration on /query must still reach it.
        parsed = parse_capability_header(request.headers.get(CAPABILITY_HEADER, ""))
        requested_capabilities: Sequence[str] | None = list(parsed) if parsed else None
        if requested_capabilities is None:
            requested_capabilities = SessionSpec.normalize_ids(
                request_context.get("client_capabilities")
            )
        source_platform = _request_surface()

        mode = _parse_mode(request_data.get("mode"))
        # Skill activation: resolve from top-level "skill" field or context.skill.
        skill_instructions = _resolve_skill_instructions(request_data, user_query, request_context)

        # THE re-derivation fix. Every other re-engage path (`/message`,
        # `/recover`, the trigger wake) already reads the session's persisted
        # binding; this one re-derived model/tools/scope/cwd from the request
        # alone, defaulted whatever the request omitted, and then PERSISTED those
        # defaults — corrupting the binding every later turn reads. Load the spec
        # first and apply only the overrides it sanctions: absence inherits.
        spec = _session_specs.load(session_id)

        # This gate needs the loaded binding to distinguish an echoed directory
        # from a new host-path claim.
        ext_policy = ExternalCwdPolicy(get_config(), catalog=_catalog)
        ext_cwd, ext_err = ext_policy.resolve(request_data, bound_cwd=spec.cwd)
        if ext_err is not None:
            return ext_err

        # A purpose-bound session cannot accept a project override. Merge owns
        # that refusal, so resolving the raw request first is backwards: a client
        # echoing an old invalid binding would 400 before the merge discarded it.
        # An editable project still resolves before the merge, because the run
        # needs its directory rather than only its catalog key.
        requested_cwd = ext_cwd
        requested_project = _requested_project(request_data)
        if requested_cwd is None and (
            requested_project is None or spec.field_editable("project")
        ):
            try:
                requested_cwd = _resolve_project_cwd(request_data)
            except ValueError as exc:
                return {"message": str(exc)}, 400

        overrides = SessionSpecOverrides.from_request_context(
            request_context,
            cwd=requested_cwd,
            mode=mode,
            skill_instructions=skill_instructions,
        )
        run_spec, refused = spec.merge_request_overrides(overrides)
        if refused:
            logging.info(
                "Session {} is purpose-bound; ignoring request override(s) {} on /query",
                session_id,
                ", ".join(refused),
            )
        if is_auto_project(spec.project) and not is_auto_project(run_spec.project):
            # `auto` is the session's MODE, not a project, so a request naming a
            # real one moves where the session RUNS without ending the mode. This
            # is not a corner case: the console and Aura both resend the project
            # the session has settled on so the turn lands in the right directory
            # (the sentinel would send it back to a scratch cwd), and taking that
            # as "stop auto-selecting" would unbind the switching tools after
            # exactly one switch. The directory still comes from the request —
            # only the binding is preserved.
            run_spec = SessionSpec.model_validate(
                {**run_spec.model_dump(), "project": AUTO_PROJECT}
            )
        run_capabilities = run_spec.run_capabilities(requested_capabilities)

        # The persisted payload the tool-grant + scope resolution reads. Built from
        # the MERGED spec rather than the raw request so device tools and the
        # ask-user gate see the session's real binding, not a partial declaration.
        context_payload = run_spec.to_context_payload(capabilities=run_capabilities)
        # Non-binding request keys (attachments, device_tools, structured
        # workspace, …) still ride the context event as they always have. The
        # spec-owned keys are SKIPPED rather than merely defaulted: re-adding a raw
        # request value for a field the merge just refused would hand the override
        # straight back through the side door.
        for key, value in request_context.items():
            if key in SessionSpec.SPEC_OWNED_CONTEXT_KEYS:
                continue
            context_payload.setdefault(key, value)

        # Validate BEFORE persisting: a malformed `device_tools` declaration
        # must 400 without poisoning the session's context event, or
        # `/message` re-engage and `/recover` (which read the LAST-PERSISTED
        # context) would inherit the same malformed declaration and 400
        # forever — a bricked session (whole-branch review, F6).
        try:
            allowed_tools, extra_session_tools = _derive_tool_grants(session_id, context_payload)
        except ValueError as exc:
            return {"message": str(exc)}, 400

        # Resolve the caller's role into an authoritative run scope BEFORE the
        # context is persisted, so the intersected grants + provenance subject
        # are what land on the session's context event.
        scope = _run_scope(
            allowed_tools=allowed_tools,
            client_capabilities=_persisted_client_capabilities(context_payload),
            strict_tool_scope=run_spec.strict_tool_scope,
        )
        _stamp_principal_subject(context_payload)

        if context_payload:
            runtime.append_context_event(session_id, context_payload)

        # Three rungs, matching every sibling re-engage path (`/message`, the idle
        # restart, the diff endpoints): the binding's own directory, then the one
        # its persisted project NAME resolves to, then the temp dir. The middle
        # rung is what a session bound before the directory was persisted needs —
        # without it the SAME session ran in its project on `/message` and in an
        # empty scratch directory on `/query`.
        project_cwd = (
            run_spec.cwd or _resolve_session_cwd(session_id) or session_temp_dir(session_id)
        )
        # Auto-select is a property of the BINDING, not of this turn: the spec
        # keeps `auto` even after the model has settled somewhere (the settled
        # project rides an ordinary context event, which is what the middle rung
        # above reads), so every later turn of an auto session still declares it
        # and the model can switch again.
        project_autoselect = is_auto_project(run_spec.project)

        # Inline @<ref> context expansion — files/dirs/@diff/URLs resolved
        # against the session cwd, pre-LLM. File/dir refs are scoped to the
        # project's git index (or session attachments); see reference_expansion.
        user_query = expand_references(
            user_query,
            project_cwd,
            attachments=_session_attachment_map(session_id),
        )

        budget = run_spec.session_step_budget or int(
            get_config_value("agent", "session_step_budget", default=0)
        )
        max_iters = int(get_config_value("agent", "max_iters", default=30))
        started = runtime.start_async(
            session_id=session_id,
            user_query=user_query,
            model_name=run_spec.model
            or get_config_value("llm", "default_model", default="unknown"),
            fallback_models=run_spec.fallback_models,
            approval_callback=scope.approval_callback,
            permission_policy=scope.permission_policy,
            hook_manager=_hook_manager,
            mode=run_spec.mode,
            allowed_tools=scope.allowed_tools,
            # Persisted on ``context_payload`` above via ``run_spec.denied_tools``
            # (``SessionSpec.to_context_payload``) — read it back the same way
            # ``allowed_tools`` came off ``_derive_tool_grants``, so a client
            # deny reaches the run.
            denied_tools=_extract_denied_tools(context_payload),
            strict_tool_scope=scope.strict_tool_scope,
            capability_mode=scope.capability_mode,
            skill_instructions=run_spec.skill_instructions,
            cwd=project_cwd,
            max_iters=max_iters,
            session_step_budget=budget,
            source_platform=source_platform,
            extra_session_tools=extra_session_tools,
            attachments=_extract_attachments(request_data),
            project_autoselect=project_autoselect,
        )
        if not started:
            return {"message": "Session is already running."}, 409
        return {"session_id": session_id, "accepted": True}, 202


class SessionStateFrame(BaseModel):
    """The authoritative run-state projection of a session.

    ONE home for the derived run state that two surfaces publish: the polled
    ``/events`` body splices it in flat, and the ``/stream`` SSE leg emits it as
    a ``session_state`` frame. Both read it from the same ``summarize_session``
    call, so a consumer that migrates between them observes identical values —
    a second copy of this projection would drift the moment either surface
    gained a field.
    """

    model_config = ConfigDict(extra="forbid")

    # Present only on a session that hit trouble; an untroubled summary carries
    # none of them and the wire body stays byte-identical without them.
    OPTIONAL_KEYS: ClassVar[tuple[str, ...]] = (
        "blocked_code",
        "failure_reason",
        "models_tried",
    )

    # Typed to what ``summarize_session`` actually produces, NOT to what a
    # settled session happens to carry. ``done_reason`` is None until a turn
    # finishes and ``title`` is None until one is stored, so declaring either
    # non-null turns an ordinary just-created session into a 500 at the very
    # moment a client first reads it.
    running: bool
    status: str
    done_reason: str | None
    title: str | None
    recoverable: bool
    terminated: bool
    terminated_at: str | None = None
    extras: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_summary(cls, summary: dict[str, Any]) -> SessionStateFrame:
        """Project a ``summarize_session`` result into the shared run state."""
        running = bool(summary["running"])
        return cls(
            running=running,
            status=summary["status"],
            # ``done_reason`` carries the LAST completion in the transcript,
            # which is the previous turn's once a new one is running — suppress
            # it so a live turn never reports a stale prior reason.
            done_reason="" if running else summary["done_reason"],
            title=summary["title"],
            recoverable=summary["recoverable"],
            terminated=summary["terminated"],
            terminated_at=summary["terminated_at"],
            extras={k: summary[k] for k in cls.OPTIONAL_KEYS if k in summary},
        )

    def wire(self) -> dict[str, Any]:
        """The flat mapping the ``/events`` body splices in."""
        return {
            "running": self.running,
            "status": self.status,
            "done_reason": self.done_reason,
            "title": self.title,
            "recoverable": self.recoverable,
            **self.extras,
            "terminated": self.terminated,
            "terminated_at": self.terminated_at,
        }

    def sse_frame(self) -> str:
        """The ``session_state`` frame the stream emits."""
        return f"data: {json.dumps({'type': 'session_state', **self.wire()})}\n\n"


@ns.route("/sessions/<string:session_id>/events")
class SessionEvents(Resource):
    """Return session events for polling."""

    @api.doc(
        security="apikey",
        params={
            "session_id": "Session id returned by POST /api/sessions.",
            "after": {
                "description": (
                    "Return only events with a timestamp strictly after this "
                    "value. Use the `ts` of the last event you received, "
                    "percent-encoded — these timestamps contain `+`, which is "
                    "otherwise decoded as a space. A value that is not an "
                    "ISO-8601 timestamp is rejected with 400 rather than "
                    "silently returning the whole transcript."
                ),
                "in": "query",
                "type": "string",
            },
            "truncate": {
                "description": (
                    "Set to 1 or true to cap large free-text payload fields "
                    "(results, tool inputs, errors) at 2000 characters."
                ),
                "in": "query",
                "type": "string",
            },
        },
        description=(
            "Return the session's event timeline plus authoritative run state — "
            "`running`, `status`, `done_reason`, `title`, and `recoverable`. Pass "
            "`after` (the `ts` of your last event) to fetch only newer events "
            "while polling; the status fields always describe the WHOLE session, "
            "never the returned window. Prefer the stream endpoint for push delivery."
        ),
    )
    @ns.response(200, "Events plus authoritative session status.", session_events_model)
    @kit.errors(
        400,
        404,
        descriptions={
            400: "The `after` cursor is not an ISO-8601 timestamp.",
            404: "No session with that id exists.",
        },
    )
    @kit.auth_error()
    @guard.requires("sessions.read")
    def get(self, session_id: str) -> tuple[dict, int]:
        """Poll session events

        Returns the session's event timeline plus authoritative run state:
        `running`, `status`, `done_reason`, `title`, and `recoverable`. Pass
        `after` to fetch only new events while polling; the status fields always
        describe the WHOLE session, never the returned window. Prefer the stream
        endpoint when you want push delivery.
        """
        # Unknown id must 404, not synthesize a phantom idle: without this
        # guard ``load_events`` returns [] and ``summarize_session`` fabricates a
        # placeholder ``{status:"idle", title:"Session <id>"}`` → a false 200.
        if not _session_exists(session_id):
            return _session_not_found(session_id)
        after_ts = request.args.get("after")
        # A filter this surface cannot apply is REFUSED, never widened. Failing
        # open here returned the entire transcript with a 200 — 14.8 MB on the
        # largest live session — so a client bug read as success on both ends
        # and nobody went looking. The live trigger is this API's own ``ts``
        # format: it contains ``+``, which arrives as a space when a client
        # echoes one back unencoded, and the parse then fails.
        # An EMPTY ``after`` is not a bad cursor, it is no cursor: it keeps
        # meaning "everything", which is what a first poll sends.
        if after_ts and EventCursor.parse(after_ts) is None:
            return {
                "message": (
                    "Query parameter `after` must be an ISO-8601 timestamp — pass the "
                    "`ts` of your last event, percent-encoded (its `+` is otherwise "
                    "decoded as a space)."
                )
            }, 400
        events = runtime.load_events(session_id, after_ts)
        # Opt-in payload cap: the console renders full ``result`` by design,
        # so only a caller (the MCP) that asks via ?truncate=1 gets the smaller
        # transcript — default behaviour is byte-identical.
        if request.args.get("truncate") in ("1", "true"):
            events = _truncate_event_freetext(events)
        notification_service.emit_completion(session_id)
        # Authoritative terminal-state + title for polling consumers (the MCP
        # facade reads these). ``summarize_session`` derives status/done_reason
        # and resolves the stored title; reuse it rather than recompute.
        # ``after_ts`` narrows the returned event window and MUST NOT narrow
        # this — status describes the whole session, so it is deliberately
        # summarized without the cursor. The unnarrowed scope is affordable
        # because the store hands the fold that session's digest rather than its
        # whole transcript: O(one record), not O(all history), per poll.
        summary = runtime.summarize_session(session_id)
        # Project through ``SessionStateFrame`` rather than spelling the fields
        # here: the SSE stream publishes the SAME run state as a
        # ``session_state`` frame, and a consumer moving between the two
        # transports must not observe a different shape. The projection reuses
        # the ONE ``summarize_session`` call above — a second independent
        # ``is_running`` could straddle a run starting/stopping and disagree,
        # whereas one call keeps the tuple atomic.
        return {
            "session_id": session_id,
            "events": events,
            **SessionStateFrame.from_summary(summary).wire(),
        }, 200

    @api.doc(
        security="apikey",
        params={"session_id": "Session id to mirror events into."},
        description=(
            "Append one or more transcript event records to a session — the "
            "ingest seam for a local-first CLI mirroring its authoritative local "
            "JSONL transcript to this deployment for cross-device visibility. Body "
            "is `{record}` (single) or `{records: [...]}` (batch); each record is "
            "an event `{type, payload, ts?}`. The session is materialised "
            "idempotently, so the first mirrored event creates it."
        ),
    )
    @ns.expect(session_events_ingest_request_model)
    @ns.response(202, "Events appended.", session_events_ingest_model)
    @kit.auth_error()
    @guard.requires("sessions.interact")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Ingest mirrored session events

        Append one or more event records to a session's transcript. The session
        is created idempotently on first use so an external local-first client
        (the CLI) can mirror its transcript here. Local JSONL stays authoritative
        on the client; this endpoint only stores what it is handed.
        """
        payload = request.get_json(silent=True) or {}
        records = payload.get("records")
        if records is None and isinstance(payload.get("record"), dict):
            records = [payload["record"]]
        if not isinstance(records, list) or not records:
            return {
                "message": "Body must include a `record` object or non-empty `records` list."
            }, 400
        clean = [rec for rec in records if isinstance(rec, dict) and rec.get("type")]
        if not clean:
            return {"message": "No valid event records (each needs a `type`)."}, 400
        runtime.ensure_session(session_id)
        for record in clean:
            runtime.append_event(session_id, record)
        return {"session_id": session_id, "appended": len(clean)}, 202


@ns.route("/sessions/<string:session_id>/timeline")
class SessionTimeline(Resource):
    """Return the session's transcript already assembled into conversation turns."""

    @api.doc(
        security="apikey",
        params={"session_id": "Session id returned by POST /api/sessions."},
        description=(
            "Return the session's transcript assembled into conversation rows — "
            "turn boundaries, plans, checklists, questions, triggers, recoveries "
            "and run failures, in transcript order. Prefer this over deriving "
            "turns from the raw event log: the assembly rules live in one place, "
            "so every client renders the same conversation. Turn bodies are NOT "
            "inlined; fetch the raw events endpoint when you need them."
        ),
    )
    @ns.response(200, "The assembled conversation.", session_timeline_model)
    @kit.errors(404, descriptions={404: "No session with that id exists."})
    @kit.auth_error()
    @guard.requires("sessions.read")
    def get(self, session_id: str) -> tuple[dict, int]:
        """Get the assembled timeline

        Returns the session's transcript already reconstructed into turns and
        markers. A terminated session is still fully readable — termination
        ends a session's run, it does not withdraw its transcript.
        """
        # Same guard as the events route: an unknown id must 404 rather than
        # render an empty-but-successful conversation.
        if not _session_exists(session_id):
            return _session_not_found(session_id)
        transcript = TranscriptTimeline.build(runtime.load_events(session_id))
        summary = runtime.summarize_session(session_id)
        # A turn's events are its whole slice of the transcript, so inlining
        # them here would repeat the entire event log once per turn. Clients
        # that need bodies read the events endpoint, which already caps
        # oversized free-text fields.
        return {
            "session_id": session_id,
            "entries": [
                entry.model_dump(exclude={"turn": {"events"}}, exclude_none=True)
                for entry in transcript.entries
            ],
            "open_turn": (
                transcript.open_turn.model_dump(exclude={"events"}, exclude_none=True)
                if transcript.open_turn
                else None
            ),
            "running": summary["running"],
            "status": summary["status"],
            "terminated": summary["terminated"],
        }, 200


class SessionProjectRebindBody(BaseModel):
    """The wire body of an explicit project rebind: a project name and nothing else.

    ``extra="forbid"`` is the load-bearing half rather than hygiene. This is the
    only surface that writes a session's ``cwd`` outright, so a client able to
    smuggle one here would anchor a session at any host path it named, straight
    past ``api.allow_external_cwd``. Refusing every field but ``project`` means
    the directory can only ever come from the server's own project resolution,
    and a renamed or server-owned field earns a 400 naming it instead of a
    silent no-op.
    """

    model_config = ConfigDict(extra="forbid")

    project: str = Field(
        ...,
        description=(
            "Name of the project to bind the session to — a configured project, "
            "or `managed:<project_id>` for a server-managed one. Its directory is "
            "resolved server-side and stored with it."
        ),
    )

    @field_validator("project")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        """A blank name is not a request to unbind — this surface only ever binds."""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must name a project")
        return cleaned


class SessionBindingRoutes:
    """Reads a session's durable binding, and rebinds the project it runs against.

    ONE owner for the projection both routes return, so the body a front end
    swaps into its cache after a rebind cannot drift from the body it hydrated
    from. Collaborators arrive as injected fields rather than being read off
    module scope, so a test points this at fresh stores without patching
    globals.
    """

    def __init__(
        self,
        *,
        specs: SessionSpecStore,
        load_last_context: Callable[[str], dict[str, object]],
        append_context_event: Callable[[str, dict[str, object]], None],
        session_exists: Callable[[str], bool],
        terminated_guard: Callable[[str], tuple[dict, int] | None],
        resolve_project_cwd: Callable[[dict[str, object]], str | None],
    ) -> None:
        """Bind the context reader, guards and project resolver this surface needs."""
        self.specs = specs
        self.load_last_context = load_last_context
        self.append_context_event = append_context_event
        self.session_exists = session_exists
        self.terminated_guard = terminated_guard
        self.resolve_project_cwd = resolve_project_cwd

    def projection(self, session_id: str) -> tuple[dict, int]:
        """The binding plus its fail-closed editable map — what BOTH routes return."""
        if not self.session_exists(session_id):
            return _session_not_found(session_id)
        spec = self.specs.load(session_id)
        return {
            "session_id": session_id,
            "spec": spec.projection(),
            "editable": spec.editable_fields(),
            "source": "spec" if self._has_typed_mirror(session_id) else "legacy_context",
        }, 200

    def rebind_project(self, session_id: str, body: object) -> tuple[dict, int]:
        """Move a session onto *body*'s project, re-resolving the directory it runs in.

        Deliberately NOT reachable as a request override: ``editable.project``
        stays false so an ordinary turn cannot re-scope a bound session in
        passing. This is the explicit action beside that guard, and it writes
        the project NAME and the DIRECTORY it resolves to TOGETHER — writing
        only the name is what left sessions running somewhere else entirely.
        Nothing is persisted until the directory resolves, so a refused rebind
        leaves the previous binding whole rather than half-applied.
        """
        if not self.session_exists(session_id):
            return _session_not_found(session_id)
        terminated = self.terminated_guard(session_id)
        if terminated is not None:
            return terminated
        parsed = self._parse(body)
        project_cwd = self._resolve(parsed.project)
        spec = self.specs.load(session_id)
        # Re-validate rather than ``model_copy(update=…)``, for the reason
        # ``merge_request_overrides`` gives: ``model_copy`` skips every field
        # validator, so the rebound values would land un-normalized.
        rebound = SessionSpec.model_validate(
            {**spec.model_dump(), "project": parsed.project, "cwd": project_cwd}
        )
        self._save(session_id, rebound)
        return self.projection(session_id)

    def _parse(self, body: object) -> SessionProjectRebindBody:
        """Validate the request body, or raise the 400 the taxonomy renders."""
        if not isinstance(body, dict):
            raise RequestInvalid.field_error(
                "body", "request body must be a JSON object", shape="envelope"
            )
        try:
            return SessionProjectRebindBody.model_validate(body)
        except ValidationError as exc:
            raise RequestInvalid.from_validation_error(exc, shape="envelope") from exc

    def _resolve(self, project: str) -> str:
        """The project's directory, through the SAME resolver session creation uses.

        Sharing the resolver is what stops the two doors disagreeing about what a
        project name means — an unknown, pathless or missing-directory project
        raises there and becomes a 400 here, rather than being re-checked against
        a second copy of the rules.
        """
        try:
            resolved = self.resolve_project_cwd({"project": project})
        except ValueError as exc:
            raise RequestInvalid.field_error("project", str(exc), shape="envelope") from exc
        if not resolved:
            raise RequestInvalid.field_error(
                "project", f"Project '{project}' has no resolvable directory.", shape="envelope"
            )
        return resolved

    def _save(self, session_id: str, spec: SessionSpec) -> None:
        """Persist the rebound spec, carrying non-spec gating keys forward.

        A context reader takes the NEWEST event's payload verbatim, so appending
        a spec-only event would blank whatever gating keys the session already
        carried (``structured_workspace`` and friends). Same carry-forward the
        unattended-fire path does for the same reason.
        """
        payload = spec.to_context_payload()
        for key, value in self.load_last_context(session_id).items():
            if key not in SessionSpec.SPEC_OWNED_CONTEXT_KEYS:
                payload.setdefault(key, value)
        self.append_context_event(session_id, payload)

    def _has_typed_mirror(self, session_id: str) -> bool:
        """Whether a durable binding was recorded, vs one reconstructed from context.

        Delegates to the spec store, which answers it with the SAME bounded read
        its ``load`` opens with — a route scanning the transcript itself was a
        second way of asking one question, free to disagree with the binding it
        labels.
        """
        return self.specs.has_typed_mirror(session_id)


# Composition root for the binding surface. Every collaborator is late-bound
# (a module function or a lambda) for the reason ``_session_specs`` is: the test
# suite swaps ``runtime`` wholesale, and a store captured here at import would
# keep answering from the one that existed then.
_session_binding = SessionBindingRoutes(
    specs=_session_specs,
    load_last_context=_load_last_context,
    append_context_event=lambda session_id, payload: runtime.append_context_event(
        session_id, payload
    ),
    session_exists=_session_exists,
    terminated_guard=_terminated_guard,
    resolve_project_cwd=_resolve_project_cwd,
)


class _BindingResource(Resource):
    """Base Resource receiving the one binding controller via ``resource_class_kwargs``.

    Flask-RESTX passes the ``Api`` as the first positional arg; ``controller``
    rides alongside it as an injected keyword so neither Resource below reaches
    into module scope for its collaborators.
    """

    def __init__(
        self, api: Any = None, *args: Any, controller: SessionBindingRoutes, **kwargs: Any
    ) -> None:
        """Capture the injected controller alongside the RESTX ``Api`` positional."""
        super().__init__(api, *args, **kwargs)
        self.controller = controller


class SessionSpecView(_BindingResource):
    """Return the session's durable purpose binding plus what may be changed."""

    @api.doc(
        security="apikey",
        params={"session_id": "Session id returned by POST /api/sessions."},
        description=(
            "Return the session's binding — the purpose it was created for, and the "
            "model, fallback ladder, tool ceiling, capabilities and working directory "
            "it runs under — together with a server-declared `editable` map naming "
            "which of those a request may override. Hydrate a composer from this "
            "rather than from client-global defaults: re-deriving the context per "
            "turn is what lets a follow-up arrive on a different model with an "
            "unrelated tool set. `editable` is authoritative and fail-closed; a "
            "field it reports false for is refused server-side, so a client should "
            "render it read-only rather than offer an edit that will be ignored."
        ),
    )
    @ns.response(200, "The session's binding and its editable map.", session_spec_model)
    @kit.errors(404, descriptions={404: "No session with that id exists."})
    @kit.auth_error()
    @guard.requires("sessions.read")
    def get(self, session_id: str) -> tuple[dict, int]:
        """Get the session binding

        Returns what this session is bound to run as, and which of those fields a
        request may legitimately override. A session created before bindings were
        recorded reports one reconstructed from its persisted context, flagged by
        `source` so a client can tell a durable binding from a reconstruction.
        """
        return self.controller.projection(session_id)


class SessionProjectView(_BindingResource):
    """Rebind the project, and therefore the directory, a session runs against."""

    @api.doc(
        security="apikey",
        params={"session_id": "Session id returned by POST /api/sessions."},
        description=(
            "Bind the session to a different project. `editable.project` is false on "
            "a session created for a purpose, so an ordinary query cannot re-scope "
            "one in passing — that guard is what stops a stray request silently "
            "moving a session, and this route is the deliberate action beside it "
            "rather than a way around it. The project's directory is resolved "
            "server-side and stored with the name, so the next turn runs there. "
            "Only `project` is accepted: any other field, including `cwd`, is a 400. "
            "Returns the same body as GET /api/sessions/{session_id}/spec."
        ),
    )
    @ns.expect(session_project_rebind_model)
    @ns.response(200, "Rebound; body is the updated binding.", session_spec_model)
    @ns.response(
        410, "Session is permanently terminated.", session_terminated_error_model
    )
    @kit.errors(
        400,
        404,
        descriptions={
            400: (
                "The body named no project, named one that is not configured or has "
                "no directory, or carried a field other than `project`."
            ),
            404: "No session with that id exists.",
        },
    )
    @kit.auth_error()
    @guard.requires("sessions.interact")
    def put(self, session_id: str) -> tuple[dict, int]:
        """Rebind the session's project

        Moves the session onto another project and re-resolves the working
        directory it runs in, persisting both together. Returns the updated
        binding in the same shape as GET /api/sessions/{session_id}/spec.
        """
        return self.controller.rebind_project(session_id, request.get_json(silent=True))


ns.add_resource(
    SessionSpecView,
    "/sessions/<string:session_id>/spec",
    resource_class_kwargs={"controller": _session_binding},
)
ns.add_resource(
    SessionProjectView,
    "/sessions/<string:session_id>/project",
    resource_class_kwargs={"controller": _session_binding},
)


@ns.route("/sessions/<string:session_id>/stream")
class SessionStream(Resource):
    r"""Stream session events via Server-Sent Events (push-based).

    Subscribes to the in-process ``SessionEventBus`` so an appended event wakes
    the stream immediately — no 0.5s poll and no per-event full-transcript
    re-read. The backlog is loaded exactly once; live events arrive on the
    subscription queue. Wire format is unchanged (``data: <json>\n\n`` plus a
    terminal ``stream_end``) so the console consumer is unaffected.
    """

    # Block on the queue for at most this long before emitting an SSE comment
    # keepalive (or re-checking run liveness).
    HEARTBEAT_S = 15.0
    # Close an idle stream after this long with no events (preserves the old
    # 5-minute auto-close).
    IDLE_CLOSE_S = 300.0
    # Streams and every other endpoint are served from ONE pool of request
    # threads, and a stream holds its thread until it closes — so unbounded
    # streams starve the whole API rather than just themselves. The bound lives
    # on the class that spends it (see stream_capacity.py for the failure it
    # prevents); a test rebinds this field rather than the config cache.
    capacity: ClassVar[StreamCapacity] = StreamCapacity(
        lambda: get_config().api.max_concurrent_streams
    )

    @staticmethod
    def _state_frame(session_id: str, session_runtime: SessionRuntime) -> str:
        """Render the shared run-state projection as a ``session_state`` frame."""
        summary = session_runtime.summarize_session(session_id)
        return SessionStateFrame.from_summary(summary).sse_frame()

    @staticmethod
    def _stream_events(
        session_id: str,
        session_runtime: SessionRuntime,
        bus: SessionEventBus,
        *,
        heartbeat_s: float = HEARTBEAT_S,
        idle_close_s: float = IDLE_CLOSE_S,
        after: str | None = None,
        _sub: Subscription | None = None,
        executor: bool = False,
    ) -> Iterator[str]:
        """Yield SSE frames for a session: backlog once, then live + heartbeats.

        Subscribes BEFORE loading the backlog so the queue is a superset of all
        post-subscribe events; the overlap with the backlog (events appended in
        the subscribe↔load race window) is dropped by exact content key.
        ``after`` trims the REPLAY leg only — the live subscription is
        untouched, so a reconnecting client that already holds the earlier
        transcript pays for the delta instead of the whole log.
        ``_sub`` is a test seam for injecting a pre-seeded subscription.

        Cost: ``O(one record)`` on the wire once ``after`` is supplied, but
        still ``O(one record)`` in the STORE on every connect — the cursor is
        applied after ``load_transcript`` returns, so it narrows the response
        without narrowing the read. That is deliberate but partial: the
        inclusive ``>=`` bound this needs has no store-level equivalent yet
        (``load_events`` is strictly-after, which would drop same-timestamp
        siblings), so pushing the filter down waits on a range read that
        honours the inclusive bound. The wire cost is the one a viewer feels;
        the read cost is bounded by a single session, never by all history.

        Concurrency: each open stream holds one request slot for as long as it
        lives, and at most ``api.max_concurrent_streams`` may be open at once —
        the route refuses the next caller with a 503 rather than letting it
        queue behind the thread pool. That bound and this generator solve two
        halves of one problem: the bound caps how many slots exist, while the
        generator gives its slot back the moment a run stops rather than at the
        next heartbeat. Without the fast release the bound would be spent by
        idle viewers; with it, a held slot means a run genuinely in flight.
        """
        sub = _sub if _sub is not None else bus.subscribe(session_id, executor=executor)
        try:
            backlog = session_runtime.session_store.load_transcript(session_id)
            if after:
                # ``>=``, not ``>``: several events can share one timestamp, so
                # a strictly-greater cursor silently drops the siblings of the
                # event the client last saw. Re-sending that one timestamp
                # group costs a few frames and the client's content-key dedupe
                # absorbs it — the reverse mistake loses transcript rows on
                # every reconnect, and nothing downstream would report it.
                backlog = [e for e in backlog if str(e.get("ts", "")) >= after]
            backlog_keys = {json.dumps(e, sort_keys=True) for e in backlog}
            for event in backlog:
                yield f"data: {json.dumps(event)}\n\n"
            # Publish the run state immediately after the replay, BEFORE the
            # first blocking get(). Without it a client cannot tell a live run
            # from an idle session until the server closes the stream one
            # heartbeat later, so every idle session would open showing a
            # spinner. This frame is what makes the stream a COMPLETE
            # transport rather than one that still needs a polled companion.
            yield SessionStream._state_frame(session_id, session_runtime)

            def emit(event: EventRecord) -> str | None:
                """Dedup an event against the backlog; return its SSE frame or None."""
                key = json.dumps(event, sort_keys=True)
                if key in backlog_keys:
                    # Already emitted from the backlog — drop the race-window dup.
                    # Assumes at-most-one race-window duplicate per content key;
                    # if a future change ever double-publishes, the safe
                    # direction is "delivered, not dropped" (so only discard once).
                    backlog_keys.discard(key)
                    return None
                return f"data: {json.dumps(event)}\n\n"

            idle_elapsed = 0.0
            while True:
                # Read liveness BEFORE choosing how long to block. An idle
                # session has nothing to wait for, and blocking a full
                # heartbeat to discover that pins one of the API's few request
                # slots for 15s per open viewer — the cost that made pushing
                # this stream to every console tab unaffordable. Polling the
                # queue without blocking closes a finished session's stream in
                # milliseconds; the client re-subscribes cheaply because the
                # `after` cursor makes its replay empty. A slot is then held
                # only while a run is genuinely in flight, which is exactly
                # when push is worth paying for.
                running = session_runtime.is_running(session_id)
                try:
                    event = sub.queue.get(timeout=heartbeat_s if running else 0.0)
                except queue.Empty:
                    if not running:
                        # Close race: the run thread publishes its terminal
                        # event (e.g. completion) right before is_running flips
                        # False. Drain the queue before closing so that final
                        # event is delivered, not dropped.
                        while True:
                            try:
                                pending = sub.queue.get_nowait()
                            except queue.Empty:
                                break
                            frame = emit(pending)
                            if frame is not None:
                                yield frame
                        # Re-publish the run state before closing: the terminal
                        # values (status, done_reason, recoverable) are only
                        # settled now, and this is the client's last chance to
                        # read them off the stream.
                        yield SessionStream._state_frame(session_id, session_runtime)
                        yield 'data: {"type": "stream_end"}\n\n'
                        break
                    idle_elapsed += heartbeat_s
                    if idle_elapsed >= idle_close_s:
                        break
                    yield ": heartbeat\n\n"
                    continue
                idle_elapsed = 0.0
                frame = emit(event)
                if frame is not None:
                    yield frame
        finally:
            if _sub is None:
                bus.unsubscribe(session_id, sub)

    @api.doc(
        security="apikey",
        params={
            "session_id": "Session id returned by POST /api/sessions.",
            "api_key": {
                "description": (
                    "API key, for EventSource clients that cannot set the "
                    "`X-API-Key` header."
                ),
                "in": "query",
                "type": "string",
            },
            "after": {
                "description": (
                    "Replay only events at or after this timestamp. Pass the `ts` "
                    "of the last event you received when reconnecting, so a "
                    "resumed stream costs the delta instead of the whole "
                    "transcript. The bound is INCLUSIVE — the timestamp you pass "
                    "is re-sent, because several events can share one — so "
                    "de-duplicate by event content."
                ),
                "in": "query",
                "type": "string",
            },
        },
        description=(
            "Open a Server-Sent Events stream. The stored backlog is replayed "
            "first (trimmed by `after` when given), then new events are pushed "
            "as they happen (`data: <json>` frames). A `session_state` frame "
            "carrying `running`, `status`, `done_reason`, `title`, `recoverable` "
            "and `terminated` is emitted after the replay and again before the "
            "stream closes, so a client needs no second endpoint for run state. "
            "Heartbeat comments keep the connection alive and a terminal "
            "`stream_end` frame is sent when the run finishes. EventSource cannot "
            "set headers, so the key may be passed as the `api_key` query param."
        ),
    )
    @api.response(200, "Server-Sent Events stream (`text/event-stream`).")
    @api.response(503, "Too many streams already open. Retry after `Retry-After`.")
    @kit.auth_error()
    def get(self, session_id: str) -> Response:
        """Stream session events

        Opens a Server-Sent Events stream. The stored backlog is replayed
        first (trimmed by `after` when given), then new events are pushed as
        they happen. A `session_state` frame carries the authoritative run
        state after the replay and again before the stream closes. Heartbeat
        comments keep the connection alive, and a terminal `stream_end` frame
        is sent when the run finishes. Because EventSource cannot set headers,
        the API key may be passed as the `api_key` query parameter instead.
        """
        # NOT migrated to ``@guard.requires`` deliberately. This route hand-builds
        # its refusal as a ``Response`` with ``json.dumps``, which emits no trailing
        # newline; the decorator returns the dict form that flask-restx serializes
        # WITH one. Every other route already sends the newline, so adopting the
        # decorator here is a one-byte wire change on a body an SSE client parses.
        # Migrate it together with a deliberate decision to normalize that byte.
        auth_error = _require_api_key() or _require_permission("sessions.read")()
        if auth_error:
            return Response(
                json.dumps(auth_error[0]),
                status=auth_error[1],
                mimetype="application/json",
            )

        # Admission comes AFTER auth: an anonymous caller must not be able to
        # spend a slot, nor learn how many are left. Refusing here rather than
        # inside the generator is what keeps the refusal an ordinary JSON
        # response — the body has not begun, so nothing half-open reaches the
        # client. Every path out of the stream returns the slot via LeasedStream.
        if not self.capacity.try_acquire():
            refusal = StreamCapacityExhausted.for_limit(self.capacity.limit())
            body, status = refusal.response()
            return Response(
                json.dumps(body),
                status=status,
                mimetype="application/json",
                headers={"Retry-After": str(refusal.RETRY_AFTER_SECONDS)},
            )

        bus = get_session_event_bus()
        return Response(
            LeasedStream(
                stream_with_context(
                    self._stream_events(
                        session_id,
                        runtime,
                        bus,
                        after=request.args.get("after"),
                        # A stream is an EXECUTOR only when its client says it
                        # can drive the device. The header rides every request
                        # from Aura, including this one, so the claim is the
                        # client's own rather than something inferred from the
                        # transcript — and a viewer that never makes the claim
                        # can never be mistaken for the phone.
                        executor=_advertises_device_control(),
                    )
                ),
                self.capacity,
            ),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Access-Control-Allow-Origin": _CORS_ORIGIN,
            },
        )


def _advertises_device_control() -> bool:
    """True when THIS request's client says it can service device tools.

    Read from ``X-Mewbo-Capabilities`` — the same header the session's
    capability set is parsed from, so a client that advertises the capability
    on its queries necessarily advertises it on the stream it opens, with no
    second contract to keep in step.
    """
    return DEVICE_CONTROL_CAPABILITY in parse_capability_header(
        request.headers.get(CAPABILITY_HEADER, "")
    )


@ns.route("/sessions/<string:session_id>/message")
class SessionMessage(Resource):
    """Steer a running session, or re-engage an idle/finished one."""

    @api.doc(
        security="apikey",
        params={"session_id": "Session id returned by POST /api/sessions."},
        description=(
            "While a run is active, the `text` is enqueued as a steering message "
            "and the call returns 202. On an idle or finished session the message "
            "re-engages it: a fresh run starts with `text` as its query and the "
            "call returns 200 with the new `run_id` (form `<session_id>:r<seq>`). "
            "Only a terminated session rejects."
        ),
    )
    @ns.response(
        202, "Steering message enqueued into the active run.", session_message_enqueued_model
    )
    @ns.response(
        200,
        "Idle session re-engaged; body carries the new `run_id`.",
        session_message_enqueued_model,
    )
    @ns.response(
        410, "Session is permanently terminated.", session_terminated_error_model
    )
    @kit.errors(400, shape="message", descriptions={400: "The `text` field is missing or empty."})
    @kit.errors(409, shape="message", descriptions={409: "The session could not be re-engaged."})
    @kit.auth_error()
    @ns.expect(session_message_model)
    @guard.requires("sessions.interact")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Send a session message

        While a run is active the text is enqueued as a steering message for
        the agent and the call returns 202. On an idle or finished session the
        message re-engages it instead: a fresh run starts with the text as its
        query and the call returns 200 with the new `run_id`. Run ids have the
        form `<session_id>:r<seq>`. Only a terminated session rejects.
        """
        payload = request.get_json(silent=True) or {}
        text = payload.get("text")
        if not text or not isinstance(text, str):
            return {"message": "'text' is required"}, 400
        # A terminated session rejects both steering and re-engagement.
        terminated = _terminated_guard(session_id)
        if terminated is not None:
            return terminated
        # No active run → re-engage through the shared delivery seam, which
        # inherits the session's persisted context (model/mode/tool allowlist)
        # like /query and /recover do.
        delivered = _deliver_user_turn(session_id, text)
        if delivered.outcome == "steered":
            return {"session_id": session_id, "enqueued": True}, 202
        if delivered.outcome == "refused":
            return {"message": "Session is already running."}, 409
        return {"session_id": session_id, "enqueued": True, "run_id": delivered.run_id}, 200


@ns.route("/sessions/<string:session_id>/interrupt")
class SessionInterrupt(Resource):
    """Interrupt the current step of a running session."""

    @api.doc(
        security="apikey",
        params={"session_id": "Session id returned by POST /api/sessions."},
        description=(
            "Stop the currently executing step of an active run and return 202. "
            "Interrupting an idle session is an idempotent no-op that returns 200 "
            "with `interrupted` false, so the call is always safe to make."
        ),
    )
    @ns.response(202, "Current step interrupted.", session_interrupt_model)
    @ns.response(
        200,
        "Session was idle; nothing to interrupt (`interrupted: false`).",
        session_interrupt_model,
    )
    @ns.response(
        410, "Session is permanently terminated.", session_terminated_error_model
    )
    @kit.auth_error()
    @guard.requires("sessions.interact")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Interrupt a session

        Stops the currently executing step of an active run and returns 202.
        Interrupting an idle session is an idempotent no-op that returns 200
        with `interrupted` false, so the call is always safe to make.
        """
        # Terminated sessions reject interrupt too, for a consistent contract.
        terminated = _terminated_guard(session_id)
        if terminated is not None:
            return terminated
        ok = runtime.interrupt_step(session_id)
        if not ok:
            return {"session_id": session_id, "interrupted": False}, 200
        return {"session_id": session_id, "interrupted": True}, 202


def _parse_device_tool_result_body(body: dict[str, object]) -> dict[str, object] | None:
    """Validate a device-tool result POST body; ``None`` on a malformed shape.

    Strips ``call_token`` (a bearer secret, checked separately by the caller)
    from the returned payload — this is exactly the ``{"status": "ok",
    "result": ...}`` / ``{"status": "error", "error": {...}}`` shape
    ``DeviceToolDispatcherImpl.dispatch`` returns, so it rides straight
    through to :meth:`DevicePendingCalls.resolve` with no reshaping.

    An error result with neither a non-empty ``error.code`` nor a non-empty
    ``error.message`` is rejected here (400), not merely defaulted downstream
    — ``{"status":"error","error":{}}`` is a valid-looking body a client could
    send, and letting it through would depend entirely on
    ``client_tools._error_envelope``'s blank-field defaulting to make the
    failure visible to the loop (review, F3). Reject early instead of
    trusting a second layer to compensate.
    """
    status = body.get("status")
    if status not in ("ok", "error"):
        return None
    if not isinstance(body.get("call_token"), str) or not body["call_token"]:
        return None
    payload: dict[str, object] = {"status": status}
    if status == "ok":
        payload["result"] = body.get("result")
    else:
        error = body.get("error")
        if not isinstance(error, dict):
            return None
        code = str(error.get("code", "") or "").strip()
        message = str(error.get("message", "") or "").strip()
        if not code and not message:
            return None
        payload["error"] = error
    return payload


@ns.route("/sessions/<string:session_id>/device_tools/<string:call_id>/result")
class SessionDeviceToolResult(Resource):
    """Deliver a client-fulfilled result for a pending device-tool call."""

    @api.doc(
        security="apikey",
        params={
            "session_id": "Session id returned by POST /api/sessions.",
            "call_id": "The `call_id` from the `device_tool_call` event being answered.",
        },
        description=(
            "Deliver the client-side result for a pending `device_tool_call` "
            "event (see `context.device_tools` on POST /query). The caller "
            "presents the single-use `call_token` carried on that event, "
            "proving session-stream read access and preventing replay — NOT "
            "proof the response came from the specific device the call was "
            "dispatched to (any concurrent viewer of the session's SSE "
            "stream receives the same token; verified per-device identity "
            "is a future phase). A result may be delivered exactly once; a "
            "second POST for the same call returns 409."
        ),
    )
    @ns.response(
        200, "Result delivered; the waiting tool call resolves.", device_tool_resolved_model
    )
    @kit.errors(
        400,
        403,
        404,
        409,
        shape="message",
        descriptions={
            400: "The request body is malformed.",
            403: "`call_token` does not match the pending call.",
            404: "No pending call with that `call_id` (unknown, expired, or already delivered).",
            409: "A result was already delivered for this call.",
        },
    )
    @kit.auth_error()
    @ns.expect(device_tool_result_model)
    @guard.requires("sessions.interact")
    def post(self, session_id: str, call_id: str) -> tuple[dict, int]:
        """Deliver a device-tool result

        Delivers the client-side result for a pending `device_tool_call`
        event; the caller presents the call's single-use `call_token`,
        proving session-stream read access and anti-replay (not device
        identity — see the route description). A result may be delivered
        exactly once.
        """
        # Delivering a device-tool result advances a live run (it resolves the
        # tool call the agent is blocked on), so it is a mutating entry point and
        # a permanently terminated session must reject it with 410 like the other
        # run-advancing routes.
        terminated = _terminated_guard(session_id)
        if terminated is not None:
            return terminated
        body = request.get_json(silent=True) or {}
        payload = _parse_device_tool_result_body(body)
        if payload is None:
            return {"message": "Invalid device tool result body."}, 400
        call_token = str(body.get("call_token", ""))
        outcome = get_pending_calls().resolve(session_id, call_id, call_token, payload)
        if outcome == "ok":
            return {"resolved": True}, 200
        if outcome == "not_found":
            return {"message": "No pending device tool call with that id."}, 404
        if outcome == "bad_token":
            return {"message": "call_token does not match the pending call."}, 403
        return {"message": "A result was already delivered for this call."}, 409


@ns.route("/sessions/<string:session_id>/questions/<string:call_id>/answer")
class SessionQuestionAnswer(Resource):
    """Deliver a human answer for a pending ask-user question group."""

    @api.doc(
        security="apikey",
        params={
            "session_id": "Session id returned by POST /api/sessions.",
            "call_id": "The `call_id` from the `user_question` event being answered.",
        },
        description=(
            "Answer a `user_question` event (emitted while the agent blocks on "
            "its `ask_user_question` tool call — see the `ask_user` entry in "
            "`X-Mewbo-Capabilities`). The caller presents the single-use "
            "`call_token` carried on that event, proving session-stream read "
            "access and preventing replay (same threat model as device tools — "
            "not proof of WHICH surface answered; the `X-Mewbo-Surface` header "
            "is recorded as `answered_via` on the resulting "
            "`user_question_answered` event). One item per question, "
            "`selected_indexes` XOR `text`; free text is always accepted, and "
            "an optional `notes` string carries whatever the question's "
            "`notes_placeholder` invited.\n\n"
            "**A question stays answerable until it is answered.** While the "
            "run is still blocked the answer resolves that tool call directly "
            "(`delivery: \"run\"`). Once the wait has ended — the call's "
            "`timeout_seconds` elapsed, a steer superseded it, the run "
            "finished, or the process restarted — the answer is recovered "
            "against the durable event and delivered as a new user turn "
            "instead (`delivery: \"message\"`), steering an active run or "
            "re-engaging an idle one. Only a real prior answer (409) or a "
            "terminated session (410) refuses one."
        ),
    )
    @ns.response(200, "Answer accepted; `delivery` names where it landed.", question_answered_model)
    @ns.response(
        410, "Session is permanently terminated.", session_terminated_error_model
    )
    @kit.errors(
        400,
        403,
        404,
        409,
        422,
        shape="message",
        descriptions={
            400: "The request body is malformed, or `notes` exceeds the cap.",
            403: "`call_token` does not match the question.",
            404: "No question with that `call_id` in this session.",
            409: "An answer was already delivered, or the session could not accept this one.",
            422: "Answers do not fit the questions (count, bounds, or arity).",
        },
    )
    @kit.auth_error()
    @ns.expect(question_answer_model)
    @guard.requires("sessions.interact")
    def post(self, session_id: str, call_id: str) -> tuple[dict, int]:
        """Answer a user question

        Delivers the human's answers for a `user_question` event. While the
        run is blocked the `ask_user_question` call resolves with them
        (`delivery: "run"`); once the wait has ended they arrive as a new user
        turn instead (`delivery: "message"`). One answer item per question
        (`selected_indexes` XOR `text`), plus optional `notes`.
        """
        # Answering advances the session (it resolves the tool call the agent
        # is blocked on, or lands as a fresh turn) — a permanently terminated
        # session rejects with 410 like every other run-advancing route.
        terminated = _terminated_guard(session_id)
        if terminated is not None:
            return terminated
        body = request.get_json(silent=True) or {}
        call_token = body.get("call_token")
        raw_answers = body.get("answers")
        notes = body.get("notes")
        if (
            not isinstance(call_token, str)
            or not call_token
            or not isinstance(raw_answers, list)
            or not raw_answers
            or (notes is not None and not isinstance(notes, str))
            or set(body) - {"call_token", "answers", "notes"}
        ):
            return {"message": "Invalid question answer body."}, 400
        if notes is not None and len(notes) > MAX_QUESTION_NOTES_CHARS:
            return {
                "message": f"'notes' must be at most {MAX_QUESTION_NOTES_CHARS} characters."
            }, 400
        try:
            items = [QuestionAnswerItem.model_validate(entry) for entry in raw_answers]
        except ValidationError as exc:
            return {"message": f"Invalid answer item: {exc}"}, 400
        answered_via = request.headers.get("X-Mewbo-Surface", "api")
        # Constructed per request (the wiki-settings idiom) so it reads the
        # CURRENT runtime rather than whichever one existed at import.
        router = QuestionAnswerRouter(
            runtime=runtime,
            pending=get_pending_questions(),
            deliver=_deliver_user_turn,
        )
        routed = router.route(
            session_id, call_id, call_token, items, answered_via=answered_via, notes=notes
        )
        if routed.outcome == "ok":
            return {"resolved": True, "delivery": routed.delivery}, 200
        if routed.outcome == "not_found":
            return {"message": routed.detail}, 404
        if routed.outcome == "bad_token":
            return {"message": routed.detail}, 403
        if routed.outcome == "invalid":
            return {"message": routed.detail}, 422
        return {"message": routed.detail}, 409


def _try_wiki_indexing_resume(session_id: str, action: str) -> dict | None:
    """Dispatch to the wiki checkpoint resume when *session_id* is an indexing job.

    A wiki **indexing** session's "Continue" must route to the checkpoint
    :class:`WikiResume` (Part B) — re-cloning at the recorded commit
    and skipping already-done phases — not the generic resolve_recovery_query
    path. This keeps clients agnostic: one endpoint handles every origin.

    Returns the recover-response dict (``{session_id, action, accepted, job_id,
    status}`` — the checkpoint path is monitored via the wiki SSE stream keyed by
    ``job_id``, not the generic ``run_id``) when the session maps to a
    *recoverable* indexing job; ``None`` when it does not (so the caller falls
    through to the generic path). A wiki **Q&A** session has no indexing job, so
    it returns ``None`` and re-runs generically.

    Guarded import: ``runtime.wiki_store`` is only set when the ``wiki`` extra is
    installed (see ``init_wiki``); a graph-less install or any failure degrades
    to the generic path rather than crashing.
    """
    store = getattr(runtime, "wiki_store", None)
    if store is None:
        return None
    try:
        from mewbo_api.wiki.resume import WikiResume

        job_id = store.find_job_by_session(session_id)
        if not job_id:
            return None
        job = store.get_job(job_id)
        if job is None or not job.is_resumable:
            return None
        # ``continue`` = checkpoint resume (skip done phases); ``retry`` =
        # restart this index from scratch (no-skip rebuild, same job_id) — the
        # user's "Restart" intent, honoured rather than silently down-graded to
        # a continue.
        result = WikiResume.resume(
            store, runtime, job_id, hook_manager=_hook_manager,
            restart=(action == "retry"),
        )
    except Exception as exc:  # pragma: no cover - defensive; fall back to generic
        logging.warning("wiki indexing resume dispatch failed for %s: %s", session_id, exc)
        return None
    # Adapt the WikiResume result ({job_id, session_id, status}) to the recover
    # response shape. The wiki checkpoint path re-drives the indexer AgentDef
    # via its own seam; callers monitor it via the wiki SSE stream
    # (``GET /v1/wiki/index/<job_id>/stream``), keyed by ``job_id`` — so the
    # generic ``run_id`` is not the monitoring handle here. ``slug`` lets the
    # client deep-link the indexing screen to the right repo.
    return {
        "session_id": result.get("session_id", session_id),
        "action": action,
        "accepted": True,
        "job_id": result.get("job_id"),
        "slug": job.slug,
        "status": result.get("status"),
    }


@ns.route("/sessions/<string:session_id>/recover")
class SessionRecovery(Resource):
    """Retry the last user query or continue after a failed run.

    Body: ``{"action": "retry" | "continue"}``. The endpoint resolves the
    appropriate query text (last user message for ``retry``; a synthetic
    recovery prompt for ``continue``), appends a ``recovery`` audit event
    to the transcript, and starts a fresh async run via the existing
    ``start_async`` pathway — prior events are automatically rebuilt into
    the system prompt by ``ContextBuilder``.

    Guarded: returns 409 if a run is already active; 400 if ``action`` is
    malformed or there is no prior user message to recover from.
    """

    @api.doc(
        security="apikey",
        params={"session_id": "Session id returned by POST /api/sessions."},
        description=(
            "Restart work on a failed or incomplete session. `retry` re-runs the "
            "last user query (optionally replaced via `edited_text`); `continue` "
            "resumes from where the run stopped. The run inherits the session's "
            "prior context. Wiki indexing sessions resume from their checkpoint "
            "and return a `job_id` to monitor instead of a `run_id`."
        ),
    )
    @ns.response(
        202,
        "Recovery run started; body carries `run_id` (or `job_id` for wiki jobs).",
        session_recover_response_model,
    )
    @ns.response(
        410, "Session is permanently terminated.", session_terminated_error_model
    )
    @kit.errors(
        400,
        shape="message",
        descriptions={400: "`action` is not `retry`/`continue`, or nothing to recover from."},
    )
    @kit.errors(
        409, shape="message", descriptions={409: "A run is already active for this session."}
    )
    @kit.auth_error()
    @ns.expect(session_recover_model)
    @guard.requires("sessions.interact")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Recover a session

        Restarts work on a failed or incomplete session. `retry` re-runs the
        last user query, optionally edited via `edited_text`; `continue`
        resumes from where the run stopped. The run inherits the session's
        prior context and settings. Wiki indexing sessions resume from their
        checkpoint instead and return a `job_id` to monitor rather than a
        `run_id`.
        """
        # A terminated session cannot be recovered — it is a hard dead-end.
        terminated = _terminated_guard(session_id)
        if terminated is not None:
            return terminated
        body = request.get_json(silent=True) or {}
        action = body.get("action")
        if action not in ("retry", "continue"):
            return {"message": "'action' must be 'retry' or 'continue'"}, 400
        from_ts: str | None = body.get("from_ts")
        edited_text: str | None = body.get("edited_text")
        model_override: str | None = body.get("model")
        if runtime.is_running(session_id):
            return {"message": "Session is already running."}, 409

        # Origin-aware dispatch (Part F4): a wiki INDEXING session's
        # recovery must route to the checkpoint ``WikiResume`` (re-clone at the
        # recorded commit + skip done phases), not the generic stitch. Server-
        # side so clients stay agnostic — one endpoint handles every origin. A
        # wiki Q&A session has no indexing job, so this returns None and the
        # generic path re-runs it.
        wiki_resumed = _try_wiki_indexing_resume(session_id, action)
        if wiki_resumed is not None:
            return wiki_resumed, 202

        try:
            user_query = runtime.resolve_recovery_query(
                session_id,
                action,
                from_ts=from_ts,
                replacement_text=edited_text,
            )
        except ValueError as exc:
            return {"message": str(exc)}, 400
        except RuntimeError as exc:
            return {"message": str(exc)}, 409

        # Reuse the same dispatch shape as SessionQuery.post so recovered
        # runs inherit the session's context and settings.
        last_context = _load_last_context(session_id)
        # Read the binding BEFORE the appends below rewrite the transcript tail.
        spec = _session_specs.load(session_id)
        mode = _parse_mode(last_context.get("mode"))
        # Tolerant: recovery reads PERSISTED context it can't 400 on behalf
        # of — a poisoned prior write self-heals (drops device tools, keeps
        # going) instead of bricking the session (review, F6).
        allowed_tools, extra_session_tools = _derive_tool_grants_tolerant(session_id, last_context)
        # The typed binding is authoritative. A legacy purpose-bound app can
        # carry a project key that was never catalog-valid; recovery must retain
        # the ordinary session-temp fallback rather than make that historical
        # value permanently unrecoverable.
        project_cwd = spec.cwd
        if project_cwd is None and not spec.purpose_bound:
            try:
                project_cwd = _resolve_project_cwd({"context": last_context})
            except ValueError as exc:
                return {"message": str(exc)}, 400
        if project_cwd is None:
            project_cwd = _resolve_session_cwd(session_id) or session_temp_dir(session_id)
        # Same precedence as ``_deliver_user_turn``: an explicit override, then
        # the typed binding, then the loose key for pre-mirror transcripts. The
        # spec was already the source for ``fallback_models`` a few lines down,
        # so reading the model itself off the newest context event meant a
        # recovered run could come back on a DIFFERENT model than the ladder it
        # was recovered with.
        model_name = model_override or spec.model or str(last_context.get("model", "")) or None
        if model_override:
            # Choosing a model is a sanctioned override, so it updates the BINDING.
            # Persisting it as a model-only context event (the prior behaviour) made
            # that event the newest one, and every reader that takes the newest
            # payload verbatim then saw a session with no tool ceiling, no playbook
            # and no cwd. Re-validate rather than model_copy: the latter skips field
            # validators, so a blank/garbage model_override would land un-normalized
            # instead of collapsing to "no override" like every other entry path.
            spec = SessionSpec.model_validate({**spec.model_dump(), "model": model_override})
            _session_specs.save(
                session_id,
                spec,
                # The loose key records what this turn will actually bind, so it has
                # to be the same union the run scope below resolves — writing the
                # bare advertisement here would re-bury the session's own capability
                # in the very event the next re-engage reads back.
                capabilities=spec.run_capabilities(
                    _persisted_client_capabilities(last_context)
                ),
            )
        # Re-inject capability-gating context (client_capabilities /
        # structured_workspace) so a recovered wiki/QA/structured session keeps
        # its capability — the orchestrator reads the MOST-RECENT context event,
        # and the model-override append above (or the recovery audit) would
        # otherwise leave a gating-less event as the latest one (F1).
        runtime.reinject_recovery_context(session_id)
        budget = _extract_session_step_budget(last_context)
        max_iters = int(get_config_value("agent", "max_iters", default=30))
        scope = _run_scope(
            allowed_tools=allowed_tools,
            # Recovery is a re-engage like any other: a recovered wiki session must
            # come back with ``wiki`` bound, not with only the recovering client's
            # rendering set.
            client_capabilities=spec.run_capabilities(
                _persisted_client_capabilities(last_context)
            ),
            strict_tool_scope=_extract_strict_tool_scope(last_context),
        )
        run_id = runtime.start_async(
            session_id=session_id,
            user_query=user_query,
            model_name=model_name,
            # The persisted ladder is part of the binding, and a run being
            # recovered is exactly when its auto-heal chain matters most —
            # omitting it here leaves the retry as defenceless as the run that
            # just died.
            fallback_models=spec.fallback_models,
            approval_callback=scope.approval_callback,
            permission_policy=scope.permission_policy,
            hook_manager=_hook_manager,
            mode=mode,
            allowed_tools=scope.allowed_tools,
            # Same persisted-context source as ``_deliver_user_turn`` — a client
            # deny must survive a recovery re-drive too.
            denied_tools=_extract_denied_tools(last_context),
            # Re-apply persisted scope — see _deliver_user_turn.
            strict_tool_scope=scope.strict_tool_scope,
            capability_mode=scope.capability_mode,
            skill_instructions=_extract_skill_instructions(last_context),
            cwd=project_cwd,
            max_iters=max_iters,
            session_step_budget=budget,
            source_platform=_request_surface(),
            extra_session_tools=extra_session_tools,
        )
        if not run_id:
            return {"message": "Session is already running."}, 409
        return {
            "session_id": session_id,
            "action": action,
            "accepted": True,
            "run_id": run_id,
        }, 202


@ns.route("/sessions/<string:session_id>/fork")
class SessionFork(Resource):
    """Fork a session, optionally from a specific message timestamp."""

    @api.doc(
        security="apikey",
        params={"session_id": "Session id returned by POST /api/sessions."},
        description=(
            "Copy the transcript into a new session and return its id. Pass "
            "`from_ts` to fork from a specific point instead of the full history, "
            "and optionally apply a new `tag` or `model`. Set `compact: true` to "
            "compact the forked transcript in the background. A running session "
            "cannot be forked."
        ),
    )
    @ns.response(
        201, "Fork created; body carries the new `session_id`.", session_fork_response_model
    )
    @ns.response(
        410, "Session is permanently terminated.", session_terminated_error_model
    )
    @kit.errors(
        400,
        shape="message",
        descriptions={400: "The fork failed (for example, an unknown `from_ts` fork point)."},
    )
    @kit.errors(
        409, shape="message", descriptions={409: "Cannot fork a session while it is running."}
    )
    @kit.auth_error()
    @ns.expect(session_fork_model)
    @guard.requires("sessions.create")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Fork a session

        Copies the transcript into a new session and returns its id. Pass
        `from_ts` to fork from a specific point instead of the full history.
        The fork records its provenance and can apply a new tag or model. A
        running session cannot be forked.
        """
        # No new work may derive from a terminated session, forks included.
        terminated = _terminated_guard(session_id)
        if terminated is not None:
            return terminated
        if runtime.is_running(session_id):
            return {"message": "Cannot fork a running session."}, 409
        body = request.get_json(silent=True) or {}
        from_ts: str | None = body.get("from_ts")
        model: str | None = body.get("model")
        compact: bool = _parse_bool(body.get("compact"))
        tag: str | None = body.get("tag")

        try:
            new_session_id = runtime.resolve_session(
                fork_from=session_id, fork_at_ts=from_ts, session_tag=tag
            )
        except Exception as exc:
            return {"message": f"Fork failed: {exc}"}, 400
        # Record provenance + optional model override as a context event.
        ctx: dict[str, object] = {"forked_from": session_id}
        if from_ts:
            ctx["forked_at"] = from_ts
        if model:
            ctx["model"] = model
        runtime.append_context_event(new_session_id, ctx)

        if compact:
            import asyncio

            try:
                asyncio.run(runtime.session_store.compact_session(new_session_id, mode="partial"))
            except Exception:
                pass  # best-effort; fork succeeded even if compaction fails

        notification_service.emit_session_created(new_session_id)
        return {
            "session_id": new_session_id,
            "forked_from": session_id,
            "forked_at": from_ts,
        }, 201


@ns.route("/sessions/<string:session_id>/plan/approve")
class SessionPlanApprove(Resource):
    """Approve or reject a pending plan-mode proposal.

    Episodic: the session is dormant (no active run) when this is called.
    On approval, emits plan_approved and starts a fresh act-mode run.
    On rejection, emits plan_rejected — session stays dormant until
    the user sends refinement guidance via /query.
    """

    @api.doc(
        security="apikey",
        params={"session_id": "Session id returned by POST /api/sessions."},
        description=(
            "Resolve a pending plan-mode proposal. Approval (`approved: true`) "
            "immediately starts a new act-mode run that implements the plan; "
            "rejection leaves the session dormant so the user can send refinement "
            "guidance via the query endpoint. Returns 404 when no proposal is "
            "pending or a run is already active."
        ),
    )
    @ns.response(200, "Decision recorded.", plan_decision_model)
    @ns.response(
        410, "Session is permanently terminated.", session_terminated_error_model
    )
    @kit.errors(400, shape="message", descriptions={400: "`approved` must be a boolean."})
    @kit.errors(
        404,
        shape="message",
        descriptions={404: "No pending plan proposal, or a run is already active."},
    )
    @kit.errors(
        500,
        shape="message",
        descriptions={500: "The plan was approved but the follow-up run could not start."},
    )
    @kit.auth_error()
    @ns.expect(plan_approve_model)
    @guard.requires("sessions.interact")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Approve or reject a plan

        Resolves a pending plan-mode proposal. Approval immediately starts a
        new run in act mode that implements the approved plan; rejection
        leaves the session dormant so the user can send refinement guidance
        via the query endpoint. Returns 404 when no proposal is pending.
        """
        # Approving a plan starts an act-mode run, so a terminated session must
        # reject it too — otherwise a pending proposal is a hole in the "no
        # further invocations" guarantee.
        terminated = _terminated_guard(session_id)
        if terminated is not None:
            return terminated
        payload = request.get_json(silent=True) or {}
        approved = payload.get("approved")
        if not isinstance(approved, bool):
            return {"message": "'approved' must be a boolean"}, 400
        if approved:
            ok = runtime.approve_plan(session_id)
            if not ok:
                return {
                    "message": (
                        "No pending plan proposal for this session, or a run is already active."
                    ),
                }, 404
            # Start a new run in act mode with a synthetic continuation. The
            # approver's role still governs the run: "full toolset" means the
            # full set the caller is entitled to, not an escape from the ceiling.
            scope = _run_scope(allowed_tools=None)
            started = runtime.start_async(
                session_id=session_id,
                user_query=(
                    "[system] The user approved your plan. Proceed with "
                    "implementation using the full toolset. The approved "
                    "plan is in the conversation history."
                ),
                approval_callback=scope.approval_callback,
                permission_policy=scope.permission_policy,
                capability_mode=scope.capability_mode,
                hook_manager=_hook_manager,
                mode="act",
                source_platform=_request_surface(),
            )
            if not started:
                return {
                    "session_id": session_id,
                    "approved": True,
                    "message": "Plan approved but could not start run.",
                }, 500
            return {"session_id": session_id, "approved": True}, 200
        else:
            ok = runtime.reject_plan(session_id)
            if not ok:
                return {
                    "message": (
                        "No pending plan proposal for this session, or a run is already active."
                    ),
                }, 404
            return {"session_id": session_id, "approved": False}, 200


@ns.route("/sessions/<string:session_id>/plan.md")
class SessionPlanFile(Resource):
    """Serve the current ``plan.md`` for a session from the scoped temp dir."""

    @api.doc(
        security="apikey",
        params={"session_id": "Session id returned by POST /api/sessions."},
        description=(
            "Return the session's current `plan.md` as `text/markdown`. A plan "
            "file exists only after a plan-mode run has written one; otherwise the "
            "call returns 404."
        ),
    )
    @api.response(200, "Plan content (`text/markdown`).")
    @kit.errors(
        400,
        404,
        500,
        shape="message",
        descriptions={
            400: "The session id is invalid (path traversal guard).",
            404: "No plan file exists for this session.",
            500: "The plan file could not be read.",
        },
    )
    @kit.auth_error()
    @guard.requires("sessions.read")
    def get(self, session_id: str) -> tuple[dict, int] | Response:
        """Fetch the session plan

        Returns the session's current `plan.md` as `text/markdown`. A plan
        file exists only after a plan-mode run has written one; otherwise the
        call returns 404.
        """
        path = plan_file_for(session_id)
        # Path-traversal defence: ensure the resolved path stays under the
        # shared plan root even if ``session_id`` contains ``..`` or ``/``.
        try:
            resolved = os.path.realpath(path)
            root = os.path.realpath(PLAN_DIR_ROOT)
        except OSError:
            return {"message": "Invalid session id."}, 400
        if not resolved.startswith(root + os.sep):
            return {"message": "Invalid session id."}, 400
        if not os.path.exists(resolved):
            return {"message": "Plan file not found."}, 404
        try:
            with open(resolved, encoding="utf-8") as handle:
                content = handle.read()
        except OSError as exc:
            return {"message": f"Failed to read plan file: {exc}"}, 500
        return Response(
            content,
            mimetype="text/markdown",
            headers={
                "Cache-Control": "no-cache",
                "Access-Control-Allow-Origin": _CORS_ORIGIN,
            },
        )


@ns.route("/sessions/<string:session_id>/agents")
class SessionAgents(Resource):
    """Return agent tree information for a session."""

    @api.doc(
        security="apikey",
        params={"session_id": "Session id returned by POST /api/sessions."},
        description=(
            "Return the session's sub-agent lifecycle events (status, model, "
            "per-agent token counts) plus rollups: `total_steps`, "
            "`total_input_tokens` (peak context pressure), and "
            "`total_input_tokens_billed` (cumulative billed input). Use it to "
            "render a live agent tree alongside the event stream."
        ),
    )
    @ns.response(200, "Agent tree and token rollups.", agents_tree_model)
    @kit.auth_error()
    @guard.requires("sessions.read")
    def get(self, session_id: str) -> tuple[dict, int]:
        """Get the agent tree

        Returns the session's sub-agent lifecycle events with status, model,
        and per-agent token counts, plus rollups: `total_steps`,
        `total_input_tokens` (peak context pressure), and
        `total_input_tokens_billed` (cumulative billed input). Use it to
        render a live agent tree alongside the event stream.
        """
        events = runtime.load_events(session_id)
        total_steps = sum(1 for e in events if e.get("type") == "tool_result")
        agents = [
            {
                "agent_id": e.get("payload", {}).get("agent_id"),
                "parent_id": e.get("payload", {}).get("parent_id"),
                "depth": e.get("payload", {}).get("depth"),
                "model": e.get("payload", {}).get("model"),
                "action": e.get("payload", {}).get("action"),
                # Cap the free-text ``detail`` so the agent tree can't regrow the
                # transcript bloat the payload cap on the events route already fixed.
                "detail": _cap_freetext(e.get("payload", {}).get("detail")),
                "status": e.get("payload", {}).get("status"),
                "steps_completed": e.get("payload", {}).get("steps_completed", 0),
                "input_tokens": e.get("payload", {}).get("input_tokens", 0),
                "output_tokens": e.get("payload", {}).get("output_tokens", 0),
                "ts": e.get("ts"),
            }
            for e in events
            if e.get("type") == "sub_agent"
        ]
        running = runtime.is_running(session_id)
        # Token totals delegate to the same usage builder the /usage endpoint
        # uses, so a root-only session (no sub_agent stop events) still reports
        # its real root (depth==0) tokens instead of 0.
        # ``total_input_tokens`` = PEAK (root_peak + sum-of-per-agent-peaks) —
        # the same "context pressure" number the history overview and console
        # badge show. The cumulative billed sum (which re-counts the growing
        # prefix on every call and is ~2× the real peak) is exposed separately
        # under ``total_input_tokens_billed`` for cost accounting.
        from mewbo_core.session.token_budget import build_usage_numbers

        usage = build_usage_numbers(events, None)
        total_input_tokens = (
            usage["root_peak_input_tokens"] + usage["sub_peak_input_tokens"]
        )
        total_output_tokens = usage["total_output_tokens"]
        return {
            "agents": agents,
            "running": running,
            "total_steps": total_steps,
            "total_input_tokens": total_input_tokens,
            "total_input_tokens_billed": usage["total_input_tokens_billed"],
            "total_output_tokens": total_output_tokens,
        }, 200


@ns.route("/sessions/<string:session_id>/usage")
class SessionUsage(Resource):
    """Return token usage broken down by root agent vs sub-agents."""

    @api.doc(
        security="apikey",
        params={"session_id": "Session id returned by POST /api/sessions."},
        description=(
            "Return token usage split between the root agent and sub-agents, "
            "including peak and billed input figures plus compaction statistics. "
            "`total_input_tokens_billed` is the cost-accounting number; the peak "
            "fields describe context pressure."
        ),
    )
    @ns.response(200, "Token usage breakdown.", usage_model)
    @kit.auth_error()
    @guard.requires("sessions.read")
    def get(self, session_id: str) -> tuple[dict, int]:
        """Get token usage

        Returns token usage split between the root agent and sub-agents,
        including peak and billed input figures and compaction statistics.
        """
        from mewbo_core.session.token_budget import build_usage_numbers

        events = runtime.load_events(session_id)
        root_model: str | None = None
        for event in reversed(events):
            if event.get("type") != "context":
                continue
            payload = event.get("payload")
            if isinstance(payload, dict):
                candidate = payload.get("model")
                if isinstance(candidate, str) and candidate:
                    root_model = candidate
                    break
        if not root_model:
            root_model = str(get_config_value("llm", "default_model", default="") or "")
        return build_usage_numbers(events, root_model), 200


@ns.route("/sessions/<string:session_id>/archive")
class SessionArchive(Resource):
    """Archive or unarchive a session."""

    @api.doc(
        security="apikey",
        params={"session_id": "Session id returned by POST /api/sessions."},
        description=(
            "Hide the session from the default session list. Archiving is fully "
            "reversible with DELETE on the same path."
        ),
    )
    @ns.response(200, "Session archived.", session_archive_model)
    @kit.errors(404, shape="message", descriptions={404: "No session with that id exists."})
    @kit.auth_error()
    @guard.requires("sessions.interact")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Archive a session

        Hides the session from the default session list. Archiving is fully
        reversible with DELETE on the same path.
        """
        if session_id not in runtime.session_store.list_sessions():
            return {"message": "Session not found."}, 404
        runtime.session_store.archive_session(session_id)
        return {"session_id": session_id, "archived": True}, 200

    @api.doc(
        security="apikey",
        params={"session_id": "Session id returned by POST /api/sessions."},
        description="Restore an archived session to the default session list.",
    )
    @ns.response(200, "Session unarchived.", session_archive_model)
    @kit.errors(404, shape="message", descriptions={404: "No session with that id exists."})
    @kit.auth_error()
    @guard.requires("sessions.interact")
    def delete(self, session_id: str) -> tuple[dict, int]:
        """Unarchive a session

        Restores an archived session to the default session list.
        """
        if session_id not in runtime.session_store.list_sessions():
            return {"message": "Session not found."}, 404
        runtime.session_store.unarchive_session(session_id)
        return {"session_id": session_id, "archived": False}, 200


@ns.route("/sessions/<string:session_id>/pin")
class SessionPin(Resource):
    """Pin or unpin a session, mirroring the archive resource's shape."""

    @api.doc(
        security="apikey",
        params={"session_id": "Session id returned by POST /api/sessions."},
        description=(
            "Pin the session so it sorts first in `GET /api/sessions`, ahead of "
            "every unpinned session regardless of recency. Pinning is an "
            "ORDERING, not a filter: a pinned session stays subject to every "
            "other active filter. Reversible with DELETE on the same path."
        ),
    )
    @ns.response(200, "Session pinned.", session_pin_model)
    @kit.errors(404, shape="message", descriptions={404: "No session with that id exists."})
    @kit.auth_error()
    @guard.requires("sessions.interact")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Pin a session

        Sorts the session first in the list, ahead of every unpinned session.
        Reversible with DELETE on the same path.
        """
        if not _session_exists(session_id):
            return _session_not_found(session_id)
        pinned_at = runtime.set_session_pinned(session_id, True)
        return {"session_id": session_id, "pinned": True, "pinned_at": pinned_at}, 200

    @api.doc(
        security="apikey",
        params={"session_id": "Session id returned by POST /api/sessions."},
        description="Unpin the session, restoring its ordinary recency ordering.",
    )
    @ns.response(200, "Session unpinned.", session_pin_model)
    @kit.errors(404, shape="message", descriptions={404: "No session with that id exists."})
    @kit.auth_error()
    @guard.requires("sessions.interact")
    def delete(self, session_id: str) -> tuple[dict, int]:
        """Unpin a session

        Restores the session's ordinary recency ordering.
        """
        if not _session_exists(session_id):
            return _session_not_found(session_id)
        runtime.set_session_pinned(session_id, False)
        return {"session_id": session_id, "pinned": False, "pinned_at": None}, 200


@ns.route("/sessions/<string:session_id>/terminate")
class SessionTerminate(Resource):
    """Permanently terminate a session (irreversible)."""

    @api.doc(
        security="apikey",
        params={"session_id": "Session id returned by POST /api/sessions."},
        description=(
            "Permanently terminate the session. Cancels any active run, fires "
            "cascade-cancel callbacks, and appends a `session_terminated` event. "
            "This is a one-way door: there is no un-terminate, and every "
            "subsequent mutating call (query, message, interrupt, recover, fork) "
            "returns 410. Reads (events, stream, history) keep working — "
            "terminated is not deleted. The call is idempotent: repeating it "
            "returns 200 with the original `terminated_at` and "
            "`cancelled_triggers: 0`."
        ),
    )
    @ns.response(200, "Session terminated (idempotent).", session_terminate_model)
    @kit.errors(404, descriptions={404: "No session with that id exists."})
    @kit.auth_error()
    @guard.requires("sessions.terminate")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Terminate a session

        Permanently terminates the session: cancels any active run, runs
        cascade-cancel callbacks, and records a `session_terminated` event.
        Irreversible — every subsequent mutating call returns 410 while reads
        keep working. Idempotent: a repeat call returns the original
        `terminated_at` and does not re-fire the side effects.
        """
        if not _session_exists(session_id):
            return _session_not_found(session_id)
        return runtime.terminate_session(session_id), 200


@ns.route("/sessions/<string:session_id>/title")
class SessionTitle(Resource):
    """Update the display title of a session."""

    @api.doc(
        security="apikey",
        params={"session_id": "Session id returned by POST /api/sessions."},
        description=(
            "Save a user-provided display title for the session. Titles are "
            "trimmed and capped at 120 characters. Use POST on the same path to "
            "have the model generate a title instead."
        ),
    )
    @ns.response(200, "Title saved.", session_title_model)
    @kit.errors(400, shape="message", descriptions={400: "The `title` field is missing or empty."})
    @kit.errors(404, shape="message", descriptions={404: "No session with that id exists."})
    @kit.auth_error()
    @ns.expect(title_patch_model)
    @guard.requires("sessions.interact")
    def patch(self, session_id: str) -> tuple[dict, int]:
        """Rename a session

        Saves a user-provided display title for the session. Titles are
        trimmed and capped at 120 characters.
        """
        if session_id not in runtime.session_store.list_sessions():
            return {"message": "Session not found."}, 404
        payload = request.get_json(silent=True) or {}
        raw = payload.get("title")
        if not isinstance(raw, str):
            return {"message": "title is required"}, 400
        title = raw.strip()[:120]
        if not title:
            return {"message": "title cannot be empty"}, 400
        runtime.session_store.save_title(session_id, title)
        return {"session_id": session_id, "title": title}, 200

    @api.doc(
        security="apikey",
        params={"session_id": "Session id returned by POST /api/sessions."},
        description=(
            "Ask the configured model to produce a title from the transcript, "
            "save it, and append a `title_update` event to the session. Returns "
            "422 when no usable title could be generated."
        ),
    )
    @ns.response(200, "Generated title saved.", session_title_model)
    @kit.errors(404, shape="message", descriptions={404: "No session with that id exists."})
    @kit.errors(422, shape="message", descriptions={422: "No usable title could be generated."})
    @kit.auth_error()
    @guard.requires("sessions.interact")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Generate a session title

        Asks the configured model to produce a title from the transcript,
        saves it, and appends a `title_update` event to the session. Returns
        422 when no usable title could be generated.
        """
        if session_id not in runtime.session_store.list_sessions():
            return {"message": "Session not found."}, 404
        import asyncio

        from mewbo_core.session.title_generator import generate_session_title

        events = runtime.session_store.load_transcript(session_id)
        title = asyncio.run(generate_session_title(events))
        if not title:
            return {"message": "Could not generate a title."}, 422
        runtime.session_store.save_title(session_id, title)
        runtime.session_store.append_event(
            session_id,
            {"type": "title_update", "payload": {"title": title}},
        )
        return {"session_id": session_id, "title": title}, 200


@ns.route("/sessions/<string:session_id>/attachments")
class SessionAttachments(Resource):
    """Upload attachments for a session."""

    @api.doc(
        security="apikey",
        params={
            "session_id": "Session id returned by POST /api/sessions.",
            "model": {
                "description": (
                    "Optional model hint. Image uploads are rejected early when "
                    "the named model lacks vision support."
                ),
                "in": "query",
                "type": "string",
            },
        },
        description=(
            "Upload one or more files as `multipart/form-data` under the `files` "
            "field (a single `file` field also works). Documents are parsed to "
            "Markdown at upload time so later runs read them without re-parsing. "
            "Unsupported file types are rejected, as are image uploads when the "
            "`model` hint names a non-vision model. Reference the returned "
            "descriptors in the `attachments` field of a query."
        ),
    )
    @ns.response(200, "Saved attachment descriptors.", attachments_response_model)
    @kit.errors(
        400,
        shape="message",
        descriptions={400: "No files, an unsupported type, or an image to a non-vision model."},
    )
    @kit.errors(404, shape="message", descriptions={404: "No session with that id exists."})
    @kit.auth_error()
    @guard.requires("sessions.interact")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Upload attachments

        Accepts one or more files as `multipart/form-data` under the `files`
        field (a single `file` field also works). Documents are parsed to
        Markdown at upload time so later runs can read them without
        re-parsing. Unsupported file types are rejected, as are image uploads
        when the `model` hint names a model without vision support. Reference
        the returned descriptors in the `attachments` field of a query.
        """
        if session_id not in runtime.session_store.list_sessions():
            return {"message": "Session not found."}, 404
        files = request.files.getlist("files")
        if not files and "file" in request.files:
            files = [request.files["file"]]
        if not files:
            return {"message": "No files uploaded."}, 400
        # Optional model hint from the client — when present we reject
        # image uploads against non-vision models eagerly so the user
        # gets a 400 instead of a silent skip at inference time.
        model_hint = (
            request.form.get("model")
            or request.args.get("model")
            or None
        )
        has_vision = model_supports_vision(model_hint) if model_hint else True

        # Pre-flight: reject any unsupported file outright (Q5 option B).
        # Better to fail loudly here than to store junk that the loader
        # will silently skip later.
        for item in files:
            if not item or not item.filename:
                continue
            if not is_supported(item.mimetype or "", item.filename):
                return {
                    "message": (
                        f"Unsupported file type: {item.filename} "
                        f"({item.mimetype or 'unknown'})."
                    )
                }, 400
            if model_hint and is_image(item.mimetype or "") and not has_vision:
                return {
                    "message": (
                        f"Model {model_hint!r} does not support image inputs. "
                        f"Remove {item.filename} or switch to a vision-capable model."
                    )
                }, 400

        attachments_dir = os.path.join(
            runtime.session_store.root_dir,
            session_id,
            "attachments",
        )
        os.makedirs(attachments_dir, exist_ok=True)
        saved: list[dict[str, object]] = []
        for item in files:
            if not item or not item.filename:
                continue
            attachment_id = uuid.uuid4().hex
            safe_name = secure_filename(item.filename)
            stored_name = f"{attachment_id}_{safe_name}" if safe_name else attachment_id
            path = os.path.join(attachments_dir, stored_name)
            item.save(path)
            size_bytes = os.path.getsize(path)
            content_type = item.mimetype or ""

            # Parse documents to Markdown at upload time (Q1). Cache the
            # result alongside the raw file as ``<stored>.md`` so the
            # context loader never re-parses on the hot path.
            parsed = False
            if not is_image(content_type):
                md_text = parse_to_markdown(path)
                if md_text:
                    try:
                        with open(parsed_sidecar_path(path), "w", encoding="utf-8") as fh:
                            fh.write(md_text)
                        parsed = True
                    except OSError as exc:
                        logging.warning(
                            "failed to write parsed sidecar for %s: %s", path, exc
                        )

            saved.append(
                {
                    "id": attachment_id,
                    "filename": item.filename,
                    "stored_name": stored_name,
                    "content_type": content_type,
                    "size_bytes": size_bytes,
                    "uploaded_at": _utc_now(),
                    "parsed": parsed,
                }
            )
        if not saved:
            return {"message": "No valid files uploaded."}, 400
        return {"attachments": saved}, 200


@ns.route("/sessions/<string:session_id>/share")
class SessionShare(Resource):
    """Create a share token for a session."""

    @api.doc(
        security="apikey",
        params={"session_id": "Session id returned by POST /api/sessions."},
        description=(
            "Mint a share token for the session. Anyone holding the token can "
            "read the transcript via GET /api/share/{token} without an API key."
        ),
    )
    @ns.response(200, "Share record with the new token.", share_record_model)
    @kit.errors(404, shape="message", descriptions={404: "No session with that id exists."})
    @kit.auth_error()
    @guard.requires("sessions.interact")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Create a share link

        Mints a share token for the session. Anyone holding the token can
        read the transcript via GET /api/share/{token} without an API key.
        """
        if session_id not in runtime.session_store.list_sessions():
            return {"message": "Session not found."}, 404
        record = share_store.create(session_id)
        return record, 200


@ns.route("/sessions/<string:session_id>/export")
class SessionExport(Resource):
    """Export transcript data for a session."""

    @api.doc(
        security="apikey",
        params={"session_id": "Session id returned by POST /api/sessions."},
        description=(
            "Return the full event transcript and the stored summary in one "
            "payload, suitable for download or offline analysis."
        ),
    )
    @ns.response(200, "Transcript and summary.", session_export_model)
    @kit.errors(404, shape="message", descriptions={404: "No session with that id exists."})
    @kit.auth_error()
    @guard.requires("sessions.read")
    def get(self, session_id: str) -> tuple[dict, int]:
        """Export a session

        Returns the full event transcript and the stored summary in one
        payload, suitable for download or offline analysis.
        """
        if session_id not in runtime.session_store.list_sessions():
            return {"message": "Session not found."}, 404
        return {
            "session_id": session_id,
            "events": runtime.session_store.load_transcript(session_id),
            "summary": runtime.session_store.load_summary(session_id),
        }, 200


def _resolve_session_cwd(session_id: str) -> str | None:
    """Return the project filesystem path for a session, or None if unresolvable.

    Checks (in order, most-recent-context-event wins):
    1. An explicit ``cwd`` persisted by :class:`ExternalCwdPolicy` — returned
       directly when the path still exists as a directory.
    2. A ``project`` name resolved via :func:`_resolve_project_cwd` — the
       existing behaviour.

    The ``auto`` sentinel is SKIPPED rather than treated as an answer, and that
    is what makes an auto-mode session work across turns. Its persisted binding
    keeps ``project: "auto"`` forever (that IS the binding — the model may switch
    again), while the project it has actually settled on rides an ordinary
    context event underneath. Reading the sentinel as "no project" would return
    ``None`` here and drop the session back into a scratch directory on the very
    next turn.
    """
    events = runtime.session_store.load_transcript(session_id)
    # Walk backwards to find the most recent context event with a cwd or project.
    for event in reversed(events):
        if event.get("type") != "context":
            continue
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            continue
        # External cwd persisted by ExternalCwdPolicy takes priority.
        raw_cwd = payload.get("cwd")
        if raw_cwd and isinstance(raw_cwd, str) and os.path.isdir(raw_cwd):
            return raw_cwd
        # Project-derived path.
        project_name = payload.get("project")
        if project_name and isinstance(project_name, str):
            if is_auto_project(project_name):
                continue
            try:
                return _resolve_project_cwd({"project": project_name})
            except ValueError as exc:
                # The QUIET half of a mis-bound session: the run never fails, it
                # just operates in an empty scratch directory forever. Log the
                # refusal (the catalog's message names what IS resolvable) and
                # keep returning None — every call site supplies its own
                # fallback, which is why the message names NONE of them.
                # It used to promise "falling back to the session temp
                # directory", which this function does not decide and which is
                # no longer even true for the app-pipeline caller (it falls back
                # to the app's staging directory). A log that names another
                # function's behaviour goes stale silently and misdirects
                # exactly the debugging session that needed it.
                logging.warning(
                    "Session {} names project {!r}, which does not resolve: {} "
                    "No project cwd; the caller supplies its own fallback.",
                    session_id,
                    project_name,
                    exc,
                )
                return None
    return None


@ns.route("/files")
class FileCatalogView(Resource):
    """List referenceable project files for the composer's `@`-mention picker."""

    @api.doc(
        security="apikey",
        params={
            "project": {
                "description": "Project name to scope files to (home composer).",
                "in": "query",
                "type": "string",
            },
            "session": {
                "description": (
                    "Session id to scope files to (in-session composer); also "
                    "includes the session's attachments."
                ),
                "in": "query",
                "type": "string",
            },
            "q": {
                "description": "Optional case-insensitive substring filter.",
                "in": "query",
                "type": "string",
            },
            "limit": {
                "description": "Max files to return (default 200, cap 2000).",
                "in": "query",
                "type": "integer",
            },
        },
        description=(
            "List the files an `@<ref>` may resolve to: the project's git index "
            "(tracked + new-but-not-`.gitignore`d) plus any files attached to the "
            "session. Pass `project` (home composer) or `session` (in-session "
            "composer). Falls back to a bounded filesystem walk for non-git "
            "projects, and returns empty lists when nothing resolves."
        ),
    )
    @ns.response(200, "The project's git-indexed files plus session attachments.", files_list_model)
    @kit.errors(
        404,
        shape="message",
        descriptions={404: "A `session` was supplied but no session with that id exists."},
    )
    @kit.auth_error()
    @guard.requires("sessions.read")
    def get(self) -> tuple[dict, int]:
        """List referenceable files

        Returns the files an `@<ref>` may resolve to: the project's git index
        (tracked + new-but-not-`.gitignore`d) plus any files attached to the
        session. Pass `project` (home composer) or `session` (in-session
        composer). Falls back to a bounded filesystem walk for non-git
        projects.
        """
        project = request.args.get("project")
        session_id = request.args.get("session")
        cwd: str | None = None
        if project:
            try:
                cwd = _resolve_project_cwd({"project": project})
            except ValueError:
                cwd = None
        if cwd is None and session_id:
            if session_id not in runtime.session_store.list_sessions():
                return {"message": "Session not found."}, 404
            cwd = _resolve_session_cwd(session_id)

        limit = min(max(int(request.args.get("limit", 200) or 200), 1), 2000)
        files = FileCatalog(cwd).list_files(limit=limit) if cwd else []
        # Session attachments are referenceable by their display filename.
        attachments = (
            sorted(_session_attachment_map(session_id).keys()) if session_id else []
        )

        query = (request.args.get("q") or "").strip().lower()
        if query:
            files = [f for f in files if query in f.lower()]
            attachments = [a for a in attachments if query in a.lower()]
        return {"files": files[:limit], "attachments": attachments}, 200


@ns.route("/sessions/<string:session_id>/git-diff")
class SessionGitDiff(Resource):
    """Read-only git diff for a session's project."""

    @api.doc(
        security="apikey",
        params={
            "session_id": "Session id returned by POST /api/sessions.",
            "scope": {
                "description": (
                    "`uncommitted` (default) diffs the working tree against "
                    "HEAD; `branch` diffs against the merge base with "
                    "origin/main (or origin/master)."
                ),
                "in": "query",
                "type": "string",
                "enum": ["uncommitted", "branch"],
            },
        },
        description=(
            "Return a unified git diff for the session's bound project. `scope` "
            "selects `uncommitted` (working tree vs HEAD, the default) or `branch` "
            "(vs the merge base with origin/main or origin/master). When the "
            "session has no project, or it is not a git repo, the call still "
            "returns 200 with `git_repo` false and a `reason`."
        ),
    )
    @ns.response(200, "Unified diff, or `git_repo: false` with a reason.", git_diff_model)
    @kit.errors(
        400, shape="message", descriptions={400: "`scope` must be `uncommitted` or `branch`."}
    )
    @kit.errors(404, shape="message", descriptions={404: "No session with that id exists."})
    @kit.auth_error()
    @guard.requires("sessions.read")
    def get(self, session_id: str) -> tuple[dict, int]:
        """Get the session diff

        Returns a unified git diff for the session's bound project. When the
        session has no project, or the project is not a git repository, the
        call still returns 200 with `git_repo` false and a `reason` instead of
        an error.
        """
        if session_id not in runtime.session_store.list_sessions():
            return {"message": "Session not found."}, 404

        scope = request.args.get("scope", "uncommitted")
        if scope not in ("uncommitted", "branch"):
            return {"message": "scope must be 'uncommitted' or 'branch'"}, 400

        cwd = _resolve_session_cwd(session_id)
        if not cwd:
            return {"git_repo": False, "reason": "no_project"}, 200

        check = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
        )
        if check.returncode != 0:
            return {"git_repo": False, "reason": "not_git"}, 200

        if scope == "uncommitted":
            result = subprocess.run(
                ["git", "-C", cwd, "diff", "HEAD"],
                capture_output=True,
                text=True,
            )
        else:
            # Branch: diff since divergence from origin/main (fallback to origin/master)
            merge_base = subprocess.run(
                ["git", "-C", cwd, "merge-base", "HEAD", "origin/main"],
                capture_output=True,
                text=True,
            )
            if merge_base.returncode != 0:
                merge_base = subprocess.run(
                    ["git", "-C", cwd, "merge-base", "HEAD", "origin/master"],
                    capture_output=True,
                    text=True,
                )
            if merge_base.returncode != 0:
                err = merge_base.stderr.strip()
                return {"git_repo": False, "reason": "git_error", "error": err}, 200
            base_commit = merge_base.stdout.strip()
            result = subprocess.run(
                ["git", "-C", cwd, "diff", f"{base_commit}...HEAD"],
                capture_output=True,
                text=True,
            )

        if result.returncode != 0:
            return {"git_repo": False, "reason": "git_error", "error": result.stderr.strip()}, 200

        return {"git_repo": True, "diff": result.stdout}, 200


@ns.route("/share/<string:token>")
class ShareLookup(Resource):
    """Resolve a share token to a session export."""

    @api.doc(
        security=[],
        params={
            "token": "Share token returned by POST /api/sessions/{session_id}/share.",
        },
        description=(
            "Public endpoint. Return the shared session's transcript and summary "
            "for a valid token — no API key required; possession of the token is "
            "the only credential."
        ),
    )
    @ns.response(200, "Shared transcript and summary.", share_lookup_model)
    @kit.errors(404, shape="message", descriptions={404: "No share token matches."})
    @guard.public("share links carry an unguessable token as their proof; no principal is involved")
    def get(self, token: str) -> tuple[dict, int]:
        """Resolve a share link

        Public endpoint. Returns the shared session's transcript and summary
        for a valid token. No API key is required; possession of the token is
        the only credential.
        """
        record = share_store.resolve(token)
        if not record:
            return {"message": "Share token not found."}, 404
        session_id = record["session_id"]
        return {
            "token": token,
            "session_id": session_id,
            "created_at": record.get("created_at"),
            "events": runtime.session_store.load_transcript(session_id),
            "summary": runtime.session_store.load_summary(session_id),
        }, 200


@ns.route("/commands")
class CommandRegistry(Resource):
    """List the server-side command registry for client discovery."""

    @api.doc(
        security="apikey",
        description=(
            "Return the server-side slash command registry — each command's "
            "`name`, `args`, and render `kind` — so clients can build command "
            "palettes without hardcoding the list. Execute one against a session "
            "via POST /api/sessions/{session_id}/command."
        ),
    )
    @ns.response(200, "Command registry.", commands_list_model)
    @kit.auth_error()
    @guard.requires("sessions.read")
    def get(self) -> tuple[dict, int]:
        """List commands

        Returns the server-side slash command registry with each command's
        name, arguments, and render kind, so clients can build command
        palettes without hardcoding the list.
        """
        from mewbo_core.session.commands import list_commands

        return {"commands": list_commands()}, 200


@ns.route("/sessions/<string:session_id>/command")
class SessionCommand(Resource):
    """Execute a server-side command against a session."""

    @api.doc(
        security="apikey",
        params={"session_id": "Session id returned by POST /api/sessions."},
        description=(
            "Execute a server-side slash command (e.g. `compact`) against the "
            "session. Commands that render into the transcript run asynchronously "
            "like a query: the call returns 202 and output arrives on the event "
            "stream. Dialog and notification commands execute inline and return "
            "their result with 200. Discover commands via GET /api/commands."
        ),
    )
    @ns.response(
        200, "Inline command result (dialog or notification render).", command_inline_model
    )
    @ns.response(
        202, "Transcript command started; watch the event stream.", command_accepted_model
    )
    @ns.response(400, "Missing name or invalid arguments.", command_error_model)
    @ns.response(404, "Unknown command.", command_unknown_model)
    @ns.response(409, "Session is already running.", command_running_model)
    @ns.response(500, "Command handler failed.", command_error_model)
    @kit.auth_error()
    @ns.expect(session_command_model)
    @guard.requires("sessions.interact")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Run a command

        Executes a server-side slash command such as `compact` against the
        session. Commands that render into the transcript run asynchronously
        like a regular query: the call returns 202 and their output arrives on
        the event stream. Dialog and notification commands execute inline and
        return their result in the response body with 200. Discover available
        commands via GET /api/commands.
        """
        # A permanently terminated session rejects EVERY server-side command,
        # mirroring the mutating query/message/fork routes. This closes a
        # fork-resurrection hole: the ``fork`` command dispatches into
        # ``mewbo_core.session.commands`` which copies the transcript store-directly,
        # bypassing the ``resolve_session`` kill-switch seam — and ``compact``
        # would mutate a frozen transcript. Guard here so neither reaches core.
        terminated = _terminated_guard(session_id)
        if terminated is not None:
            return terminated

        import asyncio

        from mewbo_core.session.commands import (
            COMMANDS,
            CommandContext,
            CommandError,
            CommandRender,
            execute_command,
        )
        from mewbo_core.session.token_budget import build_usage_numbers

        body = request.get_json(silent=True) or {}
        name = body.get("name")
        args = body.get("args") or []
        if not name or not isinstance(args, list):
            return {
                "error": "bad_request",
                "message": "name and args[] required",
            }, 400

        spec = COMMANDS.get(name)
        if spec is None:
            return {"error": "unknown_command", "name": name}, 404

        def _usage_provider(sid: str) -> dict:
            events = runtime.load_events(sid)
            root_model = ""
            for event in reversed(events):
                if event.get("type") == "context":
                    payload = event.get("payload") or {}
                    candidate = (
                        payload.get("model") if isinstance(payload, dict) else None
                    )
                    if isinstance(candidate, str) and candidate:
                        root_model = candidate
                        break
            if not root_model:
                root_model = str(
                    get_config_value("llm", "default_model", default="") or ""
                )
            return build_usage_numbers(events, root_model)

        ctx = CommandContext(
            session_id=session_id,
            session_store=runtime.session_store,
            notification_service=notification_service,
            skill_registry=getattr(runtime, "skill_registry", None),
            usage_provider=_usage_provider,
            hook_manager=_hook_manager,
            model_name=str(get_config_value("llm", "default_model", default="") or ""),
        )

        if spec.render is CommandRender.TRANSCRIPT:
            if runtime.is_running(session_id):
                return {"message": "Session is already running."}, 409

            invocation = f"/{name}"
            if args:
                invocation += " " + " ".join(args)
            runtime.session_store.append_event(
                session_id, {"type": "user", "payload": {"text": invocation}}
            )

            def _run_command(_cancel_event: object) -> None:
                # ``done_reason`` mirrors the orchestrator's convention so a
                # command run looks indistinguishable from a regular query
                # to every downstream consumer (notifications, status badge,
                # session summary): ``compacted`` / ``command:<name>`` for
                # success, ``compact_failed`` / ``command_failed:<name>`` for
                # failure. ``done: True`` flags the run as terminated so
                # ``summarize_session`` resolves status="completed" instead
                # of "incomplete".
                success_reason = "compacted" if name == "compact" else f"command:{name}"
                failure_reason = (
                    "compact_failed" if name == "compact" else f"command_failed:{name}"
                )
                try:
                    result = asyncio.run(execute_command(name, args, ctx))
                    completion_payload: dict[str, object] = {
                        "text": result.body,
                        "done": True,
                        "done_reason": success_reason,
                        "command": name,
                    }
                except CommandError as exc:
                    completion_payload = {
                        "text": f"/{name} failed: {exc}",
                        "done": True,
                        "done_reason": failure_reason,
                        "command": name,
                        "error": str(exc),
                    }
                except Exception as exc:  # noqa: BLE001
                    logging.warning("Command %s failed", name, exc_info=True)
                    completion_payload = {
                        "text": f"/{name} failed: {exc}",
                        "done": True,
                        "done_reason": failure_reason,
                        "command": name,
                        "error": str(exc),
                    }
                runtime.session_store.append_event(
                    session_id,
                    {"type": "completion", "payload": completion_payload},
                )

            started = runtime.start_command(session_id, _run_command)
            if not started:
                return {"message": "Session is already running."}, 409
            return {
                "session_id": session_id,
                "accepted": True,
                "render": CommandRender.TRANSCRIPT.value,
            }, 202

        try:
            result = asyncio.run(execute_command(name, args, ctx))
        except KeyError:
            return {"error": "unknown_command", "name": name}, 404
        except CommandError as exc:
            return {"error": "bad_args", "message": str(exc)}, 400
        except Exception as exc:  # noqa: BLE001
            logging.warning("Command %s failed", name, exc_info=True)
            return {"error": "handler_failed", "message": str(exc)}, 500

        return {
            "render": result.render.value,
            "title": result.title,
            "body": result.body,
            "metadata": result.metadata,
        }, 200


@ns.route("/notifications")
class Notifications(Resource):
    """List notifications.

    Listing gates on ``sessions.read``; dismiss and clear gate on
    ``sessions.interact``, the write verb every other session-mutating route in
    this module already uses. No ``notifications.*`` pair is minted for them —
    the existing verb covers the shape, and a new id would add catalog surface
    for nothing.

    The split matters because the store is NOT per-caller: ``dismiss`` and
    ``clear`` take no subject, so they mutate the one process-wide collection
    every principal reads. ``clear_all`` therefore destroys other people's
    notifications, which is not something a read-only role should be able to do.
    """

    @api.doc(
        security="apikey",
        params={
            "include_dismissed": {
                "description": "Set to true to include dismissed notifications.",
                "in": "query",
                "type": "boolean",
            },
        },
        description=(
            "List session lifecycle notifications (created, completed, failed). "
            "Dismissed entries are hidden unless `include_dismissed=true`. Dismiss "
            "with POST /api/notifications/dismiss."
        ),
    )
    @ns.response(200, "Notification list.", notifications_list_model)
    @kit.auth_error()
    @guard.requires("sessions.read")
    def get(self) -> tuple[dict, int]:
        """List notifications

        Returns session lifecycle notifications such as session created,
        completed, or failed. Dismissed entries are hidden unless
        `include_dismissed=true`.
        """
        include_dismissed = _parse_bool(request.args.get("include_dismissed"))
        return {
            "notifications": notification_store.list(include_dismissed=include_dismissed),
        }, 200


@ns.route("/notifications/dismiss")
class NotificationDismiss(Resource):
    """Dismiss notifications."""

    @api.doc(
        security="apikey",
        description=(
            "Mark the given notification ids as dismissed. Accepts either an "
            "`ids` array or a single `id`. Returns the number dismissed."
        ),
    )
    @ns.response(200, "Number of notifications dismissed.", notification_dismiss_response_model)
    @kit.auth_error()
    @ns.expect(notification_dismiss_model)
    @guard.requires("sessions.interact")
    def post(self) -> tuple[dict, int]:
        """Dismiss notifications

        Marks the given notification ids as dismissed. Accepts either an
        `ids` array or a single `id`. Returns the number dismissed.
        """
        payload = request.get_json(silent=True) or {}
        ids: list[str] = []
        ids_payload = payload.get("ids")
        if isinstance(ids_payload, list):
            ids = [str(item) for item in ids_payload if item]
        elif payload.get("id"):
            ids = [str(payload.get("id"))]
        dismissed = notification_store.dismiss(ids)
        return {"dismissed": dismissed}, 200


@ns.route("/notifications/clear")
class NotificationClear(Resource):
    """Clear notifications."""

    @api.doc(
        security="apikey",
        description=(
            "Delete dismissed notifications, or every notification when "
            "`clear_all` is true. Returns the number cleared."
        ),
    )
    @ns.response(200, "Number of notifications cleared.", notification_clear_response_model)
    @kit.auth_error()
    @ns.expect(notification_clear_model)
    @guard.requires("sessions.interact")
    def post(self) -> tuple[dict, int]:
        """Clear notifications

        Deletes dismissed notifications, or every notification when
        `clear_all` is true. Returns the number cleared.
        """
        payload = request.get_json(silent=True) or {}
        clear_all = payload.get("clear_all")
        if isinstance(clear_all, str):
            clear_all = _parse_bool(clear_all)
        else:
            clear_all = bool(clear_all)
        cleared = notification_store.clear(dismissed_only=not clear_all)
        return {"cleared": cleared}, 200


# Display label for a capability-gated plugin's product-tool group in
# GET /api/tools. Keyed by `PluginManifest.name` (which today
# equals the capability it declares for both shipped product plugins); a
# future capability-gated plugin not in this map falls back to a titlecased
# rendering of its manifest name rather than needing this list touched.
_PRODUCT_TOOL_SERVER_LABELS = {"wiki": "Wiki", "scg": "Agentic Search"}


@ns.route("/tools")
class Tools(Resource):
    """List available tool integrations."""

    @api.doc(
        security="apikey",
        params={
            "project": {
                "description": (
                    "Configured project name. Includes tools from that "
                    "project's own MCP configuration."
                ),
                "in": "query",
                "type": "string",
            },
        },
        description=(
            "List every known tool integration with its enablement state, the MCP "
            "server it comes from, and a `scope` of `builtin`, `project`, `system`, "
            "or `plugin`. Pass `project` to include tools configured inside that "
            "project. Use the `tool_id` values in a session's `mcp_tools` "
            "allowlist to scope what a run may call."
        ),
    )
    @ns.response(200, "Tool list.", tools_list_model)
    @kit.auth_error()
    @guard.requires("sessions.read")
    def get(self) -> tuple[dict, int]:
        """List tools

        Returns every known tool integration with its enablement state, the
        MCP server it comes from, and a `scope` of `builtin`, `project`,
        `system`, or `plugin`. Pass `project` to include tools configured
        inside that project. Use the `tool_id` values in a session's
        `mcp_tools` allowlist to scope what a run may call.
        """
        project_cwd = _scoping_cwd(request.args.get("project"))
        # Include plugin MCP servers so they appear in the integrations list
        from mewbo_core.tooling.plugins import load_all_plugin_components

        fan_out = load_all_plugin_components()
        # ``project_cwd`` resolves through the project catalog, which includes
        # repository checkouts — so the path is repo content this deployment did
        # not author, even though the catalog vetted the NAME. A server entry is
        # a command spawned during config resolution, so the directory tier
        # contributes nothing; the operator's own config is unaffected.
        registry = load_registry(
            cwd=project_cwd,
            trust_cwd=False,
            extra_mcp_servers=fan_out.mcp_servers or None,
        )
        specs = registry.list_specs(include_disabled=True)

        # Determine scope: compare against global-only servers
        global_servers: set[str] = set()
        try:
            gpath = get_mcp_config_path()
            if gpath and os.path.exists(gpath):
                with open(gpath, encoding="utf-8") as _f:
                    gc = json.load(_f)
                    global_servers = set(gc.get("servers", gc.get("mcpServers", {})).keys())
        except Exception:
            pass

        plugin_servers = set(fan_out.mcp_servers.keys())

        tools = [
            {
                "tool_id": spec.tool_id,
                "name": spec.name,
                "kind": spec.kind,
                "enabled": spec.enabled,
                "description": spec.description,
                "disabled_reason": spec.metadata.get("disabled_reason"),
                "server": spec.metadata.get("server"),
                "scope": classify_tool_scope(
                    spec, global_servers=global_servers, plugin_servers=plugin_servers
                ),
                "requires_capability": None,
            }
            for spec in specs
        ]

        # Product/internal tools (wiki_*, scg_*, agentic_search) live in a
        # capability-gated SessionToolRegistry, not the MCP/core ToolRegistry
        # above — they never appeared here before. `fan_out` (already
        # fetched for its MCP servers) also carries every plugin's manifest +
        # raw session_tool_entries, so no new discovery machinery is needed:
        # a capability-gated plugin (non-empty `requires_capabilities`) becomes
        # one `server`-grouped entry per tool. First-registration-wins across
        # plugins, mirroring SessionToolRegistry.register's own dedupe — the
        # wiki and scg manifests both contribute the shared entity-minting
        # tools (mint_entity/relate_entities/resolve_entity), which must
        # appear exactly once, not once per contributing plugin.
        seen_product_ids: set[str] = set()
        for pc in fan_out.components:
            manifest = pc.manifest
            if manifest is None or not manifest.requires_capabilities:
                continue
            server_label = _PRODUCT_TOOL_SERVER_LABELS.get(
                manifest.name, manifest.name.replace("_", " ").title()
            )
            for entry in pc.session_tool_entries:
                tool_id = str(entry.get("tool_id", "")).strip()
                if not tool_id or tool_id in seen_product_ids:
                    continue
                seen_product_ids.add(tool_id)
                tools.append(
                    {
                        "tool_id": tool_id,
                        "name": tool_id,
                        "kind": "builtin",
                        "enabled": True,
                        "description": None,
                        "disabled_reason": None,
                        "server": server_label,
                        "scope": "plugin",
                        "requires_capability": manifest.requires_capabilities[0],
                    }
                )
        return {"tools": tools}, 200


@ns.route("/skills")
class Skills(Resource):
    """List available skills."""

    @api.doc(
        security="apikey",
        params={
            "project": {
                "description": (
                    "Configured project name. Includes skills defined inside "
                    "that project."
                ),
                "in": "query",
                "type": "string",
            },
        },
        description=(
            "List the available skills, including those contributed by installed "
            "plugins, with their descriptions, tool allowlists, and invocation "
            "flags. Pass `project` to include project-local skills. Activate a "
            "skill for a run via the `skill` field of a query."
        ),
    )
    @ns.response(200, "Skill list.", skills_list_model)
    @kit.auth_error()
    @guard.requires("sessions.read")
    def get(self) -> tuple[dict, int]:
        """List skills

        Returns the available skills, including those contributed by installed
        plugins, with their descriptions, tool allowlists, and invocation
        flags. Activate a skill for a run via the `skill` field of a query.
        """
        from mewbo_core.tooling.skills import SkillRegistry

        project_cwd = _scoping_cwd(request.args.get("project"))
        registry = SkillRegistry()
        registry.load(project_cwd)

        # Include plugin skills/commands so they appear in the skills list
        from mewbo_core.tooling.plugins import load_all_plugin_components

        fan_out = load_all_plugin_components()
        for pc in fan_out.components:
            if pc.manifest is None:
                continue
            plugin_source = f"plugin:{pc.manifest.name}"
            for sd in pc.skill_dirs:
                registry.load_extra_dir(sd, source=plugin_source)
            for cf in pc.command_files:
                registry.load_command_file(cf, source=plugin_source)

        skills = [
            {
                "name": s.name,
                "description": s.description,
                "allowed_tools": s.allowed_tools,
                "user_invocable": s.user_invocable,
                "disable_model_invocation": s.disable_model_invocation,
                "context": s.context,
                "source": s.source,
            }
            for s in registry.list_all()
        ]
        return {"skills": skills}, 200


@ns.route("/query")
class MewboQuery(Resource):
    """Synchronous query endpoint (CLI compatibility)."""

    @api.doc(
        security="apikey",
        description=(
            "Run the query to completion and return the full result in one "
            "response, including the executed action steps and the `session_id`. "
            "Prefer the asynchronous session endpoints for interactive use; this "
            "remains for simple CLI-style clients. A new session is created "
            "automatically unless `session_id`, `session_tag`, or `fork_from` "
            "selects an existing one."
        ),
    )
    @ns.response(200, "Completed run with the executed action steps.", task_queue_model)
    @ns.response(
        410, "A targeted session is permanently terminated.", session_terminated_error_model
    )
    @kit.errors(
        400,
        shape="message",
        descriptions={400: "The `query` field is missing, or the named project is invalid."},
    )
    @kit.auth_error()
    @ns.expect(sync_query_model)
    @guard.requires("sessions.create")
    def post(self) -> tuple[dict, int]:
        """Run a synchronous query

        Runs the query to completion and returns the full result in one
        response, including the executed action steps. POST /api/query remains
        supported as a simple synchronous alternative for CLI-style clients;
        prefer the asynchronous session endpoints for interactive use. A new
        session is created automatically unless `session_id`, `session_tag`,
        or `fork_from` selects an existing one.
        """
        request_data = request.get_json(silent=True) or {}
        user_query = request_data.get("query")
        if not user_query:
            return {"message": "Invalid input: 'query' is required"}, 400
        mode = _parse_mode(request_data.get("mode"))
        existing_sessions = set(runtime.session_store.list_sessions())
        try:
            session_id = runtime.resolve_session(
                session_id=request_data.get("session_id"),
                session_tag=request_data.get("session_tag"),
                fork_from=request_data.get("fork_from"),
            )
        except SessionTerminatedError:
            # A terminated fork_from SOURCE rejects at the core seam — the
            # kill switch also covers fork-resurrection.
            return _terminated_response()
        if session_id not in existing_sessions:
            notification_service.emit_session_created(session_id)
        # This sync path bypasses start_async entirely, so it needs its own
        # terminated guard: a resolved (existing) terminated session rejects
        # with 410 before any run. A freshly created/forked session is
        # never terminated, so this only bites session_id/session_tag targets.
        terminated = _terminated_guard(session_id)
        if terminated is not None:
            return terminated
        context_payload = _build_context_payload(request_data)

        # Validate BEFORE persisting: a malformed `device_tools` declaration
        # must 400 without poisoning the session's context event, or
        # `/message` re-engage and `/recover` (which read the LAST-PERSISTED
        # context) would inherit the same malformed declaration and 400
        # forever — a bricked session (whole-branch review, F6).
        try:
            allowed_tools, extra_session_tools = _derive_tool_grants(session_id, context_payload)
        except ValueError as exc:
            return {"message": str(exc)}, 400

        scope = _run_scope(
            allowed_tools=allowed_tools,
            # Same union as every other run-starting path. This endpoint resolves an
            # EXISTING session by id/tag as readily as it creates one, so a caller
            # driving a wiki session through it must not silently drop ``wiki``; on a
            # genuinely new session the spec has nothing session-owned to add and the
            # resolved set is byte-identical to the advertisement.
            client_capabilities=_session_specs.load(session_id).run_capabilities(
                _persisted_client_capabilities(context_payload)
            ),
        )
        _stamp_principal_subject(context_payload)

        if context_payload:
            runtime.append_context_event(session_id, context_payload)

        # Resolve project → cwd, falling back to a per-session temp dir
        try:
            project_cwd = _resolve_project_cwd(request_data) or session_temp_dir(session_id)
        except ValueError as exc:
            return {"message": str(exc)}, 400

        # Inline @<ref> context expansion (see reference_expansion.py).
        user_query = expand_references(
            user_query,
            project_cwd,
            attachments=_session_attachment_map(session_id),
        )

        logging.info("Received user query: {}", user_query)
        task_queue: TaskQueue = runtime.run_sync(
            user_query=user_query,
            session_id=session_id,
            approval_callback=scope.approval_callback,
            permission_policy=scope.permission_policy,
            mode=mode,
            allowed_tools=scope.allowed_tools,
            denied_tools=_extract_denied_tools(context_payload),
            strict_tool_scope=scope.strict_tool_scope,
            capability_mode=scope.capability_mode,
            cwd=project_cwd,
            source_platform=_request_surface(),
            extra_session_tools=extra_session_tools,
            attachments=_extract_attachments(request_data),
            project_autoselect=is_auto_project(_requested_project(request_data)),
        )
        notification_service.emit_completion(session_id)
        task_result = deepcopy(task_queue.task_result)
        to_return = task_queue.dict()
        to_return["task_result"] = task_result
        logging.info("Returning executed action plan.")
        to_return["session_id"] = session_id
        return to_return, 200


# ---------------------------------------------------------------------------
# Config API endpoints
# ---------------------------------------------------------------------------
# All protected/secret handling lives in ``ConfigSchemaView`` (config_view.py),
# a pure, single-traversal view over the AppConfig JSON schema. Construct one
# per request via ``ConfigSchemaView.from_model()`` (cheap).


@ns.route("/config/schema")
class ConfigSchemaResource(Resource):
    """Serve the JSON Schema for AppConfig (protected fields stripped)."""

    @api.doc(
        security="apikey",
        description=(
            "Return the JSON Schema describing the application configuration. "
            "Protected fields are stripped entirely and secret fields are marked "
            "`writeOnly`, so the schema can drive a settings UI directly. The "
            "current values are served separately by GET /api/config."
        ),
    )
    @api.response(200, "Configuration JSON Schema.")
    @kit.auth_error()
    @guard.requires("config.read")
    def get(self) -> tuple[dict, int]:
        """Get the configuration schema

        Returns the JSON Schema describing the application configuration.
        Protected fields are stripped entirely and secret fields are marked
        `writeOnly`, so the schema can drive a settings UI directly.
        """
        return ConfigSchemaView.from_model().public_schema(), 200


@ns.route("/config")
class ConfigResource(Resource):
    """Read and update the application configuration."""

    @api.doc(
        security="apikey",
        description=(
            "Return the current configuration with protected and secret values "
            "stripped, plus a `secrets` map reporting which secret fields are set "
            "(true/false) without revealing their values, and a `storage` object "
            "reporting whether the server can currently persist configuration "
            "changes — checked up front so a client can warn a user before they "
            "edit and save. Update with PATCH on the same path."
        ),
    )
    @ns.response(
        200,
        "Configuration values, secret status map, and storage writability.",
        config_response_model,
    )
    @kit.auth_error()
    @guard.requires("config.read")
    def get(self) -> tuple[dict, int]:
        """Get configuration

        Returns the current configuration with protected and secret values
        stripped, plus a `secrets` map reporting which secret fields are set
        (true or false) without revealing their values, and a `storage` object
        reporting whether the server can currently persist configuration
        changes.
        """
        view = ConfigSchemaView.from_model()
        data = get_config().model_dump()
        secrets = view.secret_status(data)
        storage = AppConfig.probe_write_access(get_app_config_path())
        return {
            "config": view.strip_values(data),
            "secrets": secrets,
            "storage": storage.model_dump(),
        }, 200

    @api.doc(
        security="apikey",
        description=(
            "Deep-merge the request body into the stored configuration, validate "
            "the result, and persist it. Attempts to modify protected fields are "
            "rejected with 403. A write-only (`writeOnly`) secret sent as an empty "
            "string leaves the stored value unchanged, since its value is never "
            "read back and a client cannot echo what it was not given; send `null` "
            "to clear one. A merge that fails validation returns 422 with the "
            "validation errors and changes nothing. If the merged configuration is "
            "valid but cannot be written to disk (e.g. a read-only mount), returns "
            "500 with a machine-readable `code` and an actionable `message`; "
            "nothing changes. Mirrors the shape served by GET /api/config/schema."
        ),
    )
    @ns.response(
        200, "Updated configuration values and secret status map.", config_response_model
    )
    @kit.errors(400, shape="message", descriptions={400: "The request body was empty."})
    @kit.errors(403, shape="message", descriptions={403: "The patch touched a protected field."})
    @ns.response(
        422,
        "Merged configuration failed validation; nothing was saved.",
        config_validation_error_model,
    )
    @ns.response(
        500,
        "The configuration is valid but could not be persisted; nothing was saved.",
        config_write_error_model,
    )
    @kit.auth_error()
    @ns.expect(config_patch_model)
    @guard.requires("config.write")
    def patch(self) -> tuple[dict, int]:
        """Update configuration

        Deep-merges the request body into the stored configuration, validates
        the result, and persists it. Attempts to modify protected fields are
        rejected with 403. A write-only secret sent as an empty string leaves
        the stored value unchanged; send `null` to clear one. A merge that
        fails validation returns 422 with the validation errors and changes
        nothing. If the write itself fails (e.g.
        the configuration store is on a read-only mount), returns 500 with a
        machine-readable `code` and an actionable `message`; nothing changes.
        """
        patch = request.get_json(silent=True) or {}
        if not patch:
            return {"message": "Empty payload"}, 400

        view = ConfigSchemaView.from_model()
        violations = view.reject_protected(patch)
        if violations:
            return {"message": f"Cannot modify protected fields: {violations}"}, 403

        config_path = get_app_config_path()
        raw = _load_json(config_path)
        # A write-only secret is never read back, so a client re-sends the
        # section it edited with that field EMPTY. The view decides what an
        # empty (unchanged) and an explicit null (clear) mean; resolve against
        # the operator's own document so an `${ENV_VAR}` reference is what gets
        # carried forward, not its resolved value.
        merged = _deep_merge(dict(raw), view.resolve_secret_writes(patch, raw))
        try:
            validated = AppConfig.model_validate(merged)
        except ValidationError as exc:
            return {"message": "Validation failed", "errors": exc.errors()}, 422
        # Persist the operator's own document with the patch applied, NOT a
        # re-render of `validated`. The model is the validator here, not the
        # thing serialized: rendering it would pin every unset field to a
        # default and bake this process's environment (the `runtime.*`
        # directories, the storage URI) into a file the CLI and other
        # containers also read. See AppConfig.write_document.
        try:
            AppConfig.write_document(config_path, merged)
        except ConfigWriteError as exc:
            logging.error("Failed to persist configuration to {}: {}", exc.path, exc.reason)
            return ApiResponseKit.config_write_error_response(exc)
        reset_config()

        data = validated.model_dump()
        secrets = view.secret_status(data)
        return {
            "config": view.strip_values(data),
            "secrets": secrets,
            "storage": ConfigWriteAccess(writable=True).model_dump(),
        }, 200


@ns.route("/plugins")
class PluginList(Resource):
    """List available plugins and their components."""

    @api.doc(
        security="apikey",
        description=(
            "List each available built-in or installed plugin with its display name, "
            "version, source marketplace, scope, enabled state, and component counts "
            "(skills, agents, commands, MCP servers, hooks). Browse installable plugins "
            "via GET /api/plugins/marketplace."
        ),
    )
    @ns.response(200, "Available plugin list.", plugins_list_model)
    @kit.auth_error()
    @guard.requires("plugins.read")
    def get(self) -> tuple[dict, int]:
        """List available plugins

        O(collection) in the configured plugin collection. Returns the same
        built-in and enabled installed plugin components the session loader binds,
        without reading the components of each listed plugin again.
        """
        from mewbo_core.tooling.plugins import load_all_plugin_components

        plugins = load_all_plugin_components().components
        return PluginsListResponse(
            plugins=[
                PluginListItem(
                    name=pc.manifest.name,
                    display_name=pc.manifest.display_name or pc.manifest.name,
                    description=pc.manifest.description,
                    version=pc.manifest.version,
                    marketplace=pc.manifest.marketplace,
                    scope=pc.manifest.scope,
                    enabled=True,
                    skills=len(pc.skill_dirs),
                    agents=len(pc.agent_files),
                    commands=len(pc.command_files),
                    mcp_servers=len(pc.mcp_config or {}),
                    has_hooks=pc.hooks_config is not None,
                )
                for pc in plugins
                if pc.manifest is not None
            ]
        ).response()


@ns.route("/plugins/marketplace")
class PluginMarketplace(Resource):
    """List and install plugins from configured marketplaces."""

    @api.doc(
        security="apikey",
        description=(
            "List the plugins available for installation from the configured "
            "marketplaces. Install one with POST on this same path."
        ),
    )
    @ns.response(200, "Available plugin list.", marketplace_plugins_model)
    @kit.auth_error()
    @guard.requires("plugins.read")
    def get(self) -> tuple[dict, int]:
        """List marketplace plugins

        Returns the plugins available for installation from the configured
        marketplaces. Install one with POST on this same path.
        """
        from mewbo_core.config import get_config
        from mewbo_core.tooling.plugins import discover_marketplace_plugins

        cfg = get_config().plugins
        return {
            "plugins": discover_marketplace_plugins(marketplace_dirs=cfg.resolve_marketplace_dirs())
        }, 200

    @api.doc(
        security="apikey",
        description=(
            "Install the named plugin from a configured marketplace. Its skills, "
            "commands, agents, and MCP servers become available to sessions "
            "started after installation. Both `name` and `marketplace` are "
            "required (from GET /api/plugins/marketplace)."
        ),
    )
    @ns.response(200, "Plugin installed.", plugin_install_response_model)
    @ns.response(400, "Missing fields or unknown plugin/marketplace.", plugin_error_model)
    @ns.response(500, "Installation failed.", plugin_error_model)
    @kit.auth_error()
    @ns.expect(plugin_install_model)
    @guard.requires("plugins.admin")
    def post(self) -> tuple[dict, int]:
        """Install a plugin

        Installs the named plugin from a configured marketplace. Its skills,
        commands, agents, and MCP servers become available to sessions started
        after installation.
        """
        data = request.get_json(silent=True) or {}
        name = data.get("name")
        marketplace = data.get("marketplace")
        if not name or not marketplace:
            return {"error": "name and marketplace required"}, 400

        from mewbo_core.config import get_config
        from mewbo_core.tooling.plugins import install_plugin

        cfg = get_config().plugins
        try:
            manifest = install_plugin(
                name,
                marketplace,
                marketplace_dirs=cfg.resolve_marketplace_dirs(),
                install_base=cfg.resolve_install_dir(),
            )
            return {"installed": manifest.name, "version": manifest.version}, 200
        except ValueError as exc:
            return {"error": str(exc)}, 400
        except Exception as exc:
            return {"error": str(exc)}, 500


@ns.route("/plugins/<string:plugin_name>")
class PluginDetail(Resource):
    """Manage a specific installed plugin."""

    @api.doc(
        security="apikey",
        params={"plugin_name": "Name of an installed plugin, as listed by GET /api/plugins."},
        description=(
            "Remove an installed plugin and its components from the install "
            "directory. Sessions started afterward no longer see its skills, "
            "commands, agents, or MCP servers."
        ),
    )
    @ns.response(200, "Plugin uninstalled.", plugin_uninstall_model)
    @ns.response(404, "Plugin not found.", plugin_error_model)
    @kit.auth_error()
    @guard.requires("plugins.admin")
    def delete(self, plugin_name: str) -> tuple[dict, int]:
        """Uninstall a plugin

        Removes an installed plugin and its components from the install
        directory.
        """
        from mewbo_core.config import get_config
        from mewbo_core.tooling.plugins import uninstall_plugin

        cfg = get_config().plugins
        if uninstall_plugin(plugin_name, install_base=cfg.resolve_install_dir()):
            return {"uninstalled": plugin_name}, 200
        return {"error": "Plugin not found"}, 404


# Route permission coverage, checked at BOOT — the last statement that runs
# after every route module has imported and registered.
#
# Strict: an unbound route is a boot failure, not a warning. A route that
# reaches the URL map without declaring what it requires is indistinguishable
# from one whose author forgot, and the whole point of declaring access above
# the handler is that "forgot" cannot be silent. Refusing to start is the only
# reading of that anyone acts on — a logged warning on a server that came up
# fine is a warning nobody sees.
#
# The single exemption is stated rather than hidden: an exempted route still
# appears in the report as unbound, so the coverage number keeps telling the
# truth and the reason travels with it.
_SCIM_BEARER_GATED = (
    "authenticated by the SCIM bearer secret in the blueprint's before_request "
    "gate, not by a permission: the caller is an identity provider, which holds "
    "no Mewbo principal to carry roles"
)

guard_registry.guard.audit(
    app,
    strict=True,
    allow_unbound={
        "mewbo_api.backend.SessionStream.get": (
            "hand-builds its 401 via json.dumps under the byte-identical law"
        ),
        # The SCIM surface mounts only when auth AND scim are enabled, so it is
        # invisible to an audit taken with auth off — which is exactly how it
        # stayed unbound until this check ran in a boot with auth on. Listed one
        # handler at a time on purpose: a blanket prefix exemption would also
        # swallow a route someone adds to this blueprint later.
        #
        # The keys name the CLOSURES `_build_blueprint()` binds, not the
        # controller methods they delegate to — the closure is what Flask holds
        # in `view_functions`, so it is what the audit sees.
        **{
            f"mewbo_api.scim.routes._build_blueprint.<locals>.{handler}": _SCIM_BEARER_GATED
            for handler in (
                "service_provider_config",
                "list_users",
                "create_user",
                "get_user",
                "replace_user",
                "patch_user",
                "delete_user",
                "list_groups",
                "create_group",
                "get_group",
                "replace_group",
                "patch_group",
                "delete_group",
            )
        },
    },
)


def main() -> None:
    """Run the Mewbo API server (local-dev runner only).

    Production serves ``mewbo_api.backend:app`` under gunicorn, which imports the
    WSGI app directly and never calls this — so ``app.run`` is a developer
    convenience reachable via the ``mewbo-api`` console script, not a deployment
    path.
    """
    # debug defaults OFF: the Werkzeug reloader/debugger is an interactive
    # code-execution surface that must never run unless a developer explicitly
    # opts in for a local session. Opt in with MEWBO_API_DEBUG=1.
    debug = os.environ.get("MEWBO_API_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
    app.run(debug=debug, host="0.0.0.0", port=5124)


if __name__ == "__main__":
    main()
