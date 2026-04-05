"""Webhook routes, shared message pipeline, and channel system init.

Registers a single Flask Blueprint with a generic
``POST /api/webhooks/<platform>`` endpoint that delegates to the
appropriate :class:`ChannelAdapter`.  Non-webhook channels (e.g. email
via IMAP polling) share the same processing pipeline through
:func:`_process_inbound`.

The bot only processes messages that contain its trigger keyword
(e.g. ``@Meeseeks``).  Non-mentioned messages are silently
acknowledged — no events are appended, no LLM runs are started.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from flask import Blueprint, Flask, request
from meeseeks_core.common import get_logger
from meeseeks_core.config import get_config
from meeseeks_core.permissions import auto_approve
from meeseeks_core.token_budget import get_token_budget

from meeseeks_api.channels.base import (
    ChannelRegistry,
    DeduplicationGuard,
    InboundMessage,
)
from meeseeks_api.channels.nextcloud_talk import NextcloudTalkAdapter

if TYPE_CHECKING:
    from meeseeks_core.config import AppConfig
    from meeseeks_core.hooks import HookManager
    from meeseeks_core.session_runtime import SessionRuntime
    from meeseeks_core.types import EventRecord

logger = get_logger(name="channels.routes")

channel_bp = Blueprint("channels", __name__)

# Module-level references injected by ``init_channels()``.
_runtime: SessionRuntime | None = None
_hook_manager: HookManager | None = None
_registry: ChannelRegistry = ChannelRegistry()
_dedup: DeduplicationGuard = DeduplicationGuard()

# Matches ``/command`` or ``/command args`` after the trigger keyword.
_COMMAND_RE = re.compile(r"/([\w-]+)[^\S\n]*(.*)")


# ------------------------------------------------------------------
# Command registry
# ------------------------------------------------------------------


@dataclass(frozen=True)
class CommandContext:
    """Everything a slash-command handler needs."""

    session_id: str
    args: str
    message: InboundMessage
    tag: str


# command_name → (handler, one-line description)
_COMMANDS: dict[str, tuple[Callable[[CommandContext], str], str]] = {}


def command(name: str, description: str) -> Callable:
    """Decorator that registers a slash command."""

    def decorator(fn: Callable[[CommandContext], str]) -> Callable[[CommandContext], str]:
        _COMMANDS[name] = (fn, description)
        return fn

    return decorator


def _dispatch_command(cmd: str, ctx: CommandContext) -> str | None:
    """Run a registered command.  Returns response text, or None."""
    entry = _COMMANDS.get(cmd.lower())
    return entry[0](ctx) if entry else None


def _build_help_text() -> str:
    """Auto-generate help from the command registry."""
    lines = [
        "**Meeseeks** — AI personal assistant",
        "",
        "**Commands** (use after @mention):",
    ]
    for name, (_, desc) in _COMMANDS.items():
        lines.append(f"- `/{name}` — {desc}")
    lines += ["", "**Usage:** `@Meeseeks <your question or task>`"]
    return "\n".join(lines)


# ---- Registered commands ----


@command("help", "Show available commands")
def _cmd_help(ctx: CommandContext) -> str:
    return _build_help_text()


@command("usage", "Show session context usage and token budget")
def _cmd_usage(ctx: CommandContext) -> str:
    assert _runtime is not None  # noqa: S101
    events = _runtime.session_store.load_transcript(ctx.session_id)
    summary = _runtime.session_store.load_summary(ctx.session_id)
    budget = get_token_budget(events, summary, model_name=None)
    return (
        f"**Session context usage**\n"
        f"- Events: {len(events)}\n"
        f"- Tokens used: ~{budget.total_tokens:,}\n"
        f"- Context window: {budget.context_window:,}\n"
        f"- Utilization: {budget.utilization:.0%}\n"
        f"- Auto-compact threshold: {budget.threshold:.0%}\n"
        f"- Status: {'⚠️ Compaction needed' if budget.needs_compact else '✅ Healthy'}"
    )


@command("new", "Start a fresh conversation")
def _cmd_new(ctx: CommandContext) -> str:
    assert _runtime is not None  # noqa: S101
    new_id = _runtime.session_store.create_session()
    _runtime.session_store.tag_session(new_id, ctx.tag)
    _runtime.session_store.append_event(new_id, {
        "type": "context",
        "payload": {
            "source_platform": ctx.message.platform,
            "channel_id": ctx.message.channel_id,
            "thread_id": ctx.message.thread_id,
            "sender": ctx.message.sender_name,
            "room": ctx.message.room_name,
        },
    })
    return "Fresh conversation started. Previous context cleared."


@command("switch-project", "Switch project context (`<name>`)")
def _cmd_switch_project(ctx: CommandContext) -> str:
    assert _runtime is not None  # noqa: S101
    projects = get_config().projects
    available = {
        n: c for n, c in projects.items() if c.path and os.path.isdir(c.path)
    }
    if not ctx.args:
        return _format_project_list(available, "Usage: `/switch-project <name>`")
    if ctx.args not in available:
        return _format_project_list(available, f"Unknown project **{ctx.args}**.")
    chosen = available[ctx.args]
    _runtime.session_store.append_event(ctx.session_id, {
        "type": "context",
        "payload": {
            "source_platform": ctx.message.platform,
            "active_project": ctx.args,
            "active_project_cwd": chosen.path,
        },
    })
    return f"Switched to project **{ctx.args}** (`{chosen.path}`)."


def _format_project_list(projects: dict[str, Any], header: str) -> str:
    """Format available projects as a markdown list."""
    lines = [header, "", "**Available projects:**"]
    for name, cfg in projects.items():
        desc = f" — {cfg.description}" if cfg.description else ""
        lines.append(f"- `{name}`{desc}")
    if not projects:
        lines.append("- _(none configured)_")
    return "\n".join(lines)


# ------------------------------------------------------------------
# Webhook endpoint
# ------------------------------------------------------------------


def _process_inbound(
    adapter: Any,
    message: InboundMessage,
) -> tuple[dict[str, str], int]:
    """Shared message processing pipeline.

    Called by the webhook endpoint **and** non-webhook pollers (e.g.
    email IMAP).  Handles dedup → mention gate → trigger strip →
    session resolution → command dispatch → LLM invocation.
    """
    assert _runtime is not None  # noqa: S101
    platform = message.platform

    dedup_key = f"{platform}:{message.message_id}"
    if _dedup.is_duplicate(dedup_key):
        return {}, 200  # Replay

    # --- Gate: ignore messages that don't mention the bot ---
    if not _is_mentioned(message):
        return {}, 200

    # --- Strip trigger keyword from the user's text ---
    user_text = _strip_trigger(message)

    # --- Session resolution: thread-scoped > room-scoped ---
    if message.thread_id:
        tag = f"{platform}:thread:{message.channel_id}:{message.thread_id}"
    else:
        tag = f"{platform}:room:{message.channel_id}"

    session_id = _runtime.session_store.resolve_tag(tag)
    if session_id is None:
        session_id = _runtime.session_store.create_session()
        _runtime.session_store.tag_session(session_id, tag)
        _runtime.session_store.append_event(session_id, {
            "type": "context",
            "payload": {
                "source_platform": platform,
                "channel_id": message.channel_id,
                "thread_id": message.thread_id,
                "sender": message.sender_name,
                "room": message.room_name,
            },
        })

    # --- Check for slash commands (no LLM needed) ---
    cmd_match = _COMMAND_RE.match(user_text.strip())
    if cmd_match:
        ctx = CommandContext(
            session_id=session_id, args=cmd_match.group(2).strip(),
            message=message, tag=tag,
        )
        response = _dispatch_command(cmd_match.group(1), ctx)
        if response is not None:
            adapter.send_response(
                channel_id=message.channel_id, text=response,
                thread_id=message.thread_id, reply_to=message.message_id,
            )
            return {}, 200
        # Unknown command — fall through to LLM

    # --- Store reply target for the completion hook ---
    _runtime.session_store.append_event(session_id, {
        "type": "context",
        "payload": {
            "source_platform": platform,
            "reply_to_message_id": message.message_id,
            "channel_id": message.channel_id,
            "thread_id": message.thread_id,
        },
    })

    # --- Acknowledge receipt with a reaction (if adapter supports it) ---
    if hasattr(adapter, "send_reaction"):
        adapter.send_reaction(
            channel_id=message.channel_id,
            emoji="\N{EYES}",
            reply_to=message.message_id,
            thread_id=message.thread_id,
        )

    # --- Steer running session or start new run ---
    if _runtime.is_running(session_id):
        _runtime.enqueue_message(session_id, user_text)
        return {}, 200

    project_cwd = _get_active_project_cwd(session_id)
    client_ctx = getattr(adapter, "system_context", None)

    _runtime.start_async(
        session_id=session_id,
        user_query=user_text,
        hook_manager=_hook_manager,
        approval_callback=auto_approve,
        cwd=project_cwd,
        skill_instructions=client_ctx,
    )
    return {}, 200


@channel_bp.route("/api/webhooks/<platform>", methods=["POST"])
def webhook_receive(platform: str) -> tuple[dict[str, str], int]:
    """Receive an inbound webhook from a chat platform.

    Authenticates and parses via the platform adapter, then delegates
    to :func:`_process_inbound` for the shared processing pipeline.
    """
    if _runtime is None:
        return {"error": "Channel system not initialised"}, 500

    adapter = _registry.get(platform)
    if not adapter:
        return {"error": "Unknown platform"}, 404

    body = request.get_data()
    headers = {k: v for k, v in request.headers}

    if not adapter.verify_request(headers, body):
        return {"error": "Unauthorized"}, 401

    message = adapter.parse_inbound(headers, body)
    if message is None:
        return {}, 200  # Non-message event — acknowledge silently

    return _process_inbound(adapter, message)


# ------------------------------------------------------------------
# Mention detection & text cleanup
# ------------------------------------------------------------------


def _is_mentioned(message: InboundMessage) -> bool:
    """Return True if the message should be processed.

    Adapters may expose a ``requires_mention(message)`` method to
    dynamically decide whether the trigger keyword must appear.  For
    example, the email adapter skips mention gating for 1-to-1 emails
    but requires ``@Meeseeks`` in multi-party threads.
    """
    adapter = _registry.get(message.platform)
    if adapter is None:
        return True
    # Let adapter opt out of mention gating per message
    if hasattr(adapter, "requires_mention"):
        if not adapter.requires_mention(message):
            return True  # Adapter says no mention needed
    keyword: str = getattr(adapter, "trigger_keyword", "")
    if not keyword:
        return True  # Empty keyword → respond to all
    return keyword.lower() in message.text.lower()


def _strip_trigger(message: InboundMessage) -> str:
    """Remove the trigger keyword from the message text."""
    adapter = _registry.get(message.platform)
    keyword: str = getattr(adapter, "trigger_keyword", "") if adapter else ""
    if not keyword:
        return message.text
    pattern = re.compile(re.escape(keyword), re.IGNORECASE)
    return pattern.sub("", message.text, count=1).strip()


# ------------------------------------------------------------------
# Session context helpers
# ------------------------------------------------------------------


def _get_active_project_cwd(session_id: str) -> str | None:
    """Read the active project CWD from the session's context events."""
    assert _runtime is not None  # noqa: S101
    events = _runtime.session_store.load_transcript(session_id)
    for event in reversed(events):
        if event.get("type") != "context":
            continue
        cwd = event.get("payload", {}).get("active_project_cwd")
        if cwd:
            return str(cwd)
    return None


