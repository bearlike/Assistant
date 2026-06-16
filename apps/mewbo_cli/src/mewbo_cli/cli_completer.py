#!/usr/bin/env python3
"""Live prompt completion for the Mewbo CLI REPL.

One atomic completer drives two affordances at the ``mewbo>`` prompt:

- ``@<partial>`` → project files (git index first, walk fallback) via the shared
  :class:`~mewbo_tools.integration.file_catalog.FileCatalog` — the SAME file set
  the ``@<ref>`` expander scopes against, so a suggestion always resolves.
- ``/<partial>`` at the start of the line → registered CLI commands AND
  user-invocable skills (the dispatcher tries commands first, then skills, so we
  offer both).

The file list is cached per-cwd (a fresh :class:`FileCatalog`, itself caching
its git probe) so per-keystroke completion stays cheap. ``get_completions``
never raises — a failed git call or a missing registry degrades to no
suggestions rather than breaking the prompt.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING

from mewbo_tools.integration.file_catalog import FileCatalog
from prompt_toolkit.completion import Completer, Completion

if TYPE_CHECKING:
    from mewbo_core.skills import SkillRegistry
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

# Cap suggestions so a huge repo / skill set never floods the dropdown.
_MAX_FILE_SUGGESTIONS = 50
_FILE_CATALOG_LIMIT = 5000


class MewboCompleter(Completer):
    """Slash-command/skill and ``@``-file completion for the CLI prompt.

    Construct once after the command + skill registries exist; pass the
    instance (wrapped in a ``ThreadedCompleter``) to the ``PromptSession``.
    """

    def __init__(
        self,
        command_names: list[str],
        skill_registry: SkillRegistry | None,
        cwd_provider: Callable[[], str] = os.getcwd,
    ) -> None:
        """Bind the static command names, the skill registry, and a cwd source.

        Args:
            command_names: Registered command tokens (each leading with ``/``).
            skill_registry: Source of user-invocable skills (may be ``None``).
            cwd_provider: Returns the directory whose files ``@`` suggests;
                defaults to the process cwd (what the CLI engine expands against).
        """
        # Strip the leading "/" so command + skill names share one namespace
        # after the slash the user already typed.
        self._command_names = sorted({name.lstrip("/") for name in command_names})
        self._skill_registry = skill_registry
        self._cwd_provider = cwd_provider
        self._cached_cwd: str | None = None
        self._cached_files: list[str] = []

    # -- file list cache ------------------------------------------------

    def _files_for_cwd(self) -> list[str]:
        """Project files for the current cwd, rebuilt only when cwd changes."""
        try:
            cwd = self._cwd_provider()
        except Exception:  # pragma: no cover - defensive
            return []
        if cwd != self._cached_cwd:
            try:
                self._cached_files = FileCatalog(cwd).list_files(limit=_FILE_CATALOG_LIMIT)
            except Exception:
                self._cached_files = []
            self._cached_cwd = cwd
        return self._cached_files

    # -- prompt_toolkit interface --------------------------------------

    def get_completions(self, document: Document, complete_event: CompleteEvent):
        """Yield completions for the active ``/`` command or ``@`` file token."""
        try:
            text = document.text_before_cursor
            slash = self._slash_completions(text)
            if slash is not None:
                yield from slash
                return
            yield from self._mention_completions(text)
        except Exception:  # pragma: no cover - never break the prompt
            return

    # -- slash (commands + skills) -------------------------------------

    def _slash_completions(self, text: str):
        """Command + skill completions, or ``None`` when not in slash mode.

        Slash mode requires a leading ``/`` (after left-trim) with no space yet —
        i.e. the cursor is still inside the first ``/word``.
        """
        stripped = text.lstrip()
        if not stripped.startswith("/"):
            return None
        partial = stripped[1:]
        if any(ch.isspace() for ch in partial):
            return None

        start = -len(partial)
        seen: set[str] = set()
        results: list[Completion] = []
        for name in self._command_names:
            if name.startswith(partial) and name not in seen:
                seen.add(name)
                results.append(
                    Completion(name, start_position=start, display_meta="command")
                )
        for name in self._skill_names():
            if name.startswith(partial) and name not in seen:
                seen.add(name)
                results.append(
                    Completion(name, start_position=start, display_meta="skill")
                )
        return results

    def _skill_names(self) -> list[str]:
        if self._skill_registry is None:
            return []
        try:
            return [s.name for s in self._skill_registry.list_user_invocable()]
        except Exception:
            return []

    # -- mention (project files) ---------------------------------------

    def _mention_completions(self, text: str):
        """File completions for the active ``@partial`` token, if any."""
        partial = self._active_mention(text)
        if partial is None:
            return
        files = self._files_for_cwd()
        start = -len(partial)
        prefix: list[Completion] = []
        substr: list[Completion] = []
        for path in files:
            if partial and path.startswith(partial):
                prefix.append(Completion(path, start_position=start, display_meta="file"))
            elif partial and partial in path:
                substr.append(Completion(path, start_position=start, display_meta="file"))
            elif not partial:
                prefix.append(Completion(path, start_position=start, display_meta="file"))
            if len(prefix) >= _MAX_FILE_SUGGESTIONS:
                break
        combined = (prefix + substr)[:_MAX_FILE_SUGGESTIONS]
        yield from combined

    @staticmethod
    def _active_mention(text: str) -> str | None:
        """The ``@`` token under the cursor, or ``None``.

        A token is the last ``@`` that is at start-of-text or preceded by
        whitespace, with no whitespace between it and the cursor — mirroring the
        ``@<ref>`` expander's boundary so ``bob@host`` never triggers.
        """
        idx = text.rfind("@")
        if idx < 0:
            return None
        if idx > 0 and not text[idx - 1].isspace():
            return None
        partial = text[idx + 1 :]
        if any(ch.isspace() for ch in partial):
            return None
        return partial
