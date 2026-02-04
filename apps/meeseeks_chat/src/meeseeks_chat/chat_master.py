#!/usr/bin/env python3
"""Streamlit chat app.

This module implements a chat application using Streamlit and Meeseeks.
It allows users to interact with an AI assistant
through a chat interface.

# Please start Meeseeks Chat using the following command:
# meeseeks-chat
"""

# Standard library modules
import time
from importlib import resources

# Third-party modules
import streamlit as st
from meeseeks_core.classes import TaskQueue
from meeseeks_core.common import get_logger
from meeseeks_core.permissions import auto_approve
from meeseeks_core.session_store import SessionStore
from meeseeks_core.task_master import generate_action_plan, orchestrate_session


class ConversationBufferWindowMemory:
    """Simple rolling buffer for chat context."""

    def __init__(self, k: int = 5) -> None:
        """Initialize the buffer with a max window size."""
        self.k = k
        self.buffer: list[dict[str, object]] = []

    def save_context(self, inputs: dict[str, object], outputs: dict[str, object]) -> None:
        """Store the latest input/output pair with a fixed window size."""
        self.buffer.append({"input": inputs, "output": outputs})
        if len(self.buffer) > self.k:
            self.buffer = self.buffer[-self.k :]


logging = get_logger(name="Meeseeks-Chat")


def generate_action_plan_helper(user_input: str) -> tuple[list[str], TaskQueue]:
    """Build the action plan preview for a user query.

    Args:
        user_input: Raw user query text.

    Returns:
        Tuple of human-readable action plan entries and the task queue.
    """
    action_plan_list = []
    task_queue = generate_action_plan(user_query=user_input)
    for action_step in task_queue.action_steps:
        # * Append action step to the action plan list
        action_plan_list.append(
            f"Using `{action_step.action_consumer}` with "
            f"`{action_step.action_type}` to `{action_step.action_argument}`"
        )
    return action_plan_list, task_queue


def run_action_plan_helper(task_queue: TaskQueue) -> str:
    """Execute an action plan and combine tool responses.

    Args:
        task_queue: Precomputed task queue to run.

    Returns:
        Combined tool responses as a single string.
    """
    responses: list[str] = []
    task_queue = orchestrate_session(
        user_query=task_queue.human_message or "",
        model_name=None,
        initial_task_queue=task_queue,
        session_id=st.session_state.session_id,
        session_store=st.session_state.session_store,
        approval_callback=auto_approve,
    )
    for action_step in task_queue.action_steps:
        responses.append(action_step.result.content)

    return " ".join(responses)


def main() -> None:
    """Run the Streamlit chat application."""
    st.set_page_config(
        page_title="Meeseeks | Bedroom AI",
        page_icon=":speech_balloon:",
    )
    image_resource = (
        resources.files("meeseeks_chat").joinpath("static").joinpath("img").joinpath("banner.png")
    )
    css_resource = (
        resources.files("meeseeks_chat")
        .joinpath("static")
        .joinpath("css")
        .joinpath("streamlit_custom.css")
    )
    with resources.as_file(image_resource) as image_path:
        st.image(str(image_path), use_column_width=True)

    # Load css file as string into page_bg_img
    with resources.as_file(css_resource) as css_path:
        with open(css_path, encoding="utf-8") as f:
            page_bg_img = f.read()
    st.markdown(f"<style>{page_bg_img}</style>", unsafe_allow_html=True)

    if "conversation_memory" not in st.session_state:
        st.session_state.conversation_memory = ConversationBufferWindowMemory(k=5)
    conversation_memory = st.session_state.conversation_memory
    if "session_store" not in st.session_state:
        st.session_state.session_store = SessionStore()
    if "session_id" not in st.session_state:
        st.session_state.session_id = st.session_state.session_store.create_session()

    if "messages" not in st.session_state:
        st.session_state.messages = [
            {"role": "assistant", "content": "Hello Boss, How may I help you?"}
        ]

    for message in st.session_state.messages:
        if message["role"] == "user":
            with st.chat_message("user", avatar="👨‍💻"):
                st.markdown(message["content"])

        if message["role"] == "assistant":
            with st.chat_message("assistant", avatar="🤖"):
                st.markdown(message["content"])

        if message["role"] == "thought":
            with st.chat_message("thought", avatar="🧠"):
                with st.expander("**Action Plan (Click to Expand)**"):
                    st.caption(message["content"])

    user_input = st.chat_input("Ask me anything ✏️")
    if user_input:
        user_input = user_input.strip()
        with st.chat_message("user", avatar="👨‍💻"):
            st.markdown(user_input)
            st.session_state.messages.append({"role": "user", "content": user_input})

        with st.chat_message("thought", avatar="🧠"):
            with st.spinner("Creating Action Plan ..."):
                time.sleep(0.5)
                # * User query is processed here
                action_plan_list, task_queue = generate_action_plan_helper(user_input)
                action_plan_caption = ""
                action_plan_caption = "\n* ".join(action_plan_list)
                if action_plan_list:
                    st.session_state.messages.append(
                        {"role": "thought", "content": action_plan_caption}
                    )
                    with st.expander("**Action Plan (Click to Expand)**"):
                        st.caption(action_plan_caption)

        with st.chat_message("assistant", avatar="🤖"):
            with st.spinner("Running Action Plan ..."):
                time.sleep(0.5)
                response = run_action_plan_helper(task_queue)
                st.markdown(response)
                conversation_memory.save_context({"input": user_input}, {"output": response})
                st.session_state.messages.append({"role": "assistant", "content": response})

    st.session_state.conversation_memory = conversation_memory


if __name__ == "__main__":
    main()
