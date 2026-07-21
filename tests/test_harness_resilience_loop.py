#!/usr/bin/env python3
"""Contract tests for the tool-use loop's harness-resilience seams.

Each test drives a real seam from its caller's side and stubs only I/O (the
chat model, a session tool). Clocks arrive as arguments rather than being
patched, so nothing here sleeps or depends on wall time.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from mewbo_core.agent_context import AgentContext
from mewbo_core.common import MockSpeaker
from mewbo_core.hooks import HookManager
from mewbo_core.hypervisor import AgentHandle, AgentHypervisor
from mewbo_core.llm_resilience import DoomLoopGuard, LlmResilienceExhausted, PollClassRule
from mewbo_core.permissions import PermissionDecision, PermissionPolicy
from mewbo_core.session_tools import DEFAULT_SESSION_TOOL_MAX_RESULT_CHARS
from mewbo_core.tool_registry import ToolRegistry, ToolSpec
from mewbo_core.tool_use_loop import ToolUseLoop, _SessionToolError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec(tool_id: str = "test_tool", **kwargs) -> ToolSpec:
    return ToolSpec(
        tool_id=tool_id,
        name=tool_id,
        description=f"{tool_id} for tests",
        factory=lambda: MagicMock(),
        metadata={
            "schema": {
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
            }
        },
        **kwargs,
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


def _ctx(*, model_name: str = "primary-model", event_logger=None) -> AgentContext:
    return AgentContext.root(
        model_name=model_name,
        max_depth=5,
        registry=AgentHypervisor(max_concurrent=100),
        event_logger=event_logger,
    )


def _tool_call(tool_id: str, args: dict, call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": tool_id, "args": args, "id": call_id}])


class _FakeSessionTool:
    """Minimal standalone ``SessionTool`` implementer.

    Standalone on purpose: ``SessionTool`` is a structural Protocol whose
    defaults an implementer does NOT inherit, which is exactly the shape every
    plugin tool has and the shape the loop's ``getattr`` reads must survive.
    """

    modes = frozenset({"act"})

    def __init__(self, tool_id: str, results: list[str], *, max_result_chars=None):
        self.tool_id = tool_id
        self.schema = {
            "name": tool_id,
            "description": "fake session tool",
            "parameters": {"type": "object", "properties": {}},
        }
        self._results = list(results)
        self.calls = 0
        if max_result_chars is not None:
            self.max_result_chars = max_result_chars

    async def handle(self, action_step):
        self.calls += 1
        payload = self._results[min(self.calls - 1, len(self._results) - 1)]
        return MockSpeaker(content=payload)

    def should_terminate_run(self) -> bool:
        return False


def _run_loop(loop: ToolUseLoop, responses: list[AIMessage], specs: list[ToolSpec]):
    """Drive ``loop.run`` against a scripted model, returning (tq, state, seen)."""
    seen: list[list] = []

    async def _ainvoke(messages, config=None, **kwargs):
        seen.append(list(messages))
        return responses[min(len(seen) - 1, len(responses) - 1)]

    bound = MagicMock()
    bound.ainvoke = AsyncMock(side_effect=_ainvoke)
    # ``astream`` must be absent-shaped so ``_acall_model`` falls through to the
    # buffered path; a MagicMock attribute would be iterated as a coroutine.
    del bound.astream

    with patch("mewbo_core.tool_use_loop.build_chat_model") as build:
        build.return_value = MagicMock()
        build.return_value.bind_tools.return_value = bound
        tq, state = asyncio.run(loop.run("do the task", tool_specs=specs))
    return tq, state, seen


def _tool_messages(messages: list) -> list[str]:
    return [m.content for m in messages if isinstance(m, ToolMessage)]


# ---------------------------------------------------------------------------
# The frozen-context landmines
# ---------------------------------------------------------------------------


class TestActiveModelIsAuthoritative:
    """After a sticky escalation the loop must stop reading the frozen model."""

    def _loop(self, **kwargs) -> ToolUseLoop:
        with patch("mewbo_core.tool_use_loop.build_chat_model") as build:
            build.return_value = MagicMock()
            build.return_value.bind_tools.return_value = MagicMock()
            return ToolUseLoop(
                agent_context=_ctx(),
                tool_registry=_registry(_spec()),
                permission_policy=_policy(),
                hook_manager=_hooks(),
                **kwargs,
            )

    def test_fallback_chain_head_is_the_active_model(self):
        # The bug: the chain was rebuilt from the FROZEN configured model every
        # turn, re-offering a model that had already proven dead. Escalation
        # only survived because the strategy separately reordered on its pin.
        loop = self._loop()
        loop._active_model = "rescue-model"
        captured: dict = {}

        class _Strategy:
            async def run(self, *, models, **kwargs):
                captured["models"] = list(models)
                return AIMessage(content="ok"), "rescue-model"

        asyncio.run(
            loop._invoke_with_resilience(
                primary_model=MagicMock(),
                messages=[SystemMessage(content="sys")],
                tool_schemas=[],
                turns=0,
                invoke_config=None,
                strategy=_Strategy(),
            )
        )
        assert captured["models"][0] == "rescue-model"
        assert "primary-model" not in captured["models"]

    def test_unescalated_chain_is_unchanged(self):
        loop = self._loop()
        captured: dict = {}

        class _Strategy:
            async def run(self, *, models, **kwargs):
                captured["models"] = list(models)
                return AIMessage(content="ok"), "primary-model"

        asyncio.run(
            loop._invoke_with_resilience(
                primary_model=MagicMock(),
                messages=[SystemMessage(content="sys")],
                tool_schemas=[],
                turns=0,
                invoke_config=None,
                strategy=_Strategy(),
            )
        )
        assert captured["models"] == ["primary-model"]

    def test_compaction_window_sized_on_the_active_model(self):
        # Escalating DOWN to a smaller window while sizing against the original
        # keeps the threshold above the real ceiling, so auto-compaction never
        # fires and the run dies on a context-window error instead.
        loop = self._loop()
        loop._active_model = "small-window-model"
        loop._last_input_tokens = 1
        asked: list[str] = []

        def _max_input(model: str) -> int:
            asked.append(model)
            return 100_000

        with patch("mewbo_core.token_budget.get_model_max_input_tokens", _max_input):
            loop._should_compact_messages([])
        assert asked == ["small-window-model"]

    def test_escalation_reseats_the_spawn_seam_on_the_healed_model(self):
        # A parent that heals itself must not fan children onto the model it
        # just escaped: the spawn seam resolves an un-overridden child model
        # from its captured (frozen) context.
        loop = self._loop()
        assert loop._spawn_agent_tool is not None
        assert loop._spawn_agent_tool._agent_context.model_name == "primary-model"
        loop._tool_specs_full = []
        loop._tool_search_enabled = False
        loop._deferred_ids = set()
        loop._last_active_ids = set()

        with patch("mewbo_core.tool_use_loop.build_chat_model") as build:
            build.return_value = MagicMock()
            build.return_value.bind_tools.return_value = MagicMock()
            loop._apply_model_escalation(
                "rescue-model",
                [SystemMessage(content="sys")],
                context=None,
                plan=None,
                agent_tree="",
                tool_schemas=[],
                model=MagicMock(),
            )

        assert loop._active_model == "rescue-model"
        assert loop._spawn_agent_tool._agent_context.model_name == "rescue-model"
        # Replacement, never mutation — the original context object is frozen
        # and every other field must survive the swap intact.
        assert loop._spawn_agent_tool._agent_context.agent_id == loop._ctx.agent_id


# ---------------------------------------------------------------------------
# Tool-result sizing for the whole SessionTool class
# ---------------------------------------------------------------------------


class TestSessionToolResultSizing:
    """A session tool's result must not be clipped by registry-default accident."""

    def _manifest(self, paths: int = 1934) -> str:
        """A manifest matching a measured real repository of this scale.

        1,934 files at a mean path length of ~50 characters — the profile taken
        from an actual repository of the size this harness indexes, so the
        fixture asserts against reality rather than against a guess.
        """
        return "\n".join(
            f"packages/product_core/src/core/mod_{i:04d}/handler.py" for i in range(paths)
        )

    def _loop(self, session_tool) -> ToolUseLoop:
        with patch("mewbo_core.tool_use_loop.build_chat_model") as build:
            build.return_value = MagicMock()
            build.return_value.bind_tools.return_value = MagicMock()
            return ToolUseLoop(
                agent_context=_ctx(),
                tool_registry=_registry(_spec()),
                permission_policy=_policy(),
                hook_manager=_hooks(),
                session_id="s1",
                extra_session_tools=[session_tool],
            )

    def test_realistic_repo_manifest_reaches_the_model_intact(self):
        # The regression this closes: a ~1,900-file manifest arrived as 22
        # paths (1.14%) because a session tool has no registry spec, so the
        # 2000-char shell/MCP default applied to it by accident. The very next
        # playbook step required choosing files out of that manifest.
        manifest = self._manifest()
        assert len(manifest) > 90_000, "fixture must exercise a realistic repo size"
        assert len(manifest) < DEFAULT_SESSION_TOOL_MAX_RESULT_CHARS, (
            "the default must keep real headroom over a real repo, not merely "
            "clear it — a cap sized AT the measured artifact clips the next one"
        )
        tool = _FakeSessionTool("scan_tree", [manifest])
        loop = self._loop(tool)

        _tq, _state, seen = _run_loop(
            loop,
            [_tool_call("scan_tree", {}), AIMessage(content="done")],
            [_spec()],
        )

        delivered = _tool_messages(seen[-1])
        assert len(delivered) == 1
        assert "[truncated]" not in delivered[0]
        assert delivered[0] == manifest
        # The LAST path matters most: it is the one a 2000-char cap destroys.
        assert "mod_1933/handler.py" in delivered[0]
        assert delivered[0].count("\n") == 1933

    def test_a_tool_may_declare_a_smaller_cap(self):
        tool = _FakeSessionTool("tiny", ["x" * 5000], max_result_chars=100)
        loop = self._loop(tool)
        assert loop._result_char_cap("tiny") == 100

        _tq, _state, seen = _run_loop(
            loop, [_tool_call("tiny", {}), AIMessage(content="done")], [_spec()]
        )
        delivered = _tool_messages(seen[-1])[0]
        assert "[truncated]" in delivered
        assert len(delivered) < 200

    def test_undeclared_session_tool_gets_the_class_default(self):
        loop = self._loop(_FakeSessionTool("plain", ["ok"]))
        assert loop._result_char_cap("plain") == DEFAULT_SESSION_TOOL_MAX_RESULT_CHARS

    def test_registry_tools_keep_the_2000_default(self):
        # The default is correct for unbounded shell/MCP output; only the
        # SessionTool class was being capped by accident.
        loop = self._loop(_FakeSessionTool("plain", ["ok"]))
        assert loop._result_char_cap("test_tool") == 2000
        assert loop._result_char_cap("not_a_tool_at_all") == 2000


