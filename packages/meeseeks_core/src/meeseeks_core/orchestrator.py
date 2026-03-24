#!/usr/bin/env python3
"""Session orchestration entrypoint."""

from __future__ import annotations

import asyncio
import queue
import threading
from collections.abc import Callable

from meeseeks_core.agent_context import AgentContext
from meeseeks_core.classes import ActionStep, OrchestrationState, Plan, PlanStep, TaskQueue
from meeseeks_core.common import discover_project_instructions, get_logger, session_log_context
from meeseeks_core.compaction import should_compact, summarize_events
from meeseeks_core.components import langfuse_session_context
from meeseeks_core.config import get_config_value
from meeseeks_core.context import ContextBuilder
from meeseeks_core.hooks import HookManager, default_hook_manager
from meeseeks_core.hypervisor import AgentHypervisor
from meeseeks_core.permissions import (
    PermissionPolicy,
    approval_callback_from_config,
    load_permission_policy,
)
from meeseeks_core.planning import Planner
from meeseeks_core.session_store import SessionStore
from meeseeks_core.skills import SkillRegistry, activate_skill
from meeseeks_core.token_budget import get_token_budget
from meeseeks_core.tool_registry import ToolRegistry, filter_specs, load_registry
from meeseeks_core.tool_use_loop import ToolUseLoop

logging = get_logger(name="core.orchestrator")


