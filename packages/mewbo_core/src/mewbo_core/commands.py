"""Server-side slash command registry.

Commands are the canonical mechanism for triggering session operations from
any client (CLI, console, channel adapters). Each command has a render hint
that tells the consuming UI how to surface the result:

- ``TRANSCRIPT``: result appears in the chat/log (events emitted by caller).
- ``DIALOG``: transient info; UI shows a modal, channels reply with text.
- ``NOTIFICATION``: persisted to NotificationStore; UI may surface a balloon.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from mewbo_core.session_store import SessionStoreBase

logger = logging.getLogger(__name__)


class CommandRender(str, Enum):
    """How a client should surface a command's result."""

    TRANSCRIPT = "transcript"
    DIALOG = "dialog"
    NOTIFICATION = "notification"


@dataclass
class CommandResult:
    """Result of executing a command."""

    render: CommandRender
    title: str
    body: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CommandContext:
    """Per-call context passed to a command handler.

    The optional dependencies allow handlers to be exercised in unit tests
    without standing up the full runtime; handlers degrade gracefully when an
    optional dependency is missing.
    """

    session_id: str
    session_store: SessionStoreBase
    notification_service: Any | None = None
    skill_registry: Any | None = None
    usage_provider: Callable[[str], dict[str, Any]] | None = None
    hook_manager: Any | None = None
    model_name: str | None = None


@dataclass
class CommandDef:
    """A named command and its handler."""

    name: str
    description: str
    usage: str
    render: CommandRender
    handler: Callable[[CommandContext, list[str]], Awaitable[CommandResult]]


class CommandError(Exception):
    """Raised when a command cannot be executed (e.g. bad args)."""


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _handle_help(ctx: CommandContext, args: list[str]) -> CommandResult:
    """List all registered commands."""
    lines = ["**Available commands:**", ""]
    for cmd in COMMANDS.values():
        lines.append(f"- `{cmd.usage}` — {cmd.description}")
    return CommandResult(
        render=CommandRender.DIALOG,
        title="Commands",
        body="\n".join(lines),
    )


async def _handle_skills(ctx: CommandContext, args: list[str]) -> CommandResult:
    """List available skills from the skill registry."""
    if ctx.skill_registry is None:
        return CommandResult(
            render=CommandRender.DIALOG,
            title="Available Skills",
            body="No skills available.",
            metadata={"count": 0},
        )
    skills = ctx.skill_registry.list_all()
    if not skills:
        body = "No skills available."
    else:
        body = "\n".join(f"- **{s.name}** — {s.description}" for s in skills)
    return CommandResult(
        render=CommandRender.DIALOG,
        title="Available Skills",
        body=body,
        metadata={"count": len(skills)},
    )


async def _handle_tokens(ctx: CommandContext, args: list[str]) -> CommandResult:
    """Render a markdown table of session token usage."""
    if ctx.usage_provider is None:
        return CommandResult(
            render=CommandRender.DIALOG,
            title="Token Usage",
            body="Token usage not available.",
        )
    usage = ctx.usage_provider(ctx.session_id)
    rows = [
        ("Total input tokens", usage.get("total_input_tokens", 0)),
        ("Total output tokens", usage.get("total_output_tokens", 0)),
    ]
    lines = ["| Field | Value |", "|---|---|"]
    lines.extend(f"| {label} | {value} |" for label, value in rows)
    return CommandResult(
        render=CommandRender.DIALOG,
        title="Token Usage",
        body="\n".join(lines),
        metadata=dict(usage),
    )


async def _handle_fork(ctx: CommandContext, args: list[str]) -> CommandResult:
    """Fork the current session, optionally tagging the new session."""
    new_id = ctx.session_store.fork_session(ctx.session_id)
    tag = args[0] if args else None
    if tag:
        ctx.session_store.tag_session(new_id, tag)
    body = (
        f"Forked session as `{new_id}`"
        + (f" with tag `{tag}`" if tag else "")
        + "."
    )
    if ctx.notification_service is not None:
        ctx.notification_service.notify(
            title="Session forked",
            message=body,
            session_id=ctx.session_id,
            metadata={"new_session_id": new_id, "tag": tag},
        )
    return CommandResult(
        render=CommandRender.NOTIFICATION,
        title="Session forked",
        body=body,
        metadata={"new_session_id": new_id, "tag": tag},
    )


