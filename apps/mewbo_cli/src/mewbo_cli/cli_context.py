#!/usr/bin/env python3
"""Shared CLI context types."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mewbo_core.classes import Plan
from mewbo_core.hooks import HookManager
from mewbo_core.session_runtime import SessionRuntime
from mewbo_core.session_store import SessionStore
from mewbo_core.tool_registry import ToolRegistry
from rich.console import Console

# Synthetic continuation handed to the act-mode run that implements an approved
# plan. The approved plan already lives in the conversation history + plan.md.
# Shared by the App (TurnEngine modal-approve) and the plain-fallback /continue.
PLAN_APPROVE_CONTINUATION = (
    "[system] The user approved your plan. Proceed with implementation using the "
    "full toolset. The approved plan is in the conversation history."
)


@dataclass
class CliState:
    """State persisted across CLI interactions."""

    session_id: str
    show_plan: bool = True
    model_name: str | None = None
    fallback_models: tuple[str, ...] | None = None
    auto_approve_all: bool = False
    mode: str = "act"
    last_plan: Plan | None = field(default=None, repr=False)


@dataclass
class CommandContext:
    """Context passed to CLI command handlers."""

    console: Console
    store: SessionStore
    state: CliState
    tool_registry: ToolRegistry
    runtime: SessionRuntime
    prompt_func: Callable[[str], str] | None
    # Optional tool-approval callback + hook factory so a command that spins up a
    # follow-up run (e.g. ``/approve`` executing an approved plan in act mode)
    # inherits the App's permission modal + lifecycle hooks. Default ``None`` keeps
    # every existing construction site unchanged.
    approval_callback: Callable[..., Any] | None = None
    hook_factory: Callable[[], HookManager | None] | None = None

    def refuse_if_terminated(self, action: str) -> bool:
        """Print a refusal and return ``True`` if the session is terminated.

        Shared guard for command handlers that would otherwise run, steer, or
        fork-resurrect a permanently terminated session: ``/fork``,
        ``/retry``, ``/continue``, ``/edit``. Mirrors the ``Cannot {action}:
        {reason}`` wording ``_run_recovery`` already uses for other refusals.
        """
        if not self.runtime.is_terminated(self.state.session_id):
            return False
        self.console.print(
            f"Cannot {action}: session {self.state.session_id} is permanently terminated.",
            style="yellow",
        )
        return True
