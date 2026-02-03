#!/usr/bin/env python3
"""Model configuration helpers for ChatOpenAI."""
from __future__ import annotations

import json
import os
from collections.abc import Iterable
from typing import Any

from core.common import get_logger

logging = get_logger(name="core.llm")


def _parse_model_list_env(name: str) -> list[str]:
    raw = os.getenv(name)
    if not raw:
        return []
    raw = raw.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(item).lower() for item in data]
    except json.JSONDecodeError:
        pass
    return [entry.strip().lower() for entry in raw.split(",") if entry.strip()]


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
    normalized = model_name.lower()
    allowlist = _parse_model_list_env("MESEEKS_REASONING_EFFORT_MODELS")
    if _matches_model_list(normalized, allowlist):
        return True
    return normalized.startswith("gpt-5")


def resolve_reasoning_effort(model_name: str | None) -> str | None:
    """Resolve the reasoning effort to use for a model."""
    env_value = os.getenv("MESEEKS_REASONING_EFFORT")
    if env_value:
        return env_value.strip().lower()
    if not model_supports_reasoning_effort(model_name):
        return None
    normalized = (model_name or "").lower()
    if "gpt-5-pro" in normalized:
        return "high"
    return "medium"


def allows_temperature(model_name: str | None, reasoning_effort: str | None) -> bool:
    """Return True when temperature can be sent for the model/effect combo."""
    if not model_name:
        return True
    normalized = model_name.lower()
    if not normalized.startswith("gpt-5"):
        return True
    if normalized.startswith(("gpt-5.1", "gpt-5.2")):
        return reasoning_effort == "none"
    return False


def build_chat_model(
    model_name: str,
    temperature: float,
    *,
    openai_api_base: str | None = None,
) -> Any:
    """Build a ChatOpenAI model with reasoning-effort compatibility."""
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ImportError("langchain-openai is required to build ChatOpenAI") from exc

    reasoning_effort = resolve_reasoning_effort(model_name)
    allow_temp = allows_temperature(model_name, reasoning_effort)
    if not allow_temp:
        logging.info(
            "Omitting temperature for model '{}' with reasoning_effort '{}'.",
            model_name,
            reasoning_effort,
        )
        temperature_value: float | None = None
    else:
        temperature_value = temperature

    model_kwargs: dict[str, Any] = {}
    if reasoning_effort is not None:
        model_kwargs["reasoning_effort"] = reasoning_effort

    kwargs: dict[str, Any] = {
        "base_url": openai_api_base,
        "model": model_name,
    }
    if temperature_value is not None:
        kwargs["temperature"] = temperature_value
    if model_kwargs:
        kwargs["model_kwargs"] = model_kwargs

    return ChatOpenAI(**kwargs)


__all__ = [
    "allows_temperature",
    "build_chat_model",
    "model_supports_reasoning_effort",
    "resolve_reasoning_effort",
]