# ---------------------------------------------------------------------------
# E-IV — the tool-result contract
# ---------------------------------------------------------------------------


class TestToolResultTruth:
    """Every ``_safe_execute`` exit leaves a record, and it records what was read."""

    def _loop_with_events(self, events: list, session_tool=None) -> ToolUseLoop:
        with patch("mewbo_core.tool_use_loop.build_chat_model") as build:
            build.return_value = MagicMock()
            build.return_value.bind_tools.return_value = MagicMock()
            return ToolUseLoop(
                agent_context=_ctx(event_logger=events.append),
                tool_registry=_registry(_spec()),
                permission_policy=_policy(),
                hook_manager=_hooks(),
                session_id="s1",
                extra_session_tools=[session_tool] if session_tool else None,
            )

    def test_a_timed_out_tool_still_emits_a_tool_result_event(self):
        # Before this, a timeout killed ``_execute_tool_call`` mid-flight so the
        # emit at its tail never ran: the store showed 100% tool success for
        # runs with known live timeouts. The failure was absent, not merely
        # under-reported, so no amount of reading the store could find it.
        events: list = []
        loop = self._loop_with_events(events)

        async def _never_returns(*args, **kwargs):
            await asyncio.sleep(3600)

        with patch.object(loop, "_execute_tool_call", _never_returns), patch.object(
            loop, "_tool_execution_timeout", return_value=0.01
        ):
            result = asyncio.run(
                loop._safe_execute({"name": "test_tool", "args": {}, "id": "c1"}, [_spec()])
            )

        assert result.success is False
        emitted = [e for e in events if e["type"] == "tool_result"]
        assert len(emitted) == 1
        assert emitted[0]["payload"]["success"] is False
        assert "timed out" in emitted[0]["payload"]["error"]

    def test_a_crashing_tool_still_emits_a_tool_result_event(self):
        events: list = []
        loop = self._loop_with_events(events)

        async def _explodes(*args, **kwargs):
            raise RuntimeError("kaboom")

        with patch.object(loop, "_execute_tool_call", _explodes):
            result = asyncio.run(
                loop._safe_execute({"name": "test_tool", "args": {}, "id": "c1"}, [_spec()])
            )

        assert result.success is False
        emitted = [e for e in events if e["type"] == "tool_result"]
        assert len(emitted) == 1
        assert "kaboom" in emitted[0]["payload"]["error"]

    def test_the_event_records_what_the_model_actually_read(self):
        # The companion trap: the store kept the raw payload, not the truncated
        # string handed to the model, so a trace showed a full result for a call
        # the model saw a fraction of.
        events: list = []
        tool = _FakeSessionTool("chatty", ["y" * 9000], max_result_chars=500)
        loop = self._loop_with_events(events, session_tool=tool)

        _run_loop(loop, [_tool_call("chatty", {}), AIMessage(content="done")], [_spec()])

        payload = [e for e in events if e["type"] == "tool_result"][0]["payload"]
        assert payload["result_truncated"] is True
        assert len(payload["result_seen"]) < len(payload["result"])
        assert payload["result_seen"].endswith("[truncated]")
        assert len(payload["result"]) == 9000

    def test_an_untruncated_result_carries_no_redundant_copy(self):
        events: list = []
        tool = _FakeSessionTool("small", ["fits fine"])
        loop = self._loop_with_events(events, session_tool=tool)

        _run_loop(loop, [_tool_call("small", {}), AIMessage(content="done")], [_spec()])

        payload = [e for e in events if e["type"] == "tool_result"][0]["payload"]
        assert "result_seen" not in payload
        assert "result_truncated" not in payload


