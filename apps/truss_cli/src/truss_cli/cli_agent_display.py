#!/usr/bin/env python3
"""Real-time agent tree display for the CLI.

Thread-safe bridge between the async AgentHypervisor lifecycle hooks and
the Rich Live rendering loop.  Updated by ``HookManager.on_agent_start``
/ ``on_agent_stop`` callbacks and ``pre_tool_use`` / ``post_tool_use``
hooks; queried by Rich Live's refresh thread at 4 fps.

Features:
- Collapsible agent tree (Ctrl+O toggle during Live rendering)
- Spinner animation integrated into the Live renderable
- Elapsed time per agent and in the status footer
- Token count display (when core surfaces the data)
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text
from truss_core.classes import ActionStep
from truss_core.common import MockSpeaker
from truss_core.hypervisor import AgentHandle

# Braille spinner frames — cycles at 4 fps via Live refresh.
_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


@dataclass
class AgentDisplayState:
    """Snapshot of one agent's display state."""

    agent_id: str
    parent_id: str | None
    depth: int
    model: str
    task: str
    status: str  # running / completed / failed / cancelled / submitted / rejected
    steps: int = 0
    last_tool: str | None = None
    started_at: float = 0.0  # time.monotonic() captured from AgentHandle
    token_count: int = 0  # placeholder — rendered only when > 0
    error: str | None = None  # Error message on failure
    last_step_at: float | None = None  # For staleness detection
    stopped_at: float | None = None  # For duration after stop


class AgentDisplayManager:
    """Thread-safe agent display state for Rich Live rendering.

    Hook callbacks:
    - ``on_start`` / ``on_stop`` — registered on ``HookManager.on_agent_start``
      / ``on_agent_stop`` for sub-agent lifecycle.
    - ``on_tool_start`` / ``on_tool_end`` — registered on
      ``HookManager.pre_tool_use`` / ``post_tool_use`` for spinner display.

    Rich Live calls ``render()`` from its background refresh thread.
    """

    def __init__(self) -> None:
        """Initialize empty display state."""
        self._lock = threading.Lock()
        self._agents: dict[str, AgentDisplayState] = {}
        self._expanded: bool = True  # start expanded; Ctrl+O toggles
        self._spinner = itertools.cycle(_SPINNER_FRAMES)
        self._root_tool: str | None = None
        self._root_tool_start: float = 0.0

    # ------------------------------------------------------------------
    # Agent lifecycle hooks (on_agent_start / on_agent_stop)
    # ------------------------------------------------------------------

    def on_start(self, handle: AgentHandle) -> None:
        """Register a new agent in the display."""
        model_short = handle.model_name.rsplit("/", 1)[-1]
        with self._lock:
            self._agents[handle.agent_id] = AgentDisplayState(
                agent_id=handle.agent_id,
                parent_id=handle.parent_id,
                depth=handle.depth,
                model=model_short,
                task=handle.task_description[:60],
                status=handle.status,  # Will be "submitted" initially
                started_at=handle.started_at,
            )

    def on_stop(self, handle: AgentHandle) -> None:
        """Update an agent's terminal status."""
        with self._lock:
            state = self._agents.get(handle.agent_id)
            if state:
                state.status = handle.status
                state.steps = handle.steps_completed
                state.last_tool = handle.last_tool_id
                state.error = str(handle.error)[:100] if handle.error else None
                state.stopped_at = handle.stopped_at
                state.token_count = handle.input_tokens + handle.output_tokens

    # ------------------------------------------------------------------
    # Tool execution hooks (pre_tool_use / post_tool_use)
    # ------------------------------------------------------------------

    def on_tool_start(self, action_step: ActionStep) -> ActionStep:
        """Track current tool execution for the spinner line."""
        with self._lock:
            self._root_tool = action_step.tool_id
            self._root_tool_start = time.monotonic()
        return action_step

    def on_tool_end(self, action_step: ActionStep, result: MockSpeaker) -> MockSpeaker:
        """Clear tool execution state after completion."""
        with self._lock:
            self._root_tool = None
        return result

    # ------------------------------------------------------------------
    # Controls
    # ------------------------------------------------------------------

    def toggle_expand(self) -> None:
        """Flip collapsed/expanded state (bound to Ctrl+O)."""
        with self._lock:
            self._expanded = not self._expanded

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    @property
    def has_agents(self) -> bool:
        """True if any agents are tracked."""
        with self._lock:
            return bool(self._agents)

    @property
    def has_activity(self) -> bool:
        """True if there is anything to render (agents or active tool)."""
        with self._lock:
            return bool(self._agents) or self._root_tool is not None

    @property
    def agent_count(self) -> int:
        """Number of tracked agents."""
        with self._lock:
            return len(self._agents)

    # ------------------------------------------------------------------
    # Rendering (called from Rich Live's refresh thread at 4 fps)
    # ------------------------------------------------------------------

    def render(self) -> RenderableType:
        """Produce a Rich renderable of the current agent tree."""
        frame = next(self._spinner)

        with self._lock:
            agents = list(self._agents.values())
            root_tool = self._root_tool
            root_tool_start = self._root_tool_start
            expanded = self._expanded

        if not agents and not root_tool:
            return Text("")

        lines: list[Text] = []
        now = time.monotonic()

        # --- Agent tree or collapsed summary ---
        if agents:
            if expanded:
                _build_tree_lines(agents, lines)
            else:
                lines.append(_build_collapsed_summary(agents, now))

        # --- Footer: spinner + deepest active work ---
        running_agents = [a for a in agents if a.status == "running"]
        if running_agents:
            deepest = max(running_agents, key=lambda a: a.depth)
            elapsed = _format_elapsed(now - deepest.started_at)
            footer = Text()
            footer.append(f"{frame} ", style="green")
            footer.append(deepest.task, style="dim cyan")
            footer.append(f"  ({elapsed})", style="dim")
            lines.append(footer)
        elif root_tool:
            footer = Text()
            footer.append(f"{frame} ", style="green")
            footer.append(f"Running {root_tool}...", style="dim cyan")
            if root_tool_start:
                footer.append(f"  {_format_elapsed(now - root_tool_start)}", style="dim")
            lines.append(footer)

        if not lines:
            return Text("")

        # Wrap in a panel when there is an agent tree; bare line otherwise.
        if agents:
            return Panel(
                Group(*lines),
                title=":robot: Agents",
                border_style="blue",
                padding=(0, 1),
            )
        return Group(*lines)


