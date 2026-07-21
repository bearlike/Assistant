#!/usr/bin/env python3
"""Tests for TurnEngine — the ported run_cli dispatch + _run_query.

TurnEngine is the App-agnostic core: it classifies a line (command / skill /
query), runs it against an injected ``SessionRuntime``, and emits transcript
items via injected callbacks. Only the I/O boundary (``orchestrate_session``,
the LLM-backed ``generate_action_plan``) is stubbed.
"""

from __future__ import annotations

from typing import Any

import pytest
from mewbo_cli.cli_context import CliState
from mewbo_cli.tui.seams import PermissionGateway, TranscriptItem
from mewbo_cli.tui.turn_engine import TurnEngine
from mewbo_core.classes import ActionStep, TaskQueue
from mewbo_core.session_runtime import SessionRuntime
from mewbo_core.session_store import SessionStore
from mewbo_core.tool_registry import ToolRegistry
from rich.console import Console

# --- fakes ---------------------------------------------------------------


class _FakeCommandRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def list_commands(self) -> list[str]:
        return ["/help", "/exit", "/new"]

    def execute(self, name: str, context: Any, args: list[str]) -> bool:
        self.calls.append((name, args))
        context.console.print(f"ran {name}")
        return name != "/exit"  # /exit ends the loop


class _FakeSkill:
    def __init__(self, name: str, user_invocable: bool = True) -> None:
        self.name = name
        self.user_invocable = user_invocable


class _FakeSkillRegistry:
    def __init__(self, skills: dict[str, _FakeSkill] | None = None) -> None:
        self._skills = skills or {}

    def get(self, name: str) -> _FakeSkill | None:
        return self._skills.get(name)


# --- harness -------------------------------------------------------------


class _Harness:
    def __init__(self, tmp_path: Any, *, show_plan: bool = False, auto: bool = True,
                 skills: dict[str, _FakeSkill] | None = None) -> None:
        self.store = SessionStore(root_dir=str(tmp_path))
        self.session_id = self.store.create_session()
        self.runtime = SessionRuntime(session_store=self.store)
        self.state = CliState(session_id=self.session_id, show_plan=show_plan)
        self.commands = _FakeCommandRegistry()
        self.skills = _FakeSkillRegistry(skills)
        self.permission = PermissionGateway(auto_approve=lambda: auto)
        self.items: list[TranscriptItem] = []
        self.renderables: list[Any] = []
        self.engine = TurnEngine(
            runtime=self.runtime,
            store=self.store,
            state=self.state,
            tool_registry=ToolRegistry(),
            command_registry=self.commands,
            skill_registry=self.skills,
            permission=self.permission,
            hook_factory=lambda: None,  # foundation: no hooks needed for these paths
            emit=self.items.append,
            emit_renderable=self.renderables.append,
        )

    def kinds(self) -> list[str]:
        return [i.kind for i in self.items]


def _stub_run(monkeypatch: pytest.MonkeyPatch, *, result: str = "ok",
              steps: list[ActionStep] | None = None) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_orchestrate(*args: Any, **kwargs: Any) -> TaskQueue:
        captured.update(kwargs)
        tq = TaskQueue(action_steps=steps or [])
        tq.task_result = result
        return tq

    monkeypatch.setattr("mewbo_core.session_runtime.orchestrate_session", fake_orchestrate)
    return captured


# --- classify ------------------------------------------------------------


def test_classify_empty(tmp_path: Any) -> None:
    assert _Harness(tmp_path).engine.classify("   ") == "empty"


def test_classify_command(tmp_path: Any) -> None:
    assert _Harness(tmp_path).engine.classify("/help") == "command"


def test_classify_skill(tmp_path: Any) -> None:
    h = _Harness(tmp_path, skills={"deep-research": _FakeSkill("deep-research")})
    assert h.engine.classify("/deep-research go") == "skill"


def test_classify_unknown_slash(tmp_path: Any) -> None:
    assert _Harness(tmp_path).engine.classify("/nope") == "unknown"


def test_classify_query(tmp_path: Any) -> None:
    assert _Harness(tmp_path).engine.classify("hello there") == "query"


# --- handle: query -------------------------------------------------------


def test_handle_query_emits_user_then_assistant(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path)
    _stub_run(monkeypatch, result="the answer")

    keep = h.engine.handle("what is 2+2?")

    assert keep is True
    assert h.kinds()[0] == "user"
    assert "assistant" in h.kinds()
    assistant = next(i for i in h.items if i.kind == "assistant")
    assert "the answer" in str(assistant.payload.get("text"))


# --- last_turn_outcome -----------------------------------------------------
#
# ``run_sync``'s return (``TaskQueue``) carries no ``done_reason``/
# ``blocked_code`` — those live on the session's ``completion`` event, so
# these tests stub ``orchestrate_session`` to append one directly (as the
# real orchestrator does) rather than reusing ``_stub_run``, which returns a
# bare ``TaskQueue`` with no store side effect.


