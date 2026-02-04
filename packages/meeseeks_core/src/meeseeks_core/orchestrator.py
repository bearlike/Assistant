#!/usr/bin/env python3
"""Session orchestration entrypoint."""

from __future__ import annotations

import os
from collections.abc import Callable

from meeseeks_core.action_runner import ActionPlanRunner
from meeseeks_core.classes import ActionStep, OrchestrationState, TaskQueue
from meeseeks_core.common import get_logger
from meeseeks_core.compaction import should_compact, summarize_events
from meeseeks_core.context import ContextBuilder
from meeseeks_core.hooks import HookManager, default_hook_manager
from meeseeks_core.permissions import (
    PermissionPolicy,
    approval_callback_from_env,
    load_permission_policy,
)
from meeseeks_core.planning import Planner, ResponseSynthesizer
from meeseeks_core.reflection import StepReflector
from meeseeks_core.session_store import SessionStore
from meeseeks_core.token_budget import get_token_budget
from meeseeks_core.tool_registry import ToolRegistry, load_registry
from meeseeks_core.types import ActionStepPayload

logging = get_logger(name="core.orchestrator")


class Orchestrator:
    """Plan-act-observe orchestration loop."""

    def __init__(
        self,
        *,
        model_name: str | None = None,
        session_store: SessionStore | None = None,
        tool_registry: ToolRegistry | None = None,
        permission_policy: PermissionPolicy | None = None,
        approval_callback: Callable[[ActionStep], bool] | None = None,
        hook_manager: HookManager | None = None,
    ) -> None:
        """Initialize orchestration dependencies."""
        self._model_name = (
            model_name
            or os.getenv("ACTION_PLAN_MODEL")
            or os.getenv("DEFAULT_MODEL", "gpt-3.5-turbo")
        )
        self._session_store = session_store or SessionStore()
        self._tool_registry = tool_registry or load_registry()
        self._permission_policy = permission_policy or load_permission_policy()
        self._approval_callback = approval_callback or approval_callback_from_env()
        self._hook_manager = hook_manager or default_hook_manager()
        self._context_builder = ContextBuilder(self._session_store)
        self._planner = Planner(self._tool_registry)
        self._synthesizer = ResponseSynthesizer(self._tool_registry)

    def run(
        self,
        user_query: str,
        *,
        max_iters: int = 3,
        initial_task_queue: TaskQueue | None = None,
        return_state: bool = False,
        session_id: str | None = None,
    ) -> TaskQueue | tuple[TaskQueue, OrchestrationState]:
        """Run a plan-act-observe loop for a session."""
        if session_id is None:
            session_id = self._session_store.create_session()

        state = OrchestrationState(goal=user_query, session_id=session_id)
        state.summary = self._session_store.load_summary(session_id)
        state.tool_results = state.tool_results or []
        state.open_questions = state.open_questions or []

        self._session_store.append_event(
            session_id, {"type": "user", "payload": {"text": user_query}}
        )

        if self._should_update_summary(user_query):
            state.summary = self._update_summary_with_memory(
                session_id,
                user_query.strip(),
            )

        updated_summary = self._maybe_auto_compact(session_id)
        if updated_summary:
            state.summary = updated_summary

        if user_query.strip() == "/compact":
            summary = summarize_events(self._session_store.load_transcript(session_id))
            self._session_store.save_summary(session_id, summary)
            state.summary = summary
            state.done = True
            state.done_reason = "compacted"
            task_queue = self._build_direct_response(f"Compaction complete. Summary: {summary}")
            return (task_queue, state) if return_state else task_queue

        context = self._context_builder.build(
            session_id=session_id,
            user_query=user_query,
            model_name=self._model_name,
        )
        task_queue = initial_task_queue or self._planner.generate(
            user_query, self._model_name, context=context
        )
        state.plan = task_queue.action_steps
        self._append_action_plan(session_id, task_queue.action_steps)

        for iteration in range(max_iters):
            task_queue = self._run_action_plan(session_id, task_queue)
            state.tool_results.append(task_queue.task_result or "")

            if self._action_steps_complete(task_queue):
                state.done = True
                state.done_reason = "completed"
                break

            if iteration < max_iters - 1:
                revised_query = self._build_revised_query(user_query, task_queue)
                context = self._context_builder.build(
                    session_id=session_id,
                    user_query=revised_query,
                    model_name=self._model_name,
                )
                task_queue = self._planner.generate(
                    revised_query, self._model_name, context=context
                )
                state.plan = task_queue.action_steps
                self._append_action_plan(session_id, task_queue.action_steps)

        if state.done and self._should_synthesize_response(task_queue):
            tool_outputs = self._collect_tool_outputs(task_queue)
            response = self._synthesizer.synthesize(
                user_query=user_query,
                tool_outputs=tool_outputs,
                model_name=self._model_name,
                context=context,
            )
            task_queue.task_result = response
            self._session_store.append_event(
                session_id, {"type": "assistant", "payload": {"text": response}}
            )

        if not state.done:
            state.done_reason = "max_iterations_reached"

        self._session_store.append_event(
            session_id,
            {
                "type": "completion",
                "payload": {
                    "done": state.done,
                    "done_reason": state.done_reason,
                    "task_result": task_queue.task_result,
                },
            },
        )

        updated_summary = self._maybe_auto_compact(session_id)
        if updated_summary:
            state.summary = updated_summary

        return (task_queue, state) if return_state else task_queue

    def _run_action_plan(self, session_id: str, task_queue: TaskQueue) -> TaskQueue:
        reflector = StepReflector(self._model_name)
        runner = ActionPlanRunner(
            tool_registry=self._tool_registry,
            permission_policy=self._permission_policy,
            approval_callback=self._approval_callback,
            hook_manager=self._hook_manager,
            reflector=reflector,
            event_logger=lambda event: self._session_store.append_event(session_id, event),
        )
        return runner.run(task_queue)

    def _maybe_auto_compact(self, session_id: str) -> str | None:
        events = self._session_store.load_transcript(session_id)
        events = self._hook_manager.run_pre_compact(events)
        summary = self._session_store.load_summary(session_id)
        budget = get_token_budget(events, summary, self._model_name)
        if budget.needs_compact or should_compact(events):
            summary = summarize_events(events)
            self._session_store.save_summary(session_id, summary)
            return summary
        return None

    def _append_action_plan(self, session_id: str, steps: list[ActionStep]) -> None:
        payload_steps: list[ActionStepPayload] = [
            self._serialize_action_step(step) for step in steps
        ]
        self._session_store.append_event(
            session_id, {"type": "action_plan", "payload": {"steps": payload_steps}}
        )

    @staticmethod
    def _serialize_action_step(step: ActionStep) -> ActionStepPayload:
        payload: ActionStepPayload = {
            "action_consumer": step.action_consumer,
            "action_type": step.action_type,
            "action_argument": step.action_argument,
        }
        if step.title:
            payload["title"] = step.title
        if step.objective:
            payload["objective"] = step.objective
        if step.execution_checklist:
            payload["execution_checklist"] = step.execution_checklist
        if step.expected_output:
            payload["expected_output"] = step.expected_output
        return payload

    @staticmethod
    def _should_update_summary(text: str) -> bool:
        lowered = text.lower()
        keywords = [
            "remember",
            "note this",
            "save this",
            "pin this",
            "keep this",
            "magic number",
            "magic numbers",
        ]
        return any(keyword in lowered for keyword in keywords)

    def _update_summary_with_memory(self, session_id: str, text: str) -> str:
        summary = self._session_store.load_summary(session_id) or ""
        new_line = f"Memory: {text}"
        lines = [line for line in summary.splitlines() if line.strip()] if summary else []
        if new_line not in lines:
            lines.append(new_line)
        updated = "\n".join(lines[-10:]).strip()
        self._session_store.save_summary(session_id, updated)
        return updated

    @staticmethod
    def _build_direct_response(message: str) -> TaskQueue:
        task_queue = TaskQueue(action_steps=[])
        task_queue.task_result = message
        return task_queue

    @staticmethod
    def _collect_tool_outputs(task_queue: TaskQueue) -> list[str]:
        outputs: list[str] = []
        for step in task_queue.action_steps:
            if step.result is None:
                continue
            content = getattr(step.result, "content", step.result)
            outputs.append(str(content))
        return outputs

    @staticmethod
    def _should_synthesize_response(task_queue: TaskQueue) -> bool:
        if not task_queue.action_steps:
            return True
        return bool(Orchestrator._collect_tool_outputs(task_queue))

    @staticmethod
    def _action_steps_complete(task_queue: TaskQueue) -> bool:
        return all(step.result is not None for step in task_queue.action_steps)

    @staticmethod
    def _build_revised_query(user_query: str, task_queue: TaskQueue) -> str:
        failure_note = (
            f"Last tool failure: {task_queue.last_error}\n" if task_queue.last_error else ""
        )
        return (
            f"{user_query}\n\nPrevious tool results:\n{task_queue.task_result or ''}\n"
            f"{failure_note}"
            "Please revise the action plan to resolve remaining tasks."
        )


__all__ = ["Orchestrator"]
