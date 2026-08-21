#!/usr/bin/env python3
"""The result contract for one model call, driven from both sides of the seam.

An HTTP 200 carrying neither text nor a tool call used to satisfy the loop's
completion predicate exactly as a real answer did, so the ladder never saw it:
the run completed, the retry budget was credited and the circuit breaker was
cleared for a model that produced nothing. These tests pin the contract at the
strategy (where it is enforced) and at the loop (where the symptom was visible),
plus the reporting half — a run that escalated must name the model that served
it, not the one frozen at construction.

Only the I/O boundary is stubbed: ``build_chat_model`` and the bound model's
``ainvoke``. No network, no clock patching.

Every stub asserting emptiness carries an explicit zero-token ``usage_metadata``,
because that is the ONE signal the contract convicts on and the one the adapter
genuinely populates. A stub that instead declared a terminal ``finish_reason``
would be asserting a signal production never sends — the trap that made an
earlier version of this suite green over a live regression.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from mewbo_core.agents.agent_context import AgentContext
from mewbo_core.agents.hypervisor import AgentHypervisor
from mewbo_core.contracts.types import Event
from mewbo_core.hooks import HookManager
from mewbo_core.llm.llm_resilience import (
    CircuitBreaker,
    EmptyModelResponse,
    LlmResilienceExhausted,
    RetryAction,
    RetryBudget,
    RetryStrategy,
)
from mewbo_core.loop.orchestrator import Orchestrator
from mewbo_core.loop.tool_use_loop import ToolUseLoop
from mewbo_core.permissions import PermissionDecision, PermissionPolicy
from mewbo_core.session.session_store import SessionStore
from mewbo_core.tooling.tool_registry import ToolRegistry, ToolSpec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _broken_response() -> AIMessage:
    """A response the result contract convicts: empty, with usage proving zero output.

    The zero-token usage is not decoration — it is the ONE signal the contract
    convicts on, and the one the adapter genuinely populates on both the streamed
    and the buffered path. A bare ``AIMessage(content="")`` carries no usage,
    which the contract deliberately reads as "not proven" and lets through, so a
    stub written that way would test the opposite of what its name claims.
    """
    return AIMessage(
        content="",
        usage_metadata={"input_tokens": 128, "output_tokens": 0, "total_tokens": 128},
    )


def _spec(tool_id: str = "test_tool") -> ToolSpec:
    return ToolSpec(
        tool_id=tool_id,
        name=tool_id,
        description=f"{tool_id} for tests",
        factory=lambda: MagicMock(),
        metadata={"schema": {"type": "object", "properties": {}}},
    )


def _registry(*specs: ToolSpec) -> ToolRegistry:
    registry = ToolRegistry()
    for spec in specs:
        registry.register(spec)
    return registry


def _policy() -> PermissionPolicy:
    policy = MagicMock(spec=PermissionPolicy)
    policy.decide.return_value = PermissionDecision.ALLOW
    return policy


def _hooks() -> HookManager:
    hm = MagicMock(spec=HookManager)
    hm.run_pre_tool_use.side_effect = lambda step: step
    hm.run_post_tool_use.side_effect = lambda step, result: result
    hm.run_permission_request.side_effect = lambda step, decision: decision
    return hm


class _ScriptedProvider:
    """A ``build_chat_model`` stand-in scripting a reply PER MODEL NAME.

    The ladder's whole behaviour is which model it calls next, so a single
    shared stub (the usual shape) cannot observe it. This one records every
    invocation in order, which is the ladder's transcript.
    """

    def __init__(self, script: dict[str, list[AIMessage]]) -> None:
        self.script = script
        self.invocations: list[str] = []

    def __call__(self, *, model_name: str, **_kwargs: object) -> Any:
        model = MagicMock()
        model.bind_tools.return_value = self._bound(model_name)
        return model

    def _bound(self, model_name: str) -> Any:
        async def _ainvoke(_messages: Any, config: Any = None, **_kwargs: Any) -> AIMessage:
            self.invocations.append(model_name)
            replies = self.script.get(model_name) or [_broken_response()]
            seen = self.invocations.count(model_name)
            return replies[min(seen - 1, len(replies) - 1)]

        bound = MagicMock()
        bound.ainvoke = AsyncMock(side_effect=_ainvoke)
        # Absent-shaped so ``_acall_model`` takes the buffered path; a MagicMock
        # attribute would be iterated as a coroutine.
        del bound.astream
        return bound


def _loop(provider_models: tuple[str, ...], events: list[Event]) -> ToolUseLoop:
    ctx = AgentContext.root(
        model_name=provider_models[0],
        max_depth=5,
        registry=AgentHypervisor(max_concurrent=100),
        fallback_models=tuple(provider_models[1:]),
        event_logger=events.append,
    )
    return ToolUseLoop(
        agent_context=ctx,
        tool_registry=_registry(_spec()),
        permission_policy=_policy(),
        hook_manager=_hooks(),
    )


def _drive(loop: ToolUseLoop, provider: _ScriptedProvider) -> Any:
    """Run the loop against *provider*, with a strategy that never sleeps.

    ``from_config`` is replaced rather than the clock patched: the backoff is
    real code the ladder runs, and zeroing its jitter source is the one input
    that makes it instant without changing which branch executes.
    """
    with (
        patch("mewbo_core.loop.tool_use_loop.build_chat_model", provider),
        patch.object(RetryStrategy, "from_config", classmethod(lambda cls: _strategy())),
    ):
        return asyncio.run(loop.run("do the task", tool_specs=[_spec()]))


# ---------------------------------------------------------------------------
# The predicate — pure, so every shape a provider emits is driven directly
# ---------------------------------------------------------------------------


class TestEmptinessPredicate:
    """ONE definition of "this response carried nothing", covering every shape."""

    @pytest.mark.parametrize(
        "content",
        [
            "",
            "   ",
            [],
            [{"type": "text", "text": ""}],
            [{"type": "thinking", "thinking": "hmm"}],
            RetryStrategy.NO_CONTENT_PLACEHOLDER,
            [{"type": "text", "text": RetryStrategy.NO_CONTENT_PLACEHOLDER}],
        ],
    )
    def test_empty_shapes(self, content: object) -> None:
        """Every empty shape convicts — but ONLY once usage proves zero output.

        Emptiness alone is deliberately not the rule; see
        ``test_an_empty_turn_that_generated_tokens_is_accepted``.
        """
        zero = {"input_tokens": 12, "output_tokens": 0, "total_tokens": 12}
        assert RetryStrategy.response_is_broken(
            AIMessage(content=content, usage_metadata=zero)
        )

    def test_text_is_not_empty(self) -> None:
        zero = {"input_tokens": 12, "output_tokens": 0, "total_tokens": 12}
        assert not RetryStrategy.response_is_broken(
            AIMessage(content="an answer", usage_metadata=zero)
        )
        assert not RetryStrategy.response_is_broken(
            AIMessage(content=[{"type": "text", "text": "an answer"}], usage_metadata=zero)
        )

    def test_a_tool_call_alone_is_a_usable_result(self) -> None:
        """No text but a tool call is a working turn, not an empty response."""
        message = AIMessage(
            content="",
            tool_calls=[{"name": "t", "args": {}, "id": "c1"}],
            usage_metadata={"input_tokens": 12, "output_tokens": 0, "total_tokens": 12},
        )
        assert not RetryStrategy.response_is_broken(message)

    def test_an_empty_turn_that_generated_tokens_is_accepted(self) -> None:
        """A model with nothing left to add is a HEALTHY turn, not a failed rung.

        A child that did its work through tool writes ends with no text, and
        arriving at that still costs output tokens. Convicting it would escalate
        a working model over an ordinary turn — the regression this arm exists to
        avoid.
        """
        message = AIMessage(
            content="",
            usage_metadata={"input_tokens": 900, "output_tokens": 4, "total_tokens": 904},
        )
        assert not RetryStrategy.response_is_broken(message)

    def test_zero_output_tokens_is_the_conviction(self) -> None:
        """The reported failure shape: a 200 that generated literally nothing."""
        message = AIMessage(
            content="",
            usage_metadata={"input_tokens": 232192, "output_tokens": 0, "total_tokens": 232192},
        )
        assert RetryStrategy.generated_nothing(message)
        assert RetryStrategy.response_is_broken(message)

    @pytest.mark.parametrize("key", ["finish_reason", "stop_reason"])
    @pytest.mark.parametrize("slot", ["response_metadata", "additional_kwargs"])
    def test_a_declared_ending_does_not_rescue_a_zero_token_turn(
        self, slot: str, key: str
    ) -> None:
        """A terminal reason must NOT exempt anything — it never arrives.

        The adapter puts the provider's finish reason on the GENERATION, not the
        message, and ``ainvoke`` returns the message alone; the streamed path
        carries none at all. A predicate gated on it would be False for every
        real response and would silently degrade this contract to "empty means
        broken". Pinned so a future reader cannot reintroduce that gate from
        first principles and be misled by a stub that declares it.
        """
        message = AIMessage(
            content="",
            usage_metadata={"input_tokens": 9, "output_tokens": 0, "total_tokens": 9},
            **{slot: {key: "stop"}},
        )
        assert RetryStrategy.response_is_broken(message)

    def test_absent_usage_never_convicts(self) -> None:
        """Only a number the provider reported may condemn a rung."""
        assert not RetryStrategy.generated_nothing(AIMessage(content=""))
        assert not RetryStrategy.response_is_broken(AIMessage(content=""))
        assert not RetryStrategy.generated_nothing(
            AIMessage(
                content="",
                usage_metadata={"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
            )
        )

    def test_the_loop_reads_the_same_definition(self) -> None:
        """A second emptiness rule is how the evidence came to be discarded."""
        assert ToolUseLoop._extract_text_content(
            [{"type": "text", "text": RetryStrategy.NO_CONTENT_PLACEHOLDER}]
        ) == ""
        assert ToolUseLoop._extract_text_content("hello") == "hello"


class TestClassification:
    def test_an_empty_result_is_retried_on_the_same_model_first(self) -> None:
        """Transient, so the same model gets its attempts before the chain moves."""
        decision = RetryStrategy.classify(EmptyModelResponse("model-a"))
        assert decision.action is RetryAction.RETRY_SAME
        assert decision.reason == "empty_response"
        assert decision.retryable


# ---------------------------------------------------------------------------
# The strategy — where the contract is enforced
# ---------------------------------------------------------------------------


def _run_strategy(
    strategy: RetryStrategy, models: list[str], script: dict[str, AIMessage]
) -> tuple[Any, list[dict[str, Any]], list[str]]:
    """Drive one logical call, returning ``(result, events, models_invoked)``."""
    events: list[dict[str, Any]] = []
    invoked: list[str] = []

    async def _invoke(model_name: str, _is_fallback: bool) -> AIMessage:
        invoked.append(model_name)
        return script.get(model_name, _broken_response())

    async def _compact() -> bool:
        return False

    async def _go() -> Any:
        return await strategy.run(
            models=models,
            invoke=_invoke,
            emit=events.append,
            compact=_compact,
            agent_id="root",
            depth=0,
            step=1,
        )

    return asyncio.run(_go()), events, invoked


def _strategy(**kwargs: Any) -> RetryStrategy:
    """A strategy whose backoff is zero, so a retry test never sleeps."""
    kwargs.setdefault("breaker", CircuitBreaker(threshold=99))
    return RetryStrategy(rng=lambda: 0.0, **kwargs)


class TestStrategyContract:
    def test_the_same_model_is_retried_before_the_chain_advances(self) -> None:
        """Two attempts on the primary, then the fallback — a timeout's shape."""
        strategy = _strategy(primary_retries=2, fallback_retries=1)
        (response, model), events, invoked = _run_strategy(
            strategy,
            ["model-a", "model-b"],
            {"model-b": AIMessage(content="the real answer")},
        )
        assert model == "model-b"
        assert response.content == "the real answer"
        assert invoked == ["model-a", "model-a", "model-b"]
        retries = [e for e in events if e["type"] == "llm_retry"]
        assert [r["payload"]["error_type"] for r in retries] == ["EmptyModelResponse"]
        fallbacks = [e for e in events if e["type"] == "llm_fallback"]
        assert [f["payload"]["reason"] for f in fallbacks] == ["retries_exhausted"]
        assert fallbacks[0]["payload"]["to_model"] == "model-b"

    def test_an_empty_rung_credits_nothing_and_is_benched(self) -> None:
        """The empty path must not make a model that returns nothing look healthy.

        A single-rung chain, so the only budget movement observable is the empty
        call's own — a later rung's success credits the same bucket and would
        mask it. The budget is CHARGED (a retry costs what any retry costs);
        what must never happen is a credit.
        """
        strategy = _strategy(
            budget=RetryBudget(capacity=10.0),
            breaker=CircuitBreaker(threshold=1),
            primary_retries=2,
        )
        start = strategy.budget.tokens
        with pytest.raises(LlmResilienceExhausted):
            _run_strategy(strategy, ["model-a"], {})
        assert strategy.budget.tokens < start
        assert strategy.breaker.is_open("model-a")

    def test_every_rung_empty_exhausts_cleanly(self) -> None:
        """Terminates through the normal exhaustion path rather than spinning."""
        strategy = _strategy(primary_retries=2, fallback_retries=1)
        with pytest.raises(LlmResilienceExhausted) as caught:
            _run_strategy(strategy, ["model-a", "model-b", "model-c"], {})
        assert caught.value.models_tried == ["model-a", "model-b", "model-c"]
        assert caught.value.reason == "empty_response"
        assert strategy._pinned_model is None


