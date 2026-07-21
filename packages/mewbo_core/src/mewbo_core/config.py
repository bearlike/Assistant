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
    SerializerFunctionWrapHandler,
    ValidationInfo,
    field_validator,
    model_serializer,
    model_validator,
)

# Single source of truth for the retry/fallback defaults (defined where the
# behaviour lives); the RetryConfig fields below reference these.
from mewbo_core.llm_resilience import (
    DEFAULT_BACKOFF_BASE,
    DEFAULT_BACKOFF_CAP,
    DEFAULT_BUDGET_CAPACITY,
    DEFAULT_CB_COOLDOWN,
    DEFAULT_CB_THRESHOLD,
    DEFAULT_DOOM_LOOP_THRESHOLD,
    DEFAULT_FALLBACK_RETRIES,
    DEFAULT_PRIMARY_RETRIES,
    DEFAULT_RETRY_AFTER_CAP,
    DEFAULT_TIMEOUT,
    DEFAULT_TURN_DEADLINE,
    DEFAULT_WRITE_PROGRESS_EVENT_INTERVAL,
    DEFAULT_WRITE_PROGRESS_MAX_EVENTS,
    DEFAULT_WRITE_PROGRESS_THRESHOLD,
)

# Imported here (not redefined) so the verifier-gate default lives in ONE place
# — ``verification.py`` owns it, config references it, same pattern as the
# write-progress constants above. ``verification.py`` deliberately imports
# nothing from core at module top, so this import cannot cycle.
from mewbo_core.verification import DEFAULT_VERIFICATION_TIMEOUT

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
    """Find a config file by walking up from CWD, then ``MEWBO_HOME``.

    The first ``configs/<filename>`` found while ascending from the current
    directory wins, so running ``mewbo`` from a subdirectory (or a parent
    that contains the project) still loads the project's config instead of
    silently falling back to built-in defaults. The ascent stops at the git
    root (or the filesystem root), mirroring project-instruction discovery
    (``common._find_git_root``). When nothing is found, fall back to
    ``MEWBO_HOME`` exactly as before.
    """
    # Lazy import: common imports config at module load, so importing it at
    # module top would be a cycle (matches the codebase's in-function imports).
    from mewbo_core.common import _find_git_root

    start = Path.cwd().resolve()
    stop_at = _find_git_root(start) or Path(start.anchor)
    current = start
    while current >= stop_at:
        candidate = current / "configs" / filename
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent
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

    model_config = ConfigDict(
        validate_default=True,
        json_schema_extra={
            "title": "Runtime",
            "x-group": "server",
            "x-order": 3,
            "x-advanced": True,
        },
    )

    envmode: str = Field(
        "dev",
        description=(
            "Free-text label for this deployment (e.g. dev, staging, prod). Its "
            "only effect is being stamped onto every Langfuse trace as the "
            "`release` tag, so you can filter and compare traces across "
            "environments."
        ),
        examples=["dev"],
    )
    log_level: str = Field(
        "DEBUG",
        description="Logging verbosity. One of DEBUG, INFO, WARNING, ERROR, CRITICAL.",
        examples=["INFO"],
    )
    log_style: str = Field(
        "",
        description=(
            "Legacy override for the CLI's terminal log format. Only the literal "
            "value 'dark' has any effect (dims the log line style for a dark "
            "background); anything else uses the plain format. Takes priority "
            "over cli_log_style when set; leave empty to use that instead."
        ),
        examples=[""],
    )
    cli_log_style: str = Field(
        "dark",
        description=(
            "CLI terminal log format: only the literal value 'dark' has any "
            "effect (dims the log line style for a dark background); any other "
            "value uses the plain format. Overridden by runtime.log_style when "
            "that's set."
        ),
        examples=["dark"],
    )
    preflight_enabled: bool = Field(
        False,
        description=("Run connectivity checks for LLM, Langfuse, and Home Assistant on startup."),
    )
    developer_mode: bool = Field(
        False,
        description=(
            "Enable developer mode. Unlocks graph-only (no-LLM) repository "
            "onboarding so contributors can inspect AST graph construction "
            "without documentation generation. Read by the API, console, and CLI."
        ),
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

    @field_validator("developer_mode", mode="before")
    @classmethod
    def _normalize_developer_mode(cls, value: Any) -> bool:
        return _coerce_bool(value, default=False)


class FallbackConfig(BaseModel):
    """Opt-in cross-model fallback policy.

    Disabled by default so a run never fans out to a different model, with
    different cost, latency, output style and prompt-cache behaviour, without
    an explicit opt-in. When disabled, an error that is hopeless on the current
    model (e.g. quota exhausted) halts cleanly for one-click recovery instead.
    """

    model_config = ConfigDict(
        validate_default=True,
        json_schema_extra={"title": "Fallback"},
    )

    enabled: bool = Field(
        False,
        description=(
            "Enable automatic fallback to other models when the primary is "
            "exhausted or hits a hopeless-here error. Off by default."
        ),
    )
    models: list[str] = Field(
        default_factory=list,
        description="Ordered fallback model IDs, tried after the primary is exhausted.",
        examples=[["gpt-5.4", "gemini-2.5-pro"]],
    )
    self_steering: bool = Field(
        False,
        description=(
            "Allow the agent to steer its own model routing at runtime via the "
            "model_control tool (switch down the declared fallback ladder, or up "
            "when allow_upgrade is set). Off by default; the automatic fallback "
            "ladder still operates regardless. Every deliberate switch is bounded "
            "by max_switches and the existing retry budget / circuit breaker."
        ),
    )
    max_switches: int = Field(
        2,
        ge=0,
        description=(
            "Maximum deliberate model switches the model_control tool may perform "
            "in one run. 0 disables switching while leaving status/list readable."
        ),
    )
    allow_upgrade: bool = Field(
        False,
        description=(
            "Permit model_control switches UP the declared ladder (toward the "
            "primary). Off by default, so self-steering is a one-way ratchet "
            "downward — the direction that heals a failing primary without "
            "re-provoking it."
        ),
    )


class LLMConfig(BaseModel):
    """LLM provider connection and model selection."""

    model_config = ConfigDict(
        validate_default=True,
        json_schema_extra={"title": "Language Model", "x-group": "models", "x-order": 1},
    )

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
        json_schema_extra={"x-secret": True},
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
            "Model ID the orchestrator uses to generate a session's initial plan "
            "and, when no explicit model is passed in, as that session's default "
            "model. Falls back to default_model when empty."
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
            "Legacy ordered list of fallback model IDs. Prefer 'fallback' "
            "(typed, explicit opt-in). A non-empty value here is still honored "
            "for backward compatibility (treated as fallback enabled)."
        ),
        examples=[["gpt-5.4", "gemini-2.5-pro"]],
    )
    fallback: FallbackConfig = Field(
        default_factory=lambda: FallbackConfig.model_validate({}),
        description="Opt-in cross-model fallback policy (see FallbackConfig).",
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
        json_schema_extra={"x-advanced": True},
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
        description=(
            "Additional model IDs (or 'prefix*' patterns) that should receive the "
            "reasoning_effort parameter, on top of the built-in match for gpt-5, "
            "o3, Claude, and Gemini models. Use this to opt in a model the "
            "built-in detection doesn't recognize yet."
        ),
    )
    structured_patch_models: list[str] = Field(
        default_factory=list,
        description=(
            "Model IDs (or glob prefixes ending in '*') that prefer the "
            "structured_patch edit tool over search_replace_block. Runtime "
            "override layer ON TOP of the controllable model→tool-variant map "
            "(mewbo_core/prompts/model_variants.yaml), which now holds the "
            "built-in defaults (GPT-5/o3/o4/Codex/GPT-4); only set this to "
            "override or extend without editing that file."
        ),
        json_schema_extra={"x-advanced": True},
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

    def resolve_available_model(self, model: str, *, fallback: str, timeout: float = 4.0) -> str:
        """Return *model* if the proxy still advertises it, else *fallback*.

        Guards a persisted/stale model id (e.g. a wiki reindex replaying an old
        submission) against a model the proxy has since retired, which would
        otherwise fast-fail the whole run on an invalid-model 400. Best-effort:
        if the model list can't be fetched we trust *model* (the caller's
        retry/fallback ladder is the backstop). The provider prefix is ignored
        on both sides (``openai/x`` matches a bare ``x`` the proxy advertises).
        """
        if not model:
            return fallback
        try:
            available = self.list_models(timeout=timeout)
        except Exception:  # noqa: BLE001 — unreachable/unset proxy ⇒ trust model
            return model
        if not available:
            return model

        def _bare(name: str) -> str:
            return name.split("/", 1)[-1].strip().lower()

        target = _bare(model)
        if any(_bare(entry) == target for entry in available):
            return model
        return fallback

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

    model_config = ConfigDict(
        validate_default=True,
        json_schema_extra={"title": "Context", "x-group": "models", "x-order": 3},
    )

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

    model_config = ConfigDict(
        validate_default=True,
        json_schema_extra={"title": "Token Budget", "x-group": "models", "x-order": 2},
    )

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

    ``caveman_mode`` enables a rule-augmented "caveman" prompt that
    instructs the summarizer LLM to drop articles, filler, pleasantries,
    and hedging while preserving code, paths, URLs, and error strings
    verbatim. Reduces output tokens in the compaction summary without
    changing the
    ``<analysis>/<summary>`` response structure downstream parsers expect.
    """

    model_config = ConfigDict(
        validate_default=True,
        json_schema_extra={"title": "Compaction", "x-group": "models", "x-order": 4},
    )

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

    model_config = ConfigDict(
        validate_default=True,
        json_schema_extra={"title": "Reflection", "x-group": "models", "x-order": 5},
    )

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

    model_config = ConfigDict(
        validate_default=True,
        json_schema_extra={"title": "Langfuse", "x-group": "integrations", "x-order": 2},
    )

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
        json_schema_extra={"x-secret": True},
    )
    secret_key: str = Field(
        "",
        description="Langfuse project secret key.",
        examples=["sk-lf-xxxxxxxxxxxxxxxx"],
        json_schema_extra={"x-secret": True},
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

    model_config = ConfigDict(
        validate_default=True,
        json_schema_extra={"title": "Home Assistant", "x-group": "integrations", "x-order": 1},
    )

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
        json_schema_extra={"x-secret": True},
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

    model_config = ConfigDict(
        validate_default=True,
        json_schema_extra={"title": "Permissions", "x-group": "agent", "x-order": 2},
    )

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


class CliRemoteConfig(BaseModel):
    """Opt-in remote endpoint for the terminal CLI; CLI-scoped ONLY.

    Every other surface ignores this block. When ``base_url`` is set the CLI is
    still a strictly-local engine (the run loop + authoritative JSONL transcript
    stay on this host), but it additionally (a) mirrors each session event to the
    remote REST API fire-and-forget for cross-device visibility and (b)
    auto-registers the Mewbo MCP server so the product tools
    (``ask_wiki``/``search``/``structured_query`` + wiki-graph reads) appear in
    the CLI registry and execute remotely (compute offload). Empty ⇒ fully local.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_default=True,
        json_schema_extra={"title": "CLI Remote"},
    )

    base_url: str = Field(
        "",
        description=(
            "Base URL of the remote Mewbo deployment, a reverse-proxy root that "
            "serves the REST API under ``/api`` and the Mewbo MCP server under "
            "``/mcp``. Empty (default) ⇒ the CLI is fully local."
        ),
        examples=["https://mewbo.example.com"],
    )
    token: str = Field(
        "",
        description=(
            "API token presented to the remote deployment: sent as ``X-API-Key`` "
            "to the REST API for transcript sync and as a ``Bearer`` token to the "
            "Mewbo MCP server (which forwards it to the REST API). ``${ENV_VAR}`` "
            "references are expanded by the CLI at use time."
        ),
        json_schema_extra={"x-secret": True},
    )

    @property
    def enabled(self) -> bool:
        """True when a remote base URL is configured (sync + product tools on)."""
        return bool(self.base_url.strip())