def _stub_run_with_completion(
    monkeypatch: pytest.MonkeyPatch, h: _Harness, *, done_reason: str,
    blocked_code: str | None = None, result: str = "ok",
) -> None:
    payload: dict[str, Any] = {"done": True, "done_reason": done_reason}
    if blocked_code is not None:
        payload["blocked_code"] = blocked_code

    def fake_orchestrate(*args: Any, **kwargs: Any) -> TaskQueue:
        h.store.append_event(h.session_id, {"type": "completion", "payload": payload})
        tq = TaskQueue()
        tq.task_result = result
        return tq

    monkeypatch.setattr("mewbo_core.session_runtime.orchestrate_session", fake_orchestrate)


def test_last_turn_outcome_none_before_any_turn(tmp_path: Any) -> None:
    assert _Harness(tmp_path).engine.last_turn_outcome() is None


def test_last_turn_outcome_reflects_blocked_code(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``blocked_code`` completion (``done_reason`` stays ``"completed"``)
    reports ``"blocked"``, not the false-success ``"completed"``.
    """
    h = _Harness(tmp_path)
    _stub_run_with_completion(
        monkeypatch, h, done_reason="completed", blocked_code="repo_access"
    )
    h.engine.handle("do the thing")
    assert h.engine.last_turn_outcome() == "blocked"


def test_last_turn_outcome_reflects_unmet_goal(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path)
    _stub_run_with_completion(monkeypatch, h, done_reason="halted_no_progress")
    h.engine.handle("do the thing")
    assert h.engine.last_turn_outcome() == "unmet_goal"


def test_last_turn_outcome_resets_on_a_command_dispatch(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command turn after a blocked query must not inherit its outcome —
    otherwise ``/help`` right after a blocked run would settle the footer
    spinner as blocked too.
    """
    h = _Harness(tmp_path)
    _stub_run_with_completion(
        monkeypatch, h, done_reason="completed", blocked_code="repo_access"
    )
    h.engine.handle("do the thing")
    assert h.engine.last_turn_outcome() == "blocked"

    h.engine.handle("/help")
    assert h.engine.last_turn_outcome() is None


def test_handle_empty_is_noop(tmp_path: Any) -> None:
    h = _Harness(tmp_path)
    assert h.engine.handle("   ") is True
    assert h.items == []


# --- handle: commands ----------------------------------------------------


def test_handle_exit_command_returns_false(tmp_path: Any) -> None:
    h = _Harness(tmp_path)
    assert h.engine.handle("/exit") is False
    assert h.commands.calls == [("/exit", [])]


def test_handle_command_captures_console_output(tmp_path: Any) -> None:
    h = _Harness(tmp_path)
    keep = h.engine.handle("/help")
    assert keep is True
    assert h.commands.calls == [("/help", [])]
    # The command printed to its console; the engine forwarded the capture.
    joined = " ".join(str(getattr(r, "plain", r)) for r in h.renderables)
    assert "ran /help" in joined


def test_handle_unknown_command_emits_notice(tmp_path: Any) -> None:
    h = _Harness(tmp_path)
    assert h.engine.handle("/bogus") is True
    assert "notice" in h.kinds()
    notice = next(i for i in h.items if i.kind == "notice")
    assert "nknown" in str(notice.payload.get("text"))


# --- handle: skills ------------------------------------------------------


def test_handle_skill_runs_query_with_instructions(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path, skills={"deep-research": _FakeSkill("deep-research")})
    captured = _stub_run(monkeypatch)
    monkeypatch.setattr(
        "mewbo_cli.tui.turn_engine.activate_skill",
        lambda skill, args: ("SKILL INSTRUCTIONS", None),
    )

    keep = h.engine.handle("/deep-research find X")

    assert keep is True
    assert captured["skill_instructions"] == "SKILL INSTRUCTIONS"


# --- run_query ----------------------------------------------------------


def test_run_query_threads_permission_as_approval_callback(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path)
    captured = _stub_run(monkeypatch)
    h.engine.run_query("hi")
    assert captured["approval_callback"] is h.permission
    assert captured["source_platform"] == "cli"


def test_run_query_refuses_terminated_session(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A permanently terminated session never starts a run.

    ``SessionRuntime.run_sync`` itself doesn't guard this, so the engine must
    refuse before reaching it — asserted here by never stubbing
    ``orchestrate_session``: if the guard didn't fire, the real (unstubbed)
    orchestrator would raise instead of quietly succeeding.
    """
    h = _Harness(tmp_path)
    h.runtime.terminate_session(h.session_id)

    result = h.engine.run_query("hi")

    assert result is None
    assert h.kinds() == ["notice"]
    notice = h.items[0]
    assert "terminated" in str(notice.payload.get("text")).lower()


def test_run_query_emits_tool_items(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(tmp_path)
    step = ActionStep(tool_id="shell_tool", operation="run", tool_input="ls")
    _stub_run(monkeypatch, steps=[step])
    h.engine.run_query("do a thing")
    assert "tool" in h.kinds()
    tool = next(i for i in h.items if i.kind == "tool")
    assert tool.payload.get("tool_id") == "shell_tool"


# --- enriched tool payload ----------------------------------------------


def _tool_item(h: _Harness) -> TranscriptItem:
    return next(i for i in h.items if i.kind == "tool")


def test_tool_payload_bash_carries_command(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path)
    step = ActionStep(
        tool_id="bash", operation="execute", tool_input={"command": "ls -la /tmp"}
    )
    _stub_run(monkeypatch, steps=[step])
    h.engine.run_query("list files")
    tool = _tool_item(h)
    assert tool.payload.get("command") == "ls -la /tmp"
    # A bash step is not a file edit.
    assert "old_text" not in tool.payload


def test_tool_payload_bash_string_input_carries_command(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path)
    step = ActionStep(tool_id="shell", operation="execute", tool_input="echo hi")
    _stub_run(monkeypatch, steps=[step])
    h.engine.run_query("say hi")
    assert _tool_item(h).payload.get("command") == "echo hi"


def test_tool_payload_vendored_shell_id_carries_command(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vendored shell id (``aider_shell_tool``) still extracts its command."""
    h = _Harness(tmp_path)
    step = ActionStep(
        tool_id="aider_shell_tool", operation="set", tool_input={"command": "echo hi"}
    )
    _stub_run(monkeypatch, steps=[step])
    h.engine.run_query("say hi")
    assert _tool_item(h).payload.get("command") == "echo hi"


def test_tool_payload_edit_carries_path_and_diff(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path)
    step = ActionStep(
        tool_id="edit",
        operation="set",
        tool_input={
            "file_path": "/repo/foo.py",
            "old_string": "a = 1",
            "new_string": "a = 2",
        },
    )
    _stub_run(monkeypatch, steps=[step])
    h.engine.run_query("edit the file")
    payload = _tool_item(h).payload
    assert payload.get("file_path") == "/repo/foo.py"
    assert payload.get("old_text") == "a = 1"
    assert payload.get("new_text") == "a = 2"


def test_tool_payload_write_uses_content_as_new_text(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path)
    step = ActionStep(
        tool_id="write",
        operation="set",
        tool_input={"file_path": "/repo/new.txt", "content": "hello world"},
    )
    _stub_run(monkeypatch, steps=[step])
    h.engine.run_query("write a file")
    payload = _tool_item(h).payload
    assert payload.get("file_path") == "/repo/new.txt"
    assert payload.get("old_text") == ""
    assert payload.get("new_text") == "hello world"


def test_tool_payload_args_summary_skips_blobs(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path)
    step = ActionStep(
        tool_id="write",
        operation="set",
        tool_input={"file_path": "/repo/x.txt", "content": "x" * 5000},
    )
    _stub_run(monkeypatch, steps=[step])
    h.engine.run_query("write")
    summary = _tool_item(h).payload.get("args_summary")
    assert summary is not None
    # The large content blob is never inlined into the compact header.
    assert "xxxx" not in str(summary)
    assert "file_path=" in str(summary)


def test_run_query_omits_usage_footer_keeps_resilience(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path)
    _stub_run(monkeypatch, result="done")

    # Seed a user turn then an LLM fallback notice so the resilience replay has
    # an event after the last user turn to surface.
    real_run = h.engine.run_query

    def run_then_replay(query: str, **kwargs: Any) -> Any:
        h.store.append_event(h.session_id, {"type": "user", "payload": {"text": query}})
        h.store.append_event(
            h.session_id,
            {
                "type": "llm_fallback",
                "payload": {
                    "from_model": "openai/gpt-5",
                    "to_model": "openai/gpt-4",
                    "reason": "retries_exhausted",
                },
            },
        )
        return real_run(query, **kwargs)

    h.engine.run_query = run_then_replay  # type: ignore[method-assign]
    h.engine.run_query("do work")

    joined = " ".join(str(getattr(r, "plain", r)) for r in h.renderables)
    # The usage/context footer is gone from the transcript.
    assert "until compact" not in joined
    # …but the resilience fallback notice is still replayed.
    assert "Falling back" in joined


def test_run_query_show_plan_emits_plan(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(tmp_path, show_plan=True)
    _stub_run(monkeypatch)

    class _Plan:
        steps: list[Any] = []

    monkeypatch.setattr(
        "mewbo_cli.tui.turn_engine.generate_action_plan",
        lambda **kwargs: _Plan(),
    )
    h.engine.run_query("plan this")
    assert "plan" in h.kinds()


def test_console_factory_used_for_capture_is_recording(tmp_path: Any) -> None:
    # A custom console_factory (e.g. App-sized) is honoured for command capture.
    made: list[Console] = []

    def factory() -> Console:
        c = Console(record=True, width=80, color_system=None)
        made.append(c)
        return c

    h = _Harness(tmp_path)
    h.engine._console_factory = factory  # type: ignore[attr-defined]
    h.engine.handle("/help")
    assert made  # factory was consulted for the command console
