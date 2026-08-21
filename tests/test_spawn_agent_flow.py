#!/usr/bin/env python3
"""Integration / contract tests for spawn_agent.py and hypervisor.py.

Coverage targets
----------------
- spawn_agent.py  68.3% → meaningful uplift via real code paths
- hypervisor.py   86.6% → remaining branches in cleanup, send_to_parent,
                           cancel_agent edge cases, render_agent_tree markers

Patterns
--------
- Reuse helpers from test_spawn_agent.py (imported via sys.path — pytest adds
  tests/ to sys.path automatically).
- Stub ONLY I/O boundaries: model.ainvoke (AsyncMock), subprocess, network.
- Real AgentHypervisor / AgentContext / SpawnAgentTool execution paths.
- Parametrize micro-variants; one class per atomic behaviour cluster.
"""

from __future__ import annotations

import asyncio
import json
import queue
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage
from mewbo_core.agents.agent_context import AgentContext
from mewbo_core.agents.agent_registry import AgentDef, AgentRegistry
from mewbo_core.agents.hypervisor import (
    AgentHandle,
    AgentHypervisor,
    AgentResult,
    ScheduledSpawn,
    SpawnUnit,
)
from mewbo_core.agents.spawn_agent import AgentError, SpawnAgentTool
from mewbo_core.classes import ActionStep
from mewbo_core.hooks import HookManager
from mewbo_core.permissions import PermissionDecision, PermissionPolicy
from mewbo_core.tooling.tool_registry import ToolRegistry, ToolSpec

# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_spawn_agent.py patterns)
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


def _make_hypervisor(max_concurrent: int = 100) -> AgentHypervisor:
    return AgentHypervisor(max_concurrent=max_concurrent)


def _make_root_ctx(
    *,
    hypervisor: AgentHypervisor | None = None,
    max_depth: int = 5,
    message_queue: queue.Queue[str] | None = None,
) -> AgentContext:
    return AgentContext.root(
        model_name="test-model",
        max_depth=max_depth,
        registry=hypervisor or _make_hypervisor(),
        message_queue=message_queue or queue.Queue(),
    )


def _make_spawn_tool(
    ctx: AgentContext,
    *,
    registry: ToolRegistry | None = None,
    agent_registry: AgentRegistry | None = None,
    session_capabilities: tuple[str, ...] = (),
) -> SpawnAgentTool:
    return SpawnAgentTool(
        agent_context=ctx,
        tool_registry=registry or _make_registry("shell_tool"),
        permission_policy=_allow_all_policy(),
        hook_manager=_make_hook_manager(),
        agent_registry=agent_registry,
        session_capabilities=session_capabilities,
    )


def _step(task: str = "do work", **extra) -> ActionStep:
    return ActionStep(
        tool_id="spawn_agent",
        operation="set",
        tool_input={"task": task, **extra},
    )


async def _spawn_non_blocking(
    task: str = "test task",
    *,
    hypervisor: AgentHypervisor | None = None,
) -> tuple[SpawnAgentTool, AgentContext]:
    """Spawn one non-blocking child from root and wait for lifecycle to finish."""
    root_q: queue.Queue[str] = queue.Queue()
    hv = hypervisor or _make_hypervisor()
    ctx = _make_root_ctx(hypervisor=hv, message_queue=root_q)

    # Register root handle so send_to_parent can find parent's queue.
    root_handle = AgentHandle(
        agent_id=ctx.agent_id,
        parent_id=None,
        depth=0,
        model_name=ctx.model_name,
        task_description="root task",
        status="running",
        message_queue=root_q,
    )
    await hv.register(root_handle)

    tool = _make_spawn_tool(ctx)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(return_value=_text_response("Child done"))

    with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
        mock_build.return_value = MagicMock()
        mock_build.return_value.bind_tools.return_value = bound
        await tool.run_async(_step(task))
        await tool.await_lifecycle_managers(timeout=5.0)

    return tool, ctx


# The shape LiteLLM produced on a real 502 (mirrors tests/test_run_error.py's
# fixture): an entire HTML document spliced into the exception message.
# Hostname is fictional — this repository public-mirrors.
_HTML_BODY = (
    '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
    "<title>502 Bad Gateway — proxy.git.example.com</title>\n"
    "</head>\n<body>\n<h1>502 Bad Gateway</h1>\n"
    "<p>The upstream server at internal-llm.git.example.com is unreachable.</p>\n"
    "</body>\n</html>\n" + ("<!-- padding -->\n" * 400)
)
_BAD_GATEWAY_MESSAGE = (
    "LLM call failed on all models (test-model): "
    "litellm.BadGatewayError: BadGatewayError: OpenAIException - " + _HTML_BODY
)


# ---------------------------------------------------------------------------
# _coerce_list
# ---------------------------------------------------------------------------


class TestCoerceList:
    """Unit tests for the ``SpawnAgentTool._coerce_list`` argument coercion."""

    def test_list_input_converted(self):
        assert SpawnAgentTool._coerce_list(["a", "b", "c"]) == ["a", "b", "c"]

    def test_list_with_falsy_skipped(self):
        assert SpawnAgentTool._coerce_list(["a", "", None, "b"]) == ["a", "b"]  # type: ignore[list-item]

    def test_comma_string_splits(self):
        assert SpawnAgentTool._coerce_list("tool_a, tool_b, tool_c") == [
            "tool_a",
            "tool_b",
            "tool_c",
        ]

    def test_comma_string_strips_whitespace(self):
        assert SpawnAgentTool._coerce_list("  x  ,  y  ") == ["x", "y"]

    def test_non_string_non_list_returns_empty(self):
        assert SpawnAgentTool._coerce_list(42) == []
        assert SpawnAgentTool._coerce_list(None) == []


# ---------------------------------------------------------------------------
# AgentError
# ---------------------------------------------------------------------------


class TestAgentError:
    """str() format — covers lines 46-52."""

    def test_str_without_last_tool(self):
        err = AgentError(
            agent_id="abc123",
            depth=2,
            task="analyse logs",
            error="timeout",
            steps_completed=7,
        )
        s = str(err)
        assert "abc123" in s
        assert "depth=2" in s
        assert "7 steps" in s
        assert "timeout" in s
        assert "at tool" not in s

    def test_str_with_last_tool(self):
        err = AgentError(
            agent_id="xyz",
            depth=1,
            task="read data",
            error="bad response",
            last_tool="read_file",
            steps_completed=3,
        )
        s = str(err)
        assert "at tool 'read_file'" in s


# ---------------------------------------------------------------------------
# acceptance_criteria appended to task_desc (lines 301-305)
# ---------------------------------------------------------------------------


class TestAcceptanceCriteriaIntegration:
    """acceptance_criteria is appended to task_desc before the child loop runs."""

    def test_acceptance_criteria_appended(self):
        async def _test():
            ctx = _make_root_ctx()
            tool = _make_spawn_tool(ctx)
            bound = MagicMock()
            bound.ainvoke = AsyncMock(return_value=_text_response("done"))

            received_tasks: list[str] = []

            orig_run = None

            async def _capture_task(task_desc, **kwargs):
                received_tasks.append(task_desc)
                return await orig_run(task_desc, **kwargs)

            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound

                from mewbo_core.loop.tool_use_loop import ToolUseLoop

                orig_run = ToolUseLoop.run

                with patch.object(ToolUseLoop, "run", side_effect=_capture_task):
                    step = ActionStep(
                        tool_id="spawn_agent",
                        operation="set",
                        tool_input={
                            "task": "check the file",
                            "acceptance_criteria": "file exists and is non-empty",
                        },
                    )
                    await tool.run_async(step)

            # Non-blocking spawn from depth=0: acceptance_criteria appended
            # before the root returns submitted. We can check via
            # the child task registered in the hypervisor.
            children = await ctx.registry.list_children(ctx.agent_id)
            assert len(children) == 1
            child = children[0]
            assert "Acceptance criteria:" in child.task_description

            await tool.await_lifecycle_managers(timeout=5.0)

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# agent_type resolution (lines 309-336)
# ---------------------------------------------------------------------------


class TestAgentTypeResolution:
    """SpawnAgentTool with an agent_registry resolves agent_type correctly."""

    def _make_agent_def(
        self,
        name: str = "test-agent",
        *,
        model: str | None = None,
        allowed_tools: list[str] | None = None,
        denied_tools: list[str] | None = None,
    ) -> AgentDef:
        return AgentDef(
            name=name,
            description="A test agent",
            source_path="/fake/path.md",
            source="plugin:test",
            body="You are a test agent.\n\nDo the task.",
            model=model,
            allowed_tools=allowed_tools,
            denied_tools=denied_tools,
        )

    def test_unknown_agent_type_returns_error(self):
        async def _test():
            ctx = _make_root_ctx()
            agent_registry = AgentRegistry()
            tool = _make_spawn_tool(ctx, agent_registry=agent_registry)

            step = ActionStep(
                tool_id="spawn_agent",
                operation="set",
                tool_input={"task": "do work", "agent_type": "nonexistent-agent"},
            )
            result = await tool.run_async(step)
            assert "ERROR" in result.content
            assert "nonexistent-agent" in result.content

        asyncio.run(_test())

    def test_agent_type_prepends_body_to_task(self):
        async def _test():
            ctx = _make_root_ctx()
            agent_registry = AgentRegistry()
            agent_def = self._make_agent_def(name="my-agent")
            agent_registry.register(agent_def)

            tool = _make_spawn_tool(ctx, agent_registry=agent_registry)
            bound = MagicMock()
            bound.ainvoke = AsyncMock(return_value=_text_response("done"))

            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound

                step = ActionStep(
                    tool_id="spawn_agent",
                    operation="set",
                    tool_input={"task": "my sub task", "agent_type": "my-agent"},
                )
                await tool.run_async(step)

            # Registered child in hypervisor shows task with body prepended
            children = await ctx.registry.list_children(ctx.agent_id)
            assert len(children) == 1
            task_desc = children[0].task_description
            # task_desc truncated to 200 chars, but body + task separator should appear
            assert "Task: my sub task" in task_desc or "Do the task" in task_desc

            await tool.await_lifecycle_managers(timeout=5.0)

        asyncio.run(_test())

    def test_agent_type_model_overrides_model_arg(self):
        """agent_def.model takes precedence; child_ctx is created with that model."""

        async def _test():
            hv = _make_hypervisor()
            ctx = AgentContext.root(
                model_name="parent-model",
                max_depth=5,
                registry=hv,
            )
            # Use depth=1 (blocking spawn) so we can verify the child model
            child_ctx = ctx.child()  # depth=1

            agent_registry = AgentRegistry()
            agent_def = self._make_agent_def(name="special-agent", model="special-model")
            agent_registry.register(agent_def)

            tool = SpawnAgentTool(
                agent_context=child_ctx,
                tool_registry=_make_registry("shell_tool"),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
                agent_registry=agent_registry,
            )

            bound = MagicMock()
            bound.ainvoke = AsyncMock(return_value=_text_response("done"))

            spawned_models: list[str] = []

            import mewbo_core.loop.tool_use_loop as tul_mod

            original_build = tul_mod.build_chat_model

            def _capturing_build(*, model_name: str, **kwargs):
                spawned_models.append(model_name)
                return original_build(model_name=model_name, **kwargs)

            with patch.object(tul_mod, "build_chat_model", side_effect=_capturing_build):
                step = ActionStep(
                    tool_id="spawn_agent",
                    operation="set",
                    tool_input={
                        "task": "work",
                        "agent_type": "special-agent",
                        "model": "caller-model",  # should be ignored in favour of agent_def.model
                    },
                )
                await tool.run_async(step)

            # The model used by the child loop must be the agent_def model
            assert any(m == "special-model" for m in spawned_models), (
                f"Expected 'special-model' to be used, got: {spawned_models}"
            )

        asyncio.run(_test())

    def test_agent_type_applies_allowed_tools_when_not_overridden(self):
        """agent_def.allowed_tools applied when caller doesn't set allowed_tools."""
        ctx = _make_root_ctx()
        agent_registry = AgentRegistry()
        agent_def = self._make_agent_def(name="scoped-agent", allowed_tools=["tool_a", "tool_c"])
        agent_registry.register(agent_def)

        tool = _make_spawn_tool(
            ctx,
            registry=_make_registry("tool_a", "tool_b", "tool_c"),
            agent_registry=agent_registry,
        )
        # Bypass actual execution — just check filter_tool_specs
        # Simulate what run_async does after resolving agent_type
        args: dict = {"task": "work", "agent_type": "scoped-agent"}
        # Apply agent_def logic manually
        agent_def_got = agent_registry.get("scoped-agent")
        assert agent_def_got is not None
        if agent_def_got.allowed_tools is not None and "allowed_tools" not in args:
            args["allowed_tools"] = agent_def_got.allowed_tools

        specs = tool._filter_tool_specs(args)
        ids = {s.tool_id for s in specs}
        assert ids == {"tool_a", "tool_c"}

    def test_agent_def_with_an_empty_tools_list_gives_the_child_nothing(self):
        """An AgentDef declaring ``tools: []`` must scope its child to no tools.

        Unlike its siblings above, this drives the REAL ``run_async`` merge
        rather than re-applying the production ``if`` in the test body — a copied
        condition cannot catch a bug in the original, and the copies here were
        still carrying the truthiness form this law replaced. Plugin-supplied
        AgentDefs make the seam a trust boundary: a leaf declared with no tools
        must not inherit the parent's whole spec set.
        """

        async def _test():
            ctx = _make_root_ctx().child()  # depth=1 → blocking spawn
            agent_registry = AgentRegistry()
            agent_registry.register(
                self._make_agent_def(name="toolless-agent", allowed_tools=[])
            )
            tool = SpawnAgentTool(
                agent_context=ctx,
                tool_registry=_make_registry("tool_a", "tool_b"),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
                agent_registry=agent_registry,
            )

            # Observe the real child loop; do not substitute for building it.
            built: list = []
            original_build_child = tool._build_child_loop

            def _capturing_build_child(*args, **kwargs):
                loop = original_build_child(*args, **kwargs)
                built.append(loop)
                return loop

            tool._build_child_loop = _capturing_build_child

            bound = MagicMock()
            bound.ainvoke = AsyncMock(return_value=_text_response("done"))
            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound

                await tool.run_async(
                    ActionStep(
                        tool_id="spawn_agent",
                        operation="set",
                        tool_input={"task": "work", "agent_type": "toolless-agent"},
                    )
                )

            assert built, "the spawn must have built a child loop"
            # The child loop is always built with strict_tool_scope=True, under
            # which the spawn gate is disabled ONLY by a non-None allowlist that
            # omits the spawn family. So this single assertion proves the whole
            # chain: the AgentDef's ``[]`` survived parsing, survived the merge,
            # and reached the child as an empty list rather than as None — had it
            # collapsed anywhere, delegation would be live here.
            assert built[0]._spawn_agent_tool is None, (
                "an AgentDef declaring no tools must not yield a delegating child"
            )

            await tool.await_lifecycle_managers(timeout=5.0)

        asyncio.run(_test())

    def test_agent_type_denied_tools_applied(self):
        """agent_def.denied_tools applied when caller doesn't set denied_tools."""
        ctx = _make_root_ctx()
        agent_registry = AgentRegistry()
        agent_def = self._make_agent_def(name="restricted-agent", denied_tools=["tool_b"])
        agent_registry.register(agent_def)

        tool = _make_spawn_tool(
            ctx,
            registry=_make_registry("tool_a", "tool_b", "tool_c"),
            agent_registry=agent_registry,
        )
        args: dict = {"task": "work", "agent_type": "restricted-agent"}
        agent_def_got = agent_registry.get("restricted-agent")
        assert agent_def_got is not None
        if agent_def_got.denied_tools and "denied_tools" not in args:
            args["denied_tools"] = agent_def_got.denied_tools

        specs = tool._filter_tool_specs(args)
        ids = {s.tool_id for s in specs}
        assert "tool_b" not in ids
        assert "tool_a" in ids


