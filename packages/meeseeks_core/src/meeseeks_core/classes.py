#!/usr/bin/env python3
"""Core data models and tool abstractions for Meeseeks orchestration."""

from __future__ import annotations

import abc
import json
import os
from collections.abc import Sequence
from typing import cast

from langchain_community.document_loaders import JSONLoader
from langchain_core.documents import Document
from pydantic.v1 import BaseModel, Field, validator

from meeseeks_core.common import MockSpeaker, get_logger, get_mock_speaker, get_unique_timestamp
from meeseeks_core.components import build_langfuse_handler
from meeseeks_core.config import get_config_value
from meeseeks_core.llm import build_chat_model
from meeseeks_core.types import ActionStepPayload, ToolInput

logging = get_logger(name="core.classes")
AVAILABLE_TOOLS: list[str] = ["home_assistant_tool"]


def set_available_tools(tool_ids: list[str]) -> None:
    """Update available tool IDs for validation."""
    global AVAILABLE_TOOLS
    AVAILABLE_TOOLS = tool_ids


class ActionStep(BaseModel):
    """Action step with validation metadata."""

    title: str | None = Field(
        default=None,
        description="Short header summarizing the task for this step.",
    )
    objective: str | None = Field(
        default=None,
        description="Brief objective explaining why this step is needed.",
    )
    execution_checklist: list[str] = Field(
        default_factory=list,
        description="Short checklist of execution details for this step.",
    )
    expected_output: str | None = Field(
        default=None,
        description="Optional description of what success looks like.",
    )
    tool_id: str = Field(
        description=(
            "Specify the tool_id that should execute the action. "
            "Use only tool IDs listed under Available tools."
        )
    )
    operation: str = Field(description="Specify the execution type (get/set or execute).")
    tool_input: ToolInput = Field(
        description=(
            "Provide details for the action. If 'task', specify the task to perform. "
            "If 'talk', include the message to speak to the user."
        )
    )
    result: object | None = Field(
        alias="_result",
        default=None,
        description="Private field to persist the action status and other data.",
    )

    class Config:
        """Allow both alias and field-name population."""

        allow_population_by_field_name = True
        extra = "forbid"


class PlanStep(BaseModel):
    """High-level plan step produced by the planner."""

    title: str = Field(description="Short title for the step.")
    description: str = Field(description="One-paragraph description of the step.")


class Plan(BaseModel):
    """Plan with human-readable steps."""

    human_message: str | None = Field(
        alias="_human_message",
        default=None,
        description="Human message associated with the plan.",
    )
    steps: list[PlanStep] = Field(default_factory=list)


class TaskQueue(BaseModel):
    """Queue of executed tool steps and results."""

    human_message: str | None = Field(
        alias="_human_message",
        default=None,
        description="Human message associated with the task queue.",
    )
    plan_steps: list[PlanStep] = Field(default_factory=list)
    action_steps: list[ActionStep] = Field(default_factory=list)
    task_result: str | None = Field(
        alias="_task_result", default=None, description="Store the result for the entire task queue"
    )
    last_error: str | None = Field(
        alias="_last_error",
        default=None,
        description="Short description of the most recent tool failure.",
    )

    @validator("action_steps", allow_reuse=True)
    # pylint: disable=E0213,W0613
    def validate_actions(cls, field: list[ActionStep]) -> list[ActionStep]:
        """Normalize and validate action steps."""
        for action in field:
            action.tool_id = action.tool_id.lower()
            action.operation = action.operation.lower()
            error_msg_list = []

            if action.tool_id not in AVAILABLE_TOOLS:
                error_msg_list.append(f"`{action.tool_id}` is not a valid Assistant tool.")

            if action.operation not in ["get", "set", "execute"]:
                error_msg = f"`{action.operation}` is not a valid operation."
                error_msg_list.append(error_msg)

            if action.tool_input is None:
                error_msg_list.append("Tool input cannot be None.")

            if error_msg_list:
                for msg in error_msg_list:
                    logging.error(msg)

        return field


ActionStep.update_forward_refs(ToolInput=ToolInput)


class OrchestrationState(BaseModel):
    """State for the orchestration loop."""

    goal: str
    session_id: str | None = None
    plan: list[PlanStep] = Field(default_factory=list)
    tool_results: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    done: bool = False
    done_reason: str | None = None
    summary: str | None = None


