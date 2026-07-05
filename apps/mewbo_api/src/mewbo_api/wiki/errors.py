"""WikiError → HTTP status mapping + Flask error handler."""
from __future__ import annotations

from flask import jsonify
from mewbo_graph.wiki.errors import DocumentationUnavailableError
from mewbo_graph.wiki.types import WikiError

WIKI_CODE_STATUS: dict[str, int] = {
    "not_found": 404,
    "forbidden": 403,
    "repo_access": 502,
    "quota_exceeded": 429,
    "rate_limited": 429,
    "validation": 400,
    "cancelled": 499,
    "internal": 500,
    "network": 503,
    # A graph-only (developer-mode) project has no documentation pages —
    # a deterministic conflict, retryable=False. 409 distinguishes it from a
    # genuine 404 (the project exists; only its docs don't).
    "documentation_unavailable": 409,
}


class WikiHTTPError(Exception):
    """Wraps a WikiError with the desired HTTP status."""

    def __init__(self, error: WikiError, status: int | None = None) -> None:
        """Wrap *error* and resolve its HTTP status from WIKI_CODE_STATUS."""
        super().__init__(error.message)
        self.error = error
        self.status = status if status is not None else WIKI_CODE_STATUS.get(error.code, 500)


def wiki_error_response(err: WikiError, status: int | None = None):
    """Build a Flask response for a WikiError."""
    if status is None:
        status = WIKI_CODE_STATUS.get(err.code, 500)
    body = err.model_dump(mode="json", exclude_none=True, by_alias=True)
    resp = jsonify(body)
    resp.status_code = status
    if err.code == "rate_limited" and err.retry_after:
        resp.headers["Retry-After"] = str(err.retry_after)
    return resp


def documentation_unavailable_response(slug: str):
    """Build the 409 envelope for a graph-only project's doc read.

    Stable wire contract: ``{code: "documentation_unavailable", message,
    retryable: false}`` at HTTP 409 — same top-level shape as ``WikiError``
    (``code``/``message``) so the FE wiki error path + the MCP ``_error_detail``
    (which reads top-level ``message``) both consume it unchanged, plus an
    explicit ``retryable`` flag (this is deterministic — re-reading won't help).
    """
    from mewbo_graph.wiki.errors import DocumentationUnavailableError  # noqa: PLC0415

    body = {
        "code": "documentation_unavailable",
        "message": str(DocumentationUnavailableError(slug)),
        "retryable": False,
    }
    resp = jsonify(body)
    resp.status_code = WIKI_CODE_STATUS["documentation_unavailable"]
    return resp


def register_error_handler(app) -> None:
    """Register the WikiHTTPError + DocumentationUnavailableError handlers on *app*."""

    @app.errorhandler(WikiHTTPError)
    def _handle(exc: WikiHTTPError):
        return wiki_error_response(exc.error, exc.status)

    @app.errorhandler(DocumentationUnavailableError)
    def _handle_docs_unavailable(exc: DocumentationUnavailableError):
        # Raised DOWN at the store's doc-read seam; every wiki route that reads
        # page content inherits the 409 mapping from this ONE registration.
        return documentation_unavailable_response(exc.slug)
