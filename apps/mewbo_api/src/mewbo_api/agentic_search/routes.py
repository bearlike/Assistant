"""Flask-RESTX namespace for the Agentic Search API.

Endpoints under ``/api/agentic_search``:

Workspaces + sources (persistent — JSON/Mongo via the store):

- ``GET    /sources?project=``        list the MCP source catalog
- ``GET    /tiers``                   search-budget tiers + the model preset each runs on
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
from mewbo_core.config import get_config_value
from pydantic import ValidationError

from mewbo_api.request_context import request_surface
from mewbo_api.responses import ApiResponseKit

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

# One shared error-documentation kit for the whole namespace (DRY home for the
# error half of every operation's contract — see ``mewbo_api.responses``).
# graph_routes builds its OWN kit from ``graph_ns``; it does not reuse this one.
kit = ApiResponseKit(agentic_ns, prefix="Search")


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
        "model": fields.String(
            description=(
                "Optional model override for this run, as a LiteLLM model name. "
                "Wins over the tier's configured model (`scg.traversal.tier_models`); "
                "probes inherit it. Omit to let the tier pick."
            ),
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


# -- Success-response models (documentation only — real sample bodies) -------
#
# Field ``example=`` values are how Scalar synthesises a *real* sample response;
# the shapes below mirror each handler's actual ``return`` dict. Nested models
# stay shallow (one representative element per list) so the generated example is
# readable. Models are reused wherever two operations return the same shape.

search_source_model = agentic_ns.model(
    "SearchSource",
    {
        "id": fields.String(example="github", description="Source id (the MCP server name)."),
        "name": fields.String(example="GitHub", description="Display name."),
        "color": fields.String(example="#ffffff"),
        "bg": fields.String(example="#191919"),
        "glyph": fields.String(example="?"),
        "desc": fields.String(example="Issues, pull requests and code search."),
        "source_type": fields.String(
            example="mcp_tool_list",
            description="SCG descriptor kind a map job uses for this source.",
        ),
        "available": fields.Boolean(
            example=True,
            description="False when the configured connector's discovery failed.",
        ),
        "unavailable_reason": fields.String(
            example=None, description="Why an unavailable source is greyed out."
        ),
        "tool_ids": fields.List(
            fields.String,
            example=["mcp_github_search_issues", "mcp_github_get_file_contents"],
            description="The tool ids a run on this source is allowed to call.",
        ),
    },
)

search_sources_model = agentic_ns.model(
    "SearchSourcesResponse",
    {
        "sources": fields.List(
            fields.Nested(search_source_model),
            description="The catalog of connectors a workspace can enable.",
        )
    },
)

search_tiers_model = agentic_ns.model(
    "SearchTiersResponse",
    {
        "default_tier": fields.String(
            example="auto", description="The server's configured default tier."
        ),
        "tiers": fields.Raw(
            example={
                "fast": "openai/gpt-5.4-nano",
                "auto": "anthropic/claude-haiku-4-5",
                "deep": "anthropic/claude-opus-4-8",
            },
            description="Tier id → the resolved model preset each tier runs on.",
        ),
    },
)

search_past_query_model = agentic_ns.model(
    "SearchPastQuery",
    {
        "q": fields.String(example="Which services call the billing API?"),
        "when": fields.String(example="2 hours ago"),
        "results": fields.Integer(example=7),
        "ran_at": fields.String(example="2026-06-15T09:30:00Z"),
        "run_id": fields.String(example="r-1a2b3c4d-1"),
        "status": fields.String(example="completed"),
    },
)

search_workspace_model = agentic_ns.model(
    "SearchWorkspace",
    {
        "id": fields.String(example="ws-7f3a91"),
        "name": fields.String(example="Engineering systems"),
        "desc": fields.String(example="Issues, code and docs for the platform team"),
        "sources": fields.List(fields.String, example=["github", "linear"]),
        "instructions": fields.String(example="Prefer the billing-service repo."),
        "created": fields.String(example="Jun 15, 2026"),
        "created_at": fields.String(example="2026-06-15T09:00:00Z"),
        "updated_at": fields.String(example="2026-06-15T09:30:00Z"),
        "past_queries": fields.List(fields.Nested(search_past_query_model)),
    },
)

search_workspaces_model = agentic_ns.model(
    "SearchWorkspacesResponse",
    {
        "workspaces": fields.List(
            fields.Nested(search_workspace_model),
            description="All saved workspaces (filtered by `q` when given).",
        )
    },
)

search_workspace_item_model = agentic_ns.model(
    "SearchWorkspaceResponse",
    {"workspace": fields.Nested(search_workspace_model)},
)

search_workspace_deleted_model = agentic_ns.model(
    "SearchWorkspaceDeletedResponse",
    {
        "workspace_id": fields.String(example="ws-7f3a91"),
        "deleted": fields.Boolean(example=True),
    },
)

search_result_model = agentic_ns.model(
    "SearchResultCard",
    {
        "id": fields.String(example="r-1a2b3c4d-result-0"),
        "title": fields.String(example="billing-service/app/api/charges.py"),
        "source": fields.String(example="github"),
        "url": fields.String(example="https://github.com/acme/billing-service"),
        "snippet": fields.String(
            example="The charges router calls BillingClient.create_charge()."
        ),
        "meta": fields.Raw(
            example={"stars": 128, "language": "Python"},
            description="Agent-emitted structured facts (scalars only).",
        ),
    },
)

search_trace_agent_model = agentic_ns.model(
    "SearchTraceAgent",
    {
        "agent_id": fields.String(example="r-1a2b3c4d-probe-0"),
        "name": fields.String(example="scg-path-probe"),
        "kind": fields.String(example="probe"),
        "model": fields.String(example="anthropic/claude-haiku-4-5"),
        "steps": fields.Integer(example=3),
        "results_count": fields.Integer(example=2),
    },
)

search_run_stats_model = agentic_ns.model(
    "SearchRunStats",
    {
        "probes": fields.Integer(example=2),
        "tool_calls": fields.Integer(example=5),
        "tokens": fields.Integer(example=4821),
        "setup_ms": fields.Integer(example=180),
        "search_ms": fields.Integer(example=2200),
    },
)

search_run_payload_model = agentic_ns.model(
    "SearchRunPayload",
    {
        "run_id": fields.String(example="r-1a2b3c4d-1"),
        "session_id": fields.String(example="9e2d47c1f0a84b2c"),
        "query": fields.String(example="Which services call the billing API?"),
        "workspace_id": fields.String(example="ws-7f3a91"),
        "status": fields.String(example="running"),
        "tier": fields.String(example="auto"),
        "model": fields.String(
            example=None, description="Per-run model override, or null for the tier default."
        ),
        "total_ms": fields.Integer(example=0),
        "results": fields.List(fields.Nested(search_result_model)),
        "trace": fields.List(fields.Nested(search_trace_agent_model)),
        "related_questions": fields.List(
            fields.String, example=["Which services write to the billing DB?"]
        ),
        "stats": fields.Nested(
            search_run_stats_model,
            allow_null=True,
            description="Derived run stats; null until a real settle ran.",
        ),
        "error": fields.String(example=None),
    },
)

search_run_create_model = agentic_ns.model(
    "SearchRunCreateResponse",
    {
        "run": fields.Nested(search_run_payload_model),
        "run_id": fields.String(example="r-1a2b3c4d-1"),
        "session_id": fields.String(example="9e2d47c1f0a84b2c"),
        "status": fields.String(
            example="running",
            description="`running` for an orchestrated run, `completed` for the echo path.",
        ),
    },
)

search_run_cancel_model = agentic_ns.model(
    "SearchRunCancelResponse",
    {
        "run_id": fields.String(example="r-1a2b3c4d-1"),
        "cancelled": fields.Boolean(
            example=True,
            description="False when the run had already settled (nothing changed).",
        ),
    },
)

search_run_record_model = agentic_ns.model(
    "SearchRunRecord",
    {
        "run_id": fields.String(example="r-1a2b3c4d-1"),
        "session_id": fields.String(example="9e2d47c1f0a84b2c"),
        "workspace_id": fields.String(example="ws-7f3a91"),
        "query": fields.String(example="Which services call the billing API?"),
        "status": fields.String(example="completed"),
        "tier": fields.String(example="auto"),
        "model": fields.String(example=None),
        "created_at": fields.String(example="2026-06-15T09:30:00Z"),
        "started_at": fields.String(example="2026-06-15T09:30:01Z"),
        "completed_at": fields.String(example="2026-06-15T09:30:08Z"),
        "total_ms": fields.Integer(example=7200),
        "error": fields.String(example=None),
        "source_ids": fields.List(fields.String, example=["github", "linear"]),
        "allowed_tools": fields.List(
            fields.String, example=["mcp_github_search_issues"]
        ),
        "output_contract_version": fields.String(example="1.0"),
        "payload": fields.Nested(search_run_payload_model, allow_null=True),
    },
)

search_run_item_model = agentic_ns.model(
    "SearchRunResponse",
    {"run": fields.Nested(search_run_record_model)},
)

search_workspace_runs_model = agentic_ns.model(
    "SearchWorkspaceRunsResponse",
    {
        "runs": fields.List(
            fields.Nested(search_run_record_model),
            description="The workspace's recent run records, newest first.",
        )
    },
)

search_map_job_model = agentic_ns.model(
    "SearchMapJob",
    {
        "job_id": fields.String(example="map-5e6f7a8b"),
        "source_id": fields.String(example="github"),
        "source_type": fields.String(example="mcp_tool_list"),
        "status": fields.String(example="running"),
        "phase": fields.String(example="introspect"),
        "phase_started_at": fields.String(example="2026-06-15T09:30:05Z"),
        "node_count": fields.Integer(example=12),
        "edge_count": fields.Integer(example=18),
        "error": fields.Raw(
            example=None, description="Redacted `{code, message}` on failure; never a secret."
        ),
        "created_at": fields.String(example="2026-06-15T09:30:00Z"),
        "started_at": fields.String(example="2026-06-15T09:30:01Z"),
        "completed_at": fields.String(example=None),
    },
)

search_map_start_model = agentic_ns.model(
    "SearchMapStartResponse",
    {
        "job": fields.Nested(search_map_job_model),
        "job_id": fields.String(example="map-5e6f7a8b"),
    },
)

search_map_jobs_model = agentic_ns.model(
    "SearchMapJobsResponse",
    {
        "jobs": fields.List(
            fields.Nested(search_map_job_model),
            description="The source's map jobs, newest first.",
        )
    },
)

search_map_job_item_model = agentic_ns.model(
    "SearchMapJobResponse",
    {"job": fields.Nested(search_map_job_model)},
)

search_scg_source_model = agentic_ns.model(
    "SearchScgSource",
    {
        "source_id": fields.String(example="github"),
        "source_type": fields.String(example="mcp_tool_list"),
    },
)

search_scg_counts_model = agentic_ns.model(
    "SearchScgCounts",
    {
        "sources": fields.Integer(example=2),
        "nodes": fields.Integer(example=64),
        "edges": fields.Integer(example=120),
        "recipes": fields.Integer(example=8),
    },
)

search_scg_model = agentic_ns.model(
    "SearchScgResponse",
    {
        "enabled": fields.Boolean(example=True),
        "counts": fields.Nested(search_scg_counts_model),
        "sources": fields.List(fields.Nested(search_scg_source_model)),
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
    _register_search_launcher()


def _register_search_launcher() -> None:
    """Wire the self-facing agentic-search launcher for the ``scg`` plugin.

    The ``agentic_search`` SessionTool (in ``mewbo_graph``) lets a task-spawned
    engine agent RUN a search, but the run lifecycle (session + run store) lives
    here, up-layer. So — exactly like :func:`_register_map_phase_sink` — the api
    injects a concrete backend bound to this run store + the session runtime via
    :class:`~mewbo_graph.scg.search_launcher.SearchLauncher`. No-op when SCG is
    disabled or the graph library is absent (the tool then degrades to a
    structured "unavailable" error).
    """
    if not ScgConfig.enabled():
        return
    try:
        from mewbo_graph.scg.search_launcher import SearchLauncher

        from .search_launcher_impl import RunStoreSearchLauncher
    except ImportError:
        return

    SearchLauncher.register(RunStoreSearchLauncher(runtime=_runtime))


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
        description=(
            "Call this first when building a workspace: each entry's `id` is what "
            "you pass in a workspace's `sources` array. A configured source whose "
            "discovery failed stays listed with `available: false`. Pass `?project=` "
            "to scope the catalog to one project's connector configuration."
        ),
        params={
            "project": {
                "description": "Project name that scopes the catalog to that "
                "project's connector configuration.",
                "in": "query",
                "type": "string",
            }
        },
    )
    @agentic_ns.response(200, "The source catalog.", search_sources_model)
    @kit.auth_error()
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


@agentic_ns.route("/tiers")
class TiersResource(Resource):
    """The search-budget tiers and the model preset each one runs on."""

    @agentic_ns.doc(
        "list_tiers",
        description=(
            "Use this to populate a tier picker before a run: the response maps each "
            "tier id to the model it will actually drive with, so a client can show "
            "`auto · claude-haiku-4-5` before submit. A pure config read — available "
            "even when `scg.enabled` is off."
        ),
    )
    @agentic_ns.response(
        200, "Tier ids mapped to the model each tier runs on.", search_tiers_model
    )
    @kit.auth_error()
    def get(self) -> tuple[dict, int]:
        """List the search tiers and their model presets.

        A tier is the run's one budget knob — decomposition depth and probe
        fan-out — and it also presets the model that drives the run
        (`scg.traversal.tier_models`). A blank mapping resolves to
        `llm.default_model`, mirroring exactly what the drive does, so the
        returned model is the one that actually runs unless the request
        carries an explicit `model` override. Pure config read — available
        regardless of `scg.enabled`.
        """
        if (auth := _require_api_key()) is not None:
            return auth
        default_model = str(get_config_value("llm", "default_model") or "")
        return {
            "default_tier": ScgConfig.default_tier(),
            "tiers": {
                tier: ScgConfig.model_for_tier(tier) or default_model
                for tier in SEARCH_TIERS
            },
        }, 200


# -- Workspaces ------------------------------------------------------------


@agentic_ns.route("/workspaces")
class WorkspacesResource(Resource):
    """Collection endpoint for workspaces."""

    @agentic_ns.doc(
        "list_workspaces",
        description=(
            "Lists every saved workspace with its enabled sources, instructions and "
            "recent query history. Pass `?q=` to filter (case-insensitive substring) "
            "across name, description and past-query text — handy for a workspace "
            "switcher's search box."
        ),
        params={
            "q": {
                "description": "Case-insensitive filter matched against workspace "
                "name, description and past-query text.",
                "in": "query",
                "type": "string",
            }
        },
    )
    @agentic_ns.response(200, "Matching workspaces.", search_workspaces_model)
    @kit.auth_error()
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
        description=(
            "Creates a workspace from a name plus a selection of source ids (from "
            "`GET /sources`) and optional run instructions. The new workspace is "
            "returned with its generated `id` — use that id to start runs. Creating "
            "one may kick off background mapping of newly enabled live sources."
        ),
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
    @agentic_ns.response(201, "Workspace created.", search_workspace_item_model)
    @kit.errors(400, shape="message")
    @kit.auth_error()
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
        description=(
            "Applies a partial update — only `name`, `desc`, `sources` and "
            "`instructions` are writable, and omitted fields keep their current "
            "values. Send just the fields you want to change. Changing the source "
            "selection or instructions can trigger background re-mapping. Returns the "
            "full updated workspace."
        ),
    )
    @agentic_ns.expect(workspace_patch_request)
    @agentic_ns.response(200, "The updated workspace.", search_workspace_item_model)
    @kit.errors(400, 404, shape="message")
    @kit.auth_error()
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
        description=(
            "Removes the workspace and its stored connector configuration (including "
            "any auth material that configuration carried). Past runs stay readable "
            "by id. The response echoes the deleted id with `deleted: true`."
        ),
        params={
            "workspace_id": "Workspace id returned by "
            "POST /api/agentic_search/workspaces.",
        },
    )
    @agentic_ns.response(200, "Workspace deleted.", search_workspace_deleted_model)
    @kit.errors(404, shape="message")
    @kit.auth_error()
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
        description=(
            "Returns the workspace's recent run records, newest first — use it to "
            "rebuild a run-history list in a client. Each record is a durable "
            "snapshot; fetch one run with `GET /runs/{run_id}` for the full payload. "
            "An unknown workspace id yields an empty list (not a 404)."
        ),
        params={
            "workspace_id": "Workspace id returned by "
            "POST /api/agentic_search/workspaces.",
        },
    )
    @agentic_ns.response(200, "Recent runs, newest first.", search_workspace_runs_model)
    @kit.auth_error()
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

    @agentic_ns.doc(
        "create_run",
        description=(
            "Starts a search run over a workspace's enabled sources. POST "
            "`{workspace_id, query}` (optionally `tier` and a per-run `model` "
            "override). The response always carries `run_id`, `session_id`, `status` "
            "and the full `run` snapshot. An orchestrated run returns `running` "
            "promptly — then poll `GET /runs/{run_id}` or attach to "
            "`GET /runs/{run_id}/events` (SSE) to follow it; the echo path settles "
            "synchronously as `completed`."
        ),
    )
    @agentic_ns.expect(search_run_create_request)
    @agentic_ns.response(
        200,
        "Run started; body carries the run snapshot plus its ids.",
        search_run_create_model,
    )
    @kit.errors(400, 404, shape="message")
    @kit.auth_error()
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
        # Optional per-run model override — the /v1/structured stance: a
        # non-string (or blank) value is ignored, never a 400.
        model = body.get("model")
        project = body.get("project")
        payload = SearchRun.start(
            workspace_id=workspace_id,
            query=query.strip(),
            store=store_mod.get_store(),
            runtime=_runtime,
            project=project if isinstance(project, str) else None,
            tier=tier,
            model=model.strip() if isinstance(model, str) and model.strip() else None,
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
        description=(
            "Returns the durable run snapshot — status, query, tier, timestamps and "
            "the results/answer accumulated so far. Safe to poll while a run is "
            "`running`, and self-sufficient for reload, share and deep-link views "
            "(`/search?ws=<id>&run=<id>`) with no other context. Any valid API key "
            "resolves the same run by id."
        ),
        params={"run_id": "Run id returned by POST /api/agentic_search/runs."},
    )
    @agentic_ns.response(200, "The run snapshot.", search_run_item_model)
    @kit.errors(404, shape="message")
    @kit.auth_error()
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
        description=(
            "Requests cancellation of an in-flight run and, best effort, the session "
            "backing it. The `cancelled` flag is false when the run had already "
            "settled (in which case nothing changes). The terminal state still "
            "arrives on the event stream and the snapshot."
        ),
        params={"run_id": "Run id returned by POST /api/agentic_search/runs."},
    )
    @agentic_ns.response(
        200,
        "Cancellation attempted; `cancelled` reports the outcome.",
        search_run_cancel_model,
    )
    @kit.errors(404, shape="message")
    @kit.auth_error()
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
        description=(
            "Server-sent events (`text/event-stream`): replays the run's append-only "
            "event log from the start, then tails it live until a terminal event. "
            "Attach an `EventSource` to this URL right after `POST /runs` to render a "
            "live result reveal. Each frame's `id:` line is the event index, so a "
            "dropped connection resumes via `?after_idx=` or the `Last-Event-ID` "
            "header. EventSource can't set headers, so the API key is also accepted "
            "as the `?api_key=` query parameter."
        ),
    )
    @agentic_ns.response(200, "Server-sent event stream of run events.")
    @kit.errors(404, shape="message")
    @kit.auth_error()
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
        description=(
            "Starts a background job that indexes the connector's schema into the "
            "Source Capability Graph, making the source routable by search runs. The "
            "response carries a `job_id` — poll "
            "`GET /sources/{source_id}/map/jobs/{job_id}` or follow "
            "`GET /sources/{source_id}/map/events` (SSE). For an `mcp_tool_list` "
            "source you can omit `descriptor` and one is built from the connector's "
            "live tool list. Requires `scg.enabled` (503 when off)."
        ),
    )
    @agentic_ns.expect(source_map_request)
    @agentic_ns.response(
        202,
        "Map job accepted; track it via the job and event endpoints.",
        search_map_start_model,
    )
    @kit.errors(
        400,
        422,
        503,
        shape="message",
        descriptions={
            422: "Unsupported source type, or no connector available to introspect.",
            503: "Source Capability Graph is disabled, or the mapper is unavailable.",
        },
    )
    @kit.auth_error()
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
        description=(
            "Returns the source's mapping jobs, newest first. Each carries a coarse "
            "`status` (`queued`/`running`/`completed`/`failed`) and a fine-grained "
            "`phase`. Poll this after `POST /sources/{source_id}/map` to follow "
            "progress without holding an SSE stream open."
        ),
        params={"source_id": "Source id from GET /api/agentic_search/sources."},
    )
    @agentic_ns.response(
        200, "Map jobs for the source, newest first.", search_map_jobs_model
    )
    @kit.auth_error()
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
        description=(
            "Returns one mapping job's record. The job must belong to the source in "
            "the path, else 404. Poll this until `status` settles to `completed` or "
            "`failed`."
        ),
        params={
            "source_id": "Source id from GET /api/agentic_search/sources.",
            "job_id": "Map job id returned by POST /sources/{source_id}/map.",
        },
    )
    @agentic_ns.response(200, "The map job record.", search_map_job_item_model)
    @kit.errors(
        404,
        shape="message",
        descriptions={404: "Map job not found, or it belongs to another source."},
    )
    @kit.auth_error()
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
        description=(
            "Server-sent events (`text/event-stream`) over a mapping job's "
            "append-only event log: replays from the start, then tails live until a "
            "terminal event. The newest job for the source streams by default; pass "
            "`?job_id=` to pick one. A dropped connection resumes via `?after_idx=` "
            "or `Last-Event-ID`. EventSource can't set headers, so the API key is "
            "also accepted as `?api_key=`."
        ),
    )
    @agentic_ns.response(200, "Server-sent event stream of map job events.")
    @kit.errors(
        404,
        shape="message",
        descriptions={404: "No map job for the source, or unknown job id."},
    )
    @kit.auth_error()
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

    @agentic_ns.doc(
        "introspect_scg",
        description=(
            "Returns node/edge/source/recipe counts for the Source Capability Graph "
            "plus the list of mapped sources — use it to confirm that map jobs have "
            "populated the graph before relying on orchestrated runs. A deterministic "
            "read that never invokes a model. Requires `scg.enabled` (503 when off)."
        ),
    )
    @agentic_ns.response(
        200, "Graph counts and the mapped source list.", search_scg_model
    )
    @kit.errors(
        503,
        shape="message",
        descriptions={503: "Source Capability Graph is disabled."},
    )
    @kit.auth_error()
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
