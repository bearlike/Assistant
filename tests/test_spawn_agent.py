#!/usr/bin/env python3
"""Tests for SpawnAgentTool — sub-agent spawning, tool scoping, model validation."""

from __future__ import annotations

import asyncio
import queue
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage
from mewbo_core.agent_context import AgentContext
from mewbo_core.classes import ActionStep
from mewbo_core.hooks import HookManager
from mewbo_core.hypervisor import AgentHandle, AgentHypervisor, DelegationContract
from mewbo_core.permissions import PermissionDecision, PermissionPolicy
from mewbo_core.spawn_agent import SpawnAgentTool
from mewbo_core.tool_registry import ToolRegistry, ToolSpec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spec(
    tool_id: str = "test_tool",
    *,
    read_only: bool = False,
    capability: Literal["read", "write", "execute"] | None = None,
    always_load: bool = False,
) -> ToolSpec:
    metadata: dict = {
        "schema": {
            "type": "object",
            "properties": {"input": {"type": "string"}},
            "required": ["input"],
        }
    }
    if always_load:
        metadata["always_load"] = True
    return ToolSpec(
        tool_id=tool_id,
        name=tool_id,
        description=f"Test tool {tool_id}",
        factory=lambda: MagicMock(),
        enabled=True,
        kind="local",
        read_only=read_only,
        capability=capability,
        metadata=metadata,
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


def _tool_call_response(tool_id: str, args: dict, call_id: str = "call_1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": tool_id, "args": args, "id": call_id}])


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

            with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
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

    def test_stop_event_carries_child_result_summary(self):
        """The ``sub_agent`` stop event echoes the child's compressed result.

        Downstream consumers (the agentic-search trace) project this ``summary``
        as the probe's evidence block — the lifecycle ``detail`` is only the
        ``done_reason``. Additive: a ``start`` carries no summary, so existing
        consumers that read only the legacy keys are unaffected.
        """
        async def _test():
            events: list = []
            # depth>0 → blocking path settles inline; event_logger rides the root
            # (the frozen ctx is built with it) and child() inherits it.
            root = AgentContext.root(
                model_name="test-model",
                max_depth=5,
                registry=AgentHypervisor(max_concurrent=100),
                event_logger=events.append,
            )
            ctx = root.child()
            tool = SpawnAgentTool(
                agent_context=ctx,
                tool_registry=_make_registry("shell_tool"),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )

            bound = MagicMock()
            bound.ainvoke = AsyncMock(return_value=_text_response("Child says hello"))
            with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound
                await tool.run_async(ActionStep(
                    tool_id="spawn_agent",
                    operation="set",
                    tool_input={"task": "say hello"},
                ))

            sub = [e for e in events if e["type"] == "sub_agent"]
            starts = [e for e in sub if e["payload"]["action"] == "start"]
            stops = [e for e in sub if e["payload"]["action"] == "stop"]
            assert starts and "summary" not in starts[0]["payload"]
            assert stops and "Child says hello" in stops[0]["payload"]["summary"]
            assert stops[0]["payload"]["detail"]  # the done_reason, unchanged

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

    def test_empty_allowed_tools_grants_the_child_nothing(self):
        # The delegation twin of the credential three-state bug, and the args
        # here are MODEL-SUPPLIED. A parent spawning a child with
        # ``allowed_tools: []`` means zero tools; the double collapse
        # (``args.get(...) or None``) handed the child the parent's ENTIRE spec
        # set instead — privilege amplification on the attenuation path.
        registry = _make_registry("tool_a", "tool_b", "tool_c")
        tool = SpawnAgentTool(
            agent_context=_make_context(),
            tool_registry=registry,
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
        )

        specs = tool._filter_tool_specs({"allowed_tools": []})

        assert {s.tool_id for s in specs} == set()

    def test_absent_allowed_tools_leaves_the_child_unrestricted(self):
        registry = _make_registry("tool_a", "tool_b")
        tool = SpawnAgentTool(
            agent_context=_make_context(),
            tool_registry=registry,
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
        )

        specs = tool._filter_tool_specs({})

        assert {s.tool_id for s in specs} == {"tool_a", "tool_b"}

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
            "mewbo_core.tool_registry.get_config_value",
            side_effect=lambda *a, **kw: (
                ["blocked_tool"] if a == ("agent", "default_denied_tools") else kw.get("default")
            ),
        ):
            specs = tool._filter_tool_specs({})
            ids = {s.tool_id for s in specs}
            assert "blocked_tool" not in ids


