#!/usr/bin/env python3
"""CustomCommandLoader — markdown custom commands for the CLI.

A small atomic loader for the common custom-command contract (frontmatter +
``$ARGUMENTS``) and is a sibling to (never a fork of) the engine prompt
registry: it discovers
``*.md`` files under two roots, lowest→highest precedence:

1. ``~/.mewbo/commands/`` (user-global)
2. ``<cwd>/.claude/commands/`` (project; wins on a name clash)

Each file is YAML frontmatter (``description``, ``argument-hint``,
``allowed-tools``) + a markdown body that is the prompt template. ``$ARGUMENTS``
in the body is replaced with whatever the user typed after the command. A
subdirectory namespaces the command: ``frontend/component.md`` →
``/frontend:component``.

The loader globs + parses once and caches the result (``load()`` is cheap to
call per keystroke; :meth:`reload` forces a re-scan). It never raises out of
:meth:`load` — a malformed file is skipped with a debug log, never breaking the
prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from mewbo_core.common import get_logger

logging = get_logger(name="cli.custom_commands")

# Same frontmatter delimiter contract as the skills loader (DRY-in-spirit).
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)

#: Default roots, lowest→highest precedence. Project overrides user on a clash.
_USER_ROOT = Path.home() / ".mewbo" / "commands"
_PROJECT_SUBPATH = (".claude", "commands")


@dataclass(frozen=True)
class CustomCommand:
    """One discovered markdown custom command.

    ``name`` is the namespaced token without the leading slash
    (``"frontend:component"``). ``body`` is the raw template (``$ARGUMENTS`` not
    yet substituted); call :meth:`render` to expand it.
    """

    name: str
    description: str
    argument_hint: str
    body: str
    allowed_tools: tuple[str, ...]
    source: str  # "project" | "user"

    def render(self, arguments: str = "") -> str:
        """The body with ``$ARGUMENTS`` replaced by ``arguments`` (trimmed)."""
        return self.body.replace("$ARGUMENTS", arguments.strip())


class CustomCommandLoader:
    """Discover + parse markdown custom commands under the user/project roots."""

    def __init__(
        self,
        *,
        cwd: str | None = None,
        user_root: Path | None = None,
    ) -> None:
        """Bind the discovery roots.

        Args:
            cwd: Project directory whose ``.claude/commands`` is scanned
                (``None`` → no project root).
            user_root: Override the user-global root (defaults to
                ``~/.mewbo/commands``); handy for tests.
        """
        self._cwd = Path(cwd) if cwd else None
        self._user_root = user_root if user_root is not None else _USER_ROOT
        self._cache: list[CustomCommand] | None = None

    def load(self) -> list[CustomCommand]:
        """All commands (cached), project overriding user on a name clash.

        Globs + parses once on the first call and caches the result, so reuse as
        a per-keystroke completion source is cheap; call :meth:`reload` to pick
        up files added since construction. Returned sorted by name. Never raises
        — unreadable roots/files are skipped.
        """
        if self._cache is None:
            self._cache = self._discover()
        return self._cache

    def reload(self) -> list[CustomCommand]:
        """Force a re-glob/re-parse (drops the cache) and return the fresh list."""
        self._cache = self._discover()
        return self._cache

    def _discover(self) -> list[CustomCommand]:
        commands: dict[str, CustomCommand] = {}
        # User first (lowest precedence), then project so it overwrites.
        for command in self._scan(self._user_root, source="user"):
            commands[command.name] = command
        if self._cwd is not None:
            project_root = self._cwd.joinpath(*_PROJECT_SUBPATH)
            for command in self._scan(project_root, source="project"):
                commands[command.name] = command
        return [commands[name] for name in sorted(commands)]

    # -- internals ------------------------------------------------------

    def _scan(self, root: Path, *, source: str) -> list[CustomCommand]:
        if not root.is_dir():
            return []
        out: list[CustomCommand] = []
        try:
            paths = sorted(root.rglob("*.md"))
        except OSError as exc:  # pragma: no cover - defensive
            logging.debug("custom-command scan failed for {}: {}", root, exc)
            return []
        for path in paths:
            command = self._parse(path, root, source=source)
            if command is not None:
                out.append(command)
        return out

    @staticmethod
    def _name_for(path: Path, root: Path) -> str:
        """Namespaced command name: ``frontend/component.md`` → ``frontend:component``."""
        rel = path.relative_to(root).with_suffix("")
        return ":".join(rel.parts)

    def _parse(self, path: Path, root: Path, *, source: str) -> CustomCommand | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            logging.debug("failed to read custom command {}: {}", path, exc)
            return None

        meta: dict = {}
        body = raw
        match = _FRONTMATTER_RE.match(raw)
        if match is not None:
            try:
                parsed = yaml.safe_load(match.group(1))
            except yaml.YAMLError as exc:
                logging.debug("invalid frontmatter in {}: {}", path, exc)
                parsed = None
            if isinstance(parsed, dict):
                meta = parsed
            body = match.group(2)

        name = self._name_for(path, root)
        if not name:
            return None
        return CustomCommand(
            name=name,
            description=str(meta.get("description", "")).strip(),
            argument_hint=str(meta.get("argument-hint", "")).strip(),
            body=body,
            allowed_tools=_as_tuple(meta.get("allowed-tools")),
            source=source,
        )


def _as_tuple(value: object) -> tuple[str, ...]:
    """Normalise an ``allowed-tools`` value (list or comma string) to a tuple."""
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


__all__ = ["CustomCommand", "CustomCommandLoader"]
