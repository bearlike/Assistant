#!/usr/bin/env python3
"""Contract tests for honest terminal state.

Three seams, one contract: a run that stopped short must SAY so.

- ``session_runtime.summarize_session`` — the status derivation. ``unmet_goal``
  and ``blocked`` are the two arms that must not fall through to ``completed``;
  falling through takes the recovery affordance with them.
- ``orchestrator`` — the completion payload, which must be able to CONTRADICT
  the status it ships beside, plus the promise-as-completion gate.
- ``spawn_agent`` — the child terminal a parent is told about, the spawn-time
  model fallback, and the retry contract's stopped-short path.

Every test drives the real object from its caller's seam. The only stub is the
child loop itself (the model boundary) and, for the store, a real on-disk
``SessionStore`` under ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import json
from typing import get_args, get_type_hints
from unittest.mock import AsyncMock, MagicMock

import pytest
from mewbo_core.agents.agent_context import AgentContext
from mewbo_core.agents.hypervisor import AgentHypervisor
from mewbo_core.agents.spawn_agent import SettledStatus, SpawnAgentTool
from mewbo_core.classes import (
    CANCELLED_DONE_REASONS,
    UNACHIEVED_DONE_REASONS,
    OrchestrationState,
    TaskQueue,
)
from mewbo_core.hooks import HookManager, OutcomeAssertion
from mewbo_core.loop.orchestrator import Orchestrator
from mewbo_core.loop.session_runtime import SessionRuntime
from mewbo_core.permissions import PermissionDecision, PermissionPolicy
from mewbo_core.session.session_store import SessionStore
from mewbo_core.tooling.tool_registry import ToolRegistry, ToolSpec
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _runtime(tmp_path) -> tuple[SessionRuntime, str]:
    """A real store-backed runtime plus a session that already has a user turn.

    ``has_user_event`` gates ``recoverable``, so every status test needs one.
    """
    runtime = SessionRuntime(session_store=SessionStore(root_dir=str(tmp_path)))
    session_id = runtime.resolve_session()
    runtime.append_event(session_id, {"type": "user", "payload": {"text": "do the thing"}})
    return runtime, session_id


def _complete(runtime: SessionRuntime, session_id: str, **payload) -> dict:
    """Append a completion event and return the resulting summary."""
    body = {"done": True, "done_reason": "completed", "task_result": "", **payload}
    runtime.append_event(session_id, {"type": "completion", "payload": body})
    return runtime.summarize_session(session_id)


def _spec(tool_id: str = "shell_tool") -> ToolSpec:
    return ToolSpec(
        tool_id=tool_id,
        name=tool_id,
        description=f"Test tool {tool_id}",
        factory=lambda: MagicMock(),
        enabled=True,
        kind="local",
        metadata={"schema": {"type": "object", "properties": {}}},
    )


def _spawn_tool(ctx: AgentContext) -> SpawnAgentTool:
    registry = ToolRegistry()
    registry.register(_spec())
    policy = MagicMock(spec=PermissionPolicy)
    policy.decide.return_value = PermissionDecision.ALLOW
    hooks = MagicMock(spec=HookManager)
    hooks.run_on_agent_start.return_value = None
    hooks.run_on_agent_stop.return_value = None
    return SpawnAgentTool(
        agent_context=ctx,
        tool_registry=registry,
        permission_policy=policy,
        hook_manager=hooks,
    )


def _delegating_ctx(hv: AgentHypervisor, model_name: str = "model-a") -> AgentContext:
    """A depth-1 context, so ``_spawn_one`` takes the BLOCKING path.

    A depth-0 root hands its child to the background lifecycle manager and
    returns ``submitted`` at once — the other terminal seam, covered separately
    by :class:`TestNonBlockingRootTerminal`.
    """
    root = AgentContext.root(model_name=model_name, registry=hv)
    return root.child(model_name=model_name)


def _settled(done_reason: str, *, task_result: str = "", **state_kwargs):
    """A ``(tq, state)`` pair as a child loop that ran to a terminal returns it."""
    tq = TaskQueue(_human_message="task", action_steps=[])
    tq.task_result = task_result
    state = OrchestrationState(goal="task", done=True, done_reason=done_reason, **state_kwargs)
    return tq, state


def _stub_child_loop(tool: SpawnAgentTool, *outcomes) -> list[int]:
    """Stub the child-loop boundary; each call returns the next outcome.

    Stubs the MODEL side only — ``_drive_with_retry``, the terminal projection,
    ``mark_done`` and the event emission all still run for real.
    """
    calls: list[int] = []

    def _build(*_args, **_kwargs):
        idx = len(calls)
        calls.append(idx)
        outcome = outcomes[min(idx, len(outcomes) - 1)]
        loop = MagicMock()
        if isinstance(outcome, BaseException):
            loop.run = AsyncMock(side_effect=outcome)
        else:
            loop.run = AsyncMock(return_value=outcome)
        return loop

    tool._build_child_loop = _build  # type: ignore[method-assign]
    return calls


# ===========================================================================
# The two new derived statuses
# ===========================================================================


class TestUnmetGoalStatus:
    """A halt is not a completion."""

    @pytest.mark.parametrize(
        "done_reason", ["halted_no_progress", "verification_failed", "unmet_goal"]
    )
    def test_halt_derives_unmet_goal(self, tmp_path, done_reason):
        runtime, session_id = _runtime(tmp_path)
        summary = _complete(runtime, session_id, done_reason=done_reason)
        assert summary["status"] == "unmet_goal"

    def test_unmet_goal_is_recoverable(self, tmp_path):
        """The whole user-visible win: a laundered run had lost this."""
        runtime, session_id = _runtime(tmp_path)
        summary = _complete(runtime, session_id, done_reason="halted_no_progress")
        assert summary["recoverable"] is True

    def test_halt_used_to_read_as_completed(self, tmp_path):
        """Regression pin: ``done=True`` alone must never mean success."""
        runtime, session_id = _runtime(tmp_path)
        summary = _complete(runtime, session_id, done=True, done_reason="verification_failed")
        assert summary["status"] != "completed"


class TestBlockedStatus:
    """An unrecovered environment failure is user-actionable, not just failed."""

    @pytest.mark.parametrize(
        "code", ["repo_access", "network", "forbidden", "quota_exceeded"]
    )
    def test_blocked_code_derives_blocked(self, tmp_path, code):
        runtime, session_id = _runtime(tmp_path)
        summary = _complete(runtime, session_id, done_reason="completed", blocked_code=code)
        assert summary["status"] == "blocked"
        assert summary["blocked_code"] == code

    def test_blocked_is_recoverable(self, tmp_path):
        runtime, session_id = _runtime(tmp_path)
        summary = _complete(
            runtime, session_id, done_reason="completed", blocked_code="repo_access"
        )
        assert summary["recoverable"] is True

    def test_blocked_outranks_the_reason_derived_status(self, tmp_path):
        """The specific, actionable fact must not be masked by the coarse one."""
        runtime, session_id = _runtime(tmp_path)
        summary = _complete(
            runtime, session_id, done_reason="halted_no_progress", blocked_code="network"
        )
        assert summary["status"] == "blocked"

    def test_unknown_code_cannot_widen_the_vocabulary(self, tmp_path):
        runtime, session_id = _runtime(tmp_path)
        summary = _complete(
            runtime, session_id, done_reason="completed", blocked_code="something_else"
        )
        assert summary["status"] == "completed"
        assert "blocked_code" not in summary


class TestExistingStatusesUnchanged:
    """The new arms must not disturb the vocabulary that already worked."""

    @pytest.mark.parametrize(
        ("done_reason", "expected"),
        [
            ("completed", "completed"),
            ("canceled", "canceled"),
            ("error", "failed"),
            ("compact_failed", "failed"),
            ("command_failed:compact", "failed"),
            ("max_steps_reached", "incomplete"),
            ("budget_exhausted", "incomplete"),
            ("halted_agent_budget", "incomplete"),
            ("awaiting_approval", "awaiting_approval"),
        ],
    )
    def test_status_table(self, tmp_path, done_reason, expected):
        runtime, session_id = _runtime(tmp_path)
        assert _complete(runtime, session_id, done_reason=done_reason)["status"] == expected

    def test_not_done_is_incomplete(self, tmp_path):
        runtime, session_id = _runtime(tmp_path)
        summary = _complete(runtime, session_id, done=False, done_reason=None)
        assert summary["status"] == "incomplete"

    def test_completed_stays_terminal(self, tmp_path):
        runtime, session_id = _runtime(tmp_path)
        assert _complete(runtime, session_id)["recoverable"] is False


class TestOneTerminalGoverns:
    """A stale synthetic terminal must not blend into the real one."""

    def test_last_completion_wins_whole(self, tmp_path):
        runtime, session_id = _runtime(tmp_path)
        # A boot sweep judged the run dead and stamped it; the run was alive.
        runtime.append_event(
            session_id,
            {
                "type": "completion",
                "payload": {
                    "done": True,
                    "done_reason": "error",
                    "task_result": "",
                    "error": "interrupted: process restart",
                    "blocked_code": "network",
                },
            },
        )
        summary = _complete(runtime, session_id, done_reason="completed", task_result="done")
        assert summary["status"] == "completed"
        # The sweep's blocked_code must not survive onto the live terminal.
        assert "blocked_code" not in summary


# ===========================================================================
# The model-attributable failure projection
# ===========================================================================


class TestFailureProjection:
    def test_fallback_reason_and_models_tried(self, tmp_path):
        runtime, session_id = _runtime(tmp_path)
        runtime.append_event(
            session_id,
            {
                "type": "llm_retry",
                "payload": {"model": "model-a", "error_type": "TimeoutError"},
            },
        )
        runtime.append_event(
            session_id,
            {
                "type": "llm_fallback",
                "payload": {
                    "from_model": "model-a",
                    "to_model": "model-b",
                    "reason": "retries_exhausted",
                },
            },
        )
        summary = _complete(runtime, session_id, done_reason="error")
        assert summary["failure_reason"] == "retries_exhausted"
        assert summary["models_tried"] == ["model-a", "model-b"]

    def test_retry_only_run_still_names_a_reason(self, tmp_path):
        """The shape that burned five attempts on one model emits no fallback."""
        runtime, session_id = _runtime(tmp_path)
        for attempt in (1, 2):
            runtime.append_event(
                session_id,
                {
                    "type": "llm_retry",
                    "payload": {
                        "model": "model-a",
                        "attempt": attempt,
                        "error_type": "TimeoutError",
                    },
                },
            )
        summary = _complete(runtime, session_id, done_reason="error")
        assert summary["failure_reason"] == "TimeoutError"
        assert summary["models_tried"] == ["model-a"]

    def test_models_tried_from_error_detail_provider(self, tmp_path):
        """A fatal first attempt names its model nowhere else."""
        runtime, session_id = _runtime(tmp_path)
        summary = _complete(
            runtime,
            session_id,
            done_reason="error",
            error_detail={
                "kind": "auth",
                "title": "AuthenticationError",
                "provider": "model-a, model-b",
                "detail": "AuthenticationError: nope",
            },
        )
        assert summary["models_tried"] == ["model-a", "model-b"]

    def test_clean_session_carries_no_failure_facets(self, tmp_path):
        """Append-when-present: an untroubled session's summary is unchanged."""
        runtime, session_id = _runtime(tmp_path)
        summary = _complete(runtime, session_id)
        assert "failure_reason" not in summary
        assert "models_tried" not in summary
        assert "blocked_code" not in summary


