#!/usr/bin/env python3
"""Execute action plans with permissions, hooks, and reflection."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from meeseeks_core.classes import ActionStep, TaskQueue
from meeseeks_core.common import get_logger, get_mock_speaker
from meeseeks_core.hooks import HookManager, default_hook_manager
from meeseeks_core.permissions import (
    PermissionDecision,
    PermissionPolicy,
    approval_callback_from_config,
    load_permission_policy,
)
from meeseeks_core.reflection import StepReflection, StepReflector
from meeseeks_core.tool_registry import ToolRegistry, ToolSpec, load_registry
from meeseeks_core.types import Event, ToolResultPayload

logging = get_logger(name="core.action_runner")

EventLogger = Callable[[Event], None]


@dataclass
class StepOutcome:
    """Result of executing a tool step."""

    content: str
    reflection: StepReflection | None


class ActionPlanRunner:
    """Execute TaskQueue steps with lifecycle hooks."""

    def __init__(
        self,
        tool_registry: ToolRegistry | None = None,
        permission_policy: PermissionPolicy | None = None,
        approval_callback: Callable[[ActionStep], bool] | None = None,
        hook_manager: HookManager | None = None,
        reflector: StepReflector | None = None,
        event_logger: EventLogger | None = None,
        allowed_tool_ids: set[str] | None = None,
        mode: str = "act",
    ) -> None:
        """Initialize the action plan runner."""
        self._tool_registry = tool_registry or load_registry()
        self._permission_policy = permission_policy or load_permission_policy()
        self._approval_callback = approval_callback or approval_callback_from_config()
        self._hook_manager = hook_manager or default_hook_manager()
        self._reflector = reflector
        self._event_logger = event_logger
        self._allowed_tool_ids = allowed_tool_ids
        self._mode = mode

    def run(self, task_queue: TaskQueue) -> TaskQueue:
        """Run all steps in the task queue."""
        task_queue.last_error = None
        for idx, action_step in enumerate(task_queue.action_steps):
            logging.debug("Processing ActionStep: {}", action_step)
            if (
                self._allowed_tool_ids is not None
                and action_step.action_consumer not in self._allowed_tool_ids
            ):
                reason = "tool not allowed in plan mode"
                self._record_failure(action_step, reason, task_queue)
                self._emit_tool_result(action_step, None, error=reason)
                break
            if not self._ensure_permission(action_step):
                self._record_failure(action_step, "permission denied", task_queue)
                self._emit_tool_result(action_step, None, error="Permission denied")
                continue

            action_step = self._hook_manager.run_pre_tool_use(action_step)
            task_queue.action_steps[idx] = action_step
            tool = self._tool_registry.get(action_step.action_consumer)
            if tool is None:
                self._record_failure(action_step, "tool not available", task_queue)
                continue

            spec = self._tool_registry.get_spec(action_step.action_consumer)
            if spec is not None:
                schema_error = self._coerce_mcp_action_argument(action_step, spec)
                if schema_error:
                    self._record_failure(action_step, schema_error, task_queue)
                    self._emit_tool_result(action_step, None, error=schema_error)
                    continue

            try:
                outcome = self._execute_step(action_step)
            except Exception as exc:
                self._handle_tool_error(action_step, exc, task_queue)
                continue

            if outcome.reflection is not None and outcome.reflection.status != "ok":
                if outcome.reflection.revised_argument:
                    action_step.action_argument = outcome.reflection.revised_argument
                action_step.result = None
                self._emit_event(
                    {
                        "type": "step_reflection",
                        "payload": {
                            "action_consumer": action_step.action_consumer,
                            "action_type": action_step.action_type,
                            "action_argument": action_step.action_argument,
                            "status": outcome.reflection.status,
                            "notes": outcome.reflection.notes,
                        },
                    }
                )
                task_queue.action_steps[idx] = action_step
                break

            self._emit_tool_result(action_step, outcome.content)

        summaries = [
            summary
            for step in task_queue.action_steps
            if (summary := self._format_step_summary(step))
        ]
        task_queue.task_result = "\n".join(summaries).strip()
        return task_queue

    def _ensure_permission(self, action_step: ActionStep) -> bool:
        decision = self._permission_policy.decide(action_step)
        decision = self._hook_manager.run_permission_request(action_step, decision)
        decision_logged = False
        logging.debug(
            "Permission check: tool={} action={} decision={} callback_present={}",
            action_step.action_consumer,
            action_step.action_type,
            decision.value if isinstance(decision, PermissionDecision) else decision,
            self._approval_callback is not None,
        )
        if decision == PermissionDecision.ASK:
            approved = self._approval_callback(action_step) if self._approval_callback else False
            logging.debug(
                "Permission prompt result: tool={} action={} approved={}",
                action_step.action_consumer,
                action_step.action_type,
                approved,
            )
            decision = PermissionDecision.ALLOW if approved else PermissionDecision.DENY
            self._emit_event(
                {
                    "type": "permission",
                    "payload": {
                        "action_consumer": action_step.action_consumer,
                        "action_type": action_step.action_type,
                        "action_argument": action_step.action_argument,
                        "decision": decision.value,
                    },
                }
            )
            decision_logged = True
        if decision == PermissionDecision.DENY:
            mock = get_mock_speaker()
            message = (
                "Permission denied for " f"{action_step.action_consumer}:{action_step.action_type}."
            )
            action_step.result = mock(content=message)
            if not decision_logged:
                self._emit_event(
                    {
                        "type": "permission",
                        "payload": {
                            "action_consumer": action_step.action_consumer,
                            "action_type": action_step.action_type,
                            "action_argument": action_step.action_argument,
                            "decision": decision.value,
                        },
                    }
                )
            return False
        return True

    def _execute_step(self, action_step: ActionStep) -> StepOutcome:
        tool = self._tool_registry.get(action_step.action_consumer)
        if tool is None:
            raise RuntimeError("Tool unavailable during execution")
        action_result = tool.run(action_step)
        action_result = self._hook_manager.run_post_tool_use(action_step, action_result)
        action_step.result = action_result
        content = getattr(action_result, "content", None)
        if content is None:
            content = "" if action_result is None else str(action_result)
        reflection = None
        if self._reflector is not None:
            reflection = self._reflector.reflect(action_step, content)
        return StepOutcome(content=str(content), reflection=reflection)

    def _handle_tool_error(
        self, action_step: ActionStep, exc: Exception, task_queue: TaskQueue
    ) -> None:
        logging.error("Error processing action step: {}", exc)
        self._record_failure(action_step, str(exc), task_queue)
        self._tool_registry.disable(action_step.action_consumer, f"Runtime error: {exc}")
        self._emit_tool_result(action_step, None, error=str(exc))
        mock = get_mock_speaker()
        self._hook_manager.run_post_tool_use(action_step, mock(content=f"Tool error: {exc}"))

    def _record_failure(self, step: ActionStep, reason: str, task_queue: TaskQueue) -> None:
        note = f"{step.action_consumer} ({step.action_type}) failed"
        if reason:
            note = f"{note}: {reason}"
        task_queue.last_error = note
        if step.result is None and reason:
            mock = get_mock_speaker()
            step.result = mock(content=f"ERROR: {reason}")

    def _emit_tool_result(
        self, action_step: ActionStep, result: str | None, *, error: str | None = None
    ) -> None:
        summary = self._summarize_result(result, error)
        payload: ToolResultPayload = {
            "action_consumer": action_step.action_consumer,
            "action_type": action_step.action_type,
            "action_argument": action_step.action_argument,
            "result": result,
            "success": error is None,
            "summary": summary,
        }
        if error:
            payload["error"] = error
        self._emit_event({"type": "tool_result", "payload": payload})

    def _emit_event(self, event: Event) -> None:
        if self._event_logger is not None:
            self._event_logger(event)

    @staticmethod
    def _coerce_mcp_action_argument(action_step: ActionStep, spec: ToolSpec) -> str | None:
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

        argument = action_step.action_argument
        if isinstance(argument, str):
            stripped = argument.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    action_step.action_argument = parsed
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
                        action_step.action_argument = {target_field: argument}
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
                        action_step.action_argument = {required_field: value}
                        return None
                    return f"Missing required fields: {', '.join(missing)}."
            return None

        return "Unsupported action_argument type for MCP tool."

    @staticmethod
    def _summarize_result(result: str | None, error: str | None) -> str:
        if error:
            return f"ERROR: {error}"
        if result is None:
            return ""
        text = str(result).strip()
        if len(text) <= 500:
            return text
        return text[:497] + "..."

    @classmethod
    def _format_step_summary(cls, step: ActionStep) -> str:
        if step.result is None:
            return ""
        content = getattr(step.result, "content", step.result)
        summary = cls._summarize_result(str(content), None)
        if not summary:
            return ""
        return f"{step.action_consumer}:{step.action_type} -> {summary}"


__all__ = ["ActionPlanRunner", "EventLogger"]
