#!/usr/bin/env python3
"""Tool registry and manifest loading for Meeseeks."""

from __future__ import annotations

import importlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from meeseeks_core.classes import ActionStep, set_available_tools
from meeseeks_core.common import MockSpeaker, get_logger
from meeseeks_core.components import resolve_home_assistant_status
from meeseeks_core.types import JsonValue

logging = get_logger(name="core.tool_registry")


def _load_mcp_support():
    try:
        from meeseeks_tools.integration import mcp as mcp_module
    except Exception as exc:  # pragma: no cover - optional dependency
        logging.debug("MCP support unavailable: {}", exc)
        return None
    return mcp_module


class ToolRunner(Protocol):
    def run(self, action_step: ActionStep) -> MockSpeaker:  # pragma: no cover
        """Execute an action step and return a speaker response.

        Args:
            action_step: Action step payload to execute.

        Returns:
            MockSpeaker response from the tool.
        """


@dataclass(frozen=True)
class ToolSpec:
    """Metadata describing a tool available to the assistant."""

    tool_id: str
    name: str
    description: str
    factory: Callable[[], ToolRunner]
    enabled: bool = True
    kind: str = "local"
    prompt_path: str | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)


class ToolRegistry:
    """Registry of configured tools and their instantiated runners."""

    def __init__(self) -> None:
        """Initialize an empty registry."""
        self._tools: dict[str, ToolSpec] = {}
        self._instances: dict[str, ToolRunner] = {}

    def disable(self, tool_id: str, reason: str) -> None:
        """Disable a tool and store a reason for later reporting."""
        spec = self._tools.get(tool_id)
        if spec is None:
            return
        metadata = dict(spec.metadata)
        metadata["disabled_reason"] = reason
        self._tools[tool_id] = ToolSpec(
            tool_id=spec.tool_id,
            name=spec.name,
            description=spec.description,
            factory=spec.factory,
            enabled=False,
            kind=spec.kind,
            prompt_path=spec.prompt_path,
            metadata=metadata,
        )
        if tool_id in self._instances:
            self._instances.pop(tool_id, None)
        set_available_tools(
            [current_id for current_id, current_spec in self._tools.items() if current_spec.enabled]
        )

    def register(self, spec: ToolSpec) -> None:
        """Register a tool specification and update action validation."""
        self._tools[spec.tool_id] = spec
        set_available_tools(
            [tool_id for tool_id, tool_spec in self._tools.items() if tool_spec.enabled]
        )

    def get(self, tool_id: str) -> ToolRunner | None:
        """Return an enabled tool runner, instantiating it if needed."""
        spec = self._tools.get(tool_id)
        if spec is None or not spec.enabled:
            return None
        if tool_id not in self._instances:
            try:
                self._instances[tool_id] = spec.factory()
            except Exception as exc:  # pragma: no cover - defensive
                reason = f"Initialization failed: {exc}"
                logging.warning("Disabling tool {}: {}", tool_id, reason)
                self.disable(tool_id, reason)
                return None
        return self._instances[tool_id]

    def get_spec(self, tool_id: str) -> ToolSpec | None:
        """Return the tool specification, even if disabled."""
        return self._tools.get(tool_id)

    def list_specs(self, include_disabled: bool = False) -> list[ToolSpec]:
        """List tool specifications, optionally including disabled tools."""
        specs = list(self._tools.values())
        if include_disabled:
            return specs
        return [spec for spec in specs if spec.enabled]

    def tool_catalog(self) -> list[dict[str, str]]:
        """Return a serialized catalog of registered tool metadata."""
        return [
            {
                "tool_id": spec.tool_id,
                "name": spec.name,
                "description": spec.description,
            }
            for spec in self.list_specs()
        ]


def _import_factory(module_path: str, class_name: str) -> Callable[[], ToolRunner]:
    """Return a factory that instantiates a tool by import path."""

    def _factory() -> ToolRunner:
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        return cls()

    return _factory


