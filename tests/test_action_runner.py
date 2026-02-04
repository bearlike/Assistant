"""Tests for action runner execution paths."""

import pytest
from meeseeks_core.action_runner import ActionPlanRunner
from meeseeks_core.classes import ActionStep
from meeseeks_core.hooks import default_hook_manager
from meeseeks_core.permissions import PermissionPolicy
from meeseeks_core.tool_registry import ToolRegistry, ToolSpec


def test_execute_step_raises_when_tool_missing():
    """Raise when a tool is missing during execution."""
    runner = ActionPlanRunner(
        tool_registry=ToolRegistry(),
        permission_policy=PermissionPolicy(),
        approval_callback=None,
        hook_manager=default_hook_manager(),
    )
    step = ActionStep(
        action_consumer="missing_tool",
        action_type="get",
        action_argument="ping",
    )
    with pytest.raises(RuntimeError):
        runner._execute_step(step)


def test_execute_step_content_fallback():
    """Fallback to empty string when tool returns None."""
    registry = ToolRegistry()

    class DummyTool:
        def run(self, _step):
            return None

    registry.register(
        ToolSpec(
            tool_id="dummy_tool",
            name="Dummy",
            description="Dummy tool",
            factory=lambda: DummyTool(),
        )
    )
    runner = ActionPlanRunner(
        tool_registry=registry,
        permission_policy=PermissionPolicy(),
        approval_callback=None,
        hook_manager=default_hook_manager(),
    )
    step = ActionStep(
        action_consumer="dummy_tool",
        action_type="get",
        action_argument="ping",
    )
    outcome = runner._execute_step(step)
    assert outcome.content == ""
    assert step.result is None