async def _handle_tag(ctx: CommandContext, args: list[str]) -> CommandResult:
    """Tag the current session with a quick-lookup label."""
    if not args:
        raise CommandError("Usage: /tag NAME")
    tag = args[0]
    ctx.session_store.tag_session(ctx.session_id, tag)
    body = f"Session tagged as `{tag}`."
    if ctx.notification_service is not None:
        ctx.notification_service.notify(
            title="Session tagged",
            message=body,
            session_id=ctx.session_id,
            metadata={"tag": tag},
        )
    return CommandResult(
        render=CommandRender.NOTIFICATION,
        title="Session tagged",
        body=body,
        metadata={"tag": tag},
    )


async def _handle_compact(ctx: CommandContext, args: list[str]) -> CommandResult:
    """Compact the session transcript via the existing compaction pipeline.

    Accepts an optional free-form focus directive (Codex-style
    ``/compact <focus>``) that biases the summarizer toward the
    user's current concern without dropping critical state.
    """
    from mewbo_core.common import num_tokens_from_string
    from mewbo_core.compact import CompactionMode, record_compaction

    focus = " ".join(args).strip() or None
    result = await ctx.session_store.compact_session(
        ctx.session_id, mode=CompactionMode.FULL, focus_prompt=focus
    )
    # Persist marker + run on_compact hook through the same recorder the
    # auto path uses, so telemetry, ContextBuilder boundary detection, and
    # external on_compact hooks treat user-triggered compaction identically.
    tokens_after = num_tokens_from_string(result.summary)
    tokens_before = result.tokens_saved + tokens_after
    record_compaction(
        ctx.session_store,
        ctx.hook_manager,
        ctx.session_id,
        summary=result.summary,
        mode="user",
        model=result.model or (ctx.model_name or ""),
        tokens_before=tokens_before,
        tokens_saved=result.tokens_saved,
        events_summarized=result.events_summarized,
    )
    # Keep the chat-bubble body short and stable. The full summary is on
    # the ``context_compacted`` event we just recorded — clients should
    # render that via their compaction log component (the console does so
    # in LogsView). Duplicating the summary in the transcript drowns out
    # the marker card and makes the chat scroll unusable for long runs.
    body = (
        "Compaction complete. Earlier messages are now represented by a "
        "summary — open the **Logs** pane to see the compaction card. "
        "Continue chatting and I'll work from the summary."
    )
    return CommandResult(
        render=CommandRender.TRANSCRIPT,
        title="Compacted",
        body=body,
        metadata={
            "tokens_saved": result.tokens_saved,
            "events_summarized": result.events_summarized,
            "model": result.model or None,
            "focus": focus,
            # Keep the full summary reachable for clients that want it
            # without parsing the events stream (e.g. CLI fallback paths).
            "summary": result.summary,
        },
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


COMMANDS: dict[str, CommandDef] = {
    "compact": CommandDef(
        name="compact",
        description="Compact session history (optional focus directive biases the summary)",
        usage="/compact [focus]",
        render=CommandRender.TRANSCRIPT,
        handler=_handle_compact,
    ),
    "skills": CommandDef(
        name="skills",
        description="List available skills",
        usage="/skills",
        render=CommandRender.DIALOG,
        handler=_handle_skills,
    ),
    "tokens": CommandDef(
        name="tokens",
        description="Show session token usage",
        usage="/tokens",
        render=CommandRender.DIALOG,
        handler=_handle_tokens,
    ),
    "fork": CommandDef(
        name="fork",
        description="Fork the current session",
        usage="/fork [tag]",
        render=CommandRender.NOTIFICATION,
        handler=_handle_fork,
    ),
    "tag": CommandDef(
        name="tag",
        description="Tag the current session",
        usage="/tag NAME",
        render=CommandRender.NOTIFICATION,
        handler=_handle_tag,
    ),
    "help": CommandDef(
        name="help",
        description="List available commands",
        usage="/help",
        render=CommandRender.DIALOG,
        handler=_handle_help,
    ),
}


def list_commands() -> list[dict[str, str]]:
    """Return command metadata for client discovery."""
    return [
        {
            "name": cmd.name,
            "description": cmd.description,
            "usage": cmd.usage,
            "render": cmd.render.value,
        }
        for cmd in COMMANDS.values()
    ]


async def execute_command(
    name: str, args: list[str], ctx: CommandContext
) -> CommandResult:
    """Execute a command by name.

    Raises ``KeyError`` if the command is not registered. Handlers may raise
    ``CommandError`` for bad arguments or other expected failures.
    """
    if name not in COMMANDS:
        raise KeyError(name)
    return await COMMANDS[name].handler(ctx, args)