class TestSpawnAgentParentContainment:
    """A child derives from the PARENT's effective set — it can only narrow."""

    @staticmethod
    def _tool(registry: ToolRegistry) -> SpawnAgentTool:
        return SpawnAgentTool(
            agent_context=_make_context(),
            tool_registry=registry,
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
        )

    def test_child_cannot_widen_beyond_parent_set(self):
        """The containment property: a tool the parent never held stays absent
        even when the child's allowlist explicitly names it."""
        registry = _make_registry("tool_a", "tool_b", "tool_c")
        tool = self._tool(registry)
        # Parent ran narrowed — it never held tool_b or tool_c.
        tool.parent_tool_specs = [s for s in registry.list_specs() if s.tool_id == "tool_a"]

        specs = tool._filter_tool_specs({"allowed_tools": ["tool_a", "tool_b", "tool_c"]})

        assert {s.tool_id for s in specs} == {"tool_a"}

    def test_unnarrowed_child_inherits_parent_set_not_registry(self):
        """A plain spawn (no allowlist) gets the parent's set, not the registry —
        this is the common case the leak actually bit."""
        registry = _make_registry("tool_a", "tool_b", "tool_c")
        tool = self._tool(registry)
        tool.parent_tool_specs = [s for s in registry.list_specs() if s.tool_id != "tool_c"]

        specs = tool._filter_tool_specs({})

        assert {s.tool_id for s in specs} == {"tool_a", "tool_b"}

    def test_child_still_narrows_within_the_parent_set(self):
        """Containment only removes the widening — allow/deny still apply."""
        registry = _make_registry("tool_a", "tool_b", "tool_c")
        tool = self._tool(registry)
        tool.parent_tool_specs = [s for s in registry.list_specs() if s.tool_id != "tool_c"]

        specs = tool._filter_tool_specs({"denied_tools": ["tool_b"]})

        assert {s.tool_id for s in specs} == {"tool_a"}

    def test_unstamped_falls_back_to_registry(self):
        """Unstamped (root/unscoped) keeps the historical registry-wide set."""
        registry = _make_registry("tool_a", "tool_b", "tool_c")
        tool = self._tool(registry)

        assert tool.parent_tool_specs is None
        specs = tool._filter_tool_specs({})
        assert {s.tool_id for s in specs} == {"tool_a", "tool_b", "tool_c"}

    def test_containment_composes_with_capability_mode(self):
        """Both narrowing axes apply together — neither overrides the other.
        A write-tier spec the parent HELD is still dropped by a read_only child,
        and a tool the parent LACKED stays absent regardless of tier."""
        registry = ToolRegistry()
        registry.register(_make_spec("read_file", read_only=True))
        registry.register(_make_spec("edit", capability="write"))
        # 'notes' is READ-tier, so the tier gate would happily keep it —
        # only containment can drop it, which keeps that axis load-bearing here.
        registry.register(_make_spec("notes", read_only=True))
        tool = self._tool(registry)
        # The parent held the read and write tools, but never 'notes'.
        tool.parent_tool_specs = [
            s for s in registry.list_specs() if s.tool_id in {"read_file", "edit"}
        ]

        specs = tool._filter_tool_specs(
            {"allowed_tools": ["read_file", "edit", "notes"]},
            capability_mode="read_only",
        )

        # 'edit' dropped by the tier gate, 'notes' by containment.
        assert {s.tool_id for s in specs} == {"read_file"}

    def test_spawn_drives_child_specs_from_parent_set(self):
        """End-to-end: the specs actually handed to the child loop are contained
        by the parent's set, not re-derived from the registry."""
        async def _test():
            registry = _make_registry("tool_a", "tool_b")
            tool = self._tool(registry)
            tool.parent_tool_specs = [
                s for s in registry.list_specs() if s.tool_id == "tool_a"
            ]

            captured: dict = {}
            original = tool._filter_tool_specs

            def _spy(args, *, capability_mode="all"):
                specs = original(args, capability_mode=capability_mode)
                captured["ids"] = {s.tool_id for s in specs}
                return specs

            tool._filter_tool_specs = _spy  # type: ignore[method-assign]

            bound = MagicMock()
            bound.ainvoke = AsyncMock(return_value=_text_response("done"))
            with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound
                await tool.run_async(
                    ActionStep(
                        tool_id="spawn_agent",
                        operation="set",
                        # The child asks for a tool the parent never held.
                        tool_input={"task": "go", "allowed_tools": ["tool_a", "tool_b"]},
                    )
                )

            assert captured["ids"] == {"tool_a"}

        asyncio.run(_test())


