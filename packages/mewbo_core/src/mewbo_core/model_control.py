#!/usr/bin/env python3
"""``model_control`` SessionTool — the agent's own model-routing control.

Lets the LLM shift its OWN model routing at runtime: read where it stands on
the declared fallback ladder (``status``/``list``) and switch to another rung
(``switch``). The automatic fallback ladder in :mod:`mewbo_core.llm_resilience`
heals a model that *errors*; this tool lets a run that is merely degrading —
slow, thrashing, or pinned by its AgentDef to a model the API key rejects —
deliberately move itself before it dies.

Follows the ``update_todos.py`` / ``schedule_trigger`` shape: a class
satisfying the :class:`~mewbo_core.session_tools.SessionTool` Protocol,
terminal-free (:meth:`should_terminate_run` always ``False`` — switching is a
normal step, not a plan-mode exit), args validated at definition by a Pydantic
model, schema generated via :func:`pydantic_to_openai_tool`, structured results
returned as ``str(dict)`` (never ``json.dumps`` — the loop parses tool results
with ``ast.literal_eval``).

Because letting a model pick its own model is a footgun, the switch is fenced
by six guardrails, none optional — they are what make the tool defensible when
an operator turns it on:

1. **Allowlist** — the target must be a rung of the operator-declared ladder
   (``[primary, *fallback_models]``), never a free-text id. Under the curated
   foundry, the allowlist IS the capability guarantee.
2. **One-way ratchet** — downward along the declared ladder only; an upward
   move (back toward the primary) needs ``allow_upgrade``.
3. **Switch budget** — every deliberate switch draws on the SAME
   :class:`~mewbo_core.llm_resilience.RetryBudget` the automatic retries use
   (no parallel storm counter), and is additionally capped at ``max_switches``.
4. **Cooldown** — a target whose :class:`~mewbo_core.llm_resilience.CircuitBreaker`
   is open is refused.
5. **Hard lock** — never switch while a PRIOR turn's ``tool_use`` is still
   unanswered; tool-loop continuity outranks any cost rule. The loop applies
   the switch at the next turn boundary (transcript tail untouched), exactly
   like a sticky fallback.
6. **Emit ``llm_fallback``** — every deliberate switch emits it, because
   ``token_budget`` derives ``models_used`` from that event; a switch that did
   not emit would vanish from usage attribution.

Collaborators (the live model/ladder/strategy handles, the event sink, the
switch delegate) arrive by DI as fields so the tool is drivable in a test with
plain callables and never reaches for a global.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from mewbo_core.session_tools import DEFAULT_SESSION_TOOL_MODES

if TYPE_CHECKING:
    from collections.abc import Callable

    from mewbo_core.classes import ActionStep
    from mewbo_core.llm_resilience import RetryStrategy
    from mewbo_core.session_tools import EventLogger
    from mewbo_core.types import Event, LlmFallbackPayload

logging = get_logger(name="core.model_control")


# ------------------------------------------------------------------
# Args schema (Pydantic model ⇒ OpenAI function schema via pydantic_to_openai_tool)
# ------------------------------------------------------------------


class ModelControlArgs(BaseModel):
    """Inspect or shift THIS run's model routing along the declared ladder.

    `operation="status"` reports the model you are running now, the declared
    fallback ladder, how many deliberate switches remain, and which rungs are
    cooling down. `operation="list"` shows each rung with its direction
    relative to you (current / down / up) and whether it is available.
    `operation="switch"` moves you to `target` — which MUST be one of the
    declared rungs. Switching is a ONE-WAY ratchet DOWN the ladder (toward the
    cheaper/steadier rescue models) unless the deployment allows upgrades; a
    switch takes effect on your NEXT turn and pins the chosen model for the
    rest of the run. Use it when the current model is failing you (repeated
    timeouts, refusals it should not be making, or a model your key cannot
    reach), not to shop for style.
    """

    model_config = ConfigDict(extra="forbid")

    operation: Literal["switch", "status", "list"] = Field(
        description=(
            "`status` reports the active model + ladder; `list` enumerates the "
            "rungs; `switch` moves to `target`."
        )
    )
    target: str | None = Field(
        default=None,
        description=(
            "`switch` only: the model id to switch to. Must be one of the "
            "declared ladder rungs (see `list`)."
        ),
    )
    reason: str | None = Field(
        default=None,
        description=(
            "`switch` only: a short factual reason for the switch (recorded on "
            "the fallback event), e.g. `repeated timeouts on the primary`."
        ),
    )

    @model_validator(mode="after")
    def _check_operation_fields(self) -> ModelControlArgs:
        """Require the fields each operation needs (fail closed at the boundary)."""
        if self.operation == "switch" and not (self.target and self.target.strip()):
            raise ValueError("operation=switch requires a non-empty `target`")
        return self


# ------------------------------------------------------------------
# The SessionTool
# ------------------------------------------------------------------


class ModelControlTool:
    """Handles ``model_control`` calls — switch/status/list this run's routing."""

    tool_id: str = "model_control"
    schema: dict[str, object] = pydantic_to_openai_tool(
        ModelControlArgs, name="model_control"
    )
    modes: frozenset[str] = DEFAULT_SESSION_TOOL_MODES

    def __init__(
        self,
        *,
        session_id: str,
        agent_id: str,
        depth: int,
        get_active_model: Callable[[], str],
        get_ladder: Callable[[], list[str]],
        get_strategy: Callable[[], RetryStrategy | None],
        has_unanswered_tool_use: Callable[[], bool],
        apply_switch: Callable[[str], None],
        event_logger: EventLogger | None = None,
        get_step: Callable[[], int] | None = None,
        max_switches: int = 2,
        allow_upgrade: bool = False,
    ) -> None:
        """Bind the live routing handles + guardrail knobs.

        Args:
            session_id: Owning session id (parity with other session tools).
            agent_id: Stamped on the emitted ``llm_fallback`` event.
            depth: This agent's hypervisor depth (event attribution).
            get_active_model: Reads the model currently active for the run —
                the configured primary, or a rung a prior switch pinned.
            get_ladder: Reads the operator-declared ladder
                (``[primary, *fallback_models]``); the allowlist AND the ratchet
                axis. Read lazily because the run may have escalated.
            get_strategy: Reads the live per-run
                :class:`~mewbo_core.llm_resilience.RetryStrategy` so guardrails 3
                and 4 REUSE its ``RetryBudget`` / ``CircuitBreaker`` rather than
                re-implement them. ``None`` before the run's loop starts.
            has_unanswered_tool_use: Guardrail 5 detector — ``True`` when a prior
                turn's ``tool_use`` is still unanswered (a switch would strand it).
            apply_switch: Delegates the actual switch to the loop, which applies
                it at the next turn boundary via ``_apply_model_escalation``
                (promote active model / re-render prompt / re-derive edit tool /
                rebind). This tool never open-codes any of those.
            event_logger: Sink for the ``llm_fallback`` event (guardrail 6).
            get_step: Reads the current turn index for event attribution.
                Defaults to ``0`` when a caller has no step counter to offer.
            max_switches: Guardrail 3 cap on deliberate switches per run.
            allow_upgrade: Guardrail 2 — permit moves UP the ladder.
        """
        self._session_id = session_id
        self._agent_id = agent_id
        self._depth = depth
        self._get_active_model = get_active_model
        self._get_ladder = get_ladder
        self._get_strategy = get_strategy
        self._has_unanswered_tool_use = has_unanswered_tool_use
        self._apply_switch = apply_switch
        self._event_logger = event_logger
        self._get_step = get_step or (lambda: 0)
        self._max_switches = max_switches
        self._allow_upgrade = allow_upgrade
        # Deliberate switches performed this run. This is the ``max_switches``
        # product cap, NOT a re-implementation of the retry budget's storm guard
        # (guardrail 3 reuses that) — the two bound different things.
        self._switches_made = 0

    def should_terminate_run(self) -> bool:
        """Never terminates — inspecting/switching routing is a normal step."""
        return False

    def terminal_reason(self) -> str:
        """Unused (never terminates); default parity with the Protocol."""
        return "awaiting_approval"

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Dispatch switch/status/list off ``tool_input.operation``.

        Dispatch is a dict keyed by the ``operation`` literal, never an
        ``if op ==`` chain — each operation owns its own method.
        """
        try:
            args = ModelControlArgs.model_validate(action_step.tool_input or {})
        except ValidationError as exc:
            return self._err_result("validation", str(exc))
        dispatch = {"switch": self._switch, "status": self._status, "list": self._list}
        return dispatch[args.operation](args)

    # -- operations ---------------------------------------------------------

    def _switch(self, args: ModelControlArgs) -> MockSpeaker:
        """Validate against all six guardrails, then delegate the switch.

        The checks run in a fixed order so a refusal names the FIRST fence hit:
        target validity (allowlist, same-model, ratchet) before the
        continuity lock, and the continuity lock before any cost rule (budget /
        cooldown) — continuity outranks cost by design.
        """
        target = (args.target or "").strip()
        ladder = self._dedup(self._get_ladder())
        active = self._get_active_model()

        # (1) Allowlist — a free-text id is never a valid destination.
        if target not in ladder:
            return self._err_result(
                "off_ladder",
                f"{target!r} is not a declared model; choose one of {ladder}",
            )
        if target == active:
            return self._err_result("no_op", f"{target!r} is already the active model")

        # (2) One-way ratchet — an upward move needs the explicit opt-in.
        if self._is_upgrade(ladder, active, target) and not self._allow_upgrade:
            return self._err_result(
                "upgrade_denied",
                f"{target!r} is above the active model on the ladder; upgrades are disabled",
            )

        # (5) Hard lock — a prior unanswered tool_use outranks any cost rule.
        if self._has_unanswered_tool_use():
            return self._err_result(
                "tool_use_in_flight",
                "cannot switch while a prior tool call is still unanswered",
            )

        strategy = self._get_strategy()

        # (4) Cooldown — refuse a target whose breaker is open.
        if strategy is not None and strategy.breaker.is_open(target):
            return self._err_result(
                "cooling", f"{target!r} is cooling down after repeated failures"
            )

        # (3) Switch budget — the deliberate-switch cap AND the shared storm
        # guard (the retry budget), never a parallel counter for the latter.
        if self._switches_made >= self._max_switches:
            return self._err_result(
                "switch_budget",
                f"deliberate switch limit reached ({self._max_switches})",
            )
        if strategy is not None and not strategy.budget.can_retry():
            return self._err_result(
                "switch_budget", "the shared retry budget is exhausted"
            )

        # Sanctioned. Charge the shared budget, count the switch, emit the
        # fallback event (6), then delegate the actual promote/rebind.
        if strategy is not None:
            strategy.budget.charge()
        self._switches_made += 1
        self._emit_fallback(from_model=active, to_model=target, reason=args.reason)
        self._apply_switch(target)
        return self._ok_result(
            {
                "operation": "switch",
                "from_model": active,
                "to_model": target,
                "switches_remaining": max(0, self._max_switches - self._switches_made),
                "note": "takes effect on your next turn",
            }
        )

    def _status(self, args: ModelControlArgs) -> MockSpeaker:
        """Report the active model, ladder, remaining budget and cooling rungs."""
        del args
        ladder = self._dedup(self._get_ladder())
        strategy = self._get_strategy()
        cooling = [m for m in ladder if strategy is not None and strategy.breaker.is_open(m)]
        return self._ok_result(
            {
                "operation": "status",
                "active_model": self._get_active_model(),
                "ladder": ladder,
                "switches_made": self._switches_made,
                "max_switches": self._max_switches,
                "switches_remaining": max(0, self._max_switches - self._switches_made),
                "allow_upgrade": self._allow_upgrade,
                "cooling": cooling,
            }
        )

    def _list(self, args: ModelControlArgs) -> MockSpeaker:
        """Enumerate the ladder rungs with direction + availability per rung."""
        del args
        ladder = self._dedup(self._get_ladder())
        active = self._get_active_model()
        strategy = self._get_strategy()
        rungs: list[dict[str, object]] = []
        for pos, model in enumerate(ladder):
            if model == active:
                direction = "current"
            elif self._is_upgrade(ladder, active, model):
                direction = "up"
            else:
                direction = "down"
            rungs.append(
                {
                    "model": model,
                    "position": pos,
                    "direction": direction,
                    "cooling": strategy is not None and strategy.breaker.is_open(model),
                }
            )
        return self._ok_result({"operation": "list", "count": len(rungs), "rungs": rungs})

    # -- guardrail helpers --------------------------------------------------

    @staticmethod
    def _dedup(models: list[str]) -> list[str]:
        """Order-preserving de-duplication of the ladder (primary may repeat)."""
        seen: set[str] = set()
        out: list[str] = []
        for m in models:
            if m and m not in seen:
                seen.add(m)
                out.append(m)
        return out

    @staticmethod
    def _is_upgrade(ladder: list[str], active: str, target: str) -> bool:
        """True when *target* sits ABOVE *active* on the declared ladder.

        "Above" = a lower index (nearer the primary). An *active* model absent
        from the ladder is treated as the top rung, so every in-ladder move
        then reads as downward — the recovery-friendly reading, never a
        surprise upgrade lock.
        """
        try:
            active_idx = ladder.index(active)
        except ValueError:
            active_idx = 0
        return ladder.index(target) < active_idx

    # -- events -------------------------------------------------------------

    def _emit_fallback(
        self, *, from_model: str, to_model: str, reason: str | None
    ) -> None:
        """Emit the ``llm_fallback`` event a deliberate switch owes (guardrail 6)."""
        if self._event_logger is None:
            return
        payload: LlmFallbackPayload = {
            "agent_id": self._agent_id,
            "depth": self._depth,
            "step": self._get_step(),
            "from_model": from_model,
            "to_model": to_model,
            "reason": (reason.strip() if reason and reason.strip() else "self_steering"),
            # No provider error drove this — the switch is deliberate.
            "previous_error_type": "None",
            "sticky": True,
        }
        event: Event = {"type": "llm_fallback", "payload": payload}
        try:
            self._event_logger(event)
        except Exception as exc:  # noqa: BLE001 — a telemetry event must never break a run
            logging.warning("Failed to emit llm_fallback event: {}", exc)

    # -- structured-result envelope helpers (mirror of the wiki/scg shape) --

    @staticmethod
    def _err_result(code: str, message: str) -> MockSpeaker:
        """Structured-error envelope: ``str({"error": {"code", "message"}})``.

        The envelope SHAPE is the contract (parsed structurally by
        ``tool_use_loop._session_tool_error_envelope`` into a FAILED step),
        not this helper.
        """
        return MockSpeaker(content=str({"error": {"code": code, "message": message}}))

    @staticmethod
    def _ok_result(payload: dict[str, object]) -> MockSpeaker:
        """Successful structured payload as ``str(dict)`` (never ``json.dumps``)."""
        return MockSpeaker(content=str(payload))


__all__ = ["ModelControlArgs", "ModelControlTool"]