# ---------------------------------------------------------------------------
# Empty error strings
# ---------------------------------------------------------------------------


class TestExhaustionMessageNeverEmpty:
    def test_an_empty_error_string_substitutes_the_exception_type(self):
        # ``TimeoutError`` stringifies to "" and is the classifier's most common
        # transient verdict, so the terminal message reached the store, the
        # console and the next recovery turn with the cause erased.
        exc = LlmResilienceExhausted(["model-a"], TimeoutError(), "TimeoutError")
        assert str(exc) == "LLM call failed on all models (model-a): TimeoutError"
        assert not str(exc).endswith(": ")

    def test_a_real_message_is_prefixed_with_its_type(self):
        # Matches the span's ``<Type>: <msg>`` shape so the two durable records
        # of one failure never disagree.
        exc = LlmResilienceExhausted(["model-a"], ValueError("bad payload"), "ValueError")
        assert str(exc).endswith(": ValueError: bad payload")

    def test_a_missing_exception_reads_as_unknown(self):
        assert str(LlmResilienceExhausted([], None, "Unknown")).endswith(": Unknown")

    def test_the_rendered_cause_is_bounded(self):
        # A provider that embeds an HTML error page must not ride an unbounded
        # string into the durable payload.
        exc = LlmResilienceExhausted(["m"], ValueError("x" * 5000), "ValueError")
        assert len(LlmResilienceExhausted.describe_error(ValueError("x" * 5000))) == 500
        assert "LLM call failed on all models (m):" in str(exc)


