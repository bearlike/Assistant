#!/usr/bin/env python3
"""Tests for ThroughputMeter — live phase / tok-s / TTFT / stall.

Deterministic: an explicit ``now`` is passed to every ``mark_*`` / ``snapshot``
so no wall clock is involved. The reasoning-model predicate is injected.
"""

from __future__ import annotations

from mewbo_cli.tui.status.throughput_meter import Phase, ThroughputMeter


def _meter(*, reasoning: bool = False) -> ThroughputMeter:
    return ThroughputMeter(is_reasoning_model=lambda _m: reasoning)


def test_starts_idle() -> None:
    assert _meter().snapshot(now=0.0).phase is Phase.IDLE


def test_llm_start_enters_uploading_then_delta_streams() -> None:
    m = _meter()
    m.mark_llm_start("gpt-4o", now=0.0)
    assert m.snapshot(now=0.1).phase is Phase.UPLOADING
    # First token at t=0.5 → TTFT = 0.5, phase streaming.
    m.mark_delta("hello world!", now=0.5)
    snap = m.snapshot(now=0.5)
    assert snap.phase is Phase.STREAMING
    assert snap.ttft == 0.5


def test_streaming_tok_per_s_is_positive() -> None:
    m = _meter()
    m.mark_llm_start("gpt-4o", now=0.0)
    m.mark_delta("first", now=1.0)  # first token — sets TTFT, no rate yet
    # 8 chars ≈ 2 tokens over 1s → EWMA folds a positive rate.
    m.mark_delta("12345678", now=2.0)
    assert m.snapshot(now=2.0).tok_per_s > 0.0


def test_reasoning_model_enters_reasoning_phase_not_uploading() -> None:
    m = _meter(reasoning=True)
    m.mark_llm_start("o3", now=0.0)
    snap = m.snapshot(now=5.0)
    assert snap.phase is Phase.REASONING
    assert snap.reasoning is True


def test_reasoning_never_stalls() -> None:
    m = _meter(reasoning=True)
    m.mark_llm_start("o3", now=0.0)
    # Far past the stall threshold — a reasoning model is legitimately silent.
    snap = m.snapshot(now=ThroughputMeter.STALL_AFTER_SECONDS + 100.0)
    assert snap.phase is Phase.REASONING


def test_uploading_stalls_after_threshold() -> None:
    m = _meter()
    m.mark_llm_start("gpt-4o", now=0.0)
    before = m.snapshot(now=ThroughputMeter.STALL_AFTER_SECONDS - 1.0)
    assert before.phase is Phase.UPLOADING
    after = m.snapshot(now=ThroughputMeter.STALL_AFTER_SECONDS + 1.0)
    assert after.phase is Phase.STALLED
    assert after.stall_seconds >= ThroughputMeter.STALL_AFTER_SECONDS


def test_tool_start_and_end_transitions() -> None:
    m = _meter()
    m.mark_tool_start("bash", now=1.0)
    snap = m.snapshot(now=1.5)
    assert snap.phase is Phase.RUNNING_TOOL
    assert snap.tool_id == "bash"
    assert snap.phase_elapsed == 0.5
    m.mark_tool_end(now=2.0)
    assert m.snapshot(now=2.0).phase is Phase.IDLE


def test_running_tool_stalls_when_silent() -> None:
    m = _meter()
    m.mark_tool_start("bash", now=0.0)
    snap = m.snapshot(now=ThroughputMeter.STALL_AFTER_SECONDS + 5.0)
    assert snap.phase is Phase.STALLED


def test_llm_end_reconciles_and_returns_idle() -> None:
    m = _meter()
    m.mark_llm_start("gpt-4o", now=0.0)
    m.mark_delta("stream", now=0.5)
    m.mark_llm_end(output_tokens=123, now=1.0)
    assert m.snapshot(now=1.0).phase is Phase.IDLE


def test_reset_clears_state() -> None:
    m = _meter()
    m.mark_llm_start("gpt-4o", now=0.0)
    m.mark_delta("abcd", now=0.5)
    m.reset(now=10.0)
    snap = m.snapshot(now=10.0)
    assert snap.phase is Phase.IDLE
    assert snap.ttft is None
    assert snap.tok_per_s == 0.0


# ---------------------------------------------------------------------------
# Label / facet formatting (pure)
# ---------------------------------------------------------------------------


def test_format_label_covers_phases() -> None:
    m = _meter()
    assert ThroughputMeter.format_label(m.snapshot(now=0.0)) == "working"
    m.mark_llm_start("gpt-4o", now=0.0)
    assert ThroughputMeter.format_label(m.snapshot(now=0.1)) == "uploading…"
    m.mark_delta("hello there general", now=0.2)
    m.mark_delta("hello there general", now=0.4)
    assert "tok/s" in ThroughputMeter.format_label(m.snapshot(now=0.4))
    m.mark_tool_start("bash", now=0.5)
    assert "running bash" in ThroughputMeter.format_label(m.snapshot(now=1.0))
    stalled = m.snapshot(now=ThroughputMeter.STALL_AFTER_SECONDS + 2.0)
    assert "stalled" in ThroughputMeter.format_label(stalled)


def test_format_facet_shows_stall_and_rate_only() -> None:
    m = _meter()
    # Idle / uploading add nothing to the fleet row.
    assert ThroughputMeter.format_facet(m.snapshot(now=0.0)) == ""
    m.mark_llm_start("gpt-4o", now=0.0)
    assert ThroughputMeter.format_facet(m.snapshot(now=0.1)) == ""
    # A stall shows the glyph + seconds (the sub-agent-hang signal).
    stalled = m.snapshot(now=ThroughputMeter.STALL_AFTER_SECONDS + 3.0)
    assert "stalled" in ThroughputMeter.format_facet(stalled)
