"""Contract tests for the ``model_control`` self-steering SessionTool.

Drives the tool from the caller's seam (``handle`` with an ``ActionStep``),
stubbing only the loop-side collaborators with plain callables and injecting a
real ``RetryStrategy`` so the switch budget / circuit-breaker guardrails run
against the SAME objects the automatic fallback ladder uses (never a parallel
counter). Every refusal test doubles as a mutation check: delete the guardrail
and the switch would be sanctioned (delegate called, ``llm_fallback`` emitted)
instead of returning its error code, so the assertion flips.
"""

from __future__ import annotations

import ast
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from mewbo_core.agent_context import AgentContext
from mewbo_core.classes import ActionStep
from mewbo_core.hypervisor import AgentHypervisor
from mewbo_core.llm_resilience import CircuitBreaker, RetryBudget, RetryStrategy
from mewbo_core.model_control import ModelControlArgs, ModelControlTool
from mewbo_core.tool_use_loop import ToolUseLoop

# Sibling helpers (tests/ is on sys.path under pytest).
from test_tool_use_loop import (
    _allow_all_policy,
    _make_context,
    _make_hook_manager,
    _make_registry,
    _make_spec,
    _text_response,
    _tool_call_response,
)

LADDER = ["primary", "mid", "rescue"]


def _strategy(*, budget: RetryBudget | None = None, breaker: CircuitBreaker | None = None):
    return RetryStrategy(
        budget=budget or RetryBudget(),
        breaker=breaker or CircuitBreaker(),
    )


def _tool(
    *,
    active: str = "primary",
    ladder: list[str] | None = None,
    strategy: RetryStrategy | None = None,
    dangling: bool = False,
    switched: list[str] | None = None,
    events: list | None = None,
    max_switches: int = 3,
    allow_upgrade: bool = False,
    step: int = 0,
) -> ModelControlTool:
    strategy = strategy if strategy is not None else _strategy()
    return ModelControlTool(
        session_id="s1",
        agent_id="agent-1",
        depth=0,
        get_active_model=lambda: active,
        get_ladder=lambda: list(ladder if ladder is not None else LADDER),
        get_strategy=lambda: strategy,
        has_unanswered_tool_use=lambda: dangling,
        apply_switch=(switched if switched is not None else []).append,
        event_logger=(events.append if events is not None else None),
        get_step=lambda: step,
        max_switches=max_switches,
        allow_upgrade=allow_upgrade,
    )


def _step(tool_input) -> ActionStep:
    return ActionStep(tool_id="model_control", operation="execute", tool_input=tool_input)


def _run(tool: ModelControlTool, tool_input):
    return asyncio.run(tool.handle(_step(tool_input)))


def _payload(speaker) -> dict:
    return ast.literal_eval(speaker.content)


def _is_error(speaker) -> bool:
    return isinstance(_payload(speaker), dict) and "error" in _payload(speaker)


def _code(speaker) -> str:
    return _payload(speaker)["error"]["code"]


def _fallbacks(events: list) -> list:
    return [e for e in events if e.get("type") == "llm_fallback"]


# ---------------------------------------------------------------------------
# Protocol contract
# ---------------------------------------------------------------------------


def test_tool_id_and_modes():
    assert ModelControlTool.tool_id == "model_control"
    assert ModelControlTool.modes == frozenset({"act"})


def test_never_terminates():
    tool = _tool()
    assert tool.should_terminate_run() is False
    _run(tool, {"operation": "status"})
    assert tool.should_terminate_run() is False


def test_terminal_reason_default():
    assert _tool().terminal_reason() == "awaiting_approval"


# ---------------------------------------------------------------------------
# Args validation (at definition)
# ---------------------------------------------------------------------------


def test_switch_requires_target():
    result = _run(_tool(), {"operation": "switch"})
    assert _code(result) == "validation"


def test_unknown_operation_rejected_by_schema():
    assert _code(_run(_tool(), {"operation": "delete"})) == "validation"


