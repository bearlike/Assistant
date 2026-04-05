#!/usr/bin/env python3
"""Immutable agent context propagated through the agent hierarchy.

``AgentContext`` is the per-agent state carried by every ``ToolUseLoop``
instance. The root agent creates one via ``AgentContext.root()``; child
agents receive one via ``parent_ctx.child()``.

The hypervisor control plane (``AgentHypervisor``, ``AgentHandle``) lives
in :mod:`meeseeks_core.hypervisor`.
"""

from __future__ import annotations

import queue
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from meeseeks_core.hypervisor import AgentHandle, AgentHypervisor, AgentRegistry, AgentStatus

if TYPE_CHECKING:
    from meeseeks_core.types import Event


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
    registry: AgentHypervisor
    fallback_models: tuple[str, ...] = ()
    message_queue: queue.Queue[str] | None = None
    interrupt_step: threading.Event | None = None

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
            fallback_models=self.fallback_models,
            should_cancel=self.should_cancel,
            event_logger=self.event_logger,
            registry=self.registry,
            # Ref: [DeepMind-Delegation §4.4] Bidirectional message passing —
            # each agent gets its own queue for parent→child steering.
            # Ref: [AgentCgroup §4.2] System→agent NL feedback channel.
            message_queue=queue.Queue(),
            interrupt_step=None,
        )

    @staticmethod
    def root(
        *,
        model_name: str,
        max_depth: int = 5,
        fallback_models: tuple[str, ...] = (),
        should_cancel: Callable[[], bool] | None = None,
        event_logger: Callable[[Event], None] | None = None,
        registry: AgentHypervisor | None = None,
        message_queue: queue.Queue[str] | None = None,
        interrupt_step: threading.Event | None = None,
    ) -> AgentContext:
        """Create the root agent context."""
        reg = registry or AgentHypervisor()
        return AgentContext(
            agent_id=uuid.uuid4().hex[:12],
            parent_id=None,
            depth=0,
            max_depth=max_depth,
            model_name=model_name,
            fallback_models=fallback_models,
            should_cancel=should_cancel,
            event_logger=event_logger,
            registry=reg,
            message_queue=message_queue or queue.Queue(),
            interrupt_step=interrupt_step or threading.Event(),
        )


# Re-export hypervisor types for backwards compatibility.
__all__ = [
    "AgentContext",
    "AgentDepthExceeded",
    "AgentHandle",
    "AgentHypervisor",
    "AgentRegistry",
    "AgentStatus",
]
