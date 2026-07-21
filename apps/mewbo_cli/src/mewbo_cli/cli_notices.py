#!/usr/bin/env python3
"""Post-run notice rendering shared by the TUI and the plain fallback.

Extracted from ``cli_master`` so both the Textual ``TurnEngine`` and the plain
non-interactive runner replay the same LLM-resilience / recovery / token-usage
notices without duplicating logic — and so the TUI never imports ``cli_master``
(which would create an import cycle: ``cli_master → app → turn_engine``).

Every function writes to a Rich ``Console``; the TUI passes a recording console
and forwards the captured renderable into the transcript, while the plain path
passes the real stdout console.
"""

from __future__ import annotations

from mewbo_core.config import get_config_value
from mewbo_core.session_runtime import _STATUS_BY_DONE_REASON, SessionRuntime
from mewbo_core.session_store import SessionStoreBase
from mewbo_core.types import BLOCKED_CODES
from rich.console import Console
from rich.text import Text


def derive_task_outcome(done_reason: str | None, blocked_code: object) -> str:
    """Map a completed run's ``(done_reason, blocked_code)`` onto its honest status.

    Reads (never re-derives) ``mewbo_core.session_runtime``'s closed
    ``done_reason`` -> status table and blocked-code set — the same mapping
    ``summarize_session`` uses for ``/status`` and every other client, so a
    new reason or wall taught to core is honoured here for free instead of
    drifting behind a second hand-copied table. ``blocked_code`` outranks
    every reason: the loop leaves ``done_reason`` at ``"completed"`` for a run
    that died against an unrecovered repo/network/permission/quota wall,
    carrying the wall separately, so trusting ``done_reason`` alone would
    still read as a clean success. A reason absent from the table (including
    ``"completed"`` itself, which the table intentionally does not name) is
    trusted as-is — the historical behaviour for an unnamed reason.
    """
    if isinstance(blocked_code, str) and blocked_code in BLOCKED_CODES:
        return "blocked"
    if not done_reason:
        return "completed"
    return _STATUS_BY_DONE_REASON.get(done_reason, done_reason)


def model_basename(model: object) -> str:
    """Drop the provider prefix from a model id: ``openai/gpt-4`` → ``gpt-4``."""
    return str(model).rsplit("/", 1)[-1]