def test_string_tool_input_rejected():
    tool = _tool()
    result = asyncio.run(
        tool.handle(ActionStep(tool_id="model_control", operation="execute", tool_input="x"))
    )
    assert _code(result) == "validation"


def test_extra_field_forbidden():
    assert _code(_run(_tool(), {"operation": "status", "bogus": 1})) == "validation"


# ---------------------------------------------------------------------------
# Guardrail 1 — allowlist
# ---------------------------------------------------------------------------


def test_switch_off_ladder_refused():
    switched: list[str] = []
    events: list = []
    result = _run(
        _tool(switched=switched, events=events),
        {"operation": "switch", "target": "gpt-ghost"},
    )
    assert _code(result) == "off_ladder"
    assert switched == []  # never delegated
    assert _fallbacks(events) == []  # never emitted


def test_switch_same_model_refused():
    result = _run(_tool(active="primary"), {"operation": "switch", "target": "primary"})
    assert _code(result) == "no_op"


# ---------------------------------------------------------------------------
# Guardrail 2 — one-way ratchet
# ---------------------------------------------------------------------------


def test_upward_switch_denied_without_allow_upgrade():
    # active is the BOTTOM rung; a move to the primary is upward.
    switched: list[str] = []
    result = _run(
        _tool(active="rescue", switched=switched, allow_upgrade=False),
        {"operation": "switch", "target": "primary"},
    )
    assert _code(result) == "upgrade_denied"
    assert switched == []


def test_upward_switch_allowed_with_allow_upgrade():
    switched: list[str] = []
    events: list = []
    result = _run(
        _tool(active="rescue", switched=switched, events=events, allow_upgrade=True),
        {"operation": "switch", "target": "primary"},
    )
    assert not _is_error(result)
    assert switched == ["primary"]
    assert _fallbacks(events)[0]["payload"]["to_model"] == "primary"


def test_downward_switch_is_the_default_direction():
    switched: list[str] = []
    result = _run(
        _tool(active="primary", switched=switched, allow_upgrade=False),
        {"operation": "switch", "target": "rescue"},
    )
    assert not _is_error(result)
    assert switched == ["rescue"]


# ---------------------------------------------------------------------------
# Guardrail 4 — cooldown (reuses the CircuitBreaker)
# ---------------------------------------------------------------------------


def test_switch_to_cooling_target_refused():
    breaker = CircuitBreaker(threshold=1, cooldown=30.0)
    breaker.record_failure("mid")  # one failure trips the threshold-1 breaker
    assert breaker.is_open("mid")
    result = _run(
        _tool(strategy=_strategy(breaker=breaker)),
        {"operation": "switch", "target": "mid"},
    )
    assert _code(result) == "cooling"


# ---------------------------------------------------------------------------
# Guardrail 5 — hard lock (tool-loop continuity beats any cost rule)
# ---------------------------------------------------------------------------


def test_switch_with_unanswered_tool_use_refused():
    switched: list[str] = []
    result = _run(
        _tool(dangling=True, switched=switched),
        {"operation": "switch", "target": "mid"},
    )
    assert _code(result) == "tool_use_in_flight"
    assert switched == []


def test_hard_lock_outranks_cost_rules():
    # A cooling target AND a dangling tool_use: continuity must win the report.
    breaker = CircuitBreaker(threshold=1, cooldown=30.0)
    breaker.record_failure("mid")
    result = _run(
        _tool(dangling=True, strategy=_strategy(breaker=breaker)),
        {"operation": "switch", "target": "mid"},
    )
    assert _code(result) == "tool_use_in_flight"


# ---------------------------------------------------------------------------
# Guardrail 3 — switch budget (max_switches cap + shared RetryBudget storm guard)
# ---------------------------------------------------------------------------


def test_switch_over_max_switches_refused():
    result = _run(
        _tool(max_switches=0),
        {"operation": "switch", "target": "mid"},
    )
    assert _code(result) == "switch_budget"


