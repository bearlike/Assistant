#!/usr/bin/env python3
"""Post-mount installer wiring the session UX onto ``MewboApp`` (#157).

The global keys + their actions naturally belong on the App, but ``app.py`` is
closed (#150). The installer (an :data:`AppInstaller`) configures the *mounted*
App instead: it builds the feature collaborators (the :class:`RewindCheckpointer`,
:class:`AutoTitler`, :class:`KeybindingConfig`), parks them + a
:class:`SessionController` on the app as well-known attributes the command
handlers reach, and binds the global keys.

**Binding mechanism (option A — sibling-free, no app.py edit).** A spike showed a
``priority`` binding on a mounted *sibling* widget does NOT fire while the Input
is focused (Textual checks App → Screen → focused-widget, not arbitrary
siblings). Binding dynamically on the App itself (``app._bindings.bind``) DOES
fire with the Input focused AND appears in ``screen.active_bindings`` — so the
``Footer`` renders it for free. The controller carries the action *callables*;
the App just routes the key to ``app.action_<name>`` which we attach. The brief's
option B (declarative ``BINDINGS`` + ``action_*`` on ``app.py``) is the alternate
the controller may take instead — documented in the integration contract.

The controller owns the three actions:
``open_transcript`` (``ctrl+o``) → push :class:`TranscriptScreen`;
``open_sessions`` (``ctrl+s``) → push :class:`ResumeScreen`, apply the pick;
``clear_redraw`` (``ctrl+l``) → clear the transcript + force a refresh (the
corruption escape hatch).
"""

from __future__ import annotations

import dataclasses
import os
import types
from typing import TYPE_CHECKING, Any

from mewbo_core.common import get_logger

from mewbo_cli.cli_theme import auto_palette
from mewbo_cli.tui.keybindings import KeybindingConfig
from mewbo_cli.tui.screens.resume import ResumeScreen
from mewbo_cli.tui.screens.transcript_screen import TranscriptScreen
from mewbo_cli.tui.seams import TranscriptItem
from mewbo_cli.tui.session.autotitle import AutoTitler
from mewbo_cli.tui.session.rewind import RewindCheckpointer

if TYPE_CHECKING:
    from collections.abc import Callable

    from mewbo_cli.cli_context import CommandContext
    from mewbo_cli.tui.app import MewboApp

logger = get_logger(name="mewbo.cli.session.install")

# One MewboApp runs per process; the installer publishes the live controller
# here so the slash-command handlers (which run on the turn worker thread with a
# CommandContext that carries NO app handle) can marshal a screen push back onto
# the UI thread. ``None`` outside a running App (plain REPL / tests) → the
# handlers degrade to a notice rather than crashing.
_ACTIVE_CONTROLLER: SessionController | None = None


def active_controller() -> SessionController | None:
    """Return the live :class:`SessionController`, or ``None`` if no App is up."""
    return _ACTIVE_CONTROLLER


