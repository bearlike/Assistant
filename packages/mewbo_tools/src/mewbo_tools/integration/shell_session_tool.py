#!/usr/bin/env python3
"""The observation half of background execution: read, write, kill, list."""

from __future__ import annotations

from mewbo_core.classes import AbstractTool, ActionStep
from mewbo_core.common import MockSpeaker, get_mock_speaker
from pydantic import ValidationError

from mewbo_tools.integration.shell_session import SHELL_SESSIONS, ShellSessionArgs


class ShellSessionTool(AbstractTool):
    """Observe and steer a shell started with ``run_in_background``.

    ``read`` and ``write`` are separate operations on purpose. Codex overloads
    one ``write_stdin`` verb, where ``chars=""`` silently means "poll, do not
    write" — one name carrying opposite senses, which is the drift this
    codebase refuses elsewhere. Reading and writing are different intentions and
    get different words.
    """

    def __init__(self) -> None:
        """Initialize the shell session tool."""
        super().__init__(
            name="Shell Session",
            description="Read output from, write input to, or stop a background shell.",
            use_llm=False,
        )

    def set_state(self, action_step: ActionStep | None = None) -> MockSpeaker:
        """Dispatch one session operation onto the process-wide store."""
        speaker = get_mock_speaker()
        if action_step is None:
            return speaker(content="Action step is required.")
        argument = action_step.tool_input
        if not isinstance(argument, dict):
            return speaker(content="Tool input must be an object payload.")
        try:
            args = ShellSessionArgs.model_validate(argument)
        except ValidationError as exc:
            # Every error with its field path, plus the expected field set —
            # `errors()[0]["msg"]` alone drops `loc`, and "Extra inputs are not
            # permitted" with no field name is unactionable (the caller can
            # only drop keys at random). See ShellSessionArgs.explain_errors.
            return speaker(
                content=f"Invalid arguments: {ShellSessionArgs.explain_errors(exc)}"
            )

        try:
            return speaker(content=self._dispatch(args))
        except ValueError as exc:
            return speaker(content=str(exc))

    def _dispatch(self, args: ShellSessionArgs) -> dict[str, object]:
        """Run the requested operation, raising ``ValueError`` on a bad handle."""
        if args.operation == "list":
            return {"kind": "shell_session", "operation": "list", "sessions": SHELL_SESSIONS.list()}
        if not args.shell_id:
            raise ValueError(
                f"shell_id is required for operation {args.operation!r}. "
                "List the live sessions with {'operation': 'list'}."
            )
        session = SHELL_SESSIONS.get(args.shell_id)
        if args.operation == "kill":
            SHELL_SESSIONS.discard(args.shell_id)
            return {
                "kind": "shell_session",
                "operation": "kill",
                "shell_id": args.shell_id,
                "status": "exited",
            }
        if args.operation == "write":
            session.write(args.input, newline=args.newline)
            # Read straight back so a write and its response are ONE step: an
            # interactive program answers a prompt immediately, and making the
            # model spend a second call to see that answer is what turns driving
            # a CLI into a step-budget problem.
            payload = session.read(cursor=args.cursor, pattern=args.filter, wait_ms=args.wait_ms)
            payload["operation"] = "write"
            payload["kind"] = "shell_session"
            return payload
        payload = session.read(cursor=args.cursor, pattern=args.filter, wait_ms=args.wait_ms)
        payload["operation"] = "read"
        payload["kind"] = "shell_session"
        return payload


__all__ = ["ShellSessionTool"]