def _default_registry() -> ToolRegistry:
    """Create the built-in registry for local tools."""
    registry = ToolRegistry()
    ha_status = resolve_home_assistant_status()
    registry.register(
        ToolSpec(
            tool_id="home_assistant_tool",
            name="Home Assistant",
            description="Manage smart home devices via Home Assistant.",
            factory=_import_factory(
                "meeseeks_tools.integration.homeassistant",
                "HomeAssistant",
            ),
            enabled=ha_status.enabled,
            prompt_path="tools/home-assistant",
            metadata={"disabled_reason": ha_status.reason} if not ha_status.enabled else {},
        )
    )
    return registry


def _default_manifest_cache_path() -> str:
    base_dir = os.getenv("MESEEKS_CONFIG_DIR")
    if not base_dir:
        base_dir = os.path.join(os.path.expanduser("~"), ".meeseeks")
    os.makedirs(base_dir, exist_ok=True)
    return os.path.join(base_dir, "tool-manifest.auto.json")


def _sanitize_tool_id(server_name: str, tool_name: str) -> str:
    raw = f"mcp_{server_name}_{tool_name}".lower()
    raw = re.sub(r"[^a-z0-9_]+", "_", raw)
    raw = re.sub(r"_+", "_", raw).strip("_")
    return raw


def _built_in_manifest_entries() -> list[dict[str, object]]:
    ha_status = resolve_home_assistant_status()
    entries: list[dict[str, object]] = [
        {
            "tool_id": "home_assistant_tool",
            "name": "Home Assistant",
            "description": "Manage smart home devices via Home Assistant.",
            "module": "meeseeks_tools.integration.homeassistant",
            "class": "HomeAssistant",
            "kind": "local",
            "enabled": ha_status.enabled,
            "prompt": "tools/home-assistant",
        },
    ]
    if not ha_status.enabled and ha_status.reason:
        entries[0]["disabled_reason"] = ha_status.reason
    return entries


def _build_manifest_payload(
    mcp_tools: dict[str, list[dict[str, object]]],
) -> dict[str, object]:
    tools: list[dict[str, object]] = _built_in_manifest_entries()
    for server_name, tool_specs in mcp_tools.items():
        for tool_spec in tool_specs:
            tool_name = str(tool_spec.get("name", "")).strip()
            if not tool_name:
                continue
            tools.append(
                {
                    "tool_id": _sanitize_tool_id(server_name, tool_name),
                    "name": tool_name,
                    "description": f"MCP tool `{tool_name}` from `{server_name}`.",
                    "kind": "mcp",
                    "server": server_name,
                    "tool": tool_name,
                    "enabled": True,
                    "schema": tool_spec.get("schema"),
                }
            )
    return {"tools": tools}


def _ensure_auto_manifest(mcp_config_path: str) -> str | None:
    manifest_path = _default_manifest_cache_path()
    existing_manifest: dict[str, JsonValue] | None = None
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as handle:
                existing_manifest = json.load(handle)
        except Exception as exc:
            logging.warning("Failed to read existing MCP manifest: {}", exc)

    mcp_module = _load_mcp_support()
    if mcp_module is None:
        return manifest_path if os.path.exists(manifest_path) else None

    try:
        config = mcp_module._load_mcp_config(mcp_config_path)
        mcp_tools, failures = mcp_module.discover_mcp_tool_details_with_failures(config)
    except Exception as exc:
        logging.warning("Failed to auto-discover MCP tools: {}", exc)
        return manifest_path if os.path.exists(manifest_path) else None

    payload = _build_manifest_payload(mcp_tools)
    if failures and existing_manifest:
        payload_tools = payload.get("tools", [])
        if not isinstance(payload_tools, list):
            payload_tools = []
        tools_by_id: dict[str, dict[str, JsonValue]] = {}
        for tool in payload_tools:
            if not isinstance(tool, dict):
                continue
            tool_id = tool.get("tool_id")
            if not tool_id:
                continue
            tools_by_id[str(tool_id)] = tool
        cached_tools = existing_manifest.get("tools", [])
        if not isinstance(cached_tools, list):
            cached_tools = []
        for tool in cached_tools:
            if not isinstance(tool, dict):
                continue
            if tool.get("kind") != "mcp":
                continue
            server_name = tool.get("server")
            if server_name not in failures:
                continue
            tool_id = tool.get("tool_id")
            if not tool_id:
                continue
            disabled_tool = dict(tool)
            disabled_tool["enabled"] = False
            disabled_tool["disabled_reason"] = f"Discovery failed: {failures[server_name]}"
            tools_by_id[tool_id] = disabled_tool
        payload["tools"] = list(tools_by_id.values())
    try:
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
    except OSError as exc:
        logging.warning("Failed to write MCP tool manifest: {}", exc)
        return manifest_path if os.path.exists(manifest_path) else None
    return manifest_path


