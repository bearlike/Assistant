"""Thin async HTTP client for the Mewbo REST API.

One small wrapper centralizes the token pass-through contract: every request
forwards the caller's token as ``X-API-Key`` (the header the REST API
expects — see ``apps/mewbo_api/backend.py:_request_credential``). Tools never
construct headers themselves; they go through :class:`RestClient`.
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
    """Async client that injects the caller's token as ``X-API-Key``.

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
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"X-API-Key": token},
            timeout=timeout,
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
    """Best-effort extraction of an API error message from a failed response."""
    detail: str = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            detail = str(body.get("message") or body.get("error") or "")
    except ValueError:
        detail = resp.text.strip()
    suffix = f": {detail}" if detail else ""
    return f"REST API returned {resp.status_code}{suffix}"


__all__ = ["RestClient", "RestError"]