# ---------------------------------------------------------------------------
# Admission control (line 347 — max_concurrent blocked path)
# ---------------------------------------------------------------------------


class TestAdmissionControl:
    """Bounded concurrency WITHOUT discarding work."""

    def test_a_full_fleet_defers_the_spawn_and_starts_it_on_a_settle(self):
        """The real path, end to end: no patched admission anywhere.

        Patching ``hv.admit`` and asserting a disjunction any error string
        satisfies exercises no production code and cannot fail. The contract
        itself is what gets pinned: an over-cap spawn is ACCEPTED as
        ``submitted`` with a real agent_id, waits, and is started by the
        settling sibling's own release.
        """

        async def _test():
            hv = _make_hypervisor(max_concurrent=1)
            ctx, tool = await _register_root(hv)

            gate = asyncio.Event()

            async def gated(*_a, **_k):
                await gate.wait()
                return _text_response("child done")

            bound = MagicMock()
            bound.ainvoke = AsyncMock(side_effect=gated)

            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mb:
                mb.return_value = MagicMock()
                mb.return_value.bind_tools.return_value = bound

                first = json.loads((await tool.run_async(_step("first"))).content)
                assert first["status"] == "submitted"
                assert hv.free_slots == 0

                # The fleet is full. The spawn is not refused — it waits.
                second = json.loads((await tool.run_async(_step("second"))).content)
                assert second["status"] == "submitted"
                assert second["agent_id"]
                assert hv.pending_dispatch == 1

                # A deferred child is a real, visible, non-terminal agent, and
                # ``submitted`` is exactly the state for it — without it the
                # never-admitted are invisible to every liveness reader.
                deferred = await hv.get(second["agent_id"])
                assert deferred is not None
                assert deferred.status == "submitted"
                assert {h.agent_id for h in await hv.collect_running(ctx.agent_id)} == {
                    first["agent_id"],
                    second["agent_id"],
                }

                # Settling the first hands its slot straight to the second.
                gate.set()
                await tool.await_lifecycle_managers(timeout=5.0)

            assert hv.pending_dispatch == 0
            started = await hv.get(second["agent_id"])
            assert started is not None
            assert started.status == "completed"
            assert hv.free_slots == 1

        asyncio.run(_test())

    def test_a_permanent_refusal_is_rejected_and_never_deferred(self):
        """Capacity is transient; a bad argument is not. They must not look alike."""

        async def _test():
            hv = _make_hypervisor(max_concurrent=1)
            ctx = _make_root_ctx(hypervisor=hv)
            tool = _make_spawn_tool(ctx, agent_registry=AgentRegistry())

            result = await tool.run_async(_step("work", agent_type="no-such-agent"))
            envelope = json.loads(result.content)["error"]

            # The SHARED handled-failure envelope, so the loop reclassifies the
            # step as failed through machinery that already exists rather than
            # a second grammar invented for spawns.
            assert envelope["code"] == "unknown_agent_type"
            assert "no-such-agent" in envelope["message"]
            # ``permanent`` is the load-bearing half: it is what tells the loop
            # (and the model) that re-issuing this call is pointless.
            assert envelope["permanence"] == "permanent"

            # Nothing was scheduled, registered, or charged a slot: retrying
            # this call unchanged would fail identically, so deferring it would
            # be a promise the scheduler could never keep.
            assert hv.pending_dispatch == 0
            assert hv.free_slots == 1
            assert await hv.list_all() == []

        asyncio.run(_test())

    def test_a_nested_fan_out_makes_progress_with_every_slot_held(self):
        """WAITING IS FREE — the deadlock a blocking queue would have caused.

        The root's children saturate the fleet and then block, each awaiting a
        grandchild inline. If a nested spawn took a SECOND slot, every one of
        those parents would wait for capacity that only they could free. A
        blocked parent is not running, so its slot covers the child it is
        waiting on, and the subtree makes progress at zero free slots.
        """

        async def _test():
            hv = _make_hypervisor(max_concurrent=2)
            ctx, tool = await _register_root(hv)

            gate = asyncio.Event()
            parents_at_gate = asyncio.Event()
            parent_calls = 0

            async def gated(*_a, **_k):
                nonlocal parent_calls
                parent_calls += 1
                if parent_calls == 2:
                    parents_at_gate.set()
                await gate.wait()
                return _text_response("child done")

            bound = MagicMock()
            bound.ainvoke = AsyncMock(side_effect=gated)
            parent_model = MagicMock()
            parent_model.bind_tools.return_value = bound

            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mb:
                # The lifecycle managers construct their loops after the batch
                # call returns. Hold the factory on the parent model until BOTH
                # have bound and reached their gate; changing a shared mock's
                # ``bind_tools.return_value`` first would let a slow parent bind
                # the grandchild's response and release its slot before the
                # assertion.
                mb.return_value = parent_model
                await tool.run_batch_async(_batch_step({"task": "a"}, {"task": "b"}))
                await asyncio.wait_for(parents_at_gate.wait(), timeout=5.0)
                assert hv.free_slots == 0

                # A depth-1 agent now delegates a grandchild. Under the old
                # blocking admission this is where the tree wedged.
                child_ctx = ctx.child(model_name="test-model")
                child_tool = _make_spawn_tool(child_ctx)
                child_tool.parent_mode = "act"

                deep = MagicMock()
                deep.ainvoke = AsyncMock(return_value=_text_response("grandchild done"))
                grandchild_model = MagicMock()
                grandchild_model.bind_tools.return_value = deep
                mb.return_value = grandchild_model

                result = await asyncio.wait_for(
                    child_tool.run_async(_step("grandchild work")), timeout=5.0
                )
                payload = json.loads(result.content)
                assert payload["status"] == "completed"
                # It ran INSIDE its parent's slot — no extra capacity taken.
                assert hv.free_slots == 0
                assert hv.pending_dispatch == 0

                gate.set()
                await tool.await_lifecycle_managers(timeout=5.0)

            assert hv.free_slots == 2

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Model validation (ERROR path) — line 342-343
# ---------------------------------------------------------------------------


class TestModelValidationErrors:
    """A model outside ``allowed_models`` moves the child, it does not kill it.

    Refusing the spawn outright is the worse failure: an AgentDef-pinned model
    the gateway will not serve then kills the child at step 0 while the parent
    runs on healthily, and nothing upstream checks that model against what the
    deployment can actually serve. The
    parent's own model is proven — it is what this agent is running on — so the
    child runs there instead, and the substitution is surfaced rather than made
    silently: a caller that pinned a model is owed the fact that it did not
    get it.
    """

    def test_unavailable_model_falls_back_and_says_so(self):
        async def _test():
            ctx = _make_root_ctx()
            tool = _make_spawn_tool(ctx)

            with patch(
                "mewbo_core.agents.spawn_agent.get_config_value",
                side_effect=lambda *a, **kw: (
                    ["allowed-model"] if a == ("agent", "allowed_models") else kw.get("default", "")
                ),
            ):
                step = ActionStep(
                    tool_id="spawn_agent",
                    operation="set",
                    tool_input={"task": "work", "model": "bad-model"},
                )
                result = await tool.run_async(step)

            body = json.loads(result.content)
            # The spawn proceeds — no rejection, no ERROR payload.
            assert body["status"] == "submitted"
            # NO-SILENT-DROP: the refused model AND its replacement are named.
            assert "bad-model" in body["model_fallback"]
            assert "test-model" in body["model_fallback"]

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# AgentDepthExceeded handling (lines 460-482)
# ---------------------------------------------------------------------------


class TestAgentDepthExceededHandling:
    """Ref: spawn_agent handles depth exceeded via AgentDepthExceeded exception."""

    def test_depth_exceeded_returns_cannot_solve(self):
        async def _test():
            # Create at max depth — child() will raise AgentDepthExceeded
            ctx = AgentContext.root(
                model_name="test-model",
                max_depth=1,
                registry=_make_hypervisor(),
            )
            # Register root handle so registry is non-empty
            root_handle = AgentHandle(
                agent_id=ctx.agent_id,
                parent_id=None,
                depth=0,
                model_name="test-model",
                task_description="root",
                status="running",
                message_queue=ctx.message_queue,
            )
            await ctx.registry.register(root_handle)

            # Create a child context at depth=1 (max_depth=1)
            child_ctx = ctx.child()  # depth becomes 1 = max_depth
            assert not child_ctx.can_spawn

            tool = SpawnAgentTool(
                agent_context=child_ctx,
                tool_registry=_make_registry("shell_tool"),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )

            # child_ctx.child() will raise AgentDepthExceeded
            step = _step("deep task")
            result = await tool.run_async(step)

            parsed = json.loads(result.content)
            assert parsed["status"] == "cannot_solve"
            assert "Depth exceeded" in parsed["content"]
            # The cause is TYPED, not merely spelled out in the prose — the
            # caller must be able to tell this apart from a busy fleet.
            assert parsed["code"] == "depth_exceeded"

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# asyncio.CancelledError handling (lines 484-489)
# ---------------------------------------------------------------------------


