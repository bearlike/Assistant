"""Tests for the MCP connection pool."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from mewbo_tools.integration.mcp_pool import (
    MAX_ERRORS_BEFORE_RECONNECT,
    MCPConnectionPool,
    ServerState,
    get_mcp_pool,
    reset_mcp_pool,
)


class TestServerState:
    def test_defaults(self):
        s = ServerState(name="srv", config={"command": "echo"})
        assert s.client is None
        assert s.tools == []
        assert s.consecutive_errors == 0
        assert s.connected is False


class TestMCPConnectionPool:
    def setup_method(self):
        reset_mcp_pool()
        self.pool = MCPConnectionPool()

    def test_connect_all_empty(self):
        assert asyncio.run(self.pool.connect_all({})) == {}

    def test_server_can_be_added_and_queried(self):
        """Verify manual server registration and tool detail queries."""
        self.pool._servers["s1"] = ServerState(
            name="s1",
            config={"cmd": "x"},
            connected=True,
            tools=[{"name": "tool_a"}, {"name": "tool_b"}],
        )
        details = self.pool.get_all_tool_details()
        assert len(details["s1"]) == 2

    def test_returns_cached(self):
        self.pool._servers["s"] = ServerState(
            name="s",
            config={},
            connected=True,
            client=MagicMock(),
        )
        state = asyncio.run(self.pool.get_or_connect("s"))
        assert state.connected

    def test_reconnects_disconnected(self):
        """A disconnected server should attempt reconnection."""
        self.pool._servers["s"] = ServerState(
            name="s",
            config={"cmd": "x"},
            connected=False,
        )
        self.pool._mcp_config = {"servers": {"s": {"cmd": "x"}}}

        async def _run():
            async def mc(name, cfg):
                return ServerState(
                    name=name,
                    config=cfg,
                    connected=True,
                )

            with patch.object(self.pool, "_connect_single", side_effect=mc):
                return await self.pool.get_or_connect("s")

        state = asyncio.run(_run())
        assert state.connected

    def test_invalidate_removes_server(self):
        self.pool._servers["s"] = ServerState(
            name="s",
            config={},
            connected=True,
            client=MagicMock(),
            tools=[{"name": "t"}],
        )
        asyncio.run(self.pool.invalidate_server("s"))
        assert "s" not in self.pool._servers

    def test_invalidate_nonexistent(self):
        asyncio.run(self.pool.invalidate_server("nope"))

    def test_refresh_detects_change(self):
        async def _run():
            async def mc(name, cfg):
                self.pool._servers[name] = ServerState(
                    name=name,
                    config=cfg,
                    connected=True,
                )

            with patch.object(self.pool, "_connect_single", side_effect=mc):
                await self.pool.connect_all({"s": {"v": "1"}})
                return await self.pool.refresh_if_config_changed(
                    {"s": {"v": "2"}},
                )

        assert asyncio.run(_run())

    def test_refresh_no_change(self):
        cfg = {"s": {"v": "same"}}

        async def _run():
            async def mc(name, c):
                self.pool._servers[name] = ServerState(
                    name=name,
                    config=c,
                    connected=True,
                )

            with patch.object(self.pool, "_connect_single", side_effect=mc):
                await self.pool.connect_all(cfg)
                return await self.pool.refresh_if_config_changed(cfg)

        assert not asyncio.run(_run())

    def test_refresh_prunes_servers_absent_in_new_config(self):
        """Switching project scope must drop servers from the previous config.

        Regression guard for the bleed where /api/tools?project=B was
        returning project-A's MCP tools because connect_all only adds
        entries, never prunes. With refresh_if_config_changed, servers
        missing from the new config must be disconnected and removed.
        """

        async def _run():
            async def mc(name, cfg):
                return ServerState(
                    name=name,
                    config=cfg,
                    connected=True,
                    tools=[MagicMock(name=f"tool_{name}")],
                )

            with patch.object(self.pool, "_connect_single", side_effect=mc):
                # Project A: servers {x, y}
                await self.pool.connect_all({"servers": {"x": {"v": "1"}, "y": {"v": "1"}}})
                assert set(self.pool._servers.keys()) == {"x", "y"}
                # Switch to project B: only server {z}
                await self.pool.refresh_if_config_changed({"servers": {"z": {"v": "1"}}})

        asyncio.run(_run())
        assert set(self.pool._servers.keys()) == {"z"}
        # get_all_tool_details() surfaces only the current scope's server
        details = self.pool.get_all_tool_details()
        assert set(details.keys()) == {"z"}

    def test_shutdown_disconnects_all(self):
        self.pool._servers["a"] = ServerState(
            name="a",
            config={},
            connected=True,
        )
        self.pool._servers["b"] = ServerState(
            name="b",
            config={},
            connected=True,
        )
        asyncio.run(self.pool.shutdown())
        assert all(not s.connected for s in self.pool._servers.values())

    def test_empty_tool_details(self):
        assert self.pool.get_all_tool_details() == {}

    def test_tool_details_with_data(self):
        self.pool._servers["s"] = ServerState(
            name="s",
            config={},
            connected=True,
            tools=[{"name": "t1", "description": "d"}],
        )
        d = self.pool.get_all_tool_details()
        assert "s" in d and len(d["s"]) == 1

    def test_error_counter(self):
        s = ServerState(
            name="s",
            config={},
            consecutive_errors=MAX_ERRORS_BEFORE_RECONNECT - 1,
        )
        assert s.consecutive_errors == MAX_ERRORS_BEFORE_RECONNECT - 1


class TestPoolSingleton:
    def test_same_instance(self):
        reset_mcp_pool()
        assert get_mcp_pool() is get_mcp_pool()

    def test_reset_clears(self):
        p1 = get_mcp_pool()
        reset_mcp_pool()
        assert get_mcp_pool() is not p1


class TestQuarantineOnConfigKwargError:
    """A server whose config carries adapter-unknown keys (e.g. `oauth`) must
    be quarantined so its failure doesn't replay on every tool-use turn."""

    def setup_method(self):
        reset_mcp_pool()
        self.pool = MCPConnectionPool()

    def test_connect_all_quarantines_unknown_kwarg(self):
        async def _run():
            async def raiser(name, cfg):
                raise TypeError(
                    "_create_streamable_http_session() got an unexpected keyword argument 'oauth'"
                )

            with patch.object(self.pool, "_connect_single", side_effect=raiser):
                return await self.pool.connect_all(
                    {"servers": {"slack": {"type": "http", "oauth": {}}}}
                )

        results = asyncio.run(_run())
        assert "ERROR" in results["slack"][0]
        state = self.pool._servers["slack"]
        assert state.connected is False
        assert state.skip_reason is not None
        assert "oauth" in state.skip_reason

    def test_get_or_connect_short_circuits_when_quarantined(self):
        self.pool._servers["slack"] = ServerState(
            name="slack",
            config={"oauth": {}},
            connected=False,
            skip_reason="adapter rejected config keys (oauth)",
        )

        async def _run():
            try:
                await self.pool.get_or_connect("slack")
            except ValueError as exc:
                return exc
            return None

        exc = asyncio.run(_run())
        assert isinstance(exc, ValueError)
        assert "unavailable" in str(exc)

    def test_refresh_does_not_retry_quarantined_with_same_config(self):
        async def _run():
            raiser_called = {"n": 0}

            async def raiser(name, cfg):
                raiser_called["n"] += 1
                raise TypeError("unexpected keyword argument 'oauth'")

            cfg = {"servers": {"slack": {"type": "http", "oauth": {}}}}
            with patch.object(self.pool, "_connect_single", side_effect=raiser):
                await self.pool.connect_all(cfg)
                first_call_count = raiser_called["n"]
                # Same config → hash unchanged → early return, no retry
                await self.pool.refresh_if_config_changed(cfg)
                return first_call_count, raiser_called["n"]

        first, second = asyncio.run(_run())
        assert first == 1
        assert second == 1  # No retry

    def test_config_hash_change_clears_quarantine(self):
        """When the user edits the config for the quarantined server, the pool
        must retry (drop the placeholder, let _reconn decide the new outcome)."""

        async def _run():
            call_args: list[dict] = []

            async def maybe_fail(name, cfg):
                # Simulate the adapter rejecting an unknown kwarg on the
                # first call; succeed once the config hash changes.
                call_args.append(cfg)
                if len(call_args) == 1:
                    raise TypeError("unexpected keyword argument 'foo'")
                return ServerState(
                    name=name,
                    config=cfg,
                    connected=True,
                )

            with patch.object(self.pool, "_connect_single", side_effect=maybe_fail):
                await self.pool.connect_all(
                    {"servers": {"slack": {"type": "http", "url": "https://a"}}}
                )
                assert self.pool._servers["slack"].skip_reason is not None
                # User "fixes" the config — different shape, new hash
                await self.pool.refresh_if_config_changed(
                    {"servers": {"slack": {"type": "http", "url": "https://b"}}}
                )
            return call_args

        call_args = asyncio.run(_run())
        assert len(call_args) == 2  # first (failed) + retry after config change
        state = self.pool._servers["slack"]
        assert state.connected is True
        assert state.skip_reason is None
