#!/usr/bin/env python3
"""Real-time agent tree display for the CLI.

Thread-safe bridge between the async AgentHypervisor lifecycle hooks and
the Rich Live rendering loop. Updated by ``HookManager.on_agent_start``
/ ``on_agent_stop`` callbacks; queried by Rich Live's refresh thread.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from meeseeks_core.hypervisor import AgentHandle
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text


@dataclass
class AgentDisplayState:
    """Snapshot of one agent's display state."""

    agent_id: str
    parent_id: str | None
    depth: int
    model: str
    task: str
    status: str  # running / completed / failed / cancelled
    steps: int = 0
    last_tool: str | None = None


class AgentDisplayManager:
    """Thread-safe agent display state for Rich Live rendering.

    Call ``on_start`` / ``on_stop`` from hooks.  Rich Live calls
    ``render()`` from its background refresh thread.
    """

    def __init__(self) -> None:
        """Initialize empty display state."""
        self._lock = threading.Lock()
        self._agents: dict[str, AgentDisplayState] = {}

    # ------------------------------------------------------------------
    # Hook callbacks (called from async context / worker threads)
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
                status="running",
            )

    def on_stop(self, handle: AgentHandle) -> None:
        """Update an agent's terminal status."""
        with self._lock:
            state = self._agents.get(handle.agent_id)
            if state:
                state.status = handle.status
                state.steps = handle.steps_completed
                state.last_tool = handle.last_tool_id

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    @property
    def has_agents(self) -> bool:
        """True if any agents are tracked."""
        with self._lock:
            return bool(self._agents)

    @property
    def agent_count(self) -> int:
        """Number of tracked agents."""
        with self._lock:
            return len(self._agents)

    # ------------------------------------------------------------------
    # Rendering (called from Rich Live's refresh thread)
    # ------------------------------------------------------------------

    def render(self) -> RenderableType:
        """Produce a Rich renderable of the current agent tree."""
        with self._lock:
            agents = list(self._agents.values())

        if not agents:
            return Text("")

        # Build parent→children map.
        children_map: dict[str | None, list[AgentDisplayState]] = {}
        for agent in agents:
            children_map.setdefault(agent.parent_id, []).append(agent)

        roots = [a for a in agents if a.parent_id is None]
        lines: list[Text] = []

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

        return Panel(
            Group(*lines),
            title=":robot: Agents",
            border_style="blue",
            padding=(0, 1),
        )


def _format_agent_line(agent: AgentDisplayState, prefix: str) -> Text:
    """Format a single agent status line."""
    line = Text()
    line.append(prefix)

    # Status indicator.
    _STATUS_STYLE = {
        "running": ("● ", "green"),
        "completed": ("✓ ", "bold green"),
        "failed": ("✗ ", "bold red"),
        "cancelled": ("⊘ ", "yellow"),
    }
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
    elif agent.status == "cancelled":
        line.append("  cancelled", style="dim yellow")

    return line


__all__ = ["AgentDisplayManager", "AgentDisplayState"]
