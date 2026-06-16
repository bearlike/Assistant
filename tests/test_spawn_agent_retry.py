#!/usr/bin/env python3
"""Tests for bounded auto-retry / re-delegation of sub-agents (Gitea #118).

Coverage
--------
- ``RetryPolicy`` parse/validate + cause classification (unit, no I/O).
- The retry driver over the REAL ``SpawnAgentTool`` admit→run→resolve path,
  stubbing only the model boundary (``build_chat_model`` → ``bound.ainvoke``):
    * fail-once-then-succeed → ``completed`` with ``attempts == 2``;
    * fail > max → ``failed`` with the last error preserved + ``attempts``;
    * default off (no ``retry``) → exactly one attempt (byte-identical path);
    * cancellation is NEVER retried;
    * the non-blocking root path retries via the lifecycle manager and the
      retry is visible through ``check_agents`` + ``render_agent_tree``.

A fresh ``ToolUseLoop`` is built per attempt, so each retry re-runs the #54
fallback ladder rather than this layer reinventing model escalation.
"""

from __future__ import annotations

import asyncio
import json
import queue
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage
from mewbo_core.agent_context import AgentContext
from mewbo_core.classes import ActionStep
from mewbo_core.hooks import HookManager
from mewbo_core.hypervisor import AgentHandle, AgentHypervisor
from mewbo_core.llm_resilience import LlmResilienceExhausted
from mewbo_core.permissions import PermissionDecision, PermissionPolicy
from mewbo_core.spawn_agent import SPAWN_AGENT_SCHEMA, RetryPolicy, SpawnAgentTool
from mewbo_core.tool_registry import ToolRegistry, ToolSpec

# ---------------------------------------------------------------------------
# Helpers (mirror tests/test_spawn_agent_flow.py — stub only the model boundary)
# ---------------------------------------------------------------------------


def _make_spec(tool_id: str = "shell_tool") -> ToolSpec:
    return ToolSpec(
        tool_id=tool_id,
        name=tool_id,
        description=f"Test tool {tool_id}",
        factory=lambda: MagicMock(),
        enabled=True,
        kind="local",
        metadata={"schema": {"type": "object", "properties": {}}},
    )


def _make_registry(*tool_ids: str) -> ToolRegistry:
    registry = ToolRegistry()
    for tid in tool_ids or ("shell_tool",):
        registry.register(_make_spec(tid))
    return registry


def _make_hook_manager() -> HookManager:
    hm = MagicMock(spec=HookManager)
    hm.run_on_agent_start.return_value = None
    hm.run_on_agent_stop.return_value = None
    return hm


def _allow_all_policy() -> PermissionPolicy:
    policy = MagicMock(spec=PermissionPolicy)
    policy.decide.return_value = PermissionDecision.ALLOW
    return policy


def _make_spawn_tool(ctx: AgentContext) -> SpawnAgentTool:
    return SpawnAgentTool(
        agent_context=ctx,
        tool_registry=_make_registry("shell_tool"),
        permission_policy=_allow_all_policy(),
        hook_manager=_make_hook_manager(),
    )


def _step(task: str, **extra) -> ActionStep:
    return ActionStep(tool_id="spawn_agent", operation="set", tool_input={"task": task, **extra})


def _bound_with(side_effect) -> MagicMock:
    bound = MagicMock()
    bound.ainvoke = AsyncMock(side_effect=side_effect)
    return bound


def _patch_model(bound: MagicMock):
    """Patch build_chat_model so every attempt's ToolUseLoop binds *bound*."""
    cm = patch("mewbo_core.tool_use_loop.build_chat_model")
    mock_build = cm.start()
    mock_build.return_value = MagicMock()
    mock_build.return_value.bind_tools.return_value = bound
    return cm


# ===========================================================================
# RetryPolicy — unit (no I/O)
# ===========================================================================


