"""Tests for the MCP connection pool."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from meeseeks_tools.integration.mcp_pool import (
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
