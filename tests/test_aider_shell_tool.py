"""Tests for the Aider shell tool integration."""

from __future__ import annotations

import time

from mewbo_core.classes import ActionStep
from mewbo_tools.integration import aider_shell_tool
from mewbo_tools.integration.aider_shell_tool import AiderShellTool


def test_shell_tool_runs_command(monkeypatch, tmp_path):
    """Execute a shell command and return payload."""

    def _fake_run_cmd(command, cwd):
        assert command == "echo hello"
        assert cwd == str(tmp_path)
        return 0, "hello\n"

    monkeypatch.setattr(aider_shell_tool, "_run_command", _fake_run_cmd)

    tool = AiderShellTool()
    step = ActionStep(
        tool_id="aider_shell_tool",
        operation="set",
        tool_input={"command": "echo hello", "root": str(tmp_path)},
    )
    result = tool.set_state(step)
    payload = result.content
    assert isinstance(payload, dict)
    assert payload.get("kind") == "shell"
    assert payload.get("exit_code") == 0
    assert payload.get("stdout") == "hello\n"


def test_shell_tool_resolves_cwd(monkeypatch, tmp_path):
    """Resolve cwd within the project root."""

    def _fake_run_cmd(command, cwd):
        assert cwd == str(tmp_path / "subdir")
        return 0, "ok"

    monkeypatch.setattr(aider_shell_tool, "_run_command", _fake_run_cmd)
    (tmp_path / "subdir").mkdir()

    tool = AiderShellTool()
    step = ActionStep(
        tool_id="aider_shell_tool",
        operation="set",
        tool_input={"command": "pwd", "root": str(tmp_path), "cwd": "subdir"},
    )
    result = tool.set_state(step)
    payload = result.content
    assert isinstance(payload, dict)
    assert payload.get("cwd") == str(tmp_path / "subdir")


def test_shell_tool_blocks_escape(tmp_path):
    """Reject cwd that escapes the root."""
    tool = AiderShellTool()
    step = ActionStep(
        tool_id="aider_shell_tool",
        operation="set",
        tool_input={"command": "pwd", "root": str(tmp_path), "cwd": "../"},
    )
    result = tool.set_state(step)
    assert isinstance(result.content, str)
    assert "resolves outside all allowed project roots" in result.content


def test_shell_tool_requires_command(tmp_path):
    """Reject missing command input."""
    tool = AiderShellTool()
    step = ActionStep(
        tool_id="aider_shell_tool",
        operation="set",
        tool_input={"command": "", "root": str(tmp_path)},
    )
    result = tool.set_state(step)
    assert isinstance(result.content, str)
    assert "command is required" in result.content


def test_shell_tool_rejects_invalid_payload_type():
    """Reject invalid tool input types."""
    tool = AiderShellTool()
    step = ActionStep.model_construct(
        tool_id="aider_shell_tool",
        operation="set",
        tool_input=123,
    )
    result = tool.set_state(step)
    assert isinstance(result.content, str)
    assert "Tool input must be a string command" in result.content


def test_run_command_valid_cwd(tmp_path):
    """_run_command succeeds with a valid cwd."""
    from mewbo_tools.integration.aider_shell_tool import _run_command

    exit_code, output = _run_command("echo hello", str(tmp_path))
    assert exit_code == 0
    assert "hello" in output


def test_run_command_nonexistent_cwd(tmp_path):
    """_run_command returns error tuple for nonexistent cwd, not exception."""
    from mewbo_tools.integration.aider_shell_tool import _run_command

    exit_code, output = _run_command("echo hello", str(tmp_path / "does_not_exist"))
    assert exit_code != 0
    assert "does_not_exist" in output or "No such file" in output


def test_run_command_closes_stdin(tmp_path):
    """A command that reads stdin (e.g. a pager) never blocks — stdin is closed."""
    from mewbo_tools.integration.aider_shell_tool import _run_command

    exit_code, output = _run_command("cat", str(tmp_path))
    assert exit_code == 0
    assert output == ""


def test_run_command_pager_safe_env(tmp_path):
    """GIT_PAGER (and friends) are forced to non-interactive values."""
    from mewbo_tools.integration.aider_shell_tool import _run_command

    exit_code, output = _run_command('printf "%s" "$GIT_PAGER"', str(tmp_path))
    assert exit_code == 0
    assert output == "cat"


def test_run_command_timeout_reaps_process_group(tmp_path):
    """A hung command is killed on timeout, and its process group dies with it."""
    from mewbo_tools.integration.aider_shell_tool import _run_command

    marker = tmp_path / "reached.txt"
    started = time.monotonic()
    exit_code, output = _run_command(
        f"sleep 60 && touch {marker}",
        str(tmp_path),
        timeout=0.5,
    )
    elapsed = time.monotonic() - started

    assert exit_code == 1
    assert "timed out" in output
    assert elapsed < 10  # returned promptly, not after the full sleep
    # Give a killed-but-not-yet-reaped descendant a moment, then confirm it
    # never ran to completion (would prove it survived as an orphan).
    time.sleep(1)
    assert not marker.exists()


def test_run_command_default_timeout_under_core_tool_timeout():
    """The tool's own timeout stays under ToolUseLoop's default tool timeout.

    If this ever inverted, the outer asyncio.wait_for would cancel the
    awaiting task before the subprocess had a chance to reap itself.
    """
    from mewbo_core.tool_registry import ToolSpec
    from mewbo_tools.integration.aider_shell_tool import _DEFAULT_TIMEOUT_S

    assert _DEFAULT_TIMEOUT_S < ToolSpec.timeout