# ---------------------------------------------------------------------------
# The loop — the surface the symptom was visible on
# ---------------------------------------------------------------------------


class TestLoopContract:
    def test_an_empty_primary_escalates_instead_of_completing(self) -> None:
        events: list[Event] = []
        provider = _ScriptedProvider(
            {
                "model-a": [_broken_response()],
                "model-b": [AIMessage(content="the real answer")],
            }
        )
        loop = _loop(("model-a", "model-b"), events)
        task_queue, state = _drive(loop, provider)

        # Two attempts on the primary (an empty result is transient first),
        # then the fallback.
        assert provider.invocations == ["model-a", "model-a", "model-b"]
        assert state.done_reason == "completed"
        assert task_queue.task_result == "the real answer"
        assert loop.active_model == "model-b"
        assert [
            e["payload"]["to_model"] for e in events if e["type"] == "llm_fallback"
        ] == ["model-b"]

    def test_an_all_empty_ladder_fails_the_run(self) -> None:
        """Never a clean terminal: exhaustion RAISES out of the loop."""
        events: list[Event] = []
        provider = _ScriptedProvider({})
        loop = _loop(("model-a", "model-b"), events)
        with pytest.raises(LlmResilienceExhausted) as caught:
            _drive(loop, provider)
        assert caught.value.models_tried == ["model-a", "model-b"]
        assert provider.invocations == ["model-a", "model-a", "model-b"]

        # The failed ``llm_call_end`` (success=False) carries no ``duration_ms`` —
        # that field lives only on the successful-end payload shape.
        ends = [e for e in events if e["type"] == "llm_call_end"]
        assert ends and all(e["payload"]["success"] is False for e in ends)
        assert all("duration_ms" not in e["payload"] for e in ends)


