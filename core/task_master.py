#!/usr/bin/env python3
"""Task planning and orchestration loop for Meeseeks."""

# Standard library modules
import os
import warnings
from collections.abc import Callable
from typing import Literal, cast

from dotenv import load_dotenv
from langchain_core._api.beta_decorator import LangChainBetaWarning
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.messages.ai import AIMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate
from pydantic.v1 import BaseModel, Field

from core.classes import ActionStep, OrchestrationState, TaskQueue, get_task_master_examples
from core.common import (
    get_logger,
    get_mock_speaker,
    get_system_prompt,
    get_unique_timestamp,
    num_tokens_from_string,
)
from core.compaction import should_compact, summarize_events
from core.components import (
    ComponentStatus,
    build_langfuse_handler,
    format_component_status,
    resolve_langfuse_status,
)
from core.hooks import HookManager, default_hook_manager
from core.llm import build_chat_model
from core.permissions import (
    PermissionDecision,
    PermissionPolicy,
    approval_callback_from_env,
    load_permission_policy,
)
from core.session_store import SessionStore
from core.token_budget import get_token_budget
from core.tool_registry import ToolRegistry, load_registry
from core.types import ActionStepPayload, Event, EventRecord

logging = get_logger(name="core.task_master")

# C0116,
# Filter out LangChainBetaWarning specifically
warnings.simplefilter("ignore", LangChainBetaWarning)
load_dotenv()


def _event_payload_text(event: EventRecord) -> str:
    payload = event.get("payload", "")
    if isinstance(payload, dict):
        return str(
            payload.get("text")
            or payload.get("message")
            or payload.get("result")
            or payload
        )
    return str(payload)


def _render_event_lines(events: list[EventRecord]) -> str:
    lines: list[str] = []
    for event in events:
        event_type = event.get("type", "event")
        text = _event_payload_text(event)
        if not text:
            continue
        lines.append(f"- {event_type}: {text}")
    return "\n".join(lines).strip()


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


def _update_summary_with_memory(
    session_store: SessionStore,
    session_id: str,
    text: str,
) -> str:
    summary = session_store.load_summary(session_id) or ""
    new_line = f"Memory: {text}"
    lines = [line for line in summary.splitlines() if line.strip()] if summary else []
    if new_line not in lines:
        lines.append(new_line)
    lines = lines[-10:]
    updated = "\n".join(lines).strip()
    session_store.save_summary(session_id, updated)
    return updated


class ContextSelection(BaseModel):
    """Model output for selecting context events."""
    keep_ids: list[int] = Field(default_factory=list)
    drop_ids: list[int] = Field(default_factory=list)


def _select_context_events(
    events: list[EventRecord],
    user_query: str,
    model_name: str | None,
) -> list[EventRecord]:
    if not events:
        return []
    selector_model = (
        os.getenv("CONTEXT_SELECTOR_MODEL")
        or model_name
        or os.getenv("ACTION_PLAN_MODEL")
        or os.getenv("DEFAULT_MODEL")
    )
    if not selector_model:
        return events
    parser = PydanticOutputParser(pydantic_object=ContextSelection)  # type: ignore[type-var]
    prompt = ChatPromptTemplate(
        messages=[
            SystemMessage(
                content=(
                    "You select which prior events are still relevant to the user's "
                    "current request. Keep only events that directly help answer the "
                    "current query. If unsure, keep the event."
                )
            ),
            HumanMessagePromptTemplate.from_template(
                "User query:\n{user_query}\n\n"
                "Candidate events:\n{candidates}\n\n"
                "Return keep_ids and drop_ids.\n{format_instructions}"
            ),
        ],
        partial_variables={"format_instructions": parser.get_format_instructions()},
        input_variables=["user_query", "candidates"],
    )
    lines: list[str] = []
    for idx, event in enumerate(events, start=1):
        event_type = event.get("type", "event")
        text = _event_payload_text(event)
        if not text:
            continue
        lines.append(f"{idx}. {event_type}: {text}")
    candidates_text = "\n".join(lines).strip()
    if not candidates_text:
        return events
    model = build_chat_model(
        model_name=selector_model,
        temperature=0.0,
        openai_api_base=os.getenv("OPENAI_API_BASE"),
    )
    try:
        selection = (prompt | model | parser).invoke(
            {"user_query": user_query.strip(), "candidates": candidates_text}
        )
    except Exception as exc:  # pragma: no cover - defensive
        logging.warning("Context selection failed: {}", exc)
        return events[-3:]

    keep_ids = set(selection.keep_ids or [])
    if not keep_ids:
        return events[-3:]
    kept: list[EventRecord] = []
    for idx, event in enumerate(events, start=1):
        if idx in keep_ids:
            kept.append(event)
    return kept or events[-3:]


