"""Tests for cli_icons: ICONS constant with agent-state glyphs and spinner frames."""

from __future__ import annotations

from mewbo_cli.cli_icons import ICONS


def test_icons_check_and_cross() -> None:
    assert ICONS.check == "✓"
    assert ICONS.cross == "✗"
    assert ICONS.error == "✗"


def test_icons_spinner_is_tuple_of_strings() -> None:
    assert isinstance(ICONS.spinner, tuple)
    assert len(ICONS.spinner) >= 4
    for frame in ICONS.spinner:
        assert isinstance(frame, str)


def test_icons_tool_pending() -> None:
    assert isinstance(ICONS.tool_pending, str)
    assert len(ICONS.tool_pending) > 0


def test_icons_agent_state_keys() -> None:
    expected = {
        "submitted",
        "running",
        "completed",
        "failed",
        "cancelled",
        "rejected",
        "blocked",
        "unmet_goal",
    }
    assert set(ICONS.agent_state.keys()) == expected


def test_icons_agent_state_submitted() -> None:
    assert ICONS.agent_state["submitted"] == "⏳"


def test_icons_agent_state_running() -> None:
    assert ICONS.agent_state["running"] == "●"


def test_icons_agent_state_completed() -> None:
    assert ICONS.agent_state["completed"] == "✓"


def test_icons_agent_state_failed() -> None:
    assert ICONS.agent_state["failed"] == "✗"


def test_icons_agent_state_cancelled() -> None:
    assert ICONS.agent_state["cancelled"] == "⊘"


def test_icons_agent_state_rejected() -> None:
    assert ICONS.agent_state["rejected"] == "⊘"


def test_icons_agent_state_blocked() -> None:
    assert ICONS.agent_state["blocked"] == "⚠"


def test_icons_agent_state_unmet_goal() -> None:
    assert ICONS.agent_state["unmet_goal"] == "◎"


def test_icons_agent_state_style_keys_match_glyph_keys() -> None:
    """Every glyph state must carry a color role — no key drift between the two."""
    assert set(ICONS.agent_state_style.keys()) == set(ICONS.agent_state.keys())


def test_icons_agent_state_style_blocked_and_unmet_goal_share_warning() -> None:
    """Mirrors the console's StatusBadge: both tint warning, differ by glyph only."""
    assert ICONS.agent_state_style["blocked"] == "warning"
    assert ICONS.agent_state_style["unmet_goal"] == "warning"


def test_icons_agent_state_is_read_only() -> None:
    """agent_state must be a read-only mapping; mutation must raise TypeError."""
    import pytest

    with pytest.raises(TypeError):
        ICONS.agent_state["running"] = "x"  # type: ignore[index]
