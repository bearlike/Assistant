#!/usr/bin/env python3
"""Meeseeks API.

Single-user REST API with session-based orchestration and event polling.
"""

from __future__ import annotations

from copy import deepcopy

from flask import Flask, request
from flask_restx import Api, Resource, fields
from meeseeks_core.classes import TaskQueue
from meeseeks_core.common import get_logger
from meeseeks_core.config import get_config, get_config_value, start_preflight
from meeseeks_core.permissions import auto_approve
from meeseeks_core.session_runtime import SessionRuntime, parse_core_command
from meeseeks_core.session_store import SessionStore
from meeseeks_core.tool_registry import load_registry

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
                        "action_consumer": fields.String(
                            required=True,
                            description="The tool responsible for executing the action",
                        ),
                        "action_type": fields.String(
                            required=True,
                            description="The type of action to be performed (get/set)",
                        ),
                        "action_argument": fields.String(
                            required=True, description="The specific argument for the action"
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
    api_token = request.headers.get("X-API-Key", None)
    if api_token is None:
        return {"message": "API token is not provided."}, 401
    if api_token != MASTER_API_TOKEN:
        logging.warning("Unauthorized API call attempt with token: {}", api_token)
        return {"message": "Unauthorized"}, 401
    return None


def _handle_slash_command(session_id: str, user_query: str) -> tuple[dict, int] | None:
    command = parse_core_command(user_query)
    if command == "/terminate":
        canceled = runtime.cancel(session_id)
        return {"session_id": session_id, "canceled": canceled}, 202
    if command == "/status":
        return {"session_id": session_id, **runtime.summarize_session(session_id)}, 200
    return None


def _parse_bool(value: str | None) -> bool:
    if value is None:
        return False
    lowered = value.strip().lower()
    if not lowered:
        return False
    return lowered not in {"0", "false", "no", "off"}


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
        session_tag = payload.get("session_tag")
        if session_tag:
            runtime.session_store.tag_session(session_id, session_tag)
        context = payload.get("context")
        if isinstance(context, dict):
            runtime.append_context_event(session_id, context)
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

        context = request_data.get("context")
        if isinstance(context, dict):
            runtime.append_context_event(session_id, context)

        started = runtime.start_async(
            session_id=session_id,
            user_query=user_query,
            approval_callback=auto_approve,
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
        session_id = runtime.resolve_session(
            session_id=request_data.get("session_id"),
            session_tag=request_data.get("session_tag"),
            fork_from=request_data.get("fork_from"),
        )

        logging.info("Received user query: {}", user_query)
        task_queue: TaskQueue = runtime.run_sync(
            user_query=user_query,
            session_id=session_id,
            approval_callback=auto_approve,
        )
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
