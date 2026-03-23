#!/usr/bin/env python3
"""Tests for CLI agent display manager."""

from __future__ import annotations

from meeseeks_core.agent_context import AgentHandle
from meeseeks_cli.cli_agent_display import AgentDisplayManager, AgentDisplayState


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


# ---------------------------------------------------------------------------
# AgentDisplayManager
# ---------------------------------------------------------------------------


class TestAgentDisplayManager:
    def test_initially_empty(self):
        mgr = AgentDisplayManager()
        assert mgr.has_agents is False
        assert mgr.agent_count == 0

    def test_on_start_adds_agent(self):
        mgr = AgentDisplayManager()
        handle = AgentHandle(
            agent_id="a1", parent_id=None, depth=0,
            model_name="openai/claude-sonnet-4-6", task_description="test task",
        )
        mgr.on_start(handle)
        assert mgr.has_agents is True
        assert mgr.agent_count == 1

    def test_on_stop_updates_status(self):
        mgr = AgentDisplayManager()
        handle = AgentHandle(
            agent_id="a1", parent_id=None, depth=0,
            model_name="m", task_description="test",
        )
        mgr.on_start(handle)
        handle.status = "completed"
        handle.steps_completed = 5
        handle.last_tool_id = "shell_tool"
        mgr.on_stop(handle)

        # Check internal state.
        state = mgr._agents["a1"]
        assert state.status == "completed"
        assert state.steps == 5
        assert state.last_tool == "shell_tool"

    def test_model_name_strips_provider(self):
        mgr = AgentDisplayManager()
        handle = AgentHandle(
            agent_id="a1", parent_id=None, depth=0,
            model_name="openai/claude-sonnet-4-6-1m",
            task_description="test",
        )
        mgr.on_start(handle)
        assert mgr._agents["a1"].model == "claude-sonnet-4-6-1m"

    def test_model_name_without_provider(self):
        mgr = AgentDisplayManager()
        handle = AgentHandle(
            agent_id="a1", parent_id=None, depth=0,
            model_name="gpt-5.2",
            task_description="test",
        )
        mgr.on_start(handle)
        assert mgr._agents["a1"].model == "gpt-5.2"

    def test_render_empty_returns_empty_text(self):
        mgr = AgentDisplayManager()
        result = mgr.render()
        # Should be a Text object (empty renderable).
        assert hasattr(result, "plain")

    def test_render_with_agents_returns_panel(self):
        mgr = AgentDisplayManager()
        mgr.on_start(AgentHandle(
            agent_id="root", parent_id=None, depth=0,
            model_name="sonnet", task_description="build app",
        ))
        mgr.on_start(AgentHandle(
            agent_id="child1", parent_id="root", depth=1,
            model_name="haiku", task_description="frontend",
        ))
        result = mgr.render()
        # Should be a Panel containing the agent tree.
        from rich.panel import Panel
        assert isinstance(result, Panel)

    def test_render_tree_structure(self):
        """Verify parent-child tree lines are rendered."""
        from rich.console import Console
        from io import StringIO

        mgr = AgentDisplayManager()
        mgr.on_start(AgentHandle(
            agent_id="rootagent1", parent_id=None, depth=0,
            model_name="sonnet", task_description="root task",
        ))
        mgr.on_start(AgentHandle(
            agent_id="childagen1", parent_id="rootagent1", depth=1,
            model_name="haiku", task_description="child task",
        ))

        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=80)
        console.print(mgr.render())
        output = buf.getvalue()

        # Root agent should appear.
        assert "rootagen" in output
        # Child agent should appear with tree connector.
        assert "childage" in output

    def test_completed_agent_shows_done(self):
        from rich.console import Console
        from io import StringIO

        mgr = AgentDisplayManager()
        handle = AgentHandle(
            agent_id="doneagent1", parent_id=None, depth=0,
            model_name="m", task_description="test",
        )
        mgr.on_start(handle)
        handle.status = "completed"
        handle.steps_completed = 3
        mgr.on_stop(handle)

        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=80)
        console.print(mgr.render())
        output = buf.getvalue()
        assert "done" in output
        assert "3 steps" in output

    def test_thread_safety_concurrent_access(self):
        """Basic thread-safety smoke test."""
        import threading

        mgr = AgentDisplayManager()
        errors: list[Exception] = []

        def _writer(agent_id: str):
            try:
                handle = AgentHandle(
                    agent_id=agent_id, parent_id=None, depth=0,
                    model_name="m", task_description="test",
                )
                mgr.on_start(handle)
                handle.status = "completed"
                mgr.on_stop(handle)
            except Exception as exc:
                errors.append(exc)

        def _reader():
            try:
                for _ in range(50):
                    mgr.render()
                    _ = mgr.has_agents
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=_writer, args=(f"agent_{i}",))
            for i in range(20)
        ] + [
            threading.Thread(target=_reader)
            for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread safety errors: {errors}"
