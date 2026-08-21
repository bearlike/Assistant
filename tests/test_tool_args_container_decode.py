"""The ONE core decoder for JSON strings standing in for declared containers.

The wiki plugin proved this repair in production (every stringified
`wiki_emit_answer.blocks` recovered) while `present_ui` — with no repair —
lost every stringified `root`. The law now lives DOWN in
``mewbo_core.tooling.container_args`` with one home; the wiki base delegates
to it and the cases here drive the seams that gained it: the decoder itself,
its valid-prefix salvage, and the container-argued core SessionTools
(`present_ui`, `ask_user_question`, `update_todos`).

Payload shapes mirror what real models actually sent, recorded on this
deployment's own transcripts — a fixture written from the schema author's
intuition is exactly the fixture that cannot reproduce this failure.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from mewbo_core.classes import ActionStep
from mewbo_core.tooling.container_args import JsonContainerArguments
from pydantic import BaseModel, ConfigDict, Field


class _ListArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[dict[str, Any]] = Field(min_length=1)


class _OptionalListArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    types: list[str] | None = None


_ITEMS = [{"kind": "p", "text": "one"}, {"kind": "p", "text": "two"}]


# ── The decoder itself ────────────────────────────────────────────────────────


def test_a_declared_list_sent_as_a_json_string_is_decoded() -> None:
    decoded = JsonContainerArguments.decode(
        _ListArgs, {"items": "\n" + json.dumps(_ITEMS) + "\n"}
    )
    assert decoded.arguments == {"items": _ITEMS}
    assert decoded.notes == ()


def test_a_correct_payload_is_returned_byte_identical() -> None:
    raw = {"items": _ITEMS}
    decoded = JsonContainerArguments.decode(_ListArgs, raw)
    assert decoded.arguments is raw


def test_an_optional_list_behind_a_union_is_decoded() -> None:
    decoded = JsonContainerArguments.decode(
        _OptionalListArgs, {"query": "x", "types": '["Function", "Class"]'}
    )
    assert decoded.arguments["types"] == ["Function", "Class"]


def test_a_single_key_wrapper_naming_the_field_is_unwrapped() -> None:
    decoded = JsonContainerArguments.decode(
        _ListArgs, {"items": json.dumps({"items": _ITEMS})}
    )
    assert decoded.arguments == {"items": _ITEMS}


@pytest.mark.parametrize(
    "value",
    ["not json at all", '"a bare json string"', "42", '{"unrelated": [1, 2]}'],
)
def test_a_string_that_is_not_the_declared_container_is_left_alone(value: str) -> None:
    """A real mistake must surface as ITSELF, never laundered."""
    decoded = JsonContainerArguments.decode(_ListArgs, {"items": value})
    assert decoded.arguments == {"items": value}


def test_a_scalar_field_given_a_json_string_is_left_alone() -> None:
    decoded = JsonContainerArguments.decode(
        _OptionalListArgs, {"query": '["still", "a", "string"]'}
    )
    assert decoded.arguments["query"] == '["still", "a", "string"]'


# ── Valid-prefix salvage — the measured 9-of-33 recovery ─────────────────────


def test_a_valid_prefix_with_trailing_garbage_is_salvaged_with_a_note() -> None:
    """The model lost escape-depth tracking mid-emission; the syntactically
    complete prefix is everything it emitted before the collapse."""
    tail = '  {"component": "Bad'  # the collapsed remainder, unparseable
    decoded = JsonContainerArguments.decode(
        _ListArgs, {"items": json.dumps(_ITEMS) + tail}
    )
    assert decoded.arguments == {"items": _ITEMS}
    assert len(decoded.notes) == 1
    note = decoded.notes[0]
    assert "items" in note
    assert "2 item(s)" in note
    assert f"{len(tail)} trailing character(s)" in note


def test_a_prefix_of_the_wrong_container_is_not_salvaged() -> None:
    decoded = JsonContainerArguments.decode(
        _ListArgs, {"items": '"just a string" and garbage'}
    )
    assert decoded.arguments == {"items": '"just a string" and garbage'}
    assert decoded.notes == ()


def test_totally_corrupt_json_is_not_salvaged() -> None:
    decoded = JsonContainerArguments.decode(
        _ListArgs, {"items": '[{"kind": "p", "text": "unbalanced'}
    )
    assert decoded.arguments == {"items": '[{"kind": "p", "text": "unbalanced'}
    assert decoded.notes == ()


# ── The wired call sites — each fails on the tree WITHOUT the decoder ────────


def _step(tool_id: str, tool_input: dict[str, Any]) -> ActionStep:
    return ActionStep(tool_id=tool_id, operation="set", tool_input=tool_input)


def test_present_ui_accepts_a_stringified_root() -> None:
    """The #1 measured rejection family on this tool: `root`, correctly shaped,
    as a JSON-encoded string. Zero of 33 such calls survived before."""
    from mewbo_core.builtin_plugins.generative_ui.present_ui import PresentUiTool

    events: list[dict[str, Any]] = []
    tool = PresentUiTool(session_id="s1", event_logger=events.append)
    root = [{"component": "Heading", "value": "Build status"}]
    result = asyncio.run(
        tool.handle(
            _step("present_ui", {"summary": "status", "root": json.dumps(root)})
        )
    )
    assert not result.content.startswith("ERROR"), result.content
    assert len(events) == 1
    assert events[0]["payload"]["spec"]["root"][0]["component"] == "Heading"


def test_present_ui_salvages_a_valid_prefix_and_reports_the_drop() -> None:
    from mewbo_core.builtin_plugins.generative_ui.present_ui import PresentUiTool

    events: list[dict[str, Any]] = []
    tool = PresentUiTool(session_id="s1", event_logger=events.append)
    root = [{"component": "Heading", "value": "Build"}]
    result = asyncio.run(
        tool.handle(
            _step(
                "present_ui",
                {"summary": "s", "root": json.dumps(root) + ' {"component": "Tab'},
            )
        )
    )
    assert len(events) == 1, result.content
    assert "dropped" in result.content
    assert "trailing character(s)" in result.content


def test_update_todos_accepts_a_stringified_list() -> None:
    from mewbo_core.tooling.update_todos import UpdateTodosTool

    events: list[dict[str, Any]] = []
    tool = UpdateTodosTool(session_id="s1", event_logger=events.append)
    todos = [{"label": "write tests", "status": "in_progress"}]
    result = asyncio.run(
        tool.handle(_step("update_todos", {"todos": json.dumps(todos)}))
    )
    assert "Recorded 1 todo(s)" in result.content
    assert events[0]["payload"]["items"] == [
        {"label": "write tests", "status": "in_progress"}
    ]


def test_ask_user_question_accepts_a_stringified_questions_list() -> None:
    """A recording dispatcher receives the VALIDATED args, which is the proof
    the stringified list decoded before validation. The process-wide dispatcher
    is restored afterwards — the API registers one at import time, and leaving
    a fake (or none) behind fails an unrelated suite under full-suite order."""
    from mewbo_core.tooling.ask_user import (
        AskUserQuestionArgs,
        AskUserQuestionTool,
        QuestionDispatcher,
    )

    received: list[AskUserQuestionArgs] = []

    class _Recorder:
        async def dispatch(self, session_id: str, args: AskUserQuestionArgs):
            received.append(args)
            raise RuntimeError("stop here — validation already proved the point")

    previous = QuestionDispatcher._impl
    QuestionDispatcher.register(_Recorder())
    try:
        tool = AskUserQuestionTool(session_id="s1")
        questions = [
            {
                "question": "Deploy now?",
                "header": "Deploy",
                "options": [{"label": "yes"}, {"label": "no"}],
            }
        ]
        with pytest.raises(RuntimeError):
            asyncio.run(
                tool.handle(
                    _step("ask_user_question", {"questions": json.dumps(questions)})
                )
            )
    finally:
        QuestionDispatcher.register(previous)
    assert len(received) == 1
    assert received[0].questions[0].question == "Deploy now?"
    assert [o.label for o in received[0].questions[0].options] == ["yes", "no"]