class TestSpawnAgentCapabilityMode:
    """capability_mode coarse privilege tier at the spawn seam."""

    @staticmethod
    def _mixed_registry() -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(_make_spec("read_file", read_only=True))
        registry.register(_make_spec("edit", capability="write"))
        registry.register(_make_spec("shell", capability="execute"))
        registry.register(_make_spec("undeclared"))
        registry.register(_make_spec("tool_search", read_only=True, always_load=True))
        return registry

    def _tool(self, ctx=None) -> SpawnAgentTool:
        return SpawnAgentTool(
            agent_context=ctx or _make_context(),
            tool_registry=self._mixed_registry(),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
        )

    def test_read_only_binds_read_and_search_not_shell_or_edit(self):
        specs = self._tool()._filter_tool_specs({}, capability_mode="read_only")
        ids = {s.tool_id for s in specs}
        assert ids == {"read_file", "tool_search"}
        assert "shell" not in ids and "edit" not in ids

    def test_read_only_excludes_undeclared_safe_deny(self):
        specs = self._tool()._filter_tool_specs({}, capability_mode="read_only")
        assert "undeclared" not in {s.tool_id for s in specs}

    def test_execute_keeps_declared_drops_undeclared(self):
        specs = self._tool()._filter_tool_specs({}, capability_mode="execute")
        ids = {s.tool_id for s in specs}
        assert ids == {"read_file", "edit", "shell", "tool_search"}

    def test_capability_mode_layered_under_allow_cannot_resurrect(self):
        # allowed_tools names shell, but read_only mode removes it anyway.
        specs = self._tool()._filter_tool_specs(
            {"allowed_tools": ["shell"]}, capability_mode="read_only"
        )
        assert {s.tool_id for s in specs} == {"tool_search"}  # only always_load survives

    def test_default_mode_is_byte_identical(self):
        tool = self._tool()
        default = [s.tool_id for s in tool._filter_tool_specs({})]
        explicit_all = [s.tool_id for s in tool._filter_tool_specs({}, capability_mode="all")]
        assert default == explicit_all
        # Unfiltered set (no capability gate) — every registered tool.
        assert set(default) == {"read_file", "edit", "shell", "undeclared", "tool_search"}

    def test_spawn_narrows_requested_against_parent_ceiling(self):
        """End-to-end: a spawn under a read_only-effective parent that REQUESTS
        'all' filters with the narrowed effective mode, never the request."""
        async def _test():
            root = AgentContext.root(
                model_name="test-model",
                max_depth=5,
                registry=AgentHypervisor(max_concurrent=100),
            )
            # Parent is a child of a read_only spawn → its own ceiling is read_only.
            parent = root.child(capability_mode="read_only")
            tool = self._tool(ctx=parent)

            captured: dict = {}
            original = tool._filter_tool_specs

            def _spy(args, *, capability_mode="all"):
                captured["mode"] = capability_mode
                return original(args, capability_mode=capability_mode)

            tool._filter_tool_specs = _spy  # type: ignore[method-assign]

            bound = MagicMock()
            bound.ainvoke = AsyncMock(return_value=_text_response("done"))
            with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound
                await tool.run_async(
                    ActionStep(
                        tool_id="spawn_agent",
                        operation="set",
                        # Child REQUESTS the widest mode...
                        tool_input={"task": "go", "capability_mode": "all"},
                    )
                )

            # ...but the effective mode is narrowed to the parent's ceiling.
            assert captured["mode"] == "read_only"
            # And that effective mode really drops write/execute tools.
            kept = {s.tool_id for s in original({}, capability_mode=captured["mode"])}
            assert "edit" not in kept and "shell" not in kept

        asyncio.run(_test())


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
            "mewbo_core.spawn_agent.get_config_value",
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
            "mewbo_core.spawn_agent.get_config_value",
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
            "mewbo_core.spawn_agent.get_config_value",
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
            "mewbo_core.spawn_agent.get_config_value",
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
        from mewbo_core.tool_use_loop import ToolUseLoop

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

            with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
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

            with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
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


