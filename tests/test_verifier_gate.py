#!/usr/bin/env python3
"""Tests for verifier-gated completion at the tool-use-loop seam.

Drives the natural-completion branch with a RECORDING fake ``VerifierRunner``
(records the spec + cwd it was handed — a fake that discards the arg under test
could never catch a mis-wire). Also covers the spawn_agent NO-SILENT-DROP note
for a supplied-but-inactive spec.

No real subprocess runs anywhere here — the runner is always the fake.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from unittest.mock import AsyncMock, MagicMock, patch

import mewbo_core.config as _config_mod
from mewbo_core.classes import ActionStep
from mewbo_core.tool_use_loop import ToolUseLoop
from mewbo_core.verification import CommandVerification, VerifierResult

# Sibling-helper reuse (tests/ is on sys.path under pytest).
from test_tool_use_loop import (
    _allow_all_policy,
    _make_agent_context,
    _make_context,
    _make_hook_manager,
    _make_registry,
    _make_spec,
    _text_response,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class RecordingRunner:
    """Fake ``VerifierRunner`` that records every (spec, cwd) and replays results.

    Records the exact spec + cwd it was handed so a test can assert the loop
    passed the RIGHT spec — a fake that ignored its args couldn't fail on a
    mis-wire.
    """

    def __init__(self, results: list[VerifierResult]) -> None:
        self._results = list(results)
        self.calls: list[tuple[CommandVerification, str | None]] = []

    async def run(self, spec: CommandVerification, *, cwd: str | None) -> VerifierResult:
        self.calls.append((spec, cwd))
        return self._results.pop(0)


def _passing() -> VerifierResult:
    return VerifierResult(exit_code=0, stdout="all good", stderr="", timed_out=False)


def _failing() -> VerifierResult:
    return VerifierResult(exit_code=1, stdout="", stderr="1 test failed", timed_out=False)


def _cfg(**overrides):
    """Config side-effect: override the verification keys, delegate the rest.

    Patched onto ``tool_use_loop.get_config_value`` so the ctor's two-gate arming
    reads the values under test while every unrelated key (workspace, tool
    search) keeps its real behaviour.
    """
    real = _config_mod.get_config_value

    def _side(*keys, default=None):
        dotted = ".".join(str(k) for k in keys)
        if dotted in overrides:
            return overrides[dotted]
        return real(*keys, default=default)

    return _side


def _run_gate(
    *,
    verification,
    runner,
    model_responses,
    enabled=True,
    max_retries=2,
    capability_mode=None,
):
    """Build + run a loop with the verifier gate wired, returning (tq, state, runner)."""
    spec = _make_spec("aider_shell_tool", "Run shell")
    registry = _make_registry(spec)

    fake_model = MagicMock()
    fake_model.ainvoke = AsyncMock(side_effect=list(model_responses))
    bound = MagicMock()
    bound.ainvoke = fake_model.ainvoke

    ctx = _make_agent_context()
    if capability_mode is not None:
        ctx = dataclasses.replace(ctx, capability_mode=capability_mode)

    with (
        patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build,
        patch(
            "mewbo_core.tool_use_loop.get_config_value",
            side_effect=_cfg(
                **{
                    "agent.verification_enabled": enabled,
                    "agent.verification_max_retries": max_retries,
                }
            ),
        ),
    ):
        mock_build.return_value = MagicMock()
        mock_build.return_value.bind_tools.return_value = bound

        loop = ToolUseLoop(
            agent_context=ctx,
            tool_registry=registry,
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
            cwd="/work/space",
            verification=verification,
            verifier_runner=runner,
        )
        tq, state = asyncio.run(
            loop.run("do the task", tool_specs=[spec], context=_make_context())
        )
    return tq, state, runner, loop


# ---------------------------------------------------------------------------
# Gate behaviour at the natural-completion seam
# ---------------------------------------------------------------------------


class TestVerifierGate:
    def test_pass_first_completes_verified(self):
        """A green check on the first claim → completed, verified, one runner call."""
        runner = RecordingRunner([_passing()])
        _tq, state, runner, _loop = _run_gate(
            verification=CommandVerification(argv=["pytest"]),
            runner=runner,
            model_responses=[_text_response("Done.")],
        )
        assert state.done_reason == "completed"
        assert state.verified is True
        assert state.verify_attempts == 1
        assert len(runner.calls) == 1
        # The loop handed the runner the exact spec + workspace cwd.
        passed_spec, passed_cwd = runner.calls[0]
        assert passed_spec.argv == ["pytest"]
        assert passed_cwd == "/work/space"

    def test_fail_fail_pass_recovers(self):
        """Two red checks then green → verified, three attempts, completed."""
        runner = RecordingRunner([_failing(), _failing(), _passing()])
        _tq, state, runner, _loop = _run_gate(
            verification=CommandVerification(argv=["pytest"]),
            runner=runner,
            model_responses=[
                _text_response("try 1"),
                _text_response("try 2"),
                _text_response("try 3"),
            ],
        )
        assert state.done_reason == "completed"
        assert state.verified is True
        assert state.verify_attempts == 3
        assert len(runner.calls) == 3

    def test_exhausted_retries_flagged_honestly(self):
        """Retries spent still red → text accepted, done_reason honest, verified False."""
        runner = RecordingRunner([_failing(), _failing(), _failing()])
        tq, state, runner, _loop = _run_gate(
            verification=CommandVerification(argv=["pytest"]),
            runner=runner,
            max_retries=2,
            model_responses=[_text_response("a"), _text_response("b"), _text_response("final")],
        )
        assert state.done_reason == "verification_failed"
        assert state.verified is False
        assert state.verify_attempts == 3
        assert len(runner.calls) == 3
        # The claimed text is still returned to the caller — not swallowed.
        assert tq.task_result == "final"

    def test_zero_retries_one_check(self):
        """max_retries=0 → exactly one check; a fail flags verification_failed."""
        runner = RecordingRunner([_failing()])
        _tq, state, runner, _loop = _run_gate(
            verification=CommandVerification(argv=["pytest"]),
            runner=runner,
            max_retries=0,
            model_responses=[_text_response("done")],
        )
        assert state.done_reason == "verification_failed"
        assert state.verified is False
        assert state.verify_attempts == 1
        assert len(runner.calls) == 1

    def test_no_spec_never_calls_runner(self):
        """No verification spec → runner untouched, verified None, historical path."""
        runner = RecordingRunner([_passing()])  # would pop if wrongly called
        _tq, state, runner, _loop = _run_gate(
            verification=None,
            runner=runner,
            model_responses=[_text_response("Done, no gate.")],
        )
        assert state.done_reason == "completed"
        assert state.verified is None
        assert state.verify_attempts == 0
        assert runner.calls == []

    def test_read_only_capability_is_inert(self):
        """A spec under read_only is carried but never run (below-execute gate)."""
        runner = RecordingRunner([_passing()])
        _tq, state, runner, loop = _run_gate(
            verification=CommandVerification(argv=["pytest"]),
            runner=runner,
            capability_mode="read_only",
            model_responses=[_text_response("Done.")],
        )
        assert loop._verification_active is False
        assert state.done_reason == "completed"
        assert state.verified is None
        assert state.verify_attempts == 0
        assert runner.calls == []

    def test_master_switch_off_is_inert(self):
        """A spec with the master switch off is carried but never run."""
        runner = RecordingRunner([_passing()])
        _tq, state, runner, loop = _run_gate(
            verification=CommandVerification(argv=["pytest"]),
            runner=runner,
            enabled=False,
            model_responses=[_text_response("Done.")],
        )
        assert loop._verification_active is False
        assert state.done_reason == "completed"
        assert state.verified is None
        assert runner.calls == []

    def test_budget_exhaustion_wins_over_pending_retry(self):
        """A budget hard-stop mid-retry wraps up with the budget reason, not the gate.

        The failed check ``continue``s through the top-of-loop budget check
        FIRST, so an exhausted budget forces the wrap-up before the gate gets a
        second run.
        """
        spec = _make_spec("aider_shell_tool", "Run shell")
        registry = _make_registry(spec)

        fake_model = MagicMock()
        fake_model.ainvoke = AsyncMock(side_effect=[_text_response("claim 1")])
        bound = MagicMock()
        bound.ainvoke = fake_model.ainvoke
        wrapup_invoke = AsyncMock(return_value=_text_response("Wrapping up."))

        runner = RecordingRunner([_failing()])
        ctx = _make_agent_context()

        with (
            patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build,
            patch(
                "mewbo_core.tool_use_loop.get_config_value",
                side_effect=_cfg(
                    **{"agent.verification_enabled": True, "agent.verification_max_retries": 2}
                ),
            ),
            # False on turn 1 (gate runs, fails, continues), True on turn 2's
            # top-of-loop check → forced wrap-up.
            patch.object(ctx.registry, "budget_exhausted", side_effect=[False, True, True]),
        ):
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound
            mock_build.return_value.ainvoke = wrapup_invoke

            loop = ToolUseLoop(
                agent_context=ctx,
                tool_registry=registry,
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
                cwd="/work/space",
                verification=CommandVerification(argv=["pytest"]),
                verifier_runner=runner,
            )
            tq, state = asyncio.run(
                loop.run("do the task", tool_specs=[spec], context=_make_context())
            )

        assert state.done_reason == "budget_exhausted"
        # The gate ran once (turn 1) and then the budget won — no second check.
        assert len(runner.calls) == 1
        assert state.verify_attempts == 1
        assert state.verified is None
        wrapup_invoke.assert_awaited_once()

    def test_verification_event_emitted(self):
        """Each check emits a bounded ``verification`` event (no stdout/stderr)."""
        events: list = []
        ctx = _make_agent_context(event_logger=events.append)
        spec = _make_spec("aider_shell_tool", "Run shell")
        registry = _make_registry(spec)

        fake_model = MagicMock()
        fake_model.ainvoke = AsyncMock(side_effect=[_text_response("Done.")])
        bound = MagicMock()
        bound.ainvoke = fake_model.ainvoke
        runner = RecordingRunner([_passing()])

        with (
            patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build,
            patch(
                "mewbo_core.tool_use_loop.get_config_value",
                side_effect=_cfg(
                    **{"agent.verification_enabled": True, "agent.verification_max_retries": 2}
                ),
            ),
        ):
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound
            loop = ToolUseLoop(
                agent_context=ctx,
                tool_registry=registry,
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
                cwd="/work/space",
                verification=CommandVerification(argv=["pytest"]),
                verifier_runner=runner,
            )
            asyncio.run(loop.run("task", tool_specs=[spec], context=_make_context()))

        verifications = [e for e in events if e.get("type") == "verification"]
        assert len(verifications) == 1
        payload = verifications[0]["payload"]
        assert payload["passed"] is True
        assert payload["attempt"] == 1
        assert payload["exit_code"] == 0
        # Bounded scalars ONLY — no stdout/stderr on the wire.
        assert "stdout" not in payload
        assert "stderr" not in payload


# ---------------------------------------------------------------------------
# spawn_agent NO-SILENT-DROP note
# ---------------------------------------------------------------------------


class TestSpawnNoSilentDrop:
    """A supplied-but-inactive spec is surfaced in the spawn response, never dropped."""

    def test_blocking_spawn_response_surfaces_inactive_note(self):
        # Deferred import: reuse test_spawn_agent's depth>0 (blocking) scaffolding.
        from mewbo_core.spawn_agent import SpawnAgentTool
        from test_spawn_agent import (
            _allow_all_policy as _sa_policy,
            _make_context as _sa_context,
            _make_hook_manager as _sa_hooks,
            _make_registry as _sa_registry,
        )

        async def _test():
            registry = _sa_registry("shell_tool")
            ctx = _sa_context(depth=1)  # depth>0 → blocking path settles inline
            tool = SpawnAgentTool(
                agent_context=ctx,
                tool_registry=registry,
                permission_policy=_sa_policy(),
                hook_manager=_sa_hooks(),
            )
            fake_model = MagicMock()
            fake_model.ainvoke = AsyncMock(return_value=_text_response("child done"))
            bound = MagicMock()
            bound.ainvoke = fake_model.ainvoke

            with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound
                step = ActionStep(
                    tool_id="spawn_agent",
                    operation="set",
                    tool_input={
                        # Master switch defaults OFF → the gate is inert, so the
                        # note must appear.
                        "task": "do it",
                        "verification": {"argv": ["pytest"]},
                    },
                )
                result = await tool.run_async(step)

            body = json.loads(result.content)
            assert body["verification"] == "specified_but_inactive (verification_enabled=false)"

        asyncio.run(_test())
