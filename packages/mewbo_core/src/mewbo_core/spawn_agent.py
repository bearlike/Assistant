#!/usr/bin/env python3
"""Sub-agent spawning tool for the agent hypervisor.

``SpawnAgentTool`` creates a child ``ToolUseLoop`` instance, registers it
in the ``AgentHypervisor``, runs it to completion, and returns the result.
Tool scoping follows Claude Code's "filter before binding" pattern: denied
tools are removed from the child's ``bind_tools()`` list so the child LLM
never sees them.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mewbo_core.agent_context import AgentContext, AgentDepthExceeded
from mewbo_core.classes import ActionStep
from mewbo_core.common import MockSpeaker, get_logger
from mewbo_core.config import get_config_value
from mewbo_core.hooks import HookManager
from mewbo_core.hypervisor import AgentHandle
from mewbo_core.permissions import PermissionPolicy
from mewbo_core.session_tools import SessionToolRegistry
from mewbo_core.tool_registry import ToolRegistry, ToolSpec, filter_specs
from mewbo_core.types import Event

logging = get_logger(name="core.spawn_agent")

# Cap the compressed child result echoed onto the ``stop`` lifecycle event so a
# verbose sub-agent answer can't bloat the parent transcript / run event log.
# Mirrors the ``AgentResult.summary`` cap (here a touch larger so a probe's
# whole evidence block survives for the trace's response panel).
_SUB_AGENT_SUMMARY_CAP = 1500


@dataclass
class AgentError:
    """Structured error context from a failed sub-agent."""

    agent_id: str
    depth: int
    task: str  # First 200 chars of task description
    error: str  # Exception message
    last_tool: str | None = None
    steps_completed: int = 0

    def __str__(self) -> str:  # noqa: D105
        parts = [f"Agent {self.agent_id} (depth={self.depth})"]
        parts.append(f"failed after {self.steps_completed} steps")
        if self.last_tool:
            parts.append(f"at tool '{self.last_tool}'")
        parts.append(f": {self.error}")
        return " ".join(parts)


class SpawnAgentTask(BaseModel):
    """One entry in a ``spawn_agents`` batch (Gitea #117).

    Carries the SAME per-task fields as the single ``spawn_agent`` schema, but
    validated at definition: ``extra="forbid"`` rejects stray keys so a
    malformed fan-out fails fast instead of silently dropping a field, and a
    blank ``task`` is refused (an empty delegation is never intentional).
    """

    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1)
    model: str | None = None
    allowed_tools: list[str] | None = None
    denied_tools: list[str] | None = None
    # Deprecated — retained for schema/prompt compatibility, never enforced.
    max_steps: int | None = None
    acceptance_criteria: str | None = None
    agent_type: str | None = None
    # Opt-in bounded auto-retry (#118), parsed downstream by ``RetryPolicy``.
    # A batch entry can carry it just like a single spawn — one transient
    # failure in a wide fan-out then re-delegates instead of dropping a lane.
    retry: dict[str, Any] | None = None

    def to_args(self) -> dict[str, Any]:
        """Project to the ``args`` dict the single-spawn path consumes.

        Unset (``None``) fields are dropped so the downstream ``args.get(...)``
        defaults apply exactly as they do for an ad-hoc ``spawn_agent`` call —
        keeping the batch a thin reuse of ``_spawn_one`` rather than a fork.
        """
        return {k: v for k, v in self.model_dump().items() if v is not None}


@dataclass
class _SpawnOutcome:
    """Internal result of one admission+spawn attempt.

    Shared by the single (``run_async``) and batch (``run_batch_async``) entry
    points so both flow through the identical ``_spawn_one`` path. ``content``
    is the verbatim ``MockSpeaker`` payload the single tool returns (kept
    byte-stable); ``agent_id`` is the spawned child's id (``None`` when no slot
    was admitted); ``status`` is the lifecycle/admission state
    (``submitted``/``completed``/``failed``/``cancelled``/``cannot_solve``/``rejected``).
    """

    content: str
    agent_id: str | None
    status: str


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded auto-retry / re-delegation policy for a spawned sub-agent (#118).

    Opt-in via the ``retry`` spawn-schema field; **DEFAULT OFF** (``max == 0``)
    so an unset/absent ``retry`` is byte-identical in behaviour to the historical
    single-attempt path. On a *retryable* terminal failure the spawn bridge
    re-delegates the **same task on the same handle** (retaining the one already
    held semaphore slot for the whole sequence) up to ``max`` extra attempts,
    sleeping :meth:`backoff_for` with exponential growth between them.

    Model-level causes are deliberately NOT re-escalated here: every child
    ``ToolUseLoop`` run already drives the #54 fallback ladder internally, so a
    fresh attempt gets a fresh ladder — this layer only re-runs a whole child
    whose loop died. ``rejected`` (declined at admission, before the loop) and
    ``cancelled`` (parent-cancelled → ``CancelledError``, re-raised, never
    retried) are structurally unreachable by the retry loop.
    """

    max: int = 0
    on: tuple[str, ...] = ("timeout", "failed")
    backoff: float = 1.0

    # The coarse retry-cause vocabulary. ``failed`` is the catch-all transient
    # terminal failure; ``timeout`` is the timeout-flavoured subset (mapped via
    # the #54 classifier so this layer never re-derives provider semantics).
    _CAUSES: frozenset[str] = frozenset({"timeout", "failed"})

    @classmethod
    def from_value(cls, value: object) -> RetryPolicy:
        """Parse + validate the schema ``retry`` object. Unset/invalid → OFF.

        Validation is total (never raises): a malformed field degrades to the
        safe default rather than failing a spawn, since ``retry`` is an optional
        resilience hint, not a correctness contract.
        """
        if not isinstance(value, Mapping):
            return cls()
        raw_max: Any = value.get("max", 0)
        try:
            max_retries = max(0, int(raw_max))
        except (TypeError, ValueError):
            max_retries = 0
        on_val = value.get("on")
        if isinstance(on_val, (list, tuple)):
            on = tuple(str(x) for x in on_val if str(x) in cls._CAUSES)
        else:
            on = ("timeout", "failed")
        if not on:  # an explicit-but-empty/invalid list falls back to both
            on = ("timeout", "failed")
        raw_backoff: Any = value.get("backoff", 1.0)
        try:
            backoff = max(0.0, float(raw_backoff))
        except (TypeError, ValueError):
            backoff = 1.0
        return cls(max=max_retries, on=on, backoff=backoff)

    @property
    def enabled(self) -> bool:
        """True when at least one retry is permitted."""
        return self.max > 0

    def should_retry(self, cause: str, attempt: int) -> bool:
        """True when another attempt is allowed for this failure ``cause``.

        ``attempt`` is the number of attempts made so far (the one that just
        failed). Total attempts are bounded at ``max + 1``.
        """
        return attempt <= self.max and cause in self.on

    def backoff_for(self, attempt: int) -> float:
        """Exponential backoff (seconds) before the next attempt.

        ``attempt`` is the failed attempt's index (1-based), so the first retry
        waits ``backoff``, the second ``2 * backoff``, etc.
        """
        return self.backoff * (2 ** (max(1, attempt) - 1))

    @staticmethod
    def classify_cause(exc: BaseException) -> str:
        """Map a child-loop exception to a coarse retry cause.

        Reuses the #54 ``RetryStrategy`` classifier's reason taxonomy (DRY — the
        delegation layer never re-derives provider/timeout semantics): a
        timeout/deadline-flavoured failure is ``"timeout"``; everything else
        (transient or otherwise) collapses to the generic ``"failed"``.
        """
        from mewbo_core.llm_resilience import LlmResilienceExhausted, RetryStrategy

        reason = ""
        inner: BaseException = exc
        if isinstance(exc, LlmResilienceExhausted):
            reason = exc.reason or ""
            inner = exc.last_error or exc
        if reason not in ("timeout", "deadline"):
            reason = RetryStrategy.classify(inner).reason
        return "timeout" if reason in ("timeout", "deadline") else "failed"


