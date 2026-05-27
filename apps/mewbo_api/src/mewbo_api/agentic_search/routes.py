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
from flask_restx import Namespace, Resource
from mewbo_core.common import get_logger
from pydantic import ValidationError

from . import store as store_mod
from .catalog import SourceCatalog
from .events import RunSseGenerator
from .runs import SearchRun
from .scg.config import ScgConfig
from .schemas import WorkspaceInput

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


def init_agentic_search(
    api: object, require_api_key: AuthGuard, runtime: Any = None
) -> None:
    """Wire the namespace + capture the auth guard and the session runtime.

    When ``scg.enabled`` is on AND the SCG already holds at least one mapped
    source, swap the active :class:`SearchRunner` from the default echo replay to
    the real :class:`OrchestratedSearchRunner` (graph-routed traversal over a
    ``scg-search`` session). With the feature off — or with an empty graph — the
    echo runner stays the default so the console↔API loop still works with no LLM.
    """
    global _require_api_key, _runtime
    _require_api_key = require_api_key
    _runtime = runtime
    api.add_namespace(agentic_ns, path="/api/agentic_search")  # type: ignore[attr-defined]
    _maybe_register_orchestrated_runner()
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


def _maybe_register_orchestrated_runner() -> None:
    """Register the orchestrated runner iff SCG is enabled + a source is mapped.

    Failure-soft, mirroring the other namespace wiring in ``backend.py``: a
    missing pymongo / store error never blocks startup — the echo runner simply
    stays active. The check is gated first on the cheap ``scg.enabled`` flag so a
    disabled deployment never touches the SCG store.
    """
    if not ScgConfig.enabled():
        return
    try:
        from mewbo_graph.scg.store import get_scg_store

        from .runner import set_search_runner
        from .scg.orchestrated_runner import OrchestratedSearchRunner, SearchTier

        if not get_scg_store().list_sources():
            return  # nothing mapped yet — keep the echo runner as the default
        # Config tier is lowercase (``"auto"``); the runner's knob is capitalized.
        # The runner normalizes any unknown value back to its default, so pass the
        # raw capitalized value straight through — no second validation table here.
        tier = cast("SearchTier", ScgConfig.default_tier().capitalize())
        set_search_runner(OrchestratedSearchRunner(tier=tier))
    except Exception as exc:  # pragma: no cover — startup fail-soft
        logging.warning("orchestrated runner registration skipped: {}", exc)


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

    @agentic_ns.doc("list_sources")
    def get(self) -> tuple[dict, int]:
        """Return the source catalog, optionally scoped to ``?project=``."""
        if (auth := _require_api_key()) is not None:
            return auth
        project = request.args.get("project")
        sources = [s.model_dump() for s in SourceCatalog.entries(project)]
        return {"sources": sources}, 200


# -- Workspaces ------------------------------------------------------------


@agentic_ns.route("/workspaces")
class WorkspacesResource(Resource):
    """Collection endpoint for workspaces."""

    @agentic_ns.doc("list_workspaces")
    def get(self) -> tuple[dict, int]:
        """List all workspaces."""
        if (auth := _require_api_key()) is not None:
            return auth
        workspaces = [w.model_dump() for w in store_mod.get_store().list_workspaces()]
        return {"workspaces": workspaces}, 200

    @agentic_ns.doc("create_workspace")
    def post(self) -> tuple[dict, int]:
        """Create a new workspace."""
        if (auth := _require_api_key()) is not None:
            return auth
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return {"message": "request body must be a JSON object"}, 400
        try:
            data = WorkspaceInput.model_validate(body)
        except ValidationError as exc:
            return _validation_error(exc)
        workspace = store_mod.get_store().create_workspace(data)
        return {"workspace": workspace.model_dump()}, 201


