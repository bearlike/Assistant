#!/usr/bin/env python3
"""Plan-mode approval gating: ``ExitPlanMode`` internal tool.

In plan mode, the LLM explores the codebase with read-only tools and drafts a
plan to ``/tmp/truss/plans/<session_id>/plan.md`` using the configured edit
tool (path-scoped at the permission layer). When the plan is ready the LLM
calls ``exit_plan_mode`` — this module's handler reads the plan file, emits a
``plan_proposed`` event, and signals the loop to terminate so the thread exits
cleanly. Approval/rejection is handled episodically by ``SessionRuntime``.

Follows the internal-tool-schema pattern from ``spawn_agent.py`` and
``skills.py``: the schema is injected into ``bind_tools()``, but the tool is
NOT registered in the ``ToolRegistry``. Dispatch happens inside
``ToolUseLoop._execute_tool_call``.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

from truss_core.common import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from truss_core.classes import ActionStep
    from truss_core.types import Event

logging = get_logger(name="core.exit_plan_mode")


# ------------------------------------------------------------------
# Constants and path helpers
# ------------------------------------------------------------------

PLAN_DIR_ROOT = "/tmp/truss/plans"
"""Root directory for all per-session plan scratch directories."""

SESSION_TEMP_ROOT = "/tmp/truss/sessions"
"""Root directory for per-session temporary working directories."""

PLAN_FILE_NAME = "plan.md"
_REVISION_FILE_NAME = "revision.txt"

_SHELL_TOOL_IDS: frozenset[str] = frozenset({"aider_shell_tool"})
"""Tool IDs recognized as the shell tool. Gated by an allowlist in plan mode.

Kept as the single source of truth; if the shell tool is ever replaced or
an alternative implementation is added, extend this set.
"""

_PLAN_MODE_METACHARS: tuple[str, ...] = ("|", ">", "<", "&", ";", "$", "`")
r"""Shell metacharacters that trivially defeat a command allowlist.

