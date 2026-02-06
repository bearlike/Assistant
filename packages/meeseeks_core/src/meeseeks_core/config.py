#!/usr/bin/env python3
"""Central JSON configuration for Meeseeks."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic.v1 import BaseModel, Field, validator

_APP_CONFIG_PATH = Path("configs/app.json")
_APP_EXAMPLE_PATH = Path("configs/app.example.json")
_MCP_CONFIG_PATH = Path("configs/mcp.json")
_MCP_EXAMPLE_PATH = Path("configs/mcp.example.json")
_APP_CONFIG_PATH_OVERRIDE: Path | None = None
_MCP_CONFIG_PATH_OVERRIDE: Path | None = None
_MCP_CONFIG_DISABLED = False
_APP_CONFIG_OVERRIDE: dict[str, Any] = {}
_CONFIG_CACHE: AppConfig | None = None
_CONFIG_WARNED = False
_LAST_PREFLIGHT: dict[str, dict[str, Any]] | None = None
_logger = logging.getLogger("core.config")


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
    version: str = Field("2.1.0-alpha", example="2.1.0-alpha")
    envmode: str = Field("dev", example="dev")
    log_level: str = Field("DEBUG", example="INFO")
    log_style: str = Field("", example="")
    cli_log_style: str = Field("dark", example="dark")
    preflight_enabled: bool = False
    cache_dir: str = Field(".cache", example=".cache")
    session_dir: str = Field("./data/sessions", example="./data/sessions")
    config_dir: str = Field("~/.meeseeks", example="~/.meeseeks")

    @validator("log_level", pre=True, always=True)
    def _normalize_log_level(cls, value: Any) -> str:
        if not value:
            return "DEBUG"
        return str(value).strip().upper()

    @validator("cache_dir", "session_dir", "config_dir", pre=True, always=True)
    def _normalize_paths(cls, value: Any, field) -> str:
        if value is None:
            return str(field.default)
        return str(value)

    @validator("preflight_enabled", pre=True, always=True)
    def _normalize_preflight_enabled(cls, value: Any) -> bool:
        return _coerce_bool(value, default=False)


class LLMConfig(BaseModel):
    api_base: str = Field("", example="https://lite-llm.server.local/v1")
    api_key: str = Field("", example="sk-OPENAI_API_KEY")
    default_model: str = Field("gpt-5.2", example="gpt-5.2")
    action_plan_model: str = Field("", example="gpt-5.2")
    tool_model: str = Field("", example="gpt-5.2")
    reasoning_effort: str = Field("", example="medium")
    reasoning_effort_models: list[str] = Field(default_factory=list)

    @validator("reasoning_effort", pre=True, always=True)
    def _normalize_reasoning_effort(cls, value: Any) -> str:
        if value is None:
            return ""
        normalized = str(value).strip().lower()
        if normalized in {"low", "medium", "high", "none"}:
            return normalized
        return ""

    @validator("reasoning_effort_models", pre=True, always=True)
    def _normalize_reasoning_effort_models(cls, value: Any) -> list[str]:
        return [entry.lower() for entry in _coerce_list(value)]

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
                ok=False,
                reason="llm.api_base is not set",
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
        for model_name in {self.default_model, self.action_plan_model, self.tool_model}:
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
    recent_event_limit: int = Field(8, example=8)
    selection_threshold: float = Field(0.8, example=0.8)
    selection_enabled: bool = Field(True, example=True)
    context_selector_model: str = Field("", example="gpt-5.2")

    @validator("recent_event_limit", pre=True, always=True)
    def _normalize_recent_event_limit(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 8
        return max(parsed, 1)

    @validator("selection_threshold", pre=True, always=True)
    def _normalize_selection_threshold(cls, value: Any) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return 0.8
        return min(max(parsed, 0.0), 1.0)

    @validator("selection_enabled", pre=True, always=True)
    def _normalize_selection_enabled(cls, value: Any) -> bool:
        return _coerce_bool(value, default=True)


class TokenBudgetConfig(BaseModel):
    default_context_window: int = Field(128000, example=128000)
    auto_compact_threshold: float = Field(0.8, example=0.8)
    model_context_windows: dict[str, int] = Field(default_factory=dict)

    @validator("default_context_window", pre=True, always=True)
    def _normalize_context_window(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 128000
        return max(parsed, 1)

    @validator("auto_compact_threshold", pre=True, always=True)
    def _normalize_compact_threshold(cls, value: Any) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return 0.8
        return min(max(parsed, 0.0), 1.0)

    @validator("model_context_windows", pre=True, always=True)
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


class ReflectionConfig(BaseModel):
    enabled: bool = Field(True, example=True)
    model: str = Field("", example="gpt-5.2")

    @validator("enabled", pre=True, always=True)
    def _normalize_enabled(cls, value: Any) -> bool:
        return _coerce_bool(value, default=True)


class LangfuseConfig(BaseModel):
    enabled: bool = Field(False, example=False)
    host: str = Field("", example="https://langfuse.server.local")
    public_key: str = Field("", example="pk-lf-xxxxxxxxxxxxxxxx")
    secret_key: str = Field("", example="sk-lf-xxxxxxxxxxxxxxxx")

    @validator("enabled", pre=True, always=True)
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
    enabled: bool = Field(False, example=False)
    url: str = Field("", example="http://homeassistant.local:8123")
    token: str = Field("", example="ha_token_here")

    @validator("enabled", pre=True, always=True)
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
    policy_path: str = Field("", example="./configs/policy.json")
    approval_mode: str = Field("ask", example="ask")

    @validator("approval_mode", pre=True, always=True)
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
    disable_textual: bool = Field(False, example=False)
    approval_style: str = Field("inline", example="aider")

    @validator("disable_textual", pre=True, always=True)
    def _normalize_disable_textual(cls, value: Any) -> bool:
        return _coerce_bool(value, default=False)

    @validator("approval_style", pre=True, always=True)
    def _normalize_approval_style(cls, value: Any) -> str:
        if value is None:
            return "inline"
        normalized = str(value).strip().lower()
        if normalized in {"inline", "textual", "aider"}:
            return normalized
        return "inline"


class ChatConfig(BaseModel):
    streamlit_port: int = Field(8501, example=8501)
    streamlit_address: str = Field("127.0.0.1", example="127.0.0.1")

    @validator("streamlit_port", pre=True, always=True)
    def _normalize_port(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 8501
        return max(parsed, 1)


class APIConfig(BaseModel):
    master_token: str = Field("msk-strong-password", example="msk-strong-password")


def _runtime_config_default() -> RuntimeConfig:
    return RuntimeConfig.parse_obj({})


def _llm_config_default() -> LLMConfig:
    return LLMConfig.parse_obj({})


def _context_config_default() -> ContextConfig:
    return ContextConfig.parse_obj({})


def _token_budget_config_default() -> TokenBudgetConfig:
    return TokenBudgetConfig.parse_obj({})


def _reflection_config_default() -> ReflectionConfig:
    return ReflectionConfig.parse_obj({})


def _langfuse_config_default() -> LangfuseConfig:
    return LangfuseConfig.parse_obj({})


def _home_assistant_config_default() -> HomeAssistantConfig:
    return HomeAssistantConfig.parse_obj({})


def _permissions_config_default() -> PermissionsConfig:
    return PermissionsConfig.parse_obj({})


def _cli_config_default() -> CLIConfig:
    return CLIConfig.parse_obj({})


def _chat_config_default() -> ChatConfig:
    return ChatConfig.parse_obj({})


def _api_config_default() -> APIConfig:
    return APIConfig.parse_obj({})


class AppConfig(BaseModel):
    """Typed configuration for the Meeseeks runtime."""

    runtime: RuntimeConfig = Field(default_factory=_runtime_config_default)
    llm: LLMConfig = Field(default_factory=_llm_config_default)
    context: ContextConfig = Field(default_factory=_context_config_default)
    token_budget: TokenBudgetConfig = Field(default_factory=_token_budget_config_default)
    reflection: ReflectionConfig = Field(default_factory=_reflection_config_default)
    langfuse: LangfuseConfig = Field(default_factory=_langfuse_config_default)
    home_assistant: HomeAssistantConfig = Field(default_factory=_home_assistant_config_default)
    permissions: PermissionsConfig = Field(default_factory=_permissions_config_default)
    cli: CLIConfig = Field(default_factory=_cli_config_default)
    chat: ChatConfig = Field(default_factory=_chat_config_default)
    api: APIConfig = Field(default_factory=_api_config_default)

    class Config:
        """Pydantic configuration settings."""

        extra = "ignore"

    @classmethod
    def load(cls, path: str | Path) -> AppConfig:
        """Load configuration from a JSON file."""
        payload = _load_json(path)
        return cls.parse_obj(payload)

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize config to JSON."""
        return self.json(indent=indent, exclude_none=True)

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
                from meeseeks_tools.integration import mcp as mcp_module

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
    return str(_APP_CONFIG_PATH_OVERRIDE or _APP_CONFIG_PATH)


