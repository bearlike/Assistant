#!/usr/bin/env python3
"""Central JSON configuration for Mewbo."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

_APP_CONFIG_PATH_OVERRIDE: Path | None = None
_MCP_CONFIG_PATH_OVERRIDE: Path | None = None
_MCP_CONFIG_DISABLED = False
_APP_CONFIG_OVERRIDE: dict[str, Any] = {}
_CONFIG_CACHE: AppConfig | None = None
_CONFIG_WARNED = False
_LAST_PREFLIGHT: dict[str, dict[str, Any]] | None = None
_logger = logging.getLogger("core.config")

_PACKAGE_NAMES = ("mewbo-core", "mewbo-workspace")


def resolve_mewbo_home() -> Path:
    """Return the Mewbo home directory (``$MEWBO_HOME`` or ``~/.mewbo``)."""
    env = os.environ.get("MEWBO_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".mewbo"


def _resolve_config_path(filename: str) -> Path:
    """Find a config file: ``CWD/configs/`` first, then ``MEWBO_HOME``."""
    cwd_path = Path("configs") / filename
    if cwd_path.exists():
        return cwd_path
    return resolve_mewbo_home() / filename


def get_version() -> str:
    """Return the package version from pyproject.toml (via importlib.metadata).

    Tries ``mewbo-core`` first (always installed), then the workspace
    package (only available in local dev with ``uv sync``).
    """
    for name in _PACKAGE_NAMES:
        try:
            return _pkg_version(name)
        except PackageNotFoundError:
            continue
    return "0.0.0"


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, int | float):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _coerce_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        return [entry.strip() for entry in raw.split(",") if entry.strip()]
    return []


class RuntimeConfig(BaseModel):
    """Runtime environment settings."""

    model_config = ConfigDict(validate_default=True)

    envmode: str = Field("dev", description="Environment mode (e.g. dev, prod).", examples=["dev"])
    log_level: str = Field(
        "DEBUG",
        description="Logging verbosity. One of DEBUG, INFO, WARNING, ERROR, CRITICAL.",
        examples=["INFO"],
    )
    log_style: str = Field(
        "", description="Log output style for the core engine (empty for default).", examples=[""]
    )
    cli_log_style: str = Field(
        "dark", description="Rich console log theme for the CLI (dark or light).", examples=["dark"]
    )
    preflight_enabled: bool = Field(
        False,
        description=("Run connectivity checks for LLM, Langfuse, and Home Assistant on startup."),
    )
    cache_dir: str = Field(
        "",
        description="Directory for tool caches. Defaults to $MEWBO_HOME/cache.",
        examples=["~/.mewbo/cache"],
        json_schema_extra={"x-protected": True},
    )
    session_dir: str = Field(
        "",
        description="Directory for session transcripts. Defaults to $MEWBO_HOME/sessions.",
        examples=["~/.mewbo/sessions"],
        json_schema_extra={"x-protected": True},
    )
    config_dir: str = Field(
        "",
        description="Root configuration directory. Defaults to $MEWBO_HOME.",
        examples=["~/.mewbo"],
        json_schema_extra={"x-protected": True},
    )
    result_export_dir: str = Field(
        "",
        description="Directory for large tool result exports. Empty to disable.",
        examples=["/tmp/mewbo-results"],
    )
    projects_home: str = Field(
        "",
        description="Directory for virtual project folders. Defaults to $MEWBO_HOME/projects.",
        examples=["~/.mewbo/projects"],
        json_schema_extra={"x-protected": True},
    )

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: Any) -> str:
        if not value:
            return "DEBUG"
        return str(value).strip().upper()

    @field_validator("cache_dir", "session_dir", "config_dir", mode="before")
    @classmethod
    def _normalize_paths(cls, value: Any, info: ValidationInfo) -> str:
        raw = str(value).strip() if value is not None else ""
        if raw:
            return raw
        home = resolve_mewbo_home()
        defaults = {
            "cache_dir": str(home / "cache"),
            "session_dir": str(home / "sessions"),
            "config_dir": str(home),
        }
        return defaults.get(info.field_name or "", str(home))

    @field_validator("projects_home", mode="before")
    @classmethod
    def _normalize_projects_home(cls, value: Any) -> str:
        raw = str(value).strip() if value is not None else ""
        if raw:
            return str(Path(raw).expanduser())
        return str(resolve_mewbo_home() / "projects")

    @field_validator("preflight_enabled", mode="before")
    @classmethod
    def _normalize_preflight_enabled(cls, value: Any) -> bool:
        return _coerce_bool(value, default=False)


class LLMConfig(BaseModel):
    """LLM provider connection and model selection."""

    model_config = ConfigDict(validate_default=True)

    api_base: str = Field(
        "",
        description=(
            "Optional base URL override. Leave empty for direct "
            "provider access (LiteLLM routes automatically from "
            "the model prefix). Set only when using a proxy "
            "(e.g. LiteLLM, Bifrost)."
        ),
        examples=["", "https://my-litellm-proxy.example.com/v1"],
    )
    api_key: str = Field(
        "",
        description=("API key for the LLM provider (e.g. Anthropic, OpenAI) or proxy master key."),
        examples=["sk-ant-xxxxxxxx"],
        json_schema_extra={"x-protected": True},
    )
    default_model: str = Field(
        "gpt-5.2",
        description=(
            "Model ID using 'provider/model' syntax. LiteLLM "
            "auto-routes to the right API endpoint. When using "
            "a proxy, adjust the prefix to match its routing."
        ),
        examples=["anthropic/claude-sonnet-4-6"],
    )
    action_plan_model: str = Field(
        "",
        description=(
            "Model ID for action-plan generation. Falls back to default_model when empty."
        ),
        examples=["anthropic/claude-sonnet-4-6"],
    )
    tool_model: str = Field(
        "",
        description=("Model ID used by individual tools. Falls back to default_model when empty."),
        examples=["anthropic/claude-sonnet-4-6"],
    )
    title_model: str = Field(
        "",
        description=(
            "Model ID for session-title generation. Falls back to default_model when empty."
        ),
        examples=["anthropic/claude-haiku-4-5-20251001"],
    )
    compact_models: list[str] = Field(
        default_factory=lambda: ["default"],
        description=(
            "Priority-ordered list of models for context compaction. "
            "On failure, the next model in the list is tried. "
            'The keyword "default" resolves to the running agent\'s model. '
            'Example: ["anthropic/claude-haiku-4-5-20251001", "default"]'
        ),
        examples=[["anthropic/claude-haiku-4-5-20251001", "default"]],
    )
    fallback_models: list[str] = Field(
        default_factory=list,
        description=(
            "Ordered list of fallback model IDs. On retryable LLM failure, "
            "the system tries each in order after exhausting retries on "
            "the primary model. Empty = no fallback."
        ),
        examples=[["gpt-5.4", "gemini-2.5-pro"]],
    )
    proxy_model_prefix: str = Field(
        "openai",
        description=(
            "LiteLLM provider prefix prepended to model names when api_base is set. "
            "LiteLLM strips this prefix before forwarding the model name to the proxy, "
            "so the proxy receives the model ID it advertises in /v1/models. "
            "Leave as 'openai' for LiteLLM proxy, Bifrost, and OpenRouter. "
            "Only relevant when api_base is configured."
        ),
        examples=["openai", "azure", "vertex_ai"],
    )
    reasoning_effort: str = Field(
        "",
        description=(
            "Reasoning effort hint for supported models. One of low, medium, high, none, or empty."
        ),
        examples=["medium"],
    )
    reasoning_effort_models: list[str] = Field(
        default_factory=list,
        description="Model name patterns that support the reasoning_effort parameter.",
    )
    structured_patch_models: list[str] = Field(
        default_factory=list,
        description=(
            "Model IDs (or glob prefixes ending in '*') that prefer the "
            "structured_patch edit tool over search_replace_block. "
            "Built-in defaults cover GPT-5/o3/o4/Codex/GPT-4; "
            "only set this to override or extend."
        ),
    )

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _normalize_reasoning_effort(cls, value: Any) -> str:
        if value is None:
            return ""
        normalized = str(value).strip().lower()
        if normalized in {"low", "medium", "high", "none"}:
            return normalized
        return ""

    @field_validator("reasoning_effort_models", mode="before")
    @classmethod
    def _normalize_reasoning_effort_models(cls, value: Any) -> list[str]:
        return [entry.lower() for entry in _coerce_list(value)]

    @field_validator("structured_patch_models", mode="before")
    @classmethod
    def _normalize_structured_patch_models(cls, value: Any) -> list[str]:
        return [entry.lower() for entry in _coerce_list(value)]

    @field_validator("proxy_model_prefix", mode="before")
    @classmethod
    def _normalize_proxy_model_prefix(cls, value: Any) -> str:
        normalized = str(value).strip().strip("/") if value is not None else ""
        return normalized or "openai"

    def _resolve_api_base(self) -> str | None:
        base = self.api_base.strip()
        return base or None

    def _models_endpoint(self) -> str:
        base = self._resolve_api_base()
        if not base:
            raise ValueError("llm.api_base is not set.")
        base = base.rstrip("/")
        if base.endswith("/v1"):
            return f"{base}/models"
        return f"{base}/v1/models"

    def list_models(self, *, timeout: float = 8.0) -> list[str]:
        api_key = self.api_key.strip()
        if not api_key:
            raise ValueError("llm.api_key is not set.")
        request = Request(
            self._models_endpoint(),
            headers={"Authorization": f"Bearer {api_key}"},
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise ValueError(f"Model listing failed: HTTP {exc.code}") from exc
        except URLError as exc:
            raise ValueError(f"Model listing failed: {exc.reason}") from exc
        data = payload.get("data", [])
        return sorted([item.get("id") for item in data if item.get("id")])

    def validate_models(self) -> ConfigCheck:
        if not self._resolve_api_base():
            return ConfigCheck(
                name="llm",
                enabled=True,
                ok=True,
                reason="api_base not set; using direct provider routing",
            )
        if not self.api_key.strip():
            return ConfigCheck(
                name="llm",
                enabled=True,
                ok=False,
                reason="llm.api_key is not set",
            )
        try:
            models = self.list_models()
        except ValueError as exc:
            return ConfigCheck(name="llm", enabled=True, ok=False, reason=str(exc))
        missing: list[str] = []
        compact_explicit = {m for m in self.compact_models if m and m != "default"}
        for model_name in {
            self.default_model,
            self.action_plan_model,
            self.tool_model,
            self.title_model,
            *compact_explicit,
        }:
            if model_name and model_name not in models:
                missing.append(model_name)
        if missing:
            return ConfigCheck(
                name="llm",
                enabled=True,
                ok=False,
                reason="Configured model not found in API",
                metadata={"missing_models": missing, "available_models": models},
            )
        return ConfigCheck(name="llm", enabled=True, ok=True, metadata={"available_models": models})


class ContextConfig(BaseModel):
    """Context window selection and event filtering."""

    model_config = ConfigDict(validate_default=True)

    recent_event_limit: int = Field(
        8,
        description="Maximum number of recent events injected into the context window.",
        examples=[8],
    )
    selection_threshold: float = Field(
        0.8,
        description=(
            "Relevance score threshold (0.0-1.0) for the context selector to keep an event."
        ),
        examples=[0.8],
    )
    selection_enabled: bool = Field(
        True,
        description=(
            "Enable LLM-based context event selection. When false, all recent events are used."
        ),
        examples=[True],
    )
    context_selector_model: str = Field(
        "",
        description=("Model ID for context selection. Falls back to llm.default_model when empty."),
        examples=["anthropic/claude-sonnet-4-6"],
    )

    @field_validator("recent_event_limit", mode="before")
    @classmethod
    def _normalize_recent_event_limit(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 8
        return max(parsed, 1)

    @field_validator("selection_threshold", mode="before")
    @classmethod
    def _normalize_selection_threshold(cls, value: Any) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return 0.8
        return min(max(parsed, 0.0), 1.0)

    @field_validator("selection_enabled", mode="before")
    @classmethod
    def _normalize_selection_enabled(cls, value: Any) -> bool:
        return _coerce_bool(value, default=True)


class TokenBudgetConfig(BaseModel):
    """Token budget and auto-compaction thresholds."""

    model_config = ConfigDict(validate_default=True)

    default_context_window: int = Field(
        128000,
        description=(
            "Default context window size in tokens used when the "
            "model is not listed in model_context_windows."
        ),
        examples=[128000],
    )
    auto_compact_threshold: float = Field(
        0.8,
        description=(
            "Fraction of the context window (0.0-1.0) that triggers "
            "automatic conversation compaction."
        ),
        examples=[0.8],
    )
    model_context_windows: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Override only: per-model context window in tokens. Keys are model "
            "names (with or without provider prefix). The authoritative source "
            "is LiteLLM's model catalogue; populate this only to cap below the "
            "model's real max, or for models LiteLLM doesn't know yet."
        ),
    )

    @field_validator("default_context_window", mode="before")
    @classmethod
    def _normalize_context_window(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 128000
        return max(parsed, 1)

    @field_validator("auto_compact_threshold", mode="before")
    @classmethod
    def _normalize_compact_threshold(cls, value: Any) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return 0.8
        return min(max(parsed, 0.0), 1.0)

    @field_validator("model_context_windows", mode="before")
    @classmethod
    def _normalize_model_context_windows(cls, value: Any) -> dict[str, int]:
        if not isinstance(value, dict):
            return {}
        cleaned: dict[str, int] = {}
        for key, raw in value.items():
            try:
                cleaned[str(key)] = max(int(raw), 1)
            except (TypeError, ValueError):
                continue
        return cleaned


class CompactionConfig(BaseModel):
    """Summarization prompt selection for conversation compaction.

    ``caveman_mode`` enables a rule-augmented prompt (inspired by the
    ``JuliusBrussee/caveman`` Claude Code skill) that instructs the
    summarizer LLM to drop articles, filler, pleasantries, and hedging
    while preserving code, paths, URLs, and error strings verbatim.
    Reduces output tokens in the compaction summary without changing the
    ``<analysis>/<summary>`` response structure downstream parsers expect.
    """

    model_config = ConfigDict(validate_default=True)

    caveman_mode: bool = Field(
        False,
        description=(
            "Enable caveman-style terse summarization prompt. Drops articles, "
            "filler, pleasantries, and hedging in the compacted summary while "
            "preserving code, file paths, URLs, and error strings verbatim. "
            "Reduces compaction output tokens without changing the response "
            "structure downstream parsers expect."
        ),
        examples=[False],
    )

    @field_validator("caveman_mode", mode="before")
    @classmethod
    def _normalize_caveman_mode(cls, value: Any) -> bool:
        return _coerce_bool(value, default=False)


class ReflectionConfig(BaseModel):
    """Post-execution reflection pass settings."""

    model_config = ConfigDict(validate_default=True)

    enabled: bool = Field(
        True, description="Enable a reflection LLM pass after tool execution to verify results."
    )
    model: str = Field(
        "",
        description=(
            "Model ID for the reflection pass. Falls back to llm.default_model when empty."
        ),
        examples=["anthropic/claude-sonnet-4-6"],
    )

    @field_validator("enabled", mode="before")
    @classmethod
    def _normalize_enabled(cls, value: Any) -> bool:
        return _coerce_bool(value, default=True)


class LangfuseConfig(BaseModel):
    """Langfuse LLM observability integration."""

    model_config = ConfigDict(validate_default=True)

    enabled: bool = Field(False, description="Enable Langfuse tracing for all LLM calls.")
    host: str = Field(
        "",
        description="Langfuse server URL.",
        examples=["https://langfuse.server.local"],
    )
    project_id: str = Field(
        "",
        description="Langfuse project ID for constructing dashboard URLs.",
        examples=["clvh22gis002oru6ay1rm2eh0"],
    )
    public_key: str = Field(
        "",
        description="Langfuse project public key.",
        examples=["pk-lf-xxxxxxxxxxxxxxxx"],
        json_schema_extra={"x-protected": True},
    )
    secret_key: str = Field(
        "",
        description="Langfuse project secret key.",
        examples=["sk-lf-xxxxxxxxxxxxxxxx"],
        json_schema_extra={"x-protected": True},
    )

    @field_validator("enabled", mode="before")
    @classmethod
    def _normalize_enabled(cls, value: Any) -> bool:
        return _coerce_bool(value, default=False)

    def evaluate(self) -> tuple[bool, str | None, dict[str, Any]]:
        if not self.enabled:
            return False, "disabled via config", {}
        missing: list[str] = []
        if not self.public_key:
            missing.append("langfuse.public_key")
        if not self.secret_key:
            missing.append("langfuse.secret_key")
        if missing:
            return (
                False,
                "missing langfuse.public_key/langfuse.secret_key",
                {"required_config": missing},
            )
        try:
            from langfuse.langchain import CallbackHandler  # noqa: F401
        except ModuleNotFoundError as exc:
            message = str(exc).lower()
            if "langchain" in message:
                return False, "langchain not installed", {}
            return False, "langfuse not installed", {}
        return True, None, {}


class HomeAssistantConfig(BaseModel):
    """Home Assistant smart-home integration."""

    model_config = ConfigDict(validate_default=True)

    enabled: bool = Field(
        False,
        description=("Enable the Home Assistant tool for smart-home control."),
    )
    url: str = Field(
        "",
        description="Home Assistant API base URL.",
        examples=["http://homeassistant.local:8123"],
    )
    token: str = Field(
        "",
        description="Long-lived access token for Home Assistant authentication.",
        examples=["ha_token_here"],
        json_schema_extra={"x-protected": True},
    )

    @field_validator("enabled", mode="before")
    @classmethod
    def _normalize_enabled(cls, value: Any) -> bool:
        return _coerce_bool(value, default=False)

    def evaluate(self) -> tuple[bool, str | None, dict[str, Any]]:
        if not self.enabled:
            return False, "disabled via config", {}
        missing: list[str] = []
        if not self.url:
            missing.append("home_assistant.url")
        if not self.token:
            missing.append("home_assistant.token")
        if missing:
            return (
                False,
                "missing home_assistant.url/home_assistant.token",
                {"required_config": missing},
            )
        return True, None, {}


class PermissionsConfig(BaseModel):
    """Tool execution permission policy."""

    model_config = ConfigDict(validate_default=True)

    policy_path: str = Field(
        "",
        description="Path to a JSON or TOML permission policy file. Empty uses built-in defaults.",
        examples=["./configs/policy.json"],
    )
    approval_mode: str = Field(
        "ask",
        description=(
            "Default approval mode: 'ask' prompts the user, 'allow' auto-approves, 'deny' blocks."
        ),
        examples=["ask"],
    )

    @field_validator("approval_mode", mode="before")
    @classmethod
    def _normalize_approval_mode(cls, value: Any) -> str:
        if value is None:
            return "ask"
        normalized = str(value).strip().lower()
        if normalized in {"allow", "auto", "approve", "yes"}:
            return "allow"
        if normalized in {"deny", "never", "no"}:
            return "deny"
        return "ask"


class CLIConfig(BaseModel):
    """Terminal CLI display and interaction settings."""

    model_config = ConfigDict(validate_default=True)

    disable_textual: bool = Field(
        False,
        description="Disable the Textual TUI and fall back to plain Rich output.",
    )
    approval_style: str = Field(
        "inline",
        description=(
            "Tool-approval UI style: 'inline' (plain prompt), "
            "'textual' (TUI dialog), or 'aider' (diff-style)."
        ),
        examples=["aider"],
    )

    @field_validator("disable_textual", mode="before")
    @classmethod
    def _normalize_disable_textual(cls, value: Any) -> bool:
        return _coerce_bool(value, default=False)

    @field_validator("approval_style", mode="before")
    @classmethod
    def _normalize_approval_style(cls, value: Any) -> str:
        if value is None:
            return "inline"
        normalized = str(value).strip().lower()
        if normalized in {"inline", "textual", "aider"}:
            return normalized
        return "inline"


class ChatConfig(BaseModel):
    """Legacy config section kept for backward compatibility with app.json files."""

    model_config = ConfigDict(validate_default=True)

    port: int = Field(
        8501,
        description="TCP port for the legacy chat interface.",
        examples=[8501],
    )
    address: str = Field(
        "127.0.0.1",
        description="Bind address for the legacy chat interface.",
        examples=["127.0.0.1"],
    )

    @field_validator("port", mode="before")
    @classmethod
    def _normalize_port(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 8501
        return max(parsed, 1)


class APIConfig(BaseModel):
    """REST API authentication."""

    master_token: str = Field(
        "msk-strong-password",
        description=(
            "Bearer token required for all REST API requests. "
            "Change from the default before deploying."
        ),
        examples=["msk-strong-password"],
        json_schema_extra={"x-protected": True},
    )


class HookEntry(BaseModel):
    """A single hook configuration entry."""

    type: str = Field(
        "command", description="Hook type: 'command' (shell) or 'http' (POST to URL)."
    )
    command: str = Field("", description="Shell command to execute (type=command).")
    url: str = Field("", description="Target URL for HTTP POST (type=http).")
    headers: dict[str, str] = Field(
        default_factory=dict, description="Extra HTTP headers (type=http)."
    )
    matcher: str | None = Field(
        None, description="Optional fnmatch pattern to limit which tool IDs trigger this hook."
    )
    timeout: int = Field(30, description="Maximum seconds to wait for the hook to finish.")

    @model_validator(mode="after")
    def _validate_type_fields(self) -> HookEntry:
        if self.type == "http" and not self.url:
            msg = "HookEntry type='http' requires a non-empty 'url'."
            raise ValueError(msg)
        # Allow default empty HookEntry() for schema generation.
        if self.type == "command" and not self.command and self.url:
            msg = "HookEntry type='command' but only 'url' is set; use type='http'."
            raise ValueError(msg)
        return self


class HooksConfig(BaseModel):
    """External shell hooks fired during the session lifecycle."""

    pre_tool_use: list[HookEntry] = Field(
        default_factory=list, description="Hooks executed before each tool invocation."
    )
    post_tool_use: list[HookEntry] = Field(
        default_factory=list, description="Hooks executed after each tool invocation."
    )
    on_session_start: list[HookEntry] = Field(
        default_factory=list, description="Hooks executed when a new session begins."
    )
    on_session_end: list[HookEntry] = Field(
        default_factory=list, description="Hooks executed when a session ends."
    )


class PluginsConfig(BaseModel):
    """Plugin system configuration."""

    model_config = ConfigDict(validate_default=True)

    enabled: bool = Field(True, description="Enable the plugin system.")
    enabled_plugins: list[str] = Field(
        default_factory=list,
        description=(
            "Plugin names to enable. Empty = all installed plugins. "
            "Format: 'plugin-name' or 'plugin-name@marketplace'."
        ),
    )
    marketplaces: list[str] = Field(
        default_factory=lambda: ["anthropics/claude-plugins-official"],
        description="GitHub repos containing marketplace.json plugin indexes.",
    )
    install_path: str = Field(
        "",
        description=(
            "Override install path for Mewbo-managed plugins. "
            "Defaults to $MEWBO_HOME/plugins/ (via resolve_mewbo_home)."
        ),
    )

    @field_validator("enabled", mode="before")
    @classmethod
    def _normalize_enabled(cls, value: Any) -> bool:
        return _coerce_bool(value, default=True)

    @field_validator("enabled_plugins", "marketplaces", mode="before")
    @classmethod
    def _normalize_string_lists(cls, value: Any) -> list[str]:
        return _coerce_list(value)

    @field_validator("install_path", mode="before")
    @classmethod
    def _normalize_install_path(cls, value: Any) -> str:
        raw = str(value).strip() if value else ""
        if raw:
            return str(Path(raw).expanduser().resolve())
        return ""

    def resolve_install_dir(self) -> Path:
        """Uses install_path if set, otherwise resolve_mewbo_home() / 'plugins'."""
        if self.install_path:
            return Path(self.install_path)
        return resolve_mewbo_home() / "plugins"

    def resolve_registry_paths(self) -> list[Path]:
        """Paths to search for installed_plugins.json: CC cache + our own."""
        paths = [
            Path.home() / ".claude" / "plugins" / "installed_plugins.json",
            self.resolve_install_dir() / "installed_plugins.json",
        ]
        return [p for p in paths if p.parent.is_dir()]

    def resolve_marketplace_dirs(self, *, sync: bool = True) -> list[Path]:
        """Paths to search for marketplace.json caches.

        Scans both Claude Code's and our own marketplace directories.
        When *sync* is True and ``self.marketplaces`` lists repos that aren't
        yet cloned locally, ``sync_marketplaces`` clones them first.
        """
        dirs: list[Path] = []
        # 1. Check Claude Code's cache (read-only)
        cc_base = Path.home() / ".claude" / "plugins" / "marketplaces"
        if cc_base.is_dir():
            dirs.extend(sorted(d for d in cc_base.iterdir() if d.is_dir()))

        # 2. Check our own cache
        own_base = self.resolve_install_dir() / "marketplaces"
        if own_base.is_dir():
            dirs.extend(sorted(d for d in own_base.iterdir() if d.is_dir()))

        # 3. If nothing found and we have marketplace repos configured, sync them
        if sync and not dirs and self.marketplaces:
            from mewbo_core.plugins import sync_marketplaces

            synced = sync_marketplaces(self.marketplaces, self.resolve_install_dir())
            dirs.extend(synced)
        elif sync and self.marketplaces:
            # Even with existing dirs, ensure all configured repos are cloned
            existing_names = {d.name for d in dirs}
            missing = [
                r
                for r in self.marketplaces
                if (r.split("/")[-1] if "/" in r else r) not in existing_names
            ]
            if missing:
                from mewbo_core.plugins import sync_marketplaces

                synced = sync_marketplaces(missing, self.resolve_install_dir())
                dirs.extend(synced)

        return dirs


class ProjectConfig(BaseModel):
    """A project directory exposed to the REST API for session scoping."""

    model_config = ConfigDict(validate_default=True)

    path: str = Field("", description="Absolute path to the project root. Tilde (~) is expanded.")
    description: str = Field("", description="Short human-readable description of the project.")

    @field_validator("path", mode="before")
    @classmethod
    def _normalize_path(cls, value: Any) -> str:
        raw = str(value).strip() if value else ""
        if raw:
            return str(Path(raw).expanduser().resolve())
        return ""


def _projects_config_default() -> dict[str, ProjectConfig]:
    return {}


class WebIdeConfig(BaseModel):
    """Config for the per-session code-server "Open in Web IDE" feature."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    image: str = "codercom/code-server:latest"
    default_lifetime_hours: int = Field(default=1, ge=1, le=24)
    max_lifetime_hours: int = Field(default=8, ge=1, le=168)
    cpus: float = Field(default=1.0, ge=0.1, le=16.0)
    memory: str = Field(default="1g", pattern=r"^\d+[mgMG]$")
    pids_limit: int = Field(default=512, ge=64, le=4096)
    network: str = Field(default="mewbo-ide", pattern=r"^[a-zA-Z0-9_-]+$")
    state_dir: str = Field(default="/tmp/mewbo-ide")


