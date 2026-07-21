#!/usr/bin/env python3
"""Command palette provider + the rich input installer.

Two things live here:

- :class:`MewboCommandProvider` — a Textual :class:`~textual.command.Provider`
  for the built-in command palette (``ctrl+p``). It surfaces CLI commands +
  user-invocable skills + markdown custom commands + (optionally) MCP prompts.
  We use Textual's built-in palette rather than hand-rolling one;
  selecting an entry fills the input with the ``/command`` token (or the custom
  command's rendered body) ready to submit.
- :func:`make_input_installer` — the post-mount :data:`AppInstaller` the
  controller appends to ``cli_master._build_installers``. It (1) injects the
  rich :class:`CompletionEngine` into the mounted :class:`InputArea`, (2)
  registers the palette provider on the live app, and (3) shares a
  :class:`PromptHistory` with the widget.

The candidate sources are read live from a small :class:`InputContext` stashed
on the app instance, so the provider (constructed by Textual with only the
screen) can reach them without the app being modified.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from textual.command import DiscoveryHit, Hit, Hits, Provider

from mewbo_cli.tui.input.completion import CommandCandidate, CompletionEngine
from mewbo_cli.tui.input.custom_commands import CustomCommand, CustomCommandLoader
from mewbo_cli.tui.input.history import PromptHistory
from mewbo_cli.tui.widgets.input_area import InputArea, default_files

if TYPE_CHECKING:
    from mewbo_cli.tui.app import MewboApp

#: Attribute name under which the input context is stashed on the app instance.
_CONTEXT_ATTR = "_mewbo_input_context"

# An MCP-prompt provider maps to (name, description) pairs. None is exposed
# today (the tool registry has no prompt surface) — the installer defaults to an
# empty provider so the palette degrades gracefully; wire one here when MCP
# prompt discovery lands.
McpPromptsProvider = Callable[[], Sequence[tuple[str, str]]]


@dataclass
class InputContext:
    """Live candidate sources for the palette + completion, stashed on the app."""

    command_names: Callable[[], Sequence[str]]
    skill_candidates: Callable[[], Sequence[tuple[str, str]]]
    custom_commands: Callable[[], Sequence[CustomCommand]]
    mcp_prompts: McpPromptsProvider = field(default=lambda: [])

    def command_candidates(self) -> list[CommandCandidate]:
        """All ``/`` candidates merged (commands + skills + custom + MCP prompts)."""
        out: list[CommandCandidate] = []
        seen: set[str] = set()

        def _add(name: str, desc: str, kind: str, hint: str = "") -> None:
            key = name.lstrip("/")
            if key in seen:
                return
            seen.add(key)
            out.append(
                CommandCandidate(name=key, description=desc, kind=kind, argument_hint=hint)
            )

        for name in _safe(self.command_names, []):
            _add(name, "", "command")
        for name, desc in _safe(self.skill_candidates, []):
            _add(name, desc, "skill")
        for cmd in _safe(self.custom_commands, []):
            _add(cmd.name, cmd.description, "custom", cmd.argument_hint)
        for name, desc in _safe(self.mcp_prompts, []):
            _add(name, desc, "mcp-prompt")
        return out


def _safe(fn: Callable[[], Any], default: Any) -> Any:
    try:
        return fn()
    except Exception:
        return default


class MewboCommandProvider(Provider):
    """Surface CLI commands + skills + custom commands + MCP prompts in the palette."""

    def _context(self) -> InputContext | None:
        return getattr(self.app, _CONTEXT_ATTR, None)

    def _fill_input(self, token: str) -> None:
        """Place ``token`` (e.g. ``/help``) into the input, ready to edit/submit."""
        try:
            widget = self.app.query_one(InputArea)
        except Exception:
            return
        widget.value = token if token.endswith(" ") else f"{token} "
        widget.cursor_position = len(widget.value)
        self.app.set_focus(widget)

    async def discover(self) -> Hits:
        """Show all commands when the palette opens with an empty query."""
        ctx = self._context()
        if ctx is None:
            return
        for cand in ctx.command_candidates():
            token = f"/{cand.name}"
            yield DiscoveryHit(
                display=token,
                command=lambda t=token: self._fill_input(t),
                text=token,
                help=cand.description or cand.kind,
            )

    async def search(self, query: str) -> Hits:
        """Fuzzy-rank commands against ``query`` via Textual's matcher."""
        ctx = self._context()
        if ctx is None:
            return
        matcher = self.matcher(query)
        for cand in ctx.command_candidates():
            token = f"/{cand.name}"
            score = matcher.match(token)
            if score > 0:
                yield Hit(
                    score=score,
                    match_display=matcher.highlight(token),
                    command=lambda t=token: self._fill_input(t),
                    text=token,
                    help=cand.description or cand.kind,
                )


def make_input_installer(
    *,
    command_registry: Any,
    skill_registry: Any,
    cwd_provider: Callable[[], str] | None = None,
    mcp_prompts_provider: McpPromptsProvider | None = None,
) -> Callable[[MewboApp], None]:
    """Build the post-mount installer for the rich input.

    Args:
        command_registry: Has ``list_commands() -> list[str]`` (``/``-prefixed).
        skill_registry: Has ``list_user_invocable()`` → objects with
            ``.name``/``.description`` (may be ``None``).
        cwd_provider: Directory whose files ``@`` suggests + whose
            ``.claude/commands`` is scanned (defaults to the process cwd).
        mcp_prompts_provider: Optional ``() -> [(name, description)]`` for MCP
            prompts in the palette; defaults to none (graceful no-op).

    Returns:
        An :data:`AppInstaller`: injects the :class:`CompletionEngine` +
        :class:`PromptHistory` into the mounted :class:`InputArea` and registers
        :class:`MewboCommandProvider` on the live app.
    """
    import os

    cwd_fn = cwd_provider or os.getcwd

    def _command_names() -> list[str]:
        return list(command_registry.list_commands())

    def _skill_candidates() -> list[tuple[str, str]]:
        if skill_registry is None:
            return []
        return [
            (s.name, getattr(s, "description", "") or "")
            for s in skill_registry.list_user_invocable()
        ]

    loader = CustomCommandLoader(cwd=cwd_fn())

    context = InputContext(
        command_names=_command_names,
        skill_candidates=_skill_candidates,
        custom_commands=loader.load,
        mcp_prompts=mcp_prompts_provider or (lambda: []),
    )
    history = PromptHistory()
    files_provider = default_files(cwd_fn)

    def _install(app: MewboApp) -> None:
        # 1. Stash the live candidate sources where the provider can read them.
        setattr(app, _CONTEXT_ATTR, context)

        # 2. Inject the rich engine + shared history into the mounted InputArea.
        widget = app.query_one(InputArea)
        engine = CompletionEngine(
            files_provider=files_provider,
            commands_provider=context.command_candidates,
        )
        widget.set_completion_engine(engine)
        widget.set_history(history)

        # 3. Register the palette provider (instance-level COMMANDS so we never
        #    mutate the shared class set). ctrl+p stays Textual's default key.
        app.COMMANDS = {*type(app).COMMANDS, MewboCommandProvider}

    return _install


__all__ = [
    "InputContext",
    "McpPromptsProvider",
    "MewboCommandProvider",
    "make_input_installer",
]
