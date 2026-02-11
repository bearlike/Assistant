"""Tests for action runner execution paths."""

import pytest
from meeseeks_core.action_runner import ActionPlanRunner
from meeseeks_core.classes import ActionStep, TaskQueue, set_available_tools
from meeseeks_core.common import get_mock_speaker
from meeseeks_core.errors import ToolInputError
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
        tool_id="missing_tool",
        operation="get",
        tool_input="ping",
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
        approval_callback=lambda _step: True,
        hook_manager=default_hook_manager(),
    )
    step = ActionStep(
        tool_id="dummy_tool",
        operation="get",
        tool_input="ping",
    )
    outcome = runner._execute_step(step)
    assert outcome.content == ""
    assert step.result is None


def test_action_runner_blocks_tools_in_plan_mode():
    """Disallow non-plan-safe tools when running in plan mode."""
    registry = ToolRegistry()

    class DummyTool:
        def run(self, _step):
            return None

    registry.register(
        ToolSpec(
            tool_id="unsafe_tool",
            name="Unsafe",
            description="Unsafe tool",
            factory=lambda: DummyTool(),
        )
    )
    events = []

    runner = ActionPlanRunner(
        tool_registry=registry,
        permission_policy=PermissionPolicy(),
        approval_callback=None,
        hook_manager=default_hook_manager(),
        event_logger=events.append,
        allowed_tool_ids=set(),
        mode="plan",
    )
    task_queue = TaskQueue(
        action_steps=[
            ActionStep(
                tool_id="unsafe_tool",
                operation="set",
                tool_input="payload",
            )
        ]
    )
    task_queue = runner.run(task_queue)
    assert task_queue.last_error
    assert "tool not allowed" in task_queue.last_error
    assert "tool not allowed" in task_queue.task_result
    assert events
    payload = events[-1]["payload"]
    assert payload.get("success") is False
    assert "tool not allowed" in payload.get("summary", "")


def test_action_runner_allows_set_when_approved():
    """Allow set actions when approval callback returns True."""
    registry = ToolRegistry()
    set_available_tools(["dummy_tool"])
    events = []
    called = {"value": False}

    class DummyTool:
        def run(self, _step):
            called["value"] = True
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
        approval_callback=lambda _step: True,
        hook_manager=default_hook_manager(),
        event_logger=events.append,
    )
    task_queue = TaskQueue(
        action_steps=[
            ActionStep(
                tool_id="dummy_tool",
                operation="set",
                tool_input="payload",
            )
        ]
    )
    task_queue = runner.run(task_queue)
    assert called["value"] is True
    assert task_queue.last_error is None
    permission_events = [event for event in events if event.get("type") == "permission"]
    assert permission_events
    assert permission_events[0]["payload"]["decision"] == "allow"


def test_action_runner_denies_set_without_approval():
    """Deny set actions when approval callback is missing."""
    registry = ToolRegistry()
    set_available_tools(["dummy_tool"])
    events = []
    called = {"value": False}

    class DummyTool:
        def run(self, _step):
            called["value"] = True
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
        event_logger=events.append,
    )
    task_queue = TaskQueue(
        action_steps=[
            ActionStep(
                tool_id="dummy_tool",
                operation="set",
                tool_input="payload",
            )
        ]
    )
    task_queue = runner.run(task_queue)
    assert called["value"] is False
    assert task_queue.last_error
    assert "permission denied" in task_queue.last_error
    permission_events = [event for event in events if event.get("type") == "permission"]
    assert permission_events
    assert permission_events[0]["payload"]["decision"] == "deny"


def test_action_runner_records_tool_errors_in_task_result():
    """Surface tool failures in task_result for LLM context."""
    registry = ToolRegistry()

    class ExplodingTool:
        def run(self, _step):
            raise RuntimeError("boom")

    registry.register(
        ToolSpec(
            tool_id="exploding_tool",
            name="Exploding",
            description="Always fails",
            factory=lambda: ExplodingTool(),
        )
    )
    runner = ActionPlanRunner(
        tool_registry=registry,
        permission_policy=PermissionPolicy(),
        approval_callback=lambda _step: True,
        hook_manager=default_hook_manager(),
    )
    task_queue = TaskQueue(
        action_steps=[
            ActionStep(
                tool_id="exploding_tool",
                operation="get",
                tool_input="payload",
            )
        ]
    )
    task_queue = runner.run(task_queue)
    assert task_queue.last_error
    assert "boom" in task_queue.last_error
    assert "ERROR: boom" in task_queue.task_result