class LSPConfig(BaseModel):
    """Language Server Protocol integration settings."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(True, description="Enable native LSP tool.")
    servers: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description=(
            "Override or extend built-in server definitions. "
            'Set {"pyright": {"disabled": true}} to disable a built-in, '
            "or add custom servers with command/extensions/root_markers."
        ),
    )


class ToolSearchConfig(BaseModel):
    """Deferred tool loading via on-demand schema fetching.

    When ``mode='on'``, MCP tool schemas (and any spec with
    ``metadata.deferred=True``) are stripped from the initial ``bind_tools``
    call and surfaced to the model by name only via
    ``<available-deferred-tools>``. The model fetches schemas it actually
    needs by calling the built-in ``tool_search`` tool. Mirrors Claude
    Code's ``ToolSearchTool`` mechanism — saves substantial context tokens
    on sessions with many MCP servers connected.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["off", "on"] = Field(
        "off",
        description=(
            "'off' (default) keeps every tool's schema in the initial bind. "
            "'on' defers MCP tools and any spec with metadata.deferred=True; "
            "the model loads schemas on demand via tool_search."
        ),
    )


class AgentConfig(BaseModel):
    """Sub-agent hypervisor settings."""

    model_config = ConfigDict(validate_default=True)

    enabled: bool = Field(True, description="Enable the sub-agent spawning system.")
    max_depth: int = Field(
        5, description="Maximum nesting depth for sub-agent delegation (1 = no sub-agents)."
    )
    max_concurrent: int = Field(
        20, description="Maximum number of sub-agents allowed to run concurrently."
    )
    default_sub_model: str = Field(
        "",
        description=(
            "Default LLM model for sub-agents. Falls back to the root agent's model when empty."
        ),
        examples=["anthropic/claude-haiku-4-5"],
    )
    allowed_models: list[str] = Field(
        default_factory=list,
        description=(
            "Allowlist of model names sub-agents may use. Empty means all models are allowed."
        ),
    )
    max_iters: int = Field(
        30,
        description=(
            "Deprecated. The tool-use loop now runs until natural completion "
            "(model returns text without tool calls). This field is retained "
            "for API backward compatibility but is not enforced."
        ),
    )
    sub_agent_max_steps: int = Field(
        10,
        description=(
            "Deprecated. Sub-agents now run until natural completion. "
            "This field is retained for API backward compatibility but "
            "is not enforced. Safety is provided by session_step_budget, "
            "stall detection, and LLM timeouts."
        ),
    )
    llm_call_timeout: float = Field(
        60.0,
        description=(
            "Ceiling in seconds for a single model.ainvoke() call. "
            "Covers extended-thinking models. On timeout, the call is "
            "retried up to llm_call_retries times before cascading to "
            "fallback models."
        ),
    )
    llm_call_retries: int = Field(
        2,
        description=(
            "Maximum retry attempts for the primary model before "
            "cascading to fallback_models. Each fallback model gets "
            "one attempt."
        ),
    )
    default_denied_tools: list[str] = Field(
        default_factory=list,
        description="Tool IDs denied to all sub-agents by default (e.g. spawn_agent).",
    )
    edit_tool: str = Field(
        "",
        description=(
            "File editing mechanism override: 'search_replace_block' (Aider-style "
            "SEARCH/REPLACE blocks) or 'structured_patch' (per-file exact "
            "string replacement). Leave empty (default) to auto-select based on "
            "the active model via llm.structured_patch_models."
        ),
        examples=["", "search_replace_block", "structured_patch"],
    )
    plan_mode_shell_allowlist: list[str] = Field(
        default_factory=lambda: [
            # Filesystem inspection
            "ls",
            "pwd",
            "cat",
            "head",
            "tail",
            "wc",
            "file",
            "stat",
            "tree",
            # Searching
            "find",
            "grep",
            "rg",
            "ag",
            "ack",
            # Environment / process / system introspection
            "echo",
            "which",
            "whereis",
            "env",
            "printenv",
            "ps",
            "uname",
            "date",
            # Disk usage
            "du",
            "df",
            # Git read-only subcommands (prefix-matched; all flags/args allowed)
            "git status",
            "git log",
            "git diff",
            "git show",
            "git blame",
            "git branch",
            "git tag",
            "git remote",
            "git config --get",
            "git rev-parse",
            "git ls-files",
            "git describe",
            "git reflog",
        ],
        description=(
            "Shell command prefixes allowed during plan mode. Each entry "
            "matches a command at a word boundary (e.g. 'git log' matches "
            "'git log --oneline' but not 'git logger'). Commands containing "
            "pipes, redirects, variable expansion, command substitution, or "
            "chaining (|, >, <, &, ;, $, backtick) are always rejected. "
            "Set to an empty list to disable shell in plan mode entirely."
        ),
    )
    plan_mode_allow_mcp: bool = Field(
        True,
        description=(
            "Allow ALL user-enabled MCP tools (tools with kind='mcp') during "
            "plan mode. Matches Claude Code's permissive default and trusts "
            "the user's mcp.json configuration. Set to false to block MCP "
            "tools in plan mode regardless of their read-only status."
        ),
    )
    web_ide: WebIdeConfig | None = Field(
        default=None,
        description="Optional 'Open in Web IDE' feature config (code-server containers).",
    )
    lsp: LSPConfig = Field(
        default_factory=lambda: LSPConfig.model_validate({}),
        description="Language Server Protocol integration settings.",
    )
    tool_search: ToolSearchConfig = Field(
        default_factory=lambda: ToolSearchConfig.model_validate({}),
        description="Deferred tool loading via on-demand schema fetching.",
    )

    @field_validator("edit_tool", mode="before")
    @classmethod
    def _normalize_edit_tool(cls, value: Any) -> str:
        if value is None:
            return ""
        normalized = str(value).strip().lower()
        if normalized in {"", "search_replace_block", "structured_patch"}:
            return normalized
        return ""

    @field_validator("enabled", mode="before")
    @classmethod
    def _normalize_enabled(cls, value: Any) -> bool:
        return _coerce_bool(value, default=True)

    @field_validator("max_depth", mode="before")
    @classmethod
    def _normalize_max_depth(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 5
        return max(parsed, 1)

    @field_validator("max_concurrent", mode="before")
    @classmethod
    def _normalize_max_concurrent(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 20
        return max(parsed, 1)

    @field_validator("max_iters", mode="before")
    @classmethod
    def _normalize_max_iters(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 30
        return max(parsed, 1)

    @field_validator("sub_agent_max_steps", mode="before")
    @classmethod
    def _normalize_sub_agent_max_steps(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 10
        return max(parsed, 1)

    @field_validator(
        "allowed_models",
        "default_denied_tools",
        "plan_mode_shell_allowlist",
        mode="before",
    )
    @classmethod
    def _normalize_string_lists(cls, value: Any) -> list[str]:
        return _coerce_list(value)

    @field_validator("plan_mode_allow_mcp", mode="before")
    @classmethod
    def _normalize_plan_mode_allow_mcp(cls, value: Any) -> bool:
        return _coerce_bool(value, default=True)


class MongoDBConfig(BaseModel):
    """MongoDB connection settings."""

    uri: str = Field(
        "mongodb://localhost:27017",
        description="MongoDB connection URI (includes host, port, credentials).",
        examples=["mongodb://user:pass@localhost:27017/mewbo?authSource=admin"],
    )
    database: str = Field(
        "mewbo",
        description="MongoDB database name for session storage.",
        examples=["mewbo"],
    )

    @field_validator("uri", mode="before")
    @classmethod
    def _normalize_uri(cls, value: Any) -> str:
        env = os.environ.get("MEWBO_MONGODB_URI")
        if env:
            return env
        return str(value).strip() if value else "mongodb://localhost:27017"

    @field_validator("database", mode="before")
    @classmethod
    def _normalize_database(cls, value: Any) -> str:
        env = os.environ.get("MEWBO_MONGODB_DATABASE")
        if env:
            return env
        return str(value).strip() if value else "mewbo"


class StorageConfig(BaseModel):
    """Session storage backend configuration."""

    model_config = ConfigDict(validate_default=True)

    driver: str = Field(
        "json",
        description="Storage driver: 'json' (filesystem) or 'mongodb'.",
        examples=["json", "mongodb"],
    )
    mongodb: MongoDBConfig = Field(
        default_factory=lambda: MongoDBConfig.model_validate({}),
        description="MongoDB connection settings (used when driver is 'mongodb').",
    )

    @field_validator("driver", mode="before")
    @classmethod
    def _normalize_driver(cls, value: Any) -> str:
        env = os.environ.get("MEWBO_STORAGE_DRIVER")
        if env:
            value = env
        raw = str(value).strip().lower() if value else "json"
        if raw not in {"json", "mongodb"}:
            raise ValueError(f"Unknown storage driver {raw!r}. Expected 'json' or 'mongodb'.")
        return raw


def _storage_config_default() -> StorageConfig:
    return StorageConfig.model_validate({})


def _runtime_config_default() -> RuntimeConfig:
    return RuntimeConfig.model_validate({})


def _llm_config_default() -> LLMConfig:
    return LLMConfig.model_validate({})


def _context_config_default() -> ContextConfig:
    return ContextConfig.model_validate({})


def _token_budget_config_default() -> TokenBudgetConfig:
    return TokenBudgetConfig.model_validate({})


def _compaction_config_default() -> CompactionConfig:
    return CompactionConfig.model_validate({})


def _reflection_config_default() -> ReflectionConfig:
    return ReflectionConfig.model_validate({})


def _langfuse_config_default() -> LangfuseConfig:
    return LangfuseConfig.model_validate({})


def _home_assistant_config_default() -> HomeAssistantConfig:
    return HomeAssistantConfig.model_validate({})


def _permissions_config_default() -> PermissionsConfig:
    return PermissionsConfig.model_validate({})


def _cli_config_default() -> CLIConfig:
    return CLIConfig.model_validate({})


def _chat_config_default() -> ChatConfig:
    return ChatConfig.model_validate({})


def _api_config_default() -> APIConfig:
    return APIConfig.model_validate({})


def _agent_config_default() -> AgentConfig:
    return AgentConfig.model_validate({})


def _hooks_config_default() -> HooksConfig:
    return HooksConfig.model_validate({})


def _plugins_config_default() -> PluginsConfig:
    return PluginsConfig.model_validate({})


class AppConfig(BaseModel):
    """Typed configuration for the Mewbo runtime."""

    model_config = ConfigDict(extra="ignore", validate_default=True)

    runtime: RuntimeConfig = Field(
        default_factory=_runtime_config_default, description="Runtime environment settings."
    )
    storage: StorageConfig = Field(
        default_factory=_storage_config_default,
        description="Session storage backend (json or mongodb).",
    )
    llm: LLMConfig = Field(
        default_factory=_llm_config_default,
        description="LLM provider connection and model selection.",
    )
    context: ContextConfig = Field(
        default_factory=_context_config_default,
        description="Context window selection and event filtering.",
    )
    token_budget: TokenBudgetConfig = Field(
        default_factory=_token_budget_config_default,
        description="Token budget and auto-compaction thresholds.",
    )
    compaction: CompactionConfig = Field(
        default_factory=_compaction_config_default,
        description="Conversation compaction prompt selection (caveman mode).",
    )
    reflection: ReflectionConfig = Field(
        default_factory=_reflection_config_default,
        description="Post-execution reflection pass settings.",
    )
    langfuse: LangfuseConfig = Field(
        default_factory=_langfuse_config_default,
        description="Langfuse LLM observability integration.",
    )
    home_assistant: HomeAssistantConfig = Field(
        default_factory=_home_assistant_config_default,
        description="Home Assistant smart-home integration.",
    )
    permissions: PermissionsConfig = Field(
        default_factory=_permissions_config_default,
        description="Tool execution permission policy.",
    )
    cli: CLIConfig = Field(
        default_factory=_cli_config_default,
        description="Terminal CLI display and interaction settings.",
    )
    chat: ChatConfig = Field(
        default_factory=_chat_config_default,
        description="Legacy chat interface settings.",
    )
    api: APIConfig = Field(
        default_factory=_api_config_default, description="REST API authentication."
    )
    agent: AgentConfig = Field(
        default_factory=_agent_config_default, description="Sub-agent hypervisor settings."
    )
    hooks: HooksConfig = Field(
        default_factory=_hooks_config_default,
        description="External hooks fired during the session lifecycle (command or http).",
    )
    plugins: PluginsConfig = Field(
        default_factory=_plugins_config_default,
        description="Plugin system configuration.",
    )
    channels: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Chat platform channel adapters (nextcloud-talk, slack, etc.).",
    )
    projects: dict[str, ProjectConfig] = Field(
        default_factory=_projects_config_default,
        description="Named project directories exposed to the REST API for session scoping.",
    )

    @field_validator("projects", mode="before")
    @classmethod
    def _normalize_projects(cls, value: Any) -> dict[str, ProjectConfig]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, ProjectConfig] = {}
        for name, cfg in value.items():
            if isinstance(cfg, dict):
                result[str(name)] = ProjectConfig.model_validate(cfg)
            elif isinstance(cfg, ProjectConfig):
                result[str(name)] = cfg
        return result

    @classmethod
    def load(cls, path: str | Path) -> AppConfig:
        """Load configuration from a JSON file."""
        payload = _load_json(path)
        return cls.model_validate(payload)

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize config to JSON."""
        return self.model_dump_json(indent=indent, exclude_none=True)

    def write(self, path: str | Path, *, indent: int = 2) -> None:
        """Write config JSON to disk."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_json(indent=indent) + "\n", encoding="utf-8")

    async def preflight(self, *, disable_on_failure: bool = True) -> dict[str, dict[str, Any]]:
        """Run async validation checks for optional integrations."""
        results: dict[str, ConfigCheck] = {}

        async def _llm_check() -> ConfigCheck:
            return await asyncio.to_thread(self.llm.validate_models)

        async def _langfuse_check() -> ConfigCheck:
            enabled, reason, metadata = self.langfuse.evaluate()
            if not enabled:
                return ConfigCheck(
                    name="langfuse",
                    enabled=False,
                    ok=True,
                    reason=reason,
                    metadata=metadata,
                )
            try:
                host = self.langfuse.host.rstrip("/")
                if host:
                    await asyncio.to_thread(_probe_http, f"{host}/api/public/health")
                return ConfigCheck(name="langfuse", enabled=True, ok=True)
            except ValueError as exc:
                return ConfigCheck(name="langfuse", enabled=True, ok=False, reason=str(exc))

        async def _ha_check() -> ConfigCheck:
            enabled, reason, metadata = self.home_assistant.evaluate()
            if not enabled:
                return ConfigCheck(
                    name="home_assistant",
                    enabled=False,
                    ok=True,
                    reason=reason,
                    metadata=metadata,
                )
            try:
                url = self.home_assistant.url.rstrip("/")
                headers = {"Authorization": f"Bearer {self.home_assistant.token}"}
                await asyncio.to_thread(_probe_http, f"{url}/api/config", headers=headers)
                return ConfigCheck(name="home_assistant", enabled=True, ok=True)
            except ValueError as exc:
                return ConfigCheck(name="home_assistant", enabled=True, ok=False, reason=str(exc))

        async def _mcp_check() -> ConfigCheck:
            config_path = get_mcp_config_path()
            if not config_path:
                return ConfigCheck(name="mcp", enabled=False, ok=True, reason="mcp config disabled")
            try:
                from mewbo_tools.integration import mcp as mcp_module

                config = mcp_module._load_mcp_config(config_path)
                tools, failures = await asyncio.to_thread(
                    mcp_module.discover_mcp_tool_details_with_failures, config
                )
                if failures:
                    return ConfigCheck(
                        name="mcp",
                        enabled=True,
                        ok=False,
                        reason="mcp discovery failed",
                        metadata={"failures": {k: str(v) for k, v in failures.items()}},
                    )
                return ConfigCheck(
                    name="mcp",
                    enabled=True,
                    ok=True,
                    metadata={"servers": list(tools.keys())},
                )
            except Exception as exc:
                return ConfigCheck(name="mcp", enabled=True, ok=False, reason=str(exc))

        checks = await asyncio.gather(_llm_check(), _langfuse_check(), _ha_check(), _mcp_check())
        for check in checks:
            results[check.name] = check
        if disable_on_failure:
            langfuse_check = results.get("langfuse")
            if langfuse_check and not langfuse_check.ok and self.langfuse.enabled:
                self.langfuse.enabled = False
            ha_check = results.get("home_assistant")
            if ha_check and not ha_check.ok and self.home_assistant.enabled:
                self.home_assistant.enabled = False
        return {name: check.to_dict() for name, check in results.items()}


def _probe_http(url: str, headers: dict[str, str] | None = None) -> None:
    request = Request(url, headers=headers or {})
    try:
        with urlopen(request, timeout=6.0):
            return None
    except HTTPError as exc:
        raise ValueError(f"HTTP {exc.code} for {url}") from exc
    except URLError as exc:
        raise ValueError(f"Connection error for {url}: {exc.reason}") from exc


def start_preflight(
    config: AppConfig | None = None,
    *,
    disable_on_failure: bool = True,
    on_complete: Callable[[dict[str, dict[str, Any]]], None] | None = None,
) -> threading.Thread:
    """Run config preflight checks in a background thread."""
    target = config or get_config()

    def _runner() -> None:
        global _LAST_PREFLIGHT
        results = asyncio.run(target.preflight(disable_on_failure=disable_on_failure))
        _LAST_PREFLIGHT = results
        failures = {
            name: info
            for name, info in results.items()
            if info.get("enabled") and not info.get("ok")
        }
        for name, info in failures.items():
            reason = info.get("reason") or "unknown failure"
            _logger.warning("Preflight check failed for %s: %s", name, reason)
        if on_complete is not None:
            on_complete(results)

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    return thread


def get_last_preflight() -> dict[str, dict[str, Any]] | None:
    """Return the most recent preflight results if available."""
    return _LAST_PREFLIGHT


@dataclass
class ConfigCheck:
    """Result of a configuration preflight check."""

    name: str
    enabled: bool
    ok: bool
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the check result to a dictionary."""
        return {
            "name": self.name,
            "enabled": self.enabled,
            "ok": self.ok,
            "reason": self.reason,
            "metadata": self.metadata,
        }


def _load_json(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {}
    with target.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("Config payload must be a JSON object.")
    return payload


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _deep_merge(dict(base.get(key, {})), value)
        else:
            base[key] = value
    return base


def set_app_config_path(path: str | Path) -> None:
    """Override the app config path (tests only)."""
    global _APP_CONFIG_PATH_OVERRIDE, _CONFIG_CACHE
    _APP_CONFIG_PATH_OVERRIDE = Path(path)
    _CONFIG_CACHE = None


def set_mcp_config_path(path: str | Path | None) -> None:
    """Override the MCP config path (tests only)."""
    global _MCP_CONFIG_PATH_OVERRIDE, _MCP_CONFIG_DISABLED
    if path is None or str(path).strip() == "":
        _MCP_CONFIG_PATH_OVERRIDE = None
        _MCP_CONFIG_DISABLED = True
        return
    _MCP_CONFIG_DISABLED = False
    _MCP_CONFIG_PATH_OVERRIDE = Path(path)


def reset_config() -> None:
    """Clear cached configuration and overrides."""
    global _CONFIG_CACHE, _APP_CONFIG_OVERRIDE, _APP_CONFIG_PATH_OVERRIDE, _MCP_CONFIG_PATH_OVERRIDE
    global _MCP_CONFIG_DISABLED, _CONFIG_WARNED
    _CONFIG_CACHE = None
    _APP_CONFIG_OVERRIDE = {}
    _APP_CONFIG_PATH_OVERRIDE = None
    _MCP_CONFIG_PATH_OVERRIDE = None
    _MCP_CONFIG_DISABLED = False
    _CONFIG_WARNED = False


def set_config_override(payload: dict[str, Any], *, replace: bool = False) -> None:
    """Override config values in-memory (tests/CLI)."""
    global _APP_CONFIG_OVERRIDE, _CONFIG_CACHE
    if replace:
        _APP_CONFIG_OVERRIDE = payload
    else:
        _APP_CONFIG_OVERRIDE = _deep_merge(_APP_CONFIG_OVERRIDE, payload)
    _CONFIG_CACHE = None


def get_app_config_path() -> str:
    """Return the configured app JSON path."""
    if _APP_CONFIG_PATH_OVERRIDE:
        return str(_APP_CONFIG_PATH_OVERRIDE)
    return str(_resolve_config_path("app.json"))


def get_mcp_config_path() -> str:
    """Return the configured MCP JSON path."""
    if _MCP_CONFIG_DISABLED:
        return ""
    if _MCP_CONFIG_PATH_OVERRIDE:
        return str(_MCP_CONFIG_PATH_OVERRIDE)
    return str(_resolve_config_path("mcp.json"))


def get_config() -> AppConfig:
    """Return cached AppConfig instance."""
    global _CONFIG_CACHE, _CONFIG_WARNED
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    config_path = Path(get_app_config_path())
    if not config_path.exists() and not _CONFIG_WARNED:
        _logger.warning(
            "Config file not found at %s. Run /config init to scaffold examples.",
            config_path,
        )
        _CONFIG_WARNED = True
    base_payload = AppConfig().model_dump()
    file_payload = _load_json(get_app_config_path())
    merged = _deep_merge(base_payload, file_payload)
    if _APP_CONFIG_OVERRIDE:
        merged = _deep_merge(merged, _APP_CONFIG_OVERRIDE)
    _CONFIG_CACHE = AppConfig.model_validate(merged)
    return _CONFIG_CACHE


def get_config_value(*keys: str, default: Any | None = None) -> Any:
    """Return a nested config value or default."""
    current: Any = get_config()
    for key in keys:
        if isinstance(current, BaseModel):
            current = getattr(current, key, None)
        elif isinstance(current, dict):
            current = current.get(key)
        else:
            return default
        if current is None:
            return default
    return current


def get_config_section(*keys: str) -> dict[str, Any]:
    """Return a config section as a dictionary."""
    value = get_config_value(*keys, default={})
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    return {}


def ensure_app_config(path: str | Path) -> None:
    """Write the default config file if missing."""
    target = Path(path)
    if target.exists():
        return
    AppConfig().write(target)


_APP_SCHEMA_URL = "https://thekrishna.in/Assistant/latest/app.schema.json"
_MCP_SCHEMA_URL = (
    "https://gist.githubusercontent.com/bearlike"
    "/874db9d60a070706e4a703db1290b8d2/raw"
    "/mcp-server-config.schema.json"
)


def _example_app_payload() -> dict[str, Any]:
    payload: dict[str, Any] = {"$schema": _APP_SCHEMA_URL}
    payload.update(AppConfig().model_dump())
    # Reset resolved paths to empty so users get $MEWBO_HOME defaults
    payload["runtime"]["cache_dir"] = ""
    payload["runtime"]["session_dir"] = ""
    payload["runtime"]["config_dir"] = ""
    # Sensible placeholders for the LLM section
    payload["llm"]["api_base"] = ""
    payload["llm"]["proxy_model_prefix"] = "openai"
    payload["llm"]["api_key"] = "sk-ant-xxxxxxxx"
    payload["llm"]["default_model"] = "anthropic/claude-sonnet-4-6"
    # Placeholder credentials for optional integrations
    payload["langfuse"]["host"] = "https://langfuse.server.local"
    payload["langfuse"]["public_key"] = "pk-lf-xxxxxxxxxxxxxxxx"
    payload["langfuse"]["secret_key"] = "sk-lf-xxxxxxxxxxxxxxxx"
    payload["home_assistant"]["url"] = "http://homeassistant.local:8123"
    payload["home_assistant"]["token"] = "ha_token_here"
    return payload


def _default_example_path(filename: str) -> Path:
    """Return the example config path: ``CWD/configs/`` if present, else ``MEWBO_HOME``."""
    cwd_configs = Path("configs")
    if cwd_configs.is_dir():
        return cwd_configs / filename
    return resolve_mewbo_home() / filename


def ensure_example_configs(
    app_path: str | Path | None = None,
    mcp_path: str | Path | None = None,
) -> tuple[Path, Path]:
    """Write example config files if missing. Returns ``(app_path, mcp_path)``."""
    app_target = Path(app_path) if app_path else _default_example_path("app.example.json")
    if not app_target.exists():
        app_target.parent.mkdir(parents=True, exist_ok=True)
        app_target.write_text(
            json.dumps(_example_app_payload(), indent=2) + "\n",
            encoding="utf-8",
        )
    mcp_target = Path(mcp_path) if mcp_path else _default_example_path("mcp.example.json")
    if not mcp_target.exists():
        mcp_target.parent.mkdir(parents=True, exist_ok=True)
        mcp_target.write_text(
            json.dumps(
                {
                    "$schema": _MCP_SCHEMA_URL,
                    "servers": {
                        "codex_tools": {
                            "transport": "streamable_http",
                            "url": "http://127.0.0.1:6783/mcp/Codex-Tools-Personal",
                            "headers": {"Authorization": "Bearer YOUR_MCP_TOKEN"},
                        }
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return app_target, mcp_target


_MAX_MCP_SUBTREE_DEPTH = 5


def _discover_subtree_mcp_json(
    cwd: str | None = None,
    *,
    max_depth: int = _MAX_MCP_SUBTREE_DEPTH,
) -> list[dict[str, Any]]:
    """Walk DOWN from *cwd* to find ``.mcp.json`` in subdirectories.

    Returns parsed configs ordered deepest-first so callers can merge
    in sequence (shallower configs naturally override deeper ones).
    Skips CWD itself (handled by ``_discover_cwd_mcp_json``).
    """
    work_dir = Path(cwd) if cwd else Path.cwd()
    found: list[tuple[int, str, dict[str, Any]]] = []
    for dirpath, dirnames, _filenames in os.walk(work_dir):
        rel = Path(dirpath).relative_to(work_dir)
        depth = len(rel.parts)
        dirnames[:] = [
            d
            for d in dirnames
            if not d.startswith(".") and d not in ("node_modules", "__pycache__", ".venv", "venv")
        ]
        if depth == 0:
            continue
        if depth > max_depth:
            dirnames.clear()
            continue
        mcp_json = Path(dirpath) / ".mcp.json"
        if not mcp_json.is_file():
            continue
        try:
            with mcp_json.open(encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            _logger.warning("Failed to read %s: %s", mcp_json, exc)
            continue
        if not isinstance(raw, dict):
            continue
        if "mcpServers" in raw and "servers" not in raw:
            raw["servers"] = raw.pop("mcpServers")
        found.append((depth, str(mcp_json), raw))
    # Deepest first, then alphabetical for determinism at same depth
    found.sort(key=lambda t: (-t[0], t[1]))
    return [cfg for _, _, cfg in found]


def _discover_cwd_mcp_json(cwd: str | None = None) -> dict[str, Any] | None:
    """Read ``.mcp.json`` from *cwd* and return parsed config, or ``None``.

    Supports both ``{"mcpServers": {...}}`` (Claude Code style) and
    ``{"servers": {...}}`` (Mewbo native) schemas.
    """
    work_dir = Path(cwd) if cwd else Path.cwd()
    mcp_json = work_dir / ".mcp.json"
    if not mcp_json.is_file():
        return None
    try:
        with mcp_json.open(encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _logger.warning("Failed to read %s: %s", mcp_json, exc)
        return None
    if not isinstance(raw, dict):
        return None
    # Normalize Claude Code schema: mcpServers → servers
    if "mcpServers" in raw and "servers" not in raw:
        raw["servers"] = raw.pop("mcpServers")
    return raw


def get_merged_mcp_config(
    cwd: str | None = None,
    *,
    extra_servers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load and merge MCP configs: plugin extras + global + subtree + CWD ``.mcp.json``.

    Priority (lowest → highest): extra_servers < global < subtree (deep→shallow) < CWD.
    Returns the merged config dict with a ``servers`` key.
    When MCP is disabled (via ``set_mcp_config_path(None)``), returns ``{}``.
    """
    if _MCP_CONFIG_DISABLED:
        return {}

    # 0. Plugin MCP servers (lowest priority — user/project configs override)
    merged: dict[str, Any] = {}
    if extra_servers:
        merged = {"servers": dict(extra_servers)}

    # 1. Load global config
    global_config: dict[str, Any] = {}
    global_path = get_mcp_config_path()
    if global_path and Path(global_path).is_file():
        try:
            with open(global_path, encoding="utf-8") as fh:
                global_config = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            _logger.warning("Failed to read global MCP config %s: %s", global_path, exc)

    if not isinstance(global_config, dict):
        global_config = {}

    # 2. Discover subtree .mcp.json files (deepest first)
    subtree_configs = _discover_subtree_mcp_json(cwd)

    # 3. Discover CWD .mcp.json
    cwd_config = _discover_cwd_mcp_json(cwd)

    # 4. Merge: extra_servers ← global ← subtree (deep→shallow) ← CWD
    merged = _deep_merge(merged, global_config)
    for sub_cfg in subtree_configs:
        merged = _deep_merge(merged, sub_cfg)
    if cwd_config:
        merged = _deep_merge(merged, cwd_config)

    return merged


__all__ = [
    "AppConfig",
    "ConfigCheck",
    "HookEntry",
    "HooksConfig",
    "ProjectConfig",
    "ensure_app_config",
    "ensure_example_configs",
    "get_app_config_path",
    "get_config",
    "get_config_section",
    "get_config_value",
    "get_last_preflight",
    "get_mcp_config_path",
    "get_merged_mcp_config",
    "reset_config",
    "resolve_mewbo_home",
    "set_app_config_path",
    "set_config_override",
    "set_mcp_config_path",
    "start_preflight",
]
