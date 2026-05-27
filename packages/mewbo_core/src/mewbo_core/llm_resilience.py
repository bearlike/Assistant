#!/usr/bin/env python3
"""LLM retry / fallback resilience as a small set of atomic objects.

``RetryStrategy`` holds the policy knobs plus the live retry-budget and
circuit-breaker state; its methods describe the behaviour over that state. The
tool-use loop builds one per run (``from_config``) and drives it with injected
I/O (the model call, event emission, reactive compaction), so the state machine
is testable without a live model or the loop. ``DoomLoopGuard`` is the matching
object for no-progress detection.

See Gitea issue #4 for the failure taxonomy and the rationale behind the
defaults.
"""

from __future__ import annotations

import asyncio
import json
import random
import time as _time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langchain_core.messages import AIMessage

# Defaults are the single source of truth — config.py imports them for the
# ``agent.retry`` field defaults. Calibrated for interactive agent turns:
# longer waits than vendor-SDK retry defaults, to tolerate capacity provisioning.
DEFAULT_TIMEOUT = 120.0
DEFAULT_PRIMARY_RETRIES = 3
DEFAULT_FALLBACK_RETRIES = 1
DEFAULT_BACKOFF_BASE = 1.0
DEFAULT_BACKOFF_CAP = 60.0
DEFAULT_RETRY_AFTER_CAP = 60.0
DEFAULT_TURN_DEADLINE = 240.0  # 0 disables the wall-clock terminator
DEFAULT_BUDGET_CAPACITY = 24.0
DEFAULT_BUDGET_RETRY_COST = 1.0
DEFAULT_BUDGET_SUCCESS_CREDIT = 0.3
DEFAULT_CB_THRESHOLD = 3
DEFAULT_CB_COOLDOWN = 30.0
DEFAULT_DOOM_LOOP_THRESHOLD = 3

# Provider/proxy substrings marking a condition hopeless on the *current* model
# but recoverable on a *different* one. Narrow on purpose — users never
# maintain provider exception names.
_SWITCH_HINTS: tuple[str, ...] = (
    "no deployments available",  # proxy/router exhausted this model
    "out of extra usage",  # provider billing/quota exhaustion
    "insufficient_quota",
    "insufficient credits",
    "exceeded your current quota",
    "billing",
)

# Deterministic local errors never benefit from a retry.
_DETERMINISTIC: tuple[type[BaseException], ...] = (
    ValueError,
    TypeError,
    KeyError,
    AttributeError,
    IndexError,
    ImportError,
)

_INTERRUPTED_TOOL_RESULT = "[Tool execution was interrupted]"

# Injected I/O contracts for ``RetryStrategy.run``.
InvokeFn = Callable[[str, bool], Awaitable["AIMessage"]]  # (model, is_fallback) -> response
EmitFn = Callable[[dict[str, Any]], None]
CompactFn = Callable[[], Awaitable[bool]]  # compact in place; True if it happened


class RetryAction(str, Enum):
    """What to do with a failed LLM call."""

    RETRY_SAME = "retry_same"  # transient — back off and retry the same model
    SWITCH_MODEL = "switch_model"  # hopeless here, maybe fine elsewhere
    FATAL = "fatal"  # terminal — never retry, never switch


@dataclass(frozen=True)
class ErrorDecision:
    """Outcome of classifying an LLM exception."""

    action: RetryAction
    error_type: str
    reason: str = ""
    retry_after: float | None = None

    @property
    def retryable(self) -> bool:
        """True when the same model should be retried (transient failure)."""
        return self.action is RetryAction.RETRY_SAME


@dataclass
class RetryBudget:
    """Token-bucket retry budget — the cross-turn "stop the storm" guard.

    Drains by ``retry_cost`` per retry, refills by ``success_credit`` per
    success. Retries are refused once the bucket drops to half capacity, so a
    sustained outage degrades to immediate clean errors instead of a storm.
    """

    capacity: float = DEFAULT_BUDGET_CAPACITY
    retry_cost: float = DEFAULT_BUDGET_RETRY_COST
    success_credit: float = DEFAULT_BUDGET_SUCCESS_CREDIT
    _tokens: float = field(init=False)

    def __post_init__(self) -> None:
        """Start the bucket full."""
        self._tokens = float(self.capacity)

    @property
    def tokens(self) -> float:
        """Current token balance."""
        return self._tokens

    def can_retry(self) -> bool:
        """True while the bucket is above half capacity."""
        return self._tokens > self.capacity / 2.0

    def charge(self) -> None:
        """Debit one retry from the budget."""
        self._tokens = max(0.0, self._tokens - self.retry_cost)

    def credit(self) -> None:
        """Refill the budget after a successful call (capped at capacity)."""
        self._tokens = min(self.capacity, self._tokens + self.success_credit)


