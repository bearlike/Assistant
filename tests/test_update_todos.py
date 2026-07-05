#!/usr/bin/env python3
"""Tests for the authoritative ``update_todos`` SessionTool + ``todos`` contract.

Real code path: a real :class:`ActionStep` drives the real
:class:`UpdateTodosTool.handle`; only the event sink (I/O boundary) is a stub.
"""

from __future__ import annotations

import asyncio

from mewbo_core.classes import ActionStep
from mewbo_core.update_todos import (
    TODO_COMPLETED,
    TODO_IN_PROGRESS,
    TODO_PENDING,
    TODO_SOURCE_AGENT,
    TODO_SOURCE_PLAN,
    UPDATE_TODOS_SCHEMA,
    UpdateTodosTool,
    build_todos_event,
    normalize_todos,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# normalize_todos — coercion contract
# ---------------------------------------------------------------------------


def test_normalize_strips_labels_and_defaults_status() -> None:
    items = normalize_todos([{"label": "  do X  "}, {"label": "Y", "status": "completed"}])
    assert items == [
        {"label": "do X", "status": TODO_PENDING},
        {"label": "Y", "status": TODO_COMPLETED},
    ]


def test_normalize_enforces_exactly_one_in_progress() -> None:
    items = normalize_todos(
        [
            {"label": "a", "status": "in_progress"},
            {"label": "b", "status": "in_progress"},  # demoted to pending
            {"label": "c", "status": "in_progress"},  # demoted to pending
        ]
    )
    assert [it["status"] for it in items] == [TODO_IN_PROGRESS, TODO_PENDING, TODO_PENDING]


def test_normalize_drops_junk_and_unknown_status() -> None:
    items = normalize_todos(
        [
            {"label": "", "status": "completed"},  # empty label dropped
            "not-a-dict",  # dropped
            {"label": "keep", "status": "bogus"},  # unknown status -> pending
            {"nope": 1},  # no label -> dropped
        ]
    )
    assert items == [{"label": "keep", "status": TODO_PENDING}]


def test_normalize_non_list_is_empty() -> None:
    assert normalize_todos(None) == []
    assert normalize_todos("todos") == []
    assert normalize_todos({"label": "x"}) == []


# ---------------------------------------------------------------------------
# build_todos_event — the ONE shared contract
# ---------------------------------------------------------------------------


def test_build_event_shape_and_source() -> None:
    ev = build_todos_event(
        [{"label": "step", "status": "in_progress"}], source=TODO_SOURCE_PLAN, agent_id="root1"
    )
    assert ev["type"] == "todos"
    payload = ev["payload"]
    assert payload["source"] == TODO_SOURCE_PLAN
    assert payload["agent_id"] == "root1"
    assert payload["items"] == [{"label": "step", "status": TODO_IN_PROGRESS}]


def test_build_event_unknown_source_degrades_to_agent() -> None:
    ev = build_todos_event([], source="weird", agent_id=None)
    assert ev["payload"]["source"] == TODO_SOURCE_AGENT
    assert ev["payload"]["agent_id"] is None


# ---------------------------------------------------------------------------
# UpdateTodosTool — the SessionTool
# ---------------------------------------------------------------------------


def _tool_step(todos: object) -> ActionStep:
    return ActionStep(tool_id="update_todos", operation="set", tool_input={"todos": todos})


def test_schema_is_the_update_todos_function() -> None:
    fn = UPDATE_TODOS_SCHEMA["function"]
    assert fn["name"] == "update_todos"
    assert "todos" in fn["parameters"]["required"]


def test_tool_is_terminal_free() -> None:
    tool = UpdateTodosTool(session_id="s1")
    assert tool.should_terminate_run() is False
    # Idempotent — never latches a termination flag.
    assert tool.should_terminate_run() is False


def test_tool_emits_one_agent_sourced_event_with_agent_id() -> None:
    captured: list[dict] = []
    tool = UpdateTodosTool(session_id="s1", event_logger=captured.append, agent_id="rootA")
    step = _tool_step(
        [
            {"label": "read spec", "status": "completed"},
            {"label": "write code", "status": "in_progress"},
            {"label": "run tests", "status": "pending"},
        ]
    )
    result = _run(tool.handle(step))

    assert len(captured) == 1
    ev = captured[0]
    assert ev["type"] == "todos"
    assert ev["payload"]["source"] == TODO_SOURCE_AGENT
    assert ev["payload"]["agent_id"] == "rootA"
    labels = [it["label"] for it in ev["payload"]["items"]]
    assert labels == ["read spec", "write code", "run tests"]
    # The tool-result content is a concise human confirmation, not the raw list.
    assert "3 todo" in result.content
    assert "write code" in result.content


def test_tool_tolerates_missing_or_bad_input() -> None:
    captured: list[dict] = []
    tool = UpdateTodosTool(session_id="s1", event_logger=captured.append, agent_id="r")
    # No "todos" key at all — emits an empty authoritative list, never raises.
    result = _run(tool.handle(ActionStep(tool_id="update_todos", operation="set", tool_input={})))
    assert captured[-1]["payload"]["items"] == []
    assert "0 todo" in result.content


def test_tool_emit_failure_is_swallowed() -> None:
    def _boom(_ev: dict) -> None:
        raise RuntimeError("bus down")

    tool = UpdateTodosTool(session_id="s1", event_logger=_boom, agent_id="r")
    # A broken sink must never break the step.
    result = _run(tool.handle(_tool_step([{"label": "x", "status": "pending"}])))
    assert "1 todo" in result.content
