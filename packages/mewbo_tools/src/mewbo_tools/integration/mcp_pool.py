"""Persistent MCP connection pool with lifecycle management."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from mewbo_core.common import get_logger

from mewbo_tools.integration.exception_unwrap import (
    _is_config_kwarg_error,  # noqa: F401 - re-exported for mcp_pool.<name> callers/tests
    classify_connect_failure,
    describe_exception_group,
    unwrap_exception_group,  # noqa: F401 - re-exported for mcp_pool.<name> callers/tests
)

logger = get_logger(__name__)

MAX_ERRORS_BEFORE_RECONNECT = 3
CONNECT_TIMEOUT = 30
CALL_TIMEOUT = 60
MAX_CONCURRENT_CONNECTS = 5

# Exponential backoff applied to TRANSIENT connect failures (dns/refused/
# timeout) so a dead host is never re-dialed on every refresh — it fast-fails
# until the window elapses (Gitea #130). Permanent failures (auth/config) do
# not back off; they quarantine until the config changes.
BACKOFF_START = 5.0
BACKOFF_FACTOR = 2.0
BACKOFF_CAP = 300.0

# Failure reasons that must NEVER be retried automatically — only a config
# edit (new config hash) can clear them. A wrong secret or an adapter-rejected
# config key will fail identically forever, so retrying wastes the loop.
_NEVER_RETRY_REASONS = frozenset({"auth", "config"})

# Monotonic clock seam — module attribute so tests can patch it for
# deterministic backoff windows without real sleeps.
_monotonic = time.monotonic


# Servers whose config keys the MCP adapter does not accept are quarantined.
# The warning/info pair is emitted once per (server, reason) for the process
# lifetime so quarantine re-triggers after config changes still surface.
_WARNED_SKIP: set[tuple[str, str]] = set()

# ``_is_config_kwarg_error`` / ``unwrap_exception_group`` /
# ``classify_connect_failure`` live in ``exception_unwrap`` (the single shared
# home reused by the legacy ``mcp`` path too) and are re-exported here via the
# module import above, so existing ``mcp_pool.<name>`` callers/tests keep working.


@dataclass
class ServerState:
    """Runtime state for a single MCP server connection."""

    name: str
    config: dict[str, Any]
    client: Any | None = None
    tools: list[Any] = field(default_factory=list)
    consecutive_errors: int = 0
    connected: bool = False
    # Non-None when a connection attempt failed PERMANENTLY (auth/config) —
    # never auto-retried. Cleared when the config hash changes so the user
    # can retry by editing the config.
    skip_reason: str | None = None
    # Classified reason of the most recent TRANSIENT failure (dns/refused/
    # timeout/other) and its exponential-backoff window. While ``next_retry_at``
    # is in the future the server fast-fails instead of being re-dialed.
    failure_reason: str | None = None
    next_retry_at: float = 0.0
    backoff_secs: float = 0.0

    @property
    def status(self) -> str:
        """Coarse lifecycle label for surfacing in ``/mcp`` and snapshots."""
        if self.connected:
            return "connected"
        if self.skip_reason is not None:
            return "quarantined"
        if self.next_retry_at and self.next_retry_at > _monotonic():
            return "backoff"
        if self.failure_reason is not None:
            return "failed"
        return "pending"


def _config_hash(mcp_config: dict[str, Any]) -> str:
    """Compute a stable hash of the MCP config for change detection."""
    raw = json.dumps(mcp_config, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


class MCPConnectionPool:
    """Persistent, memoized MCP connection manager.

    Keeps one ``MultiServerMCPClient`` per MCP server alive across tool
    invocations so that connection setup and tool discovery are amortized.
    """

    def __init__(self) -> None:
        """Initialize an empty connection pool."""
        self._servers: dict[str, ServerState] = {}
        self._lock = asyncio.Lock()
        self._config_hash: str = ""
        self._mcp_config: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _connect_single(self, name: str, config: dict[str, Any]) -> ServerState:
        """Create a client, discover tools, and return a populated state."""
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
        except Exception as exc:  # pragma: no cover - runtime dependency
            raise RuntimeError("langchain-mcp-adapters is required for MCP tools.") from exc

        client = MultiServerMCPClient({name: config})  # type: ignore[dict-item]
        tools = await client.get_tools(server_name=name)
        state = ServerState(
            name=name,
            config=config,
            client=client,
            tools=list(tools),
            consecutive_errors=0,
            connected=True,
        )
        logger.info("Connected to MCP server '{}' ({} tools)", name, len(tools))
        return state

    async def _disconnect_server(self, state: ServerState) -> None:
        """Best-effort close of a single server client."""
        state.connected = False
        state.tools = []
        state.client = None
        logger.debug("Disconnected from MCP server '{}'", state.name)

    async def _quarantine(self, name: str, config: dict[str, Any], reason: str) -> None:
        """Stash a disconnected placeholder so subsequent calls short-circuit.

        The quarantined state persists until its config entry changes in
        ``refresh_if_config_changed``. The WARNING+INFO pair is emitted
        once per (server, reason) tuple.
        """
        state = ServerState(
            name=name,
            config=config,
            connected=False,
            skip_reason=reason,
        )
        async with self._lock:
            self._servers[name] = state
        key = (name, reason)
        if key not in _WARNED_SKIP:
            _WARNED_SKIP.add(key)
            logger.info(
                "MCP server '{}' skipped until config changes: {}",
                name,
                reason,
            )

    async def _record_failure(
        self, name: str, config: dict[str, Any], exc: BaseException
    ) -> str:
        """Classify a connect failure and apply quarantine OR backoff.

        Auth/config failures will never succeed without a config edit, so they
        quarantine permanently (reuse ``_quarantine``). Transient failures
        (dns/refused/timeout/other) get an exponential, capped backoff window
        so the dead host fast-fails on subsequent calls instead of being
        re-dialed on every refresh. Returns the classified reason.
        """
        reason = classify_connect_failure(exc)
        # ``describe`` names the real cause(s) — including multi-child groups,
        # which ``unwrap`` returns as the still-opaque wrapper (Gitea #132).
        detail = describe_exception_group(exc)
        if reason in _NEVER_RETRY_REASONS:
            await self._quarantine(name, config, f"{reason}: {detail}")
            return reason

        async with self._lock:
            prior = self._servers.get(name)
            prior_backoff = prior.backoff_secs if prior is not None else 0.0
        backoff = min(max(prior_backoff * BACKOFF_FACTOR, BACKOFF_START), BACKOFF_CAP)
        state = ServerState(
            name=name,
            config=config,
            connected=False,
            failure_reason=reason,
            backoff_secs=backoff,
            next_retry_at=_monotonic() + backoff,
        )
        async with self._lock:
            self._servers[name] = state
        logger.bind(mcp_server=name, reason=reason).warning(
            "MCP server '{}' connect failed [{}]: {} — backing off {:.0f}s",
            name,
            reason,
            detail,
            backoff,
        )
        logger.opt(exception=exc).debug("MCP connect traceback for '{}'", name)
        return reason

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def connect_all(self, mcp_config: dict[str, Any]) -> dict[str, list[str]]:
        """Connect to all configured servers concurrently.

        Returns a mapping of ``{server_name: [tool_names]}`` on success,
        or ``{server_name: ["ERROR: <reason>"]}`` on per-server failure.
        The entire batch never fails; individual errors are logged.
        """
        from mewbo_tools.integration.mcp import _normalize_mcp_config

        mcp_config = _normalize_mcp_config(mcp_config)
        self._mcp_config = mcp_config
        self._config_hash = _config_hash(mcp_config)
        servers = mcp_config.get("servers", {})
        sem = asyncio.Semaphore(MAX_CONCURRENT_CONNECTS)
        results: dict[str, list[str]] = {}

        async def _try_connect(name: str, cfg: dict[str, Any]) -> None:
            async with sem:
                try:
                    state = await asyncio.wait_for(
                        self._connect_single(name, cfg),
                        timeout=CONNECT_TIMEOUT,
                    )
                    async with self._lock:
                        self._servers[name] = state
                    results[name] = [getattr(t, "name", str(t)) for t in state.tools]
                except Exception as exc:
                    # ``_record_failure`` classifies, logs the named cause with a
                    # structured ``reason`` extra, and applies quarantine/backoff.
                    reason = await self._record_failure(name, cfg, exc)
                    results[name] = [f"ERROR: {reason}: {describe_exception_group(exc)}"]

        await asyncio.gather(*[_try_connect(name, cfg) for name, cfg in servers.items()])
        return results

    async def get_or_connect(self, server_name: str) -> ServerState:
        """Return an existing connection or create one on demand.

        Raises ``ValueError`` if the server is not present in the loaded
        MCP configuration or is quarantined until its config changes.
        """
        async with self._lock:
            state = self._servers.get(server_name)
            if state is not None and state.connected:
                return state
            if state is not None and state.skip_reason is not None:
                raise ValueError(f"MCP server '{server_name}' unavailable: {state.skip_reason}")
            if state is not None and state.next_retry_at and state.next_retry_at > _monotonic():
                remaining = state.next_retry_at - _monotonic()
                raise ValueError(
                    f"MCP server '{server_name}' unavailable: backing off after "
                    f"{state.failure_reason} failure (retry in {remaining:.0f}s)"
                )

        # Not connected yet -- try to connect
        servers = self._mcp_config.get("servers", {})
        if server_name not in servers:
            # Try loading config fresh
            try:
                from mewbo_tools.integration.mcp import _load_mcp_config, _normalize_mcp_config

                fresh = _normalize_mcp_config(_load_mcp_config())
                servers = fresh.get("servers", {})
                if server_name in servers:
                    self._mcp_config = fresh
                    self._config_hash = _config_hash(fresh)
            except Exception:
                pass

        config = servers.get(server_name)
        if config is None:
            raise ValueError(f"MCP server '{server_name}' not found in configuration.")

        try:
            state = await asyncio.wait_for(
                self._connect_single(server_name, config),
                timeout=CONNECT_TIMEOUT,
            )
        except Exception as exc:
            # Deferred wait-on-first-use bounds the cost to ONE attempt; the
            # failure then quarantines (auth/config) or backs off (transient)
            # so the next call fast-fails instead of re-paying the timeout.
            reason = await self._record_failure(server_name, config, exc)
            raise ValueError(
                f"MCP server '{server_name}' unavailable: {reason}"
            ) from exc
        async with self._lock:
            self._servers[server_name] = state
        return state

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        input_payload: dict[str, Any] | str,
    ) -> str:
        """Invoke *tool_name* on *server_name* with timeout and auto-reconnect.

        On success the server's error counter is reset.  After
        ``MAX_ERRORS_BEFORE_RECONNECT`` consecutive failures the server is
        invalidated and a single reconnect + retry is attempted.
        """
        state = await self.get_or_connect(server_name)
        tool_map = {getattr(t, "name", ""): t for t in state.tools}
        tool = tool_map.get(tool_name)
        if tool is None:
            return f"Tool '{tool_name}' not found on server '{server_name}'."

        try:
            result = await asyncio.wait_for(tool.ainvoke(input_payload), timeout=CALL_TIMEOUT)
            state.consecutive_errors = 0
            return str(result)
        except Exception as exc:
            state.consecutive_errors += 1
            reason = classify_connect_failure(exc)
            logger.bind(mcp_server=server_name, reason=reason).warning(
                "MCP call {}.{} failed (error #{}/{}) [{}]: {}",
                server_name,
                tool_name,
                state.consecutive_errors,
                MAX_ERRORS_BEFORE_RECONNECT,
                reason,
                describe_exception_group(exc),
            )
            logger.opt(exception=exc).debug("MCP call traceback for {}.{}", server_name, tool_name)
            if state.consecutive_errors >= MAX_ERRORS_BEFORE_RECONNECT:
                logger.info(
                    "Reconnecting to '{}' after {} consecutive errors",
                    server_name,
                    state.consecutive_errors,
                )
                await self.invalidate_server(server_name)
                # Retry once after reconnect
                state = await self.get_or_connect(server_name)
                tool_map = {getattr(t, "name", ""): t for t in state.tools}
                tool = tool_map.get(tool_name)
                if tool is None:
                    return (
                        f"Tool '{tool_name}' not found on server '{server_name}' after reconnect."
                    )
                result = await asyncio.wait_for(tool.ainvoke(input_payload), timeout=CALL_TIMEOUT)
                state.consecutive_errors = 0
                return str(result)
            raise

    async def invalidate_server(self, server_name: str) -> None:
        """Clear connection and tool cache for a single server."""
        async with self._lock:
            state = self._servers.pop(server_name, None)
        if state is not None:
            await self._disconnect_server(state)

    async def refresh_if_config_changed(
        self, mcp_config: dict[str, Any], *, connect: bool = True
    ) -> bool:
        """Compare config hash; reconnect changed/new servers, drop removed ones.

        With ``connect=False`` the config is updated and removed/changed
        servers are pruned, but new servers are NOT eagerly dialed — the
        connect is deferred to the first ``get_or_connect`` for that specific
        server (Gitea #130 Phase 2). This is what callers on the hot tool-use
        path use so a dead server never blocks an unrelated tool call.

        Returns ``True`` if the config changed and the pool was refreshed.
        """
        from mewbo_tools.integration.mcp import _normalize_mcp_config

        mcp_config = _normalize_mcp_config(mcp_config)
        new_hash = _config_hash(mcp_config)
        if new_hash == self._config_hash:
            return False

        new_servers = mcp_config.get("servers", {})
        old_names = set(self._servers.keys())
        new_names = set(new_servers.keys())

        # Disconnect removed servers
        for removed in old_names - new_names:
            await self.invalidate_server(removed)

        # Reconnect changed or new servers
        to_connect: dict[str, dict[str, Any]] = {}
        for name in new_names:
            cfg = new_servers[name]
            existing = self._servers.get(name)
            if existing is None or existing.config != cfg:
                if existing is not None:
                    await self.invalidate_server(name)
                to_connect[name] = cfg

        self._mcp_config = mcp_config
        self._config_hash = new_hash

        if to_connect and connect:
            sem = asyncio.Semaphore(MAX_CONCURRENT_CONNECTS)

            async def _reconn(name: str, cfg: dict[str, Any]) -> None:
                async with sem:
                    try:
                        state = await asyncio.wait_for(
                            self._connect_single(name, cfg),
                            timeout=CONNECT_TIMEOUT,
                        )
                        async with self._lock:
                            self._servers[name] = state
                    except Exception as exc:
                        await self._record_failure(name, cfg, exc)

            await asyncio.gather(*[_reconn(n, c) for n, c in to_connect.items()])

        return True

    async def shutdown(self) -> None:
        """Gracefully close all server connections."""
        async with self._lock:
            names = list(self._servers.keys())

        for name in names:
            await self.invalidate_server(name)

        self._config_hash = ""
        self._mcp_config = {}
        logger.info("MCP connection pool shut down")

    def get_all_tool_details(self) -> dict[str, list[dict[str, Any]]]:
        """Return cached tool details for all connected servers.

        Uses the same schema extraction as ``mcp._tool_schema_payload``.
        """
        from mewbo_tools.integration.mcp import _tool_schema_payload

        result: dict[str, list[dict[str, Any]]] = {}
        for name, state in self._servers.items():
            if not state.connected:
                continue
            details: list[dict[str, Any]] = []
            for tool in state.tools:
                details.append(
                    {
                        "name": getattr(tool, "name", ""),
                        "schema": _tool_schema_payload(tool),
                    }
                )
            result[name] = sorted(details, key=lambda d: d.get("name", ""))
        return result

    def is_connected(self, server_name: str) -> bool:
        """True when *server_name* currently has a live connection.

        Cheap, lock-free read used by the tool-call hot path to skip the
        config reload + refresh when the target server is already live —
        a healthy server never triggers a full-config reconnect mid-query.
        """
        state = self._servers.get(server_name)
        return bool(state and state.connected)

    def status_snapshot(self) -> dict[str, dict[str, Any]]:
        """Per-server lifecycle snapshot for surfacing in ``/mcp``.

        Returns ``{name: {status, tools, reason?, retry_in?}}`` where status is
        one of connected/quarantined/backoff/failed/pending.
        """
        now = _monotonic()
        snapshot: dict[str, dict[str, Any]] = {}
        for name, state in self._servers.items():
            entry: dict[str, Any] = {"status": state.status, "tools": len(state.tools)}
            reason = state.skip_reason or state.failure_reason
            if reason:
                entry["reason"] = reason
            if state.next_retry_at and state.next_retry_at > now:
                entry["retry_in"] = round(state.next_retry_at - now)
            snapshot[name] = entry
        return snapshot


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------

_pool: MCPConnectionPool | None = None


def get_mcp_pool() -> MCPConnectionPool:
    """Return the module-level connection pool singleton."""
    global _pool
    if _pool is None:
        _pool = MCPConnectionPool()
    return _pool


def reset_mcp_pool() -> None:
    """Discard the current pool singleton (useful for tests)."""
    global _pool
    _pool = None
    _WARNED_SKIP.clear()
