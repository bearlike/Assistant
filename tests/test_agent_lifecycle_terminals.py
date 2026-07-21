#!/usr/bin/env python3
"""Contract tests: every spawned agent's ``start`` gets exactly one ``stop``.

The console derives an agent's liveness purely from the last ``sub_agent``
payload it can see, so a lifecycle that ends without a terminal ``stop`` pins
that agent as live forever. These tests assert on the EVENT LOG the session
would hold — never on registry internals — because the event log is the only
surface a downstream consumer actually reads.

Real ``AgentHypervisor`` / ``AgentContext`` / ``SpawnAgentTool``; the model is
the single stubbed I/O boundary.
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
from mewbo_core.permissions import PermissionDecision, PermissionPolicy
from mewbo_core.spawn_agent import SpawnAgentTool
from mewbo_core.tool_registry import ToolRegistry, ToolSpec

# ---------------------------------------------------------------------------
# Harness (mirrors tests/test_spawn_agent_flow.py)
# ---------------------------------------------------------------------------


def _make_registry(*tool_ids: str) -> ToolRegistry:
    registry = ToolRegistry()
    for tid in tool_ids:
        registry.register(
            ToolSpec(
                tool_id=tid,
                name=tid,
                description=f"Test tool {tid}",
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
        )
    return registry


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


def _make_tool(ctx: AgentContext) -> SpawnAgentTool:
    return SpawnAgentTool(
        agent_context=ctx,
        tool_registry=_make_registry("shell_tool"),
        permission_policy=_allow_all_policy(),
        hook_manager=_make_hook_manager(),
    )


def _spawn_step(task: str = "do the thing") -> ActionStep:
    return ActionStep(tool_id="spawn_agent", operation="set", tool_input={"task": task})


def _stops(events: list[dict], agent_id: str | None = None) -> list[dict]:
    """Every terminal ``stop`` payload in the log, optionally for one agent."""
    return [
        e["payload"]
        for e in events
        if e.get("type") == "sub_agent"
        and e["payload"].get("action") == "stop"
        and (agent_id is None or e["payload"].get("agent_id") == agent_id)
    ]


def _starts(events: list[dict]) -> list[dict]:
    return [
        e["payload"]
        for e in events
        if e.get("type") == "sub_agent" and e["payload"].get("action") == "start"
    ]


async def _await_child_task(hv: AgentHypervisor, agent_id: str) -> AgentHandle:
    """Wait until the child's loop task exists, so a cancel can land on it."""
    for _ in range(200):
        handle = await hv.get(agent_id)
        if handle is not None and handle.asyncio_task is not None:
            return handle
        await asyncio.sleep(0.01)
    raise AssertionError(f"child {agent_id} never started its loop task")


async def _hang_forever(*_args, **_kwargs):
    await asyncio.Event().wait()


def _patch_model(*, response: str | None = None, boom: bool = False, hang: bool = False):
    """Patch the child loop's model — the one stubbed I/O boundary."""
    mock_build = patch("mewbo_core.tool_use_loop.build_chat_model")
    started = mock_build.start()
    if boom:
        started.side_effect = RuntimeError("provider unreachable")
        return mock_build
    bound = MagicMock()
    if hang:
        bound.ainvoke = AsyncMock(side_effect=_hang_forever)
    else:
        bound.ainvoke = AsyncMock(return_value=AIMessage(content=response or "done"))
    started.return_value = MagicMock()
    started.return_value.bind_tools.return_value = bound
    return mock_build


# ---------------------------------------------------------------------------
# C1 — every start gets exactly one terminal stop
# ---------------------------------------------------------------------------