class TestRetryPolicyParsing:
    def test_default_off(self):
        p = RetryPolicy.from_value(None)
        assert p.max == 0
        assert p.enabled is False
        # Default off must not retry any cause, even a listed one.
        assert p.should_retry("failed", attempt=1) is False

    def test_unset_is_off(self):
        assert RetryPolicy.from_value({}).max == 0

    def test_parses_max_on_backoff(self):
        p = RetryPolicy.from_value({"max": 3, "on": ["timeout"], "backoff": 0.5})
        assert p.max == 3
        assert p.on == ("timeout",)
        assert p.backoff == 0.5
        assert p.enabled is True

    def test_invalid_max_degrades_to_off(self):
        assert RetryPolicy.from_value({"max": "lots"}).max == 0
        assert RetryPolicy.from_value({"max": -4}).max == 0

    def test_empty_or_invalid_on_falls_back_to_both(self):
        assert RetryPolicy.from_value({"max": 2, "on": []}).on == ("timeout", "failed")
        assert RetryPolicy.from_value({"max": 2, "on": ["bogus"]}).on == ("timeout", "failed")

    def test_invalid_backoff_degrades_to_default(self):
        assert RetryPolicy.from_value({"max": 1, "backoff": "soon"}).backoff == 1.0

    def test_non_mapping_value_is_off(self):
        assert RetryPolicy.from_value("nope").max == 0
        assert RetryPolicy.from_value([1, 2]).max == 0


class TestRetryPolicyBehaviour:
    def test_should_retry_respects_budget_and_on(self):
        p = RetryPolicy(max=2, on=("failed",))
        assert p.should_retry("failed", attempt=1) is True
        assert p.should_retry("failed", attempt=2) is True
        assert p.should_retry("failed", attempt=3) is False  # budget spent
        assert p.should_retry("timeout", attempt=1) is False  # not in `on`

    def test_backoff_is_exponential(self):
        p = RetryPolicy(max=5, backoff=1.0)
        assert p.backoff_for(1) == 1.0
        assert p.backoff_for(2) == 2.0
        assert p.backoff_for(3) == 4.0

    def test_classify_cause_timeout_vs_failed(self):
        assert RetryPolicy.classify_cause(asyncio.TimeoutError()) == "timeout"
        assert RetryPolicy.classify_cause(ValueError("boom")) == "failed"

    def test_classify_cause_reads_exhausted_reason(self):
        exc = LlmResilienceExhausted(
            ["m"], asyncio.TimeoutError(), "TimeoutError", reason="timeout"
        )
        assert RetryPolicy.classify_cause(exc) == "timeout"
        exc2 = LlmResilienceExhausted(
            ["m"], ValueError("x"), "ValueError", reason="deterministic"
        )
        assert RetryPolicy.classify_cause(exc2) == "failed"


class TestSpawnSchemaHasRetry:
    def test_retry_field_declared(self):
        fn = SPAWN_AGENT_SCHEMA["function"]
        assert isinstance(fn, dict)
        params = fn["parameters"]
        props = params["properties"]
        assert "retry" in props
        retry = props["retry"]
        assert retry["type"] == "object"
        assert set(retry["properties"]) == {"max", "on", "backoff"}
        # `retry` must NOT be required — opt-in only.
        assert "retry" not in params["required"]


# ===========================================================================
# Driver over the real SpawnAgentTool (blocking, depth=1) path
# ===========================================================================


def _blocking_ctx() -> tuple[AgentContext, AgentHypervisor]:
    hv = AgentHypervisor(max_concurrent=100)
    root = AgentContext.root(model_name="test-model", max_depth=5, registry=hv)
    return root.child(), hv  # depth=1 → blocking spawn path


