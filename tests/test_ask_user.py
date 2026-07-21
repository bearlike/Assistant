#!/usr/bin/env python3
"""Unit tests for ``mewbo_core.ask_user`` — the ask-user-question contract.

Covers the Pydantic contract (question shape, answer IR, batch bounds), the
``QuestionDispatcher`` registration seam, and ``AskUserQuestionTool.handle``
with no / a fake registered dispatcher — including the outcome renderings
and the loop's error-envelope reclassification for the failure paths.
"""

from __future__ import annotations

import asyncio

import pytest
from mewbo_core.ask_user import (
    ASK_USER_QUESTION_TOOL_ID,
    AskUserQuestionArgs,
    AskUserQuestionTool,
    QuestionAnswerItem,
    QuestionDispatcher,
    QuestionDispatchResult,
    UserQuestion,
)
from mewbo_core.classes import ActionStep
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def _reset_dispatcher():
    """Test isolation: snapshot/restore the process-wide dispatcher seam.

    Same save/restore rationale as ``test_client_tools.py`` — a bare
    ``reset()`` would blank a registration another suite module (api startup
    wiring) already made, leaking into every test collected afterward.
    """
    previous = QuestionDispatcher._impl
    QuestionDispatcher.reset()
    yield
    QuestionDispatcher._impl = previous


def _args(**overrides) -> AskUserQuestionArgs:
    fields = {
        "questions": [
            {
                "header": "Scope",
                "question": "Which scope should this apply to?",
                "options": [
                    {"label": "Root only", "description": "Just the root agent."},
                    {"label": "All agents"},
                ],
            }
        ]
    }
    fields.update(overrides)
    return AskUserQuestionArgs.model_validate(fields)


class TestQuestionContract:
    def test_valid_args_round_trip(self):
        args = _args()
        q = args.questions[0]
        assert q.header == "Scope"
        assert q.kind == "single_select"
        assert q.options[1].description is None

    def test_kind_derivation(self):
        free = UserQuestion(header="H", question="Q?")
        single = _args().questions[0]
        multi = UserQuestion(
            header="H",
            question="Q?",
            options=[{"label": "A"}, {"label": "B"}],
            multi_select=True,
        )
        assert free.kind == "free_text"
        assert single.kind == "single_select"
        assert multi.kind == "multi_select"

    @pytest.mark.parametrize("count", [1, 5])
    def test_rejects_bad_option_counts(self, count):
        with pytest.raises(ValidationError):
            UserQuestion(
                header="H",
                question="Q?",
                options=[{"label": f"O{i}"} for i in range(count)],
            )

    def test_rejects_multi_select_without_options(self):
        with pytest.raises(ValidationError):
            UserQuestion(header="H", question="Q?", multi_select=True)

    def test_rejects_blank_and_oversized_headers(self):
        with pytest.raises(ValidationError):
            UserQuestion(header="   ", question="Q?")
        with pytest.raises(ValidationError):
            UserQuestion(header="H" * 49, question="Q?")

    def test_rejects_blank_question_and_blank_option_label(self):
        with pytest.raises(ValidationError):
            UserQuestion(header="H", question="  ")
        with pytest.raises(ValidationError):
            UserQuestion(header="H", question="Q?", options=[{"label": " "}, {"label": "B"}])

    @pytest.mark.parametrize("count", [0, 5])
    def test_rejects_bad_question_counts(self, count):
        with pytest.raises(ValidationError):
            AskUserQuestionArgs(
                questions=[
                    UserQuestion(header=f"H{i}", question="Q?") for i in range(count)
                ]
            )

    def test_extra_fields_forbidden_everywhere(self):
        with pytest.raises(ValidationError):
            AskUserQuestionArgs.model_validate({"questions": [], "extra": 1})
        with pytest.raises(ValidationError):
            UserQuestion.model_validate({"header": "H", "question": "Q?", "x": 1})
        with pytest.raises(ValidationError):
            QuestionAnswerItem.model_validate({"text": "t", "x": 1})


class TestAnswerItem:
    def test_indexes_xor_text(self):
        with pytest.raises(ValidationError):
            QuestionAnswerItem(selected_indexes=[0], text="both")
        with pytest.raises(ValidationError):
            QuestionAnswerItem()
        with pytest.raises(ValidationError):
            QuestionAnswerItem(text="   ")
        with pytest.raises(ValidationError):
            QuestionAnswerItem(selected_indexes=[])

    def test_rejects_negative_and_duplicate_indexes(self):
        with pytest.raises(ValidationError):
            QuestionAnswerItem(selected_indexes=[-1])
        with pytest.raises(ValidationError):
            QuestionAnswerItem(selected_indexes=[0, 0])


