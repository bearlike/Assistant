"""Flask-RESTX namespace for the "Open in Web IDE" feature."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from flask import request
from flask_restx import Namespace, Resource, fields
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from mewbo_api.ide import (
    SESSION_ID_RE,
    DockerUnavailable,
    IdeManager,
    MaxLifetimeReached,
)
from mewbo_api.responses import ApiResponseKit

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mewbo_core.session_runtime import SessionRuntime

ide_ns = Namespace("ide", description="Per-session Web IDE (code-server) management")

# One DRY home for this namespace's error examples (every IDE route returns the
# legacy ``{"message": ...}`` shape; the extend 409 returns a distinct
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
        "cpus": fields.Float(example=2.0),
        "memory": fields.String(example="2g"),
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
AuthGuard = Callable[[], AuthResult]


def _no_auth() -> AuthResult:
    return None


# Populated by ``init_ide`` at app startup.
_manager: IdeManager | None = None
_runtime: SessionRuntime | None = None
_require_api_key: AuthGuard = _no_auth


def init_ide(
    manager: IdeManager,
    runtime: SessionRuntime,
    require_api_key: AuthGuard,
) -> None:
    """Wire the namespace to its collaborators (called once at app startup)."""
    global _manager, _runtime, _require_api_key
    _manager = manager
    _runtime = runtime
    _require_api_key = require_api_key


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
    """Run auth + session_id regex + manager availability in one call.

    Returns ``None`` on success (the caller may then use ``_manager``
    unconditionally) or an ``(error_body, status)`` tuple that the route
    should return verbatim.
    """
    auth_error = _require_api_key()
    if auth_error:
        return auth_error
    if not SESSION_ID_RE.match(session_id):
        return {"message": "session not found"}, 404
    if _manager is None:  # pragma: no cover - only hit if init_ide wasn't called
        return {"message": "ide feature not initialized"}, 503
    return None


def _resolve_session_project(session_id: str) -> tuple[str, str] | None:
    """Return ``(project_name, project_path)`` from the latest session context event."""
    if _runtime is None:
        return None
    try:
        events = _runtime.session_store.load_transcript(session_id)
    except Exception as exc:  # pragma: no cover - storage-level failure
        logger.warning("ide: failed to load transcript for {}: {}", session_id, exc)
        return None
    if not events:
        return None

    project_name = ""
    for event in reversed(events):
        if event.get("type") != "context":
            continue
        payload = event.get("payload")
        if isinstance(payload, dict):
            candidate = payload.get("project")
            if isinstance(candidate, str) and candidate.strip():
                project_name = candidate.strip()
                break

    if not project_name:
        return None

    from mewbo_core.config import get_config

    project = get_config().projects.get(project_name)
    if project is None or not project.path:
        return None
    return project_name, project.path


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
            "and carry a project in its context (else `404`/`409`). Idempotent: call "
            "it again from another tab to reconnect."
        )
    )
    @ide_ns.response(201, "IDE container created", _ide_instance_model)
    @ide_ns.response(200, "Reconnected to the existing IDE container", _ide_instance_model)
    @kit.errors(404, 409, 503, shape="message")
    @kit.auth_error()
    def post(self, session_id: str) -> tuple[dict, int]:
        """Create or reconnect to the session's code-server container."""
        error = _precheck(session_id)
        if error:
            return error
        assert _manager is not None
        if not _session_exists(session_id):
            return {"message": "session not found"}, 404
        resolved = _resolve_session_project(session_id)
        if resolved is None:
            return {"message": "session has no project in context"}, 409
        project_name, project_path = resolved
        try:
            instance, created = _manager.ensure(session_id, project_name, project_path)
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


__all__ = ["ExtendBody", "IdeExtendResource", "IdeResource", "ide_ns", "init_ide"]
