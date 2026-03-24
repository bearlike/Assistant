#!/usr/bin/env python3
"""Agent hypervisor control plane.

This module implements the centralized control plane for Meeseeks' multi-agent
execution model. The hypervisor is responsible for:

- **Admission control** — bounding concurrent agents via an asyncio semaphore
  to prevent resource exhaustion from runaway spawning.
- **Lifecycle tracking** — maintaining an ``AgentHandle`` for every running
  agent with status, step counts, timing, and error state.
- **Structured cancellation** — cancelling individual agents or the entire
  tree, with guaranteed cleanup via ``asyncio.gather``.
- **Visibility** — exposing the live agent tree to the CLI display and API
  via query methods (``list_children``, ``list_all``, etc.).

The hypervisor is instantiated once per session in ``Orchestrator`` and shared
across all agents in the hierarchy via ``AgentContext.registry``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from meeseeks_core.spawn_agent import AgentError

AgentStatus = Literal["running", "completed", "failed", "cancelled"]


@dataclass(slots=True)
class AgentHandle:
    """Mutable runtime state for a single agent — lives in the hypervisor.

    Created when an agent registers and updated throughout its lifecycle.
    Fields are read by the CLI agent tree display and the hypervisor's
    query/cancellation methods.
    """

    agent_id: str
    parent_id: str | None
    depth: int
    model_name: str
    task_description: str
    status: AgentStatus = "running"
    started_at: float = field(default_factory=time.monotonic)
    stopped_at: float | None = None
    steps_completed: int = 0
    last_tool_id: str | None = None
    error: str | AgentError | None = None
    asyncio_task: asyncio.Task[object] | None = None


class AgentHypervisor:
    """Hypervisor control plane — manages the full agent tree for a session.

    Thread-safe via ``asyncio.Lock``. A single instance is shared across all
    agents in the hierarchy through ``AgentContext.registry``.

    Responsibilities:
        - **Admission control**: ``admit()`` / ``release()`` gate concurrency
          via an ``asyncio.Semaphore`` (default 20 slots).
        - **Registration**: ``register()`` / ``unregister()`` track agent
          handles keyed by ``agent_id``.
        - **Status**: ``update_step()`` / ``mark_done()`` record execution
          progress and terminal state.
        - **Queries**: ``list_children()`` / ``list_descendants()`` /
          ``list_all()`` expose the live tree for display and introspection.
        - **Cancellation**: ``cancel_agent()`` cancels a single agent;
          ``cleanup()`` tears down the entire tree on session exit.
    """

    def __init__(self, *, max_concurrent: int = 20) -> None:
        """Initialize hypervisor with a concurrency limit."""
        self._agents: dict[str, AgentHandle] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(max_concurrent)

    # ------------------------------------------------------------------
    # Admission control
    # ------------------------------------------------------------------

    async def admit(self) -> bool:
        """Acquire a concurrency slot. Returns False on timeout."""
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=30.0)
            return True
        except asyncio.TimeoutError:
            return False

    def release(self) -> None:
        """Release a concurrency slot after an agent completes."""
        self._semaphore.release()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    async def register(self, handle: AgentHandle) -> None:
        """Register a new agent in the hypervisor."""
        async with self._lock:
            self._agents[handle.agent_id] = handle

    async def unregister(self, agent_id: str) -> AgentHandle | None:
        """Remove an agent from the hypervisor."""
        async with self._lock:
            handle = self._agents.pop(agent_id, None)
            if handle and handle.status == "running":
                handle.status = "completed"
                handle.stopped_at = time.monotonic()
            return handle

    # ------------------------------------------------------------------
    # Status updates
    # ------------------------------------------------------------------

    async def update_step(self, agent_id: str, tool_id: str) -> None:
        """Record a completed tool execution step."""
        async with self._lock:
            handle = self._agents.get(agent_id)
            if handle:
                handle.steps_completed += 1
                handle.last_tool_id = tool_id

    async def mark_done(
        self,
        agent_id: str,
        status: AgentStatus,
        error: str | AgentError | None = None,
    ) -> None:
        """Mark an agent as done with a terminal status."""
        async with self._lock:
            handle = self._agents.get(agent_id)
            if handle:
                handle.status = status
                handle.error = error
                handle.stopped_at = time.monotonic()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def get(self, agent_id: str) -> AgentHandle | None:
        """Return a single agent handle, or None."""
        async with self._lock:
            return self._agents.get(agent_id)

    async def list_children(self, parent_id: str) -> list[AgentHandle]:
        """List direct children of a parent. Enforces isolation."""
        async with self._lock:
            return [h for h in self._agents.values() if h.parent_id == parent_id]

    async def list_descendants(self, ancestor_id: str) -> list[AgentHandle]:
        """List all descendants recursively."""
        async with self._lock:
            result: list[AgentHandle] = []
            queue = [ancestor_id]
            while queue:
                pid = queue.pop()
                for h in self._agents.values():
                    if h.parent_id == pid:
                        result.append(h)
                        queue.append(h.agent_id)
            return result

    async def list_all(self) -> list[AgentHandle]:
        """Full tree view — for CLI/API user visibility."""
        async with self._lock:
            return list(self._agents.values())

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    async def cancel_agent(self, agent_id: str) -> bool:
        """Cancel a running agent by cancelling its asyncio task."""
        async with self._lock:
            handle = self._agents.get(agent_id)
            if handle and handle.asyncio_task and not handle.asyncio_task.done():
                handle.asyncio_task.cancel()
                handle.status = "cancelled"
                handle.stopped_at = time.monotonic()
                return True
            return False

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def cleanup(self, timeout: float = 5.0) -> None:
        """Cancel all agents with graceful escalation.

        Phase 1: Request cancellation on all running asyncio tasks.
        Phase 2: Wait up to *timeout* seconds for tasks to finish.
        Phase 3: Force-mark any still-pending agents as cancelled.
        """
        async with self._lock:
            running = [
                h for h in self._agents.values() if h.status == "running"
            ]

        if not running:
            async with self._lock:
                self._agents.clear()
            return

        # Phase 1: Request cancellation.
        for handle in running:
            if handle.asyncio_task and not handle.asyncio_task.done():
                handle.asyncio_task.cancel()

        # Phase 2: Wait with timeout.
        tasks = [
            h.asyncio_task for h in running
            if h.asyncio_task and not h.asyncio_task.done()
        ]
        if tasks:
            done, pending = await asyncio.wait(
                tasks, timeout=timeout, return_when=asyncio.ALL_COMPLETED,
            )

            # Phase 3: Force-mark any still-pending as cancelled.
            for task in pending:
                for handle in running:
                    if handle.asyncio_task is task:
                        async with self._lock:
                            if handle.status == "running":
                                handle.status = "cancelled"
                                handle.error = "Force-cancelled after timeout"
                                handle.stopped_at = time.monotonic()

        # Final sweep: mark any remaining running agents and clear.
        async with self._lock:
            for h in list(self._agents.values()):
                if h.status == "running":
                    h.status = "cancelled"
                    h.stopped_at = time.monotonic()
            self._agents.clear()


# Backwards-compatible alias.
AgentRegistry = AgentHypervisor

__all__ = [
    "AgentHandle",
    "AgentHypervisor",
    "AgentRegistry",
    "AgentStatus",
]