# ---------------------------------------------------------------------------
# E-III — progress guards and the typed error envelope
# ---------------------------------------------------------------------------


class TestPollClassTools:
    """Polling a run until it settles is waiting, not a doom loop."""

    # The live shape: ONE tool id that both starts and polls a nested run,
    # told apart only by which argument is present. Its args model requires
    # exactly one of ``query`` (start) or ``run_id`` (fetch).
    _SEARCH = PollClassRule(tool_id="agentic_search", when_args=frozenset({"run_id"}))

    def test_polling_an_existing_run_never_counts_toward_a_stuck_streak(self):
        guard = DoomLoopGuard(threshold=2, poll_rules=(self._SEARCH,))
        for _ in range(4):
            guard.observe([{"name": "agentic_search", "args": {"run_id": "r1"}}])
            guard.record_result(
                [MagicMock(tool_id="agentic_search", success=True, content="processing")]
            )
        assert guard.is_stuck() is False

    def test_restarting_the_same_search_forever_still_halts(self):
        # The reason the exemption must be per-CALL and not per-tool: exempting
        # the id outright would blind the guard to a genuinely stuck agent
        # re-issuing an identical START of the same search.
        guard = DoomLoopGuard(threshold=2, poll_rules=(self._SEARCH,))
        for _ in range(4):
            guard.observe([{"name": "agentic_search", "args": {"query": "same thing"}}])
            guard.record_result(
                [MagicMock(tool_id="agentic_search", success=True, content="started")]
            )
        assert guard.is_stuck() is True

    def test_an_empty_poll_argument_is_not_a_poll(self):
        # The tool schema treats ``run_id=None`` as absent, so the guard must.
        assert self._SEARCH.matches("agentic_search", {"run_id": None}) is False
        assert self._SEARCH.matches("agentic_search", {"run_id": ""}) is False
        assert self._SEARCH.matches("agentic_search", {"run_id": "r1"}) is True
        assert self._SEARCH.matches("something_else", {"run_id": "r1"}) is False

    def test_an_unconditional_rule_matches_every_call(self):
        rule = PollClassRule(tool_id="check_agents")
        assert rule.matches("check_agents", {"wait": True}) is True
        assert rule.matches("check_agents", None) is True

    def test_an_undeclared_tool_still_halts_on_genuine_no_progress(self):
        guard = DoomLoopGuard(threshold=2, poll_rules=(self._SEARCH,))
        for _ in range(4):
            guard.observe([{"name": "spin", "args": {"x": 1}}])
            guard.record_result([MagicMock(tool_id="spin", success=True, content="same")])
        assert guard.is_stuck() is True

    def test_the_builtin_seed_still_applies_by_default(self):
        guard = DoomLoopGuard(threshold=2)
        guard.observe([{"name": "check_agents", "args": {"wait": True}}])
        assert guard.signature([{"name": "check_agents", "args": {}}]) == ""

    def test_input_and_result_signatures_stay_aligned_in_a_mixed_batch(self):
        # A result carries no arguments, so the poll ids observed on the INPUT
        # side are what the result side must drop — otherwise an
        # argument-sensitive exemption removes a call but keeps its result.
        guard = DoomLoopGuard(threshold=2, poll_rules=(self._SEARCH,))
        batch = [
            {"name": "agentic_search", "args": {"run_id": "r1"}},
            {"name": "write_notes", "args": {"text": "hi"}},
        ]
        guard.observe(batch)
        assert guard._last_poll_ids == frozenset({"agentic_search"})
        sig = guard.result_signature(
            [
                MagicMock(tool_id="agentic_search", success=True, content="processing"),
                MagicMock(tool_id="write_notes", success=True, content="ok"),
            ],
            guard._last_poll_ids,
        )
        assert "processing" not in sig
        assert "ok" in sig

    def _search_loop(self) -> tuple[ToolUseLoop, _FakeSessionTool]:
        """A loop holding a session tool that declares itself poll-class."""
        tool = _FakeSessionTool("agentic_search", ["processing"])
        tool.poll_when_args = ("run_id",)
        with patch("mewbo_core.tool_use_loop.build_chat_model") as build:
            build.return_value = MagicMock()
            build.return_value.bind_tools.return_value = MagicMock()
            loop = ToolUseLoop(
                agent_context=_ctx(),
                tool_registry=_registry(_spec()),
                permission_policy=_policy(),
                hook_manager=_hooks(),
                session_id="s1",
                extra_session_tools=[tool],
            )
        return loop, tool

    def test_runtime_polling_a_run_does_not_halt_the_loop(self):
        # END-TO-END through ``loop.run``: proves the declared exemption is
        # actually REACHED by the guard the loop constructs, not merely correct
        # in isolation. Identical repeated polls with an identical "processing"
        # answer is the exact 100%-reproducible shape that used to halt.
        loop, tool = self._search_loop()
        poll = _tool_call("agentic_search", {"run_id": "r1"})
        _tq, state, _seen = _run_loop(
            loop, [poll, poll, poll, poll, poll, AIMessage(content="answer")], [_spec()]
        )
        assert state.done_reason == "completed"
        assert tool.calls >= 4, "the run must really have polled repeatedly"

    def test_runtime_restarting_the_same_search_still_halts(self):
        # The other direction, and the reason the exemption is per-CALL: an
        # exemption that swallows genuine thrash is as bad as the false halt.
        loop, _tool = self._search_loop()
        start = _tool_call("agentic_search", {"query": "same thing"})
        _tq, state, _seen = _run_loop(
            loop, [start, start, start, start, start, AIMessage(content="answer")], [_spec()]
        )
        assert state.done_reason == "halted_no_progress"

    def test_the_loop_resolves_poll_rules_from_tool_declarations(self):
        # Core names no tool: the rules come from what each tool declares, at
        # whichever seam owns it, so adding the next one never edits core.
        polling_tool = _FakeSessionTool("agentic_search", ["processing"])
        polling_tool.poll_when_args = ("run_id",)
        waiter = _FakeSessionTool("await_thing", ["done"])
        waiter.poll_class = True
        with patch("mewbo_core.tool_use_loop.build_chat_model") as build:
            build.return_value = MagicMock()
            build.return_value.bind_tools.return_value = MagicMock()
            loop = ToolUseLoop(
                agent_context=_ctx(),
                tool_registry=_registry(),
                permission_policy=_policy(),
                hook_manager=_hooks(),
                session_id="s1",
                extra_session_tools=[polling_tool, waiter],
            )
        rules = loop._poll_class_rules(
            [
                _spec("probe", poll=True),
                _spec("run_gate", poll_when_args=("run_id",)),
                _spec("plain"),
            ]
        )
        assert PollClassRule("probe") in rules
        assert PollClassRule("run_gate", frozenset({"run_id"})) in rules
        assert PollClassRule("agentic_search", frozenset({"run_id"})) in rules
        assert PollClassRule("await_thing") in rules
        assert not any(r.tool_id == "plain" for r in rules)


