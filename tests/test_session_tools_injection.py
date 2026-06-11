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
# Gitea #84: a runtime-granted capability surfaces its session tools to the
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

    Reproduces #84 at the loop seam: on re-engagement ``allowed_tools`` is None
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
