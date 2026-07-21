#!/usr/bin/env python3
"""The REST error taxonomy — a refusal is a typed exception, not a loose dict.

Today a handler that refuses a request builds its wire body inline and
``return``s it: ``return {"message": "Unauthorized"}, 401``. Repeated across
~144 handlers that has three costs — the same body is spelled slightly
differently in different places, a refusal cannot travel up out of a helper (it
has to be threaded back through every caller as a sentinel return value), and
nothing validates that the body is well-formed for the status it rides on.

This module replaces the dict with a hierarchy where **each error class owns its
status as a class constant and renders its own wire body from a validated
Pydantic payload**. A handler raises; the registered errorhandler renders. The
refusal can therefore be raised from a controller method, a validator, or a
store wrapper, and arrive on the wire correctly without any intermediate caller
knowing it passed through.

**The compatibility law this module is built around: nothing on the wire
changes.** These classes REPRODUCE the bodies the inline call sites already
return, byte for byte — this is a refactor of HOW a body is produced, never of
WHAT a client receives. Concretely: ``AuthKit.require_api_key`` returns
``{"message": "API token is not provided."}`` / ``{"message": "Unauthorized"}``,
the permission guard returns ``{"message": "insufficient role"}``, and the
terminated-session 410 is byte-locked to the console's ``session_terminated``
sentinel. Each of those strings now has exactly ONE definition, on the classmethod
that mints it, which is what stops the next copy from drifting.

**Two wire shapes, because the API genuinely has two** (see ``responses.py``,
which documents them for OpenAPI — this module is the runtime half of the same
contract):

* ``envelope`` — ``{"error": {"code", "reason", "retryable"}}``, the canonical
  shape (the app-level 404 handler, the structured and agentic-search surfaces).
* ``message`` — ``{"message": "..."}``, the legacy auth/validation shape the
  ``/api`` routes and the IAM/admin surfaces return.

The shape is a property of the SURFACE, not of the failure kind: an unknown
session is a 404 envelope on ``/api/sessions/<id>``, while an unknown user is a
404 ``message`` on ``/api/iam/users/<id>``. So each class carries the shape its
majority of call sites use as a ClassVar default, and a single ``shape=``
argument selects the other at the raise site. Picking the wrong one is a wire
regression, so the rule is: **match what the handler you are migrating already
returns — read its ``return`` statements, do not assume.**
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal, final

from mewbo_iam import PermissionCatalog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mewbo_api.responses import ApiResponseKit

# Which of the two documented wire shapes a refusal renders as.
WireShape = Literal["message", "envelope"]


class ErrorPayload(BaseModel):
    """The validated data behind one refusal, renderable in either wire shape.

    Frozen and ``extra="forbid"``: a refusal body is a wire contract like any
    other, and a field smuggled onto one is a bug that must surface at
    construction rather than reaching a client.

    Field ORDER is load-bearing, not cosmetic. ``model_dump`` preserves
    declaration order, and the envelope's ``{code, reason, retryable}`` ordering
    is what makes a rendered body byte-identical to the hand-built dicts this
    replaces — including the console's byte-locked ``session_terminated``
    sentinel.

    ``code`` is ``int | str`` because both live on the wire today: the app-level
    404 handler emits the numeric status, while the terminated-session envelope
    emits the semantic token ``"session_terminated"``. Collapsing them to one
    type would break one of the two.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: int | str = Field(description="HTTP status echoed in the body, or a semantic token.")
    reason: str = Field(description="Human-readable failure reason; the 'message' body verbatim.")
    retryable: bool = Field(
        default=False, description="Whether retrying the same request unchanged may succeed."
    )

    @field_validator("reason")
    @classmethod
    def _require_reason(cls, value: str) -> str:
        """A refusal with an empty reason tells a client nothing. Refuse it."""
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value