def test_switch_refused_when_shared_budget_exhausted():
    # A tiny budget already drained below half — the SAME object automatic
    # retries drain. max_switches is high so only the budget gate can fire.
    budget = RetryBudget(capacity=2)
    budget.charge()  # 2 -> 1, can_retry() now False (needs > capacity/2)
    assert not budget.can_retry()
    result = _run(
        _tool(strategy=_strategy(budget=budget), max_switches=9),
        {"operation": "switch", "target": "mid"},
    )
    assert _code(result) == "switch_budget"


def test_max_switches_counts_down_across_calls():
    switched: list[str] = []
    tool = _tool(switched=switched, max_switches=1)
    first = _run(tool, {"operation": "switch", "target": "mid"})
    assert not _is_error(first)
    assert _payload(first)["switches_remaining"] == 0
    # The tool's active-model getter is fixed at "primary" here, so a second
    # switch to a different rung is still off the same active — but the cap is
    # already spent.
    second = _run(tool, {"operation": "switch", "target": "rescue"})
    assert _code(second) == "switch_budget"
    assert switched == ["mid"]


# ---------------------------------------------------------------------------
# Guardrail 6 — a sanctioned switch delegates AND emits llm_fallback
# ---------------------------------------------------------------------------


def test_sanctioned_switch_delegates_and_emits_and_charges():
    switched: list[str] = []
    events: list = []
    strategy = _strategy()
    tokens_before = strategy.budget.tokens
    result = _run(
        _tool(switched=switched, events=events, strategy=strategy, step=4),
        {"operation": "switch", "target": "mid", "reason": "repeated timeouts"},
    )
    payload = _payload(result)
    assert payload["operation"] == "switch"
    assert payload["from_model"] == "primary"
    assert payload["to_model"] == "mid"
    assert payload["switches_remaining"] == 2  # 3 - 1

    # Delegated the actual promote/rebind to the loop (never open-coded here).
    assert switched == ["mid"]

    # Emitted exactly one llm_fallback carrying the switch, sticky + attributed.
    fallbacks = _fallbacks(events)
    assert len(fallbacks) == 1
    fp = fallbacks[0]["payload"]
    assert (fp["from_model"], fp["to_model"]) == ("primary", "mid")
    assert fp["reason"] == "repeated timeouts"
    assert fp["sticky"] is True
    assert fp["step"] == 4
    assert fp["agent_id"] == "agent-1"

    # Charged the SHARED retry budget (reuse, not a parallel counter).
    assert strategy.budget.tokens < tokens_before


def test_switch_reason_defaults_when_omitted():
    events: list = []
    _run(
        _tool(events=events),
        {"operation": "switch", "target": "mid"},
    )
    assert _fallbacks(events)[0]["payload"]["reason"] == "self_steering"


def test_switch_survives_raising_event_logger():
    def _boom(_event):
        raise RuntimeError("sink down")

    switched: list[str] = []
    tool = ModelControlTool(
        session_id="s1",
        agent_id="a",
        depth=0,
        get_active_model=lambda: "primary",
        get_ladder=lambda: list(LADDER),
        get_strategy=lambda: _strategy(),
        has_unanswered_tool_use=lambda: False,
        apply_switch=switched.append,
        event_logger=_boom,
    )
    result = _run(tool, {"operation": "switch", "target": "mid"})
    # A failing telemetry sink must not fail the switch itself.
    assert not _is_error(result)
    assert switched == ["mid"]


# ---------------------------------------------------------------------------
# status / list — read-only, dispatch-dict driven
# ---------------------------------------------------------------------------


def test_status_reports_active_ladder_and_budget():
    breaker = CircuitBreaker(threshold=1, cooldown=30.0)
    breaker.record_failure("rescue")
    payload = _payload(
        _run(
            _tool(strategy=_strategy(breaker=breaker), max_switches=2),
            {"operation": "status"},
        )
    )
    assert payload["operation"] == "status"
    assert payload["active_model"] == "primary"
    assert payload["ladder"] == LADDER
    assert payload["max_switches"] == 2
    assert payload["switches_remaining"] == 2
    assert payload["cooling"] == ["rescue"]


