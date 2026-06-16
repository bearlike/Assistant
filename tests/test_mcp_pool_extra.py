"""Extra tests for MCPConnectionPool.

Targets uncovered lines in mewbo_tools/integration/mcp_pool.py:
- Lines 179-188: get_or_connect fresh-config fallback (server not in mcp_config)
- Lines 218/235: call_tool — tool not found on server
- Lines 240-251: call_tool — reconnect-after-N-errors + retry
- Lines 309/339: refresh_if_config_changed quarantine path + shutdown log

Mock pattern: patch `_connect_single` to avoid real MCP transport.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mewbo_tools.integration.mcp_pool import (
    MAX_ERRORS_BEFORE_RECONNECT,
    MCPConnectionPool,
    ServerState,
    _config_hash,
    _is_config_kwarg_error,
    get_mcp_pool,
    reset_mcp_pool,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _connected_state(name: str, tools: list | None = None) -> ServerState:
    t = tools or []
    return ServerState(name=name, config={}, connected=True, tools=t)


def _make_tool(name: str) -> MagicMock:
    t = MagicMock()
    t.name = name
    t.ainvoke = AsyncMock(return_value=f"result-{name}")
    return t


class _Pool:
    """Context manager that creates and resets a fresh pool."""

    def __enter__(self) -> MCPConnectionPool:
        reset_mcp_pool()
        self.pool = MCPConnectionPool()
        return self.pool

    def __exit__(self, *_):
        reset_mcp_pool()


# ===========================================================================
# _config_hash / _is_config_kwarg_error
# ===========================================================================


class TestHelpers:
    def test_config_hash_is_deterministic(self):
        cfg = {"servers": {"s": {"url": "http://x"}}}
        assert _config_hash(cfg) == _config_hash(cfg)

    def test_config_hash_differs_on_change(self):
        a = {"servers": {"s": {"url": "http://a"}}}
        b = {"servers": {"s": {"url": "http://b"}}}
        assert _config_hash(a) != _config_hash(b)

    def test_is_config_kwarg_error_true(self):
        exc = TypeError("got an unexpected keyword argument 'oauth'")
        assert _is_config_kwarg_error(exc) is True

    def test_is_config_kwarg_error_false_for_other_type_error(self):
        exc = TypeError("something else")
        assert _is_config_kwarg_error(exc) is False

    def test_is_config_kwarg_error_false_for_non_type_error(self):
        exc = ValueError("unexpected keyword argument 'oauth'")
        assert _is_config_kwarg_error(exc) is False


# ===========================================================================
# get_or_connect — server not in mcp_config (fresh config fallback)
# ===========================================================================


class TestGetOrConnectFreshConfig:
    def setup_method(self):
        reset_mcp_pool()
        self.pool = MCPConnectionPool()

    def teardown_method(self):
        reset_mcp_pool()

    def test_raises_when_server_not_in_any_config(self):
        """Server not in _mcp_config and not in fresh config → ValueError."""
        self.pool._mcp_config = {}

        async def _run():
            with (
                patch(
                    "mewbo_tools.integration.mcp._load_mcp_config",
                    return_value={},
                ),
                patch(
                    "mewbo_tools.integration.mcp._normalize_mcp_config",
                    side_effect=lambda c: c,
                ),
            ):
                await self.pool.get_or_connect("unknown-server")

        with pytest.raises(ValueError, match="not found"):
            asyncio.run(_run())

    def test_connects_when_found_in_fresh_config(self):
        """Server missing from _mcp_config but in fresh loaded config → connects."""
        self.pool._mcp_config = {}
        fresh_cfg = {"servers": {"srv": {"url": "http://x"}}}

        async def _run():
            with (
                patch(
                    "mewbo_tools.integration.mcp._load_mcp_config",
                    return_value=fresh_cfg,
                ),
                patch(
                    "mewbo_tools.integration.mcp._normalize_mcp_config",
                    side_effect=lambda c: c,
                ),
                patch.object(
                    self.pool,
                    "_connect_single",
                    return_value=_connected_state("srv"),
                ),
            ):
                return await self.pool.get_or_connect("srv")

        state = asyncio.run(_run())
        assert state.connected

    def test_continues_when_fresh_config_load_fails(self):
        """Exception in _load_mcp_config is swallowed; ValueError raised for server."""
        self.pool._mcp_config = {}

        async def _run():
            with patch(
                "mewbo_tools.integration.mcp._load_mcp_config",
                side_effect=FileNotFoundError("no config"),
            ):
                await self.pool.get_or_connect("srv")

        with pytest.raises(ValueError, match="not found"):
            asyncio.run(_run())


# ===========================================================================
# call_tool — tool not found on server
# ===========================================================================


class TestCallToolNotFound:
    def setup_method(self):
        reset_mcp_pool()
        self.pool = MCPConnectionPool()

    def teardown_method(self):
        reset_mcp_pool()

    def test_tool_not_on_server_returns_error_string(self):
        tool_a = _make_tool("tool_a")
        state = _connected_state("srv", tools=[tool_a])
        self.pool._servers["srv"] = state

        async def _run():
            with patch.object(self.pool, "get_or_connect", return_value=state):
                return await self.pool.call_tool("srv", "nonexistent_tool", {})

        result = asyncio.run(_run())
        assert "nonexistent_tool" in result
        assert "not found" in result

    def test_tool_not_found_after_reconnect_returns_error(self):
        """After MAX_ERRORS_BEFORE_RECONNECT, invalidate_server is called and the
        tool-not-found message is returned when the tool is absent post-reconnect."""
        bad_tool = _make_tool("good_tool")
        bad_tool.ainvoke = AsyncMock(side_effect=RuntimeError("connection lost"))
        state = _connected_state("srv", tools=[bad_tool])
        self.pool._servers["srv"] = state
        self.pool._mcp_config = {"servers": {"srv": {}}}

        reconnect_state = _connected_state("srv", tools=[])  # tool missing after reconnect

        async def _run():
            call_count = {"get": 0}

            async def patched_get_or_connect(server_name):
                call_count["get"] += 1
                if call_count["get"] == 1:
                    return state
                return reconnect_state

            invalidate_mock = AsyncMock()
            with patch.object(self.pool, "get_or_connect", side_effect=patched_get_or_connect):
                with patch.object(self.pool, "invalidate_server", invalidate_mock):
                    # Trigger enough errors to hit reconnect
                    state.consecutive_errors = MAX_ERRORS_BEFORE_RECONNECT - 1
                    result = await self.pool.call_tool("srv", "good_tool", {})
            return result, invalidate_mock

        result, invalidate_mock = asyncio.run(_run())
        # Reconnect was triggered
        invalidate_mock.assert_awaited_once_with("srv")
        # Tool absent on reconnected state → "not found" message
        assert "not found" in result


# ===========================================================================
# call_tool — auto-reconnect after MAX_ERRORS consecutive failures
# ===========================================================================


class TestCallToolAutoReconnect:
    def setup_method(self):
        reset_mcp_pool()
        self.pool = MCPConnectionPool()

    def teardown_method(self):
        reset_mcp_pool()

    def test_reconnects_and_retries_after_max_errors(self):
        """After MAX_ERRORS_BEFORE_RECONNECT, the pool reconnects and retries once."""
        tool = _make_tool("my_tool")
        tool.ainvoke = AsyncMock(side_effect=[RuntimeError("fail"), "success-after-reconnect"])

        initial_state = _connected_state("srv", tools=[tool])
        initial_state.consecutive_errors = MAX_ERRORS_BEFORE_RECONNECT - 1

        retry_tool = _make_tool("my_tool")
        retry_tool.ainvoke = AsyncMock(return_value="success-after-reconnect")
        reconnected_state = _connected_state("srv", tools=[retry_tool])

        async def _run():
            get_count = {"n": 0}

            async def patched_get(server_name):
                get_count["n"] += 1
                if get_count["n"] == 1:
                    return initial_state
                return reconnected_state

            with (
                patch.object(self.pool, "get_or_connect", side_effect=patched_get),
                patch.object(self.pool, "invalidate_server", new_callable=AsyncMock),
            ):
                return await self.pool.call_tool("srv", "my_tool", {})

        result = asyncio.run(_run())
        assert result == "success-after-reconnect"

    def test_error_counter_increments_on_failure(self):
        """Each failed call increments consecutive_errors."""
        tool = _make_tool("t")
        tool.ainvoke = AsyncMock(side_effect=RuntimeError("fail"))
        state = _connected_state("srv", tools=[tool])

        async def _run():
            with patch.object(self.pool, "get_or_connect", return_value=state):
                try:
                    await self.pool.call_tool("srv", "t", {})
                except Exception:
                    pass

        asyncio.run(_run())
        assert state.consecutive_errors == 1

    def test_successful_call_resets_error_counter(self):
        """A successful call resets consecutive_errors to 0."""
        tool = _make_tool("t")
        tool.ainvoke = AsyncMock(return_value="ok")
        state = _connected_state("srv", tools=[tool])
        state.consecutive_errors = 2

        async def _run():
            with patch.object(self.pool, "get_or_connect", return_value=state):
                return await self.pool.call_tool("srv", "t", {})

        result = asyncio.run(_run())
        assert result == "ok"
        assert state.consecutive_errors == 0

    def test_error_below_threshold_reraises(self):
        """Errors below MAX_ERRORS_BEFORE_RECONNECT are re-raised without reconnecting."""
        tool = _make_tool("t")
        tool.ainvoke = AsyncMock(side_effect=RuntimeError("transient"))
        state = _connected_state("srv", tools=[tool])
        state.consecutive_errors = 0

        async def _run():
            with patch.object(self.pool, "get_or_connect", return_value=state):
                await self.pool.call_tool("srv", "t", {})

        with pytest.raises(RuntimeError, match="transient"):
            asyncio.run(_run())

    def test_call_tool_with_string_input(self):
        """call_tool accepts a plain string input_payload."""
        tool = _make_tool("t")
        tool.ainvoke = AsyncMock(return_value="pong")
        state = _connected_state("srv", tools=[tool])

        async def _run():
            with patch.object(self.pool, "get_or_connect", return_value=state):
                return await self.pool.call_tool("srv", "t", "ping")

        result = asyncio.run(_run())
        assert result == "pong"
        tool.ainvoke.assert_awaited_once_with("ping")


# ===========================================================================
# refresh_if_config_changed — quarantine path
# ===========================================================================


class TestRefreshIfConfigChangedExtra:
    def setup_method(self):
        reset_mcp_pool()
        self.pool = MCPConnectionPool()

    def teardown_method(self):
        reset_mcp_pool()

    def test_quarantine_on_kwarg_error_during_refresh(self):
        """In refresh, if _connect_single raises a kwarg TypeError, server is quarantined."""

        async def _run():
            # Set up an initial connection with hash
            initial_cfg = {"servers": {"s": {"v": "1"}}}
            with patch.object(
                self.pool,
                "_connect_single",
                return_value=_connected_state("s"),
            ):
                await self.pool.connect_all(initial_cfg)

            # Now refresh with a new config that triggers kwarg error
            new_cfg = {"servers": {"s": {"v": "2", "oauth": {}}}}

            async def raiser(name, cfg):
                raise TypeError("unexpected keyword argument 'oauth'")

            with patch.object(self.pool, "_connect_single", side_effect=raiser):
                changed = await self.pool.refresh_if_config_changed(new_cfg)

            return changed

        changed = asyncio.run(_run())
        assert changed is True
        assert self.pool._servers["s"].skip_reason is not None

    def test_refresh_removes_server_absent_from_new_config(self):
        """Server in old config but not in new config is disconnected."""

        async def _run():
            initial_cfg = {"servers": {"x": {"v": "1"}, "y": {"v": "1"}}}
            with patch.object(
                self.pool,
                "_connect_single",
                side_effect=lambda n, c: _connected_state(n),
            ):
                await self.pool.connect_all(initial_cfg)

            assert set(self.pool._servers.keys()) == {"x", "y"}

            new_cfg = {"servers": {"y": {"v": "1"}}}
            with patch.object(
                self.pool,
                "_connect_single",
                side_effect=lambda n, c: _connected_state(n),
            ):
                await self.pool.refresh_if_config_changed(new_cfg)

        asyncio.run(_run())
        assert "x" not in self.pool._servers
        assert "y" in self.pool._servers

    def test_refresh_reconnects_changed_server_config(self):
        """Server whose config changes is invalidated and reconnected."""

        async def _run():
            initial_cfg = {"servers": {"s": {"url": "http://a"}}}
            with patch.object(
                self.pool,
                "_connect_single",
                side_effect=lambda n, c: _connected_state(n),
            ):
                await self.pool.connect_all(initial_cfg)

            new_cfg = {"servers": {"s": {"url": "http://b"}}}
            with patch.object(
                self.pool,
                "_connect_single",
                side_effect=lambda n, c: _connected_state(n),
            ) as mock_connect:
                changed = await self.pool.refresh_if_config_changed(new_cfg)
                return changed, mock_connect.call_count

        changed, reconnect_calls = asyncio.run(_run())
        assert changed is True
        assert reconnect_calls >= 1


# ===========================================================================
# shutdown
# ===========================================================================


class TestMCPPoolShutdown:
    def setup_method(self):
        reset_mcp_pool()
        self.pool = MCPConnectionPool()

    def teardown_method(self):
        reset_mcp_pool()

    def test_shutdown_clears_all_servers(self):
        self.pool._servers["a"] = _connected_state("a")
        self.pool._servers["b"] = _connected_state("b")
        self.pool._config_hash = "some-hash"
        self.pool._mcp_config = {"servers": {"a": {}, "b": {}}}

        asyncio.run(self.pool.shutdown())

        assert len(self.pool._servers) == 0
        assert self.pool._config_hash == ""
        assert self.pool._mcp_config == {}

    def test_shutdown_idempotent_on_empty_pool(self):
        asyncio.run(self.pool.shutdown())  # must not raise


# ===========================================================================
# get_all_tool_details
# ===========================================================================


class TestGetAllToolDetails:
    def setup_method(self):
        reset_mcp_pool()
        self.pool = MCPConnectionPool()

    def teardown_method(self):
        reset_mcp_pool()

    def test_skips_disconnected_servers(self):
        self.pool._servers["connected"] = _connected_state("connected", tools=[_make_tool("t")])
        self.pool._servers["disconnected"] = ServerState(
            name="disconnected", config={}, connected=False
        )
        details = self.pool.get_all_tool_details()
        assert "connected" in details
        assert "disconnected" not in details

    def test_returns_sorted_tool_names(self):
        tools = [_make_tool("z_tool"), _make_tool("a_tool"), _make_tool("m_tool")]
        self.pool._servers["s"] = _connected_state("s", tools=tools)
        details = self.pool.get_all_tool_details()
        names = [d["name"] for d in details["s"]]
        assert names == sorted(names)


# ===========================================================================
# connect_all — timeout handling
# ===========================================================================


class TestConnectAllTimeout:
    def setup_method(self):
        reset_mcp_pool()
        self.pool = MCPConnectionPool()

    def teardown_method(self):
        reset_mcp_pool()

    def test_timeout_recorded_as_error(self):
        """A server that times out during connect_all records an ERROR entry and is not connected.

        asyncio.TimeoutError.__str__() is empty, so the stored entry is exactly
        "ERROR: " — the important invariant is that the server DID NOT succeed
        (the entry starts with "ERROR: ") and no ServerState was stored in the pool.
        """

        async def _run():
            async def slow(name, cfg):
                await asyncio.sleep(999)
                return _connected_state(name)

            cfg = {"servers": {"slow-srv": {"cmd": "x"}}}
            with (
                patch.object(self.pool, "_connect_single", side_effect=slow),
                patch("mewbo_tools.integration.mcp_pool.CONNECT_TIMEOUT", 0.001),
            ):
                return await self.pool.connect_all(cfg)

        results = asyncio.run(_run())
        assert "slow-srv" in results
        assert len(results["slow-srv"]) == 1
        assert results["slow-srv"][0].startswith("ERROR: ")
        # Gitea #130: a timed-out connect is now recorded as a backoff placeholder
        # (transient failure) so it is NOT re-dialed on every refresh — it fast-
        # fails until the window elapses. The server stays disconnected.
        state = self.pool._servers["slow-srv"]
        assert state.connected is False
        assert state.failure_reason == "timeout"
        assert state.status == "backoff"

    def test_multiple_servers_connect_concurrently(self):
        """Multiple servers connect concurrently (semaphore respected)."""

        async def _run():
            cfg = {
                "servers": {
                    "s1": {"url": "http://1"},
                    "s2": {"url": "http://2"},
                    "s3": {"url": "http://3"},
                }
            }
            with patch.object(
                self.pool,
                "_connect_single",
                side_effect=lambda n, c: _connected_state(n),
            ):
                return await self.pool.connect_all(cfg)

        results = asyncio.run(_run())
        assert set(results.keys()) == {"s1", "s2", "s3"}


class _DuckGroup(Exception):
    """ExceptionGroup-like wrapper (anyio TaskGroup duck-typing)."""

    def __init__(self, message: str, exceptions: list[BaseException]) -> None:
        super().__init__(message)
        self.exceptions = tuple(exceptions)


class TestConnectFailureNamesRealCause:
    """The opaque TaskGroup wrapper must never reach the log/ERROR string (#132)."""

    def setup_method(self):
        reset_mcp_pool()
        self.pool = MCPConnectionPool()

    def teardown_method(self):
        reset_mcp_pool()

    def test_connect_all_error_string_unwraps_group(self):
        """A TaskGroup-wrapped DNS failure surfaces the real cause, not the wrapper."""
        real = OSError("[Errno -2] failed to resolve host 'postgres'")
        group = _DuckGroup("unhandled errors in a TaskGroup (1 sub-exception)", [real])

        async def _run():
            cfg = {"servers": {"sidestage-postgres": {"url": "http://x"}}}
            with patch.object(self.pool, "_connect_single", side_effect=group):
                return await self.pool.connect_all(cfg)

        results = asyncio.run(_run())
        entry = results["sidestage-postgres"][0]
        # Consolidated onto #130's `_record_failure`: ERROR carries the coarse
        # reason AND the unwrapped cause — never the opaque TaskGroup wrapper.
        assert entry == "ERROR: dns: [Errno -2] failed to resolve host 'postgres'"
        assert "sub-exception" not in entry

    def test_connect_warning_binds_classified_reason(self):
        """The WARNING names a coarse, actionable reason (dns) inline + as extra."""
        from loguru import logger as _loguru

        real = OSError("[Errno -2] failed to resolve host 'postgres'")
        group = _DuckGroup("unhandled errors in a TaskGroup (1 sub-exception)", [real])
        sink: list[str] = []
        handler_id = _loguru.add(
            lambda m: sink.append(m), level="WARNING", format="{message} reason={extra[reason]}"
        )

        async def _run():
            cfg = {"servers": {"sidestage-postgres": {"url": "http://x"}}}
            with patch.object(self.pool, "_connect_single", side_effect=group):
                await self.pool.connect_all(cfg)

        try:
            asyncio.run(_run())
        finally:
            _loguru.remove(handler_id)

        text = "".join(sink)
        assert "[dns]" in text  # inline reason in the message
        assert "reason=dns" in text  # structured extra field for downstream filtering
        assert "failed to resolve host 'postgres'" in text
        assert "sub-exception" not in text


# ===========================================================================
# Singleton helpers
# ===========================================================================


class TestPoolSingletonExtra:
    def test_reset_clears_warned_skip(self):
        from mewbo_tools.integration.mcp_pool import _WARNED_SKIP

        _WARNED_SKIP.add(("srv", "reason"))
        reset_mcp_pool()
        assert len(_WARNED_SKIP) == 0

    def test_get_mcp_pool_returns_same_instance(self):
        reset_mcp_pool()
        assert get_mcp_pool() is get_mcp_pool()