class StepReflection(BaseModel):
    """Model output for step-level reflection."""
    status: Literal["ok", "retry", "revise"] = Field(default="ok")
    notes: str | None = None
    revised_argument: str | None = None


def _reflect_on_step(
    action_step: ActionStep,
    result_text: str,
    model_name: str | None,
) -> StepReflection | None:
    if not (
        action_step.objective
        or action_step.expected_output
        or action_step.execution_checklist
    ):
        return None
    if os.getenv("MEESEEKS_STEP_REFLECTION", "1") == "0":
        return None
    reflection_model = (
        os.getenv("STEP_REFLECTION_MODEL")
        or model_name
        or os.getenv("ACTION_PLAN_MODEL")
        or os.getenv("DEFAULT_MODEL")
    )
    if not reflection_model:
        return None
    parser = PydanticOutputParser(pydantic_object=StepReflection)  # type: ignore[type-var]
    prompt = ChatPromptTemplate(
        messages=[
            SystemMessage(
                content=(
                    "Reflect on whether the tool result satisfies the step objective. "
                    "Return status 'ok' if complete, 'retry' if the step should be "
                    "re-executed, or 'revise' if the action argument needs adjustment."
                )
            ),
            HumanMessagePromptTemplate.from_template(
                "Step title: {title}\n"
                "Objective: {objective}\n"
                "Checklist: {checklist}\n"
                "Expected output: {expected}\n"
                "Tool result: {result}\n\n"
                "{format_instructions}"
            ),
        ],
        partial_variables={"format_instructions": parser.get_format_instructions()},
        input_variables=["title", "objective", "checklist", "expected", "result"],
    )
    model = build_chat_model(
        model_name=reflection_model,
        temperature=0.0,
        openai_api_base=os.getenv("OPENAI_API_BASE"),
    )
    try:
        return (prompt | model | parser).invoke(
            {
                "title": action_step.title or action_step.action_consumer,
                "objective": action_step.objective or action_step.action_argument,
                "checklist": "; ".join(action_step.execution_checklist or []),
                "expected": action_step.expected_output or "Not specified",
                "result": result_text,
            }
        )
    except Exception as exc:  # pragma: no cover - defensive
        logging.warning("Step reflection failed: {}", exc)
        return None


def _build_direct_response(message: str) -> TaskQueue:
    """Create a TaskQueue with a direct talk-to-user response.

    Args:
        message: Plaintext response to return to the user.

    Returns:
        TaskQueue populated with a single talk-to-user action and result.
    """
    step = ActionStep(
        action_consumer="talk_to_user_tool",
        action_type="set",
        action_argument=message,
    )
    task_queue = TaskQueue(action_steps=[step])
    MockSpeaker = get_mock_speaker()
    step.result = MockSpeaker(content=message)
    task_queue.task_result = message
    return task_queue


