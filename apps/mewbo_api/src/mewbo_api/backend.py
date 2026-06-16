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
import uuid
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone

from flask import Flask, Response, request, stream_with_context
from flask_restx import Api, Resource, fields
from mewbo_core.attachments import (
    is_image,
    is_supported,
    model_supports_vision,
    parse_to_markdown,
    parsed_sidecar_path,
)
from mewbo_core.classes import TaskQueue
from mewbo_core.common import get_logger
from mewbo_core.config import (
    AppConfig,
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
from mewbo_core.context import _iter_attachments
from mewbo_core.exit_plan_mode import PLAN_DIR_ROOT, plan_file_for, session_temp_dir
from mewbo_core.key_store import KeyStoreBase, create_key_store
from mewbo_core.notifications import NotificationStore
from mewbo_core.permissions import auto_approve
from mewbo_core.project_store import VirtualProject, create_project_store
from mewbo_core.session_runtime import SessionRuntime, parse_core_command
from mewbo_core.session_store import SessionStoreBase, create_session_store
from mewbo_core.share_store import ShareStore
from mewbo_core.tool_registry import ToolSpec, load_registry
from mewbo_core.types import EventRecord
from mewbo_core.worktree import WorktreeBranchInUseError, WorktreeManager
from mewbo_tools.integration.file_catalog import FileCatalog
from mewbo_tools.integration.reference_expansion import expand_references
from pydantic import ValidationError
from werkzeug.exceptions import NotFound
from werkzeug.utils import secure_filename

from mewbo_api.config_view import ConfigSchemaView
from mewbo_api.repo_identity import RepoIdentity
from mewbo_api.request_context import request_surface
from mewbo_api.responses import ApiResponseKit

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


# Get the API token from app config
MASTER_API_TOKEN = os.environ.get("MASTER_API_TOKEN") or get_config_value(
    "api", "master_token", default="msk-strong-password"
)

# Initialize logger
logging = get_logger(name="mewbo-api")
logging.info("Starting Mewbo API server.")
logging.debug("API master token configured: {}", "yes" if MASTER_API_TOKEN else "no")

_config = get_config()
if _config.runtime.preflight_enabled:
    start_preflight(_config)

# Load hooks from config so API sessions run with configured hooks
from mewbo_core.hooks import HookManager as _HookManager  # noqa: E402
from mewbo_core.session_event_bus import (  # noqa: E402
    SessionEventBus,
    Subscription,
    get_session_event_bus,
)

_hook_manager = _HookManager.load_from_config(_config.hooks)
# Bridge the append-time event bus to the hook manager: every appended event
# fires the configured ``on_event`` hooks. This is the ONLY place the bus and
# hook manager connect (the SSE stream subscribes to the same bus directly).
get_session_event_bus().register_observer(_hook_manager.run_on_event)

# Create Flask application
app = Flask(__name__)
session_store = create_session_store()
key_store: KeyStoreBase = create_key_store()
project_store = create_project_store()


def _auto_cleanup_worktree_on_session_end(session_id: str, error: str | None) -> None:
    """Auto-remove a worktree-backed session's worktree if it is clean.

    Mirrors the Claude Code default: when a worktree-bound session ends and
    leaves no uncommitted changes / unpushed commits behind, drop the
    worktree. Otherwise keep it so the user can resume or recover work.

    After reaping the child worktree, also reaps the auto-promoted parent if it
    now has no remaining worktree children (the #53 orphan-parent symptom). An
    auto-promoted parent is identified by ``path_source == "provided"`` — it was
    lifted from a config project and is system-owned, not user-created.

    Failures are swallowed — this is best-effort housekeeping, never blocking.
    """
    try:
        events = session_store.load_transcript(session_id)
    except Exception:
        return
    project_name: str | None = None
    for evt in events:
        if evt.get("type") != "context":
            continue
        payload = evt.get("payload") or {}
        candidate = payload.get("project")
        if isinstance(candidate, str) and candidate:
            project_name = candidate
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
# error bodies (envelope or legacy ``{"message"}`` shape) per route — see
# ``responses.py``. Wire it once here so import-time decorators can see it.
kit = ApiResponseKit(ns, prefix="Api")


@app.errorhandler(NotFound)
def _handle_not_found(exc: NotFound) -> tuple[dict, int]:
    """Return JSON for any unmatched route (no raw Werkzeug HTML leak).

    A request that matches no route (e.g. a ``project`` containing a ``/``
    that splits the path) would otherwise render Werkzeug's HTML 404 page.
    One app-level handler keeps every endpoint's 404 a JSON contract.
    """
    return {"error": {"code": 404, "reason": exc.description}}, 404


def _session_not_found(session_id: str) -> tuple[dict, int]:
    """Canonical JSON 404 envelope for an unknown session id (#64).

    Matches the ``@app.errorhandler(NotFound)`` shape so the MCP ``_enveloped``
    not-found mapping reads it identically whether the route or Werkzeug raised.
    """
    return {"error": {"code": 404, "reason": f"session {session_id} not found"}}, 404


def _session_exists(session_id: str) -> bool:
    """True iff *session_id* is a real stored session (the canonical guard)."""
    return session_id in runtime.session_store.list_sessions()


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

    Opt-in via ?truncate=1 so the console's full-result view is unaffected (#42).
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


@app.before_request
def log_request_info() -> None:
    """Log request metadata for debugging."""
    logging.debug("Endpoint: {}", request.endpoint)
    logging.debug("Headers: {}", request.headers)
    logging.debug("Body: {}", request.get_data())


_CORS_ORIGIN = os.environ.get("CORS_ORIGIN", "*")


@app.after_request
def _add_cors_headers(response: Response) -> Response:
    """Allow cross-origin requests. Set CORS_ORIGIN env var to restrict."""
    response.headers["Access-Control-Allow-Origin"] = _CORS_ORIGIN
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, X-API-Key, X-Mewbo-Capabilities, X-Mewbo-Surface"
    )
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
    return response


def _request_credential() -> str | None:
    """Return the presented credential (header preferred, query param for SSE)."""
    return request.headers.get("X-API-Key") or request.args.get("api_key")


def _token_matches_master(token: str) -> bool:
    """Constant-time compare *token* against the master token.

    Uses ``hmac.compare_digest`` so the master credential cannot be recovered
    via a byte-by-byte timing side-channel — the same primitive the KeyStore
    already uses to verify hashed keys.
    """
    return hmac.compare_digest(token, MASTER_API_TOKEN)


def _require_api_key() -> tuple[dict, int] | None:
    """Authorize a protected route.

    A request is authorized if the presented credential equals the master
    token (break-glass) OR matches a non-revoked stored key via the
    ``KeyStore``. Accepts the ``X-API-Key`` header or the ``api_key`` query
    param (the latter for SSE, where EventSource cannot set headers).
    """
    api_token = _request_credential()
    if api_token is None:
        return {"message": "API token is not provided."}, 401
    if _token_matches_master(api_token):
        return None
    if key_store.verify_key(api_token) is not None:
        return None
    logging.warning("Unauthorized API call attempt from {}.", request.remote_addr)
    return {"message": "Unauthorized"}, 401


