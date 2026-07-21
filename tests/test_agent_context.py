#!/usr/bin/env python3
"""Tests for AgentContext, AgentHandle, and AgentHypervisor."""

from __future__ import annotations

import asyncio

import pytest
from mewbo_core.agent_context import AgentContext, AgentDepthExceeded
from mewbo_core.hypervisor import AgentHandle, AgentHypervisor, DelegationContract

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

    def test_child_gets_own_message_queue(self):
        """Ref: [DeepMind-Delegation §4.4] Children get their own queue
        for bidirectional parent→child steering."""
        root = AgentContext.root(model_name="m")
        assert root.message_queue is not None
        child = root.child()
        # Child now gets its own queue (not None, not parent's)
        assert child.message_queue is not None
        assert child.message_queue is not root.message_queue

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
# capability_mode monotonic narrowing (Phase 1a)
# ---------------------------------------------------------------------------


class TestAgentContextCapabilityMode:
    def test_root_defaults_to_all(self):
        assert AgentContext.root(model_name="m").capability_mode == "all"

    def test_child_default_inherits_parent(self):
        # An unset request (default "all") inherits the parent's ceiling.
        root = AgentContext.root(model_name="m")
        restricted = root.child(capability_mode="read_only")
        assert restricted.child().capability_mode == "read_only"

    def test_child_narrows_from_all(self):
        root = AgentContext.root(model_name="m")
        assert root.child(capability_mode="read_only").capability_mode == "read_only"
        assert root.child(capability_mode="execute").capability_mode == "execute"

    def test_child_cannot_widen_ceiling(self):
        # A grandchild under a read_only ancestor can never climb back to all.
        root = AgentContext.root(model_name="m")
        child = root.child(capability_mode="read_only")
        grandchild = child.child(capability_mode="all")
        assert grandchild.capability_mode == "read_only"
        # ...nor to the intermediate 'execute' tier.
        assert child.child(capability_mode="execute").capability_mode == "read_only"

    def test_child_narrows_execute_to_read_only(self):
        root = AgentContext.root(model_name="m")
        mid = root.child(capability_mode="execute")
        assert mid.child(capability_mode="read_only").capability_mode == "read_only"

    def test_narrower_helper_picks_more_restrictive(self):
        n = AgentContext._narrower_capability_mode
        assert n("all", "read_only") == "read_only"
        assert n("read_only", "all") == "read_only"
        assert n("execute", "read_only") == "read_only"
        assert n("read_only", "execute") == "read_only"
        assert n("all", "all") == "all"
        assert n("execute", "execute") == "execute"

    def test_narrower_helper_unknown_collapses_to_all(self):
        n = AgentContext._narrower_capability_mode
        # A garbage request neither tightens surprisingly nor loosens below parent.
        assert n("all", "bogus") == "all"
        assert n("read_only", "bogus") == "read_only"
        assert n("bogus", "execute") == "execute"


# ---------------------------------------------------------------------------
# atomic monotonic narrowing
# ---------------------------------------------------------------------------


class TestAgentContextAtomic:
    def test_root_defaults_to_open_ended(self):
        assert AgentContext.root(model_name="m").atomic is False

    def test_child_requesting_atomic_becomes_atomic(self):
        root = AgentContext.root(model_name="m")
        child = root.child(atomic=True)
        assert child.atomic is True

    def test_child_not_requesting_atomic_stays_open_ended(self):
        root = AgentContext.root(model_name="m")
        assert root.child().atomic is False
        assert root.child(atomic=False).atomic is False

    def test_atomic_narrows_monotonically(self):
        """An atomic ancestor's descendants can never climb back to open_ended
        — ``child()`` ORs the parent's bit with the request, it never lets a
        grandchild's own (unset) request override it."""
        root = AgentContext.root(model_name="m")
        atomic_child = root.child(atomic=True)
        # The grandchild does not itself request atomic — still stays atomic.
        grandchild = atomic_child.child(atomic=False)
        assert grandchild.atomic is True
        # ...and every further descendant, no matter how deep.
        assert grandchild.child().atomic is True


# ---------------------------------------------------------------------------
# DelegationContract
# ---------------------------------------------------------------------------