class TestSpawnAgentChildBudgetExhaustion:
    """A spawned child that hits the shared session step budget
    gets a forced wrap-up turn instead of a bare halt, and its text rides the
    ordinary ``AgentResult.summary`` channel back to the parent — no new
    plumbing needed, this is a direct consequence of the loop change.

    The child settles ``failed``: ``state.done`` is True for a budget halt, so
    reading it as success is what let a fleet of halted children report clean.
    Both halves are asserted together on purpose — an honest terminal is only
    worth having if it still hands back the work that was done."""

    def test_blocking_child_summary_carries_wrapup_text(self):
        import json

        async def _test():
            registry = _make_registry("shell_tool")
            # depth=1 (non-root) -> blocking spawn path.
            ctx = _make_context(depth=1)
            # Exhaust the SHARED session budget before the child even starts.
            ctx.registry._session_step_budget = 2
            ctx.registry._total_steps = 2

            tool = SpawnAgentTool(
                agent_context=ctx,
                tool_registry=registry,
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )

            wrapup_invoke = AsyncMock(
                return_value=_text_response("Wrap-up: partial work done, X remains.")
            )

            with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = MagicMock()
                mock_build.return_value.ainvoke = wrapup_invoke

                step = ActionStep(
                    tool_id="spawn_agent",
                    operation="set",
                    tool_input={"task": "do work"},
                )
                result = await tool.run_async(step)

            parsed = json.loads(result.content)
            # A spent budget is NOT a completion — the child stopped without
            # reaching its goal, so it reports ``failed``. The wrap-up text is
            # the point of the pairing: reporting failure honestly must not
            # also throw away the partial work the wrap-up turn produced.
            assert parsed["status"] == "failed"
            assert "Wrap-up: partial work done" in parsed["summary"]

        asyncio.run(_test())


