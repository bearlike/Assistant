#!/usr/bin/env python3
"""Prompt construction and planning helpers."""

from __future__ import annotations

import os
from collections.abc import Iterable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate

from meeseeks_core.classes import TaskQueue, get_task_master_examples
from meeseeks_core.common import get_logger, get_system_prompt, num_tokens_from_string
from meeseeks_core.components import (
    ComponentStatus,
    build_langfuse_handler,
    format_component_status,
    langfuse_trace_span,
    resolve_langfuse_status,
)
from meeseeks_core.config import get_config_value
from meeseeks_core.context import ContextSnapshot, render_event_lines
from meeseeks_core.llm import build_chat_model
from meeseeks_core.tool_registry import ToolRegistry

logging = get_logger(name="core.planning")
EXAMPLE_TAG_OPEN = '<example desc="Illustrative only; not part of the live conversation">'
EXAMPLE_TAG_CLOSE = "</example>"
TOOL_DETAIL_MAX = 10
INTENT_KEYWORDS: dict[str, set[str]] = {
    "web": {
        "latest",
        "current",
        "today",
        "now",
        "verify",
        "official",
        "news",
        "fetch",
        "lookup",
        "look up",
        "search the web",
        "web search",
        "internet",
    },
    "file": {
        "file",
        "edit",
        "write",
        "create",
        "script",
        "patch",
        "diff",
        "repo",
        "directory",
        "folder",
        "pwd",
        "local",
        "workspace",
    },
    "home": {
        "home assistant",
        "ha",
        "device",
        "light",
        "switch",
        "sensor",
        "climate",
    },
    "shell": {
        "shell",
        "command",
        "run",
        "execute",
        "terminal",
        "cli",
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
    ) -> str:
        """Build an augmented system prompt string."""
        sections = [base_prompt]
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
                if include_tool_schemas:
                    schema_lines = self._render_schema_lines(specs)
                    if schema_lines:
                        sections.append("Tool input schemas:\n" + "\n".join(schema_lines))
                if include_tool_guidance:
                    tool_prompts = self._render_tool_prompts(specs, local_only=True)
                    if tool_prompts:
                        sections.append("Tool guidance:\n" + "\n\n".join(tool_prompts))
        if component_status:
            sections.append("Component status:\n" + format_component_status(component_status))
        return "\n\n".join(sections)

    @staticmethod
    def _render_schema_lines(specs) -> list[str]:
        lines: list[str] = []
        for spec in specs:
            if spec.kind != "mcp":
                continue
            schema = spec.metadata.get("schema") if spec.metadata else None
            if not isinstance(schema, dict):
                continue
            required = schema.get("required") or []
            properties = schema.get("properties") or {}
            if not isinstance(properties, dict):
                properties = {}
            field_names = list(required) or list(properties.keys())
            if not field_names:
                continue
            parts: list[str] = []
            for name in field_names:
                if not isinstance(name, str):
                    continue
                prop = properties.get(name, {})
                if not isinstance(prop, dict):
                    prop = {}
                piece = name
                prop_type = prop.get("type")
                if isinstance(prop_type, str):
                    piece += f": {prop_type}"
                description = prop.get("description")
                if isinstance(description, str) and description:
                    piece += f" - {description}"
                parts.append(piece)
            if parts:
                lines.append(f"- {spec.tool_id}: " + "; ".join(parts))
        return lines

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
        mode: str = "act",
    ) -> TaskQueue:
        """Generate a task queue from the user query."""
        if self._tool_registry is None:
            raise ValueError("Tool registry is required for planning.")
        user_id = "meeseeks-task-master"
        session_id = f"action-queue-id-{os.getpid()}-{os.urandom(4).hex()}"
        langfuse_handler = build_langfuse_handler(
            user_id=user_id,
            session_id=session_id,
            trace_name=user_id,
            version=get_config_value("runtime", "version", default="Not Specified"),
            release=get_config_value("runtime", "envmode", default="Not Specified"),
        )
        model = build_chat_model(
            model_name=model_name,
            openai_api_base=get_config_value("llm", "api_base"),
            api_key=get_config_value("llm", "api_key"),
        )
        parser = PydanticOutputParser(pydantic_object=TaskQueue)
        component_status = self._resolve_component_status()
        specs = self._tool_registry.list_specs_for_mode(mode)
        include_tool_details = True
        if mode == "act":
            specs = self._filter_specs_by_intent(specs, user_query)
            include_tool_details = len(specs) <= TOOL_DETAIL_MAX
        available_tool_ids = [spec.tool_id for spec in specs]
        system_prompt = self._prompt_builder.build(
            get_system_prompt(),
            context,
            component_status=component_status if mode == "act" else None,
            mode=mode,
            tool_specs=specs,
            include_tool_schemas=include_tool_details,
            include_tool_guidance=include_tool_details,
        )
        example_messages = self._build_example_messages(available_tool_ids, mode=mode)
        if mode == "act":
            instruction = (
                "## Generate the minimal action plan for the user query\n"
                "Prefer a single tool call when possible. "
                "Avoid multi-step plans unless necessary."
            )
        else:
            instruction = "## Generate a task queue for the user query"
        prompt = ChatPromptTemplate(
            messages=[
                SystemMessage(content=system_prompt),
                *example_messages,
                HumanMessagePromptTemplate.from_template(
                    "## Format Instructions\n{format_instructions}\n"
                    f"{instruction}\n{{user_query}}"
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
                    span.update_trace(output={"step_count": len(action_plan.action_steps or [])})
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


class ResponseSynthesizer:
    """Synthesize a response from tool outputs."""

    def __init__(self, tool_registry: ToolRegistry | None) -> None:
        """Initialize the response synthesizer."""
        self._prompt_builder = PromptBuilder(tool_registry)

    def synthesize(
        self,
        user_query: str,
        tool_outputs: list[str],
        model_name: str | None,
        context: ContextSnapshot | None,
    ) -> str:
        """Synthesize a response from tool outputs."""
        model_name = model_name or get_config_value("llm", "default_model", default="gpt-5.2")
        system_prompt = self._prompt_builder.build(
            get_system_prompt("response-synthesizer"),
            context,
            mode="synthesize",
        )
        prompt = ChatPromptTemplate(
            messages=[
                SystemMessage(content=system_prompt),
                HumanMessagePromptTemplate.from_template(
                    "User request: {user_query}\n\nTool outputs:\n{tool_outputs}\n\n"
                    "Respond to the user using the tool outputs."
                ),
            ],
            input_variables=["user_query", "tool_outputs"],
        )
        model = build_chat_model(
            model_name=model_name,
            openai_api_base=get_config_value("llm", "api_base"),
            api_key=get_config_value("llm", "api_key"),
        )
        handler = build_langfuse_handler(
            user_id="meeseeks-response",
            session_id=f"response-{os.getpid()}-{os.urandom(4).hex()}",
            trace_name="meeseeks-response",
            version=get_config_value("runtime", "version", default="Not Specified"),
            release=get_config_value("runtime", "envmode", default="Not Specified"),
        )
        config: dict[str, object] = {}
        if handler is not None:
            config["callbacks"] = [handler]
            metadata = getattr(handler, "langfuse_metadata", None)
            if isinstance(metadata, dict) and metadata:
                config["metadata"] = metadata
        with langfuse_trace_span("response-synthesize") as span:
            if span is not None:
                try:
                    span.update_trace(
                        input={
                            "user_query": user_query.strip(),
                            "tool_output_count": len(tool_outputs),
                        }
                    )
                except Exception:
                    pass
            output = (prompt | model).invoke(
                {
                    "user_query": user_query.strip(),
                    "tool_outputs": "\n".join(f"- {item}" for item in tool_outputs),
                },
                config=config or None,
            )
            if span is not None:
                try:
                    span.update_trace(output={"response": str(getattr(output, "content", output))})
                except Exception:
                    pass
        content = getattr(output, "content", output)
        return str(content).strip()


__all__ = ["Planner", "PromptBuilder", "ResponseSynthesizer"]
