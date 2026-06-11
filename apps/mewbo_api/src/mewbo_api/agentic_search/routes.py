"""Flask-RESTX namespace for the Agentic Search API.

Endpoints under ``/api/agentic_search``:

Workspaces + sources (persistent — JSON/Mongo via the store):

- ``GET    /sources?project=``        list the MCP source catalog
- ``GET    /workspaces``              list workspaces
- ``POST   /workspaces``              create workspace
- ``PATCH  /workspaces/<id>``         update workspace
- ``DELETE /workspaces/<id>``         delete workspace
- ``GET    /workspaces/<id>/runs``    recent runs for a workspace

Run lifecycle (durable run store + normalized SSE projection):

- ``POST   /runs``                    create + drive a run; back-compat ``{run}``
- ``GET    /runs/<run_id>``           run snapshot (reload / share / deep-link)
- ``GET    /runs/<run_id>/events``    normalized SSE event stream
- ``POST   /runs/<run_id>/cancel``    cancel a run

SCG indexing (Source Capability Graph — gated on ``scg.enabled``):

- ``POST   /sources/<id>/map``        start a map-source (SCG indexing) job
- ``GET    /sources/<id>/map/jobs``   map-job snapshots (latest first)
- ``GET    /sources/<id>/map/jobs/<job_id>`` one map-job snapshot
- ``GET    /sources/<id>/map/events`` SSE over the map-job event log
- ``GET    /scg``                     introspection — node/edge counts + sources

Auth: every route guards behind ``_require_api_key`` (injected by
``init_agentic_search``); SSE additionally accepts the ``api_key`` query param
because ``_require_api_key`` already honours it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from flask import Response, request, stream_with_context
from flask_restx import Namespace, Resource, fields
from mewbo_core.common import get_logger
from pydantic import ValidationError

from mewbo_api.request_context import request_surface

from . import store as store_mod
from .catalog import SourceCatalog
from .events import RunSseGenerator
from .mcp_config import WorkspaceMcpConfig
from .runs import SearchRun
from .scg.config import ScgConfig
from .schemas import SEARCH_TIERS, WorkspaceInput
from .source_sync import WorkspaceSourceSync

logging = get_logger(name="api.agentic_search.routes")

AuthResult = tuple[dict, int] | None
AuthGuard = Callable[[], AuthResult]


def _no_auth() -> AuthResult:
    return None


_require_api_key: AuthGuard = _no_auth
_runtime: Any = None  # populated by init_agentic_search; used by the real runner

agentic_ns = Namespace(
    "agentic_search",
    description="Agentic Search — multi-source workspace search.",
)


# -- Request models (documentation only — handlers validate via Pydantic) ----

workspace_create_request = agentic_ns.model(
    "WorkspaceCreateRequest",
    {
        "name": fields.String(
            required=True,
            description="Human-readable workspace name.",
            example="Engineering systems",
        ),
        "desc": fields.String(
            description="Short description shown in workspace lists.",
            default="",
            example="Issues, code and docs for the platform team",
        ),
        "sources": fields.List(
            fields.String,
            description=(
                "Ids of the sources to enable, from GET /api/agentic_search/sources. "
                "Defaults to no sources."
            ),
            example=["github", "linear"],
        ),
        "instructions": fields.String(
            description=(
                "Guidance applied to every run in this workspace, such as preferred "
                "repositories or terminology."
            ),
            default="",
        ),
    },
)

workspace_patch_request = agentic_ns.model(
    "WorkspacePatchRequest",
    {
        "name": fields.String(description="New workspace name."),
        "desc": fields.String(description="New short description."),
        "sources": fields.List(
            fields.String,
            description=(
                "Replacement list of enabled source ids. Changing the selection can "
                "start background mapping of newly enabled sources."
            ),
        ),
        "instructions": fields.String(description="Replacement run guidance."),
    },
)

search_run_create_request = agentic_ns.model(
    "SearchRunCreateRequest",
    {
        "workspace_id": fields.String(
            required=True,
            description="Workspace to search, returned by POST /api/agentic_search/workspaces.",
        ),
        "query": fields.String(
            required=True,
            description="Natural-language search query.",
            example="Which services call the billing API?",
        ),
        "tier": fields.String(
            description=(
                "Search depth: `fast`, `auto` or `deep`. The tier also picks the model "
                "that drives the run. Defaults to the server's configured tier."
            ),
            enum=["fast", "auto", "deep"],
            example="auto",
        ),
        "project": fields.String(
            description="Optional project name that scopes connector configuration.",
        ),
    },
)

source_map_request = agentic_ns.model(
    "SourceMapRequest",
    {
        "source_type": fields.String(
            required=True,
            description=(
                "Kind of connector being mapped, for example `mcp_tool_list`. "
                "`text` is not yet supported and returns 422."
            ),
            example="mcp_tool_list",
        ),
        "descriptor": fields.Raw(
            description=(
                "The connector's self-description, such as an MCP tool list or an "
                "OpenAPI document. Optional for `mcp_tool_list` sources, where it is "
                "built from the connector's live tool list when omitted."
            ),
        ),
        "auth_scope": fields.String(
            description=(
                "Redacted label for the auth the connector carries, for example "
                "`oauth:repo`. Never a token or credential."
            ),
            example="oauth:repo",
        ),
        "model": fields.String(
            description="Optional model override for the mapping session, as a LiteLLM model name.",
        ),
        "nl_context": fields.Raw(
            description=(
                "Optional workspace prose that seeds the mapping step, with "
                "`workspace_instructions` and `workspace_description` keys. Usually "
                "injected automatically when a workspace is saved."
            ),
        ),
    },
)


def init_agentic_search(
    api: object, require_api_key: AuthGuard, runtime: Any = None
) -> None:
    """Wire the namespace + capture the auth guard and the session runtime.

    The active :class:`SearchRunner` is NOT chosen here — ``get_search_runner``
    resolves it per run (orchestrated iff ``scg.enabled`` AND ≥1 mapped source),
    so mapping the first source takes effect without a process restart.
    """
    global _require_api_key, _runtime
    _require_api_key = require_api_key
    _runtime = runtime
    api.add_namespace(agentic_ns, path="/api/agentic_search")  # type: ignore[attr-defined]
    from .graph_routes import init_agentic_search_graph  # noqa: PLC0415

    init_agentic_search_graph(api, require_api_key, runtime)  # #79 workspace graph
    _register_map_phase_sink()


def _register_map_phase_sink() -> None:
    """Wire the cosmetic map-job phase sink for the relocated ``scg`` plugin.

    Map-job progress is persisted in *this* run store so it rides the SSE
    plumbing; the plugin (in ``mewbo_graph``) can't write it without importing
    up, so it emits through :class:`~mewbo_graph.scg.map_phase.MapPhaseSink` and
    we register the concrete run-store writer here. No-op when SCG is disabled
    or the graph library is absent — the phase write is purely cosmetic.
    """
    if not ScgConfig.enabled():
        return
    try:
        from mewbo_graph.scg.map_phase import MapPhaseSink

        from .scg.map_progress import MapJobProgress
        from .schemas import MapJobPhase
        from .store import get_store
    except ImportError:
        return

    def _write(job_id: str, phase: str) -> int | None:
        # The sink seam is generic ``(str, str)``; the run-store phase vocabulary
        # (``MapJobPhase``) is an api-side concern, so narrow it here at the
        # boundary. The only emitter (``scg_finalize_map``) passes valid phases.
        return MapJobProgress.emit_phase(get_store(), job_id, cast(MapJobPhase, phase))

    MapPhaseSink.register(_write)


def _validation_error(exc: ValidationError) -> tuple[dict, int]:
    """Render a Pydantic error as a 400 with a readable message."""
    errors = exc.errors()
    if not errors:
        return {"message": "invalid request body"}, 400
    first = errors[0]
    loc = ".".join(str(p) for p in first.get("loc", ())) or "body"
    return {"message": f"{loc}: {first.get('msg', 'invalid')}"}, 400


# -- Sources ---------------------------------------------------------------


@agentic_ns.route("/sources")
class SourcesResource(Resource):
    """The MCP-style connector catalog the search agent fans out across."""

    @agentic_ns.doc(
        "list_sources",
        params={
            "project": {
                "description": "Project name that scopes the catalog to that "
                "project's connector configuration.",
                "in": "query",
                "type": "string",
            }
        },
    )
    @agentic_ns.response(200, "The source catalog.")
    @agentic_ns.response(401, "Missing or invalid API key.")
    def get(self) -> tuple[dict, int]:
        """List available sources.

        Returns the catalog of connectors a workspace can enable. Each entry
        describes one source: its id, display name, type and availability.
        A configured source whose discovery failed stays listed with
        `available` false rather than being omitted. Pass `project` to scope
        the catalog to one project's configuration.
        """
        if (auth := _require_api_key()) is not None:
            return auth
        project = request.args.get("project")
        sources = [s.model_dump() for s in SourceCatalog.entries(project)]
        return {"sources": sources}, 200


# -- Workspaces ------------------------------------------------------------


@agentic_ns.route("/workspaces")
class WorkspacesResource(Resource):
    """Collection endpoint for workspaces."""

    @agentic_ns.doc(
        "list_workspaces",
        params={
            "q": {
                "description": "Case-insensitive filter matched against workspace "
                "name, description and past-query text.",
                "in": "query",
                "type": "string",
            }
        },
    )
    @agentic_ns.response(200, "Matching workspaces.")
    @agentic_ns.response(401, "Missing or invalid API key.")
    def get(self) -> tuple[dict, int]:
        """List workspaces.

        Returns all saved workspaces, each with its enabled sources,
        instructions and recent query history. Pass `q` to filter by name,
        description or past-query text.
        """
        if (auth := _require_api_key()) is not None:
            return auth
        q = request.args.get("q")
        st = store_mod.get_store()
        found = st.search_workspaces(q) if q else st.list_workspaces()
        return {"workspaces": [w.model_dump() for w in found]}, 200

    @agentic_ns.doc(
        "create_workspace",
        params={
            "project": {
                "description": "Project name used when auto-mapping newly "
                "enabled sources.",
                "in": "query",
                "type": "string",
            }
        },
    )
    @agentic_ns.expect(workspace_create_request)
    @agentic_ns.response(201, "Workspace created.")
    @agentic_ns.response(400, "Malformed or invalid request body.")
    @agentic_ns.response(401, "Missing or invalid API key.")
    def post(self) -> tuple[dict, int]:
        """Create a workspace.

        A workspace names a set of enabled sources plus optional run
        instructions. Creating one also refreshes its connector configuration
        and may start mapping newly enabled live sources in the background.
        The new workspace is returned with its generated `id`.
        """
        if (auth := _require_api_key()) is not None:
            return auth
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return {"message": "request body must be a JSON object"}, 400
        try:
            data = WorkspaceInput.model_validate(body)
        except ValidationError as exc:
            return _validation_error(exc)
        st = store_mod.get_store()
        workspace = st.create_workspace(data)
        # Refresh the persisted virtual MCP config + auto-map newly-enabled live
        # sources into the GLOBAL SCG (best-effort, idempotent — #75).
        WorkspaceSourceSync.on_workspace_saved(
            store=st,
            workspace_id=workspace.id,
            new_sources=list(workspace.sources),
            prev_sources=None,
            runtime=_runtime,
            project=request.args.get("project"),
        )
        return {"workspace": workspace.model_dump()}, 201


@agentic_ns.route("/workspaces/<string:workspace_id>")
class WorkspaceItemResource(Resource):
    """Per-workspace endpoint."""

    @agentic_ns.doc(
        "update_workspace",
        params={
            "workspace_id": "Workspace id returned by "
            "POST /api/agentic_search/workspaces.",
            "project": {
                "description": "Project name used when auto-mapping newly "
                "enabled sources.",
                "in": "query",
                "type": "string",
            },
        },
    )
    @agentic_ns.expect(workspace_patch_request)
    @agentic_ns.response(200, "The updated workspace.")
    @agentic_ns.response(400, "Malformed request body.")
    @agentic_ns.response(401, "Missing or invalid API key.")
    @agentic_ns.response(404, "Workspace not found.")
    def patch(self, workspace_id: str) -> tuple[dict, int]:
        """Update a workspace.

        Applies a partial update: only `name`, `desc`, `sources` and
        `instructions` are writable, and omitted fields keep their current
        values. Changing the source selection or the instructions can start
        background re-mapping of the affected sources. Returns the full
        updated workspace.
        """
        if (auth := _require_api_key()) is not None:
            return auth
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return {"message": "request body must be a JSON object"}, 400
        if body.get("sources") is not None and not isinstance(body["sources"], list):
            return {"message": "sources must be a list of source ids"}, 400
        st = store_mod.get_store()
        # Capture the prior selection + prose BEFORE the update so the source-sync
        # hook can map only the newly-enabled sources (#75) and detect an
        # instructions/desc change that should re-seed the map-time enrich (#83).
        existing = st.get_workspace(workspace_id)
        prev_sources = list(existing.sources) if existing is not None else None
        prev_prose = (
            (existing.instructions or "", existing.desc or "")
            if existing is not None
            else None
        )
        workspace = st.update_workspace(workspace_id, body)
        if workspace is None:
            return {"message": "workspace not found"}, 404
        # The hook is the graph-lifecycle seam: a sources change OR an
        # instructions/desc edit can re-drive the map+enrich. An instructions-only
        # PATCH carries no ``sources`` key, so the old sources-only gate skipped
        # it (the #83 gap). Fire whenever the selection or the prose moved; the
        # hook is idempotent + in-flight-guarded, so a no-op PATCH still fires
        # nothing downstream.
        prose_changed = prev_prose is not None and prev_prose != (
            workspace.instructions or "",
            workspace.desc or "",
        )
        if body.get("sources") is not None or prose_changed:
            WorkspaceSourceSync.on_workspace_saved(
                store=st,
                workspace_id=workspace.id,
                new_sources=list(workspace.sources),
                prev_sources=prev_sources,
                runtime=_runtime,
                project=request.args.get("project"),
            )
        return {"workspace": workspace.model_dump()}, 200

    @agentic_ns.doc(
        "delete_workspace",
        params={
            "workspace_id": "Workspace id returned by "
            "POST /api/agentic_search/workspaces.",
        },
    )
    @agentic_ns.response(200, "Workspace deleted.")
    @agentic_ns.response(401, "Missing or invalid API key.")
    @agentic_ns.response(404, "Workspace not found.")
    def delete(self, workspace_id: str) -> tuple[dict, int]:
        """Delete a workspace.

        Removes the workspace and its stored connector configuration,
        including any auth material that configuration carried. Past runs
        remain readable by id. The response confirms the deleted id.
        """
        if (auth := _require_api_key()) is not None:
            return auth
        st = store_mod.get_store()
        if not st.delete_workspace(workspace_id):
            return {"message": "workspace not found"}, 404
        # Drop the secret-bearing virtual config alongside the workspace so no
        # orphaned auth material lingers in the isolated config store (#75).
        WorkspaceMcpConfig.delete(st, workspace_id)
        return {"workspace_id": workspace_id, "deleted": True}, 200


@agentic_ns.route("/workspaces/<string:workspace_id>/runs")
class WorkspaceRunsResource(Resource):
    """Recent runs for a workspace (history inspection / replay)."""

    @agentic_ns.doc(
        "list_workspace_runs",
        params={
            "workspace_id": "Workspace id returned by "
            "POST /api/agentic_search/workspaces.",
        },
    )
    @agentic_ns.response(200, "Recent runs, newest first.")
    @agentic_ns.response(401, "Missing or invalid API key.")
    def get(self, workspace_id: str) -> tuple[dict, int]:
        """List runs for a workspace.

        Returns the workspace's recent run records, newest first. Use it to
        rebuild run history in a client; fetch one run with
        `GET /runs/{run_id}` for the full snapshot. An unknown workspace id
        yields an empty list.
        """
        if (auth := _require_api_key()) is not None:
            return auth
        runs = [r.model_dump() for r in store_mod.get_store().list_runs(workspace_id)]
        return {"runs": runs}, 200


# -- Runs ------------------------------------------------------------------


@agentic_ns.route("/runs")
class RunsResource(Resource):
    """Create + drive a search run scoped to a workspace."""

    @agentic_ns.doc("create_run")
    @agentic_ns.expect(search_run_create_request)
    @agentic_ns.response(200, "Run started; body carries the run snapshot plus its ids.")
    @agentic_ns.response(400, "Malformed body, missing field, or unknown tier.")
    @agentic_ns.response(401, "Missing or invalid API key.")
    @agentic_ns.response(404, "Workspace not found.")
    def post(self) -> tuple[dict, int]:
        """Start a search run.

        Runs the search agent over the workspace's enabled sources. The
        response always carries `run_id`, `session_id`, `status` and the full
        `run` snapshot. An orchestrated run returns `running` promptly and
        settles through the event stream or by polling `GET /runs/{run_id}`;
        the echo path settles synchronously as `completed`. Set `tier` to
        trade depth for latency; the tier also picks the model that drives
        the run.
        """
        if (auth := _require_api_key()) is not None:
            return auth
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return {"message": "request body must be a JSON object"}, 400
        workspace_id = body.get("workspace_id")
        query = body.get("query")
        if not isinstance(workspace_id, str) or not workspace_id.strip():
            return {"message": "workspace_id is required"}, 400
        if not isinstance(query, str) or not query.strip():
            return {"message": "query is required"}, 400
        tier = body.get("tier")
        if tier is not None and tier not in SEARCH_TIERS:
            return {"message": "tier must be one of fast|auto|deep"}, 400
        project = body.get("project")
        payload = SearchRun.start(
            workspace_id=workspace_id,
            query=query.strip(),
            store=store_mod.get_store(),
            runtime=_runtime,
            project=project if isinstance(project, str) else None,
            tier=tier,
            source_platform=request_surface(),
        )
        if payload is None:
            return {"message": "workspace not found"}, 404
        return {
            "run": payload.model_dump(),
            "run_id": payload.run_id,
            "session_id": payload.session_id,
            "status": payload.status,
        }, 200


@agentic_ns.route("/runs/<string:run_id>")
class RunItemResource(Resource):
    """Per-run snapshot endpoint."""

    @agentic_ns.doc(
        "get_run",
        params={"run_id": "Run id returned by POST /api/agentic_search/runs."},
    )
    @agentic_ns.response(200, "The run snapshot.")
    @agentic_ns.response(401, "Missing or invalid API key.")
    @agentic_ns.response(404, "Run not found.")
    def get(self, run_id: str) -> tuple[dict, int]:
        """Get a run.

        Returns the durable run snapshot: status, query, tier, timestamps and
        the results and answer accumulated so far. Safe to poll while a run
        is `running`, and self-sufficient for reload, share and deep-link
        views with no other context.
        """
        if (auth := _require_api_key()) is not None:
            return auth
        record = SearchRun.get(run_id, store=store_mod.get_store())
        if record is None:
            return {"message": "run not found"}, 404
        return {"run": record.model_dump()}, 200


@agentic_ns.route("/runs/<string:run_id>/cancel")
class RunCancelResource(Resource):
    """Cancel a run."""

    @agentic_ns.doc(
        "cancel_run",
        params={"run_id": "Run id returned by POST /api/agentic_search/runs."},
    )
    @agentic_ns.response(200, "Cancellation attempted; `cancelled` reports the outcome.")
    @agentic_ns.response(401, "Missing or invalid API key.")
    @agentic_ns.response(404, "Run not found.")
    def post(self, run_id: str) -> tuple[dict, int]:
        """Cancel a run.

        Requests cancellation of an in-flight run and, best effort, the
        session backing it. The `cancelled` flag in the response is false
        when the run had already settled, in which case nothing changes.
        The terminal state still arrives on the event stream and snapshot.
        """
        if (auth := _require_api_key()) is not None:
            return auth
        st = store_mod.get_store()
        if SearchRun.get(run_id, store=st) is None:
            return {"message": "run not found"}, 404
        cancelled = SearchRun.cancel(run_id, store=st, runtime=_runtime)
        return {"run_id": run_id, "cancelled": cancelled}, 200


@agentic_ns.route("/runs/<string:run_id>/events")
class RunEventsResource(Resource):
    """Normalized SSE event stream for a run (replay-from-start + live tail)."""

    @agentic_ns.doc(
        "stream_run_events",
        params={
            "run_id": "Run id returned by POST /api/agentic_search/runs.",
            "after_idx": {
                "description": "Replay only events with an index greater than "
                "this value. Defaults to -1, a full replay from the start.",
                "in": "query",
                "type": "integer",
            },
            "api_key": {
                "description": "API key, for EventSource clients that cannot "
                "set the X-API-Key header.",
                "in": "query",
                "type": "string",
            },
        },
    )
    @agentic_ns.response(200, "Server-sent event stream of run events.")
    @agentic_ns.response(401, "Missing or invalid API key.")
    @agentic_ns.response(404, "Run not found.")
    def get(self, run_id: str) -> Any:
        """Stream run events.

        Server-sent events (`text/event-stream`): replays the run's
        append-only event log from the start, then tails it live until a
        terminal event. Each frame's `id` line carries the event index, so a
        dropped connection resumes with `after_idx` or the `Last-Event-ID`
        header. Because EventSource cannot set headers, the API key is also
        accepted as the `api_key` query parameter.
        """
        if (auth := _require_api_key()) is not None:
            return auth
        st = store_mod.get_store()
        if SearchRun.get(run_id, store=st) is None:
            return {"message": "run not found"}, 404
        raw_after = request.args.get("after_idx") or request.headers.get("Last-Event-ID")
        try:
            after_idx = int(raw_after) if raw_after is not None else -1
        except (ValueError, TypeError):
            after_idx = -1
        gen = RunSseGenerator(store=st, run_id=run_id, after_idx=after_idx)
        return Response(
            stream_with_context(gen.generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


# -- SCG indexing (map source) + introspection -----------------------------


@agentic_ns.route("/sources/<string:source_id>/map")
class SourceMapResource(Resource):
    """Start a map-source (SCG indexing) job for one connector."""

    @agentic_ns.doc(
        "map_source",
        params={
            "source_id": "Source id from GET /api/agentic_search/sources.",
            "project": {
                "description": "Project name used to locate the connector when "
                "building a descriptor from its live tool list.",
                "in": "query",
                "type": "string",
            },
        },
    )
    @agentic_ns.expect(source_map_request)
    @agentic_ns.response(202, "Map job accepted; track it via the job and event endpoints.")
    @agentic_ns.response(400, "Malformed or invalid request body.")
    @agentic_ns.response(401, "Missing or invalid API key.")
    @agentic_ns.response(422, "Unsupported source type, or no connector available to introspect.")
    @agentic_ns.response(503, "Source Capability Graph is disabled, or the mapper is unavailable.")
    def post(self, source_id: str) -> tuple[dict, int]:
        """Map a source.

        Starts a background job that indexes the connector's schema into the
        Source Capability Graph, which makes the source routable by search
        runs. The job is asynchronous: the response carries a `job_id` to
        poll via `GET /sources/{source_id}/map/jobs/{job_id}` or to follow on
        the map event stream. When `descriptor` is omitted for an
        `mcp_tool_list` source, one is built from the connector's live tool
        list; a source with no configured connector returns 422 instead.
        Returns 503 when `scg.enabled` is off.
        """
        if (auth := _require_api_key()) is not None:
            return auth
        if not ScgConfig.enabled():
            return {"message": "SCG is disabled (set scg.enabled=true)"}, 503

        from .scg.descriptors import SourceDescriptorBuilder
        from .scg.map_job import MapSourceJob, SourceMapInput

        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return {"message": "request body must be a JSON object"}, 400
        payload = {**body, "source_id": source_id}
        model = payload.pop("model", None)
        try:
            source = SourceMapInput.model_validate(payload)
        except ValidationError as exc:
            return _validation_error(exc)
        if source.source_type == "text":
            # The schemaless ``LlmStructureProvider`` needs an injected LLM and
            # is never registered (``StructureProviderRegistry.with_defaults``
            # excludes it), so a "text" map job would always fail in-session at
            # ``scg_build_structure`` — reject honestly up-front instead.
            return {"message": "source_type 'text' not yet supported"}, 422
        if (
            source.descriptor is None
            and source.source_type == SourceDescriptorBuilder.SOURCE_TYPE
        ):
            builder = SourceDescriptorBuilder(
                source_id, project=request.args.get("project")
            )
            try:
                built = builder.build()
            except LookupError as exc:
                return {"message": str(exc)}, 422
            except RuntimeError as exc:
                return {"message": str(exc)}, 503
            source = source.model_copy(update={"descriptor": built.raw})
        try:
            job = MapSourceJob.start(
                source,
                store=store_mod.get_store(),
                runtime=_runtime,
                model=model if isinstance(model, str) else None,
            )
        except RuntimeError as exc:
            return {"message": str(exc)}, 503
        return {"job": job.model_dump(), "job_id": job.job_id}, 202


@agentic_ns.route("/sources/<string:source_id>/map/jobs")
class SourceMapJobsResource(Resource):
    """Map-job snapshots for one source (the durable poll surface)."""

    @agentic_ns.doc(
        "list_map_jobs",
        params={"source_id": "Source id from GET /api/agentic_search/sources."},
    )
    @agentic_ns.response(200, "Map jobs for the source, newest first.")
    @agentic_ns.response(401, "Missing or invalid API key.")
    def get(self, source_id: str) -> tuple[dict, int]:
        """List map jobs for a source.

        Returns the source's mapping jobs, newest first. Each record carries
        the job's status (`queued`, `running`, `completed` or `failed`) and
        its progress phase. Poll this after starting a map job to follow it
        without holding an event stream open.
        """
        if (auth := _require_api_key()) is not None:
            return auth
        jobs = store_mod.get_store().list_map_jobs(source_id=source_id)
        return {"jobs": [j.model_dump() for j in jobs]}, 200


@agentic_ns.route("/sources/<string:source_id>/map/jobs/<string:job_id>")
class SourceMapJobItemResource(Resource):
    """One map-job snapshot."""

    @agentic_ns.doc(
        "get_map_job",
        params={
            "source_id": "Source id from GET /api/agentic_search/sources.",
            "job_id": "Map job id returned by POST /sources/{source_id}/map.",
        },
    )
    @agentic_ns.response(200, "The map job record.")
    @agentic_ns.response(401, "Missing or invalid API key.")
    @agentic_ns.response(404, "Map job not found, or it belongs to another source.")
    def get(self, source_id: str, job_id: str) -> tuple[dict, int]:
        """Get a map job.

        Returns one mapping job's record. The job must belong to the source
        in the path; otherwise the response is 404. Poll this until the
        status settles to `completed` or `failed`.
        """
        if (auth := _require_api_key()) is not None:
            return auth
        job = store_mod.get_store().get_map_job(job_id)
        if job is None or job.source_id != source_id:
            return {"message": "map job not found"}, 404
        return {"job": job.model_dump()}, 200


@agentic_ns.route("/sources/<string:source_id>/map/events")
class SourceMapEventsResource(Resource):
    """SSE event stream over a map-source job's append-only event log."""

    @agentic_ns.doc(
        "stream_map_events",
        params={
            "source_id": "Source id from GET /api/agentic_search/sources.",
            "job_id": {
                "description": "Map job to stream. Defaults to the newest job "
                "for the source.",
                "in": "query",
                "type": "string",
            },
            "after_idx": {
                "description": "Replay only events with an index greater than "
                "this value. Defaults to -1, a full replay from the start.",
                "in": "query",
                "type": "integer",
            },
            "api_key": {
                "description": "API key, for EventSource clients that cannot "
                "set the X-API-Key header.",
                "in": "query",
                "type": "string",
            },
        },
    )
    @agentic_ns.response(200, "Server-sent event stream of map job events.")
    @agentic_ns.response(401, "Missing or invalid API key.")
    @agentic_ns.response(404, "No map job for the source, or unknown job id.")
    def get(self, source_id: str) -> Any:
        """Stream map job events.

        Server-sent events (`text/event-stream`) over a mapping job's
        append-only event log: replays from the start, then tails live until
        a terminal event. The newest job for the source is streamed by
        default; pass `job_id` to pick one. A dropped connection resumes with
        `after_idx` or the `Last-Event-ID` header. Because EventSource cannot
        set headers, the API key is also accepted as the `api_key` query
        parameter.
        """
        if (auth := _require_api_key()) is not None:
            return auth
        st = store_mod.get_store()
        job_id = request.args.get("job_id")
        if job_id is None:
            jobs = st.list_map_jobs(source_id=source_id)
            if not jobs:
                return {"message": "no map job for source"}, 404
            job_id = jobs[0].job_id  # newest-first
        elif st.get_map_job(job_id) is None:
            return {"message": "map job not found"}, 404
        raw_after = request.args.get("after_idx") or request.headers.get("Last-Event-ID")
        try:
            after_idx = int(raw_after) if raw_after is not None else -1
        except (ValueError, TypeError):
            after_idx = -1
        gen = RunSseGenerator(
            store=st,
            run_id=job_id,
            after_idx=after_idx,
            load=st.load_map_job_events,
        )
        return Response(
            stream_with_context(gen.generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


@agentic_ns.route("/scg")
class ScgResource(Resource):
    """Introspection over the Source Capability Graph (counts + sources)."""

    @agentic_ns.doc("introspect_scg")
    @agentic_ns.response(200, "Graph counts and the mapped source list.")
    @agentic_ns.response(401, "Missing or invalid API key.")
    @agentic_ns.response(503, "Source Capability Graph is disabled.")
    def get(self) -> tuple[dict, int]:
        """Inspect the capability graph.

        Returns node, edge, source and recipe counts for the Source
        Capability Graph, plus the list of mapped sources with their types.
        Useful to confirm that map jobs have populated the graph. The read is
        deterministic and never invokes a model. Returns 503 when
        `scg.enabled` is off.
        """
        if (auth := _require_api_key()) is not None:
            return auth
        if not ScgConfig.enabled():
            return {"message": "SCG is disabled (set scg.enabled=true)"}, 503

        from mewbo_graph.scg.store import get_scg_store

        scg = get_scg_store()
        sources = scg.list_sources()
        return {
            "enabled": True,
            "counts": {
                "sources": len(sources),
                "nodes": len(scg.query_nodes()),
                "edges": len(scg.list_edges()),
                "recipes": len(scg.list_recipes()),
            },
            "sources": [
                {"source_id": s.source_id, "source_type": s.source_type}
                for s in sources
            ],
        }, 200


__all__ = ["agentic_ns", "init_agentic_search"]
