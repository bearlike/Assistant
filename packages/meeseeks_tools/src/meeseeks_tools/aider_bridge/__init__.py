"""Aider-derived helpers for Meeseeks tooling."""

from .edit_blocks import (
    DEFAULT_FENCE,
    EditBlock,
    EditBlockApplyError,
    EditBlockParseError,
    apply_search_replace_blocks,
    parse_search_replace_blocks,
)

__all__ = [
    "DEFAULT_FENCE",
    "EditBlock",
    "EditBlockApplyError",
    "EditBlockParseError",
    "apply_search_replace_blocks",
    "parse_search_replace_blocks",
]