def fmt_tokens(n: int) -> str:
    """Format a token count: 5200 → '5.2k', 1500000 → '1.5m'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def print_resilience_events(console: Console, store: SessionStoreBase, session_id: str) -> bool:
    """Surface LLM retry / fallback / no-progress-halt events from the last run.

    The core emits these to the transcript (not via hooks), so we replay them
    after the run completes. Scoped to events after the last ``user`` event so a
    multi-turn session never re-prints prior turns' notices.

    Returns ``True`` if a doom-loop halt line was printed so the caller can
    suppress a redundant generic recovery hint (which would duplicate the
    already-visible /retry,/continue guidance in the halt line).
    """
    transcript = store.load_transcript(session_id)
    last_user_ts = ""
    for event in transcript:
        if event.get("type") == "user":
            last_user_ts = str(event.get("ts", ""))

    halt_printed = False
    for event in transcript:
        if str(event.get("ts", "")) < last_user_ts:
            continue
        etype = event.get("type")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        line: Text | None = None
        if etype == "llm_retry":
            model = model_basename(payload.get("model", "model"))
            delay = payload.get("delay", 0) or 0
            line = Text(
                f"↻ Retrying {model} after {payload.get('error_type', 'error')} "
                f"({payload.get('attempt', '?')}/{payload.get('max_attempts', '?')}, "
                f"{float(delay):.0f}s)",
                style="dim yellow",
            )
        elif etype == "llm_fallback":
            from_model = model_basename(payload.get("from_model", "?"))
            to_model = model_basename(payload.get("to_model", "?"))
            suffix = " [pinned for run]" if payload.get("sticky") else ""
            line = Text(
                f"⤳ Falling back: {from_model} → {to_model} "
                f"({payload.get('reason', 'error')}){suffix}",
                style="dim yellow",
            )
        elif etype == "recovery" and payload.get("action") == "halt_no_progress":
            line = Text(
                f"⊘ Halted: repeated '{payload.get('tool', 'tool')}' with no "
                "progress — /retry or /continue to recover",
                style="dim red",
            )
            halt_printed = True
        if line is not None:
            console.print(line)
    return halt_printed


def maybe_print_recovery_hint(
    console: Console,
    store: SessionStoreBase,
    session_id: str,
    *,
    halt_printed: bool = False,
) -> None:
    """Print a concise recovery hint after a recoverable terminal run.

    Skipped when a doom-loop halt line was already printed (it already
    mentions /retry and /continue) or when the session parked awaiting a
    plan-approval decision (that has its own approval UX). Every other case
    defers to ``summarize_session``'s derived ``recoverable`` flag — the same
    honest signal ``/status`` and every other client already trust — instead
    of re-testing ``done_reason`` against a hand-copied allowlist. That flag
    already folds in "not running", "has a prior user turn", and "not
    permanently terminated" (an externally terminated session can still carry
    a recoverable-looking ``done_reason`` like ``"canceled"``, which is why
    the flag — not the raw reason — is authoritative), so a blocked run
    (``blocked_code``, which never changes ``done_reason``) or a halted
    verification failure (``unmet_goal``) offers the hint exactly as reliably
    as a plain ``error``.
    """
    if halt_printed:
        return
    summary = SessionRuntime(session_store=store).summarize_session(session_id)
    if not summary.get("recoverable") or summary.get("status") == "awaiting_approval":
        return
    console.print(
        Text(
            "↩ This session can be recovered — /continue to resume with context "
            "intact, or /retry to redo the last step.",
            style="dim cyan",
        )
    )


def print_usage_footer(
    console: Console,
    store: SessionStoreBase,
    session_id: str,
    model_name: str | None,
) -> None:
    """Print a single-line token usage summary below the response.

    Shows root agent headroom vs. model max (the actual context-window
    pressure), plus a sub-agent rollup and compaction count. Numbers come from
    the one ``build_usage_numbers`` helper — no duplicate aggregation here.
    """
    try:
        from mewbo_core.token_budget import build_usage_numbers

        effective_model = model_name or str(
            get_config_value("llm", "default_model", default="") or ""
        )
        events = store.load_transcript(session_id)
        u = build_usage_numbers(events, effective_model)
    except Exception:
        return  # silent — footer is decorative
    max_in = u["root_max_input_tokens"]
    if max_in <= 0 and u["total_input_tokens_billed"] == 0:
        return  # nothing meaningful yet
    pct = int(round(u["root_utilization"] * 100))
    line = Text()
    line.append(f"{u['root_model']}", style="dim cyan")
    line.append("  ", style="dim")
    line.append(
        f"root {fmt_tokens(u['root_last_input_tokens'])}/{fmt_tokens(max_in)} ({pct}%)",
        style="dim",
    )
    if u["sub_peak_input_tokens"] or u["sub_output_tokens"]:
        sub_peak = fmt_tokens(u["sub_peak_input_tokens"])
        sub_out = fmt_tokens(u["sub_output_tokens"])
        line.append("  ·  sub peak ", style="dim")
        line.append(f"{sub_peak} / {sub_out} out", style="dim")
    line.append("  ·  ", style="dim")
    line.append(f"{fmt_tokens(u['tokens_until_compact'])} until compact", style="dim")
    if u["compaction_count"] > 0:
        line.append(f"  ·  ⊙ {u['compaction_count']} compaction(s)", style="dim")
    console.print(line)


__all__ = [
    "derive_task_outcome",
    "fmt_tokens",
    "maybe_print_recovery_hint",
    "model_basename",
    "print_resilience_events",
    "print_usage_footer",
]
