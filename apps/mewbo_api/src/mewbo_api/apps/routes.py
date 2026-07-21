"""Flask-RESTX namespace for the Mewbo Apps REST surface (design spec §4A).

Wire shapes match the ALREADY-COMMITTED clients verbatim (console
``src/api/apps.ts`` + ``src/types.ts``, Aura, and the injected SDK
``plugin/sdk/mewbo_app.py``) — the clients are the consumers, so the API
conforms to them.

Endpoints under ``/api/apps``:

Gallery + CRUD (master/issued-key auth)::

    GET    /apps                     gallery (lean AppSummary cards)
    POST   /apps                     create a draft app -> {app_id, session_id}
    GET    /apps/<id>                {spec, versions}  (AppDetail)
    PATCH  /apps/<id>                edit title/summary/icon -> AppDetail
    POST   /apps/<id>/archive        archive -> AppSpec  (no hard delete)

Read-only introspection (master key OR app-scoped read token)::

    GET    /apps/<id>/data/<collection>   AppDataDoc envelopes (filter/sort/limit)
    GET    /apps/<id>/system              consolidated AppSystemHealth (§2.6)
    GET    /apps/<id>/pipelines           declared pipeline surface (Phase 2)

Pipeline invocation (``mode="code"`` only — Phase 2)::

    GET    /apps/<id>/pipelines/<name>    invoke, read-auth, params from the query string
    POST   /apps/<id>/pipelines/<name>    invoke, WRITE-auth, params from the JSON body

Management (master/issued key only)::

    POST   /apps/<id>/token          mint a read (or, master-key-only, write) token
    GET    /apps/<id>/triggers       app-scoped triggers (TriggerDTO[])
    POST   /apps/<id>/pause          pause the app + its triggers -> AppSpec
    POST   /apps/<id>/resume         resume the app + its triggers -> AppSpec
    POST   /apps/<id>/rollback       repoint to an earlier version -> AppDetail

Auth model. The data + system GETs and the pipeline-list/invoke-GET accept a
master/issued key OR a short-lived app-scoped token (any scope); the token is
its own ``token_id`` (the whole signed blob) presented in the
``X-Mewbo-App-Token`` header (the SDK's contract). A token whose ``app_id``
differs from the path is 403 (no cross-app read). The invoke-POST additionally
requires a ``write``-scoped token (or the master/issued key) — a valid
``read`` token there is 403, never silently accepted. The data plane is
READ-ONLY at the REST layer — no write verb exists, so a write attempt 405s.
A ``mode="agentic"`` pipeline (the default) is never synchronously invocable —
GET/POST on it 409s; it runs only on its own schedule or via its maintainer.

Error bodies carry a top-level ``message`` (the legacy ``message`` wire-shape
``ApiResponseKit`` documents as ``shape="message"``, which the console's
``readJson`` surfaces as ``data.message``) — matching the ``agentic_search``
routes. ``ApiResponseKit`` exposes no generic runtime error builder (only the
410 ``terminated_response()``), so the shape is produced directly and documented
via the kit's ``shape="message"`` decorators.

Paradigm: the ``triggers/routes.py`` DI idiom — :class:`AppsRoutesController`
owns the collaborators + every domain method; thin Resources receive it via
``resource_class_kwargs`` and pass request data as ARGUMENTS (the controller
never reads ``flask.request``, so it unit-tests with plain dicts + a fixed NOW).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

import jsonschema
from flask import request
from flask_restx import Namespace, Resource, fields
from mewbo_core.common import get_logger
from mewbo_core.triggers.spec import TriggerSpec
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mewbo_api.auth.guard_registry import guard
from mewbo_api.responses import ApiResponseKit

from .models import AppReadTokenScope, AppSpec, AppVersion, PipelineRun, PipelineSpec, WorkspaceRef

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Mapping, Sequence

    from mewbo_core.triggers.store import TriggerStoreBase

    from .lifecycle import AppLifecycle
    from .models import PipelineResult
    from .pipeline_tracker import AppPipelineRunTracker
    from .store import AppDataStoreBase, AppStoreBase, PipelineRunStoreBase
    from .tokens import AppReadTokenSigner

    class PipelineRunner(Protocol):
        """The runner collaborator invoked for a ``mode="code"`` pipeline.

        Structural Protocol (duck-typed) — NOT an import of the concrete
        ``AppPipelineRunner`` from ``pipeline_runner.py``, which was built
        independently; any object with this shape satisfies it, real or fake.
        ``PipelineResult`` (the actual return type, now stable) IS imported
        directly from ``models.py`` since ``AppPipelineRunTracker.record_code_run``
        (``pipeline_tracker.py``) also returns it — both execution paths this
        controller can take need to unify on the SAME concrete type. Injected as
        an optional field (the ``AppRunStarter`` idiom): unwired ``None``
        degrades every invoke to a clean 503, never a crash.
        """

        def execute(
            self,
            app: AppSpec,
            pipeline: PipelineSpec,
            params: dict[str, Any],
            *,
            now: datetime,
            dry_run: bool = False,
        ) -> PipelineResult: ...

logging = get_logger(name="api.apps.routes")

# A zero-arg guard (``None`` = allowed) and the factory that builds one per
# permission id — the same shape ``require_api_key`` already has, so the two
# compose at a call site instead of one wrapping the other.
AuthGuard = Callable[[], "tuple[dict, int] | None"]
PermissionGuardFactory = Callable[[str], AuthGuard]


def _no_permission(_permission: str) -> AuthGuard:
    """Placeholder factory used until the composition root injects the real one."""
    return lambda: None

apps_ns = Namespace("apps", description="Mewbo Apps — LLM-built, trigger-maintained mini apps.")

# One shared error-doc kit for the namespace (DRY error half of every op).
kit = ApiResponseKit(apps_ns, prefix="Apps")

# Data-query result cap (defensive upper bound on ``?limit=``).
_MAX_DATA_LIMIT = 500
_DEFAULT_DATA_LIMIT = 100
# Cap on the runs array folded into the consolidated /system payload.
_SYSTEM_RUNS_LIMIT = 20

# The AppSummary projection the gallery card needs (spec: lean, NO frontend).
_SUMMARY_KEYS = (
    "app_id",
    "title",
    "summary",
    "icon",
    "status",
    "version",
    "workspace_ref",
    "created_at",
    "updated_at",
)


# ---------------------------------------------------------------------------
# Request bodies — Pydantic, extra="forbid" (a smuggled server-owned field 400s)
# ---------------------------------------------------------------------------


class AppCreateRequest(BaseModel):
    """Body for ``POST /apps`` — the creation intent + optional workspace choice."""

    model_config = ConfigDict(extra="forbid")

    intent: str = Field(min_length=1)
    workspace: WorkspaceRef | None = None


class AppPatchRequest(BaseModel):
    """Body for ``PATCH /apps/<id>`` — only presentation fields are editable here."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    summary: str | None = None
    icon: str | None = None


