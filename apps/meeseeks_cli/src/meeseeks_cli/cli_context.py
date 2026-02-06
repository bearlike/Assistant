#!/usr/bin/env python3
"""Shared CLI context types."""

from collections.abc import Callable
from dataclasses import dataclass

from meeseeks_core.session_store import SessionStore
from meeseeks_core.tool_registry import ToolRegistry
from rich.console import Console


@dataclass
class CliState:
    """State persisted across CLI interactions."""

    session_id: str
    show_plan: bool = True
    model_name: str | None = None
    auto_approve_all: bool = False
    mode: str = "act"


@dataclass
class CommandContext:
    """Context passed to CLI command handlers."""

    console: Console
    store: SessionStore
    state: CliState
    tool_registry: ToolRegistry
    prompt_func: Callable[[str], str] | None