@dataclass
class CircuitBreaker:
    """Per-model consecutive-failure cooldown.

    A ratio-based breaker needs call volume a single agent loop doesn't have,
    so this trips on consecutive failures instead: after ``threshold`` in a row
    a model is skipped for ``cooldown`` seconds, then probed once. ``clock`` is
    injected for testability.
    """

    threshold: int = DEFAULT_CB_THRESHOLD
    cooldown: float = DEFAULT_CB_COOLDOWN
    clock: Callable[[], float] = _time.monotonic
    _fails: dict[str, int] = field(default_factory=dict)
    _open_until: dict[str, float] = field(default_factory=dict)

    def is_open(self, model: str) -> bool:
        """True while the model is cooling down; half-opens once it elapses."""
        until = self._open_until.get(model)
        if until is None:
            return False
        if self.clock() >= until:
            self._open_until.pop(model, None)
            self._fails[model] = 0
            return False
        return True

    def record_failure(self, model: str) -> None:
        """Count a failure; trip the breaker at the consecutive threshold."""
        if self.threshold <= 0:
            return
        self._fails[model] = self._fails.get(model, 0) + 1
        if self._fails[model] >= self.threshold:
            self._open_until[model] = self.clock() + self.cooldown

    def record_success(self, model: str) -> None:
        """Reset the failure streak and clear any cooldown for the model."""
        self._fails.pop(model, None)
        self._open_until.pop(model, None)


class LlmResilienceExhausted(RuntimeError):
    """Raised when the retry + fallback chain is exhausted for one turn.

    Subclasses ``RuntimeError`` so existing ``except Exception`` handlers in
    the orchestrator still map it to ``done_reason="error"``, while carrying
    structured fields for events/telemetry and one-click recovery.
    """

    def __init__(
        self,
        models_tried: Iterable[str],
        last_error: BaseException | None,
        last_error_type: str,
        reason: str = "exhausted",
    ) -> None:
        """Capture the models tried and the final error for telemetry/recovery."""
        self.models_tried: list[str] = list(models_tried)
        self.last_error = last_error
        self.last_error_type = last_error_type
        self.reason = reason
        super().__init__(
            f"LLM call failed on all models ({', '.join(self.models_tried)}): {last_error}"
        )