class AppRollbackRequest(BaseModel):
    """Body for ``POST /apps/<id>/rollback`` — the version to repoint to."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)


class AppRearmRequest(BaseModel):
    """Body for ``POST /apps/<id>/rearm`` — optional post-arm seed (operator repair).

    ``extra="forbid"`` so a smuggled field (e.g. a client trying to target one
    pipeline, which this endpoint deliberately does not support) is a clean 400,
    not a silent no-op.
    """

    model_config = ConfigDict(extra="forbid")

    seed: bool = False


class AppTokenMintRequest(BaseModel):
    """Body for ``POST /apps/<id>/token`` — optional write-scope elevation.

    ``scope="write"`` additionally requires the LITERAL master token at the
    controller (see :meth:`AppsRoutesController.mint_token`) — an issued key
    may mint a read token but never a write one.
    """

    model_config = ConfigDict(extra="forbid")

    scope: AppReadTokenScope = "read"


# ---------------------------------------------------------------------------
# Controller — atomic class owning collaborators + every domain helper
# ---------------------------------------------------------------------------


class AppsRoutesController:
    """Owns the Mewbo Apps REST behavior over its injected collaborators.

    Atomic feature class (the ``TriggerRoutesController`` idiom): the stores,
    lifecycle, trigger store, token signer, auth guard and clock are its state;
    serialization + the CRUD/data/system/management operations are its methods.
    One instance is built by :func:`init_apps_routes` and DI'd into every
    Resource — no module-level mutable wiring.
    """

    # TriggerSpec's common fields stay top-level in the DTO; everything a concrete
    # kind adds folds into ``args`` (the frozen ``TriggerDTO`` shape the console +
    # SDK consume — matches ``TriggerRoutesController._dto``).
    _TRIGGER_BASE_FIELDS: frozenset[str] = frozenset(TriggerSpec.model_fields)

    # A manual-fire refusal reason -> its HTTP status. The tracker owns the domain
    # vocabulary (``FireRefusal``); the HTTP mapping is an route concern and lives
    # here alone (KISS: one owner per concern). An unknown reason maps to 502.
    _FIRE_REFUSAL_STATUS: dict[str, int] = {
        "not_configured": 503,
        "not_live": 409,
        "no_maintainer": 409,
        "already_running": 409,
        "wake_refused": 409,
        "cooldown": 429,
        "code_failed": 502,
    }

    def __init__(
        self,
        *,
        lifecycle: AppLifecycle,
        app_store: AppStoreBase,
        run_store: PipelineRunStoreBase,
        data_store: AppDataStoreBase,
        trigger_store: TriggerStoreBase,
        token_signer: AppReadTokenSigner,
        require_api_key: Callable[[], tuple[dict, int] | None],
        require_master_token: Callable[[], tuple[dict, int] | None],
        require_permission: PermissionGuardFactory = _no_permission,
        now_fn: Callable[[], datetime] | None = None,
        sdk_files: dict[str, str] | None = None,
        runner: PipelineRunner | None = None,
        tracker: AppPipelineRunTracker | None = None,
    ) -> None:
        """Capture the injected collaborators as instance state.

        *sdk_files* is the agent SDK injected into a rendered app's ``frontend.files``
        at serialization (``{"mewbo_app.py": <source>}``, read once from the plugin
        dir at startup — see :func:`init_apps_routes`). It is delivered ONLY on the
        rendered detail response; the STORED :class:`AppSpec` stays SDK-free so the
        version history is never polluted. Empty (the default) = no injection.

        *require_master_token* gates minting a WRITE-scoped app token (an issued
        key may mint read, never write — see :meth:`mint_token`); it is a
        SEPARATE guard from *require_api_key* (which any valid key satisfies),
        mirroring ``backend.py``'s own ``_require_api_key`` /
        ``_require_master_token`` split for key-management routes.

        *runner* is the ``mode="code"`` pipeline-execution collaborator
        (Phase 2, the ``AppRunStarter`` idiom): ``None`` (the default) makes
        every invoke degrade to a clean 503 "pipeline execution not configured"
        rather than a crash, so this controller works before the runner lands.

        *tracker* (Phase 2 revised ruling) is the ledger-writer: when wired,
        :meth:`invoke_pipeline` routes execution through
        ``AppPipelineRunTracker.record_code_run(kind="on_request",
        dispatch_failure=False, require_effect=True)`` so an invoke that actually
        wrote data (or genuinely failed) is real provenance in the ``PipelineRun``
        ledger, while a cache hit or a read-only render mints no row — the
        anti-spam line for a polling client. ``None`` (the default) falls back to
        calling :attr:`runner` directly with no ledger write at all, so this
        controller degrades gracefully before the tracker is wired (and every
        existing fake-runner-only route test keeps working unchanged).
        """
        self.lifecycle = lifecycle
        self.app_store = app_store
        self.run_store = run_store
        self.data_store = data_store
        self.trigger_store = trigger_store
        self.token_signer = token_signer
        self.require_api_key = require_api_key
        self.require_master_token = require_master_token
        self.require_permission = require_permission
        self.now_fn = now_fn or self._utcnow
        self.sdk_files = sdk_files or {}
        self.runner = runner
        self.tracker = tracker

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)

    # -- serialization + error helpers -------------------------------------

    @staticmethod
    def _error(message: str, status: int) -> tuple[dict, int]:
        """Error body carrying a top-level ``message`` (the shape the console reads).

        The console's ``readJson`` surfaces ``data.message``; this is the legacy
        ``message`` wire-shape ``ApiResponseKit`` documents as ``shape="message"``,
        used verbatim by the ``agentic_search`` routes.
        """
        return {"message": message}, status

    def _not_found(self) -> tuple[dict, int]:
        return self._error("app not found", 404)

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    def _load_app(self, app_id: str) -> AppSpec | None:
        return self.app_store.get(app_id)

    @staticmethod
    def _summary(spec: AppSpec) -> dict[str, Any]:
        """Project an :class:`AppSpec` to the lean gallery-card ``AppSummary``."""
        dump = spec.model_dump(mode="json")
        return {k: dump[k] for k in _SUMMARY_KEYS}

    def _detail(self, spec: AppSpec) -> dict[str, Any]:
        """The ``AppDetail`` envelope: full spec + version history, NEWEST first.

        The top-level ``spec`` is the RENDERED manifest — the injected SDK
        (:attr:`sdk_files`) is folded into its ``frontend.files`` so the served
        stlite bundle can ``import mewbo_app`` (the one server-side delivery seam,
        spec §2.5 "the SDK is the only sanctioned network path"). The ``versions``
        snapshots are left verbatim (SDK-free) — they are history, never rendered,
        and polluting them would drift every stored version.

        ``list_versions`` is oldest-first; the console renders the version list
        newest-first (latest at the top), so reverse it here.
        """
        versions = self.app_store.list_versions(spec.app_id)
        return {
            "spec": self._render_spec(spec),
            "versions": [self._render_version(v) for v in reversed(versions)],
        }

    def _render_spec(self, spec: AppSpec) -> dict[str, Any]:
        """Dump *spec* for rendering: strip server-only pipeline source, inject the SDK.

        Operates on the fresh ``model_dump`` (never the stored model). Two edits
        to ``frontend.files``, in order:

        1. **Strip pipeline source** — delegate to ``AppSpec.served_frontend_files()``
           (strategy-on-model: the model owns the predicate, this seam never
           re-derives it). A ``mode="code"`` ``entrypoint`` (and anything under the
           conventional ``pipelines/`` dir) is the ENGINE's input — it executes
           server-side via ``AppPipelineRunner``, never in stlite/pyodide — so it
           must not reach the browser's file map. The STORED :class:`AppSpec` and
           every ``versions[]`` snapshot keep it (the runner reads them).
        2. **Inject the SDK** (``sdk_files``, wins a name collision) so the served
           bundle can ``import mewbo_app``. A controller with no ``sdk_files``
           skips this half but still strips (1).
        """
        dump = spec.model_dump(mode="json")
        frontend = dump.get("frontend")
        if not (isinstance(frontend, dict) and isinstance(frontend.get("files"), dict)):
            return dump
        served = spec.served_frontend_files()
        if self.sdk_files:
            served = {**served, **self.sdk_files}
        frontend["files"] = served
        return dump

    @staticmethod
    def _render_version(version: AppVersion) -> dict[str, Any]:
        """Dump one ``versions[]`` snapshot with pipeline source STRIPPED (no SDK).

        A version snapshot is history the console renders read-only — it must not
        leak the exact ``mode="code"`` source the live strip withholds from the
        top-level spec, so the SAME ``AppSpec.served_frontend_files()`` projection
        applies here (the security strip is unconditional; the STORE keeps every
        file for the runner). The SDK is a live-render concern — opt-in and
        upgradable without a version bump — so it is NOT folded into history.
        """
        dump = version.model_dump(mode="json")
        spec_dump = dump.get("spec")
        if isinstance(spec_dump, dict):
            frontend = spec_dump.get("frontend")
            if isinstance(frontend, dict) and isinstance(frontend.get("files"), dict):
                frontend["files"] = version.spec.served_frontend_files()
        return dump

    def _trigger_dto(self, trigger: TriggerSpec, *, now: datetime) -> dict[str, Any]:
        """Serialize a trigger to the frozen ``TriggerDTO`` (base fields + ``args``).

        Mirrors ``TriggerRoutesController._dto``: the base fields stay top-level,
        every kind-specific field folds into ``args`` (webhook ``secret``
        stripped), plus a computed ``next_fire_at``.
        """
        dump = trigger.model_dump(mode="json")
        args = {k: v for k, v in dump.items() if k not in self._TRIGGER_BASE_FIELDS}
        args.pop("secret", None)
        out = {k: v for k, v in dump.items() if k in self._TRIGGER_BASE_FIELDS}
        out["args"] = args
        out["next_fire_at"] = self._iso(trigger.next_fire_at(now))
        return out

    @staticmethod
    def _run_dto(run: PipelineRun, *, declared_collections: Sequence[str]) -> dict[str, Any]:
        """Dump a :class:`PipelineRun` plus its derived honesty flags.

        Neither is a new stored field (no new :data:`PipelineRunStatus` member) —
        a ``succeeded`` run that recorded zero writes at all, or one that wrote
        SOME declared collections but silently missed another, is otherwise
        indistinguishable from a fully successful run. Both come off the
        model-owned :attr:`PipelineRun.wrote_nothing` /
        :meth:`PipelineRun.unwritten_collections`, folded onto the wire row here,
        additively. ``declared_collections`` is the app's, not the run's — the
        one caller (``system_health``) already holds the :class:`AppSpec`.
        """
        return {
            **run.model_dump(mode="json"),
            "wrote_nothing": run.wrote_nothing,
            "unwritten_collections": run.unwritten_collections(declared_collections),
        }

    def _maintainer_triggers(self, app: AppSpec) -> list[TriggerSpec]:
        """Every trigger owned by the app's maintainer session (empty if none).

        Delegates to :meth:`AppLifecycle.maintainer_triggers` — the ONE place that
        query lives — rather than re-deriving it against ``self.trigger_store``.
        """
        return self.lifecycle.maintainer_triggers(app)

    def _freshness(
        self,
        triggers: list[TriggerSpec],
        runs: list[PipelineRun],
        *,
        now: datetime,
        declared_collections: Sequence[str],
    ) -> dict[str, Any]:
        """The ``AppFreshnessWire`` sub-object (spec §2.8), derived from the ledger."""
        last_run = runs[0] if runs else None
        last_success = next((r for r in runs if r.status == "succeeded"), None)
        upcoming = [
            ft
            for ft in (t.next_fire_at(now) for t in triggers if t.status == "armed")
            if ft is not None
        ]
        return {
            "last_success_at": self._iso(last_success.ended_at) if last_success else None,
            "last_run_status": last_run.status if last_run else None,
            "next_fire_at": self._iso(min(upcoming)) if upcoming else None,
            # Honest: green only when the MOST RECENT run succeeded; a failed or
            # stuck latest run on a data-bearing app reads stale, never false-green.
            "stale": last_run is not None and last_run.status != "succeeded",
            # The predicate `stale` alone misses: a run can succeed (stale=False)
            # while a declared collection every frontend page reads never
            # receives a document — the verified incident this field exists for.
            # Scoped to the MOST RECENT run, same "latest state" scope as every
            # other field here (never a window union — an older run writing a
            # collection the current one skips is a real gap worth flagging
            # again, not history papering over it). Empty when there is no run
            # yet (nothing to report beyond a bare "never refreshed") or the
            # latest run didn't succeed (`unwritten_collections` itself gates on
            # that, matching `wrote_nothing`).
            "unwritten_collections": (
                last_run.unwritten_collections(declared_collections) if last_run else []
            ),
        }

    @staticmethod
    def _pipeline_mode(pipeline: PipelineSpec) -> str:
        """The pipeline's declared execution mode (Phase 2, additive on the model).

        Read defensively via ``getattr`` — ``mode`` lands on :class:`PipelineSpec`
        alongside the materialized-pipeline runner, built concurrently — so this
        controller works against both the pre- and post-field model shape. A
        pipeline with no declared ``mode`` has exactly the shape every legacy
        pipeline has: a ``wake_prompt`` that re-engages the maintainer, i.e.
        ``"agentic"`` — never invocable synchronously (see :meth:`invoke_pipeline`).
        """
        return getattr(pipeline, "mode", "agentic")

    @staticmethod
    def _find_pipeline(app: AppSpec, name: str) -> PipelineSpec | None:
        return next((p for p in app.pipelines if p.name == name), None)

    @staticmethod
    def _pipeline_rows(
        pipelines: list[PipelineSpec], triggers: list[TriggerSpec]
    ) -> list[dict[str, Any]]:
        """The declared per-pipeline tier the clients render (Phase 1).

        Each row carries the DECLARED ``schedule`` union (or ``null``), the
        ``on_demand`` flag, the platform-stamped ``trigger_ref``, whether that
        ref is currently ARMED on the maintainer, and the execution ``mode``
        (Phase 2, additive) — so a client renders "refreshes hourly" vs
        "on-demand" vs the unscheduled warning, plus whether the pipeline is
        synchronously invocable. Additive: the top-level ``unscheduled_pipelines``
        field is unchanged (locked with the console).
        """
        armed_ids = {t.id for t in triggers if t.status == "armed"}
        return [
            {
                "name": p.name,
                "mode": AppsRoutesController._pipeline_mode(p),
                "schedule": p.schedule.model_dump(mode="json") if p.schedule is not None else None,
                "on_demand": p.on_demand,
                "trigger_ref": p.trigger_ref,
                "armed": p.trigger_ref is not None and p.trigger_ref in armed_ids,
            }
            for p in pipelines
        ]

    @staticmethod
    def _unscheduled_pipelines(
        triggers: list[TriggerSpec], pipelines: list[PipelineSpec]
    ) -> list[str]:
        """Names of pipelines whose ``trigger_ref`` resolves to nothing armed here.

        A TOP-LEVEL ``/system`` field (a peer of ``freshness``/``triggers``/``runs``/
        ``maintainer`` — the console's landed type expects it there, not nested),
        additive. Catches both the plain on-demand shape (``trigger_ref`` left
        ``None``) and the subtler gap — a policy-capped/expired/stale ref pointing
        at a trigger that isn't armed on THIS maintainer (see
        ``AppLifecycle._warn_on_unscheduled_pipelines``, the submit-time sibling of
        this same derivation).
        """
        armed_ids = {t.id for t in triggers if t.status == "armed"}
        return [p.name for p in pipelines if p.trigger_ref not in armed_ids]

    @staticmethod
    def _maintainer(app: AppSpec, triggers: list[TriggerSpec]) -> dict[str, Any]:
        """The ``maintainer`` sub-object: session id + a liveness derived from triggers."""
        if app.maintainer_session_id is None:
            return {"session_id": None, "status": None}
        if any(t.status == "armed" for t in triggers):
            status: str | None = "active"
        elif any(t.status == "paused" for t in triggers):
            status = "paused"
        else:
            status = "idle"
        return {"session_id": app.maintainer_session_id, "status": status}

    # -- authorization (master key OR app-scoped token) ---------------------

    def _authorize(
        self,
        app_id: str,
        *,
        credential_ok: bool,
        app_token: str | None,
        required_scope: AppReadTokenScope | None,
    ) -> tuple[dict, int] | None:
        """Shared token-check core for :meth:`authorize_read` / :meth:`authorize_write`.

        Returns ``None`` when allowed, else the error tuple. A valid master /
        issued key (``credential_ok``) always passes regardless of *required_scope*
        — the platform's own health pane and management routes use it. Otherwise
        the render token (its ``token_id``) is required; it must be authentic,
        unexpired, scoped to *app_id* (a valid token for another app is 403 — no
        cross-app access), and — when *required_scope* is given — carry that
        scope (a ``read`` token on a write-gated call is 403, never a silent
        downgrade).
        """
        if credential_ok:
            return None
        if not app_token:
            return self._error("authentication required", 401)
        token = self.token_signer.verify(app_token, now=self.now_fn())
        if token is None:
            return self._error("invalid or expired app token", 401)
        if token.app_id != app_id:
            return self._error("app token does not grant access to this app", 403)
        if required_scope is not None and token.scope != required_scope:
            return self._error(f"app token does not grant {required_scope} access", 403)
        return None

    def authorize_read(
        self, app_id: str, *, credential_ok: bool, app_token: str | None
    ) -> tuple[dict, int] | None:
        """Authorize a read-only data/system/pipeline-list/invoke-GET request.

        Any valid scope (``read`` or ``write``) passes — read is the floor every
        token carries.
        """
        return self._authorize(
            app_id, credential_ok=credential_ok, app_token=app_token, required_scope=None
        )

    def authorize_write(
        self, app_id: str, *, credential_ok: bool, app_token: str | None
    ) -> tuple[dict, int] | None:
        """Authorize a write-gated pipeline invocation (``POST .../pipelines/<name>``).

        Requires a ``write``-scoped token specifically — a valid ``read`` token
        is 403, never silently accepted (see :meth:`mint_token` for how a client
        obtains one).
        """
        return self._authorize(
            app_id, credential_ok=credential_ok, app_token=app_token, required_scope="write"
        )

    # -- gallery + CRUD ----------------------------------------------------

    def list_apps(self) -> tuple[dict, int]:
        """List the gallery as lean ``AppSummary`` cards (archived hidden)."""
        return {"apps": [self._summary(a) for a in self.app_store.list_apps()]}, 200

    def create_app(self, body: Any) -> tuple[dict, int]:
        """Create a draft app + its builder session; return ``{app_id, session_id}``."""
        if not isinstance(body, dict):
            return self._error("request body must be a JSON object", 400)
        try:
            data = AppCreateRequest.model_validate(body)
        except ValidationError as exc:
            return self._error(str(exc), 400)
        workspace = data.workspace or WorkspaceRef(kind="own", key="default")
        spec = self.lifecycle.create_draft(data.intent, workspace)
        return {"app_id": spec.app_id, "session_id": spec.owner_session_id}, 201

    def get_app(self, app_id: str) -> tuple[dict, int]:
        """Return the ``AppDetail`` (manifest + version history)."""
        app = self._load_app(app_id)
        if app is None:
            return self._not_found()
        return self._detail(app), 200

    def patch_app(self, app_id: str, body: Any) -> tuple[dict, int]:
        """Edit an app's title / summary / icon (presentation only); return ``AppDetail``."""
        app = self._load_app(app_id)
        if app is None:
            return self._not_found()
        if not isinstance(body, dict):
            return self._error("request body must be a JSON object", 400)
        try:
            patch = AppPatchRequest.model_validate(body)
        except ValidationError as exc:
            return self._error(str(exc), 400)
        updates = patch.model_dump(exclude_none=True)
        if updates:
            updates["updated_at"] = self.now_fn()
            app = app.model_copy(update=updates)
            self.app_store.save(app)
        return self._detail(app), 200

    def archive_app(self, app_id: str) -> tuple[dict, int]:
        """Archive an app (soft — archived is absorbing); return the ``AppSpec``."""
        app = self.lifecycle.archive(app_id)
        if app is None:
            return self._not_found()
        return app.model_dump(mode="json"), 200

    # -- read-only data + system introspection -----------------------------

    def read_data(
        self,
        app_id: str,
        collection: str,
        *,
        filter: dict[str, Any] | None,
        sort: str | None,
        limit: int,
    ) -> tuple[dict, int]:
        """Query one collection; ``documents`` are full :class:`AppDataDoc` envelopes.

        The console's ``AppDataDoc`` type carries the envelope, and the injected
        SDK's ``data.query`` unwraps ``row["doc"]`` — so returning the envelope
        (``{app_id, collection, key, doc, updated_at}``) rather than a bare body
        is the unambiguous contract (a body that itself held a ``doc`` dict field
        would otherwise trip the SDK's tolerant fallback).
        """
        if self._load_app(app_id) is None:
            return self._not_found()
        docs = self.data_store.query(
            app_id, collection, filter=filter, limit=limit, sort=sort
        )
        documents = [d.model_dump(mode="json") for d in docs]
        return {"collection": collection, "documents": documents}, 200

    def system_health(self, app_id: str) -> tuple[dict, int]:
        """Consolidated read-only introspection (freshness + triggers + runs + maintainer)."""
        app = self._load_app(app_id)
        if app is None:
            return self._not_found()
        now = self.now_fn()
        triggers = self._maintainer_triggers(app)
        runs = self.run_store.list_runs(app_id, limit=_SYSTEM_RUNS_LIMIT)
        declared_collections = [c.name for c in app.collections]
        return {
            "app_id": app_id,
            "status": app.status,
            "freshness": self._freshness(
                triggers, runs, now=now, declared_collections=declared_collections
            ),
            "triggers": [self._trigger_dto(t, now=now) for t in triggers],
            "runs": [self._run_dto(r, declared_collections=declared_collections) for r in runs],
            "maintainer": self._maintainer(app, triggers),
            "pipelines": self._pipeline_rows(app.pipelines, triggers),
            "unscheduled_pipelines": self._unscheduled_pipelines(triggers, app.pipelines),
        }, 200

    # -- pipeline surface + invocation (Phase 2) -----------------------

    def list_pipelines(self, app_id: str) -> tuple[dict, int]:
        """List the app's declared pipeline surface (read-auth, like ``/system``).

        One row per pipeline: ``name``, ``mode``, ``on_demand``, the declared
        ``schedule`` union, whether it is currently ``armed``, and the
        ``params_schema``/``cache_ttl_seconds`` an invoker needs (both additive
        on :class:`PipelineSpec`, read defensively — see :meth:`_pipeline_mode`).
        """
        app = self._load_app(app_id)
        if app is None:
            return self._not_found()
        triggers = self._maintainer_triggers(app)
        armed_ids = {t.id for t in triggers if t.status == "armed"}
        rows = [
            {
                "name": p.name,
                "mode": self._pipeline_mode(p),
                "on_demand": p.on_demand,
                "schedule": p.schedule.model_dump(mode="json") if p.schedule is not None else None,
                "armed": p.trigger_ref is not None and p.trigger_ref in armed_ids,
                "params_schema": getattr(p, "params_schema", None),
                "cache_ttl_seconds": getattr(p, "cache_ttl_seconds", 0),
            }
            for p in app.pipelines
        ]
        return {"pipelines": rows}, 200

    @staticmethod
    def _coerce_query_value(raw: str, json_type: str | None) -> Any:
        """Minimal string -> JSON-type coercion for one query-string value.

        Only the scalar types a query string can meaningfully carry; anything
        else (``string``/``array``/``object``/undeclared) passes through
        verbatim and is left to :meth:`_validate_pipeline_params`'s jsonschema
        pass to judge.
        """
        if json_type == "integer":
            return int(raw)
        if json_type == "number":
            return float(raw)
        if json_type == "boolean":
            low = raw.strip().lower()
            if low in ("true", "1"):
                return True
            if low in ("false", "0"):
                return False
            raise ValueError(f"not a boolean: {raw!r}")
        return raw

    def _coerce_query_params(
        self, raw_params: Mapping[str, str], schema: dict[str, Any] | None
    ) -> tuple[dict[str, Any], str | None]:
        """Coerce query-string *raw_params* against *schema*'s declared property types.

        Every key must be declared in ``schema["properties"]`` — there is
        nothing else to coerce a bare query-string value against. A missing or
        malformed *schema* makes ANY supplied param "unknown" (closed-world: a
        pipeline that declares no ``params_schema`` accepts no params at all).
        """
        if not raw_params:
            return {}, None
        properties = schema.get("properties") if isinstance(schema, dict) else None
        if not isinstance(properties, dict):
            return {}, (
                f"unknown parameter(s): {', '.join(sorted(raw_params))} "
                "(pipeline declares no params_schema)"
            )
        out: dict[str, Any] = {}
        for key, raw_value in raw_params.items():
            prop = properties.get(key)
            if prop is None:
                return {}, f"unknown parameter: {key!r}"
            json_type = prop.get("type") if isinstance(prop, dict) else None
            try:
                out[key] = self._coerce_query_value(raw_value, json_type)
            except ValueError:
                return {}, f"parameter {key!r} is not a valid {json_type}"
        return out, None

    @staticmethod
    def _validate_pipeline_params(
        params: dict[str, Any], schema: dict[str, Any] | None
    ) -> str | None:
        """Validate *params* (already typed) against a pipeline's ``params_schema``.

        Closed-world on keys regardless of the schema's own
        ``additionalProperties`` (every key must be a declared property, or a
        pipeline with no usable schema accepts no params), then a full
        ``jsonschema`` pass for type/required/enum/range checks — mirrors
        ``CollectionSpec.validate_doc``'s use of the same library for the same
        reason: the schema is per-pipeline DATA, not a static Python type.
        Returns an error string, or ``None`` when *params* is valid.

        NOTE: an empty *params* is NOT unconditionally valid — only when there
        is no schema to satisfy. A schema with a ``required`` property must
        still reject an empty dict (via the ``jsonschema.validate`` pass
        below), so the "nothing to check" short-circuit is scoped to the
        no-schema branch only.
        """
        properties = schema.get("properties") if isinstance(schema, dict) else None
        if not isinstance(properties, dict):
            if not params:
                return None
            return (
                f"unknown parameter(s): {', '.join(sorted(params))} "
                "(pipeline declares no params_schema)"
            )
        unknown = sorted(k for k in params if k not in properties)
        if unknown:
            return f"unknown parameter(s): {', '.join(unknown)}"
        try:
            jsonschema.validate(instance=params, schema=schema)
        except jsonschema.ValidationError as exc:
            return f"invalid params: {exc.message}"
        return None

    def invoke_pipeline(
        self,
        app_id: str,
        name: str,
        *,
        raw_query_params: Mapping[str, str] | None = None,
        json_body: Any = None,
    ) -> tuple[dict, int]:
        """Invoke a ``mode="code"`` pipeline.

        Shared by the GET (query params, read-auth) and POST (JSON body,
        write-auth) verbs; exactly one of *raw_query_params* / *json_body* is
        passed by the caller.

        A ``mode="agentic"`` pipeline (the default for every pre-Phase-2
        pipeline — see :meth:`_pipeline_mode`) 409s: it runs on its own schedule
        or via its maintainer, never synchronously. An unwired :attr:`runner`
        (``None``, the default before the materialized-pipeline runner lands)
        503s. A runner/tracker exception surfaces as a clean 502 with
        ``{message}`` — never a traceback body. A TRACKED failure (the tracker is
        wired and the runner raised) maps to 502 from ``run.error`` instead.

        **Per-pipeline write gate (POST only).** The POST/form path (*json_body*)
        additionally requires the TARGET pipeline itself declare ``user_writable``
        — the write-scoped token is app-scoped, so this is the per-pipeline
        least-privilege line: a write credential invokes a form pipeline, never an
        arbitrary effectful one on the same app. It also collapses the stale-token
        risk without extra bookkeeping: a pipeline despec'd to ``user_writable:
        false`` now 403s here (removed entirely already 404'd above). The GET path
        (*raw_query_params*) stays open to ANY code pipeline including effectful
        ones — a served app refreshing itself is deliberate.

        When :attr:`tracker` is wired, execution routes through
        ``record_code_run(kind="on_request", dispatch_failure=False,
        require_effect=True)`` (Phase 2 revised ruling): a run that wrote
        data or genuinely failed is ledgered; a cache hit or a no-write success
        mints no row. :attr:`runner` alone (no tracker) still works — no ledger,
        matching every existing fake-runner-only test.
        """
        app = self._load_app(app_id)
        if app is None:
            return self._not_found()
        pipeline = self._find_pipeline(app, name)
        if pipeline is None:
            return self._error(f"pipeline {name!r} not found", 404)
        if self._pipeline_mode(pipeline) == "agentic":
            return self._error(
                "agentic pipeline — runs on its schedule or via its maintainer; "
                "not invocable synchronously",
                409,
            )
        # POST/form path: the pipeline itself must be user_writable (the write token
        # is app-scoped, so this per-pipeline gate stops it reaching sibling
        # effectful pipelines; a despec'd pipeline flips to 403 here). GET is exempt.
        is_write_path = raw_query_params is None
        if is_write_path and not getattr(pipeline, "user_writable", False):
            return self._error(
                f"pipeline {name!r} is not user-writable — the POST/form path requires "
                "user_writable=true; invoke it read-only via GET instead",
                403,
            )
        schema = getattr(pipeline, "params_schema", None)
        if raw_query_params is not None:
            params, error = self._coerce_query_params(raw_query_params, schema)
            if error is None:
                error = self._validate_pipeline_params(params, schema)
        else:
            if json_body is not None and not isinstance(json_body, dict):
                return self._error("request body must be a JSON object", 400)
            params = json_body or {}
            error = self._validate_pipeline_params(params, schema)
        if error is not None:
            return self._error(error, 400)
        if self.runner is None:
            return self._error("pipeline execution not configured", 503)
        try:
            if self.tracker is not None:
                # kind="on_request": this IS the on-demand invoke seam.
                # dispatch_failure=False: a user-triggered failure must never
                # auto-repair/pause the app — only an autonomous scheduled fire
                # does that. require_effect=True: only a run that wrote data (or
                # genuinely failed) is ledgered; a cache hit or a no-write success
                # mints no row (the anti-spam line for a polling client).
                run, result = self.tracker.record_code_run(
                    app,
                    pipeline,
                    params=params,
                    kind="on_request",
                    dispatch_failure=False,
                    require_effect=True,
                    now=self.now_fn(),
                )
                if result is None:
                    return self._error(run.error or "pipeline execution failed", 502)
            else:
                result = self.runner.execute(app, pipeline, params, now=self.now_fn())
        except Exception as exc:  # noqa: BLE001 - runner failures surface as a clean 502, never a traceback
            return self._error(str(exc), 502)
        return {
            "output": result.output,
            "evaluated_at": self._iso(result.evaluated_at),
            "cache": result.cache,
            "docs_written": dict(result.docs_written),
            "pipeline": name,
        }, 200

    def fire_pipeline(self, app_id: str, name: str) -> tuple[dict, int]:
        """On-demand refresh of a pipeline — BOTH modes (``POST .../pipelines/<name>/fire``).

        Read-auth (a served app may refresh itself, mirroring the GET invoke). The
        two tiers report differently, matching the seed/schedule reality:

        * ``mode="code"`` runs the engine synchronously and always ledgers a
          ``kind="on_request"`` row → 200 ``{pipeline, mode:"code",
          status:"succeeded", cache, docs_written, evaluated_at}``; a runner failure
          is ledgered (never auto-repairs) and surfaces as 502 ``{message}``.
        * ``mode="agentic"`` opens a ledger row and wakes the maintainer with the
          pipeline's ``wake_prompt`` → 202 ``{pipeline, mode:"agentic", status}``
          (``status`` is the landed signal — ``started``/``steered``). Guards:
          409 (app not live / no maintainer / a run already open) and 429
          ``{message, retry_after_seconds}`` (fired within the cooldown).

        Unknown app/pipeline → 404. An unwired fire seam (no tracker, or the mode's
        collaborator isn't wired) → 503. The refusal→status map is
        :attr:`_FIRE_REFUSAL_STATUS`.
        """
        app = self._load_app(app_id)
        if app is None:
            return self._not_found()
        pipeline = self._find_pipeline(app, name)
        if pipeline is None:
            return self._error(f"pipeline {name!r} not found", 404)
        if self.tracker is None:
            return self._error("pipeline fire not configured", 503)
        outcome = self.tracker.fire_pipeline(app, pipeline, now=self.now_fn())
        if not outcome.ok:
            status = self._FIRE_REFUSAL_STATUS.get(outcome.refusal or "", 502)
            body: dict[str, Any] = {"message": outcome.message or "pipeline fire failed"}
            if outcome.retry_after_seconds is not None:
                body["retry_after_seconds"] = outcome.retry_after_seconds
            return body, status
        if outcome.mode == "code" and outcome.result is not None:
            result = outcome.result
            return {
                "pipeline": name,
                "mode": "code",
                "status": "succeeded",
                "cache": result.cache,
                "docs_written": dict(result.docs_written),
                "evaluated_at": self._iso(result.evaluated_at),
            }, 200
        return {
            "pipeline": name,
            "mode": "agentic",
            "status": outcome.landed or "started",
        }, 202

    # -- management --------------------------------------------------------

    def rearm_app(self, app_id: str, body: Any) -> tuple[dict, int]:
        """Re-mint a live app's missing/dead schedule triggers; optionally seed them.

        Operator repair (master/issued key only — no app tokens; the route enforces
        the credential). The app must be ``live`` (a paused/broken/archived app
        would re-arm triggers it just cancelled — 409). Delegates the arming +
        optional seed to :meth:`AppLifecycle.rearm` and returns its
        ``{armed, unchanged, seeded}`` summary.
        """
        app = self._load_app(app_id)
        if app is None:
            return self._not_found()
        if not isinstance(body, dict):
            return self._error("request body must be a JSON object", 400)
        try:
            data = AppRearmRequest.model_validate(body)
        except ValidationError as exc:
            return self._error(str(exc), 400)
        if app.status != "live":
            return self._error(
                f"app is {app.status!r}, not live — re-arm applies only to a live app", 409
            )
        result = self.lifecycle.rearm(app, seed=data.seed, now=self.now_fn())
        return result, 200

    def mint_token(self, app_id: str, body: Any) -> tuple[dict, int]:
        """Mint an ``AppReadToken`` — ``scope="read"`` (default) or ``"write"``.

        Minting ``"write"`` is gated TWO ways, both structural least privilege:
        (1) the LITERAL master token (:attr:`require_master_token`, never an
        issued key — a leaked issued key must not escalate a served app's
        pipeline surface to invocable); and (2) the app must actually DECLARE a
        ``user_writable`` pipeline (else 403) — a write token exists only to let
        a frontend submit form params to a pipeline that opted in, so an app with
        none can never obtain one. The route already required
        ``require_api_key`` for read.
        """
        app = self._load_app(app_id)
        if app is None:
            return self._not_found()
        if not isinstance(body, dict):
            return self._error("request body must be a JSON object", 400)
        try:
            data = AppTokenMintRequest.model_validate(body)
        except ValidationError as exc:
            return self._error(str(exc), 400)
        if data.scope == "write":
            master_auth = self.require_master_token()
            if master_auth is not None:
                return master_auth
            if not app.has_user_writable_pipeline:
                return self._error(
                    "this app declares no user-writable pipeline; a write token "
                    "cannot be minted for it",
                    403,
                )
        token = self.token_signer.mint(app_id, now=self.now_fn(), scope=data.scope)
        return token.model_dump(mode="json"), 201

    def list_triggers(self, app_id: str) -> tuple[dict, int]:
        """List the app's triggers as ``TriggerDTO[]`` (management view)."""
        app = self._load_app(app_id)
        if app is None:
            return self._not_found()
        now = self.now_fn()
        triggers = self._maintainer_triggers(app)
        return {"triggers": [self._trigger_dto(t, now=now) for t in triggers]}, 200

    def pause_app(self, app_id: str) -> tuple[dict, int]:
        """Pause the app + its triggers; return the ``AppSpec``."""
        app = self.lifecycle.pause(app_id)
        if app is None:
            return self._not_found()
        return app.model_dump(mode="json"), 200

    def resume_app(self, app_id: str) -> tuple[dict, int]:
        """Resume the app + its triggers; return the ``AppSpec``."""
        app = self.lifecycle.resume(app_id)
        if app is None:
            return self._not_found()
        return app.model_dump(mode="json"), 200

    def rollback_app(self, app_id: str, body: Any) -> tuple[dict, int]:
        """Repoint the app to an earlier version's design; return the refreshed ``AppDetail``."""
        if self._load_app(app_id) is None:
            return self._not_found()
        if not isinstance(body, dict):
            return self._error("request body must be a JSON object", 400)
        try:
            data = AppRollbackRequest.model_validate(body)
        except ValidationError as exc:
            return self._error(str(exc), 400)
        app = self.lifecycle.rollback(app_id, data.version)
        if app is None:
            return self._error(f"version {data.version} not found", 404)
        return self._detail(app), 200