class CLIConfig(BaseModel):
    """Terminal CLI display and interaction settings."""

    model_config = ConfigDict(
        validate_default=True,
        json_schema_extra={"title": "CLI", "x-group": "interface", "x-order": 1},
    )

    disable_textual: bool = Field(
        False,
        description="Disable the Textual TUI and fall back to plain Rich output.",
    )
    remote: CliRemoteConfig = Field(
        default_factory=lambda: CliRemoteConfig.model_validate({}),
        description="Opt-in remote session sync + product tools (CLI-only).",
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

    model_config = ConfigDict(
        validate_default=True,
        json_schema_extra={"title": "Chat", "x-group": "interface", "x-order": 2},
    )

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


class APIAuthSessionConfig(BaseModel):
    """Browser session/cookie settings for federated logins."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"title": "Session"})

    cookie_name: str = Field(
        "mewbo_session",
        description="Name of the browser session cookie issued after a federated login.",
    )
    ttl_seconds: int = Field(
        28800,
        ge=1,
        description="Lifetime of a browser session, in seconds.",
    )
    secret: str = Field(
        "",
        description=(
            "Signing secret for the browser session cookie. Required once any "
            "non-API-key authenticator is configured. Write-only: never returned "
            "by the config API."
        ),
        json_schema_extra={"x-secret": True},
    )


class APIAuthScimConfig(BaseModel):
    """SCIM 2.0 provisioning settings."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"title": "SCIM"})

    enabled: bool = Field(
        False,
        description="Whether the SCIM 2.0 provisioning endpoint is served.",
    )
    secret: str = Field(
        "",
        description=(
            "Bearer secret an identity provider presents to the SCIM endpoint. "
            "Write-only: never returned by the config API."
        ),
        json_schema_extra={"x-secret": True},
    )


class AuthenticatorEntry(BaseModel):
    """One identity source in ``api.auth.authenticators``.

    Deliberately PERMISSIVE (``extra="allow"``): the authoritative model is the
    identity kernel's discriminated authenticator union, which sits a layer
    above core and must never be imported down into it. Every kind-specific
    setting therefore rides through here untyped and is re-validated STRICTLY —
    per-kind, ``extra="forbid"`` — when the server builds its auth settings at
    startup, which refuses to boot on an invalid entry. So this model is not a
    second validator and must not grow into one.

    What it DOES declare is the plaintext credentials, because the config API
    redacts by SCHEMA: a field carries ``x-secret`` or its value is returned to
    every caller holding ``config.read``. An untyped ``dict`` contributes no
    schema, so nothing marked these and they were served in the clear. Core
    knows these two NAMES — a stable wire contract it shares with the identity
    kernel — without knowing which kind each belongs to or what it means, which
    is precisely the sliver of knowledge redaction needs and no more.
    ``kind``/``name`` stay typed so an entry is self-describing.
    """

    model_config = ConfigDict(extra="allow", json_schema_extra={"title": "Authenticator"})

    name: str = Field(
        ...,
        description="Unique label for this identity source, used in logs and the admin UI.",
    )
    kind: str = Field(
        ...,
        description=(
            "Which authenticator type this entry configures: `api_key`, `oidc`, "
            "`trusted_header`, `ldap`, or `saml`. Selects which further settings "
            "the entry must carry."
        ),
    )
    client_secret: str | None = Field(
        None,
        description=(
            "OIDC client secret issued by the identity provider. "
            "Write-only: never returned by the config API."
        ),
        json_schema_extra={"x-secret": True},
    )
    bind_password: str | None = Field(
        None,
        description=(
            "Password for the LDAP service account used to search the directory. "
            "Write-only: never returned by the config API."
        ),
        json_schema_extra={"x-secret": True},
    )

    @model_serializer(mode="wrap")
    def _drop_unset_credentials(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Serialize, omitting the credential fields this entry's kind lacks.

        Declaring both credentials on one model means a plain dump stamps
        ``bind_password: None`` onto an OIDC entry and ``client_secret: None``
        onto an LDAP one. That dump is fed straight back into the strict
        per-kind union at startup, where an unexpected key is a hard boot
        failure — so a null here is not cosmetic noise, it would stop the
        server. Absent and null must stay distinguishable for these two.
        """
        data = handler(self)
        for credential in ("client_secret", "bind_password"):
            if data.get(credential) is None:
                data.pop(credential, None)
        return data


class APIAuthConfig(BaseModel):
    """Identity & access management for the REST API (opt-in).

    Off by default: with no ``auth`` block — or ``enabled: false`` — every
    request resolves to the built-in full-power identity and the server behaves
    exactly as it did before IAM existed. The union-shaped fields
    (``authenticators``, the group mappings, ``bootstrap``) are carried here as
    open objects and validated in full against the identity kernel's typed models
    at server startup, which refuses to boot on an invalid block. These settings
    are documented in full in ``docs/authentication.md``.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"title": "Authentication"})

    enabled: bool = Field(
        False,
        description=(
            "Master switch for identity & access management. When off (the "
            "default), every request resolves to the built-in full-power identity "
            "and the server behaves exactly as it did before IAM. Turn on only "
            "after configuring at least one authenticator."
        ),
    )
    authenticators: list[AuthenticatorEntry] = Field(
        default_factory=list,
        description=(
            "Ordered list of identity sources (local API keys, OIDC, trusted "
            "reverse-proxy headers, LDAP, SAML). Each entry is an object whose "
            "`kind` field selects the authenticator type, plus that type's own "
            "settings. Validated in full at server startup; an invalid entry stops "
            "the server from booting. Each authenticator type's own settings are "
            "documented in `docs/authentication.md`."
        ),
    )
    role_mappings: dict[str, Any] | None = Field(
        None,
        description=(
            "Rules mapping identity-provider group names to Mewbo roles at login: "
            "an object with an ordered `rules` list and a `default_role` applied "
            "when no rule matches. The rule format is documented in "
            "`docs/authentication.md`."
        ),
    )
    team_mappings: dict[str, Any] | None = Field(
        None,
        description=(
            "Rules mapping identity-provider group names to team slugs at login: "
            "an object with an ordered `rules` list. The rule format is documented in "
            "`docs/authentication.md`."
        ),
    )
    bootstrap: dict[str, Any] | None = Field(
        None,
        description=(
            "Cold-start admin rule: grants the admin role to the first users "
            "matching an identity-provider group or an explicit subject allowlist, "
            "so an administrator exists before any role has been assigned. Documented "
            "in `docs/authentication.md`."
        ),
    )
    session: APIAuthSessionConfig = Field(
        default_factory=lambda: APIAuthSessionConfig.model_validate({}),
        description="Browser session/cookie settings for federated logins.",
    )
    avatars: dict[str, Any] | None = Field(
        None,
        description=(
            "Avatar-resolution policy: whether to fall back to Gravatar for users "
            "without a profile picture, and the default image style. Documented in "
            "`docs/authentication.md`."
        ),
    )
    scim: APIAuthScimConfig = Field(
        default_factory=lambda: APIAuthScimConfig.model_validate({}),
        description="SCIM 2.0 provisioning settings.",
    )
    audit: dict[str, Any] | None = Field(
        None,
        description=(
            "Auth audit-trail settings: an object with an `enabled` flag; on by "
            "default once IAM is enabled. The events recorded are listed in "
            "`docs/authentication.md`."
        ),
    )


