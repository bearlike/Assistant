"""Semantic theme system for the Mewbo CLI.

Provides the canonical Palette dataclass (15 roles), build_theme() factory,
and ThemeManager (atomic class) for registering and switching Textual themes.

This is the keystone palette contract imported by DiffView (#153) and the rest
of the full-Textual rewrite (epic #149). Injected, never global.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import textual.app
from textual.theme import Theme

# ---------------------------------------------------------------------------
# Palette role contract — binding, do not rename
# ---------------------------------------------------------------------------

ROLE_NAMES: tuple[str, ...] = (
    "primary",
    "secondary",
    "accent",
    "fg_base",
    "bg_base",
    "muted",
    "border",
    "error",
    "warning",
    "success",
    "user",
    "assistant",
    "diff_add",
    "diff_del",
    "diff_eq",
)


@dataclass(frozen=True)
class Palette:
    """Semantic color roles for the Mewbo CLI theme.

    All fields are hex strings (e.g. ``#rrggbb``).  The order and names are a
    binding cross-component contract — do NOT rename or reorder.
    """

    primary: str
    secondary: str
    accent: str
    fg_base: str
    bg_base: str
    muted: str
    border: str
    error: str
    warning: str
    success: str
    user: str
    assistant: str
    diff_add: str
    diff_del: str
    diff_eq: str


# ---------------------------------------------------------------------------
# Built-in palettes
# ---------------------------------------------------------------------------

DEFAULT_PALETTE = Palette(
    primary="#7c6af7",
    secondary="#5b5bd6",
    accent="#a78bfa",
    fg_base="#e2e0fa",
    bg_base="#0e0e10",
    muted="#4b4b6b",
    border="#2e2e4e",
    error="#f87171",
    warning="#fbbf24",
    success="#34d399",
    user="#60a5fa",
    assistant="#a78bfa",
    diff_add="#166534",
    diff_del="#7f1d1d",
    diff_eq="#1e3a5f",
)

HYPER_PALETTE = Palette(
    primary="#c026d3",
    secondary="#7c3aed",
    accent="#f0abfc",
    fg_base="#f5e6ff",
    bg_base="#09000f",
    muted="#4b2060",
    border="#3b1050",
    error="#ff4d6d",
    warning="#ffbd00",
    success="#00e5a0",
    user="#38bdf8",
    assistant="#e879f9",
    diff_add="#064e3b",
    diff_del="#881337",
    diff_eq="#1e1b4b",
)

LIGHT_PALETTE = Palette(
    primary="#4f46e5",
    secondary="#6366f1",
    accent="#7c3aed",
    fg_base="#1e1b4b",
    bg_base="#f5f5ff",
    muted="#a0a0c0",
    border="#c7c7e0",
    error="#dc2626",
    warning="#d97706",
    success="#16a34a",
    user="#1d4ed8",
    assistant="#6d28d9",
    diff_add="#bbf7d0",
    diff_del="#fecaca",
    diff_eq="#dbeafe",
)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_theme(palette: Palette, *, name: str, dark: bool = True) -> Theme:
    """Build a Textual ``Theme`` from a ``Palette``.

    Native Theme fields are mapped directly; the remaining roles go into
    ``variables`` with exact underscore keys so they surface as CSS vars
    verbatim (``$diff_add``, ``$diff_del``, etc.).

    Args:
        palette: Source palette.
        name: Theme identifier registered with Textual.
        dark: Whether this is a dark theme.

    Returns:
        A :class:`textual.theme.Theme` ready for ``app.register_theme()``.
    """
    return Theme(
        name=name,
        dark=dark,
        primary=palette.primary,
        secondary=palette.secondary,
        accent=palette.accent,
        error=palette.error,
        warning=palette.warning,
        success=palette.success,
        foreground=palette.fg_base,
        background=palette.bg_base,
        variables={
            # fg_base/bg_base also exposed verbatim so the full 15-role contract
            # is reachable as CSS vars ($fg_base/$bg_base), alongside the native
            # $foreground/$background that Textual's built-in widgets consume.
            "fg_base": palette.fg_base,
            "bg_base": palette.bg_base,
            "muted": palette.muted,
            "border": palette.border,
            "user": palette.user,
            "assistant": palette.assistant,
            "diff_add": palette.diff_add,
            "diff_del": palette.diff_del,
            "diff_eq": palette.diff_eq,
        },
    )


# ---------------------------------------------------------------------------
# Light/dark detection
# ---------------------------------------------------------------------------


def detect_terminal_is_dark() -> bool:
    """Return True when the terminal background appears dark.

    Reads ``COLORFGBG`` (e.g. ``"15;0"`` → dark bg, ``"0;15"`` → light bg).
    The last segment is the background colour index: index < 8 is dark.
    Defaults to ``True`` (dark) when the variable is absent or unparseable.
    """
    raw = os.environ.get("COLORFGBG", "")
    if raw:
        parts = raw.split(";")
        try:
            bg_index = int(parts[-1])
            return bg_index < 8
        except ValueError:
            pass
    return True


def auto_palette() -> Palette:
    """Return ``DEFAULT_PALETTE`` or ``LIGHT_PALETTE`` based on terminal detection."""
    return DEFAULT_PALETTE if detect_terminal_is_dark() else LIGHT_PALETTE


# ---------------------------------------------------------------------------
# ThemeManager (atomic class)
# ---------------------------------------------------------------------------


class ThemeManager:
    """Manages available Textual themes (built-ins + user JSON themes).

    Atomic class: state attrs + class/static methods + DI.  Never imports a
    global; the caller injects built-in themes and an optional themes directory.

    JSON theme file format::

        {
            "name": "my-theme",
            "dark": true,
            "palette": {
                "primary": "#hex", ...  // all 15 ROLE_NAMES required
            }
        }

    Bad files are skipped with a warning; the CLI never crashes on one bad file.
    """

    def __init__(
        self,
        themes: Iterable[Theme],
        themes_dir: Path | None = None,
    ) -> None:
        """Initialise with built-in themes and an optional user themes directory.

        Args:
            themes: Pre-built :class:`textual.theme.Theme` instances to include.
            themes_dir: Directory to scan for ``*.json`` theme files (default
                ``~/.mewbo/themes/``).  Call :meth:`load_json_themes` to scan it.
        """
        self._themes: dict[str, Theme] = {t.name: t for t in themes}
        self._themes_dir: Path = themes_dir or Path.home() / ".mewbo" / "themes"

    # ------------------------------------------------------------------
    # Loader
    # ------------------------------------------------------------------

    def load_json_themes(self) -> None:
        """Read ``*.json`` files from the themes directory and register them.

        Silently skips files that are malformed or missing required palette roles.
        """
        if not self._themes_dir.is_dir():
            return
        for path in self._themes_dir.glob("*.json"):
            self._load_one(path)

    def _load_one(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            name: str = data["name"]
            dark: bool = bool(data.get("dark", True))
            raw_palette: dict[str, str] = data["palette"]
            # Validate all roles present
            missing = [r for r in ROLE_NAMES if r not in raw_palette]
            if missing:
                return  # skip silently
            palette = Palette(**{r: raw_palette[r] for r in ROLE_NAMES})
            theme = build_theme(palette, name=name, dark=dark)
            self._themes[theme.name] = theme
        except Exception:  # noqa: BLE001 — never crash the CLI on bad theme file
            pass

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_all(self, app: textual.app.App[object]) -> None:
        """Register every available theme with a Textual app."""
        for theme in self._themes.values():
            app.register_theme(theme)

    # ------------------------------------------------------------------
    # Query / switch
    # ------------------------------------------------------------------

    def list_themes(self) -> list[str]:
        """Return sorted theme names."""
        return sorted(self._themes)

    def switch(self, app: textual.app.App[object], name: str) -> bool:
        """Set ``app.theme = name``.  Returns False if the name is unknown."""
        if name not in self._themes:
            return False
        app.theme = name
        return True

    # ------------------------------------------------------------------
    # /theme command handler (testable unit; caller wires CLI routing)
    # ------------------------------------------------------------------

    def apply_command(self, app: textual.app.App[object] | None, arg: str | None) -> str:
        """Implement the ``/theme`` CLI command.

        * No arg / ``"list"`` → human-readable list (marks active theme).
        * A theme name → switch and return confirmation, or error if unknown.
        """
        if arg is None or arg.strip().lower() in ("", "list"):
            names = self.list_themes()
            if not names:
                return "No themes registered."
            active = getattr(app, "theme", None) if app is not None else None
            lines = [
                f"  {'*' if n == active else ' '} {n}" for n in names
            ]
            return "Available themes:\n" + "\n".join(lines)

        name = arg.strip()
        if name not in self._themes:
            return f"Unknown theme '{name}'. Use /theme list to see available themes."
        if app is not None and self.switch(app, name):
            return f"Theme switched to '{name}'."
        return f"Theme '{name}' selected (no active app to apply)."
