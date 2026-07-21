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
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

from mewbo_core.common import get_logger
from mewbo_core.prompt_registry import get_prompt_registry

if TYPE_CHECKING:
    from mewbo_core.attestation import AttestationChain
    from mewbo_core.spawn_agent import AgentError

logging = get_logger(name="core.hypervisor")

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

# The NON-terminal states — an agent sitting in one is still owed a terminal
# transition. Shutdown must settle both: an agent cancelled before its loop task
# ever began is still at ``submitted``, so filtering on ``running`` alone left it
# pinned non-terminal for every reader that derives liveness from status.
ACTIVE_STATUSES: frozenset[AgentStatus] = frozenset({"submitted", "running"})


# Ref: [CoA §3] Communication Units are TASK-TYPED — a QA/evidence probe's CU
# is shaped differently from a running summarization pass or a code-focused
# lane (§3 gives evidence/summary/signature as the worked examples). Mirrors
# `AgentStatus` in shape: a closed vocabulary shared by the spawn caller
# (`SpawnAgentTask.summary_kind`, `spawn_agent.py`) and whatever renders the
# result. `"generic"` is the untyped default — the historical single-shape
# summary predates this field and stays that shape when nothing is declared.
SummaryKind = Literal[
    "evidence",
    "running_summary",
    "code_signature",
    "generic",
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
    # Honest retry provenance. Total times this task was
    # admitted+run, incl. the first attempt (1 = never retried). Surfaced so the
    # parent sees a workstream was transparently recovered rather than silently
    # dropped. Additive (default 1): a no-retry spawn is byte-equivalent in
    # behaviour to the historical single-attempt path.
    attempts: int = 1
    # Ref: [CoA §3] — task-typed CU shape, stamped from
    # the spawn caller's optional `summary_kind`. Additive (default
    # "generic"): a spawn that never declares a kind is byte-equivalent to
    # the historical untyped-summary path.
    summary_kind: SummaryKind = "generic"
    # The terminal ``AttestationChain`` record's hash for
    # this result, or "" when no chain is wired (the common case today) or
    # the record failed to persist. Additive: a spawn under a session with no
    # attestation configured is byte-equivalent to the historical path.
    attestation_hash: str = ""
    # Verifier-gated completion provenance, projected from the child's
    # ``OrchestrationState``. ``verified is None`` (default) = the gate never
    # ran for this child (no spec, master switch off, or a below-execute
    # capability_mode) — byte-equivalent to the historical unverified path;
    # ``True``/``False`` = a ground-truth check passed / exhausted its retries.
    # ``verify_attempts`` is how many verifier runs the child drove. Surfaced so
    # a spawner never reads a claimed completion as an invisible null.
    verified: bool | None = None
    verify_attempts: int = 0


# Ref: [DeepMind-Delegation §4.7] Privilege attenuation — a
# delegation contract is a coarse, DATA-declared ceiling the SPAWNER puts on
# ONE child, layered UNDER the shared session step budget (never above it).
AutonomyTier = Literal["atomic", "open_ended"]
ModelTier = Literal["economy", "standard", "frontier"]

_AUTONOMY_TIERS: frozenset[str] = frozenset({"atomic", "open_ended"})
_MODEL_TIERS: frozenset[str] = frozenset({"economy", "standard", "frontier"})


@dataclass(frozen=True)
class DelegationContract:
    """Per-spawn delegation bounds a caller can put on ONE child.

    Every field's zero/off value is the historical unbounded-child behaviour,
    so a spawn that never declares a ``contract`` is byte-identical — this is
    an opt-in ceiling, never a new default constraint. Distinct from the
    hypervisor's SESSION-wide ``session_step_budget``: that is a shared pool
    across the whole agent tree; this is one caller's bound on one child,
    checked in addition to (never instead of) the session budget.

    Ref: [DeepMind-Delegation §4.7] Privilege attenuation — ``autonomy`` is
    the hard delegation firebreak (an atomic child can never itself spawn,
    and the bit only ever narrows down the tree, see ``AgentContext.child``).
    Ref: [AgentCgroup §4.2] Graduated enforcement — ``step_state``/
    ``wall_state``/``token_state`` each expose a warn tier before the hard
    stop, mirroring the session budget's own warn-then-halt shape.
    """

    max_steps: int = 0  # 0 = unlimited, layered UNDER the session budget
    max_wall_s: float = 0.0  # 0 = unlimited
    max_tokens: int = 0  # 0 = no signal — ADVISORY, best-effort only
    autonomy: AutonomyTier = "open_ended"
    model_tier: ModelTier | None = None
    step_warn_headroom: int = 3

    @property
    def atomic(self) -> bool:
        """True when this contract strips the child's own delegation rights."""
        return self.autonomy == "atomic"

    @property
    def enabled(self) -> bool:
        """True when the contract carries at least one real constraint."""
        return bool(
            self.max_steps > 0
            or self.max_wall_s > 0
            or self.max_tokens > 0
            or self.atomic
            or self.model_tier is not None
        )

    @classmethod
    def from_value(cls, value: object) -> DelegationContract:
        """Parse + validate the schema ``contract`` object. Unset/invalid → OFF.

        Validation is total (never raises), mirroring ``RetryPolicy.from_value``:
        a malformed field degrades to the safe default rather than failing a
        spawn, since ``contract`` is an optional caller-declared ceiling, not a
        correctness contract. Unknown keys are dropped after ONE warning per
        parse (never per-key) so a typo'd field doesn't spam the log.
        """
        if not isinstance(value, Mapping):
            return cls()

        known = {
            "max_steps",
            "max_wall_s",
            "max_tokens",
            "autonomy",
            "model_tier",
            "step_warn_headroom",
        }
        unknown = sorted(set(value.keys()) - known)
        if unknown:
            logging.warning("DelegationContract.from_value: dropping unknown keys {}", unknown)

        try:
            max_steps = max(0, int(value.get("max_steps", 0)))
        except (TypeError, ValueError):
            max_steps = 0
        try:
            max_wall_s = max(0.0, float(value.get("max_wall_s", 0.0)))
        except (TypeError, ValueError):
            max_wall_s = 0.0
        try:
            max_tokens = max(0, int(value.get("max_tokens", 0)))
        except (TypeError, ValueError):
            max_tokens = 0
        raw_autonomy = value.get("autonomy", "open_ended")
        autonomy = raw_autonomy if raw_autonomy in _AUTONOMY_TIERS else "open_ended"
        raw_model_tier = value.get("model_tier")
        model_tier = raw_model_tier if raw_model_tier in _MODEL_TIERS else None
        try:
            step_warn_headroom = max(0, int(value.get("step_warn_headroom", 3)))
        except (TypeError, ValueError):
            step_warn_headroom = 3

        return cls(
            max_steps=max_steps,
            max_wall_s=max_wall_s,
            max_tokens=max_tokens,
            autonomy=cast("AutonomyTier", autonomy),
            model_tier=cast("ModelTier | None", model_tier),
            step_warn_headroom=step_warn_headroom,
        )

    def step_state(self, steps_completed: int) -> Literal["ok", "warn", "over"]:
        """Graduated step-budget state — ``ok`` when unset (0 = unlimited)."""
        if self.max_steps <= 0:
            return "ok"
        if steps_completed >= self.max_steps:
            return "over"
        if steps_completed >= self.max_steps - self.step_warn_headroom:
            return "warn"
        return "ok"

    def wall_state(self, elapsed_s: float) -> Literal["ok", "warn", "over"]:
        """Graduated wall-clock state — warn at 80%, over at 100% of the bound."""
        if self.max_wall_s <= 0:
            return "ok"
        if elapsed_s >= self.max_wall_s:
            return "over"
        if elapsed_s >= self.max_wall_s * 0.8:
            return "warn"
        return "ok"

    def token_state(self, total_tokens: int) -> Literal["ok", "warn", "over"]:
        """Feature-detected advisory token state — best-effort, never a hard promise.

        ``total_tokens <= 0`` means the caller has no usage signal at all (a
        model/proxy that never surfaced ``usage_metadata``), so absence of
        data reads as ``ok``, never ``over`` — the same feature-detection
        posture as ``_UsageNormalizingLiteLLM``. Likewise a contract with no
        ``max_tokens`` declared is always ``ok``: this axis is advisory only,
        cost accounting is out of scope.
        """
        if total_tokens <= 0 or self.max_tokens <= 0:
            return "ok"
        return "over" if total_tokens >= self.max_tokens else "ok"

    def resolve_model_override(
        self,
        explicit: str | None,
        tier_map: Mapping[str, str],
        allowed: Any = None,
    ) -> str | None:
        """Resolve ``model_tier`` against a deployment's tier→model map.

        Returns ``None`` (fall through to the caller's existing resolution)
        when: an ``explicit`` model was already given (it always wins — this
        is the LOWEST-priority model source); no ``model_tier`` is declared;
        the tier has no map entry; or the mapped model is excluded by
        ``allowed`` (when non-empty). Otherwise returns the mapped model id.
        """
        if explicit:
            return None
        if self.model_tier is None:
            return None
        mapped = tier_map.get(self.model_tier)
        if not mapped:
            return None
        if allowed and mapped not in allowed:
            return None
        return mapped

    def snapshot(self) -> dict[str, Any]:
        """Bounded-scalar serialization.

        The ONE shared shape for ``check_agents`` NOW and attestation
        provenance in a later wave.
        """
        return asdict(self)


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
    # The registered AgentDef name this agent was spawned as (e.g.
    # ``scg-path-probe``) — ``None`` for an ad-hoc spawn with no ``agent_type``.
    # Distinct from ``model_name``: the trace projection needs the LANE identity
    # (the def), which a model name can never carry.
    agent_type: str | None = None
    status: AgentStatus = "submitted"  # Ref: [A2A v1.0] Start as submitted
    started_at: float = field(default_factory=time.monotonic)
    stopped_at: float | None = None
    steps_completed: int = 0
    last_tool_id: str | None = None
    # Ref: [AgentCgroup §4.2] Tool-call-granularity timing for stall detection
    last_step_at: float | None = None
    # The tool actually IN FLIGHT right now — stamped at dispatch start and
    # cleared back to ``None`` when the call resolves. Distinct from
    # ``last_tool_id`` (only updated on COMPLETION): during a long-running
    # call, ``last_tool_id`` still names the PREVIOUS finished tool, so the
    # watchdog must read ``active_tool_id`` for stall attribution, never
    # ``last_tool_id``.
    active_tool_id: str | None = None
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
    # Bumped by the spawn bridge's bounded-retry driver each
    # time this same task is re-admitted (1 = first/only attempt). Read by the
    # agent-tree render + check_agents payload so a retried child is visible.
    attempts: int = 1
    # The spawner's own delegation bounds for this child.
    # Additive: the default (disabled) contract is a no-op, so an ad-hoc
    # spawn with no ``contract`` behaves exactly as before.
    contract: DelegationContract = field(default_factory=DelegationContract)
    # This child's OWN spawn attestation record hash
    # (stamped by ``SpawnAgentTool`` right after registration), threaded back
    # in as the terminal record's ``spawn_hash`` link. "" when no chain is
    # wired for this session.
    attestation_spawn_hash: str = ""
    # Latched when this agent's ONE terminal ``stop`` lifecycle event is written.
    # Several paths can settle the same agent (its own success/cancel/failure
    # handler, and its parent's teardown cascade), and they RACE — the flag is
    # what keeps "exactly one stop per start" true rather than merely likely.
    # Written only through the spawn bridge's terminal emitter.
    terminal_emitted: bool = False


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
        attestation: AttestationChain | None = None,
    ) -> None:
        """Initialize hypervisor with concurrency and budget limits.

        Ref: [AgentCgroup §4.2] Session-wide budget with graduated enforcement.

        Args:
            max_concurrent: Maximum number of concurrent agents (semaphore slots).
            session_step_budget: Total tool steps allowed across all agents in the
                session. 0 means unlimited.
            attestation: optional provenance hash chain for
                this session's agent tree. The hypervisor never constructs one
                itself (it has no session_id, no store) — the orchestrator
                injects it, seeded from the store's last persisted record, when
                the feature is config-enabled. ``None`` (the default) means no
                attestation is recorded; ``SpawnAgentTool`` reads this
                attribute directly and no-ops when it's absent.
        """
        self._agents: dict[str, AgentHandle] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(max_concurrent)
        # Ref: [AgentCgroup §4.2] Session-wide resource tracking
        self._total_steps: int = 0
        self._session_step_budget: int = session_step_budget
        self.attestation = attestation

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

    async def try_admit(self) -> bool:
        """Acquire a concurrency slot without waiting.

        Returns ``False`` immediately when no slot is free, instead of
        blocking up to the :meth:`admit` timeout. The batch fan-out path
        (``spawn_agents``) uses this so an over-subscribed call marks the
        surplus entries ``rejected`` while their siblings proceed — never a
        per-entry 30s stall. Race-free in the single-threaded event loop: an
        unlocked semaphore's ``acquire()`` completes synchronously (no await
        point) between the ``locked()`` check and the acquire.
        """
        if self._semaphore.locked():
            return False
        await self._semaphore.acquire()
        return True

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

    async def mark_tool_start(self, agent_id: str, tool_id: str | None) -> None:
        """Stamp (or clear) the tool actually in flight for stall attribution.

        Ref: [AgentCgroup §4.2] ``update_step`` only stamps ``last_tool_id`` on
        COMPLETION, so a watchdog check firing mid-call would misattribute the
        stall to the previous, already-finished tool. Call this at dispatch
        start with the tool name, and again with ``None`` once the call
        resolves (success, timeout, or exception) so ``active_tool_id`` never
        lingers stale once the agent moves on.
        """
        async with self._lock:
            handle = self._agents.get(agent_id)
            if handle:
                handle.active_tool_id = tool_id

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

    async def list_visible(self, exclude_agent_id: str | None = None) -> list[AgentHandle]:
        """Snapshot of agents excluding the caller — used by check_agents."""
        async with self._lock:
            return [h for h in self._agents.values() if h.agent_id != exclude_agent_id]

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

    def budget_warning(self, *, headroom: int = 5) -> bool:
        """True when within ``headroom`` steps of the session budget (0 = off)."""
        if self._session_step_budget <= 0:
            return False
        return self.budget_remaining() <= headroom

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

    async def agent_step_state(self, agent_id: str) -> str:
        """Per-contract step-budget state for one agent.

        ``"ok"`` for an unregistered agent or one whose contract carries no
        step bound — the same inert default as a disabled contract.
        """
        async with self._lock:
            handle = self._agents.get(agent_id)
        if handle is None:
            return "ok"
        return handle.contract.step_state(handle.steps_completed)

    async def agent_token_state(self, agent_id: str) -> str:
        """Per-contract advisory token state for one agent."""
        async with self._lock:
            handle = self._agents.get(agent_id)
        if handle is None:
            return "ok"
        return handle.contract.token_state(handle.input_tokens + handle.output_tokens)

    async def over_wall_deadline_agents(
        self, now: float | None = None
    ) -> list[tuple[AgentHandle, str]]:
        """Running agents whose contract wall-clock deadline is warn/over.

        Mirrors :meth:`stalled_agents` — a per-contract cousin of the stall
        sweep, keyed on ``started_at`` rather than last-progress. ``now``
        arrives as a clock ARG (never read internally), so a caller can drive
        this deterministically without sleeping a real clock.
        """
        clock = now if now is not None else time.monotonic()
        async with self._lock:
            result: list[tuple[AgentHandle, str]] = []
            for h in self._agents.values():
                if h.status != "running" or h.contract.max_wall_s <= 0:
                    continue
                state = h.contract.wall_state(clock - h.started_at)
                if state in ("warn", "over"):
                    result.append((h, state))
            return result

    @staticmethod
    def _contract_marker(handle: AgentHandle) -> str:
        """Compact ``| contract: ...`` suffix for an agent-tree row.

        Empty (no-op) unless the handle's contract is ``enabled`` — historical
        rows for a contract-less child stay byte-identical.
        """
        if not handle.contract.enabled:
            return ""
        bits: list[str] = [handle.contract.autonomy]
        if handle.contract.max_steps > 0:
            bits.append(f"{handle.steps_completed}/{handle.contract.max_steps} steps")
        if handle.contract.max_wall_s > 0:
            elapsed = time.monotonic() - handle.started_at
            bits.append(f"{elapsed:.0f}/{handle.contract.max_wall_s:.0f}s")
        return f" | contract: {', '.join(bits)}"

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
            registry = get_prompt_registry()
            counts: dict[str, int] = {}
            for h in visible:
                counts[h.status] = counts.get(h.status, 0) + 1
            status_parts = [f"{v} {k}" for k, v in sorted(counts.items())]
            budget_str = ""
            if self._session_step_budget > 0:
                budget_str = registry.render(
                    "catalog.agent_tree.budget",
                    total_steps=self._total_steps,
                    session_step_budget=self._session_step_budget,
                )
            header = registry.render(
                "catalog.agent_tree.header",
                status_parts=", ".join(status_parts),
                budget_str=budget_str,
            )
            lines = [header]
            for h in sorted(visible, key=lambda x: (x.depth, x.agent_id)):
                indent = "  " * h.depth
                step_info = f"{h.steps_completed} steps"
                if h.last_tool_id:
                    step_info += registry.render(
                        "catalog.agent_tree.step_info_last", last_tool_id=h.last_tool_id
                    )
                # Surface bounded-retry provenance inline so the
                # root sees a child was re-delegated (omitted when never retried,
                # keeping the historical single-attempt line byte-identical).
                if h.attempts > 1:
                    step_info += f", {h.attempts} attempts"
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
                    extra = registry.render(
                        "catalog.agent_tree.result",
                        status=h.result.status,
                        summary=h.result.summary[:120],
                    )
                elif h.progress_note:
                    extra = registry.render(
                        "catalog.agent_tree.progress",
                        progress_note=h.progress_note[:120],
                    )
                compact_marker = (
                    registry.render(
                        "catalog.agent_tree.compact", compaction_count=h.compaction_count
                    )
                    if h.compaction_count
                    else ""
                )
                task_preview = h.task_description[:80]
                line = registry.render(
                    "catalog.agent_tree.line",
                    indent=indent,
                    agent_id_head=h.agent_id[:8],
                    status=h.status,
                    task_preview=task_preview,
                    step_info=step_info,
                    status_marker=status_marker,
                    compact_marker=compact_marker,
                    extra=extra,
                )
                # Appended OUTSIDE the templated line (never
                # a catalog.yaml edit) so a contract-less row's bytes are
                # completely unaffected.
                lines.append(line + self._contract_marker(h))
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

        Phase 1: Request cancellation on all active asyncio tasks.
        Phase 2: Wait up to *timeout* seconds for tasks to finish.
        Phase 3: Force-mark any still-pending agents as cancelled.

        Covers every :data:`ACTIVE_STATUSES` agent, not just ``running``: one
        still at ``submitted`` never had a loop task to cancel, which is exactly
        why it would otherwise survive shutdown unmarked.

        Marking here is deliberately IN-MEMORY only — the hypervisor holds no
        event logger, and wiring one in would invert the layering (the session
        owns the transcript, the hypervisor owns the tree). The durable terminal
        is written by whichever peer can still reach the log:

        - Phase 1/2 (the task is alive): cancelling it raises ``CancelledError``
          inside the agent's own lifecycle handler, which writes the terminal
          ``stop`` before unwinding. That is the normal path.
        - Phase 3 / the final sweep (the task is wedged, or the process is being
          torn down mid-flight): nothing in-process can still write, so the
          durable terminal comes from the startup sweep on the NEXT boot, which
          settles agent spans left open under a run that has since ended.
        """
        async with self._lock:
            active = [h for h in self._agents.values() if h.status in ACTIVE_STATUSES]

        if not active:
            async with self._lock:
                self._agents.clear()
            return

        # Phase 1: Request cancellation.
        for handle in active:
            if handle.asyncio_task and not handle.asyncio_task.done():
                handle.asyncio_task.cancel()

        # Phase 2: Wait with timeout.
        tasks = [h.asyncio_task for h in active if h.asyncio_task and not h.asyncio_task.done()]
        if tasks:
            done, pending = await asyncio.wait(
                tasks,
                timeout=timeout,
                return_when=asyncio.ALL_COMPLETED,
            )

            # Phase 3: Force-mark any still-pending as cancelled.
            for task in pending:
                for handle in active:
                    if handle.asyncio_task is task:
                        async with self._lock:
                            if handle.status in ACTIVE_STATUSES:
                                handle.status = "cancelled"
                                handle.error = "Force-cancelled after timeout"
                                handle.stopped_at = time.monotonic()

        # Final sweep: mark any remaining non-terminal agents and clear.
        async with self._lock:
            for h in list(self._agents.values()):
                if h.status in ACTIVE_STATUSES:
                    h.status = "cancelled"
                    h.stopped_at = time.monotonic()
            self._agents.clear()


# Backwards-compatible alias.
AgentRegistry = AgentHypervisor

__all__ = [
    "ACTIVE_STATUSES",
    "AgentHandle",
    "AgentHypervisor",
    "AgentRegistry",
    "AgentResult",
    "AgentStatus",
    "AutonomyTier",
    "DelegationContract",
    "ModelTier",
    "SummaryKind",
]
