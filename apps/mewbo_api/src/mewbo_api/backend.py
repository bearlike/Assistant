#!/usr/bin/env python3
"""Mewbo API.

Single-user REST API with session-based orchestration and event polling.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone

from flask import Flask, Response, request, stream_with_context
from flask_restx import Api, Resource, fields
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
from mewbo_core.exit_plan_mode import PLAN_DIR_ROOT, plan_file_for, session_temp_dir
from mewbo_core.notifications import NotificationStore
from mewbo_core.permissions import auto_approve
from mewbo_core.project_store import VirtualProject, create_project_store
from mewbo_core.session_runtime import SessionRuntime, parse_core_command
from mewbo_core.session_store import SessionStoreBase, create_session_store
from mewbo_core.share_store import ShareStore
from mewbo_core.tool_registry import ToolSpec, load_registry
from pydantic import ValidationError
from werkzeug.utils import secure_filename

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
logging.debug("Starting API server with API token: {}", MASTER_API_TOKEN)

_config = get_config()
if _config.runtime.preflight_enabled:
    start_preflight(_config)

# Load hooks from config so API sessions run with configured hooks
from mewbo_core.hooks import HookManager as _HookManager  # noqa: E402

_hook_manager = _HookManager.load_from_config(_config.hooks)

# Create Flask application
app = Flask(__name__)
session_store = create_session_store()
project_store = create_project_store()
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
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
    return response


def _require_api_key() -> tuple[dict, int] | None:
    """Validate the API key header (or query param for SSE) for protected routes."""
    api_token = request.headers.get("X-API-Key") or request.args.get("api_key")
    if api_token is None:
        return {"message": "API token is not provided."}, 401
    if api_token != MASTER_API_TOKEN:
        logging.warning("Unauthorized API call attempt with token: {}", api_token)
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


# -- Agentic Search namespace (mock) --------------------------------------
# In-memory mock backend for the Agentic Search console page. Always on;
# the real implementation will swap the body of ``store.py`` without
# changing the wire shape.
from mewbo_api.agentic_search import init_agentic_search  # noqa: E402

init_agentic_search(api, _require_api_key)
logging.info("agentic_search namespace registered at /api")


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


def _extract_allowed_tools(context_payload: dict[str, object]) -> list[str] | None:
    """Extract MCP tool allowlist from context payload, if present."""
    if not context_payload:
        return None
    mcp_tools = context_payload.get("mcp_tools")
    if isinstance(mcp_tools, list) and mcp_tools:
        return [str(t) for t in mcp_tools if t]
    return None


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


@ns.route("/models")
class Models(Resource):
    """List available LLM models."""

    @api.doc(security="apikey")
    def get(self) -> tuple[dict, int]:
        """Return available models from the LiteLLM proxy."""
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        default_model = get_config_value("llm", "default_model", default="unknown")
        try:
            models = get_config().llm.list_models()
        except ValueError:
            return {
                "models": [default_model] if default_model != "unknown" else [],
                "default": default_model,
            }, 200
        return {"models": models, "default": default_model}, 200


@ns.route("/projects")
class Projects(Resource):
    """List all projects (config-defined + managed)."""

    @api.doc(security="apikey")
    def get(self) -> tuple[dict, int]:
        """Return unified project list for the UI."""
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        # Config-defined projects
        result: list[dict] = [
            {
                "name": name,
                "path": cfg.path,
                "description": cfg.description,
                "available": os.path.isdir(cfg.path),
                "source": "config",
            }
            for name, cfg in get_config().projects.items()
            if cfg.path
        ]
        # Managed (virtual) projects
        for vp in project_store.list_projects():
            result.append(
                {
                    "name": vp.name,
                    "project_id": vp.project_id,
                    "path": vp.path,
                    "description": vp.description,
                    "available": os.path.isdir(vp.path),
                    "source": "managed",
                }
            )
        return {"projects": result}, 200


def _vproject_to_dict(p: VirtualProject) -> dict:
    return {
        "project_id": p.project_id,
        "name": p.name,
        "description": p.description,
        "path": p.path,
        "path_source": p.path_source,
        "folder_created": p.folder_created,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


@ns.route("/v_projects")
class VirtualProjects(Resource):
    """Create managed projects."""

    @api.doc(security="apikey")
    def post(self) -> tuple[dict, int]:
        """Create a new managed project."""
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

    @api.doc(security="apikey")
    def get(self, project_id: str) -> tuple[dict, int]:
        """Get a virtual project by ID."""
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        proj = project_store.get_project(project_id)
        if proj is None:
            return {"message": f"Project '{project_id}' not found"}, 404
        return _vproject_to_dict(proj), 200

    @api.doc(security="apikey")
    def patch(self, project_id: str) -> tuple[dict, int]:
        """Update name or description of a virtual project."""
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

    @api.doc(security="apikey")
    def delete(self, project_id: str) -> tuple[dict, int]:
        """Delete a virtual project."""
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        proj = project_store.get_project(project_id)
        if proj is None:
            return {"message": f"Project '{project_id}' not found"}, 404
        project_store.delete_project(project_id)
        return {}, 204


@ns.route("/sessions")
class Sessions(Resource):
    """List and create sessions."""

    @api.doc(security="apikey")
    def get(self) -> tuple[dict, int]:
        """List sessions for the single user."""
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        include_archived = _parse_bool(request.args.get("include_archived"))
        sessions = runtime.list_sessions(include_archived=include_archived)
        return {"sessions": sessions}, 200

    @api.doc(security="apikey")
    def post(self) -> tuple[dict, int]:
        """Create a new session."""
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
        # Include project in context if provided
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
        if "model" not in context_payload:
            context_payload["model"] = get_config_value("llm", "default_model", default="unknown")
        if context_payload:
            runtime.append_context_event(session_id, context_payload)
        return {"session_id": session_id}, 200


@ns.route("/sessions/<string:session_id>/query")
class SessionQuery(Resource):
    """Enqueue a query or process slash commands for a session."""

    @api.doc(security="apikey")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Handle a session query."""
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
        # Use model from context if provided, else config default
        if "model" not in context_payload:
            context_payload["model"] = get_config_value("llm", "default_model", default="unknown")
        if context_payload:
            runtime.append_context_event(session_id, context_payload)

        mode = _parse_mode(request_data.get("mode"))

        allowed_tools = _extract_allowed_tools(context_payload)

        # Skill activation: resolve from top-level "skill" field or context.skill.
        skill_instructions = _resolve_skill_instructions(request_data, user_query, context_payload)

        # Resolve project → cwd, falling back to a per-session temp dir
        try:
            project_cwd = _resolve_project_cwd(request_data) or session_temp_dir(session_id)
        except ValueError as exc:
            return {"message": str(exc)}, 400

        # Extract model for orchestration (may differ from config default)
        model_name = str(context_payload.get("model", "")) or None

        budget = int(get_config_value("agent", "session_step_budget", default=0))
        max_iters = int(get_config_value("agent", "max_iters", default=30))
        started = runtime.start_async(
            session_id=session_id,
            user_query=user_query,
            model_name=model_name,
            approval_callback=auto_approve,
            hook_manager=_hook_manager,
            mode=mode,
            allowed_tools=allowed_tools,
            skill_instructions=skill_instructions,
            cwd=project_cwd,
            max_iters=max_iters,
            session_step_budget=budget,
        )
        if not started:
            return {"message": "Session is already running."}, 409
        return {"session_id": session_id, "accepted": True}, 202