def _require_master_token() -> tuple[dict, int] | None:
    """Authorize a master-token-only route (e.g. API key management).

    Issued keys are deliberately rejected here: a leaked key must not be able
    to mint or revoke keys, or revocation would be meaningless.
    """
    api_token = _request_credential()
    if api_token is None:
        return {"message": "API token is not provided."}, 401
    if not _token_matches_master(api_token):
        logging.warning("Unauthorized key-management attempt from {}.", request.remote_addr)
        return {"message": "Unauthorized"}, 401
    return None


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
            from mewbo_api.ide import IdeManager, IdeStore
            from mewbo_api.ide_routes import ide_ns, init_ide

            _ide_store = IdeStore(_mongo_db)
            _ide_manager = IdeManager(_web_ide_cfg, _ide_store)
            init_ide(_ide_manager, runtime, _require_api_key)
            api.add_namespace(ide_ns, path="/api")
            logging.info("web_ide namespace registered at /api")
        except Exception as exc:  # pragma: no cover - startup fail-soft
            logging.warning("web_ide namespace failed to initialize: {}", exc)
            _ide_manager = None


# -- Agentic Search namespace ---------------------------------------------
# Persistent workspaces + runs (JSON/Mongo via the store) and a run lifecycle
# driven by the per-run resolved SearchRunner (echo replay, or the orchestrated
# SCG runner once scg.enabled is on and a source is mapped).
from mewbo_api.agentic_search import init_agentic_search  # noqa: E402

init_agentic_search(api, _require_api_key, runtime=runtime)
logging.info("agentic_search namespace registered at /api")


# -- Structured-response namespace ----------------------------------------
# Schema-constrained synthesis over the core StructuredResponder (down-only
# compose). POST /v1/structured returns a JSON-Schema-validated object — the
# default 'agentic' mode after a bounded session, or an inline no-loop
# 'synthesis' mode (the former /v1/structured/fast lane, folded in by #85).
from mewbo_api.structured import init_structured  # noqa: E402

init_structured(api, _require_api_key, runtime=runtime)
logging.info("structured namespace registered at /v1/structured")

# Token-streaming draft synthesis (POST /v1/draft/stream) — #50/#78.
from mewbo_api.realtime import init_realtime  # noqa: E402

init_realtime(api, _require_api_key, runtime=runtime)
logging.info("realtime draft-stream endpoint registered at /v1/draft/stream")

# -- VCS automation namespace ----------------------------------------------
# Agent pickup for GitHub/Gitea Actions: assigning or @mentioning the bot on
# an issue/PR posts here; the endpoint binds a session to the right branch
# worktree and starts/continues the run (issue #72).
from mewbo_api.vcs_pickup import init_vcs_pickup, vcs_ns  # noqa: E402

init_vcs_pickup(
    runtime,
    _require_api_key,
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
    """Extract MCP tool allowlist from context payload, if present."""
    if not context_payload:
        return None
    mcp_tools = context_payload.get("mcp_tools")
    if isinstance(mcp_tools, list) and mcp_tools:
        return [str(t) for t in mcp_tools if t]
    return None


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


def _resolve_project_cwd(request_data: dict[str, object]) -> str | None:
    """Resolve a project from the request to its filesystem path.

    Handles both config projects (by name) and managed projects
    (``managed:<project_id>``). Returns ``None`` if no project is specified.
    Raises ``ValueError`` when a project identifier is given but invalid.
    """
    project_name = request_data.get("project")
    if not project_name or not isinstance(project_name, str):
        ctx = request_data.get("context")
        if isinstance(ctx, dict):
            project_name = ctx.get("project")
    if not project_name or not isinstance(project_name, str):
        return None
    project_name = project_name.strip()
    if not project_name:
        return None

    # Managed (virtual) project: "managed:<uuid>"
    if project_name.startswith("managed:"):
        vpid = project_name[len("managed:") :]
        proj = project_store.get_project(vpid)
        if proj is None:
            raise ValueError(f"Managed project '{vpid}' not found.")
        if not os.path.isdir(proj.path):
            os.makedirs(proj.path, exist_ok=True)
        return proj.path

    # Config-defined project
    projects = get_config().projects
    project = projects.get(project_name)
    if project is None:
        raise ValueError(f"Project '{project_name}' not configured.")
    if not project.path:
        raise ValueError(f"Project '{project_name}' has no path configured.")
    if not os.path.isdir(project.path):
        raise ValueError(
            f"Project '{project_name}' directory not found: {project.path}. "
            f"In Docker, mount it via docker-compose.override.yml."
        )
    return project.path


class ExternalCwdPolicy:
    """Gate and validate caller-supplied host paths for session working directories.

    Resolution order (applied by :meth:`resolve`):
    1. Explicit ``cwd`` from the request (top-level or ``context.cwd``) —
       wins over a project-derived path when ``api.allow_external_cwd`` is on.
    2. Project-derived path via :func:`_resolve_project_cwd` — unchanged
       existing behaviour.
    3. ``None`` — caller falls back to ``session_temp_dir``.

    The flag and validation are co-located here so the route functions stay
    thin; DI with ``AppConfig`` keeps this testable without touching module
    globals.
    """

    def __init__(self, config: AppConfig) -> None:
        """Initialise with the application config (reads ``api.allow_external_cwd``)."""
        self._enabled: bool = config.api.allow_external_cwd

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

    def resolve(
        self,
        request_data: dict[str, object],
    ) -> tuple[str | None, tuple[dict, int] | None]:
        """Resolve the working directory for a session start.

        Returns ``(cwd, None)`` on success or ``(None, error_response)`` when
        the caller provided a ``cwd`` that failed validation.
        On success ``cwd`` may be ``None`` — the caller should fall back to
        ``session_temp_dir`` or another default.
        """
        raw_cwd = self._extract_cwd(request_data)
        if raw_cwd is not None:
            if not self._enabled:
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
            return raw_cwd, None
        # No explicit cwd → delegate to project resolution.
        return None, None


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
    from mewbo_core.skills import SkillRegistry, activate_skill

    # Check top-level "skill" field.
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

init_channels(app, runtime, _hook_manager, _config)

# Wiki backend (opt-in via mewbo-api[wiki] extras).
from mewbo_api.wiki import init_wiki  # noqa: E402

# Pass the shared hook manager so the wiki-qa hypervisor's session-end finalizer
# can emit the terminal ``complete`` event + reconcile the answer snapshot
# (the QA counterpart to indexing's wiki_finalize tool).
init_wiki(app, runtime, hook_manager=_hook_manager)


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
                "`skill`, and `fallback_models`."
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
                "`skill`, and `fallback_models`."
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
                "keys include `project`, `model`, and `mcp_tools` (tool allowlist)."
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

session_summary_model = ns.model(
    "SessionSummary",
    {
        "session_id": fields.String(example="9e2d47c1a0b34f12"),
        "title": fields.String(example="Refactor the billing pipeline"),
        "status": fields.String(
            example="completed", description="`idle`, `running`, `completed`, or `incomplete`."
        ),
        "done_reason": fields.String(example="completed"),
        "origin": fields.String(
            example="user", description="`user`, `wiki`, `search`, or `channel`."
        ),
        "recoverable": fields.Boolean(example=False),
        "created_at": fields.String(example="2026-06-15T18:24:05.412903+00:00"),
        "updated_at": fields.String(example="2026-06-15T18:31:42.108551+00:00"),
    },
)

sessions_list_model = ns.model(
    "SessionsListResponse",
    {"sessions": fields.List(fields.Nested(session_summary_model))},
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
            example=None, description="Originating MCP server (MCP tools only)."
        ),
        "scope": fields.String(
            example="global", description="`global`, `project`, or `plugin`."
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
    },
)

