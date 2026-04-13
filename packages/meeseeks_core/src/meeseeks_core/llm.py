#!/usr/bin/env python3
"""Model configuration helpers for ChatLiteLLM."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from typing import Any, Protocol, cast

from langchain_core.messages import BaseMessage

from meeseeks_core.config import get_config_value

_logger = logging.getLogger(__name__)

# Tracks api_base URLs whose /v1/model/info we've already pulled, so we register
# proxy capabilities exactly once per process per distinct base. Re-registration
# is harmless (litellm.register_model is idempotent) but the HTTP fetch is not.
_REGISTERED_PROXY_BASES: set[str] = set()

# Eagerly bind the LiteLLM capability lookup at module import — re-importing
# litellm submodules lazily inside hot helpers can blow up in tests that have
# polluted ``sys.modules`` (HA tests stub a couple of aiohttp helpers, which
# breaks litellm's lazy http_handler load on the second import).  None means
# "litellm not installed"; we treat that as "no caching" downstream.
try:
    from litellm.utils import supports_prompt_caching as _litellm_supports_prompt_caching
except Exception:  # pragma: no cover - dependency guard
    _litellm_supports_prompt_caching = None  # type: ignore[assignment]


class ChatModel(Protocol):
    """Protocol for LangChain-compatible chat models."""

    def invoke(
        self, input_data: object, config: object | None = None, **kwargs: object
    ) -> BaseMessage:
        """Invoke the model synchronously."""

    async def ainvoke(
        self, input_data: object, config: object | None = None, **kwargs: object
    ) -> BaseMessage:
        """Invoke the model asynchronously."""


def _normalize_model_list(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item).strip().lower() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        return [entry.strip().lower() for entry in raw.split(",") if entry.strip()]
    return []


def _strip_provider(model_name: str | None) -> str:
    if not model_name:
        return ""
    return model_name.split("/", 1)[-1].strip().lower()


def _matches_model_list(model_name: str, entries: Iterable[str]) -> bool:
    for entry in entries:
        if entry.endswith("*") and model_name.startswith(entry[:-1]):
            return True
        if model_name == entry:
            return True
    return False


def model_prefers_structured_patch(model_name: str | None) -> bool:
    """Return True if the model works better with the per-file structured_patch tool.

    GPT-5-class, o3/o4, and Codex models use structured JSON tool calls that
    map naturally to ``file_edit_tool`` (structured_patch).  Claude and Gemini
    are trained on diff/patch text formats and work better with
    ``aider_edit_block_tool`` (search_replace_block).

    The config key ``llm.structured_patch_models`` overrides the built-in list.
    """
    if not model_name:
        return False
    normalized = _strip_provider(model_name)
    raw = model_name.lower()
    allowlist = _normalize_model_list(
        get_config_value("llm", "structured_patch_models", default=[])
    )
    if _matches_model_list(raw, allowlist) or _matches_model_list(normalized, allowlist):
        return True
    # Built-in: GPT-5+, o3/o4 class, Codex models → structured_patch
    # Claude, Gemini, other open-weights → search_replace_block (aider-style)
    return (
        normalized.startswith("gpt-5")
        or normalized.startswith("o3")
        or normalized.startswith("o4")
        or normalized.startswith("codex")
        or "gpt-4" in normalized
    )


def register_proxy_model_capabilities(
    api_base: str | None,
    api_key: str | None,
    *,
    timeout: float = 5.0,
) -> int:
    """Pull proxy ``/v1/model/info`` and register advertised models.

    Hydrates LiteLLM's local ``model_cost`` map with the routes the proxy
    operator defined.

    This is the bridge that lets ``litellm.utils.supports_prompt_caching`` (and
    every other ``supports_*`` helper) report accurately for proxy-fronted
    custom model names — the SDK only consults its bundled ``model_cost.json``
    by default, which doesn't know about routes the proxy operator defined.

    Idempotent per process: each distinct ``api_base`` is fetched at most once.
    Failures are logged and swallowed — Stage 1's per-model gate just stays
    conservative, never crashes.

    Returns the number of models newly registered (0 if cached, no-op, or
    error).
    """
    if not api_base:
        return 0
    base = api_base.rstrip("/")
    if base in _REGISTERED_PROXY_BASES:
        return 0
    _REGISTERED_PROXY_BASES.add(base)
    try:
        import httpx
        import litellm

        url = base + "/model/info"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        resp = httpx.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        entries = (resp.json() or {}).get("data") or []
    except Exception as exc:
        _logger.info(
            "Skipping proxy model_info registration for %s: %s",
            base,
            exc,
        )
        return 0

    registered = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("model_name")
        info = entry.get("model_info") or {}
        if not isinstance(name, str) or not isinstance(info, dict) or not info:
            continue
        info.setdefault("litellm_provider", "openai")
        info.setdefault("mode", "chat")
        try:
            litellm.register_model({f"openai/{name}": info})
            registered += 1
        except Exception as exc:  # pragma: no cover - litellm guard
            _logger.debug("register_model failed for %s: %s", name, exc)
    return registered


def model_supports_prompt_caching(model_name: str | None) -> bool:
    """Return True when LiteLLM reports the model supports prompt caching.

    Single source of truth: ``litellm.utils.supports_prompt_caching``, which
    reads the bundled ``model_cost.json`` (extensible at runtime via
    ``litellm.register_model``).  Returns False on unknown models or any
    lookup error so the caller can skip caching gracefully without crashing
    the agent loop.
    """
    if not model_name or _litellm_supports_prompt_caching is None:
        return False
    try:
        return bool(_litellm_supports_prompt_caching(_strip_provider(model_name)))
    except Exception:
        return False


def model_supports_reasoning_effort(model_name: str | None) -> bool:
    """Return True if the model is known to support reasoning_effort.

    LiteLLM translates reasoning_effort per-provider:
    - Claude → output_config.effort
    - Gemini → thinking budget_tokens or thinking_level
    - OpenAI (o3/gpt-5) → native reasoning_effort
    """
    if not model_name:
        return False
    raw = model_name.lower()
    normalized = _strip_provider(model_name)
    allowlist = _normalize_model_list(
        get_config_value("llm", "reasoning_effort_models", default=[])
    )
    if _matches_model_list(raw, allowlist) or _matches_model_list(normalized, allowlist):
        return True
    return (
        normalized.startswith("gpt-5")
        or normalized.startswith("o3")
        or "claude" in normalized
        or "gemini" in normalized
    )


def resolve_reasoning_effort(model_name: str | None) -> str | None:
    """Resolve the reasoning effort for a model.

    Returns the configured value if set, otherwise ``None`` (let the
    provider decide).  Only returns a value when the model supports
    the parameter *and* a value is explicitly configured.
    LiteLLM translates this per-provider:
    - Claude: output_config.effort
    - Gemini: thinking budget_tokens or thinking_level
    - OpenAI: native reasoning_effort
    """
    if not model_supports_reasoning_effort(model_name):
        return None
    configured = get_config_value("llm", "reasoning_effort", default="")
    if isinstance(configured, str) and configured.strip():
        return configured.strip().lower()
    env = os.environ.get("MEESEEKS_REASONING_EFFORT", "").strip().lower()
    if env:
        return env
    return None


def _resolve_litellm_model(
    model_name: str,
    openai_api_base: str | None,
    proxy_prefix: str = "openai",
) -> str:
    if not openai_api_base:
        return model_name
    prefix = proxy_prefix.strip().strip("/") or "openai"
    if model_name.startswith(f"{prefix}/"):
        return model_name
    return f"{prefix}/{model_name}"


def build_chat_model(
    model_name: str,
    *,
    openai_api_base: str | None = None,
    api_key: str | None = None,
) -> ChatModel:
    """Build a ChatLiteLLM model with reasoning-effort compatibility.

    ``openai_api_base`` and ``api_key`` default to ``llm.api_base`` and
    ``llm.api_key`` from config when ``None``. Pass them explicitly only
    to override the configured values (e.g. tests, multi-tenant routing).
    """
    try:
        from langchain_litellm import ChatLiteLLM
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ImportError("langchain-litellm is required to build ChatLiteLLM") from exc

    if openai_api_base is None:
        openai_api_base = str(get_config_value("llm", "api_base", default="") or "")
    if api_key is None:
        api_key = str(get_config_value("llm", "api_key", default="") or "")
    proxy_prefix = (
        str(get_config_value("llm", "proxy_model_prefix", default="openai") or "openai")
        .strip()
        .strip("/")
        or "openai"
    )

    # Hydrate proxy capabilities once per process per distinct api_base, so the
    # cache-support gate below sees the proxy's advertised model_info instead
    # of just LiteLLM's bundled allowlist. No-op when api_base is unset.
    if openai_api_base:
        register_proxy_model_capabilities(openai_api_base, api_key)

    reasoning_effort = resolve_reasoning_effort(model_name)

    model_kwargs: dict[str, Any] = {
        "drop_params": True,  # Must be in model_kwargs to reach litellm.acompletion();
        # ChatLiteLLM has no drop_params field so top-level kwarg is silently ignored.
    }
    if reasoning_effort is not None:
        model_kwargs["reasoning_effort"] = reasoning_effort

    if model_supports_prompt_caching(model_name):
        # LiteLLM's hook attaches the right native marker for whichever provider
        # the call routes to (Anthropic content-block cache_control, Bedrock
        # with ttl-sanitisation).  We only declare *where* — at the system
        # message — and let LiteLLM own the per-provider syntax.  Auto-cache
        # providers (OpenAI) silently drop the kwarg and still surface savings
        # via usage_metadata.input_token_details.cache_read.
        model_kwargs["cache_control_injection_points"] = [
            {"location": "message", "role": "system", "control": {"type": "ephemeral"}}
        ]

    kwargs: dict[str, Any] = {
        "model": _resolve_litellm_model(model_name, openai_api_base, proxy_prefix),
        # Disable LiteLLM's built-in retries — the ToolUseLoop manages
        # retries itself with per-attempt timeouts and visible retry events.
        "request_timeout": float(get_config_value("llm", "request_timeout", default=60.0) or 60.0),
        "max_retries": 0,
    }
    if openai_api_base:
        kwargs["api_base"] = openai_api_base
    if api_key:
        kwargs["api_key"] = api_key
    if model_kwargs:
        kwargs["model_kwargs"] = model_kwargs

    return cast(ChatModel, ChatLiteLLM(**kwargs))


def sanitize_tool_schema(schema: Any) -> Any:
    """Recursively fix JSON Schema issues that strict LLM providers reject.

    Runs at the ``specs_to_langchain_tools`` funnel so every tool schema —
    MCP, built-in, plugin — is covered.  Fixes are valid JSON Schema, safe
    for all providers.

    Current fixes:
    - ``array`` without ``items`` → add ``"items": {}`` (required by OpenAI).
    """
    if not isinstance(schema, dict):
        return schema

    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key in ("properties", "$defs", "definitions") and isinstance(value, dict):
            result[key] = {k: sanitize_tool_schema(v) for k, v in value.items()}
        elif key in ("additionalProperties", "items") and isinstance(value, dict):
            result[key] = sanitize_tool_schema(value)
        elif key in ("anyOf", "oneOf", "allOf", "prefixItems", "items") and isinstance(value, list):
            result[key] = [sanitize_tool_schema(v) for v in value]
        else:
            result[key] = value

    # Array without items → add permissive default.
    schema_type = result.get("type")
    is_array = schema_type == "array" or (isinstance(schema_type, list) and "array" in schema_type)
    if is_array and "items" not in result:
        result["items"] = {}

    return result


def specs_to_langchain_tools(specs: list[object]) -> list[dict[str, Any]]:
    """Convert ToolSpecs to LangChain bind_tools() format.

    Each spec must have ``tool_id``, ``description``, and ``metadata["schema"]``.
    Specs without a schema are silently skipped.

    Delegates to LangChain's :func:`convert_to_openai_tool` (Anthropic-format
    input) so that schema normalisation is handled by the library rather than
    hand-rolled here.
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    tools: list[dict[str, Any]] = []
    for spec in specs:
        if not getattr(spec, "enabled", True):
            continue
        metadata = getattr(spec, "metadata", None) or {}
        schema = metadata.get("schema")
        if not isinstance(schema, dict):
            continue
        # Anthropic-format dict: LangChain maps input_schema → parameters.
        tools.append(
            convert_to_openai_tool(
                {
                    "name": getattr(spec, "tool_id", ""),
                    "description": getattr(spec, "description", ""),
                    "input_schema": sanitize_tool_schema(schema),
                }
            )
        )
    return tools


__all__ = [
    "build_chat_model",
    "ChatModel",
    "model_prefers_structured_patch",
    "model_supports_prompt_caching",
    "register_proxy_model_capabilities",
    "model_supports_reasoning_effort",
    "resolve_reasoning_effort",
    "sanitize_tool_schema",
    "specs_to_langchain_tools",
]