def _coerce_list(value: object) -> list[str]:
    """Coerce a config value to a list of strings."""
    if isinstance(value, list):
        return [str(v) for v in value if v]
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()]
    return []


# ---------------------------------------------------------------------------
# Plugin-generic body substitution (KISS — no Jinja, no template engine)
# ---------------------------------------------------------------------------

# ``${VAR:-default}`` — bash-style fallback. ``\w+`` caps the variable name
# to identifier characters; ``[^}]*`` keeps the default body shell-literal
# (no nested ``}``) without needing a full parser.
_BASH_DEFAULT_RE = re.compile(r"\$\{(\w+):-([^}]*)\}")


def substitute_agent_body(
    body: str,
    subs: Mapping[str, str],
    env: Mapping[str, str] | None = None,
) -> str:
    """Render an agent's body with plugin-generic variable substitution.

    Three passes, in order:

    1. ``${KEY}`` literal substitution from *subs*. Core passes
       ``SESSION_ID`` and ``CLAUDE_PLUGIN_ROOT``; plugins author their
       prompts against these names.
    2. Bash-style ``${VAR:-default}`` — if ``VAR`` is unset in *env*,
       the text expands to ``default``. If ``VAR`` is set, it expands
       to the env value. This keeps plugin prompts self-documenting
       (operator override path is obvious in the source).
    3. Plain ``$VAR`` expansion as a final pass, matching
       :func:`os.path.expandvars` semantics. Unset variables remain
       literal so authors can spot typos at glance.
    """
    if env is None:
        env = os.environ
    # Pass 1: direct substitutions.
    for key, value in subs.items():
        body = body.replace(f"${{{key}}}", value)

    # Pass 2: bash-style ${VAR:-default}. Read from env; fall back to default.
    def _bash_default(match: re.Match[str]) -> str:
        var_name, default = match.group(1), match.group(2)
        return env.get(var_name, default)

    body = _BASH_DEFAULT_RE.sub(_bash_default, body)

    # Pass 3: plain ``$VAR`` expansion for anything still referencing env.
    # Matches ``os.path.expandvars`` semantics without touching the real
    # ``os.environ`` when a test supplies a fake *env* mapping.
    def _plain_var(match: re.Match[str]) -> str:
        var_name = match.group(1)
        return env.get(var_name, match.group(0))

    return re.sub(r"\$(\w+)", _plain_var, body)


# ------------------------------------------------------------------
# Tool schema (injected into bind_tools, NOT in ToolRegistry)
# ------------------------------------------------------------------