plugin_model = ns.model(
    "Plugin",
    {
        "name": fields.String(example="code-review"),
        "description": fields.String(example="Multi-agent code review."),
        "version": fields.String(example="1.2.0"),
        "marketplace": fields.String(example="official"),
        "scope": fields.String(example="user"),
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
    def post(self) -> tuple[dict, int]:
        """Mint an API key

        Creates a new API key for use in the `X-API-Key` header. The plaintext
        key is returned exactly once in this response and cannot be retrieved
        again, so store it securely. Requires the master token; keys minted
        here cannot manage other keys.
        """
        auth_error = _require_master_token()
        if auth_error:
            return auth_error
        payload = request.get_json(silent=True) or {}
        label = str(payload.get("label", "")).strip()
        if not label:
            return {"message": "Invalid input: 'label' is required"}, 400
        plaintext, record = key_store.create_key(label)
        return {
            "id": record["id"],
            "label": record["label"],
            "key": plaintext,
            "created_at": record["created_at"],
        }, 201

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
    def get(self) -> tuple[dict, int]:
        """List API keys

        Returns metadata for every key: id, label, creation time, and
        revocation state. Hashes and plaintext key values are never included.
        Requires the master token.
        """
        auth_error = _require_master_token()
        if auth_error:
            return auth_error
        return {"keys": key_store.list_keys()}, 200


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
    def delete(self, key_id: str) -> tuple[dict, int]:
        """Revoke an API key

        Permanently revokes the key. Requests presenting a revoked key are
        rejected with 401 from that point on. Requires the master token.
        """
        auth_error = _require_master_token()
        if auth_error:
            return auth_error
        if not key_store.revoke_key(key_id):
            return {"message": f"Key '{key_id}' not found"}, 404
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
    def get(self) -> tuple[dict, int]:
        """List available models

        Returns the model names served by the configured LLM proxy, the
        default model, and a per-model capability map. Use
        `capabilities[name].supports_vision` to decide whether image
        attachments can be sent to a given model.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        default_model = get_config_value("llm", "default_model", default="unknown")
        try:
            models = get_config().llm.list_models()
        except ValueError:
            models = [default_model] if default_model != "unknown" else []
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
    def get(self) -> tuple[dict, int]:
        """List projects

        Returns configuration-defined and managed projects in one list. Each
        entry carries an `available` flag (whether its path exists on disk)
        and, for git checkouts, a `repo` identity plus `aliases` such as
        `owner/repo` that address the same project elsewhere in the API.
        Managed worktrees appear as child entries with `is_worktree` set.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        # Config-defined projects
        result: list[dict] = [
            _enrich_project_identity(
                {
                    "name": name,
                    "path": cfg.path,
                    "description": cfg.description,
                    "available": os.path.isdir(cfg.path),
                    "source": "config",
                }
            )
            for name, cfg in get_config().projects.items()
            if cfg.path
        ]
        # Managed (virtual) projects (includes worktrees as child entries).
        for vp in project_store.list_projects():
            result.append(
                _enrich_project_identity(
                    {
                        "name": vp.name,
                        "project_id": vp.project_id,
                        "path": vp.path,
                        "description": vp.description,
                        "available": os.path.isdir(vp.path),
                        "source": "managed",
                        "is_worktree": vp.is_worktree,
                        "parent_project_id": vp.parent_project_id,
                        "branch": vp.branch,
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
    def post(self) -> tuple[dict, int]:
        """Create a managed project

        Registers a project managed by the server, as opposed to one defined
        in static configuration. When `path` is omitted the server provisions
        a folder for it. Use the returned `project_id` with the other
        `/api/v_projects` endpoints, and as `managed:<project_id>` when
        creating sessions.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
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
    def get(self, project_id: str) -> tuple[dict, int]:
        """Get a managed project

        Returns the full project record, including its filesystem path,
        worktree linkage (`is_worktree`, `parent_project_id`, `branch`), and
        timestamps. Only managed project ids are accepted here; configured
        projects are listed via GET /api/projects.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
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
    def patch(self, project_id: str) -> tuple[dict, int]:
        """Update a managed project

        Updates the name and/or description. Fields omitted from the body are
        left unchanged. The path and worktree linkage of a project cannot be
        changed after creation.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
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
    def delete(self, project_id: str) -> tuple[dict, int]:
        """Delete a managed project

        Removes the managed project record. Returns 204 with an empty body on
        success.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
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
    """
    proj = project_store.get_project(project_key)
    if proj is not None:
        if proj.is_worktree:
            return None, ({"message": "Cannot manage worktrees of a worktree."}, 400)
        return _RepoTarget(
            project_id=proj.project_id,
            name=proj.name,
            path=proj.path,
            source="managed",
        ), None

    cfg_projects = get_config().projects
    cfg = cfg_projects.get(project_key)
    if cfg is None or not cfg.path:
        # Fall back to git-identity matching before declaring a miss.
        target, err = _resolve_repo_by_identity(project_key)
        if target is not None or err is not None:
            return target, err
        return None, ({"message": f"Project '{project_key}' not found"}, 404)

    if not promote:
        return _RepoTarget(
            project_id=None,
            name=project_key,
            path=cfg.path,
            source="config",
        ), None

    promoted = _promote_config_project(
        name=project_key, path=cfg.path, description=cfg.description or ""
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
    def get(self, project_id: str) -> tuple[dict, int]:
        """List branches

        Returns `branches`, the `current_branch` (null when HEAD is detached),
        and `branches_in_use`, the branches already checked out by the parent
        repository or another worktree. UIs should disable in-use entries,
        since creating a worktree for them fails. When the project path is
        missing or not a git repository the call still returns 200 with
        `git_repo` false and a `reason`.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
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
    def get(self, project_id: str) -> tuple[dict, int]:
        """List worktrees

        Returns the union of worktrees created through this API and worktrees
        added on disk with plain git. Each entry has a `managed` flag; managed
        entries carry a `project_id` that sessions can be pinned to. Every
        entry includes a `clean` flag indicating it has no uncommitted
        changes.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
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
    def post(self, project_id: str) -> tuple[dict, int]:
        """Create a worktree

        Checks out `branch` in a new worktree folder and registers it as a
        child managed project. Pass `base` to create a fresh branch from that
        ref instead of requiring `branch` to exist. Configured projects are
        promoted to managed projects automatically so the worktree gets a
        stable parent. Worktree lifecycle is system owned: a clean worktree is
        removed automatically when its session ends.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        payload = request.get_json(silent=True) or {}
        branch = str(payload.get("branch", "")).strip()
        # Optional ``base`` — when provided, the backend creates a fresh
        # branch from <base> via ``git worktree add -b <branch> <path> <base>``.
        # When absent, ``branch`` must already exist locally / as a remote
        # tracking ref. This mirrors the Claude Code worktree workflow.
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
    def delete(self, project_id: str, worktree_id: str) -> tuple[dict, int]:
        """Remove a worktree

        Removes a managed worktree. The call is idempotent: deleting a
        worktree that is already gone returns 200 with status
        `already_absent`. A worktree with uncommitted changes is refused with
        409 unless `force=true`. Worktrees created outside this API must be
        removed with git directly.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
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
        },
        description=(
            "List one summary per session — status, title, timestamps, and an "
            "`origin` (`user`, `wiki`, `search`, or `channel`) describing what "
            "created it. Archived sessions are hidden unless "
            "`include_archived=true`."
        ),
    )
    @ns.response(200, "Session summaries.", sessions_list_model)
    @kit.auth_error()
    def get(self) -> tuple[dict, int]:
        """List sessions

        Returns one summary per session with status, title, timestamps, and an
        `origin` field (`user`, `wiki`, `search`, or `channel`) describing
        what created it. Archived sessions are hidden unless
        `include_archived=true`.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        include_archived = _parse_bool(request.args.get("include_archived"))
        sessions = runtime.list_sessions(include_archived=include_archived)
        return {"sessions": sessions}, 200

    @api.doc(
        security="apikey",
        description=(
            "Create an empty session and return its `session_id`. Optionally bind "
            "a project, apply a lookup `session_tag`, and persist initial context "
            "(e.g. the model). Clients may declare capabilities via the "
            "`X-Mewbo-Capabilities` header (comma separated). Run queries with "
            "POST /api/sessions/{session_id}/query. An explicit `cwd` requires "
            "`api.allow_external_cwd`."
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
            403: "An explicit `cwd` was supplied but `api.allow_external_cwd` is disabled.",
        },
    )
    @kit.auth_error()
    @ns.expect(session_create_model)
    def post(self) -> tuple[dict, int]:
        """Create a session

        Creates an empty session and returns its `session_id`. Optionally
        binds a project, applies a lookup tag, and persists initial context
        such as the model to use. Clients may declare capabilities via the
        `X-Mewbo-Capabilities` header (comma separated). Run queries against
        the session with POST /api/sessions/{session_id}/query.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        payload = request.get_json(silent=True) or {}
        session_id = runtime.session_store.create_session()
        notification_service.emit_session_created(session_id)
        session_tag = payload.get("session_tag")
        if session_tag:
            runtime.session_store.tag_session(session_id, session_tag)
        context_payload = _build_context_payload(payload)
        # Capability header — clients may declare supported features (e.g. "stlite"
        # for the widget builder). Parse comma-separated values and persist in the
        # session context so the Orchestrator can conditionally enable agent types.
        capabilities_header = request.headers.get("X-Mewbo-Capabilities", "")
        if capabilities_header:
            client_capabilities = [
                c.strip() for c in capabilities_header.split(",") if c.strip()
            ]
            if client_capabilities:
                context_payload["client_capabilities"] = client_capabilities
        # External cwd (Grove / workspace managers): explicit cwd wins.
        ext_policy = ExternalCwdPolicy(get_config())
        ext_cwd, ext_err = ext_policy.resolve(payload)
        if ext_err is not None:
            return ext_err
        if ext_cwd is not None:
            context_payload["cwd"] = ext_cwd
        # Include project in context if provided
        if ext_cwd is None:
            try:
                project_cwd = _resolve_project_cwd(payload)
            except ValueError:
                project_cwd = None
            if project_cwd:
                project_name = payload.get("project") or ""
                if not project_name:
                    ctx = payload.get("context")
                    if isinstance(ctx, dict):
                        project_name = ctx.get("project", "")
                context_payload["project"] = project_name
                _populate_worktree_context(project_name, context_payload)
        if "model" not in context_payload:
            context_payload["model"] = get_config_value("llm", "default_model", default="unknown")
        if context_payload:
            runtime.append_context_event(session_id, context_payload)
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
    @kit.errors(
        400,
        shape="message",
        descriptions={400: "The `query` field is missing, or the named project is invalid."},
    )
    @kit.errors(
        403,
        descriptions={403: "An explicit `cwd` was supplied but `api.allow_external_cwd` is off."},
    )
    @kit.errors(
        409, shape="message", descriptions={409: "A run is already active for this session."}
    )
    @kit.auth_error()
    @ns.expect(session_query_model)
    def post(self, session_id: str) -> tuple[dict, int]:
        """Run a session query

        Starts an asynchronous run for `query` on the session and returns 202
        immediately; follow progress via the events or stream endpoints. The
        slash commands `/terminate` and `/status` are handled inline without
        starting a run. A session executes one run at a time, so a second
        call while one is active returns 409.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        request_data = request.get_json(silent=True) or {}
        user_query = request_data.get("query")
        if not user_query:
            return {"message": "Invalid input: 'query' is required"}, 400

        command_response = _handle_slash_command(session_id, user_query)
        if command_response is not None:
            return command_response

        if runtime.is_running(session_id):
            return {"message": "Session is already running."}, 409

        context_payload = _build_context_payload(request_data)
        # Capability header — same parsing as Sessions.post() for per-query declarations.
        capabilities_header = request.headers.get("X-Mewbo-Capabilities", "")
        if capabilities_header:
            client_capabilities = [
                c.strip() for c in capabilities_header.split(",") if c.strip()
            ]
            if client_capabilities:
                context_payload["client_capabilities"] = client_capabilities
        source_platform = _request_surface()

        # External cwd (Grove / workspace managers): explicit cwd wins.
        ext_policy = ExternalCwdPolicy(get_config())
        ext_cwd, ext_err = ext_policy.resolve(request_data)
        if ext_err is not None:
            return ext_err
        if ext_cwd is not None:
            context_payload["cwd"] = ext_cwd

        # Use model from context if provided, else config default
        if "model" not in context_payload:
            context_payload["model"] = get_config_value("llm", "default_model", default="unknown")
        if context_payload:
            runtime.append_context_event(session_id, context_payload)

        mode = _parse_mode(request_data.get("mode"))

        allowed_tools = _extract_allowed_tools(context_payload)

        # Skill activation: resolve from top-level "skill" field or context.skill.
        skill_instructions = _resolve_skill_instructions(request_data, user_query, context_payload)

        # Resolve project → cwd: explicit external cwd wins, then project, then temp dir.
        if ext_cwd is not None:
            project_cwd = ext_cwd
        else:
            try:
                project_cwd = _resolve_project_cwd(request_data) or session_temp_dir(session_id)
            except ValueError as exc:
                return {"message": str(exc)}, 400

        # Inline @<ref> context expansion — files/dirs/@diff/URLs resolved
        # against the session cwd, pre-LLM. File/dir refs are scoped to the
        # project's git index (or session attachments); see reference_expansion.
        user_query = expand_references(
            user_query,
            project_cwd,
            attachments=_session_attachment_map(session_id),
        )

        # Extract model for orchestration (may differ from config default)
        model_name = str(context_payload.get("model", "")) or None
        fallback_models = _extract_fallback_models(context_payload)

        budget = int(get_config_value("agent", "session_step_budget", default=0))
        max_iters = int(get_config_value("agent", "max_iters", default=30))
        started = runtime.start_async(
            session_id=session_id,
            user_query=user_query,
            model_name=model_name,
            fallback_models=fallback_models,
            approval_callback=auto_approve,
            hook_manager=_hook_manager,
            mode=mode,
            allowed_tools=allowed_tools,
            skill_instructions=skill_instructions,
            cwd=project_cwd,
            max_iters=max_iters,
            session_step_budget=budget,
            source_platform=source_platform,
        )
        if not started:
            return {"message": "Session is already running."}, 409
        return {"session_id": session_id, "accepted": True}, 202


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
                    "value. Use the `ts` of the last event you received."
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
            "while polling; the status fields are always computed from the full "
            "transcript. Prefer the stream endpoint for push delivery."
        ),
    )
    @ns.response(200, "Events plus authoritative session status.", session_events_model)
    @kit.errors(404, descriptions={404: "No session with that id exists."})
    @kit.auth_error()
    def get(self, session_id: str) -> tuple[dict, int]:
        """Poll session events

        Returns the session's event timeline plus authoritative run state:
        `running`, `status`, `done_reason`, `title`, and `recoverable`. Pass
        `after` to fetch only new events while polling; the status fields are
        always computed from the full transcript. Prefer the stream endpoint
        when you want push delivery.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        # Unknown id must 404, not synthesize a phantom idle (#64): without this
        # guard ``load_events`` returns [] and ``summarize_session`` fabricates a
        # placeholder ``{status:"idle", title:"Session <id>"}`` → a false 200.
        if not _session_exists(session_id):
            return _session_not_found(session_id)
        after_ts = request.args.get("after")
        events = runtime.load_events(session_id, after_ts)
        # Opt-in payload cap (#42): the console renders full ``result`` by design,
        # so only a caller (the MCP) that asks via ?truncate=1 gets the smaller
        # transcript — default behaviour is byte-identical.
        if request.args.get("truncate") in ("1", "true"):
            events = _truncate_event_freetext(events)
        notification_service.emit_completion(session_id)
        # Authoritative terminal-state + title for polling consumers (the MCP
        # facade reads these). ``summarize_session`` already computes
        # status/done_reason from the full transcript and resolves the stored
        # title; reuse it rather than recompute. ``after_ts`` only narrows the
        # returned event window, never the status — so summarize the full log.
        summary = runtime.summarize_session(session_id)
        return {
            "session_id": session_id,
            "events": events,
            "running": runtime.is_running(session_id),
            "status": summary["status"],
            "done_reason": summary["done_reason"],
            "title": summary["title"],
            # F2: lets a polling consumer (console/CLI) show a Continue/Restart
            # affordance without re-deriving recoverability from the timeline.
            "recoverable": summary["recoverable"],
        }, 200


@ns.route("/sessions/<string:session_id>/stream")
class SessionStream(Resource):
    r"""Stream session events via Server-Sent Events (push-based, #46).

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

    @staticmethod
    def _stream_events(
        session_id: str,
        session_runtime: SessionRuntime,
        bus: SessionEventBus,
        *,
        heartbeat_s: float = HEARTBEAT_S,
        idle_close_s: float = IDLE_CLOSE_S,
        _sub: Subscription | None = None,
    ) -> Iterator[str]:
        """Yield SSE frames for a session: backlog once, then live + heartbeats.

        Subscribes BEFORE loading the backlog so the queue is a superset of all
        post-subscribe events; the overlap with the backlog (events appended in
        the subscribe↔load race window) is dropped by exact content key.
        ``_sub`` is a test seam for injecting a pre-seeded subscription.
        """
        sub = _sub if _sub is not None else bus.subscribe(session_id)
        try:
            backlog = session_runtime.session_store.load_transcript(session_id)
            backlog_keys = {json.dumps(e, sort_keys=True) for e in backlog}
            for event in backlog:
                yield f"data: {json.dumps(event)}\n\n"

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
                try:
                    event = sub.queue.get(timeout=heartbeat_s)
                except queue.Empty:
                    if not session_runtime.is_running(session_id):
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
        },
        description=(
            "Open a Server-Sent Events stream. The stored backlog is replayed "
            "first, then new events are pushed as they happen (`data: <json>` "
            "frames). Heartbeat comments keep the connection alive and a terminal "
            "`stream_end` frame is sent when the run finishes. EventSource cannot "
            "set headers, so the key may be passed as the `api_key` query param."
        ),
    )
    @api.response(200, "Server-Sent Events stream (`text/event-stream`).")
    @kit.auth_error()
    def get(self, session_id: str) -> Response:
        """Stream session events

        Opens a Server-Sent Events stream. The stored backlog is replayed
        first, then new events are pushed as they happen. Heartbeat comments
        keep the connection alive, and a terminal `stream_end` frame is sent
        when the run finishes. Because EventSource cannot set headers, the API
        key may be passed as the `api_key` query parameter instead.
        """
        auth_error = _require_api_key()
        if auth_error:
            return Response(
                json.dumps(auth_error[0]),
                status=auth_error[1],
                mimetype="application/json",
            )

        bus = get_session_event_bus()
        return Response(
            stream_with_context(self._stream_events(session_id, runtime, bus)),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Access-Control-Allow-Origin": _CORS_ORIGIN,
            },
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
    @kit.errors(400, shape="message", descriptions={400: "The `text` field is missing or empty."})
    @kit.errors(409, shape="message", descriptions={409: "The session could not be re-engaged."})
    @kit.auth_error()
    @ns.expect(session_message_model)
    def post(self, session_id: str) -> tuple[dict, int]:
        """Send a session message

        While a run is active the text is enqueued as a steering message for
        the agent and the call returns 202. On an idle or finished session the
        message re-engages it instead: a fresh run starts with the text as its
        query and the call returns 200 with the new `run_id`. Run ids have the
        form `<session_id>:r<seq>`. Only a terminated session rejects.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        payload = request.get_json(silent=True) or {}
        text = payload.get("text")
        if not text or not isinstance(text, str):
            return {"message": "'text' is required"}, 400
        if runtime.enqueue_message(session_id, text):
            return {"session_id": session_id, "enqueued": True}, 202
        # No active run → re-engage: start a fresh run with this message.
        model = get_config_value("llm", "default_model", default="unknown")
        runtime.append_context_event(session_id, {"model": model})
        # Resolve cwd from session context (honours persisted external cwd) or
        # fall back to the per-session temp dir for sessions without a project.
        session_cwd = _resolve_session_cwd(session_id) or session_temp_dir(session_id)
        budget = int(get_config_value("agent", "session_step_budget", default=0))
        max_iters = int(get_config_value("agent", "max_iters", default=30))
        run_id = runtime.start_async(
            session_id=session_id,
            user_query=text,
            model_name=str(model) or None,
            approval_callback=auto_approve,
            hook_manager=_hook_manager,
            cwd=session_cwd,
            max_iters=max_iters,
            session_step_budget=budget,
            source_platform=_request_surface(),
        )
        if not run_id:
            return {"message": "Session is already running."}, 409
        return {"session_id": session_id, "enqueued": True, "run_id": run_id}, 200


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
    @kit.auth_error()
    def post(self, session_id: str) -> tuple[dict, int]:
        """Interrupt a session

        Stops the currently executing step of an active run and returns 202.
        Interrupting an idle session is an idempotent no-op that returns 200
        with `interrupted` false, so the call is always safe to make.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        ok = runtime.interrupt_step(session_id)
        if not ok:
            return {"session_id": session_id, "interrupted": False}, 200
        return {"session_id": session_id, "interrupted": True}, 202


def _try_wiki_indexing_resume(session_id: str, action: str) -> dict | None:
    """Dispatch to the wiki checkpoint resume when *session_id* is an indexing job.

    A wiki **indexing** session's "Continue" must route to the checkpoint
    :class:`WikiResume` (Gitea #54, Part B) — re-cloning at the recorded commit
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
        if job is None or not WikiResume.is_resumable(job):
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
    def post(self, session_id: str) -> tuple[dict, int]:
        """Recover a session

        Restarts work on a failed or incomplete session. `retry` re-runs the
        last user query, optionally edited via `edited_text`; `continue`
        resumes from where the run stopped. The run inherits the session's
        prior context and settings. Wiki indexing sessions resume from their
        checkpoint instead and return a `job_id` to monitor rather than a
        `run_id`.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        body = request.get_json(silent=True) or {}
        action = body.get("action")
        if action not in ("retry", "continue"):
            return {"message": "'action' must be 'retry' or 'continue'"}, 400
        from_ts: str | None = body.get("from_ts")
        edited_text: str | None = body.get("edited_text")
        model_override: str | None = body.get("model")
        if runtime.is_running(session_id):
            return {"message": "Session is already running."}, 409

        # Origin-aware dispatch (Gitea #54, Part F4): a wiki INDEXING session's
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
        events = runtime.session_store.load_transcript(session_id)
        last_context: dict[str, object] = {}
        for event in reversed(events):
            if event.get("type") == "context":
                payload = event.get("payload")
                if isinstance(payload, dict):
                    last_context = dict(payload)
                break
        mode = _parse_mode(last_context.get("mode"))
        allowed_tools = _extract_allowed_tools(last_context)
        try:
            project_cwd = _resolve_project_cwd({"context": last_context})
        except ValueError as exc:
            return {"message": str(exc)}, 400
        model_name = model_override or str(last_context.get("model", "")) or None
        if model_override:
            runtime.append_context_event(session_id, {"model": model_override})
        # Re-inject capability-gating context (client_capabilities /
        # structured_workspace) so a recovered wiki/QA/structured session keeps
        # its capability — the orchestrator reads the MOST-RECENT context event,
        # and the model-override append above (or the recovery audit) would
        # otherwise leave a gating-less event as the latest one (Gitea #54, F1).
        runtime.reinject_recovery_context(session_id)
        budget = int(get_config_value("agent", "session_step_budget", default=0))
        max_iters = int(get_config_value("agent", "max_iters", default=30))
        run_id = runtime.start_async(
            session_id=session_id,
            user_query=user_query,
            model_name=model_name,
            approval_callback=auto_approve,
            hook_manager=_hook_manager,
            mode=mode,
            allowed_tools=allowed_tools,
            cwd=project_cwd,
            max_iters=max_iters,
            session_step_budget=budget,
            source_platform=_request_surface(),
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
    def post(self, session_id: str) -> tuple[dict, int]:
        """Fork a session

        Copies the transcript into a new session and returns its id. Pass
        `from_ts` to fork from a specific point instead of the full history.
        The fork records its provenance and can apply a new tag or model. A
        running session cannot be forked.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        if runtime.is_running(session_id):
            return {"message": "Cannot fork a running session."}, 409
        body = request.get_json(silent=True) or {}
        from_ts: str | None = body.get("from_ts")
        model: str | None = body.get("model")
        compact: bool = _parse_bool(body.get("compact"))
        tag: str | None = body.get("tag")

        store = runtime.session_store
        try:
            if from_ts:
                new_session_id = store.fork_session_at(session_id, from_ts)
            else:
                new_session_id = store.fork_session(session_id)
        except Exception as exc:
            return {"message": f"Fork failed: {exc}"}, 400

        if tag:
            store.tag_session(new_session_id, tag)
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
                asyncio.run(store.compact_session(new_session_id, mode="partial"))
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
    def post(self, session_id: str) -> tuple[dict, int]:
        """Approve or reject a plan

        Resolves a pending plan-mode proposal. Approval immediately starts a
        new run in act mode that implements the approved plan; rejection
        leaves the session dormant so the user can send refinement guidance
        via the query endpoint. Returns 404 when no proposal is pending.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
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
            # Start a new run in act mode with a synthetic continuation
            started = runtime.start_async(
                session_id=session_id,
                user_query=(
                    "[system] The user approved your plan. Proceed with "
                    "implementation using the full toolset. The approved "
                    "plan is in the conversation history."
                ),
                approval_callback=auto_approve,
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
    def get(self, session_id: str) -> tuple[dict, int] | Response:
        """Fetch the session plan

        Returns the session's current `plan.md` as `text/markdown`. A plan
        file exists only after a plan-mode run has written one; otherwise the
        call returns 404.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
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
    def get(self, session_id: str) -> tuple[dict, int]:
        """Get the agent tree

        Returns the session's sub-agent lifecycle events with status, model,
        and per-agent token counts, plus rollups: `total_steps`,
        `total_input_tokens` (peak context pressure), and
        `total_input_tokens_billed` (cumulative billed input). Use it to
        render a live agent tree alongside the event stream.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
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
                # transcript bloat #42 caps on the events route.
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
        from mewbo_core.token_budget import build_usage_numbers

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
    def get(self, session_id: str) -> tuple[dict, int]:
        """Get token usage

        Returns token usage split between the root agent and sub-agents,
        including peak and billed input figures and compaction statistics.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        from mewbo_core.token_budget import build_usage_numbers

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
    def post(self, session_id: str) -> tuple[dict, int]:
        """Archive a session

        Hides the session from the default session list. Archiving is fully
        reversible with DELETE on the same path.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
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
    def delete(self, session_id: str) -> tuple[dict, int]:
        """Unarchive a session

        Restores an archived session to the default session list.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        if session_id not in runtime.session_store.list_sessions():
            return {"message": "Session not found."}, 404
        runtime.session_store.unarchive_session(session_id)
        return {"session_id": session_id, "archived": False}, 200


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
    def patch(self, session_id: str) -> tuple[dict, int]:
        """Rename a session

        Saves a user-provided display title for the session. Titles are
        trimmed and capped at 120 characters.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
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
    def post(self, session_id: str) -> tuple[dict, int]:
        """Generate a session title

        Asks the configured model to produce a title from the transcript,
        saves it, and appends a `title_update` event to the session. Returns
        422 when no usable title could be generated.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        if session_id not in runtime.session_store.list_sessions():
            return {"message": "Session not found."}, 404
        import asyncio

        from mewbo_core.title_generator import generate_session_title

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
    def post(self, session_id: str) -> tuple[dict, int]:
        """Upload attachments

        Accepts one or more files as `multipart/form-data` under the `files`
        field (a single `file` field also works). Documents are parsed to
        Markdown at upload time so later runs can read them without
        re-parsing. Unsupported file types are rejected, as are image uploads
        when the `model` hint names a model without vision support. Reference
        the returned descriptors in the `attachments` field of a query.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
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
    def post(self, session_id: str) -> tuple[dict, int]:
        """Create a share link

        Mints a share token for the session. Anyone holding the token can
        read the transcript via GET /api/share/{token} without an API key.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
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
    def get(self, session_id: str) -> tuple[dict, int]:
        """Export a session

        Returns the full event transcript and the stored summary in one
        payload, suitable for download or offline analysis.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
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
        # Project-derived path (existing behaviour).
        project_name = payload.get("project")
        if project_name and isinstance(project_name, str):
            try:
                return _resolve_project_cwd({"project": project_name})
            except ValueError:
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
    def get(self) -> tuple[dict, int]:
        """List referenceable files

        Returns the files an `@<ref>` may resolve to: the project's git index
        (tracked + new-but-not-`.gitignore`d) plus any files attached to the
        session. Pass `project` (home composer) or `session` (in-session
        composer). Falls back to a bounded filesystem walk for non-git
        projects.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error

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
    def get(self, session_id: str) -> tuple[dict, int]:
        """Get the session diff

        Returns a unified git diff for the session's bound project. When the
        session has no project, or the project is not a git repository, the
        call still returns 200 with `git_repo` false and a `reason` instead of
        an error.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        if session_id not in runtime.session_store.list_sessions():
            return {"message": "Session not found."}, 404

        scope = request.args.get("scope", "uncommitted")
        if scope not in ("uncommitted", "branch"):
            return {"message": "scope must be 'uncommitted' or 'branch'"}, 400

        cwd = _resolve_session_cwd(session_id)
        if not cwd:
            return {"git_repo": False, "reason": "no_project"}, 200

        # Check git presence
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
    def get(self) -> tuple[dict, int]:
        """List commands

        Returns the server-side slash command registry with each command's
        name, arguments, and render kind, so clients can build command
        palettes without hardcoding the list.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        from mewbo_core.commands import list_commands

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
    def post(self, session_id: str) -> tuple[dict, int]:
        """Run a command

        Executes a server-side slash command such as `compact` against the
        session. Commands that render into the transcript run asynchronously
        like a regular query: the call returns 202 and their output arrives on
        the event stream. Dialog and notification commands execute inline and
        return their result in the response body with 200. Discover available
        commands via GET /api/commands.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error

        import asyncio

        from mewbo_core.commands import (
            COMMANDS,
            CommandContext,
            CommandError,
            CommandRender,
            execute_command,
        )
        from mewbo_core.token_budget import build_usage_numbers

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
    """List notifications."""

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
    def get(self) -> tuple[dict, int]:
        """List notifications

        Returns session lifecycle notifications such as session created,
        completed, or failed. Dismissed entries are hidden unless
        `include_dismissed=true`.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
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
    def post(self) -> tuple[dict, int]:
        """Dismiss notifications

        Marks the given notification ids as dismissed. Accepts either an
        `ids` array or a single `id`. Returns the number dismissed.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
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
    def post(self) -> tuple[dict, int]:
        """Clear notifications

        Deletes dismissed notifications, or every notification when
        `clear_all` is true. Returns the number cleared.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        payload = request.get_json(silent=True) or {}
        clear_all = payload.get("clear_all")
        if isinstance(clear_all, str):
            clear_all = _parse_bool(clear_all)
        else:
            clear_all = bool(clear_all)
        cleared = notification_store.clear(dismissed_only=not clear_all)
        return {"cleared": cleared}, 200


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
            "server it comes from, and a `scope` of `global`, `project`, or "
            "`plugin`. Pass `project` to include tools configured inside that "
            "project. Use the `tool_id` values in a session's `mcp_tools` "
            "allowlist to scope what a run may call."
        ),
    )
    @ns.response(200, "Tool list.", tools_list_model)
    @kit.auth_error()
    def get(self) -> tuple[dict, int]:
        """List tools

        Returns every known tool integration with its enablement state, the
        MCP server it comes from, and a `scope` of `global`, `project`, or
        `plugin`. Pass `project` to include tools configured inside that
        project. Use the `tool_id` values in a session's `mcp_tools` allowlist
        to scope what a run may call.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        project_name = request.args.get("project")
        project_cwd = None
        if project_name:
            projects = get_config().projects
            proj = projects.get(project_name)
            if proj and proj.path:
                project_cwd = proj.path
        # Include plugin MCP servers so they appear in the integrations list
        from mewbo_core.plugins import load_all_plugin_components

        fan_out = load_all_plugin_components()
        registry = load_registry(
            cwd=project_cwd,
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

        def _tool_scope(spec: ToolSpec) -> str:
            if spec.kind != "mcp":
                return "global"
            server = spec.metadata.get("server", "")
            if server in plugin_servers:
                return "plugin"
            if server in global_servers:
                return "global"
            return "project"

        tools = [
            {
                "tool_id": spec.tool_id,
                "name": spec.name,
                "kind": spec.kind,
                "enabled": spec.enabled,
                "description": spec.description,
                "disabled_reason": spec.metadata.get("disabled_reason"),
                "server": spec.metadata.get("server"),
                "scope": _tool_scope(spec),
            }
            for spec in specs
        ]
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
    def get(self) -> tuple[dict, int]:
        """List skills

        Returns the available skills, including those contributed by installed
        plugins, with their descriptions, tool allowlists, and invocation
        flags. Activate a skill for a run via the `skill` field of a query.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        from mewbo_core.skills import SkillRegistry

        project_name = request.args.get("project")
        project_cwd = None
        if project_name:
            projects = get_config().projects
            proj = projects.get(project_name)
            if proj and proj.path:
                project_cwd = proj.path
        registry = SkillRegistry()
        registry.load(project_cwd)

        # Include plugin skills/commands so they appear in the skills list
        from mewbo_core.plugins import load_all_plugin_components

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
    """Legacy sync endpoint (CLI compatibility)."""

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
    @kit.errors(
        400,
        shape="message",
        descriptions={400: "The `query` field is missing, or the named project is invalid."},
    )
    @kit.auth_error()
    @ns.expect(sync_query_model)
    def post(self) -> tuple[dict, int]:
        """Run a synchronous query

        Runs the query to completion and returns the full result in one
        response, including the executed action steps. POST /api/query remains
        supported as a simple synchronous alternative for CLI-style clients;
        prefer the asynchronous session endpoints for interactive use. A new
        session is created automatically unless `session_id`, `session_tag`,
        or `fork_from` selects an existing one.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        request_data = request.get_json(silent=True) or {}
        user_query = request_data.get("query")
        if not user_query:
            return {"message": "Invalid input: 'query' is required"}, 400
        mode = _parse_mode(request_data.get("mode"))
        existing_sessions = set(runtime.session_store.list_sessions())
        session_id = runtime.resolve_session(
            session_id=request_data.get("session_id"),
            session_tag=request_data.get("session_tag"),
            fork_from=request_data.get("fork_from"),
        )
        if session_id not in existing_sessions:
            notification_service.emit_session_created(session_id)
        context_payload = _build_context_payload(request_data)
        if context_payload:
            runtime.append_context_event(session_id, context_payload)

        allowed_tools = _extract_allowed_tools(context_payload)

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
            approval_callback=auto_approve,
            mode=mode,
            allowed_tools=allowed_tools,
            cwd=project_cwd,
            source_platform=_request_surface(),
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
    def get(self) -> tuple[dict, int]:
        """Get the configuration schema

        Returns the JSON Schema describing the application configuration.
        Protected fields are stripped entirely and secret fields are marked
        `writeOnly`, so the schema can drive a settings UI directly.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        return ConfigSchemaView.from_model().public_schema(), 200


@ns.route("/config")
class ConfigResource(Resource):
    """Read and update the application configuration."""

    @api.doc(
        security="apikey",
        description=(
            "Return the current configuration with protected and secret values "
            "stripped, plus a `secrets` map reporting which secret fields are set "
            "(true/false) without revealing their values. Update with PATCH on "
            "the same path."
        ),
    )
    @ns.response(200, "Configuration values and secret status map.", config_response_model)
    @kit.auth_error()
    def get(self) -> tuple[dict, int]:
        """Get configuration

        Returns the current configuration with protected and secret values
        stripped, plus a `secrets` map reporting which secret fields are set
        (true or false) without revealing their values.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        view = ConfigSchemaView.from_model()
        data = get_config().model_dump()
        secrets = view.secret_status(data)
        return {"config": view.strip_values(data), "secrets": secrets}, 200

    @api.doc(
        security="apikey",
        description=(
            "Deep-merge the request body into the stored configuration, validate "
            "the result, and persist it. Attempts to modify protected fields are "
            "rejected with 403. A merge that fails validation returns 422 with the "
            "validation errors and changes nothing. Mirrors the shape served by "
            "GET /api/config/schema."
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
    @kit.auth_error()
    @ns.expect(config_patch_model)
    def patch(self) -> tuple[dict, int]:
        """Update configuration

        Deep-merges the request body into the stored configuration, validates
        the result, and persists it. Attempts to modify protected fields are
        rejected with 403. A merge that fails validation returns 422 with the
        validation errors and changes nothing.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        patch = request.get_json(silent=True) or {}
        if not patch:
            return {"message": "Empty payload"}, 400

        view = ConfigSchemaView.from_model()
        violations = view.reject_protected(patch)
        if violations:
            return {"message": f"Cannot modify protected fields: {violations}"}, 403

        config_path = get_app_config_path()
        raw = _load_json(config_path)
        merged = _deep_merge(dict(raw), patch)
        try:
            validated = AppConfig.model_validate(merged)
        except ValidationError as exc:
            return {"message": "Validation failed", "errors": exc.errors()}, 422
        validated.write(config_path)
        reset_config()

        data = validated.model_dump()
        secrets = view.secret_status(data)
        return {"config": view.strip_values(data), "secrets": secrets}, 200


@ns.route("/plugins")
class PluginList(Resource):
    """List installed plugins and their components."""

    @api.doc(
        security="apikey",
        description=(
            "List each installed plugin with its version, source marketplace, "
            "scope, and component counts (skills, agents, commands, MCP servers, "
            "hooks). Browse installable plugins via GET /api/plugins/marketplace."
        ),
    )
    @ns.response(200, "Installed plugin list.", plugins_list_model)
    @kit.auth_error()
    def get(self) -> tuple[dict, int]:
        """List installed plugins

        Returns each installed plugin with its version, source marketplace,
        scope, and component counts: skills, agents, commands, MCP servers,
        and hooks.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        from mewbo_core.config import get_config
        from mewbo_core.plugins import discover_installed_plugins

        cfg = get_config().plugins
        plugins = discover_installed_plugins(registry_paths=cfg.resolve_registry_paths())
        return {
            "plugins": [
                {
                    "name": pc.manifest.name if pc.manifest else "unknown",
                    "description": pc.manifest.description if pc.manifest else "",
                    "version": pc.manifest.version if pc.manifest else "",
                    "marketplace": pc.manifest.marketplace if pc.manifest else "",
                    "scope": pc.manifest.scope if pc.manifest else "user",
                    "skills": len(pc.skill_dirs),
                    "agents": len(pc.agent_files),
                    "commands": len(pc.command_files),
                    "mcp_servers": len(pc.mcp_config or {}),
                    "has_hooks": pc.hooks_config is not None,
                }
                for pc in plugins
                if pc.manifest is not None
            ]
        }, 200


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
    def get(self) -> tuple[dict, int]:
        """List marketplace plugins

        Returns the plugins available for installation from the configured
        marketplaces. Install one with POST on this same path.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        from mewbo_core.config import get_config
        from mewbo_core.plugins import discover_marketplace_plugins

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
    def post(self) -> tuple[dict, int]:
        """Install a plugin

        Installs the named plugin from a configured marketplace. Its skills,
        commands, agents, and MCP servers become available to sessions started
        after installation.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        data = request.get_json(silent=True) or {}
        name = data.get("name")
        marketplace = data.get("marketplace")
        if not name or not marketplace:
            return {"error": "name and marketplace required"}, 400

        from mewbo_core.config import get_config
        from mewbo_core.plugins import install_plugin

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
    def delete(self, plugin_name: str) -> tuple[dict, int]:
        """Uninstall a plugin

        Removes an installed plugin and its components from the install
        directory.
        """
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        from mewbo_core.config import get_config
        from mewbo_core.plugins import uninstall_plugin

        cfg = get_config().plugins
        if uninstall_plugin(plugin_name, install_base=cfg.resolve_install_dir()):
            return {"uninstalled": plugin_name}, 200
        return {"error": "Plugin not found"}, 404


def main() -> None:
    """Run the Mewbo API server."""
    app.run(debug=True, host="0.0.0.0", port=5124)


if __name__ == "__main__":
    main()