# ------------------------------------------------------------------
# Completion callback (on_session_end hook)
# ------------------------------------------------------------------


def _channel_completion_hook(
    session_id: str, error: str | None = None
) -> None:
    """Send the final answer back to the originating chat thread."""
    if _runtime is None:
        return

    events = _runtime.session_store.load_transcript(session_id)
    ctx = _find_channel_context(events)
    if not ctx:
        return  # Not a channel session

    adapter = _registry.get(ctx.get("source_platform", ""))
    if not adapter:
        return

    final_text = _extract_final_answer(events, error)
    if not final_text:
        return

    channel_id = ctx.get("channel_id", "")
    thread_id = ctx.get("thread_id")
    reply_to = ctx.get("reply_to_message_id") or thread_id
    adapter.send_response(
        channel_id=channel_id, text=final_text,
        thread_id=thread_id, reply_to=reply_to,
    )


def _find_channel_context(
    events: list[EventRecord],
) -> dict[str, Any] | None:
    """Find the most recent context event with ``source_platform``."""
    for event in reversed(events):
        if event.get("type") != "context":
            continue
        payload = event.get("payload", {})
        if "source_platform" in payload:
            return dict(payload)
    return None


def _extract_final_answer(
    events: list[EventRecord], error: str | None
) -> str:
    """Walk the transcript backwards to find the final answer text."""
    if error:
        return f"Session ended with an error: {error}"
    for event in reversed(events):
        etype = event.get("type", "")
        payload = event.get("payload", {})
        if etype == "completion":
            result = payload.get("task_result")
            if result:
                return str(result)
        if etype == "assistant":
            text = payload.get("text")
            if text:
                return str(text)
    return ""