def test_list_enumerates_rungs_with_direction():
    breaker = CircuitBreaker(threshold=1, cooldown=30.0)
    breaker.record_failure("rescue")
    payload = _payload(
        _run(
            _tool(active="mid", strategy=_strategy(breaker=breaker)),
            {"operation": "list"},
        )
    )
    rungs = {r["model"]: r for r in payload["rungs"]}
    assert rungs["primary"]["direction"] == "up"
    assert rungs["mid"]["direction"] == "current"
    assert rungs["rescue"]["direction"] == "down"
    assert rungs["rescue"]["cooling"] is True
    assert rungs["mid"]["cooling"] is False


def test_status_and_list_never_delegate_or_emit():
    switched: list[str] = []
    events: list = []
    tool = _tool(switched=switched, events=events)
    _run(tool, {"operation": "status"})
    _run(tool, {"operation": "list"})
    assert switched == []
    assert _fallbacks(events) == []


# ---------------------------------------------------------------------------
# Ladder hygiene
# ---------------------------------------------------------------------------


def test_ladder_deduplicated_for_allowlist_and_ratchet():
    # primary repeated in the fallback list must not double-count on the ladder.
    payload = _payload(
        _run(
            _tool(ladder=["primary", "mid", "primary", "rescue"]),
            {"operation": "status"},
        )
    )
    assert payload["ladder"] == ["primary", "mid", "rescue"]


def test_args_model_switch_target_validation_direct():
    # The per-operation requirement is validated AT DEFINITION on the model.
    import pytest
    from pydantic import ValidationError

    ModelControlArgs.model_validate({"operation": "status"})
    with pytest.raises(ValidationError):
        ModelControlArgs.model_validate({"operation": "switch"})


# ---------------------------------------------------------------------------
# Loop wiring — the tool is attached, scoped and collaborator-wired correctly
# ---------------------------------------------------------------------------


def _config_side_effect(*, self_steering: bool, max_switches: int = 2, allow_upgrade: bool = False):
    """A ``get_config_value`` stand-in: fallback knobs fixed, everything else default."""
    table = {
        ("llm", "fallback", "self_steering"): self_steering,
        ("llm", "fallback", "max_switches"): max_switches,
        ("llm", "fallback", "allow_upgrade"): allow_upgrade,
    }

    def _cfg(*keys, default=None):
        return table.get(keys, default)

    return _cfg


def _build_loop(
    *,
    self_steering: bool = True,
    fallback_models: tuple[str, ...] = ("mid", "rescue"),
    event_logger=None,
    allowed_tools=None,
    strict_tool_scope: bool = False,
) -> ToolUseLoop:
    ctx = AgentContext.root(
        model_name="primary",
        max_depth=5,
        registry=AgentHypervisor(max_concurrent=100),
        event_logger=event_logger,
        fallback_models=fallback_models,
    )
    with patch(
        "mewbo_core.tool_use_loop.get_config_value",
        side_effect=_config_side_effect(self_steering=self_steering),
    ):
        return ToolUseLoop(
            agent_context=ctx,
            tool_registry=_make_registry(_make_spec("read_file", "Read a file")),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
            session_id="s1",
            allowed_tools=allowed_tools,
            strict_tool_scope=strict_tool_scope,
        )


def _tool_of(loop: ToolUseLoop) -> ModelControlTool | None:
    return next((t for t in loop._session_tools if t.tool_id == "model_control"), None)


