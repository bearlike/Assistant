#!/usr/bin/env python3
"""MewboApp — the single Textual application shell (issue #150, epic #149).

Replaces the former four coexisting I/O stacks (Rich ``Live`` + Textual +
prompt_toolkit + raw termios) with ONE ``textual.App`` running on one event
loop in the alternate screen. An explicit :class:`ScreenState` enum
(``onboarding`` / ``landing`` / ``chat``) drives the layout; the layout itself
is ``Vertical(Header, Horizontal(Transcript, Sidebar), Input, Footer)`` with the
sidebar hidden by CSS below a width breakpoint (compact mode).

The App is closed to modification but open to extension via four injected seams
(:mod:`mewbo_cli.tui.seams`), exposed as :attr:`messages`, :attr:`sidebar_slots`,
:attr:`permission` and :attr:`input`. Wave-2 children register at those seams
without editing this file. The turn loop itself lives in
:class:`~mewbo_cli.tui.turn_engine.TurnEngine`, built here via an injected
factory and driven on a thread worker so the UI never blocks.
"""

from __future__ import annotations

import enum
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from rich.console import Group
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Footer, Input

from mewbo_cli.aider_ui import render_markdown
from mewbo_cli.tui.permission_service import PermissionMode
from mewbo_cli.tui.seams import (
    InputGateway,
    MessageRendererRegistry,
    PermissionGateway,
    SidebarSlotRegistry,
    TranscriptItem,
)
from mewbo_cli.tui.widgets.header import HeaderContext, HeaderWidget
from mewbo_cli.tui.widgets.input_area import InputArea
from mewbo_cli.tui.widgets.sidebar import SidebarView
from mewbo_cli.tui.widgets.status_line import StatusLine
from mewbo_cli.tui.widgets.transcript import TranscriptView

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rich.console import RenderableType


class ScreenState(enum.Enum):
    """The explicit lifecycle state that drives the layout."""

    ONBOARDING = "onboarding"
    LANDING = "landing"
    CHAT = "chat"


class EngineLike(Protocol):
    """The slice of :class:`TurnEngine` the App drives (handle one input line)."""

    def handle(self, text: str) -> bool:
        """Dispatch one line; return ``False`` to quit the app."""
        ...


Emit = Callable[[TranscriptItem], None]
EmitRenderable = Callable[["RenderableType"], None]
EngineFactory = Callable[[Emit, EmitRenderable], EngineLike]
AppInstaller = Callable[["MewboApp"], None]
"""A post-mount installer configures the *mounted* App (inject widget
collaborators, register sidebar slots, bind keys, push screens) without
editing this file — the extension path for the Wave-2 input/sidebar/session
children (#155/#156/#157). Run at the tail of ``on_mount``; a failing
installer degrades to a dim notice rather than breaking the App."""


# --- foundation message renderers ----------------------------------------
# Minimal renderers so the placeholder transcript shows real content. The
# transcript child (#152) registers richer renderers on the SAME seam; on_mount
# never clobbers a kind a child already registered.


def _render_user(item: TranscriptItem) -> RenderableType:
    text = Text()
    text.append("❯ ", style="bold cyan")
    text.append(str(item.payload.get("text", "")))
    return text


def _render_assistant(item: TranscriptItem) -> RenderableType:
    return render_markdown(str(item.payload.get("text", "")))


def _render_notice(item: TranscriptItem) -> RenderableType:
    return Text(str(item.payload.get("text", "")), style="dim")


def _render_tool(item: TranscriptItem) -> RenderableType:
    payload = item.payload
    label = str(payload.get("tool_id", "tool"))
    if payload.get("operation"):
        label = f"{label}:{payload['operation']}"
    if payload.get("is_mcp"):
        label = f"{label} (MCP)"
    head = Text(f"⚙ {label}", style="magenta")
    result = payload.get("result")
    if result is None:
        return head
    preview = (result if isinstance(result, str) else repr(result)).strip()
    if not preview:
        return head
    if len(preview) > 500:
        preview = preview[:500] + "…"
    return Group(head, Text(preview, style="dim"))


def _render_plan(item: TranscriptItem) -> RenderableType:
    plan = item.payload.get("plan")
    steps = list(getattr(plan, "steps", []) or [])
    lines: list[Text] = [Text("Action plan:", style="bold cyan")]
    for index, step in enumerate(steps, start=1):
        lines.append(Text(f"  {index}. {getattr(step, 'title', str(step))}"))
    if not steps:
        lines.append(Text("  (no steps)", style="dim"))
    return Group(*lines)


def _render_plan_proposal(item: TranscriptItem) -> RenderableType:
    return render_markdown(str(item.payload.get("text", "")))