# ---------------------------------------------------------------------------
# The streaming seam — "no stream" and "an empty stream" are different facts
# ---------------------------------------------------------------------------


class _Streamer:
    """A bound model whose ``astream`` yields *chunks*, then raises *raises*.

    ``astream`` is a genuine async generator FUNCTION, exactly as
    ``BaseChatModel.astream`` and the ``RunnableBinding`` from ``bind_tools``
    are — that is what the seam's capability probe reads, so a stub written any
    other way would take the buffered branch and test nothing.
    """

    def __init__(
        self, chunks: list[AIMessageChunk] | None = None, raises: Exception | None = None
    ) -> None:
        self._chunks = chunks or []
        self._raises = raises
        self.ainvoke_calls = 0

    async def astream(self, _messages: Any, config: Any = None) -> Any:
        for chunk in self._chunks:
            yield chunk
        if self._raises is not None:
            raise self._raises

    async def ainvoke(self, _messages: Any, config: Any = None) -> AIMessage:
        self.ainvoke_calls += 1
        return AIMessage(content="buffered answer")


class TestStreamingSeam:
    def _call(self, bound: _Streamer) -> AIMessage:
        loop = _loop(("model-a",), [])
        return asyncio.run(loop._acall_model(bound, [], config=None, step=1))

    def test_a_model_whose_stream_raises_still_falls_back_to_ainvoke(self) -> None:
        """The stubbed-model case the fallback exists for — unchanged."""
        bound = _Streamer(raises=AttributeError("no astream"))
        assert self._call(bound).content == "buffered answer"
        assert bound.ainvoke_calls == 1

    def test_a_mock_shaped_stream_falls_back_rather_than_failing_the_rung(self) -> None:
        """A ``MagicMock`` iterates EMPTY instead of raising.

        So it is indistinguishable from a provider that streamed nothing, and
        the safe reading of that ambiguity is the buffered call — never a failed
        rung invented from a test double. The capability probe is what draws the
        line.
        """
        bound = MagicMock()
        bound.ainvoke = AsyncMock(return_value=AIMessage(content="buffered answer"))
        assert self._call(bound).content == "buffered answer"
        assert bound.ainvoke.await_count == 1

    def test_a_real_stream_that_yields_nothing_is_never_re_issued(self) -> None:
        """The conflation: a second full call, billed, uncounted, un-evented."""
        bound = _Streamer(chunks=[])
        result = self._call(bound)
        assert bound.ainvoke_calls == 0
        # An EXPLICIT zero-token usage, not merely empty content: absent usage
        # never convicts, so a bare empty message would sail through the one
        # rule this seam is handing the verdict to.
        assert result.usage_metadata == {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }
        assert RetryStrategy.response_is_broken(result)
        # The ladder — not this seam — decides what happens next.
        assert RetryStrategy.classify(EmptyModelResponse("model-a")).retryable

    def test_a_fault_after_real_chunks_propagates(self) -> None:
        """Re-issuing here would run a tool the partial stream already carried."""
        bound = _Streamer(
            chunks=[AIMessageChunk(content="par")], raises=TypeError("mid-stream")
        )
        with pytest.raises(TypeError):
            self._call(bound)
        assert bound.ainvoke_calls == 0


