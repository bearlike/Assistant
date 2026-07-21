#!/usr/bin/env python3
"""The ``get_app`` SessionTool — the maintainer's/builder's read + stage surface.

Apps have a submit-only WRITE surface (:mod:`submit_app`) and an execute surface
(:mod:`run_pipeline`), but no way for a managed session to READ back what it just
shipped. Without one, a maintainer re-woken to modify its own app can only
introspect it by reaching outside the tool surface — and since the only update
path is "resubmit the WHOLE app" (``submit_app`` reads the entire bundle off
disk), a fresh context that no longer holds the staged frontend files literally
cannot update the app. ``get_app`` closes both gaps so the full lifecycle —
read (``get_app``), update (``submit_app``), execute (``run_pipeline``), data
(``app_data``) — is expressible as tool calls, and a versioned update is
immediately verifiable as the latest version.

Two operations, one trust envelope:

* ``operation="get"`` — the live manifest WITHOUT file bodies (a cheap "what is
  live right now"): id, title, status, the active + latest version numbers, the
  collections (name + doc count), the pipelines (mode, schedule, on-demand,
  user-writable, whether a schedule trigger is declared, last-run status +
  freshness), the policies, and the frontend/pipeline FILE LIST (path + byte
  size only).
* ``operation="stage"`` — re-materialize the FULL stored bundle (frontend files
  AND ``mode="code"`` pipeline SOURCE — the raw ``spec.frontend.files``, NOT the
  browser-served :meth:`~mewbo_api.apps.models.AppSpec.served_frontend_files`
  projection, because a maintainer editing a pipeline needs its source) into the
  staging dir for THIS session, returning the directory + file list. This is the
  canonical recovery path after the ephemeral staging dir is gone.

Resolution mirrors :mod:`run_pipeline`: there is exactly one app per
maintainer/builder session, so the app is resolved by SCOPE alone (the app whose
``maintainer_session_id`` OR ``owner_session_id`` equals this session) — this
tool takes no ``app_id`` argument, so it structurally cannot read a foreign app;
a session bound to no app reads a uniform ``not_found`` (the ``app_data`` /
``schedule_trigger`` precedent — no existence leak).

Terminal-free (the ``app_data`` / ``run_pipeline`` shape): reading or staging an
app is normal iterative work, never a run exit. Per the ``submit_widget``
post-mortem (``packages/mewbo_core/CLAUDE.md``), ``SessionTool`` is a STRUCTURAL
Protocol — a standalone class inherits NO default method bodies, so
``should_terminate_run`` / ``terminal_reason`` are defined explicitly below.

Unlike ``app_data`` / ``run_pipeline`` (whose local ``runtime.py`` Protocols
declare only the two/three methods those tools use), ``get_app`` needs the richer
READ surface — ``AppStoreBase.latest_version``, ``PipelineRunStoreBase.list_runs``,
``AppDataStoreBase.query`` — so it types its store collaborators as the concrete
store bases (``mewbo_api.apps.store``) it resolves through the same process-wide
factories, rather than the narrower plugin Protocols.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from mewbo_core.session_tools import DEFAULT_SESSION_TOOL_MODES
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mewbo_api.apps.models import PipelineRun
from mewbo_api.apps.plugin.submit_app import _apps_root  # noqa: PLC2701 — staging-root convention
from mewbo_api.apps.store import (
    get_app_data_store,
    get_app_store,
    get_pipeline_run_store,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mewbo_core.classes import ActionStep
    from mewbo_core.types import Event

    from mewbo_api.apps.models import AppSpec, PipelineSpec
    from mewbo_api.apps.store import AppDataStoreBase, AppStoreBase, PipelineRunStoreBase

logging = get_logger(name="apps.plugin.get_app")

GET_APP_TOOL_ID = "get_app"

# A collection's document count is read via ``query`` (the store exposes no bare
# count), so bound how many rows the manifest view pulls per collection — a
# reported count is capped here and flagged when it hits the ceiling, so the
# introspection call can never fault a huge collection into the model's context.
_COLLECTION_DOC_CAP = 1000


# ------------------------------------------------------------------
# Tool args
# ------------------------------------------------------------------


class GetAppArgs(BaseModel):
    """Read your app's live manifest, or re-stage its full source to disk.

    `operation="get"` (default) returns the live manifest WITHOUT file bodies —
    status, the active + latest version, collections (name + doc count),
    pipelines (mode/schedule/on-demand/trigger_declared/freshness), policies, and
    the file list (path + size). Use it to verify what is live right now, e.g. to
    confirm a `submit_app` shipped as the latest version.

    `operation="stage"` re-materializes the FULL stored bundle — every frontend
    file AND every `mode="code"` pipeline source — into your app directory
    (`${MEWBO_APPS_ROOT:-/tmp/mewbo/apps}/${SESSION_ID}/<app_id>/`), so you can
    read + edit the real files before a resubmit. This is the recovery path when
    the staging directory did not survive between turns or a restart.
    """

    model_config = ConfigDict(extra="forbid")

    operation: Literal["get", "stage"] = Field(
        default="get",
        description=(
            "`get` (default): the live manifest, no file bodies. `stage`: "
            "re-materialize the full stored bundle (incl. pipeline source) into "
            "your app directory so you can read + edit it before resubmitting."
        ),
    )


GET_APP_SCHEMA: dict[str, object] = pydantic_to_openai_tool(GetAppArgs, name=GET_APP_TOOL_ID)


# ------------------------------------------------------------------
# The SessionTool
# ------------------------------------------------------------------


class GetAppTool:
    """Handles ``get_app`` — resolve the session's app, then read it or stage it.

    Satisfies the :class:`~mewbo_core.session_tools.SessionTool` Protocol via the
    class-shaped ``tool_id``/``schema``/``modes`` attributes plus ``handle`` /
    ``should_terminate_run`` / ``terminal_reason`` (defined explicitly — structural
    Protocol, no inherited bodies; see the ``submit_widget`` post-mortem in core's
    CLAUDE.md). Terminal-free: reading/staging an app is normal iterative work.

    The three store collaborators resolve from the process-wide store factories
    (``get_app_store`` / ``get_app_data_store`` / ``get_pipeline_run_store``) when
    not passed explicitly (the plugin-manifest build path supplies only
    ``session_id`` + ``event_logger``); a test injects fakes/real Json stores
    directly. ``now_fn`` is injected so freshness reads a fixed clock in tests —
    the model owns freshness math (``PipelineRun.freshness``), never this tool.
    """

    tool_id: str = GET_APP_TOOL_ID
    schema: dict[str, object] = GET_APP_SCHEMA
    modes: frozenset[str] = DEFAULT_SESSION_TOOL_MODES

    def __init__(
        self,
        *,
        session_id: str,
        event_logger: Callable[[Event], None] | None = None,
        app_store: AppStoreBase | None = None,
        data_store: AppDataStoreBase | None = None,
        run_store: PipelineRunStoreBase | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        """Bind the owning session + the three read stores + the clock.

        Args:
            session_id: The builder/maintainer session — the scope boundary. The
                app is resolved as whichever one this session owns or maintains
                (there is exactly one; this tool takes no ``app_id`` argument, so
                it can't be pointed at a foreign app).
            event_logger: Accepted for the ``SessionToolRegistry`` manifest
                constructor shape; unused (this tool emits no transcript event).
            app_store: Manifest read store. ``None`` (the plugin path) resolves
                from ``get_app_store`` at handle time, exactly like ``app_data``.
            data_store: The app-data plane (collection doc counts for ``get``).
            run_store: The provenance ledger (per-pipeline freshness for ``get``).
            now_fn: The clock freshness is computed against. ``None`` defaults to
                UTC wall-clock; a test injects a fixed ``NOW``.
        """
        self._session_id = session_id
        self._app_store = app_store
        self._data_store = data_store
        self._run_store = run_store
        self._now_fn = now_fn or self._utcnow

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)

    # -- Protocol surface (defined explicitly — structural Protocol, no inherited bodies) --

    def should_terminate_run(self) -> bool:
        """Never terminates — reading/staging an app is normal iterative work."""
        return False

    def terminal_reason(self) -> str:
        """Unused (never terminates); default parity with the Protocol."""
        return "awaiting_approval"

    # -- dispatch -------------------------------------------------------------

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Validate, resolve the session's app, then read it or stage it."""
        try:
            args = GetAppArgs.model_validate(action_step.tool_input or {})
        except ValidationError as exc:
            return self._err("validation", str(exc))

        app_store, data_store, run_store = self._resolve_stores()
        if app_store is None or data_store is None or run_store is None:
            return self._err("unavailable", "the apps runtime is not configured")

        app = self._resolve_app(app_store)
        if app is None:
            # Uniform not_found (no app_id argument exists to leak a foreign id).
            return self._err("not_found", "no app is bound to this session")

        if args.operation == "stage":
            return self._stage(app)
        return self._get(app, app_store, data_store, run_store)

    # -- operations -----------------------------------------------------------

    def _get(
        self,
        app: AppSpec,
        app_store: AppStoreBase,
        data_store: AppDataStoreBase,
        run_store: PipelineRunStoreBase,
    ) -> MockSpeaker:
        """The live manifest WITHOUT file bodies — a cheap 'what is live right now'."""
        now = self._now_fn()
        # One ledger scan, grouped by pipeline (list_runs is newest-first, so each
        # per-pipeline slice stays newest-first) — freshness/last-run without an
        # O(pipelines) fan of store reads.
        runs_by_pipeline: dict[str, list[PipelineRun]] = {}
        for run in run_store.list_runs(app.app_id):
            runs_by_pipeline.setdefault(run.pipeline_name, []).append(run)

        return self._ok(
            {
                "operation": "get",
                "app_id": app.app_id,
                "title": app.title,
                "summary": app.summary,
                "icon": app.icon,
                "status": app.status,
                # ``version`` is the ACTIVE manifest version (what is live now — a
                # rollback repoints it below the latest snapshot); ``latest_version``
                # is the highest recorded snapshot, so a maintainer can confirm a
                # resubmit landed as the newest version.
                "version": app.version,
                "latest_version": app_store.latest_version(app.app_id),
                "collections": [
                    self._collection_row(app, c.name, data_store) for c in app.collections
                ],
                "pipelines": [
                    self._pipeline_row(p, runs_by_pipeline.get(p.name, []), now=now)
                    for p in app.pipelines
                ],
                "policies": app.policies.model_dump(mode="json"),
                "files": self._file_list(app),
            }
        )

    def _collection_row(
        self, app: AppSpec, collection: str, data_store: AppDataStoreBase
    ) -> dict[str, Any]:
        """One collection's name + stored document count (capped, honestly flagged)."""
        docs = data_store.query(app.app_id, collection, limit=_COLLECTION_DOC_CAP)
        count = len(docs)
        return {
            "name": collection,
            "doc_count": count,
            "count_capped": count >= _COLLECTION_DOC_CAP,
        }

    @staticmethod
    def _pipeline_row(
        pipeline: PipelineSpec, runs: list[PipelineRun], *, now: datetime
    ) -> dict[str, Any]:
        """One pipeline's declared tier + its last-run status / freshness from the ledger.

        ``trigger_declared`` reports whether the manifest carries a platform-stamped
        ``trigger_ref`` (a non-``None`` ref means the platform armed a schedule for
        it at submit) — deliberately NOT named ``armed`` to avoid colliding with the
        ``/system`` REST endpoint's ``armed``, which checks the LIVE trigger store.
        The two DIVERGE for a paused/broken app (the ref persists on the manifest
        while its trigger is paused), which is exactly the state a repair agent reads
        — so this tool, which resolves only the app/data/run stores, reports the
        DECLARED signal and leaves live arming to ``/system``. ``freshness_seconds``
        delegates to :meth:`PipelineRun.freshness` (the model owns freshness math):
        the age of the most recent SUCCEEDED run, or ``None`` if never succeeded.
        """
        fresh = PipelineRun.freshness(runs, now=now)
        return {
            "name": pipeline.name,
            "mode": pipeline.mode,
            "schedule": pipeline.schedule.model_dump(mode="json")
            if pipeline.schedule is not None
            else None,
            "on_demand": pipeline.on_demand,
            "user_writable": pipeline.user_writable,
            "trigger_ref": pipeline.trigger_ref,
            "trigger_declared": pipeline.trigger_ref is not None,
            "last_run_status": runs[0].status if runs else None,
            "freshness_seconds": fresh.total_seconds() if fresh is not None else None,
        }

    @staticmethod
    def _file_list(app: AppSpec) -> list[dict[str, Any]]:
        """Path + byte size for EVERY stored file — frontend AND pipeline source.

        The raw ``frontend.files`` (not the browser-served projection): a
        maintainer needs to see that the ``mode="code"`` pipeline sources exist,
        so nothing is stripped here (only bodies are withheld — path + size only).
        """
        return [
            {"path": name, "bytes": len(content.encode("utf-8"))}
            for name, content in sorted(app.frontend.files.items())
        ]

    def _stage(self, app: AppSpec) -> MockSpeaker:
        """Re-materialize the FULL stored bundle into this session's app directory.

        Writes the raw ``spec.frontend.files`` (every frontend file + every
        ``mode="code"`` pipeline source) under
        ``<apps_root>/<session_id>/<app_id>/`` — the same convention ``submit_app``
        reads back — so a resubmit carries the whole app forward. Confined TWICE:
        the app dir must resolve under THIS session's dir (defense-in-depth against
        a hostile stored ``app_id`` relocating within the apps root — the model's
        ``app_id`` validator only bans ``:``, not ``/``/``..``), and every target
        file must resolve under the app dir, even though every stored path already
        passed ``AppFrontend``'s traversal validator.
        """
        base = Path(_apps_root()).resolve()
        session_dir = (base / self._session_id).resolve()
        app_dir = (session_dir / app.app_id).resolve()
        try:
            app_dir.relative_to(session_dir)
        except ValueError:
            return self._err(
                "stage", f"app_id {app.app_id!r} escapes the session directory"
            )

        written: list[dict[str, Any]] = []
        try:
            app_dir.mkdir(parents=True, exist_ok=True)
            for rel, content in sorted(app.frontend.files.items()):
                target = (app_dir / rel).resolve()
                try:
                    target.relative_to(app_dir)
                except ValueError:
                    return self._err("stage", f"file path {rel!r} escapes the app directory")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                written.append({"path": rel, "bytes": len(content.encode("utf-8"))})
        except OSError as exc:
            return self._err("stage", f"could not stage the app bundle: {exc}")

        return self._ok(
            {
                "operation": "stage",
                "app_id": app.app_id,
                "directory": str(app_dir),
                "files": written,
            }
        )

    # -- resolution helpers ---------------------------------------------------

    def _resolve_app(self, app_store: AppStoreBase) -> AppSpec | None:
        """The app this session owns or maintains (mirrors ``run_pipeline``).

        Scope is derived purely from the session — the builder session
        (``owner_session_id``, pre-submit) or the maintainer session
        (``maintainer_session_id``, post-submit) binds this session to exactly one
        app. ``include_archived=True`` so a maintainer can still read/stage an
        archived app it owns.
        """
        for app in app_store.list_apps(include_archived=True):
            if self._session_id in (app.maintainer_session_id, app.owner_session_id):
                return app
        return None

    def _resolve_stores(
        self,
    ) -> tuple[AppStoreBase | None, AppDataStoreBase | None, PipelineRunStoreBase | None]:
        """Explicit constructor injection wins; else the process-wide store factories.

        ``or`` short-circuits, so a test's injected store is never overridden by a
        factory call. A factory failure degrades to a clean ``unavailable`` error
        rather than a crash (mirrors ``app_data._resolve_stores``).
        """
        try:
            return (
                self._app_store or get_app_store(),
                self._data_store or get_app_data_store(),
                self._run_store or get_pipeline_run_store(),
            )
        except Exception as exc:  # noqa: BLE001 — a store-init failure must not crash the agent
            logging.error("get_app: store resolution failed: {}", exc)
            return None, None, None

    # -- rendering ------------------------------------------------------------

    @staticmethod
    def _err(code: str, message: str) -> MockSpeaker:
        """Structured-error envelope (``str({"error": {...}})``) — the shared shape.

        ``str(dict)`` not ``json.dumps`` — the loop's envelope parser
        (``_session_tool_error_envelope``) is ``ast.literal_eval``, which reads
        Python repr, not JSON literals. Mirrors ``AppDataTool._err`` verbatim.
        """
        return MockSpeaker(content=str({"error": {"code": code, "message": message}}))

    @staticmethod
    def _ok(payload: dict[str, object]) -> MockSpeaker:
        """Successful structured payload — same Python-repr shape as :meth:`_err`."""
        return MockSpeaker(content=str(payload))


__all__ = [
    "GET_APP_SCHEMA",
    "GetAppArgs",
    "GetAppTool",
]
