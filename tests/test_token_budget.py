"""Tests for token budget calculations — anchored on LiteLLM + API usage_metadata."""

from unittest.mock import patch

from mewbo_core import token_budget as token_budget_module
from mewbo_core.config import set_config_override
from mewbo_core.token_budget import (
    _strip_provider_prefix,
    build_usage_numbers,
    get_model_max_input_tokens,
    get_token_budget,
    read_last_input_tokens,
)

# -- Provider prefix stripping ----------------------------------------------


def test_strip_provider_prefix_removes_routing_prefix():
    """LiteLLM routing prefixes (openai/, anthropic/) are not part of the canonical name."""
    assert _strip_provider_prefix("openai/claude-sonnet-4-6") == "claude-sonnet-4-6"
    assert _strip_provider_prefix("anthropic/claude-opus-4-7") == "claude-opus-4-7"


def test_strip_provider_prefix_passthrough():
    """Canonical names without a slash pass through unchanged."""
    assert _strip_provider_prefix("claude-sonnet-4-6") == "claude-sonnet-4-6"
    assert _strip_provider_prefix("gpt-5.4") == "gpt-5.4"


# -- LiteLLM-anchored window lookup -----------------------------------------


def test_get_model_max_input_tokens_uses_litellm_result():
    """When LiteLLM returns a value, it is the resolved window (no override, no default)."""
    set_config_override({"token_budget": {"model_context_windows": {}}}, replace=True)
    with patch.object(token_budget_module, "_litellm_max_input_tokens", return_value=1_000_000):
        assert get_model_max_input_tokens("claude-sonnet-4-6") == 1_000_000


def test_get_model_max_input_tokens_strips_prefix_before_litellm_lookup():
    """The routing prefix must be stripped before handing to LiteLLM."""
    set_config_override({"token_budget": {"model_context_windows": {}}}, replace=True)
    seen: list[str] = []

    def _fake_litellm(name: str) -> int:
        seen.append(name)
        return 1_000_000

    with patch.object(token_budget_module, "_litellm_max_input_tokens", side_effect=_fake_litellm):
        get_model_max_input_tokens("openai/claude-sonnet-4-6")
    assert seen == ["claude-sonnet-4-6"], f"LiteLLM must see canonical name, got {seen}"


def test_get_model_max_input_tokens_override_wins():
    """User-supplied override takes precedence over LiteLLM."""
    token_budget_module._litellm_max_input_tokens.cache_clear()
    set_config_override(
        {"token_budget": {"model_context_windows": {"claude-sonnet-4-6": 200000}}},
        replace=True,
    )
    assert get_model_max_input_tokens("claude-sonnet-4-6") == 200000


def test_get_model_max_input_tokens_override_matches_prefixed_name():
    """Override key may be the canonical name; prefixed model still resolves to it."""
    token_budget_module._litellm_max_input_tokens.cache_clear()
    set_config_override(
        {"token_budget": {"model_context_windows": {"claude-sonnet-4-6": 300000}}},
        replace=True,
    )
    assert get_model_max_input_tokens("openai/claude-sonnet-4-6") == 300000


def test_get_model_max_input_tokens_falls_back_to_default_for_unknown_model():
    """Unknown model with no override falls back to default_context_window."""
    token_budget_module._litellm_max_input_tokens.cache_clear()
    set_config_override(
        {
            "token_budget": {
                "model_context_windows": {},
                "default_context_window": 99000,
            }
        },
        replace=True,
    )
    assert get_model_max_input_tokens("definitely-not-a-real-model-x7z") == 99000


def test_get_model_max_input_tokens_none_model_returns_default():
    """None / empty model name returns the default window."""
    set_config_override({"token_budget": {"default_context_window": 64000}}, replace=True)
    assert get_model_max_input_tokens(None) == 64000
    assert get_model_max_input_tokens("") == 64000


# -- Budget uses real usage_metadata when available -------------------------