class AbstractTool(abc.ABC):
    """Base tool with shared initialization helpers."""

    def __init__(
        self,
        name: str,
        description: str,
        model_name: str | None = None,
        use_llm: bool = True,
    ) -> None:
        """Initialize tool configuration."""
        tool_model = get_config_value("llm", "tool_model")
        default_model = get_config_value("llm", "default_model", default="gpt-5.2")
        self.model_name = cast(
            str,
            model_name or tool_model or default_model,
        )
        self.name = name
        self.description = description
        self.use_llm = use_llm
        self._id = f"{name.lower().replace(' ', '_')}_tool"
        session_id = f"{self._id}-tool-id-{get_unique_timestamp()}"
        logging.info(f"Tool created <name={name}; session_id={session_id};>")
        self.langfuse_handler = build_langfuse_handler(
            user_id=f"meeseeks-{name}",
            session_id=session_id,
            trace_name=f"meeseeks-{self._id}",
            version=get_config_value("runtime", "version", default="Not Specified"),
            release=get_config_value("runtime", "envmode", default="Not Specified"),
        )
        self.model = None
        if self.use_llm:
            self.model = build_chat_model(
                model_name=self.model_name,
                openai_api_base=get_config_value("llm", "api_base"),
                api_key=get_config_value("llm", "api_key"),
            )
        root_cache_dir = get_config_value("runtime", "cache_dir", default=".cache")
        if not root_cache_dir:
            raise ValueError("runtime.cache_dir is not set.")
        self.cache_dir = os.path.abspath(os.path.join(str(root_cache_dir), self._id))
        logging.debug("{} cache directory is {}.", self._id, self.cache_dir)

    def _save_json(self, data: object, filename: str) -> None:
        """Persist JSON data under the cache directory."""
        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir)
        filename = os.path.join(self.cache_dir, filename)
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        logging.info(f"Data saved to {filename}.")

    def _load_rag_json(self, filename: str) -> list[Document]:
        """Load JSON content as documents."""
        logging.debug("RAG directory is {}.", self.cache_dir)
        logging.info(f"Loading `{filename}` as JSON.")
        filename = os.path.join(self.cache_dir, filename)
        filename = os.path.abspath(filename)
        loader = JSONLoader(file_path=filename, jq_schema=".", text_content=False)
        data = loader.load()
        return data

    def _load_rag_documents(self, filenames: list[str]) -> list[Document]:
        """Load and concatenate multiple JSON files."""
        rag_documents: list[Document] = []
        for rag_file in filenames:
            data = self._load_rag_json(rag_file)
            rag_documents.extend(data)
        return rag_documents

    def set_state(self, action_step: ActionStep | None = None) -> MockSpeaker:
        """Perform a state-changing action."""
        MockSpeaker = get_mock_speaker()
        return MockSpeaker(content="Not implemented yet.")

    def get_state(self, action_step: ActionStep | None = None) -> MockSpeaker:
        """Perform a read-only action."""
        MockSpeaker = get_mock_speaker()
        return MockSpeaker(content="Not implemented yet.")

    def run(self, action_step: ActionStep) -> MockSpeaker:
        """Execute the action based on the operation."""
        if action_step.operation == "set":
            return self.set_state(action_step)
        if action_step.operation == "get":
            return self.get_state(action_step)
        raise ValueError(f"Invalid operation: {action_step.operation}")


def create_task_queue(
    action_data: list[ActionStepPayload] | None = None,
    is_example: bool = True,
) -> TaskQueue:
    """Create a TaskQueue from serialized action data."""
    if action_data is None:
        raise ValueError("Action data cannot be None.")

    action_steps = [ActionStep(**action) for action in action_data]
    task_queue = TaskQueue(action_steps=action_steps)
    if is_example:
        del task_queue.human_message
    return task_queue


def create_plan(
    step_data: list[dict[str, str]] | None = None,
    is_example: bool = True,
) -> Plan:
    """Create a Plan from serialized step data."""
    if step_data is None:
        raise ValueError("Step data cannot be None.")
    steps = [PlanStep(**step) for step in step_data]
    plan = Plan(steps=steps)
    if is_example:
        del plan.human_message
    return plan


def get_task_master_examples(
    example_id: int = 0,
    available_tools: Sequence[str] | None = None,
) -> str:
    """Return serialized example plan data."""
    if available_tools is None:
        available_tools = AVAILABLE_TOOLS
    include_home_assistant = "home_assistant_tool" in available_tools
    if include_home_assistant:
        examples: list[list[dict[str, str]]] = [
            [
                {
                    "title": "Turn on strip lights",
                    "description": "Use Home Assistant to switch on the strip lights.",
                },
                {
                    "title": "Turn on heater",
                    "description": "Use Home Assistant to switch on the heater.",
                },
            ],
            [
                {
                    "title": "Check weather",
                    "description": "Use Home Assistant to retrieve today's weather details.",
                },
            ],
        ]
    else:
        examples = [[], []]
    if example_id not in range(0, len(examples)):
        raise ValueError(f"Invalid example ID: {example_id}")

    return create_plan(step_data=examples[example_id], is_example=True).json()