SPAWN_AGENT_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "spawn_agent",
        "description": (
            "Spawn a sub-agent for a genuinely independent subtask that "
            "benefits from parallel execution. Do NOT use for simple "
            "sequential operations — use your tools directly instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The specific task for the sub-agent to complete",
                },
                "model": {
                    "type": "string",
                    "description": "Optional model override for this sub-agent",
                },
                "allowed_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tool IDs the sub-agent is allowed to use. Empty = all tools.",
                },
                "denied_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tool IDs explicitly denied to the sub-agent.",
                },
                "max_steps": {
                    "type": "integer",
                    "description": (
                        "Deprecated. Sub-agents run until natural completion. "
                        "This field is retained for prompt compatibility but "
                        "is not enforced."
                    ),
                },
                "acceptance_criteria": {
                    "type": "string",
                    "description": (
                        "How to verify this sub-task is complete "
                        "(e.g., 'file exists and tests pass'). "
                        "Ref: [DeepMind-Delegation §4.1] Contract-first decomposition."
                    ),
                },
                "agent_type": {
                    "type": "string",
                    "description": (
                        "Name of a registered agent type to use "
                        "(e.g. 'feature-dev:code-reviewer'). "
                        "Loads pre-defined system prompt, tool scope, and model "
                        "from the agent registry."
                    ),
                },
                "retry": {
                    "type": "object",
                    "description": (
                        "Optional bounded auto-retry for THIS sub-agent. Default "
                        "off. On a transient terminal failure the SAME task is "
                        "re-delegated up to 'max' times with exponential backoff "
                        "— use for a wide fan-out so one transient failure does "
                        "not silently drop a workstream. Model-level failures "
                        "already reuse the built-in fallback ladder within each "
                        "attempt; cancelled/rejected agents are never retried."
                    ),
                    "properties": {
                        "max": {
                            "type": "integer",
                            "description": "Max extra retry attempts (0 = off, default).",
                        },
                        "on": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["timeout", "failed"]},
                            "description": "Failure causes to retry. Default: both.",
                        },
                        "backoff": {
                            "type": "number",
                            "description": "Base backoff seconds between attempts. Default 1.0.",
                        },
                    },
                },
            },
            "required": ["task"],
        },
    },
}


# Batch fan-out (Gitea #117). The array's ``items`` schema IS the single
# ``spawn_agent`` parameters object (DRY — one source of truth for the per-task
# fields), so every entry takes the same fields and a new spawn field is picked
# up by both tools automatically.
SPAWN_AGENTS_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "spawn_agents",
        "description": (
            "Fan out MULTIPLE independent sub-agents in ONE call — the preferred "
            "path when you have N genuinely independent subtasks. Every entry is "
            "admitted together (reliable parallel admission even if you can't emit "
            "N parallel tool-calls), returning an ORDERED list of agent_ids you "
            "monitor with check_agents. Each entry takes the SAME fields as "
            "spawn_agent. Entries that can't get a concurrency slot come back "
            "'rejected' in their slot without affecting their siblings. Do NOT "
            "use for sequential work — use your tools directly."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "items": SPAWN_AGENT_SCHEMA["function"]["parameters"],  # type: ignore[index]
                    "description": (
                        "Independent sub-tasks to spawn concurrently. Order is "
                        "preserved in the returned agent_ids."
                    ),
                },
            },
            "required": ["tasks"],
        },
    },
}


CHECK_AGENTS_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "check_agents",
        "description": (
            "Check the status of all spawned sub-agents. Returns the agent "
            "tree with progress notes and completed results. Use after "
            "spawning agents to monitor progress and collect results. "
            "Ref: [DeepMind-Delegation §4.5] Process-level monitoring."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "wait": {
                    "type": "boolean",
                    "description": (
                        "If true, wait up to timeout seconds for at least "
                        "one running agent to complete before returning. "
                        "Default: false."
                    ),
                },
                "timeout": {
                    "type": "number",
                    "description": ("Max seconds to wait when wait=true. Default: 30."),
                },
            },
            "required": [],
        },
    },
}

STEER_AGENT_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "steer_agent",
        "description": (
            "Send a steering message to a running sub-agent, or cancel it. "
            "Use to inject context, course-correct, or stop stuck agents. "
            "Ref: [DeepMind-Delegation §4.4] Adaptive coordination."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": (
                        "The agent_id to steer (8-char prefix from check_agents output)."
                    ),
                },
                "action": {
                    "type": "string",
                    "enum": ["message", "cancel"],
                    "description": (
                        "Action: 'message' sends NL feedback to the agent, "
                        "'cancel' cancels the agent."
                    ),
                },
                "message": {
                    "type": "string",
                    "description": (
                        "The steering message to send (required when action='message')."
                    ),
                },
            },
            "required": ["agent_id", "action"],
        },
    },
}


# ------------------------------------------------------------------
# SpawnAgentTool
# ------------------------------------------------------------------


