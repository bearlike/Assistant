"""Non-blocking async MCP initialization (Gitea #130).

A slow/dead MCP server must never stall startup or the tool loop:
- failures are classified (auth/config never retry; dns/refused/timeout back off);
- a backed-off server fast-fails instead of being re-dialed;
- ``refresh_if_config_changed(connect=False)`` never eagerly connects, so the
  wait is deferred to the first actual use of a specific server;
- a healthy connected server never triggers a full-config reconnect mid-query.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import mewbo_tools.integration.mcp_pool as mcp_pool
from mewbo_tools.integration.mcp_pool import (
    MCPConnectionPool,
    ServerState,
    classify_connect_failure,
    reset_mcp_pool,
    unwrap_exception_group,
)


class _FakeGroup(Exception):
    """Faithful stand-in for an anyio/builtin ``ExceptionGroup``.

    ``unwrap_exception_group`` only reads the ``.exceptions`` tuple, so this
    mirrors the wrapper's structural contract without depending on the 3.11+
    ``ExceptionGroup`` builtin (the repo targets py310).
    """

    def __init__(self, message: str, exceptions: list[BaseException]) -> None:
        super().__init__(message)
        self.exceptions = tuple(exceptions)


class TestUnwrapExceptionGroup:
    def test_single_child_group_is_peeled(self):
        real = ConnectionRefusedError("connection refused")
        group = _FakeGroup("unhandled errors in a TaskGroup (1 sub-exception)", [real])
        assert unwrap_exception_group(group) is real

    def test_nested_single_child_groups_peel_to_innermost(self):
        real = OSError("[Errno -2] failed to resolve host 'postgres'")
        inner = _FakeGroup("inner", [real])
        outer = _FakeGroup("outer", [inner])
        assert unwrap_exception_group(outer) is real

    def test_multi_child_group_returned_as_is(self):
        group = _FakeGroup("two", [ValueError("a"), ValueError("b")])
        assert unwrap_exception_group(group) is group

    def test_plain_exception_passthrough(self):
        exc = TimeoutError("timed out")
        assert unwrap_exception_group(exc) is exc


class TestClassifyConnectFailure:
    def test_dns_failure(self):
        exc = _FakeGroup("tg", [OSError("[Errno -2] failed to resolve host 'postgres'")])
        assert classify_connect_failure(exc) == "dns"

    def test_connection_refused(self):
        assert classify_connect_failure(ConnectionRefusedError("Connection refused")) == "refused"

    def test_timeout(self):
        assert classify_connect_failure(asyncio.TimeoutError()) == "timeout"

    def test_auth_401(self):
        assert classify_connect_failure(RuntimeError("HTTP 401 Unauthorized")) == "auth"

    def test_config_kwarg_error(self):
        exc = TypeError("_create_session() got an unexpected keyword argument 'oauth'")
        assert classify_connect_failure(exc) == "config"

    def test_unknown_other(self):
        assert classify_connect_failure(RuntimeError("kaboom")) == "other"


class TestBackoffOnTransientFailure:
    def setup_method(self):
        reset_mcp_pool()
        self.pool = MCPConnectionPool()

    def test_dead_host_backs_off_and_fast_fails_without_redial(self):
        """A dns/refused failure must back off; the next call fast-fails and
        does NOT re-invoke the (slow) connect — killing the per-refresh redial."""
        calls = {"n": 0}

        async def dead(name, cfg):
            calls["n"] += 1
            raise _FakeGroup("tg", [OSError("failed to resolve host 'postgres'")])

        self.pool._mcp_config = {"servers": {"pg": {"command": "postgres-mcp"}}}

        async def _run():
            with patch.object(self.pool, "_connect_single", side_effect=dead):
                # First use: dials once, fails, records a backoff window
                # (BACKOFF_START seconds — far longer than this test runs).
                try:
                    await self.pool.get_or_connect("pg")
                except ValueError:
                    pass
                first = calls["n"]
                # Second use within the backoff window: must NOT dial again.
                try:
                    await self.pool.get_or_connect("pg")
                except ValueError as exc:
                    assert "backing off" in str(exc) or "unavailable" in str(exc)
                return first, calls["n"]

        first, second = asyncio.run(_run())
        assert first == 1
        assert second == 1  # no redial inside the backoff window
        st = self.pool._servers["pg"]
        assert st.status == "backoff"
        assert st.failure_reason == "dns"
        assert st.next_retry_at > mcp_pool._monotonic()

    def test_backoff_grows_exponentially_capped(self):
        async def dead(name, cfg):
            raise ConnectionRefusedError("Connection refused")

        self.pool._mcp_config = {"servers": {"pg": {"command": "x"}}}

        async def _run():
            with patch.object(self.pool, "_connect_single", side_effect=dead):
                # Drive several failures past the backoff window each time.
                t = [1000.0]

                def now():
                    return t[0]

                with patch.object(mcp_pool, "_monotonic", side_effect=now):
                    backoffs = []
                    for _ in range(8):
                        try:
                            await self.pool.get_or_connect("pg")
                        except ValueError:
                            pass
                        backoffs.append(self.pool._servers["pg"].backoff_secs)
                        # Advance time past the window so the next call retries.
                        t[0] = self.pool._servers["pg"].next_retry_at + 1.0
                    return backoffs

        backoffs = asyncio.run(_run())
        assert backoffs[0] == mcp_pool.BACKOFF_START
        assert backoffs[1] > backoffs[0]
        assert max(backoffs) <= mcp_pool.BACKOFF_CAP


class TestNeverRetryAuth:
    def setup_method(self):
        reset_mcp_pool()
        self.pool = MCPConnectionPool()

    def test_auth_failure_quarantines_permanently(self):
        async def unauth(name, cfg):
            raise RuntimeError("HTTP 403 Forbidden")

        self.pool._mcp_config = {"servers": {"svc": {"url": "https://x"}}}

        async def _run():
            with patch.object(self.pool, "_connect_single", side_effect=unauth):
                try:
                    await self.pool.get_or_connect("svc")
                except ValueError:
                    pass

        asyncio.run(_run())
        st = self.pool._servers["svc"]
        assert st.status == "quarantined"
        assert st.skip_reason is not None
        assert st.next_retry_at == 0.0  # not a backoff — never auto-retried


class TestLazyConnectNoEagerDial:
    def setup_method(self):
        reset_mcp_pool()
        self.pool = MCPConnectionPool()

    def test_refresh_connect_false_does_not_dial(self):
        """connect=False updates config + prunes but never connects — so a dead
        server in the config never blocks the refresh (deferred to first use)."""
        dialed = {"n": 0}

        async def mc(name, cfg):
            dialed["n"] += 1
            return ServerState(name=name, config=cfg, connected=True)

        async def _run():
            with patch.object(self.pool, "_connect_single", side_effect=mc):
                changed = await self.pool.refresh_if_config_changed(
                    {"servers": {"a": {"v": "1"}, "b": {"v": "1"}}}, connect=False
                )
                return changed

        changed = asyncio.run(_run())
        assert changed is True
        assert dialed["n"] == 0  # nothing dialed eagerly
        # Config is known so a later get_or_connect can find the server.
        assert "a" in self.pool._mcp_config.get("servers", {})

    def test_is_connected_reports_state(self):
        self.pool._servers["live"] = ServerState(name="live", config={}, connected=True)
        self.pool._servers["dead"] = ServerState(name="dead", config={}, connected=False)
        assert self.pool.is_connected("live") is True
        assert self.pool.is_connected("dead") is False
        assert self.pool.is_connected("missing") is False


class TestNoPerCallRefreshChurn:
    """Gitea #130 Phase 4: a healthy connected server must never trigger a
    full-config reload + reconnect on every tool call (the mid-query stall)."""

    def setup_method(self):
        reset_mcp_pool()

    def test_connected_server_skips_config_reload_and_refresh(self):
        from mewbo_tools.integration.mcp import MCPToolRunner

        pool = mcp_pool.get_mcp_pool()
        refreshed = {"n": 0}
        loaded = {"n": 0}

        class _Tool:
            name = "deepwiki_ask"

            async def ainvoke(self, payload):
                return "ok"

        pool._servers["deepwiki"] = ServerState(
            name="deepwiki", config={}, connected=True, tools=[_Tool()]
        )

        async def _refresh(cfg, *, connect=True):
            refreshed["n"] += 1
            return False

        def _load(*args, **kwargs):
            loaded["n"] += 1
            return {"servers": {}}

        runner = MCPToolRunner(server_name="deepwiki", tool_name="deepwiki_ask")

        async def _run():
            with (
                patch.object(pool, "refresh_if_config_changed", side_effect=_refresh),
                patch("mewbo_tools.integration.mcp._load_mcp_config", side_effect=_load),
            ):
                return await runner._invoke_via_pool({"q": "x"})

        result = asyncio.run(_run())
        assert result == "ok"
        # Target server already connected → no config reload, no refresh churn.
        assert loaded["n"] == 0
        assert refreshed["n"] == 0


class TestStartupConfigHashGate:
    """Gitea #130 Phase 1: an unchanged MCP config + a cached manifest must NOT
    trigger a blocking live connect — startup uses the cache and the pool
    connects lazily on first use, so a slow/dead server never stalls the banner."""

    def test_unchanged_config_skips_discovery(self, tmp_path, monkeypatch):
        import json as _json

        import mewbo_core.tool_registry as tr
        from mewbo_tools.integration.mcp_pool import _config_hash

        cfg = {"servers": {"deepwiki": {"url": "https://x"}}}
        manifest = tmp_path / "manifest.json"
        manifest.write_text(
            _json.dumps(
                {
                    "config_hash": _config_hash(cfg),
                    "tools": [
                        {
                            "tool_id": "mcp_deepwiki_ask",
                            "name": "ask",
                            "kind": "mcp",
                            "server": "deepwiki",
                            "tool": "ask",
                            "enabled": True,
                        }
                    ],
                }
            )
        )

        monkeypatch.setattr(tr, "_default_manifest_cache_path", lambda: str(manifest))
        monkeypatch.setattr(tr, "_resolve_mcp_config", lambda *a, **k: cfg)

        def _boom(*a, **k):
            raise AssertionError("discovery must not run when config is unchanged")

        monkeypatch.setattr(tr, "_try_pool_discovery", _boom)
        monkeypatch.setattr(tr, "_load_mcp_support", _boom)

        out = tr._ensure_auto_manifest("/x/mcp.json", cwd=None, extra_mcp_servers=None)
        assert out == str(manifest)

    def test_changed_config_triggers_discovery_and_stamps_hash(self, tmp_path, monkeypatch):
        import json as _json

        import mewbo_core.tool_registry as tr
        from mewbo_tools.integration.mcp_pool import _config_hash

        old_cfg = {"servers": {"deepwiki": {"url": "https://old"}}}
        new_cfg = {"servers": {"deepwiki": {"url": "https://new"}}}
        manifest = tmp_path / "manifest.json"
        manifest.write_text(_json.dumps({"config_hash": _config_hash(old_cfg), "tools": []}))

        monkeypatch.setattr(tr, "_default_manifest_cache_path", lambda: str(manifest))
        monkeypatch.setattr(tr, "_resolve_mcp_config", lambda *a, **k: new_cfg)
        called = {"n": 0}

        def _discovery(*a, **k):
            called["n"] += 1
            return {"deepwiki": [{"name": "ask", "schema": None}]}

        monkeypatch.setattr(tr, "_try_pool_discovery", _discovery)

        tr._ensure_auto_manifest("/x/mcp.json", cwd=None, extra_mcp_servers=None)
        assert called["n"] == 1
        written = _json.loads((tmp_path / "manifest.json").read_text())
        assert written["config_hash"] == _config_hash(new_cfg)


class TestStatusSnapshot:
    def setup_method(self):
        reset_mcp_pool()
        self.pool = MCPConnectionPool()

    def test_snapshot_surfaces_status_and_reason(self):
        self.pool._servers["ok"] = ServerState(
            name="ok", config={}, connected=True, tools=[{"name": "t"}]
        )
        self.pool._servers["bad"] = ServerState(
            name="bad", config={}, connected=False, skip_reason="auth: 403"
        )
        snap = self.pool.status_snapshot()
        assert snap["ok"]["status"] == "connected"
        assert snap["bad"]["status"] == "quarantined"
        assert "auth" in snap["bad"]["reason"]