def test_get_token_budget_prefers_last_input_tokens_over_estimate():
    """When the API reported input_tokens, the budget must reflect it directly."""
    token_budget_module._litellm_max_input_tokens.cache_clear()
    set_config_override(
        {
            "token_budget": {
                "model_context_windows": {"test-model": 100_000},
                "auto_compact_threshold": 0.8,
            }
        },
        replace=True,
    )
    events = [{"type": "user", "payload": {"text": "anything"}}]
    budget = get_token_budget(events, None, "test-model", last_input_tokens=85_000)
    assert budget.total_tokens == 85_000
    assert budget.context_window == 100_000
    assert budget.utilization == 0.85
    assert budget.needs_compact is True


def test_get_token_budget_falls_back_to_estimate_without_usage_metadata():
    """Before the first LLM call we still estimate from events + summary."""
    set_config_override(
        {"token_budget": {"model_context_windows": {"test-model": 100_000}}},
        replace=True,
    )
    events = [{"type": "user", "payload": {"text": "hello"}}]
    budget = get_token_budget(events, "a summary", "test-model")
    # Not authoritative, but must be non-zero so downstream logic can reason.
    assert budget.total_tokens >= 0
    assert budget.context_window == 100_000


def test_needs_compact_threshold_edge():
    """needs_compact fires at-or-above the threshold."""
    set_config_override(
        {
            "token_budget": {
                "model_context_windows": {"m": 1000},
                "auto_compact_threshold": 0.9,
            }
        },
        replace=True,
    )
    just_below = get_token_budget([], None, "m", last_input_tokens=899)
    assert just_below.needs_compact is False
    at_threshold = get_token_budget([], None, "m", last_input_tokens=900)
    assert at_threshold.needs_compact is True


# -- read_last_input_tokens -------------------------------------------------


def test_read_last_input_tokens_returns_most_recent():
    """Scans in reverse and returns the last llm_call_end input_tokens."""
    events = [
        {"type": "user", "payload": {"text": "hi"}},
        {"type": "llm_call_end", "payload": {"input_tokens": 100, "output_tokens": 50}},
        {"type": "tool_result", "payload": {"tool_id": "x", "result": "ok"}},
        {"type": "llm_call_end", "payload": {"input_tokens": 300, "output_tokens": 90}},
    ]
    assert read_last_input_tokens(events) == 300


def test_read_last_input_tokens_returns_none_when_absent():
    """Empty or pre-LLM transcript returns None."""
    assert read_last_input_tokens([]) is None
    assert read_last_input_tokens([{"type": "user", "payload": {"text": "hi"}}]) is None


def test_read_last_input_tokens_skips_zero_values():
    """Zero input_tokens (e.g. failed LLM call) is not a meaningful signal."""
    events = [
        {"type": "llm_call_end", "payload": {"input_tokens": 500, "output_tokens": 80}},
        {"type": "llm_call_end", "payload": {"input_tokens": 0}},
    ]
    assert read_last_input_tokens(events) == 500


# -- build_usage_numbers ----------------------------------------------------


def _llm_end(
    *,
    depth: int,
    in_tok: int,
    out_tok: int,
    agent_id: str = "a0",
    cache_create: int = 0,
    cache_read: int = 0,
    reasoning: int = 0,
) -> dict:
    payload = {
        "agent_id": agent_id,
        "depth": depth,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
    }
    # Only include cache/reasoning when set so legacy events (which never
    # carried these keys) stay byte-identical to existing test fixtures.
    if cache_create:
        payload["cache_creation_input_tokens"] = cache_create
    if cache_read:
        payload["cache_read_input_tokens"] = cache_read
    if reasoning:
        payload["reasoning_output_tokens"] = reasoning
    return {"type": "llm_call_end", "payload": payload}


