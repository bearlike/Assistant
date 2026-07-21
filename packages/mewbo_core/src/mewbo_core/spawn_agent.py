#!/usr/bin/env python3
"""Sub-agent spawning tool for the agent hypervisor.

``SpawnAgentTool`` creates a child ``ToolUseLoop`` instance, registers it
in the ``AgentHypervisor``, runs it to completion, and returns the result.
Tool scoping follows the "filter before binding" pattern: denied
tools are removed from the child's ``bind_tools()`` list so the child LLM
never sees them.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any, ClassVar, Literal, cast, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mewbo_core.agent_context import AgentContext, AgentDepthExceeded
from mewbo_core.classes import (
    UNACHIEVED_DONE_REASONS,
    ActionStep,
    OrchestrationState,
    TaskQueue,
)
from mewbo_core.common import MockSpeaker, get_logger
from mewbo_core.config import get_config_value
from mewbo_core.hooks import HookManager
from mewbo_core.hypervisor import (
    ACTIVE_STATUSES,
    AgentHandle,
    AgentStatus,
    AutonomyTier,
    DelegationContract,
    ModelTier,
    SummaryKind,
)
from mewbo_core.permissions import PermissionPolicy
from mewbo_core.run_error import RunError
from mewbo_core.session_tools import SessionToolRegistry
from mewbo_core.tool_registry import CapabilityMode, ToolRegistry, ToolSpec, filter_specs
from mewbo_core.types import Event
from mewbo_core.verification import CommandVerification
from mewbo_core.workspace import WorkspaceMode

logging = get_logger(name="core.spawn_agent")

# Delegation approval policy. PARSED + carried on the spawn
# schema in v1 but ENFORCED by nothing yet — the approval-gate wiring lands in a
# later wave. Declared here as the vocabulary the schema pins via ``get_args``.
ApprovalPolicy = Literal["never", "on_failure", "on_request"]

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
    """One entry in a ``spawn_agents`` batch.

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
    # Opt-in bounded auto-retry, parsed downstream by ``RetryPolicy``.
    # A batch entry can carry it just like a single spawn — one transient
    # failure in a wide fan-out then re-delegates instead of dropping a lane.
    retry: dict[str, Any] | None = None
    # Opt-in per-agent delegation bounds, parsed downstream by
    # ``DelegationContract``. A batch entry gets the same LAYERED-UNDER-the-
    # session-budget ceiling as a single spawn.
    contract: dict[str, Any] | None = None
    # Opt-in ground-truth completion check, parsed downstream by
    # ``CommandVerification.from_value``. When the master switch is on AND this
    # child can act (capability_mode ∈ {execute, all}), its claimed completion
    # is gated behind this command passing. Default off / inactive is the
    # historical ungated path; a supplied-but-inactive spec is surfaced in the
    # spawn response + event, never silently dropped.
    verification: dict[str, Any] | None = None
    # Task-typed Communication Unit shape ([CoA §3]) for
    # this sub-agent's final summary. ``None`` (default) is the historical
    # untyped path — ``_spawn_one`` stamps ``AgentResult.summary_kind`` with
    # its own "generic" default in that case, so an unset field changes
    # nothing about a spawn's behaviour or output.
    summary_kind: SummaryKind | None = None
    # Coarse delegation privilege ceiling ([DeepMind-Delegation §4.7]
    # privilege attenuation). A pre-filter LAYERED UNDER
    # ``allowed_tools``/``denied_tools`` — it can only remove more tools, never
    # add. Gates BOTH surfaces (the two-surface law): file/registry tools
    # via ``filter_specs`` AND per-agent session action tools via
    # ``SessionToolRegistry.build_for`` (session tools default to tier
    # ``execute``, so ``read_only`` admits none unless declared ``read``).
    # ``"all"`` (default) = no capability filtering, byte-identical to the
    # historical path. Narrowed monotonically against the parent's effective
    # mode at spawn time, so a child can only ever restrict further. See
    # ``CapabilityMode`` for the tier law.
    capability_mode: CapabilityMode = "all"
    # Filesystem-containment ceiling — the SECOND privilege axis,
    # orthogonal to ``capability_mode``. Defaults to ``workspace_write`` (the
    # sensible sub-agent default: reads + writes confined to the workspace), yet
    # because narrowing is min-wins against the parent's own tier AND the ROOT
    # default is ``full_access``, a child spawned off a default root resolves to
    # ``workspace_write`` — a change that is INERT until ``agent.workspace_
    # enforcement`` is flipped on (the staged kill-switch), at which point it
    # begins confining paths. Narrowed monotonically, so a child can only ever
    # restrict further. See ``WorkspaceContainment`` for the tier law.
    workspace_mode: WorkspaceMode = "workspace_write"
    # Delegation approval policy — PARSED + validated here but
    # ENFORCED by nothing in v1 (the approval-gate wiring is a later wave). Kept
    # on the schema now so the wire contract is forward-stable; a spawn that sets
    # it today behaves exactly as ``on_failure`` (i.e. no gate).
    approval_policy: ApprovalPolicy = "on_failure"

    # The ONE mapping from a declared
    # ``summary_kind`` to the one-line directive appended to the child's task
    # text. A `Literal`-keyed class constant, not a per-call `if kind ==`
    # chain: ``_spawn_one`` does a plain dict lookup. ``"generic"`` has no
    # entry — it is the untyped default and appends nothing.
    SUMMARY_KIND_DIRECTIVES: ClassVar[dict[SummaryKind, str]] = {
        "evidence": (
            "Your final summary must be an evidence package: the "
            "facts/quotes/paths the parent needs, not narrative."
        ),
        "running_summary": (
            "Your final summary must be a running summary: the task's "
            "cumulative state so far, written so a fresh reader needs no "
            "prior turns to pick it up."
        ),
        "code_signature": (
            "Your final summary must be a function/class-signature catalog: "
            "the names, signatures, and one-line purpose of every "
            "function/class you touched or introduced."
        ),
    }

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
    """Bounded auto-retry / re-delegation policy for a spawned sub-agent.

    Opt-in via the ``retry`` spawn-schema field; **DEFAULT OFF** (``max == 0``)
    so an unset/absent ``retry`` is byte-identical in behaviour to the historical
    single-attempt path. On a *retryable* terminal failure the spawn bridge
    re-delegates the **same task on the same handle** (retaining the one already
    held semaphore slot for the whole sequence) up to ``max`` extra attempts,
    sleeping :meth:`backoff_for` with exponential growth between them.

    Model-level causes are deliberately NOT re-escalated here: every child
    ``ToolUseLoop`` run already drives the fallback ladder internally, so a
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
    # the shared classifier so this layer never re-derives provider semantics).
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

        Reuses the ``RetryStrategy`` classifier's reason taxonomy (DRY — the
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
                    "description": (
                        "Tool IDs the sub-agent is allowed to use. OMIT the "
                        "field for all tools; an empty list grants none."
                    ),
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
                "summary_kind": {
                    "type": "string",
                    "enum": list(get_args(SummaryKind)),
                    "description": (
                        "Task-typed shape for this sub-agent's final summary "
                        "(e.g. an evidence package vs. a running summary). "
                        "Default: generic (untyped)."
                    ),
                },
                "capability_mode": {
                    "type": "string",
                    "enum": list(get_args(CapabilityMode)),
                    "description": (
                        "Coarse privilege ceiling for ALL of this sub-agent's "
                        "tools — both file/registry tools AND session action "
                        "tools (submit/mint/commit/arm) — layered UNDER "
                        "allowed_tools/denied_tools (it only removes tools, never "
                        "adds). 'read_only' = read-only tools only (no writes, "
                        "shell, or session actions); 'execute' = all declared "
                        "read/write/exec tools; 'all' (default) = no capability "
                        "restriction. Narrowed against this agent's own ceiling, "
                        "so a child can never widen it. "
                        "Ref: [DeepMind-Delegation §4.7] Privilege attenuation."
                    ),
                },
                "workspace_mode": {
                    "type": "string",
                    "enum": list(get_args(WorkspaceMode)),
                    "description": (
                        "Filesystem-containment ceiling for this sub-agent — the "
                        "paths its tools may touch, orthogonal to capability_mode "
                        "(which gates WHICH tools it holds). 'read_only' = reads "
                        "confined to the workspace, no writes anywhere; "
                        "'workspace_write' (default) = reads + writes confined to "
                        "the workspace root + scratch; 'full_access' = no path "
                        "restriction. Narrowed against this agent's own ceiling, "
                        "so a child can never widen it. "
                        "Ref: [DeepMind-Delegation] Privilege attenuation."
                    ),
                },
                "approval_policy": {
                    "type": "string",
                    "enum": list(get_args(ApprovalPolicy)),
                    "description": (
                        "When this sub-agent should pause for parent approval "
                        "('never', 'on_failure' (default), 'on_request'). Reserved "
                        "for a later wave — accepted and validated now, not yet "
                        "enforced."
                    ),
                },
                "contract": {
                    "type": "object",
                    "description": (
                        "Optional bounds on THIS sub-agent's own delegation budget "
                        "— layered UNDER the shared session step budget, never "
                        "above it. Default off (no contract == the historical "
                        "unbounded child)."
                    ),
                    "properties": {
                        "max_steps": {
                            "type": "integer",
                            "description": (
                                "Max tool-execution steps for this agent alone "
                                "(0 = unlimited, default). Warned inside "
                                "step_warn_headroom, force-wrapped-up at the limit."
                            ),
                        },
                        "max_wall_s": {
                            "type": "number",
                            "description": (
                                "Max wall-clock seconds this agent may run "
                                "(0 = unlimited, default). Warned at 80%, "
                                "force-cancelled at 100% by the watchdog."
                            ),
                        },
                        "max_tokens": {
                            "type": "integer",
                            "description": (
                                "Advisory token ceiling (0 = no signal, default). "
                                "Best-effort only: cost accounting is out of "
                                "scope, and enforcement depends on the model "
                                "client actually reporting usage."
                            ),
                        },
                        "autonomy": {
                            "type": "string",
                            "enum": list(get_args(AutonomyTier)),
                            "description": (
                                "'atomic' strips this agent's (and every "
                                "descendant's) ability to spawn further "
                                "sub-agents — a hard delegation firebreak. "
                                "'open_ended' (default) delegates normally."
                            ),
                        },
                        "model_tier": {
                            "type": "string",
                            "enum": list(get_args(ModelTier)),
                            "description": (
                                "Coarse model-cost hint resolved against the "
                                "deployment's agent.model_tiers map. Never "
                                "overrides an explicit model arg or an "
                                "agent_type's configured model."
                            ),
                        },
                        "step_warn_headroom": {
                            "type": "integer",
                            "description": (
                                "Steps before max_steps at which the warning "
                                "fires (default 3)."
                            ),
                        },
                    },
                },
                "verification": {
                    "type": "object",
                    "description": (
                        "Optional ground-truth completion check. When enabled by "
                        "the deployment AND this sub-agent can act, its claimed "
                        "completion is accepted ONLY if this command exits 0 — a "
                        "failure re-drives the agent with the command's output up "
                        "to the configured retry cap. Use for a task with an "
                        "objective pass/fail check (tests, a build, a linter). "
                        "Default off (no verification == the historical ungated "
                        "child)."
                    ),
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["command"],
                            "description": (
                                "Verification kind. Only 'command' today (run an "
                                "argv, exit 0 = pass). Optional — defaults to "
                                "'command'."
                            ),
                        },
                        "argv": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "description": (
                                "Command + args as a list (NEVER a shell string) "
                                "— e.g. ['pytest', '-q']. Run under a scrubbed "
                                "environment; exit 0 is a pass."
                            ),
                        },
                        "cwd": {
                            "type": "string",
                            "description": (
                                "Working directory for the check (defaults to the "
                                "agent's workspace)."
                            ),
                        },
                        "timeout_s": {
                            "type": "number",
                            "description": (
                                "Per-check timeout in seconds (default 60), "
                                "clamped down to the deployment's ceiling."
                            ),
                        },
                    },
                    "required": ["argv"],
                },
            },
            "required": ["task"],
        },
    },
}


# Batch fan-out. The array's ``items`` schema IS the single
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
        user_instructions: str | None = None,
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
        # Operator-authored custom instructions are inherited by every child,
        # exactly like the project instructions — they describe the deployment,
        # not one agent's task, so a sub-agent that lost them would be running
        # under different rules than its parent.
        self._user_instructions = user_instructions
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
        # The parent's EFFECTIVE spec set, stamped by ToolUseLoop.run() alongside
        # the plan context (both are run()-time state — the loop only learns its
        # own specs when it is handed them, long after this tool is constructed).
        # Containment must be monotone: a child narrows, never widens. Deriving a
        # child from the raw registry instead would hand it tools the parent never
        # held, since a parent's set can be narrowed by scoping this tool cannot
        # reconstruct (a console session's ``allowed_tools`` over MCP tools, an
        # ancestor's own filtering). ``None`` means unstamped — fall back to the
        # registry so a directly-constructed tool keeps its historical set.
        self.parent_tool_specs: list[ToolSpec] | None = None
        # Track lifecycle manager tasks for deterministic cleanup.
        self._lifecycle_tasks: list[asyncio.Task[None]] = []

    def rebind_active_model(self, model_name: str) -> None:
        """Re-seat this tool's parent context onto an escalated model.

        The public seam for the loop's sticky model escalation. A parent that
        healed itself onto a rescue model must not keep spawning children onto
        the dead one: :meth:`_resolve_model` falls back to the parent context's
        ``model_name``, and each child's own context is derived from it, so a
        stale value here re-infects the whole subtree.

        ``AgentContext`` is frozen, so this REPLACES rather than mutates — and
        that is exactly why the seam is a method and not an attribute write.
        Knowing the context is a frozen dataclass is this class's business, not
        the loop's; reaching in to do the ``replace`` from outside couples the
        caller to a representation it should never have to know. Idempotent, so
        the loop may call it on every turn.
        """
        if not model_name or model_name == self._agent_context.model_name:
            return
        self._agent_context = replace(self._agent_context, model_name=model_name)

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
        """Fan out a batch of independent sub-agents from ONE tool call.

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
        # Task-typed Communication Unit ([CoA §3]): an
        # explicit `summary_kind` appends its one-line format directive to the
        # child's task text and is stamped onto every `AgentResult` this spawn
        # eventually returns. Unset/unrecognised -> "generic", the historical
        # untyped-summary path, byte-identical in task text. The directive
        # lookup is a plain dict read on `SpawnAgentTask` — never a branch here.
        raw_summary_kind = args.get("summary_kind")
        summary_kind: SummaryKind = "generic"
        if (
            isinstance(raw_summary_kind, str)
            and raw_summary_kind in SpawnAgentTask.SUMMARY_KIND_DIRECTIVES
        ):
            summary_kind = cast(SummaryKind, raw_summary_kind)
            task_desc += f"\n\n{SpawnAgentTask.SUMMARY_KIND_DIRECTIVES[summary_kind]}"
        # Coarse delegation privilege ceiling. The raw
        # REQUESTED mode; the child context narrows it against this agent's own
        # effective ceiling in ``child()``, so it can only ever restrict.
        raw_capability_mode = args.get("capability_mode", "all")
        requested_capability_mode = (
            raw_capability_mode if isinstance(raw_capability_mode, str) else "all"
        )
        # Filesystem-containment ceiling. The raw REQUESTED tier;
        # ``child()`` narrows it min-wins against this agent's own effective tier,
        # so it can only ever restrict. Default ``workspace_write`` mirrors the
        # ``SpawnAgentTask`` field default (inert until enforcement is flipped on).
        raw_workspace_mode = args.get("workspace_mode", "workspace_write")
        requested_workspace_mode = (
            raw_workspace_mode if isinstance(raw_workspace_mode, str) else "workspace_write"
        )
        # Per-agent delegation bounds — layered UNDER the shared
        # session step budget, never above it. Parsed total (never raises); an
        # absent/malformed ``contract`` is the disabled default, byte-identical
        # to the historical unbounded child.
        contract = DelegationContract.from_value(args.get("contract"))
        # Ground-truth completion check — parsed total (never raises); an
        # absent/malformed ``verification`` is ``None`` (the ungated child).
        # Threaded into the child loop, which owns the authoritative two-gate
        # activeness decision; the inactive-note below is only for reporting.
        verification = CommandVerification.from_value(args.get("verification"))
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
            # Apply agent's tool scope if specified and not overridden by caller.
            # ``is not None`` so an AgentDef declaring ``tools: []`` (grant
            # nothing) is applied rather than skipped as if it declared nothing.
            if agent_def.allowed_tools is not None and "allowed_tools" not in args:
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

        # A declared ``model_tier`` is the THIRD, LOWEST-priority model source
        # — applied only when neither an agent_type's configured model nor the
        # caller's explicit ``model`` arg already set one (explicit always wins).
        if contract.model_tier is not None:
            raw_tier_map = get_config_value("agent", "model_tiers", default={})
            tier_map = raw_tier_map if isinstance(raw_tier_map, dict) else {}
            allowed_models = _coerce_list(get_config_value("agent", "allowed_models", default=[]))
            tier_override = contract.resolve_model_override(
                model_override, tier_map, allowed_models or None
            )
            if tier_override is not None:
                model_override = tier_override

        # 1. Resolve and validate model. A model the gateway will refuse kills
        # the child at step 0 while the parent runs on healthily, and an
        # AgentDef-pinned model is the common way in: it overrides the caller
        # entirely, so nothing upstream ever checked it against what this
        # deployment can actually serve. Under the curated-foundry assumption
        # the declared allowlist IS that check.
        #
        # Falling back beats refusing. The parent's own model is proven — it is
        # what this agent is running on right now — so a child whose declared
        # model is unavailable runs on a model that works instead of dying
        # before its first step. Surfaced in the response + start event, never
        # silently swapped: a caller that pinned a model is owed the fact that
        # it did not get it.
        model_fallback_note: str | None = None
        resolved_model = self._resolve_model(model_override)
        if resolved_model.startswith("ERROR:"):
            parent_model = self._agent_context.model_name
            if not parent_model:
                return _SpawnOutcome(content=resolved_model, agent_id=None, status="rejected")
            model_fallback_note = (
                f"requested model unavailable ({resolved_model[len('ERROR: '):]}); "
                f"running on the parent's model '{parent_model}'"
            )
            logging.warning("Sub-agent model fallback: {}", model_fallback_note)
            resolved_model = parent_model

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
            # 3. Create child context. The effective capability_mode is the
            # narrower of the request and this agent's own ceiling; the
            # effective ``atomic`` bit is likewise this contract's OR'd
            # with whatever the parent already carries (see ``child()``).
            child_ctx = self._agent_context.child(
                model_name=resolved_model,
                capability_mode=requested_capability_mode,
                workspace_mode=requested_workspace_mode,
                atomic=contract.atomic,
            )

            # NO-SILENT-DROP: a supplied verification spec whose gate is inert
            # for THIS child (master switch off, or a below-execute
            # capability_mode) is surfaced verbatim in the spawn response +
            # event, never dropped to an invisible null. Non-``None`` only when
            # a spec was supplied AND it will not run — the child loop owns the
            # authoritative decision via the SAME predicate.
            verification_inactive_note: str | None = None
            if verification is not None:
                verification_inactive_note = CommandVerification.inactive_reason(
                    enabled=bool(
                        get_config_value("agent", "verification_enabled", default=False)
                    ),
                    capability_mode=child_ctx.capability_mode,
                )

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
                contract=contract,
            )
            await registry.register(handle)
            self._hook_manager.run_on_agent_start(handle)
            self._emit_event(
                child_ctx,
                "start",
                task_desc,
                handle=handle,
                verification=verification_inactive_note,
                model_fallback=model_fallback_note,
            )
            handle.attestation_spawn_hash = self._record_spawn_attestation(
                child_ctx,
                agent_type=agent_type if isinstance(agent_type, str) else None,
                model=resolved_model,
                capability_mode=child_ctx.capability_mode,
                contract=contract,
            )

            # 5. Filter tool specs (the "filter before binding" pattern). The
            # capability_mode is the child's EFFECTIVE (already-narrowed) mode.
            child_specs = self._filter_tool_specs(
                args, capability_mode=child_ctx.capability_mode
            )

            # 6. Resolve child tool scope + opt-in bounded-retry policy.
            # Ref: [DeepMind-Delegation §4.7] Privilege attenuation — sub-agents
            # inherit parent's approval policy (not None, which blocks all writes).
            # Three-state, and both collapses matter: ``_coerce_list`` cannot
            # tell absent from empty (it returns ``[]`` for either), so the
            # ``is None`` test happens BEFORE coercion. A parent that spawns a
            # child with ``allowed_tools: []`` means zero tools; reading that as
            # "unrestricted" handed the child the parent's entire spec set.
            _requested_allowed = args.get("allowed_tools")
            child_allowed_tools = (
                None if _requested_allowed is None else _coerce_list(_requested_allowed)
            )
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
                        summary_kind,
                        contract,
                        verification,
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
                submitted_body: dict[str, Any] = {
                    "agent_id": child_id,
                    "status": "submitted",
                    "task": task_desc[:200],
                    "message": (
                        "Agent spawned. Use check_agents to monitor "
                        "progress and collect results."
                    ),
                }
                # NO-SILENT-DROP: surface a supplied-but-inert verification so the
                # caller never reads a dropped gate as an invisible null. Same
                # contract for a model this deployment cannot serve.
                if verification_inactive_note is not None:
                    submitted_body["verification"] = verification_inactive_note
                if model_fallback_note is not None:
                    submitted_body["model_fallback"] = model_fallback_note
                return _SpawnOutcome(
                    content=json.dumps(submitted_body),
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
                contract=contract,
                verification=verification,
            )

            # 7. Mark done, with the terminal the child actually reached — the
            # loop returning rather than raising is not evidence of success.
            terminal = self._project_terminal_status(state)
            child_summary = self._child_summary(tq, state)
            await registry.mark_done(child_ctx.agent_id, terminal)
            self._hook_manager.run_on_agent_stop(handle)
            self._emit_terminal_stop(
                child_ctx.event_logger,
                handle,
                state.done_reason or "completed",
                summary=child_summary[:_SUB_AGENT_SUMMARY_CAP],
                summary_kind=summary_kind,
            )

            terminal_hash = self._record_terminal_attestation(
                child_ctx,
                handle,
                terminal_state=terminal,
                summary_kind=summary_kind,
                summary_text=child_summary,
                done_reason=state.done_reason,
            )

            # Ref: [CoA §3.1] Build Communication Unit — compressed context for parent
            from mewbo_core.hypervisor import AgentResult

            result = AgentResult(
                content=tq.task_result or state.done_reason or "No result",
                status=terminal,
                steps_used=handle.steps_completed,
                summary=child_summary[:500],
                attempts=handle.attempts,
                summary_kind=summary_kind,
                attestation_hash=terminal_hash,
                # Projected from the child's state so a spawner sees an honest
                # done-claim: None when the gate never ran, else pass/fail.
                verified=state.verified,
                verify_attempts=state.verify_attempts,
            )
            result_body = asdict(result)
            if verification_inactive_note is not None:
                result_body["verification"] = verification_inactive_note
            if model_fallback_note is not None:
                result_body["model_fallback"] = model_fallback_note
            return _SpawnOutcome(
                content=json.dumps(result_body),
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
                summary_kind=summary_kind,
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
            if child_ctx and handle:
                self._emit_terminal_stop(
                    child_ctx.event_logger, handle, "cancelled by parent"
                )
                self._record_terminal_attestation(
                    child_ctx,
                    handle,
                    terminal_state="cancelled",
                    summary_kind=summary_kind,
                    summary_text=(tq.task_result or "") if tq is not None else "",
                    done_reason="cancelled",
                )
            raise  # Re-raise for TaskGroup propagation.

        except Exception as exc:
            logging.error("Sub-agent failed: {}", exc)
            # Classify + bound the raw exception ONCE — a provider exception can
            # embed an entire upstream HTML error page, and every field below
            # that used to hold `str(exc)`/`str(exc)[:200]` is exactly the
            # unbounded-blob path `RunError` exists to close off at the source.
            run_error = RunError.from_exception(exc, model=resolved_model)
            # Build structured error with context from the handle.
            agent_error = AgentError(
                agent_id=child_ctx.agent_id if child_ctx else "unknown",
                depth=child_ctx.depth if child_ctx else 0,
                task=task_desc[:200],
                error=run_error.brief(),
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
            terminal_hash = ""
            if child_ctx and handle:
                # `title`, not `brief` — the detail is a lane label in the
                # console, and a bounded title can never carry the head of an
                # upstream HTML error page into it.
                self._emit_terminal_stop(
                    child_ctx.event_logger,
                    handle,
                    run_error.title,
                    summary=partial_result,
                    summary_kind=summary_kind,
                )
                terminal_hash = self._record_terminal_attestation(
                    child_ctx,
                    handle,
                    terminal_state="failed",
                    summary_kind=summary_kind,
                    summary_text=partial_result,
                    # `title` (not `brief`): the attestation cap on this field
                    # is a short label, and unlike a raw `str(exc)` slice it is
                    # markup-free by construction — a raw slice could still
                    # land the head of an HTML error page in the chain.
                    done_reason=run_error.title,
                )
            result = AgentResult(
                content=f"Sub-agent failed: {run_error.brief()}",
                status="failed",
                steps_used=handle.steps_completed if handle else 0,
                warnings=[run_error.brief()],
                # Ref: [DeepMind-Delegation §6.1] Checkpoint — partial work survives failure
                summary=partial_result,
                # The last error after a spent retry budget; attempts shows
                # how many re-delegations were tried before giving up.
                attempts=handle.attempts if handle else 1,
                summary_kind=summary_kind,
                attestation_hash=terminal_hash,
            )
            return _SpawnOutcome(
                content=json.dumps(asdict(result)),
                agent_id=child_ctx.agent_id if child_ctx else None,
                status="failed",
            )

        finally:
            if child_ctx:
                # Cancel any children spawned by this sub-agent, settling each
                # one in the transcript — see _cascade_cancel_children.
                await self._cascade_cancel_children(child_ctx)

                await registry.unregister(child_ctx.agent_id)
            # Skip the release when ownership transferred to the background
            # lifecycle manager (root non-blocking path) — releasing here too
            # would double-release the slot and inflate the semaphore.
            if not slot_transferred:
                registry.release()

    # ------------------------------------------------------------------
    # Terminal projection — what the parent is told about its child
    # ------------------------------------------------------------------

    @staticmethod
    def _project_terminal_status(state: OrchestrationState) -> AgentStatus:
        """Project a settled child's ``OrchestrationState`` onto its terminal.

        ``state.done`` answers "did the loop stop", never "did the task
        succeed", and reading it as the latter is what let a doom-looped or
        budget-spent child report ``completed`` to its parent. A child that
        stopped short is ``failed``; only a genuine natural completion whose
        ground-truth check did not fail is ``completed``.

        ``verified is False`` is checked in its own right rather than trusted to
        the reason: it is the authoritative record that a ground-truth check
        ran and did not pass, and a claim contradicted by ground truth must not
        depend on a second field spelling it the same way.
        """
        if not state.done:
            return "failed"
        if state.verified is False:
            return "failed"
        # A halt is reported to the parent as ``failed`` because ``AgentStatus``
        # is a closed four-terminal vocabulary with no "stopped short" arm; the
        # precise reason is not lost — it rides the ``stop`` event's ``detail``
        # and the attestation's ``done_reason`` exactly as before.
        reason = state.done_reason
        if isinstance(reason, str) and reason in UNACHIEVED_DONE_REASONS:
            return "failed"
        return "completed"

    @staticmethod
    def _child_summary(tq: TaskQueue, state: OrchestrationState) -> str:
        """Return the child's Communication Unit, falling back to its summary.

        The heaviest children reach their parent with an EMPTY ``task_result``:
        their work landed as store side-effects and their final turn produced no
        text, so everything they learned was discarded at the collection seam.
        A compaction summary is the one compressed record of that work the child
        already produced, so it is used rather than handing the parent nothing.

        This narrows the hole; it does not close it. A child that neither
        answered nor compacted still has no CU, which needs a forced closing
        summary turn inside the child loop.
        """
        if isinstance(tq.task_result, str) and tq.task_result.strip():
            return tq.task_result
        if isinstance(state.summary, str) and state.summary.strip():
            return state.summary
        return ""

    @staticmethod
    def _coerce_timeout(value: object, default: float = 30.0) -> float:
        """Coerce a model-supplied wait timeout, degrading to *default*.

        ``tool_input`` is authored by the model, so this is a trust boundary and
        the read is where it has to be validated. A bare ``float(...)`` over an
        untyped value raises ``ValueError``/``TypeError`` straight out of a
        monitoring call — a malformed argument would kill the run instead of
        merely being ignored. Total, mirroring ``RetryPolicy.from_value``.

        ``bool`` is excluded explicitly: it is an ``int`` subclass, so
        ``timeout: true`` would otherwise silently mean one second.
        """
        if isinstance(value, bool) or value is None:
            return default
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return default
        return default

    async def has_live_owned_runs(self) -> bool:
        """True while any agent this session owns is still non-terminal.

        The ownership index behind the promise-as-completion gate: a clean
        terminal declared while owned work is live is a promise, not a
        completion. Exposed here because the hypervisor is the only thing that
        knows, and the seam that must ask is the one accepting a completion
        claim.
        """
        try:
            agents = await self._agent_context.registry.list_all()
        except Exception:  # pragma: no cover - defensive; never fail a run here
            return False
        return any(handle.status in ACTIVE_STATUSES for handle in agents)

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
    # Tool spec filtering (the "filter before binding" pattern)
    # ------------------------------------------------------------------

    def _filter_tool_specs(
        self, args: dict[str, Any], *, capability_mode: str = "all"
    ) -> list[ToolSpec]:
        """Filter tool specs for a child agent.

        Filtering starts from the PARENT's effective set (the registry only when
        this tool was never stamped), so the child can only ever narrow it —
        the same monotone containment ``capability_mode`` already enforces at
        :meth:`AgentContext.child`, applied to the spec set itself.

        Denied tools are removed from the child's ``bind_tools()`` list —
        the child LLM never sees them. ``capability_mode`` is the
        child's effective privilege ceiling, applied as a coarse pre-filter
        LAYERED UNDER the allow/deny gates (it only removes more, never adds).
        """
        parent_specs = self.parent_tool_specs
        if parent_specs is None:
            parent_specs = self._tool_registry.list_specs()
        # ``allowed_tools`` is three-state — see ``_spawn_one``'s resolution of
        # ``child_allowed_tools``. Absent stays unrestricted; an empty list is
        # forwarded as an empty list so ``filter_specs`` grants nothing.
        requested_allowed = args.get("allowed_tools")
        return filter_specs(
            parent_specs,
            allowed=(
                None if requested_allowed is None else _coerce_list(requested_allowed)
            ),
            denied=_coerce_list(args.get("denied_tools") or []),
            capability_mode=capability_mode,
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
        timeout = self._coerce_timeout(args.get("timeout"))
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

        agents_payload: list[dict[str, Any]] = []
        for h in await registry.list_visible(exclude_agent_id=self._agent_context.agent_id):
            entry: dict[str, Any] = {
                "id": h.agent_id,
                "parent_id": h.parent_id,
                "depth": h.depth,
                "task": h.task_description,
                "status": h.status,
                "steps_completed": h.steps_completed,
                "last_tool_id": h.last_tool_id,
                "progress_note": h.progress_note,
                "compaction_count": h.compaction_count,
                "attempts": h.attempts,  # Retry provenance for the console
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
            # Additive, ONLY when a real bound was declared,
            # so a contract-less child's payload is byte-identical. Carries
            # LIVE progress (steps/elapsed) alongside the bounded-scalar
            # snapshot the same ``snapshot()`` shape a later wave's
            # attestation record reuses.
            if h.contract.enabled:
                entry["contract"] = {
                    **h.contract.snapshot(),
                    "steps_completed": h.steps_completed,
                    "elapsed_s": time.monotonic() - h.started_at,
                }
            agents_payload.append(entry)

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
        summary_kind: SummaryKind | None = None,
        verification: str | None = None,
        model_fallback: str | None = None,
    ) -> None:
        """Emit a sub_agent lifecycle event.

        ``summary`` (additive, set only on the terminal ``stop``) carries the
        child's compressed result — the Communication Unit a downstream consumer
        can project as the sub-agent's actual response (e.g. the agentic-search
        trace's per-lane evidence block, where the lifecycle ``detail`` is just
        the ``done_reason``). Omitted on every other phase, so existing consumers
        that read only the legacy keys are unaffected.
        ``summary_kind`` rides alongside ``summary`` and is
        likewise omitted when it's the untyped ``"generic"`` default, so a
        consumer that never declared a kind sees no new key.
        ``verification`` (additive, set only on ``start`` when a supplied spec's
        gate is inert) is the no-silent-drop note — omitted otherwise, so a
        spawn with no verification (or an active one) is byte-identical.
        ``model_fallback`` is the same contract for a declared model this
        deployment cannot serve: present only when the child was moved off it,
        so a spawn that got the model it asked for is byte-identical.
        """
        if ctx.event_logger is None:
            return
        self._write_lifecycle(
            ctx.event_logger,
            action=action,
            agent_id=ctx.agent_id,
            parent_id=ctx.parent_id,
            depth=ctx.depth,
            model=ctx.model_name,
            detail=detail,
            handle=handle,
            summary=summary,
            summary_kind=summary_kind,
            verification=verification,
            model_fallback=model_fallback,
        )

    def _write_lifecycle(
        self,
        logger: Callable[[Event], None],
        *,
        action: str,
        agent_id: str,
        parent_id: str | None,
        depth: int,
        model: str,
        detail: str,
        handle: AgentHandle | None = None,
        summary: str | None = None,
        summary_kind: SummaryKind | None = None,
        verification: str | None = None,
        model_fallback: str | None = None,
    ) -> None:
        """Write ONE ``sub_agent`` lifecycle event — the single payload shape.

        Identity is passed explicitly rather than read off a context because a
        parent settling a child during teardown cascade holds the child's
        HANDLE, not its context. Consoles parse this shape, so every lifecycle
        phase must build it here and nowhere else.
        """
        payload: dict[str, Any] = {
            "action": action,
            "agent_id": agent_id,
            "parent_id": parent_id,
            "depth": depth,
            "model": model,
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
        if summary_kind and summary_kind != "generic":
            payload["summary_kind"] = summary_kind
        if verification:
            payload["verification"] = verification
        if model_fallback:
            payload["model_fallback"] = model_fallback
        event: Event = {"type": "sub_agent", "payload": payload}
        logger(event)

    def _emit_terminal_stop(
        self,
        logger: Callable[[Event], None] | None,
        handle: AgentHandle,
        detail: str,
        *,
        summary: str | None = None,
        summary_kind: SummaryKind | None = None,
    ) -> None:
        """Write this agent's ONE terminal ``stop``, at most once.

        Every way an agent can settle — success, failure, cancellation, or a
        parent's teardown cascade — routes through here, because a consumer
        derives liveness from the last payload it sees and a start with no stop
        pins that agent live forever. Several of those paths can fire for the
        SAME agent (a cascade cancel lands, then the cancelled child's own
        handler unwinds), so the at-most-once guarantee is latched on the handle
        rather than assumed from the paths being mutually exclusive.

        Called AFTER the registry has been marked done, so ``handle.status``
        already carries the terminal state the payload reports.
        """
        if handle.terminal_emitted:
            return
        handle.terminal_emitted = True
        if logger is None:
            return
        self._write_lifecycle(
            logger,
            action="stop",
            agent_id=handle.agent_id,
            parent_id=handle.parent_id,
            depth=handle.depth,
            model=handle.model_name,
            detail=detail,
            handle=handle,
            summary=summary,
            summary_kind=summary_kind,
        )

    async def _cascade_cancel_children(self, ctx: AgentContext) -> None:
        """Cancel this agent's own children and settle each one in the log.

        A child of a settling agent has no one left to drive it, and one still
        at ``submitted`` never had a loop task whose cancellation could raise
        into its own handler — so the terminal is written HERE.

        The skip condition is the emission latch, NOT the child's status: a
        terminal status is no evidence a terminal EVENT was ever written. Other
        teardown paths (the child loop's own end-of-run cascade, the
        hypervisor's shutdown force-mark) settle handles in memory only, and
        they run first — so a child arriving here already marked ``cancelled``
        is precisely the one whose span would otherwise stay open forever.

        ``mark_done`` follows the cancel so the emitted payload always reports a
        terminal status: ``cancel_agent`` is a no-op for a child that never got
        an asyncio task, which would otherwise emit a ``stop`` still reading
        ``submitted``.
        """
        registry = self._agent_context.registry
        for child in await registry.list_children(ctx.agent_id):
            if child.terminal_emitted:
                continue
            if child.status in ACTIVE_STATUSES:
                await registry.cancel_agent(child.agent_id)
                await registry.mark_done(child.agent_id, "cancelled")
                detail = "parent agent settled; child cancelled with it"
            else:
                # Already terminal in memory but never written: report how it
                # actually settled rather than claiming this cascade ended it.
                detail = f"settled as {child.status} with no terminal event recorded"
            self._emit_terminal_stop(ctx.event_logger, child, detail)

    # ------------------------------------------------------------------
    # Attestation provenance
    # ------------------------------------------------------------------

    def _record_spawn_attestation(
        self,
        child_ctx: AgentContext,
        *,
        agent_type: str | None,
        model: str,
        capability_mode: str,
        contract: DelegationContract,
    ) -> str:
        """Best-effort spawn provenance record for ``child_ctx``.

        ``""`` when no ``AttestationChain`` is wired for this session (the
        common case today — the feature is orchestrator-injected and gated on
        config) or the chain's own append failed; never raises.
        """
        chain = getattr(child_ctx.registry, "attestation", None)
        if chain is None:
            return ""
        return chain.record_spawn(
            child_ctx.event_logger,
            agent_id=child_ctx.agent_id,
            parent_id=child_ctx.parent_id,
            depth=child_ctx.depth,
            agent_type=agent_type,
            model=model,
            capability_mode=capability_mode,
            contract=contract,
        )

    def _record_terminal_attestation(
        self,
        child_ctx: AgentContext,
        handle: AgentHandle,
        *,
        terminal_state: AgentStatus,
        summary_kind: SummaryKind,
        summary_text: str,
        done_reason: str | None,
    ) -> str:
        """Best-effort terminal provenance record for ``child_ctx``.

        Mirrors :meth:`_record_spawn_attestation`'s no-chain/no-raise contract.
        ``summary_text`` is hashed inside the chain, never persisted verbatim.
        """
        chain = getattr(child_ctx.registry, "attestation", None)
        if chain is None:
            return ""
        return chain.record_terminal(
            child_ctx.event_logger,
            agent_id=child_ctx.agent_id,
            parent_id=child_ctx.parent_id,
            depth=child_ctx.depth,
            terminal_state=terminal_state,
            spawn_hash=handle.attestation_spawn_hash,
            attempts=handle.attempts,
            steps_completed=handle.steps_completed,
            input_tokens=handle.input_tokens,
            output_tokens=handle.output_tokens,
            summary_kind=summary_kind,
            summary_text=summary_text,
            done_reason=done_reason,
        )

    # ------------------------------------------------------------------
    # Bounded retry driver
    # ------------------------------------------------------------------

    def _build_child_loop(
        self,
        child_ctx: AgentContext,
        child_allowed_tools: list[str] | None,
        contract: DelegationContract | None = None,
        verification: CommandVerification | None = None,
    ) -> Any:
        """Construct a fresh child ``ToolUseLoop`` for one attempt.

        A fresh loop per attempt means each retry gets its own
        ``RetryStrategy`` (the model-fallback ladder) — so model-level recovery
        is reused, never reinvented at this layer. ``contract`` and
        ``verification`` ride along unchanged across retries — they are the
        SPAWNER's declared bound/check on this child, not per-attempt state.
        Both default to off so existing callers are unaffected; the child loop
        owns the authoritative two-gate decision on whether verification runs.
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
            user_instructions=self._user_instructions,
            session_tool_registry=self._session_tool_registry,
            allowed_tools=child_allowed_tools,
            # A spawned sub-agent's allowlist is AUTHORITATIVE — its specs are
            # already strictly filtered (``_filter_tool_specs``, no built-in
            # exemption), and the spawn_agent gate must honour it so a leaf
            # scoped without spawn_agent cannot recurse into copies of itself.
            strict_tool_scope=True,
            cwd=self._cwd,
            session_id=self.session_id,
            session_capabilities=self._session_capabilities,
            enable_skills=self._enable_skills,
            contract=contract or DelegationContract(),
            verification=verification,
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
        contract: DelegationContract | None = None,
        verification: CommandVerification | None = None,
    ) -> tuple[Any, Any]:
        """Run the child loop, re-delegating the SAME task on a retryable failure.

        Returns ``(tq, state)`` from the first attempt that reached a real
        completion. Re-raises the LAST exception once the attempt budget is
        spent or the failure cause is not in ``retry.on`` (default off ⇒
        exactly one attempt, identical to the historical path).
        ``CancelledError`` is never retried — parent cancellation is terminal
        and bubbles straight up.

        BOTH child-death shapes are retryable: an exception out of the loop, and
        a loop that RETURNS having stopped short (a halt, a spent budget, a
        failed ground-truth check). The second shape is the common one, so
        watching only the first is why this contract had never once fired in
        production. A stopped-short return is still handed back after the
        budget is spent — the caller reports it honestly rather than raising.

        Slot discipline: the one semaphore slot already acquired in
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
            child_loop = self._build_child_loop(
                child_ctx, child_allowed_tools, contract, verification
            )
            child_task = asyncio.create_task(
                child_loop.run(task_desc, tool_specs=child_specs, mode=self.parent_mode)
            )
            # Populate asyncio_task so cancel_agent() and 3-phase cleanup target
            # the live attempt.
            handle.asyncio_task = child_task
            try:
                tq, state = await child_task
            except asyncio.CancelledError:
                raise  # parent cancellation is terminal — never retried
            except Exception as exc:  # noqa: BLE001 — child-loop failure is opaque
                cause = RetryPolicy.classify_cause(exc)
                if not retry.should_retry(cause, attempt):
                    raise
                await self._back_off_before_retry(child_ctx, handle, retry, attempt, cause)
                continue

            # A child that stops short RETURNS; it does not raise. Doom-loop
            # halts, spent budgets and failed ground-truth checks are the
            # dominant child-death shapes and every one of them arrives here as
            # an ordinary return — so a policy watching only the exception path
            # could never fire for them, which is why the contract had never
            # fired at all. Bucketed as the generic ``failed`` cause: nothing
            # about a halt names a provider, so ``timeout`` would be a lie.
            if self._project_terminal_status(state) != "completed" and retry.should_retry(
                "failed", attempt
            ):
                await self._back_off_before_retry(
                    child_ctx, handle, retry, attempt, state.done_reason or "failed"
                )
                continue
            return tq, state

    async def _back_off_before_retry(
        self,
        child_ctx: AgentContext,
        handle: AgentHandle,
        retry: RetryPolicy,
        attempt: int,
        cause: str,
    ) -> None:
        """Announce a re-delegation and sleep its backoff.

        Shared by both retryable paths so a raising failure and a stopped-short
        return are announced identically — a consumer counting re-delegations
        must not have to know which shape produced one.
        """
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
        summary_kind: SummaryKind,
        contract: DelegationContract | None = None,
        verification: CommandVerification | None = None,
    ) -> None:
        """Background lifecycle manager for non-blocking child execution.

        Drives the child (with bounded retry), stores the ``AgentResult`` on the
        handle, and notifies the parent via ``send_to_parent``.

        Ref: [DeepMind-Delegation §4.5] Lifecycle events at phase transitions.
        Ref: [CoA §3.1] CU stored on handle for async retrieval. ``summary_kind``
        is the caller's declared CU shape, resolved once in
        ``_spawn_one`` and stamped onto every ``AgentResult`` this manager builds.
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
                contract=contract,
                verification=verification,
            )

            # The child's REAL terminal, not the fact that its loop returned —
            # see ``_project_terminal_status``.
            terminal = self._project_terminal_status(state)
            child_summary = self._child_summary(tq, state)
            await registry.mark_done(child_ctx.agent_id, terminal)
            self._hook_manager.run_on_agent_stop(handle)
            self._emit_terminal_stop(
                child_ctx.event_logger,
                handle,
                state.done_reason or "completed",
                summary=child_summary[:_SUB_AGENT_SUMMARY_CAP],
                summary_kind=summary_kind,
            )
            terminal_hash = self._record_terminal_attestation(
                child_ctx,
                handle,
                terminal_state=terminal,
                summary_kind=summary_kind,
                summary_text=child_summary,
                done_reason=state.done_reason,
            )

            from mewbo_core.hypervisor import AgentResult

            handle.result = AgentResult(
                content=tq.task_result or state.done_reason or "No result",
                status=terminal,
                steps_used=handle.steps_completed,
                summary=child_summary[:500],
                attempts=handle.attempts,
                summary_kind=summary_kind,
                attestation_hash=terminal_hash,
                # Projected from the child's state so a spawner reading the tree
                # / check_agents sees an honest done-claim, never an invisible
                # null. None when the gate never ran, else pass/fail.
                verified=state.verified,
                verify_attempts=state.verify_attempts,
            )

        except asyncio.CancelledError:
            await registry.mark_done(child_ctx.agent_id, "cancelled")
            self._hook_manager.run_on_agent_stop(handle)
            self._emit_terminal_stop(child_ctx.event_logger, handle, "cancelled by parent")
            terminal_hash = self._record_terminal_attestation(
                child_ctx,
                handle,
                terminal_state="cancelled",
                summary_kind=summary_kind,
                summary_text=(tq.task_result or "") if tq is not None else "",
                done_reason="cancelled",
            )

            from mewbo_core.hypervisor import AgentResult

            handle.result = AgentResult(
                content="Cancelled",
                status="cancelled",
                steps_used=handle.steps_completed,
                attempts=handle.attempts,
                summary_kind=summary_kind,
                attestation_hash=terminal_hash,
            )

        except Exception as exc:
            logging.error("Sub-agent lifecycle failed: {}", exc)
            # Classify + bound the raw exception ONCE — see the identical
            # comment in `_spawn_one`'s except block for why every field
            # below routes through this instead of a raw `str(exc)`.
            run_error = RunError.from_exception(exc, model=child_ctx.model_name)
            agent_error = AgentError(
                agent_id=child_ctx.agent_id,
                depth=child_ctx.depth,
                task=task_desc[:200],
                error=run_error.brief(),
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
            # `title`, not `brief` — see the identical note in `_spawn_one`.
            self._emit_terminal_stop(
                child_ctx.event_logger,
                handle,
                run_error.title,
                summary=partial,
                summary_kind=summary_kind,
            )
            terminal_hash = self._record_terminal_attestation(
                child_ctx,
                handle,
                terminal_state="failed",
                summary_kind=summary_kind,
                summary_text=partial,
                # `title`, not `brief` — see `_spawn_one`'s except block.
                done_reason=run_error.title,
            )
            handle.result = AgentResult(
                content=f"Sub-agent failed: {run_error.brief()}",
                status="failed",
                steps_used=handle.steps_completed,
                warnings=[run_error.brief()],
                summary=partial,
                attempts=handle.attempts,
                summary_kind=summary_kind,
                attestation_hash=terminal_hash,
            )

        finally:
            # Cascade cleanup to children of this child, settling each one in
            # the transcript — see _cascade_cancel_children.
            await self._cascade_cancel_children(child_ctx)

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
        """Settle every background lifecycle manager before the run tears down.

        Called from ``ToolUseLoop.run()``'s finally block. Collect-or-cancel:
        managers get *timeout* to finish on their own, then the stragglers are
        cancelled — and, crucially, AWAITED.

        Cancelling without awaiting is what leaked children. ``cancel()`` only
        schedules the ``CancelledError``; the handler that marks the child done
        and writes its ONE terminal ``stop`` runs on a later turn of the event
        loop, which never comes if the loop tears down first. The child was then
        settled by nothing in this process — its span stayed open until a boot
        sweep reaped it days later, or forever. Awaiting here is what makes
        settlement in-process rather than next-boot.

        Exceptions are swallowed by ``return_exceptions``: these tasks own their
        own terminal reporting, and a manager that fails while being torn down
        must not take down the run that is already ending.
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
            if still_pending:
                await asyncio.gather(*still_pending, return_exceptions=True)
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
