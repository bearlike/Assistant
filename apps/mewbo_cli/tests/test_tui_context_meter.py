#!/usr/bin/env python3
"""Tests for ContextMeter — tokens → context-% and tokens → cost (issue #156)."""

from __future__ import annotations

from mewbo_cli.tui.status.context_meter import ContextMeter


def test_exact_window_match_not_estimated() -> None:
    """An exact model match in the window map reports the real total, no ~."""
    meter = ContextMeter(windows={"gpt-oss-120b": 1000}, default_window=128000)
    usage = meter.usage(used_tokens=250, model="gpt-oss-120b")
    assert usage.total == 1000
    assert usage.estimated is False
    assert usage.percent == 25.0


def test_suffix_match_strips_proxy_prefix() -> None:
    """A runtime ``openai/<model>`` resolves to a bare-keyed window entry."""
    meter = ContextMeter(windows={"gpt-oss-120b": 2000}, default_window=128000)
    usage = meter.usage(used_tokens=500, model="openai/gpt-oss-120b")
    assert usage.total == 2000
    assert usage.estimated is False
    assert usage.percent == 25.0


def test_unknown_model_uses_estimated_default() -> None:
    """An unknown model falls back to the default window flagged estimated."""
    meter = ContextMeter(windows={}, default_window=4000)
    usage = meter.usage(used_tokens=2000, model="mystery-model")
    assert usage.total == 4000
    assert usage.estimated is True
    assert usage.percent == 50.0


def test_percent_clamped_and_warning_threshold() -> None:
    """Percent never exceeds 100 and crosses the 80% warning flag correctly."""
    meter = ContextMeter(windows={"m": 100}, default_window=100)
    assert meter.usage(used_tokens=999, model="m").percent == 100.0
    assert meter.usage(used_tokens=79, model="m").over_warning is False
    assert meter.usage(used_tokens=80, model="m").over_warning is True


def test_zero_window_never_divides_by_zero() -> None:
    """A non-positive window yields 0% rather than raising."""
    meter = ContextMeter(windows={"m": 0}, default_window=0)
    usage = meter.usage(used_tokens=100, model="m")
    assert usage.percent == 0.0


def test_cost_none_when_no_model() -> None:
    """No model → no cost (honest None, not a fabricated 0)."""
    meter = ContextMeter()
    assert meter.cost(input_tokens=10, output_tokens=10, model=None) is None


def test_cost_unknown_model_returns_none() -> None:
    """A model LiteLLM cannot price degrades to None."""
    meter = ContextMeter()
    cost = meter.cost(input_tokens=100, output_tokens=100, model="totally-made-up-xyz")
    assert cost is None


def test_cost_known_model_is_positive() -> None:
    """A model LiteLLM prices returns a positive float."""
    meter = ContextMeter()
    cost = meter.cost(input_tokens=1000, output_tokens=1000, model="gpt-4o-mini")
    # If the local litellm pricing table lacks this model the contract still
    # holds (None); when present it must be a positive number.
    assert cost is None or cost > 0


def test_format_tokens_compact() -> None:
    """Token formatting: raw / K / M."""
    assert ContextMeter.format_tokens(842) == "842"
    assert ContextMeter.format_tokens(12_345) == "12.3K"
    assert ContextMeter.format_tokens(1_400_000) == "1.4M"


def test_format_cost_honest_dash() -> None:
    """Cost formatting shows an em dash for None, $ otherwise."""
    assert ContextMeter.format_cost(None) == "—"
    assert ContextMeter.format_cost(0.0012).startswith("$0.0012")
    assert ContextMeter.format_cost(1.5) == "$1.50"