@ns.route("/sessions/<string:session_id>/events")
class SessionEvents(Resource):
    """Return session events for polling."""

    @api.doc(security="apikey")
    def get(self, session_id: str) -> tuple[dict, int]:
        """Return events for the session."""
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        after_ts = request.args.get("after")
        events = runtime.load_events(session_id, after_ts)
        notification_service.emit_completion(session_id)
        return {
            "session_id": session_id,
            "events": events,
            "running": runtime.is_running(session_id),
        }, 200


@ns.route("/sessions/<string:session_id>/stream")
class SessionStream(Resource):
    """Stream session events via Server-Sent Events."""

    @api.doc(security="apikey")
    def get(self, session_id: str) -> Response:
        """Open an SSE stream for real-time session events."""
        auth_error = _require_api_key()
        if auth_error:
            return Response(
                json.dumps(auth_error[0]),
                status=auth_error[1],
                mimetype="application/json",
            )

        def generate():
            last_count = 0
            idle_cycles = 0
            while True:
                events = runtime.session_store.load_transcript(session_id)
                new_events = events[last_count:]
                if new_events:
                    for event in new_events:
                        yield f"data: {json.dumps(event)}\n\n"
                    last_count = len(events)
                    idle_cycles = 0
                else:
                    idle_cycles += 1

                running = runtime.is_running(session_id)
                if not running and not new_events:
                    yield 'data: {"type": "stream_end"}\n\n'
                    break
                # Auto-close after 5 min of inactivity
                if idle_cycles > 600:
                    break
                time.sleep(0.5)

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Access-Control-Allow-Origin": _CORS_ORIGIN,
            },
        )


