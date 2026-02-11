#!/usr/bin/env python3
"""Prompt construction and planning helpers."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate
from pydantic.v1 import BaseModel, Field

from meeseeks_core.classes import Plan, PlanStep, get_task_master_examples
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
from meeseeks_core.tool_registry import ToolRegistry, ToolSpec

logging = get_logger(name="core.planning")
EXAMPLE_TAG_OPEN = '<example desc="Illustrative only; not part of the live conversation">'
EXAMPLE_TAG_CLOSE = "</example>"
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


class ToolSelection(BaseModel):
    """Tool selection decision for a user query."""

    tool_required: bool = Field(default=False)
    tool_ids: list[str] = Field(default_factory=list)
    rationale: str | None = None


class StepDecision(BaseModel):
    """Decision on how to execute a single plan step."""

    decision: str = Field(description="tool or respond")
    tool_id: str | None = None
    args: object | None = None
    response: str | None = None


class PlanUpdate(BaseModel):
    """Updated remaining plan steps."""

    steps: list[PlanStep] = Field(default_factory=list)


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
        tool_specs: list[ToolSpec] | None = None,
        mode: str = "act",
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
            version=get_config_value("runtime", "version", default="Not Specified"),
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
        )
        example_messages = self._build_example_messages(available_tool_ids, mode=mode)
        if mode == "act":
            instruction = "## Generate the minimal plan for the user query"
        else:
            instruction = "## Generate a plan for the user query"
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


def _render_tool_catalog(specs: list[ToolSpec], *, include_schema: bool) -> str:
    lines: list[str] = []
    for spec in specs:
        line = f"- {spec.tool_id}: {spec.description}"
        if include_schema:
            schema = spec.metadata.get("schema")
            if isinstance(schema, dict):
                line += f" | schema={json.dumps(schema, ensure_ascii=True)}"
        lines.append(line)
    return "\n".join(lines).strip()


class ToolSelector:
    """Select tools needed to satisfy a request."""

    def __init__(self, tool_registry: ToolRegistry | None) -> None:
        """Initialize the tool selector."""
        self._tool_registry = tool_registry

    def select(
        self,
        user_query: str,
        model_name: str,
        *,
        tool_specs: list[ToolSpec],
        context: ContextSnapshot | None = None,
    ) -> ToolSelection:
        """Return a tool selection decision."""
        if self._tool_registry is None:
            raise ValueError("Tool registry is required for tool selection.")
        parser = PydanticOutputParser(pydantic_object=ToolSelection)  # type: ignore[type-var]
        system_prompt = get_system_prompt("tool-selector")
        tools_text = _render_tool_catalog(tool_specs, include_schema=False)
        prompt = ChatPromptTemplate(
            messages=[
                SystemMessage(
                    content=(
                        f"{system_prompt}\n\nAvailable tools:\n{tools_text}"
                        if tools_text
                        else system_prompt
                    )
                ),
                HumanMessagePromptTemplate.from_template(
                    "User request:\n{user_query}\n\n{format_instructions}"
                ),
            ],
            partial_variables={"format_instructions": parser.get_format_instructions()},
            input_variables=["user_query"],
        )
        try:
            model = build_chat_model(
                model_name=model_name,
                openai_api_base=get_config_value("llm", "api_base"),
                api_key=get_config_value("llm", "api_key"),
            )
            selection = (prompt | model | parser).invoke({"user_query": user_query.strip()})
            if selection.tool_required and selection.tool_ids:
                specs_by_id = {spec.tool_id: spec for spec in tool_specs}
                selected_caps: set[str] = set()
                for tool_id in selection.tool_ids:
                    spec = specs_by_id.get(tool_id)
                    if spec is not None:
                        selected_caps |= Planner._spec_capabilities(spec)
                if "web_search" in selected_caps:
                    for spec in tool_specs:
                        if "web_read" in Planner._spec_capabilities(spec):
                            if spec.tool_id not in selection.tool_ids:
                                selection.tool_ids.append(spec.tool_id)
            return selection
        except Exception as exc:  # pragma: no cover - defensive fallback
            logging.warning("Tool selector unavailable, falling back to all tools: {}", exc)
            return ToolSelection(
                tool_required=bool(tool_specs),
                tool_ids=[spec.tool_id for spec in tool_specs],
                rationale="fallback",
            )


class StepExecutor:
    """Decide how to execute a single plan step."""

    def __init__(self, tool_registry: ToolRegistry | None) -> None:
        """Initialize the step executor."""
        self._tool_registry = tool_registry

    def decide(
        self,
        user_query: str,
        step: PlanStep,
        model_name: str,
        *,
        allowed_tools: list[ToolSpec],
        context: ContextSnapshot | None = None,
    ) -> StepDecision:
        """Return a decision for executing the step."""
        if self._tool_registry is None:
            raise ValueError("Tool registry is required for step execution.")
        parser = PydanticOutputParser(pydantic_object=StepDecision)  # type: ignore[type-var]
        system_prompt = get_system_prompt("step-executor")
        tools_text = _render_tool_catalog(allowed_tools, include_schema=True)
        prompt = ChatPromptTemplate(
            messages=[
                SystemMessage(
                    content=(
                        f"{system_prompt}\n\nAllowed tools:\n{tools_text}"
                        if tools_text
                        else system_prompt
                    )
                ),
                HumanMessagePromptTemplate.from_template(
                    "User request:\n{user_query}\n\n"
                    "Plan step:\n- {title}\n- {description}\n\n"
                    "{format_instructions}"
                ),
            ],
            partial_variables={"format_instructions": parser.get_format_instructions()},
            input_variables=["user_query", "title", "description"],
        )
        model = build_chat_model(
            model_name=model_name,
            openai_api_base=get_config_value("llm", "api_base"),
            api_key=get_config_value("llm", "api_key"),
        )
        decision = (prompt | model | parser).invoke(
            {
                "user_query": user_query.strip(),
                "title": step.title,
                "description": step.description,
            }
        )
        return decision


class PlanUpdater:
    """Update remaining plan steps after executing a step."""

    def __init__(self, tool_registry: ToolRegistry | None) -> None:
        """Initialize the plan updater."""
        self._tool_registry = tool_registry

    def update(
        self,
        user_query: str,
        model_name: str,
        *,
        completed_step: PlanStep,
        last_result: str | None,
        remaining_steps: list[PlanStep],
        context: ContextSnapshot | None = None,
    ) -> list[PlanStep]:
        """Return updated remaining steps."""
        parser = PydanticOutputParser(pydantic_object=PlanUpdate)  # type: ignore[type-var]
        system_prompt = get_system_prompt("plan-updater")
        remaining_lines = [f"- {step.title}: {step.description}" for step in remaining_steps]
        remaining_text = "\n".join(remaining_lines) or "(none)"
        prompt = ChatPromptTemplate(
            messages=[
                SystemMessage(content=system_prompt),
                HumanMessagePromptTemplate.from_template(
                    "User request:\n{user_query}\n\n"
                    "Completed step:\n- {title}\n- {description}\n\n"
                    "Latest result:\n{result}\n\n"
                    "Remaining steps:\n{remaining}\n\n"
                    "{format_instructions}"
                ),
            ],
            partial_variables={"format_instructions": parser.get_format_instructions()},
            input_variables=["user_query", "title", "description", "result", "remaining"],
        )
        model = build_chat_model(
            model_name=model_name,
            openai_api_base=get_config_value("llm", "api_base"),
            api_key=get_config_value("llm", "api_key"),
        )
        update = (prompt | model | parser).invoke(
            {
                "user_query": user_query.strip(),
                "title": completed_step.title,
                "description": completed_step.description,
                "result": last_result or "",
                "remaining": remaining_text,
            }
        )
        return update.steps


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


__all__ = [
    "Planner",
    "PromptBuilder",
    "ResponseSynthesizer",
    "ToolSelector",
    "StepExecutor",
    "PlanUpdater",
    "ToolSelection",
    "StepDecision",
    "PlanUpdate",
]
