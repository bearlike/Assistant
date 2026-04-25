#!/usr/bin/env python3
"""MCP tool runner for integrating MCP servers into Mewbo."""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

from mewbo_core.classes import ActionStep
from mewbo_core.common import MockSpeaker, get_logger, get_mock_speaker
from mewbo_core.config import get_mcp_config_path

logging = get_logger(name="tools.integration.mcp")
_LAST_DISCOVERY_FAILURES: dict[str, str] = {}


def _log_discovery_failure(server_name: str, exc: Exception) -> None:
    logging.warning("Failed to discover MCP tools for {}: {}", server_name, exc)
    exceptions = getattr(exc, "exceptions", None)
    if isinstance(exceptions, tuple):
        for idx, sub in enumerate(exceptions, start=1):
            logging.warning("MCP discovery sub-exception {} for {}: {}", idx, server_name, sub)
            logging.opt(exception=sub).debug("MCP discovery sub-exception traceback")
    else:
        logging.opt(exception=exc).debug("MCP discovery traceback")


def _log_runtime_failure(server_name: str, tool_name: str, exc: Exception) -> None:
    logging.warning("MCP runtime error for {}.{}: {}", server_name, tool_name, exc)
    exceptions = getattr(exc, "exceptions", None)
    if isinstance(exceptions, tuple):
        for idx, sub in enumerate(exceptions, start=1):
            logging.warning(
                "MCP runtime sub-exception {} for {}.{}: {}",
                idx,
                server_name,
                tool_name,
                sub,
            )
            logging.opt(exception=sub).debug("MCP runtime sub-exception traceback")
    else:
        logging.opt(exception=exc).debug("MCP runtime traceback")


_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _expand_env_vars(config: dict[str, Any]) -> dict[str, Any]:
    """Recursively expand ``${VAR}`` and ``$VAR`` patterns in string values."""

    def _expand_str(value: str) -> str:
        def _replace(match: re.Match[str]) -> str:
            var_name = match.group(1) or match.group(2)
            resolved = os.environ.get(var_name)
            if resolved is None:
                logging.debug("Env var '{}' referenced in MCP config not found", var_name)
                return match.group(0)  # Leave unresolved
            return resolved

        return _ENV_VAR_PATTERN.sub(_replace, value)

    def _walk(obj: Any) -> Any:
        if isinstance(obj, str):
            return _expand_str(obj)
        if isinstance(obj, dict):
            return {k: _walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_walk(item) for item in obj]
        return obj

    return _walk(config)


# Claude Code-only ``.mcp.json`` extensions that langchain-mcp-adapters'
# session factories don't accept and which would otherwise blow up as
# ``unexpected keyword argument`` when the adapter spreads the connection
# dict into the session constructor. Add new entries here as plugins
# surface them — see the ``type`` pop above for the same pattern.
_UNSUPPORTED_ADAPTER_KEYS = ("oauth",)


def _normalize_mcp_config(config: dict[str, Any]) -> dict[str, Any]:
    """Normalize legacy MCP config keys for adapter compatibility."""
    # Expand env vars first
    config = _expand_env_vars(config)
    # Normalize top-level mcpServers → servers (Claude Code format)
    if "mcpServers" in config and "servers" not in config:
        config["servers"] = config.pop("mcpServers")
    elif "mcpServers" in config:
        config.pop("mcpServers")
    servers = config.get("servers", {})
    for server_config in servers.values():
        if "http_headers" in server_config and "headers" not in server_config:
            server_config["headers"] = server_config.pop("http_headers")
        # Rename "type" → "transport" (Claude Code / VS Code .mcp.json schema).
        # Always pop "type" to avoid it leaking into **kwargs during session
        # creation (deep-merge can produce configs with both keys).
        if "type" in server_config:
            if "transport" not in server_config:
                server_config["transport"] = server_config.pop("type")
            else:
                server_config.pop("type")
        # Infer transport from config shape when neither key is present
        if "transport" not in server_config and "command" in server_config:
            server_config["transport"] = "stdio"
        if server_config.get("transport") == "http":
            server_config["transport"] = "streamable_http"
        for key in _UNSUPPORTED_ADAPTER_KEYS:
            server_config.pop(key, None)
    return config