# ---------------------------------------------------------------------------
# Flask-RESTX doc models (example= drives the Scalar sample bodies)
# ---------------------------------------------------------------------------

app_spec_model = apps_ns.model(
    "AppSpec",
    {
        "app_id": fields.String(example="app-1a2b3c4d5e6f"),
        "title": fields.String(example="Inbox digest"),
        "summary": fields.String(example="Groups my unread email into tasks each morning."),
        "icon": fields.String(example="📥"),
        "status": fields.String(
            example="live",
            description="draft, building, live, paused, broken, or archived.",
        ),
        "version": fields.Integer(example=1),
        "owner_session_id": fields.String(example="9e2d47c1a0b34f12"),
        "maintainer_session_id": fields.String(example="7c1f0a84b2c39e2d"),
        "workspace_ref": fields.Raw(example={"kind": "own", "key": "default"}),
        "frontend": fields.Raw(example={"entrypoint": "app.py", "files": {"app.py": "..."}}),
        "created_at": fields.String(example="2026-07-17T09:00:00+00:00"),
        "updated_at": fields.String(example="2026-07-17T09:05:00+00:00"),
    },
)

app_summary_model = apps_ns.model(
    "AppSummary",
    {
        "app_id": fields.String(example="app-1a2b3c4d5e6f"),
        "title": fields.String(example="Inbox digest"),
        "summary": fields.String(example="Groups my unread email into tasks."),
        "icon": fields.String(example="📥"),
        "status": fields.String(example="live"),
        "version": fields.Integer(example=1),
        "workspace_ref": fields.Raw(example={"kind": "own", "key": "default"}),
        "created_at": fields.String(example="2026-07-17T09:00:00+00:00"),
        "updated_at": fields.String(example="2026-07-17T09:05:00+00:00"),
    },
)