class APIConfig(BaseModel):
    """REST API authentication."""

    model_config = ConfigDict(
        json_schema_extra={"title": "API Server", "x-group": "server", "x-order": 1},
    )

    master_token: str = Field(
        "msk-strong-password",
        description=(
            "Bearer token required for all REST API requests. "
            "Change from the default before deploying."
        ),
        examples=["msk-strong-password"],
        json_schema_extra={"x-protected": True},
    )
    allow_external_cwd: bool = Field(
        False,
        description=(
            "Allow callers to anchor sessions in an arbitrary host path via the "
            "`cwd` field on POST /api/sessions and POST /api/sessions/{id}/query. "
            "Off by default; enable only for trusted external workspace managers "
            "that manage their own worktrees."
        ),
    )
    apps_token_secret: str = Field(
        "",
        description=(
            "Signing secret for Mewbo Apps render tokens — the short-lived, "
            "app-scoped read tokens the served app frontend presents on the "
            "read-only data/system endpoints. Set this to sign (and rotate) app "
            "tokens independently of the master token; when left empty it falls "
            "back to the master token, logging one startup warning."
        ),
        json_schema_extra={"x-secret": True},
    )
    auth: APIAuthConfig = Field(
        default_factory=lambda: APIAuthConfig.model_validate({}),
        description=(
            "Identity & access management (opt-in; off by default). Configures "
            "authenticators, roles, and sessions. Documented in "
            "`docs/authentication.md`."
        ),
    )


class HookEntry(BaseModel):
    """A single hook configuration entry."""

    model_config = ConfigDict(json_schema_extra={"title": "Hook"})

    type: Literal["command", "http"] = Field(
        "command", description="Hook type: 'command' (shell) or 'http' (POST to URL)."
    )
    command: str = Field(
        "",
        description=(
            "Shell command run via subprocess with shell=True, on the host "
            "running the API/CLI process and with that process's own "
            "privileges: no sandboxing, no approval prompt. Only point this "
            "at trusted, version-controlled scripts. (type=command)"
        ),
    )
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
    """External shell hooks fired during the session lifecycle.

    Command hooks run unsandboxed shell commands with the API/CLI process's
    own privileges (see ``HookEntry.command``'s docstring) — a caller who can
    PATCH this section can execute arbitrary code on the host. ``x-protected``
    puts the whole section in the same never-read-never-written-via-API tier
    as the other host-level settings in this file: settable only by editing
    the config file directly, never over the network regardless of
    credential (see ``ConfigSchemaView`` in ``apps/mewbo_api``).
    """

    model_config = ConfigDict(
        json_schema_extra={
            "title": "Hooks",
            "x-group": "integrations",
            "x-order": 3,
            "x-protected": True,
        },
    )

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
    on_event: list[HookEntry] = Field(
        default_factory=list,
        description=(
            "Hooks executed (fire-and-forget) for every event appended to a "
            "session transcript. The matcher fnmatches the event type."
        ),
    )