def get_mcp_config_path() -> str:
    """Return the configured MCP JSON path."""
    if _MCP_CONFIG_DISABLED:
        return ""
    return str(_MCP_CONFIG_PATH_OVERRIDE or _MCP_CONFIG_PATH)


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
    base_payload = AppConfig().dict()
    file_payload = _load_json(get_app_config_path())
    merged = _deep_merge(base_payload, file_payload)
    if _APP_CONFIG_OVERRIDE:
        merged = _deep_merge(merged, _APP_CONFIG_OVERRIDE)
    _CONFIG_CACHE = AppConfig.parse_obj(merged)
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
        return value.dict()
    if isinstance(value, dict):
        return value
    return {}


def ensure_app_config(path: str | Path) -> None:
    """Write the default config file if missing."""
    target = Path(path)
    if target.exists():
        return
    AppConfig().write(target)


def _example_app_payload() -> dict[str, Any]:
    payload = AppConfig().dict()
    payload["llm"]["api_base"] = "https://lite-llm.server.local/v1"
    payload["llm"]["api_key"] = "sk-OPENAI_API_KEY"
    payload["langfuse"]["host"] = "https://langfuse.server.local"
    payload["langfuse"]["public_key"] = "pk-lf-xxxxxxxxxxxxxxxx"
    payload["langfuse"]["secret_key"] = "sk-lf-xxxxxxxxxxxxxxxx"
    payload["home_assistant"]["url"] = "http://homeassistant.local:8123"
    payload["home_assistant"]["token"] = "ha_token_here"
    return payload


