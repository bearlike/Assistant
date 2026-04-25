#!/usr/bin/env python3
"""Tool registry and manifest loading for Truss."""

from __future__ import annotations

import importlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from truss_core.classes import ActionStep, set_available_tools
from truss_core.common import MockSpeaker, get_logger
from truss_core.components import ComponentStatus, resolve_home_assistant_status
from truss_core.config import get_config_value, get_mcp_config_path
from truss_core.types import JsonValue

logging = get_logger(name="core.tool_registry")

# Built-in tool ID for the on-demand schema fetcher. MCP tools (and any
# spec with metadata.deferred=True) are stripped from the initial bind
# and discovered through this tool when ``agent.tool_search.mode == "on"``.
TOOL_SEARCH_TOOL_ID = "tool_search"


def _load_mcp_support():
    try:
        from truss_tools.integration import mcp as mcp_module
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
    concurrency_safe: bool = True  # Can run in parallel (True for backward compat)
    read_only: bool = False  # No side effects
    interrupt_behavior: str = "block"  # "cancel" or "block" on user interrupt
    max_result_chars: int = 2000  # Per-tool result size cap (0 = unlimited)
    timeout: float = 120.0  # Per-tool execution timeout in seconds

    def is_plan_safe(self) -> bool:
        """Return True if the tool is safe to use in plan mode.

        A tool is plan-safe when it does not mutate state — i.e., its
        ``read_only`` field is True. The legacy ``plan_safe`` metadata key
        is still honoured as a fallback for external tool manifests.
        """
        if self.read_only:
            return True
        return bool(self.metadata.get("plan_safe"))


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
            concurrency_safe=spec.concurrency_safe,
            read_only=spec.read_only,
            interrupt_behavior=spec.interrupt_behavior,
            max_result_chars=spec.max_result_chars,
            timeout=spec.timeout,
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

    def list_specs_for_mode(self, mode: str, *, include_disabled: bool = False) -> list[ToolSpec]:
        """List specs filtered by orchestration mode."""
        specs = self.list_specs(include_disabled=include_disabled)
        if mode != "plan":
            return specs
        return [spec for spec in specs if spec.is_plan_safe()]

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


def is_always_load(spec: ToolSpec) -> bool:
    """Return True if the tool's full schema must always be in the bound list.

    Marked via ``metadata.always_load=True``. Mirrors Claude Code's
    ``alwaysLoad`` opt-out — used by tools that the model needs immediately
    (the search tool itself, or any tool whose absence would block the
    model from making progress).
    """
    return bool(spec.metadata.get("always_load"))


def is_deferred(spec: ToolSpec) -> bool:
    """Return True if the tool's schema should be omitted from the initial bind.

    Deferred tools surface as names only via ``<available-deferred-tools>`` —
    the model fetches their schemas on demand via ``tool_search``. Mirrors
    Claude Code's ``isDeferredTool``: ``always_load`` wins, the search tool
    itself never defers, all MCP tools defer, and other tools opt-in via
    ``metadata.deferred=True``.
    """
    if is_always_load(spec):
        return False
    if spec.tool_id == TOOL_SEARCH_TOOL_ID:
        return False
    if spec.kind == "mcp":
        return True
    return bool(spec.metadata.get("deferred"))


_TOOL_SEARCH_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Use 'select:tool_a,tool_b' for direct fetch by name, "
                "or keywords for fuzzy search. Prefix a term with '+' to "
                "require it (e.g. '+linear issues')."
            ),
        },
        "max_results": {
            "type": "integer",
            "description": "Maximum number of matches to return (default 5).",
        },
    },
    "required": ["query"],
}


_FILE_EDIT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string", "description": "Path to the file to edit"},
        "old_string": {"type": "string", "description": "Exact string to find in the file"},
        "new_string": {"type": "string", "description": "Replacement string"},
        "replace_all": {
            "type": "boolean",
            "description": "Replace all occurrences (default false)",
        },
    },
    "required": ["file_path", "old_string", "new_string"],
}