class TestDelegationContract:
    def test_default_is_disabled_and_open_ended(self):
        c = DelegationContract()
        assert c.enabled is False
        assert c.atomic is False

    def test_from_value_total_never_raises(self):
        """Every malformed shape degrades to a safe default — never raises."""
        garbage = [
            None,
            "not a mapping",
            123,
            [],
            {"max_steps": "not-an-int"},
            {"max_wall_s": "not-a-float"},
            {"max_tokens": object()},
            {"autonomy": "bogus-tier"},
            {"model_tier": "bogus-tier"},
            {"step_warn_headroom": "nope"},
            {"max_steps": -5, "max_wall_s": -1.0, "max_tokens": -1},
            {"unknown_field": "gets dropped"},
        ]
        for value in garbage:
            contract = DelegationContract.from_value(value)
            assert isinstance(contract, DelegationContract)
            # Negatives clamp to zero rather than going negative.
            assert contract.max_steps >= 0
            assert contract.max_wall_s >= 0.0
            assert contract.max_tokens >= 0

    def test_from_value_parses_valid_mapping(self):
        contract = DelegationContract.from_value(
            {
                "max_steps": 10,
                "max_wall_s": 60.0,
                "max_tokens": 5000,
                "autonomy": "atomic",
                "model_tier": "economy",
                "step_warn_headroom": 2,
            }
        )
        assert contract.max_steps == 10
        assert contract.max_wall_s == 60.0
        assert contract.max_tokens == 5000
        assert contract.atomic is True
        assert contract.model_tier == "economy"
        assert contract.step_warn_headroom == 2
        assert contract.enabled is True

    def test_step_state_graduated(self):
        c = DelegationContract(max_steps=10, step_warn_headroom=3)
        assert c.step_state(5) == "ok"
        assert c.step_state(7) == "warn"
        assert c.step_state(10) == "over"
        assert c.step_state(11) == "over"

    def test_step_state_unset_is_always_ok(self):
        assert DelegationContract().step_state(10_000) == "ok"

    def test_wall_state_graduated(self):
        c = DelegationContract(max_wall_s=100.0)
        assert c.wall_state(10.0) == "ok"
        assert c.wall_state(80.0) == "warn"
        assert c.wall_state(100.0) == "over"

    def test_token_advisory_no_signal_noop(self):
        """The token axis is advisory: absence of data (or no declared ceiling)
        always reads ``ok``, never ``over`` — never a false failure just
        because a proxy didn't surface usage."""
        # No max_tokens declared at all -> ok regardless of how large total is.
        assert DelegationContract().token_state(999_999) == "ok"
        # max_tokens declared but total_tokens<=0 (no usage signal) -> ok.
        c = DelegationContract(max_tokens=100)
        assert c.token_state(0) == "ok"
        assert c.token_state(-5) == "ok"
        # A genuine signal past the ceiling is the one case that reads over.
        assert c.token_state(150) == "over"
        assert c.token_state(50) == "ok"

    def test_resolve_model_override_explicit_always_wins(self):
        c = DelegationContract(model_tier="economy")
        assert (
            c.resolve_model_override("caller-explicit-model", {"economy": "tier-model"}, None)
            is None
        )

    def test_resolve_model_override_maps_tier(self):
        c = DelegationContract(model_tier="economy")
        assert c.resolve_model_override(None, {"economy": "tier-model"}, None) == "tier-model"

    def test_resolve_model_override_no_tier_declared(self):
        c = DelegationContract()
        assert c.resolve_model_override(None, {"economy": "tier-model"}, None) is None

    def test_resolve_model_override_unmapped_tier_falls_through(self):
        c = DelegationContract(model_tier="frontier")
        assert c.resolve_model_override(None, {"economy": "tier-model"}, None) is None

    def test_resolve_model_override_respects_allowlist(self):
        c = DelegationContract(model_tier="economy")
        assert c.resolve_model_override(None, {"economy": "tier-model"}, ["other-model"]) is None
        assert (
            c.resolve_model_override(None, {"economy": "tier-model"}, ["tier-model"])
            == "tier-model"
        )

    def test_snapshot_is_bounded_scalars(self):
        c = DelegationContract(max_steps=5, autonomy="atomic")
        snap = c.snapshot()
        assert snap == {
            "max_steps": 5,
            "max_wall_s": 0.0,
            "max_tokens": 0,
            "autonomy": "atomic",
            "model_tier": None,
            "step_warn_headroom": 3,
        }


# ---------------------------------------------------------------------------
# AgentRegistry
# ---------------------------------------------------------------------------


