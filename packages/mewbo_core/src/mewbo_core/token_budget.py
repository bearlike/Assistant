#!/usr/bin/env python3
"""Token budgeting anchored on LiteLLM-authoritative model metadata.

Philosophy: trust the API, not estimates. LiteLLM's ``get_model_info`` is
the source of truth for each model's ``max_input_tokens``; LangChain's
``response.usage_metadata.input_tokens`` is the source of truth for what
the current prompt actually consumed. No char-count heuristics, no fake
overhead additions, no regex guessing from the model name.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from mewbo_core.common import get_logger, num_tokens_from_string
from mewbo_core.config import get_config_value
from mewbo_core.types import EventRecord

logger = get_logger(name="core.token_budget")


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
        """Return True when utilization meets or exceeds the configured threshold."""
        return self.utilization >= self.threshold


def _strip_provider_prefix(model_name: str) -> str:
    """Strip a LiteLLM routing prefix (``openai/``, ``anthropic/``, ...).

    LiteLLM's ``get_model_info`` requires the canonical model name; routing
    prefixes are proxy hints that don't exist in the model catalogue.
    """
    if "/" in model_name:
        return model_name.split("/", 1)[1]
    return model_name


@lru_cache(maxsize=128)
def _litellm_max_input_tokens(canonical_name: str) -> int | None:
    """Look up ``max_input_tokens`` from LiteLLM's model catalogue.

    Cached because the catalogue is static per process. Returns ``None``
    when the model is not in LiteLLM's database.
    """
    try:
        import litellm

        info = litellm.get_model_info(canonical_name)
        value = info.get("max_input_tokens")
        if isinstance(value, int) and value > 0:
            return value
    except Exception:
        # LiteLLM raises a bare Exception for unknown models; swallow and
        # fall back to config / defaults.
        return None
    return None


def _load_context_overrides() -> dict[str, int]:
    """Load user-supplied context window overrides from config.

    Overrides are an escape hatch for proxies that cap below the model's
    real maximum, or for models LiteLLM doesn't know yet. They are NOT
    the primary source.
    """
    overrides = get_config_value("token_budget", "model_context_windows", default={})
    if not isinstance(overrides, dict):
        return {}
    return {str(key): int(value) for key, value in overrides.items()}


def get_model_max_input_tokens(model_name: str | None) -> int:
    """Resolve the maximum input tokens for a model.

    Priority: user override -> LiteLLM catalogue -> config default.
    """
    default_window = int(get_config_value("token_budget", "default_context_window", default=128000))
    if not model_name:
        return default_window
    overrides = _load_context_overrides()
    if model_name in overrides:
        return overrides[model_name]
    canonical = _strip_provider_prefix(model_name)
    if canonical in overrides:
        return overrides[canonical]
    from_litellm = _litellm_max_input_tokens(canonical)
    if from_litellm is not None:
        return from_litellm
    return default_window


# Backwards-compatible alias. Existing call sites use get_context_window;
# the name is kept but the behaviour is now LiteLLM-anchored.
get_context_window = get_model_max_input_tokens


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
    """Estimate total tokens for a sequence of events (fallback only).

    Used only when no real ``usage_metadata`` is available (e.g. a fresh
    session before the first LLM call). After the first response lands,
    ``last_input_tokens`` from the API is the authoritative signal.
    """
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
    *,
    last_input_tokens: int | None = None,
) -> TokenBudget:
    """Calculate token utilization and remaining context budget.

    When ``last_input_tokens`` is supplied (from a real
    ``response.usage_metadata`` read), it is used as the authoritative
    total. Otherwise we fall back to estimating from events + summary.
    """
    if threshold is None:
        threshold = float(get_config_value("token_budget", "auto_compact_threshold", default=0.8))
    context_window = get_model_max_input_tokens(model_name)

    if last_input_tokens is not None and last_input_tokens > 0:
        # API-reported usage is authoritative. event_tokens/summary_tokens
        # are recomputed only for diagnostics.
        event_tokens = estimate_event_tokens(events)
        summary_tokens = estimate_summary_tokens(summary)
        total_tokens = last_input_tokens
    else:
        event_tokens = estimate_event_tokens(events)
        summary_tokens = estimate_summary_tokens(summary)
        total_tokens = event_tokens + summary_tokens

    remaining_tokens = max(context_window - total_tokens, 0)
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


def build_usage_numbers(
    events: list[EventRecord],
    root_model: str | None,
) -> dict[str, Any]:
    """Walk a transcript once and return raw usage numbers.

    Split by depth so clients can render root (hypervisor) vs sub-agents
    without conflation. Numbers only — no formatting, no color states, no
    labels. Clients format what they need.

    Returned keys (input has two semantics, output only one):

    **Context-pressure (peak) — what matters for compaction / window math.**
    ``input_tokens`` on an ``llm_call_end`` event is the prompt size for
    that call. Within a turn the prompt GROWS as tool results accumulate
    (step 1: ~13K baseline, step 11: ~27K), so summing gives a nonsense
    number that double-counts the baseline once per call. The peak (max
    across root calls) is the real context pressure:
      - ``root_peak_input_tokens``: max input_tokens seen on any depth==0 call.
      - ``sub_peak_input_tokens``: sum of per-sub-agent peaks (each sub-agent
        runs in an isolated context, so summing their peaks — not their sum
        inputs — represents "combined peak pressure of parallel sub-contexts").

    **Cumulative (billable) — what the provider charges for.**
    Sum across all calls. Useful for cost dashboards. Named with
    ``_billed_in`` suffix to make the semantic explicit:
      - ``root_input_tokens_billed``, ``sub_input_tokens_billed``.

    **Output is additive everywhere.** Each output token is produced once;
    summing is correct:
      - ``root_output_tokens``, ``sub_output_tokens``.

    Other keys: ``root_model``, ``root_max_input_tokens``,
    ``root_last_input_tokens``, ``root_utilization``,
    ``tokens_until_compact``, ``compact_threshold``,
    ``root_llm_calls``, ``sub_llm_calls``, ``sub_agent_count``,
    ``total_input_tokens_billed``, ``total_output_tokens``,
    ``compaction_count``, ``compaction_tokens_saved``.
    """
    root_input_billed = root_output = root_calls = 0
    root_peak_input = 0
    root_cache_creation = root_cache_read = root_reasoning = 0
    sub_input_billed = sub_output = sub_calls = 0
    sub_peak_per_agent: dict[str, int] = {}
    sub_cache_creation = sub_cache_read = sub_reasoning = 0
    sub_agent_ids: set[str] = set()
    compaction_count = 0
    compaction_tokens_saved = 0

    for event in events:
        etype = event.get("type")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if etype == "llm_call_end":
            depth = payload.get("depth", 0)
            in_tok = int(payload.get("input_tokens", 0) or 0)
            out_tok = int(payload.get("output_tokens", 0) or 0)
            # Cache + reasoning subtotals are 0 on legacy events (pre cache
            # capture), so accumulating is a no-op for old transcripts.
            cache_create = int(payload.get("cache_creation_input_tokens", 0) or 0)
            cache_read = int(payload.get("cache_read_input_tokens", 0) or 0)
            reasoning = int(payload.get("reasoning_output_tokens", 0) or 0)
            if depth == 0:
                root_input_billed += in_tok
                root_output += out_tok
                root_calls += 1
                root_cache_creation += cache_create
                root_cache_read += cache_read
                root_reasoning += reasoning
                if in_tok > root_peak_input:
                    root_peak_input = in_tok
            else:
                sub_input_billed += in_tok
                sub_output += out_tok
                sub_calls += 1
                sub_cache_creation += cache_create
                sub_cache_read += cache_read
                sub_reasoning += reasoning
                agent_id = payload.get("agent_id")
                if isinstance(agent_id, str):
                    sub_agent_ids.add(agent_id)
                    prev = sub_peak_per_agent.get(agent_id, 0)
                    if in_tok > prev:
                        sub_peak_per_agent[agent_id] = in_tok
        elif etype == "context_compacted":
            compaction_count += 1
            saved = payload.get("tokens_saved", 0)
            if isinstance(saved, int) and saved > 0:
                compaction_tokens_saved += saved

    sub_peak_input = sum(sub_peak_per_agent.values())
    max_input = get_model_max_input_tokens(root_model)
    last_input = read_last_input_tokens(events) or 0
    threshold = float(get_config_value("token_budget", "auto_compact_threshold", default=0.8))
    compact_at = int(max_input * threshold)
    utilization = (last_input / max_input) if max_input else 0.0
    tokens_until_compact = max(compact_at - last_input, 0)

    return {
        "root_model": root_model or "",
        "root_max_input_tokens": max_input,
        "root_last_input_tokens": last_input,
        "root_utilization": round(utilization, 4),
        "tokens_until_compact": tokens_until_compact,
        "compact_threshold": threshold,
        # Context-pressure (peak) — use these for window math.
        "root_peak_input_tokens": root_peak_input,
        "sub_peak_input_tokens": sub_peak_input,
        # Cumulative (billable) — use these for cost. ``input_tokens_billed``
        # is the raw provider count INCLUDING cached portions; pair it with
        # ``cache_read_tokens`` if you need to apply the discount client-side
        # (Anthropic cache reads bill at 0.1×, OpenAI at 0.5×).
        "root_input_tokens_billed": root_input_billed,
        "sub_input_tokens_billed": sub_input_billed,
        "total_input_tokens_billed": root_input_billed + sub_input_billed,
        # Output is additive everywhere.
        "root_output_tokens": root_output,
        "sub_output_tokens": sub_output,
        "total_output_tokens": root_output + sub_output,
        # Cache + reasoning subtotals (zero for legacy events that didn't
        # capture them). Cache reads served from prompt cache; cache creation
        # tokens written to cache; reasoning tokens are the hidden output of
        # extended-thinking / o1-class models.
        "root_cache_creation_tokens": root_cache_creation,
        "root_cache_read_tokens": root_cache_read,
        "root_reasoning_tokens": root_reasoning,
        "sub_cache_creation_tokens": sub_cache_creation,
        "sub_cache_read_tokens": sub_cache_read,
        "sub_reasoning_tokens": sub_reasoning,
        "total_cache_creation_tokens": root_cache_creation + sub_cache_creation,
        "total_cache_read_tokens": root_cache_read + sub_cache_read,
        "total_reasoning_tokens": root_reasoning + sub_reasoning,
        "root_llm_calls": root_calls,
        "sub_llm_calls": sub_calls,
        "sub_agent_count": len(sub_agent_ids),
        "compaction_count": compaction_count,
        "compaction_tokens_saved": compaction_tokens_saved,
    }


def read_last_input_tokens(events: list[EventRecord]) -> int | None:
    """Return the most recent ``llm_call_end`` event's ``input_tokens``.

    Session-store counterpart to the in-memory ``_last_input_tokens`` field
    on ``ToolUseLoop``. Lets callers outside the loop (e.g. the orchestrator's
    compaction check) read the authoritative per-call token count that was
    already persisted.
    """
    for event in reversed(events):
        if event.get("type") != "llm_call_end":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        value = payload.get("input_tokens")
        if isinstance(value, int) and value > 0:
            return value
    return None