# ===========================================================================
# The payload must be able to contradict the status
# ===========================================================================


class TestErrorPayloadUnsuppressed:
    """Drives the real emission rule — deleting it must fail these."""

    @staticmethod
    def _attach(
        last_error: str, done_reason: str | None, blocked_code: str | None = None
    ) -> tuple[dict, TaskQueue]:
        orch = Orchestrator.__new__(Orchestrator)
        orch._model_name = "model-a"
        tq = TaskQueue(_human_message="task", action_steps=[])
        tq.last_error = last_error
        payload: dict = {"done": True, "done_reason": done_reason, "task_result": "out"}
        # A real state, because the gate reads ``terminal_status`` off it rather
        # than re-deriving an outcome from ``done_reason`` at the call site.
        state = OrchestrationState(goal="task", done=True, done_reason=done_reason)
        state.blocked_code = blocked_code
        orch._attach_failure_record(payload, tq, state)
        return payload, tq

    def test_a_blocked_run_keeps_the_error_despite_a_completed_reason(self):
        """The regression withholding ``error`` from a success nearly caused.

        A run that died against a credential, a network path or a quota keeps
        ``done_reason == "completed"`` on purpose and carries the wall ONLY in
        ``blocked_code`` — so ``terminal_status`` calls it a success. Gating on
        that projection alone dropped the error from exactly the runs a user
        must see, and silently: a client reading neither field (Aura reads
        ``error`` alone) then renders a clean success for a run that did no
        work. ``blocked_code`` is therefore consulted independently, the same
        way the status layer and the console already consult it.
        """
        payload, _tq = self._attach(
            "ERROR: clone failed for slug", "completed", blocked_code="repo_access"
        )
        assert payload["error"].startswith("ERROR: clone failed")
        assert payload["last_error"] == payload["error"]

    def test_completed_reason_carries_the_record_but_not_the_card_trigger(self):
        """A recovered tool failure is not a session failure.

        ``error`` is the one key a client renders as a user-facing error card,
        so emitting it here put an error under a complete, correct answer. The
        RECORD still rides the payload on ``last_error``/``error_detail``, which
        is what keeps a LAUNDERED run (a halt presenting as success) falsifiable
        against its own record — only the render trigger is withheld.
        """
        payload, _tq = self._attach("ERROR: clone failed for slug", "completed")
        assert "error" not in payload
        assert payload["last_error"].startswith("ERROR: clone failed")
        assert payload["error_detail"]["kind"] == "tool_failure"

    def test_error_still_rides_a_failed_reason(self):
        """A run that stopped short keeps the card trigger — it really failed."""
        payload, _tq = self._attach("litellm.RateLimitError: slow down", "unmet_goal")
        assert payload["error"] == payload["last_error"]
        assert payload["error_detail"]["kind"] == "rate_limited"

    def test_sticky_error_is_clamped_on_the_attribute_too(self):
        """Its readers never check done_reason, so the clamp is unconditional."""
        _payload, tq = self._attach("x" * 9000, "completed")
        assert len(tq.last_error) <= 500

    def test_halt_with_no_sticky_error_still_gets_a_record(self):
        """The shape that left the diagnostic empty on most error-ish runs."""
        payload, _tq = self._attach("", "halted_no_progress")
        assert "error_detail" in payload
        assert "halted_no_progress" in payload["error_detail"]["detail"]

    def test_halt_record_stays_off_the_flat_keys(self):
        """A wrap-up answer is not an error card to put in front of a user."""
        payload, _tq = self._attach("", "budget_exhausted")
        assert "error" not in payload
        assert "last_error" not in payload

    def test_clean_completion_carries_nothing(self):
        payload, _tq = self._attach("", "completed")
        assert "error" not in payload
        assert "error_detail" not in payload