_AIDER_EDIT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "content": {"type": "string", "description": "SEARCH/REPLACE block content"},
        "root": {"type": "string", "description": "Project root directory"},
    },
    "required": ["content"],
}


def _all_edit_tool_specs() -> list[ToolSpec]:
    """Return ToolSpecs for both edit tools.

    Both are always registered; ``ToolUseLoop._configured_edit_tool_id()``
    selects the active one per-model at schema-building time.
    """
    file_edit_spec = ToolSpec(
        tool_id="file_edit_tool",
        name="File Edit",
        description="Apply exact string replacement to a file.",
        factory=_import_factory("truss_tools.integration.file_edit_tool", "FileEditTool"),
        prompt_path="tools/file-edit",
        concurrency_safe=False,
        metadata={
            "reflect": True,
            "capabilities": ["file_write"],
            "schema": _FILE_EDIT_SCHEMA,
        },
    )
    aider_edit_spec = ToolSpec(
        tool_id="aider_edit_block_tool",
        name="Aider Edit Blocks",
        description="Apply Aider-style SEARCH/REPLACE blocks to files.",
        factory=_import_factory("truss_tools.integration.aider_edit_blocks", "AiderEditBlockTool"),
        prompt_path="tools/aider-edit-blocks",
        concurrency_safe=False,
        metadata={
            "reflect": True,
            "capabilities": ["file_write"],
            "schema": _AIDER_EDIT_SCHEMA,
        },
    )
    return [file_edit_spec, aider_edit_spec]


def _all_edit_tool_manifest_entries() -> list[dict[str, object]]:
    """Return manifest entries for both edit tools."""
    return [
        {
            "tool_id": "file_edit_tool",
            "name": "File Edit",
            "description": "Apply exact string replacement to a file.",
            "module": "truss_tools.integration.file_edit_tool",
            "class": "FileEditTool",
            "kind": "local",
            "enabled": True,
            "prompt": "tools/file-edit",
            "reflect": True,
            "capabilities": ["file_write"],
            "schema": _FILE_EDIT_SCHEMA,
        },
        {
            "tool_id": "aider_edit_block_tool",
            "name": "Aider Edit Blocks",
            "description": "Apply Aider-style SEARCH/REPLACE blocks to files.",
            "module": "truss_tools.integration.aider_edit_blocks",
            "class": "AiderEditBlockTool",
            "kind": "local",
            "enabled": True,
            "prompt": "tools/aider-edit-blocks",
            "reflect": True,
            "capabilities": ["file_write"],
            "schema": _AIDER_EDIT_SCHEMA,
        },
    ]


def _resolve_lsp_status() -> ComponentStatus:
    """Determine whether the LSP tool should be enabled."""
    from truss_core.config import get_config

    cfg = get_config()
    lsp_cfg = cfg.agent.lsp
    if not lsp_cfg.enabled:
        return ComponentStatus(name="lsp_tool", enabled=False, reason="disabled via config")
    try:
        from truss_tools.integration.lsp import LSP_AVAILABLE

        if not LSP_AVAILABLE:
            return ComponentStatus(
                name="lsp_tool",
                enabled=False,
                reason="pygls not installed",
            )
        from truss_tools.integration.lsp.servers import available_servers

        servers = available_servers(lsp_cfg.servers)
        if not servers:
            return ComponentStatus(
                name="lsp_tool",
                enabled=False,
                reason="no language server binaries found on PATH",
            )
    except Exception as exc:
        return ComponentStatus(name="lsp_tool", enabled=False, reason=str(exc))
    return ComponentStatus(name="lsp_tool", enabled=True)