@dataclass
class RetryStrategy:
    """Bounded retry/fallback state machine for one tool-use run.

    Holds the policy knobs and the live ``RetryBudget`` / ``CircuitBreaker``
    state; :meth:`run` drives one logical LLM call across the model chain using
    injected I/O. Terminal error classes are checked first: it retries transient
    failures with full-jitter backoff, switches on hopeless-here errors and
    halts on fatal ones — bounded by a per-call attempt count, a wall-clock
    deadline, the retry budget and the circuit breaker. The caller appends the
    returned message *after* :meth:`run` returns, so a retry never duplicates a
    tool call or bloats context with a partial generation.
    """

    timeout: float = DEFAULT_TIMEOUT
    primary_retries: int = DEFAULT_PRIMARY_RETRIES
    fallback_retries: int = DEFAULT_FALLBACK_RETRIES
    backoff_base: float = DEFAULT_BACKOFF_BASE
    backoff_cap: float = DEFAULT_BACKOFF_CAP
    retry_after_cap: float = DEFAULT_RETRY_AFTER_CAP
    turn_deadline: float = DEFAULT_TURN_DEADLINE
    budget: RetryBudget = field(default_factory=RetryBudget)
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    clock: Callable[[], float] = _time.monotonic
    rng: Callable[[], float] = random.random

    @classmethod
    def from_config(cls) -> RetryStrategy:
        """Build a strategy from ``agent.retry`` config (hot-read per run)."""
        from mewbo_core.config import get_config_value as g

        cb_threshold = int(
            g("agent", "retry", "circuit_breaker_threshold", default=DEFAULT_CB_THRESHOLD)
        )
        cb_cooldown = float(
            g("agent", "retry", "circuit_breaker_cooldown", default=DEFAULT_CB_COOLDOWN)
        )
        budget_capacity = float(
            g("agent", "retry", "budget_capacity", default=DEFAULT_BUDGET_CAPACITY)
        )
        return cls(
            timeout=float(g("agent", "llm_call_timeout", default=DEFAULT_TIMEOUT)),
            primary_retries=int(g("agent", "llm_call_retries", default=DEFAULT_PRIMARY_RETRIES)),
            fallback_retries=int(
                g("agent", "retry", "fallback_retries", default=DEFAULT_FALLBACK_RETRIES)
            ),
            backoff_base=float(g("agent", "retry", "backoff_base", default=DEFAULT_BACKOFF_BASE)),
            backoff_cap=float(g("agent", "retry", "backoff_cap", default=DEFAULT_BACKOFF_CAP)),
            retry_after_cap=float(
                g("agent", "retry", "retry_after_cap", default=DEFAULT_RETRY_AFTER_CAP)
            ),
            turn_deadline=float(
                g("agent", "retry", "turn_deadline", default=DEFAULT_TURN_DEADLINE)
            ),
            budget=RetryBudget(capacity=budget_capacity),
            breaker=CircuitBreaker(threshold=cb_threshold, cooldown=cb_cooldown),
        )

    @staticmethod
    def _retry_after(exc: BaseException) -> float | None:
        """Read ``Retry-After`` (seconds) from an error's httpx response."""
        headers = getattr(getattr(exc, "response", None), "headers", None) or {}
        try:
            val = headers.get("retry-after")
        except AttributeError:
            return None
        try:
            return float(val) if val else None
        except (ValueError, TypeError):
            return None

    @staticmethod
    def classify(exc: BaseException) -> ErrorDecision:
        """Classify an LLM exception into the retry / switch / fatal model.

        Terminal classes are checked first. Cancellation is never retried (it
        bubbles past this layer). Context-window overflow is ``switch_model`` so
        the caller can compact-then-switch.
        """
        name = type(exc).__name__
        if isinstance(exc, asyncio.CancelledError):
            return ErrorDecision(RetryAction.FATAL, name, "cancelled")
        if isinstance(exc, asyncio.TimeoutError):
            return ErrorDecision(RetryAction.RETRY_SAME, "TimeoutError", "timeout")

        try:
            import litellm.exceptions as lx
        except ImportError:
            if isinstance(exc, _DETERMINISTIC):
                return ErrorDecision(RetryAction.FATAL, name, "deterministic")
            return ErrorDecision(RetryAction.RETRY_SAME, name, "unknown")

        try:
            msg = str(getattr(exc, "message", "") or str(exc)).lower()
        except Exception:  # noqa: BLE001 — defensive: some exc __str__ raise
            msg = name.lower()

        def _is(attr: str) -> bool:
            cls = getattr(lx, attr, None)
            return cls is not None and isinstance(exc, cls)

        if _is("ContextWindowExceededError"):
            return ErrorDecision(RetryAction.SWITCH_MODEL, name, "context_window")
        if _is("AuthenticationError"):
            # An expired token may refresh on a different deployment; a bad key
            # won't — but switching is the safe, non-looping action either way.
            return ErrorDecision(RetryAction.SWITCH_MODEL, name, "auth")
        if _is("PermissionDeniedError"):
            return ErrorDecision(RetryAction.FATAL, name, "permission_denied")
        if _is("RateLimitError"):
            if any(h in msg for h in _SWITCH_HINTS):
                return ErrorDecision(RetryAction.SWITCH_MODEL, name, "no_deployments")
            ra = RetryStrategy._retry_after(exc)
            return ErrorDecision(RetryAction.RETRY_SAME, name, "rate_limit", retry_after=ra)
        if _is("ContentPolicyViolationError"):
            return ErrorDecision(RetryAction.FATAL, name, "content_policy")
        if _is("BadRequestError"):
            # Quota/billing exhaustion is often surfaced as a 400 invalid request
            # — hopeless on this provider, switchable.
            if any(h in msg for h in _SWITCH_HINTS):
                return ErrorDecision(RetryAction.SWITCH_MODEL, name, "quota_exhausted")
            return ErrorDecision(RetryAction.FATAL, name, "bad_request")
        if _is("Timeout"):
            return ErrorDecision(RetryAction.RETRY_SAME, "Timeout", "timeout")
        if _is("InternalServerError") or _is("ServiceUnavailableError"):
            return ErrorDecision(RetryAction.RETRY_SAME, name, "server_error")
        if _is("BadGatewayError"):
            return ErrorDecision(RetryAction.RETRY_SAME, name, "bad_gateway")
        if _is("APIConnectionError"):
            return ErrorDecision(RetryAction.RETRY_SAME, name, "connection")

        if isinstance(exc, _DETERMINISTIC):
            return ErrorDecision(RetryAction.FATAL, name, "deterministic")
        return ErrorDecision(RetryAction.RETRY_SAME, name, "unknown")

    def backoff(self, attempt: int, retry_after: float | None = None) -> float:
        """Full-jitter exponential backoff: ``random(0, min(cap, base*2^(n-1)))``.

        A server ``Retry-After`` is honored as a *floor* (capped) — the server
        knows its recovery window better than the formula does.
        """
        ceiling = min(self.backoff_cap, self.backoff_base * (2 ** (max(1, attempt) - 1)))
        delay = self.rng() * max(0.0, ceiling)
        if retry_after is not None and retry_after > 0:
            delay = max(delay, min(retry_after, self.retry_after_cap))
        return delay

    def _deadline_reached(self, started: float) -> bool:
        return self.turn_deadline > 0 and (self.clock() - started) > self.turn_deadline

    async def run(
        self,
        *,
        models: Sequence[str],
        invoke: InvokeFn,
        emit: EmitFn,
        compact: CompactFn,
        agent_id: str,
        depth: int,
        step: int,
    ) -> tuple[AIMessage, str]:
        """Obtain one successful response across ``models``; raise on exhaustion.

        Returns ``(response, model_name)``. Raises :class:`LlmResilienceExhausted`
        when every model/attempt is spent. Cancellation bubbles up untouched.
        """
        started = self.clock()
        last_exc: BaseException | None = None
        last_reason = "exhausted"
        tried: list[str] = []
        compacted = False

        for idx, model_name in enumerate(models):
            is_fallback = idx > 0
            # Skip a cooling model when an alternative exists, but never skip the
            # sole primary — there would be nothing left to fall back to.
            if self.breaker.is_open(model_name) and (is_fallback or len(models) > 1):
                continue
            if is_fallback:
                emit(
                    {
                        "type": "llm_fallback",
                        "payload": {
                            "agent_id": agent_id,
                            "depth": depth,
                            "step": step,
                            "from_model": tried[-1] if tried else model_name,
                            "to_model": model_name,
                            "reason": last_reason,
                            "previous_error_type": (
                                type(last_exc).__name__ if last_exc else "Unknown"
                            ),
                        },
                    }
                )
            tried.append(model_name)
            attempts = self.primary_retries if not is_fallback else self.fallback_retries

            attempt = 0
            stop_chain = False
            while attempt < attempts:
                if self._deadline_reached(started):
                    last_reason = "deadline"
                    last_exc = last_exc or TimeoutError("turn deadline exceeded")
                    stop_chain = True
                    break
                attempt += 1
                try:
                    response = await asyncio.wait_for(
                        invoke(model_name, is_fallback), timeout=self.timeout
                    )
                except asyncio.CancelledError:
                    raise  # cancellation is terminal — must bubble past retry
                except Exception as exc:  # noqa: BLE001 — provider errors are opaque
                    last_exc = exc
                    decision = self.classify(exc)
                    last_reason = decision.reason
                    self.breaker.record_failure(model_name)
                    # Context overflow: compact once, then retry the same model.
                    if decision.reason == "context_window" and not compacted and await compact():
                        compacted = True
                        attempt -= 1  # the compaction retry is "free"
                        continue
                    if decision.action is RetryAction.FATAL:
                        stop_chain = True
                        break
                    if decision.action is RetryAction.SWITCH_MODEL:
                        break  # advance to the next model in the chain
                    if not self.budget.can_retry():
                        last_reason = "budget_exhausted"
                        stop_chain = True
                        break
                    if attempt >= attempts:
                        break  # primary exhausted -> next model (if any)
                    self.budget.charge()
                    delay = self.backoff(attempt, decision.retry_after)
                    emit(
                        {
                            "type": "llm_retry",
                            "payload": {
                                "agent_id": agent_id,
                                "depth": depth,
                                "step": step,
                                "model": model_name,
                                "attempt": attempt,
                                "max_attempts": attempts,
                                "error": str(exc)[:200],
                                "error_type": decision.error_type,
                                "delay": delay,
                                "retryable": True,
                            },
                        }
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
                else:
                    self.breaker.record_success(model_name)
                    self.budget.credit()
                    return response, model_name

            if stop_chain:
                break

        raise LlmResilienceExhausted(
            tried, last_exc, type(last_exc).__name__ if last_exc else "Unknown", reason=last_reason
        )


@dataclass
class DoomLoopGuard:
    """Detects a model stuck repeating the same tool + input with no progress.

    Holds the recent tool-call signatures; the loop calls :meth:`observe` each
    turn and :meth:`is_stuck` to decide whether to halt cleanly. ``threshold
    <= 0`` disables detection.
    """

    threshold: int = DEFAULT_DOOM_LOOP_THRESHOLD
    _signatures: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls) -> DoomLoopGuard:
        """Build a guard from ``agent.retry.doom_loop_threshold``."""
        from mewbo_core.config import get_config_value

        return cls(
            threshold=int(
                get_config_value(
                    "agent", "retry", "doom_loop_threshold", default=DEFAULT_DOOM_LOOP_THRESHOLD
                )
            )
        )

    @staticmethod
    def signature(tool_calls: Sequence[Any]) -> str:
        """Stable signature of a tool-call batch (name + sorted args, no id)."""
        parts: list[str] = []
        for tc in tool_calls or []:
            if isinstance(tc, dict):
                tc_name, tc_args = tc.get("name", ""), tc.get("args", {})
            else:
                tc_name, tc_args = getattr(tc, "name", ""), getattr(tc, "args", {})
            try:
                rendered = json.dumps(tc_args, sort_keys=True, default=str)
            except (TypeError, ValueError):
                rendered = repr(tc_args)
            parts.append(f"{tc_name}:{rendered}")
        return "|".join(parts)

    def observe(self, tool_calls: Sequence[Any]) -> None:
        """Record this turn's tool-call batch."""
        self._signatures.append(self.signature(tool_calls))

    def is_stuck(self) -> bool:
        """True when the last ``threshold`` observed batches are identical."""
        if self.threshold <= 0 or len(self._signatures) < self.threshold:
            return False
        tail = self._signatures[-self.threshold :]
        return all(s == tail[0] and s != "" for s in tail)