@ns.route("/sessions/<string:session_id>/message")
class SessionMessage(Resource):
    """Enqueue a user steering message into a running session."""

    @api.doc(security="apikey")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Send a message to a running session."""
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        payload = request.get_json(silent=True) or {}
        text = payload.get("text")
        if not text or not isinstance(text, str):
            return {"message": "'text' is required"}, 400
        ok = runtime.enqueue_message(session_id, text)
        if not ok:
            return {"message": "No active run for this session."}, 404
        return {"session_id": session_id, "enqueued": True}, 202


@ns.route("/sessions/<string:session_id>/interrupt")
class SessionInterrupt(Resource):
    """Interrupt the current step of a running session."""

    @api.doc(security="apikey")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Interrupt the current step of a running session."""
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        ok = runtime.interrupt_step(session_id)
        if not ok:
            return {"message": "No active run for this session."}, 404
        return {"session_id": session_id, "interrupted": True}, 202


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

    @api.doc(security="apikey")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Trigger a retry/continue recovery for a completed/failed session."""
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
        budget = int(get_config_value("agent", "session_step_budget", default=0))
        max_iters = int(get_config_value("agent", "max_iters", default=30))
        started = runtime.start_async(
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
        )
        if not started:
            return {"message": "Session is already running."}, 409
        return {
            "session_id": session_id,
            "action": action,
            "accepted": True,
        }, 202


@ns.route("/sessions/<string:session_id>/fork")
class SessionFork(Resource):
    """Fork a session, optionally from a specific message timestamp."""

    @api.doc(security="apikey")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Create a new session by forking from a point in this session."""
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

    @api.doc(security="apikey")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Signal plan approval or rejection (binary)."""
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

    @api.doc(security="apikey")
    def get(self, session_id: str) -> tuple[dict, int] | Response:
        """Return plan.md content, or 404 if absent."""
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

    @api.doc(security="apikey")
    def get(self, session_id: str) -> tuple[dict, int]:
        """Return sub-agent events for the session."""
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
                "detail": e.get("payload", {}).get("detail"),
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
        stop_agents = [a for a in agents if a.get("action") == "stop"]
        total_input_tokens = sum(a.get("input_tokens", 0) for a in stop_agents)
        total_output_tokens = sum(a.get("output_tokens", 0) for a in stop_agents)
        return {
            "agents": agents,
            "running": running,
            "total_steps": total_steps,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
        }, 200


@ns.route("/sessions/<string:session_id>/usage")
class SessionUsage(Resource):
    """Return token usage broken down by root agent vs sub-agents."""

    @api.doc(security="apikey")
    def get(self, session_id: str) -> tuple[dict, int]:
        """Return root/sub-agent token usage + compaction stats."""
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

    @api.doc(security="apikey")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Archive a session."""
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        if session_id not in runtime.session_store.list_sessions():
            return {"message": "Session not found."}, 404
        runtime.session_store.archive_session(session_id)
        return {"session_id": session_id, "archived": True}, 200

    @api.doc(security="apikey")
    def delete(self, session_id: str) -> tuple[dict, int]:
        """Unarchive a session."""
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

    @api.doc(security="apikey")
    def patch(self, session_id: str) -> tuple[dict, int]:
        """Persist a user-edited title for a session."""
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

    @api.doc(security="apikey")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Regenerate session title using AI."""
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

    @api.doc(security="apikey")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Upload one or more files for a session."""
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
            saved.append(
                {
                    "id": attachment_id,
                    "filename": item.filename,
                    "stored_name": stored_name,
                    "content_type": item.mimetype,
                    "size_bytes": size_bytes,
                    "uploaded_at": _utc_now(),
                }
            )
        if not saved:
            return {"message": "No valid files uploaded."}, 400
        return {"attachments": saved}, 200


