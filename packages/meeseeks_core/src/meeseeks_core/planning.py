#!/usr/bin/env python3
"""Prompt construction and planning helpers."""

from __future__ import annotations

import os
from collections.abc import Iterable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate

from meeseeks_core.classes import Plan, get_task_master_examples
from meeseeks_core.common import get_logger, get_system_prompt, num_tokens_from_string
from meeseeks_core.components import (
    ComponentStatus,
    build_langfuse_handler,
    format_component_status,
    langfuse_trace_span,
    resolve_langfuse_status,
)
from meeseeks_core.config import get_config_value, get_version
from meeseeks_core.context import ContextSnapshot, render_event_lines
from meeseeks_core.llm import build_chat_model
from meeseeks_core.tool_registry import ToolRegistry, ToolSpec

logging = get_logger(name="core.planning")
EXAMPLE_TAG_OPEN = '<example desc="Illustrative only; not part of the live conversation">'
EXAMPLE_TAG_CLOSE = "</example>"
INTENT_KEYWORDS: dict[str, set[str]] = {
    "web": {
        "latest", "current", "today", "now", "verify", "official", "news",
        "fetch", "lookup", "look up", "search the web", "web search", "internet",
    },
    "file": {
        "file", "edit", "write", "create", "script", "patch", "diff", "repo",
        "directory", "folder", "pwd", "local", "workspace",
    },
    "home": {
        "home assistant", "ha", "device", "light", "switch", "sensor", "climate",
    },
    "shell": {
        "shell", "command", "run", "execute", "terminal", "cli",
    },
}
INTENT_CAPABILITIES: dict[str, set[str]] = {
    "web": {"web_search", "web_read"},
    "file": {"file_read", "file_write"},
    "home": {"home_assistant"},
    "shell": {"shell_exec"},
}


class PromptBuilder:
    """Build system prompts with contextual sections."""

    def __init__(self, tool_registry: ToolRegistry | None) -> None:
        """Initialize prompt builder dependencies."""
        self._tool_registry = tool_registry

    def build(
        self,
        base_prompt: str,
        context: ContextSnapshot | None,
        component_status: Iterable[ComponentStatus] | None = None,
        *,
        mode: str = "act",
        tool_specs=None,
        include_tool_schemas: bool = True,
        include_tool_guidance: bool = True,
        project_instructions: str | None = None,
    ) -> str:
        """Build an augmented system prompt string."""
        sections = [base_prompt]
        if project_instructions:
            sections.append(f"Project instructions:\n{project_instructions}")
        if context and context.summary:
            sections.append(f"Session summary:\n{context.summary}")
        if context and context.selected_events:
            rendered = render_event_lines(context.selected_events)
            if rendered:
                sections.append("Relevant earlier context:\n" + rendered)
        if context and context.recent_events:
            rendered = render_event_lines(context.recent_events)
            if rendered:
                sections.append("Recent conversation:\n" + rendered)
        if self._tool_registry is not None:
            specs = tool_specs or self._tool_registry.list_specs()
            if specs:
                tool_lines = "\n".join(f"- {spec.tool_id}: {spec.description}" for spec in specs)
                sections.append(f"Available tools:\n{tool_lines}")
            if mode == "act":
                if include_tool_guidance:
                    tool_prompts = self._render_tool_prompts(specs, local_only=True)
                    if tool_prompts:
                        sections.append("Tool guidance:\n" + "\n\n".join(tool_prompts))
        if component_status:
            sections.append("Component status:\n" + format_component_status(component_status))
        return "\n\n".join(sections)

    @staticmethod
    def _render_tool_prompts(specs, *, local_only: bool = False) -> list[str]:
        prompts: list[str] = []
        for spec in specs:
            if not spec.prompt_path:
                continue
            if local_only and spec.kind != "local":
                continue
            try:
                tool_prompt = get_system_prompt(spec.prompt_path)
            except OSError as exc:
                logging.warning("Failed to load tool prompt for {}: {}", spec.tool_id, exc)
                continue
            if tool_prompt:
                prompts.append(tool_prompt)
        return prompts