class SessionController:
    """Carries the global session actions over a mounted ``MewboApp`` (DI).

    Atomic class: holds the app + its feature collaborators and exposes the
    three action methods bound to ``ctrl+o`` / ``ctrl+s`` / ``ctrl+l``. Kept
    UI-thin — each action pushes a screen or clears the transcript; the heavy
    session logic lives in the runtime/store and the command handlers.
    """

    def __init__(
        self,
        app: MewboApp,
        *,
        runtime: Any,
        store: Any,
        state: Any,
    ) -> None:
        """Bind the mounted app and the shared runtime/store/state."""
        self.app = app
        self.runtime = runtime
        self.store = store
        self.state = state
        self.palette = auto_palette()
        # Populated by the installer post-construction (kept here for typing).
        self.checkpointer: RewindCheckpointer | None = None
        self.titler: AutoTitler | None = None
        self.keymap: KeybindingConfig | None = None

    # -- actions ----------------------------------------------------------

    def open_transcript(self) -> None:
        """Push the read-only full-transcript modal for the active session."""
        sid = self.state.session_id
        self.app.push_screen(
            TranscriptScreen(lambda: self.runtime.load_events(sid), palette=self.palette)
        )

    def open_sessions(self) -> None:
        """Push the session switcher; apply the picked session on dismiss."""
        screen = ResumeScreen(
            self.runtime.list_sessions, current_session_id=self.state.session_id
        )
        self.app.push_screen(screen, self._apply_session)

    def clear_redraw(self) -> None:
        """Clear the transcript and force a full redraw (corruption escape hatch)."""
        try:
            from mewbo_cli.tui.widgets.transcript import TranscriptView

            self.app.query_one(TranscriptView).clear()
        except Exception:  # noqa: BLE001 - clear is best-effort
            pass
        self.app.refresh(layout=True)

    # -- helpers ----------------------------------------------------------

    def _apply_session(self, session_id: str | None) -> None:
        """Switch the active session (retains model + worktree) + refresh header."""
        if not session_id or session_id == self.state.session_id:
            return
        self.state.session_id = session_id
        self._refresh_header(session_id)
        self._notice(f"Resumed session {session_id[:8]}")

    def _refresh_header(self, session_id: str) -> None:
        """Best-effort: re-point the header's session id without editing header.py."""
        try:
            from mewbo_cli.tui.widgets.header import HeaderWidget

            header = self.app.query_one(HeaderWidget)
            header._ctx = dataclasses.replace(header._ctx, session_id=session_id)
            header._refresh_content()
        except Exception:  # noqa: BLE001 - header refresh is cosmetic
            pass

    def _notice(self, text: str) -> None:
        """Write a dim notice into the transcript (falls back to ``notify``)."""
        try:
            self.app._write_item(TranscriptItem("notice", {"text": text}))
        except Exception:  # noqa: BLE001
            self.app.notify(text)

    # -- per-turn hooks (called from app.py _run_turn — see contract) ------

    def checkpoint_turn(self, text: str) -> None:
        """Capture a pre-turn workspace checkpoint (called BEFORE the turn).

        Anchors the checkpoint at the latest session event so a later
        ``/rewind`` knows where to truncate the conversation. Best-effort: a
        non-git cwd or git error just means rewind is unavailable for the turn.
        """
        if self.checkpointer is None:
            return
        try:
            events = self.runtime.load_events(self.state.session_id)
            after_ts = str(events[-1].get("ts")) if events else None
            label = text.strip().splitlines()[0][:60] if text.strip() else "turn"
            self.checkpointer.checkpoint(label=label, after_ts=after_ts)
        except Exception as exc:  # noqa: BLE001 - checkpoint is best-effort
            logger.warning("Turn checkpoint failed: {}: {}", type(exc).__name__, exc)

    def autotitle_after_turn(self) -> None:
        """Kick off auto-titling once (called AFTER the first turn)."""
        if self.titler is None:
            return
        try:
            self.titler.maybe_title(self.state.session_id)
        except Exception as exc:  # noqa: BLE001 - titling is best-effort
            logger.warning("Auto-title kick failed: {}: {}", type(exc).__name__, exc)

    def rewind(self, checkpoint_index: int | None = None) -> str:
        """Revert the workspace + truncate the conversation to a checkpoint.

        Restores files via the checkpointer (which captures current work first)
        and truncates the session transcript after the checkpoint's anchor ts.
        Returns a human-readable status line for the command to print.
        """
        if self.checkpointer is None or not self.checkpointer.is_git_repo():
            return "Rewind unavailable: not inside a git repository."
        checkpoints = self.checkpointer.checkpoints()
        if not checkpoints:
            return "No checkpoints to rewind to yet."
        index = checkpoint_index if checkpoint_index is not None else len(checkpoints) - 1
        if not 0 <= index < len(checkpoints):
            return f"No checkpoint #{index}; have {len(checkpoints)}."
        checkpoint = checkpoints[index]
        if not self.checkpointer.restore(checkpoint):
            return "Rewind failed: git restore error (workspace left untouched)."
        truncated = False
        if checkpoint.after_ts:
            try:
                self.store.truncate_after(self.state.session_id, checkpoint.after_ts)
                truncated = True
            except Exception as exc:  # noqa: BLE001 - workspace already reverted
                logger.warning("Conversation truncate failed: {}: {}", type(exc).__name__, exc)
        conv = " + conversation" if truncated else ""
        return (
            f"Rewound workspace (tracked edits reverted, turn-created files removed)"
            f"{conv} to checkpoint #{index} ({checkpoint.label}). "
            "Prior state saved as a recoverable safety checkpoint."
        )


