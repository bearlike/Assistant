#!/usr/bin/env python3
"""Declarative keybindings + user-override loader for the Mewbo TUI (#157).

The session feature owns three global keys — ``ctrl+o`` (transcript),
``ctrl+s`` (sessions) and ``ctrl+l`` (clear + redraw) — declared once here as
data so they bind identically whether the controller wires them via the
post-mount installer (the shipped path) or declaratively on ``MewboApp``
(option B in the brief). Each entry carries its own help text so Textual's
``Footer`` renders it for free.

User overrides live in ``~/.mewbo/keybindings.json`` (a JSON object mapping an
``action`` name to a key, the file-contract that mirrors themes). A bad file is
skipped wholesale — a broken keymap must never crash the App. We deliberately do
NOT bind keys other children own: ``shift+tab`` (permission mode, #154),
``ctrl+p``/``ctrl+r`` (palette/history, #155), ``ctrl+c`` (quit, app.py).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from mewbo_core.common import get_logger

logger = get_logger(name="mewbo.cli.keybindings")

#: Default location of the user override file (mirrors ``~/.mewbo/themes``).
DEFAULT_KEYBINDINGS_PATH = Path("~/.mewbo/keybindings.json")


@dataclass(frozen=True)
class KeyBinding:
    """One global key → action mapping with its Footer help text.

    ``action`` is the bare action name (no ``action_`` prefix); the controller
    or installer routes it to the matching callable. ``key`` is a Textual key
    string (e.g. ``"ctrl+o"``). ``description`` is shown in the Footer.
    """

    action: str
    key: str
    description: str
    show: bool = True


#: The global default bindings this feature owns. Ordered for a stable Footer.
DEFAULT_BINDINGS: tuple[KeyBinding, ...] = (
    KeyBinding("open_transcript", "ctrl+o", "Transcript"),
    KeyBinding("open_sessions", "ctrl+s", "Sessions"),
    KeyBinding("clear_redraw", "ctrl+l", "Clear"),
)


class KeybindingConfig:
    """Load + merge user key overrides onto the declarative defaults.

    Atomic class (DI, no globals): construct with the default bindings and an
    optional override path, then read :meth:`bindings` for the effective map.
    An override re-points an existing *action* to a different key; an entry for
    an unknown action, or a malformed file, is ignored (never raised) so a
    user typo degrades to the default rather than breaking the prompt.
    """

    def __init__(
        self,
        *,
        defaults: Iterable[KeyBinding] = DEFAULT_BINDINGS,
        path: Path | str | None = None,
    ) -> None:
        """Bind the default keymap and the override file path."""
        self._defaults: tuple[KeyBinding, ...] = tuple(defaults)
        self._path = Path(path).expanduser() if path is not None else None

    @classmethod
    def load(
        cls,
        *,
        defaults: Iterable[KeyBinding] = DEFAULT_BINDINGS,
        path: Path | str | None = DEFAULT_KEYBINDINGS_PATH,
    ) -> KeybindingConfig:
        """Construct a config rooted at the user override path (expanded)."""
        return cls(defaults=defaults, path=path)

    def _load_overrides(self) -> dict[str, str]:
        """Return ``{action: key}`` overrides; empty on any error."""
        if self._path is None or not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Skipping bad keybindings file {}: {}", self._path, exc)
            return {}
        if not isinstance(raw, dict):
            logger.warning("Keybindings file {} is not a JSON object; ignoring.", self._path)
            return {}
        overrides: dict[str, str] = {}
        for action, key in raw.items():
            if isinstance(action, str) and isinstance(key, str) and action.strip() and key.strip():
                overrides[action] = key.strip()
        return overrides

    def bindings(self) -> list[KeyBinding]:
        """Return the effective bindings (defaults with overrides applied).

        Order follows :data:`DEFAULT_BINDINGS`; an override only changes the
        ``key`` of a known action — it never adds or removes an action.
        """
        overrides = self._load_overrides()
        merged: list[KeyBinding] = []
        for default in self._defaults:
            key = overrides.get(default.action, default.key)
            merged.append(
                KeyBinding(default.action, key, default.description, show=default.show)
            )
        return merged

    def describe(self) -> str:
        """Render a human-readable ``key  →  description`` table for ``/keybindings``.

        Includes a note about the override file so the user knows where to
        customise. Pure string output — the caller prints it.
        """
        lines = ["Global keybindings:"]
        for binding in self.bindings():
            lines.append(f"  {binding.key:<10} {binding.description}")
        target = self._path or DEFAULT_KEYBINDINGS_PATH
        lines.append("")
        lines.append(f"Override actions in {target} (JSON: {{\"action\": \"key\"}}).")
        lines.append("Actions: " + ", ".join(b.action for b in self._defaults))
        return "\n".join(lines)


__all__ = [
    "DEFAULT_BINDINGS",
    "DEFAULT_KEYBINDINGS_PATH",
    "KeyBinding",
    "KeybindingConfig",
]
