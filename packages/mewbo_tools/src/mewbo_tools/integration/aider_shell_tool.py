#!/usr/bin/env python3
"""Shell execution helper adapted from Aider."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass

from mewbo_core.classes import AbstractTool, ActionStep
from mewbo_core.common import MockSpeaker, get_mock_speaker

from mewbo_tools.core import resolve_safe_path

# Environment overlay that keeps every shell invocation non-interactive: no
# pager waiting on a keypress, no credential prompt blocking on stdin.
_PAGER_SAFE_ENV = {
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
}

# Slightly under ToolSpec's default 120s tool-call timeout
# (mewbo_core.tool_registry.ToolSpec.timeout, enforced by
# ToolUseLoop._safe_execute via asyncio.wait_for) so the subprocess reaps
# itself before the outer timeout fires. Cancelling that asyncio.wait_for
# only abandons the awaiting coroutine — it can't kill a blocking
# subprocess.run() — so without a bound here the process (and any pager
# child) would leak as an orphan every time the outer timeout fired.
_DEFAULT_TIMEOUT_S = 115.0


@dataclass(frozen=True)
class ShellRequest:
    command: str
    cwd: str


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
        cwd = str(resolve_safe_path(argument.get("cwd") or root, root=root))
        return ShellRequest(command=command, cwd=cwd)
    raise ValueError("Tool input must be a string command or an object payload.")


def _run_command(command: str, cwd: str, *, timeout: float = _DEFAULT_TIMEOUT_S) -> tuple[int, str]:
    """Run a shell command non-interactively, reaping the whole process group on timeout.

    Always a plain subprocess — never a PTY (the vendored Aider ``run_cmd`` picks a
    pexpect PTY whenever the parent has a tty, which lets a paginated command like
    ``git log`` block forever on ``less`` and leaks the interactive rc-file banner
    into the captured output). ``start_new_session`` puts the command in its own
    process group so a timeout can kill the command AND any children it spawned
    (e.g. a pager) instead of orphaning them.
    """
    env = {**os.environ, **_PAGER_SAFE_ENV}
    try:
        # shell=True is the tool's entire purpose: ``command`` is the agent's own tool
        # argument, and running an agent-authored shell command IS the documented trust
        # boundary — there is nothing to sanitize (quoting would defeat the feature).
        # Admission is gated upstream by tool-scope/allowed_tools; ``cwd`` is separately
        # confined by resolve_safe_path. The command itself cannot be sandboxed — it is a
        # shell — so the guard is on whether the agent holds this tool at all, not the string.
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=env,
        )
    except OSError as exc:
        return 1, str(exc)

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, (stdout or "") + (stderr or "")
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
        output = (stdout or "") + (stderr or "")
        message = f"[shell] command timed out after {timeout}s and was killed"
        return 1, f"{output}\n{message}" if output else message


class AiderShellTool(AbstractTool):
    """Run shell commands non-interactively, no PTY."""

    def __init__(self) -> None:
        """Initialize the shell execution tool."""
        super().__init__(
            name="Aider Shell",
            description="Run shell commands non-interactively (no PTY, pager-safe).",
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
