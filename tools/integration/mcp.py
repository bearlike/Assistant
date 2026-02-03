#!/usr/bin/env python3
"""MCP tool runner for integrating MCP servers into Meeseeks."""
from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any

from core.classes import ActionStep
from core.common import MockSpeaker, get_logger, get_mock_speaker

logging = get_logger(name="tools.integration.mcp")


def _normalize_mcp_config(config: dict[str, Any]) -> dict[str, Any]:
    """Normalize legacy MCP config keys for adapter compatibility."""
    servers = config.get("servers", {})
    for server_config in servers.values():
        if "http_headers" in server_config and "headers" not in server_config:
            server_config["headers"] = server_config.pop("http_headers")
        if server_config.get("transport") == "http":
            server_config["transport"] = "streamable_http"
    return config


def _load_mcp_config(path: str | None = None) -> dict[str, Any]:
    """Load MCP server configuration from disk.

    Returns:
        Parsed MCP configuration dictionary.

    Raises:
        ValueError: If MESEEKS_MCP_CONFIG is not set.
        OSError: If the configuration file cannot be read.
        json.JSONDecodeError: If the configuration is invalid JSON.
    """
    config_path = path or os.getenv("MESEEKS_MCP_CONFIG")
    if not config_path:
        raise ValueError("MESEEKS_MCP_CONFIG is not set.")
    config_path = os.path.abspath(config_path)
    with open(config_path, encoding="utf-8") as handle:
        config = json.load(handle)
    return _normalize_mcp_config(config)


def save_mcp_config(config: dict[str, Any], path: str | None = None) -> None:
    """Persist an MCP configuration payload to disk.

    Args:
        config: MCP configuration payload to write.
        path: Optional explicit file path (defaults to MESEEKS_MCP_CONFIG).
    """
    config_path = path or os.getenv("MESEEKS_MCP_CONFIG")
    if not config_path:
        raise ValueError("MESEEKS_MCP_CONFIG is not set.")
    config_path = os.path.abspath(config_path)
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")


def _run_async(coro: Any) -> Any:
    """Run an async coroutine safely from sync code."""
    try:
        return asyncio.run(coro)
    except RuntimeError as exc:
        if "asyncio.run()" not in str(exc):
            raise

    result: dict[str, Any] = {}
    error: dict[str, Exception] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except Exception as inner_exc:  # pragma: no cover - defensive
            error["value"] = inner_exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error["value"]
    return result.get("value")


async def _discover_mcp_tools_async(config: dict[str, Any]) -> dict[str, list[str]]:
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except Exception as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError(
            "langchain-mcp-adapters is required for MCP tools."
        ) from exc

    servers = config.get("servers", {})
    discovered: dict[str, list[str]] = {}
    for server_name, server_config in servers.items():
        client = MultiServerMCPClient({server_name: server_config})
        tools = await client.get_tools(server_name=server_name)
        discovered[server_name] = sorted({tool.name for tool in tools})
    return discovered


def discover_mcp_tools(config: dict[str, Any]) -> dict[str, list[str]]:
    """Discover MCP tool names per server from configuration."""
    return _run_async(_discover_mcp_tools_async(_normalize_mcp_config(config)))


def tool_auto_approved(
    config: dict[str, Any],
    server_name: str,
    tool_name: str,
) -> bool:
    """Return True when a tool is marked as auto-approved."""
    server_config = config.get("servers", {}).get(server_name, {})
    if server_config.get("auto_approve_all"):
        return True
    allowlist = server_config.get("auto_approve_tools", [])
    return tool_name in allowlist


def mark_tool_auto_approved(
    config: dict[str, Any],
    server_name: str,
    tool_name: str,
) -> dict[str, Any]:
    """Record a tool as auto-approved in the MCP config."""
    servers = config.setdefault("servers", {})
    server_config = servers.setdefault(server_name, {})
    allowlist = server_config.setdefault("auto_approve_tools", [])
    if tool_name not in allowlist:
        allowlist.append(tool_name)
        server_config["auto_approve_tools"] = sorted(set(allowlist))
    return config


class MCPToolRunner:
    """Wrapper to invoke MCP tools via langchain-mcp-adapters."""

    def __init__(self, server_name: str, tool_name: str) -> None:
        """Initialize the MCP tool runner for a specific server tool.

        Args:
            server_name: MCP server name from configuration.
            tool_name: Tool name to invoke on the server.
        """
        self.server_name = server_name
        self.tool_name = tool_name

    async def _invoke_async(self, input_text: str) -> str:
        """Invoke an MCP tool asynchronously and return its output.

        Args:
            input_text: Input text to send to the MCP tool.

        Returns:
            Stringified tool response.

        Raises:
            RuntimeError: If MCP adapters are not installed.
            ValueError: If the server or tool is not configured.
        """
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
        except Exception as exc:  # pragma: no cover - runtime dependency
            raise RuntimeError(
                "langchain-mcp-adapters is required for MCP tools."
            ) from exc

        config = _load_mcp_config()
        servers = config.get("servers", {})
        if not servers or self.server_name not in servers:
            raise ValueError(
                f"MCP server '{self.server_name}' not found in config."
            )

        client = MultiServerMCPClient({self.server_name: servers[self.server_name]})
        tools = await client.get_tools(server_name=self.server_name)
        tool_map = {tool.name: tool for tool in tools}
        tool = tool_map.get(self.tool_name)
        if tool is None:
            raise ValueError(
                f"Tool '{self.tool_name}' not found on MCP server '{self.server_name}'."
            )
        result = await tool.ainvoke(input_text)
        return str(result)

    def run(self, action_step: ActionStep) -> MockSpeaker:
        """Execute the MCP tool using the action step argument.

        Args:
            action_step: Action step containing the prompt argument.

        Returns:
            MockSpeaker with the tool response content.

        Raises:
            ValueError: If action_step is None.
        """
        if action_step is None:
            raise ValueError("Action step cannot be None.")
        MockSpeakerType = get_mock_speaker()
        result = asyncio.run(self._invoke_async(action_step.action_argument))
        return MockSpeakerType(content=result)
