#!/usr/bin/env python3
"""Tests for the async tool-use conversation loop."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage

from meeseeks_core.agent_context import AgentContext, AgentRegistry
from meeseeks_core.classes import ActionStep, Plan, PlanStep
from meeseeks_core.context import ContextSnapshot
from meeseeks_core.hooks import HookManager
from meeseeks_core.permissions import PermissionDecision, PermissionPolicy
from meeseeks_core.token_budget import TokenBudget
from meeseeks_core.tool_registry import ToolRegistry, ToolSpec
from meeseeks_core.tool_use_loop import (
    ToolUseLoop,
    _coerce_mcp_tool_input,
    _infer_operation,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context() -> ContextSnapshot:
    return ContextSnapshot(
        summary=None,
        recent_events=[],
        selected_events=None,
        events=[],
        budget=TokenBudget(
            total_tokens=0,
            summary_tokens=0,
            event_tokens=0,
            context_window=128000,
            remaining_tokens=128000,
            utilization=0.0,
            threshold=0.8,
        ),
    )


def _make_spec(tool_id: str = "test_tool", description: str = "A test tool") -> ToolSpec:
    return ToolSpec(
        tool_id=tool_id,
        name=tool_id,
        description=description,
        factory=lambda: MagicMock(),
        enabled=True,
        kind="local",
        metadata={
            "schema": {
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
            }
        },
    )


def _make_registry(*specs: ToolSpec) -> ToolRegistry:
    registry = ToolRegistry()
    for spec in specs:
        registry.register(spec)
    return registry


def _allow_all_policy() -> PermissionPolicy:
    policy = MagicMock(spec=PermissionPolicy)
    policy.decide.return_value = PermissionDecision.ALLOW
    return policy


def _make_hook_manager() -> HookManager:
    hm = MagicMock(spec=HookManager)
    hm.run_pre_tool_use.side_effect = lambda step: step
    hm.run_post_tool_use.side_effect = lambda step, result: result
    hm.run_permission_request.side_effect = lambda step, decision: decision
    return hm


def _make_agent_context(
    *,
    model_name: str = "test-model",
    should_cancel=None,
    max_depth: int = 5,
) -> AgentContext:
    """Create a root AgentContext for tests."""
    return AgentContext.root(
        model_name=model_name,
        max_depth=max_depth,
        should_cancel=should_cancel,
        registry=AgentRegistry(max_concurrent=100),
    )


def _text_response(content: str) -> AIMessage:
    """AIMessage with no tool calls (text-only)."""
    return AIMessage(content=content)


def _tool_call_response(tool_id: str, args: dict, call_id: str = "call_1") -> AIMessage:
    """AIMessage with a single tool call."""
    return AIMessage(
        content="",
        tool_calls=[{"name": tool_id, "args": args, "id": call_id}],
    )


# ---------------------------------------------------------------------------
# _infer_operation
# ---------------------------------------------------------------------------


class TestInferOperation:
    def test_shell_is_set(self):
        assert _infer_operation("aider_shell_tool") == "set"

    def test_edit_is_set(self):
        assert _infer_operation("aider_edit_block_tool") == "set"

    def test_read_is_get(self):
        assert _infer_operation("aider_read_file_tool") == "get"

    def test_list_is_get(self):
        assert _infer_operation("aider_list_dir_tool") == "get"

    def test_unknown_defaults_to_set(self):
        assert _infer_operation("some_random_tool") == "set"

    def test_web_search_is_get(self):
        assert _infer_operation("mcp_internet_search_web_search") == "get"


# ---------------------------------------------------------------------------
# _coerce_mcp_tool_input
# ---------------------------------------------------------------------------


class TestCoerceMcpToolInput:
    def test_non_mcp_returns_none(self):
        spec = _make_spec()
        step = ActionStep(tool_id="test", operation="set", tool_input="hello")
        assert _coerce_mcp_tool_input(step, spec) is None

    def test_mcp_string_to_dict(self):
        spec = ToolSpec(
            tool_id="mcp_test",
            name="test",
            description="test",
            factory=lambda: MagicMock(),
            kind="mcp",
            metadata={
                "schema": {
                    "required": ["query"],
                    "properties": {"query": {"type": "string"}},
                }
            },
        )
        step = ActionStep(tool_id="mcp_test", operation="get", tool_input="hello world")
        result = _coerce_mcp_tool_input(step, spec)
        assert result is None
        assert step.tool_input == {"query": "hello world"}

    def test_mcp_valid_dict_passes(self):
        spec = ToolSpec(
            tool_id="mcp_test",
            name="test",
            description="test",
            factory=lambda: MagicMock(),
            kind="mcp",
            metadata={
                "schema": {
                    "required": ["query"],
                    "properties": {"query": {"type": "string"}},
                }
            },
        )
        step = ActionStep(tool_id="mcp_test", operation="get", tool_input={"query": "hi"})
        result = _coerce_mcp_tool_input(step, spec)
        assert result is None


# ---------------------------------------------------------------------------
# ToolUseLoop (async)
# ---------------------------------------------------------------------------


class TestToolUseLoopTextResponse:
    """Test that a text-only response completes the loop immediately."""

    def test_text_response_returns_content(self):
        spec = _make_spec()
        registry = _make_registry(spec)
        fake_model = MagicMock()
        fake_model.ainvoke = AsyncMock(return_value=_text_response("The answer is 42."))
        bound = MagicMock()
        bound.ainvoke = fake_model.ainvoke

        with patch("meeseeks_core.tool_use_loop.build_chat_model") as mock_build:
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound

            loop = ToolUseLoop(
                agent_context=_make_agent_context(),
                tool_registry=registry,
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )
            tq, state = asyncio.run(
                loop.run("What is 6*7?", tool_specs=[spec], context=_make_context())
            )

        assert state.done is True
        assert state.done_reason == "completed"
        assert "42" in (tq.task_result or "")
        assert len(tq.action_steps) == 0
        assert fake_model.ainvoke.call_count == 1


class TestToolUseLoopToolCall:
    """Test a single tool call followed by a text response."""

    def test_tool_then_text(self):
        spec = _make_spec("aider_shell_tool", "Run shell commands")
        registry = _make_registry(spec)

        fake_model = MagicMock()
        fake_model.ainvoke = AsyncMock(
            side_effect=[
                _tool_call_response("aider_shell_tool", {"command": "echo hello"}, "call_1"),
                _text_response("Done. Output: hello"),
            ]
        )
        bound = MagicMock()
        bound.ainvoke = fake_model.ainvoke

        mock_tool = MagicMock()
        mock_speaker = MagicMock()
        mock_speaker.content = "hello\n"
        mock_tool.run.return_value = mock_speaker

        with (
            patch("meeseeks_core.tool_use_loop.build_chat_model") as mock_build,
            patch.object(registry, "get", return_value=mock_tool),
        ):
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound

            loop = ToolUseLoop(
                agent_context=_make_agent_context(),
                tool_registry=registry,
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )
            tq, state = asyncio.run(
                loop.run("Run echo hello", tool_specs=[spec], context=_make_context())
            )

        assert state.done is True
        assert state.done_reason == "completed"
        assert len(tq.action_steps) == 1
        assert tq.action_steps[0].tool_id == "aider_shell_tool"
        assert fake_model.ainvoke.call_count == 2


class TestToolUseLoopMaxSteps:
    """Test that max_steps terminates the loop."""

    def test_max_steps_reached(self):
        spec = _make_spec("aider_shell_tool", "Run shell")
        registry = _make_registry(spec)

        fake_model = MagicMock()
        fake_model.ainvoke = AsyncMock(
            return_value=_tool_call_response("aider_shell_tool", {"command": "ls"}, "call_loop")
        )
        bound = MagicMock()
        bound.ainvoke = fake_model.ainvoke

        mock_tool = MagicMock()
        mock_speaker = MagicMock()
        mock_speaker.content = "file.txt"
        mock_tool.run.return_value = mock_speaker

        with (
            patch("meeseeks_core.tool_use_loop.build_chat_model") as mock_build,
            patch.object(registry, "get", return_value=mock_tool),
        ):
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound

            loop = ToolUseLoop(
                agent_context=_make_agent_context(),
                tool_registry=registry,
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )
            tq, state = asyncio.run(
                loop.run("keep looping", tool_specs=[spec], context=_make_context(), max_steps=3)
            )

        assert state.done is True
        assert state.done_reason == "max_steps_reached"
        assert len(tq.action_steps) == 3


class TestToolUseLoopPermissionDenied:
    """Test that permission denial is fed back as a ToolMessage."""

    def test_denied_tool_appears_in_result(self):
        spec = _make_spec("aider_shell_tool", "Run shell")
        registry = _make_registry(spec)

        fake_model = MagicMock()
        fake_model.ainvoke = AsyncMock(
            side_effect=[
                _tool_call_response("aider_shell_tool", {"command": "rm -rf /"}, "call_1"),
                _text_response("Permission was denied."),
            ]
        )
        bound = MagicMock()
        bound.ainvoke = fake_model.ainvoke

        deny_policy = MagicMock(spec=PermissionPolicy)
        deny_policy.decide.return_value = PermissionDecision.DENY

        with patch("meeseeks_core.tool_use_loop.build_chat_model") as mock_build:
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound

            loop = ToolUseLoop(
                agent_context=_make_agent_context(),
                tool_registry=registry,
                permission_policy=deny_policy,
                hook_manager=_make_hook_manager(),
            )
            tq, state = asyncio.run(
                loop.run("destroy everything", tool_specs=[spec], context=_make_context())
            )

        assert state.done is True
        assert "denied" in (tq.task_result or "").lower() or tq.last_error is not None


class TestToolUseLoopWithPlan:
    """Test that a plan is embedded in the system prompt."""

    def test_plan_in_system_prompt(self):
        spec = _make_spec()
        registry = _make_registry(spec)
        plan = Plan(
            steps=[
                PlanStep(title="Step 1", description="Do the first thing"),
                PlanStep(title="Step 2", description="Do the second thing"),
            ]
        )

        fake_model = MagicMock()
        fake_model.ainvoke = AsyncMock(return_value=_text_response("Plan executed."))
        bound = MagicMock()
        bound.ainvoke = fake_model.ainvoke

        with patch("meeseeks_core.tool_use_loop.build_chat_model") as mock_build:
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound

            loop = ToolUseLoop(
                agent_context=_make_agent_context(),
                tool_registry=registry,
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )
            tq, state = asyncio.run(
                loop.run("Execute my plan", tool_specs=[spec], context=_make_context(), plan=plan)
            )

        call_args = fake_model.ainvoke.call_args
        messages = call_args[0][0]
        system_content = messages[0].content
        assert "Step 1" in system_content
        assert "Step 2" in system_content
        assert "Execute this plan" in system_content


class TestToolUseLoopCancel:
    """Test cancellation mid-loop."""

    def test_cancel_stops_loop(self):
        spec = _make_spec()
        registry = _make_registry(spec)

        fake_model = MagicMock()
        fake_model.ainvoke = AsyncMock(return_value=_text_response("should not reach"))
        bound = MagicMock()
        bound.ainvoke = fake_model.ainvoke

        with patch("meeseeks_core.tool_use_loop.build_chat_model") as mock_build:
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound

            ctx = _make_agent_context(should_cancel=lambda: True)
            loop = ToolUseLoop(
                agent_context=ctx,
                tool_registry=registry,
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )
            tq, state = asyncio.run(
                loop.run("do stuff", tool_specs=[spec], context=_make_context())
            )

        assert state.done is True
        assert state.done_reason == "canceled"
        assert fake_model.ainvoke.call_count == 0