class TestBlockingRetry:
    def test_fail_once_then_succeed_attempts_2(self):
        async def _test():
            ctx, _hv = _blocking_ctx()
            tool = _make_spawn_tool(ctx)
            bound = _bound_with([ValueError("transient boom"), AIMessage(content="recovered")])
            cm = _patch_model(bound)
            try:
                result = await tool.run_async(
                    _step("flaky work", retry={"max": 2, "backoff": 0})
                )
            finally:
                cm.stop()

            parsed = json.loads(result.content)
            assert parsed["status"] == "completed"
            assert parsed["attempts"] == 2
            assert bound.ainvoke.call_count == 2

        asyncio.run(_test())

    def test_fail_beyond_max_resolves_failed_last_error_preserved(self):
        async def _test():
            ctx, _hv = _blocking_ctx()
            tool = _make_spawn_tool(ctx)

            async def _always_fail(*a, **k):
                raise ValueError("persistent boom")

            bound = _bound_with(_always_fail)
            cm = _patch_model(bound)
            try:
                result = await tool.run_async(
                    _step("doomed work", retry={"max": 1, "backoff": 0})
                )
            finally:
                cm.stop()

            parsed = json.loads(result.content)
            assert parsed["status"] == "failed"
            assert parsed["attempts"] == 2  # 1 initial + 1 retry, then give up
            assert "persistent boom" in (parsed["content"] + " ".join(parsed["warnings"]))
            assert bound.ainvoke.call_count == 2

        asyncio.run(_test())

    def test_default_off_single_attempt(self):
        """No `retry` field ⇒ exactly one attempt (byte-identical behaviour)."""

        async def _test():
            ctx, _hv = _blocking_ctx()
            tool = _make_spawn_tool(ctx)

            async def _always_fail(*a, **k):
                raise ValueError("boom")

            bound = _bound_with(_always_fail)
            cm = _patch_model(bound)
            try:
                result = await tool.run_async(_step("plain work"))
            finally:
                cm.stop()

            parsed = json.loads(result.content)
            assert parsed["status"] == "failed"
            assert parsed["attempts"] == 1
            assert bound.ainvoke.call_count == 1

        asyncio.run(_test())

    def test_on_filter_skips_unlisted_cause(self):
        """`on: ['timeout']` must NOT retry a generic 'failed' cause."""

        async def _test():
            ctx, _hv = _blocking_ctx()
            tool = _make_spawn_tool(ctx)

            async def _always_fail(*a, **k):
                raise ValueError("boom")  # classifies as 'failed', not 'timeout'

            bound = _bound_with(_always_fail)
            cm = _patch_model(bound)
            try:
                result = await tool.run_async(
                    _step("typed work", retry={"max": 3, "on": ["timeout"], "backoff": 0})
                )
            finally:
                cm.stop()

            parsed = json.loads(result.content)
            assert parsed["status"] == "failed"
            assert parsed["attempts"] == 1  # 'failed' not in `on` → no retry
            assert bound.ainvoke.call_count == 1

        asyncio.run(_test())

    def test_cancellation_is_never_retried(self):
        """A parent cancellation propagates and is not turned into a retry."""

        async def _test():
            ctx, _hv = _blocking_ctx()
            tool = _make_spawn_tool(ctx)

            async def _stall(*a, **k):
                await asyncio.sleep(999)
                return AIMessage(content="never")

            bound = _bound_with(_stall)
            cm = _patch_model(bound)
            try:
                run_task = asyncio.create_task(
                    tool.run_async(_step("stalling", retry={"max": 5, "backoff": 0}))
                )
                await asyncio.sleep(0.05)
                run_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await run_task
            finally:
                cm.stop()

            # The model was entered exactly once — cancellation short-circuited
            # the retry loop rather than spawning a fresh attempt.
            assert bound.ainvoke.call_count == 1

        asyncio.run(_test())


# ===========================================================================
# Non-blocking root path — retry via the lifecycle manager
# ===========================================================================


class TestNonBlockingRetry:
    def test_root_retry_visible_in_check_agents_and_tree(self):
        async def _test():
            root_q: queue.Queue[str] = queue.Queue()
            hv = AgentHypervisor(max_concurrent=100)
            ctx = AgentContext.root(
                model_name="test-model", max_depth=5, registry=hv, message_queue=root_q
            )
            root_handle = AgentHandle(
                agent_id=ctx.agent_id,
                parent_id=None,
                depth=0,
                model_name="test-model",
                task_description="root",
                status="running",
                message_queue=root_q,
            )
            await hv.register(root_handle)

            tool = _make_spawn_tool(ctx)
            bound = _bound_with([ValueError("transient"), AIMessage(content="recovered")])
            cm = _patch_model(bound)
            try:
                await tool.run_async(_step("flaky root child", retry={"max": 2, "backoff": 0}))
                await tool.await_lifecycle_managers(timeout=5.0)
            finally:
                cm.stop()

            children = await hv.list_children(ctx.agent_id)
            assert len(children) == 1
            child = children[0]
            assert child.status == "completed"
            assert child.attempts == 2
            assert child.result is not None
            assert child.result.attempts == 2

            # check_agents payload carries attempts.
            check = await tool.handle_check_agents(_step("ignored"))
            payload = json.loads(check.content)
            agent_rows = [a for a in payload["agents"] if a["id"] == child.agent_id]
            assert agent_rows and agent_rows[0]["attempts"] == 2

            # render_agent_tree shows the retry marker.
            tree = await hv.render_agent_tree()
            assert "2 attempts" in tree

        asyncio.run(_test())