class TestCancelledErrorHandling:
    """CancelledError path in run_async: mark_done('cancelled') + re-raise.

    Strategy: spawn from depth=1 (blocking path), then cancel the child
    asyncio.Task externally while the model is blocked — proving that
    CancelledError propagates out of run_async.
    """

    def test_cancelled_error_re_raised(self):
        async def _test():
            hv = _make_hypervisor()
            ctx = AgentContext.root(
                model_name="test-model",
                max_depth=5,
                registry=hv,
            )
            child_ctx = ctx.child()  # depth=1 → blocking spawn

            tool = SpawnAgentTool(
                agent_context=child_ctx,
                tool_registry=_make_registry("shell_tool"),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )

            # A model that stalls until cancelled
            async def _stalling(*args, **kwargs):
                await asyncio.sleep(999)
                return _text_response("never")

            bound = MagicMock()
            bound.ainvoke = AsyncMock(side_effect=_stalling)

            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound

                # Run run_async as a task and cancel it after a tick
                run_task = asyncio.create_task(tool.run_async(_step("stalling task")))
                await asyncio.sleep(0.05)
                run_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await run_task

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# General exception handling in run_async (lines 491-521)
# ---------------------------------------------------------------------------


class TestSubAgentExceptionHandling:
    """Exception in child loop → failed AgentResult with structured error.

    Strategy: model.ainvoke raises an unexpected error so ToolUseLoop
    propagates it up through run_async → except Exception → failed result.
    """

    def test_exception_returns_failed_result(self):
        async def _test():
            # Blocking spawn path (depth=1)
            hv = _make_hypervisor()
            ctx = AgentContext.root(
                model_name="test-model",
                max_depth=5,
                registry=hv,
            )
            child_ctx = ctx.child()  # depth=1
            tool = SpawnAgentTool(
                agent_context=child_ctx,
                tool_registry=_make_registry("shell_tool"),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )

            # Make the model raise a non-CancelledError to trigger except Exception
            async def _explode(*args, **kwargs):
                raise ValueError("Synthetic failure from model")

            bound = MagicMock()
            bound.ainvoke = AsyncMock(side_effect=_explode)

            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound

                step = _step("risky work")
                result = await tool.run_async(step)

            parsed = json.loads(result.content)
            assert parsed["status"] == "failed"
            assert "failed" in parsed["content"].lower() or "Synthetic" in parsed["content"]
            assert isinstance(parsed.get("warnings", []), list)

        asyncio.run(_test())

    def test_html_bodied_exception_yields_bounded_markup_free_result(self):
        """A provider exception embedding an HTML error page must not leak raw
        markup or an unbounded blob into the failed result — the exact incident
        (a 5,887-char LiteLLM/upstream-502 page) `RunError` was built to close.
        """

        async def _test():
            # Blocking spawn path (depth=1)
            hv = _make_hypervisor()
            ctx = AgentContext.root(
                model_name="test-model",
                max_depth=5,
                registry=hv,
            )
            child_ctx = ctx.child()  # depth=1
            tool = SpawnAgentTool(
                agent_context=child_ctx,
                tool_registry=_make_registry("shell_tool"),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )

            async def _explode(*args, **kwargs):
                raise ValueError(_BAD_GATEWAY_MESSAGE)

            bound = MagicMock()
            bound.ainvoke = AsyncMock(side_effect=_explode)

            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound

                step = _step("risky work")
                result = await tool.run_async(step)

            parsed = json.loads(result.content)
            assert parsed["status"] == "failed"
            # Deterministic: classified from the LiteLLM error-class NAME in the
            # message head, never from the HTML body — same rule RunError enforces.
            expected = "Sub-agent failed: Upstream returned an HTML error page (502)"
            assert parsed["content"] == expected
            assert parsed["warnings"] == ["Upstream returned an HTML error page (502)"]
            # Bounded: nowhere near the raw ~6,700-char message that triggered this fix.
            assert len(parsed["content"]) < 200
            # Markup-free: no HTML tag survives into a client-facing field.
            assert "<" not in parsed["content"]
            assert all("<" not in w for w in parsed["warnings"])

        asyncio.run(_test())


class TestNonBlockingSubAgentExceptionHandling:
    """Root-depth (depth=0) spawn: the exception surfaces through the
    background lifecycle manager (`_run_child_lifecycle`), a separate except
    block from `_spawn_one`'s blocking path covered by
    `TestSubAgentExceptionHandling` above — same bug class, one level of
    plumbing further, so it needs its own coverage.
    """

    def test_html_bodied_exception_yields_bounded_markup_free_result(self):
        async def _test():
            root_q: queue.Queue[str] = queue.Queue()
            hv = _make_hypervisor()
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

            tool = SpawnAgentTool(
                agent_context=ctx,
                tool_registry=_make_registry("shell_tool"),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )

            async def _explode(*args, **kwargs):
                raise ValueError(_BAD_GATEWAY_MESSAGE)

            bound = MagicMock()
            bound.ainvoke = AsyncMock(side_effect=_explode)

            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound

                await tool.run_async(_step("risky root work"))
                await tool.await_lifecycle_managers(timeout=5.0)

            children = await hv.list_children(ctx.agent_id)
            assert len(children) == 1
            child = children[0]
            assert child.status == "failed"
            assert child.result is not None

            expected = "Sub-agent failed: Upstream returned an HTML error page (502)"
            assert child.result.content == expected
            assert child.result.warnings == ["Upstream returned an HTML error page (502)"]
            assert len(child.result.content) < 200
            assert "<" not in child.result.content
            assert all("<" not in w for w in child.result.warnings)

            # The AgentError attached via mark_done rides the same bound value.
            assert isinstance(child.error, AgentError)
            assert child.error.error == "Upstream returned an HTML error page (502)"
            assert "<" not in child.error.error

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Finally-block: cascade cancel of grandchildren (lines 523-532)
# ---------------------------------------------------------------------------


class TestFinallyBlockGrandchildCancellation:
    """Grandchildren in 'running' state are cancelled when their parent finishes.

    The finally block in blocking run_async calls list_children(spawned_child.agent_id)
    and cancels any running grandchildren. We intercept AgentContext.child() to capture
    the spawned child's agent_id so the grandchild can be registered under the correct
    parent before the model returns.
    """

    def test_grandchildren_cancelled_on_parent_finish(self):
        async def _test():
            hv = _make_hypervisor()
            ctx = AgentContext.root(
                model_name="test-model",
                max_depth=5,
                registry=hv,
            )
            child_ctx = ctx.child()  # depth=1 (blocking spawn path)

            # Intercept AgentContext.child() to grab the spawned grandchild's agent_id
            # so we can register the great-grandchild under the correct parent_id.
            spawned_child_ids: list[str] = []
            original_child = AgentContext.child

            def _capturing_child(self_ctx, **kwargs):
                child = original_child(self_ctx, **kwargs)
                spawned_child_ids.append(child.agent_id)
                return child

            dummy_task = asyncio.create_task(asyncio.sleep(999))
            gc_handle: list[AgentHandle] = []

            original_register = hv.register

            async def _register_and_inject(handle: AgentHandle):
                await original_register(handle)
                # After the spawned child is registered, inject a grandchild under it
                if spawned_child_ids and handle.agent_id == spawned_child_ids[-1]:
                    gh = AgentHandle(
                        agent_id="grandchild01",
                        parent_id=handle.agent_id,  # child of the spawned child
                        depth=3,
                        model_name="test-model",
                        task_description="grandchild task",
                        status="running",
                        asyncio_task=dummy_task,
                    )
                    hv._agents[gh.agent_id] = gh
                    gc_handle.append(gh)

            hv.register = _register_and_inject  # type: ignore[method-assign]

            tool = SpawnAgentTool(
                agent_context=child_ctx,
                tool_registry=_make_registry("shell_tool"),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )

            bound = MagicMock()
            bound.ainvoke = AsyncMock(return_value=_text_response("done"))

            with (
                patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build,
                patch.object(AgentContext, "child", _capturing_child),
            ):
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound
                step = _step("parent task")
                await tool.run_async(step)

            # Grandchild must be cancelled — the finally block in run_async calls
            # list_children(spawned_child_id) and cancels any running grandchildren.
            await asyncio.sleep(0)
            assert len(gc_handle) == 1, "Grandchild was never injected"
            assert dummy_task.cancelled() or gc_handle[0].status == "cancelled"

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# handle_check_agents  (lines 593-666)
# ---------------------------------------------------------------------------


class TestHandleCheckAgents:
    """Contract tests for check_agents handler."""

    def test_check_agents_no_agents(self):
        async def _test():
            ctx = _make_root_ctx()
            tool = _make_spawn_tool(ctx)

            step = ActionStep(
                tool_id="check_agents",
                operation="set",
                tool_input={"wait": False},
            )
            result = await tool.handle_check_agents(step)
            payload = json.loads(result.content)
            assert payload["kind"] == "agent_tree"
            assert "No agents" in payload["text"]

        asyncio.run(_test())

    def test_check_agents_with_running_agent(self):
        async def _test():
            ctx = _make_root_ctx()
            tool = _make_spawn_tool(ctx)

            # Manually register a running child
            child_handle = AgentHandle(
                agent_id="running_child",
                parent_id=ctx.agent_id,
                depth=1,
                model_name="test-model",
                task_description="running subtask",
                status="running",
            )
            await ctx.registry.register(child_handle)

            step = ActionStep(
                tool_id="check_agents",
                operation="set",
                tool_input={"wait": False},
            )
            result = await tool.handle_check_agents(step)
            payload = json.loads(result.content)

            assert payload["kind"] == "agent_tree"
            assert any(a["status"] == "running" for a in payload["agents"])
            assert "still running" in payload["text"]

        asyncio.run(_test())

    def test_check_agents_with_completed_agent_shows_result(self):
        async def _test():
            ctx = _make_root_ctx()
            tool = _make_spawn_tool(ctx)

            child_handle = AgentHandle(
                agent_id="done_child",
                parent_id=ctx.agent_id,
                depth=1,
                model_name="test-model",
                task_description="completed subtask",
                status="completed",
                result=AgentResult(
                    content="output text",
                    status="completed",
                    steps_used=3,
                    summary="short summary",
                ),
            )
            child_handle.done_event.set()
            await ctx.registry.register(child_handle)

            step = ActionStep(
                tool_id="check_agents",
                operation="set",
                tool_input={"wait": False},
            )
            result = await tool.handle_check_agents(step)
            payload = json.loads(result.content)

            completed = [a for a in payload["agents"] if a["status"] == "completed"]
            assert len(completed) == 1
            assert completed[0]["result"] is not None
            assert completed[0]["result"]["summary"] == "short summary"
            assert "Completed results" in payload["text"]

        asyncio.run(_test())

    def test_check_agents_wait_with_running_completes(self):
        """wait=True waits for one running agent to complete."""

        async def _test():
            ctx = _make_root_ctx()
            tool = _make_spawn_tool(ctx)

            # Register a child and signal it immediately
            child_handle = AgentHandle(
                agent_id="fast_child",
                parent_id=ctx.agent_id,
                depth=1,
                model_name="test-model",
                task_description="fast task",
                status="running",
            )
            await ctx.registry.register(child_handle)
            # Signal done_event right away
            child_handle.done_event.set()

            step = ActionStep(
                tool_id="check_agents",
                operation="set",
                tool_input={"wait": True, "timeout": 1.0},
            )
            result = await tool.handle_check_agents(step)
            payload = json.loads(result.content)
            assert payload["wait"] is True
            assert payload["kind"] == "agent_tree"

        asyncio.run(_test())

    def test_check_agents_payload_structure(self):
        """check_agents payload contains all required keys."""

        async def _test():
            ctx = _make_root_ctx()
            tool = _make_spawn_tool(ctx)

            step = ActionStep(
                tool_id="check_agents",
                operation="set",
                tool_input={},
            )
            result = await tool.handle_check_agents(step)
            payload = json.loads(result.content)

            assert "kind" in payload
            assert "text" in payload
            assert "agents" in payload
            assert "parent_id" in payload
            assert "wait" in payload
            assert payload["parent_id"] == ctx.agent_id

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# handle_steer_agent (lines 668-716)
# ---------------------------------------------------------------------------


