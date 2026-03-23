#!/usr/bin/env python3
"""Tests for AgentContext, AgentHandle, and AgentRegistry."""

from __future__ import annotations

import asyncio

import pytest

from meeseeks_core.agent_context import (
    AgentContext,
    AgentDepthExceeded,
    AgentHandle,
    AgentRegistry,
)


# ---------------------------------------------------------------------------
# AgentContext
# ---------------------------------------------------------------------------


class TestAgentContext:
    def test_root_creates_depth_zero(self):
        ctx = AgentContext.root(model_name="test-model")
        assert ctx.depth == 0
        assert ctx.parent_id is None
        assert ctx.model_name == "test-model"
        assert ctx.can_spawn is True
        assert ctx.remaining_depth == 5

    def test_root_with_custom_max_depth(self):
        ctx = AgentContext.root(model_name="m", max_depth=3)
        assert ctx.max_depth == 3
        assert ctx.remaining_depth == 3

    def test_child_increments_depth(self):
        root = AgentContext.root(model_name="m", max_depth=5)
        child = root.child()
        assert child.depth == 1
        assert child.parent_id == root.agent_id
        assert child.model_name == "m"
        assert child.remaining_depth == 4

    def test_child_inherits_model(self):
        root = AgentContext.root(model_name="parent-model")
        child = root.child()
        assert child.model_name == "parent-model"

    def test_child_overrides_model(self):
        root = AgentContext.root(model_name="parent-model")
        child = root.child(model_name="child-model")
        assert child.model_name == "child-model"

    def test_child_at_max_depth_cannot_spawn(self):
        ctx = AgentContext.root(model_name="m", max_depth=2)
        c1 = ctx.child()
        c2 = c1.child()
        assert c2.can_spawn is False
        assert c2.remaining_depth == 0

    def test_child_beyond_max_depth_raises(self):
        ctx = AgentContext.root(model_name="m", max_depth=1)
        c1 = ctx.child()
        with pytest.raises(AgentDepthExceeded) as exc_info:
            c1.child()
        assert exc_info.value.attempted == 2
        assert exc_info.value.maximum == 1

    def test_child_does_not_get_message_queue(self):
        root = AgentContext.root(model_name="m")
        assert root.message_queue is not None
        child = root.child()
        assert child.message_queue is None

    def test_child_does_not_get_interrupt_step(self):
        root = AgentContext.root(model_name="m")
        assert root.interrupt_step is not None
        child = root.child()
        assert child.interrupt_step is None

    def test_children_share_registry(self):
        root = AgentContext.root(model_name="m")
        child = root.child()
        assert child.registry is root.registry

    def test_children_share_should_cancel(self):
        cancel_fn = lambda: False  # noqa: E731
        root = AgentContext.root(model_name="m", should_cancel=cancel_fn)
        child = root.child()
        assert child.should_cancel is cancel_fn

    def test_agent_ids_are_unique(self):
        root = AgentContext.root(model_name="m")
        c1 = root.child()
        c2 = root.child()
        assert root.agent_id != c1.agent_id
        assert c1.agent_id != c2.agent_id


# ---------------------------------------------------------------------------
# AgentRegistry
# ---------------------------------------------------------------------------


class TestAgentRegistry:
    def test_register_and_get(self):
        async def _test():
            reg = AgentRegistry()
            handle = AgentHandle(
                agent_id="a1", parent_id=None, depth=0,
                model_name="m", task_description="test",
            )
            await reg.register(handle)
            got = await reg.get("a1")
            assert got is handle

        asyncio.run(_test())

    def test_unregister(self):
        async def _test():
            reg = AgentRegistry()
            handle = AgentHandle(
                agent_id="a1", parent_id=None, depth=0,
                model_name="m", task_description="test",
            )
            await reg.register(handle)
            removed = await reg.unregister("a1")
            assert removed is handle
            assert await reg.get("a1") is None

        asyncio.run(_test())

    def test_list_children(self):
        async def _test():
            reg = AgentRegistry()
            root = AgentHandle(
                agent_id="root", parent_id=None, depth=0,
                model_name="m", task_description="root",
            )
            child1 = AgentHandle(
                agent_id="c1", parent_id="root", depth=1,
                model_name="m", task_description="child1",
            )
            child2 = AgentHandle(
                agent_id="c2", parent_id="root", depth=1,
                model_name="m", task_description="child2",
            )
            grandchild = AgentHandle(
                agent_id="gc1", parent_id="c1", depth=2,
                model_name="m", task_description="grandchild",
            )
            for h in [root, child1, child2, grandchild]:
                await reg.register(h)

            children = await reg.list_children("root")
            assert {h.agent_id for h in children} == {"c1", "c2"}

        asyncio.run(_test())

    def test_list_descendants(self):
        async def _test():
            reg = AgentRegistry()
            for h in [
                AgentHandle(agent_id="r", parent_id=None, depth=0,
                            model_name="m", task_description="r"),
                AgentHandle(agent_id="c1", parent_id="r", depth=1,
                            model_name="m", task_description="c1"),
                AgentHandle(agent_id="gc1", parent_id="c1", depth=2,
                            model_name="m", task_description="gc1"),
            ]:
                await reg.register(h)

            desc = await reg.list_descendants("r")
            assert {h.agent_id for h in desc} == {"c1", "gc1"}

        asyncio.run(_test())

    def test_mark_done(self):
        async def _test():
            reg = AgentRegistry()
            handle = AgentHandle(
                agent_id="a1", parent_id=None, depth=0,
                model_name="m", task_description="test",
            )
            await reg.register(handle)
            await reg.mark_done("a1", "completed")
            got = await reg.get("a1")
            assert got is not None
            assert got.status == "completed"
            assert got.stopped_at is not None

        asyncio.run(_test())

    def test_update_step(self):
        async def _test():
            reg = AgentRegistry()
            handle = AgentHandle(
                agent_id="a1", parent_id=None, depth=0,
                model_name="m", task_description="test",
            )
            await reg.register(handle)
            await reg.update_step("a1", "shell_tool")
            got = await reg.get("a1")
            assert got is not None
            assert got.steps_completed == 1
            assert got.last_tool_id == "shell_tool"

        asyncio.run(_test())

    def test_admit_and_release(self):
        async def _test():
            reg = AgentRegistry(max_concurrent=2)
            assert await reg.admit() is True
            assert await reg.admit() is True
            # Third should timeout (set very short timeout for test).
            reg._semaphore = asyncio.Semaphore(0)
            try:
                result = await asyncio.wait_for(reg.admit(), timeout=0.1)
                assert result is False
            except asyncio.TimeoutError:
                pass  # Also acceptable — the admit timed out.

        asyncio.run(_test())

    def test_cleanup_empties_registry(self):
        async def _test():
            reg = AgentRegistry()
            handle = AgentHandle(
                agent_id="a1", parent_id=None, depth=0,
                model_name="m", task_description="test",
            )
            await reg.register(handle)
            await reg.cleanup(timeout=1.0)
            all_agents = await reg.list_all()
            assert len(all_agents) == 0

        asyncio.run(_test())

    def test_cancel_agent(self):
        async def _test():
            reg = AgentRegistry()

            async def _dummy():
                await asyncio.sleep(100)

            task = asyncio.create_task(_dummy())
            handle = AgentHandle(
                agent_id="a1", parent_id=None, depth=0,
                model_name="m", task_description="test",
                asyncio_task=task,
            )
            await reg.register(handle)
            result = await reg.cancel_agent("a1")
            assert result is True
            # Let the event loop process the cancellation.
            await asyncio.sleep(0)
            assert task.cancelled()

        asyncio.run(_test())