class TestErrorEnvelopePermanence:
    """The envelope gains a typed retry verdict, additively."""

    def test_permanence_is_parsed_when_declared(self):
        parsed = _SessionToolError.parse(
            str({"error": {"code": "repo_access", "message": "no", "permanence": "permanent"}})
        )
        assert parsed is not None
        assert parsed.permanence == "permanent"
        assert parsed.blocks_completion is True

    def test_an_envelope_without_permanence_is_unchanged(self):
        parsed = _SessionToolError.parse(str({"error": {"code": "validation", "message": "bad"}}))
        assert parsed is not None
        assert parsed.permanence is None
        assert parsed.summary == "validation: bad"
        assert parsed.blocks_completion is False

    def test_an_unrecognised_permanence_reads_as_undeclared(self):
        # A typo must never present as a typed verdict downstream.
        parsed = _SessionToolError.parse(
            str({"error": {"code": "x", "message": "y", "permanence": "mostly"}})
        )
        assert parsed is not None and parsed.permanence is None

    def test_a_json_style_envelope_is_still_declined(self):
        # Recorded because it is the live trap: these envelopes travel as
        # ``str(dict)`` and the parser is ``ast.literal_eval``. A tool reaching
        # for ``json.dumps`` emits ``true``/``null`` literals Python cannot
        # evaluate, and its failure then records as a success.
        assert _SessionToolError.parse('{"error": {"code": "x", "retry": true}}') is None

    def test_an_envelope_return_is_still_a_FAILED_step(self):
        # The behaviour the old string-only helper provided, preserved through
        # the structured parse that replaced its call site. A tool that RETURNS
        # an envelope returns it as a *successful* MockSpeaker, so without this
        # reclassification the step records success=True and the per-step
        # failure nudge never fires — an enveloped error renders as "ok".
        envelope = str({"error": {"code": "validation", "message": "bad args"}})
        tool = _FakeSessionTool("enveloping", [envelope])
        with patch("mewbo_core.tool_use_loop.build_chat_model") as build:
            build.return_value = MagicMock()
            build.return_value.bind_tools.return_value = MagicMock()
            loop = ToolUseLoop(
                agent_context=_ctx(),
                tool_registry=_registry(_spec()),
                permission_policy=_policy(),
                hook_manager=_hooks(),
                session_id="s1",
                extra_session_tools=[tool],
            )
        result = asyncio.run(
            loop._execute_tool_call(
                {"name": "enveloping", "args": {}, "id": "c1"}, [_spec()]
            )
        )
        assert result.success is False
        assert "bad args" in result.content

    def test_the_permanence_verdict_reaches_the_event(self):
        events: list = []
        envelope = str(
            {"error": {"code": "forbidden", "message": "denied", "permanence": "permanent"}}
        )
        tool = _FakeSessionTool("gated", [envelope])
        with patch("mewbo_core.tool_use_loop.build_chat_model") as build:
            build.return_value = MagicMock()
            build.return_value.bind_tools.return_value = MagicMock()
            loop = ToolUseLoop(
                agent_context=_ctx(event_logger=events.append),
                tool_registry=_registry(_spec()),
                permission_policy=_policy(),
                hook_manager=_hooks(),
                session_id="s1",
                extra_session_tools=[tool],
            )
        _run_loop(loop, [_tool_call("gated", {}), AIMessage(content="done")], [_spec()])
        payload = [e for e in events if e["type"] == "tool_result"][0]["payload"]
        assert payload["permanence"] == "permanent"
        assert payload["success"] is False


# ---------------------------------------------------------------------------
# E-V — the tool ceiling reaches loop-injected tools
# ---------------------------------------------------------------------------