A command containing any of these *outside* quoted arguments is rejected
in plan mode regardless of its allowlist match, because each enables
escape:
  - ``|`` / ``;`` / ``&`` — chain to an unchecked command
  - ``>`` / ``<`` — redirect to write files or read arbitrary input
  - ``$`` / `` ` `` — variable expansion / command substitution

The same characters appearing *inside* a single- or double-quoted argument
are literal data to the shell (e.g. ``grep "a\|b" file``) and pose no
allowlist-escape risk, so they are permitted. See
:func:`_has_unquoted_metachar`.
"""


def _has_unquoted_metachar(command: str) -> bool:
    r"""Return True if ``command`` contains a metachar outside any quotes.

    Walks ``command`` left-to-right tracking single/double quote state
    with POSIX semantics:

    - Inside single quotes, every character is literal (no escaping, no
      other quote types recognized) until the closing ``'``.
    - Inside double quotes, metachars are literal data to the shell,
      so they are NOT flagged; only ``"`` closes the quoted region.
    - Outside any quotes, a backslash escapes the next character as a
      literal, but the escaped char is still considered "unquoted" —
      ``\\|`` at the command level would let the model smuggle a pipe
      through a second allowlist-evasion syntax, so a backslash
      followed by a metachar is rejected.
    - Outside any quotes, any character in :data:`_PLAN_MODE_METACHARS`
      triggers an immediate True return.

    This mirrors the shell's rule that metachars are only operators
    when they are not protected by quotes.
    """
    in_single = False
    in_double = False
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if in_single:
            if ch == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if ch == '"':
                in_double = False
            i += 1
            continue
        # Outside any quotes below.
        if ch == "\\":
            # Backslash escapes the next char at the command level. If
            # that char is a metachar we reject — allowing ``\|`` would
            # give the model a second metachar-smuggling syntax that
            # bypasses the allowlist. Otherwise skip past both.
            if i + 1 < n and command[i + 1] in _PLAN_MODE_METACHARS:
                return True
            i += 2
            continue
        if ch == '"':
            in_double = True
            i += 1
            continue
        if ch == "'":
            in_single = True
            i += 1
            continue
        if ch in _PLAN_MODE_METACHARS:
            return True
        i += 1
    return False


def session_temp_dir(session_id: str) -> str:
    """Return the temporary working directory for a session, creating it if needed."""
    path = os.path.join(SESSION_TEMP_ROOT, session_id)
    os.makedirs(path, exist_ok=True)
    return path


def plan_dir_for(session_id: str) -> str:
    """Return the scoped plan directory path for a session.

    Args:
        session_id: Opaque session identifier.

    Returns:
        Absolute path ``/tmp/truss/plans/<session_id>/``.
    """
    return os.path.join(PLAN_DIR_ROOT, session_id)


def plan_file_for(session_id: str) -> str:
    """Return the ``plan.md`` path for a session."""
    return os.path.join(plan_dir_for(session_id), PLAN_FILE_NAME)


def ensure_plan_dir(session_id: str) -> str:
    """Create the session-scoped plan directory and empty plan file.

    Returns:
        Absolute path to the created directory.
    """
    path = plan_dir_for(session_id)
    os.makedirs(path, exist_ok=True)
    # Touch the plan file so edit tools can write immediately —
    # models waste steps trying to mkdir/create if the file is missing.
    plan_file = plan_file_for(session_id)
    if not os.path.exists(plan_file):
        Path(plan_file).touch()
    return path


def is_inside_plan_dir(candidate: str, session_id: str) -> bool:
    """Return True if ``candidate`` resolves inside the session's plan dir.

    Rejects path traversal: ``Path.resolve()`` normalises ``..`` segments
    before the containment check.
    """
    try:
        resolved_candidate = Path(candidate).resolve()
        resolved_plan_dir = Path(plan_dir_for(session_id)).resolve()
    except (OSError, ValueError):
        return False
    try:
        resolved_candidate.relative_to(resolved_plan_dir)
    except ValueError:
        return False
    return True


def is_shell_command_plan_safe(command: str, allowlist: list[str]) -> bool:
    """Return True iff ``command`` is plan-mode safe under ``allowlist``.

    Two conditions must hold:

    1. The raw ``command`` string contains NO shell metacharacters from
       :data:`_PLAN_MODE_METACHARS`. Pipes/redirects/chaining/substitution
       all enable trivial escape from a command allowlist.
    2. After tokenizing with :func:`shlex.split`, the joined-by-single-space
       form of the command starts with one of the ``allowlist`` prefixes
       at a word boundary. ``"git log"`` matches ``"git log --oneline"``
       but NOT ``"git logger"`` or ``"git"`` alone.

    Args:
        command: Raw shell command string as received from the tool call.
        allowlist: List of command prefixes (single or multi-token) that
            are permitted. An empty list denies everything.

    Returns:
        True if the command is permitted in plan mode, False otherwise.
    """
    if not command or not command.strip():
        return False
    if not allowlist:
        return False
    if _has_unquoted_metachar(command):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens:
        return False
    cmd_str = " ".join(tokens)
    for prefix in allowlist:
        prefix = prefix.strip()
        if not prefix:
            continue
        if cmd_str == prefix or cmd_str.startswith(prefix + " "):
            return True
    return False


def _next_revision(session_id: str) -> int:
    """Read, increment, and persist a per-session revision counter.

    Stored as a plain-text integer at ``<plan_dir>/revision.txt`` so the
    counter survives across orchestrator runs (cross-run refinement).
    """
    ensure_plan_dir(session_id)
    rev_path = os.path.join(plan_dir_for(session_id), _REVISION_FILE_NAME)
    current = 0
    if os.path.exists(rev_path):
        try:
            with open(rev_path, encoding="utf-8") as handle:
                current = int(handle.read().strip() or "0")
        except (OSError, ValueError):
            current = 0
    next_rev = current + 1
    try:
        with open(rev_path, "w", encoding="utf-8") as handle:
            handle.write(str(next_rev))
    except OSError as exc:
        logging.warning("Failed to persist plan revision counter: {}", exc)
    return next_rev


# ------------------------------------------------------------------
# Internal tool schema (injected into bind_tools, NOT in ToolRegistry)
# ------------------------------------------------------------------

EXIT_PLAN_MODE_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "exit_plan_mode",
        "description": (
            "Signal that your plan is complete and ready for user approval. "
            "Call this after you have written the full plan to the session's "
            "plan.md file using your edit tool. The tool takes no content "
            "parameters — it reads the plan from disk. Do NOT call this "
            "before writing the plan file, and do NOT call it again after "
            "rejection until the user has provided refinement guidance."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": (
                        "Optional one-line summary of the plan for event logs. "
                        "The full plan content is read from the file."
                    ),
                },
            },
            "required": [],
        },
    },
}


# ------------------------------------------------------------------
# Handler (wired into ToolUseLoop like SpawnAgentTool)
# ------------------------------------------------------------------


class _HandlerResult:
    """Lightweight speaker-style wrapper carrying tool-result content."""

    __slots__ = ("content",)

    def __init__(self, content: str) -> None:
        self.content = content


class ExitPlanModeTool:
    """Handles ``exit_plan_mode`` tool calls during plan mode.

    One instance is created per root ``ToolUseLoop``. Holds the session id
    and an event logger. After emitting ``plan_proposed``, sets a termination
    flag that the outer loop polls to exit cleanly — approval/rejection is
    handled episodically by ``SessionRuntime``.

    Class-level ``tool_id`` and ``schema`` attributes let this class satisfy
    the :class:`~truss_core.session_tools.SessionTool` Protocol.
    """

    tool_id: str = "exit_plan_mode"
    schema: dict[str, object] = EXIT_PLAN_MODE_SCHEMA
    modes: frozenset[str] = frozenset({"plan"})

    def __init__(
        self,
        *,
        session_id: str,
        event_logger: Callable[[Event], None] | None = None,
    ) -> None:
        """Initialize the handler.

        Args:
            session_id: Session identifier used to resolve the plan path.
            event_logger: Callback for emitting ``plan_*`` events; usually
                ``agent_context.event_logger``.
        """
        self._session_id = session_id
        self._event_logger = event_logger
        self._terminate_run_pending = False
        self._last_revision = 0
        self._empty_file_warned = False

    @property
    def plan_path(self) -> str:
        """Absolute path to the session's ``plan.md``."""
        return plan_file_for(self._session_id)

    def should_terminate_run(self) -> bool:
        """Return True once if the run should terminate for plan approval; resets the flag."""
        if self._terminate_run_pending:
            self._terminate_run_pending = False
            return True
        return False

    async def handle(self, action_step: ActionStep) -> _HandlerResult:
        """Execute an ``exit_plan_mode`` tool call.

        Reads ``plan.md`` from the session's scoped directory, emits a
        ``plan_proposed`` event, sets the termination flag so the loop
        exits cleanly, and returns immediately. Approval/rejection is
        handled episodically by ``SessionRuntime``.
        """
        path = self.plan_path
        if not os.path.exists(path):
            msg = (
                f"Plan file not found at {path}. Use your edit tool to write "
                "the plan first, then call exit_plan_mode."
            )
            return _HandlerResult(content=f"ERROR: {msg}")

        try:
            with open(path, encoding="utf-8") as handle:
                content = handle.read()
        except OSError as exc:
            return _HandlerResult(content=f"ERROR: Failed to read plan file: {exc}")

        if not content.strip():
            if not self._empty_file_warned:
                self._empty_file_warned = True
                return _HandlerResult(
                    content=(
                        f"Plan file at {path} is empty. Write the plan "
                        "using your edit tool first, then call "
                        "exit_plan_mode again. Call exit_plan_mode once "
                        "more to override and exit without a plan."
                    )
                )
            # Second call with empty file — override, allow exit.
            self._empty_file_warned = False

        revision = _next_revision(self._session_id)
        self._last_revision = revision

        args = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        summary = str(args.get("summary", "") or "")

        self._emit(
            {
                "type": "plan_proposed",
                "payload": {
                    "plan_path": path,
                    "revision": revision,
                    "content": content,
                    "summary": summary,
                },
            }
        )
        logging.info(
            "Plan proposed for session {} (revision={}, chars={})",
            self._session_id,
            revision,
            len(content),
        )

        self._terminate_run_pending = True
        return _HandlerResult(
            content=(
                f"Plan proposed (revision {revision}). The run will now "
                "terminate and await user approval. No further actions needed."
            )
        )

    def _emit(self, event: Event) -> None:
        if self._event_logger is not None:
            try:
                self._event_logger(event)
            except Exception as exc:  # pragma: no cover - defensive
                logging.warning("Failed to emit plan event: {}", exc)


__all__ = [
    "EXIT_PLAN_MODE_SCHEMA",
    "ExitPlanModeTool",
    "PLAN_DIR_ROOT",
    "PLAN_FILE_NAME",
    "SHELL_TOOL_IDS",
    "ensure_plan_dir",
    "is_inside_plan_dir",
    "is_shell_command_plan_safe",
    "plan_dir_for",
    "plan_file_for",
]

# Public alias for consumers (tool_use_loop). The underscore variant is the
# module-internal canonical name; exposing under a non-underscored symbol
# keeps imports clean at the call site without duplicating the data.
SHELL_TOOL_IDS = _SHELL_TOOL_IDS