class TestSpawnAgentDelegationContract:
    """Per-spawn ``contract`` wiring at the ``spawn_agent`` seam:
    parsing, the child's OWN step budget (layered under the session budget),
    model_tier resolution, and ``check_agents`` visibility."""

    def test_unset_contract_byte_identical(self):
        """No ``contract`` arg -> the child's handle carries the disabled
        default and the returned AgentResult JSON is unchanged."""
        import json

        async def _test():
            hypervisor = AgentHypervisor(max_concurrent=10)
            ctx = AgentContext.root(model_name="test-model", max_depth=5, registry=hypervisor)
            tool = SpawnAgentTool(
                agent_context=ctx,
                tool_registry=_make_registry("shell_tool"),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )
            bound = MagicMock()
            bound.ainvoke = AsyncMock(return_value=_text_response("Done!"))
            with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound
                result = await tool.run_async(
                    ActionStep(
                        tool_id="spawn_agent", operation="set", tool_input={"task": "do work"}
                    )
                )
                await tool.await_lifecycle_managers(timeout=5.0)

            agent_id = json.loads(result.content)["agent_id"]
            handle = await hypervisor.get(agent_id)
            assert handle is not None
            assert handle.contract == DelegationContract()
            assert handle.contract.enabled is False

        asyncio.run(_test())

    def test_step_budget_over_runs_wrapup(self):
        """A child with its OWN ``contract.max_steps`` and an always-continue
        fake tool (never naturally completes) is halted by the CONTRACT, not
        session budget or natural completion — the ``stop`` event's
        ``done_reason`` is ``halted_agent_budget`` and the returned
        ``AgentResult.summary`` carries the real wrap-up text, not a stump."""
        import json

        async def _test():
            events: list = []
            root = AgentContext.root(
                model_name="test-model",
                max_depth=5,
                registry=AgentHypervisor(max_concurrent=100),
                event_logger=events.append,
            )
            ctx = root.child()  # depth=1 -> blocking path settles inline
            registry = _make_registry("shell_tool")
            tool = SpawnAgentTool(
                agent_context=ctx,
                tool_registry=registry,
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )

            call_n = {"i": 0}

            def _always_tool_call(msgs, **kwargs):
                call_n["i"] += 1
                n = call_n["i"]
                # Distinct args every turn — a doom-loop guard must not trip.
                return _tool_call_response("shell_tool", {"command": f"x{n}"}, f"c{n}")

            bound = MagicMock()
            bound.ainvoke = AsyncMock(side_effect=_always_tool_call)

            run_n = {"i": 0}

            def _tool_run(step):
                run_n["i"] += 1
                speaker = MagicMock()
                speaker.content = f"ok-{run_n['i']}"
                return speaker

            mock_tool = MagicMock()
            mock_tool.run.side_effect = _tool_run

            wrapup_invoke = AsyncMock(
                return_value=_text_response("Wrap-up: partial work done, X remains.")
            )

            with (
                patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build,
                patch.object(registry, "get", return_value=mock_tool),
            ):
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound
                mock_build.return_value.ainvoke = wrapup_invoke

                step = ActionStep(
                    tool_id="spawn_agent",
                    operation="set",
                    tool_input={"task": "do work", "contract": {"max_steps": 2}},
                )
                result = await tool.run_async(step)

            parsed = json.loads(result.content)
            # An exhausted per-agent contract is a halt, not a completion; the
            # wrap-up text still rides back so the parent keeps the partial work.
            assert parsed["status"] == "failed"
            assert "Wrap-up: partial work done" in parsed["summary"]

            stops = [
                e["payload"]
                for e in events
                if e["type"] == "sub_agent" and e["payload"]["action"] == "stop"
            ]
            assert stops and stops[0]["detail"] == "halted_agent_budget"

        asyncio.run(_test())

    def test_model_tier_resolves_and_degrades(self):
        """model_tier maps through agent.model_tiers; an unmapped tier falls
        through to the ordinary resolution chain; an explicit model arg
        always wins over the tier regardless."""
        import json

        async def _spawn(tool_input: dict, tier_map: dict) -> str:
            """Spawn one non-blocking root child; return the resolved model
            it was registered under."""
            hypervisor = AgentHypervisor(max_concurrent=10)
            ctx = AgentContext.root(model_name="test-model", max_depth=5, registry=hypervisor)
            tool = SpawnAgentTool(
                agent_context=ctx,
                tool_registry=_make_registry("shell_tool"),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )
            bound = MagicMock()
            bound.ainvoke = AsyncMock(return_value=_text_response("Done!"))

            def _config(*args, **kwargs):
                if args == ("agent", "model_tiers"):
                    return tier_map
                return kwargs.get("default")

            with (
                patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build,
                patch("mewbo_core.spawn_agent.get_config_value", side_effect=_config),
            ):
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound
                result = await tool.run_async(
                    ActionStep(tool_id="spawn_agent", operation="set", tool_input=tool_input)
                )
                await tool.await_lifecycle_managers(timeout=5.0)

            agent_id = json.loads(result.content)["agent_id"]
            handle = await hypervisor.get(agent_id)
            assert handle is not None
            return handle.model_name

        async def _test():
            tier_map = {"economy": "tier-model"}

            # A mapped tier resolves to the tier's model.
            resolved = await _spawn({"task": "x", "contract": {"model_tier": "economy"}}, tier_map)
            assert resolved == "tier-model"

            # An unmapped tier is a no-op fallthrough -> ordinary chain (no
            # default_sub_model / allowed_models configured -> parent's model).
            resolved = await _spawn(
                {"task": "x", "contract": {"model_tier": "frontier"}}, tier_map
            )
            assert resolved == "test-model"

            # An explicit model arg always wins over the tier.
            resolved = await _spawn(
                {"task": "x", "model": "caller-model", "contract": {"model_tier": "economy"}},
                tier_map,
            )
            assert resolved == "caller-model"

        asyncio.run(_test())

    def test_check_agents_surfaces_contract(self):
        """check_agents' per-agent payload carries a ``contract`` block IFF
        the child's contract is enabled — a contract-less sibling's entry
        carries no such key."""
        import json

        async def _test():
            root = AgentContext.root(
                model_name="test-model", max_depth=5, registry=AgentHypervisor(max_concurrent=100)
            )
            registry = _make_registry("shell_tool")
            tool = SpawnAgentTool(
                agent_context=root,
                tool_registry=registry,
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )
            bound = MagicMock()
            bound.ainvoke = AsyncMock(return_value=_text_response("Done!"))
            with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound

                with_contract = await tool.run_async(
                    ActionStep(
                        tool_id="spawn_agent",
                        operation="set",
                        tool_input={"task": "with contract", "contract": {"max_steps": 10}},
                    )
                )
                without_contract = await tool.run_async(
                    ActionStep(
                        tool_id="spawn_agent",
                        operation="set",
                        tool_input={"task": "without contract"},
                    )
                )
                await tool.await_lifecycle_managers(timeout=5.0)

            with_id = json.loads(with_contract.content)["agent_id"]
            without_id = json.loads(without_contract.content)["agent_id"]

            payload = json.loads(
                (await tool.handle_check_agents(ActionStep(
                    tool_id="check_agents", operation="set", tool_input={}
                ))).content
            )
            by_id = {a["id"]: a for a in payload["agents"]}
            assert "contract" in by_id[with_id]
            assert by_id[with_id]["contract"]["max_steps"] == 10
            assert "contract" not in by_id[without_id]

        asyncio.run(_test())