class SpawnAgentTool:
    """Spawns a child ToolUseLoop as a sub-agent."""

    def __init__(
        self,
        *,
        agent_context: AgentContext,
        tool_registry: ToolRegistry,
        permission_policy: PermissionPolicy,
        approval_callback: Callable[[ActionStep], bool] | None = None,
        hook_manager: HookManager,
        project_instructions: str | None = None,
        cwd: str | None = None,
        agent_registry: Any = None,
        session_tool_registry: SessionToolRegistry | None = None,
        session_capabilities: tuple[str, ...] = (),
        enable_skills: bool = True,
    ) -> None:
        """Initialize with parent context and shared registries."""
        self._agent_context = agent_context
        self._tool_registry = tool_registry
        self._permission_policy = permission_policy
        self._approval_callback = approval_callback
        self._hook_manager = hook_manager
        self._project_instructions = project_instructions
        self._cwd = cwd
        self._agent_registry = agent_registry
        self._session_tool_registry = session_tool_registry
        self._session_capabilities = session_capabilities
        # Children inherit the parent drive's skill policy: a headless search
        # run disables auto-skill injection for the ROOT *and* every probe it
        # spawns (the audit found every server-side agent burning step 1 on
        # ``activate_skill``).
        self._enable_skills = enable_skills
        # Plan-mode context — set by ToolUseLoop.run() so children
        # inherit the session's plan path and mode.
        self.session_id: str | None = None
        self.parent_mode: str = "act"
        # Track lifecycle manager tasks for deterministic cleanup.
        self._lifecycle_tasks: list[asyncio.Task[None]] = []

    async def run_async(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a single sub-agent. Returns the result as a MockSpeaker.

        Thin wrapper over :meth:`_spawn_one` (blocking admission) — the batch
        path (:meth:`run_batch_async`) shares the same core.
        """
        args = (
            action_step.tool_input
            if isinstance(action_step.tool_input, dict)
            else {"task": str(action_step.tool_input)}
        )
        outcome = await self._spawn_one(args, blocking_admit=True)
        return MockSpeaker(content=outcome.content)

    async def run_batch_async(self, action_step: ActionStep) -> MockSpeaker:
        """Fan out a batch of independent sub-agents from ONE tool call (#117).

        Pure composition over :meth:`_spawn_one` — every entry is admitted
        through the SAME hypervisor semaphore (non-blocking, so an
        over-subscribed batch marks the surplus ``rejected`` instead of
        stalling) and root children run on the existing non-blocking lifecycle
        path. Returns the ordered ``agent_id``s; the model collects results via
        the existing ``check_agents``. The orchestration loop is untouched.
        """
        raw = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        raw_tasks = raw.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            return MockSpeaker(
                content="ERROR: spawn_agents requires a non-empty 'tasks' array."
            )
        try:
            tasks = [SpawnAgentTask.model_validate(entry) for entry in raw_tasks]
        except ValidationError as exc:
            return MockSpeaker(content=f"ERROR: invalid spawn_agents task: {exc}")

        agents: list[dict[str, Any]] = []
        agent_ids: list[str | None] = []
        spawned = 0
        rejected = 0
        for idx, task in enumerate(tasks):
            # blocking_admit=False → siblings never stall behind one full slot.
            outcome = await self._spawn_one(task.to_args(), blocking_admit=False)
            agent_ids.append(outcome.agent_id)
            if outcome.agent_id is not None:
                spawned += 1
            else:
                rejected += 1
            agents.append(
                {
                    "index": idx,
                    "agent_id": outcome.agent_id,
                    "status": outcome.status,
                    "task": task.task[:200],
                }
            )

        summary = f"Spawned {spawned}/{len(tasks)} agent(s)"
        if rejected:
            summary += f"; {rejected} rejected (no free concurrency slot)"
        summary += ". Use check_agents to monitor progress and collect results."
        return MockSpeaker(
            content=json.dumps(
                {
                    "kind": "agent_batch",
                    "text": summary,
                    "agents": agents,
                    "agent_ids": agent_ids,
                    "spawned": spawned,
                    "rejected": rejected,
                }
            )
        )

    async def _spawn_one(
        self, args: dict[str, Any], *, blocking_admit: bool
    ) -> _SpawnOutcome:
        """Admit and launch ONE sub-agent — shared core of single + batch spawn.

        ``blocking_admit`` selects the admission mode: ``True`` (single spawn)
        waits up to the hypervisor timeout for a slot; ``False`` (batch fan-out)
        tries non-blocking so an over-subscribed batch rejects the surplus entry
        in place. Returns a :class:`_SpawnOutcome` carrying the verbatim
        single-tool ``content`` plus the structured ``agent_id``/``status``.
        """
        from mewbo_core.prompt_registry import get_prompt_registry

        registry_prompts = get_prompt_registry()
        task_desc = str(args.get("task", ""))
        acceptance_criteria = str(args.get("acceptance_criteria", "") or "")
        if acceptance_criteria:
            # Ref: [DeepMind-Delegation §4.1] Contract-first decomposition —
            # delegation is contingent upon the outcome having precise verification.
            task_desc += registry_prompts.render(
                "spawn.acceptance_criteria", acceptance_criteria=acceptance_criteria
            )
        model_override = args.get("model")

        # agent_type: look up registered agent definition and apply its config.
        agent_type = args.get("agent_type")
        if agent_type and self._agent_registry:
            agent_def = self._agent_registry.get(
                agent_type, self._session_capabilities
            )
            if agent_def is None:
                return _SpawnOutcome(
                    content=f"ERROR: Unknown agent type '{agent_type}'",
                    agent_id=None,
                    status="rejected",
                )
            # Prepend agent system prompt to task, running the plugin-generic
            # body substitution first so ``${SESSION_ID}``,
            # ``${CLAUDE_PLUGIN_ROOT}``, and bash-style ``${VAR:-default}``
            # expansions resolve before the body hits the child LLM.
            body_subs = {
                "SESSION_ID": self.session_id or "",
                "CLAUDE_PLUGIN_ROOT": agent_def.plugin_root,
            }
            rendered_body = substitute_agent_body(agent_def.body, body_subs)
            task_desc = registry_prompts.render(
                "spawn.task_body", body=rendered_body, task=task_desc
            )
            # Apply agent's tool scope if specified and not overridden by caller
            if agent_def.allowed_tools and "allowed_tools" not in args:
                args["allowed_tools"] = agent_def.allowed_tools
            if agent_def.denied_tools and "denied_tools" not in args:
                args["denied_tools"] = agent_def.denied_tools
            # Apply agent's model if specified.
            # Registered agent types with a configured model are authoritative.
            # LLM's model arg on spawn_agent is ignored — config has already made this decision.
            # (Ad-hoc spawns without agent_type continue to honor the LLM's model arg.)
            if agent_def.model:
                model_override = agent_def.model

        registry = self._agent_context.registry

        # 1. Resolve and validate model.
        resolved_model = self._resolve_model(model_override)
        if resolved_model.startswith("ERROR:"):
            return _SpawnOutcome(content=resolved_model, agent_id=None, status="rejected")

        # 2. Admission control. Blocking single-spawn waits for a slot; the
        # batch path admits non-blocking so the surplus is rejected, not stalled.
        admitted = await registry.admit() if blocking_admit else await registry.try_admit()
        if not admitted:
            return _SpawnOutcome(
                content="ERROR: Max concurrent agents reached. Try again later.",
                agent_id=None,
                status="rejected",
            )

        child_ctx: AgentContext | None = None
        handle: AgentHandle | None = None
        tq = None  # Initialized early so error handlers can read partial results
        # Set once the non-blocking root path hands slot ownership to the
        # background lifecycle manager — the finally must then NOT release (the
        # lifecycle manager releases exactly once when the child settles).
        slot_transferred = False
        try:
            # 3. Create child context.
            child_ctx = self._agent_context.child(model_name=resolved_model)

            # 4. Register in registry.
            # Ref: [A2A v1.0] Agent starts as "submitted", transitions to "running"
            handle = AgentHandle(
                agent_id=child_ctx.agent_id,
                parent_id=child_ctx.parent_id,
                depth=child_ctx.depth,
                model_name=child_ctx.model_name,
                task_description=task_desc[:200],
                # The AgentDef name this child was spawned as (``None`` for an
                # ad-hoc spawn) — carried on the handle so every lifecycle event
                # (incl. the background ``stop`` in ``_run_child_lifecycle``,
                # which only holds the handle) can stamp the lane identity.
                agent_type=agent_type if isinstance(agent_type, str) else None,
                status="submitted",
                message_queue=child_ctx.message_queue,
            )
            await registry.register(handle)
            self._hook_manager.run_on_agent_start(handle)
            self._emit_event(child_ctx, "start", task_desc, handle=handle)

            # 5. Filter tool specs (Claude Code "filter before binding" pattern).
            child_specs = self._filter_tool_specs(args)

            # 6. Resolve child tool scope + opt-in bounded-retry policy (#118).
            # Ref: [DeepMind-Delegation §4.7] Privilege attenuation — sub-agents
            # inherit parent's approval policy (not None, which blocks all writes).
            child_allowed_tools = _coerce_list(args.get("allowed_tools") or []) or None
            retry = RetryPolicy.from_value(args.get("retry"))
            # Ref: [A2A v1.0] Transition to "running" when execution begins.
            handle.status = "running"

            # Ref: [DeepMind-Delegation §4.4] Root agent delegates non-blockingly
            # to maintain continuous monitoring capability (epoll model).
            if self._agent_context.depth == 0:
                # Non-blocking: the lifecycle manager drives the child (with
                # bounded retry) and stores the result in the background.
                lm_task = asyncio.create_task(
                    self._run_child_lifecycle(
                        child_ctx,
                        handle,
                        child_specs,
                        child_allowed_tools,
                        task_desc,
                        retry,
                    )
                )
                self._lifecycle_tasks.append(lm_task)
                # Store child_id before clearing refs (finally guard)
                child_id = child_ctx.agent_id
                # Prevent finally block from cleaning up — lifecycle manager owns
                # both the handle AND the semaphore slot (released once on settle).
                child_ctx = None
                handle = None
                slot_transferred = True
                return _SpawnOutcome(
                    content=json.dumps(
                        {
                            "agent_id": child_id,
                            "status": "submitted",
                            "task": task_desc[:200],
                            "message": (
                                "Agent spawned. Use check_agents to monitor "
                                "progress and collect results."
                            ),
                        }
                    ),
                    agent_id=child_id,
                    status="submitted",
                )

            # Blocking: current behavior for non-root agents. The retry driver
            # re-delegates the SAME task on a retryable terminal failure (a
            # single attempt when retry is off), raising the last error once the
            # attempt budget is spent.
            tq, state = await self._drive_with_retry(
                child_ctx=child_ctx,
                handle=handle,
                child_specs=child_specs,
                child_allowed_tools=child_allowed_tools,
                task_desc=task_desc,
                retry=retry,
            )

            # 7. Mark done.
            await registry.mark_done(child_ctx.agent_id, "completed")
            self._hook_manager.run_on_agent_stop(handle)
            self._emit_event(
                child_ctx,
                "stop",
                state.done_reason or "completed",
                handle=handle,
                summary=(tq.task_result or "")[:_SUB_AGENT_SUMMARY_CAP],
            )

            # Ref: [CoA §3.1] Build Communication Unit — compressed context for parent
            from mewbo_core.hypervisor import AgentResult

            result = AgentResult(
                content=tq.task_result or state.done_reason or "No result",
                status="completed" if state.done else "failed",
                steps_used=handle.steps_completed,
                summary=(tq.task_result or "")[:500],
                attempts=handle.attempts,
            )
            return _SpawnOutcome(
                content=json.dumps(asdict(result)),
                agent_id=child_ctx.agent_id,
                status=result.status,
            )

        except AgentDepthExceeded as exc:
            # child() raises this before `handle` is built or registered, so
            # there is nothing registered to mark done — just surface the result.
            from mewbo_core.hypervisor import AgentResult

            result = AgentResult(
                content=f"Depth exceeded: {exc}",
                status="cannot_solve",
                steps_used=0,
                warnings=[str(exc)],
            )
            return _SpawnOutcome(
                content=json.dumps(asdict(result)),
                agent_id=None,
                status="cannot_solve",
            )

        except asyncio.CancelledError:
            if child_ctx:
                await registry.mark_done(child_ctx.agent_id, "cancelled")
            if handle:
                self._hook_manager.run_on_agent_stop(handle)
            raise  # Re-raise for TaskGroup propagation.

        except Exception as exc:
            logging.error("Sub-agent failed: {}", exc)
            # Build structured error with context from the handle.
            agent_error = AgentError(
                agent_id=child_ctx.agent_id if child_ctx else "unknown",
                depth=child_ctx.depth if child_ctx else 0,
                task=task_desc[:200],
                error=str(exc),
                last_tool=handle.last_tool_id if handle else None,
                steps_completed=handle.steps_completed if handle else 0,
            )
            if child_ctx:
                await registry.mark_done(
                    child_ctx.agent_id,
                    "failed",
                    error=agent_error,
                )
            if handle:
                self._hook_manager.run_on_agent_stop(handle)
            from mewbo_core.hypervisor import AgentResult

            partial_result = (tq.task_result or "")[:500] if tq is not None else ""
            result = AgentResult(
                content=f"Sub-agent failed: {exc}",
                status="failed",
                steps_used=handle.steps_completed if handle else 0,
                warnings=[str(exc)],
                # Ref: [DeepMind-Delegation §6.1] Checkpoint — partial work survives failure
                summary=partial_result,
                # #118 — the last error after a spent retry budget; attempts shows
                # how many re-delegations were tried before giving up.
                attempts=handle.attempts if handle else 1,
            )
            return _SpawnOutcome(
                content=json.dumps(asdict(result)),
                agent_id=child_ctx.agent_id if child_ctx else None,
                status="failed",
            )

        finally:
            if child_ctx:
                # Cancel any children spawned by this sub-agent.
                children = await registry.list_children(child_ctx.agent_id)
                for child in children:
                    if child.status == "running":
                        await registry.cancel_agent(child.agent_id)

                await registry.unregister(child_ctx.agent_id)
            # Skip the release when ownership transferred to the background
            # lifecycle manager (root non-blocking path) — releasing here too
            # would double-release the slot and inflate the semaphore (#117).
            if not slot_transferred:
                registry.release()

    # ------------------------------------------------------------------
    # Model resolution
    # ------------------------------------------------------------------

    def _resolve_model(self, model_override: object) -> str:
        """Resolve model for the child agent.

        Returns model name, or ``"ERROR: ..."`` string on validation failure.
        """
        allowed_models = _coerce_list(get_config_value("agent", "allowed_models", default=[]))
        default_sub = str(get_config_value("agent", "default_sub_model", default="") or "").strip()

        if model_override and isinstance(model_override, str):
            model = model_override.strip()
            if allowed_models and model not in allowed_models:
                return (
                    f"ERROR: Model '{model}' not in allowed_models. "
                    f"Available: {', '.join(allowed_models)}"
                )
            return model

        if default_sub:
            return default_sub

        return self._agent_context.model_name

    # ------------------------------------------------------------------
    # Tool spec filtering (Claude Code "filter before binding" pattern)
    # ------------------------------------------------------------------

    def _filter_tool_specs(self, args: dict[str, Any]) -> list[ToolSpec]:
        """Filter tool specs for a child agent.

        Denied tools are removed from the child's ``bind_tools()`` list —
        the child LLM never sees them.
        """
        return filter_specs(
            self._tool_registry.list_specs(),
            allowed=args.get("allowed_tools") or None,
            denied=_coerce_list(args.get("denied_tools") or []),
        )

    # ------------------------------------------------------------------
    # Agent management handlers (root-only, Ref: [DeepMind-Delegation §4.5])
    # ------------------------------------------------------------------

    async def handle_check_agents(self, action_step: ActionStep) -> MockSpeaker:
        """Return agent tree state with completed results and progress.

        Emits a JSON payload with ``kind: "agent_tree"``. The ``text`` field
        carries the rendered ASCII tree the LLM consumes; the ``agents`` list
        is the structured snapshot the console uses to render CheckAgentsCard.
        """
        args = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        wait = bool(args.get("wait", False))
        timeout = float(args.get("timeout", 30))
        registry = self._agent_context.registry
        parent_id = self._agent_context.agent_id

        if wait:
            running = await registry.collect_running(parent_id)
            if running:
                waiters = [asyncio.create_task(h.done_event.wait()) for h in running]
                # asyncio.wait() does not raise on timeout — it returns
                # (done, pending) with pending non-empty when the deadline hits.
                _done, pending = await asyncio.wait(
                    waiters,
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()

        # Build response.
        tree = await registry.render_agent_tree(
            exclude_agent_id=self._agent_context.agent_id,
        )
        completed = await registry.collect_completed(parent_id)
        running = await registry.collect_running(parent_id)

        parts: list[str] = []
        if tree:
            parts.append(f"Agent tree:\n{tree}")

        if completed:
            parts.append("\nCompleted results:")
            for h in completed:
                r = h.result
                if r:
                    parts.append(f"  [{h.agent_id[:8]}] {r.status}: {r.summary or r.content[:300]}")

        if running:
            parts.append(f"\n{len(running)} agent(s) still running.")
        elif not completed:
            parts.append("No agents spawned.")

        text = "\n".join(parts) or "No agents."

        agents_payload = [
            {
                "id": h.agent_id,
                "parent_id": h.parent_id,
                "depth": h.depth,
                "task": h.task_description,
                "status": h.status,
                "steps_completed": h.steps_completed,
                "last_tool_id": h.last_tool_id,
                "progress_note": h.progress_note,
                "compaction_count": h.compaction_count,
                "attempts": h.attempts,  # #118 — retry provenance for the console
                "result": (
                    {
                        "status": h.result.status,
                        "summary": h.result.summary,
                        "content": h.result.content,
                    }
                    if h.result is not None
                    else None
                ),
            }
            for h in await registry.list_visible(
                exclude_agent_id=self._agent_context.agent_id,
            )
        ]

        payload = {
            "kind": "agent_tree",
            "text": text,
            "agents": agents_payload,
            "parent_id": parent_id,
            "wait": wait,
        }
        return MockSpeaker(content=json.dumps(payload))

    async def handle_steer_agent(self, action_step: ActionStep) -> MockSpeaker:
        """Send a steering message to or cancel a running agent."""
        args = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        agent_id = str(args.get("agent_id", ""))
        action = str(args.get("action", ""))
        message = str(args.get("message", ""))
        registry = self._agent_context.registry

        # Resolve short prefix to full agent_id.
        handle = await registry.get(agent_id)
        if handle is None:
            all_agents = await registry.list_all()
            matches = [h for h in all_agents if h.agent_id.startswith(agent_id)]
            if len(matches) == 1:
                handle = matches[0]
                agent_id = handle.agent_id
            elif len(matches) > 1:
                return MockSpeaker(
                    content=f"ERROR: Ambiguous prefix '{agent_id}' matches {len(matches)} agents.",
                )
            else:
                return MockSpeaker(
                    content=f"ERROR: Agent '{agent_id}' not found.",
                )

        if action == "cancel":
            reason = await registry.cancel_agent(agent_id)
            if reason is None:
                return MockSpeaker(
                    content=f"Agent {agent_id[:8]} cancelled.",
                )
            return MockSpeaker(
                content=f"Agent {agent_id[:8]} cannot cancel: {reason}",
            )
        if action == "message":
            if not message:
                return MockSpeaker(
                    content="ERROR: 'message' is required when action='message'.",
                )
            reason = await registry.send_message(
                agent_id,
                f"[From parent] {message}",
            )
            if reason is None:
                return MockSpeaker(content="Message sent.")
            return MockSpeaker(content=f"Message failed: {reason}")

        return MockSpeaker(
            content=f"ERROR: Unknown action '{action}'. Use 'message' or 'cancel'.",
        )

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    def _emit_event(
        self,
        ctx: AgentContext,
        action: str,
        detail: str,
        handle: AgentHandle | None = None,
        summary: str | None = None,
    ) -> None:
        """Emit a sub_agent lifecycle event.

        ``summary`` (additive, set only on the terminal ``stop``) carries the
        child's compressed result — the Communication Unit a downstream consumer
        can project as the sub-agent's actual response (e.g. the agentic-search
        trace's per-lane evidence block, where the lifecycle ``detail`` is just
        the ``done_reason``). Omitted on every other phase, so existing consumers
        that read only the legacy keys are unaffected.
        """
        if ctx.event_logger is not None:
            payload: dict[str, Any] = {
                "action": action,
                "agent_id": ctx.agent_id,
                "parent_id": ctx.parent_id,
                "depth": ctx.depth,
                "model": ctx.model_name,
                "detail": detail,
                "status": handle.status if handle else action,
                "steps_completed": handle.steps_completed if handle else 0,
                "input_tokens": handle.input_tokens if handle else 0,
                "output_tokens": handle.output_tokens if handle else 0,
            }
            # ``agent_type`` (additive) carries the spawned AgentDef name so a
            # consumer can label the lane by its DEFINITION (e.g.
            # ``scg-path-probe``) instead of falling back to the model name —
            # the agentic-search trace projection's lane identity. Read off the
            # handle so the terminal ``stop`` (emitted from the background
            # lifecycle manager, which holds only the handle) carries it too.
            # Omitted for an ad-hoc spawn (no ``agent_type``) so legacy consumers
            # reading only the existing keys are untouched.
            if handle is not None and handle.agent_type:
                payload["agent_type"] = handle.agent_type
            if summary:
                payload["summary"] = summary
            event: Event = {"type": "sub_agent", "payload": payload}
            ctx.event_logger(event)

    # ------------------------------------------------------------------
    # Bounded retry driver  (Ref: Gitea #118)
    # ------------------------------------------------------------------

    def _build_child_loop(
        self,
        child_ctx: AgentContext,
        child_allowed_tools: list[str] | None,
    ) -> Any:
        """Construct a fresh child ``ToolUseLoop`` for one attempt.

        A fresh loop per attempt means each retry gets its own #54
        ``RetryStrategy`` (the model-fallback ladder) — so model-level recovery
        is reused, never reinvented at this layer.
        """
        # Import here to avoid a circular import at module load time.
        from mewbo_core.tool_use_loop import ToolUseLoop

        return ToolUseLoop(
            agent_context=child_ctx,
            tool_registry=self._tool_registry,
            permission_policy=self._permission_policy,
            approval_callback=self._approval_callback,
            hook_manager=self._hook_manager,
            project_instructions=self._project_instructions,
            session_tool_registry=self._session_tool_registry,
            allowed_tools=child_allowed_tools,
            cwd=self._cwd,
            session_id=self.session_id,
            session_capabilities=self._session_capabilities,
            enable_skills=self._enable_skills,
        )

    async def _drive_with_retry(
        self,
        *,
        child_ctx: AgentContext,
        handle: AgentHandle,
        child_specs: list[ToolSpec],
        child_allowed_tools: list[str] | None,
        task_desc: str,
        retry: RetryPolicy,
    ) -> tuple[Any, Any]:
        """Run the child loop, re-delegating the SAME task on a retryable failure.

        Returns ``(tq, state)`` from the first successful attempt. Re-raises the
        LAST exception once the attempt budget is spent or the failure cause is
        not in ``retry.on`` (default off ⇒ exactly one attempt, identical to the
        historical path). ``CancelledError`` is never retried — parent
        cancellation is terminal and bubbles straight up.

        Slot discipline (#118): the one semaphore slot already acquired in
        ``run_async`` is *held across all attempts* — re-admission re-uses that
        slot rather than releasing and racing for a new one, so concurrency stays
        bounded exactly as on the no-retry path. ``handle.attempts`` is bumped per
        attempt so the agent tree / ``check_agents`` surface the re-delegation.
        """
        attempt = 0
        while True:
            attempt += 1
            handle.attempts = attempt
            # Each attempt is a fresh run on the SAME handle/agent_id: reset the
            # transient running state (the prior attempt's loop marked it failed
            # in its own finally) so the tree reflects the live attempt.
            handle.status = "running"
            handle.error = None
            child_loop = self._build_child_loop(child_ctx, child_allowed_tools)
            child_task = asyncio.create_task(
                child_loop.run(task_desc, tool_specs=child_specs, mode=self.parent_mode)
            )
            # Populate asyncio_task so cancel_agent() and 3-phase cleanup target
            # the live attempt.
            handle.asyncio_task = child_task
            try:
                return await child_task
            except asyncio.CancelledError:
                raise  # parent cancellation is terminal — never retried
            except Exception as exc:  # noqa: BLE001 — child-loop failure is opaque
                cause = RetryPolicy.classify_cause(exc)
                if not retry.should_retry(cause, attempt):
                    raise
                delay = retry.backoff_for(attempt)
                self._emit_event(
                    child_ctx,
                    "retry",
                    f"attempt {attempt} {cause}; re-delegating (max {retry.max})",
                    handle=handle,
                )
                logging.warning(
                    "Sub-agent {} attempt {} failed ({}); retrying in {:.1f}s",
                    child_ctx.agent_id[:8],
                    attempt,
                    cause,
                    delay,
                )
                if delay > 0:
                    await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    # Non-blocking lifecycle manager  (Ref: [DeepMind-Delegation §4.4])
    # ------------------------------------------------------------------

    async def _run_child_lifecycle(
        self,
        child_ctx: AgentContext,
        handle: AgentHandle,
        child_specs: list[ToolSpec],
        child_allowed_tools: list[str] | None,
        task_desc: str,
        retry: RetryPolicy,
    ) -> None:
        """Background lifecycle manager for non-blocking child execution.

        Drives the child (with bounded retry), stores the ``AgentResult`` on the
        handle, and notifies the parent via ``send_to_parent``.

        Ref: [DeepMind-Delegation §4.5] Lifecycle events at phase transitions.
        Ref: [CoA §3.1] CU stored on handle for async retrieval.
        """
        registry = self._agent_context.registry
        tq = None
        try:
            tq, state = await self._drive_with_retry(
                child_ctx=child_ctx,
                handle=handle,
                child_specs=child_specs,
                child_allowed_tools=child_allowed_tools,
                task_desc=task_desc,
                retry=retry,
            )

            await registry.mark_done(child_ctx.agent_id, "completed")
            self._hook_manager.run_on_agent_stop(handle)
            self._emit_event(
                child_ctx,
                "stop",
                state.done_reason or "completed",
                handle=handle,
                summary=(tq.task_result or "")[:_SUB_AGENT_SUMMARY_CAP],
            )

            from mewbo_core.hypervisor import AgentResult

            handle.result = AgentResult(
                content=tq.task_result or state.done_reason or "No result",
                status="completed" if state.done else "failed",
                steps_used=handle.steps_completed,
                summary=(tq.task_result or "")[:500],
                attempts=handle.attempts,
            )

        except asyncio.CancelledError:
            await registry.mark_done(child_ctx.agent_id, "cancelled")
            self._hook_manager.run_on_agent_stop(handle)

            from mewbo_core.hypervisor import AgentResult

            handle.result = AgentResult(
                content="Cancelled",
                status="cancelled",
                steps_used=handle.steps_completed,
                attempts=handle.attempts,
            )

        except Exception as exc:
            logging.error("Sub-agent lifecycle failed: {}", exc)
            agent_error = AgentError(
                agent_id=child_ctx.agent_id,
                depth=child_ctx.depth,
                task=task_desc[:200],
                error=str(exc),
                last_tool=handle.last_tool_id,
                steps_completed=handle.steps_completed,
            )
            await registry.mark_done(
                child_ctx.agent_id,
                "failed",
                error=agent_error,
            )
            self._hook_manager.run_on_agent_stop(handle)

            from mewbo_core.hypervisor import AgentResult

            partial = (tq.task_result or "")[:500] if tq is not None else ""
            handle.result = AgentResult(
                content=f"Sub-agent failed: {exc}",
                status="failed",
                steps_used=handle.steps_completed,
                warnings=[str(exc)],
                summary=partial,
                attempts=handle.attempts,
            )

        finally:
            # Cascade cleanup to children of this child.
            children = await registry.list_children(child_ctx.agent_id)
            for child in children:
                if child.status == "running":
                    await registry.cancel_agent(child.agent_id)

            # Notify parent before releasing the semaphore slot.
            # Result and status are set in the try/except blocks above.
            # The handle stays in the registry so check_agents and
            # render_agent_tree can surface the result; session cleanup()
            # clears it at session end.
            if handle and handle.result:
                notification = (
                    f"[Agent {child_ctx.agent_id[:8]} {handle.result.status}] "
                    f"Task: {task_desc} | "
                    f"{handle.result.summary or handle.result.content[:300]}"
                )
            else:
                _status = handle.status if handle else "unknown"
                notification = f"[Agent {child_ctx.agent_id[:8]} {_status}] Task: {task_desc}"
            await registry.send_to_parent(child_ctx.agent_id, notification)

            registry.release()

    async def await_lifecycle_managers(self, timeout: float = 3.0) -> None:
        """Wait for background lifecycle managers to complete cleanup.

        Called from ``ToolUseLoop.run()`` finally block to ensure
        deterministic cleanup before the event loop tears down.
        """
        pending = [t for t in self._lifecycle_tasks if not t.done()]
        if pending:
            _done, still_pending = await asyncio.wait(
                pending,
                timeout=timeout,
                return_when=asyncio.ALL_COMPLETED,
            )
            for task in still_pending:
                task.cancel()
        self._lifecycle_tasks.clear()


__all__ = [
    "AgentError",
    "CHECK_AGENTS_SCHEMA",
    "RetryPolicy",
    "SPAWN_AGENT_SCHEMA",
    "SPAWN_AGENTS_SCHEMA",
    "STEER_AGENT_SCHEMA",
    "SpawnAgentTask",
    "SpawnAgentTool",
    "substitute_agent_body",
]