def load_registry(manifest_path: str | None = None) -> ToolRegistry:
    """Load tool registry from a JSON manifest if available."""
    if manifest_path is None:
        manifest_path = os.getenv("MESEEKS_TOOL_MANIFEST")

    if not manifest_path:
        mcp_config_path = os.getenv("MESEEKS_MCP_CONFIG")
        if mcp_config_path:
            auto_manifest = _ensure_auto_manifest(mcp_config_path)
            if auto_manifest:
                manifest_path = auto_manifest

    if not manifest_path:
        return _default_registry()

    manifest_path = os.path.abspath(manifest_path)
    if not os.path.exists(manifest_path):
        logging.warning("Tool manifest not found: {}", manifest_path)
        return _default_registry()

    try:
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except Exception as exc:  # pragma: no cover - defensive
        logging.error("Failed to load tool manifest: {}", exc)
        return _default_registry()

    registry = ToolRegistry()
    for tool in manifest.get("tools", []):
        kind = tool.get("kind", "local")
        prompt_path = tool.get("prompt")
        if kind == "local":
            module_path = tool.get("module")
            class_name = tool.get("class")
            if not module_path or not class_name:
                logging.warning("Skipping tool with missing module/class: {}", tool)
                continue
            factory = _import_factory(module_path, class_name)
        else:
            mcp_module = _load_mcp_support()
            if mcp_module is None:
                logging.warning(
                    "Skipping MCP tool because MCP support is not installed: {}",
                    tool,
                )
                continue
            MCPToolRunner = mcp_module.MCPToolRunner

            server_name = tool.get("server")
            tool_name = tool.get("tool")
            if not server_name or not tool_name:
                logging.warning("Skipping MCP tool with missing server/tool: {}", tool)
                continue

            def _mcp_factory(
                server_name: str = server_name,
                tool_name: str = tool_name,
            ) -> ToolRunner:
                return MCPToolRunner(server_name=server_name, tool_name=tool_name)

            factory = _mcp_factory

        spec = ToolSpec(
            tool_id=tool.get("tool_id", ""),
            name=tool.get("name", tool.get("tool_id", "")),
            description=tool.get("description", ""),
            factory=factory,
            enabled=tool.get("enabled", True),
            kind=kind,
            prompt_path=prompt_path,
            metadata={
                key: value
                for key, value in tool.items()
                if key
                not in {
                    "tool_id",
                    "name",
                    "description",
                    "module",
                    "class",
                    "enabled",
                    "kind",
                    "prompt",
                }
            },
        )
        if not spec.tool_id:
            logging.warning("Skipping tool with empty tool_id: {}", tool)
            continue
        registry.register(spec)

    if not registry.list_specs(include_disabled=True):
        return _default_registry()

    return registry


__all__ = [
    "ToolRegistry",
    "ToolSpec",
    "load_registry",
]
