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
    resolve_langfuse_status,
)
from meeseeks_core.context import ContextSnapshot, render_event_lines
from meeseeks_core.llm import build_chat_model
from meeseeks_core.tool_registry import ToolRegistry

logging = get_logger(name="core.planning")
EXAMPLE_TAG_OPEN = '<example desc="Illustrative only; not part of the live conversation">'
EXAMPLE_TAG_CLOSE = "</example>"


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
            catalog = self._tool_registry.tool_catalog()
            if catalog:
                tool_lines = "\n".join(
                    f"- {tool['tool_id']}: {tool['description']}" for tool in catalog
                )
                sections.append(f"Available tools:\n{tool_lines}")
            schema_lines = self._render_schema_lines(self._tool_registry.list_specs())
            if schema_lines:
                sections.append("MCP tool input schemas:\n" + "\n".join(schema_lines))
            tool_prompts = self._render_tool_prompts(self._tool_registry.list_specs())
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
    def _render_tool_prompts(specs) -> list[str]:
        prompts: list[str] = []
        for spec in specs:
            if not spec.prompt_path:
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
    def _build_example_messages(available_tool_ids: list[str]) -> list[BaseMessage]:
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
            version=os.getenv("VERSION", "Not Specified"),
            release=os.getenv("ENVMODE", "Not Specified"),
        )
        model = build_chat_model(
            model_name=model_name,
            temperature=0.4,
            openai_api_base=os.getenv("OPENAI_API_BASE"),
        )
        parser = PydanticOutputParser(pydantic_object=TaskQueue)
        component_status = self._resolve_component_status()
        available_tool_ids = [spec.tool_id for spec in self._tool_registry.list_specs()]
        system_prompt = self._prompt_builder.build(
            get_system_prompt(),
            context,
            component_status=component_status,
        )
        example_messages = self._build_example_messages(available_tool_ids)
        prompt = ChatPromptTemplate(
            messages=[
                SystemMessage(content=system_prompt),
                *example_messages,
                HumanMessagePromptTemplate.from_template(
                    "## Format Instructions\n{format_instructions}\n"
                    "## Generate a task queue for the user query\n{user_query}"
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
        action_plan = (prompt | model | parser).invoke(
            {"user_query": user_query.strip()},
            config=config or None,
        )
        action_plan.human_message = user_query
        return action_plan

    def _resolve_component_status(self) -> list[ComponentStatus]:
        status: list[ComponentStatus] = [resolve_langfuse_status()]
        if self._tool_registry is not None:
            for spec in self._tool_registry.list_specs(include_disabled=True):
                status.append(
                    ComponentStatus(
                        name=f"tool:{spec.tool_id}",
                        enabled=spec.enabled,
                        reason=spec.metadata.get("disabled_reason"),
                    )
                )
        return status


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
        model_name = model_name or os.getenv("DEFAULT_MODEL", "gpt-3.5-turbo")
        system_prompt = self._prompt_builder.build(
            get_system_prompt("response-synthesizer"),
            context,
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
            temperature=0.3,
            openai_api_base=os.getenv("OPENAI_API_BASE"),
        )
        output = (prompt | model).invoke(
            {
                "user_query": user_query.strip(),
                "tool_outputs": "\n".join(f"- {item}" for item in tool_outputs),
            }
        )
        content = getattr(output, "content", output)
        return str(content).strip()


__all__ = ["Planner", "PromptBuilder", "ResponseSynthesizer"]