class TestLoopInjectedToolCeiling:
    """A strict allowlist is authoritative for what the loop injects, too."""

    def _loop(self, allowed_tools, *, strict: bool, with_skills: bool = True) -> ToolUseLoop:
        skills = None
        if with_skills:
            skills = MagicMock()
            skills.list_auto_invocable.return_value = [MagicMock()]
        with patch("mewbo_core.tool_use_loop.build_chat_model") as build:
            build.return_value = MagicMock()
            build.return_value.bind_tools.return_value = MagicMock()
            return ToolUseLoop(
                agent_context=_ctx(),
                tool_registry=_registry(_spec()),
                permission_policy=_policy(),
                hook_manager=_hooks(),
                skill_registry=skills,
                session_id="s1",
                allowed_tools=allowed_tools,
                strict_tool_scope=strict,
            )

    def _schema_names(self, loop: ToolUseLoop) -> set[str]:
        names: set[str] = set()
        for schema in loop._directly_bound_tool_schemas(plan_mode=False):
            if not isinstance(schema, dict):
                continue
            # Two shapes reach the binder: the spawn family ships the OpenAI
            # ``{"type": "function", "function": {...}}`` envelope, session tools
            # a bare schema.
            fn = schema.get("function")
            name = fn.get("name") if isinstance(fn, dict) else schema.get("name")
            if name:
                names.add(str(name))
        return names

    def test_strict_scope_withholds_unnamed_loop_injected_tools(self):
        # These bypass ``filter_specs`` by design, and so bypassed the allowlist
        # too: an agent scoped to five tools in fact held those five plus every
        # one of these. That surplus is the surface a wandering run wanders into.
        loop = self._loop(["test_tool", "spawn_agent", "check_agents"], strict=True)
        names = self._schema_names(loop)
        assert "check_agents" in names
        assert "steer_agent" not in names
        assert "activate_skill" not in names
        assert "update_todos" not in [t.tool_id for t in loop._session_tools]

    def test_exit_plan_mode_is_never_capped(self):
        # The one deliberate exemption: it is the ONLY way out of plan mode, so
        # withholding it from a scope that forgot to name it leaves a plan-mode
        # run with no exit. A structural terminator, not a distraction surface.
        loop = self._loop(["test_tool"], strict=True)
        assert "exit_plan_mode" in [t.tool_id for t in loop._session_tools]

    def test_strict_scope_admits_what_it_names(self):
        loop = self._loop(
            ["test_tool", "spawn_agent", "steer_agent", "activate_skill", "update_todos"],
            strict=True,
        )
        names = self._schema_names(loop)
        assert "steer_agent" in names
        assert "activate_skill" in names
        assert "update_todos" in [t.tool_id for t in loop._session_tools]

    def test_a_permissive_console_root_keeps_every_builtin(self):
        # The df875 trap: a console/mobile root always sends a large
        # ``context.mcp_tools`` list, which is a ceiling over MCP tools alone and
        # never names a built-in. Treating it as authoritative here would strip
        # the built-ins off every such session.
        loop = self._loop(["mcp_github_search", "mcp_linear_issues"], strict=False)
        names = self._schema_names(loop)
        assert {"check_agents", "steer_agent", "activate_skill"} <= names
        assert "update_todos" in [t.tool_id for t in loop._session_tools]

    def test_an_unrestricted_root_is_unchanged(self):
        loop = self._loop(None, strict=True)
        names = self._schema_names(loop)
        assert {"check_agents", "steer_agent", "activate_skill"} <= names

    def test_an_empty_strict_allowlist_grants_nothing(self):
        # Three-state law: ``[]`` is an explicit grant of nothing, never
        # "unrestricted" — collapsing the two is the fail-open direction on the
        # gate that binds an agent's whole tool surface.
        loop = self._loop([], strict=True)
        assert self._schema_names(loop) == set()
        assert [t.tool_id for t in loop._session_tools] == ["exit_plan_mode"]

    def test_tool_search_survives_a_strict_scope_while_tools_are_deferred(self):
        # Load-bearing: strip it while tools are deferred and a scoped agent
        # loses its MCP schemas AND the only means to fetch them.
        loop = self._loop(["test_tool"], strict=True)
        loop._tool_search_enabled = True
        loop._deferred_ids = {"mcp_github_search"}
        kept = loop._select_active_specs(
            [_spec("tool_search"), _spec("test_tool")], discovered=set()
        )
        assert "tool_search" in {s.tool_id for s in kept}

    def test_tool_search_is_capped_when_there_is_nothing_to_fetch(self):
        loop = self._loop(["test_tool"], strict=True)
        loop._tool_search_enabled = False
        loop._deferred_ids = set()
        kept = loop._select_active_specs(
            [_spec("tool_search"), _spec("test_tool")], discovered=set()
        )
        assert "tool_search" not in {s.tool_id for s in kept}
        assert "test_tool" in {s.tool_id for s in kept}


# ---------------------------------------------------------------------------
# blocked_code on run state
# ---------------------------------------------------------------------------