def _default_registry() -> ToolRegistry:
    """Create the built-in registry for local tools."""
    registry = ToolRegistry()
    ha_status = resolve_home_assistant_status()
    ha_metadata: dict[str, JsonValue] = {
        "schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Task to perform"},
            },
            "required": ["task"],
        },
    }
    if not ha_status.enabled:
        ha_metadata["disabled_reason"] = ha_status.reason
    registry.register(
        ToolSpec(
            tool_id="home_assistant_tool",
            name="Home Assistant",
            description="Manage smart home devices via Home Assistant.",
            factory=_import_factory(
                "truss_tools.integration.homeassistant",
                "HomeAssistant",
            ),
            enabled=ha_status.enabled,
            prompt_path="tools/home-assistant",
            metadata=ha_metadata,
        )
    )
    for edit_spec in _all_edit_tool_specs():
        registry.register(edit_spec)
    registry.register(
        ToolSpec(
            tool_id="read_file",
            name="Read File",
            description="Read local files.",
            factory=_import_factory(
                "truss_tools.integration.aider_file_tools",
                "ReadFileTool",
            ),
            prompt_path="tools/read-file",
            read_only=True,
            metadata={
                "schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path to read"},
                        "root": {"type": "string", "description": "Project root"},
                        "offset": {
                            "type": "integer",
                            "description": "Line to start from (0-based). For large files.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max lines to read. Defaults to 2000.",
                        },
                    },
                    "required": ["path"],
                },
            },
        )
    )
    registry.register(
        ToolSpec(
            tool_id="aider_list_dir_tool",
            name="Aider List Directory",
            description="List files under a directory using Aider helpers.",
            factory=_import_factory(
                "truss_tools.integration.aider_file_tools",
                "AiderListDirTool",
            ),
            prompt_path="tools/aider-list-dir",
            read_only=True,
            metadata={
                "schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Directory path to list"},
                    },
                    "required": ["path"],
                },
            },
        )
    )
    registry.register(
        ToolSpec(
            tool_id="aider_shell_tool",
            name="Aider Shell",
            description="Run shell commands using Aider helpers.",
            factory=_import_factory(
                "truss_tools.integration.aider_shell_tool",
                "AiderShellTool",
            ),
            prompt_path="tools/aider-shell",
            concurrency_safe=False,
            metadata={
                "reflect": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "Shell command to execute",
                        },
                        "cwd": {"type": "string", "description": "Working directory"},
                    },
                    "required": ["command"],
                },
            },
        )
    )
    # LSP tool — opt-in, requires pygls + at least one server binary
    lsp_status = _resolve_lsp_status()
    lsp_meta: dict[str, JsonValue] = {
        "schema": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["diagnostics", "definition", "references", "hover"],
                    "description": "LSP operation to perform",
                },
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the file",
                },
                "line": {
                    "type": "integer",
                    "description": "0-based line number (for definition/references/hover)",
                },
                "character": {
                    "type": "integer",
                    "description": "0-based column (for definition/references/hover)",
                },
            },
            "required": ["operation", "file_path"],
        },
    }
    if not lsp_status.enabled:
        lsp_meta["disabled_reason"] = lsp_status.reason
    registry.register(
        ToolSpec(
            tool_id="lsp_tool",
            name="Language Server",
            description=(
                "Query language servers for code diagnostics, "
                "go-to-definition, find-references, and hover info."
            ),
            factory=_import_factory(
                "truss_tools.integration.lsp.tool",
                "LSPTool",
            ),
            enabled=lsp_status.enabled,
            prompt_path="tools/lsp",
            read_only=True,
            concurrency_safe=True,
            metadata=lsp_meta,
        )
    )
    _register_tool_search(registry)
    return registry


def _register_tool_search(registry: ToolRegistry) -> None:
    """Register the on-demand schema-fetching tool against ``registry``.

    Bound last so the runner's factory closes over the registry instance
    that holds every other spec — including any MCP entries merged in by
    ``load_registry``. Always-loaded itself (``metadata.always_load``) so
    the model can reach it on turn one.
    """

    def _factory() -> ToolRunner:
        from truss_tools.integration.tool_search import ToolSearchRunner

        return ToolSearchRunner(registry)

    registry.register(
        ToolSpec(
            tool_id=TOOL_SEARCH_TOOL_ID,
            name="Tool Search",
            description=(
                "Fetch full JSON schemas for deferred tools so they can be "
                "called. Use 'select:name1,name2' for direct fetch or "
                "keywords for fuzzy search."
            ),
            factory=_factory,
            prompt_path="tools/tool-search",
            read_only=True,
            concurrency_safe=True,
            metadata={"schema": _TOOL_SEARCH_SCHEMA, "always_load": True},
        )
    )