def test_build_usage_numbers_empty_transcript():
    """Empty transcript returns zeros with a sensible model window."""
    set_config_override(
        {
            "token_budget": {
                "model_context_windows": {"m": 100_000},
                "auto_compact_threshold": 0.8,
            }
        },
        replace=True,
    )
    u = build_usage_numbers([], "m")
    assert u["root_peak_input_tokens"] == 0
    assert u["root_input_tokens_billed"] == 0
    assert u["sub_peak_input_tokens"] == 0
    assert u["sub_input_tokens_billed"] == 0
    assert u["total_input_tokens_billed"] == 0
    assert u["compaction_count"] == 0
    assert u["root_max_input_tokens"] == 100_000
    assert u["tokens_until_compact"] == 80_000  # 100K * 0.8


def test_build_usage_numbers_splits_root_vs_sub_by_depth():
    """Depth==0 lands in root; depth>0 lands in sub-agent rollup."""
    set_config_override(
        {"token_budget": {"model_context_windows": {"m": 100_000}}},
        replace=True,
    )
    events = [
        _llm_end(depth=0, in_tok=1000, out_tok=100),
        _llm_end(depth=0, in_tok=2000, out_tok=200),
        _llm_end(depth=1, in_tok=500, out_tok=50, agent_id="sub1"),
        _llm_end(depth=1, in_tok=300, out_tok=30, agent_id="sub1"),
        _llm_end(depth=2, in_tok=100, out_tok=10, agent_id="sub2"),
    ]
    u = build_usage_numbers(events, "m")
    # Root: peak = max(1000, 2000) = 2000; billed = sum = 3000.
    assert u["root_peak_input_tokens"] == 2000
    assert u["root_input_tokens_billed"] == 3000
    assert u["root_output_tokens"] == 300
    assert u["root_llm_calls"] == 2
    # Sub: sub1 peak=500 (max of 500,300); sub2 peak=100. Combined peak = 600.
    # Billed = 500+300+100 = 900.
    assert u["sub_peak_input_tokens"] == 600
    assert u["sub_input_tokens_billed"] == 900
    assert u["sub_output_tokens"] == 90
    assert u["sub_llm_calls"] == 3
    assert u["sub_agent_count"] == 2
    assert u["total_input_tokens_billed"] == 3900
    assert u["total_output_tokens"] == 390


def test_build_usage_numbers_peak_is_max_not_sum_across_growing_prompt():
    """Regression: in a real tool-use loop the root prompt grows across calls
    as tool results stack on the same context. Summing input_tokens across
    calls double-counts the baseline — use max. This is the bug that made
    the frontend show ``120K in`` on a turn whose real context peak was 27K.
    """
    set_config_override(
        {"token_budget": {"model_context_windows": {"m": 200_000}}},
        replace=True,
    )
    # Simulate an 11-step tool-use loop where the prompt climbs from 13K
    # to 27K as each tool result is appended.
    events = [
        _llm_end(depth=0, in_tok=13_080, out_tok=200),
        _llm_end(depth=0, in_tok=14_500, out_tok=180),
        _llm_end(depth=0, in_tok=16_000, out_tok=220),
        _llm_end(depth=0, in_tok=17_500, out_tok=150),
        _llm_end(depth=0, in_tok=19_000, out_tok=210),
        _llm_end(depth=0, in_tok=20_500, out_tok=190),
        _llm_end(depth=0, in_tok=22_000, out_tok=170),
        _llm_end(depth=0, in_tok=23_500, out_tok=230),
        _llm_end(depth=0, in_tok=25_000, out_tok=250),
        _llm_end(depth=0, in_tok=26_000, out_tok=180),
        _llm_end(depth=0, in_tok=27_039, out_tok=300),
    ]
    u = build_usage_numbers(events, "m")
    # Peak = the last (largest) call. That's the real context pressure.
    assert u["root_peak_input_tokens"] == 27_039
    # Billed = sum. ~224K — clearly different from peak; guards against
    # anyone regressing "peak" back to "sum".
    assert u["root_input_tokens_billed"] == sum(
        [13_080, 14_500, 16_000, 17_500, 19_000, 20_500, 22_000, 23_500, 25_000, 26_000, 27_039]
    )
    assert u["root_input_tokens_billed"] > u["root_peak_input_tokens"] * 5
    # last_input_tokens = the most recent event's input_tokens.
    assert u["root_last_input_tokens"] == 27_039
    # tokens_until_compact uses peak/last, not sum: 160K - 27K = 133K.
    assert u["tokens_until_compact"] == int(200_000 * 0.8) - 27_039


