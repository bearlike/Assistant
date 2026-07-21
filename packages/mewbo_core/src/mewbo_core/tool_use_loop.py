#!/usr/bin/env python3
"""Async tool-use conversation loop with sub-agent support."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import platform as _platform
import queue as _queue_mod
import re
import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Any

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from mewbo_core.agent_context import AgentContext
from mewbo_core.classes import ActionStep, OrchestrationState, Plan, TaskQueue
from mewbo_core.common import get_git_context, get_logger, get_mock_speaker, get_system_prompt
from mewbo_core.components import (
    build_langfuse_handler,
    langfuse_propagate,
    langfuse_trace_span,
    record_span_exception,
)
from mewbo_core.config import get_config_value, get_version
from mewbo_core.context import ContextSnapshot, render_event_lines
from mewbo_core.exit_plan_mode import (
    SHELL_TOOL_IDS,
    ExitPlanModeTool,
    ensure_plan_dir,
    is_inside_plan_dir,
    is_shell_command_plan_safe,
    plan_file_for,
)
from mewbo_core.hooks import HookManager
from mewbo_core.hypervisor import AgentHandle, DelegationContract
from mewbo_core.llm import build_chat_model, specs_to_langchain_tools
from mewbo_core.llm_resilience import (
    DOOM_LOOP_EXEMPT_TOOLS,
    DoomLoopGuard,
    LlmResilienceExhausted,
    PollClassRule,
    RetryStrategy,
    WriteProgressSignal,
    repair_tool_pairing,
)
from mewbo_core.permissions import PermissionDecision, PermissionPolicy
from mewbo_core.prompt_registry import get_prompt_registry
from mewbo_core.session_tools import (
    DEFAULT_SESSION_TOOL_MAX_RESULT_CHARS,
    DEFAULT_SESSION_TOOL_MODES,
    SessionTool,
    SessionToolRegistry,
)
from mewbo_core.tool_registry import (
    TOOL_SEARCH_TOOL_ID,
    ToolRegistry,
    ToolSpec,
    is_deferred,
)
from mewbo_core.types import BLOCKED_CODES, Event, RecoveryHaltPayload, VerificationPayload
from mewbo_core.update_todos import UpdateTodosTool
from mewbo_core.verification import (
    CommandVerification,
    CommandVerifierRunner,
    VerifierOutcome,
    VerifierRunner,
)
from mewbo_core.workspace import WorkspaceContainment, active_containment

logging = get_logger(name="core.tool_use_loop")

# LLM error classification, backoff, retry budget, circuit breaking and
# tool-call-pairing repair live in ``llm_resilience`` (pure + unit-tested).

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Sentinel emitted when the model returns no content.
# Empty-string assistant content replayed in history causes extended-thinking
# models (e.g. ``claude-opus-4-6``) to hallucinate framework-style placeholder
# text. To keep assistant turns non-empty, substitute a neutral text block
# instead of ``""``.
# The placeholder is filtered out of ``agent_message`` events so it never
# reaches the UI.
_NO_CONTENT_PLACEHOLDER = "(no content)"

# How many of the most recent LLM retry/fallback events the resilience note
# carries. Bounded so a storm of retries can't balloon the system prompt; the
# error strings are already truncated to 200 chars upstream (``LlmRetryPayload``).
DEFAULT_RESILIENCE_NOTE_EVENTS = 5

# Fixed, directive-free header for the resilience-note prompt slot. It labels
# ground truth (what the harness already did), never instructs the model — the
# sanctioned "feed the grounded fact back" precedent, not a detector's verdict.
_RESILIENCE_NOTE_HEADER = (
    "Automatic model-resilience activity on this run so far "
    "(performed by the harness, most recent last):"
)


@dataclass
class ResilienceNote:
    """Bounded, factual record of this run's LLM retry/fallback events.

    The model is otherwise BLIND to its own retries: ``RetryStrategy.run`` has
    only two outward channels — ``emit`` (event log / console / CLI) and
    ``raise`` — and never touches the message list, so a run that quietly
    re-drives a failing model, or escalates down its ladder, never sees any of
    it. This note carries the SAME grounded facts the event log already holds —
    which model was tried, how many attempts, the error class, whether a switch
    occurred — into a dedicated system-prompt slot.

    Holds only the most recent ``max_events`` records (hot in-process runtime
    state — a plain dataclass, no trust boundary). :meth:`render` returns the
    whole note, or ``""`` when nothing has gone wrong yet, so a clean run pays
    nothing and the slot stays empty.
    """

    max_events: int = DEFAULT_RESILIENCE_NOTE_EVENTS
    _events: list[str] = field(default_factory=list)

    def record(self, event: Event) -> None:
        """Append a compact summary for a retry/fallback event; ignore the rest."""
        etype = event.get("type")
        if etype not in ("llm_retry", "llm_fallback"):
            return
        payload = event.get("payload") or {}
        self._events.append(self._summarize(etype, payload))
        if len(self._events) > self.max_events:
            del self._events[: len(self._events) - self.max_events]

    @staticmethod
    def _summarize(etype: str, payload: dict[str, Any]) -> str:
        """One factual line for a retry or a fallback event."""
        if etype == "llm_retry":
            return (
                f"retried {payload.get('model', '?')} "
                f"(attempt {payload.get('attempt', '?')}/{payload.get('max_attempts', '?')}, "
                f"{payload.get('error_type', '?')})"
            )
        return (
            f"switched {payload.get('from_model', '?')} -> {payload.get('to_model', '?')} "
            f"({payload.get('reason', '?')})"
        )

    def render(self) -> str:
        """The full note text, or ``""`` when no resilience event has occurred."""
        if not self._events:
            return ""
        lines = "\n".join(f"- {entry}" for entry in self._events)
        return f"{_RESILIENCE_NOTE_HEADER}\n{lines}"

# How many times the promise-as-completion gate refuses a clean terminal while
# owned background runs are live before letting a stubborn model through. One
# nudge is normally enough — the model waits with ``check_agents(wait=true)``,
# the children settle, the next terminal is honest. The cap only bounds a model
# that keeps re-declaring done without waiting; the orchestrator's honesty
# downgrade still records the truth once it is let through, so the whole run's
# budget is never spent spinning here.
_PROMISE_GATE_MAX_NUDGES = 3

# Event-side snapshot cap, decoupled from the per-tool model-facing cap: the
# frontend scrolls the full output and ``result_file`` backstops anything
# pathological. Acts as a FLOOR — a tool declaring a larger model-facing cap
# raises this too, so the store always records at least what the model read.
_EVENT_SNAPSHOT_MAX_CHARS = 100_000

# Maps tool_id patterns to the AbstractTool operation ("get" or "set").
_OPERATION_SET_KEYWORDS = frozenset(
    {
        "shell",
        "edit",
        "write",
        "create",
        "set",
        "update",
        "delete",
        "apply",
        "remove",
        "patch",
        "insert",
        "append",
        "replace",
        "upload",
        "post",
        "put",
    }
)
_OPERATION_GET_KEYWORDS = frozenset(
    {
        "read",
        "list",
        "search",
        "get",
        "fetch",
        "query",
        "lookup",
        "web_search",
        "web_url_read",
        # SCG reasoning tools — default-allowed (no secrets cross them; the
        # auth_scope descriptor stays redacted). Reads (`scg_route`/`scg_observe`)
        # and the additive learned-memory deposit (`scg_memory`) are classified
        # `get` so the default permission policy ALLOWs them without an extra
        # config knob, the same way `web_url_read` is GET-classified by its exact
        # id. The verbs (route/observe) carry no SET keyword; `scg_memory` is the
        # one deliberate write whitelisted here because a connector insight is a
        # propositional reachability fact, never a record value or credential.
        # Exact ids — no false-positive substring match on other tools.
        "scg_route",
        "scg_observe",
        "scg_memory",
    }
)


@dataclass(frozen=True)
class ToolCallResult:
    """Result of executing a single tool call."""

    tool_call_id: str
    tool_id: str
    content: str
    success: bool
    # Structured-envelope facts, present only for a session tool that returned
    # one. ``blocked_code`` is the envelope code when it names a condition no
    # retry can clear (credentials, reachability, permission, quota);
    # ``permanence`` is the tool's own retry verdict. Both default to ``None``,
    # so every other execution path is unchanged.
    blocked_code: str | None = None
    permanence: str | None = None


@dataclass
class ToolBatch:
    """A batch of tool calls with shared concurrency mode."""

    calls: list[Any]
    concurrent: bool


@dataclass
class _CachedFileRead:
    """Tracks a file read for dedup detection."""

    path: str  # normalized absolute path
    offset: int
    limit: int | None
    mtime: float  # os.path.getmtime at read time


class ToolUseLoop:
    """Async tool-use conversation loop.

    Each instance owns one conversation with one LLM. Sub-agents are
    created by spawning new ``ToolUseLoop`` instances via the
    ``spawn_agent`` internal tool.
    """

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
        skill_instructions: str | None = None,
        skill_registry: Any = None,
        agent_registry: Any = None,
        session_tool_registry: SessionToolRegistry | None = None,
        allowed_tools: list[str] | None = None,
        strict_tool_scope: bool = False,
        cwd: str | None = None,
        session_id: str | None = None,
        session_capabilities: tuple[str, ...] = (),
        extra_session_tools: list[SessionTool] | None = None,
        enable_skills: bool = True,
        contract: DelegationContract | None = None,
        verification: CommandVerification | None = None,
        verifier_runner: VerifierRunner | None = None,
    ) -> None:
        """Initialize the tool-use loop.

        Args:
            agent_context: Required — carries model, cancel, logger, registry.
            tool_registry: Registered tools available to this agent.
            permission_policy: Permission rules for tool execution.
            approval_callback: Optional callback for ASK decisions (None for sub-agents).
            hook_manager: Lifecycle hooks.
            project_instructions: CLAUDE.md / AGENTS.md content discovered at session start.
            user_instructions: Operator-authored custom instructions, ALREADY RENDERED
                by ``Orchestrator`` from the stored template (``system_instructions/``).
                A plain string — the loop does no Jinja and no store work. Lives on the
                instance, not on a per-call arg, so it survives the in-place system-prompt
                re-render that a model escalation performs.
            skill_instructions: Pre-rendered skill body (from user /skill invocation).
            skill_registry: SkillRegistry for auto-invocation catalog + activate_skill handling.
            agent_registry: AgentRegistry for agent type catalog + spawn_agent type lookup.
            session_tool_registry: Registry of plugin-contributed session-tool
                factories.  Each matching factory (filtered by ``allowed_tools``)
                is instantiated for this agent and added to ``self._session_tools``.
            allowed_tools: The agent's allowlist used to filter which session
                tools the plugin registry should build for this agent.  ``None``
                means "no plugin session tools" (root agents get only the
                built-in ``ExitPlanModeTool``).
            strict_tool_scope: Whether ``allowed_tools`` is AUTHORITATIVE for this
                agent. ``True`` (spawned leaf sub-agents, wiki-qa/search runs) —
                the allowlist is the whole tool scope, so it also gates
                ``spawn_agent``. ``False`` (the FE default) — ``allowed_tools`` is
                only a PERMISSIVE ceiling over MCP tools (``context.mcp_tools``);
                built-ins and the internal ``spawn_agent`` are NOT scoped by it,
                mirroring the orchestrator's permissive ``filter_specs`` branch.
            cwd: Working directory for this agent (project root).
            session_id: Session identifier — used for plan-mode path scoping.
            session_capabilities: Client-advertised capability tuple from the
                ``X-Mewbo-Capabilities`` header (persisted on the session
                context event). Used to filter capability-gated agents and
                skills out of the system-prompt catalogs and ``activate_skill``
                / ``spawn_agent`` lookups.
            extra_session_tools: Caller-injected ``SessionTool`` instances
                appended to ``self._session_tools`` without a plugin manifest
                (e.g. the structured-response ``emit_result`` tool).
            enable_skills: When ``False``, the auto-invocable ``activate_skill``
                schema is never injected even if the registry holds skills — a
                headless product drive (search/wiki) can opt out so it doesn't
                burn its first step activating a host ``~/.claude`` skill it
                never intended to expose. Default ``True`` (unchanged behavior).
            contract: The spawner's declared ``DelegationContract`` for THIS
                agent — ``None``/disabled is the historical
                unbounded child. Checked in the run loop's budget block
                IN ADDITION TO (never instead of) the shared session budget.
            verification: The spawner's declared ground-truth completion check
                for THIS agent — ``None`` is the historical ungated path. Only
                RUN when the two-gate ``self._verification_active`` holds
                (master switch on AND a spec AND an execute/all capability
                mode); otherwise it is carried but inert.
            verifier_runner: Injected ``VerifierRunner`` (defaults to
                ``CommandVerifierRunner``). A test passes a recording fake so
                the gate is exercised without a real subprocess.
        """
        self._ctx = agent_context
        self._contract = contract
        # The model whose per-model prompt overrides + tool variant are ACTIVE.
        # Starts at the configured primary; the fallback ladder promotes it
        # to the escalated model on a sticky switch (see ``_apply_model_escalation``)
        # so the heal becomes behavioural, not just a model swap.
        self._active_model = agent_context.model_name
        self._enable_skills = enable_skills
        self._tool_registry = tool_registry
        self._permission_policy = permission_policy
        self._approval_callback = approval_callback
        self._hook_manager = hook_manager
        self._project_instructions = project_instructions
        self._user_instructions = user_instructions
        self._skill_instructions = skill_instructions
        self._skill_registry = skill_registry
        self._agent_registry = agent_registry
        self._session_tool_registry = session_tool_registry
        self._cwd = cwd
        self._session_id = session_id
        self._session_capabilities = session_capabilities
        # Retained so the tool ceiling can reach the tools this loop injects
        # OUTSIDE ``filter_specs`` — see :meth:`_loop_injected_admitted`.
        self._allowed_tools = allowed_tools
        self._strict_tool_scope = strict_tool_scope

        # Filesystem-containment firebreak. Built ONCE here from
        # three inputs: the enforcement kill-switch (``agent.workspace_
        # enforcement``, staged OFF by default), this agent's narrowed
        # ``workspace_mode``, and the workspace ``cwd``. It is a non-None
        # ``WorkspaceContainment`` ONLY when all three admit containment
        # (enforcement on AND a restrictive tier AND a real cwd) — so with the
        # flag off, a full_access tier, or no cwd, ``self._containment is None``
        # and EVERY path resolves byte-identically to the historical tenant union.
        # ``self._containment is not None`` is therefore the single "containment
        # active" predicate the loop's root-injection + tool-execution seams read.
        self._containment: WorkspaceContainment | None = None
        if (
            cwd
            and agent_context.workspace_mode != "full_access"
            and bool(get_config_value("agent", "workspace_enforcement", default=False))
        ):
            self._containment = WorkspaceContainment(
                mode=agent_context.workspace_mode, root=cwd
            )

        # Verifier-gated completion. Two-gate arming computed ONCE (mirrors the
        # write-progress signal): a ground-truth check only gates an agent that
        # (a) has a spec, (b) runs under the master switch, and (c) could
        # plausibly ACT — capability_mode ∈ {execute, all}. A read-only child,
        # the disabled default, or the staged-off switch leaves the gate inert,
        # so every natural completion is accepted byte-identically. The runner
        # is injected (default ``CommandVerifierRunner``) so a test drives the
        # gate with a recording fake and never spawns a real subprocess.
        self._verification = verification
        self._verifier_runner: VerifierRunner = verifier_runner or CommandVerifierRunner()
        self._verification_active = verification is not None and CommandVerification.gate_active(
            enabled=bool(get_config_value("agent", "verification_enabled", default=False)),
            capability_mode=agent_context.capability_mode,
        )
        # Latches for the run: whether a check has already passed (never re-run
        # once green) and how many failed re-drives remain.
        self._verify_passed = False
        self._verify_retries_left = int(
            get_config_value("agent", "verification_max_retries", default=2)
        )

        # Dedup cache for read_file: prevents redundant reads when the
        # same file + range hasn't changed on disk (mtime check).
        self._file_read_cache: dict[str, _CachedFileRead] = {}

        # Plan-mode state (mutable across the loop's lifetime).
        self._current_mode: str = "act"
        # Authoritative token count from the most recent LLM response's
        # usage_metadata.input_tokens. Zero until the first call lands.
        self._last_input_tokens: int = 0

        # In-flight LLM call, for the liveness leg. ``None`` whenever no call is
        # outstanding; a monotonic timestamp while one is. The retry strategy
        # already bounds each attempt with ``asyncio.wait_for``, but that bound
        # can only fire if the awaited coroutine reaches a cancellation point —
        # a provider read wedged below the event loop never does, and one such
        # call sat silent for 24 minutes having emitted ``llm_call_start`` and
        # no end. Nothing noticed it live: the sweepers that would have run once
        # each, at process boot.
        self._llm_call_started_at: float | None = None
        self._llm_call_step: int = 0

        # Error-visibility seam: a bounded, factual note of this run's LLM
        # retry/fallback events, injected as its OWN system-prompt slot so the
        # model is no longer blind to its own retries. ``_active_resilience_note``
        # tracks the text last baked into ``messages[0]`` so the loop re-renders
        # the prompt only when the note actually changes (a clean run never
        # pays; a healing run re-renders at most once per new event).
        self._resilience_note = ResilienceNote()
        self._active_resilience_note: str = ""

        # Self-steering routing state. The live per-run ``RetryStrategy`` (set in
        # ``run``) is what the ``model_control`` tool reuses for its switch
        # budget + cooldown; ``_requested_switch`` is a model the tool asked to
        # switch to, applied at the NEXT turn boundary (like a sticky fallback,
        # transcript tail untouched); ``_live_messages`` lets the continuity-lock
        # guardrail inspect the in-flight transcript.
        self._retry_strategy: RetryStrategy | None = None
        self._requested_switch: str | None = None
        self._live_messages: list[BaseMessage] | None = None

        # Create SpawnAgentTool when this agent can spawn children — gated on
        # BOTH depth (``can_spawn``) AND tool scope. spawn_agent is injected
        # here rather than through ``filter_specs``, so an explicit ``tools:``
        # allowlist that omits it would otherwise be silently bypassed: a leaf
        # agent scoped to build-and-submit (the st-widget-builder) could still
        # delegate, and misread its own errors as "delegate to a scoped agent",
        # recursing into copies of itself (mobile session 8c04e341…). An ABSENT
        # allowlist (``None``) stays unrestricted (root / ad-hoc spawns); an
        # EMPTY one grants nothing, delegation included. That distinction is
        # load-bearing, not pedantry: a role-bounded viewer's composed allowlist
        # omits the spawn family precisely to disable delegation, and can compose
        # down to empty — reading empty as "unrestricted" would hand delegation
        # back to the principal the ceiling exists to deny.
        #
        # The allowlist gate applies ONLY when the scope is STRICT (a spawned
        # leaf / wiki-qa / search run — where ``allowed_tools`` is the whole
        # authoritative tool scope). Under a PERMISSIVE scope (the FE default),
        # ``allowed_tools`` is ``context.mcp_tools`` — a ceiling over MCP tools
        # only, which never lists the internal spawn_agent; built-ins stay (see
        # the orchestrator's permissive ``filter_specs`` branch), so spawn_agent
        # must stay too. Without this carve-out every console/Aura session that
        # advertised MCP tools had root delegation silently disabled (session
        # 04ea546e…: the st-widget-builder skill mandates spawn_agent, which was
        # unreachable, so the agent violated the skill and built the widget itself).
        spawn_in_scope = (
            not strict_tool_scope
            or allowed_tools is None
            or bool({"spawn_agent", "spawn_agents"} & set(allowed_tools))
        ) and not self._ctx.atomic  # Atomic is a hard firebreak
        self._spawn_agent_tool: Any = None
        if agent_context.can_spawn and spawn_in_scope:
            from mewbo_core.spawn_agent import SpawnAgentTool

            # Ref: [DeepMind-Delegation §4.7] Sub-agents inherit parent's approval
            # policy so they can execute write/edit/shell tools.
            self._spawn_agent_tool = SpawnAgentTool(
                agent_context=agent_context,
                tool_registry=tool_registry,
                permission_policy=permission_policy,
                approval_callback=approval_callback,
                hook_manager=hook_manager,
                project_instructions=project_instructions,
                user_instructions=user_instructions,
                cwd=cwd,
                agent_registry=agent_registry,
                session_tool_registry=session_tool_registry,
                session_capabilities=session_capabilities,
                enable_skills=enable_skills,
            )

        # Assemble session tools — per-agent stateful handlers that carry
        # their own schema, dispatch, and run-termination flag. The core's
        # built-in ``ExitPlanModeTool`` is always attached to root agents
        # with a session id; plugin-contributed tools are selected by EITHER
        # the agent's ``allowed_tools`` allowlist OR a capability gate (a
        # factory whose ``requires_capabilities`` ⊆ ``session_capabilities``),
        # so a runtime-granted capability surfaces its tools to the
        # root agent without the client listing them explicitly.
        self._session_tools: list[SessionTool] = []
        if agent_context.depth == 0 and session_id is not None:
            # Deliberately NOT ceiling-checked: ``exit_plan_mode`` is the only
            # way out of plan mode, so withholding it from a strict scope that
            # failed to name it would leave the run with no exit at all. It is a
            # structural terminator, never a surface an agent wanders into.
            self._session_tools.append(
                ExitPlanModeTool(
                    session_id=session_id,
                    event_logger=agent_context.event_logger,
                )
            )
            # Authoritative live todos (act mode): terminal-free, re-emits the
            # FULL statused list as ONE ``todos`` event on each call. Attached
            # inline (not via the plugin factory) so it bypasses the plugin
            # factory's allowlist gate — hence the explicit ceiling check here,
            # or an AgentDef's authoritative ``tools:`` under-states what its
            # agent actually holds.
            if self._loop_injected_admitted("update_todos"):
                self._session_tools.append(
                    UpdateTodosTool(
                        session_id=session_id,
                        event_logger=agent_context.event_logger,
                        agent_id=agent_context.agent_id,
                    )
                )
        if session_tool_registry is not None and session_id is not None:
            self._session_tools.extend(
                session_tool_registry.build_for(
                    allowed_tools,
                    session_id=session_id,
                    event_logger=agent_context.event_logger,
                    session_capabilities=session_capabilities,
                    # df875 law: a PERMISSIVE allowlist (FE mcp_tools) is
                    # only an MCP ceiling, so an unconditional tool
                    # (schedule_trigger) still surfaces; a STRICT AgentDef scope
                    # must name it. Mirrors the spawn_in_scope gate above.
                    strict_tool_scope=strict_tool_scope,
                    # Delegation privilege ceiling: the loop's effective
                    # (already-narrowed) capability_mode gates SESSION tools too,
                    # not just registry tools — else a read_only spawn would still
                    # receive write-tier session actions (submit/mint/commit/arm).
                    # Root is always "all" (no-op); a narrowed sub-agent attenuates.
                    capability_mode=agent_context.capability_mode,
                )
            )
        # Caller-injected session tools (e.g. the structured-response emit
        # tool) — no plugin manifest needed. They terminate / dispatch through
        # the same machinery as plugin tools.
        if extra_session_tools:
            self._session_tools.extend(extra_session_tools)

        # Self-steering model control. Bound whenever the operator opts into
        # self-steering fallback — unlike a task tool it is resilience
        # infrastructure (peer of the automatic fallback ladder), so it is NOT
        # ceiling-checked against ``allowed_tools``: a child pinned by its
        # AgentDef to a model the key rejects (the live failure) must be able to
        # switch even though that AgentDef never named the tool, and is attached
        # at EVERY depth, not just the root. Off by default (self_steering=False)
        # ⇒ zero extra surface, so it never widens a strict scope uninvited.
        if session_id is not None and bool(
            get_config_value("llm", "fallback", "self_steering", default=False)
        ):
            from mewbo_core.model_control import ModelControlTool

            self._session_tools.append(
                ModelControlTool(
                    session_id=session_id,
                    agent_id=agent_context.agent_id,
                    depth=agent_context.depth,
                    get_active_model=lambda: self._active_model,
                    get_ladder=lambda: [self._ctx.model_name, *self._ctx.fallback_models],
                    get_strategy=lambda: self._retry_strategy,
                    has_unanswered_tool_use=self._has_dangling_tool_use,
                    apply_switch=self._request_model_switch,
                    # Route through ``_emit_event`` (not the raw sink) so a
                    # deliberate switch's ``llm_fallback`` is captured by the
                    # resilience note too, keeping it the complete switch record.
                    event_logger=self._emit_event,
                    get_step=lambda: self._llm_call_step,
                    max_switches=int(
                        get_config_value("llm", "fallback", "max_switches", default=2)
                    ),
                    allow_upgrade=bool(
                        get_config_value("llm", "fallback", "allow_upgrade", default=False)
                    ),
                )
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        user_query: str,
        *,
        tool_specs: list[ToolSpec],
        context: ContextSnapshot | None = None,
        plan: Plan | None = None,
        mode: str = "act",
    ) -> tuple[TaskQueue, OrchestrationState]:
        """Run the async tool-use loop and return TaskQueue + OrchestrationState."""
        state = OrchestrationState(goal=user_query)
        # Plan-mode is enforced via: (1) filtered tool schema at bind time,
        # (2) path-scoped permission check on edits, (3) the exit_plan_mode
        # approval gate. The loop flips ``_current_mode`` to ``"act"`` after
        # the user approves a plan and re-binds tools.
        self._current_mode = mode if mode in {"plan", "act"} else "act"
        if self._current_mode == "plan" and self._session_id is not None:
            state.plan_path = plan_file_for(self._session_id)
            ensure_plan_dir(self._session_id)
        # Propagate plan context so children inherit session and mode.
        if self._spawn_agent_tool is not None:
            self._spawn_agent_tool.session_id = self._session_id
            self._spawn_agent_tool.parent_mode = self._current_mode
            # The EFFECTIVE set, not the deferral-active subset: deferral strips
            # schemas from the initial bind and re-fetches them through
            # tool_search, so a child seeded from it would lose tools this agent
            # genuinely holds. Children narrow this set; they never widen it.
            self._spawn_agent_tool.parent_tool_specs = list(tool_specs)
        executed_steps: list[ActionStep] = []
        tool_outputs: list[str] = []
        last_error: str | None = None
        final_response: str | None = None

        # Register self in the hypervisor registry.
        # Reuse the handle created by SpawnAgentTool when one already exists for
        # this agent_id — avoids overwriting it and losing the reference held by
        # the lifecycle manager (which later stores AgentResult on the handle).
        existing = await self._ctx.registry.get(self._ctx.agent_id)
        if existing is not None:
            handle = existing
        else:
            handle = AgentHandle(
                agent_id=self._ctx.agent_id,
                parent_id=self._ctx.parent_id,
                depth=self._ctx.depth,
                model_name=self._ctx.model_name,
                task_description=user_query[:200],
                # A spawned child's handle is normally
                # pre-registered (and pre-stamped) by SpawnAgentTool before its
                # loop ever runs, so this branch is the exception (a directly
                # constructed loop, e.g. the root). Stamping ``self._contract``
                # here too keeps the handle self-consistent with whatever this
                # loop was actually built with, instead of silently reading
                # back the disabled default.
                contract=self._contract or DelegationContract(),
            )
            await self._ctx.registry.register(handle)
        handle.status = "running"

        # Ref: [AgentCgroup §4.2] Background watchdog for stall detection.
        # Code-level reflex — zero token cost. Root only.
        watchdog_task: asyncio.Task[None] | None = None
        if self._ctx.depth == 0:
            watchdog_task = asyncio.create_task(self._watchdog())

        # Langfuse context managers — initialized in try, cleaned in finally.
        agent_span: Any = None
        _agent_span_cm: Any = None
        _propagate_cm: Any = None

        try:
            # Ref: [DeepMind-Delegation §4.5] Global eye for root agent
            agent_tree = ""
            if self._ctx.depth == 0:
                agent_tree = await self._ctx.registry.render_agent_tree(
                    exclude_agent_id=self._ctx.agent_id,
                )
            # Deferred-tool partitioning. When ``agent.tool_search.mode`` is
            # 'on', MCP / metadata.deferred specs are stripped from the
            # initial bind and surfaced by name only via the
            # ``<available-deferred-tools>`` block. The model fetches the
            # schemas it needs through ``tool_search``; the per-turn re-bind
            # hook below grows the bound list as tools are discovered.
            self._tool_search_enabled = self._is_tool_search_enabled(tool_specs)
            self._tool_specs_full = list(tool_specs)
            self._deferred_ids = (
                {s.tool_id for s in tool_specs if is_deferred(s)}
                if self._tool_search_enabled
                else set()
            )
            active_specs = self._select_active_specs(tool_specs, discovered=set())
            messages = self._build_messages(
                user_query,
                context,
                plan,
                agent_tree=agent_tree,
            )
            # Expose the live transcript so the model_control continuity-lock
            # guardrail can inspect it (a mutable reference — it sees appends).
            self._live_messages = messages
            tool_schemas = self._build_tool_schemas_for_mode(
                active_specs,
                self._current_mode,
            )
            model = self._bind_model(tool_schemas)
            self._last_active_ids = {s.tool_id for s in active_specs}

            langfuse_handler = build_langfuse_handler(
                user_id="mewbo-tool-use",
                session_id=f"tool-use-{self._ctx.agent_id}",
                trace_name="mewbo-tool-use",
                version=get_version(),
                release=get_config_value("runtime", "envmode", default="Not Specified"),
            )
            invoke_config: dict[str, Any] = {}
            if langfuse_handler is not None:
                invoke_config["callbacks"] = [langfuse_handler]
                metadata = getattr(langfuse_handler, "langfuse_metadata", None)
                if isinstance(metadata, dict) and metadata:
                    invoke_config["metadata"] = metadata

            # -- Langfuse: agent-level span + attribute propagation --------
            _agent_role = "root" if self._ctx.depth == 0 else f"child-{self._ctx.agent_id[:8]}"
            _agent_span_name = f"agent:{_agent_role}"
            _agent_span_cm = langfuse_trace_span(
                _agent_span_name,
                metadata={
                    "agentid": self._ctx.agent_id[:12],
                    "model": self._ctx.model_name,
                    "depth": str(self._ctx.depth),
                    "mode": self._current_mode,
                },
                input_data={"task": user_query[:200]},
            )
            agent_span = _agent_span_cm.__enter__()
            _propagate_cm = langfuse_propagate(
                tags=[
                    "mewbo-tool-use",
                    f"model:{self._ctx.model_name}",
                    f"depth:{self._ctx.depth}",
                ]
            )
            _propagate_cm.__enter__()

            turns = 0
            # One atomic resilience strategy per run — holds the retry budget,
            # circuit breaker and policy knobs; survives every turn. Retained on
            # the instance so the model_control tool reuses THIS run's budget +
            # circuit breaker for its own switch guardrails.
            retry_strategy = RetryStrategy.from_config()
            self._retry_strategy = retry_strategy
            doom_guard = DoomLoopGuard.from_config(
                extra_poll_rules=self._poll_class_rules(tool_specs)
            )
            # Two-gate arming, computed ONCE for the run: the write-progress
            # signal is only meaningful for an agent that could plausibly
            # WRITE at all — narrowed by its own capability_mode AND actually
            # holding a write-tier tool. Anything else (read_only children, a
            # session with no write tools bound) leaves the signal permanently
            # inert.
            write_capable = self._ctx.capability_mode in {"execute", "all"} and any(
                s.capability_tier() == "write" for s in tool_specs
            )
            write_progress = WriteProgressSignal.from_config(write_capable=write_capable)
            write_progress_stamped = False
            # One-shot latch so the per-agent budget warning
            # (distinct from the session-wide ``loop.budget_warning`` above)
            # fires exactly once per run, not on every turn inside headroom.
            contract_step_warned = False
            # ``(tool_id, code)`` of the most recent blocked-class envelope
            # error that has NOT since been cleared. "Unrecovered" is judged per
            # tool: a later SUCCESS from the same tool means the blocked
            # operation went through after all, and only that clears it —
            # an unrelated tool succeeding says nothing about whether the repo
            # ever became reachable.
            last_blocked: tuple[str, str] | None = None
            # Consecutive promise-as-completion refusals (see the gate below).
            promise_nudges = 0
            while not state.done:
                # Check cancellation.
                if self._ctx.should_cancel is not None and self._ctx.should_cancel():
                    state.done = True
                    state.done_reason = "canceled"
                    break

                # Check for interrupt (root agent only).
                if self._ctx.interrupt_step is not None and self._ctx.interrupt_step.is_set():
                    self._ctx.interrupt_step.clear()
                    messages.append(
                        HumanMessage(
                            content=get_prompt_registry().render("loop.interrupt_marker")
                        )
                    )

                # Drain any queued user steering messages (root agent only).
                if self._ctx.message_queue is not None:
                    while not self._ctx.message_queue.empty():
                        try:
                            msg = self._ctx.message_queue.get_nowait()
                            messages.append(HumanMessage(content=msg))
                        except _queue_mod.Empty:
                            break

                # Apply a deliberate model switch the model_control tool
                # requested last turn. Delegated to the SAME escalation path a
                # sticky fallback uses (promote active model / re-render prompt /
                # re-derive edit tool / rebind), applied at this turn boundary so
                # the transcript tail is untouched. Pin it on the strategy too, so
                # ``_order_models`` keeps the chosen model at the chain head
                # instead of a prior sticky pin reordering it back out.
                if self._requested_switch is not None:
                    _switch_target = self._requested_switch
                    self._requested_switch = None
                    retry_strategy._pinned_model = _switch_target
                    tool_schemas, model = self._apply_model_escalation(
                        _switch_target,
                        messages,
                        context=context,
                        plan=plan,
                        agent_tree=agent_tree,
                        tool_schemas=tool_schemas,
                        model=model,
                    )

                # Keep the resilience note current in the system prompt: re-render
                # ``messages[0]`` only when the note text changed since it was last
                # baked in, so a clean run never pays and a healing run re-renders
                # at most once per new retry/fallback event.
                _note = self._resilience_note.render()
                if _note != self._active_resilience_note:
                    self._active_resilience_note = _note
                    messages[0] = SystemMessage(
                        content=self._render_system_prompt(context, plan, agent_tree)
                    )

                with langfuse_trace_span(
                    f"step:{turns}",
                    metadata={
                        "turn": str(turns),
                        "model": self._ctx.model_name,
                    },
                ) as span:
                    if span is not None:
                        try:
                            span.update_trace(input={"turn": turns, "message_count": len(messages)})
                        except Exception:
                            pass

                    # Graduated enforcement: warn as the budget nears, then force
                    # ONE wrap-up turn at exhaustion so an unbounded
                    # fan-out can't run away — but the agent still gets to
                    # answer instead of a bare halt.
                    if self._ctx.registry.budget_exhausted():
                        final_response = await self._budget_wrapup_turn(
                            "budget_exhausted",
                            state=state,
                            messages=messages,
                            tool_outputs=tool_outputs,
                            invoke_config=invoke_config,
                        )
                        break
                    if self._ctx.registry.budget_warning():
                        messages.append(
                            SystemMessage(
                                content=get_prompt_registry().render("loop.budget_warning")
                            )
                        )

                    # DelegationContract. A per-agent bound
                    # LAYERED UNDER the session budget just checked above:
                    # checked here regardless (a spawner's ceiling applies even
                    # when the shared pool has headroom left). Disabled
                    # contracts (the historical default) skip this entirely.
                    if self._contract is not None and self._contract.enabled:
                        contract_over = False
                        step_state = await self._ctx.registry.agent_step_state(
                            self._ctx.agent_id
                        )
                        if step_state == "over":
                            contract_over = True
                        elif step_state == "warn" and not contract_step_warned:
                            contract_step_warned = True
                            messages.append(
                                SystemMessage(
                                    content=get_prompt_registry().render(
                                        "loop.agent_budget_warning"
                                    )
                                )
                            )
                        if not contract_over:
                            token_state = await self._ctx.registry.agent_token_state(
                                self._ctx.agent_id
                            )
                            if token_state == "over":
                                contract_over = True
                        if contract_over:
                            final_response = await self._budget_wrapup_turn(
                                "halted_agent_budget",
                                state=state,
                                messages=messages,
                                tool_outputs=tool_outputs,
                                invoke_config=invoke_config,
                            )
                            break

                    # Heartbeat events so clients (console/CLI) can distinguish
                    # "waiting on LLM" from a silent hang.
                    self._emit_event(
                        {
                            "type": "llm_call_start",
                            "payload": {
                                "agent_id": self._ctx.agent_id,
                                "depth": self._ctx.depth,
                                "step": turns,
                                "model": self._active_model,
                            },
                        }
                    )
                    # Arm the liveness leg for exactly the window this call is
                    # outstanding; the ``finally`` disarms it on every exit so a
                    # completed call can never read as a wedged one.
                    self._llm_call_started_at = _time.monotonic()
                    self._llm_call_step = turns
                    try:
                        response, _final_model = await self._invoke_with_resilience(
                            primary_model=model,
                            messages=messages,
                            tool_schemas=tool_schemas,
                            turns=turns,
                            invoke_config=invoke_config,
                            strategy=retry_strategy,
                        )
                    except LlmResilienceExhausted as exhausted:
                        # Clean halt: surface a true failure (never masked as
                        # "completed") so the FE can offer one-click recovery.
                        self._emit_event(
                            {
                                "type": "llm_call_end",
                                "payload": {
                                    "agent_id": self._ctx.agent_id,
                                    "depth": self._ctx.depth,
                                    "step": turns,
                                    "success": False,
                                    "model": (
                                        exhausted.models_tried[-1]
                                        if exhausted.models_tried
                                        else self._ctx.model_name
                                    ),
                                    "error_type": exhausted.last_error_type,
                                    "reason": exhausted.reason,
                                },
                            }
                        )
                        if span is not None:
                            try:
                                span.update(
                                    level="ERROR",
                                    # Same substitution as the completion string:
                                    # ``str(TimeoutError())`` is empty, and this
                                    # span write would otherwise carry a void
                                    # status_message for the very failure it marks.
                                    status_message=LlmResilienceExhausted.describe_error(
                                        exhausted.last_error
                                    ),
                                    metadata={
                                        "errortype": exhausted.last_error_type,
                                        "models_tried": ",".join(exhausted.models_tried),
                                        "reason": exhausted.reason,
                                    },
                                )
                            except Exception:
                                pass
                        record_span_exception(
                            span,
                            exhausted.last_error,
                            attributes={
                                "errortype": exhausted.last_error_type or "",
                                "reason": exhausted.reason or "",
                            },
                        )
                        raise
                    finally:
                        self._llm_call_started_at = None
                    # Fallback ladder: if the resilience strategy escalated
                    # to (and pinned) a different model, re-render the system
                    # prompt + re-derive the edit-tool variant against THAT model
                    # so the heal is behavioural, not just a model swap.
                    tool_schemas, model = self._apply_model_escalation(
                        _final_model,
                        messages,
                        context=context,
                        plan=plan,
                        agent_tree=agent_tree,
                        tool_schemas=tool_schemas,
                        model=model,
                    )
                    _step_usage = getattr(response, "usage_metadata", None)
                    _h_ref = await self._ctx.registry.get(self._ctx.agent_id)
                    # LangChain ``UsageMetadata`` exposes provider cache and
                    # reasoning subtotals (Anthropic + OpenAI normalised):
                    #   input_token_details.cache_creation — written to cache
                    #     this call (Anthropic 5-min: 1.25× input price)
                    #   input_token_details.cache_read — served from cache
                    #     (Anthropic: 0.1× input; OpenAI: 0.5× input)
                    #   output_token_details.reasoning — extended-thinking /
                    #     o1 hidden tokens (billed as output)
                    # Capturing them per call lets clients show fresh-vs-
                    # cached breakdown and an honest billable signal that
                    # accounts for cache discounts.
                    _in_det = _step_usage.get("input_token_details") or {} if _step_usage else {}
                    _out_det = _step_usage.get("output_token_details") or {} if _step_usage else {}
                    self._emit_event(
                        {
                            "type": "llm_call_end",
                            "payload": {
                                "agent_id": self._ctx.agent_id,
                                "depth": self._ctx.depth,
                                "step": turns,
                                "success": True,
                                "model": _final_model,
                                "input_tokens": (
                                    _step_usage.get("input_tokens", 0) if _step_usage else 0
                                ),
                                "output_tokens": (
                                    _step_usage.get("output_tokens", 0) if _step_usage else 0
                                ),
                                "cache_creation_input_tokens": int(
                                    _in_det.get("cache_creation", 0) or 0
                                ),
                                "cache_read_input_tokens": int(_in_det.get("cache_read", 0) or 0),
                                "reasoning_output_tokens": int(_out_det.get("reasoning", 0) or 0),
                                "cumulative_input_tokens": (_h_ref.input_tokens if _h_ref else 0),
                                "cumulative_output_tokens": (_h_ref.output_tokens if _h_ref else 0),
                            },
                        }
                    )
                    # Strip thinking blocks from the response before appending
                    # to the conversation history.  Anthropic requires a
                    # ``signature`` field on thinking blocks when replayed,
                    # but proxies (LiteLLM) may not preserve it.
                    raw = getattr(response, "content", None)
                    if isinstance(raw, list):
                        sanitized = [
                            block
                            for block in raw
                            if not (isinstance(block, dict) and block.get("type") == "thinking")
                        ]
                        # Never leave empty-string assistant content.
                        # Empty assistant turns in
                        # history cause extended-thinking models to
                        # hallucinate framework-style placeholders.
                        if not sanitized:
                            sanitized = [{"type": "text", "text": _NO_CONTENT_PLACEHOLDER}]
                        response = AIMessage(
                            content=sanitized,
                            tool_calls=response.tool_calls,
                            additional_kwargs=response.additional_kwargs,
                            usage_metadata=response.usage_metadata,
                            id=response.id,
                        )
                    elif response.tool_calls and (not raw or not str(raw).strip()):
                        # The proxy (LiteLLM) strips thinking blocks itself
                        # and returns ``content=""`` (a STRING, not a list).
                        # Without this branch, the empty string survives
                        # sanitisation and gets replayed in history, causing
                        # the model to hallucinate placeholder meta-text.
                        response = AIMessage(
                            content=_NO_CONTENT_PLACEHOLDER,
                            tool_calls=response.tool_calls,
                            additional_kwargs=response.additional_kwargs,
                            usage_metadata=response.usage_metadata,
                            id=response.id,
                        )
                    messages.append(response)

                    if not response.tool_calls:
                        # Text response — the model claims completion.
                        content = self._extract_text_content(getattr(response, "content", ""))
                        # Verifier gate: run the ground-truth check BEFORE
                        # accepting the claim (only when armed and not already
                        # green). A failure with a retry left injects the
                        # grounded verifier output and ``continue``s — funnelling
                        # back through the top-of-loop budget checks FIRST, so
                        # retries are bounded by BOTH verification_max_retries AND
                        # the step/wall budget. Exhausted retries accept the text
                        # but flag it honestly (``verification_failed``); a pass
                        # falls through to the normal ``completed`` accept.
                        if self._verification_active and not self._verify_passed:
                            state.verify_attempts += 1
                            outcome = await self._run_verifier(
                                step=turns, attempt=state.verify_attempts
                            )
                            if outcome.passed:
                                self._verify_passed = True
                                state.verified = True
                            elif self._verify_retries_left > 0:
                                self._verify_retries_left -= 1
                                messages.append(
                                    SystemMessage(
                                        content=get_prompt_registry().render(
                                            "loop.verification_failed",
                                            output=outcome.feedback,
                                        )
                                    )
                                )
                                continue
                            else:
                                final_response = content
                                tool_outputs.append(content)
                                state.done = True
                                state.done_reason = "verification_failed"
                                state.verified = False
                                break
                        # Promise-as-completion gate (root only). A clean
                        # terminal declared while the root's OWN background runs
                        # are still live is a promise about future work — "I'll
                        # check back shortly" with an unfinished probe fleet
                        # behind it — not a completion. Refuse it here, upstream
                        # of the orchestrator's honesty downgrade, and send the
                        # model to await its runs. ``collect_running`` is keyed on
                        # THIS agent so it counts only owned CHILDREN — the seam
                        # deliberately does NOT read the session-wide ownership
                        # index, because the root's own handle is still
                        # ``running`` here (it is marked done only in the loop's
                        # finally), so that index would count the root itself and
                        # refuse every terminal. Bounded, so a model that will not
                        # wait is eventually let through rather than burning the
                        # whole budget spinning.
                        if self._ctx.depth == 0 and promise_nudges < _PROMISE_GATE_MAX_NUDGES:
                            live_owned = await self._ctx.registry.collect_running(
                                self._ctx.agent_id
                            )
                            if live_owned:
                                promise_nudges += 1
                                messages.append(
                                    SystemMessage(
                                        content=get_prompt_registry().render(
                                            "loop.agents_still_running",
                                            count=len(live_owned),
                                            ids=", ".join(
                                                h.agent_id[:8] for h in live_owned
                                            ),
                                        )
                                    )
                                )
                                continue
                        final_response = content
                        tool_outputs.append(content)
                        state.done = True
                        state.done_reason = "completed"
                        # The accept used to be unconditional: a run whose clone
                        # never authenticated still stamped a clean completion
                        # because its LAST turn happened to be text. Consult the
                        # unrecovered blocked-class error instead — it says the
                        # goal was unreachable this run whatever the closing
                        # prose claims. Carried as its OWN field, never as a new
                        # done_reason: that vocabulary is a wire contract shared
                        # with every client, and the status layer owns the
                        # mapping from this code to a user-facing state.
                        if last_blocked is not None:
                            state.blocked_code = last_blocked[1]
                        break

                    # The model is repeating the same tool + input with no
                    # progress. Halt cleanly and hand back for one-click
                    # recovery instead of executing the same call again.
                    doom_guard.observe(response.tool_calls)
                    if doom_guard.is_stuck():
                        # Keep the transcript valid for resume: the AIMessage
                        # with tool_calls was already appended, so synthesize
                        # interrupted results for its dangling calls.
                        repair_tool_pairing(messages)
                        _repeated = response.tool_calls[0].get("name", "tool")
                        last_error = (
                            f"Halted: model repeated '{_repeated}' "
                            f"{doom_guard.threshold}x with identical input and result "
                            "(no progress)."
                        )
                        halt_payload: RecoveryHaltPayload = {
                            "action": "halt_no_progress",
                            "agent_id": self._ctx.agent_id,
                            "depth": self._ctx.depth,
                            "step": turns,
                            "tool": _repeated,
                        }
                        self._emit_event({"type": "recovery", "payload": halt_payload})
                        state.done = True
                        state.done_reason = "halted_no_progress"
                        break

                    # Emit intermediate text as agent_message for trace logs.
                    text_content = self._extract_text_content(getattr(response, "content", ""))
                    if text_content:
                        self._emit_event(
                            {
                                "type": "agent_message",
                                "payload": {
                                    "text": text_content,
                                    "agent_id": self._ctx.agent_id,
                                    "depth": self._ctx.depth,
                                },
                            }
                        )

                    # Execute tool calls with concurrency-aware partitioning.
                    # Exclusive tools run alone; concurrent-safe tools are gathered.
                    specs_map = {s.tool_id: s for s in tool_specs}
                    batches = self._partition_tool_calls(response.tool_calls, specs_map)
                    results: list[ToolCallResult] = []
                    for batch in batches:
                        if batch.concurrent:
                            batch_results = await asyncio.gather(
                                *[self._safe_execute(tc, tool_specs) for tc in batch.calls],
                            )
                            results.extend(batch_results)
                        else:
                            result = await self._safe_execute(batch.calls[0], tool_specs)
                            results.append(result)

                    # Feed results back to the doom guard so "no progress" means
                    # same input AND same outcome — a call whose result advances
                    # (e.g. check_agents as children finish) is healthy progress.
                    doom_guard.record_result(results)

                    # Track the blocked-class condition across turns so the
                    # completion seam can tell "finished" from "gave up".
                    for _r in results:
                        if _r.blocked_code:
                            last_blocked = (_r.tool_id, _r.blocked_code)
                        elif (
                            _r.success
                            and last_blocked is not None
                            and _r.tool_id == last_blocked[0]
                        ):
                            last_blocked = None

                    # Write-progress signal: distinct from the doom guard —
                    # counts consecutive steps for a write-capable agent with
                    # no WRITE-tier tool execution (read/execute/search/
                    # unknown-tier steps only). Observe-only: crossing the
                    # threshold always emits telemetry; the optional reminder
                    # never names the signal or its criteria.
                    write_progress.observe(results, specs_map)
                    if write_progress.threshold_crossed():
                        self._emit_event(
                            {
                                "type": "write_progress_signal",
                                "payload": {
                                    "agent_id": self._ctx.agent_id,
                                    "depth": self._ctx.depth,
                                    "step": turns,
                                    "steps_since_write": write_progress.steps_since_write,
                                    "threshold": write_progress.threshold,
                                },
                            }
                        )
                        if write_progress.reminder_enabled:
                            messages.append(
                                SystemMessage(
                                    content=get_prompt_registry().render(
                                        "loop.task_objective_reminder", goal=state.goal
                                    )
                                )
                            )
                    elif write_progress.exhausted() and not write_progress_stamped:
                        # Go quiet after the last event — flag it once for the
                        # parent's global eye instead of firing forever.
                        write_progress_stamped = True
                        _h_write_progress = await self._ctx.registry.get(self._ctx.agent_id)
                        if _h_write_progress:
                            _h_write_progress.progress_note = (
                                "No write-tier tool call in the last "
                                f"{write_progress.steps_since_write} steps."
                            )

                    for tool_call, result in zip(response.tool_calls, results):
                        messages.append(
                            ToolMessage(
                                content=result.content,
                                tool_call_id=result.tool_call_id,
                            )
                        )
                        tool_outputs.append(f"{result.tool_id}: {result.content}")
                        if not result.success:
                            last_error = result.content
                        # Track as ActionStep for TaskQueue compatibility.
                        action_step = self._tool_call_to_action_step(tool_call)
                        mock = get_mock_speaker()
                        action_step.result = mock(content=result.content)
                        executed_steps.append(action_step)

                    # Episodic plan-mode: a session tool (e.g. exit_plan_mode)
                    # signals the loop to terminate so the thread exits
                    # cleanly. Approval/rejection happens out-of-band via
                    # SessionRuntime. Materialise the list so every tool's
                    # flag is consumed — ``any`` short-circuits and would
                    # leave a second tool's flag set for the next turn.
                    # ``terminal_reason()`` lets each tool declare the right
                    # done_reason (default: "awaiting_approval"; emit tool: "completed").
                    term_tools = [
                        (t, t.should_terminate_run()) for t in self._session_tools
                    ]
                    terminating = [t for t, flag in term_tools if flag]
                    if terminating:
                        state.done = True
                        state.done_reason = terminating[0].terminal_reason()
                        break

                    # Update registry step count.
                    await self._ctx.registry.update_step(
                        self._ctx.agent_id,
                        results[-1].tool_id if results else "",
                    )
                    turns += 1

                    # Ref: [DeepMind-Delegation §4.5] Auto-update progress
                    # for parent monitoring. Zero token cost — direct write.
                    if self._ctx.depth > 0 and results:
                        last = results[-1]
                        note = f"turn {turns}: {last.tool_id}"
                        snippet = last.content[:100] if last.content else ""
                        if last.success:
                            note += f" -> {snippet}"
                        else:
                            note += f" -> FAILED: {snippet}"
                        handle = await self._ctx.registry.get(
                            self._ctx.agent_id,
                        )
                        if handle:
                            handle.progress_note = note

                    # Inject failure feedback so the model can adapt.
                    failures = [r for r in results if not r.success]
                    if failures:
                        messages.append(
                            SystemMessage(
                                content=f"{len(failures)}/{len(results)} tool call(s)"
                                " failed this step — adapt your approach."
                            )
                        )

                    # Re-bind newly discovered deferred tools. Discovery is
                    # derived from the message history each turn, so this is
                    # compaction-resilient: whatever survives compaction
                    # still drives the bound set on the next iteration.
                    if self._tool_search_enabled and self._deferred_ids:
                        discovered = self._discovered_from_messages(messages)
                        active_specs = self._select_active_specs(
                            self._tool_specs_full, discovered=discovered
                        )
                        new_active_ids = {s.tool_id for s in active_specs}
                        if new_active_ids != self._last_active_ids:
                            tool_schemas = self._build_tool_schemas_for_mode(
                                active_specs, self._current_mode
                            )
                            model = self._bind_model(tool_schemas)
                            self._last_active_ids = new_active_ids

                    # Proactive mid-loop compaction check.
                    if self._should_compact_messages(messages):
                        _compact_info = await self._compact_messages(messages)
                        if _compact_info:
                            await self._ctx.registry.record_compaction(
                                self._ctx.agent_id,
                            )
                            self._emit_event(
                                {
                                    "type": "context_compacted",
                                    "payload": {
                                        **_compact_info,
                                        "agent_id": self._ctx.agent_id,
                                        "depth": self._ctx.depth,
                                        "mode": "mid_loop",
                                        "turn": turns,
                                    },
                                }
                            )

                    if span is not None:
                        try:
                            span.update_trace(
                                output={
                                    "tool_calls": len(response.tool_calls),
                                    "turns": turns,
                                }
                            )
                        except Exception:
                            pass

            # Safety net: currently unreachable (all loop exits set
            # state.done=True), but retained as defensive code for future
            # exit paths that may break without setting state.done.
            if not state.done and final_response is None and messages:
                # Ref: [DeepMind-Delegation §4.5] Inject child results at synthesis.
                # Root may have non-blocking children still running — give them
                # a brief grace period, then include available results.
                if self._ctx.depth == 0:
                    running = await self._ctx.registry.collect_running(
                        self._ctx.agent_id,
                    )
                    if running:
                        await asyncio.sleep(2.0)
                    completed = await self._ctx.registry.collect_completed(
                        self._ctx.agent_id,
                    )
                    if completed:
                        result_lines = []
                        for h in completed:
                            r = h.result
                            if r:
                                result_lines.append(
                                    f"[{h.agent_id[:8]}] {r.status}: {r.summary or r.content[:300]}"
                                )
                        if result_lines:
                            messages.append(
                                SystemMessage(
                                    content=get_prompt_registry().render(
                                        "loop.agent_results_header",
                                        joined="\n".join(result_lines),
                                    ),
                                )
                            )
                    still_running = await self._ctx.registry.collect_running(
                        self._ctx.agent_id,
                    )
                    if still_running:
                        ids = ", ".join(h.agent_id[:8] for h in still_running)
                        messages.append(
                            SystemMessage(
                                content=get_prompt_registry().render(
                                    "loop.agents_still_running",
                                    count=len(still_running),
                                    ids=ids,
                                ),
                            )
                        )

                final_response = await self._unbound_wrapup_invoke(
                    prompt_key="loop.final_answer_synthesis",
                    model_name=self._ctx.model_name,
                    messages=messages,
                    tool_outputs=tool_outputs,
                    invoke_config=invoke_config,
                )

            if not state.done:
                state.done = True
                state.done_reason = "completed"

            # Forced closing summary for a CHILD that finished via store
            # side-effects. The heaviest sub-agents did all their real work
            # through tool writes and then stopped on empty text, so the parent
            # — which projects ``task_result`` as the child's summary — received
            # nothing, and a 3.3M-token probe reached its parent blank. One
            # unbound wrap-up turn forces a compressed summary before stop.
            # Root is exempt: its empty terminal is a user-facing turn, not a
            # summary owed upstream. A cancel is exempt too — it is not a
            # completion to summarize. The budget/synthesis paths already filled
            # ``final_response``, so the empty-guard skips them (no double turn).
            if (
                self._ctx.depth > 0
                and state.done_reason != "canceled"
                and not (final_response or "").strip()
            ):
                final_response = await self._unbound_wrapup_invoke(
                    prompt_key="loop.final_answer_synthesis",
                    model_name=self._active_model,
                    messages=messages,
                    tool_outputs=tool_outputs,
                    invoke_config=invoke_config,
                )

            # Build TaskQueue for compatibility with CLI / API consumers.
            plan_steps = list(plan.steps) if plan and plan.steps else []
            task_queue = TaskQueue(
                plan_steps=plan_steps,
                action_steps=executed_steps,
            )
            # task_result is the LLM's final synthesized text only.
            task_queue.task_result = (final_response or "").strip()
            task_queue.last_error = last_error
            state.tool_results = tool_outputs
            # Every OTHER terminal path (halt, budget wrap-up, verifier
            # exhaustion, plan-mode exit) carries the same fact — a blocked run
            # is blocked regardless of which exit it took.
            if state.blocked_code is None and last_blocked is not None:
                state.blocked_code = last_blocked[1]

        finally:
            # Close Langfuse agent span and propagation context.
            if agent_span is not None:
                try:
                    agent_span.update(
                        output={
                            "total_steps": turns,
                            "done_reason": state.done_reason or "unknown",
                        }
                    )
                except Exception:
                    pass
            if _propagate_cm is not None:
                try:
                    _propagate_cm.__exit__(None, None, None)
                except Exception:  # pragma: no cover - defensive
                    pass
            if _agent_span_cm is not None:
                try:
                    _agent_span_cm.__exit__(None, None, None)
                except Exception:  # pragma: no cover - defensive
                    pass

            # Stop the background watchdog.
            if watchdog_task is not None and not watchdog_task.done():
                watchdog_task.cancel()

            # Wait for lifecycle managers to complete cleanup.
            if self._spawn_agent_tool is not None:
                await self._spawn_agent_tool.await_lifecycle_managers(timeout=3.0)

            # Cleanup: cancel any child agents still running.
            children = await self._ctx.registry.list_children(self._ctx.agent_id)
            for child in children:
                if child.status == "running":
                    await self._ctx.registry.cancel_agent(child.agent_id)

            await self._ctx.registry.mark_done(
                self._ctx.agent_id,
                "completed" if state.done else "failed",
            )

        return task_queue, state

    # ------------------------------------------------------------------
    # Error-isolated task wrapper
    # ------------------------------------------------------------------

    # A wait/sync tool (``check_agents``) bounds its OWN wait internally on the
    # requested ``timeout``; the outer execution ceiling must never clip that
    # below what the model asked for. Headroom on top of the requested wait for
    # the post-wait agent-tree render + result collection.
    _WAIT_TOOL_RENDER_MARGIN_S = 30.0

    def _get_tool_timeout(self, tool_name: str) -> float:
        """Get timeout for a tool. Uses spec.timeout, falls back to 120s."""
        spec = self._tool_registry.get_spec(tool_name) if self._tool_registry else None
        if spec:
            return spec.timeout
        return 120.0

    def llm_call_stall_age(self, now: float) -> float | None:
        """Seconds the in-flight LLM call has been outstanding, else ``None``.

        Pure read over ``(_llm_call_started_at, now)`` with no clock of its own,
        so the liveness rule is exercisable at any age without waiting for one
        — the watchdog supplies ``time.monotonic()``, a test supplies a number.
        """
        started = self._llm_call_started_at
        if started is None:
            return None
        return max(0.0, now - started)

    def _loop_injected_admitted(self, tool_id: str) -> bool:
        """Whether the tool ceiling admits a tool this loop injects itself.

        The loop binds several tools OUTSIDE ``filter_specs`` by design — the
        spawn family, ``activate_skill``, the agent-management tools, the
        inline session tools. By design also meant outside the allowlist, so a
        strictly-scoped agent whose AgentDef named five tools was in fact
        handed those five plus everything here. That surplus is what a
        wandering run wanders into: across the observed burner sessions the
        winners and the burners ran the SAME model on the SAME task, and what
        separated them was reachable tool surface, not capability.

        The gate is the SAME shape as the spawn seam's (``spawn_in_scope``) and
        obeys the same three-state law: ``None`` is unrestricted, ``[]`` grants
        nothing, a list grants exactly its members. Crucially it fires ONLY
        under STRICT scope — a permissive console/mobile root always sends a
        large ``context.mcp_tools`` list that is a ceiling over MCP tools alone
        and never names a built-in, so treating it as authoritative here would
        strip every such session's built-ins. That is the same trap that once
        silently broke the mobile alarm flow.
        """
        if not self._strict_tool_scope or self._allowed_tools is None:
            return True
        return tool_id in self._allowed_tools

    def _poll_class_rules(self, tool_specs: list[ToolSpec]) -> list[PollClassRule]:
        """Poll-class rules for this run, gathered from what each tool declares.

        Two declaration seams, because this loop binds two populations that
        never meet: a registry tool declares ``ToolSpec.poll`` /
        ``poll_when_args``, and a session tool (absent from the registry
        entirely) declares ``poll_class`` / ``poll_when_args``. Both are read
        via ``getattr`` — ``SessionTool`` is a structural Protocol whose
        defaults a standalone implementer does not inherit, and these are
        optional attributes rather than contract members.

        Resolving per run from declarations is what keeps core out of it: no
        tool id appears here, so the next self-polling tool is added by
        declaring it on that tool, in whichever package owns it. A nested run's
        status probe answering "processing" twice in under a second is honest
        waiting, and no repetition-based detector can tell that from a stuck
        loop unless the tool says which call is which.
        """
        rules: list[PollClassRule] = []
        for source in (*tool_specs, *self._session_tools):
            tool_id = getattr(source, "tool_id", "")
            if not tool_id:
                continue
            when_args = tuple(getattr(source, "poll_when_args", ()) or ())
            if when_args:
                rules.append(
                    PollClassRule(tool_id=tool_id, when_args=frozenset(when_args))
                )
            # ``poll`` on a registry spec, ``poll_class`` on a session tool —
            # either spelling means "every call to me is a poll".
            elif getattr(source, "poll", False) or getattr(source, "poll_class", False):
                rules.append(PollClassRule(tool_id=tool_id))
        return rules

    def _result_char_cap(self, tool_id: str) -> int:
        """The model-facing result cap for *tool_id* (0 = uncapped).

        Resolution order — session-tool declaration, then registry spec, then
        the 2000 default. The session-tool arm is what this exists for: a
        session tool is never registered in the stateless ``ToolRegistry``, so
        ``get_spec`` returns ``None`` for EVERY one of them and the default
        applied to the whole class by accident rather than by any decision. The
        number is calibrated for unbounded shell/MCP output and is wrong for a
        curated first-party payload: a repo-manifest scan reached the model as
        22 of ~1,900 paths — 1.14% — while the next step of its own playbook
        required picking files out of that manifest.

        The ONE resolver behind both the model-facing truncation and the event
        summary, so the store can never claim a cap the model did not read.
        """
        session_tool = next(
            (t for t in self._session_tools if t.tool_id == tool_id), None
        )
        if session_tool is not None:
            # ``SessionTool`` is a structural Protocol: a standalone implementer
            # inherits no defaults from it, so the declaration must be read
            # defensively rather than assumed present on the instance.
            declared = getattr(session_tool, "max_result_chars", None)
            if isinstance(declared, int) and not isinstance(declared, bool):
                return declared
            return DEFAULT_SESSION_TOOL_MAX_RESULT_CHARS
        spec = self._tool_registry.get_spec(tool_id) if self._tool_registry else None
        return spec.max_result_chars if spec else 2000

    def _tool_execution_timeout(self, tool_name: str, tool_call: Any) -> float:
        """The outer ``asyncio.wait_for`` ceiling for one tool call.

        For a self-bounding WAIT tool (``DOOM_LOOP_EXEMPT_TOOLS`` — currently
        ``check_agents``) the model's requested ``wait``/``timeout`` is the real
        bound; the outer ceiling must sit ABOVE it or a legitimate long wait
        aborts with "timed out after 120.0s" the requested 300s never reached
        (production: three such clips). Every other tool keeps the flat ceiling.
        """
        ceiling = self._get_tool_timeout(tool_name)
        if tool_name not in DOOM_LOOP_EXEMPT_TOOLS:
            return ceiling
        args = tool_call.get("args") if isinstance(tool_call, dict) else None
        if not isinstance(args, dict) or not args.get("wait"):
            return ceiling
        requested = args.get("timeout")
        if isinstance(requested, bool) or not isinstance(requested, (int, float)):
            return ceiling
        return max(ceiling, float(requested) + self._WAIT_TOOL_RENDER_MARGIN_S)

    def _partition_tool_calls(
        self, tool_calls: list[Any], specs_map: dict[str, ToolSpec]
    ) -> list[ToolBatch]:
        """Group consecutive concurrent-safe tools; isolate exclusive tools."""
        batches: list[ToolBatch] = []
        current_concurrent: list[Any] = []
        for tc in tool_calls:
            spec = specs_map.get(tc.get("name", ""))
            is_safe = spec.concurrency_safe if spec else True
            if is_safe:
                current_concurrent.append(tc)
            else:
                if current_concurrent:
                    batches.append(ToolBatch(calls=list(current_concurrent), concurrent=True))
                    current_concurrent = []
                batches.append(ToolBatch(calls=[tc], concurrent=False))
        if current_concurrent:
            batches.append(ToolBatch(calls=list(current_concurrent), concurrent=True))
        return batches

    async def _safe_execute(
        self,
        tool_call: Any,
        tool_specs: list[ToolSpec],
    ) -> ToolCallResult:
        """Execute a tool call with timeout.

        Catches exceptions so gather does not cancel siblings.
        """
        tool_name = tool_call.get("name", "")
        timeout = self._tool_execution_timeout(tool_name, tool_call)
        # Ref: [AgentCgroup §4.2] Stamp the in-flight tool BEFORE awaiting it so
        # the watchdog can attribute a stall to the tool actually running, not
        # the last one that completed (``update_step`` only fires after this
        # call returns). Cleared in ``finally`` — including on a concurrent
        # batch, where a sibling's clear can race this one; that only degrades
        # attribution to "unknown", never to a wrong tool name.
        await self._ctx.registry.mark_tool_start(self._ctx.agent_id, tool_name)
        try:
            return await asyncio.wait_for(
                self._execute_tool_call(tool_call, tool_specs),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            error_msg = f"Tool '{tool_name}' timed out after {timeout}s"
            logging.error(error_msg)
            # A timeout kills ``_execute_tool_call`` mid-flight, so the emit at
            # its tail never runs and the step left NO ``tool_result`` event
            # behind at all. The durable record then showed 100% tool success
            # for runs with known live timeouts — the failure was not
            # under-reported but structurally absent, and no amount of reading
            # the store could have found it. Every exit of this method emits.
            self._emit_timeout_or_crash_result(tool_call, error_msg)
            return ToolCallResult(
                tool_call_id=tool_call.get("id", ""),
                tool_id=tool_name,
                content=f"ERROR: {error_msg}",
                success=False,
            )
        except asyncio.CancelledError:
            raise  # Must propagate for TaskGroup cancellation.
        except Exception as exc:
            # Same contract as the timeout branch: an exception that escaped
            # every inner handler would otherwise return a failed result the
            # event log has no record of.
            self._emit_timeout_or_crash_result(tool_call, str(exc))
            return ToolCallResult(
                tool_call_id=tool_call.get("id", ""),
                tool_id=tool_name,
                content=f"ERROR: {exc}",
                success=False,
            )
        finally:
            await self._ctx.registry.mark_tool_start(self._ctx.agent_id, None)

    def _emit_timeout_or_crash_result(self, tool_call: Any, error: str) -> None:
        """Emit the ``tool_result`` event for a call that died inside the wrapper.

        Best-effort by construction: this runs on a path that is ALREADY
        failing, so a fault while building the ActionStep must not replace the
        tool's real error with an event-emission error.
        """
        try:
            self._emit_tool_result_event(
                self._tool_call_to_action_step(tool_call), None, error=error
            )
        except Exception as emit_exc:  # noqa: BLE001 — never mask the real failure
            logging.warning("failed to emit tool_result for a failed call: {}", emit_exc)

    # ------------------------------------------------------------------
    # Message construction
    # ------------------------------------------------------------------

    def _build_messages(
        self,
        user_query: str,
        context: ContextSnapshot | None,
        plan: Plan | None,
        agent_tree: str = "",
    ) -> list[BaseMessage]:
        """Build the initial message list for the conversation."""
        system_prompt = self._render_system_prompt(context, plan, agent_tree)
        return self._messages_from_system_prompt(system_prompt, user_query, context)

    def _render_system_prompt(
        self,
        context: ContextSnapshot | None,
        plan: Plan | None,
        agent_tree: str = "",
    ) -> str:
        """Assemble the system-prompt text for the ACTIVE model.

        Extracted from ``_build_messages`` so the fallback ladder can
        re-render it against the ESCALATED model mid-run (its per-model prompt
        overrides) without rebuilding the whole transcript — see
        ``_apply_model_escalation``. Reads ``self._active_model`` (the escalated
        model after a sticky switch, else the configured primary).
        """
        registry = get_prompt_registry()
        model = self._active_model
        system_parts: list[str] = [get_system_prompt("system")]

        # Environment context.
        work_dir = self._cwd or str(Path.cwd())
        system_parts.append(
            registry.render(
                "loop.section.environment",
                model=model,
                work_dir=work_dir,
                platform=_platform.system().lower(),
                date=_date.today().isoformat(),
                version=get_version(),
            )
        )

        # Project instructions (CLAUDE.md / AGENTS.md).
        if self._project_instructions:
            system_parts.append(
                registry.render(
                    "loop.section.project_instructions",
                    model=model,
                    project_instructions=self._project_instructions,
                )
            )

        # Operator-authored custom instructions (rendered upstream, in the
        # orchestrator). Sits right after the project instructions so the
        # operator's text reads as an extension of them.
        if self._user_instructions:
            system_parts.append(
                registry.render(
                    "loop.section.user_instructions",
                    model=model,
                    user_instructions=self._user_instructions,
                )
            )

        # Ref: [DeepMind-Delegation §4.5] Root agent's global eye — live agent tree
        if agent_tree:
            system_parts.append(
                registry.render("loop.section.agent_tree", model=model, agent_tree=agent_tree)
            )

        # Git context (injected after project instructions).
        git_ctx = get_git_context(self._cwd)
        if git_ctx:
            system_parts.append(
                registry.render("loop.section.git_context", model=model, git_ctx=git_ctx)
            )

        # Active skill instructions (from user /skill invocation).
        if self._skill_instructions:
            system_parts.append(
                registry.render(
                    "loop.section.skill_instructions",
                    model=model,
                    skill_instructions=self._skill_instructions,
                )
            )

        # Resilience note (error-visibility seam): the harness's own retry /
        # fallback activity, fed back as grounded fact so the model is not blind
        # to its own retries. Its OWN slot — never overloaded onto
        # ``skill_instructions`` — and empty (skipped) on a clean run.
        resilience_note = self._resilience_note.render()
        if resilience_note:
            system_parts.append(resilience_note)

        # Auto-invocable skills catalog (for LLM-driven activation).
        if self._skill_registry is not None:
            catalog = self._skill_registry.render_catalog(self._session_capabilities)
            if catalog:
                system_parts.append(catalog)

        # Registered agent types catalog (for spawn_agent agent_type selection).
        if self._agent_registry is not None:
            agent_catalog = self._agent_registry.render_catalog(self._session_capabilities)
            if agent_catalog:
                system_parts.append(agent_catalog)

        # Session context.
        if context and context.summary:
            system_parts.append(
                registry.render(
                    "loop.section.session_summary", model=model, summary=context.summary
                )
            )
        if context and context.recent_events:
            rendered = render_event_lines(context.recent_events)
            if rendered:
                system_parts.append(
                    registry.render(
                        "loop.section.recent_conversation", model=model, rendered=rendered
                    )
                )

        # Attached file contents.
        if context and context.attachment_texts:
            system_parts.append(
                registry.render(
                    "loop.section.attached_files",
                    model=model,
                    joined="\n---\n".join(context.attachment_texts),
                )
            )

        # Tool-specific guidance from prompt files.
        tool_guidance = self._render_tool_guidance()
        if tool_guidance:
            system_parts.append(
                registry.render(
                    "loop.section.tool_guidance", model=model, tool_guidance=tool_guidance
                )
            )

        # Deferred-tool catalog (names only). Schemas are fetched on demand
        # by the model via the ``tool_search`` tool; the per-turn re-bind in
        # ``run()`` then makes the matched tools invocable.
        deferred_block = self._render_deferred_tool_block()
        if deferred_block:
            system_parts.append(deferred_block)

        # Plan context.
        if plan and plan.steps:
            plan_lines = "\n".join(
                f"{i + 1}. {s.title} — {s.description}" for i, s in enumerate(plan.steps)
            )
            system_parts.append(
                registry.render(
                    "loop.section.plan_execution", model=model, plan_lines=plan_lines
                )
            )

        # Depth-aware sub-agent guidance.
        system_parts.append(self._build_depth_guidance())

        # Plan-mode reminder — injected when the loop was started in plan
        # mode. Rendered with the session-scoped plan path and the shell
        # command allowlist so the model knows exactly what it may write
        # and which shell commands it is permitted to run.
        if self._current_mode == "plan" and self._session_id is not None:
            if self._ctx.depth == 0:
                # Root hypervisor: automata prompt + plan path for review.
                try:
                    hyper_template = registry.render("file.plan_hypervisor").strip()
                except OSError:
                    hyper_template = ""
                if hyper_template:
                    plan_path = plan_file_for(self._session_id)
                    system_parts.append(
                        hyper_template
                        + registry.render("loop.plan_file_suffix", plan_path=plan_path)
                    )
            else:
                # Plan sub-agent: full plan-mode prompt with placeholders.
                plan_path = plan_file_for(self._session_id)
                shell_allowlist = self._plan_mode_shell_allowlist()
                if shell_allowlist:
                    bullets = "\n".join(f"    - `{entry}`" for entry in shell_allowlist)
                else:
                    bullets = "    - (none — shell is disabled in plan mode)"
                system_parts.append(
                    registry.render(
                        "loop.plan_mode_reminder",
                        plan_path=plan_path,
                        shell_allowlist_bullets=bullets,
                    )
                )

        return "\n\n".join(p for p in system_parts if p)

    def _messages_from_system_prompt(
        self,
        system_prompt: str,
        user_query: str,
        context: ContextSnapshot | None,
    ) -> list[BaseMessage]:
        """Wrap the assembled system prompt + user turn into the message list."""
        # If the active context carries images for a vision-capable model,
        # build a multipart HumanMessage that interleaves the user's text
        # with ``image_url`` parts (LiteLLM/OpenAI Chat Completions format).
        # Otherwise stick with plain-string content to keep the cache
        # prefix friendly.
        image_parts = list(getattr(context, "attachment_images", []) or []) if context else []
        if image_parts:
            # langchain's HumanMessage expects ``list[str | dict]`` (invariant);
            # widen the element type so mypy accepts mixed text/image parts.
            human_content: list[str | dict] = [
                {"type": "text", "text": user_query},
                *image_parts,
            ]
            return [SystemMessage(content=system_prompt), HumanMessage(content=human_content)]
        return [SystemMessage(content=system_prompt), HumanMessage(content=user_query)]

    def _build_depth_guidance(self) -> str:
        """Build delegation-lifecycle-aware prompt guidance.

        Ref: [DeepMind-Delegation §4.1] Contract-first decomposition — root
        agents define verifiable acceptance criteria for sub-tasks.
        Ref: [CoA §3.2] Manager/worker role separation — root synthesizes,
        sub-agents execute.
        Ref: [Aletheia §3] Verification by same model in different role
        prevents confirmation bias.
        Ref: [DeepMind-Delegation §4.7] Liability firebreaks at chain boundaries.
        """
        depth = self._ctx.depth
        max_depth = self._ctx.max_depth
        remaining = self._ctx.remaining_depth
        is_root = depth == 0
        is_leaf = not self._ctx.can_spawn
        registry = get_prompt_registry()
        model = self._active_model

        if is_root:
            # Ref: [CoA §3.2] Root = manager/hypervisor.
            # Ref: [DeepMind-Delegation §4.4] Non-blocking delegation protocol.
            # The base template carries both openings (plan vs direct execution)
            # plus the shared delegation/safety/synthesize/awareness/stop tail.
            return registry.render(
                "loop.depth.root",
                model=model,
                plan_mode=self._current_mode == "plan",
                depth=depth,
                max_depth=max_depth,
            )
        if is_leaf:
            # Ref: [DeepMind-Delegation §4.7] Liability firebreak at leaf
            return registry.render(
                "loop.depth.leaf", model=model, depth=depth, max_depth=max_depth
            )
        # Sub-orchestrator: can delegate but has bounded scope.
        guidance = registry.render(
            "loop.depth.suborchestrator",
            model=model,
            depth=depth,
            max_depth=max_depth,
            remaining=remaining,
        )
        if remaining <= 2:
            # Ref: [DeepMind-Delegation §4.7] Approaching delegation boundary
            guidance += registry.render("loop.depth.boundary", model=model)
        return guidance

    # ------------------------------------------------------------------
    # Background watchdog  (Ref: [AgentCgroup §4.2])
    # ------------------------------------------------------------------

    async def _watchdog(self) -> None:
        """Code-level safety monitor — zero token cost.

        Periodically checks for stalled agents and injects NL warnings.
        Runs only for the root agent as a background asyncio task.

        Ref: [AgentCgroup §4.2] Graduated enforcement via NL injection.
        Ref: [DeepMind-Delegation §4.4] Internal trigger: delegatee
        unresponsive → diagnose → intervene.
        """
        stall_threshold = float(get_config_value("agent", "stall_threshold_s", default=120.0))
        check_interval = float(get_config_value("agent", "stall_check_interval_s", default=30.0))
        # Deliberately well past the retry strategy's per-attempt timeout AND
        # its turn deadline: reaching this age means the bound that should have
        # cancelled the call did not, which is the only case worth an alarm.
        llm_liveness_s = float(get_config_value("agent", "llm_call_liveness_s", default=300.0))
        # Per-agent ids already warned of their OWN wall
        # deadline, so the 80% warning fires exactly once per agent across the
        # whole watchdog lifetime (never re-sent on every sweep).
        warned_wall_deadline: set[str] = set()
        # Steps already reported as wedged, so one wedge yields one event rather
        # than one per sweep. Re-arms naturally: the next call is a new step.
        reported_wedged_steps: set[int] = set()
        try:
            while True:
                await asyncio.sleep(check_interval)

                # LLM-call liveness. The stall sweep below cannot see this: it
                # keys on TOOL activity, and a wedged model call has no tool in
                # flight, so the agent looks merely quiet. Emitting makes the
                # wedge visible while it is happening instead of leaving an
                # ``llm_call_start`` with no end for a boot-time sweeper to find.
                age = self.llm_call_stall_age(_time.monotonic())
                if (
                    age is not None
                    and llm_liveness_s > 0
                    and age >= llm_liveness_s
                    and self._llm_call_step not in reported_wedged_steps
                ):
                    reported_wedged_steps.add(self._llm_call_step)
                    self._emit_event(
                        {
                            "type": "llm_call_stalled",
                            "payload": {
                                "agent_id": self._ctx.agent_id,
                                "depth": self._ctx.depth,
                                "step": self._llm_call_step,
                                "model": self._active_model,
                                "age_s": round(age, 1),
                                "threshold_s": llm_liveness_s,
                            },
                        }
                    )

                stalled = await self._ctx.registry.stalled_agents(threshold=stall_threshold)
                for h in stalled:
                    await self._ctx.registry.send_message(
                        h.agent_id,
                        get_prompt_registry().render("loop.stall_warning"),
                    )
                    # Root self-stall: this loop IS the one synchronously
                    # awaiting the stalled tool call, so a line queued here can
                    # only be drained AFTER that call resolves — stale and
                    # misleading by the time it arrives. Never enqueue it for
                    # self; the send_message warning above still reaches
                    # genuinely separate sub-agents (their own message_queue is
                    # drained mid-run by their own loop, not this one).
                    if h.agent_id == self._ctx.agent_id:
                        continue
                    if self._ctx.message_queue is not None:
                        # active_tool_id is the tool actually IN FLIGHT at
                        # detection time; last_tool_id (only updated on
                        # completion) would name the wrong, already-finished
                        # tool for a call still running.
                        detail = (
                            f"stalled on {h.active_tool_id}"
                            if h.active_tool_id
                            else f"stalled — no tool activity for over {stall_threshold:.0f}s"
                        )
                        self._ctx.message_queue.put_nowait(
                            f"[Watchdog: Agent {h.agent_id[:8]} {detail}]",
                        )

                # Per-contract wall-clock deadline sweep.
                # Mirrors the stall sweep above but keyed on an agent's OWN
                # declared bound, never global inactivity. Two-strike by
                # design: the 80% warn ALWAYS precedes the 100% cancel, so
                # cancel_agent here is the ONE contract-driven kill switch in
                # the codebase — every other agent termination is either
                # graceful (natural completion) or parent-requested
                # (steer_agent cancel).
                for h, wall_state in await self._ctx.registry.over_wall_deadline_agents():
                    if wall_state == "over":
                        await self._ctx.registry.cancel_agent(h.agent_id)
                        continue
                    if h.agent_id in warned_wall_deadline:
                        continue
                    warned_wall_deadline.add(h.agent_id)
                    await self._ctx.registry.send_message(
                        h.agent_id,
                        get_prompt_registry().render("loop.wall_deadline_warning"),
                    )
        except asyncio.CancelledError:
            pass  # Normal shutdown path.

    def _render_tool_guidance(self) -> str:
        """Render tool-specific prompt guidance for local tools.

        Routes through the prompt registry WITH the active model so a per-model
        override of a tool-guidance entry (e.g. the structured-patch discipline
        nudge on ``file.tools.file-edit``, paired with the edit-tool variant via
        the shared model prefix) actually applies. ``get_system_prompt``
        alone can't: a tool's ``prompt_path`` (``tools/file-edit``) maps to the
        dotted registry id ``file.tools.file-edit``, so the slash form misses the
        registry and would silently read the raw ``.txt``, dropping ``model=``.
        Falls back to the legacy path for any prompt the registry doesn't
        inventory. Byte-identical to the old output when no override matches.
        """
        registry = get_prompt_registry()
        prompts: list[str] = []
        for spec in self._tool_registry.list_specs():
            if spec.kind != "local" or not spec.prompt_path:
                continue
            try:
                reg_id = "file." + spec.prompt_path.replace("/", ".")
                if registry.has(reg_id):
                    prompt = registry.render(reg_id, model=self._active_model).strip()
                else:
                    prompt = get_system_prompt(spec.prompt_path)
            except OSError:
                continue
            if prompt:
                prompts.append(prompt)
        return "\n\n".join(prompts)

    # ------------------------------------------------------------------
    # Skill activation (internal tool handler)
    # ------------------------------------------------------------------

    def _handle_activate_skill(self, action_step: ActionStep) -> Any:
        """Handle an ``activate_skill`` tool call from the LLM.

        Returns the skill body as a mock result so it arrives as a
        ``ToolMessage`` — the LLM reads the instructions and follows them.
        """
        from mewbo_core.skills import activate_skill

        args = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        skill_name = str(args.get("skill_name", ""))
        skill_args = str(args.get("args", ""))

        registry = self._skill_registry
        skill = (
            registry.get(skill_name, self._session_capabilities) if registry else None
        )
        if skill is None:
            msg = f"ERROR: Unknown skill '{skill_name}'"
            return type("R", (), {"content": msg})()
        if skill.disable_model_invocation:
            msg = f"ERROR: Skill '{skill_name}' is user-invocable only"
            return type("R", (), {"content": msg})()

        instructions, _ = activate_skill(skill, skill_args)
        logging.info("LLM auto-activated skill '{}'", skill_name)
        body = f"## Skill: {skill_name}\n\n{instructions}\n\nFollow these instructions now."
        return type("R", (), {"content": body})()

    # ------------------------------------------------------------------
    # Model binding
    # ------------------------------------------------------------------

    def _configured_edit_tool_id(self) -> str:
        """Return the tool_id for the edit tool appropriate for the active model.

        Prefers model-derived capability detection (via
        ``llm.model_prefers_structured_patch``).  The ``agent.edit_tool``
        config value is honoured as an explicit override when non-empty.
        """
        from mewbo_core.llm import model_prefers_structured_patch

        # Explicit user override always wins
        override = get_config_value("agent", "edit_tool", default="")
        if override == "structured_patch":
            return "file_edit_tool"
        if override == "search_replace_block":
            return "aider_edit_block_tool"

        # Derive from the ACTIVE model identity (escalated model after a
        # sticky switch, else the configured primary) so the tool VARIANT adapts
        # in lockstep with the per-model prompt on escalation.
        model_name: str | None = getattr(self, "_active_model", None) or getattr(
            self._ctx, "model_name", None
        )
        if model_prefers_structured_patch(model_name):
            return "file_edit_tool"
        return "aider_edit_block_tool"

    def _plan_mode_shell_allowlist(self) -> list[str]:
        """Return the configured shell command prefix allowlist for plan mode.

        Read from ``agent.plan_mode_shell_allowlist``. An empty list means
        the shell tool is disabled in plan mode.
        """
        raw = get_config_value("agent", "plan_mode_shell_allowlist", default=[])
        if isinstance(raw, list):
            return [str(item).strip() for item in raw if str(item).strip()]
        if isinstance(raw, str):
            return [entry.strip() for entry in raw.split(",") if entry.strip()]
        return []

    # ------------------------------------------------------------------
    # LLM call resilience (retry / fallback / circuit-break / budget)
    # ------------------------------------------------------------------

    @staticmethod
    def _chunk_delta_text(chunk: Any) -> str:
        """Incremental text carried by one streamed chunk (whitespace-preserving).

        Unlike ``_extract_text_content`` this never strips — token deltas must
        keep their spacing — and concatenates list-form text blocks without the
        newline separator (a chunk is a fragment, not a finished message).
        """
        content = getattr(chunk, "content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        return ""

    async def _acall_model(
        self,
        bound: Any,
        messages: list[BaseMessage],
        *,
        config: dict[str, Any] | None,
        step: int,
    ) -> AIMessage:
        """Invoke the model, streaming token deltas for true time-to-first-token.

        Consumes ``bound.astream`` and emits one ``agent_message_delta`` event per
        text chunk so clients render tokens as the model produces them, then
        returns the fully-accumulated message — content, ``tool_calls`` and
        ``usage_metadata`` are identical in shape to ``ainvoke`` (LangChain's
        ``AIMessageChunk.__add__`` aggregates tool-call chunks and sums usage;
        ``ChatLiteLLM._astream`` already requests ``stream_options.include_usage``
        so the usage chunk arrives). Falls back to ``ainvoke`` when no usable
        stream is available (a stubbed model, or any path that yields nothing) so
        non-streaming callers are byte-for-byte unaffected. Real provider errors
        mid-stream propagate to the resilience strategy — only the structural
        "no usable stream" signals are swallowed here.
        """
        cfg = config or None
        accumulated: AIMessageChunk | None = None
        try:
            async for chunk in bound.astream(messages, config=cfg):
                accumulated = chunk if accumulated is None else accumulated + chunk
                delta = self._chunk_delta_text(chunk)
                if delta:
                    self._emit_event(
                        {
                            "type": "agent_message_delta",
                            "payload": {
                                "text": delta,
                                "agent_id": self._ctx.agent_id,
                                "depth": self._ctx.depth,
                                "step": step,
                            },
                        }
                    )
        except (TypeError, AttributeError, NotImplementedError):
            # ``bound`` exposes no usable async stream (e.g. a stubbed model) —
            # fall through to the buffered path. Provider/runtime errors are NOT
            # caught here; they belong to the resilience strategy.
            accumulated = None
        if accumulated is None:
            return await bound.ainvoke(messages, config=cfg)
        return AIMessage(
            content=accumulated.content,
            tool_calls=list(getattr(accumulated, "tool_calls", []) or []),
            additional_kwargs=getattr(accumulated, "additional_kwargs", {}) or {},
            response_metadata=getattr(accumulated, "response_metadata", {}) or {},
            usage_metadata=getattr(accumulated, "usage_metadata", None),
            id=getattr(accumulated, "id", None),
        )

    async def _capture_usage(self, response: AIMessage) -> None:
        """Accumulate token usage + the compaction anchor from a response."""
        usage = getattr(response, "usage_metadata", None)
        if not usage:
            return
        handle = await self._ctx.registry.get(self._ctx.agent_id)
        if handle:
            handle.input_tokens += usage.get("input_tokens", 0)
            handle.output_tokens += usage.get("output_tokens", 0)
        # Authoritative signal for compaction: what the API said this consumed.
        self._last_input_tokens = int(usage.get("input_tokens", 0) or 0)

    async def _invoke_with_resilience(
        self,
        *,
        primary_model: Any,
        messages: list[BaseMessage],
        tool_schemas: Any,
        turns: int,
        invoke_config: dict[str, Any] | None,
        strategy: RetryStrategy,
    ) -> tuple[AIMessage, str]:
        """Drive the resilience strategy for one turn with loop-local I/O.

        The model call, event emission and reactive compaction are injected so
        the strategy stays loop-agnostic and unit-testable. ``messages.append``
        is the caller's job and only happens after this returns, so a retry
        never duplicates a tool call or bloats context with a partial output.
        """

        async def _invoke(model_name: str, is_fallback: bool) -> AIMessage:
            # ``primary_model`` is pre-bound to the ACTIVE model (the configured
            # primary, or — after a sticky escalation — the pinned escalated
            # model, since ``_apply_model_escalation`` rebinds it). Key the reuse
            # off the model NAME, not ``is_fallback``: sticky escalation reorders
            # a pinned rescue model to idx 0 (so ``is_fallback`` is False) —
            # reusing a stale binding there would silently call the wrong model.
            bound = (
                primary_model
                if not is_fallback and model_name == self._active_model
                else build_chat_model(model_name=model_name).bind_tools(tool_schemas)
            )
            return await self._acall_model(bound, messages, config=invoke_config, step=turns)

        async def _compact() -> bool:
            info = await self._compact_messages(messages)
            if not info:
                return False
            await self._ctx.registry.record_compaction(self._ctx.agent_id)
            self._emit_event(
                {
                    "type": "context_compacted",
                    "payload": {
                        **info,
                        "agent_id": self._ctx.agent_id,
                        "depth": self._ctx.depth,
                        "mode": "reactive",
                        "turn": turns,
                    },
                }
            )
            return True

        # Chain head is the ACTIVE model, not the frozen configured one. Once a
        # sticky escalation has promoted ``_active_model``, re-offering
        # ``_ctx.model_name`` puts the model that just died back at the head of
        # every subsequent turn's chain; escalation survived that only because
        # ``RetryStrategy._order_models`` reorders on its own pin, i.e. by
        # accident of a second mechanism rather than by this call being right.
        # Before any escalation the two are identical, so the ordinary path is
        # unchanged.
        response, model_name = await strategy.run(
            models=[self._active_model, *self._ctx.fallback_models],
            invoke=_invoke,
            emit=self._emit_event,
            compact=_compact,
            agent_id=self._ctx.agent_id,
            depth=self._ctx.depth,
            step=turns,
        )
        await self._capture_usage(response)
        return response, model_name

    def _request_model_switch(self, target: str) -> None:
        """Record a deliberate model switch the ``model_control`` tool sanctioned.

        Deferred, never applied inline: the loop promotes the model at the next
        turn boundary through :meth:`_apply_model_escalation`, so the switch
        lands exactly like a sticky fallback (transcript tail untouched) — the
        tool-loop-continuity contract the tool already enforced at request time.
        """
        self._requested_switch = target

    def _has_dangling_tool_use(self) -> bool:
        """True when a PRIOR turn's ``tool_use`` is still unanswered.

        The continuity-lock guardrail for ``model_control``: switching models
        while a tool call sits unpaired would carry the dangling pair into the
        first request on the new model. The in-flight batch (the most recent
        ``AIMessage``'s tool_calls, whose results are being produced right now)
        is excluded — counting it would make every switch look unsafe; only an
        EARLIER unpaired call, the kind a compaction slice can strand, blocks a
        switch.
        """
        messages = self._live_messages or []
        answered = {
            m.tool_call_id for m in messages if isinstance(m, ToolMessage)
        }
        ai_batches = [m for m in messages if isinstance(m, AIMessage) and m.tool_calls]
        for m in ai_batches[:-1]:  # exclude the in-flight (last) batch
            for tc in m.tool_calls:
                tcid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tcid and tcid not in answered:
                    return True
        return False

    def _apply_model_escalation(
        self,
        final_model: str,
        messages: list[BaseMessage],
        *,
        context: ContextSnapshot | None,
        plan: Plan | None,
        agent_tree: str,
        tool_schemas: list[dict[str, Any]],
        model: Any,
    ) -> tuple[list[dict[str, Any]], Any]:
        """Promote the active model to an escalated one and re-derive prompts.

        No-op (returns the inputs unchanged) unless the resilience strategy ended
        the turn on a DIFFERENT model than the one currently active — i.e. a
        sticky escalation down the fallback ladder. On a real switch it:

        - promotes ``self._active_model`` so every subsequent per-step render +
          the edit-tool variant selection resolve the ESCALATED model's overrides;
        - re-renders ``messages[0]`` (the system prompt) in place against it, so
          the next turn carries that model's compatibility-adjusted prompt;
        - recomputes the bound tool schemas (the edit-tool VARIANT can differ
          per model) + rebinds, mirroring the deferred-tool re-bind path.

        The transcript tail is untouched; the change takes effect next turn.
        """
        if not final_model or final_model == self._active_model:
            return tool_schemas, model
        self._active_model = final_model
        # Re-seat the spawn seam on the healed model. An un-overridden child
        # resolves its model from the spawn tool's captured context — the model
        # this loop just escalated AWAY from — so a self-healed parent would
        # otherwise fan its children onto the dead one. The re-seat goes through
        # the tool's own ``rebind_active_model`` seam: that ``AgentContext`` is
        # frozen is the tool's business, not the loop's.
        if self._spawn_agent_tool is not None:
            self._spawn_agent_tool.rebind_active_model(final_model)
        messages[0] = SystemMessage(
            content=self._render_system_prompt(context, plan, agent_tree)
        )
        # This re-render already baked in the current resilience-note slot, so
        # keep the tracker honest — else the top-of-loop refresh re-renders once
        # more for a note that is already present.
        self._active_resilience_note = self._resilience_note.render()
        discovered = self._discovered_from_messages(messages)
        active_specs = self._select_active_specs(self._tool_specs_full, discovered=discovered)
        tool_schemas = self._build_tool_schemas_for_mode(active_specs, self._current_mode)
        model = self._bind_model(tool_schemas)
        self._last_active_ids = {s.tool_id for s in active_specs}
        self._emit_event(
            {
                "type": "llm_prompt_revariant",
                "payload": {
                    "agent_id": self._ctx.agent_id,
                    "depth": self._ctx.depth,
                    "model": final_model,
                    "edit_tool": self._configured_edit_tool_id(),
                },
            }
        )
        return tool_schemas, model

    # ------------------------------------------------------------------
    # Graduated exhaustion — forced wrap-up turns
    # ------------------------------------------------------------------

    async def _unbound_wrapup_invoke(
        self,
        *,
        prompt_key: str,
        model_name: str,
        messages: list[BaseMessage],
        tool_outputs: list[str],
        invoke_config: dict[str, Any],
    ) -> str:
        """One text-only LLM call with tools UNBOUND — never raises.

        Falls back to recent tool output on failure. Shared by the
        (currently unreachable, defensive) final-synthesis safety net and
        :meth:`_budget_wrapup_turn`; the only difference between callers is
        which directive gets injected and which model answers it.
        """
        final_response = ""
        try:
            messages.append(SystemMessage(content=get_prompt_registry().render(prompt_key)))
            unbound = build_chat_model(model_name=model_name)
            synthesis_timeout = float(get_config_value("agent", "llm_call_timeout", default=60.0))
            synthesis: AIMessage = await asyncio.wait_for(
                unbound.ainvoke(messages, config=invoke_config or None),
                timeout=synthesis_timeout,
            )
            _usage = getattr(synthesis, "usage_metadata", None)
            if _usage:
                _h = await self._ctx.registry.get(self._ctx.agent_id)
                if _h:
                    _h.input_tokens += _usage.get("input_tokens", 0)
                    _h.output_tokens += _usage.get("output_tokens", 0)
            final_response = self._extract_text_content(getattr(synthesis, "content", ""))
        except Exception:
            logging.warning("Unbound wrap-up turn failed.", exc_info=True)

        # Fallback: if the call produced nothing, build a minimal response
        # from successful tool outputs so the caller isn't left empty-handed.
        if not final_response or not final_response.strip():
            successful = [o for o in tool_outputs if o and not o.startswith("ERROR")]
            if successful:
                final_response = "Partial results:\n\n" + "\n\n".join(successful[-5:])
        return final_response

    async def _budget_wrapup_turn(
        self,
        reason: str,
        *,
        state: OrchestrationState,
        messages: list[BaseMessage],
        tool_outputs: list[str],
        invoke_config: dict[str, Any],
    ) -> str:
        """Force one text-only wrap-up call in place of a bare halt.

        Reusable across every graduated-exhaustion caller: this wave's session
        step budget calls it with ``reason="budget_exhausted"``; a future wave
        routes per-agent contract exhaustion through the SAME helper with a
        different reason (e.g. ``"halted_agent_budget"``). Runs against
        ``self._active_model`` (the escalated model if a sticky switch pinned one — unlike
        the legacy safety net, which intentionally keeps its pre-existing
        ``_ctx.model_name`` wart). Deliberately skips
        ``registry.update_step`` — this closing call must not re-trip the very
        budget that triggered it. Sets ``state.done``/``state.done_reason``;
        the caller's ``break`` is the only remaining step, and the (already
        done-guarded) final-synthesis safety net is skipped as a result.
        """
        final_response = await self._unbound_wrapup_invoke(
            prompt_key="loop.budget_exhausted_wrapup",
            model_name=self._active_model,
            messages=messages,
            tool_outputs=tool_outputs,
            invoke_config=invoke_config,
        )
        state.done = True
        state.done_reason = reason
        return final_response

    async def _run_verifier(self, *, step: int, attempt: int) -> VerifierOutcome:
        """Run the ground-truth completion verifier once and emit its verdict.

        The model does no I/O: the injected runner performs the subprocess
        (never raising — a timeout/OS error comes back as a failed result), the
        spec interprets that raw result into a pass/fail ``VerifierOutcome``, and
        this method emits the BOUNDED telemetry (``verification`` event, scalars
        only — the grounded stdout/stderr rides only the outcome's feedback into
        the model's own context, never onto the wire). Guarded by
        ``self._verification_active`` at the sole call site, so the spec is
        non-None here.
        """
        assert self._verification is not None  # _verification_active guards this
        result = await self._verifier_runner.run(self._verification, cwd=self._cwd)
        outcome = self._verification.interpret(result)
        payload: VerificationPayload = {
            "agent_id": self._ctx.agent_id,
            "depth": self._ctx.depth,
            "step": step,
            "passed": outcome.passed,
            "attempt": attempt,
            "exit_code": outcome.exit_code,
            "timed_out": outcome.timed_out,
        }
        self._emit_event({"type": "verification", "payload": payload})
        return outcome

    # ------------------------------------------------------------------
    # Mid-loop context compaction
    # ------------------------------------------------------------------

    def _should_compact_messages(self, messages: list[BaseMessage]) -> bool:
        """Token-reality check anchored on the last response's usage_metadata.

        ``messages`` is intentionally unused — the LLM's ``usage_metadata``
        already reflects everything it saw (system prompt, tool schemas,
        all messages). No char-count estimation, no manual overhead.
        """
        del messages  # Signature preserved for callers; data comes from the API.
        if self._last_input_tokens <= 0:
            return False  # No LLM call yet; nothing authoritative to check.
        from mewbo_core.token_budget import get_model_max_input_tokens

        # Size the window against the ACTIVE model. Reading the frozen
        # configured one keeps a large window after escalating DOWN to a small
        # model, so the threshold sits above that model's real ceiling,
        # auto-compaction never fires, and the run dies on
        # ContextWindowExceededError instead of compacting.
        max_input = get_model_max_input_tokens(self._active_model)
        threshold = float(get_config_value("token_budget", "auto_compact_threshold", default=0.8))
        return self._last_input_tokens >= max_input * threshold

    async def _compact_messages(
        self,
        messages: list[BaseMessage],
    ) -> dict[str, Any] | None:
        """Compact the in-flight message list by summarizing older messages.

        Keeps messages[0] (system prompt) and the last ``recent_keep``
        messages, summarizing everything in between via the structured
        compaction prompt.  Returns a dict with ``summary`` and
        ``events_summarized`` on success, or ``None`` if skipped.
        """
        recent_keep = 6  # ~3 turn pairs (AI + Tool)
        if len(messages) <= recent_keep + 2:
            return None  # Not enough to compact

        to_summarize = messages[1:-recent_keep]
        kept_tail = messages[-recent_keep:]

        # Build text representation for the summarizer.
        lines: list[str] = []
        for m in to_summarize:
            role = getattr(m, "type", "unknown")
            text = m.content if isinstance(m.content, str) else str(m.content)
            lines.append(f"[{role}] {text[:2000]}")
        summary_input = "\n".join(lines)

        # If root agent, include agent tree so delegation context survives.
        if self._ctx.depth == 0:
            tree = await self._ctx.registry.render_agent_tree(
                exclude_agent_id=self._ctx.agent_id,
            )
            if tree:
                summary_input += f"\n\n# Active agent tree at compaction:\n{tree}"

        # Invoke the compaction LLM with priority-ordered model fallback.
        from mewbo_core.compact import (
            _extract_summary,
            get_compact_prompt,
            resolve_compact_models,
        )

        _compact_models = resolve_compact_models(self._active_model)
        _compact_model = _compact_models[0]
        _msgs = [
            SystemMessage(content=get_compact_prompt(model=_compact_model)),
            HumanMessage(
                content=get_prompt_registry().render(
                    "loop.compaction_drive", summary_input=summary_input
                )
            ),
        ]
        response = None
        for _i, _candidate in enumerate(_compact_models):
            _compact_model = _candidate
            try:
                llm = build_chat_model(model_name=_compact_model)
                response = await llm.ainvoke(_msgs)
                break
            except Exception:
                if _i < len(_compact_models) - 1:
                    logging.warning(
                        "Mid-loop compact model %s failed, trying next: %s",
                        _compact_model,
                        _compact_models[_i + 1],
                        exc_info=True,
                    )
                else:
                    logging.warning("Mid-loop compaction LLM call failed", exc_info=True)
                    return None

        if response is None:
            return None  # all models failed

        # Capture compaction LLM tokens on the agent handle.
        _usage = getattr(response, "usage_metadata", None)
        if _usage:
            _h = await self._ctx.registry.get(self._ctx.agent_id)
            if _h:
                _h.input_tokens += _usage.get("input_tokens", 0)
                _h.output_tokens += _usage.get("output_tokens", 0)

        raw = response.content if hasattr(response, "content") else str(response)
        if isinstance(raw, list):
            raw = next(
                (b["text"] for b in raw if isinstance(b, dict) and b.get("type") == "text"),
                "",
            )
        summary = _extract_summary(raw)
        events_summarized = len(to_summarize)

        # Rebuild messages in-place.
        system_msg = messages[0]
        messages.clear()
        messages.append(system_msg)
        messages.append(
            SystemMessage(
                content=get_prompt_registry().render("loop.compacted_marker", summary=summary)
            )
        )
        messages.extend(kept_tail)
        # The recent-tail slice can orphan a tool_use/tool_result pair, which
        # Anthropic rejects with a 400. Rebalance before the list is replayed.
        repair_tool_pairing(messages)
        return {
            "summary": summary,
            "events_summarized": events_summarized,
            "model": _compact_model,
        }

    def _is_tool_search_enabled(self, tool_specs: list[ToolSpec] | None = None) -> bool:
        """Return True if the deferred-tool / on-demand-schema feature is on.

        Read fresh from config so the field can be flipped without a
        process restart. Sub-orchestrators inherit by reading the same
        ``agent.tool_search.mode`` value.

        - ``off`` → never defer.
        - ``on`` → always defer.
        - ``auto`` → defer only when the number of deferrable specs in
          ``tool_specs`` exceeds ``agent.tool_search.auto_threshold``, so a
          lean / zero-MCP session keeps verbatim binding and pays nothing.
          When called without ``tool_specs`` (no set to measure) ``auto``
          conservatively stays off.
        """
        mode = str(get_config_value("agent", "tool_search", "mode", default="off")).lower()
        if mode == "on":
            return True
        if mode == "auto":
            if not tool_specs:
                return False
            raw_threshold = get_config_value("agent", "tool_search", "auto_threshold", default=25)
            try:
                threshold = int(raw_threshold)
            except (TypeError, ValueError):
                threshold = 25
            deferrable = sum(1 for s in tool_specs if is_deferred(s))
            return deferrable > threshold
        return False

    def _select_active_specs(
        self,
        specs: list[ToolSpec],
        *,
        discovered: set[str],
    ) -> list[ToolSpec]:
        """Return the spec subset to bind on the model this turn.

        ``non_deferred ∪ {tool_search} ∪ (deferred ∩ discovered)``. When
        deferred-loading is off, returns ``specs`` unchanged. The same
        function drives both the run-start bind and the per-turn re-bind
        so there is exactly one source of truth for what is bound.

        ``tool_search`` reaches here having been EXEMPTED from the allowlist by
        ``filter_specs`` (it is ``always_load``). That exemption is load-bearing
        and stays: strip it while tools are deferred and a scoped sub-agent
        loses its MCP schemas AND the only means to fetch them. It is also
        justified ONLY while something is deferred — with nothing to fetch, the
        tool is pure surface area, and it was among the top repeat callees in
        the runs that burned a session's whole budget without answering. So a
        STRICT scope that did not name it caps it in exactly that case.
        """
        deferral_active = self._tool_search_enabled and bool(self._deferred_ids)
        if not deferral_active and not self._loop_injected_admitted(TOOL_SEARCH_TOOL_ID):
            specs = [s for s in specs if s.tool_id != TOOL_SEARCH_TOOL_ID]
        if not deferral_active:
            return list(specs)
        keep: list[ToolSpec] = []
        for spec in specs:
            if spec.tool_id in self._deferred_ids and spec.tool_id not in discovered:
                continue
            keep.append(spec)
        return keep

    # Match each ``<function>{...}</function>`` line emitted by ToolSearchRunner.
    # The runner serialises ``{"name": ..., "description": ..., "parameters": {...}}``
    # with ``json.dumps`` so ``"name"`` is always the first field; anchoring there
    # avoids brace-counting through the nested ``parameters`` schema.
    _DISCOVERED_FUNC_RE = re.compile(r'<function>\s*\{\s*"name"\s*:\s*"([^"]+)"')

    def _discovered_from_messages(self, messages: list[BaseMessage]) -> set[str]:
        """Scan message history for tool names exposed by past tool_search calls.

        Discovery is derived from messages — no separate state — so the
        set survives compaction unchanged: whatever messages remain after
        compaction still parse the same way. Only ``ToolMessage`` content
        is scanned; the regex matches the ``<function>...</function>``
        line format produced by ``ToolSearchRunner``.
        """
        if not self._deferred_ids:
            return set()
        names: set[str] = set()
        for msg in messages:
            if not isinstance(msg, ToolMessage):
                continue
            content = msg.content
            if isinstance(content, str):
                for match in self._DISCOVERED_FUNC_RE.finditer(content):
                    name = match.group(1)
                    if name in self._deferred_ids:
                        names.add(name)
        return names

    def _render_deferred_tool_block(self) -> str:
        """Render a compact ``<available-mcp-servers>`` system-prompt section.

        Lists MCP servers with their tool counts (one line) plus any
        non-MCP deferred tool ids. Server names — not full tool ids — keep
        the prompt tight even when many MCP tools are connected; the model
        searches by keyword (``tool_search`` + server name or capability)
        rather than scanning a long flat list.
        """
        if not getattr(self, "_tool_search_enabled", False):
            return ""
        deferred_ids: set[str] = getattr(self, "_deferred_ids", set())
        if not deferred_ids:
            return ""
        deferred_specs = [s for s in self._tool_specs_full if s.tool_id in deferred_ids]

        servers: dict[str, int] = {}
        other: list[str] = []
        for spec in deferred_specs:
            if spec.kind == "mcp":
                server = str(spec.metadata.get("server") or "unknown")
                servers[server] = servers.get(server, 0) + 1
            else:
                other.append(spec.tool_id)

        parts: list[str] = []
        if servers:
            summary = ", ".join(f"{name} ({n})" for name, n in sorted(servers.items()))
            parts.append(f"<available-mcp-servers>{summary}</available-mcp-servers>")
        if other:
            parts.append(f"Other deferred tools: {', '.join(sorted(other))}.")
        parts.append(get_prompt_registry().render("loop.section.deferred_tools"))
        return "\n".join(parts)

    def _build_tool_schemas_for_mode(
        self,
        specs: list[ToolSpec],
        mode: str,
    ) -> list[dict[str, Any]]:
        """Return langchain tool schemas appropriate for ``mode``.

        In plan mode the schema is filtered to: read-only tools + the
        configured edit tool (path-scoped at the permission layer) + the
        shell tool (command-allowlisted at the permission layer, iff the
        allowlist is non-empty) + all MCP tools. MCP specs carry no
        read-only signal (the wire protocol exposes no ``readOnlyHint``),
        so Mewbo cannot classify a third-party MCP tool's effect — a mode
        filter over user-land tools would be guesswork, not gating. In act
        mode all specs pass through.
        """
        if mode != "plan":
            return specs_to_langchain_tools(specs)
        edit_tool_id = self._configured_edit_tool_id()
        shell_enabled = bool(self._plan_mode_shell_allowlist())
        filtered = [
            spec
            for spec in specs
            if spec.read_only
            or spec.kind == "mcp"
            or (spec.tool_id == edit_tool_id and self._ctx.depth > 0)
            or (shell_enabled and spec.tool_id in SHELL_TOOL_IDS)
        ]
        return specs_to_langchain_tools(filtered)

    def _directly_bound_tool_schemas(self, *, plan_mode: bool) -> list[dict[str, Any]]:
        """The schemas the loop binds DIRECTLY, outside the ``ToolRegistry``.

        Three populations ``_bind_model`` appends beyond the registry specs: the
        spawn family (gated on ``self._spawn_agent_tool`` + the plan-mode rule),
        ``activate_skill`` (when auto-invocable skills exist and skills are on),
        and the per-agent SESSION TOOLS (mode-filtered). This is the ONE source
        of truth, shared by ``_bind_model`` (what gets BOUND) and the
        ``tool_search`` supplement (what search can FIND) — so the searchable set
        can never drift from the bound set, and a strictly-scoped agent can never
        widen its surface via search.
        """
        extra: list[dict[str, Any]] = []
        # In plan mode, the root (depth=0) gets agent management tools so it
        # can spawn and monitor the plan sub-agent. Non-root plan agents get
        # no agent tools — they explore and draft only.
        plan_root = plan_mode and self._ctx.depth == 0
        if (not plan_mode or plan_root) and self._spawn_agent_tool is not None:
            from mewbo_core.spawn_agent import SPAWN_AGENT_SCHEMA, SPAWN_AGENTS_SCHEMA

            extra.extend([SPAWN_AGENT_SCHEMA, SPAWN_AGENTS_SCHEMA])
            # Ref: [DeepMind-Delegation §4.4] Root-only management tools
            # for non-blocking agent monitoring and steering. Each is ceiling-
            # checked on its OWN id: a delegating AgentDef that means to monitor
            # names ``check_agents`` (every strict def in the tree does), and one
            # that never steers should not be handed ``steer_agent`` merely for
            # having named a spawn tool.
            if self._ctx.depth == 0:
                from mewbo_core.spawn_agent import (
                    CHECK_AGENTS_SCHEMA,
                    STEER_AGENT_SCHEMA,
                )

                extra.extend(
                    schema
                    for tool_id, schema in (
                        ("check_agents", CHECK_AGENTS_SCHEMA),
                        ("steer_agent", STEER_AGENT_SCHEMA),
                    )
                    if self._loop_injected_admitted(tool_id)
                )
        # Inject activate_skill schema when auto-invocable skills exist — unless
        # the drive opted out (``enable_skills=False``), so a headless product
        # run never burns a step activating a host ``~/.claude`` skill.
        if (
            self._enable_skills
            and self._loop_injected_admitted("activate_skill")
            and not plan_mode
            and self._skill_registry is not None
            and self._skill_registry.list_auto_invocable(self._session_capabilities)
        ):
            from mewbo_core.skills import ACTIVATE_SKILL_SCHEMA

            extra.append(ACTIVATE_SKILL_SCHEMA)
        # Session-tool schemas only for tools whose ``modes`` include the current
        # orchestration mode. Data-driven — no tool_id string match. Plugin tools
        # missing the attribute default to act-mode.
        current_mode = "plan" if plan_mode else "act"
        for session_tool in self._session_tools:
            tool_modes = getattr(session_tool, "modes", None) or DEFAULT_SESSION_TOOL_MODES
            if current_mode in tool_modes:
                extra.append(session_tool.schema)
        return extra

    def _bind_model(self, tool_schemas: list[dict[str, Any]]) -> Any:
        """Build a chat model (for the ACTIVE model) and bind tool schemas."""
        model = build_chat_model(model_name=self._active_model)
        plan_mode = self._current_mode == "plan"
        tool_schemas = [
            *tool_schemas,
            *self._directly_bound_tool_schemas(plan_mode=plan_mode),
        ]
        if tool_schemas:
            return model.bind_tools(tool_schemas)
        return model

    # ------------------------------------------------------------------
    # Tool execution (async)
    # ------------------------------------------------------------------

    async def _execute_tool_call(
        self,
        tool_call: Any,
        tool_specs: list[ToolSpec],
    ) -> ToolCallResult:
        """Execute a single LLM tool_call: permission → hooks → run → emit event."""
        tool_call_id: str = tool_call.get("id") or ""
        tool_id: str = tool_call.get("name") or ""

        action_step = self._tool_call_to_action_step(tool_call)

        # A session tool that RETURNS a structured-error envelope (wiki/scg
        # ``err_result``) is a handled failure, not a raise — captured here and
        # applied at the common emit/return path so the step records
        # ``success=False`` and the loop's failure nudge fires (the model still
        # gets the envelope text). ``None`` for every normal result.
        session_tool_error: _SessionToolError | None = None

        # MCP input coercion.
        spec = self._tool_registry.get_spec(tool_id)
        if spec is not None:
            coercion_error = _coerce_mcp_tool_input(action_step, spec)
            if coercion_error:
                self._emit_tool_result_event(action_step, None, error=coercion_error)
                return ToolCallResult(
                    tool_call_id=tool_call_id,
                    tool_id=tool_id,
                    content=f"ERROR: {coercion_error}",
                    success=False,
                )

        # Permission check.
        if not self._check_permission(action_step):
            # If the permission branch wrote a detailed error to
            # ``action_step.result`` (e.g., plan-mode path scoping), use
            # that as the tool-result content so the model can self-correct.
            denial_content = getattr(action_step.result, "content", None) or "Permission denied"
            self._emit_tool_result_event(action_step, None, error=str(denial_content))
            return ToolCallResult(
                tool_call_id=tool_call_id,
                tool_id=tool_id,
                content=str(denial_content),
                success=False,
            )

        # Pre-tool hook.
        action_step = self._hook_manager.run_pre_tool_use(action_step)

        # File read dedup: return a stub if this file was already read
        # with the same params and hasn't changed on disk.
        if tool_id == "read_file":
            dedup_stub = self._check_file_read_cache(action_step)
            if dedup_stub is not None:
                self._emit_tool_result_event(action_step, dedup_stub)
                return ToolCallResult(
                    tool_call_id=tool_call_id,
                    tool_id=tool_id,
                    content=dedup_stub,
                    success=True,
                )

        # Execute — internal tools (spawn_agent, session tools, activate_skill)
        # first, then the registry.
        session_tool = next(
            (t for t in self._session_tools if t.tool_id == tool_id), None
        )
        if tool_id == "spawn_agent" and self._spawn_agent_tool is not None:
            try:
                result = await self._spawn_agent_tool.run_async(action_step)
            except Exception as exc:
                logging.error("spawn_agent failed: {}", exc)
                self._emit_tool_result_event(action_step, None, error=str(exc))
                return ToolCallResult(
                    tool_call_id=tool_call_id,
                    tool_id=tool_id,
                    content=f"ERROR: {exc}",
                    success=False,
                )
        elif tool_id == "spawn_agents" and self._spawn_agent_tool is not None:
            try:
                result = await self._spawn_agent_tool.run_batch_async(action_step)
            except Exception as exc:
                logging.error("spawn_agents failed: {}", exc)
                self._emit_tool_result_event(action_step, None, error=str(exc))
                return ToolCallResult(
                    tool_call_id=tool_call_id,
                    tool_id=tool_id,
                    content=f"ERROR: {exc}",
                    success=False,
                )
        elif tool_id == "check_agents" and self._spawn_agent_tool is not None:
            try:
                result = await self._spawn_agent_tool.handle_check_agents(action_step)
            except Exception as exc:
                logging.error("check_agents failed: {}", exc)
                self._emit_tool_result_event(action_step, None, error=str(exc))
                return ToolCallResult(
                    tool_call_id=tool_call_id,
                    tool_id=tool_id,
                    content=f"ERROR: {exc}",
                    success=False,
                )
        elif tool_id == "steer_agent" and self._spawn_agent_tool is not None:
            try:
                result = await self._spawn_agent_tool.handle_steer_agent(action_step)
            except Exception as exc:
                logging.error("steer_agent failed: {}", exc)
                self._emit_tool_result_event(action_step, None, error=str(exc))
                return ToolCallResult(
                    tool_call_id=tool_call_id,
                    tool_id=tool_id,
                    content=f"ERROR: {exc}",
                    success=False,
                )
        elif session_tool is not None:
            try:
                result = await session_tool.handle(action_step)
            except Exception as exc:
                logging.error("session tool {} failed: {}", tool_id, exc)
                self._emit_tool_result_event(action_step, None, error=str(exc))
                return ToolCallResult(
                    tool_call_id=tool_call_id,
                    tool_id=tool_id,
                    content=f"ERROR: {exc}",
                    success=False,
                )
            # A handled error envelope (returned, not raised) → reclassify as a
            # FAILED step while keeping the envelope text as the model output.
            session_tool_error = _SessionToolError.parse(result)
        elif (
            tool_id == TOOL_SEARCH_TOOL_ID
            and (tool_search_runner := self._tool_registry.get(tool_id)) is not None
        ):
            # tool_search searches the ToolRegistry, but the loop ALSO binds
            # spawn-family / activate_skill / session-tool schemas directly —
            # invisible to the registry and so historically unsearchable.
            # Hand the runner the SAME directly-bound schemas as this turn's
            # bind so those tools are findable; by construction search can only
            # surface what is already bound, never widen a scoped agent's scope.
            supplement = self._directly_bound_tool_schemas(
                plan_mode=self._current_mode == "plan"
            )
            try:
                result = await asyncio.to_thread(
                    tool_search_runner.run, action_step, supplement=supplement
                )
            except Exception as exc:
                logging.error("tool_search failed: {}", exc)
                self._emit_tool_result_event(action_step, None, error=str(exc))
                return ToolCallResult(
                    tool_call_id=tool_call_id,
                    tool_id=tool_id,
                    content=f"ERROR: {exc}",
                    success=False,
                )
        elif tool_id == "activate_skill" and self._skill_registry is not None:
            result = self._handle_activate_skill(action_step)
        else:
            tool = self._tool_registry.get(tool_id)
            if tool is None:
                self._emit_tool_result_event(action_step, None, error="Tool not available")
                return ToolCallResult(
                    tool_call_id=tool_call_id,
                    tool_id=tool_id,
                    content="ERROR: Tool not available",
                    success=False,
                )
            try:
                # Prefer async execution for tools that support it (MCP tools).
                # Falls back to to_thread for sync-only tools (aider_*, etc.).
                #
                # Publish the active containment for the duration of
                # THIS tool's run so ``resolve_safe_path`` (called deep inside the
                # aider file/edit/shell tools, and the LSP tool) enforces the
                # firebreak without the loop threading a live object through JSON
                # args. A no-op when ``self._containment`` is None/inactive, so a
                # full_access / enforcement-off run pays nothing. ``contextvars``
                # propagate into ``asyncio.to_thread``, so the sync-tool thread
                # sees the same active containment as this awaiting frame.
                with active_containment(self._containment):
                    if hasattr(tool, "arun"):
                        result = await tool.arun(action_step)
                    else:
                        result = await asyncio.to_thread(tool.run, action_step)
            except Exception as exc:
                logging.error("Tool execution failed: {}", exc)
                self._emit_tool_result_event(action_step, None, error=str(exc))
                return ToolCallResult(
                    tool_call_id=tool_call_id,
                    tool_id=tool_id,
                    content=f"ERROR: {exc}",
                    success=False,
                )

        # Post-tool hook.
        result = self._hook_manager.run_post_tool_use(action_step, result)

        content = getattr(result, "content", None)
        if content is None:
            content = "" if result is None else str(result)
        content_str = str(content) if not isinstance(content, str) else content
        max_chars = self._result_char_cap(tool_id)
        if isinstance(content, dict):
            # Truncate large text fields inside the dict before serializing,
            # so the JSON envelope (metadata like exit_code, duration_ms) is
            # always preserved even when stdout/stderr are huge.
            if max_chars:
                truncated = dict(content)
                for field in ("stdout", "stderr", "output", "result", "content", "text"):
                    val = truncated.get(field)
                    if isinstance(val, str) and len(val) > max_chars:
                        truncated[field] = val[:max_chars] + "\n[truncated]"
                content = truncated
            content_str = json.dumps(content, ensure_ascii=False, default=str)

        # Micro-compaction: strip ANSI escapes.
        content_str = _ANSI_ESCAPE_RE.sub("", content_str)

        # Snapshot for the event — preserves the JSON envelope so the
        # frontend can always parse structured fields (exit_code, duration_ms).
        # Use a generous event-side cap (decoupled from `max_chars`, which is
        # the LLM-context cap): the frontend can scroll the full output, and
        # `result_file` still backstops pathological results when an export dir
        # is configured. Without this, MCP tool responses get truncated to
        # 2000 chars in the UI even though the full content exists in memory.
        event_str = content_str
        # Floor the event cap at the model-facing cap. Without that, a tool
        # whose declared cap exceeds the event cap would record LESS than the
        # model read — inverting the very fidelity the paired
        # ``result``/``result_seen`` keys exist to establish.
        event_max_chars = max(_EVENT_SNAPSHOT_MAX_CHARS, max_chars)
        if len(event_str) > event_max_chars:
            event_str = event_str[:event_max_chars] + "\n[truncated — see result_file]"

        # Save large results to file when export dir is configured.
        result_file: str | None = None
        export_dir = str(get_config_value("runtime", "result_export_dir", default="") or "")
        if export_dir and max_chars and len(content_str) > max_chars:
            try:
                export_path = Path(export_dir)
                export_path.mkdir(parents=True, exist_ok=True)
                fid = tool_call_id or f"{tool_id}-{int(_time.time() * 1000)}"
                safe_id = re.sub(r"[^\w\-]", "_", fid)
                result_path = export_path / f"{safe_id}.txt"
                result_path.write_text(content_str, encoding="utf-8")
                result_file = str(result_path)
                content_str = (
                    f"[Full output ({len(content_str)} chars) saved to {result_file}. "
                    f"Read the file for complete content.]"
                )
            except OSError:
                pass  # Fall through to normal truncation

        # Final truncation for the LLM (safety net).
        if max_chars and len(content_str) > max_chars:
            content_str = content_str[:max_chars] + "\n[truncated]"

        # Populate file read cache after successful read.
        if tool_id == "read_file":
            self._populate_file_read_cache(action_step)

        # Invalidate file read cache when a file is edited.
        if tool_id in ("file_edit_tool", "aider_edit_block_tool"):
            edit_args = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
            edited_path = str(edit_args.get("file_path", "") or edit_args.get("path", ""))
            if edited_path:
                norm = os.path.normpath(edited_path)
                self._file_read_cache.pop(norm, None)
                # Passive LSP diagnostics: surface errors after edits.
                content_str = _append_lsp_feedback(
                    content_str,
                    norm,
                    self._cwd or "",
                )

        # A session tool's handled error envelope records as a FAILED step (so
        # the loop's per-step failure nudge fires) while the model still receives
        # the full envelope JSON as the tool output — error surfacing without
        # hiding the structured detail the tool chose to return.
        if session_tool_error is not None:
            self._emit_tool_result_event(
                action_step,
                event_str,
                error=session_tool_error.summary,
                result_file=result_file,
                seen=content_str,
                permanence=session_tool_error.permanence,
            )
            return ToolCallResult(
                tool_call_id=tool_call_id,
                tool_id=tool_id,
                content=content_str,
                success=False,
                blocked_code=(
                    session_tool_error.code
                    if session_tool_error.blocks_completion
                    else None
                ),
                permanence=session_tool_error.permanence,
            )

        self._emit_tool_result_event(
            action_step, event_str, result_file=result_file, seen=content_str
        )
        return ToolCallResult(
            tool_call_id=tool_call_id,
            tool_id=tool_id,
            content=content_str,
            success=True,
        )

    # ------------------------------------------------------------------
    # ActionStep construction
    # ------------------------------------------------------------------

    def _tool_call_to_action_step(self, tool_call: Any) -> ActionStep:
        """Convert an LLM tool_call dict to an ActionStep."""
        tool_id: str = tool_call.get("name") or ""
        args: Any = tool_call.get("args") or {}
        # Inject session cwd as `root` for registered local tools (aider-style
        # file/shell tools consume it via `argument.get("root")`). Unregistered
        # tools — session tools, spawn_agent, activate_skill — use strict
        # schemas that reject stray keys, so they opt out by not being here.
        #
        # Two regimes for the `root` injection:
        #   * ACTIVE containment (`self._containment is not None`): the workspace
        #     root is AUTHORITATIVE — always forced, OVERRIDING any model-supplied
        #     `root`, so a model cannot widen its way out of the jail by naming a
        #     sibling root. (`resolve_safe_path` then also collapses the tenant
        #     union to the containment's allowed roots via the active-containment
        #     context set around execution below.)
        #   * No active containment (flag off / full_access / no cwd): today's
        #     ADVISORY behaviour, byte-identical — inject only when the model did
        #     not supply its own `root`.
        if isinstance(args, dict):
            spec = self._tool_registry.get_spec(tool_id)
            if spec is not None and spec.kind != "mcp":
                if self._containment is not None:
                    args = {**args, "root": self._containment.root}
                elif self._cwd and "root" not in args:
                    args = {**args, "root": self._cwd}
        operation = _infer_operation(tool_id)
        return ActionStep(
            title=tool_id,
            tool_id=tool_id,
            operation=operation,
            tool_input=args,
        )

    # ------------------------------------------------------------------
    # File-read dedup cache
    # ------------------------------------------------------------------

    def _check_file_read_cache(self, action_step: ActionStep) -> str | None:
        """Return a stub if this file was already read with the same params, else ``None``."""
        args = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        path = str(args.get("path", ""))
        if not path:
            return None
        root = str(args.get("root") or "")
        offset = int(args.get("offset", 0) or 0)
        limit = args.get("limit")
        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                limit = None

        try:
            joined = os.path.join(root, path) if root else path
            full_path = os.path.normpath(joined)
        except (TypeError, ValueError):
            return None

        cached = self._file_read_cache.get(full_path)
        if cached is None:
            return None
        if cached.offset != offset or cached.limit != limit:
            return None

        # Check mtime — file may have been edited externally.
        try:
            current_mtime = os.path.getmtime(full_path)
        except OSError:
            return None
        if current_mtime != cached.mtime:
            del self._file_read_cache[full_path]
            return None

        return (
            "File unchanged since last read. The content from the earlier "
            "Read tool_result in this conversation is still current — "
            "refer to that instead of re-reading."
        )

    def _populate_file_read_cache(self, action_step: ActionStep) -> None:
        """Record a successful file read for future dedup."""
        args = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        path = str(args.get("path", ""))
        if not path:
            return
        root = str(args.get("root") or "")
        offset = int(args.get("offset", 0) or 0)
        limit = args.get("limit")
        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                limit = None
        try:
            joined = os.path.join(root, path) if root else path
            full_path = os.path.normpath(joined)
            mtime = os.path.getmtime(full_path)
        except (TypeError, ValueError, OSError):
            return
        self._file_read_cache[full_path] = _CachedFileRead(
            path=full_path,
            offset=offset,
            limit=limit,
            mtime=mtime,
        )

    # ------------------------------------------------------------------
    # Permission
    # ------------------------------------------------------------------

    def _check_permission(self, action_step: ActionStep) -> bool:
        """Check permission for an action step. Returns True if allowed."""
        # Plan-mode gating is authoritative: read-only tools, the scoped
        # edit tool, and exit_plan_mode are allowed; everything else is
        # denied. The normal approval policy is bypassed so that plan-mode
        # exploration does not get blocked by ASK rules.
        if self._current_mode == "plan":
            return self._plan_mode_permission(action_step)

        decision = self._permission_policy.decide(action_step)
        decision = self._hook_manager.run_permission_request(action_step, decision)
        if decision == PermissionDecision.ASK:
            approved = self._approval_callback(action_step) if self._approval_callback else False
            decision = PermissionDecision.ALLOW if approved else PermissionDecision.DENY
            self._emit_event(
                {
                    "type": "permission",
                    "payload": {
                        "tool_id": action_step.tool_id,
                        "operation": action_step.operation,
                        "tool_input": action_step.tool_input,
                        "decision": decision.value,
                    },
                }
            )
        if decision == PermissionDecision.DENY:
            mock = get_mock_speaker()
            action_step.result = mock(content=f"Permission denied for {action_step.tool_id}.")
            return False
        return True

    def _plan_mode_permission(self, action_step: ActionStep) -> bool:
        """Plan-mode permission branch: allow read-only + path-scoped edits.

        Returns True if the action is allowed in plan mode (and normal
        policy checks should also run), or False to deny outright. The
        denial path sets an actionable error on ``action_step.result`` and
        emits a permission event so the model and user see the refusal.
        """
        tool_id = action_step.tool_id
        # Always allow internal tools that signal loop termination.
        if tool_id == "exit_plan_mode":
            return True
        # Root (depth=0) can use agent management tools to spawn and
        # monitor the plan sub-agent.  Non-root plan agents cannot.
        _AGENT_MGMT_TOOLS = {"spawn_agent", "spawn_agents", "check_agents", "steer_agent"}
        if tool_id in _AGENT_MGMT_TOOLS and self._ctx.depth == 0:
            return True
        spec = self._tool_registry.get_spec(tool_id)
        # Read-only tools are unrestricted in plan mode.
        if spec is not None and spec.read_only:
            return True
        # The configured edit tool is allowed ONLY when its target path
        # resolves inside the session's plan directory.
        edit_tool_id = self._configured_edit_tool_id()
        if tool_id == edit_tool_id and self._session_id is not None:
            args = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
            candidate = str(args.get("file_path", "") or "")
            if candidate and is_inside_plan_dir(candidate, self._session_id):
                return True
            attempted = candidate or "<missing file_path>"
            plan_path = plan_file_for(self._session_id)
            msg = get_prompt_registry().render(
                "loop.plan_edit_restricted", plan_path=plan_path, attempted=attempted
            )
            mock = get_mock_speaker()
            action_step.result = mock(content=msg)
            self._emit_event(
                {
                    "type": "permission",
                    "payload": {
                        "tool_id": tool_id,
                        "operation": action_step.operation,
                        "tool_input": action_step.tool_input,
                        "decision": "deny",
                    },
                }
            )
            return False
        # Shell tool: permitted only when the command matches an allowlisted
        # prefix AND contains no shell metacharacters. The denial message
        # includes the allowlist so the model can self-correct in one turn.
        shell_allowlist = self._plan_mode_shell_allowlist()
        if tool_id in SHELL_TOOL_IDS and shell_allowlist:
            args = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
            command = str(args.get("command", "") or "").strip()
            if is_shell_command_plan_safe(command, shell_allowlist):
                return True
            allowed_preview = ", ".join(shell_allowlist)
            attempted = command or "<missing command>"
            plan_hint = ""
            if self._session_id is not None:
                if self._ctx.depth == 0:
                    plan_hint = (
                        " You cannot write the plan directly. Spawn a sub-agent to draft it."
                    )
                else:
                    plan_hint = (
                        f" To write the plan, use your edit tool on "
                        f"{plan_file_for(self._session_id)}."
                    )
            msg = (
                f"Plan mode: shell command blocked. You attempted: `{attempted}`. "
                f"Allowed prefixes: {allowed_preview}. No pipes, redirects, "
                f"`&&`/`;`, `$VAR` expansion, or backticks.{plan_hint}"
            )
            mock = get_mock_speaker()
            action_step.result = mock(content=msg)
            self._emit_event(
                {
                    "type": "permission",
                    "payload": {
                        "tool_id": tool_id,
                        "operation": action_step.operation,
                        "tool_input": action_step.tool_input,
                        "decision": "deny",
                    },
                }
            )
            return False
        # User-enabled MCP tools: unconditionally allowed. Mewbo cannot
        # classify a third-party MCP tool's effect, so plan mode trusts the
        # user's mcp.json rather than guessing at a mode filter.
        if spec is not None and spec.kind == "mcp":
            return True
        # Everything else (agent tools for non-root, shell when allowlist
        # empty, MCP when flag is False, hallucinated tool names) is denied.
        plan_hint = ""
        if self._session_id is not None:
            if self._ctx.depth == 0:
                plan_hint = " You cannot write the plan directly. Spawn a sub-agent to draft it."
            else:
                plan_hint = f" Plan file: {plan_file_for(self._session_id)}."
        mock = get_mock_speaker()
        action_step.result = mock(
            content=(
                f"Plan mode: tool '{tool_id}' is unavailable. Use read-only "
                "tools to explore and your edit tool to draft the plan, "
                f"then call exit_plan_mode.{plan_hint}"
            )
        )
        self._emit_event(
            {
                "type": "permission",
                "payload": {
                    "tool_id": tool_id,
                    "operation": action_step.operation,
                    "tool_input": action_step.tool_input,
                    "decision": "deny",
                },
            }
        )
        return False

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    def _emit_tool_result_event(
        self,
        action_step: ActionStep,
        result: str | None,
        *,
        error: str | None = None,
        result_file: str | None = None,
        seen: str | None = None,
        permanence: str | None = None,
    ) -> None:
        """Emit one ``tool_result`` event for a finished (or failed) tool call.

        *result* is the RAW payload snapshot (event-side cap) and *seen* is the
        string actually handed to the model after truncation. Both are recorded,
        labeled, because they routinely differ by two orders of magnitude and
        the store previously kept only the raw one: a trace showed a full
        100K-character result for a call the model read 2,000 characters of, so
        every "the model had this and ignored it" reading of a transcript was
        unfalsifiable. *seen* is omitted when it is identical to *result*.

        *permanence* carries a tool's own verdict on whether its failure can
        ever succeed on retry (see :class:`_SessionToolError`).
        """
        max_chars = self._result_char_cap(action_step.tool_id)
        # `summary` is a short preview for log titles / agent-tree rendering;
        # the full payload lives in `result`, which the frontend renders in a
        # scrollable container. Keep `summary` capped at the LLM-context size
        # so it stays human-skimmable.
        if max_chars and result and len(result) > max_chars:
            summary = error or result[:max_chars]
        else:
            summary = error or result or ""
        payload: dict[str, Any] = {
            "tool_id": action_step.tool_id,
            "operation": action_step.operation,
            "tool_input": action_step.tool_input,
            "result": result,
            "success": error is None,
            "summary": f"ERROR: {error}" if error else summary,
        }
        if error:
            payload["error"] = error
        if result_file:
            payload["result_file"] = result_file
        # What the model actually read, recorded only when it differs from the
        # raw snapshot — so a consumer can tell a truncated read from a full one
        # instead of inferring it from a cap it would have to re-derive.
        if seen is not None and seen != result:
            payload["result_seen"] = seen
            payload["result_truncated"] = True
        if permanence:
            payload["permanence"] = permanence
        # Always tag with agent_id and model so the console can display
        # badges for all agents including the root.
        payload["agent_id"] = self._ctx.agent_id
        payload["model"] = self._ctx.model_name
        self._emit_event({"type": "tool_result", "payload": payload})

    def _emit_event(self, event: Event) -> None:
        # Capture retry/fallback events for the resilience note before they
        # leave for the sink (no-op for every other event type). This is the ONE
        # place both the strategy's automatic switches and the model_control
        # tool's deliberate ones flow through, so the note sees them all.
        self._resilience_note.record(event)
        if self._ctx.event_logger is not None:
            self._ctx.event_logger(event)

    @staticmethod
    def _extract_text_content(content: object) -> str:
        """Extract plain text from an AIMessage content field."""
        if isinstance(content, list):
            text = "\n".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
        else:
            text = (str(content) if content else "").strip()
        # Drop the internal "(no content)" placeholder so it never surfaces
        # in ``agent_message`` events — internal-only events are filtered
        # from agent_message display.
        if text == _NO_CONTENT_PLACEHOLDER:
            return ""
        return text


# ------------------------------------------------------------------
# Standalone helpers
# ------------------------------------------------------------------


# Envelope codes that mean "a human must change something outside this run" —
# credentials, reachability, permission, quota. They are what separates a run
# that FAILED from one that is BLOCKED: retrying, re-planning or switching
# models cannot clear any of them, so a run that ends while one of these stands
# unrecovered has not completed its goal no matter how clean its last turn read.
# A tool's own verdict on whether its failure could ever succeed on retry.
# ``permanent`` is the one that was missing: 66 consecutive failures of a single
# tool in one session — chains of 13 inside 90 seconds — all against a
# precondition that tool could never satisfy, because nothing in the envelope
# could say so and neither the loop nor the model could tell futile from
# retry-worthy.
_ENVELOPE_PERMANENCE: frozenset[str] = frozenset({"permanent", "transient"})


@dataclass(frozen=True)
class _SessionToolError:
    """A session tool's structured ``{"error": {...}}`` envelope, parsed.

    SessionTools across the graph plugin suites (wiki ``_err_result`` / scg
    ``err_result``) signal a HANDLED failure by RETURNING a ``MockSpeaker`` whose
    content is ``str({"error": {"code": ..., "message": ...}})`` — a *successful*
    return, so without this seam the loop recorded the step as ``success=True``
    and the per-step failure-feedback nudge never fired (an embedding-429
    rendered as "✓ ok"). The parse is purely structural: core never imports the
    graph layer, so the envelope SHAPE is the entire contract.

    ``permanence`` is optional and additive — an envelope that omits it parses
    exactly as before and reads ``None`` (unknown), so no existing tool changes.
    """

    code: str
    message: str
    permanence: str | None = None

    @property
    def summary(self) -> str:
        """The ``"code: message"`` line for the event's ``error`` field."""
        if self.code and self.message:
            return f"{self.code}: {self.message}"
        return self.code or self.message

    @property
    def blocks_completion(self) -> bool:
        """True when this error means the run is blocked, not merely failed."""
        return self.code in BLOCKED_CODES

    @classmethod
    def parse(cls, content: object) -> _SessionToolError | None:
        """Parse *content* as an error envelope, or ``None`` if it is not one.

        The literal parse is ``ast.literal_eval``, NOT ``json.loads``: these
        envelopes travel as ``str(dict)`` (single quotes, Python repr), so a
        tool that reaches for ``json.dumps`` produces a payload this seam
        silently declines to recognise — and its failure then records as a
        success. That asymmetry is why the envelope contract says ``str(dict)``.
        """
        text = content if isinstance(content, str) else getattr(content, "content", None)
        if not isinstance(text, str):
            return None
        stripped = text.strip()
        # Cheap reject before the (bounded) literal parse: the envelope is the
        # repr of a one-key ``{"error": …}`` dict, so it always starts with
        # ``{'error'``.
        if not stripped.startswith("{'error'") and not stripped.startswith('{"error"'):
            return None
        try:
            parsed = ast.literal_eval(stripped)
        except (ValueError, SyntaxError):
            return None
        if not isinstance(parsed, dict):
            return None
        err = parsed.get("error")
        if not isinstance(err, dict):
            return None
        code = str(err.get("code", "")).strip()
        message = str(err.get("message", "")).strip()
        if not code and not message:
            return None
        raw_permanence = str(err.get("permanence", "")).strip().lower()
        return cls(
            code=code,
            message=message,
            # An unrecognised value reads as "undeclared" rather than being
            # carried through: a typo must never present as a typed verdict.
            permanence=raw_permanence if raw_permanence in _ENVELOPE_PERMANENCE else None,
        )


def _session_tool_error_envelope(content: object) -> str | None:
    """Return the ``"code: message"`` summary if *content* is an error envelope.

    The string projection of :meth:`_SessionToolError.parse`.

    **It has no caller inside this module, and that is deliberate — do not
    "fix" it by deleting it or by re-wiring the dispatch seam to it.** The
    dispatch seam needs the envelope's CODE and its permanence verdict, not
    just a display string, so it parses the structured form directly; the
    reclassification this name is documented for (an envelope RETURN records
    as a FAILED step) still happens there, unchanged. This projection survives
    because it is the name the contract is documented under across four
    packages — ``client_tools``, ``ask_user``, ``triggers.session_tool`` and
    the apps plugin suite all tell tool authors to satisfy *this* checker — and
    because the suite asserts the envelope shape through it. Removing it means
    retiring that name everywhere it is published, not just here.
    """
    parsed = _SessionToolError.parse(content)
    return parsed.summary if parsed is not None else None


def _append_lsp_feedback(content: str, file_path: str, cwd: str) -> str:
    """Append passive LSP diagnostics to an edit tool result (if available)."""
    try:
        from mewbo_tools.integration.lsp import get_passive_diagnostics

        feedback = get_passive_diagnostics(file_path, cwd)
        if feedback:
            return f"{content}\n\n--- Passive Feedback (LSP) ---\n{feedback}"
    except Exception:
        pass  # LSP not installed or not running — silently skip
    return content


def _infer_operation(tool_id: str) -> str:
    """Map a tool_id to 'get' or 'set' for AbstractTool dispatch."""
    lowered = tool_id.lower()
    if any(keyword in lowered for keyword in _OPERATION_SET_KEYWORDS):
        return "set"
    if any(keyword in lowered for keyword in _OPERATION_GET_KEYWORDS):
        return "get"
    return "set"


def _coerce_mcp_tool_input(action_step: ActionStep, spec: ToolSpec) -> str | None:
    """Validate and coerce tool_input for MCP tools against their schema.

    Returns an error string if coercion fails, or None on success.
    Mutates ``action_step.tool_input`` in place when coercion is needed.
    """
    if spec.kind != "mcp":
        return None
    schema = spec.metadata.get("schema") if spec.metadata else None
    if not isinstance(schema, dict):
        return None
    required = schema.get("required") or []
    properties = schema.get("properties") or {}
    if not isinstance(properties, dict):
        properties = {}
    expected_fields = list(required) or list(properties.keys())

    argument = action_step.tool_input
    if isinstance(argument, str):
        stripped = argument.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                action_step.tool_input = parsed
                argument = parsed
        if isinstance(argument, str):
            if expected_fields:
                preferred_fields = ["query", "question", "input", "text", "q"]
                target_field = None
                if len(expected_fields) == 1:
                    target_field = expected_fields[0]
                else:
                    for preferred in preferred_fields:
                        if preferred in expected_fields:
                            target_field = preferred
                            break
                if target_field:
                    action_step.tool_input = {target_field: argument}
                    return None
            fields = ", ".join(expected_fields) if expected_fields else "schema-defined fields"
            return f"Expected JSON object with fields: {fields}."

    if isinstance(argument, dict):
        if required:
            missing = [name for name in required if name not in argument]
            if missing:
                if len(required) == 1 and len(argument) == 1:
                    required_field = required[0]
                    value = next(iter(argument.values()))
                    prop = properties.get(required_field, {})
                    if (
                        isinstance(prop, dict)
                        and prop.get("type") == "array"
                        and isinstance(value, str)
                    ):
                        items = prop.get("items")
                        if isinstance(items, dict) and items.get("type") == "string":
                            value = [value]
                    if (
                        isinstance(prop, dict)
                        and prop.get("type") == "string"
                        and isinstance(value, list)
                        and len(value) == 1
                    ):
                        value = value[0]
                    action_step.tool_input = {required_field: value}
                    return None
                return f"Missing required fields: {', '.join(missing)}."
        return None

    return "Unsupported tool_input type for MCP tool."


__all__ = ["ToolCallResult", "ToolUseLoop"]