def _default_manifest_cache_path() -> str:
    base_dir = get_config_value("runtime", "config_dir")
    if not base_dir:
        base_dir = os.path.join(os.path.expanduser("~"), ".truss")
    base_dir = os.path.expanduser(str(base_dir))
    os.makedirs(base_dir, exist_ok=True)
    return os.path.join(base_dir, "tool-manifest.auto.json")


def _sanitize_tool_id(server_name: str, tool_name: str) -> str:
    """Build the internal ``mcp_<server>_<tool>`` tool id.

    Some MCP servers (searxng, deepwiki, …) emit tool names that already
    embed the server name (e.g. server ``internet-search`` exposes
    ``Internet-Search-searxng_web_search``). Concatenating naively yields
    duplicate-prefix ids like ``mcp_internet_search_internet_search_…``,
    which leak into Langfuse traces, the picker, and skill allowlists.
    Strip the redundant prefix once both sides have been normalised so
    the id stays ``mcp_<server>_<tool>``.
    """
    server_norm = re.sub(r"[^a-z0-9_]+", "_", server_name.lower()).strip("_")
    tool_norm = re.sub(r"[^a-z0-9_]+", "_", tool_name.lower()).strip("_")
    if server_norm and tool_norm.startswith(f"{server_norm}_"):
        tool_norm = tool_norm[len(server_norm) + 1 :]
    raw = f"mcp_{server_norm}_{tool_norm}" if tool_norm else f"mcp_{server_norm}"
    return re.sub(r"_+", "_", raw).strip("_")


def _built_in_manifest_entries() -> list[dict[str, object]]:
    ha_status = resolve_home_assistant_status()
    entries: list[dict[str, object]] = [
        {
            "tool_id": "home_assistant_tool",
            "name": "Home Assistant",
            "description": "Manage smart home devices via Home Assistant.",
            "module": "truss_tools.integration.homeassistant",
            "class": "HomeAssistant",
            "kind": "local",
            "enabled": ha_status.enabled,
            "prompt": "tools/home-assistant",
            "schema": {
                "type": "object",
                "properties": {"task": {"type": "string", "description": "Task to perform"}},
                "required": ["task"],
            },
        },
        *_all_edit_tool_manifest_entries(),
        {
            "tool_id": "read_file",
            "name": "Read File",
            "description": "Read local files.",
            "module": "truss_tools.integration.aider_file_tools",
            "class": "ReadFileTool",
            "kind": "local",
            "enabled": True,
            "prompt": "tools/read-file",
            "read_only": True,
            "schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read"},
                    "root": {"type": "string", "description": "Project root"},
                    "offset": {
                        "type": "integer",
                        "description": "Line to start from (0-based). For large files.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max lines to read. Defaults to 2000.",
                    },
                },
                "required": ["path"],
            },
        },
        {
            "tool_id": "aider_list_dir_tool",
            "name": "Aider List Directory",
            "description": "List files under a directory using Aider helpers.",
            "module": "truss_tools.integration.aider_file_tools",
            "class": "AiderListDirTool",
            "kind": "local",
            "enabled": True,
            "prompt": "tools/aider-list-dir",
            "read_only": True,
            "schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path to list"},
                },
                "required": ["path"],
            },
        },
        {
            "tool_id": "aider_shell_tool",
            "name": "Aider Shell",
            "description": "Run shell commands using Aider helpers.",
            "module": "truss_tools.integration.aider_shell_tool",
            "class": "AiderShellTool",
            "kind": "local",
            "enabled": True,
            "prompt": "tools/aider-shell",
            "reflect": True,
            "schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "cwd": {"type": "string", "description": "Working directory"},
                },
                "required": ["command"],
            },
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