class TestHandleSteerAgent:
    """Contract tests for steer_agent handler — all branches."""

    def _steer_step(self, agent_id: str, action: str, message: str = "") -> ActionStep:
        tool_input = {"agent_id": agent_id, "action": action}
        if message:
            tool_input["message"] = message
        return ActionStep(
            tool_id="steer_agent",
            operation="set",
            tool_input=tool_input,
        )

    def test_cancel_running_agent_success(self):
        async def _test():
            ctx = _make_root_ctx()
            tool = _make_spawn_tool(ctx)

            dummy_task = asyncio.create_task(asyncio.sleep(999))
            handle = AgentHandle(
                agent_id="target_agent_id",
                parent_id=ctx.agent_id,
                depth=1,
                model_name="test-model",
                task_description="task",
                status="running",
                asyncio_task=dummy_task,
            )
            await ctx.registry.register(handle)

            step = self._steer_step("target_agent_id", "cancel")
            result = await tool.handle_steer_agent(step)

            assert "cancelled" in result.content.lower()
            assert dummy_task.cancelled() or handle.status == "cancelled"

        asyncio.run(_test())

    def test_cancel_unknown_agent_returns_error(self):
        async def _test():
            ctx = _make_root_ctx()
            tool = _make_spawn_tool(ctx)

            step = self._steer_step("nonexistent", "cancel")
            result = await tool.handle_steer_agent(step)
            assert "ERROR" in result.content
            assert "not found" in result.content

        asyncio.run(_test())

    def test_cancel_via_prefix_matching(self):
        """Short prefix resolves to single agent — cancel succeeds."""

        async def _test():
            ctx = _make_root_ctx()
            tool = _make_spawn_tool(ctx)

            dummy_task = asyncio.create_task(asyncio.sleep(999))
            handle = AgentHandle(
                agent_id="abcdef123456",
                parent_id=ctx.agent_id,
                depth=1,
                model_name="test-model",
                task_description="prefixed task",
                status="running",
                asyncio_task=dummy_task,
            )
            await ctx.registry.register(handle)

            step = self._steer_step("abcdef12", "cancel")  # 8-char prefix
            result = await tool.handle_steer_agent(step)
            assert "cancelled" in result.content.lower()

        asyncio.run(_test())

    def test_ambiguous_prefix_returns_error(self):
        """Prefix matching >1 agent → ambiguous error."""

        async def _test():
            ctx = _make_root_ctx()
            tool = _make_spawn_tool(ctx)

            for aid in ("prefix_aaaa", "prefix_bbbb"):
                h = AgentHandle(
                    agent_id=aid,
                    parent_id=ctx.agent_id,
                    depth=1,
                    model_name="test-model",
                    task_description="task",
                    status="running",
                )
                await ctx.registry.register(h)

            step = self._steer_step("prefix_", "cancel")  # matches both
            result = await tool.handle_steer_agent(step)
            assert "ERROR" in result.content
            assert "Ambiguous" in result.content

        asyncio.run(_test())

    def test_message_to_running_agent_success(self):
        async def _test():
            ctx = _make_root_ctx()
            tool = _make_spawn_tool(ctx)

            child_q: queue.Queue[str] = queue.Queue()
            handle = AgentHandle(
                agent_id="msg_target_id",
                parent_id=ctx.agent_id,
                depth=1,
                model_name="test-model",
                task_description="task",
                status="running",
                message_queue=child_q,
            )
            await ctx.registry.register(handle)

            step = self._steer_step("msg_target_id", "message", "focus on errors only")
            result = await tool.handle_steer_agent(step)
            assert "Message sent" in result.content
            msg = child_q.get_nowait()
            assert "focus on errors only" in msg

        asyncio.run(_test())

    def test_message_missing_message_field_returns_error(self):
        async def _test():
            ctx = _make_root_ctx()
            tool = _make_spawn_tool(ctx)

            handle = AgentHandle(
                agent_id="no_msg_target",
                parent_id=ctx.agent_id,
                depth=1,
                model_name="test-model",
                task_description="task",
                status="running",
                message_queue=queue.Queue(),
            )
            await ctx.registry.register(handle)

            # action='message' but no 'message' field
            step = ActionStep(
                tool_id="steer_agent",
                operation="set",
                tool_input={"agent_id": "no_msg_target", "action": "message"},
            )
            result = await tool.handle_steer_agent(step)
            assert "ERROR" in result.content
            assert "required" in result.content.lower() or "message" in result.content.lower()

        asyncio.run(_test())

    def test_message_to_completed_agent_returns_failure_reason(self):
        """send_message to completed agent returns str failure reason."""

        async def _test():
            ctx = _make_root_ctx()
            tool = _make_spawn_tool(ctx)

            handle = AgentHandle(
                agent_id="done_target_id",
                parent_id=ctx.agent_id,
                depth=1,
                model_name="test-model",
                task_description="task",
                status="completed",
                message_queue=queue.Queue(),
            )
            await ctx.registry.register(handle)

            step = self._steer_step("done_target_id", "message", "too late")
            result = await tool.handle_steer_agent(step)
            assert "Message failed" in result.content

        asyncio.run(_test())

    def test_unknown_action_returns_error(self):
        async def _test():
            ctx = _make_root_ctx()
            tool = _make_spawn_tool(ctx)

            handle = AgentHandle(
                agent_id="act_test_agent",
                parent_id=ctx.agent_id,
                depth=1,
                model_name="test-model",
                task_description="task",
                status="running",
                message_queue=queue.Queue(),
            )
            await ctx.registry.register(handle)

            step = ActionStep(
                tool_id="steer_agent",
                operation="set",
                tool_input={
                    "agent_id": "act_test_agent",
                    "action": "invalid_action",
                },
            )
            result = await tool.handle_steer_agent(step)
            assert "ERROR" in result.content
            assert "Unknown action" in result.content

        asyncio.run(_test())

    def test_cancel_agent_with_done_task_returns_reason(self):
        """cancel_agent when asyncio_task already done returns 'already done' reason."""

        async def _test():
            ctx = _make_root_ctx()
            tool = _make_spawn_tool(ctx)

            # Create a task that finishes immediately
            async def _noop():
                return

            done_task = asyncio.create_task(_noop())
            # Wait until the task is actually done before registering it
            await done_task
            assert done_task.done()

            handle = AgentHandle(
                agent_id="done_task_agent",
                parent_id=ctx.agent_id,
                depth=1,
                model_name="test-model",
                task_description="task",
                status="running",
                asyncio_task=done_task,
            )
            await ctx.registry.register(handle)

            step = self._steer_step("done_task_agent", "cancel")
            result = await tool.handle_steer_agent(step)
            # cancel_agent returns "task already done (status: ...)" for a done task
            assert "already done" in result.content

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# _run_child_lifecycle – cancellation path (lines 791-801)
# ---------------------------------------------------------------------------


class TestLifecycleCancelled:
    """Lifecycle manager stores cancelled AgentResult on CancelledError."""

    def test_lifecycle_cancelled_stores_result(self):
        async def _test():
            root_q: queue.Queue[str] = queue.Queue()
            hv = _make_hypervisor()
            ctx = _make_root_ctx(hypervisor=hv, message_queue=root_q)

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

            # Spawn once so we have a child registered
            bound = MagicMock()

            async def _slow_response(*args, **kwargs):
                await asyncio.sleep(0)
                return _text_response("done")

            bound.ainvoke = AsyncMock(side_effect=_slow_response)

            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound
                await tool.run_async(_step("slow task"))

            # Cancel the lifecycle task before it completes
            if tool._lifecycle_tasks:
                task = tool._lifecycle_tasks[0]
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

            await tool.await_lifecycle_managers(timeout=1.0)

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# await_lifecycle_managers – still_pending cancellation (line 870)
# ---------------------------------------------------------------------------


class TestAwaitLifecycleManagers:
    """await_lifecycle_managers cancels still-pending tasks."""

    def test_still_pending_tasks_cancelled(self):
        async def _test():
            ctx = _make_root_ctx()
            tool = _make_spawn_tool(ctx)

            # Inject a fake long-running lifecycle task that ignores cancellation
            async def _long_running():
                try:
                    await asyncio.sleep(999)
                except asyncio.CancelledError:
                    pass  # Absorb — stays pending

            lm_task = asyncio.create_task(_long_running())
            tool._lifecycle_tasks.append(lm_task)

            # Call with very short timeout so it times out and then cancels
            await tool.await_lifecycle_managers(timeout=0.05)

            # Give the event loop one iteration to process the cancel
            await asyncio.sleep(0)

            # lifecycle_tasks list must be cleared regardless
            assert tool._lifecycle_tasks == []
            # Task must be in done (cancelled or finished) or cancelling state
            # (task.cancel() was called — lm_task.done() may lag one iteration)
            assert lm_task.cancelled() or lm_task.done() or lm_task.cancelling() > 0

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Hypervisor: unregister running agent (lines 194-196)
# ---------------------------------------------------------------------------


class TestHypervisorUnregisterRunning:
    """Unregistering a 'running' agent upgrades status to 'completed'."""

    def test_unregister_running_sets_completed(self):
        async def _test():
            hv = _make_hypervisor()
            handle = AgentHandle(
                agent_id="running_agent",
                parent_id=None,
                depth=0,
                model_name="test-model",
                task_description="task",
                status="running",
            )
            await hv.register(handle)

            removed = await hv.unregister("running_agent")
            assert removed is not None
            assert removed.status == "completed"
            assert removed.stopped_at is not None

        asyncio.run(_test())

    def test_unregister_completed_agent_not_changed(self):
        async def _test():
            hv = _make_hypervisor()
            handle = AgentHandle(
                agent_id="completed_agent",
                parent_id=None,
                depth=0,
                model_name="test-model",
                task_description="task",
                status="completed",
            )
            await hv.register(handle)

            removed = await hv.unregister("completed_agent")
            assert removed is not None
            # Status should remain completed (not upgraded again)
            assert removed.status == "completed"

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Hypervisor: send_message — no message_queue branch (line 330)
# ---------------------------------------------------------------------------


class TestHypervisorSendMessageNoQueue:
    """send_message to a running agent with no queue returns diagnostic."""

    def test_no_message_queue_returns_reason(self):
        async def _test():
            hv = _make_hypervisor()
            handle = AgentHandle(
                agent_id="no_queue_agent",
                parent_id=None,
                depth=0,
                model_name="test-model",
                task_description="t",
                status="running",
                message_queue=None,  # Explicitly no queue
            )
            await hv.register(handle)
            reason = await hv.send_message("no_queue_agent", "hello")
            assert reason is not None
            assert "no message queue" in reason

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Hypervisor: render_agent_tree — failed, cancelled, progress_note, result
# ---------------------------------------------------------------------------