class TestBackgroundLifecycleTerminals:
    """The root (depth-0) non-blocking path, driven by ``_run_child_lifecycle``."""

    def _drive(self, *, outcome: str) -> list[dict]:
        """Spawn one background child and settle it via ``outcome``.

        Returns the full captured event log. ``outcome`` is one of
        ``completed`` (the model returns), ``failed`` (the model raises) or
        ``cancelled`` (the child hangs and is cancelled through the registry).
        """
        events: list[dict] = []

        async def _test():
            hv = AgentHypervisor(max_concurrent=100)
            root = AgentContext.root(
                model_name="test-model",
                max_depth=5,
                registry=hv,
                message_queue=queue.Queue(),
                event_logger=events.append,
            )
            tool = _make_tool(root)  # depth 0 → non-blocking lifecycle manager

            patcher = _patch_model(
                boom=outcome == "failed",
                hang=outcome == "cancelled",
                response="child result",
            )
            try:
                result = await tool.run_async(_spawn_step())
                child_id = json.loads(result.content)["agent_id"]
                if outcome == "cancelled":
                    await _await_child_task(hv, child_id)
                    await hv.cancel_agent(child_id)
                await tool.await_lifecycle_managers(timeout=3.0)
            finally:
                patcher.stop()

        asyncio.run(_test())
        return events

    @pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
    def test_every_start_gets_exactly_one_stop(self, outcome):
        """No terminal path may leave a start pinned open — nor emit twice."""
        events = self._drive(outcome=outcome)
        assert len(_starts(events)) == 1
        stops = _stops(events)
        assert len(stops) == 1, f"{outcome}: expected exactly one stop, got {len(stops)}"

    def test_cancelled_stop_reports_cancelled_status(self):
        """A cancelled child's terminal event carries the cancelled status."""
        stops = _stops(self._drive(outcome="cancelled"))
        assert stops[0]["status"] == "cancelled"
        assert stops[0]["detail"]  # the cancellation reason, never blank

    def test_failed_stop_reports_failed_status(self):
        """A failed child settles as ``failed``, not as a silent open span."""
        stops = _stops(self._drive(outcome="failed"))
        assert stops[0]["status"] == "failed"

    def test_terminal_payload_shape_matches_the_success_shape(self):
        """Consoles parse one shape — a cancelled stop must not be a special case."""
        success = _stops(self._drive(outcome="completed"))[0]
        cancelled = _stops(self._drive(outcome="cancelled"))[0]
        required = {
            "action",
            "agent_id",
            "parent_id",
            "depth",
            "model",
            "detail",
            "status",
            "steps_completed",
            "input_tokens",
            "output_tokens",
        }
        assert required <= set(success)
        assert required <= set(cancelled), "cancelled stop is missing keys consoles read"


class TestBlockingLifecycleTerminals:
    """The depth>0 blocking path, settled inline by ``_spawn_one``."""

    def _drive_depth1(self, *, boom: bool) -> list[dict]:
        events: list[dict] = []

        async def _test():
            hv = AgentHypervisor(max_concurrent=100)
            root = AgentContext.root(
                model_name="test-model",
                max_depth=5,
                registry=hv,
                message_queue=queue.Queue(),
                event_logger=events.append,
            )
            tool = _make_tool(root.child())  # depth 1 → blocking
            patcher = _patch_model(boom=boom, response="inline result")
            try:
                await tool.run_async(_spawn_step())
            finally:
                patcher.stop()

        asyncio.run(_test())
        return events

    def test_blocking_success_emits_exactly_one_stop(self):
        events = self._drive_depth1(boom=False)
        assert len(_starts(events)) == 1
        assert len(_stops(events)) == 1

    def test_blocking_failure_emits_exactly_one_stop(self):
        """The inline failure path settled the registry but wrote no event."""
        events = self._drive_depth1(boom=True)
        assert len(_starts(events)) == 1
        stops = _stops(events)
        assert len(stops) == 1
        assert stops[0]["status"] == "failed"

    def test_blocking_cancel_emits_exactly_one_stop(self):
        """Cancelling the awaiting parent settles the child in the log too."""
        events: list[dict] = []

        async def _test():
            hv = AgentHypervisor(max_concurrent=100)
            root = AgentContext.root(
                model_name="test-model",
                max_depth=5,
                registry=hv,
                message_queue=queue.Queue(),
                event_logger=events.append,
            )
            tool = _make_tool(root.child())
            patcher = _patch_model(hang=True)
            try:
                spawn = asyncio.create_task(tool.run_async(_spawn_step()))
                # Wait for the start event, then cancel the awaiting spawn.
                for _ in range(200):
                    if _starts(events):
                        break
                    await asyncio.sleep(0.01)
                spawn.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await spawn
            finally:
                patcher.stop()

        asyncio.run(_test())
        assert len(_starts(events)) == 1
        stops = _stops(events)
        assert len(stops) == 1
        assert stops[0]["status"] == "cancelled"