class TestBlockedCodeReachesTheStatus:
    """The loop→payload→status chain, end to end across the two agents' halves.

    ``done_reason`` stays ``"completed"`` for a blocked run at the loop layer,
    so the payload key is the ONLY carrier: drop it and the fact that a run died
    against a credential or a network path is unreachable from the record.
    """

    def test_state_field_is_the_loop_side_contract(self):
        """Pin the field the loop stamps, so a rename cannot pass silently."""
        state = OrchestrationState(goal="task", done=True, done_reason="completed")
        assert state.blocked_code is None
        state.blocked_code = "repo_access"
        assert state.blocked_code == "repo_access"

    @pytest.mark.parametrize(
        "code", ["repo_access", "network", "forbidden", "quota_exceeded"]
    )
    def test_blocked_run_reads_blocked_not_completed(self, tmp_path, code):
        runtime, session_id = _runtime(tmp_path)
        # Exactly the payload the orchestrator writes for a blocked run: a
        # "completed" reason, with the wall recorded beside it.
        summary = _complete(
            runtime, session_id, done=True, done_reason="completed", blocked_code=code
        )
        assert summary["status"] == "blocked"
        assert summary["blocked_code"] == code
        assert summary["recoverable"] is True

    def test_blocked_code_rides_a_raised_failure_too(self, tmp_path):
        """A blocked run that then died of something else keeps the actionable half."""
        runtime, session_id = _runtime(tmp_path)
        summary = _complete(
            runtime,
            session_id,
            done_reason="error",
            error="boom",
            blocked_code="quota_exceeded",
        )
        assert summary["status"] == "blocked"