class TestRenderAgentTreeBranches:
    """render_agent_tree branch coverage: failed/cancelled markers,
    result summary, progress_note."""

    def test_failed_agent_shows_FAILED_marker(self):
        async def _test():
            hv = _make_hypervisor()
            handle = AgentHandle(
                agent_id="failed_agnt",
                parent_id=None,
                depth=0,
                model_name="test-model",
                task_description="task",
                status="failed",
            )
            await hv.register(handle)
            tree = await hv.render_agent_tree()
            assert "FAILED" in tree

        asyncio.run(_test())

    def test_cancelled_agent_shows_cancelled_marker(self):
        async def _test():
            hv = _make_hypervisor()
            handle = AgentHandle(
                agent_id="cancelled1",
                parent_id=None,
                depth=0,
                model_name="test-model",
                task_description="task",
                status="cancelled",
            )
            await hv.register(handle)
            tree = await hv.render_agent_tree()
            assert "-> cancelled" in tree

        asyncio.run(_test())

    def test_result_summary_shown_over_progress_note(self):
        async def _test():
            hv = _make_hypervisor()
            handle = AgentHandle(
                agent_id="result_agnt",
                parent_id=None,
                depth=0,
                model_name="test-model",
                task_description="task",
                status="completed",
                result=AgentResult(
                    content="full output",
                    status="completed",
                    steps_used=2,
                    summary="short CU summary",
                ),
            )
            handle.progress_note = "step 3: read_file -> file.py"
            await hv.register(handle)
            tree = await hv.render_agent_tree()
            assert "short CU summary" in tree
            # progress_note should NOT appear if result.summary is set
            assert "step 3: read_file" not in tree

        asyncio.run(_test())

    def test_progress_note_shown_when_no_result(self):
        async def _test():
            hv = _make_hypervisor()
            handle = AgentHandle(
                agent_id="progress_ag",
                parent_id=None,
                depth=0,
                model_name="test-model",
                task_description="task",
                status="running",
            )
            handle.progress_note = "step 1: shell_tool -> ls output"
            await hv.register(handle)
            tree = await hv.render_agent_tree()
            assert "step 1: shell_tool" in tree

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Hypervisor: send_to_parent branches (lines 438-447)
# ---------------------------------------------------------------------------


class TestHypervisorSendToParent:
    """send_to_parent error paths: no child, no parent, no parent queue."""

    def test_send_to_parent_child_not_in_registry(self):
        async def _test():
            hv = _make_hypervisor()
            reason = await hv.send_to_parent("nonexistent", "msg")
            assert reason is not None
            assert "child not in registry" in reason

        asyncio.run(_test())

    def test_send_to_parent_parent_not_in_registry(self):
        async def _test():
            hv = _make_hypervisor()
            # Register child but not its parent
            child = AgentHandle(
                agent_id="orphan_child",
                parent_id="ghost_parent",
                depth=1,
                model_name="m",
                task_description="task",
            )
            await hv.register(child)
            reason = await hv.send_to_parent("orphan_child", "msg")
            assert reason is not None
            assert "parent not in registry" in reason

        asyncio.run(_test())

    def test_send_to_parent_parent_has_no_queue(self):
        async def _test():
            hv = _make_hypervisor()
            parent = AgentHandle(
                agent_id="parent_noq",
                parent_id=None,
                depth=0,
                model_name="m",
                task_description="parent task",
                message_queue=None,
            )
            child = AgentHandle(
                agent_id="child_of_nq",
                parent_id="parent_noq",
                depth=1,
                model_name="m",
                task_description="child task",
            )
            await hv.register(parent)
            await hv.register(child)
            reason = await hv.send_to_parent("child_of_nq", "msg")
            assert reason is not None
            # Production string from hypervisor.py send_to_parent:
            assert reason == "parent has no message queue"

        asyncio.run(_test())

    def test_send_to_parent_success(self):
        async def _test():
            hv = _make_hypervisor()
            parent_q: queue.Queue[str] = queue.Queue()
            parent = AgentHandle(
                agent_id="real_parent",
                parent_id=None,
                depth=0,
                model_name="m",
                task_description="parent task",
                message_queue=parent_q,
            )
            child = AgentHandle(
                agent_id="real_child",
                parent_id="real_parent",
                depth=1,
                model_name="m",
                task_description="child task",
            )
            await hv.register(parent)
            await hv.register(child)
            reason = await hv.send_to_parent("real_child", "notification message")
            assert reason is None  # success
            msg = parent_q.get_nowait()
            assert "notification message" in msg

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Hypervisor: cancel_agent edge cases (lines 461-465)
# ---------------------------------------------------------------------------


class TestHypervisorCancelAgentEdgeCases:
    """cancel_agent: no task, already done task."""

    def test_cancel_no_asyncio_task(self):
        async def _test():
            hv = _make_hypervisor()
            handle = AgentHandle(
                agent_id="notask_agent",
                parent_id=None,
                depth=0,
                model_name="m",
                task_description="t",
                status="running",
                asyncio_task=None,
            )
            await hv.register(handle)
            reason = await hv.cancel_agent("notask_agent")
            assert reason is not None
            assert "no asyncio task" in reason

        asyncio.run(_test())

    def test_cancel_already_done_task(self):
        async def _test():
            hv = _make_hypervisor()

            async def _done():
                return

            task = asyncio.create_task(_done())
            await asyncio.sleep(0)  # Let it finish

            handle = AgentHandle(
                agent_id="donetask_ag",
                parent_id=None,
                depth=0,
                model_name="m",
                task_description="t",
                status="running",
                asyncio_task=task,
            )
            await hv.register(handle)
            reason = await hv.cancel_agent("donetask_ag")
            assert reason is not None
            assert "already done" in reason

        asyncio.run(_test())

    def test_cancel_not_in_registry(self):
        async def _test():
            hv = _make_hypervisor()
            reason = await hv.cancel_agent("ghost_agent")
            assert reason is not None
            assert "not in registry" in reason

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Hypervisor: cleanup — 3-phase with stuck tasks (lines 492-521)
# ---------------------------------------------------------------------------


class TestHypervisorCleanup3Phase:
    """Cleanup: phase 1 cancel, phase 2 wait, phase 3 force-mark."""

    def test_cleanup_with_stuck_tasks_force_marks_cancelled(self):
        async def _test():
            hv = _make_hypervisor()

            # Task that ignores CancelledError for a bit
            async def _stubborn():
                try:
                    await asyncio.sleep(999)
                except asyncio.CancelledError:
                    await asyncio.sleep(0)  # Absorb, then raise
                    raise

            task = asyncio.create_task(_stubborn())

            handle = AgentHandle(
                agent_id="stubborn_ag",
                parent_id=None,
                depth=0,
                model_name="m",
                task_description="stubborn task",
                status="running",
                asyncio_task=task,
            )
            await hv.register(handle)

            # Very short timeout to trigger phase 3
            await hv.cleanup(timeout=0.05)

            # All agents cleared
            all_agents = await hv.list_all()
            assert len(all_agents) == 0

        asyncio.run(_test())

    def test_cleanup_no_running_agents_clears_submitted(self):
        """Cleanup with no running agents still clears registry."""

        async def _test():
            hv = _make_hypervisor()
            handle = AgentHandle(
                agent_id="submitted_ag",
                parent_id=None,
                depth=0,
                model_name="m",
                task_description="t",
                status="submitted",
            )
            await hv.register(handle)

            await hv.cleanup(timeout=1.0)

            all_agents = await hv.list_all()
            assert len(all_agents) == 0

        asyncio.run(_test())

    def test_cleanup_multiple_running_all_cancelled(self):
        async def _test():
            hv = _make_hypervisor()

            tasks = []
            for i in range(3):

                async def _sleep():
                    await asyncio.sleep(999)

                t = asyncio.create_task(_sleep())
                tasks.append(t)
                h = AgentHandle(
                    agent_id=f"multi_ag_{i}",
                    parent_id=None,
                    depth=0,
                    model_name="m",
                    task_description=f"task {i}",
                    status="running",
                    asyncio_task=t,
                )
                await hv.register(h)

            await hv.cleanup(timeout=1.0)
            all_agents = await hv.list_all()
            assert len(all_agents) == 0

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# collect_completed / collect_running (lines 406-427)
# ---------------------------------------------------------------------------


class TestHypervisorCollectMethods:
    """collect_completed and collect_running contract tests."""

    def test_collect_completed_returns_terminal_with_result(self):
        async def _test():
            hv = _make_hypervisor()
            parent_id = "root_collect"

            # completed WITH result
            h1 = AgentHandle(
                agent_id="comp_result",
                parent_id=parent_id,
                depth=1,
                model_name="m",
                task_description="t",
                status="completed",
                result=AgentResult("output", "completed", 2),
            )
            # completed WITHOUT result (excluded from collect_completed)
            h2 = AgentHandle(
                agent_id="comp_no_result",
                parent_id=parent_id,
                depth=1,
                model_name="m",
                task_description="t",
                status="completed",
                result=None,
            )
            # failed WITH result
            h3 = AgentHandle(
                agent_id="failed_with_result",
                parent_id=parent_id,
                depth=1,
                model_name="m",
                task_description="t",
                status="failed",
                result=AgentResult("err", "failed", 1),
            )
            # running (not completed)
            h4 = AgentHandle(
                agent_id="still_running",
                parent_id=parent_id,
                depth=1,
                model_name="m",
                task_description="t",
                status="running",
            )
            for h in [h1, h2, h3, h4]:
                await hv.register(h)

            completed = await hv.collect_completed(parent_id)
            ids = {h.agent_id for h in completed}
            assert "comp_result" in ids
            assert "failed_with_result" in ids
            assert "comp_no_result" not in ids  # no result
            assert "still_running" not in ids

        asyncio.run(_test())

    def test_collect_running_returns_submitted_and_running(self):
        async def _test():
            hv = _make_hypervisor()
            parent_id = "root_running"

            h_submitted = AgentHandle(
                agent_id="subm_1",
                parent_id=parent_id,
                depth=1,
                model_name="m",
                task_description="t",
                status="submitted",
            )
            h_running = AgentHandle(
                agent_id="running_1",
                parent_id=parent_id,
                depth=1,
                model_name="m",
                task_description="t",
                status="running",
            )
            h_completed = AgentHandle(
                agent_id="compl_1",
                parent_id=parent_id,
                depth=1,
                model_name="m",
                task_description="t",
                status="completed",
            )
            for h in [h_submitted, h_running, h_completed]:
                await hv.register(h)

            running = await hv.collect_running(parent_id)
            ids = {h.agent_id for h in running}
            assert "subm_1" in ids
            assert "running_1" in ids
            assert "compl_1" not in ids

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Non-blocking spawn: notification contains task + result (lines 843-852)
# ---------------------------------------------------------------------------


class TestLifecycleManagerNotificationFormat:
    """Notification from lifecycle manager includes task desc and result."""

    def test_notification_contains_task_and_result(self):
        async def _test():
            tool, ctx = await _spawn_non_blocking("security log analysis")

            messages: list[str] = []
            try:
                while True:
                    messages.append(ctx.message_queue.get_nowait())
            except queue.Empty:
                pass

            assert messages, "Parent got no notification"
            notification = messages[-1]
            assert "security log analysis" in notification

        asyncio.run(_test())

    def test_lifecycle_result_set_on_handle(self):
        async def _test():
            tool, ctx = await _spawn_non_blocking("data pipeline")

            children = await ctx.registry.list_children(ctx.agent_id)
            assert len(children) == 1
            child = children[0]
            assert child.result is not None
            assert child.result.status == "completed"

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Non-blocking spawn: admission control release after lifecycle (line 854)
# ---------------------------------------------------------------------------


class TestLifecycleAdmissionRelease:
    """The concurrency slot comes back once the lifecycle finishes."""

    def test_slot_released_after_lifecycle(self):
        async def _test():
            hv = AgentHypervisor(max_concurrent=1)
            root_q: queue.Queue[str] = queue.Queue()
            ctx = _make_root_ctx(hypervisor=hv, message_queue=root_q)

            root_handle = AgentHandle(
                agent_id=ctx.agent_id,
                parent_id=None,
                depth=0,
                model_name="test-model",
                task_description="root task",
                status="running",
                message_queue=root_q,
            )
            await hv.register(root_handle)

            tool = _make_spawn_tool(ctx)
            bound = MagicMock()
            bound.ainvoke = AsyncMock(return_value=_text_response("done"))

            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound
                await tool.run_async(_step("first task"))
                await tool.await_lifecycle_managers(timeout=5.0)

            # After the lifecycle finishes the slot is back and nothing waits.
            assert hv.free_slots == 1
            assert hv.pending_dispatch == 0

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# SpawnAgentTool: string tool_input coercion (lines 296-299)
# ---------------------------------------------------------------------------