# ------------------------------------------------------------------
# Initialisation
# ------------------------------------------------------------------


def init_channels(
    app: Flask,
    runtime: SessionRuntime,
    hook_manager: HookManager,
    config: AppConfig,
) -> None:
    """Wire channel adapters into the Flask app.

    Called once at API startup.  No-ops if no channels are configured.
    """
    global _runtime, _hook_manager  # noqa: PLW0603
    _runtime = runtime
    _hook_manager = hook_manager

    nc_cfg = config.channels.get("nextcloud-talk", {})
    if nc_cfg.get("enabled") and nc_cfg.get("bot_secret"):
        adapter = NextcloudTalkAdapter(
            bot_secret=nc_cfg["bot_secret"],
            nextcloud_url=nc_cfg.get("nextcloud_url", ""),
            allowed_backends=nc_cfg.get("allowed_backends"),
            host_header=nc_cfg.get("nextcloud_host_header"),
            trigger_keyword=nc_cfg.get("trigger_keyword", "@Meeseeks"),
        )
        _registry.register(adapter)
        logger.info("Nextcloud Talk channel adapter registered")

    # -- Email channel (IMAP polling + SMTP replies) --
    email_cfg = config.channels.get("email", {})
    if email_cfg.get("enabled"):
        from meeseeks_api.channels.email_adapter import EmailAdapter, EmailPoller

        email_adapter = EmailAdapter(
            smtp_host=email_cfg["smtp_host"],
            smtp_port=email_cfg.get("smtp_port", 587),
            smtp_ssl=email_cfg.get("smtp_ssl", False),
            smtp_starttls=email_cfg.get("smtp_starttls", True),
            username=email_cfg["username"],
            password=email_cfg["password"],
            from_address=email_cfg.get("from_address"),
            allowed_senders=email_cfg.get("allowed_senders"),
            allowed_recipients=email_cfg.get("allowed_recipients"),
        )
        _registry.register(email_adapter)

        poller = EmailPoller(
            adapter=email_adapter,
            imap_host=email_cfg["imap_host"],
            imap_port=email_cfg.get("imap_port", 993),
            imap_ssl=email_cfg.get("imap_ssl", True),
            username=email_cfg["username"],
            password=email_cfg["password"],
            mailbox=email_cfg.get("mailbox", "INBOX"),
            poll_interval=email_cfg.get("poll_interval_seconds", 30),
            process_fn=_process_inbound,
        )
        poller.start()
        logger.info(
            "Email channel registered (polling %s every %ds)",
            email_cfg["imap_host"],
            email_cfg.get("poll_interval_seconds", 30),
        )

    hook_manager.on_session_end.append(_channel_completion_hook)

    app.register_blueprint(channel_bp)
    logger.info(
        "Channel webhook routes registered (platforms: %s)",
        _registry.platforms() or "none",
    )
