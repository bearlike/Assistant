#!/usr/bin/env python3
"""Core error types for tool/runtime coordination."""

from __future__ import annotations


class ToolInputError(Exception):
    """Raised when a tool input is invalid but the tool remains healthy."""


__all__ = ["ToolInputError"]
