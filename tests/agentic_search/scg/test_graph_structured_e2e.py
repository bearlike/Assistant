"""End-to-end: a graph-first structured run terminates in a validated emit.

The #77 acceptance contract for deliverable 2 — a structured run bound to a
search workspace stays an ORDINARY agentic session (the real ``StructuredResponder``
/ ``ToolUseLoop``, NOT a separate path) but is granted the ``scg`` capability +
graph traversal tools + the workspace source scope, and terminates in the
schema-validated ``emit_result``. Drives the REAL ToolUseLoop with a fake LLM at
the model seam: turn 1 calls ``scg_route`` (graph consulted), turn 2 calls
``emit_result`` (validated terminal). Asserts the scope was bound DURING the
drive and the emitted object is returned.

Stub boundary: only ``model.ainvoke`` (the LLM). The StructuredResponder, the
emit tool, the scope binding, and the loop are all real.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage
from mewbo_api.agentic_search.scg.workspace_binding import WorkspaceGraphBinding
from mewbo_api.agentic_search.schemas import Workspace
from mewbo_core.agent_context import AgentContext
from mewbo_core.context import ContextSnapshot
from mewbo_core.hooks import HookManager
from mewbo_core.hypervisor import AgentHypervisor
from mewbo_core.permissions import PermissionDecision, PermissionPolicy
from mewbo_core.structured_response import StructuredResponder
from mewbo_core.token_budget import TokenBudget
from mewbo_core.tool_registry import ToolRegistry
from mewbo_core.tool_use_loop import ToolUseLoop
from mewbo_graph.scg.scope import ScgScope

_OWNER_SCHEMA = {
    "type": "object",
    "properties": {"owner": {"type": "string"}},
    "required": ["owner"],
    "additionalProperties": False,
}


def _ctx() -> ContextSnapshot:
    return ContextSnapshot(
        summary=None,
        recent_events=[],
        selected_events=None,
        events=[],
        budget=TokenBudget(
            total_tokens=0, summary_tokens=0, event_tokens=0, context_window=128000,
            remaining_tokens=128000, utilization=0.0, threshold=0.8,
        ),
    )


def _tool_call(tool_id: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": tool_id, "args": args, "id": call_id}])


class _GraphFirstRuntime:
    """Drives the REAL ToolUseLoop and records the scope active during the drive.

    Mirrors ``test_structured_grounded_integration._RunWithRedriveRuntime`` but
    captures ``ScgScope.allowed()`` inside ``run_sync`` so the test proves the
    workspace source scope was bound around the drive (the graph-first invariant).
    A fake ``scg_route`` SessionTool stands in for the (graph) tool so the loop's
    first turn is a real route call without a live SCG engine.
    """

    def __init__(self, responses: list[AIMessage]) -> None:
        self._queue = list(responses)
        self.context_events: list[dict] = []
        self.tags: list[str] = []
        self.run_calls = 0
        self.scope_during_run: frozenset[str] | None = None
        self.skill_instructions: str | None = None

    def resolve_session(self, *, session_tag=None, session_id=None) -> str:
        return "sess-gf"

    def tag_session(self, session_id: str, tag: str) -> None:
        self.tags.append(tag)

    def append_context_event(self, session_id: str, context: dict) -> None:
        self.context_events.append(context)

    def run_sync(self, **kwargs: Any) -> object:
        self.run_calls += 1
        self.scope_during_run = ScgScope.allowed()
        self.skill_instructions = kwargs.get("skill_instructions")
        extra_tools = list(kwargs.get("extra_session_tools", []))

        queue = self._queue

        async def _ainvoke(messages, **_kw):  # noqa: ANN001
            return queue.pop(0) if queue else AIMessage(content="(done)")

        bound = MagicMock()
        bound.ainvoke = _ainvoke
        policy = MagicMock(spec=PermissionPolicy)
        policy.decide.return_value = PermissionDecision.ALLOW
        hm = MagicMock(spec=HookManager)
        hm.run_pre_tool_use.side_effect = lambda step: step
        hm.run_post_tool_use.side_effect = lambda step, result: result
        hm.run_permission_request.side_effect = lambda step, decision: decision

        with patch("mewbo_core.tool_use_loop.build_chat_model") as build:
            build.return_value = MagicMock()
            build.return_value.bind_tools.return_value = bound
            loop = ToolUseLoop(
                agent_context=AgentContext.root(
                    model_name="test-model", max_depth=5, should_cancel=None,
                    registry=AgentHypervisor(max_concurrent=100), event_logger=None,
                ),
                tool_registry=ToolRegistry(),
                permission_policy=policy,
                hook_manager=hm,
                session_id=kwargs.get("session_id", "sess-gf"),
                extra_session_tools=extra_tools,
            )
            asyncio.run(
                loop.run(
                    kwargs.get("user_query", ""),
                    tool_specs=ToolRegistry().list_specs(),
                    context=_ctx(),
                )
            )
        return object()

    def start_command(self, session_id: str, target) -> bool:
        target(threading.Event())
        return True


class _FakeRouteTool:
    """A minimal ``scg_route`` SessionTool standing in for the graph tool."""

    tool_id = "scg_route"
    modes = frozenset({"act"})
    schema = {
        "type": "function",
        "function": {
            "name": "scg_route",
            "description": "Route a query to ranked SCG pathways.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "k": {"type": "integer"},
                },
                "required": ["query"],
            },
        },
    }

    def __init__(self) -> None:
        self.called = False

    def should_terminate_run(self) -> bool:
        return False  # only emit_result terminates the run

    async def handle(self, action_step) -> Any:  # noqa: ANN001
        from mewbo_core.common import MockSpeaker

        self.called = True
        return MockSpeaker(content="recipes: [github#search_issues] memory_hints: []")


def test_graph_first_structured_routes_then_emits() -> None:
    """route (graph consulted) → emit_result (validated terminal); scope bound.

    Turn 1: the model calls ``scg_route`` — proves the graph is consulted.
    Turn 2: the model calls ``emit_result`` with a valid object — the terminal.
    The run returns the validated object; the workspace scope was bound during
    the drive (the graph-first invariant).
    """
    ws = Workspace(id="ws-1", name="Eng", sources=["github", "linear"])
    binding = WorkspaceGraphBinding.for_workspace(ws, ["mcp_github_search"])

    responses = [
        _tool_call("scg_route", {"query": "who owns billing", "k": 3}, "c1"),
        _tool_call("emit_result", {"owner": "team-payments"}, "c2"),
    ]
    runtime = _GraphFirstRuntime(responses)
    route_tool = _FakeRouteTool()

    responder = StructuredResponder(
        runtime=runtime,
        schema=_OWNER_SCHEMA,
        workspace=ws.id,
        allowed_tools=binding.allowed_tools(),
        capabilities=binding.capabilities,
        context_events=binding.context_events,
        extra_instructions="GRAPH-FIRST: route then emit.",
        scope_factory=binding.scope,
    )

    # Inject the fake route tool into the loop by extending the responder's drive
    # extra tools: patch _drive to add the route tool alongside the emit tool.
    original_run_sync = runtime.run_sync

    def _run_sync_with_route(**kwargs: Any) -> object:
        kwargs["extra_session_tools"] = [*kwargs.get("extra_session_tools", []), route_tool]
        return original_run_sync(**kwargs)

    runtime.run_sync = _run_sync_with_route  # type: ignore[method-assign]

    out = responder.run("Who owns the billing service?")

    assert out == {"owner": "team-payments"}, "the validated structured object is returned"
    assert route_tool.called is True, "the graph (scg_route) was consulted before emit"
    # The workspace source scope was bound DURING the drive (graph-first invariant).
    assert runtime.scope_during_run == frozenset({"github", "linear"})
    # The scope reset after the drive (no leak).
    assert ScgScope.allowed() is None
    # The structured provenance tag was stamped (#78 seam, reused) — unique per
    # session (``structured:run:<id>``), never the bare prefix (#87).
    assert "structured:run:sess-gf" in runtime.tags
    # The scg capability + the graph-first playbook reached the session/prompt.
    caps = [c["client_capabilities"] for c in runtime.context_events if "client_capabilities" in c]
    assert caps and "scg" in caps[0]
    assert runtime.skill_instructions and "GRAPH-FIRST" in runtime.skill_instructions