def test_action_runner_preserves_tool_on_input_error():
    """Do not disable tools for expected input errors."""
    registry = ToolRegistry()

    class InputErrorTool:
        def run(self, _step):
            raise ToolInputError("bad input")

    registry.register(
        ToolSpec(
            tool_id="input_error_tool",
            name="InputError",
            description="Fails on input",
            factory=lambda: InputErrorTool(),
        )
    )
    events = []
    runner = ActionPlanRunner(
        tool_registry=registry,
        permission_policy=PermissionPolicy(),
        approval_callback=lambda _step: True,
        hook_manager=default_hook_manager(),
        event_logger=events.append,
    )
    task_queue = TaskQueue(
        action_steps=[
            ActionStep(
                tool_id="input_error_tool",
                operation="get",
                tool_input="payload",
            )
        ]
    )
    task_queue = runner.run(task_queue)
    assert task_queue.last_error
    assert "bad input" in task_queue.last_error
    spec = registry.get_spec("input_error_tool")
    assert spec is not None
    assert spec.enabled is True
    assert events
    assert events[-1]["payload"]["success"] is False


def test_action_runner_preserves_mcp_tool_on_runtime_error():
    """Do not disable MCP tools on transient runtime errors."""
    registry = ToolRegistry()

    class ExplodingTool:
        def run(self, _step):
            raise RuntimeError("network aborted")

    registry.register(
        ToolSpec(
            tool_id="mcp_tool",
            name="MCP Tool",
            description="MCP tool",
            factory=lambda: ExplodingTool(),
            kind="mcp",
        )
    )
    runner = ActionPlanRunner(
        tool_registry=registry,
        permission_policy=PermissionPolicy(),
        approval_callback=lambda _step: True,
        hook_manager=default_hook_manager(),
    )
    task_queue = TaskQueue(
        action_steps=[
            ActionStep(
                tool_id="mcp_tool",
                operation="get",
                tool_input="payload",
            )
        ]
    )
    task_queue = runner.run(task_queue)
    assert task_queue.last_error
    spec = registry.get_spec("mcp_tool")
    assert spec is not None
    assert spec.enabled is True


def test_summarize_result_truncates_long_text():
    """Truncate long tool output summaries."""
    long_text = "x" * 600
    summary = ActionPlanRunner._summarize_result(long_text, None)
    assert summary.endswith("...")
    assert len(summary) == 500


def test_format_step_summary_skips_empty_result():
    """Skip summaries when tool output is empty."""
    step = ActionStep(
        tool_id="dummy",
        operation="get",
        tool_input="payload",
    )
    step.result = get_mock_speaker()(content="")
    assert ActionPlanRunner._format_step_summary(step) == ""


def test_summarize_result_none_returns_empty():
    """Return empty summaries for None results."""
    summary = ActionPlanRunner._summarize_result(None, None)
    assert summary == ""


def test_action_runner_coerces_json_string_payload():
    """Parse JSON string tool_input into a structured payload."""
    step = ActionStep(
        tool_id="mcp_json_tool",
        operation="get",
        tool_input='{"query": "hello"}',
    )
    spec = ToolSpec(
        tool_id="mcp_json_tool",
        name="JSON tool",
        description="MCP tool",
        factory=lambda: object(),
        kind="mcp",
        metadata={
            "schema": {
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
            }
        },
    )
    error = ActionPlanRunner._coerce_mcp_tool_input(step, spec)
    assert error is None
    assert step.tool_input == {"query": "hello"}


def test_action_runner_preserves_output_on_reflection():
    """Keep tool output in tool_result payload when reflection requests retry/revise."""
    registry = ToolRegistry()
    events = []

    class DummyTool:
        def run(self, _step):
            return get_mock_speaker()(content="raw output")

    class DummyReflector:
        def reflect(self, *_args, **_kwargs):
            class DummyReflection:
                status = "retry"
                notes = "needs more"
                revised_argument = None

            return DummyReflection()

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
        approval_callback=lambda _step: True,
        hook_manager=default_hook_manager(),
        event_logger=events.append,
        reflector=DummyReflector(),
    )
    task_queue = TaskQueue(
        action_steps=[
            ActionStep(
                tool_id="dummy_tool",
                operation="get",
                tool_input="payload",
            )
        ]
    )
    task_queue = runner.run(task_queue)
    tool_events = [event for event in events if event.get("type") == "tool_result"]
    assert tool_events
    payload = tool_events[-1]["payload"]
    assert payload.get("success") is False
    assert payload.get("error")
    assert payload.get("result") == "raw output"
    assert task_queue.action_steps[0].result is not None
