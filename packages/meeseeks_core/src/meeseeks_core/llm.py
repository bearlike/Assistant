#!/usr/bin/env python3
"""Model configuration helpers for ChatLiteLLM."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol, cast

from meeseeks_core.config import get_config_value


class ChatModel(Protocol):
    """Protocol for LangChain-compatible chat models."""

    def invoke(self, input_data: object, config: object | None = None, **kwargs: object) -> object:
        """Invoke the model with structured input."""


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
    """Return True if the model is known to support reasoning_effort."""
    if not model_name:
        return False
    raw = model_name.lower()
    normalized = _strip_provider(model_name)
    allowlist = _normalize_model_list(
        get_config_value("llm", "reasoning_effort_models", default=[])
    )
    if _matches_model_list(raw, allowlist) or _matches_model_list(normalized, allowlist):
        return True
    return normalized.startswith("gpt-5")


def resolve_reasoning_effort(model_name: str | None) -> str | None:
    """Resolve the reasoning effort to use for a model."""
    configured = get_config_value("llm", "reasoning_effort", default="")
    if isinstance(configured, str) and configured.strip():
        return configured.strip().lower()
    if not model_supports_reasoning_effort(model_name):
        return None
    normalized = _strip_provider(model_name)
    if "gpt-5-pro" in normalized:
        return "high"
    return "medium"


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
    """Build a ChatLiteLLM model with reasoning-effort compatibility."""
    try:
        from langchain_litellm import ChatLiteLLM
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ImportError("langchain-litellm is required to build ChatLiteLLM") from exc

    reasoning_effort = resolve_reasoning_effort(model_name)

    model_kwargs: dict[str, Any] = {}
    if reasoning_effort is not None:
        model_kwargs["reasoning_effort"] = reasoning_effort

    kwargs: dict[str, Any] = {
        "model": _resolve_litellm_model(model_name, openai_api_base),
    }
    if openai_api_base:
        kwargs["api_base"] = openai_api_base
    if api_key:
        kwargs["api_key"] = api_key
    if model_kwargs:
        kwargs["model_kwargs"] = model_kwargs

    return cast(ChatModel, ChatLiteLLM(**kwargs))


__all__ = [
    "build_chat_model",
    "ChatModel",
    "model_supports_reasoning_effort",
    "resolve_reasoning_effort",
]
