"""Flask-RESTX namespace for the "Open in Web IDE" feature."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar, Protocol

from flask import request
from flask_restx import Namespace, Resource, fields
from loguru import logger
from mewbo_core.workspaces.project_catalog import ProjectCatalog, ProjectResolutionError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from mewbo_api.auth.guard_registry import guard
from mewbo_api.ide import (
    SESSION_ID_RE,
    DockerUnavailable,
    IdeManager,
    MaxLifetimeReached,
)
from mewbo_api.responses import ApiResponseKit

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mewbo_core.loop.session_runtime import SessionRuntime

ide_ns = Namespace("ide", description="Per-session Web IDE (code-server) management")

# One DRY home for this namespace's error examples (every IDE route returns the
# ``{"message": ...}`` shape; the extend 409 returns a distinct
# ``max_lifetime`` body documented inline on that route). Built at module level
# so the import-time decorators can see it; ``Ide`` prefix namespaces the
# generated model names on the shared Api registry.
kit = ApiResponseKit(ide_ns, prefix="Ide")

# The JSON shape of an IDE instance (``IdeInstance.to_dict``). ``password`` is on
# POST responses only. Examples drive Scalar's sample body.
_ide_instance_model = ide_ns.model(
    "IdeInstance",
    {
        "session_id": fields.String(example="9e2d47c1f0"),
        "status": fields.String(
            example="running", description="Container lifecycle state."
        ),
        "url": fields.String(
            example="https://ide.example.com/s/9e2d47c1f0/",
            description="Browser entry point for the code-server instance.",
        ),
        "project_name": fields.String(example="my-project"),
        "project_path": fields.String(example="/srv/projects/my-project"),
        "created_at": fields.String(example="2026-06-15T10:00:00+00:00"),
        "expires_at": fields.String(
            example="2026-06-15T14:00:00+00:00",
            description="When the instance is reaped unless extended.",
        ),
        "max_deadline": fields.String(
            example="2026-06-22T10:00:00+00:00",
            description="Hard ceiling past which extension is refused.",
        ),
        "remaining_seconds": fields.Integer(example=14400),
        "extensions": fields.Integer(
            example=0, description="How many times the deadline has been pushed."
        ),
        "password": fields.String(
            example="hunter2-9e2d47",
            description="code-server access password — returned on POST only.",
        ),
    },
)

# The extend route's 409 body is a distinct shape (NOT the standard envelope).
_ide_max_lifetime_model = ide_ns.model(
    "IdeMaxLifetimeError",
    {
        "error": fields.String(
            example="max_lifetime_reached",
            description="Stable error code for the lifetime ceiling.",
        ),
        "max_deadline": fields.String(
            example="2026-06-22T10:00:00+00:00",
            description="The ceiling the requested deadline exceeded.",
        ),
    },
)

AuthResult = tuple[dict, int] | None


# Populated by ``init_ide`` at app startup.
_manager: IdeManager | None = None
_runtime: SessionRuntime | None = None
_resolver: IdeWorkspaceResolver | None = None


def init_ide(
    manager: IdeManager,
    runtime: SessionRuntime,
    catalog_source: Callable[[], ProjectCatalog],
) -> None:
    """Wire the namespace to its collaborators (called once at app startup).

    ``catalog_source`` is a CALLABLE rather than a bound catalog because the
    composition root re-points its catalog at the config and project store the
    process currently holds on every call; capturing the instance here would
    freeze whichever sources existed at startup.

    Authentication and authorization are NOT wired here: every view in this
    module declares its own requirement with ``@guard.requires``, which resolves
    the live ``AuthKit`` through ``guard_registry`` at request time. Injecting a
    guard here as well would be a second, silently-unused path to the same
    decision.
    """
    global _manager, _runtime, _resolver
    _manager = manager
    _runtime = runtime
    _resolver = IdeWorkspaceResolver.over_catalog(catalog_source)


class ExtendBody(BaseModel):
    """Request body for ``POST /ide/extend``. Exactly one field is required."""

    model_config = ConfigDict(extra="forbid")

    hours: int | None = Field(default=None, ge=1, le=168)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> ExtendBody:
        if (self.hours is None) == (self.expires_at is None):
            raise ValueError("exactly one of 'hours' or 'expires_at' must be provided")
        return self


def _precheck(session_id: str) -> AuthResult:
    """Check the session_id shape + manager availability in one call.

    Returns ``None`` on success (the caller may then use ``_manager``
    unconditionally) or an ``(error_body, status)`` tuple that the route
    should return verbatim.

    Authentication/authorization is NOT here: each verb declares it with
    ``@guard.requires("ide.access")``, which runs before the handler body and
    therefore still answers ahead of both checks below — the original
    auth -> session-id -> manager ordering is unchanged.
    """
    if not SESSION_ID_RE.match(session_id):
        return {"message": "session not found"}, 404
    # ``_manager`` and ``_resolver`` are set together by ``init_ide``; both are
    # named so a caller may use either unconditionally afterwards.
    if _manager is None or _resolver is None:  # pragma: no cover - init_ide not called
        return {"message": "ide feature not initialized"}, 503
    return None


class IdeWorkspaceUnavailable(Exception):
    """A tier RECOGNISED the session and still cannot mount it.

    Distinct from "no tier matched", which is the generic 409: this carries the
    specific sentence for a session the server knows is IDE-eligible but whose
    directory is missing or could not be produced. Without it the mount falls
    through to the broker's generic ``workspace_denied`` 403, which is
    undiagnosable from the console.
    """


class IdeWorkspace(BaseModel):
    """The display name + on-disk directory an IDE container mounts.

    Validated rather than a bare tuple because both fields are persisted on
    ``IdeInstance`` and echoed to the console capsule, and because an empty
    ``project_path`` would reach the container backend as a bind source that
    docker materializes as an empty directory. ``extra="forbid"`` keeps a tier
    from smuggling a field the mount path does not read.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_name: str = Field(min_length=1)
    project_path: str = Field(min_length=1)


