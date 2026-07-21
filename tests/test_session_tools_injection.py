"""Contract test: an injected SessionTool reaches the loop via run_sync."""
from __future__ import annotations

from unittest.mock import patch

from mewbo_core.agent_context import AgentContext
from mewbo_core.classes import ActionStep
from mewbo_core.common import MockSpeaker
from mewbo_core.hypervisor import AgentHypervisor
from mewbo_core.permissions import PermissionPolicy
from mewbo_core.session_runtime import SessionRuntime
from mewbo_core.session_store import SessionStore
from mewbo_core.session_tools import SessionToolFactory, SessionToolRegistry
from mewbo_core.structured_response import EmitStructuredResponseTool
from mewbo_core.tool_use_loop import ToolUseLoop


def test_run_sync_forwards_extra_session_tools_to_orchestrate(tmp_path):
    runtime = SessionRuntime(session_store=SessionStore(root_dir=str(tmp_path)))
    sid = runtime.resolve_session()
    emit = EmitStructuredResponseTool(
        session_id=sid, schema={"type": "object", "properties": {}}
    )
    with patch("mewbo_core.session_runtime.orchestrate_session") as mock_orch:
        mock_orch.return_value = object()
        runtime.run_sync(
            user_query="hi",
            session_id=sid,
            extra_session_tools=[emit],
        )
    _, kwargs = mock_orch.call_args
    assert kwargs["extra_session_tools"] == [emit]


# ---------------------------------------------------------------------------
# A runtime-granted capability surfaces its session tools to the
# ROOT agent — the exact gap that left scg_* missing on re-engagement.
# ---------------------------------------------------------------------------


class _CapGatedSessionTool:
    """A capability-gated SessionTool fixture (stands in for ``scg_memory``)."""

    tool_id = "scg_memory"
    schema = {"type": "function", "function": {"name": "scg_memory"}}

    def __init__(self, *, session_id: str, event_logger=None) -> None:
        self.session_id = session_id

    async def handle(self, action_step: ActionStep) -> MockSpeaker:  # pragma: no cover
        return MockSpeaker(content="ok")

    def should_terminate_run(self) -> bool:
        return False


def _registry_with_gated_tool() -> SessionToolRegistry:
    reg = SessionToolRegistry()
    reg.register(
        SessionToolFactory(
            tool_id="scg_memory",
            build=lambda sid, el: _CapGatedSessionTool(session_id=sid, event_logger=el),
            requires_capabilities=("scg",),
        )
    )
    return reg


def _root_loop(reg: SessionToolRegistry, *, caps: tuple[str, ...]) -> ToolUseLoop:
    """Build a depth-0 loop the way the orchestrator does for a re-engaged run."""
    ctx = AgentContext.root(
        model_name="test-model",
        max_depth=5,
        registry=AgentHypervisor(max_concurrent=10),
        event_logger=None,
    )
    return ToolUseLoop(
        agent_context=ctx,
        tool_registry=None,  # session tools are assembled before any spec binding
        permission_policy=PermissionPolicy(rules=[]),
        hook_manager=None,
        session_tool_registry=reg,
        allowed_tools=None,  # re-engagement carries no explicit scg allowlist
        session_id="reengaged-sess",
        session_capabilities=caps,
    )


def test_runtime_granted_capability_builds_session_tool_for_root_agent():
    """A root run with ``scg`` in its caps (and NO allowlist) gets ``scg_memory``.

    Reproduces at the loop seam: on re-engagement ``allowed_tools`` is None
    (the stored context held no scg entry), but ``_session_capabilities`` unions
    the runtime grant. Pre-fix the tool was absent (allowlist-only gate) and the
    agent answered ``TOOLS-MISSING``; now the capability alone surfaces it.
    """
    loop = _root_loop(_registry_with_gated_tool(), caps=("scg",))
    assert "scg_memory" in {t.tool_id for t in loop._session_tools}


def test_without_capability_session_tool_absent():
    """No ``scg`` capability ⇒ the gated tool stays hidden (negative control)."""
    loop = _root_loop(_registry_with_gated_tool(), caps=())
    assert "scg_memory" not in {t.tool_id for t in loop._session_tools}


# ---------------------------------------------------------------------------
# / df875: an UNCONDITIONAL session tool (schedule_trigger) reaches a
# PERMISSIVE FE root at the loop seam, even with a large mcp_tools allowlist.
# ---------------------------------------------------------------------------


class _UnconditionalSessionTool:
    """A schedule_trigger-shaped unconditional SessionTool fixture."""

    tool_id = "schedule_trigger"
    schema = {"type": "function", "function": {"name": "schedule_trigger"}}

    def __init__(self, *, session_id: str, event_logger=None) -> None:
        self.session_id = session_id

    async def handle(self, action_step: ActionStep) -> MockSpeaker:  # pragma: no cover
        return MockSpeaker(content="ok")

    def should_terminate_run(self) -> bool:
        return False


def _registry_with_unconditional() -> SessionToolRegistry:
    reg = SessionToolRegistry()
    reg.register(
        SessionToolFactory(
            tool_id="schedule_trigger",
            build=lambda sid, el: _UnconditionalSessionTool(
                session_id=sid, event_logger=el
            ),
            unconditional=True,
        )
    )
    return reg


def _scoped_root_loop(
    reg: SessionToolRegistry,
    *,
    allowed_tools,
    strict_tool_scope: bool,
) -> ToolUseLoop:
    """A depth-0 loop with an explicit allowlist + scope, as the orchestrator builds it."""
    ctx = AgentContext.root(
        model_name="test-model",
        max_depth=5,
        registry=AgentHypervisor(max_concurrent=10),
        event_logger=None,
    )
    return ToolUseLoop(
        agent_context=ctx,
        tool_registry=None,
        permission_policy=PermissionPolicy(rules=[]),
        hook_manager=None,
        session_tool_registry=reg,
        allowed_tools=allowed_tools,
        strict_tool_scope=strict_tool_scope,
        session_id="fe-root-sess",
        session_capabilities=(),
    )