class PluginsConfig(BaseModel):
    """Plugin system configuration."""

    model_config = ConfigDict(
        validate_default=True,
        json_schema_extra={"title": "Plugins", "x-group": "plugins", "x-order": 1},
    )

    enabled: bool = Field(
        True,
        description=(
            "Turn the whole plugin system on, including Mewbo's own built-in "
            "suites such as `widget_builder`.\n\n"
            "While this is off, no plugin contributes anything to the agent, "
            "whether it is built in or installed from a marketplace: no agent "
            "definitions, no skills, no hooks, no MCP tools. The "
            "`enabled_plugins` and `marketplaces` settings below are ignored "
            "entirely until you turn it back on."
        ),
    )
    enabled_plugins: list[str] = Field(
        default_factory=list,
        description=(
            "Plugin names to enable. Empty = all installed plugins. "
            "Format: 'plugin-name' or 'plugin-name@marketplace'."
        ),
    )
    marketplaces: list[str] = Field(
        default_factory=lambda: ["anthropics/claude-plugins-official"],
        description=(
            "Marketplace catalogs holding a marketplace.json plugin index, on any "
            "git host. Each entry is a full git URL "
            "(https/ssh/git, or scp-style git@host:owner/repo), a 'host/owner/repo' "
            "shorthand, or a bare 'owner/repo' (cloned from marketplace_default_host)."
        ),
    )
    marketplace_default_host: str = Field(
        "github.com",
        description=(
            "Default git host for bare 'owner/repo' marketplace entries. Full URLs "
            "and 'host/owner/repo' entries ignore this."
        ),
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

    @field_validator("marketplace_default_host", mode="before")
    @classmethod
    def _normalize_default_host(cls, value: Any) -> str:
        raw = str(value).strip() if value else ""
        return raw or "github.com"

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

        # 3. Ensure every configured catalog is cloned (skip ones already present).
        if sync and self.marketplaces:
            from mewbo_core.plugins import marketplace_dir_name, sync_marketplaces

            existing_names = {d.name for d in dirs}
            missing = []
            for entry in self.marketplaces:
                canonical = marketplace_dir_name(entry, default_host=self.marketplace_default_host)
                # Legacy pre-host-agnostic leaf name (and Claude Code's own
                # leaf-named cache) — reuse those clones instead of re-cloning
                # the same catalog under the new canonical name.
                legacy_leaf = entry.rstrip("/").split("/")[-1]
                if canonical not in existing_names and legacy_leaf not in existing_names:
                    missing.append(entry)
            if missing:
                synced = sync_marketplaces(
                    missing,
                    self.resolve_install_dir(),
                    default_host=self.marketplace_default_host,
                )
                dirs.extend(synced)

        return dirs


class TriggersConfig(BaseModel):
    """Reverse-invocation trigger subsystem.

    The durable peer of the sub-agent hypervisor: a background watcher that
    fires time / cron / CI / forge-PR / webhook triggers and re-invokes the
    sessions that armed them. OFF by default (``enabled=False``), so the feature
    is un-enableable until an operator turns it on and a stock deployment pays
    nothing. The lower half of this section is the admission policy the
    ``schedule_trigger`` tool + the arm route enforce (mirrors
    ``mewbo_core.triggers.policy.TriggerPolicy`` field-for-field; ``to_policy``
    builds one).
    """

    model_config = ConfigDict(
        validate_default=True,
        json_schema_extra={"title": "Triggers", "x-group": "automation", "x-order": 1},
    )

    enabled: bool = Field(
        False,
        description=(
            "Turn the trigger watcher on. Nothing fires until you do.\n\n"
            "A trigger is how Mewbo starts a session later, on its own, with nobody "
            "watching: at a set time, on a repeating schedule, or when a CI run "
            "finishes, a pull request changes, or a webhook calls in. The watcher is "
            "the background loop that notices those moments and wakes the session "
            "that asked to be woken. While it is off, the trigger routes still work, "
            "so a session can arm a trigger and you can list, pause, or cancel it, "
            "but no trigger ever fires. Armed triggers simply wait until you turn "
            "the watcher on."
        ),
    )
    tick_interval_seconds: float = Field(
        5.0,
        ge=1.0,
        description=(
            "How often the watcher wakes up to look at the schedule, in seconds.\n\n"
            "On each pass it expires the triggers whose deadline has gone by and "
            "fires the time and cron triggers that have come due. A shorter interval "
            "wakes a session closer to the moment it asked for; a longer one costs "
            "the server less. This is also the cadence at which the forge poll below "
            "gets a chance to run."
        ),
    )
    poll_interval_seconds: float = Field(
        60.0,
        ge=5.0,
        description=(
            "How often the watcher asks the forge about CI runs and pull requests, "
            "in seconds.\n\n"
            "Time and cron triggers can be judged from the clock alone, but "
            "`ci.workflow` and `forge.pr` triggers cannot: the watcher has to call "
            "the forge's REST API to see what changed. Those calls are rate-limited "
            "and cost a round trip each, so they run on this deliberately coarser "
            "cadence rather than on every pass. Raise it if you are bumping into API "
            "limits; lower it if you want CI results picked up sooner."
        ),
    )
    max_consecutive_failures: int = Field(
        5,
        ge=1,
        description=(
            "How many errors in a row one trigger may hit before it is given up "
            "on.\n\n"
            "When a fire or a forge poll raises, the watcher records the error on "
            "the trigger and leaves it armed, so a passing outage never throws away "
            "a schedule. Once a trigger has failed this many times back to back "
            "without a single success in between, the watcher stops retrying it and "
            "moves it to `failed`. Any success resets the count to zero."
        ),
    )
    max_armed_per_session: int = Field(
        20,
        ge=1,
        description=(
            "The most triggers one session may have armed at the same time.\n\n"
            "Triggers are armed by the agent from inside a session, so this ceiling "
            "is what keeps a single session from filling the schedule with wakes. An "
            "attempt to arm one past the limit is refused, and the agent is told why. "
            "Cancelling a trigger, or letting one finish, frees the slot again."
        ),
    )
    max_fires_cap: int = Field(
        100,
        ge=1,
        description=(
            "The ceiling on how many times any single trigger may fire.\n\n"
            "A repeating trigger, a cron schedule for instance, can name its own "
            "`max_fires` limit when it is armed. This is the ceiling on that request: "
            "an attempt to arm a trigger asking for more is refused. A trigger that "
            "reaches its own limit completes and stops firing."
        ),
    )
    default_expiry_days: float = Field(
        7.0,
        gt=0.0,
        description=(
            "How long an armed trigger lives when it names no expiry of its own, in "
            "days.\n\n"
            "Every trigger expires eventually, so that a wake nobody remembers "
            "arming cannot linger forever. When the agent arms one without setting "
            "an expiry date, this many days from the moment of arming is stamped on "
            "it. Once that moment passes, the watcher expires the trigger instead of "
            "firing it."
        ),
    )
    cron_min_interval_seconds: int = Field(
        60,
        ge=1,
        description=(
            "The shortest gap allowed between two fires of a cron trigger, in "
            "seconds.\n\n"
            "A cron expression can be written to fire far more often than a session "
            "is worth waking, so this is the floor. When a cron trigger is armed, the "
            "gap between its first two fires is measured, and a schedule tighter than "
            "this is rejected there and then rather than being throttled later."
        ),
    )
    webhook_payload_max_bytes: int = Field(
        200_000,
        ge=1,
        description=(
            "How much of an incoming webhook body the woken session gets to see, in "
            "bytes.\n\n"
            "A webhook can carry a large payload, and all of it becomes context the "
            "session has to read. A body bigger than this is truncated rather than "
            "rejected: the call still fires the trigger, the session receives the "
            "first part of the body, and it is told the payload was cut short. When "
            "a signature is configured, it is checked against the whole body before "
            "any truncation happens."
        ),
    )

    def to_policy(self) -> Any:
        """Build the ``TriggerPolicy`` these fields describe.

        Lazy import keeps this config module free of any dependency on the
        triggers domain package (and sidesteps an import cycle, since the
        trigger store reads ``get_config_value`` from here).
        """
        from datetime import timedelta

        from mewbo_core.triggers.policy import TriggerPolicy

        return TriggerPolicy(
            max_armed_per_session=self.max_armed_per_session,
            max_fires_cap=self.max_fires_cap,
            default_expiry=timedelta(days=self.default_expiry_days),
            cron_min_interval_seconds=self.cron_min_interval_seconds,
            webhook_payload_max_bytes=self.webhook_payload_max_bytes,
        )


class ProjectConfig(BaseModel):
    """A project directory exposed to the REST API for session scoping."""

    model_config = ConfigDict(
        validate_default=True,
        json_schema_extra={"title": "Project"},
    )

    path: str = Field(
        "",
        description=(
            "Absolute path to the project's root directory on the API host's "
            "filesystem (tilde is expanded). The directory must already exist, "
            "because Mewbo does not create it, and a session request against "
            "this project fails if the path is missing."
        ),
    )
    description: str = Field(
        "",
        description=(
            "Short blurb shown next to this project's name in project pickers. "
            "Purely informational, with no effect on behavior."
        ),
    )

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

    model_config = ConfigDict(extra="forbid", json_schema_extra={"title": "Web IDE"})

    enabled: bool = Field(
        default=False,
        description=(
            "Turn on the 'Open in Web IDE' feature (per-session code-server "
            "containers via Docker). Also requires a MongoDB-backed session "
            "store; toggling this needs an API process restart to take effect "
            "since the /api/ide routes are registered at startup."
        ),
    )
    image: str = Field(
        default="codercom/code-server:latest",
        description="Docker image used to launch each session's code-server container.",
    )
    default_lifetime_hours: int = Field(
        default=1,
        ge=1,
        le=24,
        description=(
            "Hours a new Web IDE container stays up before it self-terminates, "
            "unless the session extends it first."
        ),
    )
    max_lifetime_hours: int = Field(
        default=8,
        ge=1,
        le=168,
        description=(
            "Hard ceiling on a session's total Web IDE lifetime across all "
            "extensions; a request to extend past this is rejected."
        ),
    )
    cpus: float = Field(
        default=1.0,
        ge=0.1,
        le=16.0,
        description=(
            "CPU core limit for each Web IDE container, e.g. 1.0 = one core "
            "(maps to Docker's --cpus / nano_cpus)."
        ),
    )
    memory: str = Field(
        default="1g",
        pattern=r"^\d+[mgMG]$",
        description=(
            "Memory limit for each Web IDE container, in Docker's --memory "
            "syntax: digits followed by m or g, e.g. '1g' or '512m'."
        ),
    )
    pids_limit: int = Field(
        default=512,
        ge=64,
        le=4096,
        description=(
            "Maximum number of processes/threads allowed inside a Web IDE "
            "container; bounds a runaway process from exhausting the host."
        ),
    )
    network: str = Field(
        default="mewbo-ide",
        pattern=r"^[a-zA-Z0-9_-]+$",
        description=(
            "Docker network each Web IDE container joins. Must be the same "
            "network the ide-proxy is attached to, or the proxy can't reach "
            "the container."
        ),
    )
    proxy_url: str = Field(
        default="http://127.0.0.1:5126",
        description=(
            "Base URL the API uses to reach the ide-proxy for readiness "
            "probes. The default suits a host-networked API, where the proxy "
            "is published on loopback 127.0.0.1:5126. A bridge-networked API "
            "(e.g. one joined to extra Docker networks via a compose override) "
            "cannot reach that loopback: attach it to the `network` above and "
            "point this at the proxy's in-network name, e.g. "
            "http://mewbo-ide-proxy:8080."
        ),
    )
    state_dir: str = Field(
        default="/tmp/mewbo-ide",
        description=(
            "Host directory where each Web IDE container's expiry-deadline file "
            "is written; the container's internal watchdog reads it to "
            "self-terminate on schedule."
        ),
    )


class LSPConfig(BaseModel):
    """Language Server Protocol integration settings."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"title": "LSP"})

    enabled: bool = Field(
        True,
        description=(
            "Master switch for the native LSP tool (hover/diagnostics/"
            "go-to-definition). When off, or when the pygls dependency isn't "
            "installed, the tool is never registered and the agent works from "
            "grep/read alone."
        ),
    )
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
    Code's ``ToolSearchTool`` mechanism, saving substantial context tokens
    on sessions with many MCP servers connected.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"title": "Tool Search"})

    mode: Literal["off", "on", "auto"] = Field(
        "auto",
        description=(
            "'off' keeps every tool's schema in the initial bind. "
            "'on' always defers MCP tools and any spec with "
            "metadata.deferred=True; the model loads schemas on demand via "
            "tool_search. 'auto' defers only when the number of deferrable "
            "tools exceeds auto_threshold, so lean / zero-MCP sessions keep "
            "verbatim binding and pay nothing, while many-MCP sessions are "
            "spared ~240 tokens per tool every turn."
        ),
    )
    auto_threshold: int = Field(
        25,
        ge=0,
        description=(
            "In 'auto' mode, defer tool schemas only when more than this "
            "many deferrable tools (MCP + metadata.deferred specs) are "
            "registered. Ignored when mode is 'off' or 'on'."
        ),
    )


class RetryConfig(BaseModel):
    """Automatic LLM-call retry / fallback resilience knobs.

    Same-model retry hardening (full-jitter backoff, circuit breaker, retry
    budget, wall-clock deadline, doom-loop halt) is always on; cross-model
    fallback is opt-in via ``llm.fallback``. Defaults are calibrated from
    production agent loops, not the tighter vendor-SDK defaults.
    """

    model_config = ConfigDict(
        validate_default=True,
        json_schema_extra={"title": "Retry"},
    )

    backoff_base: float = Field(
        DEFAULT_BACKOFF_BASE,
        description="Base seconds for full-jitter backoff: random(0, min(cap, base*2^(n-1))).",
    )
    backoff_cap: float = Field(DEFAULT_BACKOFF_CAP, description="Maximum backoff delay in seconds.")
    retry_after_cap: float = Field(
        DEFAULT_RETRY_AFTER_CAP,
        description=(
            "Upper bound in seconds applied to a server's Retry-After header "
            "before the loop sleeps on it; caps how long one misbehaving "
            "response can stall a run."
        ),
    )
    turn_deadline: float = Field(
        DEFAULT_TURN_DEADLINE,
        description=(
            "Wall-clock seconds budget for one logical LLM call across all "
            "retries and fallbacks. Checked before each attempt. 0 disables."
        ),
    )
    fallback_retries: int = Field(
        DEFAULT_FALLBACK_RETRIES,
        description="Attempts per fallback model after the primary is exhausted.",
    )
    circuit_breaker_threshold: int = Field(
        DEFAULT_CB_THRESHOLD,
        description=(
            "Consecutive per-model failures before that model is cooled down "
            "and skipped (when an alternative exists). 0 disables."
        ),
    )
    circuit_breaker_cooldown: float = Field(
        DEFAULT_CB_COOLDOWN,
        description="Seconds a model is skipped after tripping the circuit breaker.",
    )
    budget_capacity: float = Field(
        DEFAULT_BUDGET_CAPACITY,
        description=(
            "Token-bucket retry budget. Retries stop once the bucket drops to "
            "half capacity, so a sustained outage fails fast instead of storming."
        ),
    )
    doom_loop_threshold: int = Field(
        DEFAULT_DOOM_LOOP_THRESHOLD,
        description=(
            "Halt cleanly when the model repeats the same tool + identical input "
            "this many times in a row (no progress). 0 disables."
        ),
    )


class AgentConfig(BaseModel):
    """Sub-agent hypervisor settings."""

    model_config = ConfigDict(
        validate_default=True,
        json_schema_extra={"title": "Agent", "x-group": "agent", "x-order": 1},
    )

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
    model_tiers: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Coarse model-cost tier -> concrete model id map (e.g. "
            "{'economy': 'anthropic/claude-haiku-4-5', 'frontier': "
            "'anthropic/claude-opus-4-6'}), resolved by a spawned agent's "
            "DelegationContract.model_tier. An explicit spawn "
            "model arg or an agent_type's configured model always wins; a "
            "declared tier with no map entry is a silent no-op fallthrough."
        ),
    )
    max_iters: int = Field(
        30,
        deprecated=True,
        description=(
            "Deprecated. The tool-use loop now runs until natural completion "
            "(model returns text without tool calls). This field is retained "
            "for API backward compatibility but is not enforced."
        ),
    )
    sub_agent_max_steps: int = Field(
        10,
        deprecated=True,
        description=(
            "Deprecated. Sub-agents now run until natural completion. "
            "This field is retained for API backward compatibility but "
            "is not enforced. Safety is provided by session_step_budget, "
            "stall detection, and LLM timeouts."
        ),
    )
    session_step_budget: int = Field(
        0,
        description=(
            "Ceiling on total tool-execution steps across every agent in a "
            "session (root + all sub-agents combined), enforced by the "
            "hypervisor: a warning is injected as the budget nears, and the "
            "run hard-stops at exhaustion. 0 = unlimited."
        ),
    )
    attestation_enabled: bool = Field(
        True,
        description=(
            "Record a best-effort provenance hash chain over "
            "every spawn/terminal transition in a session's agent tree — "
            "bounded scalars + a contract snapshot + a summary FINGERPRINT "
            "only, never task text or raw summary content. Additive and "
            "never fatal: a failed or absent chain degrades to the historical "
            "no-attestation behaviour. Default ON; this is the kill switch."
        ),
    )
    default_workspace_mode: str = Field(
        "full_access",
        description=(
            "Root filesystem-containment tier every session starts at, "
            "narrowed per sub-agent by spawn_agent's workspace_mode. One "
            "of 'read_only' (reads confined to the workspace, no writes), "
            "'workspace_write' (reads + writes confined to the workspace), or "
            "'full_access' (no path restriction; default, historical behaviour). "
            "Only bites when workspace_enforcement is on."
        ),
        examples=["full_access", "workspace_write", "read_only"],
    )
    workspace_enforcement: bool = Field(
        False,
        description=(
            "Master switch for workspace_mode filesystem containment. "
            "OFF by default (staged): while off, every path resolves "
            "through the historical tenant-union allowlist regardless of an "
            "agent's workspace_mode, so the tiers are carried + narrowed but "
            "never enforced. Flip on to collapse each contained agent's reachable "
            "paths to its own workspace root + Mewbo scratch."
        ),
    )
    stall_threshold_s: float = Field(
        120.0,
        description=(
            "Seconds of no tool-execution progress before the watchdog "
            "flags an agent as stalled and injects an NL warning into its "
            "message queue."
        ),
    )
    stall_check_interval_s: float = Field(
        30.0,
        description="Seconds between watchdog stall-detection sweeps.",
    )
    write_progress_signal_step_threshold: int = Field(
        DEFAULT_WRITE_PROGRESS_THRESHOLD,
        description=(
            "Consecutive non-write tool-execution steps before the "
            "write-progress signal fires telemetry for a write-capable "
            "agent. 0 disables."
        ),
    )
    write_progress_signal_event_interval: int = Field(
        DEFAULT_WRITE_PROGRESS_EVENT_INTERVAL,
        description=(
            "Steps between repeat write-progress signal events once the threshold is crossed."
        ),
    )
    write_progress_signal_max_events: int = Field(
        DEFAULT_WRITE_PROGRESS_MAX_EVENTS,
        description="Maximum write-progress signal events emitted before it goes quiet.",
    )
    write_progress_signal_reminder_enabled: bool = Field(
        False,
        description=(
            "When the write-progress signal fires, also inject a "
            "criterion-blind objective-restatement reminder (states the "
            "task goal only — never the signal or its criteria). Default "
            "off: telemetry alone is the observe-only default."
        ),
    )
    verification_enabled: bool = Field(
        False,
        description=(
            "Master switch for verifier-gated completion. OFF by default "
            "(staged): while off, a spawn's verification spec is carried but "
            "never run, so every natural completion is accepted exactly as "
            "historically. Flip on to gate a write-capable agent's claimed "
            "completion behind a ground-truth command check before its text "
            "is accepted."
        ),
    )
    verification_max_retries: int = Field(
        2,
        description=(
            "Maximum times a failed completion verifier re-drives the agent "
            "before its text is accepted, honestly flagged verification_failed. "
            "Clamped to [0, 10] and bounded ALSO by the step/wall budget, "
            "whichever is tighter. 0 = one check, no retry."
        ),
    )
    verification_timeout_s: float = Field(
        DEFAULT_VERIFICATION_TIMEOUT,
        description=(
            "Ceiling in seconds on a single verifier subprocess; a spawn's "
            "per-spec timeout_s is clamped down to this at run. Clamped to "
            "[1, 600]."
        ),
    )
    llm_call_timeout: float = Field(
        DEFAULT_TIMEOUT,
        description=(
            "Ceiling in seconds for a single model.ainvoke() call. "
            "Covers extended-thinking models (raised from 60s because bare "
            "timeouts were the largest single failure class). On timeout, the call is "
            "retried up to llm_call_retries times before cascading to "
            "fallback models."
        ),
    )
    llm_call_retries: int = Field(
        DEFAULT_PRIMARY_RETRIES,
        description=(
            "Maximum attempts for the primary model before cascading to "
            "fallback models (default 2 = one try + one retry). Each fallback "
            "model gets retry.fallback_retries attempts. A rescue model that "
            "wins is pinned for the rest of the run. "
            "Backoff/budget/circuit-breaker live under agent.retry."
        ),
    )

    @field_validator("llm_call_timeout", mode="before")
    @classmethod
    def _llm_call_timeout_env(cls, value: Any) -> Any:
        # Deployments where a single model call legitimately runs long (slow
        # local inference, a saturated proxy) need to lift the ceiling without
        # editing a config file. Invalid values fall through to pydantic's own
        # float coercion error.
        env = os.environ.get("MEWBO_AGENT_LLM_CALL_TIMEOUT")
        return env if env else value

    @field_validator("attestation_enabled", mode="before")
    @classmethod
    def _normalize_attestation_enabled(cls, value: Any) -> bool:
        return _coerce_bool(value, default=True)

    @field_validator("default_workspace_mode", mode="before")
    @classmethod
    def _normalize_default_workspace_mode(cls, value: Any) -> str:
        # Unknown / malformed collapses to the widest (no-op) tier — the same
        # unknown → widest rule the AgentContext narrowing applies, so a typo
        # never silently CONTAINS a deployment that meant full access.
        if value is None:
            return "full_access"
        normalized = str(value).strip().lower()
        if normalized in {"read_only", "workspace_write", "full_access"}:
            return normalized
        return "full_access"

    @field_validator("workspace_enforcement", mode="before")
    @classmethod
    def _normalize_workspace_enforcement(cls, value: Any) -> bool:
        return _coerce_bool(value, default=False)

    @field_validator("verification_enabled", mode="before")
    @classmethod
    def _normalize_verification_enabled(cls, value: Any) -> bool:
        return _coerce_bool(value, default=False)

    @field_validator("verification_max_retries", mode="before")
    @classmethod
    def _clamp_verification_max_retries(cls, value: Any) -> int:
        # Coerce-and-clamp: a malformed value collapses to the default rather
        # than failing config load; the bound keeps a runaway retry count from
        # starving the step/wall budget.
        try:
            return max(0, min(10, int(value)))
        except (TypeError, ValueError):
            return 2

    @field_validator("verification_timeout_s", mode="before")
    @classmethod
    def _clamp_verification_timeout(cls, value: Any) -> float:
        try:
            return max(1.0, min(600.0, float(value)))
        except (TypeError, ValueError):
            return DEFAULT_VERIFICATION_TIMEOUT

    retry: RetryConfig = Field(
        default_factory=lambda: RetryConfig.model_validate({}),
        description="Automatic LLM-call retry / fallback resilience knobs.",
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

    @field_validator("session_step_budget", mode="before")
    @classmethod
    def _normalize_session_step_budget(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 0
        return max(parsed, 0)

    @field_validator("stall_threshold_s", mode="before")
    @classmethod
    def _normalize_stall_threshold_s(cls, value: Any) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return 120.0
        return parsed if parsed > 0 else 120.0

    @field_validator("stall_check_interval_s", mode="before")
    @classmethod
    def _normalize_stall_check_interval_s(cls, value: Any) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return 30.0
        return parsed if parsed > 0 else 30.0

    @field_validator("write_progress_signal_step_threshold", mode="before")
    @classmethod
    def _normalize_write_progress_step_threshold(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return DEFAULT_WRITE_PROGRESS_THRESHOLD
        return max(parsed, 0)

    @field_validator("write_progress_signal_event_interval", mode="before")
    @classmethod
    def _normalize_write_progress_event_interval(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return DEFAULT_WRITE_PROGRESS_EVENT_INTERVAL
        return max(parsed, 1)

    @field_validator("write_progress_signal_max_events", mode="before")
    @classmethod
    def _normalize_write_progress_max_events(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return DEFAULT_WRITE_PROGRESS_MAX_EVENTS
        return max(parsed, 0)

    @field_validator(
        "allowed_models",
        "default_denied_tools",
        "plan_mode_shell_allowlist",
        mode="before",
    )
    @classmethod
    def _normalize_string_lists(cls, value: Any) -> list[str]:
        return _coerce_list(value)

    @field_validator("model_tiers", mode="before")
    @classmethod
    def _normalize_model_tiers(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        normalized: dict[str, str] = {}
        dropped: list[Any] = []
        for k, v in value.items():
            if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                normalized[k.strip()] = v.strip()
            else:
                dropped.append(k)
        if dropped:
            _logger.warning("agent.model_tiers: dropping malformed entries %r", dropped)
        return normalized


class MongoDBConfig(BaseModel):
    """MongoDB connection settings."""

    # validate_default=True so the env-override validators below run even when
    # the field falls back to its default (the common `model_validate({})`
    # path); without it the MEWBO_MONGODB_* overrides silently never applied.
    model_config = ConfigDict(validate_default=True, json_schema_extra={"title": "MongoDB"})

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

    model_config = ConfigDict(
        validate_default=True,
        json_schema_extra={
            "title": "Storage",
            "x-group": "server",
            "x-order": 2,
            "x-advanced": True,
        },
    )

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


def _triggers_config_default() -> TriggersConfig:
    return TriggersConfig.model_validate({})


class WikiEmbeddingConfig(BaseModel):
    """Embedding settings for the wiki indexer."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"title": "Embedding"})

    enabled: bool = Field(
        True,
        description=(
            "When false, ``wiki_build_graph`` skips embedding generation and "
            "retrieval falls back to BM25 + graph traversal only."
        ),
        examples=[True, False],
    )
    model: str = Field(
        "openai/text-embedding-3-small",
        description=(
            "Embedding model ID routed through the LLM proxy. Must support "
            "the OpenAI ``/v1/embeddings`` shape (LiteLLM normalises Gemini "
            "and others to this shape). Pin a fast model here to speed up "
            "indexing, since embedding is per-node and runs synchronously."
        ),
        examples=["openai/gemini-embedding-001", "openai/text-embedding-3-large"],
    )
    # No ``dimensions`` knob — the Embedder reads ``len(vector)`` off the
    # actual response, which is always accurate. LangChain's
    # ``OpenAIEmbeddings`` only sends a ``dimensions`` parameter when the
    # caller wants truncation (OpenAI v3 family only); we don't expose
    # that here to keep the surface small.
    batch_size: int = Field(
        64,
        description=(
            "Number of graph nodes embedded per API call during indexing. "
            "Higher values index faster but send larger requests; lower it if "
            "you hit the embedding provider's rate or payload limits."
        ),
        examples=[32, 64, 128],
        gt=0,
    )


class WikiMemoryConfig(BaseModel):
    """Knobs for the multiplex memory layer (atomic insights over the graph)."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"title": "Memory"})

    enabled: bool = Field(
        True, description="Master switch for the memory layer (gates wiki_submit_insight)."
    )
    model: str = Field(
        "",
        description=(
            "Chat model for condense + LLM dedup on the human/REST/MCP path. "
            "Empty → falls back to default_qa_model, then default_model."
        ),
    )
    max_insight_chars: int = Field(200, description="Hard cap on a memory note's length.")
    max_anchors: int = Field(8, description="Max code anchors per note.")
    dedup_k: int = Field(5, description="kNN candidate window for fuzzy + LLM dedup tiers.")
    dedup_cosine: float = Field(0.6, description="Cosine floor for the LLM dedup tier.")
    fuzzy_jaccard: float = Field(0.85, description="Jaccard floor for the fuzzy dedup tier.")
    fusion_w_ppr: float = Field(
        0.1,
        description=(
            "Weight applied to a code node's score when it's surfaced only by "
            "following a memory note's anchor rather than direct text/code "
            "search. Raise it to rank memory-anchored context higher relative "
            "to direct hits; lower it toward 0 to favor direct hits."
        ),
    )
    hub_degree: int = Field(50, description="Degree above which an anchor is hub-damped.")
    expansion_hops: int = Field(1, description="Structural hops to expand from an anchor.")


class WikiRefreshConfig(BaseModel):
    """Thresholds for the on-demand incremental refresh."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"title": "Refresh"})

    default_mode: Literal["auto", "full", "incremental"] = Field(
        "auto", description="Default re-index strategy when none is requested."
    )
    closure_max_depth: int = Field(4, description="Reverse-dependency closure depth cap.")
    drift_keep: float = Field(0.90, description="Cosine ≥ this keeps a memory anchor (no LLM).")
    drift_invalidate: float = Field(0.75, description="Cosine < this invalidates a memory anchor.")
    page_keep: float = Field(0.05, description="Doc staleness < this → keep.")
    page_edit: float = Field(0.35, description="Doc staleness < this → edit.")
    page_regen: float = Field(0.70, description="Doc staleness ≥ this → regenerate + review.")
    new_page_min: int = Field(5, description="Uncovered public symbols to propose a new page.")
    require_scope_confirm: bool = Field(
        False, description="Require a human gate on the scope preview before the act phase."
    )


class WikiConfig(BaseModel):
    """Operator-facing knobs for the wiki subsystem."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"title": "Wiki", "x-group": "workspace", "x-order": 2},
    )

    default_model: str = Field(
        "",
        description=(
            "Model the wiki picker pre-selects for indexing (the wizard). "
            "Overrides ``llm.default_model`` for the wizard. Empty string "
            "means: fall back to ``llm.default_model``."
        ),
        examples=["openai/gpt-5.4-mini"],
    )
    default_qa_model: str = Field(
        "",
        description=(
            "Model the Q&A composer pre-selects. Typically smaller/faster "
            "than ``default_model`` because Q&A is a tight read-only loop "
            "where latency matters more than depth. Empty string means: "
            "fall back to ``default_model``, then to ``llm.default_model``."
        ),
        examples=["openai/gpt-5.4-nano"],
    )
    default_depth: Literal["", "comprehensive", "concise"] = Field(
        "",
        description=(
            "Indexing depth the wizard pre-selects. Empty string means: use "
            "the wizard's own default (``comprehensive``)."
        ),
    )
    default_language: str = Field(
        "",
        description=(
            "Language code the wizard pre-selects (e.g. ``en``, ``es``). "
            "Empty string means: use the wizard's own default."
        ),
    )
    embedding: WikiEmbeddingConfig = Field(
        default_factory=lambda: WikiEmbeddingConfig.model_validate({}),
        description="Embedding settings for the wiki indexer.",
    )
    memory: WikiMemoryConfig = Field(
        default_factory=lambda: WikiMemoryConfig.model_validate({}),
        description="Multiplex memory-layer knobs (atomic insights over the graph).",
    )
    refresh: WikiRefreshConfig = Field(
        default_factory=lambda: WikiRefreshConfig.model_validate({}),
        description="On-demand incremental-refresh thresholds.",
    )


def _wiki_config_default() -> WikiConfig:
    return WikiConfig.model_validate({})


class ScgTierModelsConfig(BaseModel):
    """Per-tier model mapping: the tier picks the brain, not just the budget.

    A tier maps to the LLM that drives the whole run (orchestrator session AND
    its probe sub-agents, which inherit the session model). An empty string
    falls back to ``llm.default_model``. An explicit per-request ``model``
    override (where the endpoint offers one) always wins over the tier map.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"title": "Tier models"})

    fast: str = Field(
        "openai/gpt-oss-120b",
        description="Model for `fast` tier runs (the tier still sets the low-latency budget).",
    )
    auto: str = Field(
        "openai/gpt-oss-120b",
        description="Model for `auto` tier runs (the tier still sets the balanced budget).",
    )
    deep: str = Field(
        "openai/gpt-oss-120b",
        description="Model for `deep` tier runs (the tier still sets the exhaustive budget).",
    )


