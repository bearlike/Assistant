"""Shared test fixtures for the MCP tool tests.

We stub ONLY the HTTP boundary (per the repo testing rules) by injecting an
``httpx.MockTransport`` into :class:`~mewbo_mcp.rest.RestClient`. The
``FakeRest`` helper records every outbound request and replies from a route
table keyed by ``(METHOD, path)`` — so tests assert the exact REST
path/method/body the tools emit and the shaping of the (faked) response.

Exposed as the ``fake_rest`` pytest fixture so tests get it via dependency
injection (rather than ``from conftest import ...``, which would clash with
the repository-root ``conftest.py`` on ``sys.path``).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from mewbo_mcp.rest import RestClient

Handler = Callable[[httpx.Request], httpx.Response]


@dataclass
class RecordedRequest:
    """A captured outbound request."""

    method: str
    path: str
    json: Any
    headers: dict[str, str]
    params: dict[str, str]


@dataclass
class FakeRest:
    """A fake REST backend backed by an injectable httpx transport."""

    routes: dict[tuple[str, str], Any] = field(default_factory=dict)
    handlers: dict[tuple[str, str], Handler] = field(default_factory=dict)
    requests: list[RecordedRequest] = field(default_factory=list)

    def on(self, method: str, path: str, response: Any, status: int = 200) -> FakeRest:
        """Register a canned JSON ``response`` for ``METHOD path``."""
        self.routes[(method.upper(), path)] = (status, response)
        return self

    def on_handler(self, method: str, path: str, handler: Handler) -> FakeRest:
        """Register a dynamic ``handler`` for ``METHOD path`` (e.g. poll loops)."""
        self.handlers[(method.upper(), path)] = handler
        return self

    def on_sse(self, method: str, path: str, frames: str, status: int = 200) -> FakeRest:
        """Register an SSE (``text/event-stream``) response body for ``METHOD path``.

        ``frames`` is the raw SSE body; the RestClient streams it line-by-line
        (``aiter_lines``), exactly like the real wiki QA endpoint.
        """

        def _handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status,
                content=frames.encode(),
                headers={"content-type": "text/event-stream"},
            )

        self.handlers[(method.upper(), path)] = _handler
        return self

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        body: Any = None
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = request.content.decode()
        self.requests.append(
            RecordedRequest(
                method=request.method,
                path=request.url.path,
                json=body,
                headers=dict(request.headers),
                params=dict(request.url.params),
            )
        )
        key = (request.method, request.url.path)
        if key in self.handlers:
            return self.handlers[key](request)
        if key in self.routes:
            status, payload = self.routes[key]
            if isinstance(payload, str):
                return httpx.Response(status, text=payload)
            return httpx.Response(status, json=payload)
        return httpx.Response(404, json={"message": f"no route for {key}"})

    def client(self, token: str = "mk_test") -> RestClient:
        """Return a RestClient wired to this fake backend with ``token``."""
        transport = httpx.MockTransport(self._dispatch)
        return RestClient("http://api.test", token, transport=transport)

    # Convenience assertions ------------------------------------------------

    def find(self, method: str, path: str) -> RecordedRequest:
        """Return the (first) recorded request matching ``METHOD path``."""
        for req in self.requests:
            if req.method == method.upper() and req.path == path:
                return req
        raise AssertionError(f"no recorded request for {method} {path}: {self.paths()}")

    def paths(self) -> list[str]:
        """Return ``"METHOD path"`` for every recorded request (for diagnostics)."""
        return [f"{r.method} {r.path}" for r in self.requests]


@pytest.fixture
def fake_rest() -> FakeRest:
    """Provide a fresh FakeRest backend per test."""
    return FakeRest()
