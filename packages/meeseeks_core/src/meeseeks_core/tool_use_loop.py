#!/usr/bin/env python3
"""Unified tool-use conversation loop."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from meeseeks_core.classes import ActionStep, OrchestrationState, Plan, TaskQueue
from meeseeks_core.common import get_logger, get_mock_speaker, get_system_prompt
from meeseeks_core.components import build_langfuse_handler, langfuse_trace_span
from meeseeks_core.config import get_config_value
from meeseeks_core.context import ContextSnapshot, render_event_lines
from meeseeks_core.hooks import HookManager
from meeseeks_core.llm import build_chat_model, specs_to_langchain_tools
from meeseeks_core.permissions import PermissionDecision, PermissionPolicy
from meeseeks_core.tool_registry import ToolRegistry, ToolSpec
from meeseeks_core.types import Event

logging = get_logger(name="core.tool_use_loop")

# Maps tool_id patterns to the AbstractTool operation ("get" or "set").
# Shell and edit tools use set_state(); read/list tools use get_state().
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
    }
)


@dataclass(frozen=True)
class ToolCallResult:
    """Result of executing a single tool call."""

    tool_call_id: str
    tool_id: str
    content: str
    success: bool


class ToolUseLoop:
    """Unified tool-use conversation loop.

    Replaces the old Planner → StepExecutor → PlanUpdater → Synthesizer
    pipeline with a single conversation where the LLM directly decides
    which tools to call via native ``bind_tools`` / ``tool_use``.
    """

    def __init__(
        self,
        *,
        model_name: str,
        tool_registry: ToolRegistry,
        permission_policy: PermissionPolicy,
        approval_callback: Callable[[ActionStep], bool] | None = None,
        hook_manager: HookManager,
        event_logger: Callable[[Event], None] | None = None,
    ) -> None:
        """Initialize the tool-use loop dependencies."""
        self._model_name = model_name
        self._tool_registry = tool_registry
        self._permission_policy = permission_policy
        self._approval_callback = approval_callback
        self._hook_manager = hook_manager
        self._event_logger = event_logger

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        user_query: str,
        *,
        tool_specs: list[ToolSpec],
        context: ContextSnapshot | None = None,
        max_steps: int = 10,
        plan: Plan | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> tuple[TaskQueue, OrchestrationState]:
        """Run the tool-use loop and return a TaskQueue + OrchestrationState."""
        state = OrchestrationState(goal=user_query)
        executed_steps: list[ActionStep] = []
        tool_outputs: list[str] = []
        last_error: str | None = None

        messages = self._build_messages(user_query, context, plan)
        tool_schemas = specs_to_langchain_tools(tool_specs)
        model = self._bind_model(tool_schemas)

        langfuse_handler = build_langfuse_handler(
            user_id="meeseeks-tool-use",
            session_id=f"tool-use-{os.getpid()}-{os.urandom(4).hex()}",
            trace_name="meeseeks-tool-use",
            version=get_config_value("runtime", "version", default="Not Specified"),
            release=get_config_value("runtime", "envmode", default="Not Specified"),
        )
        invoke_config: dict[str, Any] = {}
        if langfuse_handler is not None:
            invoke_config["callbacks"] = [langfuse_handler]

        steps_run = 0
        for _ in range(max_steps):
            if should_cancel is not None and should_cancel():
                state.done = True
                state.done_reason = "canceled"
                break

            with langfuse_trace_span("tool-use-step") as span:
                if span is not None:
                    try:
                        span.update_trace(input={"step": steps_run, "message_count": len(messages)})
                    except Exception:
                        pass

                response: AIMessage = model.invoke(messages, config=invoke_config or None)
                messages.append(response)

                if not response.tool_calls:
                    # Text response — task is done.
                    content = str(getattr(response, "content", "") or "")
                    tool_outputs.append(content)
                    state.done = True
                    state.done_reason = "completed"
                    break

                # Execute each tool call in this response.
                for tool_call in response.tool_calls:
                    result = self._execute_tool_call(tool_call, tool_specs)
                    messages.append(
                        ToolMessage(content=result.content, tool_call_id=result.tool_call_id)
                    )
                    tool_outputs.append(f"{result.tool_id}: {result.content}")
                    if not result.success:
                        last_error = result.content
                    # Track as ActionStep for TaskQueue compatibility.
                    action_step = self._tool_call_to_action_step(tool_call)
                    mock = get_mock_speaker()
                    action_step.result = mock(content=result.content)
                    executed_steps.append(action_step)

                steps_run += 1

                if span is not None:
                    try:
                        span.update_trace(
                            output={
                                "tool_calls": len(response.tool_calls),
                                "steps_run": steps_run,
                            }
                        )
                    except Exception:
                        pass

        # Determine final state if loop exhausted without breaking.
        if not state.done:
            state.done = True
            state.done_reason = "max_steps_reached" if steps_run >= max_steps else "completed"

        # Build TaskQueue for compatibility with CLI / API consumers.
        plan_steps = list(plan.steps) if plan and plan.steps else []
        task_queue = TaskQueue(
            plan_steps=plan_steps,
            action_steps=executed_steps,
        )
        task_queue.task_result = "\n".join(item for item in tool_outputs if item).strip()
        task_queue.last_error = last_error
        state.tool_results = tool_outputs

        return task_queue, state

    # ------------------------------------------------------------------
    # Message construction
    # ------------------------------------------------------------------

    def _build_messages(
        self,
        user_query: str,
        context: ContextSnapshot | None,
        plan: Plan | None,
    ) -> list[BaseMessage]:
        """Build the initial message list for the conversation."""
        system_parts: list[str] = [get_system_prompt("system")]

        # Session context.
        if context and context.summary:
            system_parts.append(f"Session summary:\n{context.summary}")
        if context and context.recent_events:
            rendered = render_event_lines(context.recent_events)
            if rendered:
                system_parts.append(f"Recent conversation:\n{rendered}")

        # Tool-specific guidance from prompt files.
        tool_guidance = self._render_tool_guidance()
        if tool_guidance:
            system_parts.append(f"Tool guidance:\n{tool_guidance}")

        # Plan context.
        if plan and plan.steps:
            plan_lines = "\n".join(
                f"{i + 1}. {s.title} — {s.description}" for i, s in enumerate(plan.steps)
            )
            system_parts.append(
                f"Execute this plan:\n{plan_lines}\n"
                "Follow steps in order. Adapt if results require it."
            )

        system_prompt = "\n\n".join(system_parts)
        return [SystemMessage(content=system_prompt), HumanMessage(content=user_query)]

    def _render_tool_guidance(self) -> str:
        """Render tool-specific prompt guidance for local tools."""
        prompts: list[str] = []
        for spec in self._tool_registry.list_specs():
            if spec.kind != "local" or not spec.prompt_path:
                continue
            try:
                prompt = get_system_prompt(spec.prompt_path)
            except OSError:
                continue
            if prompt:
                prompts.append(prompt)
        return "\n\n".join(prompts)

    # ------------------------------------------------------------------
    # Model binding
    # ------------------------------------------------------------------

    def _bind_model(self, tool_schemas: list[dict[str, Any]]) -> Any:
        """Build a chat model and bind tool schemas."""
        model = build_chat_model(
            model_name=self._model_name,
            openai_api_base=get_config_value("llm", "api_base"),
            api_key=get_config_value("llm", "api_key"),
        )
        if tool_schemas:
            return model.bind_tools(tool_schemas)
        return model

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    def _execute_tool_call(
        self,
        tool_call: dict[str, Any],
        tool_specs: list[ToolSpec],
    ) -> ToolCallResult:
        """Execute a single LLM tool_call: permission → hooks → run → emit event."""
        tool_call_id: str = tool_call.get("id") or ""
        tool_id: str = tool_call.get("name") or ""

        action_step = self._tool_call_to_action_step(tool_call)

        # MCP input coercion.
        spec = self._tool_registry.get_spec(tool_id)
        if spec is not None:
            coercion_error = _coerce_mcp_tool_input(action_step, spec)
            if coercion_error:
                self._emit_tool_result_event(action_step, None, error=coercion_error)
                return ToolCallResult(
                    tool_call_id=tool_call_id, tool_id=tool_id,
                    content=f"ERROR: {coercion_error}", success=False,
                )

        # Permission check.
        if not self._check_permission(action_step):
            self._emit_tool_result_event(action_step, None, error="Permission denied")
            return ToolCallResult(
                tool_call_id=tool_call_id, tool_id=tool_id,
                content="Permission denied", success=False,
            )

        # Pre-tool hook.
        action_step = self._hook_manager.run_pre_tool_use(action_step)

        # Execute.
        tool = self._tool_registry.get(tool_id)
        if tool is None:
            self._emit_tool_result_event(action_step, None, error="Tool not available")
            return ToolCallResult(
                tool_call_id=tool_call_id, tool_id=tool_id,
                content="ERROR: Tool not available", success=False,
            )

        try:
            result = tool.run(action_step)
        except Exception as exc:
            logging.error("Tool execution failed: {}", exc)
            self._emit_tool_result_event(action_step, None, error=str(exc))
            return ToolCallResult(
                tool_call_id=tool_call_id, tool_id=tool_id,
                content=f"ERROR: {exc}", success=False,
            )

        # Post-tool hook.
        result = self._hook_manager.run_post_tool_use(action_step, result)

        content = getattr(result, "content", None)
        if content is None:
            content = "" if result is None else str(result)
        content_str = str(content) if not isinstance(content, str) else content
        # Flatten dict payloads (e.g. shell tool returns a dict).
        if isinstance(content, dict):
            content_str = json.dumps(content, ensure_ascii=False, default=str)

        self._emit_tool_result_event(action_step, content_str)
        return ToolCallResult(
            tool_call_id=tool_call_id, tool_id=tool_id,
            content=content_str, success=True,
        )

    # ------------------------------------------------------------------
    # ActionStep construction
    # ------------------------------------------------------------------

    def _tool_call_to_action_step(self, tool_call: dict[str, Any]) -> ActionStep:
        """Convert an LLM tool_call dict to an ActionStep."""
        tool_id: str = tool_call.get("name") or ""
        args: Any = tool_call.get("args") or {}
        operation = _infer_operation(tool_id)
        return ActionStep(
            title=tool_id,
            tool_id=tool_id,
            operation=operation,
            tool_input=args,
        )

    # ------------------------------------------------------------------
    # Permission
    # ------------------------------------------------------------------

    def _check_permission(self, action_step: ActionStep) -> bool:
        """Check permission for an action step. Returns True if allowed."""
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

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    def _emit_tool_result_event(
        self,
        action_step: ActionStep,
        result: str | None,
        *,
        error: str | None = None,
    ) -> None:
        summary = error or (result[:500] if result and len(result) > 500 else result) or ""
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
        self._emit_event({"type": "tool_result", "payload": payload})

    def _emit_event(self, event: Event) -> None:
        if self._event_logger is not None:
            self._event_logger(event)


# ------------------------------------------------------------------
# Standalone helpers (extracted from action_runner.py)
# ------------------------------------------------------------------


def _infer_operation(tool_id: str) -> str:
    """Map a tool_id to 'get' or 'set' for AbstractTool dispatch."""
    lowered = tool_id.lower()
    if any(keyword in lowered for keyword in _OPERATION_SET_KEYWORDS):
        return "set"
    if any(keyword in lowered for keyword in _OPERATION_GET_KEYWORDS):
        return "get"
    # Default to "set" — safer than "get" since most tools implement set_state().
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