def _augment_system_prompt(
    system_prompt: str,
    tool_registry: ToolRegistry | None,
    session_summary: str | None = None,
    recent_events: list[EventRecord] | None = None,
    selected_events: list[EventRecord] | None = None,
    component_status: list[ComponentStatus] | None = None,
) -> str:
    """Append tool catalog and session summary information to a base prompt.

    Args:
        system_prompt: Base system prompt content.
        tool_registry: Optional registry used to add tool descriptions.
        session_summary: Optional summarized context for the session.
        recent_events: Optional recent events to inject for short-term context.
        selected_events: Optional selected events to inject as relevant context.
        component_status: Optional component status list to include.

    Returns:
        Augmented prompt string including tool catalog and summary context.
    """
    sections = [system_prompt]
    if session_summary:
        sections.append(f"Session summary:\n{session_summary}")
    if selected_events:
        rendered = _render_event_lines(selected_events)
        if rendered:
            sections.append("Relevant earlier context:\n" + rendered)
    if recent_events:
        rendered = _render_event_lines(recent_events)
        if rendered:
            sections.append("Recent conversation:\n" + rendered)
    if tool_registry is not None:
        catalog = tool_registry.tool_catalog()
        if catalog:
            tool_lines = "\n".join(
                f"- {tool['tool_id']}: {tool['description']}" for tool in catalog
            )
            sections.append(f"Available tools:\n{tool_lines}")
        tool_prompts: list[str] = []
        for spec in tool_registry.list_specs():
            if not spec.prompt_path:
                continue
            try:
                tool_prompt = get_system_prompt(spec.prompt_path)
            except OSError as exc:
                logging.warning(
                    "Failed to load tool prompt for {}: {}", spec.tool_id, exc
                )
                continue
            if tool_prompt:
                tool_prompts.append(tool_prompt)
        if tool_prompts:
            sections.append("Tool guidance:\n" + "\n\n".join(tool_prompts))
    if component_status:
        sections.append(
            "Component status:\n" + format_component_status(component_status)
        )
    return "\n\n".join(sections)


def generate_action_plan(
    user_query: str,
    model_name: str | None = None,
    tool_registry: ToolRegistry | None = None,
    session_summary: str | None = None,
    recent_events: list[EventRecord] | None = None,
    selected_events: list[EventRecord] | None = None,
) -> TaskQueue:
    """Use the LangChain pipeline to generate an action plan.

    Args:
        user_query: User request to transform into an action plan.
        model_name: Optional model override for planning.
        tool_registry: Optional registry used to list available tools.
        session_summary: Optional summary to provide prior context.
        recent_events: Optional recent events to inject into the prompt.
        selected_events: Optional selected events to inject into the prompt.

    Returns:
        TaskQueue containing the ordered action plan.

    Raises:
        ValueError: If model configuration is invalid.
    """
    if tool_registry is None:
        tool_registry = load_registry()

    user_id = "meeseeks-task-master"
    session_id = f"action-queue-id-{get_unique_timestamp()}"
    trace_name = user_id
    version = os.getenv("VERSION", "Not Specified")
    release = os.getenv("ENVMODE", "Not Specified")

    langfuse_handler = build_langfuse_handler(
        user_id=user_id,
        session_id=session_id,
        trace_name=trace_name,
        version=version,
        release=release,
    )

    model_name = cast(
        str,
        model_name
        or os.getenv("ACTION_PLAN_MODEL")
        or os.getenv("DEFAULT_MODEL", "gpt-3.5-turbo"),
    )

    model = build_chat_model(
        model_name=model_name,
        temperature=0.4,
        openai_api_base=os.getenv("OPENAI_API_BASE"),
    )

    parser = PydanticOutputParser(pydantic_object=TaskQueue)  # type: ignore[type-var]
    logging.debug(
        "Generating action plan <model='{}'; user_query='{}'>", model_name, user_query)

    component_status: list[ComponentStatus] = [resolve_langfuse_status()]
    if tool_registry is not None:
        for spec in tool_registry.list_specs(include_disabled=True):
            component_status.append(
                ComponentStatus(
                    name=f"tool:{spec.tool_id}",
                    enabled=spec.enabled,
                    reason=spec.metadata.get("disabled_reason"),
                )
            )

    available_tool_ids = [
        spec.tool_id for spec in tool_registry.list_specs()
    ]
    prompt = ChatPromptTemplate(
        messages=[
            SystemMessage(
                content=_augment_system_prompt(
                    get_system_prompt(),
                    tool_registry,
                    session_summary=session_summary,
                    recent_events=recent_events,
                    selected_events=selected_events,
                    component_status=component_status,
                )
            ),
            HumanMessage(content="Turn on strip lights and heater."),
            AIMessage(
                content=get_task_master_examples(
                    example_id=0,
                    available_tools=available_tool_ids,
                )
            ),
            HumanMessage(content="What is the weather today?"),
            AIMessage(
                content=get_task_master_examples(
                    example_id=1,
                    available_tools=available_tool_ids,
                )
            ),
            HumanMessagePromptTemplate.from_template(
                
                    "## Format Instructions\n{format_instructions}\n"
                    "## Generate a task queue for the user query\n{user_query}"
                
            ),
        ],
        partial_variables={
            "format_instructions": parser.get_format_instructions()
        },
        input_variables=["user_query"]
    )

    estimator = num_tokens_from_string(str(prompt))
    logging.info("Input Prompt Token length is `{}`.", estimator)

    config: dict[str, object] = {}
    if langfuse_handler is not None:
        config["callbacks"] = [langfuse_handler]

    action_plan = (prompt | model | parser).invoke(
        {"user_query": user_query.strip()},
        config=config or None,
    )

    action_plan.human_message = user_query
    logging.info("Action plan generated <{}>", action_plan)
    return action_plan