class IdeMountTier(Protocol):
    """One way a session earns a directory — structural, so a tier is any class.

    A tier answers ``None`` when the session is simply not its kind, an
    :class:`IdeWorkspace` when it binds, and raises
    :class:`IdeWorkspaceUnavailable` when it recognises the session and still
    cannot produce a directory.
    """

    def resolve(
        self, session_id: str, runtime: SessionRuntime
    ) -> IdeWorkspace | None:  # pragma: no cover - Protocol body
        """Return this tier's mount for *session_id*, or ``None``."""
        ...


class CatalogProjectMount:
    """Tier 1 — the session's context ``project`` names a project in the catalog.

    **The trap this exists to close: a private "project name → directory" rule
    beside the one catalog.** Reading the key straight out of ``app.json``
    recognised a CONFIGURED project and nothing else, so every console session
    anchored to a managed project (``managed:<id>``), a worktree or a repository
    slug fell out of the walk and read as "session has no project in context" —
    a name the server resolves everywhere else, refused only here. The catalog
    is the one resolver; this tier is a caller, never a second copy of it.

    ``O(1)`` on the Mongo driver, ``O(one session)`` on the base store for the
    event read — ``latest_event_of_type`` is bounded by the TYPE, so this reads
    the one event it needs instead of a whole transcript to pull one string out
    of its tail. Catalog resolution then adds an ``O(collection)`` leg over the
    project and repository stores.

    Two narrowings on the event read that look optional and are not. A tail
    bounded by COUNT would be cheaper still and wrong: the newest ``context``
    event sits arbitrarily far back after a long run, and missing it turns a
    launchable session into a ``409``. And ``payload_key`` is what keeps this
    equivalent to the backwards transcript scan it replaced — context events
    merge key-by-key, so the newest one need not be the newest one MENTIONING a
    project (measured on the deployed store: 15 of 204 such sessions).
    """

    # Refusal codes that mean "not something this tier can bind", so the walk
    # continues into the wiki and app tiers. Every OTHER code means the catalog
    # RECOGNISED the project and still cannot hand over a directory, which is
    # the tier's own refusal — and the catalog already writes an actionable
    # sentence for it, so it is surfaced verbatim rather than restated.
    FALL_THROUGH_CODES: ClassVar[frozenset[str]] = frozenset(
        {"not_found", "empty", "auto_sentinel"}
    )

    def __init__(self, catalog_source: Callable[[], ProjectCatalog]) -> None:
        """Bind the catalog accessor; a callable so late-bound sources are read."""
        self.catalog_source = catalog_source

    def resolve(self, session_id: str, runtime: SessionRuntime) -> IdeWorkspace | None:
        """Return the catalog project's mount, ``None``, or refuse with a sentence."""
        event = runtime.session_store.latest_event_of_type(
            session_id, "context", payload_key="project"
        )
        if event is None:
            return None
        payload = event.get("payload")
        candidate = payload.get("project") if isinstance(payload, dict) else None
        if not isinstance(candidate, str) or not candidate.strip():
            return None
        project_key = candidate.strip()

        catalog = self.catalog_source()
        try:
            path = catalog.resolve(project_key)
        except ProjectResolutionError as exc:
            if exc.code in self.FALL_THROUGH_CODES:
                return None
            raise IdeWorkspaceUnavailable(exc.message) from exc
        # The DISPLAY name, looked up only on the resolving path: a raw
        # ``managed:<uuid>`` is persisted on ``IdeInstance`` and echoed into the
        # console capsule, where it names nothing a user recognises.
        entry = catalog.find(project_key)
        return IdeWorkspace(
            project_name=entry.name if entry is not None else project_key,
            project_path=path,
        )