class TestCascadeTerminals:
    """A child cancelled by its parent's cascade is settled in the log."""

    @pytest.mark.parametrize("grandchild_status", ["running", "submitted"])
    def test_cascaded_child_gets_a_terminal_stop(self, grandchild_status):
        """The cascade tears the grandchild down — the log must say so.

        A ``submitted`` grandchild is the sharper case: nothing is driving it,
        so it can never emit its own terminal.
        """
        events: list[dict] = []

        async def _test():
            hv = AgentHypervisor(max_concurrent=100)
            root = AgentContext.root(
                model_name="test-model",
                max_depth=5,
                registry=hv,
                message_queue=queue.Queue(),
                event_logger=events.append,
            )
            tool = _make_tool(root)
            patcher = _patch_model(hang=True)
            grand_task = asyncio.create_task(_hang_forever())
            try:
                result = await tool.run_async(_spawn_step())
                child_id = json.loads(result.content)["agent_id"]
                await _await_child_task(hv, child_id)
                await hv.register(
                    AgentHandle(
                        agent_id="grandchild-1",
                        parent_id=child_id,
                        depth=2,
                        model_name="test-model",
                        task_description="nested work",
                        status=grandchild_status,
                        asyncio_task=grand_task,
                    )
                )
                await hv.cancel_agent(child_id)
                await tool.await_lifecycle_managers(timeout=3.0)
            finally:
                grand_task.cancel()
                patcher.stop()

        asyncio.run(_test())
        grand_stops = _stops(events, agent_id="grandchild-1")
        assert len(grand_stops) == 1, "cascaded grandchild left pinned open"
        assert grand_stops[0]["status"] == "cancelled"
        assert grand_stops[0]["parent_id"] is not None


# ---------------------------------------------------------------------------
# C2 — cleanup covers submitted, not just running
# ---------------------------------------------------------------------------


class TestHypervisorCleanupCoverage:
    """``cleanup()`` must settle every non-terminal agent it is clearing."""

    @pytest.mark.parametrize("status", ["running", "submitted"])
    def test_cleanup_marks_non_terminal_agents_cancelled(self, status):
        """An agent still at ``submitted`` was silently skipped by the filter."""

        async def _test():
            hv = AgentHypervisor(max_concurrent=10)
            handle = AgentHandle(
                agent_id="a1",
                parent_id=None,
                depth=1,
                model_name="test-model",
                task_description="work",
                status=status,
            )
            await hv.register(handle)
            await hv.cleanup(timeout=0.1)
            return handle

        handle = asyncio.run(_test())
        assert handle.status == "cancelled", f"{handle.status} left non-terminal"
        assert handle.stopped_at is not None

    def test_cleanup_leaves_terminal_status_untouched(self):
        """A completed agent is never rewritten to cancelled."""

        async def _test():
            hv = AgentHypervisor(max_concurrent=10)
            handle = AgentHandle(
                agent_id="done-1",
                parent_id=None,
                depth=1,
                model_name="test-model",
                task_description="work",
                status="completed",
            )
            await hv.register(handle)
            await hv.cleanup(timeout=0.1)
            return handle

        assert asyncio.run(_test()).status == "completed"