def _load_mcp_config(
    path: str | None = None,
    *,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Load MCP server configuration from disk.

    When *cwd* is provided (or *path* is omitted), uses
    ``get_merged_mcp_config(cwd)`` to merge global config with any
    CWD-local ``.mcp.json``.  Falls back to single-file loading when
    an explicit *path* is given.

    Returns:
        Parsed MCP configuration dictionary.

    Raises:
        ValueError: If the MCP config path is not set.
        OSError: If the configuration file cannot be read.
        json.JSONDecodeError: If the configuration is invalid JSON.
    """
    if path is None or cwd is not None:
        # Use merged discovery: global + CWD .mcp.json
        from mewbo_core.config import get_merged_mcp_config

        merged = get_merged_mcp_config(cwd)
        if not merged or not merged.get("servers"):
            # Fall back to single-path for backward compat
            config_path = get_mcp_config_path()
            if not config_path:
                raise ValueError("MCP config path is not set.")
            config_path = os.path.abspath(config_path)
            if not os.path.exists(config_path):
                raise ValueError(f"MCP config not found at {config_path}.")
            with open(config_path, encoding="utf-8") as handle:
                merged = json.load(handle)
        return _normalize_mcp_config(merged)

    config_path = os.path.abspath(path)
    if not os.path.exists(config_path):
        raise ValueError(f"MCP config not found at {config_path}.")
    with open(config_path, encoding="utf-8") as handle:
        config = json.load(handle)
    return _normalize_mcp_config(config)


def save_mcp_config(config: dict[str, Any], path: str | None = None) -> None:
    """Persist an MCP configuration payload to disk.

    Args:
        config: MCP configuration payload to write.
        path: Optional explicit file path (defaults to the configured MCP path).
    """
    config_path = path or get_mcp_config_path()
    if not config_path:
        raise ValueError("MCP config path is not set.")
    config_path = os.path.abspath(config_path)
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")


def _schema_from_args_schema(args_schema: Any) -> dict[str, Any] | None:
    """Extract a JSON Schema dict from an MCP tool's args_schema.

    Returns the schema as-is, stripping only Pydantic internals (``$defs``,
    ``definitions``) that reference resolved types.  All standard JSON Schema
    keywords (``type``, ``anyOf``, ``properties``, etc.) are preserved so that
    downstream LLM providers receive valid schemas.
    """
    if args_schema is None:
        return None
    if isinstance(args_schema, dict):
        schema = args_schema
    elif hasattr(args_schema, "model_json_schema"):
        schema = args_schema.model_json_schema()
    elif hasattr(args_schema, "schema"):
        schema = args_schema.schema()
    else:
        return None
    if not isinstance(schema, dict):
        return None
    # Strip Pydantic internals that only make sense alongside $ref.
    return {k: v for k, v in schema.items() if k not in ("$defs", "definitions")}


def _tool_schema_payload(tool: Any) -> dict[str, Any] | None:
    args_schema = getattr(tool, "args_schema", None)
    return _schema_from_args_schema(args_schema)


async def _discover_mcp_tool_details_with_failures_async(
    config: dict[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Exception]]:
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except Exception as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError("langchain-mcp-adapters is required for MCP tools.") from exc

    servers = config.get("servers", {})
    discovered: dict[str, list[dict[str, Any]]] = {}
    failures: dict[str, Exception] = {}
    for server_name, server_config in servers.items():
        try:
            client = MultiServerMCPClient({server_name: server_config})
            tools = await client.get_tools(server_name=server_name)
        except Exception as exc:
            _log_discovery_failure(server_name, exc)
            failures[server_name] = exc
            discovered[server_name] = []
            continue
        details: list[dict[str, Any]] = []
        for tool in tools:
            details.append(
                {
                    "name": tool.name,
                    "schema": _tool_schema_payload(tool),
                }
            )
        discovered[server_name] = sorted(details, key=lambda item: item.get("name", ""))
    return discovered, failures


async def _discover_mcp_tool_details_async(
    config: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    details, _ = await _discover_mcp_tool_details_with_failures_async(config)
    return details


def discover_mcp_tools(config: dict[str, Any]) -> dict[str, list[str]]:
    """Discover MCP tool names per server from configuration."""
    details = discover_mcp_tool_details(config)
    return {
        server_name: [tool["name"] for tool in tools if tool.get("name")]
        for server_name, tools in details.items()
    }


def discover_mcp_tool_details(config: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Discover MCP tool names and schemas per server from configuration."""
    return asyncio.run(_discover_mcp_tool_details_async(_normalize_mcp_config(config)))


def discover_mcp_tool_details_with_failures(
    config: dict[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Exception]]:
    """Discover MCP tool names, schemas, and per-server failures."""
    discovered, failures = asyncio.run(
        _discover_mcp_tool_details_with_failures_async(_normalize_mcp_config(config))
    )
    _record_discovery_failures(failures)
    return discovered, failures


def _record_discovery_failures(failures: dict[str, Exception]) -> None:
    _LAST_DISCOVERY_FAILURES.clear()
    for server_name, exc in failures.items():
        reason = str(exc).strip().splitlines()[0] if exc else "Unknown error"
        _LAST_DISCOVERY_FAILURES[server_name] = reason


def get_last_discovery_failures() -> dict[str, str]:
    """Return last MCP discovery failures per server (if any)."""
    return dict(_LAST_DISCOVERY_FAILURES)


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

    def __init__(self, server_name: str, tool_name: str, *, cwd: str | None = None) -> None:
        """Initialize the MCP tool runner for a specific server tool.

        Args:
            server_name: MCP server name from configuration.
            tool_name: Tool name to invoke on the server.
            cwd: Project working directory for merged config loading.
        """
        self.server_name = server_name
        self.tool_name = tool_name
        self._cwd = cwd

    async def _invoke_async(self, input_payload: str | dict[str, Any]) -> str:
        """Invoke an MCP tool asynchronously and return its output.

        Prefers the connection pool for persistent, cached connections.
        Falls back to the legacy one-shot client path when the pool is
        unavailable.

        Args:
            input_payload: Input payload to send to the MCP tool.

        Returns:
            Stringified tool response.

        Raises:
            RuntimeError: If MCP adapters are not installed.
            ValueError: If the server or tool is not configured.
        """
        try:
            return await self._invoke_via_pool(input_payload)
        except Exception as pool_exc:
            logging.debug(
                "Pool invocation failed for {}.{}, falling back: {}",
                self.server_name,
                self.tool_name,
                pool_exc,
            )
            return await self._invoke_legacy(input_payload)

    async def _invoke_via_pool(self, input_payload: str | dict[str, Any]) -> str:
        """Invoke via the persistent connection pool."""
        from mewbo_tools.integration.mcp_pool import get_mcp_pool

        pool = get_mcp_pool()

        # Ensure the pool has the latest MCP config so it can detect
        # config changes between invocations.
        config = _load_mcp_config(cwd=self._cwd)
        config = _normalize_mcp_config(config)
        await pool.refresh_if_config_changed(config)

        state = await pool.get_or_connect(self.server_name)

        tool_map = {getattr(t, "name", ""): t for t in state.tools} if state.tools else {}
        tool = tool_map.get(self.tool_name)
        if tool is None:
            raise ValueError(
                f"Tool '{self.tool_name}' not found on MCP server '{self.server_name}'."
            )

        prepared = _prepare_mcp_input(tool, input_payload)
        result = await pool.call_tool(self.server_name, self.tool_name, prepared)
        return str(result)

    async def _invoke_legacy(self, input_payload: str | dict[str, Any]) -> str:
        """Legacy one-shot client path (fallback)."""
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
        except Exception as exc:  # pragma: no cover - runtime dependency
            raise RuntimeError("langchain-mcp-adapters is required for MCP tools.") from exc

        config = _load_mcp_config(cwd=self._cwd)
        servers = config.get("servers", {})
        if not servers or self.server_name not in servers:
            raise ValueError(f"MCP server '{self.server_name}' not found in config.")

        client = MultiServerMCPClient({self.server_name: servers[self.server_name]})
        tools = await client.get_tools(server_name=self.server_name)
        tool_map = {tool.name: tool for tool in tools}
        tool = tool_map.get(self.tool_name)
        if tool is None:
            raise ValueError(
                f"Tool '{self.tool_name}' not found on MCP server '{self.server_name}'."
            )
        try:
            result = await tool.ainvoke(_prepare_mcp_input(tool, input_payload))
            return str(result)
        except Exception as exc:
            _log_runtime_failure(self.server_name, self.tool_name, exc)
            raise

    async def arun(self, action_step: ActionStep) -> MockSpeaker:
        """Async execution — preferred when called from an async context.

        Calls ``_invoke_async`` directly, avoiding the ``asyncio.run()``
        wrapper that would fail inside a running event loop.
        """
        if action_step is None:
            raise ValueError("Action step cannot be None.")
        MockSpeakerType = get_mock_speaker()
        result = await self._invoke_async(action_step.tool_input)
        return MockSpeakerType(content=result)

    def run(self, action_step: ActionStep) -> MockSpeaker:
        """Sync execution — for use from sync-only callers.

        Raises:
            ValueError: If action_step is None.
            RuntimeError: If called from inside a running event loop.
        """
        if action_step is None:
            raise ValueError("Action step cannot be None.")
        MockSpeakerType = get_mock_speaker()
        result = asyncio.run(self._invoke_async(action_step.tool_input))
        return MockSpeakerType(content=result)


def _prepare_mcp_input(
    tool: Any,
    input_payload: str | dict[str, Any],
) -> str | dict[str, Any]:
    """Convert action input into the payload expected by MCP tools.

    LangChain MCP tools with args_schema reject raw strings, so we coerce
    string inputs into schema-shaped dictionaries when possible.
    """
    if isinstance(input_payload, dict):
        return input_payload
    if not isinstance(input_payload, str):
        return input_payload

    stripped = input_payload.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed

    args_schema = getattr(tool, "args_schema", None)
    field_names: list[str] = []
    schema_properties: dict[str, Any] | None = None
    if args_schema is not None:
        if isinstance(args_schema, dict):
            props = args_schema.get("properties")
            if isinstance(props, dict):
                schema_properties = props
                field_names = list(props.keys())
        else:
            fields = getattr(args_schema, "model_fields", None)
            if isinstance(fields, dict):
                field_names = list(fields.keys())
            else:
                fields = getattr(args_schema, "__fields__", None)
                if isinstance(fields, dict):
                    field_names = list(fields.keys())

    if not field_names:
        return input_payload

    def _wrap_value(field_name: str) -> dict[str, Any]:
        if schema_properties and field_name in schema_properties:
            prop = schema_properties[field_name]
            if isinstance(prop, dict) and prop.get("type") == "array":
                items = prop.get("items")
                if isinstance(items, dict) and items.get("type") == "string":
                    return {field_name: [input_payload]}
        return {field_name: input_payload}

    if len(field_names) == 1:
        return _wrap_value(field_names[0])

    for preferred in ("query", "question", "input", "text", "q"):
        if preferred in field_names:
            return _wrap_value(preferred)

    return input_payload