@ns.route("/sessions/<string:session_id>/share")
class SessionShare(Resource):
    """Create a share token for a session."""

    @api.doc(security="apikey")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Create a share token for the session."""
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

    @api.doc(security="apikey")
    def get(self, session_id: str) -> tuple[dict, int]:
        """Return transcript and summary for a session."""
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
    """Return the project filesystem path for a session, or None if unresolvable."""
    events = runtime.session_store.load_transcript(session_id)
    # Walk backwards to find the most recent context event that names a project.
    project_name: str | None = None
    for event in reversed(events):
        if event.get("type") == "context":
            payload = event.get("payload", {})
            if isinstance(payload, dict) and payload.get("project"):
                project_name = str(payload["project"])
                break
    if not project_name:
        return None
    try:
        return _resolve_project_cwd({"project": project_name})
    except ValueError:
        return None


@ns.route("/sessions/<string:session_id>/git-diff")
class SessionGitDiff(Resource):
    """Read-only git diff for a session's project."""

    @api.doc(security="apikey")
    def get(self, session_id: str) -> tuple[dict, int]:
        """Return a unified diff for the session's project (scope: uncommitted|branch)."""
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

    def get(self, token: str) -> tuple[dict, int]:
        """Return transcript and summary for a share token."""
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

    @api.doc(security="apikey")
    def get(self) -> tuple[dict, int]:
        """Return the command registry metadata."""
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        from mewbo_core.commands import list_commands

        return {"commands": list_commands()}, 200


@ns.route("/sessions/<string:session_id>/command")
class SessionCommand(Resource):
    """Execute a server-side command against a session."""

    @api.doc(security="apikey")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Dispatch a slash command.

        TRANSCRIPT-render commands (``/compact`` and the like) run in the
        same ``RunRegistry`` thread regular queries use, so:

        - the user-bubble event is written **before** any work begins
        - ``is_running()`` flips true for the duration, driving the FE's
          events polling and run indicator off authoritative server state
          (survives refresh, multi-tab safe, no browser-side patching)
        - the handler's own events (e.g. ``context_compacted``) stream
          into the transcript live instead of arriving in a single burst

        DIALOG and NOTIFICATION commands stay synchronous: they're cheap,
        produce no transcript, and the response body feeds the dialog or
        notification balloon directly.
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

    @api.doc(security="apikey")
    def get(self) -> tuple[dict, int]:
        """Return notifications for the UI."""
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

    @api.doc(security="apikey")
    def post(self) -> tuple[dict, int]:
        """Dismiss a notification or list of notifications."""
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

    @api.doc(security="apikey")
    def post(self) -> tuple[dict, int]:
        """Clear dismissed notifications (or all when clear_all is true)."""
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

    @api.doc(security="apikey")
    def get(self) -> tuple[dict, int]:
        """Return tool specs for the UI."""
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

    @api.doc(security="apikey")
    def get(self) -> tuple[dict, int]:
        """Return skill specs for the UI."""
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

    @api.doc(security="apikey")
    @api.expect(
        api.model(
            "Query",
            {
                "query": fields.String(required=True, description="The user query"),
                "session_id": fields.String(required=False, description="Existing session id"),
                "session_tag": fields.String(required=False, description="Human-friendly tag"),
                "fork_from": fields.String(required=False, description="Session id or tag to fork"),
                "mode": fields.String(
                    required=False,
                    description="Optional orchestration mode (plan or act)",
                ),
            },
        )
    )
    @api.response(200, "Success", task_queue_model)
    @api.response(400, "Invalid input")
    @api.response(401, "Unauthorized")
    def post(self) -> tuple[dict, int]:
        """Process a synchronous query (legacy)."""
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

        logging.info("Received user query: {}", user_query)
        task_queue: TaskQueue = runtime.run_sync(
            user_query=user_query,
            session_id=session_id,
            approval_callback=auto_approve,
            mode=mode,
            allowed_tools=allowed_tools,
            cwd=project_cwd,
        )
        notification_service.emit_completion(session_id)
        task_result = deepcopy(task_queue.task_result)
        to_return = task_queue.dict()
        to_return["task_result"] = task_result
        logging.info("Returning executed action plan.")
        to_return["session_id"] = session_id
        return to_return, 200


# ---------------------------------------------------------------------------
# Config API helpers
# ---------------------------------------------------------------------------

_PROTECTED_KEY = "x-protected"


