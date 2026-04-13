"""Tests for the hook manager system."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from meeseeks_core.classes import ActionStep
from meeseeks_core.common import MockSpeaker
from meeseeks_core.config import HookEntry, HooksConfig
from meeseeks_core.hooks import (
    HookManager,
    _hook_env,
    _make_command_hook,
    _make_post_tool_hook,
    _matches,
)
from meeseeks_core.permissions import PermissionDecision


def _step(tool_id: str = "test_tool") -> ActionStep:
    return ActionStep(tool_id=tool_id, operation="get", tool_input="")


# -- Error isolation --------------------------------------------------------


class TestHookErrorIsolation:
    """A failing hook must not crash execution."""

    def test_pre_tool_use(self):
        mgr = HookManager(pre_tool_use=[lambda s: (_ for _ in ()).throw(RuntimeError)])
        result = mgr.run_pre_tool_use(_step())
        assert result.tool_id == "test_tool"

    def test_post_tool_use(self):
        mgr = HookManager(
            post_tool_use=[lambda s, r: (_ for _ in ()).throw(ValueError)]
        )
        result = mgr.run_post_tool_use(_step(), MockSpeaker(content="ok"))
        assert result.content == "ok"

    def test_permission_request(self):
        def bad(s, d):
            raise TypeError("broken")

        mgr = HookManager(permission_request=[bad])
        d = mgr.run_permission_request(_step(), PermissionDecision.ASK)
        assert d == PermissionDecision.ASK

    def test_pre_compact(self):
        mgr = HookManager(pre_compact=[lambda e: (_ for _ in ()).throw(Exception)])
        events = [{"type": "user", "payload": {"text": "hi"}}]
        assert mgr.run_pre_compact(events) == events

    def test_on_agent_start(self):
        mgr = HookManager(on_agent_start=[lambda h: (_ for _ in ()).throw(Exception)])
        mgr.run_on_agent_start(MagicMock())  # must not raise

    def test_on_agent_stop(self):
        mgr = HookManager(on_agent_stop=[lambda h: (_ for _ in ()).throw(Exception)])
        mgr.run_on_agent_stop(MagicMock())

    def test_on_session_start(self):
        def bad(sid):
            raise Exception("fail")

        mgr = HookManager(on_session_start=[bad])
        mgr.run_on_session_start("s1")

    def test_on_session_end(self):
        def bad(sid, err):
            raise Exception("fail")

        mgr = HookManager(on_session_end=[bad])
        mgr.run_on_session_end("s1", None)

    def test_on_compact(self):
        def bad(session_id: str) -> None:
            raise Exception("fail")

        mgr = HookManager(on_compact=[bad])
        mgr.run_on_compact("test-session")


# -- Chaining ---------------------------------------------------------------


class TestHookChaining:
    def test_pre_tool_chains(self):
        def h1(s):
            return ActionStep(
                tool_id=s.tool_id + "_1", operation=s.operation, tool_input=s.tool_input,
            )

        def h2(s):
            return ActionStep(
                tool_id=s.tool_id + "_2", operation=s.operation, tool_input=s.tool_input,
            )

        mgr = HookManager(pre_tool_use=[h1, h2])
        assert mgr.run_pre_tool_use(_step("x")).tool_id == "x_1_2"

    def test_post_tool_chains(self):
        def h(s, r):
            return MockSpeaker(content=r.content + "+")

        mgr = HookManager(post_tool_use=[h])
        assert mgr.run_post_tool_use(_step(), MockSpeaker(content="a")).content == "a+"


# -- Session lifecycle hooks ------------------------------------------------


class TestSessionLifecycleHooks:
    def test_session_start_fires(self):
        cb = MagicMock()
        HookManager(on_session_start=[cb]).run_on_session_start("s1")
        cb.assert_called_once_with("s1")

    def test_session_end_fires_with_error(self):
        cb = MagicMock()
        HookManager(on_session_end=[cb]).run_on_session_end("s1", "err")
        cb.assert_called_once_with("s1", "err")

    def test_session_end_fires_with_none(self):
        cb = MagicMock()
        HookManager(on_session_end=[cb]).run_on_session_end("s1", None)
        cb.assert_called_once_with("s1", None)

    def test_on_compact_fires(self):
        cb = MagicMock()
        HookManager(on_compact=[cb]).run_on_compact("result")
        cb.assert_called_once_with("result")


# -- Matcher ----------------------------------------------------------------


class TestMatcher:
    def test_none_matches_all(self):
        assert _matches(None, "anything") is True

    def test_exact(self):
        assert _matches("my_tool", "my_tool") is True
        assert _matches("my_tool", "other") is False

    def test_wildcard(self):
        assert _matches("aider_*", "aider_edit") is True
        assert _matches("aider_*", "shell") is False

    def test_question_mark(self):
        assert _matches("tool_?", "tool_a") is True
        assert _matches("tool_?", "tool_ab") is False


# -- Config loading ---------------------------------------------------------


class TestLoadFromConfig:
    def test_empty_config(self):
        mgr = HookManager.load_from_config(HooksConfig())
        assert mgr.pre_tool_use == []
        assert mgr.on_session_start == []

    def test_pre_tool_loaded(self):
        cfg = HooksConfig(pre_tool_use=[HookEntry(command="echo hi")])
        mgr = HookManager.load_from_config(cfg)
        assert len(mgr.pre_tool_use) == 1

    def test_post_tool_loaded(self):
        cfg = HooksConfig(post_tool_use=[HookEntry(command="echo done")])
        assert len(HookManager.load_from_config(cfg).post_tool_use) == 1

    def test_session_hooks_loaded(self):
        cfg = HooksConfig(
            on_session_start=[HookEntry(command="echo s")],
            on_session_end=[HookEntry(command="echo e")],
        )
        mgr = HookManager.load_from_config(cfg)
        assert len(mgr.on_session_start) == 1
        assert len(mgr.on_session_end) == 1

    def test_matcher_skips_non_matching(self):
        cfg = HooksConfig(pre_tool_use=[HookEntry(command="echo x", matcher="shell_*")])
        mgr = HookManager.load_from_config(cfg)
        result = mgr.run_pre_tool_use(_step("read_file"))
        assert result.tool_id == "read_file"  # unchanged — hook didn't match


class TestHookEntryDefaults:
    def test_defaults(self):
        e = HookEntry()
        assert e.type == "command"
        assert e.command == ""
        assert e.matcher is None
        assert e.timeout == 30

    def test_hooks_config_defaults(self):
        c = HooksConfig()
        assert c.pre_tool_use == []
        assert c.on_session_start == []


# -- Hook env var passing ------------------------------------------------------


class TestHookEnvVars:
    """Tests for _hook_env() returning correct env vars."""

    def test_hook_env_includes_tool_id(self):
        step = _step("shell_run")
        env = _hook_env(step)
        assert env["MEESEEKS_TOOL_ID"] == "shell_run"

    def test_hook_env_includes_operation(self):
        step = ActionStep(tool_id="read_file", operation="get", tool_input="")
        env = _hook_env(step)
        assert env["MEESEEKS_OPERATION"] == "get"

    def test_hook_env_includes_result_content(self):
        step = _step("tool_a")
        env = _hook_env(step, result_content="some output")
        assert env["MEESEEKS_TOOL_RESULT"] == "some output"

    def test_hook_env_truncates_long_result(self):
        step = _step("tool_b")
        long_content = "x" * 5000
        env = _hook_env(step, result_content=long_content)
        assert len(env["MEESEEKS_TOOL_RESULT"]) == 2000

    def test_hook_env_no_result_key_when_none(self):
        step = _step("tool_c")
        env = _hook_env(step)
        assert "MEESEEKS_TOOL_RESULT" not in env

    def test_hook_env_inherits_os_environ(self, monkeypatch):
        monkeypatch.setenv("MY_CUSTOM_VAR", "hello")
        step = _step("tool_d")
        env = _hook_env(step)
        assert env["MY_CUSTOM_VAR"] == "hello"


class TestCommandHookPassesEnv:
    """Verify subprocess.run gets the correct env vars from hooks."""

    def test_pre_tool_hook_passes_env(self):
        entry = HookEntry(command="echo test", matcher=None, timeout=5)
        hook = _make_command_hook(entry)
        step = ActionStep(tool_id="my_tool", operation="set", tool_input="data")
        with patch("meeseeks_core.hooks.subprocess.run") as mock_run:
            hook(step)
            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args
            passed_env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert passed_env["MEESEEKS_TOOL_ID"] == "my_tool"
            assert passed_env["MEESEEKS_OPERATION"] == "set"

    def test_post_tool_hook_passes_result_env(self):
        entry = HookEntry(command="echo done", matcher=None, timeout=5)
        hook = _make_post_tool_hook(entry)
        step = _step("shell_tool")
        result = MockSpeaker(content="output data")
        with patch("meeseeks_core.hooks.subprocess.run") as mock_run:
            hook(step, result)
            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args
            passed_env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert passed_env["MEESEEKS_TOOL_ID"] == "shell_tool"
            assert passed_env["MEESEEKS_TOOL_RESULT"] == "output data"
