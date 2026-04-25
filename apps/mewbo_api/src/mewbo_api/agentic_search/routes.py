"""Flask-RESTX namespace for the Agentic Search mock API.

Endpoints under ``/api/agentic_search``:

- ``GET    /sources``                  list MCP source catalog
- ``GET    /workspaces``               list workspaces
- ``POST   /workspaces``               create workspace
- ``PATCH  /workspaces/<id>``          update workspace
- ``DELETE /workspaces/<id>``          delete workspace
- ``POST   /runs``                     execute a canned search run

Auth: every route guards behind ``_require_api_key`` injected by
``init_agentic_search`` at app startup, matching the rest of the API.
"""

from __future__ import annotations

from collections.abc import Callable

from flask import request
from flask_restx import Namespace, Resource

from . import store

AuthResult = tuple[dict, int] | None
AuthGuard = Callable[[], AuthResult]


def _no_auth() -> AuthResult:
    return None


_require_api_key: AuthGuard = _no_auth

agentic_ns = Namespace(
    "agentic_search",
    description="Agentic Search — multi-source workspace search (mock implementation).",
)


def init_agentic_search(api: object, require_api_key: AuthGuard) -> None:
    """Wire the namespace and register it with the API.

    ``api`` is a ``flask_restx.Api`` instance; ``require_api_key`` is the
    same guard used by every other namespace in ``backend.py``. Mounted
    under ``/api/agentic_search`` so route bodies stay short.
    """
    global _require_api_key
    _require_api_key = require_api_key
    api.add_namespace(agentic_ns, path="/api/agentic_search")  # type: ignore[attr-defined]


# -- Sources ---------------------------------------------------------------


@agentic_ns.route("/sources")
class SourcesResource(Resource):
    """List of MCP-style connectors the search agent can fan out across."""

    @agentic_ns.doc("list_sources")
    def get(self) -> tuple[dict, int]:
        """Return the MCP source catalog."""
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        return {"sources": store.list_sources()}, 200


# -- Workspaces ------------------------------------------------------------


@agentic_ns.route("/workspaces")
class WorkspacesResource(Resource):
    """Collection endpoint for workspaces."""

    @agentic_ns.doc("list_workspaces")
    def get(self) -> tuple[dict, int]:
        """List all workspaces."""
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        return {"workspaces": store.list_workspaces()}, 200

    @agentic_ns.doc("create_workspace")
    def post(self) -> tuple[dict, int]:
        """Create a new workspace."""
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return {"message": "request body must be a JSON object"}, 400
        if not body.get("name"):
            return {"message": "name is required"}, 400
        sources = body.get("sources")
        if sources is not None and not isinstance(sources, list):
            return {"message": "sources must be a list of source ids"}, 400
        workspace = store.create_workspace(body)
        return {"workspace": workspace}, 201


@agentic_ns.route("/workspaces/<string:workspace_id>")
class WorkspaceItemResource(Resource):
    """Per-workspace endpoint."""

    @agentic_ns.doc("update_workspace")
    def patch(self, workspace_id: str) -> tuple[dict, int]:
        """Apply a partial update to a workspace."""
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return {"message": "request body must be a JSON object"}, 400
        sources = body.get("sources")
        if sources is not None and not isinstance(sources, list):
            return {"message": "sources must be a list of source ids"}, 400
        workspace = store.update_workspace(workspace_id, body)
        if workspace is None:
            return {"message": "workspace not found"}, 404
        return {"workspace": workspace}, 200

    @agentic_ns.doc("delete_workspace")
    def delete(self, workspace_id: str) -> tuple[dict, int]:
        """Delete a workspace."""
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        if not store.delete_workspace(workspace_id):
            return {"message": "workspace not found"}, 404
        return {"workspace_id": workspace_id, "deleted": True}, 200


# -- Runs ------------------------------------------------------------------


@agentic_ns.route("/runs")
class RunsResource(Resource):
    """Execute a canned search run scoped to a workspace."""

    @agentic_ns.doc("create_run")
    def post(self) -> tuple[dict, int]:
        """Execute a search run scoped to a workspace."""
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return {"message": "request body must be a JSON object"}, 400
        workspace_id = body.get("workspace_id")
        query = body.get("query")
        if not isinstance(workspace_id, str) or not workspace_id:
            return {"message": "workspace_id is required"}, 400
        if not isinstance(query, str) or not query.strip():
            return {"message": "query is required"}, 400
        payload = store.run_search(workspace_id, query.strip())
        if payload is None:
            return {"message": "workspace not found"}, 404
        return {"run": payload}, 200