def make_session_installer(
    *,
    store: Any,
    runtime: Any,
    state: Any,
    keybinding_path: Any = None,
    **_ignored: Any,
) -> Callable[[MewboApp], None]:
    """Build the post-mount installer for the session UX (#157).

    ``store``/``runtime``/``state`` come from ``cli_master._run_app`` scope and
    are exactly the deps the controller + command handlers need. Extra deps
    (``tool_registry``/``config``/``args``…) are accepted and ignored so the
    controller can pass the whole ``_build_installers`` kwarg bag uniformly.
    ``keybinding_path`` overrides the override-file location (tests).

    The returned installer (run at the tail of ``on_mount`` inside the App's
    try/except) parks the collaborators on the app and binds the global keys.
    """
    cwd = os.getcwd()

    def _install(app: MewboApp) -> None:
        controller = SessionController(app, runtime=runtime, store=store, state=state)
        checkpointer = RewindCheckpointer(cwd=cwd)
        titler = AutoTitler(store=store)
        keymap = KeybindingConfig.load(path=keybinding_path) if keybinding_path else (
            KeybindingConfig.load()
        )

        # Park collaborators where the command handlers find them. Stored as
        # plain attributes (the app object is the only shared mutable surface a
        # closed app.py leaves us) — namespaced so they never clash.
        app._session_controller = controller
        app._rewind_checkpointer = checkpointer
        app._auto_titler = titler
        app._keybinding_config = keymap

        # Publish the controller for the slash-command handlers (process-global,
        # one App per process). Stash collaborators on it too so a handler that
        # only has the controller can reach them.
        controller.checkpointer = checkpointer
        controller.titler = titler
        controller.keymap = keymap
        global _ACTIVE_CONTROLLER
        _ACTIVE_CONTROLLER = controller

        # Bind each action onto the App and dynamically register the key. App
        # binding fires with the Input focused and shows in the Footer (spike).
        actions: dict[str, Callable[[], None]] = {
            "open_transcript": controller.open_transcript,
            "open_sessions": controller.open_sessions,
            "clear_redraw": controller.clear_redraw,
        }
        for binding in keymap.bindings():
            handler = actions.get(binding.action)
            if handler is None:
                continue
            _attach_action(app, binding.action, handler)
            try:
                app._bindings.bind(
                    binding.key,
                    binding.action,
                    description=binding.description,
                    show=binding.show,
                )
            except Exception as exc:  # noqa: BLE001 - a bad key never breaks the App
                logger.warning("Could not bind {} → {}: {}", binding.key, binding.action, exc)

    return _install


def _attach_action(app: MewboApp, action: str, handler: Callable[[], None]) -> None:
    """Attach ``action_<name>`` to the app instance routing to ``handler``."""

    def _method(self: MewboApp) -> None:
        handler()

    setattr(app, f"action_{action}", types.MethodType(_method, app))


# --- command handlers (controller registers these on REGISTRY) -----------
# Standalone ``(CommandContext, list[str]) -> bool`` handlers so the controller
# can register them via ``REGISTRY.command(...)( )`` without editing
# cli_commands.py. The interactive ones marshal a screen push onto the UI thread
# through the live controller (commands run on the turn worker thread); they
# degrade to a notice when no App is up (plain REPL / tests).


def cmd_resume(context: CommandContext, args: list[str]) -> bool:
    """``/resume`` — open the session switcher (same as ``ctrl+s``)."""
    del args
    controller = active_controller()
    if controller is None:
        context.console.print("Session switcher needs the interactive TUI.")
        return True
    controller.app.call_from_thread(controller.open_sessions)
    return True


def cmd_rewind(context: CommandContext, args: list[str]) -> bool:
    """``/rewind [N]`` — revert workspace + conversation to checkpoint N (default latest)."""
    controller = active_controller()
    if controller is None:
        context.console.print("Rewind needs the interactive TUI.")
        return True
    index: int | None = None
    if args:
        try:
            index = int(args[0])
        except ValueError:
            context.console.print("Usage: /rewind [checkpoint-index]")
            return True
    status = controller.app.call_from_thread(controller.rewind, index)
    context.console.print(status)
    return True


def cmd_keybindings(context: CommandContext, args: list[str]) -> bool:
    """``/keybindings`` — show the effective global keymap + override-file path."""
    del args
    controller = active_controller()
    keymap = controller.keymap if controller and controller.keymap else KeybindingConfig.load()
    context.console.print(keymap.describe())
    return True


def cmd_compact(context: CommandContext, args: list[str]) -> bool:
    """``/compact [FOCUS]`` — summarize + shrink the session transcript.

    A first-class entry point to the engine's summarizer (FULL compaction). The
    optional remainder biases the summary. Mirrors the existing ``/summarize``
    path; provided so the controller can ensure ``/compact`` is registered even
    if the base registration is ever removed.
    """
    focus = " ".join(args).strip()
    user_query = f"/compact {focus}".rstrip()
    task_queue = context.runtime.run_sync(
        user_query=user_query,
        session_id=context.state.session_id,
        source_platform="cli",
    )
    context.console.print(task_queue.task_result or "(compacted)")
    return True


__all__ = [
    "SessionController",
    "active_controller",
    "cmd_compact",
    "cmd_keybindings",
    "cmd_resume",
    "cmd_rewind",
    "make_session_installer",
]
