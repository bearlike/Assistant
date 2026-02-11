#!/usr/bin/env python3
"""Shell execution helper adapted from Aider."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from meeseeks_core.classes import AbstractTool, ActionStep
from meeseeks_core.common import MockSpeaker, get_mock_speaker


@dataclass(frozen=True)
class ShellRequest:
    command: str
    cwd: str


def _resolve_cwd(root: str, cwd: str | None) -> str:
    root_path = Path(root).resolve()
    target = Path(cwd) if cwd else root_path
    if not target.is_absolute():
        target = root_path / target
    resolved = target.resolve()
    try:
        resolved.relative_to(root_path)
    except ValueError as exc:
        raise ValueError(f"CWD '{cwd}' resolves outside the project root.") from exc
    return str(resolved)


def _parse_shell_request(action_step: ActionStep | None) -> ShellRequest:
    if action_step is None:
        raise ValueError("Action step is required.")
    argument = action_step.tool_input
    if isinstance(argument, str):
        command = argument.strip()
        if not command:
            raise ValueError("command is required.")
        return ShellRequest(command=command, cwd=os.getcwd())
    if isinstance(argument, dict):
        command = str(argument.get("command", "")).strip()
        if not command:
            raise ValueError("command is required.")
        root = str(argument.get("root") or os.getcwd())
        cwd = _resolve_cwd(root, argument.get("cwd"))
        return ShellRequest(command=command, cwd=cwd)
    raise ValueError("Tool input must be a string command or an object payload.")


def _run_command(command: str, cwd: str) -> tuple[int, str]:
    try:
        from meeseeks_tools.vendor.aider.run_cmd import run_cmd
    except Exception:
        run_cmd = None

    if run_cmd is not None:
        try:
            return run_cmd(command, verbose=False, cwd=cwd)
        except Exception as exc:
            return 1, str(exc)

    completed = subprocess.run(
        command,
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


class AiderShellTool(AbstractTool):
    """Run shell commands using Aider's run_cmd helper."""

    def __init__(self) -> None:
        """Initialize the shell execution tool."""
        super().__init__(
            name="Aider Shell",
            description="Run shell commands via Aider's run_cmd helper.",
            use_llm=False,
        )

    def set_state(self, action_step: ActionStep | None = None) -> MockSpeaker:
        """Execute a shell command and return stdout/stderr."""
        try:
            request = _parse_shell_request(action_step)
        except ValueError as exc:
            MockSpeaker = get_mock_speaker()
            return MockSpeaker(content=str(exc))

        started = time.monotonic()
        exit_code, output = _run_command(request.command, request.cwd)
        duration_ms = int((time.monotonic() - started) * 1000)

        payload: dict[str, object] = {
            "kind": "shell",
            "command": request.command,
            "cwd": request.cwd,
            "exit_code": exit_code,
            "stdout": output,
            "stderr": "",
            "duration_ms": duration_ms,
        }
        MockSpeaker = get_mock_speaker()
        return MockSpeaker(content=payload)


__all__ = ["AiderShellTool"]