class TestSpawnAgentSummaryKind:
    """Phase 1b — task-typed ``AgentResult.summary_kind`` (Ref: [CoA §3]).

    ``summary_kind`` is opt-in: unset (or unrecognised) must leave the child's
    task text and the returned ``AgentResult`` byte-identical to the historical
    untyped path. Declared, it appends ONE directive (a plain dict lookup on
    ``SpawnAgentTask.SUMMARY_KIND_DIRECTIVES`` — never a per-kind branch in the
    spawn service) and is stamped onto the result and the ``stop`` event.
    """

    async def _spawn_blocking(self, tool_input: dict):
        """Depth=1 (non-root) so the blocking path settles inline."""
        ctx = _make_context(depth=1)
        tool = SpawnAgentTool(
            agent_context=ctx,
            tool_registry=_make_registry("shell_tool"),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
        )
        bound = MagicMock()
        bound.ainvoke = AsyncMock(return_value=_text_response("Done!"))
        with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound
            result = await tool.run_async(
                ActionStep(tool_id="spawn_agent", operation="set", tool_input=tool_input)
            )
        sent_messages = bound.ainvoke.call_args_list[0].args[0]
        human_content = sent_messages[1].content
        return result, human_content

    def test_evidence_kind_appends_directive_and_stamps_result(self):
        import json

        async def _test():
            result, human_content = await self._spawn_blocking(
                {"task": "investigate the outage", "summary_kind": "evidence"}
            )
            assert "evidence package" in human_content
            parsed = json.loads(result.content)
            assert parsed["summary_kind"] == "evidence"

        asyncio.run(_test())

    def test_running_summary_and_code_signature_kinds_use_their_own_directive(self):
        """The directive text is looked up per kind, not one fixed string."""
        from mewbo_core.spawn_agent import SpawnAgentTask

        async def _test():
            _, running_content = await self._spawn_blocking(
                {"task": "watch the deploy", "summary_kind": "running_summary"}
            )
            _, code_content = await self._spawn_blocking(
                {"task": "map the auth module", "summary_kind": "code_signature"}
            )
            assert (
                SpawnAgentTask.SUMMARY_KIND_DIRECTIVES["running_summary"] in running_content
            )
            assert SpawnAgentTask.SUMMARY_KIND_DIRECTIVES["code_signature"] in code_content
            # The two directives are genuinely distinct — no shared fixed string.
            assert running_content != code_content

        asyncio.run(_test())

    def test_unset_summary_kind_is_byte_identical(self):
        """No ``summary_kind`` -> task text untouched, result stamped "generic"."""
        import json

        async def _test():
            result, human_content = await self._spawn_blocking({"task": "do work"})
            assert human_content == "do work"
            parsed = json.loads(result.content)
            assert parsed["summary_kind"] == "generic"

        asyncio.run(_test())

    def test_unrecognised_summary_kind_falls_back_to_generic(self):
        """A malformed/unknown kind degrades to the safe default, never raises."""
        import json

        async def _test():
            result, human_content = await self._spawn_blocking(
                {"task": "do work", "summary_kind": "not-a-real-kind"}
            )
            assert human_content == "do work"
            parsed = json.loads(result.content)
            assert parsed["summary_kind"] == "generic"

        asyncio.run(_test())

    def test_stop_event_includes_summary_kind_when_declared(self):
        async def _test():
            events: list = []
            root = AgentContext.root(
                model_name="test-model",
                max_depth=5,
                registry=AgentHypervisor(max_concurrent=100),
                event_logger=events.append,
            )
            ctx = root.child()
            tool = SpawnAgentTool(
                agent_context=ctx,
                tool_registry=_make_registry("shell_tool"),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )
            bound = MagicMock()
            bound.ainvoke = AsyncMock(return_value=_text_response("Done!"))
            with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound
                await tool.run_async(ActionStep(
                    tool_id="spawn_agent",
                    operation="set",
                    tool_input={"task": "say hello", "summary_kind": "evidence"},
                ))

            stops = [
                e["payload"]
                for e in events
                if e["type"] == "sub_agent" and e["payload"]["action"] == "stop"
            ]
            assert stops and stops[0]["summary_kind"] == "evidence"

        asyncio.run(_test())

    def test_stop_event_omits_summary_kind_by_default(self):
        async def _test():
            events: list = []
            root = AgentContext.root(
                model_name="test-model",
                max_depth=5,
                registry=AgentHypervisor(max_concurrent=100),
                event_logger=events.append,
            )
            ctx = root.child()
            tool = SpawnAgentTool(
                agent_context=ctx,
                tool_registry=_make_registry("shell_tool"),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )
            bound = MagicMock()
            bound.ainvoke = AsyncMock(return_value=_text_response("Done!"))
            with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound
                await tool.run_async(ActionStep(
                    tool_id="spawn_agent",
                    operation="set",
                    tool_input={"task": "say hello"},
                ))

            stops = [
                e["payload"]
                for e in events
                if e["type"] == "sub_agent" and e["payload"]["action"] == "stop"
            ]
            assert stops and "summary_kind" not in stops[0]

        asyncio.run(_test())