def _collect_protected_paths(
    schema: dict,
    *,
    defs: dict | None = None,
    prefix: str = "",
) -> set[str]:
    """Return dot-separated paths of all x-protected fields in *schema*."""
    if defs is None:
        defs = schema.get("$defs", {})
    protected: set[str] = set()
    props = schema.get("properties", {})
    for name, prop in props.items():
        path = f"{prefix}{name}" if not prefix else f"{prefix}.{name}"
        if prop.get(_PROTECTED_KEY):
            protected.add(path)
        # Recurse into $ref
        ref = prop.get("$ref")
        if ref:
            ref_name = ref.rsplit("/", 1)[-1]
            if ref_name in defs:
                protected |= _collect_protected_paths(defs[ref_name], defs=defs, prefix=path)
        # Recurse into inline objects
        if prop.get("type") == "object" and "properties" in prop:
            protected |= _collect_protected_paths(prop, defs=defs, prefix=path)
    return protected


def _strip_protected_from_schema(schema: dict) -> dict:
    """Return a copy of *schema* with x-protected properties removed."""
    schema = json.loads(json.dumps(schema))  # deep copy
    defs = schema.get("$defs", {})
    for def_schema in defs.values():
        props = def_schema.get("properties")
        if not props:
            continue
        to_remove = [k for k, v in props.items() if v.get(_PROTECTED_KEY)]
        for k in to_remove:
            del props[k]
        req = def_schema.get("required")
        if req:
            def_schema["required"] = [r for r in req if r not in to_remove]
    return schema


def _strip_protected_values(data: dict, protected_paths: set[str], prefix: str = "") -> None:
    """Remove protected keys from a config dict **in-place**."""
    for key in list(data.keys()):
        path = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        if path in protected_paths:
            del data[key]
        elif isinstance(data[key], dict):
            _strip_protected_values(data[key], protected_paths, path)


def _find_protected_in_patch(patch: dict, protected_paths: set[str], prefix: str = "") -> list[str]:
    """Return protected dot-paths found in *patch*."""
    violations: list[str] = []
    for key, value in patch.items():
        path = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        if path in protected_paths:
            violations.append(path)
        elif isinstance(value, dict):
            violations.extend(_find_protected_in_patch(value, protected_paths, path))
    return violations


@ns.route("/config/schema")
class ConfigSchemaResource(Resource):
    """Serve the JSON Schema for AppConfig (protected fields stripped)."""

    @api.doc(security="apikey", description="Get the AppConfig JSON Schema.")
    def get(self) -> tuple[dict, int]:
        """Return the JSON Schema with protected fields stripped."""
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        schema = AppConfig.model_json_schema()
        return _strip_protected_from_schema(schema), 200


@ns.route("/config")
class ConfigResource(Resource):
    """Read and update the application configuration."""

    @api.doc(security="apikey", description="Get current configuration values.")
    def get(self) -> tuple[dict, int]:
        """Return current config values with protected fields omitted."""
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        config = get_config()
        data = config.model_dump()
        schema = AppConfig.model_json_schema()
        protected = _collect_protected_paths(schema)
        _strip_protected_values(data, protected)
        return {"config": data}, 200

    @api.doc(security="apikey", description="Partially update configuration.")
    def patch(self) -> tuple[dict, int]:
        """Apply a partial config update, validate, and persist."""
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        patch = request.get_json(silent=True) or {}
        if not patch:
            return {"message": "Empty payload"}, 400

        schema = AppConfig.model_json_schema()
        protected = _collect_protected_paths(schema)
        violations = _find_protected_in_patch(patch, protected)
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
        _strip_protected_values(data, protected)
        return {"config": data}, 200


@ns.route("/plugins")
class PluginList(Resource):
    """List installed plugins and their components."""

    @api.doc(security="apikey")
    def get(self) -> tuple[dict, int]:
        """List installed plugins and their components."""
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

    @api.doc(security="apikey")
    def get(self) -> tuple[dict, int]:
        """List available plugins from configured marketplaces."""
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        from mewbo_core.config import get_config
        from mewbo_core.plugins import discover_marketplace_plugins

        cfg = get_config().plugins
        return {
            "plugins": discover_marketplace_plugins(marketplace_dirs=cfg.resolve_marketplace_dirs())
        }, 200

    @api.doc(security="apikey")
    def post(self) -> tuple[dict, int]:
        """Install a plugin from a marketplace."""
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

    @api.doc(security="apikey")
    def delete(self, plugin_name: str) -> tuple[dict, int]:
        """Uninstall a plugin."""
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