def run_action_plan(
    task_queue: TaskQueue,
    tool_registry: ToolRegistry | None = None,
    event_logger: Callable[[Event], None] | None = None,
    permission_policy: PermissionPolicy | None = None,
    approval_callback: Callable[[ActionStep], bool] | None = None,
    hook_manager: HookManager | None = None,
    model_name: str | None = None,
) -> TaskQueue:
    """Execute the generated action plan with permission checks.

    Args:
        task_queue: The action plan to run.
        tool_registry: Optional registry used to resolve tools.
        event_logger: Optional callback to emit orchestration events.
        permission_policy: Optional policy to decide tool permissions.
        approval_callback: Optional human approval callback.
        hook_manager: Optional hook manager for lifecycle callbacks.
        model_name: Optional model name for step reflection.

    Returns:
        Updated TaskQueue with action results.
    """
    if tool_registry is None:
        tool_registry = load_registry()
    if permission_policy is None:
        permission_policy = load_permission_policy()
    if approval_callback is None:
        approval_callback = approval_callback_from_env()
    if hook_manager is None:
        hook_manager = default_hook_manager()

    results: list[str] = []

    for idx, action_step in enumerate(task_queue.action_steps):
        logging.debug("Processing ActionStep: {}", action_step)
        decision = permission_policy.decide(action_step)
        decision = hook_manager.run_permission_request(action_step, decision)
        decision_logged = False
        if decision == PermissionDecision.ASK:
            approved = approval_callback(action_step) if approval_callback else False
            decision = PermissionDecision.ALLOW if approved else PermissionDecision.DENY
            if event_logger is not None:
                event_logger(
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
            MockSpeaker = get_mock_speaker()
            message = (
                "Permission denied for "
                f"{action_step.action_consumer}:{action_step.action_type}."
            )
            action_step.result = MockSpeaker(content=message)
            results.append(message)
            if event_logger is not None and not decision_logged:
                event_logger(
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
            continue

        action_step = hook_manager.run_pre_tool_use(action_step)
        task_queue.action_steps[idx] = action_step
        tool = tool_registry.get(action_step.action_consumer)

        if tool is None:
            logging.error(
                "No tool found for consumer: {}", action_step.action_consumer
            )
            continue

        try:
            action_result = tool.run(action_step)
            action_result = hook_manager.run_post_tool_use(action_step, action_result)
            action_step.result = action_result
            content = getattr(action_result, "content", None)
            if content is None:
                content = "" if action_result is None else str(action_result)

            reflection = _reflect_on_step(action_step, content, model_name)
            if reflection is not None and reflection.status != "ok":
                if reflection.revised_argument:
                    action_step.action_argument = reflection.revised_argument
                action_step.result = None
                if event_logger is not None:
                    event_logger(
                        {
                            "type": "step_reflection",
                            "payload": {
                                "action_consumer": action_step.action_consumer,
                                "action_type": action_step.action_type,
                                "action_argument": action_step.action_argument,
                                "status": reflection.status,
                                "notes": reflection.notes,
                            },
                        }
                    )
                task_queue.action_steps[idx] = action_step
                break

            results.append(content)
            if event_logger is not None:
                event_logger(
                    {
                        "type": "tool_result",
                        "payload": {
                            "action_consumer": action_step.action_consumer,
                            "action_type": action_step.action_type,
                            "action_argument": action_step.action_argument,
                            "result": content,
                        },
                    }
                )
        except Exception as e:
            logging.error("Error processing action step: {}", e)
            tool_registry.disable(action_step.action_consumer, f"Runtime error: {e}")
            action_step.result = None
            if event_logger is not None:
                event_logger(
                    {
                        "type": "tool_result",
                        "payload": {
                            "action_consumer": action_step.action_consumer,
                            "action_type": action_step.action_type,
                            "action_argument": action_step.action_argument,
                            "result": None,
                            "error": str(e),
                        },
                    }
                )

    task_queue.task_result = " ".join(results).strip()

    return task_queue


def _action_steps_complete(task_queue: TaskQueue) -> bool:
    """Return True if all action steps have completed with results.

    Args:
        task_queue: Queue to inspect for completion.

    Returns:
        True when every action has a non-null result.
    """
    return all(step.result is not None for step in task_queue.action_steps)


def _maybe_auto_compact(
    session_store: SessionStore,
    session_id: str,
    model_name: str | None,
    hook_manager: HookManager,
) -> str | None:
    """Compact and persist session history when it exceeds token thresholds.

    Args:
        session_store: Store used for transcript and summary persistence.
        session_id: Unique session identifier.
        model_name: Optional model name to estimate context window size.
        hook_manager: Hook manager to pre-process events.

    Returns:
        Updated summary string if compaction occurred; otherwise None.
    """
    events = session_store.load_transcript(session_id)
    events = hook_manager.run_pre_compact(events)
    summary = session_store.load_summary(session_id)
    budget = get_token_budget(events, summary, model_name)
    if budget.needs_compact or should_compact(events):
        summary = summarize_events(events)
        session_store.save_summary(session_id, summary)
        return summary
    return None


def orchestrate_session(
    user_query: str,
    model_name: str | None = None,
    max_iters: int = 3,
    initial_task_queue: TaskQueue | None = None,
    return_state: bool = False,
    session_id: str | None = None,
    session_store: SessionStore | None = None,
    tool_registry: ToolRegistry | None = None,
    permission_policy: PermissionPolicy | None = None,
    approval_callback: Callable[[ActionStep], bool] | None = None,
    hook_manager: HookManager | None = None,
) -> TaskQueue | tuple[TaskQueue, OrchestrationState]:
    """Orchestrate a session using a plan-act-observe-decide loop.

    Args:
        user_query: User input that initiates the orchestration cycle.
        model_name: Optional model override for planning.
        max_iters: Maximum planning iterations before giving up.
        initial_task_queue: Optional pre-computed task queue.
        return_state: Whether to return orchestration state along with results.
        session_id: Optional existing session identifier.
        session_store: Optional session store for transcript persistence.
        tool_registry: Optional tool registry for resolution.
        permission_policy: Optional permission policy override.
        approval_callback: Optional approval callback for ASK decisions.
        hook_manager: Optional hook manager for lifecycle callbacks.

    Returns:
        TaskQueue alone, or a tuple of TaskQueue and OrchestrationState.

    Raises:
        ValueError: If user_query is invalid.
    """
    if tool_registry is None:
        tool_registry = load_registry()

    resolved_model_name = (
        model_name
        or os.getenv("ACTION_PLAN_MODEL")
        or os.getenv("DEFAULT_MODEL", "gpt-3.5-turbo")
    )

    if session_store is None:
        session_store = SessionStore()

    if session_id is None:
        session_id = session_store.create_session()

    state = OrchestrationState(goal=user_query, session_id=session_id)
    state.summary = session_store.load_summary(session_id)
    if state.tool_results is None:
        state.tool_results = []
    if state.open_questions is None:
        state.open_questions = []
    if hook_manager is None:
        hook_manager = default_hook_manager()

    session_store.append_event(
        session_id, {"type": "user", "payload": {"text": user_query}}
    )

    if _should_update_summary(user_query):
        state.summary = _update_summary_with_memory(
            session_store,
            session_id,
            user_query.strip(),
        )

    updated_summary = _maybe_auto_compact(
        session_store,
        session_id,
        resolved_model_name,
        hook_manager,
    )
    if updated_summary:
        state.summary = updated_summary

    if user_query.strip() == "/compact":
        events = session_store.load_transcript(session_id)
        summary = summarize_events(events)
        session_store.save_summary(session_id, summary)
        state.summary = summary
        state.done = True
        state.done_reason = "compacted"
        task_queue = _build_direct_response(
            f"Compaction complete. Summary: {summary}"
        )
        if return_state:
            return task_queue, state
        return task_queue

    recent_limit = int(os.getenv("MEESEEKS_RECENT_EVENT_LIMIT", "8"))
    events = session_store.load_transcript(session_id)
    context_events = [
        event
        for event in events
        if event.get("type") in {"user", "tool_result"}
    ]
    recent_events = context_events[-recent_limit:] if recent_limit > 0 else []
    candidate_events = (
        context_events[:-recent_limit] if recent_limit > 0 else context_events
    )
    selected_events: list[EventRecord] | None = None
    budget = get_token_budget(events, state.summary, resolved_model_name)
    selection_threshold = float(os.getenv("MEESEEKS_CONTEXT_SELECT_THRESHOLD", "0.8"))
    if (
        os.getenv("MEESEEKS_CONTEXT_SELECTION", "1") != "0"
        and candidate_events
        and budget.utilization >= selection_threshold
    ):
        selected_events = _select_context_events(
            candidate_events,
            user_query=user_query,
            model_name=resolved_model_name,
        )

    if initial_task_queue is None:
        task_queue = generate_action_plan(
            user_query=user_query,
            model_name=resolved_model_name,
            tool_registry=tool_registry,
            session_summary=state.summary,
            recent_events=recent_events,
            selected_events=selected_events,
        )
    else:
        task_queue = initial_task_queue
    state.plan = task_queue.action_steps
    steps: list[ActionStepPayload] = [
        _serialize_action_step(step) for step in task_queue.action_steps
    ]
    session_store.append_event(
        session_id,
        {
            "type": "action_plan",
            "payload": {"steps": steps},
        },
    )

    for iteration in range(max_iters):
        task_queue = run_action_plan(
            task_queue,
            tool_registry=tool_registry,
            event_logger=lambda event: session_store.append_event(session_id, event),
            permission_policy=permission_policy,
            approval_callback=approval_callback,
            hook_manager=hook_manager,
            model_name=resolved_model_name,
        )
        state.tool_results.append(task_queue.task_result or "")

        if _action_steps_complete(task_queue):
            state.done = True
            state.done_reason = "completed"
            break

        if iteration < max_iters - 1:
            revised_query = (
                f"{user_query}\n\nPrevious tool results:\n{task_queue.task_result or ''}\n"
                "Please revise the action plan to resolve remaining tasks."
            )
            events = session_store.load_transcript(session_id)
            context_events = [
                event
                for event in events
                if event.get("type") in {"user", "tool_result"}
            ]
            recent_events = context_events[-recent_limit:] if recent_limit > 0 else []
            candidate_events = (
                context_events[:-recent_limit] if recent_limit > 0 else context_events
            )
            selected_events = None
            budget = get_token_budget(events, state.summary, resolved_model_name)
            if (
                os.getenv("MEESEEKS_CONTEXT_SELECTION", "1") != "0"
                and candidate_events
                and budget.utilization >= selection_threshold
            ):
                selected_events = _select_context_events(
                    candidate_events,
                    user_query=revised_query,
                    model_name=resolved_model_name,
                )
            task_queue = generate_action_plan(
                user_query=revised_query,
                model_name=resolved_model_name,
                tool_registry=tool_registry,
                session_summary=state.summary,
                recent_events=recent_events,
                selected_events=selected_events,
            )
            state.plan = task_queue.action_steps
            revised_steps: list[ActionStepPayload] = [
                _serialize_action_step(step) for step in task_queue.action_steps
            ]
            session_store.append_event(
                session_id,
                {
                    "type": "action_plan",
                    "payload": {"steps": revised_steps},
                },
            )

    if not state.done:
        state.done_reason = "max_iterations_reached"

    session_store.append_event(
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

    updated_summary = _maybe_auto_compact(
        session_store,
        session_id,
        resolved_model_name,
        hook_manager,
    )
    if updated_summary:
        state.summary = updated_summary

    if return_state:
        return task_queue, state

    return task_queue
