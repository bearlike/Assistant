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

from truss_core.agent_context import AgentContext, AgentDepthExceeded
from truss_core.classes import ActionStep
from truss_core.common import MockSpeaker, get_logger
from truss_core.config import get_config_value
from truss_core.hooks import HookManager
from truss_core.hypervisor import AgentHandle
from truss_core.permissions import PermissionPolicy
from truss_core.session_tools import SessionToolRegistry
from truss_core.tool_registry import ToolRegistry, ToolSpec, filter_specs
from truss_core.types import Event

logging = get_logger(name="core.spawn_agent")


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
            },
            "required": ["task"],
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
        # Plan-mode context — set by ToolUseLoop.run() so children
        # inherit the session's plan path and mode.
        self.session_id: str | None = None
        self.parent_mode: str = "act"
        # Track lifecycle manager tasks for deterministic cleanup.
        self._lifecycle_tasks: list[asyncio.Task[None]] = []

    async def run_async(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a sub-agent asynchronously. Returns result as MockSpeaker."""
        args = (
            action_step.tool_input
            if isinstance(action_step.tool_input, dict)
            else {"task": str(action_step.tool_input)}
        )
        task_desc = str(args.get("task", ""))
        acceptance_criteria = str(args.get("acceptance_criteria", "") or "")
        if acceptance_criteria:
            # Ref: [DeepMind-Delegation §4.1] Contract-first decomposition —
            # delegation is contingent upon the outcome having precise verification.
            task_desc += f"\n\nAcceptance criteria: {acceptance_criteria}"
        model_override = args.get("model")

        # agent_type: look up registered agent definition and apply its config.
        agent_type = args.get("agent_type")
        if agent_type and self._agent_registry:
            agent_def = self._agent_registry.get(
                agent_type, self._session_capabilities
            )
            if agent_def is None:
                return MockSpeaker(content=f"ERROR: Unknown agent type '{agent_type}'")
            # Prepend agent system prompt to task, running the plugin-generic
            # body substitution first so ``${SESSION_ID}``,
            # ``${CLAUDE_PLUGIN_ROOT}``, and bash-style ``${VAR:-default}``
            # expansions resolve before the body hits the child LLM.
            body_subs = {
                "SESSION_ID": self.session_id or "",
                "CLAUDE_PLUGIN_ROOT": agent_def.plugin_root,
            }
            rendered_body = substitute_agent_body(agent_def.body, body_subs)
            task_desc = f"{rendered_body}\n\n---\n\nTask: {task_desc}"
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
            return MockSpeaker(content=resolved_model)

        # 2. Admission control.
        if not await registry.admit():
            return MockSpeaker(content="ERROR: Max concurrent agents reached. Try again later.")

        child_ctx: AgentContext | None = None
        handle: AgentHandle | None = None
        tq = None  # Initialized early so error handlers can read partial results
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
                status="submitted",
                message_queue=child_ctx.message_queue,
            )
            await registry.register(handle)
            self._hook_manager.run_on_agent_start(handle)
            self._emit_event(child_ctx, "start", task_desc, handle=handle)

            # 5. Filter tool specs (Claude Code "filter before binding" pattern).
            child_specs = self._filter_tool_specs(args)

            # 6. Create and run child loop.
            # Import here to avoid circular import at module level.
            from truss_core.tool_use_loop import ToolUseLoop

            # Ref: [DeepMind-Delegation §4.7] Privilege attenuation — sub-agents
            # inherit parent's approval policy (not None, which blocks all writes).
            child_allowed_tools = _coerce_list(args.get("allowed_tools") or []) or None
            child_loop = ToolUseLoop(
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
            )
            # Ref: [A2A v1.0] Transition to "running" when execution begins
            handle.status = "running"
            # Populate asyncio_task so cancel_agent() and 3-phase cleanup work.
            # No step limit — the child runs until natural completion
            # (text response without tool calls), cancellation, or error.
            # Safety: session budget, stall detection, LLM timeouts.
            child_task = asyncio.create_task(
                child_loop.run(
                    task_desc,
                    tool_specs=child_specs,
                    mode=self.parent_mode,
                )
            )
            handle.asyncio_task = child_task

            # Ref: [DeepMind-Delegation §4.4] Root agent delegates non-blockingly
            # to maintain continuous monitoring capability (epoll model).
            if self._agent_context.depth == 0:
                # Non-blocking: lifecycle manager handles completion in background
                lm_task = asyncio.create_task(
                    self._run_child_lifecycle(
                        child_task,
                        child_ctx,
                        handle,
                        task_desc,
                    )
                )
                self._lifecycle_tasks.append(lm_task)
                # Store child_id before clearing refs (finally guard)
                child_id = child_ctx.agent_id
                # Prevent finally block from cleaning up — lifecycle manager owns it
                child_ctx = None
                handle = None
                return MockSpeaker(
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
                    )
                )

            # Blocking: current behavior for non-root agents.
            tq, state = await child_task

            # 7. Mark done.
            await registry.mark_done(child_ctx.agent_id, "completed")
            self._hook_manager.run_on_agent_stop(handle)
            self._emit_event(child_ctx, "stop", state.done_reason or "completed", handle=handle)

            # Ref: [CoA §3.1] Build Communication Unit — compressed context for parent
            from truss_core.hypervisor import AgentResult

            result = AgentResult(
                content=tq.task_result or state.done_reason or "No result",
                status="completed" if state.done else "failed",
                steps_used=handle.steps_completed,
                summary=(tq.task_result or "")[:500],
            )
            return MockSpeaker(content=json.dumps(asdict(result)))

        except AgentDepthExceeded as exc:
            agent_error = AgentError(
                agent_id=handle.agent_id if handle else "unknown",
                depth=child_ctx.depth if child_ctx else 0,
                task=task_desc[:200],
                error=str(exc),
            )
            if handle:
                await registry.mark_done(
                    handle.agent_id,
                    "failed",
                    error=agent_error,
                )
                self._hook_manager.run_on_agent_stop(handle)
            from truss_core.hypervisor import AgentResult

            result = AgentResult(
                content=f"Depth exceeded: {exc}",
                status="cannot_solve",
                steps_used=handle.steps_completed if handle else 0,
                warnings=[str(exc)],
            )
            return MockSpeaker(content=json.dumps(asdict(result)))

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
            from truss_core.hypervisor import AgentResult

            partial_result = (tq.task_result or "")[:500] if tq is not None else ""
            result = AgentResult(
                content=f"Sub-agent failed: {exc}",
                status="failed",
                steps_used=handle.steps_completed if handle else 0,
                warnings=[str(exc)],
                # Ref: [DeepMind-Delegation §6.1] Checkpoint — partial work survives failure
                summary=partial_result,
            )
            return MockSpeaker(content=json.dumps(asdict(result)))

        finally:
            if child_ctx:
                # Cancel any children spawned by this sub-agent.
                children = await registry.list_children(child_ctx.agent_id)
                for child in children:
                    if child.status == "running":
                        await registry.cancel_agent(child.agent_id)

                await registry.unregister(child_ctx.agent_id)
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
                try:
                    _done, pending = await asyncio.wait(
                        waiters,
                        timeout=timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except asyncio.TimeoutError:
                    pending = set(waiters)
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
    ) -> None:
        """Emit a sub_agent lifecycle event."""
        if ctx.event_logger is not None:
            event: Event = {
                "type": "sub_agent",
                "payload": {
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
                },
            }
            ctx.event_logger(event)

    # ------------------------------------------------------------------
    # Non-blocking lifecycle manager  (Ref: [DeepMind-Delegation §4.4])
    # ------------------------------------------------------------------

    async def _run_child_lifecycle(
        self,
        child_task: asyncio.Task[Any],
        child_ctx: AgentContext,
        handle: AgentHandle,
        task_desc: str,
    ) -> None:
        """Background lifecycle manager for non-blocking child execution.

        Wraps child execution, stores the ``AgentResult`` on the handle,
        and notifies the parent via ``send_to_parent``.

        Ref: [DeepMind-Delegation §4.5] Lifecycle events at phase transitions.
        Ref: [CoA §3.1] CU stored on handle for async retrieval.
        """
        registry = self._agent_context.registry
        tq = None
        try:
            tq, state = await child_task

            await registry.mark_done(child_ctx.agent_id, "completed")
            self._hook_manager.run_on_agent_stop(handle)
            self._emit_event(
                child_ctx,
                "stop",
                state.done_reason or "completed",
                handle=handle,
            )

            from truss_core.hypervisor import AgentResult

            handle.result = AgentResult(
                content=tq.task_result or state.done_reason or "No result",
                status="completed" if state.done else "failed",
                steps_used=handle.steps_completed,
                summary=(tq.task_result or "")[:500],
            )

        except asyncio.CancelledError:
            await registry.mark_done(child_ctx.agent_id, "cancelled")
            self._hook_manager.run_on_agent_stop(handle)

            from truss_core.hypervisor import AgentResult

            handle.result = AgentResult(
                content="Cancelled",
                status="cancelled",
                steps_used=handle.steps_completed,
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

            from truss_core.hypervisor import AgentResult

            partial = (tq.task_result or "")[:500] if tq is not None else ""
            handle.result = AgentResult(
                content=f"Sub-agent failed: {exc}",
                status="failed",
                steps_used=handle.steps_completed,
                warnings=[str(exc)],
                summary=partial,
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
    "SPAWN_AGENT_SCHEMA",
    "STEER_AGENT_SCHEMA",
    "SpawnAgentTool",
    "substitute_agent_body",
]