class TestSpawnAgentSchema:
    """Ref: [DeepMind-Delegation §4.1] Contract-first decomposition with acceptance criteria."""

    def test_schema_includes_max_steps_deprecated(self):
        """max_steps field is retained in schema for backward compatibility."""
        from mewbo_core.spawn_agent import SPAWN_AGENT_SCHEMA

        props = SPAWN_AGENT_SCHEMA["function"]["parameters"]["properties"]
        assert "max_steps" in props
        assert props["max_steps"]["type"] == "integer"
        assert "deprecated" in props["max_steps"]["description"].lower()

    def test_schema_includes_acceptance_criteria(self):
        from mewbo_core.spawn_agent import SPAWN_AGENT_SCHEMA

        props = SPAWN_AGENT_SCHEMA["function"]["parameters"]["properties"]
        assert "acceptance_criteria" in props
        assert props["acceptance_criteria"]["type"] == "string"

    def test_schema_includes_summary_kind(self):
        """Phase 1b — optional task-typed CU shape, ``spawn_agents``
        picks it up for free since its ``items`` schema IS this one (DRY)."""
        from mewbo_core.spawn_agent import SPAWN_AGENT_SCHEMA, SPAWN_AGENTS_SCHEMA

        props = SPAWN_AGENT_SCHEMA["function"]["parameters"]["properties"]
        assert "summary_kind" in props
        assert props["summary_kind"]["type"] == "string"
        assert set(props["summary_kind"]["enum"]) == {
            "evidence",
            "running_summary",
            "code_signature",
            "generic",
        }
        batch_props = SPAWN_AGENTS_SCHEMA["function"]["parameters"]["properties"]["tasks"][
            "items"
        ]["properties"]
        assert "summary_kind" in batch_props


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

    with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
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
        from mewbo_core.spawn_agent import substitute_agent_body

        body = "root=${CLAUDE_PLUGIN_ROOT}\nsession=${SESSION_ID}"
        out = substitute_agent_body(
            body,
            {"CLAUDE_PLUGIN_ROOT": "/plugins/x", "SESSION_ID": "s1"},
            env={},
        )
        assert out == "root=/plugins/x\nsession=s1"

    def test_bash_default_when_env_unset(self):
        from mewbo_core.spawn_agent import substitute_agent_body

        body = "root=${MEWBO_WIDGET_ROOT:-/tmp/mewbo/widgets}"
        out = substitute_agent_body(body, {}, env={})
        assert out == "root=/tmp/mewbo/widgets"

    def test_bash_default_honours_env_when_set(self):
        from mewbo_core.spawn_agent import substitute_agent_body

        body = "root=${MEWBO_WIDGET_ROOT:-/tmp/mewbo/widgets}"
        out = substitute_agent_body(
            body, {}, env={"MEWBO_WIDGET_ROOT": "/custom/path"}
        )
        assert out == "root=/custom/path"

    def test_plain_dollar_var_expands_from_env(self):
        from mewbo_core.spawn_agent import substitute_agent_body

        body = "home is $HOME"
        out = substitute_agent_body(body, {}, env={"HOME": "/root"})
        assert out == "home is /root"

    def test_unknown_plain_dollar_var_stays_literal(self):
        from mewbo_core.spawn_agent import substitute_agent_body

        body = "unset $NOT_A_REAL_VARIABLE"
        out = substitute_agent_body(body, {}, env={})
        assert out == "unset $NOT_A_REAL_VARIABLE"

    def test_all_three_passes_compose(self):
        from mewbo_core.spawn_agent import substitute_agent_body

        body = (
            "plugin=${CLAUDE_PLUGIN_ROOT} "
            "root=${MEWBO_WIDGET_ROOT:-/tmp/mewbo/widgets} "
            "shell=$SHELL"
        )
        out = substitute_agent_body(
            body,
            {"CLAUDE_PLUGIN_ROOT": "/plugins/widget-builder"},
            env={"SHELL": "/bin/zsh"},
        )
        assert out == (
            "plugin=/plugins/widget-builder "
            "root=/tmp/mewbo/widgets "
            "shell=/bin/zsh"
        )
