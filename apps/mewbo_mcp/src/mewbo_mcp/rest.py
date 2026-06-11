"""Thin async HTTP client for the Mewbo REST API.

One small wrapper centralizes the request-header contract: every request
forwards the caller's token as ``X-API-Key`` (the header the REST API
expects — see ``apps/mewbo_api/backend.py:_request_credential``) and stamps the
originating surface as ``X-Mewbo-Surface: mcp`` so MCP-invoked sessions are
tagged ``surface:mcp``. Tools never construct headers themselves; they go
through :class:`RestClient`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx


class RestError(Exception):
    """A non-2xx response (or transport failure) from the REST API."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        """Store the human-readable message and optional HTTP status."""
        super().__init__(message)
        self.status_code = status_code


class RestClient:
    """Async client: injects the caller's token as ``X-API-Key`` + surface ``X-Mewbo-Surface: mcp``.

    Construct one per tool call with the authenticated token. The client is an
    async context manager so the underlying connection is always closed.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Build the client. ``transport`` is injectable for testing (no live server)."""
        # Structured timeout: keep the ``timeout`` read budget for individual fast
        # calls but bound connect to 10s. The aggregate long-running-tool runtime
        # risk is handled upstream by the lowered poll budgets (#41) — each is held
        # strictly under this read ceiling so the tool returns a resumable handle
        # before any transport/proxy timeout.
        # ``X-Mewbo-Surface`` stamps the originating client surface onto every
        # session run (the API defaults it to ``"api"``); sending ``"mcp"`` here
        # tags MCP-invoked sessions — create/followup/structured_query — as
        # ``surface:mcp`` in Langfuse. Set once alongside ``X-API-Key`` so it
        # rides every request and tools never construct headers themselves.
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"X-API-Key": token, "X-Mewbo-Surface": "mcp"},
            timeout=httpx.Timeout(timeout, connect=10.0),
            transport=transport,
        )

    async def __aenter__(self) -> RestClient:
        """Enter the async context (returns self)."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Issue a request and return the parsed JSON body.

        Raises :class:`RestError` on transport failure or a non-2xx status,
        surfacing the API's ``message``/``error`` field when present so the
        MCP caller gets an actionable reason rather than a bare status code.
        """
        try:
            resp = await self._client.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:  # transport-level failure
            raise RestError(f"Request to {method} {path} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise RestError(_error_message(resp), status_code=resp.status_code)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """Issue a GET request."""
        return await self.request("GET", path, params=params)

    async def post(
        self, path: str, *, json: Any | None = None, params: dict[str, Any] | None = None
    ) -> Any:
        """Issue a POST request."""
        return await self.request("POST", path, json=json, params=params)

    async def stream_lines(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """Stream a request body line-by-line (for SSE endpoints).

        Yields decoded text lines as they arrive so callers can read just the
        first few frames of a never-ending SSE stream and disconnect. Raises
        :class:`RestError` on a non-2xx status. The underlying connection is
        always closed when the iterator is exhausted or abandoned.
        """
        try:
            async with self._client.stream(method, path, json=json, params=params) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise RestError(_error_message(resp), status_code=resp.status_code)
                async for line in resp.aiter_lines():
                    yield line
        except httpx.HTTPError as exc:  # transport-level failure
            raise RestError(f"Stream of {method} {path} failed: {exc}") from exc


def _error_message(resp: httpx.Response) -> str:
    """Best-effort extraction of an API error message from a failed response.

    Never leaks a raw HTML body (e.g. Werkzeug's default 404 page): a non-JSON
    body is capped to a short hint and an HTML page is dropped entirely, so a
    consuming agent gets an actionable reason instead of a kilobyte of markup.
    """
    detail = _error_detail(resp)
    suffix = f": {detail}" if detail else ""
    return f"REST API returned {resp.status_code}{suffix}"


def _error_detail(resp: httpx.Response) -> str:
    """Pull a clean reason from a failed response; a terse hint when none is available.

    Priority:
    1. Structured ``{"error": {"reason": ...}}`` or ``{"message": ...}`` body.
    2. Short plain-text body (capped, never HTML).
    3. Terse ``"<method> <path>"`` hint so the caller knows which call failed
       (better than a bare status code with no context).
    """
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        # Structured envelope ``{"error": {"reason": ...}}`` (the API's contract).
        err = body.get("error")
        if isinstance(err, dict) and err.get("reason"):
            return str(err["reason"])
        if isinstance(err, str) and err:
            return err
        if isinstance(body.get("message"), str) and body["message"]:
            return str(body["message"])
        # Structured JSON but no useful reason field — fall through to the hint.
    else:
        # Non-JSON body: never dump raw HTML; short plain text is ok.
        text = (resp.text or "").strip()
        if text:
            head = text[:200].lower()
            if "<html" not in head and "<!doctype" not in head:
                return text[:200]
    # Empty body or HTML page: emit a terse, actionable transport hint.
    try:
        url = str(resp.url)
    except RuntimeError:
        url = ""
    return f"no error body (check connectivity to {url})" if url else "no error body"


__all__ = ["RestClient", "RestError"]
