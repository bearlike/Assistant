#!/usr/bin/env python3
"""Shared Textual content-markup helpers for sidebar widgets.

``TodoPanel`` and ``StatusBar`` both render their content as Textual markup
strings (``[$var]…[/]``) handed straight to ``Static.update`` so theme color
variables resolve. Untrusted text (agent ids, task labels, branches, …) must
have markup metacharacters escaped first; that escape lives here so both widgets
share ONE implementation (DRY).
"""

from __future__ import annotations


def escape_markup(text: str) -> str:
    """Escape Textual markup metacharacters so ``text`` renders literally.

    A backslash and an opening bracket are the two characters Textual's markup
    parser treats specially; doubling/escaping them keeps arbitrary content from
    being interpreted as a tag.
    """
    return text.replace("\\", "\\\\").replace("[", "\\[")


__all__ = ["escape_markup"]