def test_build_usage_numbers_sub_agent_peaks_sum_across_isolated_contexts():
    """Two sub-agents running in isolated contexts: each has its own peak.
    The combined 'peak pressure' is the sum of per-agent maxes, NOT the
    sum of every call's input (that over-counts like the root bug).
    """
    set_config_override(
        {"token_budget": {"model_context_windows": {"m": 200_000}}},
        replace=True,
    )
    events = [
        # sub1 grows 5K → 10K → 15K.
        _llm_end(depth=1, in_tok=5_000, out_tok=100, agent_id="sub1"),
        _llm_end(depth=1, in_tok=10_000, out_tok=100, agent_id="sub1"),
        _llm_end(depth=1, in_tok=15_000, out_tok=100, agent_id="sub1"),
        # sub2 grows 3K → 6K.
        _llm_end(depth=1, in_tok=3_000, out_tok=50, agent_id="sub2"),
        _llm_end(depth=1, in_tok=6_000, out_tok=50, agent_id="sub2"),
    ]
    u = build_usage_numbers(events, "m")
    # Combined peak = sub1_peak + sub2_peak = 15K + 6K = 21K. Not 39K.
    assert u["sub_peak_input_tokens"] == 15_000 + 6_000
    # Billed still sums every call.
    assert u["sub_input_tokens_billed"] == 5_000 + 10_000 + 15_000 + 3_000 + 6_000
    assert u["sub_agent_count"] == 2


def test_build_usage_numbers_tokens_until_compact_reflects_last_call():
    """tokens_until_compact uses the last root input_tokens vs threshold."""
    set_config_override(
        {
            "token_budget": {
                "model_context_windows": {"m": 100_000},
                "auto_compact_threshold": 0.8,
            }
        },
        replace=True,
    )
    events = [
        _llm_end(depth=0, in_tok=10_000, out_tok=100),
        _llm_end(depth=0, in_tok=75_000, out_tok=200),  # last = 75K
    ]
    u = build_usage_numbers(events, "m")
    assert u["root_last_input_tokens"] == 75_000
    assert u["root_utilization"] == 0.75
    assert u["tokens_until_compact"] == 5_000  # 80K - 75K
    # Peak matches last here since inputs grew monotonically.
    assert u["root_peak_input_tokens"] == 75_000


