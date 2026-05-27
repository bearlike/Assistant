#!/usr/bin/env python3
"""Unit tests for the LLM resilience objects.

Covers the three-way error classifier, full-jitter backoff, the token-bucket
retry budget, the per-model circuit breaker, the doom-loop guard and
tool-call-pairing repair — no live model or network.
"""

from __future__ import annotations

import asyncio

import litellm.exceptions as lx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from mewbo_core.llm_resilience import (
    CircuitBreaker,
    DoomLoopGuard,
    LlmResilienceExhausted,
    RetryAction,
    RetryBudget,
    RetryStrategy,
    repair_tool_pairing,
)

_SENTINEL = object()


async def _never_compact() -> bool:
    return False


def _mk(cls: type, message: str):
    """Construct a litellm exception across signature variants."""
    for attempt in (
        lambda: cls(message=message, llm_provider="openai", model="test-model"),
        lambda: cls(message, "openai", "test-model"),
        lambda: cls(message),
    ):
        try:
            return attempt()
        except TypeError:
            continue
    # openai-derived errors (e.g. PermissionDeniedError) require an httpx
    # response; classification only needs the type, so bypass __init__.
    inst = cls.__new__(cls)
    inst.message = message  # type: ignore[attr-defined]
    return inst


class TestClassifyLlmError:
    def test_timeout_is_retry_same(self):
        d = RetryStrategy.classify(asyncio.TimeoutError())
        assert d.action is RetryAction.RETRY_SAME
        assert d.reason == "timeout"

    def test_cancellation_is_fatal(self):
        d = RetryStrategy.classify(asyncio.CancelledError())
        assert d.action is RetryAction.FATAL
        assert d.reason == "cancelled"

    def test_deterministic_error_is_fatal(self):
        assert RetryStrategy.classify(ValueError("bug")).action is RetryAction.FATAL
        assert RetryStrategy.classify(TypeError("bug")).action is RetryAction.FATAL

    def test_context_window_switches_model(self):
        d = RetryStrategy.classify(_mk(lx.ContextWindowExceededError, "too long"))
        assert d.action is RetryAction.SWITCH_MODEL
        assert d.reason == "context_window"

    def test_auth_switches_model(self):
        d = RetryStrategy.classify(_mk(lx.AuthenticationError, "bad key"))
        assert d.action is RetryAction.SWITCH_MODEL

    def test_permission_denied_is_fatal(self):
        d = RetryStrategy.classify(_mk(lx.PermissionDeniedError, "nope"))
        assert d.action is RetryAction.FATAL

    def test_server_error_is_retry_same(self):
        d = RetryStrategy.classify(_mk(lx.InternalServerError, "500"))
        assert d.action is RetryAction.RETRY_SAME
        assert d.reason == "server_error"

    def test_rate_limit_no_deployments_switches(self):
        d = RetryStrategy.classify(
            _mk(lx.RateLimitError, "No deployments available for selected model")
        )
        assert d.action is RetryAction.SWITCH_MODEL
        assert d.reason == "no_deployments"

    def test_rate_limit_generic_retries_with_header(self):
        exc = _mk(lx.RateLimitError, "slow down")

        class _Resp:
            headers = {"retry-after": "7"}

        exc.response = _Resp()
        d = RetryStrategy.classify(exc)
        assert d.action is RetryAction.RETRY_SAME
        assert d.reason == "rate_limit"
        assert d.retry_after == 7.0

    def test_bad_request_quota_switches(self):
        d = RetryStrategy.classify(
            _mk(lx.BadRequestError, "You're out of extra usage. Add more at ...")
        )
        assert d.action is RetryAction.SWITCH_MODEL
        assert d.reason == "quota_exhausted"

    def test_bad_request_generic_is_fatal(self):
        d = RetryStrategy.classify(_mk(lx.BadRequestError, "malformed tool schema"))
        assert d.action is RetryAction.FATAL
        assert d.reason == "bad_request"


