#!/usr/bin/env python3
"""Immutable agent context propagated through the agent hierarchy.

``AgentContext`` is the per-agent state carried by every ``ToolUseLoop``
instance. The root agent creates one via ``AgentContext.root()``; child
agents receive one via ``parent_ctx.child()``.

The hypervisor control plane (``AgentHypervisor``, ``AgentHandle``) lives
in :mod:`mewbo_core.hypervisor`.
"""

from __future__ import annotations

import queue
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from mewbo_core.hypervisor import AgentHandle, AgentHypervisor, AgentRegistry, AgentStatus

if TYPE_CHECKING:
    from mewbo_core.types import Event


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
    # Effective delegation privilege ceiling for THIS agent. Propagated
    # like ``fallback_models`` but MONOTONICALLY narrowed
    # at every hop — see :meth:`child`. ``"all"`` (root default) means no
    # capability filtering. A plain ``str`` (not the ``CapabilityMode``
    # Literal): this is hot in-process state that crosses no trust boundary,
    # so the validated Literal lives at the ``SpawnAgentTask`` seam instead.
    capability_mode: str = "all"
    # Effective FILESYSTEM-containment ceiling for THIS agent. The
    # second privilege axis, ORTHOGONAL to ``capability_mode`` and narrowed the
    # same way — monotonically, min-wins, at every ``child()`` hop (see below).
    # ``"full_access"`` (root default) means no path restriction; a narrower tier
    # confines reads (and, above ``read_only``, writes) to the agent's workspace.
    # A plain ``str`` for the same reason as ``capability_mode``: hot in-process
    # state crossing no trust boundary — the validated ``WorkspaceMode`` Literal
    # lives at the ``SpawnAgentTask`` seam. Enforcement is separately gated on
    # ``agent.workspace_enforcement`` (staged OFF), so this field is carried and
    # narrowed today but only bites once that flag flips.
    workspace_mode: str = "full_access"
    # Ref: [DeepMind-Delegation §4.7] Delegation firebreak — set by
    # a DelegationContract(autonomy="atomic"). Propagated like
    # ``capability_mode`` but MONOTONICALLY: once set, every descendant stays
    # atomic too (see ``child``), so a grandchild can never re-enable
    # delegation an ancestor gave up.
    atomic: bool = False
    message_queue: queue.Queue[str] | None = None
    interrupt_step: threading.Event | None = None

    # Monotonic privilege ranking for ``capability_mode`` narrowing:
    # lower rank = more restrictive. Mirrors the tiers in
    # ``tool_registry._CAPABILITY_MODE_TIERS`` (kept consistent by a test).
    _CAPABILITY_MODE_RANK: ClassVar[dict[str, int]] = {
        "read_only": 0,
        "execute": 1,
        "all": 2,
    }

    # Monotonic privilege ranking for ``workspace_mode`` narrowing; same
    # min-wins shape, a second axis. Kept in lockstep with
    # ``mewbo_core.workspace.WORKSPACE_MODE_RANK`` by a test.
    _WORKSPACE_MODE_RANK: ClassVar[dict[str, int]] = {
        "read_only": 0,
        "workspace_write": 1,
        "full_access": 2,
    }

    @property
    def can_spawn(self) -> bool:
        """True if this agent is allowed to create children."""
        return self.depth < self.max_depth

    @property
    def remaining_depth(self) -> int:
        """Number of spawn levels remaining below this agent."""
        return max(0, self.max_depth - self.depth)

    @staticmethod
    def _narrow(parent: str, requested: str, rank_table: dict[str, int]) -> str:
        """Return the more restrictive of two modes on ONE privilege axis.

        The shared min-wins narrowing behind BOTH ``capability_mode`` and
        ``workspace_mode``: privilege attenuation is monotonic down the
        agent hierarchy — a child can only ever narrow a mode, never widen it
        ([DeepMind-Delegation §4.7]), so a grandchild under a restrictive ancestor
        can never climb back. An unrecognised mode collapses to the axis's WIDEST
        (no-op) tier — the highest-ranked key in *rank_table* (``all`` for
        capability, ``full_access`` for workspace) — so a stray string neither
        tightens surprisingly nor loosens below the parent; the authoritative
        validation is the ``Literal`` at the ``SpawnAgentTask`` seam upstream.
        This preserves each axis's existing unknown → widest handling byte-for-byte.
        """
        widest = max(rank_table, key=lambda k: rank_table[k])
        p = parent if parent in rank_table else widest
        r = requested if requested in rank_table else widest
        return p if rank_table[p] <= rank_table[r] else r

    @staticmethod
    def _narrower_capability_mode(parent: str, requested: str) -> str:
        """Narrow ``capability_mode`` — thin alias over :meth:`_narrow`.

        Retained as the named entry point (referenced by tests + call sites); the
        min-wins / unknown → ``all`` semantics are now the shared ``_narrow``.
        """
        return AgentContext._narrow(
            parent, requested, AgentContext._CAPABILITY_MODE_RANK
        )

    def child(
        self,
        *,
        model_name: str | None = None,
        capability_mode: str = "all",
        workspace_mode: str = "full_access",
        atomic: bool = False,
    ) -> AgentContext:
        """Create a child context with depth+1.

        ``capability_mode`` is the child's REQUESTED delegation privilege
        ceiling; the stored value is the narrower of it and this (parent)
        agent's own ``capability_mode`` — a child can only restrict, so
        an unset request (default ``"all"``) simply inherits the parent's mode.

        ``workspace_mode`` is the SAME story on the filesystem-containment
        axis: the child's requested tier is narrowed min-wins against this
        agent's own, so an unset request (default ``"full_access"``) inherits the
        parent's tier and a grandchild under a ``read_only`` ancestor stays
        ``read_only``.

        ``atomic`` is this child's OWN requested firebreak (from
        its ``DelegationContract``); the stored value is ``self.atomic or
        atomic`` — a simple OR, never narrower — so an atomic ancestor's
        descendants can never climb back to ``open_ended``.

        ``model_name`` falls back to ``self.model_name``, which the inheriting
        child then runs on. That fallback is only correct while this context's
        ``model_name`` is the parent's LIVE model, and a parent that heals down
        its fallback ladder promotes a new one mid-run — on the loop, since this
        context is frozen. So the loop re-seats the spawn seam's context (via
        ``dataclasses.replace``) whenever it escalates: without that, a parent
        that had just escaped a dead model would fan every un-overridden child
        straight back onto it, and the children would die at step 0 while the
        parent ran healthy.

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
            capability_mode=self._narrow(
                self.capability_mode, capability_mode, self._CAPABILITY_MODE_RANK
            ),
            workspace_mode=self._narrow(
                self.workspace_mode, workspace_mode, self._WORKSPACE_MODE_RANK
            ),
            atomic=self.atomic or atomic,
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
        workspace_mode: str = "full_access",
        capability_mode: str = "all",
    ) -> AgentContext:
        """Create the root agent context.

        ``workspace_mode`` seeds the root of the filesystem-containment
        axis from ``agent.default_workspace_mode`` (default ``"full_access"``);
        every child narrows from here. Left at the default, the whole tree is
        unrestricted — byte-identical to the pre-existing path.

        ``capability_mode`` seeds the root of the delegation-privilege axis the
        same way (default ``"all"`` — no filtering). A caller resolving a
        principal's role ceiling passes a narrower tier (``read_only`` for a
        viewer) so the ROOT agent — not only its spawned children — is capped;
        every child then narrows monotonically from here via :meth:`child`. Left
        at ``"all"`` the whole tree is unrestricted, byte-identical to before.
        """
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
            workspace_mode=workspace_mode,
            capability_mode=capability_mode,
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
