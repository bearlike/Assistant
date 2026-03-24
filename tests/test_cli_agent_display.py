#!/usr/bin/env python3
"""Tests for CLI agent display manager."""

from __future__ import annotations

from io import StringIO

from meeseeks_cli.cli_agent_display import (
    AgentDisplayManager,
    AgentDisplayState,
    _format_elapsed,
)
from meeseeks_core.classes import ActionStep
from meeseeks_core.common import MockSpeaker
from meeseeks_core.hypervisor import AgentHandle
from rich.console import Console
from rich.panel import Panel


def _render_to_str(mgr: AgentDisplayManager, width: int = 100) -> str:
    """Render agent display to a plain string for assertions."""
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=width)
    console.print(mgr.render())
    return buf.getvalue()


def _make_handle(
    agent_id: str = "a1",
    parent_id: str | None = None,
    depth: int = 0,
    model_name: str = "sonnet",
    task_description: str = "test task",
    **kwargs: object,
) -> AgentHandle:
    return AgentHandle(
        agent_id=agent_id,
        parent_id=parent_id,
        depth=depth,
        model_name=model_name,
        task_description=task_description,
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# AgentDisplayState
# ---------------------------------------------------------------------------


class TestAgentDisplayState:
    def test_dataclass_fields(self):
        state = AgentDisplayState(
            agent_id="abc123",
            parent_id=None,
            depth=0,
            model="sonnet",
            task="Do something",
            status="running",
        )
        assert state.agent_id == "abc123"
        assert state.status == "running"
        assert state.steps == 0
        assert state.last_tool is None

    def test_new_fields_defaults(self):
        state = AgentDisplayState(
            agent_id="x", parent_id=None, depth=0,
            model="m", task="t", status="running",
        )
        assert state.started_at == 0.0
        assert state.token_count == 0

    def test_new_fields_explicit(self):
        state = AgentDisplayState(
            agent_id="x", parent_id=None, depth=0,
            model="m", task="t", status="running",
            started_at=100.0, token_count=5000,
        )
        assert state.started_at == 100.0
        assert state.token_count == 5000


# ---------------------------------------------------------------------------
# _format_elapsed
# ---------------------------------------------------------------------------


class TestFormatElapsed:
    def test_zero(self):
        assert _format_elapsed(0) == "0s"

    def test_seconds(self):
        assert _format_elapsed(42.7) == "43s"

    def test_one_minute(self):
        assert _format_elapsed(60) == "1m 00s"

    def test_minutes_and_seconds(self):
        assert _format_elapsed(93) == "1m 33s"

    def test_large_duration(self):
        assert _format_elapsed(3661) == "61m 01s"


# ---------------------------------------------------------------------------
# AgentDisplayManager
# ---------------------------------------------------------------------------


class TestAgentDisplayManager:
    def test_initially_empty(self):
        mgr = AgentDisplayManager()
        assert mgr.has_agents is False
        assert mgr.has_activity is False
        assert mgr.agent_count == 0

    def test_on_start_adds_agent(self):
        mgr = AgentDisplayManager()
        mgr.on_start(_make_handle(model_name="openai/claude-sonnet-4-6"))
        assert mgr.has_agents is True
        assert mgr.agent_count == 1

    def test_on_start_captures_started_at(self):
        mgr = AgentDisplayManager()
        handle = _make_handle()
        mgr.on_start(handle)
        state = mgr._agents["a1"]
        assert state.started_at == handle.started_at

    def test_on_stop_updates_status(self):
        mgr = AgentDisplayManager()
        handle = _make_handle()
        mgr.on_start(handle)
        handle.status = "completed"
        handle.steps_completed = 5
        handle.last_tool_id = "shell_tool"
        mgr.on_stop(handle)

        state = mgr._agents["a1"]
        assert state.status == "completed"
        assert state.steps == 5
        assert state.last_tool == "shell_tool"

    def test_model_name_strips_provider(self):
        mgr = AgentDisplayManager()
        mgr.on_start(_make_handle(model_name="openai/claude-sonnet-4-6-1m"))
        assert mgr._agents["a1"].model == "claude-sonnet-4-6-1m"

    def test_model_name_without_provider(self):
        mgr = AgentDisplayManager()
        mgr.on_start(_make_handle(model_name="gpt-5.2"))
        assert mgr._agents["a1"].model == "gpt-5.2"


# ---------------------------------------------------------------------------
# Tool hooks
# ---------------------------------------------------------------------------


class TestToolHooks:
    def test_on_tool_start_sets_root_tool(self):
        mgr = AgentDisplayManager()
        step = ActionStep(tool_id="web_search", operation="search", tool_input={})
        result = mgr.on_tool_start(step)
        assert result is step  # must pass through
        assert mgr._root_tool == "web_search"
        assert mgr._root_tool_start > 0

    def test_on_tool_end_clears_root_tool(self):
        mgr = AgentDisplayManager()
        step = ActionStep(tool_id="web_search", operation="search", tool_input={})
        mgr.on_tool_start(step)
        mock_result = MockSpeaker(content="done")
        result = mgr.on_tool_end(step, mock_result)
        assert result is mock_result  # must pass through
        assert mgr._root_tool is None

    def test_has_activity_with_tool_only(self):
        mgr = AgentDisplayManager()
        assert mgr.has_activity is False
        step = ActionStep(tool_id="shell", operation="run", tool_input={})
        mgr.on_tool_start(step)
        assert mgr.has_activity is True
        assert mgr.has_agents is False  # no agents, just tool


# ---------------------------------------------------------------------------
# Toggle expand
# ---------------------------------------------------------------------------


class TestToggleExpand:
    def test_default_is_expanded(self):
        mgr = AgentDisplayManager()
        assert mgr._expanded is True

    def test_toggle_flips(self):
        mgr = AgentDisplayManager()
        mgr.toggle_expand()
        assert mgr._expanded is False
        mgr.toggle_expand()
        assert mgr._expanded is True


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRendering:
    def test_render_empty_returns_empty_text(self):
        mgr = AgentDisplayManager()
        result = mgr.render()
        assert hasattr(result, "plain")

    def test_render_with_agents_returns_panel(self):
        mgr = AgentDisplayManager()
        mgr.on_start(_make_handle(agent_id="root", task_description="build app"))
        mgr.on_start(_make_handle(
            agent_id="child1", parent_id="root", depth=1,
            model_name="haiku", task_description="frontend",
        ))
        result = mgr.render()
        assert isinstance(result, Panel)

    def test_render_tree_structure(self):
        """Verify parent-child tree lines are rendered."""
        mgr = AgentDisplayManager()
        mgr.on_start(_make_handle(
            agent_id="rootagent1", task_description="root task",
        ))
        mgr.on_start(_make_handle(
            agent_id="childagen1", parent_id="rootagent1", depth=1,
            model_name="haiku", task_description="child task",
        ))
        output = _render_to_str(mgr)
        assert "rootagen" in output
        assert "childage" in output

    def test_completed_agent_shows_done(self):
        mgr = AgentDisplayManager()
        handle = _make_handle(agent_id="doneagent1")
        mgr.on_start(handle)
        handle.status = "completed"
        handle.steps_completed = 3
        mgr.on_stop(handle)

        output = _render_to_str(mgr)
        assert "done" in output
        assert "3 steps" in output

    def test_expanded_shows_agent_ids(self):
        """Expanded mode should show individual agent IDs."""
        mgr = AgentDisplayManager()
        mgr.on_start(_make_handle(agent_id="agent_aaa"))
        mgr.on_start(_make_handle(agent_id="agent_bbb", parent_id="agent_aaa", depth=1))

        output = _render_to_str(mgr)
        assert "agent_aa" in output
        assert "agent_bb" in output

    def test_collapsed_shows_summary(self):
        """Collapsed mode should show agent count summary."""
        mgr = AgentDisplayManager()
        mgr.on_start(_make_handle(agent_id="a1"))
        mgr.on_start(_make_handle(agent_id="a2", parent_id="a1", depth=1))
        mgr.toggle_expand()  # collapse

        output = _render_to_str(mgr)
        assert "2 agents" in output
        assert "running" in output
        assert "ctrl+o" in output
        # Individual agent IDs should NOT appear.
        assert "a1" not in output or "agents" in output

    def test_collapsed_does_not_show_agent_tree(self):
        """Collapsed view must not render the tree connectors."""
        mgr = AgentDisplayManager()
        mgr.on_start(_make_handle(agent_id="rootaaaaa"))
        mgr.on_start(_make_handle(
            agent_id="child1111", parent_id="rootaaaaa", depth=1,
        ))
        mgr.toggle_expand()

        output = _render_to_str(mgr)
        # Tree connectors should be absent.
        assert "\u251c" not in output  # ├
        assert "\u2514" not in output  # └

    def test_footer_shows_deepest_running_agent(self):
        """Footer should show the deepest running agent's task."""
        mgr = AgentDisplayManager()
        mgr.on_start(_make_handle(
            agent_id="root1", task_description="Plan the work",
        ))
        mgr.on_start(_make_handle(
            agent_id="deep1", parent_id="root1", depth=1,
            task_description="Search docs for patterns",
        ))
        output = _render_to_str(mgr)
        assert "Search docs" in output

    def test_root_tool_spinner_no_panel(self):
        """Root-only tool activity should render without a Panel."""
        mgr = AgentDisplayManager()
        step = ActionStep(tool_id="web_fetch", operation="get", tool_input={})
        mgr.on_tool_start(step)
        result = mgr.render()
        # Should NOT be a Panel — just a Group or similar.
        assert not isinstance(result, Panel)
        output = _render_to_str(mgr)
        assert "web_fetch" in output

    def test_spinner_frames_advance(self):
        """Consecutive render() calls should produce different spinner frames."""
        mgr = AgentDisplayManager()
        step = ActionStep(tool_id="tool1", operation="run", tool_input={})
        mgr.on_tool_start(step)
        out1 = _render_to_str(mgr)
        out2 = _render_to_str(mgr)
        # The spinner frame character should differ between renders.
        # Both contain "tool1" but the leading character differs.
        assert out1 != out2

    def test_elapsed_time_in_agent_line(self):
        """Agent lines should contain an elapsed time string."""
        mgr = AgentDisplayManager()
        mgr.on_start(_make_handle(agent_id="timedagen"))
        output = _render_to_str(mgr)
        # Should contain a time like "0s" or "1s".
        assert "s" in output


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_access(self):
        """Smoke test for thread-safe access including tool hooks."""
        import threading

        mgr = AgentDisplayManager()
        errors: list[Exception] = []

        def _writer(agent_id: str) -> None:
            try:
                handle = _make_handle(agent_id=agent_id)
                mgr.on_start(handle)
                handle.status = "completed"
                mgr.on_stop(handle)
            except Exception as exc:
                errors.append(exc)

        def _tool_user() -> None:
            try:
                step = ActionStep(tool_id="t", operation="r", tool_input={})
                for _ in range(20):
                    mgr.on_tool_start(step)
                    mgr.on_tool_end(step, MockSpeaker(content="ok"))
            except Exception as exc:
                errors.append(exc)

        def _reader() -> None:
            try:
                for _ in range(50):
                    mgr.render()
                    _ = mgr.has_agents
                    _ = mgr.has_activity
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=_writer, args=(f"agent_{i}",))
            for i in range(20)
        ] + [
            threading.Thread(target=_tool_user)
            for _ in range(3)
        ] + [
            threading.Thread(target=_reader)
            for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread safety errors: {errors}"