def test_build_usage_numbers_aggregates_cache_and_reasoning_per_depth():
    """Cache + reasoning subtotals from `usage_metadata.input_token_details`
    and `output_token_details` accumulate per depth so the UI can show fresh
    vs cached input and apply the right billing discount.

    Anthropic cache reads bill at 0.1× input price; OpenAI cached at 0.5×.
    Without per-depth aggregation we cannot tell the user "this turn saved
    Xk tokens via cache hits" or distinguish cache-creation cost from raw
    input cost.
    """
    set_config_override(
        {"token_budget": {"model_context_windows": {"m": 200_000}}},
        replace=True,
    )
    events = [
        # Root call 1: writes the system prompt to cache.
        _llm_end(depth=0, in_tok=13_000, out_tok=200, cache_create=12_000),
        # Root call 2: cache hit — most of the prompt served from cache.
        # Includes a reasoning step (extended thinking).
        _llm_end(depth=0, in_tok=14_500, out_tok=300, cache_read=12_000, reasoning=80),
        # One sub-agent that benefits from cache reads as well.
        _llm_end(
            depth=1,
            agent_id="sub1",
            in_tok=5_000,
            out_tok=150,
            cache_read=4_500,
        ),
    ]
    u = build_usage_numbers(events, "m")
    # Root cache + reasoning aggregation.
    assert u["root_cache_creation_tokens"] == 12_000
    assert u["root_cache_read_tokens"] == 12_000
    assert u["root_reasoning_tokens"] == 80
    # Sub-agent rollup.
    assert u["sub_cache_creation_tokens"] == 0
    assert u["sub_cache_read_tokens"] == 4_500
    assert u["sub_reasoning_tokens"] == 0
    # Totals.
    assert u["total_cache_creation_tokens"] == 12_000
    assert u["total_cache_read_tokens"] == 16_500
    assert u["total_reasoning_tokens"] == 80
    # Existing semantics unchanged: peak=max(13K,14.5K)=14.5K, billed=sum.
    assert u["root_peak_input_tokens"] == 14_500
    assert u["root_input_tokens_billed"] == 27_500


def test_build_usage_numbers_legacy_events_without_cache_keys_yield_zero():
    """Pre-existing transcripts (recorded before cache capture landed in
    tool_use_loop) have no cache_* keys on their llm_call_end payloads.
    Aggregation must default to zero, not crash.
    """
    set_config_override(
        {"token_budget": {"model_context_windows": {"m": 100_000}}},
        replace=True,
    )
    events = [
        # Plain payload, no cache fields — what we'd see in a session
        # captured before this commit.
        {
            "type": "llm_call_end",
            "payload": {"depth": 0, "input_tokens": 1000, "output_tokens": 50},
        },
    ]
    u = build_usage_numbers(events, "m")
    assert u["root_cache_creation_tokens"] == 0
    assert u["root_cache_read_tokens"] == 0
    assert u["root_reasoning_tokens"] == 0
    assert u["total_cache_read_tokens"] == 0


def test_build_usage_numbers_compaction_aggregation():
    """Counts context_compacted events and sums tokens_saved."""
    set_config_override(
        {"token_budget": {"model_context_windows": {"m": 100_000}}},
        replace=True,
    )
    events = [
        _llm_end(depth=0, in_tok=1000, out_tok=100),
        {"type": "context_compacted", "payload": {"tokens_saved": 12_000, "mode": "auto"}},
        _llm_end(depth=0, in_tok=2000, out_tok=200),
        {"type": "context_compacted", "payload": {"tokens_saved": 8_000, "mode": "mid_loop"}},
    ]
    u = build_usage_numbers(events, "m")
    assert u["compaction_count"] == 2
    assert u["compaction_tokens_saved"] == 20_000


def test_build_usage_numbers_clamps_tokens_until_compact_at_zero():
    """When usage is above threshold, tokens_until_compact is 0 (not negative)."""
    set_config_override(
        {
            "token_budget": {
                "model_context_windows": {"m": 100_000},
                "auto_compact_threshold": 0.8,
            }
        },
        replace=True,
    )
    events = [_llm_end(depth=0, in_tok=90_000, out_tok=100)]
    u = build_usage_numbers(events, "m")
    assert u["tokens_until_compact"] == 0


# -- LiteLLM failure path ---------------------------------------------------


def test_litellm_unknown_model_falls_through_to_default():
    """When LiteLLM raises (unknown model), fallback chain kicks in."""
    token_budget_module._litellm_max_input_tokens.cache_clear()
    set_config_override(
        {
            "token_budget": {
                "model_context_windows": {},
                "default_context_window": 50_000,
            }
        },
        replace=True,
    )
    with patch.object(
        token_budget_module,
        "_litellm_max_input_tokens",
        return_value=None,
    ):
        assert get_model_max_input_tokens("some-exotic-model") == 50_000