class WikiCheckoutMount:
    """Tier 2 — a wiki MAINTAINER session mounts its project's surviving checkout.

    The binding is the server-stamped ``wiki:maintain:<slug>`` TAG, read through
    ``WikiJobCtx.for_maintainer`` — the same resolver the wiki page-write tools
    use, so a session this tier mounts is exactly a session those tools serve. A
    context ``slug`` key is deliberately NOT accepted: any caller can write one
    into a session's context, so trusting it would hand an IDE a checkout of a
    project the caller was never given a session for.

    The checkout itself comes from ``resolve_qa_clone_dir`` (via
    ``for_maintainer``), never from re-deriving the clone-root layout here.

    Cost: ``O(1)`` tag read, one project read, and ``O(collection)`` in the slug's
    job count for the checkout walk.
    """

    def resolve(self, session_id: str, runtime: SessionRuntime) -> IdeWorkspace | None:
        """Return the wiki checkout's mount, ``None``, or refuse with a sentence."""
        try:
            from mewbo_graph.plugins.wiki._ctx import WikiJobCtx, resolve_runtime
        except ImportError:  # pragma: no cover - base install without the wiki extra
            return None
        wiki_runtime = resolve_runtime()
        store = getattr(wiki_runtime, "wiki_store", None)
        if store is None:
            return None
        ctx = WikiJobCtx.for_maintainer(session_id, runtime, store)
        if ctx is None:
            return None
        if not ctx.clone_dir.is_dir():
            raise IdeWorkspaceUnavailable(
                f"wiki project '{ctx.slug}' has no checkout on disk — "
                "re-index the project, then open the Web IDE again"
            )
        return IdeWorkspace(project_name=ctx.slug, project_path=str(ctx.clone_dir))


class AppStagingMount:
    """Tier 3 — an app's builder/maintainer/opened-against session mounts its staging dir.

    Staging is EPHEMERAL: an app's source lives in its manifest and reaches disk
    only when something materializes it, so this tier MATERIALIZES on demand
    through ``AppStagingArea`` — the one implementation ``get_app``'s ``stage``
    operation also calls. Refusing instead would leave the feature working only
    in the rare window after a stage and before a restart.

    **Must pass ``session_tags``, exactly like every other app surface.**
    ``app_for_session`` resolves by two id FIELDS first (owner/maintainer) and
    only THEN by the server-stamped ``app:<id>:<session_id>`` tag — see
    ``mewbo_api/apps/CLAUDE.md`` → "ONE resolver owns both tiers". Omitting the
    tag argument silently drops back to the pre-tag behaviour, and it is a
    DEFAULT that fails quietly rather than an error: this tier, ``run_pipeline``
    and ``app_data`` each lost the tag tier this way, independently. A session
    opened via ``POST /apps/<id>/session
    {"new_session": true}`` (the composer's "start a new conversation" action)
    is neither the owner nor the maintainer, so it resolved to no app at all and
    the Web IDE answered "session has no project in context" for a session that
    plainly has one open. Read off the already-injected ``runtime.session_store``
    — the same collaborator ``CatalogProjectMount`` reads context off — rather
    than the apps plugin's own ``session_tags_for`` singleton, which would open
    a second store connection this tier has no need for.

    Cost: ``O(collection)`` in the number of stored apps for the session→app
    scan, plus ``O(one app)`` for the write (the bundle is capped at submit).
    """

    def resolve(self, session_id: str, runtime: SessionRuntime) -> IdeWorkspace | None:
        """Return the staging dir's mount, ``None``, or refuse with a sentence."""
        from mewbo_api.apps.staging import AppStagingArea, AppStagingError
        from mewbo_api.apps.store import get_app_store

        area = AppStagingArea(session_id=session_id)
        tags = runtime.session_store.tags_for_session(session_id)
        app = area.app_for_session(get_app_store(), session_tags=tags)
        if app is None:
            return None
        try:
            bundle = area.materialize(app)
        except AppStagingError as exc:
            raise IdeWorkspaceUnavailable(
                f"could not stage app '{app.app_id}' for the Web IDE: {exc}"
            ) from exc
        return IdeWorkspace(
            project_name=app.title or app.app_id, project_path=bundle.directory
        )