class TestBackoff:
    @staticmethod
    def _s(rng, *, cap: float = 60.0) -> RetryStrategy:
        return RetryStrategy(backoff_base=1.0, backoff_cap=cap, retry_after_cap=60.0, rng=rng)

    def test_exponential_ceiling_with_full_jitter_high(self):
        s = self._s(lambda: 1.0)
        assert s.backoff(1) == 1.0
        assert s.backoff(2) == 2.0
        assert s.backoff(3) == 4.0

    def test_cap_applied(self):
        assert self._s(lambda: 1.0, cap=8.0).backoff(10) == 8.0

    def test_full_jitter_low_is_zero(self):
        assert self._s(lambda: 0.0).backoff(5) == 0.0

    def test_retry_after_is_floor_and_capped(self):
        s = self._s(lambda: 0.0)
        assert s.backoff(1, retry_after=30.0) == 30.0
        assert s.backoff(1, retry_after=999.0) == 60.0


class TestRetryBudget:
    def test_drains_then_refuses_at_half(self):
        b = RetryBudget(capacity=4.0, retry_cost=1.0, success_credit=0.5)
        assert b.can_retry()  # 4 > 2
        b.charge()
        assert b.tokens == 3.0 and b.can_retry()
        b.charge()
        assert b.tokens == 2.0 and not b.can_retry()  # 2 is not > 2

    def test_credit_refills_capped(self):
        b = RetryBudget(capacity=4.0, retry_cost=1.0, success_credit=0.5)
        b.charge()
        b.charge()
        b.credit()
        assert b.tokens == 2.5 and b.can_retry()
        for _ in range(20):
            b.credit()
        assert b.tokens == 4.0  # never exceeds capacity


class TestCircuitBreaker:
    def test_opens_after_threshold_and_cools_down(self):
        now = {"t": 0.0}
        cb = CircuitBreaker(threshold=2, cooldown=10.0, clock=lambda: now["t"])
        cb.record_failure("m")
        assert not cb.is_open("m")
        cb.record_failure("m")
        assert cb.is_open("m")
        now["t"] = 9.9
        assert cb.is_open("m")
        now["t"] = 10.0
        assert not cb.is_open("m")  # cooldown elapsed -> half-open probe

    def test_success_resets(self):
        cb = CircuitBreaker(threshold=2, cooldown=10.0, clock=lambda: 0.0)
        cb.record_failure("m")
        cb.record_success("m")
        cb.record_failure("m")
        assert not cb.is_open("m")  # streak reset, only 1 fail since success

    def test_threshold_zero_never_opens(self):
        cb = CircuitBreaker(threshold=0, cooldown=10.0, clock=lambda: 0.0)
        for _ in range(5):
            cb.record_failure("m")
        assert not cb.is_open("m")


class TestDoomLoopGuard:
    def test_detects_identical_streak(self):
        g = DoomLoopGuard(threshold=3)
        tc = [{"name": "read", "args": {"path": "a"}, "id": "1"}]
        g.observe(tc)
        g.observe(tc)
        assert not g.is_stuck()
        g.observe(tc)
        assert g.is_stuck()

    def test_different_call_breaks_streak(self):
        g = DoomLoopGuard(threshold=3)
        g.observe([{"name": "read", "args": {"path": "a"}, "id": "1"}])
        g.observe([{"name": "read", "args": {"path": "b"}, "id": "2"}])
        g.observe([{"name": "read", "args": {"path": "a"}, "id": "3"}])
        assert not g.is_stuck()

    def test_threshold_zero_disables(self):
        g = DoomLoopGuard(threshold=0)
        for _ in range(5):
            g.observe([{"name": "t", "args": {}, "id": "1"}])
        assert not g.is_stuck()

    def test_signature_is_order_independent_on_args(self):
        a = DoomLoopGuard.signature([{"name": "t", "args": {"x": 1, "y": 2}, "id": "1"}])
        b = DoomLoopGuard.signature([{"name": "t", "args": {"y": 2, "x": 1}, "id": "2"}])
        assert a == b  # id excluded; arg key order normalised


