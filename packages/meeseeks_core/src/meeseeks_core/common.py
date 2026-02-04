#!/usr/bin/env python3
"""Common helpers shared across the assistant runtime."""

from __future__ import annotations

import json
import logging as logging_real
import os
import sys
import time
from importlib import resources
from typing import NamedTuple

import tiktoken
from jinja2 import Environment, PackageLoader
from loguru import logger as loguru_logger


class MockSpeaker(NamedTuple):
    """Simple mock response container used across tools and tests."""

    content: str


def get_mock_speaker() -> type[MockSpeaker]:
    """Return a mock speaker for testing."""
    return MockSpeaker


_LOG_CONFIGURED = False


def _resolve_log_level() -> str:
    level_name = os.getenv("LOG_LEVEL")
    if level_name:
        return level_name.strip().upper()
    return "DEBUG"


def _should_use_cli_dark_logs() -> bool:
    style = os.getenv("MEESEEKS_LOG_STYLE", "")
    if not style:
        style = os.getenv("MEESEEKS_CLI_LOG_STYLE", "")
    return style.lower() == "dark"


def _configure_logging() -> None:
    global _LOG_CONFIGURED
    if _LOG_CONFIGURED:
        return
    log_level = _resolve_log_level()
    loggers_to_suppress = [
        "request",
        "httpcore",
        "urllib3.connectionpool",
        "openai._base_client",
        "aiohttp_client_cache.signatures",
        "LangChainDeprecationWarning",
        "watchdog.observers.inotify_buffer",
        "PIL.PngImagePlugin",
    ]
    for logger_name in loggers_to_suppress:
        logging_real.getLogger(logger_name).setLevel(logging_real.ERROR)

    loguru_logger.remove()
    colorize = sys.stderr.isatty()
    if _should_use_cli_dark_logs():
        format_str = (
            "<dim>{time:YYYY-MM-DD HH:mm:ss} [{extra[name]}] "
            "<level>{level}</level> {message}{exception}</dim>"
        )
    else:
        format_str = "{time:YYYY-MM-DD HH:mm:ss} [{extra[name]}] <level>{level}</level> {message}"
    loguru_logger.add(sys.stderr, level=log_level, format=format_str, colorize=colorize)
    _LOG_CONFIGURED = True


def get_logger(name: str | None = None):
    """Get the logger for the module."""
    _configure_logging()
    if not name:
        name = __name__
    return loguru_logger.bind(name=name)


def num_tokens_from_string(string: str, encoding_name: str = "cl100k_base") -> int:
    """Get the number of tokens in a string using a specific model."""
    # TODO: Add support for dynamic model selection
    encoding = tiktoken.get_encoding(encoding_name)
    num_tokens = len(encoding.encode(string))
    return num_tokens


def get_unique_timestamp() -> int:
    """Get a unique timestamp for the task queue."""
    # Get the number of seconds since epoch (Jan 1, 1970) as a float
    current_timestamp = int(time.time())
    # Convert it to string for uniqueness and consistency
    unique_timestamp = str(current_timestamp)
    # Return the integer version of this string timestamp
    return int("".join(str(x) for x in map(int, unique_timestamp)))


def get_system_prompt(name: str = "action-planner") -> str:
    """Get the system prompt for the task queue."""
    logging = get_logger(name="core.common.get_system_prompt")
    prompt_resource = resources.files("meeseeks_core").joinpath("prompts").joinpath(f"{name}.txt")
    with resources.as_file(prompt_resource) as system_prompt_path:
        with open(system_prompt_path, encoding="utf-8") as system_prompt_file:
            system_prompt = system_prompt_file.read()
        logging.debug("Getting system prompt from `{}`", system_prompt_path)
    del logging
    return system_prompt.strip()


def format_action_argument(argument: object) -> str:
    """Format an action argument for logs and prompts."""
    if isinstance(argument, dict):
        return json.dumps(argument, ensure_ascii=True)
    return str(argument)


def ha_render_system_prompt(
    all_entities: object | None = None,
    name: str = "homeassistant-set-state",
) -> str:
    """Render the Home Assistant Jinja2 system prompt."""
    if all_entities is not None:
        all_entities = str(all_entities).strip()
    logging = get_logger(name="core.common.render_system_prompt")

    # TODO: Catch and log TemplateNotFound when necessary.
    template_env = Environment(loader=PackageLoader("meeseeks_core", "prompts"))
    template = template_env.get_template(f"{name}.txt")
    logging.debug("Render system prompt for `{}`", name)
    del logging

    return template.render(ALL_ENTITIES=all_entities)