class TestLoopWiring:
    def test_attached_when_self_steering_on(self):
        assert _tool_of(_build_loop(self_steering=True)) is not None

    def test_absent_when_self_steering_off(self):
        assert _tool_of(_build_loop(self_steering=False)) is None

    def test_attached_under_strict_scope_without_being_named(self):
        # Child-scope case (a pinned model the key rejects): the tool is
        # resilience infrastructure, NOT ceiling-checked, so a strict AgentDef
        # that never listed it still binds it.
        loop = _build_loop(strict_tool_scope=True, allowed_tools=["read_file"])
        assert _tool_of(loop) is not None

    def test_wired_tool_reads_live_active_model_and_ladder(self):
        loop = _build_loop(fallback_models=("mid", "rescue"))
        payload = _payload(_run(_tool_of(loop), {"operation": "status"}))
        assert payload["active_model"] == "primary"
        assert payload["ladder"] == ["primary", "mid", "rescue"]

    def test_request_model_switch_records_deferred_target(self):
        loop = _build_loop()
        assert loop._requested_switch is None
        loop._request_model_switch("mid")
        assert loop._requested_switch == "mid"

    def test_dangling_detects_stranded_prior_tool_use(self):
        loop = _build_loop()
        loop._live_messages = [
            SystemMessage(content="sys"),
            AIMessage(content="", tool_calls=[{"name": "read_file", "args": {}, "id": "a"}]),
            # 'a' never answered; a new batch 'b' is the in-flight one, answered.
            AIMessage(content="", tool_calls=[{"name": "model_control", "args": {}, "id": "b"}]),
            ToolMessage(content="ok", tool_call_id="b"),
        ]
        assert loop._has_dangling_tool_use() is True

    def test_dangling_false_for_in_flight_batch_only(self):
        loop = _build_loop()
        loop._live_messages = [
            SystemMessage(content="sys"),
            HumanMessage(content="hi"),
            # Only the current (last) batch is unanswered — that is normal.
            AIMessage(content="", tool_calls=[{"name": "model_control", "args": {}, "id": "b"}]),
        ]
        assert loop._has_dangling_tool_use() is False

    def test_dangling_false_when_all_prior_paired(self):
        loop = _build_loop()
        loop._live_messages = [
            SystemMessage(content="sys"),
            AIMessage(content="", tool_calls=[{"name": "read_file", "args": {}, "id": "a"}]),
            ToolMessage(content="ok", tool_call_id="a"),
            AIMessage(content="", tool_calls=[{"name": "model_control", "args": {}, "id": "b"}]),
        ]
        assert loop._has_dangling_tool_use() is False


class TestEndToEndSwitch:
    """A model that calls ``model_control(switch)`` heals its own routing.

    Drives a full two-turn run through the real loop: turn 1 the model requests
    the switch, turn 2 the deferred switch is applied at the boundary via
    ``_apply_model_escalation`` (active model promoted, prompt re-rendered) and
    the model completes. Proves the whole seam end to end, not just the tool.
    """

    def test_switch_promotes_active_model_and_emits_fallback(self):
        events: list = []
        spec = _make_spec("read_file", "Read a file")
        fake = MagicMock()
        fake.ainvoke = AsyncMock(
            side_effect=[
                _tool_call_response(
                    "model_control", {"operation": "switch", "target": "mid"}, "call_1"
                ),
                _text_response("Healed onto mid; the answer is 42."),
            ]
        )
        bound = MagicMock()
        bound.ainvoke = fake.ainvoke

        ctx = AgentContext.root(
            model_name="primary",
            max_depth=5,
            registry=AgentHypervisor(max_concurrent=100),
            event_logger=events.append,
            fallback_models=("mid", "rescue"),
        )
        with (
            patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build,
            patch(
                "mewbo_core.tool_use_loop.get_config_value",
                side_effect=_config_side_effect(self_steering=True),
            ),
        ):
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound
            loop = ToolUseLoop(
                agent_context=ctx,
                tool_registry=_make_registry(spec),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
                session_id="s1",
            )
            _tq, state = asyncio.run(
                loop.run("do work", tool_specs=[spec], context=_make_context())
            )

        assert state.done is True
        # The deferred switch was applied at the turn boundary.
        assert loop._active_model == "mid"
        # And the deliberate switch surfaced as an llm_fallback (usage attribution).
        fallbacks = _fallbacks(events)
        assert any(f["payload"]["to_model"] == "mid" for f in fallbacks)
        assert fake.ainvoke.call_count == 2
