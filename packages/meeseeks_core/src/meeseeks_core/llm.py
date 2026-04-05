#!/usr/bin/env python3
"""Model configuration helpers for ChatLiteLLM."""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any, Protocol, cast

from langchain_core.messages import BaseMessage

from meeseeks_core.config import get_config_value


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


def _resolve_litellm_model(model_name: str, openai_api_base: str | None) -> str:
    if "/" in model_name:
        return model_name
    if openai_api_base:
        return f"openai/{model_name}"
    return model_name


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

    reasoning_effort = resolve_reasoning_effort(model_name)

    model_kwargs: dict[str, Any] = {
        "drop_params": True,  # Must be in model_kwargs to reach litellm.acompletion();
        # ChatLiteLLM has no drop_params field so top-level kwarg is silently ignored.
    }
    if reasoning_effort is not None:
        model_kwargs["reasoning_effort"] = reasoning_effort

    kwargs: dict[str, Any] = {
        "model": _resolve_litellm_model(model_name, openai_api_base),
        # Disable LiteLLM's built-in retries — the ToolUseLoop manages
        # retries itself with per-attempt timeouts and visible retry events.
        "request_timeout": float(
            get_config_value("llm", "request_timeout", default=60.0) or 60.0
        ),
        "max_retries": 0,
    }
    if openai_api_base:
        kwargs["api_base"] = openai_api_base
    if api_key:
        kwargs["api_key"] = api_key
    if model_kwargs:
        kwargs["model_kwargs"] = model_kwargs

    return cast(ChatModel, ChatLiteLLM(**kwargs))


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
                    "input_schema": schema,
                }
            )
        )
    return tools


__all__ = [
    "build_chat_model",
    "ChatModel",
    "model_supports_reasoning_effort",
    "resolve_reasoning_effort",
    "specs_to_langchain_tools",
]
