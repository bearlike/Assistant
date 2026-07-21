#!/usr/bin/env python3
"""LLM retry / fallback resilience as a small set of atomic objects.

``RetryStrategy`` holds the policy knobs plus the live retry-budget and
circuit-breaker state; its methods describe the behaviour over that state. The
tool-use loop builds one per run (``from_config``) and drives it with injected
I/O (the model call, event emission, reactive compaction), so the state machine
is testable without a live model or the loop. ``DoomLoopGuard`` is the matching
object for no-progress detection.

See the design notes for the failure taxonomy and the rationale behind the
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

from mewbo_core.run_error import render_exception

if TYPE_CHECKING:
    from langchain_core.messages import AIMessage

    from mewbo_core.tool_registry import ToolSpec
    from mewbo_core.types import LlmFallbackPayload, LlmRetryPayload

# Defaults are the single source of truth — config.py imports them for the
# ``agent.retry`` field defaults. Calibrated for interactive agent turns:
# longer waits than vendor-SDK retry defaults, to tolerate capacity provisioning.
DEFAULT_TIMEOUT = 120.0
# One try + one retry, then advance to the next model. A 3rd same-model attempt
# rarely recovers a transient fault that survived a backed-off retry; the run is
# better served by escalating to a healthy model (which is then pinned — see
# ``RetryStrategy._pinned_model``).
DEFAULT_PRIMARY_RETRIES = 2
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
# Consecutive non-write tool-execution steps before the write-progress signal
# fires telemetry for a write-capable agent that keeps exploring instead of
# acting. The event repeats every ``event_interval`` steps thereafter, up to
# ``max_events``.
DEFAULT_WRITE_PROGRESS_THRESHOLD = 25
DEFAULT_WRITE_PROGRESS_EVENT_INTERVAL = 10
DEFAULT_WRITE_PROGRESS_MAX_EVENTS = 2

# Synchronization / wait primitives are NOT progress-making work. Polling them
# (e.g. ``check_agents`` while spawned children finish one by one) is the
# intended epoll pattern — repeated identical calls there are healthy waiting,
# never a doom loop. They are dropped from the doom signature entirely.
#
# This is only the BUILT-IN seed of the poll class, never the whole of it: any
# tool whose documented contract is "call me again until my run settles"
# belongs here, and a hardcoded id cannot know about one. ``DoomLoopGuard``
# resolves its effective rules from this seed PLUS whatever a tool DECLARES
# (``ToolSpec.poll`` / ``poll_when_args``, or a session tool's ``poll_class`` /
# ``poll_when_args``) and whatever the operator lists in
# ``agent.retry.poll_tools`` — see :meth:`DoomLoopGuard.from_config`. A
# self-polling nested run whose first two probes both answer "processing" is
# honest waiting; halting it there was a 100%-reproducible false positive.
DOOM_LOOP_EXEMPT_TOOLS: frozenset[str] = frozenset({"check_agents"})

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

# An unknown/retired model id surfaces as a 400 too (e.g. the proxy dropped a
# model a persisted submission still names) — hopeless on THIS model but
# recoverable on a fallback, so it is ``switch_model``, not a fatal malformed
# request. Paired with a ``"model"`` token check to avoid matching unrelated
# 400s ("file does not exist" etc.).
_INVALID_MODEL_HINTS: tuple[str, ...] = (
    "invalid model",
    "model not found",
    "does not exist",
    "no such model",
    "unknown model",
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
            f"LLM call failed on all models ({', '.join(self.models_tried)}): "
            f"{self.describe_error(last_error)}"
        )

    @staticmethod
    def describe_error(exc: BaseException | None) -> str:
        """Render *exc* for the exhaustion message — never as the empty string.

        Several exception classes stringify to ``""`` (``TimeoutError`` is the
        one that bites: the classifier's most common transient verdict), and
        this seam used to preserve the void verbatim — a run's terminal failure
        reached the store, the console and the next recovery turn as
        ``"LLM call failed on all models (<model>): "`` with the cause erased.
        That string is the durable forensic record after a trace ages out, so
        the cause must survive in it.

        Delegates the ``"<Type>: <msg>"`` rendering to
        :func:`mewbo_core.run_error.render_exception`, the SAME renderer the
        Langfuse span's ``status_message`` (``components.py:
        _span_status_message``) uses, so the two records of one failure never
        disagree. ``exc is None`` and an exception whose own ``__str__`` raises
        are handled here rather than in the shared renderer — neither is a
        genuine "exception to render": one has nothing to render, the other
        can't even produce ``str(exc)``.
        """
        if exc is None:
            return "Unknown"
        try:
            return render_exception(exc, limit=500)
        except Exception:  # noqa: BLE001 — defensive: some exc __str__ raise
            return type(exc).__name__[:500]


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
    # Sticky escalation: once a rescue model wins (the configured primary was
    # not the survivor), it is pinned and tried first on every subsequent turn —
    # so a dead primary is not re-probed each step. ``None`` until an escalation
    # succeeds; the circuit breaker + budget still bound any further switching.
    _pinned_model: str | None = None

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
            # An unknown/retired model id is a 400 too — switch to a fallback
            # rather than dying fatally on this one.
            if "model" in msg and any(h in msg for h in _INVALID_MODEL_HINTS):
                return ErrorDecision(RetryAction.SWITCH_MODEL, name, "invalid_model")
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

        # The configured primary is always ``models[0]``; capture it BEFORE any
        # reorder so a successful rescue model can be recognised as != primary
        # (and then pinned). The pin reorders the chain so a dead primary is not
        # re-probed every turn — the breaker + budget still bound the switching.
        primary = models[0] if models else ""
        ordered = self._order_models(models)

        # Why the chain advanced to the model now being tried. Distinct from
        # ``last_reason`` (the final telemetry reason): a transient error that
        # exhausts the per-model cap advances with ``retries_exhausted`` even
        # though the underlying ``last_reason`` is e.g. ``timeout``.
        advance_reason = last_reason

        for idx, model_name in enumerate(ordered):
            is_fallback = idx > 0
            # Skip a cooling model when an alternative exists, but never skip the
            # sole primary — there would be nothing left to fall back to.
            if self.breaker.is_open(model_name) and (is_fallback or len(ordered) > 1):
                continue
            if is_fallback:
                # Escalation is always sticky: the destination model is pinned
                # for the rest of the run on success (see the success branch).
                payload: LlmFallbackPayload = {
                    "agent_id": agent_id,
                    "depth": depth,
                    "step": step,
                    "from_model": tried[-1] if tried else model_name,
                    "to_model": model_name,
                    "reason": advance_reason,
                    "previous_error_type": (type(last_exc).__name__ if last_exc else "Unknown"),
                    "sticky": True,
                }
                emit({"type": "llm_fallback", "payload": payload})
            tried.append(model_name)
            attempts = self.primary_retries if not is_fallback else self.fallback_retries

            attempt = 0
            stop_chain = False
            while attempt < attempts:
                if self._deadline_reached(started):
                    last_reason = advance_reason = "deadline"
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
                        advance_reason = decision.reason  # hopeless-here → carry the cause
                        break  # advance to the next model in the chain
                    if not self.budget.can_retry():
                        last_reason = advance_reason = "budget_exhausted"
                        stop_chain = True
                        break
                    if attempt >= attempts:
                        # Per-model retry cap tripped on a transient error: the
                        # advance is "we gave up retrying", not a hopeless class.
                        # Charge the budget for this final failed call too — else
                        # exhausting a model is "free" and a chain of single-try
                        # fallbacks never depletes the cross-turn storm guard.
                        self.budget.charge()
                        advance_reason = "retries_exhausted"
                        break  # primary exhausted -> next model (if any)
                    self.budget.charge()
                    delay = self.backoff(attempt, decision.retry_after)
                    retry_payload: LlmRetryPayload = {
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
                    }
                    emit({"type": "llm_retry", "payload": retry_payload})
                    if delay > 0:
                        await asyncio.sleep(delay)
                else:
                    self.breaker.record_success(model_name)
                    self.budget.credit()
                    # Sticky escalation: if a rescue model (not the configured
                    # primary) won, pin it so the next turn tries it first.
                    if model_name != primary:
                        self._pinned_model = model_name
                    return response, model_name

            if stop_chain:
                break

        raise LlmResilienceExhausted(
            tried, last_exc, type(last_exc).__name__ if last_exc else "Unknown", reason=last_reason
        )

    def _order_models(self, models: Sequence[str]) -> list[str]:
        """Return the model chain with a pinned rescue model tried first.

        Encapsulates the sticky-escalation reorder so the driver keeps passing
        ``[primary, *fallback_models]`` unchanged each turn. When nothing is
        pinned (or the pin is no longer in the chain), order is unchanged.
        """
        pinned = self._pinned_model
        if pinned and pinned in models:
            return [pinned, *(m for m in models if m != pinned)]
        return list(models)


@dataclass(frozen=True)
class PollClassRule:
    """Declares when a call to one tool is POLLING rather than progress.

    A name-only exemption set is what produced the original false positive, and
    a name-only set cannot express the shape that actually occurs: the tool that
    self-polls is usually the SAME tool that starts the work, distinguishable
    only by argument. ``agentic_search`` is the live case — one tool id whose
    args require exactly one of ``query`` (start the run, returns immediately)
    or ``run_id`` (fetch the run's state). Starting is progress; fetching is
    waiting. Exempting the id outright would blind the guard to a genuinely
    stuck agent re-issuing the same search forever.

    ``when_args`` empty means unconditional (``check_agents``, a pure wait
    primitive). Otherwise the call is a poll only when it carries one of the
    named arguments. Arguments arrive as a METHOD ARG — this model reads no
    state of its own, which is what lets a test drive every case directly.
    """

    tool_id: str
    when_args: frozenset[str] = frozenset()

    def matches(self, tool_name: str, args: Any = None) -> bool:
        """True when *tool_name* called with *args* is a poll under this rule."""
        if tool_name != self.tool_id:
            return False
        if not self.when_args:
            return True
        if not isinstance(args, dict):
            return False
        # A key present but empty (``run_id=None`` / ``""``) is not a poll: the
        # tool schema treats it as absent, so the guard must too.
        return any(args.get(key) not in (None, "", [], {}) for key in self.when_args)


# The built-in rules: a pure wait primitive, exempt on every call.
_DEFAULT_POLL_RULES: tuple[PollClassRule, ...] = tuple(
    PollClassRule(tool_id) for tool_id in sorted(DOOM_LOOP_EXEMPT_TOOLS)
)


def _call_name_and_args(call: Any) -> tuple[str, Any]:
    """Read ``(name, args)`` off a tool call in either dict or attribute form."""
    if isinstance(call, dict):
        return call.get("name", ""), call.get("args", {})
    return getattr(call, "name", ""), getattr(call, "args", {})


@dataclass
class DoomLoopGuard:
    """Detects a model genuinely stuck — same action, same OUTCOME, no progress.

    A doom loop is *not* merely "the same tool input repeated": a model that
    re-issues an identical call whose **result advances** (e.g. polling a wait
    primitive whose state moves on, or accumulating into a growing structure)
    is making progress and must not be halted. The guard therefore tracks both
    the per-turn input signature (:meth:`observe`, pre-execution) and the result
    signature (:meth:`record_result`, post-execution); :meth:`is_stuck` halts
    only when the last ``threshold`` turns share an identical input **and** an
    identical result. ``threshold <= 0`` disables detection.

    POLL-CLASS calls are dropped from both signatures — polling while the thing
    waited on runs is intended epoll, never a loop. The class is RESOLVED from
    declarations, not hardcoded (see :attr:`poll_rules` / :meth:`from_config`),
    and it is per-CALL rather than per-tool: ``check_agents`` is merely the
    built-in seed, and the tool that matters is exempt only on the argument
    shape that means "fetch state", never on the one that means "start work".
    """

    threshold: int = DEFAULT_DOOM_LOOP_THRESHOLD
    # Effective poll rules for THIS run. Defaults to the built-in seed so a
    # directly-constructed guard behaves exactly as before; the loop builds it
    # from the seed ∪ tool-declared ∪ operator-declared rules.
    poll_rules: tuple[PollClassRule, ...] = _DEFAULT_POLL_RULES
    _signatures: list[str] = field(default_factory=list)
    _results: list[str] = field(default_factory=list)
    # Tool ids whose CALL was a poll in the just-observed turn. Carried across
    # the observe → record_result pair because a result carries no arguments:
    # without it an argument-sensitive exemption could drop a call from the
    # input signature but keep its result, leaving the two out of step.
    _last_poll_ids: frozenset[str] = frozenset()

    @classmethod
    def from_config(
        cls, *, extra_poll_rules: Iterable[PollClassRule] = ()
    ) -> DoomLoopGuard:
        """Build a guard from ``agent.retry.doom_loop_threshold``.

        *extra_poll_rules* carries what the CALLER resolved for this run from
        tool declarations. Combined with the built-in seed and the operator's
        ``agent.retry.poll_tools`` list (names, so unconditional), which means a
        tool whose contract is "call me again until my run settles" is
        declarable at whichever seam owns it — and adding the next such tool
        never requires editing this module.
        """
        from mewbo_core.config import get_config_value

        declared = get_config_value("agent", "retry", "poll_tools", default=[])
        if isinstance(declared, str):
            declared = [part.strip() for part in declared.split(",") if part.strip()]
        rules: dict[tuple[str, frozenset[str]], PollClassRule] = {}
        for rule in (
            *_DEFAULT_POLL_RULES,
            *(PollClassRule(str(name)) for name in (declared or [])),
            *extra_poll_rules,
        ):
            rules[(rule.tool_id, rule.when_args)] = rule
        return cls(
            threshold=int(
                get_config_value(
                    "agent", "retry", "doom_loop_threshold", default=DEFAULT_DOOM_LOOP_THRESHOLD
                )
            ),
            poll_rules=tuple(rules.values()),
        )

    @staticmethod
    def is_poll_call(
        tool_name: str,
        args: Any = None,
        poll_rules: tuple[PollClassRule, ...] = _DEFAULT_POLL_RULES,
    ) -> bool:
        """True when this specific CALL counts as polling under *poll_rules*."""
        return any(rule.matches(tool_name, args) for rule in poll_rules)

    @staticmethod
    def poll_call_ids(
        tool_calls: Sequence[Any],
        poll_rules: tuple[PollClassRule, ...] = _DEFAULT_POLL_RULES,
    ) -> frozenset[str]:
        """Tool ids in this batch whose call was a poll (argument-sensitive)."""
        return frozenset(
            name
            for name, args in (_call_name_and_args(tc) for tc in tool_calls or [])
            if DoomLoopGuard.is_poll_call(name, args, poll_rules)
        )

    @staticmethod
    def signature(
        tool_calls: Sequence[Any],
        poll_rules: tuple[PollClassRule, ...] = _DEFAULT_POLL_RULES,
    ) -> str:
        """Stable signature of a tool-call batch (name + sorted args, no id).

        Poll-class CALLS are excluded, so a batch consisting only of them
        yields ``""`` (never counted toward a stuck streak).
        """
        parts: list[str] = []
        for tc in tool_calls or []:
            tc_name, tc_args = _call_name_and_args(tc)
            if DoomLoopGuard.is_poll_call(tc_name, tc_args, poll_rules):
                continue
            try:
                rendered = json.dumps(tc_args, sort_keys=True, default=str)
            except (TypeError, ValueError):
                rendered = repr(tc_args)
            parts.append(f"{tc_name}:{rendered}")
        return "|".join(parts)

    @staticmethod
    def result_signature(
        results: Sequence[Any],
        exclude_ids: frozenset[str] = DOOM_LOOP_EXEMPT_TOOLS,
    ) -> str:
        """Stable signature of a tool-result batch (success + content).

        Drops results for *exclude_ids* so it stays aligned with
        :meth:`signature`. A result carries no arguments, so the caller supplies
        the ids that were polls THIS turn (:meth:`poll_call_ids`) rather than
        this method re-deriving them — an argument-sensitive rule cannot be
        evaluated against a result alone. A *changing* signature across turns
        means the world advanced — i.e. the model is making progress even if it
        called the same tool.
        """
        parts: list[str] = []
        for r in results or []:
            tool_id = getattr(r, "tool_id", "") or ""
            if tool_id in exclude_ids:
                continue
            success = getattr(r, "success", True)
            content = getattr(r, "content", "") or ""
            parts.append(f"{int(bool(success))}:{content}")
        return "|".join(parts)

    def observe(self, tool_calls: Sequence[Any]) -> None:
        """Record this turn's tool-call batch (input signature, pre-execution)."""
        self._last_poll_ids = self.poll_call_ids(tool_calls, self.poll_rules)
        self._signatures.append(self.signature(tool_calls, self.poll_rules))

    def record_result(self, results: Sequence[Any]) -> None:
        """Record this turn's tool results (post-execution) for progress checks."""
        self._results.append(self.result_signature(results, self._last_poll_ids))

    def is_stuck(self) -> bool:
        """True only on genuine no-progress: identical input AND identical result.

        The just-observed turn has no result yet, so progress is judged from the
        prior identical-input turns' results. When no results have been recorded
        at all (a pure-input caller), this falls back to input-identity.
        """
        if self.threshold <= 0 or len(self._signatures) < self.threshold:
            return False
        tail = self._signatures[-self.threshold :]
        if not all(s == tail[0] and s != "" for s in tail):
            return False
        # Inputs are identical. Only a doom loop if the world also stood still.
        if not self._results:
            return True  # legacy/pure-input caller — input identity is all we have
        needed = self.threshold - 1
        if needed <= 0:
            return True
        res_tail = self._results[-needed:]
        if len(res_tail) < needed:
            return False  # not enough executed history yet — keep going
        return all(r == res_tail[0] for r in res_tail)


@dataclass
class WriteProgressSignal:
    """Counts consecutive steps without a write-tier tool execution.

    Distinct from :class:`DoomLoopGuard` (identical input *and* identical
    result = stuck): this tracks privilege TIER, not repetition — a long
    streak of read/execute/search/unknown-tier steps with no WRITE at all,
    even when every one of them is genuinely new work. Armed only when the
    agent could plausibly write at all (the two-gate check computed once at
    loop construction — see ``tool_use_loop.py``); when not armed every
    method here is inert, so a read-only or non-write session pays nothing.

    Observe-only: crossing the threshold emits telemetry (a
    ``write_progress_signal`` event) unconditionally. ``reminder_enabled``
    additionally injects a criterion-blind objective-restatement message — it
    never names this signal or its criteria, so it cannot teach an agent the
    tell it is being measured against.

    ``threshold <= 0`` disables detection entirely.
    """

    write_capable: bool = False
    threshold: int = DEFAULT_WRITE_PROGRESS_THRESHOLD
    event_interval: int = DEFAULT_WRITE_PROGRESS_EVENT_INTERVAL
    max_events: int = DEFAULT_WRITE_PROGRESS_MAX_EVENTS
    reminder_enabled: bool = False
    _nonprogress: int = 0
    _fires: int = 0

    @property
    def steps_since_write(self) -> int:
        """Consecutive steps observed since the last write-tier execution."""
        return self._nonprogress

    @classmethod
    def from_config(cls, *, write_capable: bool = False) -> WriteProgressSignal:
        """Build a signal from ``agent.write_progress_signal_*`` config.

        Gated by *write_capable*.
        """
        from mewbo_core.config import get_config_value

        return cls(
            write_capable=write_capable,
            threshold=int(
                get_config_value(
                    "agent",
                    "write_progress_signal_step_threshold",
                    default=DEFAULT_WRITE_PROGRESS_THRESHOLD,
                )
            ),
            event_interval=int(
                get_config_value(
                    "agent",
                    "write_progress_signal_event_interval",
                    default=DEFAULT_WRITE_PROGRESS_EVENT_INTERVAL,
                )
            ),
            max_events=int(
                get_config_value(
                    "agent",
                    "write_progress_signal_max_events",
                    default=DEFAULT_WRITE_PROGRESS_MAX_EVENTS,
                )
            ),
            reminder_enabled=bool(
                get_config_value(
                    "agent", "write_progress_signal_reminder_enabled", default=False
                )
            ),
        )

    def observe(self, results: Sequence[Any], specs_map: dict[str, ToolSpec]) -> None:
        """Record one step's progress verdict from its executed tool results.

        A step is progress iff any executed tool's spec resolves to the
        ``"write"`` capability tier — that resets the non-progress streak to
        zero. Everything else (a declared read/execute/search tier, AND a
        tool_id absent from ``specs_map`` entirely — session tools,
        spawn_agent, activate_skill and tool_search are never registry specs)
        falls through to the same non-progress branch: a tool this signal
        can't look up can't be PROVEN a write, so it is never credited as one.
        """
        is_write = any(
            (spec := specs_map.get(getattr(r, "tool_id", ""))) is not None
            and spec.capability_tier() == "write"
            for r in results
        )
        if is_write:
            self._nonprogress = 0
        else:
            self._nonprogress += 1

    def threshold_crossed(self) -> bool:
        """True (and consumes one event slot) exactly when a fresh event is due.

        Only for a write-capable agent with detection enabled and events
        remaining; fires once the non-progress streak crosses ``threshold``
        and again every ``event_interval`` steps after, so telemetry repeats
        periodically instead of exactly once.
        """
        if not self.write_capable or self.threshold <= 0 or self._fires >= self.max_events:
            return False
        if self._nonprogress < self.threshold:
            return False
        if (self._nonprogress - self.threshold) % self.event_interval != 0:
            return False
        self._fires += 1
        return True

    def exhausted(self) -> bool:
        """True once every allotted event has been spent — go quiet from here."""
        return self._fires >= self.max_events


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
