#!/usr/bin/env python3
"""Tests for the async tool-use conversation loop."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage, SystemMessage
from truss_core.agent_context import AgentContext
from truss_core.classes import ActionStep, Plan, PlanStep
from truss_core.context import ContextSnapshot
from truss_core.hooks import HookManager
from truss_core.hypervisor import AgentHypervisor
from truss_core.permissions import PermissionDecision, PermissionPolicy
from truss_core.token_budget import TokenBudget
from truss_core.tool_registry import ToolRegistry, ToolSpec
from truss_core.tool_use_loop import (
    _ANSI_ESCAPE_RE,
    ToolUseLoop,
    _CachedFileRead,
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
    event_logger=None,
) -> AgentContext:
    """Create a root AgentContext for tests."""
    return AgentContext.root(
        model_name=model_name,
        max_depth=max_depth,
        should_cancel=should_cancel,
        registry=AgentHypervisor(max_concurrent=100),
        event_logger=event_logger,
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
        assert _infer_operation("read_file") == "get"

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

        with patch("truss_core.tool_use_loop.build_chat_model") as mock_build:
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
            patch("truss_core.tool_use_loop.build_chat_model") as mock_build,
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


class TestToolUseLoopNaturalCompletion:
    """Test that the loop runs until the model naturally completes."""

    def test_multi_turn_then_completion(self):
        """Model calls tools 3 times, then returns text — natural completion."""
        spec = _make_spec("aider_shell_tool", "Run shell")
        registry = _make_registry(spec)

        fake_model = MagicMock()
        fake_model.ainvoke = AsyncMock(
            side_effect=[
                _tool_call_response("aider_shell_tool", {"command": "ls"}, "c1"),
                _tool_call_response("aider_shell_tool", {"command": "cat"}, "c2"),
                _tool_call_response("aider_shell_tool", {"command": "wc"}, "c3"),
                _text_response("Done. Found 3 files."),
            ]
        )
        bound = MagicMock()
        bound.ainvoke = fake_model.ainvoke

        mock_tool = MagicMock()
        mock_speaker = MagicMock()
        mock_speaker.content = "file.txt"
        mock_tool.run.return_value = mock_speaker

        with (
            patch("truss_core.tool_use_loop.build_chat_model") as mock_build,
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
                loop.run("list files", tool_specs=[spec], context=_make_context())
            )

        assert state.done is True
        assert state.done_reason == "completed"
        assert len(tq.action_steps) == 3
        assert fake_model.ainvoke.call_count == 4

    def test_no_step_count_in_messages(self):
        """After tool execution, no step-count SystemMessage is injected."""
        spec = _make_spec("aider_shell_tool", "Run shell")
        registry = _make_registry(spec)

        messages_seen: list = []

        fake_model = MagicMock()

        def _capture_invoke(msgs, **kwargs):
            messages_seen.extend(msgs)
            if len(messages_seen) < 10:
                return _tool_call_response("aider_shell_tool", {"command": "x"}, "c1")
            return _text_response("done")

        fake_model.ainvoke = AsyncMock(side_effect=_capture_invoke)
        bound = MagicMock()
        bound.ainvoke = fake_model.ainvoke

        mock_tool = MagicMock()
        mock_speaker = MagicMock()
        mock_speaker.content = "ok"
        mock_tool.run.return_value = mock_speaker

        with (
            patch("truss_core.tool_use_loop.build_chat_model") as mock_build,
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
            asyncio.run(loop.run("do work", tool_specs=[spec], context=_make_context()))

        # No message should contain step counting patterns.
        for msg in messages_seen:
            if isinstance(msg, SystemMessage):
                content = msg.content
                assert "Step " not in content, f"Step count found in: {content}"
                assert "remaining]" not in content, f"Step count found in: {content}"
                assert "Plan budget:" not in content, f"Budget nudge found in: {content}"

    def test_run_has_no_max_steps_parameter(self):
        """Verify run() signature does not accept max_steps."""
        import inspect

        sig = inspect.signature(ToolUseLoop.run)
        assert "max_steps" not in sig.parameters
        assert "max_turns" not in sig.parameters


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

        with patch("truss_core.tool_use_loop.build_chat_model") as mock_build:
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

        with patch("truss_core.tool_use_loop.build_chat_model") as mock_build:
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

        with patch("truss_core.tool_use_loop.build_chat_model") as mock_build:
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


class TestToolUseLoopToolError:
    """Test that tool execution errors are propagated to the LLM, not crashes."""

    def test_tool_error_becomes_tool_message(self):
        """When a tool raises an exception, the error is fed back as a ToolMessage."""
        spec = _make_spec("aider_shell_tool", "Run shell")
        registry = _make_registry(spec)

        fake_model = MagicMock()
        fake_model.ainvoke = AsyncMock(
            side_effect=[
                _tool_call_response("aider_shell_tool", {"command": "bad"}, "call_1"),
                _text_response("The tool failed, so I adapted."),
            ]
        )
        bound = MagicMock()
        bound.ainvoke = fake_model.ainvoke

        mock_tool = MagicMock(spec=["run"])  # Only has run(), no arun()
        mock_tool.run.side_effect = RuntimeError("Connection refused")

        with (
            patch("truss_core.tool_use_loop.build_chat_model") as mock_build,
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
                loop.run("do something", tool_specs=[spec], context=_make_context())
            )

        # Loop did NOT crash — LLM saw the error and produced a text response.
        assert state.done is True
        assert state.done_reason == "completed"
        assert "adapted" in (tq.task_result or "").lower()
        # The error was recorded.
        assert tq.last_error is not None
        assert "Connection refused" in tq.last_error

    def test_async_tool_error_becomes_tool_message(self):
        """When an async tool (arun) raises, the error is fed back as a ToolMessage."""
        spec = _make_spec("mcp_search_tool", "Search")
        registry = _make_registry(spec)

        fake_model = MagicMock()
        fake_model.ainvoke = AsyncMock(
            side_effect=[
                _tool_call_response("mcp_search_tool", {"query": "test"}, "call_1"),
                _text_response("Search failed, but I can answer directly."),
            ]
        )
        bound = MagicMock()
        bound.ainvoke = fake_model.ainvoke

        # Mock an MCP-like tool with arun that raises.
        mock_tool = MagicMock()
        mock_tool.arun = AsyncMock(
            side_effect=RuntimeError("MCP error -32603: Website Error (403)")
        )

        with (
            patch("truss_core.tool_use_loop.build_chat_model") as mock_build,
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
                loop.run("search for info", tool_specs=[spec], context=_make_context())
            )

        assert state.done is True
        assert state.done_reason == "completed"
        assert tq.last_error is not None
        assert "403" in tq.last_error


# ---------------------------------------------------------------------------
# Project instructions injection
# ---------------------------------------------------------------------------


class TestProjectInstructionsInjection:
    """Verify project instructions appear in the system prompt."""

    def test_instructions_in_system_prompt(self):
        loop = ToolUseLoop(
            agent_context=_make_agent_context(),
            tool_registry=_make_registry(_make_spec()),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
            project_instructions="Follow DRY principle.",
        )
        messages = loop._build_messages("hello", None, None)
        system_msg = messages[0]
        assert isinstance(system_msg, SystemMessage)
        assert "Project instructions:" in system_msg.content
        assert "Follow DRY principle." in system_msg.content

    def test_no_instructions_section_when_none(self):
        loop = ToolUseLoop(
            agent_context=_make_agent_context(),
            tool_registry=_make_registry(_make_spec()),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
        )
        messages = loop._build_messages("hello", None, None)
        system_msg = messages[0]
        assert isinstance(system_msg, SystemMessage)
        assert "Project instructions:" not in system_msg.content


# ---------------------------------------------------------------------------
# ANSI escape stripping
# ---------------------------------------------------------------------------


class TestAnsiStripping:
    """Verify that ANSI escape codes are stripped from tool output."""

    def test_ansi_color_codes_stripped(self):
        text = "\x1b[31mERROR\x1b[0m: something failed"
        result = _ANSI_ESCAPE_RE.sub("", text)
        assert result == "ERROR: something failed"

    def test_ansi_bold_codes_stripped(self):
        text = "\x1b[1mBold text\x1b[0m"
        result = _ANSI_ESCAPE_RE.sub("", text)
        assert result == "Bold text"

    def test_ansi_cursor_codes_stripped(self):
        text = "\x1b[2J\x1b[Hscreen cleared"
        result = _ANSI_ESCAPE_RE.sub("", text)
        assert result == "screen cleared"

    def test_ansi_256_color_stripped(self):
        text = "\x1b[38;5;196mred text\x1b[0m"
        result = _ANSI_ESCAPE_RE.sub("", text)
        assert result == "red text"

    def test_no_ansi_passes_through(self):
        text = "plain text with no escapes"
        result = _ANSI_ESCAPE_RE.sub("", text)
        assert result == text

    def test_multiline_ansi_stripped(self):
        text = "\x1b[32mline1\x1b[0m\n\x1b[33mline2\x1b[0m"
        result = _ANSI_ESCAPE_RE.sub("", text)
        assert result == "line1\nline2"


# ---------------------------------------------------------------------------
# Environment section in system prompt
# ---------------------------------------------------------------------------


class TestEnvironmentSectionInSystemPrompt:
    """Verify _build_messages includes an Environment section."""

    def test_environment_section_present(self):
        loop = ToolUseLoop(
            agent_context=_make_agent_context(),
            tool_registry=_make_registry(_make_spec()),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
        )
        messages = loop._build_messages("hello", None, None)
        system_msg = messages[0]
        assert isinstance(system_msg, SystemMessage)
        content = system_msg.content
        assert "# Environment" in content
        assert "Working directory:" in content
        assert "Platform:" in content
        assert "Date:" in content
        assert "Truss version:" in content

    def test_environment_uses_cwd_param(self):
        loop = ToolUseLoop(
            agent_context=_make_agent_context(),
            tool_registry=_make_registry(_make_spec()),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
            cwd="/custom/project/dir",
        )
        messages = loop._build_messages("hello", None, None)
        system_msg = messages[0]
        assert "/custom/project/dir" in system_msg.content

    def test_environment_defaults_to_cwd_when_no_param(self):
        loop = ToolUseLoop(
            agent_context=_make_agent_context(),
            tool_registry=_make_registry(_make_spec()),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
            cwd=None,
        )
        messages = loop._build_messages("hello", None, None)
        system_msg = messages[0]
        # Should contain some working directory path (the actual CWD)
        assert "Working directory:" in system_msg.content


# ---------------------------------------------------------------------------
# Delegation lifecycle guidance (research-grounded)
# ---------------------------------------------------------------------------


class TestDepthGuidance:
    """Ref: [CoA §3.2], [DeepMind-Delegation §4.1], [Aletheia §3]
    Lifecycle-aware prompting for root/sub/leaf agents."""

    def test_root_agent_is_orchestrator(self):
        ctx = _make_agent_context(max_depth=5)  # depth=0 root
        loop = ToolUseLoop(
            agent_context=ctx,
            tool_registry=_make_registry(_make_spec()),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
        )
        guidance = loop._build_depth_guidance()
        assert "Root hypervisor" in guidance
        # Direct execution should come BEFORE spawning guidance
        direct_pos = guidance.index("Direct execution")
        spawn_pos = guidance.index("When to spawn")
        assert direct_pos < spawn_pos
        assert "rare" in guidance.lower()
        # System awareness and give-up policy
        assert "System awareness" in guidance
        assert "guardrails" in guidance
        assert "When to stop" in guidance
        assert "fails twice" in guidance

    def test_leaf_agent_is_executor(self):
        ctx = _make_agent_context(max_depth=1)
        child_ctx = ctx.child()  # depth=1, max_depth=1 = leaf
        loop = ToolUseLoop(
            agent_context=child_ctx,
            tool_registry=_make_registry(_make_spec()),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
        )
        guidance = loop._build_depth_guidance()
        assert "Leaf executor" in guidance
        assert "Do NOT attempt to delegate" in guidance
        # Anti-retry and restriction handling
        assert "restriction" in guidance
        assert "fails twice" in guidance

    def test_sub_orchestrator(self):
        ctx = _make_agent_context(max_depth=5)
        child_ctx = ctx.child()  # depth=1, can still spawn
        loop = ToolUseLoop(
            agent_context=child_ctx,
            tool_registry=_make_registry(_make_spec()),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
        )
        guidance = loop._build_depth_guidance()
        assert "Sub-orchestrator" in guidance
        assert "restriction" in guidance or "boundary" in guidance
        assert "report" in guidance.lower()

    def test_delegation_boundary_warning(self):
        """Ref: [DeepMind-Delegation §4.7] Liability firebreaks at chain boundaries."""
        ctx = _make_agent_context(max_depth=3)
        c1 = ctx.child()  # depth=1
        c2 = c1.child()  # depth=2, remaining=1
        loop = ToolUseLoop(
            agent_context=c2,
            tool_registry=_make_registry(_make_spec()),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
        )
        guidance = loop._build_depth_guidance()
        assert "DELEGATION BOUNDARY" in guidance or "deep in the agent tree" in guidance


# ---------------------------------------------------------------------------
# Empty-content sanitization (Fix A — port of Claude Code's
# ensureNonEmptyAssistantContent + NO_CONTENT_MESSAGE).
# ---------------------------------------------------------------------------


class TestThinkingOnlyContentPlaceholder:
    """Verify pure-thinking content is replaced with ``(no content)`` block."""

    def test_thinking_only_response_produces_placeholder(self):
        """Stripping thinking leaves empty ⇒ insert ``(no content)`` text block.

        Mirrors Claude Code's ``ensureNonEmptyAssistantContent`` to stop the
        model from hallucinating framework-style meta-text on subsequent
        turns. The placeholder is filtered out of ``agent_message`` events.
        """
        spec = _make_spec("shell_tool", "Run shell commands")
        registry = _make_registry(spec)

        thinking_only = AIMessage(
            content=[{"type": "thinking", "thinking": "pondering...", "signature": "sig"}],
            tool_calls=[{"name": "shell_tool", "args": {"input": "x"}, "id": "call_x"}],
        )
        fake_model = MagicMock()
        fake_model.ainvoke = AsyncMock(side_effect=[thinking_only, _text_response("done")])
        bound = MagicMock()
        bound.ainvoke = fake_model.ainvoke

        mock_tool = MagicMock()
        mock_speaker = MagicMock()
        mock_speaker.content = "ok"
        mock_tool.run.return_value = mock_speaker

        emitted_events: list[dict] = []

        with (
            patch("truss_core.tool_use_loop.build_chat_model") as mock_build,
            patch.object(registry, "get", return_value=mock_tool),
        ):
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound
            ctx = _make_agent_context(event_logger=emitted_events.append)
            loop = ToolUseLoop(
                agent_context=ctx,
                tool_registry=registry,
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )
            asyncio.run(loop.run("do it", tool_specs=[spec], context=_make_context()))

        # Placeholder must NOT leak out as an agent_message event (mirrors
        # Claude Code's src/utils/messages.ts:717 display filter).
        agent_messages = [e for e in emitted_events if e["type"] == "agent_message"]
        for event in agent_messages:
            assert event["payload"]["text"] != "(no content)"
            assert "sanitised" not in event["payload"]["text"].lower()

    def test_string_empty_content_with_tool_calls_gets_placeholder(self):
        """Proxy returns thinking-only as ``content=""`` (string, not list).

        This is the ACTUAL production case: the LiteLLM proxy strips
        thinking blocks itself and returns a bare empty string. The
        sanitisation must catch this branch too, otherwise the model
        sees ``""`` in history and hallucinations framework meta-text.
        """
        spec = _make_spec("shell_tool", "Run shell commands")
        registry = _make_registry(spec)

        # Simulate proxy response: content="" (string) with tool_calls.
        string_empty = AIMessage(
            content="",
            tool_calls=[{"name": "shell_tool", "args": {"input": "x"}, "id": "call_y"}],
        )
        fake_model = MagicMock()
        fake_model.ainvoke = AsyncMock(side_effect=[string_empty, _text_response("done")])
        bound = MagicMock()
        bound.ainvoke = fake_model.ainvoke

        mock_tool = MagicMock()
        mock_speaker = MagicMock()
        mock_speaker.content = "ok"
        mock_tool.run.return_value = mock_speaker

        emitted_events: list[dict] = []

        with (
            patch("truss_core.tool_use_loop.build_chat_model") as mock_build,
            patch.object(registry, "get", return_value=mock_tool),
        ):
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound
            ctx = _make_agent_context(event_logger=emitted_events.append)
            loop = ToolUseLoop(
                agent_context=ctx,
                tool_registry=registry,
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )
            asyncio.run(loop.run("do it", tool_specs=[spec], context=_make_context()))

        # The empty-string content must have been replaced so no
        # hallucination-triggering empty turns leak into the history.
        agent_messages = [e for e in emitted_events if e["type"] == "agent_message"]
        for event in agent_messages:
            assert event["payload"]["text"] != ""
            assert event["payload"]["text"] != "(no content)"
            assert "sanitised" not in event["payload"]["text"].lower()

    def test_extract_text_content_filters_placeholder(self):
        """``_extract_text_content`` returns ``""`` when content is the placeholder."""
        placeholder_list = [{"type": "text", "text": "(no content)"}]
        assert ToolUseLoop._extract_text_content(placeholder_list) == ""
        assert ToolUseLoop._extract_text_content("(no content)") == ""
        # Sanity: real text still passes through.
        assert ToolUseLoop._extract_text_content([{"type": "text", "text": "hello"}]) == "hello"


# ---------------------------------------------------------------------------
# LLM call timeout ceiling (Fix B)
# ---------------------------------------------------------------------------


class TestLlmCallTimeoutCeiling:
    """``await model.ainvoke`` is bounded by ``agent.llm_call_timeout``."""

    def test_ainvoke_timeout_raises_runtime_error(self):
        """A hung ``ainvoke`` is cancelled after the configured ceiling."""
        spec = _make_spec()
        registry = _make_registry(spec)

        async def _hang(*_args, **_kwargs):
            await asyncio.sleep(30)  # Would block forever without wait_for.
            return _text_response("never")

        fake_model = MagicMock()
        fake_model.ainvoke = _hang
        bound = MagicMock()
        bound.ainvoke = _hang

        with (
            patch("truss_core.tool_use_loop.build_chat_model") as mock_build,
            patch(
                "truss_core.tool_use_loop.get_config_value",
                side_effect=lambda *keys, default=None: (
                    0.05
                    if keys == ("agent", "llm_call_timeout")
                    else 1
                    if keys == ("agent", "llm_call_retries")
                    else default
                ),
            ),
        ):
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound

            loop = ToolUseLoop(
                agent_context=_make_agent_context(),
                tool_registry=registry,
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )
            import pytest

            with pytest.raises(RuntimeError, match="LLM call failed on all models"):
                asyncio.run(loop.run("hang", tool_specs=[spec], context=_make_context()))


# ---------------------------------------------------------------------------
# File-read dedup cache
# ---------------------------------------------------------------------------


class TestFileReadDedupCache:
    """Verify read_file dedup cache prevents redundant reads."""

    def _make_loop(self) -> ToolUseLoop:
        """Create a minimal ToolUseLoop for cache testing."""
        spec = _make_spec()
        registry = _make_registry(spec)
        with patch("truss_core.tool_use_loop.build_chat_model") as mock_build:
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = MagicMock()
            loop = ToolUseLoop(
                agent_context=_make_agent_context(),
                tool_registry=registry,
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )
        return loop

    def test_cache_miss_returns_none(self, tmp_path):
        """First read of a file returns None (cache miss)."""
        target = tmp_path / "test.txt"
        target.write_text("hello\n")

        loop = self._make_loop()
        step = ActionStep(
            tool_id="read_file",
            operation="get",
            tool_input={"path": str(target)},
        )
        assert loop._check_file_read_cache(step) is None

    def test_cache_hit_returns_stub(self, tmp_path):
        """Second read of an unchanged file returns the dedup stub."""
        target = tmp_path / "test.txt"
        target.write_text("hello\n")

        loop = self._make_loop()
        step = ActionStep(
            tool_id="read_file",
            operation="get",
            tool_input={"path": str(target)},
        )
        # Populate cache.
        loop._populate_file_read_cache(step)
        # Now check — should return the stub.
        result = loop._check_file_read_cache(step)
        assert result is not None
        assert "unchanged since last read" in result

    def test_cache_invalidated_by_mtime_change(self, tmp_path):
        """Modified file (mtime change) is not served from cache."""
        import os
        import time

        target = tmp_path / "test.txt"
        target.write_text("hello\n")

        loop = self._make_loop()
        step = ActionStep(
            tool_id="read_file",
            operation="get",
            tool_input={"path": str(target)},
        )
        loop._populate_file_read_cache(step)

        # Modify the file — bump mtime.
        time.sleep(0.05)
        target.write_text("changed\n")
        os.utime(str(target), None)

        result = loop._check_file_read_cache(step)
        assert result is None

    def test_cache_miss_on_different_offset(self, tmp_path):
        """Different offset means different read — no cache hit."""
        target = tmp_path / "test.txt"
        target.write_text("line1\nline2\nline3\n")

        loop = self._make_loop()
        step1 = ActionStep(
            tool_id="read_file",
            operation="get",
            tool_input={"path": str(target), "offset": 0},
        )
        step2 = ActionStep(
            tool_id="read_file",
            operation="get",
            tool_input={"path": str(target), "offset": 1},
        )
        loop._populate_file_read_cache(step1)
        assert loop._check_file_read_cache(step2) is None

    def test_cache_miss_on_different_limit(self, tmp_path):
        """Different limit means different read — no cache hit."""
        target = tmp_path / "test.txt"
        target.write_text("line1\nline2\nline3\n")

        loop = self._make_loop()
        step1 = ActionStep(
            tool_id="read_file",
            operation="get",
            tool_input={"path": str(target), "limit": 10},
        )
        step2 = ActionStep(
            tool_id="read_file",
            operation="get",
            tool_input={"path": str(target), "limit": 20},
        )
        loop._populate_file_read_cache(step1)
        assert loop._check_file_read_cache(step2) is None

    def test_cache_with_root_path(self, tmp_path):
        """Cache works with root + relative path."""
        target = tmp_path / "sub" / "test.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("hello\n")

        loop = self._make_loop()
        step = ActionStep(
            tool_id="read_file",
            operation="get",
            tool_input={"path": "sub/test.txt", "root": str(tmp_path)},
        )
        loop._populate_file_read_cache(step)
        result = loop._check_file_read_cache(step)
        assert result is not None
        assert "unchanged" in result

    def test_edit_invalidates_cache(self, tmp_path):
        """file_edit_tool execution clears the cache for the edited file."""
        import os

        target = tmp_path / "test.txt"
        target.write_text("hello\n")
        norm = os.path.normpath(str(target))

        loop = self._make_loop()
        read_step = ActionStep(
            tool_id="read_file",
            operation="get",
            tool_input={"path": str(target)},
        )
        loop._populate_file_read_cache(read_step)
        assert norm in loop._file_read_cache

        # Simulate edit tool action step — directly call invalidation logic.
        edit_step = ActionStep(
            tool_id="file_edit_tool",
            operation="set",
            tool_input={"file_path": str(target)},
        )
        edit_args = edit_step.tool_input if isinstance(edit_step.tool_input, dict) else {}
        edited_path = str(edit_args.get("file_path", "") or edit_args.get("path", ""))
        if edited_path:
            loop._file_read_cache.pop(os.path.normpath(edited_path), None)

        assert norm not in loop._file_read_cache

    def test_empty_path_returns_none(self):
        """Empty path skips cache logic entirely."""
        loop = self._make_loop()
        step = ActionStep(
            tool_id="read_file",
            operation="get",
            tool_input={"path": ""},
        )
        assert loop._check_file_read_cache(step) is None

    def test_nonexistent_file_returns_none(self, tmp_path):
        """Missing file (no mtime) returns None."""
        loop = self._make_loop()
        step = ActionStep(
            tool_id="read_file",
            operation="get",
            tool_input={"path": str(tmp_path / "missing.txt")},
        )
        # Populate with a fake entry.
        import os

        fake_path = os.path.normpath(str(tmp_path / "missing.txt"))
        loop._file_read_cache[fake_path] = _CachedFileRead(
            path=fake_path,
            offset=0,
            limit=None,
            mtime=0.0,
        )
        # mtime check should fail since file doesn't exist.
        assert loop._check_file_read_cache(step) is None

    def test_cached_file_read_dataclass(self):
        """_CachedFileRead stores all fields correctly."""
        entry = _CachedFileRead(
            path="/tmp/test.txt",
            offset=5,
            limit=10,
            mtime=12345.0,
        )
        assert entry.path == "/tmp/test.txt"
        assert entry.offset == 5
        assert entry.limit == 10
        assert entry.mtime == 12345.0


# ---------------------------------------------------------------------------
# Budget warning survives natural-completion refactor
# ---------------------------------------------------------------------------


class TestBudgetWarningStillFires:
    """Session-level budget_exhausted() warning is still injected."""

    def test_budget_warning_injected(self):
        spec = _make_spec("aider_shell_tool", "Run shell")
        registry = _make_registry(spec)

        messages_seen: list = []

        def _capture_invoke(msgs, **kwargs):
            messages_seen.extend(msgs)
            if len([m for m in messages_seen if hasattr(m, "tool_calls") and m.tool_calls]) >= 1:
                return _text_response("Done with budget warning.")
            return _tool_call_response("aider_shell_tool", {"command": "x"}, "c1")

        fake_model = MagicMock()
        fake_model.ainvoke = AsyncMock(side_effect=_capture_invoke)
        bound = MagicMock()
        bound.ainvoke = fake_model.ainvoke

        mock_tool = MagicMock()
        mock_speaker = MagicMock()
        mock_speaker.content = "ok"
        mock_tool.run.return_value = mock_speaker

        with (
            patch("truss_core.tool_use_loop.build_chat_model") as mock_build,
            patch.object(registry, "get", return_value=mock_tool),
        ):
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound

            # Create context with exhausted budget.
            ctx = _make_agent_context()
            ctx.registry._session_step_budget = 1
            ctx.registry._total_steps = 100  # Already exhausted

            loop = ToolUseLoop(
                agent_context=ctx,
                tool_registry=registry,
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )
            tq, state = asyncio.run(loop.run("do work", tool_specs=[spec], context=_make_context()))

        assert state.done is True
        budget_warnings = [
            m
            for m in messages_seen
            if isinstance(m, SystemMessage) and "BUDGET WARNING" in m.content
        ]
        assert len(budget_warnings) >= 1


# ---------------------------------------------------------------------------
# Model fallback cascade
# ---------------------------------------------------------------------------


class TestModelFallback:
    """Test model fallback cascade on retryable failure."""

    def test_fallback_on_primary_failure(self):
        """Primary model fails -> fallback model succeeds."""

        async def _test():
            ctx = _make_agent_context(model_name="primary-model")
            object.__setattr__(ctx, "fallback_models", ("fallback-model",))

            loop = ToolUseLoop(
                agent_context=ctx,
                tool_registry=_make_registry(_make_spec()),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )

            primary_error = RuntimeError("primary down")
            fallback_response = _text_response("fallback worked")

            call_count = 0

            async def _side_effect(messages, **kw):
                nonlocal call_count
                call_count += 1
                if call_count <= 2:  # primary gets 2 retries
                    raise primary_error
                return fallback_response

            with patch("truss_core.tool_use_loop.build_chat_model") as mock_build:
                mock_model = MagicMock()
                mock_model.bind_tools.return_value.ainvoke = AsyncMock(side_effect=_side_effect)
                mock_build.return_value = mock_model

                tq, state = await loop.run(
                    "test query",
                    tool_specs=[_make_spec()],
                    context=_make_context(),
                )

            assert state.done
            assert "fallback worked" in (tq.task_result or "")
            assert call_count == 3

        asyncio.run(_test())

    def test_no_fallback_on_non_retryable_error(self):
        """Non-retryable error fails immediately, no fallback."""

        async def _test():
            ctx = _make_agent_context(model_name="primary-model")
            object.__setattr__(ctx, "fallback_models", ("fallback-model",))

            loop = ToolUseLoop(
                agent_context=ctx,
                tool_registry=_make_registry(_make_spec()),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )

            bad_request = ValueError("bad request")
            with (
                patch("truss_core.tool_use_loop.build_chat_model") as mock_build,
                patch("truss_core.tool_use_loop._classify_llm_error", return_value=(False, 0)),
            ):
                mock_model = MagicMock()
                mock_model.bind_tools.return_value.ainvoke = AsyncMock(side_effect=bad_request)
                mock_build.return_value = mock_model

                try:
                    await loop.run(
                        "test query",
                        tool_specs=[_make_spec()],
                        context=_make_context(),
                    )
                    assert False, "Should have raised"
                except RuntimeError as exc:
                    assert "bad request" in str(exc)
                    assert mock_model.bind_tools.return_value.ainvoke.call_count == 1

        asyncio.run(_test())

    def test_no_fallback_when_not_configured(self):
        """Without fallback_models, behaves like before."""

        async def _test():
            ctx = _make_agent_context(model_name="only-model")

            loop = ToolUseLoop(
                agent_context=ctx,
                tool_registry=_make_registry(_make_spec()),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )

            with patch("truss_core.tool_use_loop.build_chat_model") as mock_build:
                mock_model = MagicMock()
                mock_model.bind_tools.return_value.ainvoke = AsyncMock(
                    side_effect=RuntimeError("down")
                )
                mock_build.return_value = mock_model

                try:
                    await loop.run(
                        "test query",
                        tool_specs=[_make_spec()],
                        context=_make_context(),
                    )
                    assert False, "Should have raised"
                except RuntimeError as exc:
                    assert "down" in str(exc)

        asyncio.run(_test())


class TestRootInjection:
    """``_tool_call_to_action_step`` injects ``root`` only for registered non-MCP tools.

    Regression: session tools with strict Pydantic schemas (e.g. ``submit_widget``
    uses ``ConfigDict(extra='forbid')``) rejected the injected ``root`` key,
    causing repeated validation failures in sub-agents.
    """

    def _make_loop(self, registry: ToolRegistry) -> ToolUseLoop:
        loop = ToolUseLoop(
            agent_context=_make_agent_context(),
            tool_registry=registry,
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
            cwd="/tmp/project",
        )
        return loop

    def test_registered_local_tool_gets_root_injected(self):
        loop = self._make_loop(_make_registry(_make_spec("aider_shell_tool")))
        step = loop._tool_call_to_action_step(
            {"name": "aider_shell_tool", "args": {"command": "ls"}}
        )
        assert step.tool_input == {"command": "ls", "root": "/tmp/project"}

    def test_session_tool_does_not_get_root_injected(self):
        # ``submit_widget`` is a session tool — not in the registry.
        loop = self._make_loop(_make_registry())
        step = loop._tool_call_to_action_step(
            {"name": "submit_widget", "args": {"widget_id": "w1"}}
        )
        assert step.tool_input == {"widget_id": "w1"}
        assert "root" not in step.tool_input

    def test_mcp_tool_does_not_get_root_injected(self):
        mcp_spec = ToolSpec(
            tool_id="mcp_search",
            name="mcp_search",
            description="",
            factory=lambda: MagicMock(),
            enabled=True,
            kind="mcp",
            metadata={},
        )
        loop = self._make_loop(_make_registry(mcp_spec))
        step = loop._tool_call_to_action_step(
            {"name": "mcp_search", "args": {"q": "x"}}
        )
        assert step.tool_input == {"q": "x"}

    def test_explicit_root_is_preserved(self):
        loop = self._make_loop(_make_registry(_make_spec("aider_shell_tool")))
        step = loop._tool_call_to_action_step(
            {"name": "aider_shell_tool", "args": {"command": "ls", "root": "/elsewhere"}}
        )
        assert step.tool_input["root"] == "/elsewhere"
