#!/usr/bin/env python3
"""Shared CLI context types."""

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass

from rich.console import Console

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from core.session_store import SessionStore  # noqa: E402
from core.tool_registry import ToolRegistry  # noqa: E402


@dataclass
class CliState:
    """State persisted across CLI interactions."""
    session_id: str
    show_plan: bool = True
    model_name: str | None = None
    auto_approve_all: bool = False


@dataclass
class CommandContext:
    """Context passed to CLI command handlers."""
    console: Console
    store: SessionStore
    state: CliState
    tool_registry: ToolRegistry
    prompt_func: Callable[[str], str] | None
