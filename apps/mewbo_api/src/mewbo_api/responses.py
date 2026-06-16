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
- **message** ``{"message": "..."}`` — the legacy auth/validation shape some
  ``/api`` routes still return. Pass ``shape="message"`` (or use
  :meth:`ApiResponseKit.auth_error`) where a route returns this.

Match the shape to what the route's ``return`` statements actually produce;
when in doubt, read the handler.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from flask_restx import fields

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
                    description="Human-readable failure reason (legacy auth/validation shape).",
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
                desc = overrides.get(code) or _ERROR_CATALOG[code][0]
                model = self._model_for(shape, code)
                func = self.r.response(code, desc, model)(func)
            return func

        return decorator

    def auth_error(self, *, code: int = 401) -> Callable:
        """Document the legacy ``{"message": ...}`` auth failure on a route."""
        return self.errors(code, shape="message")

    # ── internals ───────────────────────────────────────────────────────────
    def _model_for(self, shape: str, code: int) -> Any:
        key = (shape, code)
        if key in self._cache:
            return self._cache[key]
        if shape == "message":
            model = self.r.model(
                f"{self.prefix}MessageError{code}",
                {
                    "message": fields.String(
                        example=_ERROR_CATALOG[code][1],
                        description="Human-readable failure reason.",
                    )
                },
            )
        else:
            _, reason, retryable = _ERROR_CATALOG[code]
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