class TestAgentHypervisor:
    def test_register_and_get(self):
        async def _test():
            reg = AgentHypervisor()
            handle = AgentHandle(
                agent_id="a1",
                parent_id=None,
                depth=0,
                model_name="m",
                task_description="test",
            )
            await reg.register(handle)
            got = await reg.get("a1")
            assert got is handle

        asyncio.run(_test())

    def test_unregister(self):
        async def _test():
            reg = AgentHypervisor()
            handle = AgentHandle(
                agent_id="a1",
                parent_id=None,
                depth=0,
                model_name="m",
                task_description="test",
            )
            await reg.register(handle)
            removed = await reg.unregister("a1")
            assert removed is handle
            assert await reg.get("a1") is None

        asyncio.run(_test())

    def test_list_children(self):
        async def _test():
            reg = AgentHypervisor()
            root = AgentHandle(
                agent_id="root",
                parent_id=None,
                depth=0,
                model_name="m",
                task_description="root",
            )
            child1 = AgentHandle(
                agent_id="c1",
                parent_id="root",
                depth=1,
                model_name="m",
                task_description="child1",
            )
            child2 = AgentHandle(
                agent_id="c2",
                parent_id="root",
                depth=1,
                model_name="m",
                task_description="child2",
            )
            grandchild = AgentHandle(
                agent_id="gc1",
                parent_id="c1",
                depth=2,
                model_name="m",
                task_description="grandchild",
            )
            for h in [root, child1, child2, grandchild]:
                await reg.register(h)

            children = await reg.list_children("root")
            assert {h.agent_id for h in children} == {"c1", "c2"}

        asyncio.run(_test())

    def test_list_descendants(self):
        async def _test():
            reg = AgentHypervisor()
            for h in [
                AgentHandle(
                    agent_id="r", parent_id=None, depth=0, model_name="m", task_description="r"
                ),
                AgentHandle(
                    agent_id="c1", parent_id="r", depth=1, model_name="m", task_description="c1"
                ),
                AgentHandle(
                    agent_id="gc1", parent_id="c1", depth=2, model_name="m", task_description="gc1"
                ),
            ]:
                await reg.register(h)

            desc = await reg.list_descendants("r")
            assert {h.agent_id for h in desc} == {"c1", "gc1"}

        asyncio.run(_test())

    def test_mark_done(self):
        async def _test():
            reg = AgentHypervisor()
            handle = AgentHandle(
                agent_id="a1",
                parent_id=None,
                depth=0,
                model_name="m",
                task_description="test",
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
            reg = AgentHypervisor()
            handle = AgentHandle(
                agent_id="a1",
                parent_id=None,
                depth=0,
                model_name="m",
                task_description="test",
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
            reg = AgentHypervisor(max_concurrent=2)
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

    def test_try_admit_non_blocking(self):
        """try_admit grabs a free slot, returns False immediately when full."""

        async def _test():
            reg = AgentHypervisor(max_concurrent=1)
            assert await reg.try_admit() is True  # one slot taken
            assert await reg.try_admit() is False  # full → no wait, no block
            reg.release()
            assert await reg.try_admit() is True  # slot freed → admits again

        asyncio.run(_test())

    def test_try_admit_is_race_free_under_concurrency(self):
        """Concurrent try_admit on N slots admits EXACTLY N (the contract).

        Pins the non-blocking admission's atomicity: an unlocked semaphore's
        acquire() does not suspend, so the locked()-check + acquire() runs as
        one synchronous unit and over-admission is impossible even when many
        callers race via asyncio.gather.
        """

        async def _test():
            reg = AgentHypervisor(max_concurrent=3)
            results = await asyncio.gather(*[reg.try_admit() for _ in range(20)])
            assert sum(1 for r in results if r) == 3

        asyncio.run(_test())

    def test_cleanup_empties_registry(self):
        async def _test():
            reg = AgentHypervisor()
            handle = AgentHandle(
                agent_id="a1",
                parent_id=None,
                depth=0,
                model_name="m",
                task_description="test",
            )
            await reg.register(handle)
            await reg.cleanup(timeout=1.0)
            all_agents = await reg.list_all()
            assert len(all_agents) == 0

        asyncio.run(_test())

    def test_cancel_agent(self):
        async def _test():
            reg = AgentHypervisor()

            async def _dummy():
                await asyncio.sleep(100)

            task = asyncio.create_task(_dummy())
            handle = AgentHandle(
                agent_id="a1",
                parent_id=None,
                depth=0,
                model_name="m",
                task_description="test",
                asyncio_task=task,
            )
            await reg.register(handle)
            result = await reg.cancel_agent("a1")
            assert result is None  # None = success
            # Let the event loop process the cancellation.
            await asyncio.sleep(0)
            assert task.cancelled()

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Hypervisor active governance (Ref: research transfers)
# ---------------------------------------------------------------------------


class TestHypervisorBudget:
    """Ref: [AgentCgroup §4.2] Session-wide budget tracking with graduated enforcement."""

    def test_total_steps_tracks_across_agents(self):
        async def _test():
            reg = AgentHypervisor()
            for aid in ("a1", "a2"):
                h = AgentHandle(
                    agent_id=aid, parent_id=None, depth=0, model_name="m", task_description="t"
                )
                await reg.register(h)
            await reg.update_step("a1", "tool1")
            await reg.update_step("a1", "tool2")
            await reg.update_step("a2", "tool3")
            assert reg.total_steps == 3

        asyncio.run(_test())

    def test_budget_exhausted_unlimited(self):
        """Budget=0 means unlimited — never exhausted."""
        reg = AgentHypervisor(session_step_budget=0)
        assert reg.budget_exhausted() is False

    def test_budget_exhausted_at_limit(self):
        async def _test():
            reg = AgentHypervisor(session_step_budget=2)
            h = AgentHandle(
                agent_id="a1", parent_id=None, depth=0, model_name="m", task_description="t"
            )
            await reg.register(h)
            await reg.update_step("a1", "t1")
            assert reg.budget_exhausted() is False
            await reg.update_step("a1", "t2")
            assert reg.budget_exhausted() is True

        asyncio.run(_test())

    def test_budget_remaining(self):
        reg = AgentHypervisor(session_step_budget=10)
        assert reg.budget_remaining() == 10
        reg._total_steps = 7
        assert reg.budget_remaining() == 3

    def test_budget_remaining_unlimited(self):
        reg = AgentHypervisor(session_step_budget=0)
        assert reg.budget_remaining() == -1  # Unlimited sentinel


class TestHypervisorStallDetection:
    """Ref: [DeepMind-Delegation §4.4] Internal trigger: unresponsive delegatee."""

    def test_last_step_at_updated(self):
        async def _test():
            reg = AgentHypervisor()
            h = AgentHandle(
                agent_id="a1", parent_id=None, depth=0, model_name="m", task_description="t"
            )
            await reg.register(h)
            assert h.last_step_at is None
            await reg.update_step("a1", "tool")
            assert h.last_step_at is not None

        asyncio.run(_test())

    def test_stalled_agents_returns_stalled(self):
        async def _test():
            import time

            reg = AgentHypervisor()
            h = AgentHandle(
                agent_id="a1",
                parent_id=None,
                depth=0,
                model_name="m",
                task_description="t",
                status="running",
            )
            h.last_step_at = time.monotonic() - 200  # 200s ago
            await reg.register(h)
            stalled = await reg.stalled_agents(threshold=120.0)
            assert len(stalled) == 1
            assert stalled[0].agent_id == "a1"

        asyncio.run(_test())

    def test_stalled_agents_ignores_active(self):
        async def _test():
            import time

            reg = AgentHypervisor()
            h = AgentHandle(
                agent_id="a1",
                parent_id=None,
                depth=0,
                model_name="m",
                task_description="t",
                status="running",
            )
            h.last_step_at = time.monotonic()  # Just now
            await reg.register(h)
            stalled = await reg.stalled_agents(threshold=120.0)
            assert len(stalled) == 0

        asyncio.run(_test())


class TestHypervisorMessagePassing:
    """Ref: [DeepMind-Delegation §4.4] Bidirectional adaptive coordination."""

    def test_send_message_to_running_agent(self):
        async def _test():
            import queue

            reg = AgentHypervisor()
            q = queue.Queue()
            h = AgentHandle(
                agent_id="a1",
                parent_id=None,
                depth=0,
                model_name="m",
                task_description="t",
                status="running",
                message_queue=q,
            )
            await reg.register(h)
            result = await reg.send_message("a1", "wrap up now")
            assert result is None  # None = success
            assert q.get_nowait() == "wrap up now"

        asyncio.run(_test())

    def test_send_message_to_completed_agent_fails(self):
        async def _test():
            import queue

            reg = AgentHypervisor()
            q = queue.Queue()
            h = AgentHandle(
                agent_id="a1",
                parent_id=None,
                depth=0,
                model_name="m",
                task_description="t",
                status="completed",
                message_queue=q,
            )
            await reg.register(h)
            result = await reg.send_message("a1", "hello")
            assert result is not None  # str = failure reason
            assert q.empty()

        asyncio.run(_test())

    def test_send_message_to_unknown_agent_fails(self):
        async def _test():
            reg = AgentHypervisor()
            result = await reg.send_message("nonexistent", "hello")
            assert result is not None  # str = failure reason

        asyncio.run(_test())


class TestHypervisorGlobalEye:
    """Ref: [DeepMind-Delegation §4.5] Root agent's global eye via render_agent_tree."""

    def test_empty_tree(self):
        async def _test():
            reg = AgentHypervisor()
            tree = await reg.render_agent_tree()
            assert tree == ""

        asyncio.run(_test())

    def test_tree_shows_agents(self):
        async def _test():
            reg = AgentHypervisor()
            root = AgentHandle(
                agent_id="root1234",
                parent_id=None,
                depth=0,
                model_name="m",
                task_description="Main task",
                status="running",
            )
            child = AgentHandle(
                agent_id="child567",
                parent_id="root1234",
                depth=1,
                model_name="m",
                task_description="Sub task",
                status="completed",
            )
            child.steps_completed = 5
            await reg.register(root)
            await reg.register(child)
            tree = await reg.render_agent_tree()
            assert "root1234" in tree
            assert "child567" in tree
            assert "running" in tree
            assert "completed" in tree
            assert "5 steps" in tree

        asyncio.run(_test())

    def test_tree_shows_budget(self):
        async def _test():
            reg = AgentHypervisor(session_step_budget=100)
            h = AgentHandle(
                agent_id="a1",
                parent_id=None,
                depth=0,
                model_name="m",
                task_description="t",
                status="running",
            )
            await reg.register(h)
            await reg.update_step("a1", "tool")
            tree = await reg.render_agent_tree()
            assert "Budget:" in tree
            assert "1/100" in tree

        asyncio.run(_test())

    def test_tree_excludes_specified_agent(self):
        """Excluding the calling agent's own ID prevents self-steering."""

        async def _test():
            reg = AgentHypervisor()
            root = AgentHandle(
                agent_id="root1234",
                parent_id=None,
                depth=0,
                model_name="m",
                task_description="Root task",
                status="running",
            )
            child = AgentHandle(
                agent_id="child567",
                parent_id="root1234",
                depth=1,
                model_name="m",
                task_description="Child task",
                status="submitted",
            )
            await reg.register(root)
            await reg.register(child)

            # Exclude root — only child visible
            tree = await reg.render_agent_tree(exclude_agent_id="root1234")
            assert "root1234" not in tree
            assert "child567" in tree

            # Default (no exclusion) shows all
            tree_all = await reg.render_agent_tree()
            assert "root1234" in tree_all
            assert "child567" in tree_all

        asyncio.run(_test())

    def test_tree_exclude_only_agent_returns_empty(self):
        """If the only registered agent is excluded, return empty string."""

        async def _test():
            reg = AgentHypervisor()
            h = AgentHandle(
                agent_id="solo1234",
                parent_id=None,
                depth=0,
                model_name="m",
                task_description="t",
                status="running",
            )
            await reg.register(h)
            tree = await reg.render_agent_tree(exclude_agent_id="solo1234")
            assert tree == ""

        asyncio.run(_test())


class TestHypervisorCompaction:
    """Compaction tracking on AgentHandle and visibility in agent tree."""

    def test_record_compaction_increments_count(self):
        async def _test():
            reg = AgentHypervisor()
            h = AgentHandle(
                agent_id="agent123",
                parent_id=None,
                depth=0,
                model_name="m",
                task_description="t",
                status="running",
            )
            await reg.register(h)
            assert h.compaction_count == 0
            await reg.record_compaction("agent123")
            assert h.compaction_count == 1
            assert h.last_compacted_at is not None
            await reg.record_compaction("agent123")
            assert h.compaction_count == 2

        asyncio.run(_test())

    def test_tree_shows_compaction_marker(self):
        async def _test():
            reg = AgentHypervisor()
            h = AgentHandle(
                agent_id="compact1",
                parent_id=None,
                depth=0,
                model_name="m",
                task_description="task",
                status="running",
            )
            await reg.register(h)
            await reg.record_compaction("compact1")
            tree = await reg.render_agent_tree()
            assert "compacted x1" in tree

        asyncio.run(_test())

    def test_tree_hides_marker_when_no_compaction(self):
        async def _test():
            reg = AgentHypervisor()
            h = AgentHandle(
                agent_id="nocomp12",
                parent_id=None,
                depth=0,
                model_name="m",
                task_description="task",
                status="running",
            )
            await reg.register(h)
            tree = await reg.render_agent_tree()
            assert "compacted" not in tree

        asyncio.run(_test())


class TestAgentResultStructure:
    """Ref: [CoA §3.1] Communication Units enable structured inter-agent context."""

    def test_agent_result_fields(self):
        from mewbo_core.hypervisor import AgentResult

        result = AgentResult(
            content="Task completed",
            status="completed",
            steps_used=5,
            summary="Found the answer",
        )
        assert result.content == "Task completed"
        assert result.status == "completed"
        assert result.steps_used == 5
        assert result.summary == "Found the answer"
        assert result.warnings == []
        assert result.artifacts == []
        # Ref: Phase 1b — additive default, byte-identical to the
        # historical untyped-summary path when the caller never declares a kind.
        assert result.summary_kind == "generic"

    def test_agent_result_serialization(self):
        """AgentResult must be JSON-serializable for inter-agent passing."""
        import json
        from dataclasses import asdict

        from mewbo_core.hypervisor import AgentResult

        result = AgentResult(
            content="output",
            status="failed",
            steps_used=3,
            warnings=["timeout on tool X"],
            summary="partial work done",
        )
        serialized = json.dumps(asdict(result))
        deserialized = json.loads(serialized)
        assert deserialized["status"] == "failed"
        assert deserialized["warnings"] == ["timeout on tool X"]
        assert deserialized["summary"] == "partial work done"
        assert deserialized["summary_kind"] == "generic"

    def test_agent_result_summary_kind_explicit(self):
        """Ref: Phase 1b — task-typed CU shape (Ref: [CoA §3])."""
        import json
        from dataclasses import asdict

        from mewbo_core.hypervisor import AgentResult

        result = AgentResult(
            content="output",
            status="completed",
            steps_used=3,
            summary="fact A; fact B",
            summary_kind="evidence",
        )
        assert result.summary_kind == "evidence"
        deserialized = json.loads(json.dumps(asdict(result)))
        assert deserialized["summary_kind"] == "evidence"

    def test_cannot_solve_status(self):
        """Ref: [Aletheia §3] Explicit failure admission as first-class outcome."""
        from mewbo_core.hypervisor import AgentResult

        result = AgentResult(
            content="Cannot solve: depth exceeded",
            status="cannot_solve",
            steps_used=0,
        )
        assert result.status == "cannot_solve"


class TestAgentStatusExpansion:
    """Ref: [A2A v1.0] Expanded state machine with submitted/rejected states."""

    def test_submitted_status(self):
        h = AgentHandle(
            agent_id="a1", parent_id=None, depth=0, model_name="m", task_description="t"
        )
        assert h.status == "submitted"  # Default is now submitted, not running

    def test_rejected_status(self):
        async def _test():
            reg = AgentHypervisor()
            h = AgentHandle(
                agent_id="a1", parent_id=None, depth=0, model_name="m", task_description="t"
            )
            await reg.register(h)
            await reg.mark_done("a1", "rejected")
            got = await reg.get("a1")
            assert got is not None
            assert got.status == "rejected"

        asyncio.run(_test())


class TestDoneEvent:
    """Event-driven wait: done_event is set when agent reaches terminal state."""

    def test_mark_done_sets_done_event(self):
        hyper = AgentHypervisor(max_concurrent=10)
        handle = AgentHandle(
            agent_id="evt_test",
            parent_id=None,
            depth=0,
            model_name="test",
            task_description="test",
        )
        asyncio.run(hyper.register(handle))
        assert not handle.done_event.is_set()
        asyncio.run(hyper.mark_done("evt_test", "completed"))
        assert handle.done_event.is_set()

    def test_cancel_agent_sets_done_event(self):
        hyper = AgentHypervisor(max_concurrent=10)
        handle = AgentHandle(
            agent_id="cancel_evt",
            parent_id=None,
            depth=0,
            model_name="test",
            task_description="test",
        )

        async def _run():
            await hyper.register(handle)
            handle.asyncio_task = asyncio.create_task(asyncio.sleep(999))
            cancelled = await hyper.cancel_agent("cancel_evt")
            assert cancelled is None  # None = success
            assert handle.done_event.is_set()

        asyncio.run(_run())
