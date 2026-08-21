"""Shared OpenAPI response documentation kit for the Mewbo REST API.

Flask-RESTX builds the live Swagger 2.0 schema that ``scripts/ci/
generate_openapi_spec.py`` exports to ``docs/openapi.json`` and the docs site
renders with Scalar. Scalar synthesizes a sample request/response body from a
model's field ``example=`` values, so rich field examples are how every
operation gets a *real* sample output — for the success path and for every
error code it can emit.

This module is the one DRY home for the **error** half of that contract. It
declares the two error wire-shapes the API actually returns and exposes a
combinator that attaches example-bearing error responses to a Resource method
without repeating ``@ns.response(...)`` lines per route.

Each route module owns one kit, built from its **module-level namespace** (so
the decorators that run at import time can see it) with a unique ``prefix`` that
namespaces the generated model names — Flask-RESTX resolves every model on the
shared ``Api`` registry, so two namespaces minting an ``ErrorEnvelope`` would
collide without distinct prefixes::

    # at module level, right after the Namespace is created
    kit = ApiResponseKit(agentic_ns, prefix="Search")
    ...
    class Workspaces(Resource):
        @kit.errors(400, 401)                         # documents 400 + 401 with examples
        @agentic_ns.response(201, "Workspace created.", workspace_model)
        def post(self): ...

Two wire-shapes exist in the codebase and both are documented faithfully (this
module never changes what a route *returns* — it only documents it):

- **envelope** ``{"error": {"code", "reason", "retryable"}}`` — the canonical
  shape (``@app.errorhandler(NotFound)``, the structured + agentic-search
  surfaces). Default for :meth:`ApiResponseKit.errors`.
- **message** ``{"message": "..."}`` — the auth/validation shape some
  ``/api`` routes still return. Pass ``shape="message"`` (or use
  :meth:`ApiResponseKit.auth_error`) where a route returns this.

Match the shape to what the route's ``return`` statements actually produce;
when in doubt, read the handler.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from flask_restx import fields
from mewbo_core.config import ConfigWriteError

# code -> (default description, example ``reason``, example ``retryable``).
# Descriptions are the generic meaning of the status on this API; pass a
# per-route ``descriptions=`` override to :meth:`ApiResponseKit.errors` when a
# route narrows the meaning (e.g. 409 "a run is already active").
_ERROR_CATALOG: dict[int, tuple[str, str, bool]] = {
    400: (
        "Malformed, missing, or invalid request body or parameters.",
        "query is required",
        False,
    ),
    401: ("Missing or invalid API key.", "API token is not provided.", False),
    403: (
        "Not permitted: a protected field, a disabled feature, or a "
        "master-token-only action.",
        "master token required",
        False,
    ),
    404: (
        "The referenced resource does not exist.",
        "session 9e2d47c1 not found",
        False,
    ),
    409: (
        "The resource is in a conflicting state for this operation.",
        "a structured run is already active for this session",
        True,
    ),
    413: (
        "The uploaded payload exceeds the limit the endpoint publishes.",
        "Audio upload exceeds the 10485760 byte limit (received 12000000 bytes).",
        False,
    ),
    422: (
        "Understood but unprocessable — the request could not be carried out.",
        "the run could not be started",
        True,
    ),
    429: (
        "Rate limited. Retry after the delay in the `Retry-After` header.",
        "rate limited",
        True,
    ),
    500: ("Unexpected server error.", "internal error", True),
    502: (
        "An upstream collaborator (e.g. a pipeline runner) raised while handling the request.",
        "pipeline execution failed: connection refused",
        True,
    ),
    503: (
        "The feature is not configured or is temporarily unavailable.",
        "structured responses are not configured on this server",
        True,
    ),
}


class ApiResponseKit:
    """Declares a namespace's error wire-shapes once and documents routes.

    Build one per route module from that module's namespace. ``registrar`` is
    any object exposing Flask-RESTX ``.model()`` and ``.response()`` — a
    ``Namespace`` (normal case) or the root ``Api``. ``prefix`` namespaces the
    generated model names so distinct namespaces never collide on the shared
    registry. State: two base models plus a per-(shape, code) cache of
    example-bearing error models.
    """

    # The ONE wire body every mutating surface returns for a permanently
    # terminated session. Both ``backend.py`` and ``triggers/routes.py``
    # (and the structured GET) reject with this exact shape; the console's
    # ``session_terminated`` sentinel matches it byte-for-byte, so it must never
    # drift — hence it lives HERE, on the response kit both surfaces already
    # import, rather than in a bare constant module that would re-open a
    # backend↔routes import cycle. ``code`` is the semantic token (a string, not
    # an HTTP status) and ``retryable`` is false: a terminated session never
    # comes back.
    TERMINATED_ERROR_BODY: dict[str, object] = {
        "code": "session_terminated",
        "reason": "Session is permanently terminated",
        "retryable": False,
    }

    @classmethod
    def terminated_response(cls) -> tuple[dict, int]:
        """The ONE 410 Gone response for a permanently terminated session."""
        return {"error": cls.TERMINATED_ERROR_BODY}, 410

    @classmethod
    def config_write_error_response(cls, exc: ConfigWriteError) -> tuple[dict, int]:
        """The ONE 500 body for a configuration write a client cannot retry past.

        Sibling to :meth:`terminated_response`: ``PATCH /api/config`` is the
        only caller today, but the shape lives here so it can never drift from
        what ``ConfigResource.patch`` actually returns. Stays 500 rather than
        503 — the request itself was valid and the failure isn't transient, it
        needs an operator to fix the store (read-only mount, permission,
        disk), so inviting a client retry/backoff would be wrong. ``code`` is
        the machine-readable reason (``read_only`` / ``permission_denied`` /
        ``no_space`` / ``io_error``, from ``mewbo_core.config.ConfigWriteError``);
        ``message`` is the actionable reason text. The server-side path lives
        on ``exc.path`` for the caller to log — it must never reach this body.
        """
        return {"message": exc.reason, "code": exc.code}, 500

    def __init__(self, registrar: Any, prefix: str = "") -> None:
        """Build the base error models on *registrar*, prefixed by *prefix*."""
        self.r = registrar
        self.prefix = prefix
        self._error_body = registrar.model(
            f"{prefix}ErrorBody",
            {
                "code": fields.Integer(
                    example=404, description="HTTP status code, echoed in the body."
                ),
                "reason": fields.String(
                    example="session 9e2d47c1 not found",
                    description="Human-readable failure reason.",
                ),
                "retryable": fields.Boolean(
                    example=False,
                    description="Whether retrying the same request unchanged may succeed.",
                ),
            },
        )
        self.envelope = registrar.model(
            f"{prefix}ErrorEnvelope",
            {
                "error": fields.Nested(
                    self._error_body,
                    description="The error envelope returned by most endpoints.",
                )
            },
        )
        self.message = registrar.model(
            f"{prefix}MessageError",
            {
                "message": fields.String(
                    example="API token is not provided.",
                    description="Human-readable failure reason (auth/validation shape).",
                )
            },
        )
        # (shape, code) -> concrete example-bearing model
        self._cache: dict[tuple[str, int], Any] = {}

    # ── public API ──────────────────────────────────────────────────────────
    def errors(
        self,
        *codes: int,
        shape: str = "envelope",
        descriptions: dict[int, str] | None = None,
    ) -> Callable:
        """Decorator: document *codes* on a Resource method, with examples.

        ``shape`` selects the wire-shape (``"envelope"`` default or
        ``"message"``). ``descriptions`` overrides the generic per-status text
        for routes that narrow a code's meaning. Codes are documented in
        ascending order regardless of call order.
        """
        overrides = descriptions or {}

        def decorator(func: Callable) -> Callable:
            for code in sorted(codes, reverse=True):
                desc = overrides.get(code) or self._catalog_entry(code)[0]
                model = self._model_for(shape, code)
                func = self.r.response(code, desc, model)(func)
            return func

        return decorator

    def auth_error(self, *, code: int = 401) -> Callable:
        """Document the ``{"message": ...}`` auth failure on a route."""
        return self.errors(code, shape="message")

    # ── internals ───────────────────────────────────────────────────────────
    @staticmethod
    def _catalog_entry(code: int) -> tuple[str, str, bool]:
        """The catalog row for *code*, or a failure that NAMES what is missing.

        This lookup runs at DECORATION — module scope, which in this app is boot
        — so a miss does not fail one request, it makes ``mewbo_api.backend``
        unimportable and takes the whole API down. Failing fast is right (a route
        documenting a status the reference cannot describe is a contract hole),
        but a bare ``KeyError: 413`` names neither the file to edit nor the fact
        that a ROUTE caused it. Measured: that exact traceback is what a new
        namespace declaring an undocumented status produces, and it reads as a
        dict bug rather than as a missing catalog row.
        """
        try:
            return _ERROR_CATALOG[code]
        except KeyError:
            known = ", ".join(str(c) for c in sorted(_ERROR_CATALOG))
            raise KeyError(
                f"HTTP {code} has no _ERROR_CATALOG entry in "
                f"mewbo_api/responses.py, so a route declaring it cannot be "
                f"documented and the app fails to IMPORT. Add a row for {code} "
                f"(description, example reason, retryable). Known: {known}."
            ) from None

    def _model_for(self, shape: str, code: int) -> Any:
        key = (shape, code)
        if key in self._cache:
            return self._cache[key]
        if shape == "message":
            model = self.r.model(
                f"{self.prefix}MessageError{code}",
                {
                    "message": fields.String(
                        example=self._catalog_entry(code)[1],
                        description="Human-readable failure reason.",
                    )
                },
            )
        else:
            _, reason, retryable = self._catalog_entry(code)
            body = self.r.model(
                f"{self.prefix}ErrorBody{code}",
                {
                    "code": fields.Integer(example=code),
                    "reason": fields.String(example=reason),
                    "retryable": fields.Boolean(example=retryable),
                },
            )
            model = self.r.model(
                f"{self.prefix}ErrorEnvelope{code}", {"error": fields.Nested(body)}
            )
        self._cache[key] = model
        return model