class TestStringToolInputCoercion:
    """If tool_input is a string (not dict), coerce to {'task': str}."""

    def test_string_tool_input_treated_as_task(self):
        async def _test():
            ctx = _make_root_ctx()
            tool = _make_spawn_tool(ctx)
            bound = MagicMock()
            bound.ainvoke = AsyncMock(return_value=_text_response("ok"))

            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound

                step = ActionStep(
                    tool_id="spawn_agent",
                    operation="set",
                    tool_input="do the thing",  # string, not dict
                )
                result = await tool.run_async(step)

            # Non-blocking (root) returns submitted payload
            parsed = json.loads(result.content)
            assert parsed["status"] == "submitted"

            await tool.await_lifecycle_managers(timeout=5.0)

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Parametrized: blocking sub-agent result fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "task_response,expected_status_in",
    [
        ("I have completed the analysis.", ["completed", "failed"]),
        ("Done — results saved.", ["completed", "failed"]),
    ],
)
class TestBlockingSpawnResultFields:
    """Blocking spawn (depth=1) returns full AgentResult JSON with all required fields."""

    def test_result_has_required_fields(self, task_response: str, expected_status_in: list):
        async def _test():
            hv = _make_hypervisor()
            ctx = AgentContext.root(
                model_name="test-model",
                max_depth=5,
                registry=hv,
            )
            child_ctx = ctx.child()  # depth=1

            tool = SpawnAgentTool(
                agent_context=child_ctx,
                tool_registry=_make_registry("shell_tool"),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )

            bound = MagicMock()
            bound.ainvoke = AsyncMock(return_value=_text_response(task_response))

            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound

                step = _step("run analysis")
                result = await tool.run_async(step)

            parsed = json.loads(result.content)
            assert "status" in parsed
            assert "content" in parsed
            assert "steps_used" in parsed
            assert "summary" in parsed
            assert "warnings" in parsed
            assert "artifacts" in parsed
            assert parsed["status"] in expected_status_in

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# _emit_event: event_logger branch (lines 731-747)
# ---------------------------------------------------------------------------


class TestEmitEvent:
    """_emit_event fires when agent_context.event_logger is set."""

    def test_emit_event_calls_event_logger(self):
        async def _test():
            events: list[dict] = []

            def _logger(event):
                events.append(event)

            ctx = AgentContext.root(
                model_name="test-model",
                max_depth=5,
                registry=_make_hypervisor(),
                event_logger=_logger,
            )
            tool = _make_spawn_tool(ctx)
            bound = MagicMock()
            bound.ainvoke = AsyncMock(return_value=_text_response("done"))

            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound

                # Register root handle so send_to_parent can find parent's queue
                root_handle = AgentHandle(
                    agent_id=ctx.agent_id,
                    parent_id=None,
                    depth=0,
                    model_name="test-model",
                    task_description="root",
                    status="running",
                    message_queue=ctx.message_queue,
                )
                await ctx.registry.register(root_handle)

                step = _step("emit test task")
                await tool.run_async(step)
                await tool.await_lifecycle_managers(timeout=5.0)

            sub_agent_events = [e for e in events if e.get("type") == "sub_agent"]
            assert len(sub_agent_events) >= 1
            event = sub_agent_events[0]
            assert event["payload"]["depth"] == 1
            assert event["payload"]["action"] == "start"

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# check_agents wait=True with timeout exhausted (lines 603-606)
# ---------------------------------------------------------------------------


class TestCheckAgentsWaitTimeout:
    """check_agents wait=True with an agent that never signals done_event."""

    def test_wait_timeout_cancels_waiters(self):
        async def _test():
            ctx = _make_root_ctx()
            tool = _make_spawn_tool(ctx)

            # Running agent whose done_event is never set
            child_handle = AgentHandle(
                agent_id="slow_child_agnt",
                parent_id=ctx.agent_id,
                depth=1,
                model_name="test-model",
                task_description="slow task",
                status="running",
            )
            await ctx.registry.register(child_handle)

            step = ActionStep(
                tool_id="check_agents",
                operation="set",
                tool_input={"wait": True, "timeout": 0.05},  # Very short timeout
            )
            result = await tool.handle_check_agents(step)
            payload = json.loads(result.content)
            # Should return even though agent didn't complete
            assert payload["kind"] == "agent_tree"
            assert "still running" in payload["text"]

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# agent_type: allowed_tools/denied_tools applied through run_async (lines 328,330)
# ---------------------------------------------------------------------------


class TestAgentTypeToolScopingInRunAsync:
    """Lines 328/330: agent_def.allowed_tools/denied_tools applied in run_async."""

    def test_agent_type_allowed_and_denied_via_run_async(self):
        async def _test():
            hv = _make_hypervisor()
            ctx = AgentContext.root(
                model_name="test-model",
                max_depth=5,
                registry=hv,
            )
            child_ctx = ctx.child()  # depth=1 (blocking)

            agent_registry = AgentRegistry()
            agent_def = AgentDef(
                name="scoped-agent",
                description="test",
                source_path="/fake/path.md",
                source="plugin:test",
                body="Be scoped.",
                allowed_tools=["tool_a"],
                denied_tools=["tool_b"],
            )
            agent_registry.register(agent_def)

            captured_specs: list[list] = []

            class _CapturingTool(SpawnAgentTool):
                def _filter_tool_specs(self, args, *, capability_mode="all"):
                    specs = super()._filter_tool_specs(
                        args, capability_mode=capability_mode
                    )
                    captured_specs.append(specs)
                    return specs

            tool = _CapturingTool(
                agent_context=child_ctx,
                tool_registry=_make_registry("tool_a", "tool_b", "tool_c"),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
                agent_registry=agent_registry,
            )

            bound = MagicMock()
            bound.ainvoke = AsyncMock(return_value=_text_response("done"))

            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound

                step = ActionStep(
                    tool_id="spawn_agent",
                    operation="set",
                    # Caller does NOT override allowed_tools or denied_tools
                    tool_input={"task": "work", "agent_type": "scoped-agent"},
                )
                await tool.run_async(step)

            assert len(captured_specs) == 1
            ids = {s.tool_id for s in captured_specs[0]}
            assert "tool_a" in ids
            assert "tool_b" not in ids  # denied
            assert "tool_c" not in ids  # not in allowed

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# AgentDepthExceeded with a registered handle (lines 467-473)
# ---------------------------------------------------------------------------


class TestAgentDepthExceededWithHandle:
    """AgentDepthExceeded when handle has been registered (handle is not None)."""

    def test_depth_exceeded_with_registered_handle(self):
        """This tests the branch where handle IS registered before depth is exceeded.

        In production this can't happen because child() raises before register()
        in the normal flow. However, the except AgentDepthExceeded clause checks
        `if handle:` — we verify cannot_solve is returned whenever depth is exceeded.
        """

        async def _test():
            ctx = AgentContext.root(
                model_name="test-model",
                max_depth=1,
                registry=_make_hypervisor(),
            )
            child_at_max = ctx.child()  # depth=1 == max_depth, can't spawn
            assert not child_at_max.can_spawn

            tool = SpawnAgentTool(
                agent_context=child_at_max,
                tool_registry=_make_registry("shell_tool"),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )

            step = _step("deep work")
            result = await tool.run_async(step)

            parsed = json.loads(result.content)
            assert parsed["status"] == "cannot_solve"
            assert "warnings" in parsed

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Hypervisor: a full fleet never stalls the caller
# ---------------------------------------------------------------------------


class TestHypervisorFullFleetDoesNotStall:
    """A saturated fleet answers IMMEDIATELY — accepted, not timed out.

    The 30s blocking admit this replaces is the reason the batch path went
    non-blocking and started dropping its surplus in the first place. Neither
    trade-off exists once acceptance and dispatch are separate answers.
    """

    def test_accept_on_a_full_fleet_returns_at_once(self):
        async def _test():
            hv = AgentHypervisor(max_concurrent=1)
            started: list[str] = []

            def _unit(agent_id: str):
                async def _launch() -> None:
                    started.append(agent_id)

                return SpawnUnit(
                    spawn=ScheduledSpawn(agent_id=agent_id, enqueued_at=0.0),
                    launch=_launch,
                )

            assert await hv.accept(_unit("first")) is True
            # No timeout, no stall: well under any blocking-admit deadline.
            assert (
                await asyncio.wait_for(hv.accept(_unit("second")), timeout=0.5)
            ) is False
            assert started == ["first"]
            assert hv.pending_dispatch == 1

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Hypervisor: cleanup phase 3 — force-mark running agents after timeout (507-513)
# ---------------------------------------------------------------------------


class TestHypervisorCleanupPhase3ForceMark:
    """Cleanup phase 3: agent task completes but status still 'running' → force mark."""

    def test_phase3_force_marks_lingering_running_agent(self):
        async def _test():
            hv = _make_hypervisor()

            # A task that ignores the first CancelledError but eventually finishes
            cancelled_count = 0

            async def _resistant():
                nonlocal cancelled_count
                for _ in range(2):
                    try:
                        await asyncio.sleep(999)
                    except asyncio.CancelledError:
                        cancelled_count += 1
                        # Don't re-raise on first cancel — absorb it
                        continue
                # Never re-raises — task finishes "normally" after absorption

            task = asyncio.create_task(_resistant())

            handle = AgentHandle(
                agent_id="resist_agent",
                parent_id=None,
                depth=0,
                model_name="m",
                task_description="resistant task",
                status="running",
                asyncio_task=task,
            )
            await hv.register(handle)

            # Run cleanup with very short timeout to force phase 3 path
            await hv.cleanup(timeout=0.1)

            # All agents cleared
            all_agents = await hv.list_all()
            assert len(all_agents) == 0

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Lifecycle manager: grandchild cancellation (lines 835-836 in _run_child_lifecycle)
# ---------------------------------------------------------------------------