# ---------------------------------------------------------------------------
# Reporting — which model a failed run names
# ---------------------------------------------------------------------------


class TestServedModelIsReported:
    def test_a_run_escalating_a_to_c_reports_c(self) -> None:
        """Three rungs, the first two empty: the loop ends active on the third."""
        events: list[Event] = []
        provider = _ScriptedProvider({"model-c": [AIMessage(content="answer")]})
        loop = _loop(("model-a", "model-b", "model-c"), events)
        _drive(loop, provider)
        assert loop.active_model == "model-c"
        ends = [e for e in events if e["type"] == "llm_call_end"]
        assert ends[-1]["payload"]["model"] == "model-c"

    def test_the_failure_record_names_the_served_model(self, tmp_path) -> None:
        """``error_detail.provider`` is what an operator benches a model from."""
        store = SessionStore(root_dir=str(tmp_path))
        orch = Orchestrator(session_store=store, model_name="model-a")
        session_id = store.create_session()

        async def _escalate_then_die(self: ToolUseLoop, *_args: Any, **_kwargs: Any):
            # Exactly what ``_apply_model_escalation`` does on a sticky switch.
            self._active_model = "model-c"
            raise RuntimeError("the escalated model then failed")

        with patch.object(ToolUseLoop, "run", _escalate_then_die):
            orch.run(user_query="hi", session_id=session_id, max_iters=1)

        completion = next(
            e for e in store.load_transcript(session_id) if e.get("type") == "completion"
        )
        assert completion["payload"]["error_detail"]["provider"] == "model-c"
