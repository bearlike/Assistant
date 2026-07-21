#!/usr/bin/env python3
"""make_sidebar_installer — wire the faceted sidebar slots into MewboApp.

The post-mount :data:`~mewbo_cli.tui.app.AppInstaller` for the sidebar. The
controller appends ``make_sidebar_installer(...)`` to
``cli_master._build_installers`` and the App runs it once at the tail of
``on_mount`` (already fully composed, so ``app.query_one`` works).

It does four things:

1. registers the three faceted sidebar sections — **Fleet** (order 10),
   **Plan** (order 20) and **Context** (order 30) — on ``app.sidebar_slots``,
   and wires the fleet's row-select to the in-place drill controller,
2. refreshes the mounted :class:`~mewbo_cli.tui.widgets.sidebar.SidebarView` so
   the late-registered slots appear,
3. seeds the status bar with the current model/provider/branch and sets the
   **terminal title** from a short session description,
4. starts the **statusLine** interval (running the user script with session JSON
   on stdin) when one is configured — degrading silently otherwise.

The widgets are stored on the App as ``app._fleet_panel`` / ``app._todo_panel``
/ ``app._status_bar`` (plus ``app._drill_controller``) so the controller's turn
worker can drive them (``app.call_from_thread(panel.refresh_fleet)``).

One atomic factory; everything it needs is injected (no globals).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from mewbo_core.worktree import WorktreeManager

from mewbo_cli.cli_theme import DEFAULT_PALETTE, Palette
from mewbo_cli.tui.status.context_meter import ContextMeter
from mewbo_cli.tui.status.statusline import StatusLineRunner, StatusLineState
from mewbo_cli.tui.status.terminal_title import set_terminal_title
from mewbo_cli.tui.widgets.fleet_drill import FleetDrillController
from mewbo_cli.tui.widgets.fleet_panel import FleetPanel
from mewbo_cli.tui.widgets.sidebar import SidebarSection
from mewbo_cli.tui.widgets.status_bar import StatusBar, StatusState
from mewbo_cli.tui.widgets.todo_panel import TodoPanel, TodoState

if TYPE_CHECKING:
    from mewbo_cli.tui.agent_transcript_hub import AgentTranscriptHub
    from mewbo_cli.tui.app import MewboApp
    from mewbo_cli.tui.seams import MessageRendererRegistry


def _provider_of(model: str | None) -> str | None:
    """Derive the provider from a ``provider/model`` id (else ``None``)."""
    if not model:
        return None
    return model.split("/", 1)[0] if "/" in model else None


def _git_branch(cwd: str) -> str | None:
    """Return the current git branch for ``cwd``, never raising."""
    try:
        return WorktreeManager.current_branch(cwd)
    except Exception:
        return None


def _git_stash_count(cwd: str) -> int:
    """Return the git stash depth for ``cwd`` (``0`` on any error / no stash)."""
    import subprocess

    try:
        completed = subprocess.run(  # noqa: S603,S607 - fixed argv, no shell
            ["git", "-C", cwd, "stash", "list"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    if completed.returncode != 0:
        return 0
    return sum(1 for line in completed.stdout.splitlines() if line.strip())


def make_sidebar_installer(
    *,
    state: Any,
    hub: AgentTranscriptHub,
    registry: MessageRendererRegistry,
    palette: Palette = DEFAULT_PALETTE,
    config: Any | None = None,
    runtime: Any | None = None,
    cwd: str | None = None,
    todo_provider: Callable[[], TodoState] | None = None,
    queue_count_provider: Callable[[], int] | None = None,
    statusline: StatusLineRunner | None = None,
    meter: ContextMeter | None = None,
) -> Callable[[MewboApp], None]:
    """Build the post-mount installer that registers the faceted sidebar.

    The sidebar splits into three clearly delineated, ordered sections via the
    existing ``SidebarSlotRegistry``: **Fleet** (the selectable hypervisor fleet,
    primary) · **Plan** (the tri-state todo dock) · **Context** (the context/cost
    gauge). Selecting a fleet row drills into that agent's transcript in place
    via the :class:`FleetDrillController`.

    Args:
        state: the ``CliState`` (read ``model_name`` / ``session_id``).
        hub: the :class:`AgentTranscriptHub` — the fleet rows + drill-in source.
        registry: the message renderer registry reused for drill-in rendering.
        palette: semantic colour palette for the fleet/drill widgets.
        config: the ``AppConfig`` (unused directly today; reserved for knobs).
        runtime: the ``SessionRuntime`` — used to read live token totals so the
            statusLine payload carries real ``context_window`` / ``tokens`` /
            ``cost`` (not zeros). Optional; payload falls back to zeros if absent.
        cwd: working dir for git-branch detection (default: process cwd).
        todo_provider: DI callable returning the live :class:`TodoState`.
        queue_count_provider: DI callable returning the queued count; the
            controller wires this to the input area's ``queued_count``.
        statusline: an explicit :class:`StatusLineRunner` (default: live-config).
        meter: an explicit :class:`ContextMeter` (default: live-config).

    Returns:
        An ``AppInstaller`` (``Callable[[MewboApp], None]``).
    """
    import os

    runner = statusline or StatusLineRunner()
    context_meter = meter or ContextMeter()
    work_dir = cwd or os.getcwd()

    def _install(app: MewboApp) -> None:
        # 1. Build the three section bodies + the in-place drill controller.
        drill = FleetDrillController(app=app, hub=hub, registry=registry, palette=palette)
        fleet = FleetPanel(
            rows_provider=hub.fleet_rows,
            on_select=drill.open,
            palette=palette,
            id="fleet-panel",
        )
        todo = TodoPanel(
            todo_provider=todo_provider,
            queue_count_provider=queue_count_provider,
            id="todo-panel",
        )
        status = StatusBar(meter=context_meter, id="status-bar")

        # Register the three faceted sections (Fleet primary, then Plan, Context).
        app.sidebar_slots.register(
            "fleet", lambda: SidebarSection("Fleet", fleet, id="sec-fleet"), order=10
        )
        app.sidebar_slots.register(
            "plan", lambda: SidebarSection("Plan", todo, id="sec-plan"), order=20
        )
        app.sidebar_slots.register(
            "context", lambda: SidebarSection("Context", status, id="sec-context"), order=30
        )

        # Expose for the controller's turn worker to drive thread-safely.
        app._fleet_panel = fleet
        app._todo_panel = todo
        app._status_bar = status
        app._drill_controller = drill
        app._context_meter = context_meter

        # drive the foot activity label off the ROOT throughput meter so
        # the live phase/tok-s/stall repaint on the transcript's 10 Hz spinner
        # tick (a hung agent visibly flips to "stalled" without any new event).
        transcript = getattr(app, "_transcript", None)
        setter = getattr(transcript, "set_activity_label_provider", None)
        if callable(setter):
            setter(hub.root_activity_label)

        # 2. Refresh the mounted sidebar so the late slots appear.
        from mewbo_cli.tui.widgets.sidebar import SidebarView

        sidebar = app.query_one(SidebarView)
        app.call_later(sidebar.refresh_slots)

        # 3. Seed the gauge (model only — it feeds the meter; branch/tokens live
        #    in the footer) + set the terminal title.
        model = getattr(state, "model_name", None)
        branch = _git_branch(work_dir)
        # Apply once the slot widget is actually mounted.
        app.call_later(lambda: status.apply_state(StatusState(model=model)))

        session_id = getattr(state, "session_id", None)
        title = f"Mewbo · {session_id[:8]}" if session_id else "Mewbo"
        # Defer the raw OSC write until after the first paint so it can't
        # interleave with Textual's own output during initial render.
        app.call_later(lambda: set_terminal_title(title))

        # 4. Start the statusLine refresh interval when configured.
        #
        # The user script is a blocking ``subprocess.run`` (up to the runner's
        # timeout), so it MUST NOT run on the event-loop thread — an async tick
        # offloads it via ``asyncio.to_thread`` so a slow script never freezes
        # the TUI. The first tick is scheduled (``call_later``), never called
        # inline during ``on_mount``.
        if runner.enabled:
            async def _tick() -> None:
                st = _build_statusline_state(
                    model=model,
                    work_dir=work_dir,
                    branch=branch,
                    session_id=session_id,
                    runtime=runtime,
                    meter=context_meter,
                )
                line = await asyncio.to_thread(runner.run, st)
                if line is not None:
                    # Keep it on the App's sub_title so it never fights the
                    # status bar's own vitals row.
                    app.sub_title = line.splitlines()[0] if line else ""

            app.set_interval(runner.interval, _tick)
            app.call_later(_tick)

    _install.__name__ = "sidebar_installer"
    return _install


def make_statusline_installer(
    *,
    state: Any,
    runtime: Any | None = None,
    hub: AgentTranscriptHub | None = None,
    cwd: str | None = None,
    model: str | None = None,
    interval: float = 3.0,
) -> Callable[[MewboApp], None]:
    """Build the post-mount installer that drives the docked :class:`StatusLine`.

    The IDE-style footer bar (host · model · cwd · branch/stash · session tokens)
    is composed by the App itself (a foundation layout element, like the
    keybinding ``Footer``); this installer feeds it the **live** facets that
    change over a session — git branch/stash and the running token totals — on a
    refresh interval. The git + event reads are offloaded to a worker thread
    (``asyncio.to_thread``) so a slow store/subprocess never freezes the TUI.

    Args:
        state: the ``CliState`` (read ``model_name`` / ``session_id``).
        runtime: the ``SessionRuntime`` for live token totals (optional → zeros).
        hub: the :class:`AgentTranscriptHub` — the ctx-gauge's live context-size
            source (``root_last_input_tokens()``). Optional; without it the
            gauge falls back to the cumulative session total (less accurate but
            never crashes).
        cwd: working dir for git facets (default: process cwd).
        model: the resolved display model used when ``state.model_name`` is unset
            (``--model`` is optional; the real model otherwise comes from config).
        interval: refresh cadence in seconds (floored at 1.0).
    """
    import os

    from mewbo_cli.tui.widgets.status_line import StatusLine, StatusLineData

    work_dir = cwd or os.getcwd()
    model_fallback = model
    period = max(1.0, float(interval))

    def _compute() -> StatusLineData:
        """Snapshot the live facets (runs on a worker thread).

        ``model``/``session_id`` are read off ``state`` *per tick* (not captured
        at construction): the model is resolved onto the state after installers
        are built, and ``/models`` can change it mid-session — both are reflected.
        ``state.model_name`` is only set by ``--model``, so fall back to the
        resolved display model otherwise.
        """
        model_name = getattr(state, "model_name", None) or model_fallback
        session_id = getattr(state, "session_id", None)
        input_tokens, output_tokens = _session_token_totals(runtime, session_id)
        return StatusLineData(
            model=model_name,
            cwd=work_dir,
            branch=_git_branch(work_dir),
            stash=_git_stash_count(work_dir),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def _install(app: MewboApp) -> None:
        try:
            line = app.query_one("#statusline", StatusLine)
        except Exception:
            return  # no status line in this App build → nothing to drive
        app._status_line = line

        async def _tick() -> None:
            data = await asyncio.to_thread(_compute)
            line.apply(data)
            # Drive the sidebar context/cost gauge from the same numbers so it
            # tracks the live session instead of the install-time seed (zeros).
            bar = getattr(app, "_status_bar", None)
            if bar is not None:
                # The ctx gauge wants the LIVE context size (the most recent
                # prompt), never the cumulative session total — the hub already
                # rolls that up per llm_call_end (root_last_input_tokens()).
                # Cost still uses the cumulative input/output below (unchanged).
                used = (
                    hub.root_last_input_tokens()
                    if hub is not None
                    else max(0, data.input_tokens) + max(0, data.output_tokens)
                )
                bar.apply_state(
                    StatusState(
                        model=data.model,
                        used_tokens=used,
                        input_tokens=data.input_tokens,
                        output_tokens=data.output_tokens,
                    )
                )

        app.set_interval(period, _tick)
        app.call_later(_tick)

    _install.__name__ = "statusline_installer"
    return _install


def _build_statusline_state(
    *,
    model: str | None,
    work_dir: str,
    branch: str | None,
    session_id: str | None,
    runtime: Any | None,
    meter: ContextMeter,
) -> StatusLineState:
    """Assemble the statusLine payload state with REAL live fields.

    Reads token totals off the runtime's session events (best-effort) and
    derives the context-window usage + cost via the injected
    :class:`ContextMeter`, so the user's script sees real numbers rather than
    zeros. Falls back to zeros only when no runtime/session is available.
    """
    input_tokens, output_tokens = _session_token_totals(runtime, session_id)
    used = input_tokens + output_tokens
    usage = meter.usage(used_tokens=used, model=model)
    cost = meter.cost(input_tokens=input_tokens, output_tokens=output_tokens, model=model)
    return StatusLineState(
        model=model,
        provider=_provider_of(model),
        cwd=work_dir,
        git_branch=branch,
        context_used=usage.used,
        context_total=usage.total,
        context_percent=usage.percent,
        context_estimated=usage.estimated,
        cost=cost,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        session_id=session_id,
    )


# ---------------------------------------------------------------------------
# /context command — token-attribution breakdown
# ---------------------------------------------------------------------------


def cmd_context(context: Any, args: list[str]) -> bool:
    """``/context`` — print a token-attribution breakdown for the session.

    A standalone command handler the controller registers via
    ``REGISTRY.command("/context", "...")(cmd_context)`` (this module does NOT
    edit ``cli_commands.py``). Reads the live agent token totals off the
    runtime's session events and renders context-window usage + cost.

    Signature matches the ``CommandRegistry`` contract:
    ``(context: CommandContext, args: list[str]) -> bool`` (return ``True`` to
    keep the session running).
    """
    console = getattr(context, "console", None)
    state = getattr(context, "state", None)
    runtime = getattr(context, "runtime", None)
    model = getattr(state, "model_name", None)
    session_id = getattr(state, "session_id", None)

    input_tokens, output_tokens = _session_token_totals(runtime, session_id)
    used = input_tokens + output_tokens

    meter = ContextMeter()
    usage = meter.usage(used_tokens=used, model=model)
    cost = meter.cost(input_tokens=input_tokens, output_tokens=output_tokens, model=model)

    prefix = "~" if usage.estimated else ""
    lines = [
        "Context attribution",
        f"  model        {model or '—'}",
        f"  window       {prefix}{usage.total:,} tokens"
        + (" (estimated)" if usage.estimated else ""),
        f"  used         {used:,} tokens ({prefix}{usage.percent:.1f}%)",
        f"    input      {input_tokens:,}",
        f"    output     {output_tokens:,}",
        f"  cost         {ContextMeter.format_cost(cost)}",
    ]
    if usage.over_warning:
        lines.append("  ! over 80% — consider /compact to reclaim context")

    text = "\n".join(lines)
    if console is not None:
        console.print(text)
    else:  # pragma: no cover - console always present in real CLI runs
        print(text)
    return True


def _session_token_totals(runtime: Any, session_id: str | None) -> tuple[int, int]:
    """Best-effort sum of input/output tokens from the session's events.

    Reads ``token_usage``-shaped events off the runtime; returns ``(0, 0)`` when
    the runtime cannot supply them (never raises).
    """
    if runtime is None or not session_id:
        return 0, 0
    try:
        events = runtime.load_events(session_id)
    except Exception:
        return 0, 0
    total_in = 0
    total_out = 0
    for ev in events or []:
        payload = ev.get("payload") if isinstance(ev, dict) else None
        if not isinstance(payload, dict):
            continue
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else payload
        if not isinstance(usage, dict):
            continue
        in_tok = usage.get("input_tokens") or usage.get("prompt_tokens")
        out_tok = usage.get("output_tokens") or usage.get("completion_tokens")
        if isinstance(in_tok, (int, float)):
            total_in += int(in_tok)
        if isinstance(out_tok, (int, float)):
            total_out += int(out_tok)
    return total_in, total_out


__all__ = ["cmd_context", "make_sidebar_installer", "make_statusline_installer"]
