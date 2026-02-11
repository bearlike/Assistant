#!/usr/bin/env python3
"""Meeseeks API.

Single-user REST API with session-based orchestration and event polling.
"""

from __future__ import annotations

import os
import uuid
from copy import deepcopy
from datetime import datetime, timezone

from flask import Flask, request
from flask_restx import Api, Resource, fields
from meeseeks_core.classes import TaskQueue
from meeseeks_core.common import get_logger
from meeseeks_core.config import get_config, get_config_value, start_preflight
from meeseeks_core.notifications import NotificationStore
from meeseeks_core.permissions import auto_approve
from meeseeks_core.session_runtime import SessionRuntime, parse_core_command
from meeseeks_core.session_store import SessionStore
from meeseeks_core.share_store import ShareStore
from meeseeks_core.tool_registry import load_registry
from werkzeug.utils import secure_filename


class NotificationService:
    """Emit session lifecycle notifications for the API."""

    def __init__(self, store: NotificationStore, session_store: SessionStore) -> None:
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
MASTER_API_TOKEN = get_config_value("api", "master_token", default="msk-strong-password")

# Initialize logger
logging = get_logger(name="meeseeks-api")
logging.info("Starting Meeseeks API server.")
logging.debug("Starting API server with API token: {}", MASTER_API_TOKEN)

_config = get_config()
if _config.runtime.preflight_enabled:
    start_preflight(_config)


# Create Flask application
app = Flask(__name__)
session_store = SessionStore()
runtime = SessionRuntime(session_store=session_store)
notification_store = NotificationStore(root_dir=session_store.root_dir)
share_store = ShareStore(root_dir=session_store.root_dir)

authorizations = {"apikey": {"type": "apiKey", "in": "header", "name": "X-API-KEY"}}
VERSION = get_config_value("runtime", "version", default="(Dev)")
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


def _require_api_key() -> tuple[dict, int] | None:
    """Validate the API key header for protected routes."""
    api_token = request.headers.get("X-API-Key", None)
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


notification_service = NotificationService(notification_store, runtime.session_store)


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
        if context_payload:
            runtime.append_context_event(session_id, context_payload)

        mode = _parse_mode(request_data.get("mode"))

        started = runtime.start_async(
            session_id=session_id,
            user_query=user_query,
            approval_callback=auto_approve,
            mode=mode,
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
        registry = load_registry()
        specs = registry.list_specs(include_disabled=True)
        tools = [
            {
                "tool_id": spec.tool_id,
                "name": spec.name,
                "kind": spec.kind,
                "enabled": spec.enabled,
                "description": spec.description,
                "disabled_reason": spec.metadata.get("disabled_reason"),
                "server": spec.metadata.get("server"),
            }
            for spec in specs
        ]
        return {"tools": tools}, 200


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

        logging.info("Received user query: {}", user_query)
        task_queue: TaskQueue = runtime.run_sync(
            user_query=user_query,
            session_id=session_id,
            approval_callback=auto_approve,
            mode=mode,
        )
        notification_service.emit_completion(session_id)
        task_result = deepcopy(task_queue.task_result)
        to_return = task_queue.dict()
        to_return["task_result"] = task_result
        logging.info("Returning executed action plan.")
        to_return["session_id"] = session_id
        return to_return, 200


def main() -> None:
    """Run the Meeseeks API server."""
    app.run(debug=True, host="0.0.0.0", port=5124)


if __name__ == "__main__":
    main()