class Orchestrator:
    """Unified tool-use orchestration loop."""

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
            or get_config_value("llm", "action_plan_model")
            or get_config_value("llm", "default_model", default="gpt-5.2")
        )
        self._session_store = session_store or SessionStore()
        self._tool_registry = tool_registry or load_registry()
        self._permission_policy = permission_policy or load_permission_policy()
        self._approval_callback = approval_callback or approval_callback_from_config()
        self._hook_manager = hook_manager or default_hook_manager()
        self._context_builder = ContextBuilder(self._session_store)
        self._planner = Planner(self._tool_registry)
        self._project_instructions = discover_project_instructions()
        self._skill_registry = SkillRegistry()
        self._skill_registry.load()

    def run(
        self,
        user_query: str,
        *,
        max_iters: int = 3,
        initial_plan: Plan | None = None,
        return_state: bool = False,
        session_id: str | None = None,
        mode: str | None = None,
        should_cancel: Callable[[], bool] | None = None,
        allowed_tools: list[str] | None = None,
        skill_instructions: str | None = None,
        message_queue: queue.Queue[str] | None = None,
        interrupt_step: threading.Event | None = None,
    ) -> TaskQueue | tuple[TaskQueue, OrchestrationState]:
        """Run orchestration for a session."""
        if session_id is None:
            session_id = self._session_store.create_session()

        with session_log_context(session_id):
            with langfuse_session_context(session_id):
                return self._run_with_session_context(
                    user_query,
                    max_iters=max_iters,
                    initial_plan=initial_plan,
                    return_state=return_state,
                    session_id=session_id,
                    mode=mode,
                    should_cancel=should_cancel,
                    allowed_tools=allowed_tools,
                    skill_instructions=skill_instructions,
                    message_queue=message_queue,
                    interrupt_step=interrupt_step,
                )

    def _run_with_session_context(
        self,
        user_query: str,
        *,
        max_iters: int,
        initial_plan: Plan | None,
        return_state: bool,
        session_id: str,
        mode: str | None,
        should_cancel: Callable[[], bool] | None,
        allowed_tools: list[str] | None = None,
        skill_instructions: str | None = None,
        message_queue: queue.Queue[str] | None = None,
        interrupt_step: threading.Event | None = None,
    ) -> TaskQueue | tuple[TaskQueue, OrchestrationState]:
        """Run orchestration with Langfuse session context set."""
        state = OrchestrationState(goal=user_query, session_id=session_id)
        resolved_mode = self._resolve_mode(user_query, mode)
        state.summary = self._session_store.load_summary(session_id)
        state.tool_results = state.tool_results or []
        state.open_questions = state.open_questions or []
        task_queue: TaskQueue | None = None

        try:
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
            tool_specs = self._tool_registry.list_specs_for_mode(resolved_mode)
            if allowed_tools:
                tool_specs = filter_specs(tool_specs, allowed=allowed_tools)

            # Skill invocation detection and hot-reload.
            self._skill_registry.maybe_reload()
            if skill_instructions is None:
                _si, _ts = self._try_skill_invocation(user_query, tool_specs)
                if _si is not None:
                    skill_instructions = _si
                if _ts is not None:
                    tool_specs = _ts

            if resolved_mode == "plan":
                # Plan-only mode: generate plan, return without executing.
                plan = initial_plan or self._planner.generate(
                    user_query,
                    self._model_name,
                    context=context,
                    tool_specs=self._tool_registry.list_specs(),
                    mode="plan",
                    project_instructions=self._project_instructions,
                )
                state.plan = plan.steps
                state.done = True
                state.done_reason = "planned"
                self._append_action_plan(session_id, plan.steps)
                task_queue = TaskQueue(plan_steps=plan.steps, action_steps=[])
            else:
                # Act mode: run the async tool-use loop via the agent hypervisor.
                max_depth = int(
                    get_config_value("agent", "max_depth", default=5)
                )
                max_concurrent = int(
                    get_config_value("agent", "max_concurrent", default=20)
                )
                registry = AgentHypervisor(max_concurrent=max_concurrent)
                root_ctx = AgentContext.root(
                    model_name=self._model_name,
                    max_depth=max_depth,
                    should_cancel=should_cancel,
                    event_logger=lambda event: self._session_store.append_event(
                        session_id, event
                    ),
                    registry=registry,
                    message_queue=message_queue,
                    interrupt_step=interrupt_step,
                )
                loop = ToolUseLoop(
                    agent_context=root_ctx,
                    tool_registry=self._tool_registry,
                    permission_policy=self._permission_policy,
                    approval_callback=self._approval_callback,
                    hook_manager=self._hook_manager,
                    project_instructions=self._project_instructions,
                    skill_instructions=skill_instructions,
                    skill_registry=self._skill_registry,
                )
                max_steps = max(1, max_iters) * 3
                try:
                    task_queue, state = asyncio.run(
                        loop.run(
                            user_query,
                            tool_specs=tool_specs,
                            context=context,
                            max_steps=max_steps,
                            plan=initial_plan,
                        )
                    )
                finally:
                    # Belt-and-suspenders: ensure all agents cleaned up.
                    try:
                        asyncio.run(registry.cleanup(timeout=5.0))
                    except Exception:
                        pass
                state.session_id = session_id

            # Emit assistant response event.
            if task_queue.task_result and resolved_mode != "plan":
                self._session_store.append_event(
                    session_id,
                    {"type": "assistant", "payload": {"text": task_queue.task_result}},
                )

            if not state.done:  # pragma: no cover - defensive guard
                state.done = True
                state.done_reason = "max_iterations_reached"

            completion_payload: dict[str, object] = {
                "done": state.done,
                "done_reason": state.done_reason,
                "task_result": task_queue.task_result,
            }
            if task_queue.last_error:
                completion_payload["error"] = task_queue.last_error
                completion_payload["last_error"] = task_queue.last_error
            self._session_store.append_event(
                session_id,
                {"type": "completion", "payload": completion_payload},
            )

            updated_summary = self._maybe_auto_compact(session_id)
            if updated_summary:
                state.summary = updated_summary

            return (task_queue, state) if return_state else task_queue
        except Exception as exc:
            logging.exception("Orchestration failed for session {}", session_id)
            if task_queue is None:
                task_queue = TaskQueue(_human_message=user_query, action_steps=[])
            task_queue.last_error = str(exc)
            state.done = True
            state.done_reason = "error"
            self._session_store.append_event(
                session_id,
                {
                    "type": "completion",
                    "payload": {
                        "done": True,
                        "done_reason": state.done_reason,
                        "task_result": task_queue.task_result,
                        "error": str(exc),
                        "last_error": str(exc),
                    },
                },
            )
            return (task_queue, state) if return_state else task_queue

    # ------------------------------------------------------------------
    # Session helpers (kept from original)
    # ------------------------------------------------------------------

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

    def _append_action_plan(self, session_id: str, steps: list[PlanStep]) -> None:
        payload_steps = [{"title": step.title, "description": step.description} for step in steps]
        self._session_store.append_event(
            session_id, {"type": "action_plan", "payload": {"steps": payload_steps}}
        )

    @staticmethod
    def _should_update_summary(text: str) -> bool:
        lowered = text.lower()
        keywords = [
            "remember", "note this", "save this", "pin this",
            "keep this", "magic number", "magic numbers",
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

    def _try_skill_invocation(
        self,
        user_query: str,
        tool_specs: list,
    ) -> tuple[str | None, list | None]:
        """Detect ``/skill-name args`` in the query and activate the skill.

        Returns ``(skill_instructions, scoped_tool_specs)`` on match,
        or ``(None, None)`` if the query is not a skill invocation.
        """
        query = user_query.strip()
        if not query.startswith("/"):
            return None, None

        parts = query.split(None, 1)
        name = parts[0].lstrip("/")
        args = parts[1] if len(parts) > 1 else ""

        skill = self._skill_registry.get(name)
        if skill is None:
            return None, None

        logging.info("Activating skill '{}' with args '{}'", name, args)
        instructions, scoped_specs = activate_skill(skill, args, tool_specs)
        return instructions, scoped_specs

    @staticmethod
    def _resolve_mode(user_query: str, mode: str | None) -> str:
        if mode in {"plan", "act"}:
            return mode
        lowered = user_query.strip().lower()
        plan_triggers = [
            "make a plan", "create a plan", "draft a plan",
            "plan the", "plan for", "planning",
        ]
        if any(trigger in lowered for trigger in plan_triggers):
            return "plan"
        return "act"


__all__ = ["Orchestrator"]