class TestOutcomeAssertionChannel:
    """A session-end hook's return channel — the majority of the laundering.

    A wiki session ends clean: no exception, no halt, no blocked envelope. Every
    signal the loop owns says success, while the job it existed to run never
    reached terminal `complete`. Only the hook holding that job can see it, and
    until this channel existed the return value was discarded.
    """

    def test_returning_none_asserts_nothing(self):
        """Every hook written before this channel must keep working."""
        hooks = HookManager()
        hooks.on_session_end.append(lambda _sid, _err: None)
        assert hooks.run_on_session_end("s1", None) == []

    def test_assertion_is_collected_and_stamped_with_its_source(self):
        hooks = HookManager()

        def wiki_job_hook(_sid, _err):
            return OutcomeAssertion(
                reason="job_not_complete", detail="job stalled in phase enrich"
            )

        hooks.on_session_end.append(wiki_job_hook)
        [assertion] = hooks.run_on_session_end("s1", None)
        assert assertion.reason == "job_not_complete"
        assert assertion.detail == "job stalled in phase enrich"
        assert assertion.source == "wiki_job_hook"

    def test_a_stray_truthy_return_never_invents_a_failure(self):
        hooks = HookManager()
        hooks.on_session_end.append(lambda _sid, _err: "sure, whatever")
        assert hooks.run_on_session_end("s1", None) == []

    def test_a_raising_hook_is_isolated(self):
        hooks = HookManager()

        def boom(_sid, _err):
            raise RuntimeError("hook exploded")

        hooks.on_session_end.append(boom)
        hooks.on_session_end.append(
            lambda _sid, _err: OutcomeAssertion(reason="still_reported")
        )
        assert [a.reason for a in hooks.run_on_session_end("s1", None)] == ["still_reported"]

    def test_fields_are_clamped_not_rejected(self):
        """Dropping an over-long assertion would restore the laundering."""
        assertion = OutcomeAssertion(
            reason="a b\nc", detail="x" * 900, source="s" * 200
        )
        assert assertion.reason == "a_b_c"
        assert len(assertion.detail) == 500
        assert len(assertion.source) == 64

    def test_empty_reason_is_refused(self):
        with pytest.raises(ValidationError):
            OutcomeAssertion(reason="")