class IdeWorkspaceResolver:
    """Decides which directory a session's IDE container mounts.

    An ordered tuple of tiers, each owning its own binding rule and its own
    refusal — there is no tier discriminator to branch on here, so adding a
    fourth surface means adding a tier class, never an arm in this method.

    A tier's own failure (a dead store, a missing optional extra) is logged and
    falls through to the next tier: a wiki outage must not stop a configured
    project from opening. A deliberate refusal
    (:class:`IdeWorkspaceUnavailable`) propagates — it is the answer.
    """

    def __init__(self, tiers: tuple[IdeMountTier, ...]) -> None:
        """Bind the tier order; injectable so a test can drive one tier alone."""
        self.tiers: tuple[IdeMountTier, ...] = tiers

    @classmethod
    def over_catalog(
        cls, catalog_source: Callable[[], ProjectCatalog]
    ) -> IdeWorkspaceResolver:
        """The production tier order, with the catalog handed to the tier needing it.

        A classmethod rather than a default argument because the first tier can
        no longer be constructed without a collaborator: a default that reached
        for the composition root's catalog itself would put an app-global back
        inside the class the injection exists to keep free of one.
        """
        return cls(
            tiers=(
                CatalogProjectMount(catalog_source),
                WikiCheckoutMount(),
                AppStagingMount(),
            )
        )

    def resolve(self, session_id: str, runtime: SessionRuntime | None) -> IdeWorkspace | None:
        """Return the first tier's mount, or ``None`` when no tier binds."""
        if runtime is None:
            return None
        for tier in self.tiers:
            try:
                workspace = tier.resolve(session_id, runtime)
            except IdeWorkspaceUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 - one tier must not sink the rest
                logger.warning(
                    "ide: {} failed for {}: {}", type(tier).__name__, session_id, exc
                )
                continue
            if workspace is not None:
                return workspace
        return None


def _session_exists(session_id: str) -> bool:
    if _runtime is None:
        return False
    try:
        return session_id in set(_runtime.session_store.list_sessions())
    except Exception as exc:  # pragma: no cover
        logger.warning("ide: failed to list sessions: {}", exc)
        return False


@ide_ns.route("/sessions/<string:session_id>/ide")
class IdeResource(Resource):
    """Create, fetch, or delete the IDE instance bound to a session."""

    @ide_ns.doc(
        description=(
            "Create the session's code-server container, or reconnect to an existing "
            "one. Returns `201` on first create and `200` on reconnect; the response "
            "body is the IDE instance and — uniquely on this verb — includes the "
            "`password` so any browser tab can open the IDE. The session must exist "
            "and resolve to a directory, else `404`/`409`. It resolves when the "
            "session's context names a project the catalog knows (configured, managed, "
            "worktree, or a checked-out repository), when the session maintains a wiki "
            "project whose checkout still exists, or when it maintains a Mewbo App, "
            "whose staging directory is materialized on demand. Idempotent: call it "
            "again from another tab to reconnect."
        )
    )
    @ide_ns.response(201, "IDE container created", _ide_instance_model)
    @ide_ns.response(200, "Reconnected to the existing IDE container", _ide_instance_model)
    @kit.errors(404, 409, 503, shape="message")
    @kit.auth_error()
    @guard.requires("ide.access")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Create or reconnect to the session's code-server container."""
        error = _precheck(session_id)
        if error:
            return error
        assert _manager is not None
        assert _resolver is not None
        if not _session_exists(session_id):
            return {"message": "session not found"}, 404
        try:
            workspace = _resolver.resolve(session_id, _runtime)
        except IdeWorkspaceUnavailable as exc:
            return {"message": str(exc)}, 409
        if workspace is None:
            return {"message": "session has no project in context"}, 409
        try:
            instance, created = _manager.ensure(
                session_id, workspace.project_name, workspace.project_path
            )
        except DockerUnavailable as exc:
            logger.warning("ide: docker unavailable: {}", exc)
            return {"message": "docker daemon unreachable"}, 503
        # POST is the only endpoint that returns the password — it's the
        # entry point for both initial create and reconnect from any tab.
        return instance.to_dict(include_password=True), (201 if created else 200)

    @ide_ns.doc(
        description=(
            "Return the current state of the session's IDE instance. The `password` "
            "is omitted here (it is only returned by `POST`), so this is the cheap "
            "poll the session page hits every ~30s. `404` when no instance exists "
            "for the session."
        )
    )
    @ide_ns.response(200, "Current IDE instance state", _ide_instance_model)
    @kit.errors(404, 503, shape="message")
    @kit.auth_error()
    @guard.requires("ide.access")
    def get(self, session_id: str) -> tuple[dict, int]:
        """Return current instance state or 404 if none exists."""
        error = _precheck(session_id)
        if error:
            return error
        assert _manager is not None
        try:
            instance = _manager.get(session_id)
        except DockerUnavailable:
            return {"message": "docker daemon unreachable"}, 503
        if instance is None:
            return {"message": "no ide instance for session"}, 404
        return instance.to_dict(), 200

    @ide_ns.doc(
        description=(
            "Stop and remove the session's code-server container, deleting its Mongo "
            "record and deadline file. Returns `204` (no body) on success; `404` if "
            "the session had no IDE instance to remove."
        )
    )
    @ide_ns.response(204, "IDE container stopped and removed (no body)")
    @kit.errors(404, 503, shape="message")
    @kit.auth_error()
    @guard.requires("ide.access")
    def delete(self, session_id: str) -> tuple[dict, int]:
        """Stop and remove the container, deleting Mongo + deadline file."""
        error = _precheck(session_id)
        if error:
            return error
        assert _manager is not None
        try:
            removed = _manager.stop(session_id)
        except DockerUnavailable:
            return {"message": "docker daemon unreachable"}, 503
        if not removed:
            return {"message": "no ide instance for session"}, 404
        return {}, 204


