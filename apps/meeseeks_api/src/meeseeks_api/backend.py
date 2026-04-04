#!/usr/bin/env python3
"""Meeseeks API.

Single-user REST API with session-based orchestration and event polling.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone

from flask import Flask, Response, request, stream_with_context
from flask_restx import Api, Resource, fields
from meeseeks_core.classes import TaskQueue
from meeseeks_core.common import get_logger
from meeseeks_core.config import (
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
from meeseeks_core.notifications import NotificationStore
from meeseeks_core.permissions import auto_approve
from meeseeks_core.session_runtime import SessionRuntime, parse_core_command
from meeseeks_core.session_store import SessionStoreBase, create_session_store
from meeseeks_core.share_store import ShareStore
from meeseeks_core.tool_registry import load_registry
from pydantic import ValidationError
from werkzeug.utils import secure_filename


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

    def emit_session_created(self, session_id: str) -> None:
        """Append a session-created event and notify."""
        self._session_store.append_event(
            session_id,
            {"type": "session", "payload": {"event": "created"}},
        )
        self.notify(
            title="Session created",
            message=f"Session {session_id} created.",
            session_id=session_id,
            event_type="created",
        )

    def emit_started(self, session_id: str) -> None:
        """Notify that a session started running."""
        self.notify(
            title="Session started",
            message=f"Session {session_id} started.",
            session_id=session_id,
            event_type="started",
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
        done = bool(payload.get("done"))
        done_reason = str(payload.get("done_reason") or "")
        if done and done_reason.lower() == "completed":
            self.notify(
                title="Session completed",
                message=f"Session {session_id} completed.",
                session_id=session_id,
                event_type="completed",
                metadata={"completion_ts": completion_ts, "done_reason": done_reason},
            )
            return
        self.notify(
            title="Session finished",
            message=f"Session {session_id} finished with status '{done_reason}'.",
            level="warning",
            session_id=session_id,
            event_type="failed",
            metadata={"completion_ts": completion_ts, "done_reason": done_reason},
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
logging = get_logger(name="meeseeks-api")
logging.info("Starting Meeseeks API server.")
logging.debug("Starting API server with API token: {}", MASTER_API_TOKEN)

_config = get_config()
if _config.runtime.preflight_enabled:
    start_preflight(_config)


# Create Flask application
app = Flask(__name__)
session_store = create_session_store()
runtime = SessionRuntime(session_store=session_store)
notification_store = NotificationStore(root_dir=session_store.root_dir)
share_store = ShareStore(root_dir=session_store.root_dir)

authorizations = {"apikey": {"type": "apiKey", "in": "header", "name": "X-API-KEY"}}
VERSION = get_version()
api = Api(
    app,
    version=VERSION,
    title="Meeseeks API",
    description="Interact with Meeseeks through a REST API",
    doc="/swagger-ui/",
    authorizations=authorizations,
    security="apikey",
)

ns = api.namespace("api", description="Meeseeks operations")

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
    """Merge context and attachments into a single payload."""
    payload: dict[str, object] = {}
    context = request_data.get("context")
    if isinstance(context, dict):
        payload.update(context)
    attachments = request_data.get("attachments")
    if isinstance(attachments, list):
        payload["attachments"] = attachments
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
    """Resolve a project name from the request to its filesystem path.

    Returns the project path as ``cwd``, or ``None`` if no project is specified.
    Raises ``ValueError`` when a project name is given but not configured.
    """
    project_name = request_data.get("project")
    if not project_name or not isinstance(project_name, str):
        # Also check inside context payload
        ctx = request_data.get("context")
        if isinstance(ctx, dict):
            project_name = ctx.get("project")
    if not project_name or not isinstance(project_name, str):
        return None
    project_name = project_name.strip()
    if not project_name:
        return None
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
    from meeseeks_core.skills import SkillRegistry, activate_skill

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
    """List configured projects."""

    @api.doc(security="apikey")
    def get(self) -> tuple[dict, int]:
        """Return configured projects for the UI."""
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        projects = get_config().projects
        result = [
            {
                "name": name,
                "path": cfg.path,
                "description": cfg.description,
                "available": os.path.isdir(cfg.path),
            }
            for name, cfg in projects.items()
            if cfg.path
        ]
        return {"projects": result}, 200


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
            context_payload["model"] = get_config_value(
                "llm", "default_model", default="unknown"
            )
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
        # Use model from context if provided, else config default
        if "model" not in context_payload:
            context_payload["model"] = get_config_value(
                "llm", "default_model", default="unknown"
            )
        if context_payload:
            runtime.append_context_event(session_id, context_payload)

        mode = _parse_mode(request_data.get("mode"))

        allowed_tools = _extract_allowed_tools(context_payload)

        # Skill activation: resolve from top-level "skill" field or context.skill.
        skill_instructions = _resolve_skill_instructions(request_data, user_query, context_payload)

        # Resolve project → cwd
        try:
            project_cwd = _resolve_project_cwd(request_data)
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
            mode=mode,
            allowed_tools=allowed_tools,
            skill_instructions=skill_instructions,
            cwd=project_cwd,
            max_iters=max_iters,
            session_step_budget=budget,
        )
        if not started:
            return {"message": "Session is already running."}, 409
        notification_service.emit_started(session_id)
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
                "ts": e.get("ts"),
            }
            for e in events
            if e.get("type") == "sub_agent"
        ]
        running = runtime.is_running(session_id)
        return {
            "agents": agents,
            "running": running,
            "total_steps": total_steps,
        }, 200


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
        registry = load_registry(cwd=project_cwd)
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

        tools = [
            {
                "tool_id": spec.tool_id,
                "name": spec.name,
                "kind": spec.kind,
                "enabled": spec.enabled,
                "description": spec.description,
                "disabled_reason": spec.metadata.get("disabled_reason"),
                "server": spec.metadata.get("server"),
                "scope": (
                    "global"
                    if spec.kind != "mcp" or spec.metadata.get("server") in global_servers
                    else "project"
                ),
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
        from meeseeks_core.skills import SkillRegistry

        project_name = request.args.get("project")
        project_cwd = None
        if project_name:
            projects = get_config().projects
            proj = projects.get(project_name)
            if proj and proj.path:
                project_cwd = proj.path
        registry = SkillRegistry()
        registry.load(project_cwd)
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
class MeeseeksQuery(Resource):
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
        notification_service.emit_started(session_id)

        allowed_tools = _extract_allowed_tools(context_payload)

        # Resolve project → cwd
        try:
            project_cwd = _resolve_project_cwd(request_data)
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
                protected |= _collect_protected_paths(
                    defs[ref_name], defs=defs, prefix=path
                )
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


def main() -> None:
    """Run the Meeseeks API server."""
    app.run(debug=True, host="0.0.0.0", port=5124)


if __name__ == "__main__":
    main()