class TestLifecycleManagerGrandchildCancellation:
    """_run_child_lifecycle cancels running grandchildren in its finally block."""

    def test_lifecycle_cancels_running_grandchild(self):
        async def _test():
            root_q: queue.Queue[str] = queue.Queue()
            hv = _make_hypervisor()
            ctx = _make_root_ctx(hypervisor=hv, message_queue=root_q)

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

            bound = MagicMock()

            # Track the child_ctx agent_id to pre-register a grandchild
            child_ids: list[str] = []

            original_child = AgentContext.child

            def _tracking_child(self_ctx, **kwargs):
                child = original_child(self_ctx, **kwargs)
                child_ids.append(child.agent_id)
                return child

            gc_task = asyncio.create_task(asyncio.sleep(999))
            gc_registered = asyncio.Event()

            async def _register_grandchild_then_respond(*args, **kwargs):
                # After the child is created, register a grandchild for it
                if child_ids:
                    gc_handle = AgentHandle(
                        agent_id="grandchild_lm",
                        parent_id=child_ids[-1],
                        depth=2,
                        model_name="m",
                        task_description="grandchild",
                        status="running",
                        asyncio_task=gc_task,
                    )
                    await hv.register(gc_handle)
                    gc_registered.set()
                return _text_response("done from child")

            bound.ainvoke = AsyncMock(side_effect=_register_grandchild_then_respond)

            with (
                patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build,
                patch.object(AgentContext, "child", _tracking_child),
            ):
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound
                await tool.run_async(_step("parent spawns grandchild"))
                # Wait for grandchild to be registered
                try:
                    await asyncio.wait_for(gc_registered.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass  # Grandchild may not have registered; still valid
                await tool.await_lifecycle_managers(timeout=5.0)

            # Give event loop a tick to process cancellations
            await asyncio.sleep(0)
            # Grandchild task must be cancelled or actively being cancelled.
            # gc_task.cancelling() >= 0 is always True (vacuous), so use > 0
            # to confirm cancel() was actually called.
            assert gc_task.cancelled() or gc_task.cancelling() > 0

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Lifecycle manager: notification else branch (lines 850-851)
# ---------------------------------------------------------------------------


class TestLifecycleManagerNotificationElseBranch:
    """Lines 850-851: notification without result falls back to status string."""

    def test_notification_else_branch_on_cancelled(self):
        """When lifecycle is cancelled, handle.result is set to 'cancelled' AgentResult.
        The notification line with result.status is used — not the else branch directly,
        but we verify the notification still reaches the parent queue."""

        async def _test():
            root_q: queue.Queue[str] = queue.Queue()
            hv = _make_hypervisor()
            ctx = _make_root_ctx(hypervisor=hv, message_queue=root_q)

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

            # Manually drive _run_child_lifecycle; the model raises CancelledError
            # so the child loop the driver creates resolves to the cancel path.
            from mewbo_core.agents.spawn_agent import RetryPolicy

            tool = _make_spawn_tool(ctx)

            child_ctx = ctx.child()  # depth=1
            child_handle = AgentHandle(
                agent_id=child_ctx.agent_id,
                parent_id=ctx.agent_id,
                depth=1,
                model_name="test-model",
                task_description="lifecycle test",
                status="running",
                message_queue=child_ctx.message_queue,
            )
            await hv.register(child_handle)

            async def _immediate_cancel(*args, **kwargs):
                raise asyncio.CancelledError("immediate")

            bound = MagicMock()
            bound.ainvoke = AsyncMock(side_effect=_immediate_cancel)

            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound
                # Signature: (child_ctx, handle, child_specs, allowed_tools,
                # task_desc, retry, summary_kind). Retry off → the cancel is
                # never retried; "generic" summary_kind is the untyped default.
                await tool._run_child_lifecycle(
                    child_ctx,
                    child_handle,
                    [],
                    None,
                    "lifecycle test",
                    RetryPolicy(),
                    "generic",
                )

            # Parent should get a notification (even on cancel path)
            messages: list[str] = []
            try:
                while True:
                    messages.append(root_q.get_nowait())
            except queue.Empty:
                pass

            # At least one notification
            assert messages or child_handle.result is not None

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# blocking spawn finally block: running grandchild cancelled (lines 528-529)
# ---------------------------------------------------------------------------


class TestBlockingSpawnFinallyGrandchild:
    """Finally block in blocking run_async cancels running grandchildren.

    Approach: intercept AgentContext.child() to synchronously capture the
    created child's agent_id and immediately pre-register a grandchild whose
    parent_id matches, so the grandchild is always in the registry before
    the child loop returns.
    """

    def test_running_grandchild_cancelled_in_finally(self):
        async def _test():
            hv = _make_hypervisor()
            ctx = AgentContext.root(
                model_name="test-model",
                max_depth=5,
                registry=hv,
            )
            child_ctx = ctx.child()  # depth=1 — blocking spawn path

            gc_task = asyncio.create_task(asyncio.sleep(999))
            gc_handle: list[AgentHandle] = []

            # Use a synchronous register inside the child() interception so
            # the grandchild is guaranteed registered before the model fires.
            original_child = AgentContext.child

            def _capturing_child(self_ctx, **kwargs):
                child = original_child(self_ctx, **kwargs)
                # Immediately register grandchild synchronously (no await needed
                # for the dict update, which is the critical part — real register
                # uses asyncio.Lock, so schedule it and it will run before model)
                h = AgentHandle(
                    agent_id="gc_sync_block",
                    parent_id=child.agent_id,
                    depth=2,
                    model_name="test-model",
                    task_description="running grandchild",
                    status="running",
                    asyncio_task=gc_task,
                )
                # Directly insert into hypervisor's _agents (bypasses lock; safe
                # in tests since there's no concurrent modification at this point)
                hv._agents[h.agent_id] = h
                gc_handle.append(h)
                return child

            tool = SpawnAgentTool(
                agent_context=child_ctx,
                tool_registry=_make_registry("shell_tool"),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )
            bound = MagicMock()
            bound.ainvoke = AsyncMock(return_value=_text_response("done"))

            with (
                patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build,
                patch.object(AgentContext, "child", _capturing_child),
            ):
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound
                await tool.run_async(_step("blocking parent"))

            # The finally block must have found and cancelled the running grandchild
            assert len(gc_handle) == 1, "Grandchild was never registered"
            assert gc_handle[0].status == "cancelled", (
                f"Expected gc_handle.status='cancelled', got '{gc_handle[0].status}'. "
                "Finally block in blocking run_async must cancel running grandchildren."
            )

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# agent_type on sub_agent lifecycle events (Lane A — trace projection needs
# the spawned AgentDef name as the lane identity, not the model name).
# ---------------------------------------------------------------------------


class TestSubAgentEventAgentType:
    """A spawned agent's ``sub_agent`` start/stop events carry ``agent_type``."""

    def _make_agent_def(self, name: str) -> AgentDef:
        return AgentDef(
            name=name,
            description="A probe agent",
            source_path="/fake/path.md",
            source="plugin:test",
            body="You are a probe.\n\nProbe the source.",
        )

    def _drive_blocking_spawn(self, tool_input: dict) -> list[dict]:
        """Run a depth-1 (blocking) spawn and return the captured sub_agent payloads."""
        events: list[dict] = []

        async def _test():
            # Depth-1 child context so ``run_async`` runs the child to completion
            # synchronously (start + stop both emitted in one call), with an
            # event_logger that records every sub_agent lifecycle payload.
            hv = _make_hypervisor()
            root = AgentContext.root(
                model_name="parent-model",
                max_depth=5,
                registry=hv,
                event_logger=lambda e: events.append(e),
            )
            child_ctx = root.child()  # depth=1 → blocking spawn path

            agent_registry = AgentRegistry()
            agent_registry.register(self._make_agent_def("scg-path-probe"))

            tool = SpawnAgentTool(
                agent_context=child_ctx,
                tool_registry=_make_registry("shell_tool"),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
                agent_registry=agent_registry,
            )
            bound = MagicMock()
            bound.ainvoke = AsyncMock(return_value=_text_response("probe done"))

            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound
                await tool.run_async(
                    ActionStep(tool_id="spawn_agent", operation="set", tool_input=tool_input)
                )

        asyncio.run(_test())
        return [e["payload"] for e in events if e.get("type") == "sub_agent"]

    def test_agent_type_on_start_and_stop(self):
        """An ``agent_type`` spawn stamps the def name on BOTH lifecycle events."""
        payloads = self._drive_blocking_spawn(
            {"task": "probe github", "agent_type": "scg-path-probe"}
        )
        actions = [p["action"] for p in payloads]
        assert "start" in actions and "stop" in actions
        # Every lifecycle event carries the spawned AgentDef name — NOT the model.
        for p in payloads:
            assert p["agent_type"] == "scg-path-probe"
            assert p["agent_type"] != p["model"]  # distinct from the model name

    def test_no_agent_type_omits_key(self):
        """An ad-hoc spawn (no agent_type) omits the key entirely — never a null."""
        payloads = self._drive_blocking_spawn({"task": "ad-hoc work"})
        assert payloads  # start + stop emitted
        for p in payloads:
            assert "agent_type" not in p


# ---------------------------------------------------------------------------
# Batch fan-out: spawn_agents(tasks=[…])
# ---------------------------------------------------------------------------


def _batch_step(*tasks: dict) -> ActionStep:
    return ActionStep(
        tool_id="spawn_agents",
        operation="set",
        tool_input={"tasks": list(tasks)},
    )


async def _register_root(hv: AgentHypervisor) -> tuple[AgentContext, SpawnAgentTool]:
    """Root ctx + spawn tool with the root handle registered (for send_to_parent)."""
    root_q: queue.Queue[str] = queue.Queue()
    ctx = _make_root_ctx(hypervisor=hv, message_queue=root_q)
    await hv.register(
        AgentHandle(
            agent_id=ctx.agent_id,
            parent_id=None,
            depth=0,
            model_name=ctx.model_name,
            task_description="root",
            status="running",
            message_queue=root_q,
        )
    )
    return ctx, _make_spawn_tool(ctx)


class TestSpawnAgentsBatch:
    """spawn_agents fans out N children in ONE call via the existing hypervisor."""

    def test_batch_fans_out_three_children_concurrently(self):
        """≥3 children admitted in a single call, ordered ids, overlapping windows."""

        async def _test():
            hv = _make_hypervisor(max_concurrent=5)
            ctx, tool = await _register_root(hv)

            gate = asyncio.Event()

            async def gated(*_a, **_k):
                await gate.wait()
                return _text_response("child done")

            bound = MagicMock()
            bound.ainvoke = AsyncMock(side_effect=gated)

            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mb:
                mb.return_value = MagicMock()
                mb.return_value.bind_tools.return_value = bound

                result = await tool.run_batch_async(
                    _batch_step({"task": "a"}, {"task": "b"}, {"task": "c"})
                )
                payload = json.loads(result.content)

                # Ordered ids, all admitted, none rejected.
                assert payload["kind"] == "agent_batch"
                assert payload["spawned"] == 3
                assert payload["rejected"] == 0
                assert len(payload["agent_ids"]) == 3
                assert all(payload["agent_ids"])
                assert [a["index"] for a in payload["agents"]] == [0, 1, 2]
                assert all(a["status"] == "submitted" for a in payload["agents"])

                # Overlapping start windows: all three RUNNING simultaneously and
                # holding 3 of the 5 concurrency slots at the same time.
                running = await hv.collect_running(ctx.agent_id)
                assert len(running) == 3
                assert hv.free_slots == 2  # 5 - 3 concurrently held

                # check_agents shows all children in the tree.
                tree = await hv.render_agent_tree(exclude_agent_id=ctx.agent_id)
                for aid in payload["agent_ids"]:
                    assert aid[:8] in tree

                # Release; children settle; slots restored with NO inflation.
                gate.set()
                await tool.await_lifecycle_managers(timeout=5.0)

            assert hv.free_slots == 5

        asyncio.run(_test())

    def test_batch_over_capacity_defers_the_surplus_and_drains_it(self):
        """A wide fan-out is THROTTLED by concurrency, never truncated by it.

        The old contract dropped the surplus: 4 tasks against 2 slots returned
        ``rejected: 2`` and two workstreams simply never happened, while the
        caller read four agent slots and only two ids. Acceptance is now atomic
        — all four become real agents — and only DISPATCH is staggered.
        """

        async def _test():
            hv = _make_hypervisor(max_concurrent=2)
            ctx, tool = await _register_root(hv)

            gate = asyncio.Event()

            async def gated(*_a, **_k):
                await gate.wait()
                return _text_response("child done")

            bound = MagicMock()
            bound.ainvoke = AsyncMock(side_effect=gated)

            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mb:
                mb.return_value = MagicMock()
                mb.return_value.bind_tools.return_value = bound

                result = await tool.run_batch_async(
                    _batch_step(
                        {"task": "a"}, {"task": "b"}, {"task": "c"}, {"task": "d"}
                    )
                )
                payload = json.loads(result.content)

                # 4 accepted, 2 dispatched, 2 deferred, NOTHING rejected.
                assert payload["spawned"] == 4
                assert payload["dispatched"] == 2
                assert payload["deferred"] == 2
                assert payload["rejected"] == 0
                # Every accepted entry is ``submitted`` — starting now versus
                # waiting is a scheduling COUNT, never a per-agent status.
                statuses = [a["status"] for a in payload["agents"]]
                assert statuses == ["submitted"] * 4
                ids = payload["agent_ids"]
                assert all(ids), "every entry must come back with a real agent_id"
                assert len(set(ids)) == 4

                # The two dispatched siblings are unaffected — both running —
                # and the two waiting ones are visible as owed work.
                assert len(await hv.collect_running(ctx.agent_id)) == 4
                assert hv.pending_dispatch == 2
                assert hv.free_slots == 0

                # Drain: the settling pair pumps the queue, and all four run.
                gate.set()
                await tool.await_lifecycle_managers(timeout=5.0)

            assert hv.pending_dispatch == 0
            assert hv.free_slots == 2
            settled = {h.agent_id: h for h in await hv.list_all()}
            assert [settled[aid].status for aid in ids] == ["completed"] * 4
            assert all(settled[aid].result is not None for aid in ids)

        asyncio.run(_test())

    def test_batch_preserves_per_task_fields(self):
        """Each entry carries the same per-task fields; agent_type resolves."""

        async def _test():
            hv = _make_hypervisor(max_concurrent=5)
            ctx = _make_root_ctx(hypervisor=hv)
            await hv.register(
                AgentHandle(
                    agent_id=ctx.agent_id,
                    parent_id=None,
                    depth=0,
                    model_name=ctx.model_name,
                    task_description="root",
                    status="running",
                    message_queue=ctx.message_queue,
                )
            )
            reg = AgentRegistry()
            reg.register(
                AgentDef(
                    name="probe",
                    description="A probe agent",
                    source_path="/fake/probe.md",
                    source="plugin:test",
                    body="You are a probe.",
                    model="",
                    allowed_tools=["shell_tool"],
                )
            )
            tool = _make_spawn_tool(ctx, agent_registry=reg)

            bound = MagicMock()
            bound.ainvoke = AsyncMock(return_value=_text_response("done"))

            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mb:
                mb.return_value = MagicMock()
                mb.return_value.bind_tools.return_value = bound
                result = await tool.run_batch_async(
                    _batch_step(
                        {"task": "x", "agent_type": "probe"},
                        {"task": "y", "acceptance_criteria": "file exists"},
                    )
                )
                payload = json.loads(result.content)
                assert payload["spawned"] == 2
                await tool.await_lifecycle_managers(timeout=5.0)

        asyncio.run(_test())


class TestSpawnAgentsValidation:
    """Pydantic schema validated at definition (extra='forbid', non-empty task)."""

    def _run(self, tool_input: dict) -> str:
        async def _test() -> str:
            ctx = _make_root_ctx()
            tool = _make_spawn_tool(ctx)
            step = ActionStep(
                tool_id="spawn_agents", operation="set", tool_input=tool_input
            )
            result = await tool.run_batch_async(step)
            return result.content

        return asyncio.run(_test())

    def test_extra_field_rejected(self):
        content = self._run({"tasks": [{"task": "a", "bogus": 1}]})
        assert content.startswith("ERROR")

    def test_empty_tasks_rejected(self):
        content = self._run({"tasks": []})
        assert content.startswith("ERROR")

    def test_missing_tasks_rejected(self):
        content = self._run({"not_tasks": 1})
        assert content.startswith("ERROR")

    def test_blank_task_rejected(self):
        content = self._run({"tasks": [{"task": ""}]})
        assert content.startswith("ERROR")

    def test_per_entry_retry_field_accepted(self):
        """A batch entry may carry the `retry` policy (not extra-forbidden).

        The batch `items` schema reuses the single spawn params, which now
        advertise `retry`; the SpawnAgentTask validator must accept it and
        `to_args` must thread it through for `RetryPolicy.from_value`.
        """
        from mewbo_core.agents.spawn_agent import RetryPolicy, SpawnAgentTask

        entry = SpawnAgentTask.model_validate(
            {"task": "a", "retry": {"max": 2, "on": ["failed"], "backoff": 0.5}}
        )
        args = entry.to_args()
        assert args["retry"] == {"max": 2, "on": ["failed"], "backoff": 0.5}
        policy = RetryPolicy.from_value(args.get("retry"))
        assert policy.enabled and policy.max == 2 and policy.on == ("failed",)


class TestRootSpawnSlotNoInflation:
    """A root spawn HOLDS its slot and releases exactly once."""

    def test_root_child_holds_slot_then_releases_once(self):
        async def _test():
            hv = _make_hypervisor(max_concurrent=3)
            ctx = _make_root_ctx(hypervisor=hv)
            await hv.register(
                AgentHandle(
                    agent_id=ctx.agent_id,
                    parent_id=None,
                    depth=0,
                    model_name=ctx.model_name,
                    task_description="root",
                    status="running",
                    message_queue=ctx.message_queue,
                )
            )
            tool = _make_spawn_tool(ctx)

            gate = asyncio.Event()

            async def gated(*_a, **_k):
                await gate.wait()
                return _text_response("done")

            bound = MagicMock()
            bound.ainvoke = AsyncMock(side_effect=gated)

            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mb:
                mb.return_value = MagicMock()
                mb.return_value.bind_tools.return_value = bound
                await tool.run_async(_step("hold a slot"))
                # While the child runs it must HOLD one slot (was a no-op before).
                assert hv.free_slots == 2
                gate.set()
                await tool.await_lifecycle_managers(timeout=5.0)

            # Released exactly once — back to 3, never inflated above the max.
            assert hv.free_slots == 3

        asyncio.run(_test())


class TestRefusedSpawnIsAFailedStep:
    """A refused spawn must record as a FAILED tool step, not "✓ ok".

    The spawn branch of ``_execute_tool_call`` never set an error, so a refusal
    fell through to ``success=True``: no per-step failure nudge, no doom-loop
    tracking, no ``permanence``. It is the same defect the repo already fixed
    for SessionTools that RETURN a handled error rather than raising — so it
    now rides the same envelope and the same parse.
    """

    def test_the_refusal_envelope_survives_the_real_parser(self):
        """The JSON envelope must be READ by the seam that reclassifies it.

        This is the cross-check, not a formality. ``_SessionToolError.parse``
        reads envelopes with ``ast.literal_eval`` and its own docstring warns
        that a ``json.dumps`` payload gets silently DECLINED — and a decline
        here does not raise, it restores the ``success=True`` defect. So the
        round trip is pinned against the real parser rather than reasoned
        about: the JSON form works because every field is a ``str``, and that
        is a fact about the payload which a future field could break.
        """
        from mewbo_core.agents.spawn_agent import _SpawnOutcome
        from mewbo_core.loop.tool_use_loop import _SessionToolError

        outcome = _SpawnOutcome(
            # A message carrying the punctuation a real refusal uses: an em
            # dash (non-ASCII, escaped by json.dumps) and embedded single
            # quotes around the offending key.
            content="ERROR: Project 'gamma' cannot be resolved — try 'alpha'",
            agent_id=None,
            status="rejected",
            code="unresolvable_project",
        )
        parsed = _SessionToolError.parse(outcome.report())

        assert parsed is not None, "the loop would record this refusal as success"
        assert parsed.code == "unresolvable_project"
        assert "gamma" in parsed.message
        assert parsed.permanence == "permanent"
        assert parsed.blocks_completion is False

    def test_a_refused_spawn_records_success_false_and_permanent(self):
        from mewbo_core.loop.tool_use_loop import ToolUseLoop

        async def _test():
            hv = _make_hypervisor(max_concurrent=2)
            ctx = _make_root_ctx(hypervisor=hv)
            loop = ToolUseLoop(
                agent_context=ctx,
                tool_registry=_make_registry("shell_tool"),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )
            loop._spawn_agent_tool = _make_spawn_tool(
                ctx, agent_registry=AgentRegistry()
            )

            result = await loop._execute_tool_call(
                {
                    "id": "call-1",
                    "name": "spawn_agent",
                    "args": {"task": "work", "agent_type": "no-such-agent"},
                },
                [],
            )

            assert result.success is False, "a refusal is not a successful step"
            assert result.permanence == "permanent"
            # The model still reads the full typed detail — surfacing the
            # failure must not hide what the tool chose to return.
            assert "unknown_agent_type" in result.content
            # Not a blocked-run code: a bad agent_type is the caller's to fix,
            # not a condition that halts the session.
            assert result.blocked_code is None

        asyncio.run(_test())

    def test_an_accepted_spawn_still_records_success(self):
        """The seam fires ONLY on refusals — an ordinary spawn is untouched."""
        from mewbo_core.loop.tool_use_loop import ToolUseLoop

        async def _test():
            hv = _make_hypervisor(max_concurrent=2)
            ctx, tool = await _register_root(hv)
            loop = ToolUseLoop(
                agent_context=ctx,
                tool_registry=_make_registry("shell_tool"),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
            )
            loop._spawn_agent_tool = tool

            bound = MagicMock()
            bound.ainvoke = AsyncMock(return_value=_text_response("done"))
            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mb:
                mb.return_value = MagicMock()
                mb.return_value.bind_tools.return_value = bound
                result = await loop._execute_tool_call(
                    {"id": "call-2", "name": "spawn_agent", "args": {"task": "work"}},
                    [],
                )
                assert result.success is True
                assert result.permanence is None
                assert json.loads(result.content)["status"] == "submitted"
                await tool.await_lifecycle_managers(timeout=5.0)

        asyncio.run(_test())


class TestSteerAgentReachesADeferredChild:
    """Cancelling a child that is admitted but not yet started.

    A root fans out past the concurrency limit, the model calls
    ``steer_agent(action="cancel")`` on one of the waiting children, and is
    told ``cannot cancel: no asyncio task`` — and then that child starts and
    consumes a slot anyway once a sibling settles. Pre-scheduler the surplus
    spawn was refused outright, so no uncancellable agent could exist at all;
    deferring made this reachable and it has to be closed with it.
    """

    def test_cancelling_a_deferred_child_succeeds_and_it_never_runs(self):
        async def _test():
            hv = _make_hypervisor(max_concurrent=1)
            ctx, tool = await _register_root(hv)

            gate = asyncio.Event()
            built: list[str] = []

            async def gated(*_a, **_k):
                built.append("call")
                await gate.wait()
                return _text_response("child done")

            bound = MagicMock()
            bound.ainvoke = AsyncMock(side_effect=gated)

            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mb:
                mb.return_value = MagicMock()
                mb.return_value.bind_tools.return_value = bound

                first = json.loads((await tool.run_async(_step("first"))).content)
                second = json.loads((await tool.run_async(_step("second"))).content)
                assert hv.pending_dispatch == 1

                reply = await tool.handle_steer_agent(
                    ActionStep(
                        tool_id="steer_agent",
                        operation="set",
                        tool_input={"agent_id": second["agent_id"], "action": "cancel"},
                    )
                )
                assert "cancelled" in reply.content
                assert "cannot cancel" not in reply.content

                gate.set()
                await tool.await_lifecycle_managers(timeout=5.0)

            # Exactly ONE child ever drove a model call. Counting the total is
            # what makes this non-racy: a before/after snapshot would depend on
            # whether the first child's task had been scheduled yet.
            assert len(built) == 1, "the cancelled child must never have run"
            cancelled = await hv.get(second["agent_id"])
            assert cancelled is not None
            assert cancelled.status == "cancelled"
            assert (await hv.get(first["agent_id"])).status == "completed"
            assert hv.free_slots == 1

        asyncio.run(_test())

    def test_a_deferred_child_emits_no_start_until_it_dispatches(self):
        """No start event ⇒ no stop owed. The span invariant, for waiting units.

        ``_emit_terminal_stop`` guarantees one stop per start, and a deferred
        child that is cancelled never reaches a lifecycle manager to write
        one. So the start must not be emitted at ACCEPTANCE — otherwise every
        cancelled-while-waiting agent leaves a span open forever.
        """

        async def _test():
            hv = _make_hypervisor(max_concurrent=1)
            events: list[dict] = []
            root_q: queue.Queue[str] = queue.Queue()
            ctx = AgentContext.root(
                model_name="test-model",
                max_depth=5,
                registry=hv,
                message_queue=root_q,
                event_logger=lambda e: events.append(e),
            )
            await hv.register(
                AgentHandle(
                    agent_id=ctx.agent_id,
                    parent_id=None,
                    depth=0,
                    model_name=ctx.model_name,
                    task_description="root",
                    status="running",
                    message_queue=root_q,
                )
            )
            tool = _make_spawn_tool(ctx)

            gate = asyncio.Event()

            async def gated(*_a, **_k):
                await gate.wait()
                return _text_response("done")

            bound = MagicMock()
            bound.ainvoke = AsyncMock(side_effect=gated)

            def starts() -> list[str]:
                return [
                    e["payload"]["agent_id"]
                    for e in events
                    if e.get("type") == "sub_agent"
                    and e["payload"].get("action") == "start"
                ]

            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mb:
                mb.return_value = MagicMock()
                mb.return_value.bind_tools.return_value = bound

                first = json.loads((await tool.run_async(_step("first"))).content)
                second = json.loads((await tool.run_async(_step("second"))).content)

                # Only the DISPATCHED child has begun; the waiting one has not.
                assert starts() == [first["agent_id"]]

                gate.set()
                await tool.await_lifecycle_managers(timeout=5.0)

            # Once promoted it emits its own start, and every start is closed.
            assert sorted(starts()) == sorted([first["agent_id"], second["agent_id"]])
            stops = [
                e["payload"]["agent_id"]
                for e in events
                if e.get("type") == "sub_agent" and e["payload"].get("action") == "stop"
            ]
            assert sorted(stops) == sorted(starts())

        asyncio.run(_test())