# ======================================================================
# Module-level helpers
# ======================================================================


def _format_elapsed(seconds: float) -> str:
    """Format *seconds* as a compact elapsed string."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds) // 60
    secs = int(seconds) % 60
    return f"{minutes}m {secs:02d}s"


def _build_tree_lines(agents: list[AgentDisplayState], lines: list[Text]) -> None:
    """Append depth-first tree lines for *agents* to *lines*."""
    children_map: dict[str | None, list[AgentDisplayState]] = {}
    for agent in agents:
        children_map.setdefault(agent.parent_id, []).append(agent)

    roots = [a for a in agents if a.parent_id is None]

    def _walk(parent_id: str | None, prefix: str) -> None:
        siblings = children_map.get(parent_id, [])
        for i, child in enumerate(siblings):
            is_last = i == len(siblings) - 1
            connector = "\u2514\u2500 " if is_last else "\u251c\u2500 "
            next_prefix = prefix + ("   " if is_last else "\u2502  ")
            lines.append(_format_agent_line(child, prefix + connector))
            _walk(child.agent_id, next_prefix)

    for root in roots:
        lines.append(_format_agent_line(root, ""))
        _walk(root.agent_id, "")


def _build_collapsed_summary(agents: list[AgentDisplayState], now: float) -> Text:
    """Build a single summary line for the collapsed view."""
    running = sum(1 for a in agents if a.status == "running")
    done = len(agents) - running
    earliest = min((a.started_at for a in agents if a.started_at), default=now)
    elapsed = _format_elapsed(now - earliest)

    summary = Text()
    summary.append("● ", style="green")
    parts: list[str] = []
    if running:
        parts.append(f"{running} running")
    if done:
        parts.append(f"{done} done")
    summary.append(f"{len(agents)} agents: {', '.join(parts)}", style="bold")
    summary.append(f"  ({elapsed})", style="dim")
    summary.append("  ctrl+o expand", style="dim italic")
    return summary


_STATUS_STYLE: dict[str, tuple[str, str]] = {
    "running": ("● ", "green"),
    "completed": ("✓ ", "bold green"),
    "failed": ("✗ ", "bold red"),
    "cancelled": ("⊘ ", "yellow"),
    "submitted": ("⏳ ", "dim"),
    "rejected": ("⊘ ", "red"),
}


def _format_agent_line(agent: AgentDisplayState, prefix: str) -> Text:
    """Format a single agent status line."""
    line = Text()
    line.append(prefix)

    # Status indicator.
    indicator, style = _STATUS_STYLE.get(agent.status, ("? ", "dim"))
    line.append(indicator, style=style)

    # Agent ID (short) + model.
    line.append(agent.agent_id[:8], style="bold")
    line.append(f" ({agent.model})", style="dim")

    # Status detail.
    if agent.status == "running":
        detail = f"  step {agent.steps}"
        if agent.last_tool:
            detail += f"  \u25b8 {agent.last_tool}"
        line.append(detail, style="dim cyan")
    elif agent.status == "completed":
        line.append(f"  done  {agent.steps} steps", style="dim green")
    elif agent.status == "failed":
        line.append("  failed", style="dim red")
        if agent.error:
            line.append(f" \u2014 {agent.error[:80]}", style="dim red")
    elif agent.status == "cancelled":
        line.append("  cancelled", style="dim yellow")
    elif agent.status == "submitted":
        line.append("  queued", style="dim")
    elif agent.status == "rejected":
        line.append("  rejected", style="dim red")
        if agent.error:
            line.append(f" \u2014 {agent.error[:80]}", style="dim red")

    # Elapsed time.
    if agent.started_at:
        line.append(f"  {_format_elapsed(time.monotonic() - agent.started_at)}", style="dim")

    # Token count (future — renders only when non-zero).
    if agent.token_count > 0:
        count = agent.token_count
        tok = f"{count / 1000:.1f}k" if count >= 1000 else str(count)
        line.append(f"  {tok} tokens", style="dim")

    return line


__all__ = ["AgentDisplayManager", "AgentDisplayState"]
