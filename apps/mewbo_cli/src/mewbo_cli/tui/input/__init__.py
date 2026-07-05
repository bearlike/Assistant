#!/usr/bin/env python3
"""Rich sigil-dispatched input for the Mewbo TUI (issue #155, epic #149).

This package owns the input-area behaviour layered onto the foundation
:class:`~mewbo_cli.tui.widgets.input_area.InputArea`:

- :mod:`completion` — :class:`CompletionEngine`, a pure (text, cursor) →
  ranked-candidates engine: ``@`` caret-anchored tiered file ranking, ``/``
  fuzzy command/skill ranking with match highlight + argument hints, ``!`` bash.
- :mod:`custom_commands` — :class:`CustomCommandLoader`, a small atomic loader
  for markdown ``.claude/commands/*.md`` (+ ``~/.mewbo/commands``) custom
  commands (frontmatter + ``$ARGUMENTS`` template + subdir namespacing).
- :mod:`history` — :class:`PromptHistory`, persistent prompt history with
  reverse search (``ctrl+r``).
- :mod:`palette` — Textual :class:`~textual.command.Provider` subclasses for the
  built-in command palette (commands + skills + MCP prompts) and
  :func:`make_input_installer`, the post-mount installer the controller wires.

The package depends only on the foundation seams + existing helpers
(``FileCatalog``, ``cli_commands``); it never edits ``app.py``.
"""

from __future__ import annotations
