"""WikiError → HTTP status mapping + Flask error handler."""
from __future__ import annotations

from flask import jsonify
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


def register_error_handler(app) -> None:
    """Register the WikiHTTPError handler on *app*."""

    @app.errorhandler(WikiHTTPError)
    def _handle(exc: WikiHTTPError):
        return wiki_error_response(exc.error, exc.status)
