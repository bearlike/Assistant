#!/usr/bin/env python3
"""A span tree spanning concurrent sub-agents must be walkable root→child.

The defect these cover: every span opened by the engine passed the session's
``trace_context`` — a ``trace_id`` and nothing else — so the SDK minted a random
parent span id, wrapped it in a non-recording span nobody exports, and parented
the observation to it. Every span was therefore an orphan by construction, at
every depth, and per-agent token attribution from a trace alone was impossible:
a child's spans could not be told from a sibling's because neither reached the
root.

Concurrency is load-bearing here, not decoration. A single child would fail on
the orphaning alone, but only concurrent children can catch the other half — a
handoff that keeps the parent id anywhere task-shared cross-links one child's
spans under its sibling, and the tree stays wrong while every span resolves.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import queue
from contextvars import ContextVar
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage
from mewbo_core import components as comp_module
from mewbo_core.agents.agent_context import AgentContext
from mewbo_core.agents.hypervisor import AgentHandle, AgentHypervisor
from mewbo_core.agents.spawn_agent import SpawnAgentTool
from mewbo_core.classes import ActionStep
from mewbo_core.components import langfuse_trace_span
from mewbo_core.config import AppConfig, reset_config, set_app_config_path
from mewbo_core.hooks import HookManager
from mewbo_core.permissions import PermissionDecision, PermissionPolicy
from mewbo_core.tooling.tool_registry import ToolRegistry, ToolSpec

_TRACE_ID = "a" * 32

# What a spawned child's own agent span is called. Named for the AgentDef, so
# every sibling shares it — see the cross-link test for why that forces identity
# checks onto span ids rather than names.
_CHILD_AGENT_SPAN = "invoke_agent subagent"

# The fake tracer's ambient span, held exactly the way OpenTelemetry holds its
# own: in a ContextVar, so ``asyncio.create_task``'s context copy carries it
# into a child task and a sibling task cannot see it. Modelling this with
# anything task-shared would make the cross-linking half of the bug untestable.
_AMBIENT: ContextVar[tuple[str, str] | None] = ContextVar("fake_ambient_span", default=None)


@dataclass(frozen=True)
class _SpanRecord:
    """One observation as it would be exported."""

    name: str
    span_id: str
    parent_span_id: str | None
    trace_id: str


class _FakeTracer:
    """Model the two parenting mechanisms the Langfuse SDK actually implements.

    Verified against the installed SDK's ``start_as_current_observation``:

    - a ``trace_context`` carrying a ``trace_id`` and NO ``parent_span_id``
      parents the span to a freshly generated span id that is never exported —
      reproduced here as a ``phantom:`` id that is deliberately absent from the
      recorded set, which is what makes an orphan detectable;
    - a ``trace_context`` carrying both attaches to the named parent;
    - no ``trace_context`` at all falls through to ordinary ambient nesting.
    """

    def __init__(self) -> None:
        self.records: list[_SpanRecord] = []
        self._ids = itertools.count(1)

    # -- the surface `components.py` consumes -----------------------------

    def get_current_trace_id(self) -> str | None:
        ambient = _AMBIENT.get()
        return ambient[0] if ambient else None

    def get_current_observation_id(self) -> str | None:
        ambient = _AMBIENT.get()
        return ambient[1] if ambient else None

    @contextlib.contextmanager
    def start_as_current_observation(self, *, name: str, trace_context=None, **_kwargs):
        ambient = _AMBIENT.get()
        if trace_context and trace_context.get("trace_id"):
            trace_id = trace_context["trace_id"]
            parent = trace_context.get("parent_span_id") or f"phantom:{next(self._ids)}"
        elif ambient:
            trace_id, parent = ambient
        else:
            trace_id, parent = f"unpinned:{next(self._ids)}", None
        span_id = f"{next(self._ids):016x}"
        self.records.append(_SpanRecord(name, span_id, parent, trace_id))
        token = _AMBIENT.set((trace_id, span_id))
        try:
            yield MagicMock()
        finally:
            _AMBIENT.reset(token)

    # -- assertions the tests read ----------------------------------------

    @property
    def by_id(self) -> dict[str, _SpanRecord]:
        return {record.span_id: record for record in self.records}

    def named(self, prefix: str) -> list[_SpanRecord]:
        return [record for record in self.records if record.name.startswith(prefix)]

    def ancestry(self, record: _SpanRecord) -> list[str]:
        """Span names from *record* up to whatever it hangs off, root last.

        A chain ending in an id absent from the trace is an orphan, and the
        unresolvable id is kept in the chain so a failure names it.
        """
        chain = [record.name]
        seen = {record.span_id}
        parent = record.parent_span_id
        while parent is not None and parent not in seen:
            seen.add(parent)
            found = self.by_id.get(parent)
            if found is None:
                chain.append(f"<unresolved {parent}>")
                break
            chain.append(found.name)
            parent = found.parent_span_id
        return chain


def _allow_all_policy() -> PermissionPolicy:
    policy = MagicMock(spec=PermissionPolicy)
    policy.decide.return_value = PermissionDecision.ALLOW
    return policy


def _hook_manager() -> HookManager:
    hm = MagicMock(spec=HookManager)
    hm.run_pre_tool_use.side_effect = lambda step: step
    hm.run_post_tool_use.side_effect = lambda step, result: result
    hm.run_permission_request.side_effect = lambda step, decision: decision
    hm.run_on_agent_start.return_value = None
    hm.run_on_agent_stop.return_value = None
    return hm


def _tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="shell_tool",
            name="shell_tool",
            description="Test tool",
            factory=lambda: MagicMock(),
            enabled=True,
            kind="local",
            metadata={"schema": {"type": "object", "properties": {}}},
        )
    )
    return registry


@pytest.fixture()
def tracer(tmp_path):
    """Langfuse enabled, a trace pinned, and every SDK edge replaced by the fake."""
    cfg_path = tmp_path / "app.json"
    AppConfig.model_validate(
        {"langfuse": {"enabled": True, "public_key": "pk", "secret_key": "sk"}}
    ).write(cfg_path)
    reset_config()
    set_app_config_path(cfg_path)

    fake = _FakeTracer()
    ctx_token = comp_module._LANGFUSE_TRACE_CONTEXT.set({"trace_id": _TRACE_ID})
    ambient_token = _AMBIENT.set(None)
    with (
        patch("langfuse.get_client", return_value=fake),
        patch("langfuse.propagate_attributes", lambda **_kw: contextlib.nullcontext()),
        # The LangChain callback handler is a live client + exporter: an I/O
        # boundary, and not the seam under test.
        patch("mewbo_core.loop.tool_use_loop.build_langfuse_handler", return_value=None),
    ):
        yield fake
    _AMBIENT.reset(ambient_token)
    comp_module._LANGFUSE_TRACE_CONTEXT.reset(ctx_token)
    reset_config()


def _ancestor_ids(tracer: object, record: object) -> list[str]:
    """Span ids from *record*'s parent upward, stopping at the trace root.

    The name-based ``ancestry`` helper answers "what shape is this chain"; this
    answers "whose subtree is this in", which is a question about identity and
    so cannot be asked of names that siblings share.
    """
    ids: list[str] = []
    by_id = tracer.by_id  # type: ignore[attr-defined]
    current = record
    while True:
        parent = getattr(current, "parent_span_id", None)
        if parent is None or parent not in by_id:
            return ids
        ids.append(parent)
        current = by_id[parent]


async def _spawn_concurrent_children(count: int) -> None:
    """Fan *count* children out of one parent turn, exactly as the loop does.

    The spawn runs inside an ``invoke_agent root`` → ``agent_step`` span pair
    because that is where ``spawn_agent`` executes in production: the parent's
    per-turn span is the observation a child has to be able to name once its
    own task starts.
    """
    hypervisor = AgentHypervisor(max_concurrent=100)
    root_q: queue.Queue[str] = queue.Queue()
    ctx = AgentContext.root(
        model_name="test-model",
        registry=hypervisor,
        message_queue=root_q,
    )
    await hypervisor.register(
        AgentHandle(
            agent_id=ctx.agent_id,
            parent_id=None,
            depth=0,
            model_name=ctx.model_name,
            task_description="root task",
            status="running",
            message_queue=root_q,
        )
    )
    tool = SpawnAgentTool(
        agent_context=ctx,
        tool_registry=_tool_registry(),
        permission_policy=_allow_all_policy(),
        hook_manager=_hook_manager(),
    )

    async def _reply(*_args, **_kwargs) -> AIMessage:
        # Yield the event loop so the children genuinely interleave rather than
        # each running to completion before the next one starts.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return AIMessage(content="child done")

    bound = MagicMock()
    bound.ainvoke = AsyncMock(side_effect=_reply)
    with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as build_model:
        build_model.return_value = MagicMock()
        build_model.return_value.bind_tools.return_value = bound
        with langfuse_trace_span("invoke_agent root", as_type="agent"):
            with langfuse_trace_span("agent_step"):
                for index in range(count):
                    await tool.run_async(
                        ActionStep(
                            tool_id="spawn_agent",
                            operation="set",
                            tool_input={"task": f"child work {index}"},
                        )
                    )
            await tool.await_lifecycle_managers(timeout=10.0)


class TestConcurrentSubAgentSpanTree:
    """The tree two concurrent children build has to be walkable and disjoint."""

    def test_every_span_resolves_to_the_one_root(self, tracer):
        """No observation may name a parent the trace does not contain.

        One exception, and it is structural rather than a defect: the trace's
        own root has to pin a trace id, and pinning one is only expressible by
        handing the SDK a parent it then invents. That phantom lands once, on
        the root, instead of on every span at every depth.
        """
        asyncio.run(_spawn_concurrent_children(2))

        known = set(tracer.by_id)
        orphans = [
            record
            for record in tracer.records
            if record.parent_span_id is not None and record.parent_span_id not in known
        ]
        assert [record.name for record in orphans] == ["invoke_agent root"], (
            "spans whose parent is absent from the trace: "
            f"{[tracer.ancestry(record) for record in orphans]}"
        )

    def test_each_child_agent_span_hangs_off_the_spawning_turn(self, tracer):
        """A child's own span names the parent turn that spawned it."""
        asyncio.run(_spawn_concurrent_children(2))

        child_spans = tracer.named(_CHILD_AGENT_SPAN)
        assert len(child_spans) == 2, [record.name for record in tracer.records]
        for record in child_spans:
            # Sliced at the root: the chain continues one hop into the trace
            # root's own phantom parent, which is the structural exception the
            # resolvability test pins separately.
            assert tracer.ancestry(record)[:3] == [
                record.name,
                "agent_step",
                "invoke_agent root",
            ]

    def test_concurrent_children_do_not_cross_link(self, tracer):
        """Each child's turns sit under ITS OWN agent span, never a sibling's.

        This is the assertion a single-child test cannot make. A handoff that
        parks the captured parent anywhere shared between tasks still produces a
        fully resolvable tree — with one child's turns filed under the other.
        """
        asyncio.run(_spawn_concurrent_children(3))

        child_spans = tracer.named(_CHILD_AGENT_SPAN)
        assert len(child_spans) == 3

        # Matched by span ID, never by span NAME. Every child agent span is now
        # named for its AgentDef, so all three siblings share one name — a
        # name-keyed check would report every turn as belonging to every child
        # and pass no matter how badly the tree was cross-linked. The rename is
        # deliberate (a per-child id in a span name is unbounded cardinality),
        # so identity has to come from the id the tree is actually built on.
        subtrees = {
            record.span_id: [
                turn
                for turn in tracer.named("agent_step")
                if record.span_id in _ancestor_ids(tracer, turn)
            ]
            for record in child_spans
        }
        for child_id, turns in subtrees.items():
            assert turns, f"child {child_id} recorded no turn under its own span"

        claimed = [turn.span_id for turns in subtrees.values() for turn in turns]
        assert len(claimed) == len(set(claimed)), "a turn is filed under two children"

    def test_all_children_share_the_root_trace(self, tracer):
        """A child that starts its own trace is unattributable, resolvable or not."""
        asyncio.run(_spawn_concurrent_children(2))

        assert {record.trace_id for record in tracer.records} == {_TRACE_ID}
