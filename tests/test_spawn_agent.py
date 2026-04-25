#!/usr/bin/env python3
"""Tests for SpawnAgentTool — sub-agent spawning, tool scoping, model validation."""

from __future__ import annotations

import asyncio
import queue
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage
from meeseeks_core.agent_context import AgentContext
from meeseeks_core.classes import ActionStep
from meeseeks_core.hooks import HookManager
from meeseeks_core.hypervisor import AgentHandle, AgentHypervisor
from meeseeks_core.permissions import PermissionDecision, PermissionPolicy
from meeseeks_core.spawn_agent import SpawnAgentTool
from meeseeks_core.tool_registry import ToolRegistry, ToolSpec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spec(tool_id: str = "test_tool") -> ToolSpec:
    return ToolSpec(
        tool_id=tool_id,
        name=tool_id,
        description=f"Test tool {tool_id}",
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


def _make_registry(*tool_ids: str) -> ToolRegistry:
    registry = ToolRegistry()
    for tid in tool_ids:
        registry.register(_make_spec(tid))
    return registry


def _make_context(
    *,
    max_depth: int = 5,
    depth: int = 0,
) -> AgentContext:
    root = AgentContext.root(
        model_name="test-model",
        max_depth=max_depth,
        registry=AgentHypervisor(max_concurrent=100),
    )
    ctx = root
    for _ in range(depth):
        ctx = ctx.child()
    return ctx


def _make_hook_manager() -> HookManager:
    hm = MagicMock(spec=HookManager)
    hm.run_pre_tool_use.side_effect = lambda step: step
    hm.run_post_tool_use.side_effect = lambda step, result: result
    hm.run_permission_request.side_effect = lambda step, decision: decision
    hm.run_on_agent_start.return_value = None
    hm.run_on_agent_stop.return_value = None
    return hm


def _allow_all_policy() -> PermissionPolicy:
    policy = MagicMock(spec=PermissionPolicy)
    policy.decide.return_value = PermissionDecision.ALLOW
    return policy


def _text_response(content: str) -> AIMessage:
    return AIMessage(content=content)


# ---------------------------------------------------------------------------
# SpawnAgentTool
# ---------------------------------------------------------------------------


class TestSpawnAgentBasic:
    """Test basic sub-agent spawn and return."""

    def test_spawn_returns_child_result(self):
        async def _test():
            registry = _make_registry("shell_tool")
            ctx = _make_context()
            tool = SpawnAgentTool(
                agent_context=ctx,
                tool_registry=registry,
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )

            # Mock the child's model to return a text response immediately.
            fake_model = MagicMock()
            fake_model.ainvoke = AsyncMock(return_value=_text_response("Child says hello"))
            bound = MagicMock()
            bound.ainvoke = fake_model.ainvoke

            with patch("meeseeks_core.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound

                step = ActionStep(
                    tool_id="spawn_agent",
                    operation="set",
                    tool_input={"task": "say hello"},
                )
                result = await tool.run_async(step)

            assert "hello" in result.content.lower()

        asyncio.run(_test())


class TestSpawnAgentToolScoping:
    """Test tool filtering: allowed_tools, denied_tools, config denied."""

    def test_allowed_tools_restricts(self):
        registry = _make_registry("tool_a", "tool_b", "tool_c")
        ctx = _make_context()
        tool = SpawnAgentTool(
            agent_context=ctx,
            tool_registry=registry,
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
        )

        specs = tool._filter_tool_specs({"allowed_tools": ["tool_a", "tool_c"]})
        ids = {s.tool_id for s in specs}
        assert ids == {"tool_a", "tool_c"}

    def test_denied_tools_removes(self):
        registry = _make_registry("tool_a", "tool_b", "tool_c")
        ctx = _make_context()
        tool = SpawnAgentTool(
            agent_context=ctx,
            tool_registry=registry,
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
        )

        specs = tool._filter_tool_specs({"denied_tools": ["tool_b"]})
        ids = {s.tool_id for s in specs}
        assert "tool_b" not in ids
        assert "tool_a" in ids

    def test_deny_takes_precedence_over_allow(self):
        registry = _make_registry("tool_a", "tool_b")
        ctx = _make_context()
        tool = SpawnAgentTool(
            agent_context=ctx,
            tool_registry=registry,
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
        )

        specs = tool._filter_tool_specs(
            {
                "allowed_tools": ["tool_a", "tool_b"],
                "denied_tools": ["tool_b"],
            }
        )
        ids = {s.tool_id for s in specs}
        assert ids == {"tool_a"}

    def test_config_denied_always_applied(self):
        registry = _make_registry("tool_a", "blocked_tool")
        ctx = _make_context()
        tool = SpawnAgentTool(
            agent_context=ctx,
            tool_registry=registry,
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
        )

        with patch(
            "meeseeks_core.tool_registry.get_config_value",
            side_effect=lambda *a, **kw: (
                ["blocked_tool"] if a == ("agent", "default_denied_tools") else kw.get("default")
            ),
        ):
            specs = tool._filter_tool_specs({})
            ids = {s.tool_id for s in specs}
            assert "blocked_tool" not in ids


class TestSpawnAgentModelValidation:
    """Test model resolution and validation."""

    def test_explicit_model_used(self):
        ctx = _make_context()
        tool = SpawnAgentTool(
            agent_context=ctx,
            tool_registry=_make_registry(),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
        )
        with patch(
            "meeseeks_core.spawn_agent.get_config_value",
            return_value=[],
        ):
            result = tool._resolve_model("custom-model")
        assert result == "custom-model"

    def test_invalid_model_returns_error(self):
        ctx = _make_context()
        tool = SpawnAgentTool(
            agent_context=ctx,
            tool_registry=_make_registry(),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
        )
        with patch(
            "meeseeks_core.spawn_agent.get_config_value",
            side_effect=lambda *a, **kw: (
                ["allowed-model"] if a == ("agent", "allowed_models") else kw.get("default", "")
            ),
        ):
            result = tool._resolve_model("forbidden-model")
        assert result.startswith("ERROR:")

    def test_default_sub_model_used_when_no_override(self):
        ctx = _make_context()
        tool = SpawnAgentTool(
            agent_context=ctx,
            tool_registry=_make_registry(),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
        )
        with patch(
            "meeseeks_core.spawn_agent.get_config_value",
            side_effect=lambda *a, **kw: (
                []
                if a == ("agent", "allowed_models")
                else "default-sub"
                if a == ("agent", "default_sub_model")
                else kw.get("default", "")
            ),
        ):
            result = tool._resolve_model(None)
        assert result == "default-sub"

    def test_inherits_parent_model_as_fallback(self):
        ctx = _make_context()
        tool = SpawnAgentTool(
            agent_context=ctx,
            tool_registry=_make_registry(),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
        )
        with patch(
            "meeseeks_core.spawn_agent.get_config_value",
            side_effect=lambda *a, **kw: (
                []
                if a == ("agent", "allowed_models")
                else ""
                if a == ("agent", "default_sub_model")
                else kw.get("default", "")
            ),
        ):
            result = tool._resolve_model(None)
        assert result == "test-model"


class TestSpawnAgentDepthGate:
    """Test that agents at max_depth cannot spawn."""

    def test_leaf_agent_has_no_spawn_tool(self):
        """ToolUseLoop at max_depth should not create a SpawnAgentTool."""
        from meeseeks_core.tool_use_loop import ToolUseLoop

        ctx = _make_context(max_depth=1, depth=1)
        assert ctx.can_spawn is False

        loop = ToolUseLoop(
            agent_context=ctx,
            tool_registry=_make_registry("shell"),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
        )
        assert loop._spawn_agent_tool is None


# ---------------------------------------------------------------------------
# Research-grounded tests: approval_callback, AgentResult, lifecycle
# ---------------------------------------------------------------------------


class TestSpawnAgentApprovalCallback:
    """Ref: [DeepMind-Delegation §4.7] Sub-agents inherit parent's approval policy."""

    def test_approval_callback_stored(self):
        ctx = _make_context()
        callback = lambda _step: True  # noqa: E731
        tool = SpawnAgentTool(
            agent_context=ctx,
            tool_registry=_make_registry("shell"),
            permission_policy=_allow_all_policy(),
            approval_callback=callback,
            hook_manager=_make_hook_manager(),
        )
        assert tool._approval_callback is callback

    def test_approval_callback_defaults_to_none(self):
        ctx = _make_context()
        tool = SpawnAgentTool(
            agent_context=ctx,
            tool_registry=_make_registry("shell"),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
        )
        assert tool._approval_callback is None


class TestSpawnAgentResult:
    """Ref: [CoA §3.1] Sub-agents return structured AgentResult (Communication Unit)."""

    def test_result_is_json_with_status(self):
        """Non-root spawn returns blocking JSON AgentResult."""
        import json

        async def _test():
            registry = _make_registry("shell_tool")
            # Use depth=1 (non-root) to test blocking spawn path.
            ctx = _make_context(depth=1)
            tool = SpawnAgentTool(
                agent_context=ctx,
                tool_registry=registry,
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )

            fake_model = MagicMock()
            fake_model.ainvoke = AsyncMock(return_value=_text_response("Done!"))
            bound = MagicMock()
            bound.ainvoke = fake_model.ainvoke

            with patch("meeseeks_core.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound

                step = ActionStep(
                    tool_id="spawn_agent",
                    operation="set",
                    tool_input={"task": "do work"},
                )
                result = await tool.run_async(step)

            # Result should be valid JSON AgentResult (blocking path)
            parsed = json.loads(result.content)
            assert "status" in parsed
            assert "content" in parsed
            assert "steps_used" in parsed
            assert "summary" in parsed
            assert parsed["status"] in ("completed", "failed")

        asyncio.run(_test())

    def test_root_spawn_returns_immediately(self):
        """Root spawn returns non-blocking submission confirmation."""
        import json

        async def _test():
            registry = _make_registry("shell_tool")
            # Root (depth=0) gets non-blocking spawn.
            ctx = _make_context(depth=0)
            tool = SpawnAgentTool(
                agent_context=ctx,
                tool_registry=registry,
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )

            fake_model = MagicMock()
            fake_model.ainvoke = AsyncMock(return_value=_text_response("Done!"))
            bound = MagicMock()
            bound.ainvoke = fake_model.ainvoke

            with patch("meeseeks_core.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound

                step = ActionStep(
                    tool_id="spawn_agent",
                    operation="set",
                    tool_input={"task": "do work"},
                )
                result = await tool.run_async(step)

            # Non-blocking: returns submission confirmation, not full result.
            parsed = json.loads(result.content)
            assert parsed["status"] == "submitted"
            assert "agent_id" in parsed
            assert "task" in parsed

            # Clean up lifecycle tasks.
            await tool.await_lifecycle_managers(timeout=5.0)

        asyncio.run(_test())


class TestSpawnAgentSchema:
    """Ref: [DeepMind-Delegation §4.1] Contract-first decomposition with acceptance criteria."""

    def test_schema_includes_max_steps_deprecated(self):
        """max_steps field is retained in schema for backward compatibility."""
        from meeseeks_core.spawn_agent import SPAWN_AGENT_SCHEMA

        props = SPAWN_AGENT_SCHEMA["function"]["parameters"]["properties"]
        assert "max_steps" in props
        assert props["max_steps"]["type"] == "integer"
        assert "deprecated" in props["max_steps"]["description"].lower()

    def test_schema_includes_acceptance_criteria(self):
        from meeseeks_core.spawn_agent import SPAWN_AGENT_SCHEMA

        props = SPAWN_AGENT_SCHEMA["function"]["parameters"]["properties"]
        assert "acceptance_criteria" in props
        assert props["acceptance_criteria"]["type"] == "string"


# ---------------------------------------------------------------------------
# Non-blocking lifecycle: result visibility and parent notification
# ---------------------------------------------------------------------------


async def _spawn_root_agent_and_wait(
    task: str = "analyse data for anomalies",
) -> tuple[SpawnAgentTool, AgentContext]:
    """Spawn one non-blocking child from root and wait for lifecycle to finish.

    Registers the root handle in the hypervisor so send_to_parent can locate
    the parent's message_queue — mirroring what ToolUseLoop.run() does in prod.
    """
    root_queue: queue.Queue[str] = queue.Queue()
    hypervisor = AgentHypervisor(max_concurrent=10)
    ctx = AgentContext.root(
        model_name="test-model",
        max_depth=5,
        registry=hypervisor,
        message_queue=root_queue,
    )
    # Register root handle so send_to_parent can find the parent's message_queue.
    root_handle = AgentHandle(
        agent_id=ctx.agent_id,
        parent_id=None,
        depth=0,
        model_name=ctx.model_name,
        task_description="root task",
        status="running",
        message_queue=root_queue,
    )
    await hypervisor.register(root_handle)

    tool = SpawnAgentTool(
        agent_context=ctx,
        tool_registry=_make_registry("shell_tool"),
        permission_policy=_allow_all_policy(),
        hook_manager=_make_hook_manager(),
    )

    fake_model = MagicMock()
    fake_model.ainvoke = AsyncMock(
        return_value=_text_response("Analysis complete: found 3 anomalies")
    )
    bound = MagicMock()
    bound.ainvoke = fake_model.ainvoke

    with patch("meeseeks_core.tool_use_loop.build_chat_model") as mock_build:
        mock_build.return_value = MagicMock()
        mock_build.return_value.bind_tools.return_value = bound

        step = ActionStep(
            tool_id="spawn_agent",
            operation="set",
            tool_input={"task": task},
        )
        await tool.run_async(step)
        # Keep patch active until lifecycle completes so build_chat_model stays mocked.
        await tool.await_lifecycle_managers(timeout=5.0)

    return tool, ctx


class TestNonBlockingLifecycle:
    """Non-blocking root spawns: handles persist after completion for check_agents visibility.

    Regression suite for the three bugs identified via trace 8a63a463:
    - Bug 1: premature unregister cleared completed handles before parent could read them
    - Bug 2: send_to_parent fired after unregister so always failed silently
    - Bug 3: notification contained only status string, not task description or result
    """

    def test_completed_handle_stays_in_registry_after_lifecycle(self):
        """AgentHandle must remain in hypervisor after non-blocking lifecycle completes."""

        async def _test():
            _, ctx = await _spawn_root_agent_and_wait()
            children = await ctx.registry.list_children(ctx.agent_id)
            assert len(children) == 1, (
                f"Expected 1 completed child in registry, got {len(children)}. "
                "Premature unregister is the likely cause."
            )
            child = children[0]
            assert child.status == "completed"
            assert child.result is not None

        asyncio.run(_test())

    def test_check_agents_returns_completed_result_not_empty(self):
        """check_agents must surface completed agents and results — not 'No agents spawned'."""
        import json

        async def _test():
            tool, ctx = await _spawn_root_agent_and_wait(task="find anomalies")

            step = ActionStep(
                tool_id="check_agents",
                operation="set",
                tool_input={"wait": False},
            )
            result = await tool.handle_check_agents(step)
            payload = json.loads(result.content)

            assert payload["agents"], (
                "check_agents returned empty agents list after all children completed. "
                "Handles were removed from registry before parent could collect results."
            )
            completed = [a for a in payload["agents"] if a["status"] == "completed"]
            assert len(completed) == 1, f"Expected 1 completed agent, got: {payload['agents']}"
            assert completed[0]["result"] is not None
            assert "No agents spawned" not in payload["text"]

        asyncio.run(_test())

    def test_parent_receives_notification_with_task_and_result(self):
        """Parent message_queue must receive notification
        containing full task description and result."""

        async def _test():
            task_desc = "analyse security logs for intrusion patterns"
            _, ctx = await _spawn_root_agent_and_wait(task=task_desc)

            messages: list[str] = []
            try:
                while True:
                    messages.append(ctx.message_queue.get_nowait())
            except queue.Empty:
                pass

            assert messages, (
                "Parent message_queue received no completion notification. "
                "send_to_parent likely fired after unregister and failed silently."
            )
            notification = messages[-1]
            assert task_desc in notification, (
                f"Notification does not contain full task description.\n"
                f"Expected to find: {task_desc!r}\n"
                f"Got: {notification!r}"
            )

        asyncio.run(_test())


class TestSubstituteAgentBody:
    """Unit tests for the plugin-generic agent body substitution pass.

    Lives alongside the SpawnAgentTool tests because ``substitute_agent_body``
    is the only novel bit of the widget-builder-as-plugin refactor — every
    other change was a mechanical port.
    """

    def test_direct_substitution_from_subs(self):
        from meeseeks_core.spawn_agent import substitute_agent_body

        body = "root=${CLAUDE_PLUGIN_ROOT}\nsession=${SESSION_ID}"
        out = substitute_agent_body(
            body,
            {"CLAUDE_PLUGIN_ROOT": "/plugins/x", "SESSION_ID": "s1"},
            env={},
        )
        assert out == "root=/plugins/x\nsession=s1"

    def test_bash_default_when_env_unset(self):
        from meeseeks_core.spawn_agent import substitute_agent_body

        body = "root=${MEESEEKS_WIDGET_ROOT:-/tmp/meeseeks/widgets}"
        out = substitute_agent_body(body, {}, env={})
        assert out == "root=/tmp/meeseeks/widgets"

    def test_bash_default_honours_env_when_set(self):
        from meeseeks_core.spawn_agent import substitute_agent_body

        body = "root=${MEESEEKS_WIDGET_ROOT:-/tmp/meeseeks/widgets}"
        out = substitute_agent_body(
            body, {}, env={"MEESEEKS_WIDGET_ROOT": "/custom/path"}
        )
        assert out == "root=/custom/path"

    def test_plain_dollar_var_expands_from_env(self):
        from meeseeks_core.spawn_agent import substitute_agent_body

        body = "home is $HOME"
        out = substitute_agent_body(body, {}, env={"HOME": "/root"})
        assert out == "home is /root"

    def test_unknown_plain_dollar_var_stays_literal(self):
        from meeseeks_core.spawn_agent import substitute_agent_body

        body = "unset $NOT_A_REAL_VARIABLE"
        out = substitute_agent_body(body, {}, env={})
        assert out == "unset $NOT_A_REAL_VARIABLE"

    def test_all_three_passes_compose(self):
        from meeseeks_core.spawn_agent import substitute_agent_body

        body = (
            "plugin=${CLAUDE_PLUGIN_ROOT} "
            "root=${MEESEEKS_WIDGET_ROOT:-/tmp/meeseeks/widgets} "
            "shell=$SHELL"
        )
        out = substitute_agent_body(
            body,
            {"CLAUDE_PLUGIN_ROOT": "/plugins/widget-builder"},
            env={"SHELL": "/bin/zsh"},
        )
        assert out == (
            "plugin=/plugins/widget-builder "
            "root=/tmp/meeseeks/widgets "
            "shell=/bin/zsh"
        )
