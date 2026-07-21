#!/usr/bin/env python3
"""Tests for the provenance hash chain.

Coverage
--------
- ``AttestationChain`` integrity: a healthy chain verifies; a tampered
  record's hash mismatch and a reordered chain's broken link are both caught.
- The real ``SpawnAgentTool`` path, 2 levels deep (root -> B -> C): every
  spawn/terminal seam records, in chain order, with correct lineage, and the
  parent's ``AgentResult.attestation_hash`` matches the child's own terminal
  record.
- Best-effort isolation: a raising event_logger degrades to a no-op — the
  spawn itself is unaffected.
- The privacy/exfil law: no task text or raw summary text ever rides a
  record; only a sha256 fingerprint.
- The kill switch: no ``AttestationChain`` wired (``attestation=None``, the
  historical default) -> zero attestation events, byte-identical spawn.

Patterns mirror ``tests/test_spawn_agent_flow.py`` / ``test_spawn_agent_retry.py``:
stub only the model boundary (``build_chat_model`` -> ``bound.ainvoke``), drive
the real ``AgentHypervisor``/``AgentContext``/``SpawnAgentTool``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage
from mewbo_core.agent_context import AgentContext
from mewbo_core.attestation import (
    GENESIS_HASH,
    AttestationChain,
    ContractSnapshot,
    SpawnAttestation,
    TerminalAttestation,
)
from mewbo_core.classes import ActionStep
from mewbo_core.hooks import HookManager
from mewbo_core.hypervisor import AgentHypervisor
from mewbo_core.permissions import PermissionDecision, PermissionPolicy
from mewbo_core.spawn_agent import SpawnAgentTool
from mewbo_core.tool_registry import ToolRegistry, ToolSpec

# ---------------------------------------------------------------------------
# Shared helpers (mirror tests/test_spawn_agent_flow.py)
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


def _text_response(content: str) -> AIMessage:
    return AIMessage(content=content)


def _tool_call_response(tool_id: str, args: dict, call_id: str = "call_1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": tool_id, "args": args, "id": call_id}])


def _step(task: str, **extra) -> ActionStep:
    return ActionStep(tool_id="spawn_agent", operation="set", tool_input={"task": task, **extra})


def _make_spawn_tool(ctx: AgentContext) -> SpawnAgentTool:
    return SpawnAgentTool(
        agent_context=ctx,
        tool_registry=_make_registry("shell_tool"),
        permission_policy=_allow_all_policy(),
        hook_manager=_make_hook_manager(),
    )


def _attestation_events(events: list) -> list:
    return [e for e in events if e.get("type") == "attestation"]


# ===========================================================================
# (a) Chain integrity — pure, no I/O
# ===========================================================================


class TestChainIntegrity:
    def test_healthy_chain_verifies_ok(self):
        events: list = []
        chain = AttestationChain(session_id="s1")
        chain.record_spawn(
            events.append,
            agent_id="a",
            parent_id=None,
            depth=1,
            agent_type=None,
            model="m",
            capability_mode="all",
            contract=None,
        )
        chain.record_terminal(
            events.append,
            agent_id="a",
            parent_id=None,
            depth=1,
            terminal_state="completed",
            spawn_hash=chain.head,
            attempts=1,
            steps_completed=2,
            input_tokens=10,
            output_tokens=20,
            summary_kind="generic",
            summary_text="done",
            done_reason="completed",
        )
        chain.record_spawn(
            events.append,
            agent_id="b",
            parent_id="a",
            depth=2,
            agent_type=None,
            model="m",
            capability_mode="all",
            contract=None,
        )

        payloads = [e["payload"] for e in events]
        assert len(payloads) == 3
        result = AttestationChain.verify(payloads)
        assert result.ok is True
        assert result.broken_index is None

    def test_mutated_record_reports_hash_mismatch(self):
        events: list = []
        chain = AttestationChain(session_id="s1")
        chain.record_spawn(
            events.append, agent_id="a", parent_id=None, depth=1, agent_type=None,
            model="m", capability_mode="all", contract=None,
        )
        chain.record_terminal(
            events.append, agent_id="a", parent_id=None, depth=1, terminal_state="completed",
            spawn_hash=chain.head, attempts=1, steps_completed=1, input_tokens=0,
            output_tokens=0, summary_kind="generic", summary_text="x", done_reason=None,
        )
        chain.record_spawn(
            events.append, agent_id="b", parent_id="a", depth=2, agent_type=None,
            model="m", capability_mode="all", contract=None,
        )

        payloads = [dict(e["payload"]) for e in events]
        # Tamper with the middle record's body — its stored record_hash no
        # longer matches a recomputation over the mutated fields.
        payloads[1]["attempts"] = 99

        result = AttestationChain.verify(payloads)
        assert result.ok is False
        assert result.broken_index == 1
        assert result.reason == "hash_mismatch"

    def test_reordered_chain_reports_link_break(self):
        events: list = []
        chain = AttestationChain(session_id="s1")
        chain.record_spawn(
            events.append, agent_id="a", parent_id=None, depth=1, agent_type=None,
            model="m", capability_mode="all", contract=None,
        )
        chain.record_terminal(
            events.append, agent_id="a", parent_id=None, depth=1, terminal_state="completed",
            spawn_hash=chain.head, attempts=1, steps_completed=1, input_tokens=0,
            output_tokens=0, summary_kind="generic", summary_text="x", done_reason=None,
        )
        chain.record_spawn(
            events.append, agent_id="b", parent_id="a", depth=2, agent_type=None,
            model="m", capability_mode="all", contract=None,
        )

        payloads = [e["payload"] for e in events]
        reordered = [payloads[1], payloads[0], payloads[2]]

        result = AttestationChain.verify(reordered)
        assert result.ok is False
        assert result.broken_index == 0
        assert result.reason == "link_break"


# ===========================================================================
# (b) Real 2-level spawn tree: root -> B -> C
# ===========================================================================


class TestNestedSpawnTree:
    def test_root_to_b_to_c_chain_order_and_lineage(self):
        async def _test():
            events: list = []
            hv = AgentHypervisor(
                max_concurrent=100,
                attestation=AttestationChain(session_id="sess-1"),
            )
            root_ctx = AgentContext.root(
                model_name="test-model", max_depth=5, registry=hv, event_logger=events.append,
            )
            tool = _make_spawn_tool(root_ctx)

            # Chronological, single-threaded script:
            #  1. B's first turn -> delegates to C via spawn_agent.
            #  2. C's only turn  -> natural completion (no tools).
            #  3. B's second turn -> natural completion, tool result fed back.
            call_log: list[int] = []

            def _script(*_args, **_kwargs):
                call_log.append(1)
                n = len(call_log)
                if n == 1:
                    return _tool_call_response("spawn_agent", {"task": "C task"}, "call_c")
                if n == 2:
                    return _text_response("C done")
                return _text_response("B done")

            bound = MagicMock()
            bound.ainvoke = AsyncMock(side_effect=_script)

            with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound
                outcome = await tool.run_async(_step("B task"))
                await tool.await_lifecycle_managers(timeout=5.0)

            b_agent_id = json.loads(outcome.content)["agent_id"]

            att_events = _attestation_events(events)
            assert len(att_events) == 4
            phases = [e["payload"]["phase"] for e in att_events]
            assert phases == ["spawn", "spawn", "terminal", "terminal"]

            b_spawn, c_spawn, c_terminal, b_terminal = (e["payload"] for e in att_events)
            assert b_spawn["agent_id"] == b_agent_id
            assert b_spawn["parent_id"] == root_ctx.agent_id
            assert c_spawn["parent_id"] == b_agent_id
            assert c_terminal["agent_id"] == c_spawn["agent_id"]
            assert c_terminal["parent_id"] == b_agent_id
            assert c_terminal["spawn_hash"] == c_spawn["record_hash"]
            assert b_terminal["agent_id"] == b_agent_id
            assert b_terminal["parent_id"] == root_ctx.agent_id
            assert b_terminal["spawn_hash"] == b_spawn["record_hash"]

            # B's own AgentResult (stored on its handle by the non-blocking
            # lifecycle manager) carries the SAME hash as its terminal record.
            b_handle = await hv.get(b_agent_id)
            assert b_handle is not None
            assert b_handle.result is not None
            assert b_handle.result.attestation_hash == b_terminal["record_hash"]

            # The whole persisted prefix verifies.
            result = AttestationChain.verify([e["payload"] for e in att_events])
            assert result.ok is True

        asyncio.run(_test())


# ===========================================================================
# (c) Best-effort failure isolation
# ===========================================================================


class TestFailureIsolation:
    def test_raising_event_logger_never_breaks_the_spawn(self):
        """A sink that raises on attestation writes still lets the spawn complete."""

        async def _test():
            def _flaky_logger(event):
                if event.get("type") == "attestation":
                    raise RuntimeError("provenance sink is down")
                # Non-attestation events (sub_agent lifecycle) still land.

            hv = AgentHypervisor(
                max_concurrent=100,
                attestation=AttestationChain(session_id="sess-1"),
            )
            root_ctx = AgentContext.root(
                model_name="test-model", max_depth=5, registry=hv, event_logger=_flaky_logger,
            )
            child_ctx = root_ctx.child()  # depth=1 -> blocking path, inherits event_logger
            tool = _make_spawn_tool(child_ctx)

            bound = MagicMock()
            bound.ainvoke = AsyncMock(return_value=_text_response("Done!"))
            with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound
                result = await tool.run_async(_step("do work"))

            parsed = json.loads(result.content)
            assert parsed["status"] == "completed"
            assert parsed["attestation_hash"] == ""
            # Head never advanced — both the spawn and terminal writes failed.
            assert hv.attestation.head == GENESIS_HASH

        asyncio.run(_test())

    def test_record_spawn_logs_once_and_returns_empty_on_failure(self):
        """Unit-level: a bad event_logger degrades to '' with exactly one log line."""
        chain = AttestationChain(session_id="s1")

        def _boom(_event):
            raise RuntimeError("sink down")

        with patch("mewbo_core.attestation.logging") as mock_logging:
            result = chain.record_spawn(
                _boom, agent_id="a", parent_id=None, depth=1, agent_type=None,
                model="m", capability_mode="all", contract=None,
            )
        assert result == ""
        assert chain.head == GENESIS_HASH
        mock_logging.warning.assert_called_once()

    def test_record_terminal_with_no_event_logger_bound(self):
        """``event_logger=None`` (no sink at all) is also best-effort, not a raise."""
        chain = AttestationChain(session_id="s1")
        result = chain.record_terminal(
            None, agent_id="a", parent_id=None, depth=1, terminal_state="completed",
            spawn_hash=GENESIS_HASH, attempts=1, steps_completed=1, input_tokens=0,
            output_tokens=0, summary_kind="generic", summary_text="x", done_reason=None,
        )
        assert result == ""
        assert chain.head == GENESIS_HASH


# ===========================================================================
# (e) Privacy/exfil law — no task text, no raw summary text
# ===========================================================================


class TestExclusionGuard:
    def test_no_task_or_summary_text_on_the_wire(self):
        events: list = []
        chain = AttestationChain(session_id="s1")
        chain.record_spawn(
            events.append, agent_id="a", parent_id=None, depth=1,
            agent_type="secret-agent-def", model="m", capability_mode="all", contract=None,
        )
        secret_summary = "SUPER_SECRET_TASK_DETAIL_do_not_leak_this_string"
        chain.record_terminal(
            events.append, agent_id="a", parent_id=None, depth=1, terminal_state="completed",
            spawn_hash=chain.head, attempts=1, steps_completed=3, input_tokens=1,
            output_tokens=1, summary_kind="generic", summary_text=secret_summary,
            done_reason="completed",
        )

        # Field-level guard: neither record model declares a task/summary field.
        assert "task" not in SpawnAttestation.model_fields
        assert "summary" not in TerminalAttestation.model_fields

        # Wire-level guard: the secret never appears anywhere in either payload.
        for event in events:
            blob = json.dumps(event["payload"])
            assert secret_summary not in blob

        terminal_payload = events[1]["payload"]
        expected_hash = hashlib.sha256(secret_summary.encode("utf-8")).hexdigest()
        assert terminal_payload["summary_hash"] == expected_hash

    def test_contract_snapshot_is_bounded_scalars_only(self):
        snapshot = ContractSnapshot.from_contract(None)
        assert snapshot.max_steps == 0
        assert snapshot.autonomy == "open_ended"


# ===========================================================================
# (f) Kill switch — no chain wired -> zero attestation events
# ===========================================================================


class TestKillSwitch:
    def test_no_chain_wired_is_byte_identical_legacy_path(self):
        async def _test():
            events: list = []
            hv = AgentHypervisor(max_concurrent=100)  # attestation defaults to None
            assert hv.attestation is None
            root_ctx = AgentContext.root(
                model_name="test-model", max_depth=5, registry=hv, event_logger=events.append,
            )
            child_ctx = root_ctx.child()  # depth=1 -> blocking path
            tool = _make_spawn_tool(child_ctx)

            bound = MagicMock()
            bound.ainvoke = AsyncMock(return_value=_text_response("Done!"))
            with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound
                result = await tool.run_async(_step("do work"))

            parsed = json.loads(result.content)
            assert parsed["status"] == "completed"
            assert parsed["attestation_hash"] == ""
            assert _attestation_events(events) == []

        asyncio.run(_test())