class TestRepairToolPairing:
    def test_synthesizes_missing_tool_result(self):
        msgs: list = [
            SystemMessage(content="sys"),
            AIMessage(
                content="",
                tool_calls=[{"name": "t", "args": {}, "id": "a", "type": "tool_call"}],
            ),
        ]
        repaired = repair_tool_pairing(msgs)
        assert repaired == 1
        assert isinstance(msgs[-1], ToolMessage)
        assert msgs[-1].tool_call_id == "a"

    def test_drops_orphan_tool_result(self):
        msgs: list = [
            ToolMessage(content="x", tool_call_id="ghost"),
            HumanMessage(content="hi"),
        ]
        repaired = repair_tool_pairing(msgs)
        assert repaired == 1
        assert not any(isinstance(m, ToolMessage) for m in msgs)

    def test_balanced_is_unchanged(self):
        msgs: list = [
            AIMessage(
                content="",
                tool_calls=[{"name": "t", "args": {}, "id": "a", "type": "tool_call"}],
            ),
            ToolMessage(content="ok", tool_call_id="a"),
        ]
        assert repair_tool_pairing(msgs) == 0
        assert len(msgs) == 2


class TestRetryStrategy:
    @staticmethod
    def _strategy(**kw) -> RetryStrategy:
        params: dict = {
            "primary_retries": 3,
            "fallback_retries": 1,
            "rng": lambda: 0.0,  # full jitter -> 0 delay, no real sleep
            "budget": RetryBudget(capacity=10.0),
            "breaker": CircuitBreaker(threshold=99),
        }
        params.update(kw)
        return RetryStrategy(**params)

    @staticmethod
    def _run(strategy: RetryStrategy, models, invoke, events: list):
        async def _go():
            return await strategy.run(
                models=models,
                invoke=invoke,
                emit=events.append,
                compact=_never_compact,
                agent_id="a",
                depth=0,
                step=0,
            )

        return asyncio.run(_go())

    def test_returns_on_first_success(self):
        async def invoke(model, is_fb):
            return _SENTINEL

        events: list = []
        resp, model = self._run(self._strategy(), ["p"], invoke, events)
        assert resp is _SENTINEL and model == "p"
        assert events == []

    def test_retries_transient_then_succeeds(self):
        calls: list = []

        async def invoke(model, is_fb):
            calls.append(model)
            if len(calls) == 1:
                raise asyncio.TimeoutError()
            return _SENTINEL

        events: list = []
        resp, model = self._run(self._strategy(), ["p"], invoke, events)
        assert resp is _SENTINEL and len(calls) == 2
        assert [e["type"] for e in events] == ["llm_retry"]
        assert events[0]["payload"]["model"] == "p"

    def test_switches_to_fallback_on_hopeless_error(self):
        calls: list = []

        async def invoke(model, is_fb):
            calls.append((model, is_fb))
            if not is_fb:
                raise _mk(lx.RateLimitError, "No deployments available for selected model")
            return _SENTINEL

        events: list = []
        resp, model = self._run(self._strategy(), ["p", "f"], invoke, events)
        assert model == "f" and resp is _SENTINEL
        assert calls == [("p", False), ("f", True)]  # no wasted same-model retries
        assert any(e["type"] == "llm_fallback" for e in events)

    def test_fatal_does_not_retry_or_fallback(self):
        calls: list = []

        async def invoke(model, is_fb):
            calls.append(model)
            raise ValueError("bug")

        with pytest.raises(LlmResilienceExhausted) as ei:
            self._run(self._strategy(), ["p", "f"], invoke, [])
        assert calls == ["p"]
        assert ei.value.reason == "deterministic"

    def test_exhausts_after_primary_retries(self):
        calls: list = []

        async def invoke(model, is_fb):
            calls.append(model)
            raise asyncio.TimeoutError()

        with pytest.raises(LlmResilienceExhausted) as ei:
            self._run(self._strategy(primary_retries=3), ["p"], invoke, [])
        assert len(calls) == 3
        assert ei.value.models_tried == ["p"]

    def test_deadline_halts_chain(self):
        ticks = {"v": 0.0}

        def clock():
            ticks["v"] += 100.0
            return ticks["v"]

        async def invoke(model, is_fb):
            raise asyncio.TimeoutError()

        with pytest.raises(LlmResilienceExhausted) as ei:
            self._run(self._strategy(turn_deadline=10.0, clock=clock), ["p"], invoke, [])
        assert ei.value.reason == "deadline"


def test_exhausted_exception_carries_fields():
    err = LlmResilienceExhausted(["a", "b"], ValueError("x"), "ValueError", "deterministic")
    assert isinstance(err, RuntimeError)
    assert "a, b" in str(err)
    assert err.models_tried == ["a", "b"]
    assert err.last_error_type == "ValueError"
    assert err.reason == "deterministic"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