def ensure_example_configs(
    app_path: str | Path | None = None,
    mcp_path: str | Path | None = None,
) -> None:
    """Write example config files if missing."""
    app_target = Path(app_path) if app_path else _APP_EXAMPLE_PATH
    if not app_target.exists():
        app_target.parent.mkdir(parents=True, exist_ok=True)
        app_target.write_text(json.dumps(_example_app_payload(), indent=2) + "\n", encoding="utf-8")
    mcp_target = Path(mcp_path) if mcp_path else _MCP_EXAMPLE_PATH
    if not mcp_target.exists():
        mcp_target.parent.mkdir(parents=True, exist_ok=True)
        mcp_target.write_text(
            json.dumps(
                {
                    "servers": {
                        "codex_tools": {
                            "transport": "streamable_http",
                            "url": "http://127.0.0.1:6783/mcp/Codex-Tools-Personal",
                            "headers": {"Authorization": "Bearer YOUR_MCP_TOKEN"},
                        }
                    }
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


__all__ = [
    "AppConfig",
    "ConfigCheck",
    "ensure_app_config",
    "ensure_example_configs",
    "get_app_config_path",
    "get_config",
    "get_config_section",
    "get_config_value",
    "get_last_preflight",
    "get_mcp_config_path",
    "reset_config",
    "set_app_config_path",
    "set_config_override",
    "set_mcp_config_path",
    "start_preflight",
]
