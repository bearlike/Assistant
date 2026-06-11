#!/usr/bin/env python3
"""Integration and contract tests for orchestrator.py and planning.py.

Covers the real behavior paths missing from coverage:
- Orchestrator session lifecycle: memory update, auto-compact triggers, tool scoping
- Session capabilities parsing from transcript context events
- Skill invocation detection and mode resolution
- PromptBuilder with project_instructions, context sections
- Planner intent inference and spec filtering (no real LLM needed)
- _maybe_auto_compact with needs_compact=True (mocked LLM compact)
- _update_summary_with_memory keyword detection
- _try_skill_invocation path when a skill matches
- _session_capabilities reading client_capabilities from transcript
- plan-mode path ensuring plan directory is created
- strict_tool_scope vs permissive allowed_tools filtering
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from mewbo_core.context import ContextSnapshot
from mewbo_core.orchestrator import Orchestrator
from mewbo_core.planning import Planner, PromptBuilder
from mewbo_core.session_store import SessionStore
from mewbo_core.token_budget import TokenBudget
from mewbo_core.tool_registry import ToolRegistry, ToolSpec
from mewbo_core.tool_use_loop import ToolUseLoop

# ---------------------------------------------------------------------------
# Helpers shared across all test classes
# ---------------------------------------------------------------------------


def _make_orchestrator(tmp_path):
    """Return (Orchestrator, SessionStore) backed by a fresh tmp dir."""
    store = SessionStore(root_dir=str(tmp_path))
    return Orchestrator(session_store=store), store


def _make_context(
    summary: str | None = None,
    recent_events=None,
    selected_events=None,
) -> ContextSnapshot:
    return ContextSnapshot(
        summary=summary,
        recent_events=recent_events or [],
        selected_events=selected_events,
        events=[],
        budget=TokenBudget(
            total_tokens=0,
            summary_tokens=0,
            event_tokens=0,
            context_window=128000,
            remaining_tokens=128000,
            utilization=0.0,
            threshold=0.8,
        ),
    )


def _make_spec(tool_id: str, kind: str = "local") -> ToolSpec:
    return ToolSpec(
        tool_id=tool_id,
        name=tool_id,
        description=f"Test tool {tool_id}",
        factory=lambda: MagicMock(),
        enabled=True,
        kind=kind,
    )


async def _simple_loop_run(*args, **kwargs):
    """Replacement for ToolUseLoop.run that completes immediately."""
    from mewbo_core.classes import OrchestrationState, TaskQueue

    tq = TaskQueue(action_steps=[])
    tq.task_result = "Done"
    state = OrchestrationState(goal="test", session_id="s1")
    state.done = True
    state.done_reason = "completed"
    return tq, state


# ---------------------------------------------------------------------------
# PromptBuilder contract tests
# ---------------------------------------------------------------------------


class TestPromptBuilder:
    """PromptBuilder.build() contract — sections are conditionally included."""

    def test_project_instructions_included(self):
        """Line 108: project_instructions section appended when provided."""
        builder = PromptBuilder(tool_registry=None)
        result = builder.build(
            "base prompt",
            context=None,
            project_instructions="Use Python 3.12",
        )
        assert "Project instructions:" in result
        assert "Use Python 3.12" in result

    def test_no_project_instructions_absent(self):
        builder = PromptBuilder(tool_registry=None)
        result = builder.build("base prompt", context=None)
        assert "Project instructions:" not in result

    def test_session_summary_appended(self):
        builder = PromptBuilder(tool_registry=None)
        ctx = _make_context(summary="Previous session summary")
        result = builder.build("base", context=ctx)
        assert "Session summary:" in result
        assert "Previous session summary" in result

    def test_selected_events_included(self):
        """Relevant earlier context section from selected_events."""
        builder = PromptBuilder(tool_registry=None)
        selected = [{"type": "user", "payload": {"text": "old query"}}]
        ctx = _make_context(selected_events=selected)
        result = builder.build("base", context=ctx)
        assert "Relevant earlier context:" in result

    def test_recent_events_included(self):
        builder = PromptBuilder(tool_registry=None)
        recent = [{"type": "assistant", "payload": {"text": "recent reply"}}]
        ctx = _make_context(recent_events=recent)
        result = builder.build("base", context=ctx)
        assert "Recent conversation:" in result

    def test_tool_registry_lists_specs(self):
        """Available tools section from registry specs."""
        registry = ToolRegistry()
        registry.register(_make_spec("shell_tool"))
        builder = PromptBuilder(tool_registry=registry)
        result = builder.build("base", context=None)
        assert "Available tools:" in result
        assert "shell_tool" in result

    def test_component_status_appended(self):
        from mewbo_core.components import ComponentStatus

        builder = PromptBuilder(tool_registry=None)
        status = [ComponentStatus(name="langfuse", enabled=True)]
        result = builder.build("base", context=None, component_status=status)
        assert "Component status:" in result


# ---------------------------------------------------------------------------
# Planner intent inference and spec filtering (no real LLM)
# ---------------------------------------------------------------------------


class TestPlannerIntentInference:
    """Planner._infer_intent_capabilities and _filter_specs_by_intent."""

    def test_web_keywords_infer_web_capabilities(self):
        caps = Planner._infer_intent_capabilities("What is the latest news today?")
        assert "web_search" in caps

    def test_file_keywords_infer_file_capabilities(self):
        caps = Planner._infer_intent_capabilities("edit the config file")
        assert "file_write" in caps

    def test_shell_keywords_infer_shell_capabilities(self):
        caps = Planner._infer_intent_capabilities("run the shell command")
        assert "shell_exec" in caps

    def test_home_keywords_infer_home_capabilities(self):
        caps = Planner._infer_intent_capabilities("turn on the light switch")
        assert "home_assistant" in caps

    def test_no_keywords_returns_empty(self):
        caps = Planner._infer_intent_capabilities("hello world")
        assert caps == set()

    def test_spec_capabilities_from_metadata(self):
        """Line 287-288: explicit capabilities list in metadata takes priority."""
        spec = MagicMock()
        spec.metadata = {"capabilities": ["web_search", "file_read"]}
        spec.tool_id = "something_random"
        caps = Planner._spec_capabilities(spec)
        assert caps == {"web_search", "file_read"}

    def test_spec_capabilities_inferred_from_tool_id_web_search(self):
        """Line 292-293: web_search inferred from tool_id."""
        spec = MagicMock()
        spec.metadata = {}
        spec.tool_id = "internet_search_tool"
        caps = Planner._spec_capabilities(spec)
        assert "web_search" in caps

    def test_spec_capabilities_inferred_web_url_read(self):
        """Line 294-295: web_read inferred from web_url_read in tool_id."""
        spec = MagicMock()
        spec.metadata = {}
        spec.tool_id = "web_url_read_tool"
        caps = Planner._spec_capabilities(spec)
        assert "web_read" in caps

    def test_spec_capabilities_inferred_file_read(self):
        """Line 296-297: file_read from aider_read_file."""
        spec = MagicMock()
        spec.metadata = {}
        spec.tool_id = "aider_read_file"
        caps = Planner._spec_capabilities(spec)
        assert "file_read" in caps

    def test_spec_capabilities_inferred_file_write(self):
        """Line 298-299: file_write from aider edit tool."""
        spec = MagicMock()
        spec.metadata = {}
        spec.tool_id = "aider_edit_block_tool"
        caps = Planner._spec_capabilities(spec)
        assert "file_write" in caps

    def test_spec_capabilities_inferred_shell(self):
        """Line 300-301: shell_exec from shell in tool_id."""
        spec = MagicMock()
        spec.metadata = {}
        spec.tool_id = "aider_shell_tool"
        caps = Planner._spec_capabilities(spec)
        assert "shell_exec" in caps

    def test_spec_capabilities_inferred_home_assistant(self):
        """Line 302-303: home_assistant from home_assistant in tool_id."""
        spec = MagicMock()
        spec.metadata = {}
        spec.tool_id = "home_assistant_tool"
        caps = Planner._spec_capabilities(spec)
        assert "home_assistant" in caps

    def test_filter_specs_by_intent_filters_matching(self):
        """Line 310: intent filtering keeps only matching specs."""
        registry = ToolRegistry()
        registry.register(_make_spec("internet_search_tool"))
        registry.register(_make_spec("shell_exec_tool"))
        planner = Planner(tool_registry=registry)
        specs = registry.list_specs()

        filtered = planner._filter_specs_by_intent(specs, "search the web for latest news")
        ids = {s.tool_id for s in filtered}
        assert "internet_search_tool" in ids

    def test_filter_specs_returns_all_when_no_match(self):
        """Line 311: when filtered is empty, fall back to all specs."""
        registry = ToolRegistry()
        registry.register(_make_spec("shell_tool"))
        planner = Planner(tool_registry=registry)
        specs = registry.list_specs()

        # Query that matches a capability not present in specs → falls back to all
        planner._filter_specs_by_intent(specs, "run the shell command")
        # shell_tool is the only spec and doesn't match shell_exec fallback pattern (no 'shell' hit)
        # Actually shell_tool has 'shell' in tool_id → shell_exec. So we want a query that
        # has file write keywords but no file tools to test the fallback path.
        registry2 = ToolRegistry()
        registry2.register(_make_spec("some_unrelated_tool"))
        planner2 = Planner(tool_registry=registry2)
        specs2 = registry2.list_specs()
        filtered2 = planner2._filter_specs_by_intent(specs2, "edit the config file")
        # No matching specs → returns original list as fallback
        assert set(s.tool_id for s in filtered2) == {"some_unrelated_tool"}

    def test_filter_specs_no_intent_returns_unchanged(self):
        """Line 308-309: empty requested capabilities returns specs unchanged."""
        registry = ToolRegistry()
        registry.register(_make_spec("tool_a"))
        registry.register(_make_spec("tool_b"))
        planner = Planner(tool_registry=registry)
        specs = registry.list_specs()
        # No keywords → empty caps → return all specs unchanged
        result = planner._filter_specs_by_intent(specs, "hello world")
        assert {s.tool_id for s in result} == {"tool_a", "tool_b"}


# ---------------------------------------------------------------------------
# Planner.generate() — plan-mode spec selection (mocked LLM)
# ---------------------------------------------------------------------------


class TestPlannerGenerate:
    """Planner.generate() uses different spec selection per mode (no real LLM)."""

    def _make_mock_chain(self, return_plan):
        """Build a mock (prompt | model | parser) chain that returns return_plan."""

        chain = MagicMock()
        chain.__or__ = lambda self, other: chain
        chain.invoke = MagicMock(return_value=return_plan)
        return chain

    def test_plan_mode_uses_all_specs(self):
        """Line 209: mode='plan' calls list_specs() (not list_specs_for_mode)."""
        from mewbo_core.classes import Plan

        registry = MagicMock(spec=ToolRegistry)
        registry.list_specs.return_value = [_make_spec("tool_a")]
        registry.list_specs_for_mode.return_value = []

        planner = Planner(tool_registry=registry)

        plan_result = Plan(steps=[])

        with (
            patch("mewbo_core.planning.build_chat_model") as mock_build,
            patch("mewbo_core.planning.build_langfuse_handler", return_value=None),
            patch("mewbo_core.planning.langfuse_trace_span") as mock_span,
        ):
            mock_model = MagicMock()
            mock_parser = MagicMock()
            mock_chain = MagicMock()
            mock_chain.invoke.return_value = plan_result
            # Make (prompt | model | parser) chain work
            mock_build.return_value = mock_model
            mock_model.__or__ = MagicMock(return_value=mock_chain)
            mock_chain.__or__ = MagicMock(return_value=mock_chain)

            span_ctx = MagicMock()
            span_ctx.__enter__ = MagicMock(return_value=None)
            span_ctx.__exit__ = MagicMock(return_value=False)
            mock_span.return_value = span_ctx

            with patch("mewbo_core.planning.PydanticOutputParser") as mock_p_cls:
                mock_p_cls.return_value = mock_parser
                mock_parser.get_format_instructions.return_value = ""
                mock_parser.__or__ = MagicMock(return_value=mock_chain)

                with patch("mewbo_core.planning.ChatPromptTemplate") as mock_pt:
                    prompt_inst = MagicMock()
                    prompt_inst.__or__ = MagicMock(return_value=mock_chain)
                    mock_pt.return_value = prompt_inst

                    planner.generate("do task", "test-model", mode="plan")

        # plan mode should call list_specs (not list_specs_for_mode)
        registry.list_specs.assert_called()
        # The mode='plan' branch specifically avoids list_specs_for_mode
        registry.list_specs_for_mode.assert_not_called()

    def test_feedback_appended_to_prompt(self):
        """Line 232: feedback string is appended when provided."""
        from mewbo_core.classes import Plan

        registry = MagicMock(spec=ToolRegistry)
        registry.list_specs.return_value = [_make_spec("tool_a")]
        registry.list_specs_for_mode.return_value = []

        planner = Planner(tool_registry=registry)
        plan_result = Plan(steps=[])

        captured_prompts = []

        with (
            patch("mewbo_core.planning.build_chat_model") as mock_build,
            patch("mewbo_core.planning.build_langfuse_handler", return_value=None),
            patch("mewbo_core.planning.langfuse_trace_span") as mock_span,
        ):
            mock_model = MagicMock()
            mock_chain = MagicMock()
            mock_chain.invoke.return_value = plan_result
            mock_build.return_value = mock_model

            span_ctx = MagicMock()
            span_ctx.__enter__ = MagicMock(return_value=None)
            span_ctx.__exit__ = MagicMock(return_value=False)
            mock_span.return_value = span_ctx

            with patch("mewbo_core.planning.PydanticOutputParser") as mock_p_cls:
                mock_parser = MagicMock()
                mock_p_cls.return_value = mock_parser
                mock_parser.get_format_instructions.return_value = ""

                with patch("mewbo_core.planning.ChatPromptTemplate") as mock_pt:

                    def capture_prompt(*args, **kwargs):
                        captured_prompts.append(kwargs)
                        prompt_inst = MagicMock()
                        prompt_inst.__or__ = MagicMock(return_value=mock_chain)
                        return prompt_inst

                    mock_pt.side_effect = capture_prompt

                    planner.generate(
                        "do task",
                        "test-model",
                        mode="act",
                        feedback="The plan was wrong because X",
                    )

        # feedback was passed — verify the feedback text appears in the captured prompt
        assert len(captured_prompts) == 1
        # ChatPromptTemplate is constructed with kwargs; inspect the messages argument.
        # The feedback string should appear somewhere in the prompt definition.
        prompt_kwargs = captured_prompts[0]
        # Check all keyword args for the feedback text
        prompt_text = str(prompt_kwargs)
        assert "The plan was wrong because X" in prompt_text

    def test_generate_raises_without_registry(self):
        """Line 195: ValueError when tool_registry is None."""
        planner = Planner(tool_registry=None)
        with pytest.raises(ValueError, match="Tool registry"):
            planner.generate("query", "model")


# ---------------------------------------------------------------------------
# Orchestrator: _should_update_summary keyword detection
# ---------------------------------------------------------------------------


class TestShouldUpdateSummary:
    @pytest.mark.parametrize(
        "text",
        [
            "please remember this",
            "note this down",
            "save this for later",
            "pin this info",
            "keep this",
            "magic number is 42",
            "magic numbers: 1 2 3",
        ],
    )
    def test_memory_keywords_trigger_update(self, text):
        assert Orchestrator._should_update_summary(text) is True

    def test_ordinary_text_does_not_trigger(self):
        assert Orchestrator._should_update_summary("What is the weather today?") is False


# ---------------------------------------------------------------------------
# Orchestrator: _update_summary_with_memory
# ---------------------------------------------------------------------------


class TestUpdateSummaryWithMemory:
    def test_adds_memory_line(self, tmp_path):
        """Line 666-673: memory line appended to session summary."""
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()
        result = orch._update_summary_with_memory(session_id, "remember this fact")
        assert "Memory: remember this fact" in result
        assert store.load_summary(session_id) is not None

    def test_no_duplicate_memory_lines(self, tmp_path):
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()
        orch._update_summary_with_memory(session_id, "same fact")
        result = orch._update_summary_with_memory(session_id, "same fact")
        count = result.count("Memory: same fact")
        assert count == 1

    def test_capped_at_ten_lines(self, tmp_path):
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()
        for i in range(15):
            orch._update_summary_with_memory(session_id, f"fact_{i}")
        summary = store.load_summary(session_id) or ""
        lines = [ln for ln in summary.splitlines() if ln.strip()]
        assert len(lines) <= 10


# ---------------------------------------------------------------------------
# Orchestrator: _session_capabilities
# ---------------------------------------------------------------------------


class TestSessionCapabilities:
    def test_reads_client_capabilities_from_context_event(self, tmp_path):
        """Lines 499-509: reads client_capabilities from the transcript."""
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()
        store.append_event(
            session_id,
            {
                "type": "context",
                "payload": {"client_capabilities": ["wiki", "search"]},
            },
        )
        caps = orch._session_capabilities(session_id)
        assert "wiki" in caps

    def test_empty_when_no_context_events(self, tmp_path):
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()
        store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})
        caps = orch._session_capabilities(session_id)
        assert caps == ()

    def test_returns_empty_on_load_error(self, tmp_path):
        """Line 500-501: exception in load_transcript returns empty tuple."""
        orch, store = _make_orchestrator(tmp_path)
        session_id = "nonexistent-session"
        # Should not raise; returns ()
        caps = orch._session_capabilities(session_id)
        assert caps == ()

    def test_runtime_provider_grants_capability_to_plain_session(self, tmp_path):
        """#83-B: a registered runtime provider surfaces ``scg`` to a PLAIN session.

        An ordinary session advertises NO capabilities, yet the single read-point
        (`_session_capabilities`, consumed by every downstream gate) unions in the
        provider's runtime grant — so the scg tools/AgentDefs scope in without the
        client advertising ``scg``.
        """
        from mewbo_core.capabilities import (
            register_session_capability_provider,
            reset_session_capability_providers,
        )

        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()
        store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})
        reset_session_capability_providers()
        try:
            # No provider yet → plain session stays bare.
            assert orch._session_capabilities(session_id) == ()
            # Predicate holds → the provider grants scg.
            register_session_capability_provider(
                lambda adv: ("scg",) if "scg" not in adv else ()
            )
            assert "scg" in orch._session_capabilities(session_id)
        finally:
            reset_session_capability_providers()

    def test_runtime_provider_withholds_when_predicate_false(self, tmp_path):
        """A provider whose predicate is False grants nothing (disabled / empty graph)."""
        from mewbo_core.capabilities import (
            register_session_capability_provider,
            reset_session_capability_providers,
        )

        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()
        store.append_event(
            session_id,
            {"type": "context", "payload": {"client_capabilities": ["wiki"]}},
        )
        reset_session_capability_providers()
        try:
            register_session_capability_provider(lambda _adv: ())  # predicate false
            caps = orch._session_capabilities(session_id)
            assert "scg" not in caps
            assert "wiki" in caps  # advertised capability survives untouched
        finally:
            reset_session_capability_providers()

    def test_runtime_granted_capability_lands_in_trace_metadata(self, tmp_path):
        """#84: a RUNTIME-granted capability shows in the Langfuse trace facet.

        The merged context carries only the advertised caps; ``run`` overlays the
        augmented set (advertised ∪ provider grants) before deriving provenance,
        so a session that never advertised ``scg`` but got it from the runtime
        provider still surfaces ``scg`` in the trace's ``capabilities`` metadata —
        otherwise the grant is live in the run yet invisible to a trace filter.
        """
        from mewbo_core.capabilities import (
            register_session_capability_provider,
            reset_session_capability_providers,
        )

        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()
        store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})

        captured: dict[str, object] = {}

        def _capture(*_args, **kwargs):
            captured.update(kwargs)
            raise _StopRun()  # short-circuit before the real loop runs

        reset_session_capability_providers()
        try:
            register_session_capability_provider(
                lambda adv: ("scg",) if "scg" not in adv else ()
            )
            with patch(
                "mewbo_core.orchestrator.langfuse_session_context",
                side_effect=_capture,
            ), patch.object(orch, "_session_store", store):
                with pytest.raises(_StopRun):
                    orch.run("deposit a bridge", session_id=session_id)
        finally:
            reset_session_capability_providers()

        meta = captured.get("metadata") or {}
        assert "scg" in str(meta.get("capabilities", ""))
        # And the low-cardinality capabilities chip is NOT a tag (high-ish card,
        # stays metadata-only) — the tag list carries origin/product/etc.
        assert isinstance(captured.get("tags"), list)


class _StopRun(Exception):
    """Sentinel to short-circuit ``Orchestrator.run`` after provenance derive."""


# ---------------------------------------------------------------------------
# Orchestrator: _resolve_mode
# ---------------------------------------------------------------------------


class TestResolveMode:
    @pytest.mark.parametrize(
        "mode,expected",
        [
            ("plan", "plan"),
            ("act", "act"),
            (None, "act"),
            ("unknown", "act"),
            ("", "act"),
        ],
    )
    def test_mode_resolution(self, mode, expected):
        assert Orchestrator._resolve_mode(mode) == expected


# ---------------------------------------------------------------------------
# Orchestrator: _try_skill_invocation
# ---------------------------------------------------------------------------


class TestTrySkillInvocation:
    def test_non_slash_query_returns_none_none(self, tmp_path):
        orch, _ = _make_orchestrator(tmp_path)
        si, ts = orch._try_skill_invocation("hello world", [], ())
        assert si is None
        assert ts is None

    def test_slash_command_not_in_registry_returns_none_none(self, tmp_path):
        orch, _ = _make_orchestrator(tmp_path)
        si, ts = orch._try_skill_invocation("/no-such-skill", [], ())
        assert si is None
        assert ts is None

    def test_slash_command_activates_matching_skill(self, tmp_path):
        """Lines 708-713: matching skill returns instructions + scoped specs."""
        orch, _ = _make_orchestrator(tmp_path)

        # Create a mock skill
        mock_skill = MagicMock()
        orch._skill_registry.get = MagicMock(return_value=mock_skill)

        with patch("mewbo_core.orchestrator.activate_skill") as mock_activate:
            mock_activate.return_value = ("skill instructions", ["spec_a"])
            si, ts = orch._try_skill_invocation("/my-skill some args", ["spec_a"], ())

        assert si == "skill instructions"
        assert ts == ["spec_a"]


# ---------------------------------------------------------------------------
# Orchestrator: memory update triggered from run() (integration)
# ---------------------------------------------------------------------------


class TestMemoryUpdateIntegration:
    def test_run_updates_summary_on_memory_keyword(self, tmp_path):
        """Line 236: _update_summary_with_memory called when 'remember' keyword in query."""
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()

        with patch.object(ToolUseLoop, "run", _simple_loop_run):
            orch.run(
                user_query="please remember that the port is 8080",
                session_id=session_id,
                max_iters=1,
            )

        summary = store.load_summary(session_id)
        assert summary is not None
        assert "8080" in summary


# ---------------------------------------------------------------------------
# Orchestrator: allowed_tools strict and permissive scope modes (integration)
# ---------------------------------------------------------------------------


class TestToolScopeIntegration:
    """Lines 314, 321, 325-326: strict vs permissive tool scope."""

    def test_strict_tool_scope_filters_to_allowed_only(self, tmp_path):
        """strict_tool_scope=True: only tools in allowed_tools survive."""
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()

        captured_specs = []

        async def capturing_run(self_loop, *args, tool_specs=None, **kwargs):
            if tool_specs is not None:
                captured_specs.extend(tool_specs)
            from mewbo_core.classes import OrchestrationState, TaskQueue

            tq = TaskQueue(action_steps=[])
            tq.task_result = "Done"
            state = OrchestrationState(goal="test", session_id=session_id)
            state.done = True
            state.done_reason = "completed"
            return tq, state

        with patch.object(ToolUseLoop, "run", capturing_run):
            orch.run(
                user_query="do something",
                session_id=session_id,
                allowed_tools=["read_file"],
                strict_tool_scope=True,
                max_iters=1,
            )

        # Strict scope: only "read_file" from allowed_tools survives; no other
        # local builtins and no MCP tools should be present.
        captured_ids = {s.tool_id for s in captured_specs}
        assert "read_file" in captured_ids
        # Non-allowed builtins must NOT be present in strict mode
        assert "aider_shell_tool" not in captured_ids
        assert "home_assistant_tool" not in captured_ids

    def test_permissive_tool_scope_keeps_builtins(self, tmp_path):
        """Lines 325-326: permissive mode adds builtins to allowed_tools."""
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()

        captured_specs = []

        async def capturing_run(self_loop, *args, tool_specs=None, **kwargs):
            if tool_specs is not None:
                captured_specs.extend(tool_specs)
            from mewbo_core.classes import OrchestrationState, TaskQueue

            tq = TaskQueue(action_steps=[])
            tq.task_result = "Done"
            state = OrchestrationState(goal="test", session_id=session_id)
            state.done = True
            state.done_reason = "completed"
            return tq, state

        with patch.object(ToolUseLoop, "run", capturing_run):
            orch.run(
                user_query="do something",
                session_id=session_id,
                allowed_tools=["mcp_some_tool"],
                strict_tool_scope=False,
                max_iters=1,
            )

        # Permissive mode: builtin (non-MCP) tools survive even when not in allowed_tools.
        # "read_file" is a local builtin so it must always be present.
        captured_ids = {s.tool_id for s in captured_specs}
        assert "read_file" in captured_ids


# ---------------------------------------------------------------------------
# Orchestrator: plan-mode path ensures plan directory created
# ---------------------------------------------------------------------------


class TestPlanModeIntegration:
    def test_plan_mode_sets_plan_path_on_state(self, tmp_path):
        """Lines 348-349: plan_path set on state when mode='plan'."""
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()

        with patch.object(ToolUseLoop, "run", _simple_loop_run):
            with patch("mewbo_core.orchestrator.ensure_plan_dir") as mock_epd:
                with patch("mewbo_core.orchestrator.plan_file_for", return_value="/tmp/plan.md"):
                    tq, state = orch.run(
                        user_query="plan this",
                        session_id=session_id,
                        mode="plan",
                        max_iters=1,
                        return_state=True,
                    )

        mock_epd.assert_called_once_with(session_id)
        assert state.plan_path == "/tmp/plan.md"


# ---------------------------------------------------------------------------
# Orchestrator: auto-compact triggers within run()
# ---------------------------------------------------------------------------


class TestAutoCompactIntegration:
    def test_auto_compact_skips_if_last_event_is_context_compacted(self, tmp_path):
        """Line 559-560: thrash guard — skip if last event is context_compacted."""
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()
        store.append_event(session_id, {"type": "context_compacted", "payload": {}})

        # Should return None (skip compaction) — no error
        result = orch._maybe_auto_compact(session_id)
        assert result is None

    def test_auto_compact_skips_when_budget_ok(self, tmp_path):
        """Line 571-572: skip if budget does not need compaction."""
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()
        store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})

        with patch("mewbo_core.orchestrator.get_token_budget") as mock_budget:
            budget = MagicMock()
            budget.needs_compact = False
            mock_budget.return_value = budget
            result = orch._maybe_auto_compact(session_id)

        assert result is None

    def test_auto_compact_runs_when_budget_exhausted(self, tmp_path):
        """Lines 573-596: compact_conversation called when needs_compact=True."""
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()
        store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})

        with patch("mewbo_core.orchestrator.get_token_budget") as mock_budget:
            budget = MagicMock()
            budget.needs_compact = True
            budget.total_tokens = 10000
            mock_budget.return_value = budget

            with patch("mewbo_core.compact.compact_conversation") as mock_compact:
                compact_result = MagicMock()
                compact_result.model = "test-model"
                compact_result.summary = "Compacted summary"
                compact_result.tokens_saved = 5000

                async def fake_compact(*args, **kwargs):
                    return compact_result

                mock_compact.side_effect = fake_compact

                with patch("mewbo_core.compact.record_compaction"):
                    result = orch._maybe_auto_compact(session_id)

        assert result == "Compacted summary"

    def test_auto_compact_returns_none_on_compact_failure(self, tmp_path):
        """Line 583-584: compact exception → log and return None."""
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()
        store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})

        with patch("mewbo_core.orchestrator.get_token_budget") as mock_budget:
            budget = MagicMock()
            budget.needs_compact = True
            budget.total_tokens = 10000
            mock_budget.return_value = budget

            async def raiser(*args, **kwargs):
                raise RuntimeError("compaction failed")

            with patch("mewbo_core.compact.compact_conversation", raiser):
                result = orch._maybe_auto_compact(session_id)

        assert result is None


# ---------------------------------------------------------------------------
# Orchestrator: completion event + error event paths
# ---------------------------------------------------------------------------


class TestCompletionEvents:
    def test_completion_event_emitted_on_success(self, tmp_path):
        """Lines 431-442: completion event with task_result."""
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()

        with patch.object(ToolUseLoop, "run", _simple_loop_run):
            orch.run(user_query="do something", session_id=session_id, max_iters=1)

        events = store.load_transcript(session_id)
        completion = [e for e in events if e.get("type") == "completion"]
        assert len(completion) == 1
        assert completion[0]["payload"]["done"] is True

    def test_run_emits_assistant_event_with_task_result(self, tmp_path):
        """Line 413-417: assistant event with non-empty task_result."""
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()

        with patch.object(ToolUseLoop, "run", _simple_loop_run):
            orch.run(user_query="do something", session_id=session_id, max_iters=1)

        events = store.load_transcript(session_id)
        assistant_events = [e for e in events if e.get("type") == "assistant"]
        assert len(assistant_events) == 1
        assert assistant_events[0]["payload"]["text"] == "Done"

    def test_run_emits_closure_when_no_task_result(self, tmp_path):
        """Line 419-423: synthetic closure when task_result is empty."""
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()

        async def empty_result_run(*args, **kwargs):
            from mewbo_core.classes import OrchestrationState, TaskQueue

            tq = TaskQueue(action_steps=[])
            tq.task_result = ""  # Empty
            tq.last_error = None
            state = OrchestrationState(goal="test", session_id=session_id)
            state.done = True
            state.done_reason = "completed"
            return tq, state

        with patch.object(ToolUseLoop, "run", empty_result_run):
            orch.run(user_query="do something", session_id=session_id, max_iters=1)

        events = store.load_transcript(session_id)
        assistant_events = [e for e in events if e.get("type") == "assistant"]
        assert len(assistant_events) == 1
        # Synthetic closure should have been emitted
        text = assistant_events[0]["payload"]["text"]
        assert text.startswith("(Run ended:")

    def test_run_emits_last_error_in_completion(self, tmp_path):
        """Lines 436-439: last_error included in completion payload."""
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()

        async def error_result_run(*args, **kwargs):
            from mewbo_core.classes import OrchestrationState, TaskQueue

            tq = TaskQueue(action_steps=[])
            tq.task_result = ""
            tq.last_error = "Something went wrong"
            state = OrchestrationState(goal="test", session_id=session_id)
            state.done = True
            state.done_reason = "error"
            return tq, state

        with patch.object(ToolUseLoop, "run", error_result_run):
            tq, state = orch.run(
                user_query="do something",
                session_id=session_id,
                max_iters=1,
                return_state=True,
            )

        events = store.load_transcript(session_id)
        completion = [e for e in events if e.get("type") == "completion"]
        assert completion[0]["payload"].get("last_error") == "Something went wrong"

    def test_run_with_return_state(self, tmp_path):
        """Line 448: return_state=True returns (task_queue, state) tuple."""
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()

        with patch.object(ToolUseLoop, "run", _simple_loop_run):
            result = orch.run(
                user_query="do something",
                session_id=session_id,
                max_iters=1,
                return_state=True,
            )

        assert isinstance(result, tuple)
        tq, state = result
        assert state.done is True


# ---------------------------------------------------------------------------
# Orchestrator: registry cleanup in finally block
# ---------------------------------------------------------------------------


class TestRegistryCleanup:
    def test_registry_cleanup_runs_after_loop(self, tmp_path):
        """Lines 401-402: registry.cleanup() called even on success."""
        from mewbo_core.hypervisor import AgentHypervisor

        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()

        cleanup_called = []

        original_cleanup = AgentHypervisor.cleanup

        async def tracking_cleanup(self, timeout=5.0):
            cleanup_called.append(True)
            await original_cleanup(self, timeout=timeout)

        with patch.object(AgentHypervisor, "cleanup", tracking_cleanup):
            with patch.object(ToolUseLoop, "run", _simple_loop_run):
                orch.run(user_query="do task", session_id=session_id, max_iters=1)

        assert len(cleanup_called) >= 1


# ---------------------------------------------------------------------------
# Orchestrator: hook fires session_end on both success and failure
# ---------------------------------------------------------------------------


class TestSessionEndHook:
    def test_session_end_fires_on_success(self, tmp_path):
        """Line 482: run_on_session_end called with None error on success."""
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()

        end_args = []
        orch._hook_manager.run_on_session_end = lambda sid, err: end_args.append((sid, err))

        with patch.object(ToolUseLoop, "run", _simple_loop_run):
            orch.run(user_query="go", session_id=session_id, max_iters=1)

        assert end_args
        sid, err = end_args[-1]
        assert sid == session_id
        assert err is None

    def test_session_end_fires_with_error_message_on_failure(self, tmp_path):
        """Line 482: error_msg forwarded to run_on_session_end on exception."""
        orch, store = _make_orchestrator(tmp_path)
        session_id = store.create_session()

        end_args = []
        orch._hook_manager.run_on_session_end = lambda sid, err: end_args.append((sid, err))

        async def failing_run(*args, **kwargs):
            raise RuntimeError("boom!")

        with patch.object(ToolUseLoop, "run", failing_run):
            orch.run(user_query="go", session_id=session_id, max_iters=1)

        assert end_args
        sid, err = end_args[-1]
        assert sid == session_id
        assert "boom!" in (err or "")
