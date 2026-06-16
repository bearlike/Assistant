"""Tests for live streamed-token rendering in the CLI (Gitea #137).

Covers the ``AgentDisplayManager`` streaming preview and the
``_stream_tokens_to`` bus pump that feeds it ``agent_message_delta`` events.
"""

# ruff: noqa: I001
import time

from rich.console import Console
from rich.text import Text

from mewbo_core.session_event_bus import (
    get_session_event_bus,
    reset_session_event_bus_for_tests,
)

from mewbo_cli.cli_agent_display import AgentDisplayManager
from mewbo_cli.cli_master import _stream_tokens_to


def _render_text(display: AgentDisplayManager) -> str:
    """Render the display to plain text for assertions."""
    console = Console(width=80, file=None, record=True)
    console.print(display.render())
    return console.export_text()


def test_on_token_delta_accumulates_and_renders() -> None:
    display = AgentDisplayManager()
    assert isinstance(display.render(), Text)  # nothing yet → blank
    assert display.has_activity is False

    display.on_token_delta("Hello ")
    display.on_token_delta("world")

    assert display.has_activity is True
    out = _render_text(display)
    assert "Hello world" in out
    assert "Responding" in out


def test_on_token_delta_ignores_empty() -> None:
    display = AgentDisplayManager()
    display.on_token_delta("")
    assert display.has_activity is False
    assert isinstance(display.render(), Text)


def test_stream_preview_clips_to_tail() -> None:
    display = AgentDisplayManager()
    for i in range(20):
        display.on_token_delta(f"line {i}\n")
    out = _render_text(display)
    # Tail kept, head clipped with an ellipsis marker.
    assert "line 19" in out
    assert "line 0\n" not in out
    assert "…" in out


def test_stream_tokens_to_pumps_root_deltas() -> None:
    reset_session_event_bus_for_tests()
    bus = get_session_event_bus()
    display = AgentDisplayManager()
    session_id = "sess-stream"

    with _stream_tokens_to(display, session_id):
        bus.publish(
            session_id,
            {"ts": "t1", "type": "agent_message_delta",
             "payload": {"text": "Hi ", "depth": 0, "step": 0}},
        )
        bus.publish(
            session_id,
            {"ts": "t2", "type": "agent_message_delta",
             "payload": {"text": "there", "depth": 0, "step": 0}},
        )
        # Sub-agent (depth>0) deltas and unrelated events are ignored.
        bus.publish(
            session_id,
            {"ts": "t3", "type": "agent_message_delta",
             "payload": {"text": "CHILD", "depth": 1, "step": 0}},
        )
        bus.publish(
            session_id,
            {"ts": "t4", "type": "tool_result", "payload": {"result": "ok"}},
        )
        # Daemon drain thread is async — wait briefly for it to consume.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and "there" not in _render_text(display):
            time.sleep(0.02)

    out = _render_text(display)
    assert "Hi there" in out
    assert "CHILD" not in out


def test_stream_tokens_to_noop_without_session() -> None:
    display = AgentDisplayManager()
    # No session id → graceful no-op context (must not raise).
    with _stream_tokens_to(display, None):
        pass
    assert display.has_activity is False
