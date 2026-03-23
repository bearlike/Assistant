#!/usr/bin/env python3
"""Sub-agent spawning tool for the agent hypervisor.

``SpawnAgentTool`` creates a child ``ToolUseLoop`` instance, registers it
in the ``AgentRegistry``, runs it to completion, and returns the result.
Tool scoping follows Claude Code's "filter before binding" pattern: denied
tools are removed from the child's ``bind_tools()`` list so the child LLM
never sees them.
"""

from __future__ import annotations

import asyncio
from typing import Any

from meeseeks_core.agent_context import AgentContext, AgentDepthExceeded, AgentHandle
from meeseeks_core.classes import ActionStep
from meeseeks_core.common import MockSpeaker, get_logger
from meeseeks_core.config import get_config_value
from meeseeks_core.hooks import HookManager
from meeseeks_core.permissions import PermissionPolicy
from meeseeks_core.tool_registry import ToolRegistry, ToolSpec
from meeseeks_core.types import Event

logging = get_logger(name="core.spawn_agent")


def _coerce_list(value: object) -> list[str]:
    """Coerce a config value to a list of strings."""
    if isinstance(value, list):
        return [str(v) for v in value if v]
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()]
    return []


# ------------------------------------------------------------------
# Tool schema (injected into bind_tools, NOT in ToolRegistry)
# ------------------------------------------------------------------

SPAWN_AGENT_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "spawn_agent",
        "description": (
            "Spawn a sub-agent to handle a subtask independently. "
            "Specify allowed_tools to restrict what the sub-agent can do. "
            "Use for tasks that can be decomposed into independent parallel work."
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
            },
            "required": ["task"],
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
        hook_manager: HookManager,
    ) -> None:
        self._agent_context = agent_context
        self._tool_registry = tool_registry
        self._permission_policy = permission_policy
        self._hook_manager = hook_manager

    async def run_async(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a sub-agent asynchronously. Returns result as MockSpeaker."""
        args = (
            action_step.tool_input
            if isinstance(action_step.tool_input, dict)
            else {"task": str(action_step.tool_input)}
        )
        task_desc = str(args.get("task", ""))
        model_override = args.get("model")
        registry = self._agent_context.registry

        # 1. Resolve and validate model.
        resolved_model = self._resolve_model(model_override)
        if resolved_model.startswith("ERROR:"):
            return MockSpeaker(content=resolved_model)

        # 2. Admission control.
        if not await registry.admit():
            return MockSpeaker(
                content="ERROR: Max concurrent agents reached. Try again later."
            )

        child_ctx: AgentContext | None = None
        handle: AgentHandle | None = None
        try:
            # 3. Create child context.
            child_ctx = self._agent_context.child(model_name=resolved_model)

            # 4. Register in registry.
            handle = AgentHandle(
                agent_id=child_ctx.agent_id,
                parent_id=child_ctx.parent_id,
                depth=child_ctx.depth,
                model_name=child_ctx.model_name,
                task_description=task_desc[:200],
            )
            await registry.register(handle)
            self._hook_manager.run_on_agent_start(handle)
            self._emit_event(child_ctx, "start", task_desc)

            # 5. Filter tool specs (Claude Code "filter before binding" pattern).
            child_specs = self._filter_tool_specs(args)

            # 6. Create and run child loop.
            # Import here to avoid circular import at module level.
            from meeseeks_core.tool_use_loop import ToolUseLoop

            child_loop = ToolUseLoop(
                agent_context=child_ctx,
                tool_registry=self._tool_registry,
                permission_policy=self._permission_policy,
                approval_callback=None,  # Sub-agents auto-deny on ASK.
                hook_manager=self._hook_manager,
            )
            max_steps = int(
                get_config_value("agent", "sub_agent_max_steps", default=10)
            )
            tq, state = await child_loop.run(
                task_desc,
                tool_specs=child_specs,
                max_steps=max_steps,
            )

            # 7. Mark done.
            await registry.mark_done(child_ctx.agent_id, "completed")
            self._hook_manager.run_on_agent_stop(handle)
            self._emit_event(child_ctx, "stop", state.done_reason or "completed")

            return MockSpeaker(
                content=tq.task_result or state.done_reason or "No result"
            )

        except AgentDepthExceeded as exc:
            if handle:
                await registry.mark_done(handle.agent_id, "failed", error=str(exc))
                self._hook_manager.run_on_agent_stop(handle)
            return MockSpeaker(content=f"ERROR: {exc}")

        except asyncio.CancelledError:
            if child_ctx:
                await registry.mark_done(child_ctx.agent_id, "cancelled")
            if handle:
                self._hook_manager.run_on_agent_stop(handle)
            raise  # Re-raise for TaskGroup propagation.

        except Exception as exc:
            logging.error("Sub-agent failed: {}", exc)
            if child_ctx:
                await registry.mark_done(
                    child_ctx.agent_id, "failed", error=str(exc)
                )
            if handle:
                self._hook_manager.run_on_agent_stop(handle)
            return MockSpeaker(content=f"ERROR: Sub-agent failed: {exc}")

        finally:
            if child_ctx:
                await registry.unregister(child_ctx.agent_id)
            registry.release()

    # ------------------------------------------------------------------
    # Model resolution
    # ------------------------------------------------------------------

    def _resolve_model(self, model_override: object) -> str:
        """Resolve model for the child agent.

        Returns model name, or ``"ERROR: ..."`` string on validation failure.
        """
        allowed_models = _coerce_list(
            get_config_value("agent", "allowed_models", default=[])
        )
        default_sub = str(
            get_config_value("agent", "default_sub_model", default="") or ""
        ).strip()

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
        specs = self._tool_registry.list_specs()

        # 1. Allowlist: if specified, ONLY these tools are available.
        allowed: list[str] = args.get("allowed_tools") or []
        if allowed:
            allowed_set = set(allowed)
            specs = [s for s in specs if s.tool_id in allowed_set]

        # 2. Denylist: per-spawn denied + config default_denied (hard floor).
        denied: set[str] = set(args.get("denied_tools") or [])
        config_denied = set(
            _coerce_list(get_config_value("agent", "default_denied_tools", default=[]))
        )
        denied |= config_denied

        # 3. Deny takes precedence over allow.
        return [s for s in specs if s.tool_id not in denied]

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    def _emit_event(self, ctx: AgentContext, action: str, detail: str) -> None:
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
                },
            }
            ctx.event_logger(event)


__all__ = ["SPAWN_AGENT_SCHEMA", "SpawnAgentTool"]