_FOUNDATION_RENDERERS: dict[str, Callable[[TranscriptItem], RenderableType]] = {
    "user": _render_user,
    "assistant": _render_assistant,
    "notice": _render_notice,
    "tool": _render_tool,
    "plan": _render_plan,
    "plan_proposal": _render_plan_proposal,
}


class MewboApp(App[int]):
    """The single Mewbo CLI Textual application."""

    CSS = """
    #body { height: 1fr; }
    #transcript { width: 1fr; padding: 0 1; }
    #sidebar { width: 36; border-left: solid $panel; padding: 0 1; }
    Screen.compact #sidebar { display: none; }
    /* The composer flows above the bottom bar (NOT docked itself — a second
       bottom-dock would overlap the Footer, the regression this fixes). The
       margin sets it clearly off from the transcript above and the status bar
       below. */
    #input { height: auto; margin: 1 1; }
    /* ONE bottom-docked bar stacks the IDE status line flush above the keybinding
       Footer, so the two never fight for the bottom edge. */
    #footerbar { dock: bottom; height: auto; }
    #statusline { height: 1; }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
        Binding("shift+tab", "cycle_permission_mode", "Permission mode", show=True, priority=True),
    ]

    #: Hide the sidebar (compact mode) below this terminal width.
    SIDEBAR_BREAKPOINT = 90

    screen_state: reactive[ScreenState] = reactive(ScreenState.LANDING)
    #: Current permission operating mode; ``shift+tab`` cycles it.
    permission_mode: reactive[PermissionMode] = reactive(PermissionMode.NORMAL)

    def __init__(
        self,
        *,
        header_ctx: HeaderContext,
        messages: MessageRendererRegistry,
        sidebar_slots: SidebarSlotRegistry,
        permission: PermissionGateway,
        input_gateway: InputGateway,
        engine_factory: EngineFactory,
        onboarding: bool = False,
        landing_hint: str | None = None,
        onboarding_notices: Sequence[str] = (),
        installers: Sequence[AppInstaller] = (),
    ) -> None:
        """Inject the header context, the four seams and the engine factory.

        ``installers`` are post-mount hooks (the input/sidebar/session children
        register here) run once at the tail of :meth:`on_mount`; see
        :data:`AppInstaller`.
        """
        super().__init__()
        self._header_ctx = header_ctx
        self.messages = messages
        self.sidebar_slots = sidebar_slots
        self.permission = permission
        self.input = input_gateway
        self._engine_factory = engine_factory
        self._onboarding = onboarding
        self._landing_hint = landing_hint
        self._onboarding_notices = list(onboarding_notices)
        self._installers = list(installers)
        self._engine: EngineLike | None = None
        self._transcript: TranscriptView | None = None
        self._input_widget: InputArea | None = None

    # -- composition ------------------------------------------------------

    def compose(self) -> ComposeResult:
        """Lay out header, transcript+sidebar body, input and footer."""
        yield HeaderWidget(self._header_ctx, id="header")
        with Horizontal(id="body"):
            yield TranscriptView(self.messages, id="transcript")
            yield SidebarView(self.sidebar_slots, id="sidebar")
        yield InputArea(self.input, id="input")
        with Vertical(id="footerbar"):
            yield StatusLine(id="statusline")
            yield Footer()

    def on_mount(self) -> None:
        """Wire the engine, register default renderers and set initial state."""
        self._transcript = self.query_one(TranscriptView)
        self._input_widget = self.query_one(InputArea)

        for kind, renderer in _FOUNDATION_RENDERERS.items():
            if not self.messages.has(kind):
                self.messages.register(kind, renderer)

        self._engine = self._engine_factory(self._emit, self._emit_renderable)

        if self._onboarding:
            self.screen_state = ScreenState.ONBOARDING
            notices = self._onboarding_notices or [
                "Welcome to Mewbo. Run /init to scaffold config, then start chatting."
            ]
            for line in notices:
                self._write_item(TranscriptItem("notice", {"text": line}))
        else:
            self.screen_state = ScreenState.LANDING
            self._write_item(
                TranscriptItem(
                    "notice",
                    {"text": self._landing_hint or "Mewbo ready. Type a message, or /help."},
                )
            )

        self._apply_compact(self.size.width or 80)
        if self._input_widget is not None:
            self.set_focus(self._input_widget)

        self._run_installers()

    def _run_installers(self) -> None:
        """Run each post-mount installer; a failure degrades to a dim notice."""
        for installer in self._installers:
            try:
                installer(self)
            except Exception as exc:  # noqa: BLE001 - never let an installer break the App
                name = getattr(installer, "__name__", installer)
                self._write_item(
                    TranscriptItem("notice", {"text": f"⚠ installer {name} failed: {exc}"})
                )

    # -- reactive / layout ------------------------------------------------

    def watch_screen_state(self, state: ScreenState) -> None:
        """Mirror the screen state onto Screen CSS classes (layout hooks)."""
        if self._transcript is None:  # not mounted yet
            return
        screen = self.screen
        screen.set_class(state is ScreenState.ONBOARDING, "onboarding")
        screen.set_class(state is ScreenState.LANDING, "landing")
        screen.set_class(state is ScreenState.CHAT, "chat")

    def on_resize(self, event: events.Resize) -> None:
        """Toggle compact mode (sidebar hidden) at the width breakpoint."""
        self._apply_compact(event.size.width)

    def _apply_compact(self, width: int) -> None:
        if self._transcript is None:  # not mounted yet
            return
        self.screen.set_class(width < self.SIDEBAR_BREAKPOINT, "compact")

    # -- permission mode --------------------------------------------------

    def action_cycle_permission_mode(self) -> None:
        """Cycle permission_mode on shift+tab: NORMAL → AUTO_ACCEPT_EDITS → PLAN → NORMAL."""
        self.permission_mode = self.permission_mode.next()

    def watch_permission_mode(self, mode: PermissionMode) -> None:
        """Reflect the active permission mode in the Footer via sub_title."""
        labels: dict[PermissionMode, str] = {
            PermissionMode.NORMAL: "",
            PermissionMode.AUTO_ACCEPT_EDITS: "auto-accept edits",
            PermissionMode.PLAN: "plan mode",
        }
        self.sub_title = labels.get(mode, mode.value)

    # -- input handling ---------------------------------------------------

    @on(Input.Submitted)
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value
        event.input.value = ""
        if not text.strip():
            return
        if self.screen_state is not ScreenState.CHAT:
            self.screen_state = ScreenState.CHAT
        self._run_turn(text)

    @work(thread=True, exclusive=True, group="turn")
    def _run_turn(self, text: str) -> None:
        """Run one dispatch turn off the UI thread; exit if the engine says so.

        Wave-2 hooks (resolved live so the App stays closed to those children):
        the session controller (#157) takes a pre-turn workspace checkpoint and
        kicks idempotent auto-titling after the turn; the input widget (#155) is
        marked busy for the turn so follow-up Enters queue, and any queued
        message is drained and run next.
        """
        if self._engine is None:
            return
        # Close the busy-gate FIRST — before any blocking pre-turn work (the git
        # checkpoint below runs subprocesses) — so a second Enter in that window
        # queues instead of starting a concurrent turn against the same session.
        if self._input_widget is not None:
            self.call_from_thread(self._input_widget.set_busy, True)
        # Show the "working" spinner for the whole turn. The hub updates its label
        # live (thinking / running <tool>); on settle it COLLAPSES into a muted
        # turn-summary line carrying the total elapsed (#161).
        turn_started = time.monotonic()
        if self._transcript is not None:
            self.call_from_thread(self._transcript.start_activity, "Working")
        controller = getattr(self, "_session_controller", None)
        if controller is not None:
            controller.checkpoint_turn(text)  # BEFORE the turn (worker thread; no UI touch)
        try:
            keep = self._engine.handle(text)
        finally:
            if self._transcript is not None:
                self.call_from_thread(
                    self._transcript.finish_activity, time.monotonic() - turn_started
                )
            if self._input_widget is not None:
                self.call_from_thread(self._input_widget.set_busy, False)
            if controller is not None:
                controller.autotitle_after_turn()  # AFTER the turn (idempotent)
        if not keep:
            self.call_from_thread(self.exit, 0)
            return
        if self._input_widget is not None:
            nxt = self.call_from_thread(self._input_widget.drain_next)
            if nxt:
                self.call_from_thread(self._run_turn, nxt)

    # -- emit callbacks (thread-safe transcript writes) -------------------

    def _emit(self, item: TranscriptItem) -> None:
        self.call_from_thread(self._write_item, item)

    def _emit_renderable(self, renderable: RenderableType) -> None:
        self.call_from_thread(self._write_renderable, renderable)

    def _write_item(self, item: TranscriptItem) -> None:
        if self._transcript is not None:
            self._transcript.write_item(item)

    def _write_renderable(self, renderable: RenderableType) -> None:
        if self._transcript is not None:
            self._transcript.write_renderable(renderable)


__all__ = [
    "AppInstaller",
    "EngineFactory",
    "EngineLike",
    "MewboApp",
    "PermissionMode",
    "ScreenState",
]