class TestBlockedCodeOnRunState:
    """A clean-looking last turn must not launder an unrecovered blocker."""

    def _loop(self, tool) -> ToolUseLoop:
        with patch("mewbo_core.tool_use_loop.build_chat_model") as build:
            build.return_value = MagicMock()
            build.return_value.bind_tools.return_value = MagicMock()
            return ToolUseLoop(
                agent_context=_ctx(),
                tool_registry=_registry(_spec()),
                permission_policy=_policy(),
                hook_manager=_hooks(),
                session_id="s1",
                extra_session_tools=[tool],
            )

    def test_an_unrecovered_blocker_is_stamped_on_a_clean_completion(self):
        # The reference shape: a clone that never authenticated, followed by a
        # text turn, stamped ``completed`` and lost its recovery affordance.
        envelope = str({"error": {"code": "repo_access", "message": "auth failed"}})
        loop = self._loop(_FakeSessionTool("clone_repo", [envelope]))

        _tq, state, _seen = _run_loop(
            loop,
            [_tool_call("clone_repo", {}), AIMessage(content="All set!")],
            [_spec()],
        )

        assert state.done_reason == "completed", "done_reason vocabulary is unchanged"
        assert state.blocked_code == "repo_access"

    def test_a_later_success_from_the_same_tool_clears_it(self):
        envelope = str({"error": {"code": "network", "message": "unreachable"}})
        loop = self._loop(_FakeSessionTool("clone_repo", [envelope, "cloned ok"]))

        _tq, state, _seen = _run_loop(
            loop,
            [
                _tool_call("clone_repo", {}, "c1"),
                _tool_call("clone_repo", {"retry": 1}, "c2"),
                AIMessage(content="All set!"),
            ],
            [_spec()],
        )
        assert state.blocked_code is None

    def test_an_unrelated_tool_succeeding_does_not_clear_it(self):
        # Recovery is judged per tool: another tool working says nothing about
        # whether the repo ever became reachable.
        envelope = str({"error": {"code": "forbidden", "message": "denied"}})
        blocked = _FakeSessionTool("clone_repo", [envelope])
        other = _FakeSessionTool("take_notes", ["noted"])
        with patch("mewbo_core.tool_use_loop.build_chat_model") as build:
            build.return_value = MagicMock()
            build.return_value.bind_tools.return_value = MagicMock()
            loop = ToolUseLoop(
                agent_context=_ctx(),
                tool_registry=_registry(_spec()),
                permission_policy=_policy(),
                hook_manager=_hooks(),
                session_id="s1",
                extra_session_tools=[blocked, other],
            )

        _tq, state, _seen = _run_loop(
            loop,
            [
                _tool_call("clone_repo", {}, "c1"),
                _tool_call("take_notes", {}, "c2"),
                AIMessage(content="All set!"),
            ],
            [_spec()],
        )
        assert state.blocked_code == "forbidden"

    def test_a_non_blocking_envelope_code_is_not_stamped(self):
        # ``validation`` is the tool telling the model to fix its arguments —
        # a retry can clear it, so it is a failed step and nothing more.
        envelope = str({"error": {"code": "validation", "message": "bad args"}})
        loop = self._loop(_FakeSessionTool("clone_repo", [envelope]))

        _tq, state, _seen = _run_loop(
            loop,
            [_tool_call("clone_repo", {}), AIMessage(content="All set!")],
            [_spec()],
        )
        assert state.blocked_code is None

    def test_a_clean_run_carries_no_blocked_code(self):
        loop = self._loop(_FakeSessionTool("clone_repo", ["cloned ok"]))
        _tq, state, _seen = _run_loop(
            loop,
            [_tool_call("clone_repo", {}), AIMessage(content="All set!")],
            [_spec()],
        )
        assert state.blocked_code is None
        assert state.done_reason == "completed"


# ---------------------------------------------------------------------------
# E-I — LLM-call liveness
# ---------------------------------------------------------------------------


class TestLlmCallLiveness:
    """A wedged model call must be visible while it is wedged."""

    def _loop(self) -> ToolUseLoop:
        with patch("mewbo_core.tool_use_loop.build_chat_model") as build:
            build.return_value = MagicMock()
            build.return_value.bind_tools.return_value = MagicMock()
            return ToolUseLoop(
                agent_context=_ctx(),
                tool_registry=_registry(_spec()),
                permission_policy=_policy(),
                hook_manager=_hooks(),
            )

    def test_no_in_flight_call_reads_as_no_age(self):
        assert self._loop().llm_call_stall_age(now=1_000.0) is None

    def test_an_in_flight_call_reports_its_age_against_the_supplied_clock(self):
        # The clock is an argument, so a 24-minute wedge is expressible without
        # waiting 24 minutes or patching time.
        loop = self._loop()
        loop._llm_call_started_at = 1_000.0
        assert loop.llm_call_stall_age(now=1_000.0 + 1440.0) == 1440.0

    def test_the_age_is_disarmed_once_the_call_returns(self):
        loop = self._loop()
        captured: list[float | None] = []

        class _Strategy:
            async def run(self, **kwargs):
                captured.append(loop.llm_call_stall_age(now=0.0))
                return AIMessage(content="ok"), "primary-model"

        _tq, _state, _seen = _run_loop(loop, [AIMessage(content="done")], [_spec()])
        # A completed run leaves nothing outstanding, so a later sweep can never
        # read a finished call as a wedged one.
        assert loop.llm_call_stall_age(now=10_000.0) is None


# ---------------------------------------------------------------------------
# The loop-side promise-as-completion gate
# ---------------------------------------------------------------------------


