"""Ask-user question CLI surface — answer-mapping + dispatcher tests.

Covers the two load-bearing seams the interactive TUI adds for the
``ask_user_question`` tool, without a Textual pilot:

- ``collect_answers`` — the pure fold from per-question widget state to the
  ordered ``QuestionAnswerItem`` list the modal dismisses with.
- ``TuiQuestionDispatcher.dispatch`` — the outcome mapping (answered / declined
  / interrupted), driven with a fake App whose ``call_from_thread`` returns a
  canned modal dismiss value (DI makes this trivial — no real modal is mounted).
"""

from __future__ import annotations

import asyncio

from mewbo_cli.tui.question_dispatcher import TuiQuestionDispatcher
from mewbo_cli.tui.widgets.ask_user_modal import _QuestionInput, collect_answers
from mewbo_core.ask_user import AskUserQuestionArgs, QuestionAnswerItem


def _args(*questions: dict) -> AskUserQuestionArgs:
    return AskUserQuestionArgs.model_validate({"questions": list(questions)})


_FREE_TEXT = {"header": "Notes", "question": "Anything else?"}
_SINGLE = {
    "header": "Auth",
    "question": "Which auth method?",
    "options": [{"label": "OAuth"}, {"label": "API key"}],
}
_MULTI = {
    "header": "Scopes",
    "question": "Which scopes?",
    "options": [{"label": "read"}, {"label": "write"}, {"label": "admin"}],
    "multi_select": True,
}


# ---------------------------------------------------------------------------
# collect_answers — the modal's pure answer-mapping core
# ---------------------------------------------------------------------------


def test_collect_free_text_answer():
    """A free-text question maps its Other text to a text answer item."""
    args = _args(_FREE_TEXT)
    result = collect_answers(args.questions, [_QuestionInput(other_text="ship it")])
    assert result == [QuestionAnswerItem(text="ship it")]


def test_collect_single_select_answer():
    """A single-select choice maps to a one-element selected_indexes item."""
    args = _args(_SINGLE)
    result = collect_answers(args.questions, [_QuestionInput(selected_indexes=[1])])
    assert result == [QuestionAnswerItem(selected_indexes=[1])]


def test_collect_multi_select_answer_dedupes_and_sorts():
    """A multi-select maps its toggled values to sorted, unique indexes."""
    args = _args(_MULTI)
    result = collect_answers(args.questions, [_QuestionInput(selected_indexes=[2, 0, 0])])
    assert result == [QuestionAnswerItem(selected_indexes=[0, 2])]


def test_free_text_wins_over_selection():
    """The ever-present Other text supersedes a selection on the same question."""
    args = _args(_SINGLE)
    result = collect_answers(
        args.questions,
        [_QuestionInput(selected_indexes=[0], other_text="  something else  ")],
    )
    assert result == [QuestionAnswerItem(text="something else")]


def test_out_of_bounds_indexes_are_dropped_as_unanswered():
    """An index past the option count is not a valid answer → unanswered → None."""
    args = _args(_SINGLE)
    result = collect_answers(args.questions, [_QuestionInput(selected_indexes=[9])])
    assert result is None


def test_any_unanswered_question_yields_none():
    """One blank question keeps the whole group unanswered (modal stays open)."""
    args = _args(_SINGLE, _FREE_TEXT)
    result = collect_answers(
        args.questions,
        [_QuestionInput(selected_indexes=[0]), _QuestionInput()],
    )
    assert result is None


def test_collect_preserves_question_order():
    """Items come back one-per-question in the questions' order."""
    args = _args(_SINGLE, _MULTI, _FREE_TEXT)
    result = collect_answers(
        args.questions,
        [
            _QuestionInput(selected_indexes=[0]),
            _QuestionInput(selected_indexes=[1]),
            _QuestionInput(other_text="notes"),
        ],
    )
    assert result == [
        QuestionAnswerItem(selected_indexes=[0]),
        QuestionAnswerItem(selected_indexes=[1]),
        QuestionAnswerItem(text="notes"),
    ]


# ---------------------------------------------------------------------------
# TuiQuestionDispatcher — outcome mapping over a fake App
# ---------------------------------------------------------------------------


class _FakeApp:
    """Stand-in for the mounted ``MewboApp``.

    ``call_from_thread`` is the only method the dispatcher touches; it returns a
    canned modal dismiss value (``list[QuestionAnswerItem] | None``) instead of
    ever mounting a real modal — the DI seam that makes this unit-testable.
    """

    def __init__(self, dismiss_value, *, raises: bool = False) -> None:
        self._dismiss_value = dismiss_value
        self._raises = raises

    def push_screen_wait(self, modal):  # pragma: no cover - passed as an arg only
        raise AssertionError("push_screen_wait must be reached via call_from_thread")

    def call_from_thread(self, func, *args, **kwargs):
        if self._raises:
            raise RuntimeError("app loop is gone")
        return self._dismiss_value


def _dispatch(app_provider, args: AskUserQuestionArgs):
    dispatcher = TuiQuestionDispatcher(app_provider=app_provider)
    return asyncio.run(dispatcher.dispatch("session-1", args))


def test_dispatch_answered_maps_modal_items():
    """A returned item list → ``answered`` with the items + a ``cli`` chip."""
    args = _args(_SINGLE)
    items = [QuestionAnswerItem(selected_indexes=[0])]
    result = _dispatch(lambda: _FakeApp(items), args)
    assert result.outcome == "answered"
    assert result.answers == (QuestionAnswerItem(selected_indexes=[0]),)
    assert result.answered_via == "cli"
    # The rendered tool result reflects the chosen option label.
    assert "OAuth" in result.render(args)


def test_dispatch_declined_when_modal_dismissed_with_none():
    """``esc`` (None dismiss) → ``declined`` (the user answers by message)."""
    args = _args(_SINGLE)
    result = _dispatch(lambda: _FakeApp(None), args)
    assert result.outcome == "declined"
    assert result.answers == ()


def test_dispatch_interrupted_when_app_unavailable():
    """No mounted App → ``interrupted`` rather than wedging the run."""
    args = _args(_SINGLE)
    result = _dispatch(lambda: None, args)
    assert result.outcome == "interrupted"


def test_dispatch_interrupted_when_bridge_raises():
    """A failing modal bridge → ``interrupted`` (never propagates into the loop)."""
    args = _args(_SINGLE)
    result = _dispatch(lambda: _FakeApp(None, raises=True), args)
    assert result.outcome == "interrupted"
