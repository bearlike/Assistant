#!/usr/bin/env python3
"""Agent hypervisor: context propagation, lifecycle tracking, and registry.

AgentContext is the immutable context passed through the agent hierarchy.
AgentRegistry is the hypervisor control plane — tracks all agents, enforces
admission control, and guarantees cleanup.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from meeseeks_core.types import Event

AgentStatus = Literal["running", "completed", "failed", "cancelled"]


class AgentDepthExceeded(Exception):
    """Raised when attempting to spawn beyond max_depth."""

    def __init__(self, attempted: int, maximum: int) -> None:
        """Initialize with the attempted and maximum depth values."""
        super().__init__(f"Agent depth {attempted} exceeds maximum {maximum}")
        self.attempted = attempted
        self.maximum = maximum


@dataclass(frozen=True, slots=True)
class AgentContext:
    """Immutable context propagated through the agent hierarchy.

    Every ToolUseLoop instance requires an AgentContext. The root agent
    creates one via ``AgentContext.root()``. Child agents receive one
    via ``parent_ctx.child()``.
    """

    agent_id: str
    parent_id: str | None
    depth: int
    max_depth: int
    model_name: str
    should_cancel: Callable[[], bool] | None
    event_logger: Callable[[Event], None] | None
    registry: AgentRegistry
    message_queue: asyncio.Queue[str] | None = None
    interrupt_step: asyncio.Event | None = None

    @property
    def can_spawn(self) -> bool:
        """True if this agent is allowed to create children."""
        return self.depth < self.max_depth

    @property
    def remaining_depth(self) -> int:
        """Number of spawn levels remaining below this agent."""
        return max(0, self.max_depth - self.depth)

    def child(self, *, model_name: str | None = None) -> AgentContext:
        """Create a child context with depth+1.

        Raises:
            AgentDepthExceeded: If ``depth + 1 > max_depth``.
        """
        next_depth = self.depth + 1
        if next_depth > self.max_depth:
            raise AgentDepthExceeded(next_depth, self.max_depth)
        return AgentContext(
            agent_id=uuid.uuid4().hex[:12],
            parent_id=self.agent_id,
            depth=next_depth,
            max_depth=self.max_depth,
            model_name=model_name or self.model_name,
            should_cancel=self.should_cancel,
            event_logger=self.event_logger,
            registry=self.registry,
            message_queue=None,
            interrupt_step=None,
        )

    @staticmethod
    def root(
        *,
        model_name: str,
        max_depth: int = 5,
        should_cancel: Callable[[], bool] | None = None,
        event_logger: Callable[[Event], None] | None = None,
        registry: AgentRegistry | None = None,
    ) -> AgentContext:
        """Create the root agent context."""
        reg = registry or AgentRegistry()
        return AgentContext(
            agent_id=uuid.uuid4().hex[:12],
            parent_id=None,
            depth=0,
            max_depth=max_depth,
            model_name=model_name,
            should_cancel=should_cancel,
            event_logger=event_logger,
            registry=reg,
            message_queue=asyncio.Queue(),
            interrupt_step=asyncio.Event(),
        )


@dataclass(slots=True)
class AgentHandle:
    """Mutable state for a running agent — lives in the registry."""

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
    error: str | None = None
    asyncio_task: asyncio.Task[object] | None = None


class AgentRegistry:
    """Hypervisor control plane — tracks all agents in the tree.

    Thread-safe via ``asyncio.Lock``. Shared across all agents via
    ``AgentContext.registry``.
    """

    def __init__(self, *, max_concurrent: int = 20) -> None:
        """Initialize registry with a concurrency limit."""
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
        """Register a new agent in the registry."""
        async with self._lock:
            self._agents[handle.agent_id] = handle

    async def unregister(self, agent_id: str) -> AgentHandle | None:
        """Remove an agent from the registry."""
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
        error: str | None = None,
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
        """Cancel all running agents and await completion."""
        async with self._lock:
            running = [
                h
                for h in self._agents.values()
                if h.asyncio_task and not h.asyncio_task.done()
            ]
            for h in running:
                if h.asyncio_task:
                    h.asyncio_task.cancel()

        if running:
            tasks = [h.asyncio_task for h in running if h.asyncio_task]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        async with self._lock:
            for h in list(self._agents.values()):
                if h.status == "running":
                    h.status = "cancelled"
                    h.stopped_at = time.monotonic()
            self._agents.clear()


__all__ = [
    "AgentContext",
    "AgentDepthExceeded",
    "AgentHandle",
    "AgentRegistry",
    "AgentStatus",
]