apps_list_model = apps_ns.model(
    "AppList", {"apps": fields.List(fields.Nested(app_summary_model))}
)

app_version_model = apps_ns.model(
    "AppVersion",
    {
        "app_id": fields.String(example="app-1a2b3c4d5e6f"),
        "version": fields.Integer(example=1),
        "spec": fields.Nested(app_spec_model),
        "author": fields.String(example="builder"),
        "note": fields.String(example="app submitted"),
    },
)

app_detail_model = apps_ns.model(
    "AppDetail",
    {
        "spec": fields.Nested(app_spec_model),
        "versions": fields.List(fields.Nested(app_version_model)),
    },
)

app_create_model = apps_ns.model(
    "AppCreated",
    {
        "app_id": fields.String(example="app-1a2b3c4d5e6f"),
        "session_id": fields.String(
            example="9e2d47c1a0b34f12",
            description="The builder session to stream build progress from.",
        ),
    },
)

app_create_request = apps_ns.model(
    "AppCreateRequest",
    {
        "intent": fields.String(
            required=True,
            example="Summarize my unread email into a task list every morning.",
        ),
        "workspace": fields.Raw(
            example={"kind": "own", "key": "default"},
            description="Workspace binding; defaults to a private own-workspace.",
        ),
    },
)

app_patch_request = apps_ns.model(
    "AppPatchRequest",
    {
        "title": fields.String(example="Inbox digest"),
        "summary": fields.String(example="Groups my unread email into tasks."),
        "icon": fields.String(example="📥"),
    },
)

