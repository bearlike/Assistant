#!/usr/bin/env python3
"""Core data models and tool abstractions for Meeseeks orchestration."""
from __future__ import annotations

import abc
import json
import os
from collections.abc import Sequence
from typing import cast

from dotenv import load_dotenv
from langchain_community.document_loaders import JSONLoader
from langchain_core.documents import Document
from pydantic.v1 import BaseModel, Field, validator

from core.common import MockSpeaker, get_logger, get_mock_speaker, get_unique_timestamp
from core.components import build_langfuse_handler
from core.llm import build_chat_model
from core.types import ActionStepPayload

load_dotenv()
logging = get_logger(name="core.classes")
AVAILABLE_TOOLS: list[str] = ["home_assistant_tool", "talk_to_user_tool"]


def set_available_tools(tool_ids: list[str]) -> None:
    """Update the global tool list for ActionStep validation.

    Args:
        tool_ids: List of tool identifiers to allow.
    """
    global AVAILABLE_TOOLS
    AVAILABLE_TOOLS = tool_ids


class ActionStep(BaseModel):
    """Defines an action step within a task queue with validation.

    Attributes:
        title: Short task header for the step.
        objective: Brief objective describing the intent of the step.
        execution_checklist: Small checklist of execution hints.
        expected_output: Optional description of the expected outcome.
        action_consumer: Tool identifier that should execute the action.
        action_type: Action category, typically "get" or "set".
        action_argument: Natural language argument for the tool.
        result: Optional tool result payload.
    """
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
    action_consumer: str = Field(
        description=(
            "Specify the tool_id that should execute the action. "
            "Use only tool IDs listed under Available tools."
        )
    )
    action_type: str = Field(
        description="Specify either 'get' or 'set' to indicate the action type."
    )
    action_argument: str = Field(
        description=(
            "Provide details for the action. If 'task', specify the task to perform. "
            "If 'talk', include the message to speak to the user."
        )
    )
    result: MockSpeaker | None = Field(
        alias="_result",
        default=None,
        description='Private field to persist the action status and other data.'
    )


class TaskQueue(BaseModel):
    """Manages a queue of actions to be performed, tracking their results.

    Attributes:
        human_message: Original user message for the task queue.
        action_steps: Ordered list of action steps to execute.
        task_result: Aggregated result of the task queue.
    """
    human_message: str | None = Field(
        alias="_human_message",
        default=None,
        description='Human message associated with the task queue.'
    )
    action_steps: list[ActionStep] = Field(default_factory=list)
    task_result: str | None = Field(
        alias="_task_result",
        default=None,
        description='Store the result for the entire task queue'
    )

    @validator("action_steps", allow_reuse=True)
    # pylint: disable=E0213,W0613
    def validate_actions(cls, field: list[ActionStep]) -> list[ActionStep]:
        """Normalize and validate action steps within a task queue.

        Args:
            field: Action steps to normalize and validate.

        Returns:
            Normalized list of action steps.
        """
        for action in field:
            # Normalize once and store it
            action.action_consumer = action.action_consumer.lower()
            action.action_type = action.action_type.lower()
            error_msg_list = []

            # Check if action_consumer is valid
            if action.action_consumer not in AVAILABLE_TOOLS:
                error_msg_list.append(
                    f"`{action.action_consumer}` is not a valid Assistant consumer.")

            # Check if action_type is valid
            if action.action_type not in ["get", "set"]:
                error_msg = f"`{action.action_type}` is not a valid action type."
                error_msg_list.append(error_msg)

            # Specific checks for "talk_to_user" consumer
            if action.action_consumer == "talk_to_user_tool" and \
                    action.action_type == "get":
                error_msg = f"`{action.action_consumer}` does not support 'get' action type."
                error_msg_list.append(error_msg)

            # Check for None argument
            if action.action_argument is None:
                error_msg_list.append("Action argument cannot be None.")

            # Handle errors if any
            if error_msg_list:
                error_msg = "\n".join(error_msg_list)
                for msg in error_msg_list:
                    logging.error(msg)  # Log

        return field


class OrchestrationState(BaseModel):
    """Track state for the orchestration loop.

    Attributes:
        goal: User goal for the session.
        session_id: Unique session identifier.
        plan: Current action plan.
        tool_results: Result strings from executed tools.
        open_questions: Outstanding questions for the user.
        done: Whether orchestration is finished.
        done_reason: Reason for completion.
        summary: Optional session summary string.
    """
    goal: str
    session_id: str | None = None
    plan: list[ActionStep] = Field(default_factory=list)
    tool_results: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    done: bool = False
    done_reason: str | None = None
    summary: str | None = None


