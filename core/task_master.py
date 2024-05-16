#!/usr/bin/env python3

# Standard library modules
import warnings
from typing import List
import os

# Third-party modules
from langchain.prompts import ChatPromptTemplate, HumanMessagePromptTemplate
from langchain.output_parsers import PydanticOutputParser
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.messages.ai import AIMessage
from langchain_core._api.beta_decorator import LangChainBetaWarning
from langchain_openai import ChatOpenAI
from langfuse.callback import CallbackHandler
from dotenv import load_dotenv

# User-defined modules
from core.classes import TaskQueue, get_task_master_examples
from core.common import get_unique_timestamp
from core.common import get_logger, get_system_prompt
from core.common import num_tokens_from_string
from tools.integration.homeassistant import HomeAssistant
from tools.core.talk_to_user import TalkToUser

logging = get_logger(name="core.task_master")

# C0116,
# Filter out LangChainBetaWarning specifically
warnings.simplefilter("ignore", LangChainBetaWarning)
load_dotenv()


def generate_action_plan(user_query: str, model_name: str = None) -> List[dict]:
    """
    Use the LangChain pipeline to generate an action plan based on the user query.

    Args:
        user_query (str): The user query to generate the action plan.

    Returns:
        List[dict]: The generated action plan as a list of dictionaries.
    """
    user_id = "meeseeks-task-master"
    session_id = f"action-queue-id-{get_unique_timestamp()}"
    trace_name = user_id
    version = os.getenv("VERSION", "Not Specified")
    release = os.getenv("ENVMODE", "Not Specified")

    langfuse_handler = CallbackHandler(
        user_id=user_id,
        session_id=session_id,
        trace_name=trace_name,
        version=version,
        release=release
    )

    model_name = model_name or os.getenv(
        "ACTION_PLAN_MODEL", os.getenv("DEFAULT_MODEL", "gpt-3.5-turbo"))

    model = ChatOpenAI(
        openai_api_base=os.getenv("OPENAI_API_BASE"),
        model=model_name,
        temperature=0.4
    )

    parser = PydanticOutputParser(pydantic_object=TaskQueue)
    logging.debug(
        "Generating action plan <model='%s'; user_query='%s'>", model_name, user_query)

    prompt = ChatPromptTemplate(
        messages=[
            SystemMessage(content=get_system_prompt()),
            HumanMessage(content="Turn on strip lights and heater."),
            AIMessage(get_task_master_examples(example_id=0)),
            HumanMessage(content="What is the weather today?"),
            AIMessage(get_task_master_examples(example_id=1)),
            HumanMessagePromptTemplate.from_template(
                "## Format Instructions\n{format_instructions}\n## Generate a task queue for the user query\n{user_query}"
            ),
        ],
        partial_variables={
            "format_instructions": parser.get_format_instructions()
        },
        input_variables=["user_query"]
    )

    estimator = num_tokens_from_string(str(prompt))
    logging.info("Input Prompt Token length is `%s`.", estimator)

    action_plan = (prompt | model | parser).invoke(
        {"user_query": user_query.strip()},
        config={"callbacks": [langfuse_handler]}
    )

    action_plan.human_message = user_query
    logging.info("Action plan generated <%s>", action_plan)
    return action_plan


def run_action_plan(task_queue: TaskQueue) -> TaskQueue:
    """
    Run the generated action plan.

    Args:
        task_queue (TaskQueue): The action plan to run.

    Returns:
        TaskQueue: The updated action plan after running.
    """
    tool_dict = {
        "home_assistant_tool": HomeAssistant(),
        "talk_to_user_tool": TalkToUser()
    }

    results = []

    for action_step in task_queue.action_steps:
        logging.debug(f"Processing ActionStep: {action_step}")
        tool = tool_dict.get(action_step.action_consumer)

        if tool is None:
            logging.error(
                f"No tool found for consumer: {action_step.action_consumer}")
            continue

        try:
            action_result = tool.run(action_step)
            action_step.result = action_result
            results.append(
                action_result.content if action_result.content is not None else "")
        except Exception as e:
            logging.error(f"Error processing action step: {e}")
            action_step.result = None

    task_queue.task_result = " ".join(results).strip()

    return task_queue