app_rollback_request = apps_ns.model(
    "AppRollbackRequest", {"version": fields.Integer(required=True, example=2)}
)

app_token_model = apps_ns.model(
    "AppReadToken",
    {
        "token_id": fields.String(
            example="app-1a2b3c4d5e6f:1752742800:read:9f3a...:AbC-...",
            description="The presented bearer credential (X-Mewbo-App-Token header).",
        ),
        "app_id": fields.String(example="app-1a2b3c4d5e6f"),
        "scope": fields.String(
            example="read", description="'read' (default) or 'write' (master-key-only to mint)."
        ),
        "expires_at": fields.String(example="2026-07-17T09:30:00+00:00"),
    },
)

app_token_mint_request = apps_ns.model(
    "AppTokenMintRequest",
    {
        "scope": fields.String(
            example="read",
            description=(
                "'read' (default) or 'write'. Minting 'write' requires the literal "
                "master token — an issued key may mint read only."
            ),
        ),
    },
)

app_trigger_model = apps_ns.model(
    "AppTriggerDTO",
    {
        "id": fields.String(example="3f8c1e9a2b7d4c05"),
        "session_id": fields.String(example="7c1f0a84b2c39e2d"),
        "kind": fields.String(example="time.cron"),
        "status": fields.String(example="armed"),
        "wake_prompt": fields.String(example="Ingest new email and regroup tasks."),
        "action": fields.String(example="message"),
        "args": fields.Raw(example={"cron": "0 9 * * *"}),
        "fires": fields.Integer(example=3),
        "max_fires": fields.Integer(example=None),
        "expires_at": fields.String(example="2026-07-24T09:00:00+00:00"),
        "next_fire_at": fields.String(example="2026-07-18T09:00:00+00:00"),
        "created_at": fields.String(example="2026-07-17T09:00:00+00:00"),
        "created_by": fields.String(example="user"),
        "last_fired_at": fields.String(example="2026-07-17T09:00:00+00:00"),
        "last_error": fields.String(example=None),
    },
)
app_triggers_model = apps_ns.model(
    "AppTriggers", {"triggers": fields.List(fields.Nested(app_trigger_model))}
)