@agentic_ns.route("/workspaces/<string:workspace_id>")
class WorkspaceItemResource(Resource):
    """Per-workspace endpoint."""

    @agentic_ns.doc("update_workspace")
    def patch(self, workspace_id: str) -> tuple[dict, int]:
        """Apply a partial update to a workspace."""
        if (auth := _require_api_key()) is not None:
            return auth
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return {"message": "request body must be a JSON object"}, 400
        if body.get("sources") is not None and not isinstance(body["sources"], list):
            return {"message": "sources must be a list of source ids"}, 400
        workspace = store_mod.get_store().update_workspace(workspace_id, body)
        if workspace is None:
            return {"message": "workspace not found"}, 404
        return {"workspace": workspace.model_dump()}, 200

    @agentic_ns.doc("delete_workspace")
    def delete(self, workspace_id: str) -> tuple[dict, int]:
        """Delete a workspace."""
        if (auth := _require_api_key()) is not None:
            return auth
        if not store_mod.get_store().delete_workspace(workspace_id):
            return {"message": "workspace not found"}, 404
        return {"workspace_id": workspace_id, "deleted": True}, 200


@agentic_ns.route("/workspaces/<string:workspace_id>/runs")
class WorkspaceRunsResource(Resource):
    """Recent runs for a workspace (history inspection / replay)."""

    @agentic_ns.doc("list_workspace_runs")
    def get(self, workspace_id: str) -> tuple[dict, int]:
        """List recent runs for *workspace_id* (newest first)."""
        if (auth := _require_api_key()) is not None:
            return auth
        runs = [r.model_dump() for r in store_mod.get_store().list_runs(workspace_id)]
        return {"runs": runs}, 200


# -- Runs ------------------------------------------------------------------


@agentic_ns.route("/runs")
class RunsResource(Resource):
    """Create + drive a search run scoped to a workspace."""

    @agentic_ns.doc("create_run")
    def post(self) -> tuple[dict, int]:
        """Start a run. Returns the run id + the normalized payload (back-compat)."""
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
        project = body.get("project")
        payload = SearchRun.start(
            workspace_id=workspace_id,
            query=query.strip(),
            store=store_mod.get_store(),
            runtime=_runtime,
            project=project if isinstance(project, str) else None,
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

    @agentic_ns.doc("get_run")
    def get(self, run_id: str) -> tuple[dict, int]:
        """Return the run record + its accumulated payload."""
        if (auth := _require_api_key()) is not None:
            return auth
        record = SearchRun.get(run_id, store=store_mod.get_store())
        if record is None:
            return {"message": "run not found"}, 404
        return {"run": record.model_dump()}, 200


@agentic_ns.route("/runs/<string:run_id>/cancel")
class RunCancelResource(Resource):
    """Cancel a run."""

    @agentic_ns.doc("cancel_run")
    def post(self, run_id: str) -> tuple[dict, int]:
        """Cancel *run_id*; best-effort cancels the backing session when real."""
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

    @agentic_ns.doc("stream_run_events")
    def get(self, run_id: str) -> Any:
        """Stream typed search events as ``text/event-stream``."""
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

    @agentic_ns.doc("map_source")
    def post(self, source_id: str) -> tuple[dict, int]:
        """Start a :class:`MapSourceJob`; return the record + ``job_id``.

        Gated on ``scg.enabled`` (503 when off). The path ``source_id`` plus the
        JSON body (``source_type``, optional ``descriptor`` / ``auth_scope`` /
        ``model``) form the map contract; ``descriptor`` is an UNTRUSTED schema
        the job carries in the user query, never the system prompt.
        """
        if (auth := _require_api_key()) is not None:
            return auth
        if not ScgConfig.enabled():
            return {"message": "SCG is disabled (set scg.enabled=true)"}, 503

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


@agentic_ns.route("/sources/<string:source_id>/map/events")
class SourceMapEventsResource(Resource):
    """SSE event stream over a map-source job's append-only event log."""

    @agentic_ns.doc("stream_map_events")
    def get(self, source_id: str) -> Any:
        """Stream the latest map-job's events for *source_id* as SSE.

        Reuses :class:`RunSseGenerator` verbatim — the map-job event log shares
        the run event-log shape, so the same replay-from-idx + tail generator
        projects it. ``?job_id=`` selects a specific job; otherwise the newest
        job for *source_id* is streamed. 404 when no job exists.
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
    def get(self) -> tuple[dict, int]:
        """Return SCG node/edge/source/recipe counts + the mapped source list.

        Gated on ``scg.enabled`` (503 when off) so a disabled deployment never
        touches the SCG store. Reads the deterministic core's
        :func:`get_scg_store` — never an LLM.
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