class TestPromiseGate:
    """A root must not declare a clean terminal while its own runs are live."""

    def _drive_root(self, hv, ctx, ainvoke):
        events: list = []
        with patch("mewbo_core.tool_use_loop.build_chat_model") as build:
            build.return_value = MagicMock()
            bound = MagicMock()
            bound.ainvoke = AsyncMock(side_effect=ainvoke)
            del bound.astream
            build.return_value.bind_tools.return_value = bound
            loop = ToolUseLoop(
                agent_context=ctx,
                tool_registry=_registry(_spec()),
                permission_policy=_policy(),
                hook_manager=_hooks(),
            )
            tq, state = asyncio.run(loop.run("kick off the reindex", tool_specs=[_spec()]))
        return tq, state, events

    def _register_live_child(self, hv, ctx, agent_id="child-1"):
        asyncio.run(
            hv.register(
                AgentHandle(
                    agent_id=agent_id,
                    parent_id=ctx.agent_id,
                    depth=1,
                    model_name="primary-model",
                    task_description="probe",
                    status="running",
                )
            )
        )

    def test_a_terminal_is_refused_until_the_owned_run_settles(self):
        hv = AgentHypervisor(max_concurrent=100)
        ctx = AgentContext.root(model_name="primary-model", max_depth=5, registry=hv)
        self._register_live_child(hv, ctx)
        calls = {"n": 0}

        async def _ainvoke(messages, config=None, **kwargs):
            calls["n"] += 1
            # By the second turn the background run has settled, so the gate
            # opens and the (now honest) terminal is accepted.
            if calls["n"] == 2:
                handle = await hv.get("child-1")
                if handle is not None:
                    handle.status = "completed"
            return AIMessage(content="All set — I'll check back on it shortly.")

        _tq, state, _events = self._drive_root(hv, ctx, _ainvoke)
        assert state.done_reason == "completed"
        # Refused once, accepted on the second — the gate cost exactly one extra
        # turn, not a spin.
        assert calls["n"] == 2

    def test_the_root_own_running_handle_does_not_trip_the_gate(self):
        # The root is still ``running`` at the accept (marked done only in the
        # loop's finally). Reading the session-wide ownership index here would
        # count the root itself and refuse every terminal; keying on owned
        # CHILDREN is what avoids that.
        hv = AgentHypervisor(max_concurrent=100)
        ctx = AgentContext.root(model_name="primary-model", max_depth=5, registry=hv)
        calls = {"n": 0}

        async def _ainvoke(messages, config=None, **kwargs):
            calls["n"] += 1
            return AIMessage(content="Done. 12 pages written.")

        _tq, state, _events = self._drive_root(hv, ctx, _ainvoke)
        assert state.done_reason == "completed"
        assert calls["n"] == 1  # accepted on the first terminal, never refused

    def test_the_gate_is_bounded_for_a_model_that_will_not_wait(self):
        from mewbo_core.tool_use_loop import _PROMISE_GATE_MAX_NUDGES

        hv = AgentHypervisor(max_concurrent=100)
        ctx = AgentContext.root(model_name="primary-model", max_depth=5, registry=hv)
        self._register_live_child(hv, ctx)  # never settles
        calls = {"n": 0}

        async def _ainvoke(messages, config=None, **kwargs):
            calls["n"] += 1
            return AIMessage(content="I'll follow up later.")

        _tq, state, _events = self._drive_root(hv, ctx, _ainvoke)
        # After the cap the terminal is let through (the orchestrator's honesty
        # downgrade records the truth); the whole budget is never spent spinning.
        assert state.done_reason == "completed"
        assert calls["n"] == _PROMISE_GATE_MAX_NUDGES + 1


# ---------------------------------------------------------------------------
# A child that finished on store side-effects is forced to summarize
# ---------------------------------------------------------------------------


class TestForcedChildSummary:
    def _drive_child(self, *, main_text, summary_text, depth_child=True):
        hv = AgentHypervisor(max_concurrent=100)
        root = AgentContext.root(model_name="primary-model", max_depth=5, registry=hv)
        ctx = root.child() if depth_child else root
        with patch("mewbo_core.tool_use_loop.build_chat_model") as build:
            unbound = MagicMock()
            unbound.ainvoke = AsyncMock(return_value=AIMessage(content=summary_text))
            build.return_value = unbound
            bound = MagicMock()
            bound.ainvoke = AsyncMock(return_value=AIMessage(content=main_text))
            del bound.astream
            unbound.bind_tools.return_value = bound
            loop = ToolUseLoop(
                agent_context=ctx,
                tool_registry=_registry(_spec()),
                permission_policy=_policy(),
                hook_manager=_hooks(),
            )
            tq, state = asyncio.run(loop.run("map the repo", tool_specs=[_spec()]))
        return tq, state

    def test_a_child_ending_on_empty_text_is_forced_to_summarize(self):
        # The heaviest sub-agents did their work through tool writes then
        # stopped on empty text, reaching the parent — which projects
        # task_result as the summary — blank.
        tq, state = self._drive_child(main_text="", summary_text="Mapped 40 files.")
        assert state.done_reason == "completed"
        assert tq.task_result == "Mapped 40 files."

    def test_a_child_with_real_closing_text_is_not_re_summarized(self):
        # A non-empty terminal already carries the summary; no extra turn.
        tq, state = self._drive_child(
            main_text="Indexed everything, 40 files.", summary_text="SHOULD-NOT-APPEAR"
        )
        assert tq.task_result == "Indexed everything, 40 files."

    def test_the_root_empty_terminal_is_not_forced_to_summarize(self):
        # The root's empty terminal is a user-facing turn, not a summary owed
        # upstream — forcing one would burn a call for nobody.
        tq, state = self._drive_child(
            main_text="", summary_text="SHOULD-NOT-APPEAR", depth_child=False
        )
        assert tq.task_result == ""