class AbstractTool(abc.ABC):
    """Abstract base class for tools, providing common features and methods."""

    def _setup_cache_dir(self, name: str) -> str:
        """Set up and return the cache directory path.

        Args:
            name: Tool name used to construct the cache path.

        Returns:
            Absolute path to the cache directory.

        Raises:
            ValueError: If CACHE_DIR is not configured.
        """
        root_cache_dir = os.getenv("CACHE_DIR")
        if not root_cache_dir:
            raise ValueError("CACHE_DIR environment variable is not set.")
        cache_path = os.path.join(
            root_cache_dir, "..", ".cache", f"{name.lower().replace(' ', '_')}_tool")
        os.makedirs(cache_path, exist_ok=True)
        return os.path.abspath(cache_path)

    def __init__(
        self,
        name: str,
        description: str,
        model_name: str | None = None,
        temperature: float = 0.3,
        use_llm: bool = True,
    ) -> None:
        """Initialize the tool with optional model configuration.

        Args:
            name: Tool display name.
            description: Short description of tool behavior.
            model_name: Optional model override for the tool.
            temperature: Sampling temperature for the model.
            use_llm: Whether to initialize an LLM client for the tool.

        Raises:
            ValueError: If CACHE_DIR is not configured.
        """
        self.model_name = cast(
            str,
            model_name
            or os.getenv("TOOL_MODEL")
            or os.getenv("DEFAULT_MODEL", "gpt-3.5-turbo"),
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
            version=os.getenv("VERSION", "Not Specified"),
            release=os.getenv("ENVMODE", "Not Specified"),
        )
        self.model = None
        if self.use_llm:
            self.model = build_chat_model(
                model_name=self.model_name,
                temperature=temperature,
                openai_api_base=os.getenv("OPENAI_API_BASE"),
            )
        root_cache_dir = os.getenv("CACHE_DIR", None)
        if root_cache_dir is None:
            raise ValueError("CACHE_DIR environment variable is not set.")

        cache_dir = os.path.join(root_cache_dir, "..", ".cache", self._id)
        self.cache_dir = os.path.abspath(cache_dir)
        logging.debug("{} cache directory is {}.", self._id, self.cache_dir)

    def _save_json(self, data: object, filename: str) -> None:
        """Save a dictionary to a JSON file.

        Args:
            data: Serializable payload to store.
            filename: Output filename under the cache directory.

        Raises:
            OSError: If the file cannot be written.
        """
        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir)
        filename = os.path.join(self.cache_dir, filename)
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        logging.info(f"Data saved to {filename}.")

    def _load_rag_json(self, filename: str) -> list[Document]:
        """Load a dictionary from a JSON file.

        Args:
            filename: JSON filename under the cache directory.

        Returns:
            List of loaded Documents.

        Raises:
            OSError: If the file cannot be read.
        """
        logging.debug("RAG directory is {}.", self.cache_dir)
        logging.info(f"Loading `{filename}` as JSON.")
        filename = os.path.join(self.cache_dir, filename)
        filename = os.path.abspath(filename)
        loader = JSONLoader(
            file_path=filename,
            jq_schema='.',
            text_content=False)
        data = loader.load()
        return data

    def _load_rag_documents(self, filenames: list[str]) -> list[Document]:
        """Load and concatenate multiple JSON files into RAG documents.

        Args:
            filenames: List of JSON files to load.

        Returns:
            Combined list of Documents for RAG ingestion.
        """
        rag_documents: list[Document] = []
        for rag_file in filenames:
            data = self._load_rag_json(rag_file)
            rag_documents.extend(data)
        return rag_documents

    def set_state(self, action_step: ActionStep | None = None) -> MockSpeaker:
        """Perform a state-changing action.

        Args:
            action_step: Action step containing the action arguments.

        Returns:
            MockSpeaker response for the action.
        """
        MockSpeaker = get_mock_speaker()
        return MockSpeaker(content="Not implemented yet.")

    def get_state(self, action_step: ActionStep | None = None) -> MockSpeaker:
        """Perform a read-only action.

        Args:
            action_step: Action step containing the query arguments.

        Returns:
            MockSpeaker response for the action.
        """
        MockSpeaker = get_mock_speaker()
        return MockSpeaker(content="Not implemented yet.")

    def run(self, action_step: ActionStep) -> MockSpeaker:
        """Execute the action based on the action type.

        Args:
            action_step: ActionStep object with action details.

        Returns:
            MockSpeaker response for the action.

        Raises:
            ValueError: If the action type is unsupported.
        """
        if action_step.action_type == "set":
            return self.set_state(action_step)
        if action_step.action_type == "get":
            return self.get_state(action_step)
        raise ValueError(f"Invalid action type: {action_step.action_type}")