app_run_model = apps_ns.model(
    "AppPipelineRun",
    {
        "run_key": fields.String(example="run-9f8e7d6c5b4a"),
        "app_id": fields.String(example="app-1a2b3c4d5e6f"),
        "pipeline_name": fields.String(example="ingest-email"),
        "status": fields.String(example="succeeded"),
        "docs_written": fields.Raw(example={"tasks": 4}),
        "started_at": fields.String(example="2026-07-17T09:00:00+00:00"),
        "ended_at": fields.String(example="2026-07-17T09:00:08+00:00"),
        "error": fields.String(example=None),
        "unwritten_collections": fields.List(
            fields.String,
            example=[],
            description=(
                "Declared collection names THIS run recorded zero writes to "
                "(empty unless the run succeeded and skipped one)."
            ),
        ),
    },
)

app_freshness_model = apps_ns.model(
    "AppFreshness",
    {
        "last_success_at": fields.String(example="2026-07-17T09:00:08+00:00"),
        "last_run_status": fields.String(example="succeeded"),
        "next_fire_at": fields.String(example="2026-07-18T09:00:00+00:00"),
        "stale": fields.Boolean(example=False),
        "unwritten_collections": fields.List(
            fields.String,
            example=[],
            description=(
                "Declared collection names the most recent run recorded zero "
                "writes to (empty when it wrote to all of them, or hasn't run)."
            ),
        ),
    },
)

app_pipeline_row_model = apps_ns.model(
    "AppPipelineRow",
    {
        "name": fields.String(example="morning-organize"),
        "mode": fields.String(
            example="agentic",
            description=(
                "'agentic' (wakes the maintainer; the legacy default) or "
                "'code' (synchronously invocable via GET/POST .../pipelines/<name>)."
            ),
        ),
        "schedule": fields.Raw(
            example={"kind": "time.cron", "cron": "0 7 * * *"},
            description="The declared schedule union (time.cron/time.at), or null for on-demand.",
        ),
        "on_demand": fields.Boolean(example=False),
        "trigger_ref": fields.String(
            example="3f8c1e9a2b7d4c05",
            description="Platform-stamped id of the armed trigger (null when unscheduled).",
        ),
        "armed": fields.Boolean(
            example=True, description="Whether trigger_ref is currently armed on the maintainer."
        ),
    },
)

app_pipeline_surface_row_model = apps_ns.model(
    "AppPipelineSurfaceRow",
    {
        "name": fields.String(example="weekly-report"),
        "mode": fields.String(
            example="code",
            description="'agentic' (not synchronously invocable) or 'code' (materialized).",
        ),
        "on_demand": fields.Boolean(example=True),
        "schedule": fields.Raw(
            example=None,
            description="The declared schedule union (time.cron/time.at), or null for on-demand.",
        ),
        "armed": fields.Boolean(
            example=False, description="Whether this pipeline's trigger is currently armed."
        ),
        "params_schema": fields.Raw(
            example={"type": "object", "properties": {"since": {"type": "string"}}},
            description="JSON Schema for invoke params, or null (no params accepted).",
        ),
        "cache_ttl_seconds": fields.Integer(
            example=300, description="How long a materialized result may be served from cache."
        ),
    },
)

app_pipelines_list_model = apps_ns.model(
    "AppPipelines", {"pipelines": fields.List(fields.Nested(app_pipeline_surface_row_model))}
)

app_pipeline_invoke_request = apps_ns.schema_model(
    "AppPipelineInvokeRequest",
    {
        "type": "object",
        "additionalProperties": True,
        "description": (
            "The pipeline's own params — validated against ITS declared "
            "params_schema (per-pipeline JSON Schema data, not a fixed shape; "
            "unknown keys or a pipeline with no params_schema both 400)."
        ),
        "example": {"since": "2026-07-01"},
    },
)

app_pipeline_invoke_result_model = apps_ns.model(
    "AppPipelineInvokeResult",
    {
        "output": fields.Raw(
            example={"count": 12}, description="The runner's materialized output."
        ),
        "evaluated_at": fields.String(example="2026-07-17T09:00:00+00:00"),
        "cache": fields.String(example="miss", description="'hit' or 'miss'."),
        "docs_written": fields.Raw(
            example={"tasks": 4},
            description="Per-collection document counts this invocation wrote.",
        ),
        "pipeline": fields.String(example="weekly-report"),
    },
)

app_pipeline_fire_result_model = apps_ns.model(
    "AppPipelineFireResult",
    {
        "pipeline": fields.String(example="weekly-report"),
        "mode": fields.String(
            example="code",
            description="'code' (ran synchronously) or 'agentic' (woke the maintainer).",
        ),
        "status": fields.String(
            example="succeeded",
            description=(
                "'succeeded' for a code fire; the landed signal "
                "('started'/'steered') for an agentic fire."
            ),
        ),
        "cache": fields.String(example="miss", description="'hit'/'miss' (code fire only)."),
        "docs_written": fields.Raw(
            example={"tasks": 4}, description="Per-collection counts this fire wrote (code only)."
        ),
        "evaluated_at": fields.String(
            example="2026-07-17T09:00:00+00:00", description="When the code fire evaluated."
        ),
    },
)

app_rearm_request = apps_ns.model(
    "AppRearmRequest",
    {
        "seed": fields.Boolean(
            example=False,
            description="Also fire each re-armed pipeline once immediately (default false).",
        ),
    },
)

