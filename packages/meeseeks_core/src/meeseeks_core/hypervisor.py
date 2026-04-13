#!/usr/bin/env python3
"""Agent hypervisor — active governor for multi-agent delegation.

The hypervisor is the centralized control plane that manages the full agent
tree for a session. It acts as an **active governor** (not just a registry),
mediating every delegation decision and result handoff.

Scientific grounding:
- Ref: [DeepMind-Delegation §4.4] Adaptive coordination cycle — the hypervisor
  monitors agents and intervenes via NL feedback when triggers fire.
- Ref: [DeepMind-Delegation §4.5] Structural transparency — configurable
  monitoring with lifecycle events at each phase transition.
- Ref: [AgentCgroup §4.2] Graduated enforcement — warn, throttle, feedback
  (never kill first; killing destroys 31-48% of accumulated context).
- Ref: [A2A v1.0] Task state machine — agents progress through
  submitted → running → completed/failed/cancelled/rejected.
- Ref: [CoA §3.1] Communication Units — structured AgentResult with
  compressed summary field enables inter-agent context passing.

Responsibilities:
- **Admission control** — bounding concurrent agents via semaphore.
- **Lifecycle tracking** — AgentHandle with delegation phase awareness.
- **Active monitoring** — budget tracking, stall detection, NL interventions.
- **Global eye** — render_agent_tree() gives the root agent a live view.
- **Bidirectional messaging** — send_message() enables parent→child steering.
- **Structured results** — AgentResult carries Communication Units between agents.
- **Graceful shutdown** — 3-phase escalation: cancel → wait → force-mark.
"""

from __future__ import annotations

import asyncio
import queue
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from meeseeks_core.spawn_agent import AgentError

# Ref: [A2A v1.0] Task state machine with terminal states being absorbing.
# Expanded from 4 to 6 states: added 'submitted' (pre-execution) and
# 'rejected' (declined at admission).
AgentStatus = Literal[
    "submitted",  # Registered, awaiting execution start
    "running",  # Actively executing tools
    "completed",  # Finished successfully (terminal)
    "failed",  # Finished with error (terminal)
    "cancelled",  # Cancelled by parent/timeout (terminal)
    "rejected",  # Declined at admission (terminal)
]


# Ref: [CoA §3.1] Communication Units compress inter-agent context.
# AgentResult.summary is the CU — a compressed synthesis of what the
# sub-agent learned, suitable for passing to sibling/parent agents.
@dataclass
class AgentResult:
    """Structured result from a sub-agent — the Communication Unit.

    Ref: [CoA §3.1] Each agent produces a CU that grows with relevant info
    and drops irrelevant content, preventing context explosion in chains.
    Ref: [Aletheia §3] ``cannot_solve`` status enables explicit failure
    admission as a first-class outcome, saving downstream waste.
    Ref: [DeepMind-Delegation §6.1] ``summary`` serves as a checkpoint
    snapshot — even on failure, partial work survives for retry.
    """

    content: str  # Primary output text
    status: str  # completed | failed | partial | cannot_solve
    steps_used: int  # Tool steps consumed
    artifacts: list[str] = field(default_factory=list)  # Files touched
    warnings: list[str] = field(default_factory=list)  # Non-fatal issues
    summary: str = ""  # Compressed CU for parent context