def create_task_queue(
    action_data: list[ActionStepPayload] | None = None,
    is_example: bool = True,
) -> TaskQueue:
    """Create a TaskQueue object from serialized action data.

    Args:
        action_data: List of action step payloads.
        is_example: Whether to drop the human_message field.

    Returns:
        TaskQueue populated with the action steps.

    Raises:
        ValueError: If action_data is None.
    """
    if action_data is None:
        raise ValueError("Action data cannot be None.")

    # Convert the input data to ActionStep objects
    action_steps = [ActionStep(**action) for action in action_data]
    # Create a TaskQueue object with the action steps
    task_queue = TaskQueue(action_steps=action_steps)
    if is_example:
        del task_queue.human_message
    return task_queue


def get_task_master_examples(
    example_id: int = 0,
    available_tools: Sequence[str] | None = None,
) -> str:
    """Get serialized example task queue data.

    Args:
        example_id: Index of the example to return.
        available_tools: Optional tool IDs to shape the examples.

    Returns:
        JSON-serialized task queue string.

    Raises:
        ValueError: If example_id is out of range.
    """
    if available_tools is None:
        available_tools = AVAILABLE_TOOLS
    include_home_assistant = "home_assistant_tool" in available_tools
    if include_home_assistant:
        examples: list[list[ActionStepPayload]] = [
            [
                {
                    "title": "Turn on strip lights",
                    "objective": "Activate the strip lights via Home Assistant.",
                    "execution_checklist": [
                        "Use Home Assistant set action",
                        "Target strip lights",
                    ],
                    "expected_output": "Strip lights are powered on.",
                    "action_consumer": "home_assistant_tool",
                    "action_type": "set",
                    "action_argument": "Power on the strip lights.",
                },
                {
                    "title": "Turn on heater",
                    "objective": "Activate the heater via Home Assistant.",
                    "execution_checklist": [
                        "Use Home Assistant set action",
                        "Target heater",
                    ],
                    "expected_output": "Heater is powered on.",
                    "action_consumer": "home_assistant_tool",
                    "action_type": "set",
                    "action_argument": "Power on the Heater.",
                },
                {
                    "title": "Confirm actions to user",
                    "objective": "Let the user know the devices are being powered on.",
                    "execution_checklist": [
                        "Use talk_to_user_tool",
                        "Confirm both devices",
                    ],
                    "expected_output": "User receives confirmation.",
                    "action_consumer": "talk_to_user_tool",
                    "action_type": "set",
                    "action_argument": (
                        "Got it, boss! I'm using Home Assistant to power on the strip lights "
                        "and the heater."
                    ),
                },
            ],
            [
                {
                    "title": "Check weather",
                    "objective": "Retrieve today's weather from Home Assistant.",
                    "execution_checklist": [
                        "Use Home Assistant get action",
                        "Ask for today's weather",
                    ],
                    "expected_output": "Weather details are returned.",
                    "action_consumer": "home_assistant_tool",
                    "action_type": "get",
                    "action_argument": "Get today's weather.",
                },
            ]
        ]
    else:
        examples = [
            [
                {
                    "title": "Greet user",
                    "objective": "Open the conversation and ask how to help.",
                    "execution_checklist": ["Use talk_to_user_tool"],
                    "expected_output": "User is prompted for next task.",
                    "action_consumer": "talk_to_user_tool",
                    "action_type": "set",
                    "action_argument": "Got it, boss! How can I help you today?",
                },
            ],
            [
                {
                    "title": "Request next task",
                    "objective": "Ask the user for their next request.",
                    "execution_checklist": ["Use talk_to_user_tool"],
                    "expected_output": "User provides a follow-up request.",
                    "action_consumer": "talk_to_user_tool",
                    "action_type": "set",
                    "action_argument": "Happy to help. What should I take care of next?",
                },
            ],
        ]
    if example_id not in range(0, len(examples)):
        raise ValueError(f"Invalid example ID: {example_id}")

    return create_task_queue(action_data=examples[example_id], is_example=True).json()
