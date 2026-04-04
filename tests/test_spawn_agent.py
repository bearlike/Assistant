#!/usr/bin/env python3
"""Tests for SpawnAgentTool — sub-agent spawning, tool scoping, model validation."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage
from meeseeks_core.agent_context import AgentContext
from meeseeks_core.classes import ActionStep
from meeseeks_core.hooks import HookManager
from meeseeks_core.hypervisor import AgentHypervisor
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
    *, max_depth: int = 5, depth: int = 0,
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
            fake_model.ainvoke = AsyncMock(
                return_value=_text_response("Child says hello")
            )
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

        specs = tool._filter_tool_specs({
            "allowed_tools": ["tool_a", "tool_b"],
            "denied_tools": ["tool_b"],
        })
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
                [] if a == ("agent", "allowed_models")
                else "default-sub" if a == ("agent", "default_sub_model")
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
                [] if a == ("agent", "allowed_models")
                else "" if a == ("agent", "default_sub_model")
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
            fake_model.ainvoke = AsyncMock(
                return_value=_text_response("Done!")
            )
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
            fake_model.ainvoke = AsyncMock(
                return_value=_text_response("Done!")
            )
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

    def test_schema_includes_max_steps(self):
        from meeseeks_core.spawn_agent import SPAWN_AGENT_SCHEMA
        props = SPAWN_AGENT_SCHEMA["function"]["parameters"]["properties"]
        assert "max_steps" in props
        assert props["max_steps"]["type"] == "integer"

    def test_schema_includes_acceptance_criteria(self):
        from meeseeks_core.spawn_agent import SPAWN_AGENT_SCHEMA
        props = SPAWN_AGENT_SCHEMA["function"]["parameters"]["properties"]
        assert "acceptance_criteria" in props
        assert props["acceptance_criteria"]["type"] == "string"