class TestOutcomeAssertionDrivesStatus:
    """The assertion has to reach the derived status, or it changes nothing."""

    @staticmethod
    def _assert_after_completion(runtime, session_id, **completion):
        _ = _complete(runtime, session_id, **completion)
        runtime.append_event(
            session_id,
            {
                "type": "outcome_assertion",
                "payload": OutcomeAssertion(
                    reason="job_not_complete", detail="never reached finalize"
                ).model_dump(mode="json"),
            },
        )
        return runtime.summarize_session(session_id)

    def test_clean_completion_becomes_unmet_goal(self, tmp_path):
        runtime, session_id = _runtime(tmp_path)
        summary = self._assert_after_completion(runtime, session_id, done_reason="completed")
        assert summary["status"] == "unmet_goal"
        assert summary["recoverable"] is True
        assert summary["unmet_goal_reason"] == "job_not_complete"
        assert summary["unmet_goal_detail"] == "never reached finalize"

    def test_blocked_is_not_overwritten(self, tmp_path):
        """The more specific, actionable status wins."""
        runtime, session_id = _runtime(tmp_path)
        summary = self._assert_after_completion(
            runtime, session_id, done_reason="completed", blocked_code="repo_access"
        )
        assert summary["status"] == "blocked"
        # Both facts survive — the assertion is still reported.
        assert summary["unmet_goal_reason"] == "job_not_complete"

    def test_failed_is_not_overwritten(self, tmp_path):
        runtime, session_id = _runtime(tmp_path)
        summary = self._assert_after_completion(runtime, session_id, done_reason="error")
        assert summary["status"] == "failed"

    def test_a_later_clean_turn_is_not_tainted(self, tmp_path):
        """Turn 5 must not inherit turn 1's unmet purpose."""
        runtime, session_id = _runtime(tmp_path)
        self._assert_after_completion(runtime, session_id, done_reason="completed")
        summary = _complete(runtime, session_id, done_reason="completed", task_result="ok")
        assert summary["status"] == "completed"
        assert "unmet_goal_reason" not in summary

    def test_malformed_assertion_never_breaks_derivation(self, tmp_path):
        runtime, session_id = _runtime(tmp_path)
        _ = _complete(runtime, session_id, done_reason="completed")
        runtime.append_event(
            session_id,
            {"type": "outcome_assertion", "payload": {"bogus": "shape"}},
        )
        summary = runtime.summarize_session(session_id)
        assert summary["status"] == "completed"
        assert "unmet_goal_reason" not in summary

    def test_no_assertion_leaves_the_summary_untouched(self, tmp_path):
        runtime, session_id = _runtime(tmp_path)
        summary = _complete(runtime, session_id, done_reason="completed")
        assert "unmet_goal_reason" not in summary
        assert "unmet_goal_detail" not in summary


