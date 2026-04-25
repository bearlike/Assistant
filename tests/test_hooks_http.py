"""Tests for HTTP hook type (fire-and-forget POST to external URLs)."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from mewbo_core.classes import ActionStep
from mewbo_core.config import HookEntry, HooksConfig
from mewbo_core.hooks import HookManager


def _step(tool_id: str = "read_file", operation: str = "get") -> ActionStep:
    return ActionStep(tool_id=tool_id, operation=operation, tool_input={})


class TestHttpHookFactory:
    """Test _make_http_hook and related factories."""

    def test_pre_tool_http_hook_posts_json(self) -> None:
        config = HooksConfig(pre_tool_use=[HookEntry(type="http", url="http://example.com/hook")])
        manager = HookManager.load_from_config(config)
        assert len(manager.pre_tool_use) == 1

        with patch("mewbo_core.hooks._post_json") as mock_post:
            step = _step()
            result = manager.run_pre_tool_use(step)
            assert result is step
            # Give the daemon thread a moment to start
            time.sleep(0.1)
            mock_post.assert_called_once()
            args = mock_post.call_args[0]
            assert args[0] == "http://example.com/hook"
            payload = args[1]
            assert payload["event"] == "pre_tool_use"
            assert payload["tool_id"] == "read_file"
            assert payload["operation"] == "get"

    def test_pre_tool_http_hook_respects_matcher(self) -> None:
        config = HooksConfig(
            pre_tool_use=[HookEntry(type="http", url="http://example.com/hook", matcher="shell_*")]
        )
        manager = HookManager.load_from_config(config)

        with patch("mewbo_core.hooks._post_json") as mock_post:
            manager.run_pre_tool_use(_step("read_file"))
            time.sleep(0.05)
            mock_post.assert_not_called()

            manager.run_pre_tool_use(_step("shell_exec"))
            time.sleep(0.05)
            mock_post.assert_called_once()

    def test_post_tool_http_hook(self) -> None:
        config = HooksConfig(post_tool_use=[HookEntry(type="http", url="http://example.com/post")])
        manager = HookManager.load_from_config(config)

        mock_result = MagicMock()
        mock_result.content = "some output"

        with patch("mewbo_core.hooks._post_json") as mock_post:
            manager.run_post_tool_use(_step(), mock_result)
            time.sleep(0.1)
            mock_post.assert_called_once()
            payload = mock_post.call_args[0][1]
            assert payload["event"] == "post_tool_use"
            assert payload["result_preview"] == "some output"

    def test_session_start_http_hook(self) -> None:
        config = HooksConfig(
            on_session_start=[HookEntry(type="http", url="http://example.com/start")]
        )
        manager = HookManager.load_from_config(config)

        with patch("mewbo_core.hooks._post_json") as mock_post:
            manager.run_on_session_start("sess-123")
            time.sleep(0.1)
            mock_post.assert_called_once()
            payload = mock_post.call_args[0][1]
            assert payload["event"] == "session_start"
            assert payload["session_id"] == "sess-123"

    def test_session_end_http_hook_with_error(self) -> None:
        config = HooksConfig(on_session_end=[HookEntry(type="http", url="http://example.com/end")])
        manager = HookManager.load_from_config(config)

        with patch("mewbo_core.hooks._post_json") as mock_post:
            manager.run_on_session_end("sess-456", error="timeout")
            time.sleep(0.1)
            mock_post.assert_called_once()
            payload = mock_post.call_args[0][1]
            assert payload["event"] == "session_end"
            assert payload["session_id"] == "sess-456"
            assert payload["error"] == "timeout"

    def test_mixed_command_and_http_hooks(self) -> None:
        config = HooksConfig(
            pre_tool_use=[
                HookEntry(type="command", command="echo hello"),
                HookEntry(type="http", url="http://example.com/hook"),
            ]
        )
        manager = HookManager.load_from_config(config)
        assert len(manager.pre_tool_use) == 2


class TestSessionEnvEnrichment:
    """Test that session hooks pass env vars."""

    def test_session_start_passes_session_id(self) -> None:
        config = HooksConfig(
            on_session_start=[HookEntry(type="command", command="echo $MEWBO_SESSION_ID")]
        )
        manager = HookManager.load_from_config(config)

        with patch("subprocess.run") as mock_run:
            manager.run_on_session_start("test-session-id")
            mock_run.assert_called_once()
            env = mock_run.call_args[1]["env"]
            assert env["MEWBO_SESSION_ID"] == "test-session-id"

    def test_session_end_passes_session_id_and_error(self) -> None:
        config = HooksConfig(on_session_end=[HookEntry(type="command", command="echo test")])
        manager = HookManager.load_from_config(config)

        with patch("subprocess.run") as mock_run:
            manager.run_on_session_end("sess-789", error="failed")
            mock_run.assert_called_once()
            env = mock_run.call_args[1]["env"]
            assert env["MEWBO_SESSION_ID"] == "sess-789"
            assert env["MEWBO_ERROR"] == "failed"