class Planner:
    """Generate action plans via LLM."""

    def __init__(self, tool_registry: ToolRegistry | None) -> None:
        """Initialize the planner."""
        self._tool_registry = tool_registry
        self._prompt_builder = PromptBuilder(tool_registry)

    @staticmethod
    def _build_example_messages(available_tool_ids: list[str], *, mode: str) -> list[BaseMessage]:
        if mode != "plan":
            return []

        def wrap(text: str) -> str:
            return f"{EXAMPLE_TAG_OPEN}{text}{EXAMPLE_TAG_CLOSE}"

        return [
            HumanMessage(content=wrap("Turn on strip lights and heater.")),
            AIMessage(
                content=wrap(
                    get_task_master_examples(example_id=0, available_tools=available_tool_ids)
                )
            ),
            HumanMessage(content=wrap("What is the weather today?")),
            AIMessage(
                content=wrap(
                    get_task_master_examples(example_id=1, available_tools=available_tool_ids)
                )
            ),
        ]

    def generate(
        self,
        user_query: str,
        model_name: str,
        context: ContextSnapshot | None = None,
        *,
        tool_specs: list[ToolSpec] | None = None,
        mode: str = "act",
        feedback: str | None = None,
        project_instructions: str | None = None,
    ) -> Plan:
        """Generate a plan from the user query."""
        if self._tool_registry is None:
            raise ValueError("Tool registry is required for planning.")
        user_id = "meeseeks-task-master"
        session_id = f"action-queue-id-{os.getpid()}-{os.urandom(4).hex()}"
        langfuse_handler = build_langfuse_handler(
            user_id=user_id,
            session_id=session_id,
            trace_name=user_id,
            version=get_version(),
            release=get_config_value("runtime", "envmode", default="Not Specified"),
        )
        model = build_chat_model(
            model_name=model_name,
            openai_api_base=get_config_value("llm", "api_base"),
            api_key=get_config_value("llm", "api_key"),
        )
        parser = PydanticOutputParser(pydantic_object=Plan)
        component_status = self._resolve_component_status()
        if tool_specs is not None:
            specs = tool_specs
        elif mode == "plan":
            specs = self._tool_registry.list_specs()
        else:
            specs = self._tool_registry.list_specs_for_mode(mode)
        if mode == "act" and tool_specs is None:
            specs = self._filter_specs_by_intent(specs, user_query)
        available_tool_ids = [spec.tool_id for spec in specs]
        system_prompt = self._prompt_builder.build(
            get_system_prompt(),
            context,
            component_status=component_status if mode == "act" else None,
            mode=mode,
            tool_specs=specs,
            include_tool_schemas=False,
            include_tool_guidance=False,
            project_instructions=project_instructions,
        )
        example_messages = self._build_example_messages(available_tool_ids, mode=mode)
        if mode == "act":
            instruction = "## Generate the minimal plan for the user query"
        else:
            instruction = "## Generate a plan for the user query"

        user_prompt = "{user_query}"
        if feedback:
            user_prompt += f"\n\nPrevious plan was rejected. Feedback: {feedback}"

        prompt = ChatPromptTemplate(
            messages=[
                SystemMessage(content=system_prompt),
                *example_messages,
                HumanMessagePromptTemplate.from_template(
                    "## Format Instructions\n{format_instructions}\n"
                    f"{instruction}\n{user_prompt}"
                ),
            ],
            partial_variables={"format_instructions": parser.get_format_instructions()},
            input_variables=["user_query"],
        )
        logging.info(
            "Generating action plan <model='{}'; user_query='{}'>",
            model_name,
            user_query,
        )
        logging.info("Input prompt token length is `{}`.", num_tokens_from_string(str(prompt)))
        config: dict[str, object] = {}
        if langfuse_handler is not None:
            config["callbacks"] = [langfuse_handler]
            metadata = getattr(langfuse_handler, "langfuse_metadata", None)
            if isinstance(metadata, dict) and metadata:
                config["metadata"] = metadata
        with langfuse_trace_span("action-plan") as span:
            if span is not None:
                try:
                    span.update_trace(input={"user_query": user_query.strip()})
                except Exception:
                    pass
            action_plan = (prompt | model | parser).invoke(
                {"user_query": user_query.strip()},
                config=config or None,
            )
            if span is not None:
                try:
                    span.update_trace(output={"step_count": len(action_plan.steps or [])})
                except Exception:
                    pass
        action_plan.human_message = user_query
        return action_plan

    @staticmethod
    def _infer_intent_capabilities(user_query: str) -> set[str]:
        lowered = user_query.lower()
        requested: set[str] = set()
        for intent, keywords in INTENT_KEYWORDS.items():
            if any(keyword in lowered for keyword in keywords):
                requested |= INTENT_CAPABILITIES[intent]
        return requested

    @staticmethod
    def _spec_capabilities(spec) -> set[str]:
        metadata = spec.metadata or {}
        capabilities = metadata.get("capabilities")
        if isinstance(capabilities, list):
            return {str(item) for item in capabilities if isinstance(item, str)}

        tool_id = spec.tool_id.lower()
        inferred: set[str] = set()
        if "internet_search" in tool_id or "web_search" in tool_id or "searxng" in tool_id:
            inferred.add("web_search")
        if "web_url_read" in tool_id or "web_url" in tool_id:
            inferred.add("web_read")
        if "aider_read_file" in tool_id or "aider_list_dir" in tool_id:
            inferred.add("file_read")
        if "aider_edit_block" in tool_id:
            inferred.add("file_write")
        if "shell" in tool_id:
            inferred.add("shell_exec")
        if "home_assistant" in tool_id:
            inferred.add("home_assistant")
        return inferred

    def _filter_specs_by_intent(self, specs, user_query: str):
        requested = self._infer_intent_capabilities(user_query)
        if not requested:
            return specs
        filtered = [spec for spec in specs if self._spec_capabilities(spec).intersection(requested)]
        return filtered or specs

    def _resolve_component_status(self) -> list[ComponentStatus]:
        return [resolve_langfuse_status()]


__all__ = [
    "Planner",
    "PromptBuilder",
]