class TestCheckAgentsTimeoutCoercion:
    """``tool_input`` is model-authored, so the read is where it is validated."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(5, 5.0), (2.5, 2.5), ("7", 7.0), (None, 30.0), ("soon", 30.0), (True, 30.0), ({}, 30.0)],
    )
    def test_coercion_is_total(self, value, expected):
        assert SpawnAgentTool._coerce_timeout(value) == expected


class TestRebindActiveModel:
    """Exercises the line a stale diagnostic reported as an undefined name."""

    def test_rebinds_onto_the_escalated_model(self):
        hv = AgentHypervisor()
        tool = _spawn_tool(AgentContext.root(model_name="model-a", registry=hv))
        tool.rebind_active_model("rescue-model")
        assert tool._agent_context.model_name == "rescue-model"

    def test_is_idempotent_and_ignores_empty(self):
        hv = AgentHypervisor()
        ctx = AgentContext.root(model_name="model-a", registry=hv)
        tool = _spawn_tool(ctx)
        tool.rebind_active_model("model-a")
        tool.rebind_active_model("")
        assert tool._agent_context is ctx

    def test_children_spawn_onto_the_rebound_model(self):
        """The whole point: a healed parent must not spawn onto the dead model.

        Asserted on the context the child loop is BUILT with, not on a registry
        handle — the blocking path unregisters its child in ``finally``, so a
        post-hoc registry read finds nothing and would pass for the wrong reason.
        """
        built_on: list[str] = []

        async def _run():
            hv = AgentHypervisor()
            tool = _spawn_tool(_delegating_ctx(hv))
            tool.parent_tool_specs = [_spec()]
            tool.rebind_active_model("rescue-model")

            def _build(child_ctx, *_args, **_kwargs):
                built_on.append(child_ctx.model_name)
                loop = MagicMock()
                loop.run = AsyncMock(return_value=_settled("completed", task_result="ok"))
                return loop

            tool._build_child_loop = _build  # type: ignore[method-assign]
            return await tool._spawn_one({"task": "probe"}, blocking_admit=True)

        outcome = asyncio.run(_run())
        assert outcome.status == "completed"
        assert built_on == ["rescue-model"]


class TestPromiseAsCompletion:
    """A terminal that commits to future work while nothing is scheduled."""

    def test_note_fires_on_a_bare_promise(self):
        note = Orchestrator._promise_note(
            "Kicked off the reindex. I'll check back on it shortly.",
            owned_runs_live=False,
        )
        assert note is not None
        assert "will not happen on its own" in note

    def test_silent_when_work_is_actually_live(self):
        """Then the promise is simply true."""
        assert (
            Orchestrator._promise_note(
                "I'll follow up when the probes land.", owned_runs_live=True
            )
            is None
        )

    def test_silent_on_ordinary_prose(self):
        assert Orchestrator._promise_note("Done. 12 pages written.", owned_runs_live=False) is None
        assert Orchestrator._promise_note("", owned_runs_live=False) is None

    def test_ownership_index_reads_live_children(self):
        async def _run() -> bool:
            hv = AgentHypervisor()
            ctx = AgentContext.root(model_name="model-a", registry=hv)
            tool = _spawn_tool(ctx)
            assert await tool.has_live_owned_runs() is False
            from mewbo_core.agents.hypervisor import AgentHandle

            await hv.register(
                AgentHandle(
                    agent_id="child-1",
                    parent_id=ctx.agent_id,
                    depth=1,
                    model_name="model-a",
                    task_description="probe",
                    status="running",
                )
            )
            return await tool.has_live_owned_runs()

        assert asyncio.run(_run()) is True


# ===========================================================================
# The terminal a parent is told about
# ===========================================================================


class TestChildTerminalProjection:
    @pytest.mark.parametrize(
        "done_reason",
        [
            "halted_no_progress",
            "verification_failed",
            "max_steps_reached",
            "budget_exhausted",
            "halted_agent_budget",
        ],
    )
    def test_stopped_short_projects_failed(self, done_reason):
        _tq, state = _settled(done_reason)
        assert state.terminal_status() == "failed"

    def test_natural_completion_projects_completed(self):
        _tq, state = _settled("completed")
        assert state.terminal_status() == "completed"

    def test_failed_ground_truth_check_beats_the_reason(self):
        _tq, state = _settled("completed", verified=False)
        assert state.terminal_status() == "failed"

    def test_not_done_projects_failed(self):
        state = OrchestrationState(goal="task", done=False, done_reason=None)
        assert state.terminal_status() == "failed"

    @pytest.mark.parametrize("done_reason", ["canceled", "cancelled"])
    def test_cancellation_projects_cancelled(self, done_reason):
        """A stopped run is neither a success nor an error.

        ``failed`` would report an error nobody hit; ``completed`` reports a
        success nobody achieved. Both spellings are exercised because the loop
        mints the one-L form while the lifecycle vocabulary uses the two-L one.
        """
        _tq, state = _settled(done_reason)
        assert state.terminal_status() == "cancelled"

    def test_cancellation_is_not_folded_into_the_unachieved_reasons(self):
        """The two vocabularies answer different questions and must stay apart.

        A cancelled run did not fall short of its goal — it was never allowed to
        pursue one. Folding it in would make it project ``failed``, which is the
        second wrong answer this seam has given.
        """
        assert not CANCELLED_DONE_REASONS & UNACHIEVED_DONE_REASONS

    def test_projection_range_is_exactly_settled_status(self):
        """Pins the projection's declared range against the terminal vocabulary.

        ``_ChildSettled.status`` declares ``SettledStatus`` and returns this
        projection verbatim, so a member added to one and not the other is a
        type that lies. The projection could not say ``cancelled`` at all until
        this seam was fixed, which is exactly the drift this pins.
        """
        projected = get_type_hints(OrchestrationState.terminal_status)["return"]
        assert set(get_args(projected)) == set(get_args(SettledStatus))

    def test_ground_truth_failure_outranks_cancellation(self):
        """A contradicted claim is substantive; a stop is only a stop."""
        _tq, state = _settled("canceled", verified=False)
        assert state.terminal_status() == "failed"


class TestHaltedChildIsNotStampedCompleted:
    """End-to-end through the real spawn path: a halted child reports failed."""

    def _drive(self, done_reason: str, depth_zero: bool = False):
        async def _run():
            hv = AgentHypervisor()
            ctx = _delegating_ctx(hv)
            tool = _spawn_tool(ctx)
            tool.parent_tool_specs = [_spec()]
            _stub_child_loop(tool, _settled(done_reason, task_result="partial findings"))
            outcome = await tool._spawn_one(
                {"task": "probe the thing"}, blocking_admit=True
            )
            handles = await hv.list_all()
            return outcome, {h.agent_id: h for h in handles}

        return asyncio.run(_run())

    def test_halted_child_reports_failed_to_the_parent(self):
        outcome, _handles = self._drive("halted_no_progress")
        assert outcome.status == "failed"
        body = json.loads(outcome.content)
        assert body["status"] == "failed"

    def test_completed_child_still_reports_completed(self):
        outcome, _handles = self._drive("completed")
        assert outcome.status == "completed"
        assert json.loads(outcome.content)["status"] == "completed"

    def test_partial_work_survives_the_honest_terminal(self):
        """Reporting failure must not also destroy what the child produced."""
        outcome, _handles = self._drive("halted_no_progress")
        assert json.loads(outcome.content)["summary"] == "partial findings"


class TestNonBlockingRootTerminal:
    """The root fan-out path settles its child through the lifecycle manager.

    The second of the two launder sites: a root's children never touch the
    blocking path, so both had to be fixed and both have to be pinned.
    """

    def _drive(self, done_reason: str):
        async def _run():
            hv = AgentHypervisor()
            ctx = AgentContext.root(model_name="model-a", registry=hv)
            tool = _spawn_tool(ctx)
            tool.parent_tool_specs = [_spec()]
            _stub_child_loop(tool, _settled(done_reason, task_result="partial findings"))
            outcome = await tool._spawn_one({"task": "probe"}, blocking_admit=True)
            assert outcome.status == "submitted", "root spawns are non-blocking"
            await tool.await_lifecycle_managers(timeout=2.0)
            return (await hv.list_all())[0]

        return asyncio.run(_run())

    def test_halted_child_settles_as_failed(self):
        handle = self._drive("halted_no_progress")
        assert handle.status == "failed"
        assert handle.result is not None
        assert handle.result.status == "failed"

    def test_completed_child_settles_as_completed(self):
        handle = self._drive("completed")
        assert handle.status == "completed"
        assert handle.result.status == "completed"

    def test_halted_child_still_hands_back_its_work(self):
        handle = self._drive("halted_no_progress")
        assert handle.result.summary == "partial findings"


class TestChildSummaryFallback:
    """The heaviest children answer with nothing; their compaction is the CU."""

    def test_task_result_preferred(self):
        tq, state = _settled("completed", task_result="the answer")
        state.summary = "a compaction"
        assert SpawnAgentTool._child_summary(tq, state) == "the answer"

    def test_falls_back_to_the_compaction_summary(self):
        tq, state = _settled("completed", task_result="")
        state.summary = "a compaction"
        assert SpawnAgentTool._child_summary(tq, state) == "a compaction"

    def test_empty_when_neither_exists(self):
        tq, state = _settled("completed", task_result="")
        assert SpawnAgentTool._child_summary(tq, state) == ""


# ===========================================================================
# Spawn hygiene — model fallback + retry on a stopped-short child
# ===========================================================================


class TestSpawnModelFallback:
    """A model the deployment cannot serve must not kill the child at step 0."""

    def _spawn_with_model(self, model: str, allowed: list[str]):
        async def _run():
            hv = AgentHypervisor()
            ctx = _delegating_ctx(hv, "parent-model")
            tool = _spawn_tool(ctx)
            tool.parent_tool_specs = [_spec()]
            _stub_child_loop(tool, _settled("completed", task_result="ok"))
            from unittest.mock import patch

            with patch(
                "mewbo_core.agents.spawn_agent.get_config_value",
                side_effect=lambda *a, **kw: (
                    allowed if a == ("agent", "allowed_models") else kw.get("default", "")
                ),
            ):
                return await tool._spawn_one(
                    {"task": "probe", "model": model}, blocking_admit=True
                )

        return asyncio.run(_run())

    def test_unavailable_model_falls_back_to_the_parent(self):
        outcome = self._spawn_with_model("forbidden-model", ["parent-model"])
        assert outcome.status == "completed"
        body = json.loads(outcome.content)
        assert "model_fallback" in body
        assert "parent-model" in body["model_fallback"]

    def test_available_model_is_byte_identical(self):
        outcome = self._spawn_with_model("parent-model", ["parent-model"])
        assert "model_fallback" not in json.loads(outcome.content)


class TestRetryFiresOnStoppedShort:
    """The reason the contract had never once fired in production."""

    def _drive(self, retry_cfg, *outcomes):
        async def _run():
            hv = AgentHypervisor()
            ctx = _delegating_ctx(hv)
            tool = _spawn_tool(ctx)
            tool.parent_tool_specs = [_spec()]
            calls = _stub_child_loop(tool, *outcomes)
            args = {"task": "probe"}
            if retry_cfg is not None:
                args["retry"] = retry_cfg
            outcome = await tool._spawn_one(args, blocking_admit=True)
            return outcome, len(calls)

        return asyncio.run(_run())

    def test_halt_is_re_delegated_then_succeeds(self):
        outcome, attempts = self._drive(
            {"max": 1, "backoff": 0},
            _settled("halted_no_progress"),
            _settled("completed", task_result="second time"),
        )
        assert attempts == 2
        assert outcome.status == "completed"

    def test_halt_reported_honestly_once_the_budget_is_spent(self):
        outcome, attempts = self._drive(
            {"max": 1, "backoff": 0},
            _settled("halted_no_progress"),
            _settled("halted_no_progress"),
        )
        assert attempts == 2
        assert outcome.status == "failed"

    def test_default_off_is_exactly_one_attempt(self):
        """Byte-identical to the ungated path when nothing opts in."""
        outcome, attempts = self._drive(None, _settled("halted_no_progress"))
        assert attempts == 1
        assert outcome.status == "failed"

    def test_a_clean_child_is_never_retried(self):
        _outcome, attempts = self._drive(
            {"max": 2, "backoff": 0}, _settled("completed", task_result="ok")
        )
        assert attempts == 1


# ===========================================================================
# Owned children settle in-process, not at next boot
# ===========================================================================


class TestLifecycleManagersSettleInProcess:
    def test_cancelled_managers_settle_before_the_call_returns(self):
        """``cancel()`` only SCHEDULES the handler that writes the terminal.

        The observation has to be taken INSIDE the event loop, at the moment
        ``await_lifecycle_managers`` returns. Asserting after ``asyncio.run``
        proves nothing: its own shutdown cancels and awaits leftover tasks, so
        the handler runs there regardless and the test passes with the fix
        removed. What matters is that the child is settled while the run still
        owns it — the difference between an in-process terminal and a span left
        open for a boot sweep to reap.
        """
        settled: list[str] = []

        async def _run() -> list[str]:
            async def _never_finishes():
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    # Stands in for the real handler: mark_done + terminal stop.
                    settled.append("child")
                    raise

            hv = AgentHypervisor()
            ctx = AgentContext.root(model_name="model-a", registry=hv)
            tool = _spawn_tool(ctx)
            tool._lifecycle_tasks.append(asyncio.create_task(_never_finishes()))
            await tool.await_lifecycle_managers(timeout=0.01)
            return list(settled)

        observed = asyncio.run(_run())
        assert observed == ["child"], "a cancelled manager must settle before teardown"

    def test_finished_managers_need_no_cancellation(self):
        async def _run():
            async def _quick():
                return None

            hv = AgentHypervisor()
            ctx = AgentContext.root(model_name="model-a", registry=hv)
            tool = _spawn_tool(ctx)
            task = asyncio.create_task(_quick())
            tool._lifecycle_tasks.append(task)
            await tool.await_lifecycle_managers(timeout=1.0)
            return task

        task = asyncio.run(_run())
        assert task.done() and not task.cancelled()
