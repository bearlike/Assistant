#!/usr/bin/env python3
"""Token budgeting utilities."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass

import tomllib

from meeseeks_core.common import num_tokens_from_string
from meeseeks_core.types import EventRecord


@dataclass(frozen=True)
class TokenBudget:
    """Token accounting snapshot used to decide compaction."""

    total_tokens: int
    summary_tokens: int
    event_tokens: int
    context_window: int
    remaining_tokens: int
    utilization: float
    threshold: float

    @property
    def needs_compact(self) -> bool:
        """Return True when utilization exceeds the configured threshold."""
        return self.utilization >= self.threshold


def _parse_context_from_model(model_name: str) -> int | None:
    """Parse a model context window size from a model name string."""
    matches = re.findall(r"(\d+(?:\.\d+)?)([km])", model_name.lower())
    if not matches:
        return None
    values = []
    for value, suffix in matches:
        magnitude = float(value)
        if suffix == "k":
            magnitude *= 1000
        elif suffix == "m":
            magnitude *= 1_000_000
        values.append(int(magnitude))
    return max(values) if values else None


def _load_context_overrides() -> dict[str, int]:
    """Load context window overrides from an env var or JSON/TOML file."""
    env_value = os.getenv("MESEEKS_MODEL_CONTEXT_WINDOWS")
    if not env_value:
        return {}
    if os.path.exists(env_value):
        with open(env_value, "rb") as handle:
            if env_value.endswith(".toml"):
                data = tomllib.load(handle)
            else:
                data = json.load(handle)
    else:
        data = json.loads(env_value)
    return {str(key): int(value) for key, value in data.items()}


def get_context_window(model_name: str | None) -> int:
    """Resolve the context window for a model name or default."""
    default_window = int(os.getenv("MESEEKS_DEFAULT_CONTEXT_WINDOW", "128000"))
    if not model_name:
        return default_window
    overrides = _load_context_overrides()
    if model_name in overrides:
        return overrides[model_name]
    parsed = _parse_context_from_model(model_name)
    if parsed:
        return parsed
    return default_window


def _event_to_text(event: EventRecord) -> str:
    """Extract a representative text string from an event payload."""
    payload = event.get("payload", "")
    if isinstance(payload, dict):
        payload_data = dict(payload)
        for key in ("text", "message", "result"):
            if key in payload_data:
                return str(payload_data[key])
        return json.dumps(payload_data, sort_keys=True)
    return str(payload)


def estimate_event_tokens(events: Iterable[EventRecord]) -> int:
    """Estimate total tokens for a sequence of events."""
    texts = [_event_to_text(event) for event in events]
    joined = "\n".join(text for text in texts if text)
    if not joined:
        return 0
    return num_tokens_from_string(joined)


def estimate_summary_tokens(summary: str | None) -> int:
    """Estimate token usage for the stored summary."""
    if not summary:
        return 0
    return num_tokens_from_string(summary)


def get_token_budget(
    events: Iterable[EventRecord],
    summary: str | None,
    model_name: str | None,
    threshold: float | None = None,
) -> TokenBudget:
    """Calculate token utilization and remaining context budget."""
    event_tokens = estimate_event_tokens(events)
    summary_tokens = estimate_summary_tokens(summary)
    total_tokens = event_tokens + summary_tokens
    context_window = get_context_window(model_name)
    remaining_tokens = max(context_window - total_tokens, 0)
    if threshold is None:
        threshold = float(os.getenv("MESEEKS_AUTO_COMPACT_THRESHOLD", "0.8"))
    utilization = total_tokens / context_window if context_window else 0.0
    return TokenBudget(
        total_tokens=total_tokens,
        summary_tokens=summary_tokens,
        event_tokens=event_tokens,
        context_window=context_window,
        remaining_tokens=remaining_tokens,
        utilization=utilization,
        threshold=threshold,
    )