_ide_extend_model = ide_ns.model(
    "IdeExtendRequest",
    {
        "hours": fields.Integer(
            example=4,
            min=1,
            max=168,
            description=(
                "Extend the deadline by this many hours (1–168). "
                "Mutually exclusive with expires_at."
            ),
        ),
        "expires_at": fields.String(
            example="2026-06-15T18:00:00+00:00",
            description="Set an absolute new deadline (ISO 8601). Mutually exclusive with hours.",
        ),
    },
)


@ide_ns.route("/sessions/<string:session_id>/ide/extend")
class IdeExtendResource(Resource):
    """Extend the deadline of a running IDE instance."""

    @ide_ns.doc(
        description=(
            "Push the IDE instance's `expires_at` forward. Supply **exactly one** of "
            "`hours` (1–168, relative) or `expires_at` (absolute ISO 8601). The new "
            "deadline is clamped to the instance's `max_deadline`; exceeding it "
            "returns `409` with a `max_lifetime_reached` body carrying that ceiling."
        )
    )
    @ide_ns.expect(_ide_extend_model)
    @ide_ns.response(200, "Deadline extended; updated IDE instance state", _ide_instance_model)
    @ide_ns.response(
        409, "Requested deadline exceeds the lifetime ceiling", _ide_max_lifetime_model
    )
    @kit.errors(404, 503, shape="message")
    @kit.errors(400, shape="message")
    @kit.auth_error()
    @guard.requires("ide.access")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Push ``expires_at`` forward, rejecting requests past ``max_deadline``."""
        error = _precheck(session_id)
        if error:
            return error
        assert _manager is not None
        payload = request.get_json(silent=True) or {}
        try:
            body = ExtendBody.model_validate(payload)
        except ValidationError as exc:
            errors = [
                {"loc": list(err.get("loc", ())), "msg": str(err.get("msg", ""))}
                for err in exc.errors()
            ]
            return {"message": "invalid request body", "errors": errors}, 400
        try:
            instance = _manager.extend(session_id, hours=body.hours, expires_at=body.expires_at)
        except LookupError:
            return {"message": "no ide instance for session"}, 404
        except MaxLifetimeReached as exc:
            return (
                {
                    "error": "max_lifetime_reached",
                    "max_deadline": exc.max_deadline.isoformat(),
                },
                409,
            )
        except ValueError as exc:
            return {"message": str(exc)}, 400
        except DockerUnavailable:
            return {"message": "docker daemon unreachable"}, 503
        return instance.to_dict(), 200


__all__ = [
    "AppStagingMount",
    "CatalogProjectMount",
    "ExtendBody",
    "IdeExtendResource",
    "IdeResource",
    "IdeWorkspace",
    "IdeWorkspaceResolver",
    "IdeWorkspaceUnavailable",
    "WikiCheckoutMount",
    "ide_ns",
    "init_ide",
]