def repair_tool_pairing(messages: list[Any]) -> int:
    """Rebalance tool_use / tool_result pairs in place; return repairs made.

    The model API rejects a dangling ``tool_use`` or an orphan ``tool_result``.
    A compaction slice can orphan a pair, so this drops orphan results and
    synthesizes an interrupted result for any unanswered call.
    """
    from langchain_core.messages import AIMessage, ToolMessage

    declared: set[str] = set()
    for m in messages:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls or []:
                tcid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tcid:
                    declared.add(tcid)

    repaired = 0
    answered: set[str] = set()
    kept: list[Any] = []
    for m in messages:
        if isinstance(m, ToolMessage):
            if m.tool_call_id not in declared:
                repaired += 1  # orphan tool_result — drop it
                continue
            answered.add(m.tool_call_id)
        kept.append(m)

    rebuilt: list[Any] = []
    for m in kept:
        rebuilt.append(m)
        if isinstance(m, AIMessage) and m.tool_calls:
            for tc in m.tool_calls:
                tcid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tcid and tcid not in answered:
                    rebuilt.append(
                        ToolMessage(content=_INTERRUPTED_TOOL_RESULT, tool_call_id=tcid)
                    )
                    answered.add(tcid)
                    repaired += 1

    if repaired:
        messages[:] = rebuilt
    return repaired