app_rearm_result_model = apps_ns.model(
    "AppRearmResult",
    {
        "armed": fields.List(
            fields.Raw(example={"pipeline": "morning-organize", "trigger_id": "3f8c1e9a2b7d4c05"}),
            description="Pipelines whose schedule trigger was (re-)minted, with the new id.",
        ),
        "unchanged": fields.List(
            fields.String,
            example=["already-armed-pipeline"],
            description="Scheduled pipelines already armed (no action taken).",
        ),
        "seeded": fields.List(
            fields.String,
            example=[],
            description="Re-armed pipelines fired once immediately (only when seed=true).",
        ),
    },
)

app_system_model = apps_ns.model(
    "AppSystemHealth",
    {
        "app_id": fields.String(example="app-1a2b3c4d5e6f"),
        "status": fields.String(example="live"),
        "freshness": fields.Nested(app_freshness_model),
        "triggers": fields.List(fields.Nested(app_trigger_model)),
        "runs": fields.List(fields.Nested(app_run_model)),
        "maintainer": fields.Raw(
            example={"session_id": "7c1f0a84b2c39e2d", "status": "active"}
        ),
        "pipelines": fields.List(
            fields.Nested(app_pipeline_row_model),
            description=(
                "The declared per-pipeline tier: schedule union, on_demand, "
                "trigger_ref, and armed — so a client renders 'refreshes hourly' vs "
                "'on-demand' vs the unscheduled warning."
            ),
        ),
        "unscheduled_pipelines": fields.List(
            fields.String,
            example=[],
            description=(
                "Names of pipelines with no trigger currently armed on the "
                "maintainer (on-demand by design, or a schedule that never took)."
            ),
        ),
    },
)

app_data_model = apps_ns.model(
    "AppDataResult",
    {
        "collection": fields.String(example="tasks"),
        "documents": fields.List(
            fields.Raw(
                example={
                    "app_id": "app-1a2b3c4d5e6f",
                    "collection": "tasks",
                    "key": "t-001",
                    "doc": {"title": "Reply to invoice thread", "done": False},
                    "updated_at": "2026-07-17T09:00:00+00:00",
                }
            ),
            description="Full AppDataDoc envelopes; the SDK unwraps `row['doc']`.",
        ),
    },
)


# ---------------------------------------------------------------------------
# Resource adapters — thin HTTP boundary; the injected controller does the work
# ---------------------------------------------------------------------------


class _ControllerResource(Resource):
    """Base Resource receiving the one controller via ``resource_class_kwargs``."""

    def __init__(
        self, api: Any = None, *args: Any, controller: AppsRoutesController, **kwargs: Any
    ) -> None:
        super().__init__(api, *args, **kwargs)
        self.controller = controller

    # -- auth helpers (master key OR app-scoped token) ---------------------

    def _credential_ok(self, permission: str) -> bool:
        """Whether the API-KEY channel authorizes this request for *permission*.

        A key that authenticates but whose role lacks *permission* is NOT
        credential-ok, so the request falls through to the app-token channel
        exactly as an anonymous one does. That fall-through is the point: an
        app-scoped token is a SEPARATE credential with its own app-bound
        authority, and a caller holding a valid one must keep working whatever
        role their API key does or does not carry.
        """
        if self.controller.require_api_key() is not None:
            return False
        return self.controller.require_permission(permission)() is None

    def _read_auth(self, app_id: str, permission: str = "apps.read") -> tuple[dict, int] | None:
        """Authorize a read-only request: permitted key OR a valid app token."""
        return self.controller.authorize_read(
            app_id,
            credential_ok=self._credential_ok(permission),
            app_token=self._extract_app_token(),
        )

    def _write_auth(self, app_id: str, permission: str = "apps.submit") -> tuple[dict, int] | None:
        """Authorize a write-gated request: permitted key OR a WRITE-scoped token."""
        return self.controller.authorize_write(
            app_id,
            credential_ok=self._credential_ok(permission),
            app_token=self._extract_app_token(),
        )

    @staticmethod
    def _extract_app_token() -> str | None:
        """Read the render token: ``X-Mewbo-App-Token`` (SDK), then Bearer, then ``?token=``."""
        header = request.headers.get("X-Mewbo-App-Token") or request.headers.get("X-App-Token")
        if header and header.strip():
            return header.strip()
        authz = request.headers.get("Authorization", "")
        if authz[:7].lower() == "bearer ":
            return authz[7:].strip() or None
        return request.args.get("token")


class AppsCollection(_ControllerResource):
    """List the gallery or create a draft app."""

    @apps_ns.doc(security="apikey")
    @apps_ns.response(200, "The app gallery (lean summaries).", apps_list_model)
    @kit.auth_error()
    @guard.requires("apps.read")
    def get(self) -> tuple[dict, int]:
        """List every app in the gallery (archived apps hidden)."""
        return self.controller.list_apps()

    @apps_ns.doc(security="apikey")
    @apps_ns.expect(app_create_request)
    @apps_ns.response(201, "Draft app created.", app_create_model)
    @kit.errors(400, shape="message")
    @kit.auth_error()
    @guard.requires("apps.submit")
    def post(self) -> tuple[dict, int]:
        """Create a draft app from an intent (mints a builder session to stream)."""
        return self.controller.create_app(request.get_json(silent=True) or {})


class AppItem(_ControllerResource):
    """Fetch or edit a single app (archive is POST /archive — apps are versioned)."""

    @apps_ns.doc(security="apikey")
    @apps_ns.response(200, "The app manifest + version history.", app_detail_model)
    @kit.errors(404, shape="message")
    @kit.auth_error()
    @guard.requires("apps.read")
    def get(self, app_id: str) -> tuple[dict, int]:
        """Return the app's manifest and version history."""
        return self.controller.get_app(app_id)

    @apps_ns.doc(security="apikey")
    @apps_ns.expect(app_patch_request)
    @apps_ns.response(200, "The updated app detail.", app_detail_model)
    @kit.errors(400, 404, shape="message")
    @kit.auth_error()
    @guard.requires("apps.submit")
    def patch(self, app_id: str) -> tuple[dict, int]:
        """Edit an app's title / summary / icon."""
        return self.controller.patch_app(app_id, request.get_json(silent=True) or {})


class AppArchive(_ControllerResource):
    """Archive an app (apps are versioned entities — no hard delete)."""

    @apps_ns.doc(security="apikey")
    @apps_ns.response(200, "The archived app.", app_spec_model)
    @kit.errors(404, shape="message")
    @kit.auth_error()
    @guard.requires("apps.admin")
    def post(self, app_id: str) -> tuple[dict, int]:
        """Archive an app (removes it from the gallery; absorbing)."""
        return self.controller.archive_app(app_id)


class AppData(_ControllerResource):
    """Read-only query over one collection (master key OR app-scoped token)."""

    @apps_ns.doc(
        security="apikey",
        params={
            "filter": {
                "description": "JSON object of equality matches on top-level doc fields.",
                "in": "query",
                "type": "string",
            },
            "sort": {
                "description": "Doc field to sort by; prefix with '-' for descending.",
                "in": "query",
                "type": "string",
            },
            "limit": {"description": "Max documents (<=500).", "in": "query", "type": "integer"},
        },
    )
    @apps_ns.response(200, "The collection's AppDataDoc envelopes.", app_data_model)
    @kit.errors(401, 403, 404, shape="message")
    @guard.dual_channel(
        "apps.read",
        channel="app_token",
        enforced_by="AppsRoutesController.authorize_read",
    )
    def get(self, app_id: str, collection: str) -> tuple[dict, int]:
        """Query a collection. Read-only — no write verb exists on this route."""
        auth = self._read_auth(app_id)
        if auth:
            return auth
        raw_filter = request.args.get("filter")
        flt: dict[str, Any] | None = None
        if raw_filter:
            try:
                parsed = json.loads(raw_filter)
            except json.JSONDecodeError:
                parsed = None
            flt = parsed if isinstance(parsed, dict) else None
        limit = request.args.get("limit", type=int) or _DEFAULT_DATA_LIMIT
        limit = max(1, min(limit, _MAX_DATA_LIMIT))
        return self.controller.read_data(
            app_id, collection, filter=flt, sort=request.args.get("sort"), limit=limit
        )


class AppSystem(_ControllerResource):
    """Consolidated read-only health (freshness + triggers + runs + maintainer)."""

    @apps_ns.doc(security="apikey")
    @apps_ns.response(200, "The app's system-health payload.", app_system_model)
    @kit.errors(401, 403, 404, shape="message")
    @guard.dual_channel(
        "apps.read",
        channel="app_token",
        enforced_by="AppsRoutesController.authorize_read",
    )
    def get(self, app_id: str) -> tuple[dict, int]:
        """Freshness, triggers, recent runs, and maintainer status in one payload."""
        auth = self._read_auth(app_id)
        if auth:
            return auth
        return self.controller.system_health(app_id)