def _try_pool_discovery(
    mcp_config_path: str,
    *,
    cwd: str | None = None,
    extra_mcp_servers: dict[str, dict] | None = None,
) -> dict[str, list[dict[str, object]]] | None:
    """Attempt MCP tool discovery via the connection pool.

    Returns the tool details dict on success, or ``None`` if the pool
    path is unavailable or fails.
    """
    try:
        from truss_tools.integration.mcp import _normalize_mcp_config
        from truss_tools.integration.mcp_pool import get_mcp_pool
    except Exception:
        return None

    try:
        import asyncio

        if extra_mcp_servers:
            from truss_core.config import get_merged_mcp_config

            merged = get_merged_mcp_config(cwd, extra_servers=extra_mcp_servers)
            config = _normalize_mcp_config(merged)
        else:
            from truss_tools.integration.mcp import _load_mcp_config

            config = _normalize_mcp_config(_load_mcp_config(mcp_config_path, cwd=cwd))
        pool = get_mcp_pool()
        # refresh_if_config_changed diffs against the pool's previous config
        # and disconnects servers that are no longer present — essential when
        # the same long-lived pool serves multiple project scopes in the API
        # process. connect_all is additive-only and would let a previous
        # project's MCP servers bleed into the current project's tool list.
        asyncio.run(pool.refresh_if_config_changed(config))
        details = pool.get_all_tool_details()
        # If pool connected but discovered zero tools across all servers,
        # treat that as a failure and fall through to the legacy path.
        total_tools = sum(len(tools) for tools in details.values())
        if total_tools == 0:
            logging.debug("Pool connected but found no tools, falling back to legacy")
            return None
        return details
    except Exception as exc:
        logging.debug("Pool-based MCP discovery failed, will use legacy path: {}", exc)
        return None


def _ensure_auto_manifest(
    mcp_config_path: str,
    *,
    cwd: str | None = None,
    extra_mcp_servers: dict[str, dict] | None = None,
) -> str | None:
    manifest_path = _default_manifest_cache_path()
    existing_manifest: dict[str, JsonValue] | None = None
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as handle:
                existing_manifest = json.load(handle)
        except Exception as exc:
            logging.warning("Failed to read existing MCP manifest: {}", exc)

    # Try pool-based discovery first (faster, persistent connections)
    pool_tools = _try_pool_discovery(mcp_config_path, cwd=cwd, extra_mcp_servers=extra_mcp_servers)

    mcp_module = _load_mcp_support()
    mcp_tools: dict[str, list[dict[str, object]]] = {}
    failures: dict[str, Exception] = {}
    global_failure: Exception | None = None

    if pool_tools is not None:
        mcp_tools = pool_tools
        logging.debug("MCP tools discovered via connection pool")
    elif mcp_module is None:
        global_failure = RuntimeError("MCP support is not installed.")
    else:
        try:
            if extra_mcp_servers:
                from truss_core.config import get_merged_mcp_config

                config = mcp_module._normalize_mcp_config(
                    get_merged_mcp_config(cwd, extra_servers=extra_mcp_servers)
                )
            else:
                config = mcp_module._load_mcp_config(
                    mcp_config_path if mcp_config_path else None, cwd=cwd
                )
            mcp_tools, failures = mcp_module.discover_mcp_tool_details_with_failures(config)
        except Exception as exc:
            logging.warning("Failed to auto-discover MCP tools: {}", exc)
            global_failure = exc

    payload = _build_manifest_payload(mcp_tools)
    if (failures or global_failure) and existing_manifest:
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
            if not isinstance(server_name, str) or not server_name:
                continue
            if not global_failure and server_name not in failures:
                continue
            tool_id = tool.get("tool_id")
            if not tool_id:
                continue
            disabled_tool = dict(tool)
            disabled_tool["enabled"] = False
            if global_failure:
                disabled_tool["disabled_reason"] = f"Discovery failed: {global_failure}"
            else:
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