class InvalidRequestPayload(ErrorPayload):
    """A 400 payload that must NAME the thing it is refusing.

    ``field`` is required and validated non-empty at definition. That is the
    entire point of the subclass: "invalid request body" is unactionable, and a
    400 that does not say which field failed sends the caller to read the source.
    Pydantic's own ``ValidationError`` already knows the location, so
    :meth:`RequestInvalid.from_validation_error` carries it across rather than
    discarding it.
    """

    field: str = Field(description="The offending field's location, e.g. 'roles.0'.")

    @field_validator("field")
    @classmethod
    def _require_field(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must name the offending request field")
        return value


class PermissionDeniedPayload(ErrorPayload):
    """A 403 payload naming the catalog permission the caller lacked.

    The id is validated against :class:`~mewbo_iam.PermissionCatalog` AT
    CONSTRUCTION, for the same reason ``PermissionRequirement`` validates the ids
    a decorator declares: a typo'd permission is a refusal that can never be
    satisfied by any role, and it must surface as a loud failure rather than as a
    403 nobody can act on.

    The id does NOT reach the wire — the body stays the byte-identical
    ``{"message": "insufficient role"}`` the guard already returns. It is carried
    for the log and the audit trail, which is where "denied WHAT" belongs.
    """

    permission: str = Field(description="Catalog permission id the principal lacked.")

    @field_validator("permission")
    @classmethod
    def _validate_catalog_id(cls, value: str) -> str:
        if not PermissionCatalog.is_valid(value):
            known = ", ".join(sorted(PermissionCatalog.ALL))
            raise ValueError(f"unknown permission id {value!r}. Valid ids are: {known}")
        return value


class ApiError(Exception):
    """Base class: a refusal that knows its own status and renders its own body.

    Subclasses own ``status`` as a ClassVar — the status is intrinsic to the
    failure kind, so it is never passed in at a raise site and can never be
    mismatched to the class. ``wire_shape`` is the surface-default, overridable
    per raise via ``shape=`` (see the module docstring for why the shape belongs
    to the surface rather than the kind).

    Rendering is pure: :meth:`response` returns ``(body, status)``, the exact
    tuple shape every handler in this app already returns, so a migrated handler
    can raise or return with no adapter in between. :meth:`to_response` is the
    Flask-facing sibling for the registered errorhandler.
    """

    status: ClassVar[int]
    wire_shape: ClassVar[WireShape] = "envelope"

    def __init__(self, payload: ErrorPayload, *, shape: WireShape | None = None) -> None:
        """Capture the validated *payload* and the optional per-raise shape."""
        self.payload = payload
        self.shape: WireShape = shape or self.wire_shape
        super().__init__(payload.reason)

    def body(self) -> dict[str, Any]:
        """The wire body in this refusal's shape.

        ``message`` renders the reason alone. ``envelope`` renders
        ``{code, reason, retryable}`` in declaration order, dropping subclass
        fields (``field``, ``permission``) — those are for the log and the audit
        trail, and adding them to the wire would change bodies this module exists
        to preserve.
        """
        if self.shape == "message":
            return {"message": self.payload.reason}
        return {
            "error": ErrorPayload(
                code=self.payload.code,
                reason=self.payload.reason,
                retryable=self.payload.retryable,
            ).model_dump()
        }

    def response(self) -> tuple[dict[str, Any], int]:
        """``(body, status)`` — the tuple every handler in this app returns."""
        return self.body(), self.status

    def to_response(self) -> tuple[dict[str, Any], int]:
        """What the Flask errorhandler returns. Flask jsonifies the dict."""
        return self.response()

    @property
    def data(self) -> dict[str, Any]:
        """The rendered body, under the attribute name flask-restx reads.

        NOT decoration and NOT for our own use — this exists solely to defeat a
        flask-restx behavior that would otherwise corrupt every envelope body
        raised inside a ``Resource``. ``Api.handle_error`` runs
        ``default_data["message"] = default_data.get("message", str(e))`` on the
        handler's returned body, appending a stray ``message`` key beside our
        ``error`` object. The very next line is
        ``data = getattr(e, "data", default_data)``, so an exception that
        supplies its own ``data`` is taken verbatim and skips that injection.

        Measured before the fix: a raised 404 arrived as
        ``{"error": {...}, "message": "session abc not found. You have requested
        this URI ... did you mean ...?"}``.

        This closes the injection. The 404 "did you mean" half is a SECOND,
        separate mutation that runs after this attribute is read, and is disabled
        by ``RESTX_ERROR_404_HELP = False`` — see
        :func:`register_api_error_handler`.
        """
        return self.body()


@final
class RequestInvalid(ApiError):
    """400 — the request body or parameters are malformed, missing, or invalid.

    Defaults to the ``message`` shape: this is the legacy validation shape the
    ``/api`` routes and every admin surface already return.
    """

    status: ClassVar[int] = 400
    wire_shape: ClassVar[WireShape] = "message"

    @classmethod
    def field_error(
        cls, field: str, reason: str, *, shape: WireShape | None = None
    ) -> RequestInvalid:
        """A 400 naming the offending *field* and why it was refused."""
        return cls(
            InvalidRequestPayload(code=cls.status, reason=reason, field=field, retryable=False),
            shape=shape,
        )

    @classmethod
    def from_validation_error(
        cls, exc: ValidationError, *, shape: WireShape | None = None
    ) -> RequestInvalid:
        """Map a Pydantic ``ValidationError`` to a 400, keeping the field location.

        Renders the ``"<location>: <msg>"`` string the admin surfaces put on the
        wire. Only the FIRST failure is reported — the established contract on
        these routes, and the one a form can actually act on.
        """
        errors = exc.errors()
        if not errors:
            return cls.field_error("body", "invalid request body", shape=shape)
        first = errors[0]
        location = ".".join(str(part) for part in first.get("loc", ())) or "body"
        return cls.field_error(
            location, f"{location}: {first.get('msg', 'invalid value')}", shape=shape
        )


@final
class AuthenticationRequired(ApiError):
    """401 — no credential was presented, or the one presented did not resolve.

    The two bodies below are the ONLY 401s this API returns, and both are
    byte-locked to what ``AuthKit.require_api_key`` / ``require_master_token``
    already produce. They are classmethods rather than call-site strings so the
    next surface cannot invent a third spelling.
    """

    status: ClassVar[int] = 401
    wire_shape: ClassVar[WireShape] = "message"

    #: Byte-locked reasons. The kit returns these exact strings today.
    MISSING_CREDENTIAL: ClassVar[str] = "API token is not provided."
    INVALID_CREDENTIAL: ClassVar[str] = "Unauthorized"

    @classmethod
    def missing_credential(cls, *, shape: WireShape | None = None) -> AuthenticationRequired:
        """No ``X-API-Key`` header or ``api_key`` query was presented at all."""
        return cls(
            ErrorPayload(code=cls.status, reason=cls.MISSING_CREDENTIAL, retryable=False),
            shape=shape,
        )

    @classmethod
    def invalid_credential(cls, *, shape: WireShape | None = None) -> AuthenticationRequired:
        """A credential was presented but did not resolve to a principal.

        Deliberately indistinguishable from "wrong token" / "revoked" / "expired"
        on the wire: the audit trail carries the real reason, and a 401 that
        discriminates is an oracle.
        """
        return cls(
            ErrorPayload(code=cls.status, reason=cls.INVALID_CREDENTIAL, retryable=False),
            shape=shape,
        )


@final
class PermissionDenied(ApiError):
    """403 — authenticated, but the principal lacks the required permission.

    The body is the guard's byte-identical ``{"message": "insufficient role"}``.
    The permission id rides on the payload for the log and audit trail only.
    """

    status: ClassVar[int] = 403
    wire_shape: ClassVar[WireShape] = "message"

    #: Byte-locked reason — what the permission guard returns today.
    INSUFFICIENT_ROLE: ClassVar[str] = "insufficient role"

    @classmethod
    def insufficient_role(
        cls, permission: str, *, shape: WireShape | None = None
    ) -> PermissionDenied:
        """Deny for a catalog *permission*, validated at construction."""
        return cls(
            PermissionDeniedPayload(
                code=cls.status,
                reason=cls.INSUFFICIENT_ROLE,
                permission=permission,
                retryable=False,
            ),
            shape=shape,
        )

    @property
    def permission(self) -> str:
        """The catalog permission id this refusal was raised for."""
        assert isinstance(self.payload, PermissionDeniedPayload)  # noqa: S101 - class invariant
        return self.payload.permission


@final
class ResourceNotFound(ApiError):
    """404 — the referenced resource does not exist.

    Envelope by default, matching the app-level ``@app.errorhandler(NotFound)``
    and ``_session_not_found`` that the MCP facade's not-found mapping reads. The
    IAM/admin surfaces pass ``shape="message"``.
    """

    status: ClassVar[int] = 404
    wire_shape: ClassVar[WireShape] = "envelope"

    @classmethod
    def for_reason(cls, reason: str, *, shape: WireShape | None = None) -> ResourceNotFound:
        """A 404 whose *reason* names what was not found."""
        return cls(ErrorPayload(code=cls.status, reason=reason, retryable=False), shape=shape)


@final
class StateConflict(ApiError):
    """409 — the resource is in a conflicting state for this operation.

    ``message`` by default: every 409 in the app today (duplicate team slug,
    built-in role, an already-active run) rides the legacy shape.
    """

    status: ClassVar[int] = 409
    wire_shape: ClassVar[WireShape] = "message"

    @classmethod
    def for_reason(
        cls, reason: str, *, retryable: bool = False, shape: WireShape | None = None
    ) -> StateConflict:
        """A 409 explaining the conflicting state."""
        return cls(ErrorPayload(code=cls.status, reason=reason, retryable=retryable), shape=shape)


@final
class SessionTerminated(ApiError):
    """410 — the session is permanently terminated. The ONE byte-locked envelope.

    The console matches this body's ``session_terminated`` code exactly, so it
    must never drift. :meth:`response` therefore DELEGATES to
    ``ApiResponseKit.terminated_response()`` rather than re-rendering: the body
    cannot diverge from the constant even if this class is later edited, because
    this class does not build it.

    The payload is still constructed from the same constant so the generic render
    path stays meaningful (and is asserted equal to the delegated body in
    verification) — but the delegation, not that agreement, is the guarantee.
    """

    status: ClassVar[int] = 410
    wire_shape: ClassVar[WireShape] = "envelope"

    def __init__(self) -> None:
        """Build the payload from the ONE terminated-session constant."""
        body = ApiResponseKit.TERMINATED_ERROR_BODY
        super().__init__(
            ErrorPayload(
                code=str(body["code"]),
                reason=str(body["reason"]),
                retryable=bool(body["retryable"]),
            )
        )

    def response(self) -> tuple[dict[str, Any], int]:
        """The ONE 410 response, taken verbatim from ``ApiResponseKit``."""
        return ApiResponseKit.terminated_response()


def register_api_error_handler(app: Any, api: Any = None) -> None:
    """Register the :class:`ApiError` handler on *app* AND on the RESTX *api*.

    **BOTH registrations are required, and this was verified empirically rather
    than assumed.** A Flask ``@app.errorhandler`` alone does NOT cover a
    ``Resource``: flask-restx installs its own ``error_router`` ahead of Flask's
    dispatch, and for any route it owns it hands the exception to
    ``Api.handle_error``, which knows nothing about ``ApiError`` and renders a
    generic ``500 {"message": "Internal Server Error"}``. Measured: with only the
    app-level handler, a 404 raised inside a ``Resource.get`` arrives as a 500.

    So the app registration serves the Blueprint surfaces (wiki, IAM, git
    credentials, auth) and the ``@api.errorhandler`` registration serves every
    ``Resource``. Both render through the same :meth:`ApiError.to_response`, so
    the two surfaces cannot drift.

    Call ONCE at boot, near the ``Api(app, ...)`` setup, alongside the existing
    ``@app.errorhandler(NotFound)``. Passing *api* is optional only so a test can
    mount the app half alone; production must pass both.

    **Sets ``RESTX_ERROR_404_HELP = False``, and that is required for
    correctness, not taste.** With it left at its default, flask-restx rewrites
    ``data["message"]`` on any 404 it renders, appending "You have requested this
    URI [...] but did you mean /x or /y ?" — which both re-adds the stray
    ``message`` key that :attr:`ApiError.data` exists to prevent and leaks a list
    of nearby route paths to an unauthenticated caller. Neither is wanted on a
    JSON API. The app-level ``@app.errorhandler(NotFound)`` that serves unmatched
    routes is unaffected: flask-restx only renders errors for routes it owns.
    """
    app.config["RESTX_ERROR_404_HELP"] = False

    @app.errorhandler(ApiError)
    def _handle_api_error(exc: ApiError) -> tuple[dict[str, Any], int]:
        return exc.to_response()

    if api is None:
        return

    @api.errorhandler(ApiError)
    def _handle_restx_api_error(exc: ApiError) -> tuple[dict[str, Any], int]:
        """Render a refusal raised inside a flask-restx ``Resource``.

        RESTX ignores a handler's returned status and re-reads it from a ``code``
        attribute on the exception in some paths, so the status is returned
        explicitly here AND the body is the same one the app handler produces.
        """
        return exc.to_response()


__all__ = [
    "ApiError",
    "AuthenticationRequired",
    "ErrorPayload",
    "InvalidRequestPayload",
    "PermissionDenied",
    "PermissionDeniedPayload",
    "RequestInvalid",
    "ResourceNotFound",
    "SessionTerminated",
    "StateConflict",
    "WireShape",
    "register_api_error_handler",
]
