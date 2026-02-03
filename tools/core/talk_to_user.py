#!/usr/bin/env python3
"""Echo tool for simple user responses.

Talk to User is a basic tool that accepts user input and returns it as output.
It serves as a minimal example and can be extended with filters or validators.
"""

from __future__ import annotations

# Third-party modules
from dotenv import load_dotenv

from core.classes import AbstractTool, ActionStep

# User-defined modules
from core.common import MockSpeaker, get_logger, get_mock_speaker

load_dotenv()
logging = get_logger(name="tools.core.talk_to_user")


class TalkToUser(AbstractTool):
    """Tool that returns the user's message back as the response."""

    def __init__(self) -> None:
        """Initialize the tool metadata."""
        super().__init__(
            name="Talk to User",
            description="Directly talk to the user.",
            use_llm=False,
        )

    def set_state(self, action_step: ActionStep | None = None) -> MockSpeaker:
        """Return the action argument as the response content.

        Args:
            action_step: Action step containing the response text.

        Returns:
            MockSpeaker wrapping the response content.

        Raises:
            ValueError: If action_step is None.
        """
        if action_step is None:
            raise ValueError("Action step cannot be None.")
        MockSpeaker = get_mock_speaker()
        return MockSpeaker(content=action_step.action_argument)

    def get_state(self, action_step: ActionStep | None = None) -> MockSpeaker:
        """TalkToUser does not support read operations.

        Args:
            action_step: Ignored action step.

        Raises:
            NotImplementedError: Always, because GET is unsupported.
        """
        raise NotImplementedError("This method is not supported by TalkToUser.")