class AppToken(_ControllerResource):
    """Mint a short-lived, app-scoped read (or, master-key-only, write) token."""

    @apps_ns.doc(security="apikey")
    @apps_ns.expect(app_token_mint_request)
    @apps_ns.response(201, "A minted token (token_id is the credential).", app_token_model)
    @kit.errors(400, 403, 404, shape="message")
    @kit.auth_error()
    @guard.requires("apps.use")
    def post(self, app_id: str) -> tuple[dict, int]:
        """Mint a render-scoped token carrying only this app_id + a scope.

        ``scope="write"`` (body) additionally requires the literal master
        token — an issued key may mint read only.

        The route gates on ``apps.use``, not ``apps.admin``: minting a READ
        token is what a client does to RENDER an app, so requiring the admin
        verb here would stop an ordinary member from opening any app at all.
        The escalation this route could otherwise carry is the write scope, and
        that is already held shut by the master-token check inside
        :meth:`AppsRoutesController.mint_token`.
        """
        return self.controller.mint_token(app_id, request.get_json(silent=True) or {})


class AppPipelines(_ControllerResource):
    """List the app's declared pipeline surface (master/issued key OR app token)."""

    @apps_ns.doc(security="apikey")
    @apps_ns.response(200, "The declared pipeline surface.", app_pipelines_list_model)
    @kit.errors(401, 403, 404, shape="message")
    @guard.dual_channel(
        "apps.read",
        channel="app_token",
        enforced_by="AppsRoutesController.authorize_read",
    )
    def get(self, app_id: str) -> tuple[dict, int]:
        """List every pipeline's declared mode/schedule/params surface."""
        auth = self._read_auth(app_id)
        if auth:
            return auth
        return self.controller.list_pipelines(app_id)


class AppPipelineInvoke(_ControllerResource):
    """Invoke one ``mode="code"`` pipeline: GET (read-auth) / POST (write-auth)."""

    @apps_ns.doc(security="apikey")
    @apps_ns.response(200, "The pipeline's materialized output.", app_pipeline_invoke_result_model)
    @kit.errors(
        400,
        401,
        403,
        404,
        409,
        502,
        503,
        shape="message",
        descriptions={
            409: "The pipeline is mode='agentic' — not invocable synchronously.",
            502: "The runner raised while executing the pipeline.",
            503: "No pipeline runner is configured on this deployment.",
        },
    )
    @guard.dual_channel(
        "apps.use",
        channel="app_token",
        enforced_by="AppsRoutesController.authorize_read",
    )
    def get(self, app_id: str, name: str) -> tuple[dict, int]:
        """Invoke a pipeline read-only: query params become ``params``."""
        auth = self._read_auth(app_id, "apps.use")
        if auth:
            return auth
        return self.controller.invoke_pipeline(
            app_id, name, raw_query_params=request.args.to_dict(flat=True)
        )

    @apps_ns.doc(security="apikey")
    @apps_ns.expect(app_pipeline_invoke_request)
    @apps_ns.response(200, "The pipeline's materialized output.", app_pipeline_invoke_result_model)
    @kit.errors(
        400,
        401,
        403,
        404,
        409,
        502,
        503,
        shape="message",
        descriptions={
            409: "The pipeline is mode='agentic' — not invocable synchronously.",
            502: "The runner raised while executing the pipeline.",
            503: "No pipeline runner is configured on this deployment.",
        },
    )
    @guard.dual_channel(
        "apps.submit",
        channel="app_token",
        enforced_by="AppsRoutesController.authorize_write",
    )
    def post(self, app_id: str, name: str) -> tuple[dict, int]:
        """Invoke a pipeline: params in the JSON body. Requires a WRITE-scoped token."""
        auth = self._write_auth(app_id, "apps.submit")
        if auth:
            return auth
        return self.controller.invoke_pipeline(
            app_id, name, json_body=request.get_json(silent=True)
        )


class AppPipelineFire(_ControllerResource):
    """On-demand refresh of a pipeline — BOTH modes (read-auth, like the GET invoke)."""

    @apps_ns.doc(security="apikey")
    @apps_ns.response(200, "A code pipeline ran and ledgered.", app_pipeline_fire_result_model)
    @apps_ns.response(
        202, "An agentic pipeline's maintainer was woken.", app_pipeline_fire_result_model
    )
    @kit.errors(
        401,
        403,
        404,
        409,
        429,
        502,
        503,
        shape="message",
        descriptions={
            409: (
                "App not live, no maintainer, a run already open for this pipeline, "
                "or the maintainer refused the wake."
            ),
            429: "Fired within the cooldown window (carries retry_after_seconds).",
            502: "A code pipeline's runner raised (ledgered, no auto-repair).",
            503: "No fire seam is configured on this deployment.",
        },
    )
    @guard.dual_channel(
        "apps.use",
        channel="app_token",
        enforced_by="AppsRoutesController.authorize_read",
    )
    def post(self, app_id: str, name: str) -> tuple[dict, int]:
        """Fire a pipeline now: a code pipeline runs synchronously; an agentic one wakes."""
        auth = self._read_auth(app_id, "apps.use")
        if auth:
            return auth
        return self.controller.fire_pipeline(app_id, name)


class AppTriggers(_ControllerResource):
    """List the app's triggers (management view)."""

    @apps_ns.doc(security="apikey")
    @apps_ns.response(200, "The app's triggers.", app_triggers_model)
    @kit.errors(404, shape="message")
    @kit.auth_error()
    @guard.requires("apps.read")
    def get(self, app_id: str) -> tuple[dict, int]:
        """List the app's triggers for the management surface."""
        return self.controller.list_triggers(app_id)


class AppPause(_ControllerResource):
    """Pause the app + its triggers."""

    @apps_ns.doc(security="apikey")
    @apps_ns.response(200, "The paused app.", app_spec_model)
    @kit.errors(404, shape="message")
    @kit.auth_error()
    @guard.requires("apps.admin")
    def post(self, app_id: str) -> tuple[dict, int]:
        """Pause the app and every armed trigger it owns."""
        return self.controller.pause_app(app_id)


class AppResume(_ControllerResource):
    """Resume the app + its triggers."""

    @apps_ns.doc(security="apikey")
    @apps_ns.response(200, "The resumed app.", app_spec_model)
    @kit.errors(404, shape="message")
    @kit.auth_error()
    @guard.requires("apps.admin")
    def post(self, app_id: str) -> tuple[dict, int]:
        """Resume the app and re-arm every paused trigger it owns."""
        return self.controller.resume_app(app_id)


class AppRearm(_ControllerResource):
    """Re-mint a live app's missing/dead schedule triggers (operator repair, key-only)."""

    @apps_ns.doc(security="apikey")
    @apps_ns.expect(app_rearm_request)
    @apps_ns.response(200, "The re-arm summary.", app_rearm_result_model)
    @kit.errors(
        400,
        404,
        409,
        shape="message",
        descriptions={409: "The app is not live — re-arm applies only to a live app."},
    )
    @kit.auth_error()
    @guard.requires("apps.admin")
    def post(self, app_id: str) -> tuple[dict, int]:
        """Re-arm the app's declared schedules (master/issued key only)."""
        return self.controller.rearm_app(app_id, request.get_json(silent=True) or {})


class AppRollback(_ControllerResource):
    """Repoint the app to an earlier version's design."""

    @apps_ns.doc(security="apikey")
    @apps_ns.expect(app_rollback_request)
    @apps_ns.response(200, "The refreshed app detail at its new version.", app_detail_model)
    @kit.errors(400, 404, shape="message")
    @kit.auth_error()
    @guard.requires("apps.admin")
    def post(self, app_id: str) -> tuple[dict, int]:
        """Roll the app back to an earlier version (recorded as a new version)."""
        return self.controller.rollback_app(app_id, request.get_json(silent=True) or {})


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

# The single DI handle: construction result of ``init_apps_routes``. The request
# path NEVER reads it (Resources receive the controller by injection); it exists
# so the composition root holds the instance and a route test can point the one
# registered controller at fresh stores by reassigning its fields.
_controller: AppsRoutesController | None = None


def init_apps_routes(api: Any, controller: AppsRoutesController) -> None:
    """Register the namespace, DI the controller into every Resource (once, startup)."""
    global _controller  # noqa: PLW0603 - single composition-root handle, set once
    _controller = controller
    injected = {"resource_class_kwargs": {"controller": controller}}
    for resource, path in (
        (AppsCollection, "/apps"),
        (AppItem, "/apps/<string:app_id>"),
        (AppArchive, "/apps/<string:app_id>/archive"),
        (AppData, "/apps/<string:app_id>/data/<string:collection>"),
        (AppSystem, "/apps/<string:app_id>/system"),
        (AppToken, "/apps/<string:app_id>/token"),
        (AppPipelines, "/apps/<string:app_id>/pipelines"),
        (AppPipelineInvoke, "/apps/<string:app_id>/pipelines/<string:name>"),
        (AppPipelineFire, "/apps/<string:app_id>/pipelines/<string:name>/fire"),
        (AppTriggers, "/apps/<string:app_id>/triggers"),
        (AppPause, "/apps/<string:app_id>/pause"),
        (AppResume, "/apps/<string:app_id>/resume"),
        (AppRearm, "/apps/<string:app_id>/rearm"),
        (AppRollback, "/apps/<string:app_id>/rollback"),
    ):
        apps_ns.add_resource(resource, path, **injected)
    api.add_namespace(apps_ns, path="/api")


__all__ = [
    "AppsRoutesController",
    "AppCreateRequest",
    "AppPatchRequest",
    "AppRearmRequest",
    "AppRollbackRequest",
    "AppTokenMintRequest",
    "apps_ns",
    "init_apps_routes",
]