class TestAnswerResolution:
    def test_single_select_renders_label(self):
        args = _args()
        assert (
            args.render_answers([QuestionAnswerItem(selected_indexes=[1])])
            == "Scope: All agents"
        )

    def test_free_text_always_accepted_even_with_options(self):
        args = _args()
        assert (
            args.render_answers([QuestionAnswerItem(text="neither, actually")])
            == "Scope: neither, actually"
        )

    def test_multi_select_joins_labels(self):
        args = AskUserQuestionArgs(
            questions=[
                UserQuestion(
                    header="Features",
                    question="Which features?",
                    options=[{"label": "A"}, {"label": "B"}, {"label": "C"}],
                    multi_select=True,
                )
            ]
        )
        rendered = args.render_answers([QuestionAnswerItem(selected_indexes=[0, 2])])
        assert rendered == "Features: A, C"

    def test_count_mismatch_raises(self):
        with pytest.raises(ValueError, match="expected 1 answer"):
            _args().render_answers([])

    def test_indexes_on_free_text_question_raise(self):
        args = AskUserQuestionArgs(
            questions=[UserQuestion(header="H", question="Q?")]
        )
        with pytest.raises(ValueError, match="free-text"):
            args.render_answers([QuestionAnswerItem(selected_indexes=[0])])

    def test_out_of_bounds_index_raises(self):
        with pytest.raises(ValueError, match="2 options"):
            _args().render_answers([QuestionAnswerItem(selected_indexes=[2])])

    def test_single_select_arity_enforced(self):
        with pytest.raises(ValueError, match="exactly one"):
            _args().render_answers([QuestionAnswerItem(selected_indexes=[0, 1])])


class TestDispatcherSeam:
    def test_unregistered_by_default(self):
        assert QuestionDispatcher.available() is False

    def test_register_reset_cycle(self):
        class _Fake:
            async def dispatch(self, session_id, args):
                return QuestionDispatchResult(outcome="declined")

        QuestionDispatcher.register(_Fake())
        assert QuestionDispatcher.available() is True
        QuestionDispatcher.reset()
        assert QuestionDispatcher.available() is False

    def test_dispatch_with_no_impl_returns_none(self):
        assert asyncio.run(QuestionDispatcher.dispatch("s1", _args())) is None


def _step(tool_input) -> ActionStep:
    return ActionStep(
        tool_id=ASK_USER_QUESTION_TOOL_ID, operation="execute", tool_input=tool_input
    )


class TestAskUserQuestionTool:
    def test_shape_and_modes(self):
        tool = AskUserQuestionTool("sess-1")
        assert tool.tool_id == ASK_USER_QUESTION_TOOL_ID
        assert tool.schema["function"]["name"] == ASK_USER_QUESTION_TOOL_ID
        assert tool.modes == frozenset({"plan", "act"})
        assert tool.should_terminate_run() is False
        # Structural-Protocol trap: terminal_reason must exist on the class.
        assert tool.terminal_reason() == "awaiting_approval"

    def test_handle_without_dispatcher_returns_unavailable_envelope(self):
        from mewbo_core.tool_use_loop import _session_tool_error_envelope

        tool = AskUserQuestionTool("sess-1")
        result = asyncio.run(tool.handle(_step({"questions": [
            {"header": "H", "question": "Q?"}
        ]})))
        assert "ask_user_unavailable" in result.content
        assert _session_tool_error_envelope(result) is not None

    def test_handle_invalid_args_returns_validation_envelope(self):
        from mewbo_core.tool_use_loop import _session_tool_error_envelope

        class _MustNotDispatch:
            async def dispatch(self, session_id, args):  # pragma: no cover
                raise AssertionError("dispatch must not run for invalid args")

        QuestionDispatcher.register(_MustNotDispatch())
        tool = AskUserQuestionTool("sess-1")
        result = asyncio.run(tool.handle(_step({"questions": []})))
        detected = _session_tool_error_envelope(result)
        assert detected is not None and detected.startswith("validation:")

    def test_handle_answered_returns_rendered_tool_result(self):
        calls = []

        class _Fake:
            async def dispatch(self, session_id, args):
                calls.append((session_id, args))
                return QuestionDispatchResult(
                    outcome="answered",
                    answers=(QuestionAnswerItem(selected_indexes=[0]),),
                    answered_via="console",
                )

        QuestionDispatcher.register(_Fake())
        tool = AskUserQuestionTool("sess-1")
        result = asyncio.run(
            tool.handle(
                _step(
                    {
                        "questions": [
                            {
                                "header": "Scope",
                                "question": "Which?",
                                "options": [{"label": "Root only"}, {"label": "All"}],
                            }
                        ]
                    }
                )
            )
        )
        assert result.content == "The user answered:\nScope: Root only"
        assert calls and calls[0][0] == "sess-1"
        assert isinstance(calls[0][1], AskUserQuestionArgs)

    @pytest.mark.parametrize(
        ("outcome", "needle"),
        [
            ("declined", "sent a new message instead"),
            ("interrupted", "interrupted the run"),
            ("cancelled", "cancelled before the user answered"),
        ],
    )
    def test_handle_non_answer_outcomes_render_plainly(self, outcome, needle):
        from mewbo_core.tool_use_loop import _session_tool_error_envelope

        class _Fake:
            async def dispatch(self, session_id, args):
                return QuestionDispatchResult(outcome=outcome)

        QuestionDispatcher.register(_Fake())
        tool = AskUserQuestionTool("sess-1")
        result = asyncio.run(tool.handle(_step({"questions": [
            {"header": "H", "question": "Q?"}
        ]})))
        assert needle in result.content
        # Legitimate outcomes are NOT failures — the envelope detector must
        # not reclassify them.
        assert _session_tool_error_envelope(result) is None
