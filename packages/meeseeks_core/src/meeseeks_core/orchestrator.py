#!/usr/bin/env python3
"""Session orchestration entrypoint."""

from __future__ import annotations

from collections.abc import Callable

from meeseeks_core.action_runner import ActionPlanRunner
from meeseeks_core.classes import ActionStep, OrchestrationState, Plan, PlanStep, TaskQueue
from meeseeks_core.common import get_logger, session_log_context
from meeseeks_core.compaction import should_compact, summarize_events
from meeseeks_core.components import langfuse_session_context
from meeseeks_core.config import get_config_value
from meeseeks_core.context import ContextBuilder
from meeseeks_core.hooks import HookManager, default_hook_manager
from meeseeks_core.permissions import (
    PermissionPolicy,
    approval_callback_from_config,
    load_permission_policy,
)
from meeseeks_core.planning import (
    Planner,
    PlanUpdater,
    ResponseSynthesizer,
    StepExecutor,
    ToolSelector,
)
from meeseeks_core.reflection import StepReflector
from meeseeks_core.session_store import SessionStore
from meeseeks_core.token_budget import get_token_budget
from meeseeks_core.tool_registry import ToolRegistry, ToolSpec, load_registry

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
        self._tool_selector = ToolSelector(self._tool_registry)
        self._step_executor = StepExecutor(self._tool_registry)
        self._plan_updater = PlanUpdater(self._tool_registry)
        self._synthesizer = ResponseSynthesizer(self._tool_registry)

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
    ) -> TaskQueue | tuple[TaskQueue, OrchestrationState]:
        """Run a plan-act-observe loop for a session."""
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
            plan = initial_plan
            tool_specs = (
                self._tool_registry.list_specs()
                if resolved_mode == "plan"
                else self._tool_registry.list_specs_for_mode("act")
            )
            if plan is None:
                if resolved_mode != "plan":
                    selection = self._tool_selector.select(
                        user_query,
                        self._model_name,
                        tool_specs=tool_specs,
                        context=context,
                    )
                    if selection.tool_required and selection.tool_ids:
                        selected_ids = self._expand_tool_ids(set(selection.tool_ids), tool_specs)
                        tool_specs = [spec for spec in tool_specs if spec.tool_id in selected_ids]
                plan = self._planner.generate(
                    user_query,
                    self._model_name,
                    context=context,
                    tool_specs=tool_specs,
                    mode=resolved_mode,
                )
            if resolved_mode != "plan" and plan and self._plan_needs_verification(plan):
                tool_specs = self._ensure_web_verification_tools(
                    tool_specs,
                    self._tool_registry.list_specs_for_mode("act"),
                )
            state.plan = plan.steps
            self._append_action_plan(session_id, plan.steps)

            task_queue = TaskQueue(plan_steps=plan.steps, action_steps=[])
            tool_outputs: list[str] = []
            executed_steps: list[ActionStep] = []
            completed_steps: list[PlanStep] = []
            remaining_steps: list[PlanStep] = list(plan.steps)
            last_error: str | None = None
            direct_response: str | None = None

            if resolved_mode == "plan":
                state.done = True
                state.done_reason = "planned"
            else:
                max_steps = max(0, max_iters) * 5
                steps_run = 0
                allowed_tool_ids = {spec.tool_id for spec in tool_specs}
                while remaining_steps and steps_run < max_steps:
                    if should_cancel is not None and should_cancel():
                        state.done = True
                        state.done_reason = "canceled"
                        break
                    current_step = remaining_steps.pop(0)
                    decision = self._step_executor.decide(
                        user_query,
                        current_step,
                        self._model_name,
                        allowed_tools=tool_specs,
                        context=context,
                    )
                    decision_type = (decision.decision or "").strip().lower()
                    if decision_type == "respond":
                        if decision.response:
                            direct_response = decision.response
                            tool_outputs.append(decision.response)
                        else:
                            last_error = "Step executor returned an empty response."
                            tool_outputs.append(f"ERROR: {last_error}")
                        completed_steps.append(current_step)
                        steps_run += 1
                        state.done = True
                        state.done_reason = "completed" if direct_response else "incomplete"
                        break
                    elif decision_type == "tool":
                        tool_id = str(decision.tool_id or "").strip()
                        if not tool_id or tool_id not in allowed_tool_ids:
                            last_error = f"Tool '{tool_id or 'unknown'}' not allowed for this step."
                            tool_outputs.append(f"ERROR: {last_error}")
                        else:
                            args = decision.args
                            if args is None:
                                args = ""
                            elif not isinstance(args, (dict, str)):
                                args = str(args)
                            action_step = self._build_action_step(
                                current_step,
                                tool_id,
                                args,
                            )
                            run_queue = TaskQueue(plan_steps=plan.steps, action_steps=[action_step])
                            run_queue = self._run_action_plan(
                                session_id,
                                run_queue,
                                mode=resolved_mode,
                                should_cancel=should_cancel,
                            )
                            executed_steps.extend(run_queue.action_steps)
                            if run_queue.task_result:
                                tool_outputs.append(run_queue.task_result)
                            if run_queue.last_error:
                                last_error = run_queue.last_error
                    else:
                        last_error = f"Invalid step decision: {decision.decision}"
                        tool_outputs.append(f"ERROR: {last_error}")

                    completed_steps.append(current_step)
                    steps_run += 1
                    if should_cancel is not None and should_cancel():
                        state.done = True
                        state.done_reason = "canceled"
                        break
                    if remaining_steps:
                        remaining_steps = self._plan_updater.update(
                            user_query,
                            self._model_name,
                            completed_step=current_step,
                            last_result=tool_outputs[-1] if tool_outputs else None,
                            remaining_steps=remaining_steps,
                            context=context,
                        )
                    state.plan = completed_steps + remaining_steps
                    self._append_action_plan(session_id, state.plan)

                if not state.done:
                    if remaining_steps and steps_run >= max_steps:
                        state.done_reason = "max_steps_reached"
                    elif last_error:
                        state.done_reason = (
                            "blocked" if "permission denied" in last_error.lower() else "incomplete"
                        )
                    else:
                        state.done_reason = "completed"
                    state.done = True

            task_queue.plan_steps = completed_steps + remaining_steps
            task_queue.action_steps = executed_steps
            task_queue.task_result = "\n".join(item for item in tool_outputs if item).strip()
            task_queue.last_error = last_error
            state.tool_results.extend(tool_outputs)

            if direct_response is not None and resolved_mode != "plan" and state.done:
                task_queue.task_result = direct_response
                self._session_store.append_event(
                    session_id, {"type": "assistant", "payload": {"text": direct_response}}
                )
            elif (
                state.done
                and resolved_mode != "plan"
                and self._should_synthesize_response(task_queue)
            ):
                tool_outputs = tool_outputs or self._collect_tool_outputs(task_queue)
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

            if not state.done:  # pragma: no cover - defensive guard
                state.done_reason = "max_iterations_reached"

            completion_payload = {
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

    def _run_action_plan(
        self,
        session_id: str,
        task_queue: TaskQueue,
        *,
        mode: str,
        should_cancel: Callable[[], bool] | None = None,
    ) -> TaskQueue:
        reflector = StepReflector(self._model_name)
        allowed_tools = None
        if mode == "plan":
            allowed_tools = {
                spec.tool_id for spec in self._tool_registry.list_specs_for_mode("plan")
            }
        runner = ActionPlanRunner(
            tool_registry=self._tool_registry,
            permission_policy=self._permission_policy,
            approval_callback=self._approval_callback,
            hook_manager=self._hook_manager,
            reflector=reflector,
            event_logger=lambda event: self._session_store.append_event(session_id, event),
            allowed_tool_ids=allowed_tools,
            mode=mode,
            should_cancel=should_cancel,
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

    def _append_action_plan(self, session_id: str, steps: list[PlanStep]) -> None:
        payload_steps = [self._serialize_plan_step(step) for step in steps]
        self._session_store.append_event(
            session_id, {"type": "action_plan", "payload": {"steps": payload_steps}}
        )

    @staticmethod
    def _serialize_plan_step(step: PlanStep) -> dict[str, str]:
        return {"title": step.title, "description": step.description}

    def _build_action_step(
        self,
        plan_step: PlanStep,
        tool_id: str,
        args: object | None,
    ) -> ActionStep:
        operation = self._infer_operation(tool_id)
        return ActionStep(
            title=plan_step.title,
            objective=plan_step.description,
            tool_id=tool_id,
            operation=operation,
            tool_input=args if args is not None else "",
        )

    @staticmethod
    def _infer_operation(tool_id: str) -> str:
        lowered = tool_id.lower()
        write_keywords = [
            "set",
            "edit",
            "write",
            "update",
            "delete",
            "create",
            "apply",
            "add",
            "remove",
            "patch",
            "insert",
            "append",
            "replace",
            "upload",
            "post",
            "put",
        ]
        if any(keyword in lowered for keyword in write_keywords):
            return "set"
        read_keywords = [
            "read",
            "list",
            "search",
            "get",
            "fetch",
            "query",
            "lookup",
            "web_search",
            "web_url_read",
        ]
        if any(keyword in lowered for keyword in read_keywords):
            return "get"
        return "get"

    @staticmethod
    def _expand_tool_ids(selected_ids: set[str], tool_specs: list[ToolSpec]) -> set[str]:
        if not selected_ids:
            return selected_ids
        lowered_selected = {tool_id.lower() for tool_id in selected_ids}
        has_web_search = any(
            key in tool_id
            for tool_id in lowered_selected
            for key in ("internet_search", "web_search", "searxng")
        )
        if has_web_search:
            for spec in tool_specs:
                tool_id = spec.tool_id.lower()
                if "web_url_read" in tool_id or "web_url" in tool_id or "web_read" in tool_id:
                    selected_ids.add(spec.tool_id)
        return selected_ids

    @staticmethod
    def _plan_needs_verification(plan: Plan) -> bool:
        keywords = ("open", "verify", "read", "source", "citation", "citations")
        for step in plan.steps:
            combined = f"{step.title} {step.description}".lower()
            if any(keyword in combined for keyword in keywords):
                return True
        return False

    @staticmethod
    def _ensure_web_verification_tools(
        selected: list[ToolSpec],
        all_specs: list[ToolSpec],
    ) -> list[ToolSpec]:
        existing = {spec.tool_id for spec in selected}
        needed = []
        for spec in all_specs:
            if spec.tool_id in existing:
                continue
            tool_id = spec.tool_id.lower()
            if (
                "internet_search" in tool_id
                or "web_search" in tool_id
                or "searxng" in tool_id
                or "web_url_read" in tool_id
                or "web_url" in tool_id
                or "web_read" in tool_id
            ):
                needed.append(spec)
        return selected + needed

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
        if outputs or not task_queue.last_error:
            return outputs
        return [f"ERROR: {task_queue.last_error}"]

    @staticmethod
    def _should_synthesize_response(task_queue: TaskQueue) -> bool:
        if not task_queue.action_steps:
            return True
        return bool(Orchestrator._collect_tool_outputs(task_queue))

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

    @staticmethod
    def _resolve_mode(user_query: str, mode: str | None) -> str:
        if mode in {"plan", "act"}:
            return mode
        lowered = user_query.strip().lower()
        plan_triggers = [
            "make a plan",
            "create a plan",
            "draft a plan",
            "plan the",
            "plan for",
            "planning",
        ]
        if any(trigger in lowered for trigger in plan_triggers):
            return "plan"
        return "act"

    @staticmethod
    def _should_replan(task_queue: TaskQueue, iteration: int, max_iters: int, *, mode: str) -> bool:
        if iteration >= max_iters - 1:
            return False
        if mode == "plan":
            return False
        if task_queue.last_error:
            lowered = task_queue.last_error.lower()
            if "permission denied" in lowered or "tool not allowed" in lowered:
                return False
        return True


__all__ = ["Orchestrator"]