def load_registry(
    manifest_path: str | None = None,
    *,
    cwd: str | None = None,
    extra_mcp_servers: dict[str, dict] | None = None,
) -> ToolRegistry:
    """Load tool registry, auto-discovering MCP tools when configured."""
    if manifest_path is None:
        mcp_config_path = get_mcp_config_path()
        # Check for CWD .mcp.json even when no global config exists
        has_cwd_mcp = False
        if cwd:
            from pathlib import Path as _Path

            has_cwd_mcp = (_Path(cwd) / ".mcp.json").is_file()
        # Also check for subtree .mcp.json files
        has_subtree_mcp = False
        if cwd and not has_cwd_mcp:
            from truss_core.config import _discover_subtree_mcp_json

            has_subtree_mcp = bool(_discover_subtree_mcp_json(cwd))
        if (
            (mcp_config_path and os.path.exists(mcp_config_path))
            or has_cwd_mcp
            or has_subtree_mcp
            or extra_mcp_servers
        ):
            manifest_path = _ensure_auto_manifest(
                mcp_config_path or "", cwd=cwd, extra_mcp_servers=extra_mcp_servers
            )

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
                _cwd: str | None = cwd,
            ) -> ToolRunner:
                return MCPToolRunner(server_name=server_name, tool_name=tool_name, cwd=_cwd)

            factory = _mcp_factory

        spec = ToolSpec(
            tool_id=tool.get("tool_id", ""),
            name=tool.get("name", tool.get("tool_id", "")),
            description=tool.get("description", ""),
            factory=factory,
            enabled=tool.get("enabled", True),
            kind=kind,
            prompt_path=prompt_path,
            read_only=bool(tool.get("read_only", False)),
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
                    "read_only",
                }
            },
        )
        if not spec.tool_id:
            logging.warning("Skipping tool with empty tool_id: {}", tool)
            continue
        registry.register(spec)

    if not registry.list_specs(include_disabled=True):
        return _default_registry()

    builtin_registry = _default_registry()
    existing_ids = {spec.tool_id for spec in registry.list_specs(include_disabled=True)}
    for spec in builtin_registry.list_specs(include_disabled=True):
        if spec.tool_id == TOOL_SEARCH_TOOL_ID:
            # Skip — re-registered below so its factory binds to ``registry``,
            # not the throwaway ``builtin_registry`` instance.
            continue
        if spec.tool_id in existing_ids:
            continue
        registry.register(spec)
        existing_ids.add(spec.tool_id)

    # Bind tool_search to the final, merged registry so it can search all specs.
    _register_tool_search(registry)

    set_available_tools([spec.tool_id for spec in registry.list_specs()])
    return registry


def filter_specs(
    specs: list[ToolSpec],
    *,
    allowed: list[str] | None = None,
    denied: list[str] | None = None,
) -> list[ToolSpec]:
    """Filter tool specs by allowlist and/or denylist.

    If *allowed* is non-empty only specs whose ``tool_id`` is in the list
    are kept.  Then any spec whose ``tool_id`` appears in *denied* (merged
    with the config ``agent.default_denied_tools``) is removed.  Deny
    always takes precedence over allow.
    """
    if allowed:
        allowed_set = set(allowed)
        specs = [s for s in specs if s.tool_id in allowed_set]

    denied_set: set[str] = set(denied or [])
    config_denied_raw = get_config_value("agent", "default_denied_tools", default=[])
    if isinstance(config_denied_raw, str):
        config_denied_raw = [s.strip() for s in config_denied_raw.split(",") if s.strip()]
    denied_set |= set(config_denied_raw or [])

    if denied_set:
        specs = [s for s in specs if s.tool_id not in denied_set]

    return specs


__all__ = [
    "TOOL_SEARCH_TOOL_ID",
    "ToolRegistry",
    "ToolSpec",
    "filter_specs",
    "is_always_load",
    "is_deferred",
    "load_registry",
]