def test_permissive_fe_root_binds_unconditional_tool_despite_mcp_allowlist():
    """THE Aura regression at the loop seam.

    The console/Aura root is PERMISSIVE (``strict_tool_scope=False``) yet always
    carries a large ``context.mcp_tools`` allowlist that never lists built-ins.
    ``schedule_trigger`` (unconditional) MUST still be bound, or Aura's "set an
    alarm in N minutes" flow — which arms a time trigger through it — breaks.
    """
    mcp_only = [f"mcp__server__tool_{i}" for i in range(139)]
    loop = _scoped_root_loop(
        _registry_with_unconditional(),
        allowed_tools=mcp_only,
        strict_tool_scope=False,
    )
    assert "schedule_trigger" in {t.tool_id for t in loop._session_tools}


def test_strict_scoped_agent_omitting_it_does_not_bind_it():
    """A STRICT AgentDef scope (authoritative) that omits it does NOT bind it."""
    loop = _scoped_root_loop(
        _registry_with_unconditional(),
        allowed_tools=["read_file"],
        strict_tool_scope=True,
    )
    assert "schedule_trigger" not in {t.tool_id for t in loop._session_tools}


# ---------------------------------------------------------------------------
# Phase 1a: capability_mode gates SESSION tools at the loop seam too
# (the two-surface fix). A read_only spawn's loop must bind no write-tier
# session action tool; execute/all stay byte-identical.
# ---------------------------------------------------------------------------


class _WriteSessionTool:
    """A submit/mint-shaped session ACTION (default execute tier)."""

    tool_id = "submit_page"
    schema = {"type": "function", "function": {"name": "submit_page"}}

    def __init__(self, *, session_id: str, event_logger=None) -> None:
        self.session_id = session_id

    async def handle(self, action_step: ActionStep) -> MockSpeaker:  # pragma: no cover
        return MockSpeaker(content="ok")

    def should_terminate_run(self) -> bool:
        return False


class _ReadSessionTool:
    """A read-only session tool that explicitly declares tier ``read``."""

    tool_id = "read_notes"
    schema = {"type": "function", "function": {"name": "read_notes"}}

    def __init__(self, *, session_id: str, event_logger=None) -> None:
        self.session_id = session_id

    async def handle(self, action_step: ActionStep) -> MockSpeaker:  # pragma: no cover
        return MockSpeaker(content="ok")

    def should_terminate_run(self) -> bool:
        return False


def _registry_write_and_read() -> SessionToolRegistry:
    reg = SessionToolRegistry()
    reg.register(
        SessionToolFactory(
            tool_id="submit_page",  # default tier -> execute (write action)
            build=lambda sid, el: _WriteSessionTool(session_id=sid, event_logger=el),
        )
    )
    reg.register(
        SessionToolFactory(
            tool_id="read_notes",
            build=lambda sid, el: _ReadSessionTool(session_id=sid, event_logger=el),
            capability="read",
        )
    )
    return reg


def _sub_loop_with_mode(
    reg: SessionToolRegistry, *, capability_mode: str, allowed_tools
) -> ToolUseLoop:
    """A spawned sub-agent loop (depth 1) whose effective ceiling is ``capability_mode``.

    Root is always ``all``; ``child(capability_mode=...)`` narrows exactly as the
    spawn path does, so the depth-1 context carries the requested ceiling. Only
    ``build_for`` session tools land in ``_session_tools`` at depth>0 (the inline
    ExitPlanMode/UpdateTodos are root-only), so the list is the gated set alone.
    """
    root = AgentContext.root(
        model_name="test-model",
        max_depth=5,
        registry=AgentHypervisor(max_concurrent=10),
        event_logger=None,
    )
    child = root.child(capability_mode=capability_mode)
    return ToolUseLoop(
        agent_context=child,
        tool_registry=None,
        permission_policy=PermissionPolicy(rules=[]),
        hook_manager=None,
        session_tool_registry=reg,
        allowed_tools=allowed_tools,
        session_id="sub-sess",
        session_capabilities=(),
    )


def test_read_only_spawn_binds_no_write_session_tools():
    """(a) A read_only spawn's loop binds ZERO write-tier session tools."""
    loop = _sub_loop_with_mode(
        _registry_write_and_read(),
        capability_mode="read_only",
        allowed_tools=["submit_page"],  # named, but read_only strips it
    )
    assert {t.tool_id for t in loop._session_tools} == set()


def test_read_only_spawn_binds_declared_read_session_tool():
    """(c) A session tool that declares tier 'read' survives a read_only spawn."""
    loop = _sub_loop_with_mode(
        _registry_write_and_read(),
        capability_mode="read_only",
        allowed_tools=["submit_page", "read_notes"],
    )
    assert {t.tool_id for t in loop._session_tools} == {"read_notes"}


def test_execute_and_all_spawns_bind_identical_session_tool_set():
    """(b) execute/all spawns bind the identical session-tool set (byte-identical)."""
    allowed = ["submit_page", "read_notes"]
    execute = {
        t.tool_id
        for t in _sub_loop_with_mode(
            _registry_write_and_read(), capability_mode="execute", allowed_tools=allowed
        )._session_tools
    }
    all_mode = {
        t.tool_id
        for t in _sub_loop_with_mode(
            _registry_write_and_read(), capability_mode="all", allowed_tools=allowed
        )._session_tools
    }
    assert execute == all_mode == {"submit_page", "read_notes"}