class ScgTraversalConfig(BaseModel):
    """Traversal defaults for SCG search (the per-run tier budget knob)."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"title": "Traversal"})

    default_tier: Literal["fast", "auto", "deep"] = Field(
        "auto",
        description=(
            "Default search tier, one budget knob over decomposition depth "
            "and probe fan-out. Overridable per run."
        ),
    )
    tier_models: ScgTierModelsConfig = Field(
        default_factory=lambda: ScgTierModelsConfig.model_validate({}),
        description="Which LLM each search tier runs on (fast/auto/deep).",
    )


class ScgConfig(BaseModel):
    """Operator-facing knobs for the Source Capability Graph (agentic search)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"title": "SCG", "x-group": "workspace", "x-order": 3},
    )

    enabled: bool = Field(
        False,
        description=(
            "Master switch for the SCG feature (source mapping and "
            "orchestrated agentic-search runs)."
        ),
    )
    traversal: ScgTraversalConfig = Field(
        default_factory=lambda: ScgTraversalConfig.model_validate({}),
        description="Traversal defaults (the per-run search tier).",
    )


def _scg_config_default() -> ScgConfig:
    return ScgConfig.model_validate({})


# ---------------------------------------------------------------------------
# Curation annotation contract (drives the faceted Settings UI)
# ---------------------------------------------------------------------------
# Section models carry presentation/security metadata in their JSON schema via
# ``model_config = ConfigDict(json_schema_extra=...)``. Pydantic emits these on
# the corresponding ``$defs/<Class>`` node (a submodel field is a bare
# ``$ref`` and drops sibling ``json_schema_extra``, so the metadata MUST live on
# the class, not the field). The API's ``ConfigSchemaView`` reads them.
#
# Class-level (``$defs/<Class>``):
#   x-group     -> facet id. The union is CLOSED and mirrored, id for id, by the
#                  console's ``FacetId`` (``apps/mewbo_console/src/components/
#                  settings/facets.ts``):
#
#                      "models" | "agent" | "plugins" | "automation"
#                      | "integrations" | "interface" | "server" | "security"
#                      | "workspace"
#
#                  A section with no x-group — or with one the console doesn't
#                  declare — is SILENTLY bucketed into the "other" fallback
#                  facet: no error, no warning, the section simply disappears
#                  from the facet a user would look in. So the two sides move in
#                  ONE change, always. Keep this list in lockstep with
#                  ``facets.ts`` when a facet is added, renamed, or removed.
#   x-order     -> ordering within the facet (1-based).
#   x-advanced  -> whole section is power-user-only; hidden by default.
#
# Field-level (``$defs/<Class>/properties/<field>``):
#   x-protected -> never read AND never written through the API; stripped from
#                  the public schema and from value dumps; rejected in PATCH.
#                  Used for host paths and the API master token.
#   x-secret    -> write-only: settable via PATCH but never read back. Kept in
#                  the public schema marked ``writeOnly: true``; its value is
#                  stripped from dumps. The API reports is-set status separately.
#   x-advanced  -> single field is power-user-only; hidden by default.
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
    wiki: WikiConfig = Field(
        default_factory=_wiki_config_default,
        description="Wiki subsystem defaults and embedding behavior.",
    )
    scg: ScgConfig = Field(
        default_factory=_scg_config_default,
        description="Source Capability Graph feature gate and search-tier default.",
    )
    hooks: HooksConfig = Field(
        default_factory=_hooks_config_default,
        description="External hooks fired during the session lifecycle (command or http).",
    )
    plugins: PluginsConfig = Field(
        default_factory=_plugins_config_default,
        description=(
            "How Mewbo finds, installs, and enables plugins.\n\n"
            "A plugin extends what the agent can do. It brings its own skills, agent "
            "definitions, lifecycle hooks, and MCP tools, and those hooks run on this "
            "machine, so install only what you trust. Marketplaces are the catalogs "
            "Mewbo looks in for plugins to offer you; the list above shows what is "
            "installed today and what is available to install."
        ),
    )
    triggers: TriggersConfig = Field(
        default_factory=_triggers_config_default,
        description=(
            "Ceilings on the triggers that start a session without you.\n\n"
            "A trigger is a reverse invocation. Instead of you opening a session, a "
            "session asks to be woken later and Mewbo wakes it, on its own, with "
            "nobody watching. There are five kinds: `time.at` fires once at a set "
            "moment, `time.cron` fires on a repeating schedule, `ci.workflow` fires "
            "when a CI run finishes, `forge.pr` fires when a pull request changes, "
            "and `webhook` fires when something outside calls in. The agent arms a "
            "trigger from inside a session, and the triggers list above is where you "
            "watch, pause, and cancel what it armed. The settings here are the limits "
            "all of that has to stay inside."
        ),
    )
    channels: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Chat platform channel adapters (nextcloud-talk, slack, etc.).",
        json_schema_extra={"x-group": "integrations", "x-order": 4},
    )
    projects: dict[str, ProjectConfig] = Field(
        default_factory=_projects_config_default,
        description=(
            "Directories you've already created, registered here by hand under "
            'a short name; sessions reference them by that name (e.g. `"'
            'project": "<key>"`). Distinct from Mewbo-managed ("virtual") '
            "projects, which the API creates and owns itself and which sessions "
            "reference as `managed:<project_id>`: entries here are never "
            "created, modified, or deleted by Mewbo, only pointed at."
        ),
        json_schema_extra={"x-group": "workspace", "x-order": 1},
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


def effective_fallback_models() -> list[str]:
    """Resolve the active fallback model chain honoring the opt-in policy.

    Precedence: when ``llm.fallback.enabled`` is set, use ``llm.fallback.models``
    (falling back to the legacy ``llm.fallback_models`` if the typed list is
    empty). When fallback is disabled, a non-empty legacy ``llm.fallback_models``
    is still honored for backward compatibility; otherwise there is no fallback.
    """
    enabled = bool(get_config_value("llm", "fallback", "enabled", default=False))
    typed = list(get_config_value("llm", "fallback", "models", default=[]) or [])
    legacy = list(get_config_value("llm", "fallback_models", default=[]) or [])
    if enabled:
        return typed or legacy
    return legacy


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
