"""A discarded MCP session must be reconnected, not retried into.

A streamable-HTTP server answers 404 to a request carrying a session id it has
already dropped, and every subsequent call on that session fails identically.
Waiting for the consecutive-error threshold spends two more round trips proving
what the first failure established.
"""

from __future__ import annotations

import asyncio

import pytest
from mewbo_tools.integration.mcp_pool import (
    MAX_ERRORS_BEFORE_RECONNECT,
    MCPConnectionPool,
    ServerState,
)


class _FakeTool:
    """A bound MCP tool; the transport boundary and nothing else."""

    def __init__(self, name: str, *, errors: list[Exception | None]) -> None:
        self.name = name
        self._errors = list(errors)
        self.calls = 0

    async def ainvoke(self, payload):
        self.calls += 1
        error = self._errors.pop(0) if self._errors else None
        if error is not None:
            raise error
        return f"ok:{payload}"


def _pool_with(tool: _FakeTool, reconnect_tool: _FakeTool | None = None) -> MCPConnectionPool:
    """A pool holding one live server, with reconnection stubbed at the transport."""
    pool = MCPConnectionPool()
    pool._servers["srv"] = ServerState(
        name="srv", config={}, client=object(), tools=[tool], connected=True
    )
    pool._mcp_config = {"servers": {"srv": {}}}

    async def _fake_connect(name: str, config: dict) -> ServerState:
        return ServerState(
            name=name,
            config=config,
            client=object(),
            tools=[reconnect_tool if reconnect_tool is not None else tool],
            connected=True,
        )

    pool._connect_single = _fake_connect  # type: ignore[method-assign]
    return pool


def _call(pool: MCPConnectionPool):
    return asyncio.run(pool.call_tool("srv", "search", {"q": "x"}))


@pytest.mark.parametrize(
    "message",
    [
        "Session not found",
        "HTTP 404: invalid session id",
        "Session terminated by server",
        "anyio ClosedResourceError",
    ],
)
def test_invalid_session_reconnects_on_the_first_failure(message):
    """One failure is enough evidence — the session cannot heal by being reused."""
    dead = _FakeTool("search", errors=[RuntimeError(message)])
    fresh = _FakeTool("search", errors=[])
    pool = _pool_with(dead, reconnect_tool=fresh)

    assert _call(pool) == "ok:{'q': 'x'}"
    assert dead.calls == 1
    assert fresh.calls == 1


def test_second_failure_after_reconnect_propagates():
    """Exactly one retry: a fresh session that also fails is a real failure."""
    dead = _FakeTool("search", errors=[RuntimeError("session not found")])
    still_dead = _FakeTool("search", errors=[RuntimeError("session not found")])
    pool = _pool_with(dead, reconnect_tool=still_dead)

    with pytest.raises(RuntimeError, match="session not found"):
        _call(pool)
    assert still_dead.calls == 1


def test_ordinary_failure_still_waits_for_the_error_threshold():
    """Refresh-on-invalid is narrow: an unrelated error keeps the old policy."""
    flaky = _FakeTool("search", errors=[ValueError("tool blew up")])
    pool = _pool_with(flaky)

    with pytest.raises(ValueError, match="tool blew up"):
        _call(pool)
    assert pool._servers["srv"].consecutive_errors == 1
    assert flaky.calls == 1


def test_ordinary_failures_reconnect_once_the_threshold_is_reached():
    """The pre-existing consecutive-error path is untouched."""
    errors = [ValueError("boom")] * MAX_ERRORS_BEFORE_RECONNECT
    flaky = _FakeTool("search", errors=errors)
    fresh = _FakeTool("search", errors=[])
    pool = _pool_with(flaky, reconnect_tool=fresh)

    for _ in range(MAX_ERRORS_BEFORE_RECONNECT - 1):
        with pytest.raises(ValueError):
            _call(pool)
    assert _call(pool) == "ok:{'q': 'x'}"
    assert fresh.calls == 1


class _TaskGroupError(Exception):
    """An anyio-style exception group — duck-typed on ``exceptions``, as the
    unwrap helper is, so this test does not depend on the builtin type."""

    def __init__(self, *children: BaseException) -> None:
        super().__init__("unhandled errors in a TaskGroup (1 sub-exception)")
        self.exceptions = tuple(children)


def test_session_invalid_is_read_through_the_task_group_wrapper():
    """The real cause arrives inside an anyio group whose own text names nothing."""
    wrapper = _TaskGroupError(RuntimeError("Session expired"))

    assert MCPConnectionPool._is_session_invalid(wrapper) is True
    assert MCPConnectionPool._is_session_invalid(RuntimeError("rate limited")) is False