@dataclass(slots=True)
class AgentHandle:
    """Mutable runtime state for a single agent — lives in the hypervisor.

    Created when an agent registers and updated throughout its lifecycle.
    Fields are read by the CLI agent tree display and the hypervisor's
    query/cancellation methods.

    Ref: [A2A v1.0] Handle starts as ``submitted``, transitions to
    ``running`` when the loop begins, then to a terminal state.
    Ref: [AgentCgroup §4.2] ``last_step_at`` enables tool-call-granularity
    stall detection without destroying accumulated context.
    Ref: [DeepMind-Delegation §4.4] ``message_queue`` enables bidirectional
    adaptive coordination — the hypervisor injects NL feedback.
    """

    agent_id: str
    parent_id: str | None
    depth: int
    model_name: str
    task_description: str
    status: AgentStatus = "submitted"  # Ref: [A2A v1.0] Start as submitted
    started_at: float = field(default_factory=time.monotonic)
    stopped_at: float | None = None
    steps_completed: int = 0
    last_tool_id: str | None = None
    # Ref: [AgentCgroup §4.2] Tool-call-granularity timing for stall detection
    last_step_at: float | None = None
    error: str | AgentError | None = None
    asyncio_task: asyncio.Task[object] | None = None
    # Ref: [DeepMind-Delegation §4.4] Bidirectional message passing
    message_queue: queue.Queue[str] | None = None
    # Ref: [CoA §3.1] Completed CU stored on handle for async retrieval
    result: AgentResult | None = None
    # Ref: [DeepMind-Delegation §4.5] Auto-updated progress for monitoring
    progress_note: str | None = None
    # Context compaction tracking — visible in agent tree rendering.
    compaction_count: int = 0
    last_compacted_at: float | None = None
    # Token usage — accumulated from LLM response.usage_metadata per call.
    # Written only by the owning ToolUseLoop coroutine; read by CLI/API.
    input_tokens: int = 0
    output_tokens: int = 0
    # Signaled when agent reaches a terminal state (completed/failed/cancelled).
    done_event: asyncio.Event = field(default_factory=asyncio.Event)


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

    def __init__(
        self,
        *,
        max_concurrent: int = 20,
        session_step_budget: int = 0,
    ) -> None:
        """Initialize hypervisor with concurrency and budget limits.

        Ref: [AgentCgroup §4.2] Session-wide budget with graduated enforcement.

        Args:
            max_concurrent: Maximum number of concurrent agents (semaphore slots).
            session_step_budget: Total tool steps allowed across all agents in the
                session. 0 means unlimited.
        """
        self._agents: dict[str, AgentHandle] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(max_concurrent)
        # Ref: [AgentCgroup §4.2] Session-wide resource tracking
        self._total_steps: int = 0
        self._session_step_budget: int = session_step_budget

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
        """Record a completed tool execution step.

        Ref: [AgentCgroup §4.2] Track at tool-call granularity, not agent
        granularity. Updates last_step_at for stall detection and total_steps
        for session budget enforcement.
        """
        async with self._lock:
            handle = self._agents.get(agent_id)
            if handle:
                handle.steps_completed += 1
                handle.last_tool_id = tool_id
                handle.last_step_at = time.monotonic()
                self._total_steps += 1

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
                handle.done_event.set()

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
    # Budget & monitoring  (Ref: [AgentCgroup §4.2], [DeepMind-Delegation §4.4])
    # ------------------------------------------------------------------

    @property
    def total_steps(self) -> int:
        """Total tool steps executed across all agents in the session."""
        return self._total_steps

    def budget_exhausted(self) -> bool:
        """Check if the session step budget is exhausted (0 = unlimited).

        Ref: [AgentCgroup §4.2] Graduated enforcement — this is the trigger
        check. The response (NL warning injection) happens in ToolUseLoop.
        """
        return self._session_step_budget > 0 and self._total_steps >= self._session_step_budget

    def budget_remaining(self) -> int:
        """Steps remaining in the session budget (0 = unlimited)."""
        if self._session_step_budget <= 0:
            return -1  # Unlimited
        return max(0, self._session_step_budget - self._total_steps)

    async def stalled_agents(self, threshold: float = 120.0) -> list[AgentHandle]:
        """Return agents not making progress within threshold seconds.

        Ref: [DeepMind-Delegation §4.4] Internal trigger: delegatee
        unresponsive → diagnose → evaluate → intervene.
        """
        now = time.monotonic()
        async with self._lock:
            return [
                h
                for h in self._agents.values()
                if h.status == "running"
                and h.last_step_at is not None
                and (now - h.last_step_at) > threshold
            ]

    # ------------------------------------------------------------------
    # Bidirectional messaging  (Ref: [DeepMind-Delegation §4.4], [AgentCgroup §4.2])
    # ------------------------------------------------------------------

    async def send_message(self, agent_id: str, message: str) -> str | None:
        """Send a steering message to a running agent.

        Returns ``None`` on success, or a diagnostic string on failure.

        Ref: [DeepMind-Delegation §4.4] Adaptive coordination — the hypervisor
        injects NL feedback (budget warnings, stall nudges) into the agent's
        message queue. The ToolUseLoop drains this queue between steps.
        Ref: [AgentCgroup §4.2] Bidirectional system↔agent feedback loop.
        """
        async with self._lock:
            handle = self._agents.get(agent_id)
            if not handle:
                return "agent not in registry"
            if handle.status != "running":
                return f"agent status is '{handle.status}'"
            if not handle.message_queue:
                return "no message queue"
            handle.message_queue.put_nowait(message)
            return None

    async def record_compaction(self, agent_id: str) -> None:
        """Record that an agent compacted its context."""
        async with self._lock:
            handle = self._agents.get(agent_id)
            if handle:
                handle.compaction_count += 1
                handle.last_compacted_at = time.monotonic()

    # ------------------------------------------------------------------
    # Global eye  (Ref: [DeepMind-Delegation §4.5])
    # ------------------------------------------------------------------

    async def render_agent_tree(
        self,
        *,
        exclude_agent_id: str | None = None,
    ) -> str:
        """Render a concise text summary of the agent tree for the root's system prompt.

        Ref: [DeepMind-Delegation §4.5] Structural transparency — the root
        agent (the hypervisor's "brain") gets a live view of all agents so
        it can reason about the delegation state and intervene if needed.

        Args:
            exclude_agent_id: If provided, omit this agent from the rendered
                tree.  Used so the calling agent does not see itself listed.
        """
        async with self._lock:
            if not self._agents:
                return ""
            visible = [h for h in self._agents.values() if h.agent_id != exclude_agent_id]
            if not visible:
                return ""
            counts: dict[str, int] = {}
            for h in visible:
                counts[h.status] = counts.get(h.status, 0) + 1
            status_parts = [f"{v} {k}" for k, v in sorted(counts.items())]
            budget_str = ""
            if self._session_step_budget > 0:
                budget_str = f" | Budget: {self._total_steps}/{self._session_step_budget} steps"
            header = f"Agents: {', '.join(status_parts)}{budget_str}"
            lines = [header]
            for h in sorted(visible, key=lambda x: (x.depth, x.agent_id)):
                indent = "  " * h.depth
                step_info = f"{h.steps_completed} steps"
                if h.last_tool_id:
                    step_info += f", last: {h.last_tool_id}"
                status_marker = ""
                if h.status == "completed":
                    status_marker = " -> success"
                elif h.status == "failed":
                    status_marker = " -> FAILED"
                elif h.status == "cancelled":
                    status_marker = " -> cancelled"
                # Ref: [DeepMind-Delegation §4.5] Progress/result in tree view
                extra = ""
                if h.result and h.result.summary:
                    extra = f" | result({h.result.status}): {h.result.summary[:120]}"
                elif h.progress_note:
                    extra = f" | progress: {h.progress_note[:120]}"
                compact_marker = f" | compacted x{h.compaction_count}" if h.compaction_count else ""
                task_preview = h.task_description[:80]
                lines.append(
                    f"{indent}- [{h.agent_id[:8]}] {h.status}: "
                    f'"{task_preview}" ({step_info}{status_marker}{compact_marker}{extra})'
                )
            return "\n".join(lines)

    # ------------------------------------------------------------------
    # Async delegation queries  (Ref: [CoA §3.1], [DeepMind-Delegation §4.5])
    # ------------------------------------------------------------------

    async def collect_completed(self, parent_id: str) -> list[AgentHandle]:
        """Return children that reached a terminal state with a stored result.

        Ref: [CoA §3.1] Async CU retrieval — parent reads results
        when ready, not when child finishes.
        """
        terminal = {"completed", "failed", "cancelled"}
        async with self._lock:
            return [
                h
                for h in self._agents.values()
                if h.parent_id == parent_id and h.status in terminal and h.result is not None
            ]

    async def collect_running(self, parent_id: str) -> list[AgentHandle]:
        """Return children of *parent_id* that are still active."""
        async with self._lock:
            return [
                h
                for h in self._agents.values()
                if h.parent_id == parent_id and h.status in ("submitted", "running")
            ]

    async def send_to_parent(self, child_agent_id: str, message: str) -> str | None:
        """Route a message from a child agent to its parent's queue.

        Returns ``None`` on success, or a diagnostic string on failure.

        Ref: [DeepMind-Delegation §4.4] Bidirectional message passing —
        enables lifecycle manager to notify parent on child completion.
        """
        async with self._lock:
            child = self._agents.get(child_agent_id)
            if not child or not child.parent_id:
                return "child not in registry or no parent"
            parent = self._agents.get(child.parent_id)
            if not parent:
                return "parent not in registry"
            if not parent.message_queue:
                return "parent has no message queue"
            parent.message_queue.put_nowait(message)
            return None

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    async def cancel_agent(self, agent_id: str) -> str | None:
        """Cancel a running agent by cancelling its asyncio task.

        Returns ``None`` on success, or a diagnostic string on failure.
        """
        async with self._lock:
            handle = self._agents.get(agent_id)
            if not handle:
                return "agent not in registry"
            if not handle.asyncio_task:
                return "no asyncio task"
            if handle.asyncio_task.done():
                return f"task already done (status: {handle.status})"
            handle.asyncio_task.cancel()
            handle.status = "cancelled"
            handle.stopped_at = time.monotonic()
            handle.done_event.set()
            return None

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
            running = [h for h in self._agents.values() if h.status == "running"]

        if not running:
            async with self._lock:
                self._agents.clear()
            return

        # Phase 1: Request cancellation.
        for handle in running:
            if handle.asyncio_task and not handle.asyncio_task.done():
                handle.asyncio_task.cancel()

        # Phase 2: Wait with timeout.
        tasks = [h.asyncio_task for h in running if h.asyncio_task and not h.asyncio_task.done()]
        if tasks:
            done, pending = await asyncio.wait(
                tasks,
                timeout=timeout,
                return_when=asyncio.ALL_COMPLETED,
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
    "AgentResult",
    "AgentStatus",
]
