#!/usr/bin/env python3
"""Tests for the ``verification`` domain model — parse totality + verdicts.

Companion to ``test_verifier_gate.py`` (which drives the LOOP seam). This
module exercises ``CommandVerification`` in isolation: the total
``from_value`` parse, ``extra="forbid"`` rejection, the pure ``interpret``
verdict (with its bounded feedback tail), and the shared inactive-reason
predicate that both the loop and spawn_agent read.
"""

from __future__ import annotations

import pytest
from mewbo_core.verification import (
    _FEEDBACK_TAIL_CAP,
    CommandVerification,
    VerifierResult,
)
from pydantic import ValidationError


class TestFromValueTotality:
    """``from_value`` is TOTAL — garbage degrades to ``None`` (gate inert)."""

    @pytest.mark.parametrize("value", [None, "pytest -q", 42, [], ["true"], object()])
    def test_non_mapping_is_none(self, value):
        assert CommandVerification.from_value(value) is None

    def test_garbage_mapping_is_none(self):
        # A mapping with a wrong non-empty kind can't select the union member.
        assert CommandVerification.from_value({"kind": "bogus", "argv": ["x"]}) is None
        # A mapping with neither kind nor argv is not a valid spec.
        assert CommandVerification.from_value({"foo": "bar"}) is None

    def test_empty_argv_is_none(self):
        assert CommandVerification.from_value({"argv": []}) is None
        assert CommandVerification.from_value({"kind": "command", "argv": []}) is None

    def test_blank_string_in_argv_is_none(self):
        assert CommandVerification.from_value({"argv": ["pytest", ""]}) is None

    def test_missing_kind_defaults_to_command(self):
        spec = CommandVerification.from_value({"argv": ["pytest", "-q"]})
        assert spec is not None
        assert spec.kind == "command"
        assert spec.argv == ["pytest", "-q"]

    def test_full_mapping_parses(self):
        spec = CommandVerification.from_value(
            {"kind": "command", "argv": ["make", "test"], "cwd": "/w", "timeout_s": 30}
        )
        assert spec is not None
        assert spec.cwd == "/w"
        assert spec.timeout_s == 30.0

    def test_never_raises_on_bad_types(self):
        # argv of the wrong element type must degrade, not raise.
        assert CommandVerification.from_value({"argv": [1, 2, 3]}) is None
        assert CommandVerification.from_value({"argv": "pytest"}) is None


class TestExtraForbid:
    """A smuggled extra key is a clean rejection, not a silent no-op."""

    def test_extra_key_rejected_on_construct(self):
        with pytest.raises(ValidationError):
            CommandVerification(kind="command", argv=["true"], token="sneaky")

    def test_extra_key_degrades_via_from_value(self):
        assert CommandVerification.from_value({"argv": ["true"], "token": "x"}) is None


class TestInterpret:
    """``interpret`` is a pure verdict over an already-collected result."""

    def _spec(self) -> CommandVerification:
        return CommandVerification(argv=["true"])

    def test_exit_zero_passes(self):
        outcome = self._spec().interpret(
            VerifierResult(exit_code=0, stdout="ok", stderr="", timed_out=False)
        )
        assert outcome.passed is True
        assert outcome.exit_code == 0
        assert outcome.timed_out is False

    def test_nonzero_exit_fails_with_bounded_tail(self):
        long_err = "E" * (_FEEDBACK_TAIL_CAP * 3)
        outcome = self._spec().interpret(
            VerifierResult(exit_code=2, stdout="", stderr=long_err, timed_out=False)
        )
        assert outcome.passed is False
        assert outcome.exit_code == 2
        # Only the TAIL of stderr is kept, capped at the module constant.
        body = outcome.feedback.split("stderr:\n", 1)[1]
        assert len(body) == _FEEDBACK_TAIL_CAP
        assert body == long_err[-_FEEDBACK_TAIL_CAP:]

    def test_stderr_then_stdout_ordering(self):
        outcome = self._spec().interpret(
            VerifierResult(exit_code=1, stdout="the-output", stderr="the-error", timed_out=False)
        )
        assert outcome.feedback.index("the-error") < outcome.feedback.index("the-output")

    def test_timeout_fails(self):
        outcome = self._spec().interpret(
            VerifierResult(exit_code=-1, stdout="", stderr="", timed_out=True)
        )
        assert outcome.passed is False
        assert outcome.timed_out is True
        assert "timed out" in outcome.feedback

    def test_runner_error_fails(self):
        outcome = self._spec().interpret(
            VerifierResult(
                exit_code=-1, stdout="", stderr="", timed_out=False, error="No such file: pytest"
            )
        )
        assert outcome.passed is False
        assert "could not run" in outcome.feedback
        assert "pytest" in outcome.feedback

    def test_clean_exit_with_no_output_still_grounds(self):
        outcome = self._spec().interpret(
            VerifierResult(exit_code=3, stdout="", stderr="", timed_out=False)
        )
        assert outcome.passed is False
        assert "exit code 3" in outcome.feedback


class TestInactiveReason:
    """The ONE two-gate predicate the loop + spawn_agent share."""

    def test_active_when_enabled_and_actable(self):
        assert CommandVerification.inactive_reason(enabled=True, capability_mode="all") is None
        assert (
            CommandVerification.inactive_reason(enabled=True, capability_mode="execute") is None
        )
        assert CommandVerification.gate_active(enabled=True, capability_mode="all") is True

    def test_master_switch_off(self):
        reason = CommandVerification.inactive_reason(enabled=False, capability_mode="all")
        assert reason == "specified_but_inactive (verification_enabled=false)"
        assert CommandVerification.gate_active(enabled=False, capability_mode="all") is False

    def test_below_execute_capability(self):
        reason = CommandVerification.inactive_reason(enabled=True, capability_mode="read_only")
        assert reason == "specified_but_inactive (capability_mode=read_only)"
        assert CommandVerification.gate_active(enabled=True, capability_mode="read_only") is False
