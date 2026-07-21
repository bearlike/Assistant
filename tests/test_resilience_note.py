"""Tests for the resilience note — the error-visibility seam.

Unit-covers ``ResilienceNote`` (record/render, event filtering, bounding) and
then the loop wiring: a retry/fallback event routed through ``_emit_event``
reaches the note, and a non-empty note renders as its OWN system-prompt slot
(never overloaded onto ``skill_instructions``), one bounded block, deduplicated
so a clean turn adds nothing.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from langchain_core.messages import SystemMessage
from mewbo_core.tool_use_loop import (
    _RESILIENCE_NOTE_HEADER,
    ResilienceNote,
    ToolUseLoop,
)

# Sibling helpers (tests/ is on sys.path under pytest).
from test_tool_use_loop import (
    _allow_all_policy,
    _make_agent_context,
    _make_hook_manager,
    _make_registry,
    _make_spec,
)


def _retry(model="primary", attempt=1, max_attempts=2, error_type="TimeoutError"):
    return {
        "type": "llm_retry",
        "payload": {
            "model": model,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "error_type": error_type,
        },
    }


def _fallback(from_model="primary", to_model="rescue", reason="retries_exhausted"):
    return {
        "type": "llm_fallback",
        "payload": {"from_model": from_model, "to_model": to_model, "reason": reason},
    }


# ---------------------------------------------------------------------------
# ResilienceNote unit
# ---------------------------------------------------------------------------


def test_empty_before_any_event():
    assert ResilienceNote().render() == ""


def test_non_resilience_events_ignored():
    note = ResilienceNote()
    note.record({"type": "llm_call_start", "payload": {"model": "primary"}})
    note.record({"type": "tool_result", "payload": {}})
    assert note.render() == ""


def test_retry_event_renders_model_attempt_and_error_class():
    note = ResilienceNote()
    note.record(_retry(model="primary", attempt=1, max_attempts=2, error_type="TimeoutError"))
    text = note.render()
    assert text.startswith(_RESILIENCE_NOTE_HEADER)
    assert "primary" in text
    assert "1/2" in text
    assert "TimeoutError" in text


def test_fallback_event_renders_switch():
    note = ResilienceNote()
    note.record(_fallback(from_model="primary", to_model="rescue", reason="no_deployments"))
    text = note.render()
    assert "primary -> rescue" in text
    assert "no_deployments" in text


def test_bounded_to_max_events():
    note = ResilienceNote(max_events=5)
    for i in range(8):
        note.record(_retry(model=f"m{i}"))
    lines = [ln for ln in note.render().splitlines() if ln.startswith("- ")]
    assert len(lines) == 5
    # Oldest three (m0, m1, m2) dropped; the most recent five (m3..m7) survive.
    assert "m3" in note.render()
    assert "m2" not in note.render()
    assert "m7" in note.render()


def test_render_is_stable_for_dedup():
    # The loop dedups on the rendered string, so identical event history must
    # render byte-identically; a NEW event must change it.
    note = ResilienceNote()
    note.record(_retry())
    first = note.render()
    assert note.render() == first
    note.record(_fallback())
    assert note.render() != first


# ---------------------------------------------------------------------------
# Loop wiring — emit routing + the system-prompt slot
# ---------------------------------------------------------------------------


def _loop(event_logger=None) -> ToolUseLoop:
    return ToolUseLoop(
        agent_context=_make_agent_context(event_logger=event_logger),
        tool_registry=_make_registry(_make_spec("read_file", "Read a file")),
        permission_policy=_allow_all_policy(),
        hook_manager=_make_hook_manager(),
        session_id="s1",
    )


def test_emit_event_routes_retry_into_note():
    loop = _loop()
    assert loop._resilience_note.render() == ""
    loop._emit_event(_retry(model="primary", error_type="RateLimitError"))
    text = loop._resilience_note.render()
    assert "primary" in text
    assert "RateLimitError" in text


def test_emit_event_still_forwards_to_sink():
    seen: list = []
    loop = _loop(event_logger=seen.append)
    loop._emit_event(_retry())
    # Capture for the note must not swallow delivery to the real sink.
    assert [e["type"] for e in seen] == ["llm_retry"]


def test_render_system_prompt_omits_slot_when_clean():
    loop = _loop()
    prompt = loop._render_system_prompt(None, None, "")
    assert _RESILIENCE_NOTE_HEADER not in prompt


def test_render_system_prompt_includes_note_slot_after_events():
    loop = _loop()
    loop._emit_event(_retry(model="primary"))
    loop._emit_event(_fallback(from_model="primary", to_model="rescue"))
    prompt = loop._render_system_prompt(None, None, "")
    # Exactly one note block, carrying both grounded events.
    assert prompt.count(_RESILIENCE_NOTE_HEADER) == 1
    assert "primary -> rescue" in prompt
    # Its OWN slot: not fused into skill_instructions (none set here anyway).
    assert loop._skill_instructions is None


def test_note_survives_model_escalation_rerender():
    # _apply_model_escalation re-renders messages[0]; the note slot must ride it.
    loop = _loop()
    loop._emit_event(_retry(model="primary", error_type="TimeoutError"))
    with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
        mock_build.return_value = MagicMock()
        mock_build.return_value.bind_tools.return_value = MagicMock()
        messages = [SystemMessage(content="orig")]
        loop._tool_specs_full = []
        loop._last_active_ids = set()
        loop._deferred_ids = set()
        loop._tool_search_enabled = False
        loop._current_mode = "act"
        tool_schemas, _model = loop._apply_model_escalation(
            "rescue",
            messages,
            context=None,
            plan=None,
            agent_tree="",
            tool_schemas=[],
            model=MagicMock(),
        )
    assert _RESILIENCE_NOTE_HEADER in messages[0].content
    # Escalation keeps the note tracker honest so the loop won't re-render again.
    assert loop._active_resilience_note == loop._resilience_note.render()
