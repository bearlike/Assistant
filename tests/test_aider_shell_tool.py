"""Tests for the Aider shell tool integration."""

from __future__ import annotations

from truss_core.classes import ActionStep
from truss_tools.integration import aider_shell_tool
from truss_tools.integration.aider_shell_tool import AiderShellTool


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
    from truss_tools.integration.aider_shell_tool import _run_command

    exit_code, output = _run_command("echo hello", str(tmp_path))
    assert exit_code == 0
    assert "hello" in output


def test_run_command_nonexistent_cwd(tmp_path):
    """_run_command returns error tuple for nonexistent cwd, not exception."""
    from truss_tools.integration.aider_shell_tool import _run_command

    exit_code, output = _run_command("echo hello", str(tmp_path / "does_not_exist"))
    assert exit_code != 0
    assert "does_not_exist" in output or "No such file" in output
