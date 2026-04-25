#!/usr/bin/env python3
"""Command registry for Truss CLI."""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from truss_core.config import get_config_value, get_mcp_config_path
from truss_core.token_budget import get_token_budget, read_last_input_tokens
from truss_core.tool_registry import ToolRegistry, ToolSpec, load_registry

from truss_cli.cli_context import CommandContext
from truss_cli.cli_dialogs import DialogFactory


@dataclass(frozen=True)
class Command:
    """CLI command registration metadata."""

    name: str
    help: str
    handler: Callable[[CommandContext, list[str]], bool]


class CommandRegistry:
    """Registry for CLI commands and their handlers."""

    def __init__(self) -> None:
        """Initialize an empty command registry."""
        self._commands: dict[str, Command] = {}

    def command(self, name: str, help_text: str) -> Callable[[Callable], Callable]:
        """Register a command decorator for CLI handlers.

        Args:
            name: Command token (e.g., "/help").
            help_text: Help text shown in the CLI.

        Returns:
            Decorator that registers a command handler.
        """

        def decorator(func: Callable[[CommandContext, list[str]], bool]) -> Callable:
            self._commands[name] = Command(name=name, help=help_text, handler=func)
            return func

        return decorator

    def execute(self, name: str, context: CommandContext, args: list[str]) -> bool:
        """Execute a registered command handler.

        Args:
            name: Command token to execute.
            context: Command execution context.
            args: Command arguments.

        Returns:
            True to continue, False to exit the CLI loop.
        """
        command = self._commands.get(name)
        if command is None:
            context.console.print("Unknown command. Use /help for a list of commands.")
            return True
        return command.handler(context, args)

    def help_text(self) -> str:
        """Return formatted help text for all commands."""
        lines = [f"{cmd.name} - {cmd.help}" for cmd in self._commands.values()]
        return "\n".join(sorted(lines))

    def list_commands(self) -> list[str]:
        """Return a sorted list of registered command tokens."""
        return sorted(self._commands.keys())


REGISTRY = CommandRegistry()


@REGISTRY.command("/help", "Show help")
def _cmd_help(context: CommandContext, args: list[str]) -> bool:
    del args
    # Render as Text so help strings can contain literal "[name]" /
    # "[focus]" tokens without Rich treating them as markup tags and
    # silently dropping them.
    context.console.print(Panel(Text(REGISTRY.help_text()), title="Commands"))
    return True


@REGISTRY.command("/exit", "Exit the CLI")
def _cmd_exit(context: CommandContext, args: list[str]) -> bool:
    del context, args
    return False


@REGISTRY.command("/quit", "Exit the CLI")
def _cmd_quit(context: CommandContext, args: list[str]) -> bool:
    del context, args
    return False


@REGISTRY.command("/new", "Start a new session")
def _cmd_new(context: CommandContext, args: list[str]) -> bool:
    del args
    context.state.session_id = context.store.create_session()
    context.console.print(f"New session: {context.state.session_id}")
    return True


@REGISTRY.command("/session", "Show current session id")
def _cmd_session(context: CommandContext, args: list[str]) -> bool:
    del args
    context.console.print(f"Session: {context.state.session_id}")
    return True


@REGISTRY.command("/summary", "Show current session summary")
def _cmd_summary(context: CommandContext, args: list[str]) -> bool:
    del args
    summary = context.store.load_summary(context.state.session_id) or "(empty)"
    context.console.print(Panel(summary, title="Session Summary"))
    return True


@REGISTRY.command("/summarize", "Summarize and compact this session")
def _cmd_summarize(context: CommandContext, args: list[str]) -> bool:
    # Forward any free-form remainder as the focus directive — the
    # orchestrator routes ``/compact <focus>`` to ``_handle_compact``,
    # which passes it as ``focus_prompt`` to the summarizer.
    focus = " ".join(args).strip()
    user_query = f"/compact {focus}".rstrip()
    task_queue = context.runtime.run_sync(
        user_query=user_query,
        session_id=context.state.session_id,
    )
    context.console.print(Panel(task_queue.task_result or "", title="Summary"))
    return True


@REGISTRY.command("/compact", "Compact session transcript (/compact FOCUS to bias the summary)")
def _cmd_compact(context: CommandContext, args: list[str]) -> bool:
    return _cmd_summarize(context, args)


@REGISTRY.command("/status", "Show current session status")
def _cmd_status(context: CommandContext, args: list[str]) -> bool:
    del args
    summary = context.runtime.summarize_session(context.state.session_id)
    context.console.print(Panel(json.dumps(summary, indent=2), title="Session Status"))
    return True


@REGISTRY.command("/terminate", "Cancel the active session run")
def _cmd_terminate(context: CommandContext, args: list[str]) -> bool:
    del args
    canceled = context.runtime.cancel(context.state.session_id)
    if canceled:
        context.console.print("Cancellation requested.", style="yellow")
    else:
        context.console.print("No active run to cancel.", style="yellow")
    return True


def _run_recovery(context: CommandContext, action: str) -> bool:
    """Shared implementation for ``/retry`` and ``/continue``.

    Resolves the recovery query text via :meth:`SessionRuntime.resolve_recovery_query`
    (which also appends the ``recovery`` audit event), then runs the
    resolved query synchronously through ``run_sync`` — the same path
    used by ``/compact``.
    """
    try:
        query = context.runtime.resolve_recovery_query(context.state.session_id, action)
    except (ValueError, RuntimeError) as exc:
        context.console.print(f"Cannot {action}: {exc}", style="yellow")
        return True
    label = "Retrying last query" if action == "retry" else "Continuing"
    context.console.print(f"{label}...", style="cyan")
    task_queue = context.runtime.run_sync(
        user_query=query,
        session_id=context.state.session_id,
        model_name=context.state.model_name,
        tool_registry=context.tool_registry,
        mode=context.state.mode,
    )
    if task_queue.task_result:
        context.console.print(Panel(task_queue.task_result, title="Response"))
    elif task_queue.last_error:
        context.console.print(f"{action.title()} failed: {task_queue.last_error}", style="red")
    return True


@REGISTRY.command("/retry", "Re-run the last user query after a failed run")
def _cmd_retry(context: CommandContext, args: list[str]) -> bool:
    del args
    return _run_recovery(context, "retry")


@REGISTRY.command("/continue", "Resume after a failed run with a recovery prompt")
def _cmd_continue(context: CommandContext, args: list[str]) -> bool:
    del args
    return _run_recovery(context, "continue")


@REGISTRY.command("/tag", "Tag this session: /tag NAME")
def _cmd_tag(context: CommandContext, args: list[str]) -> bool:
    if not args:
        if context.prompt_func is None:
            context.console.print("Usage: /tag NAME")
            return True
        dialogs = DialogFactory(console=context.console, prompt_func=context.prompt_func)
        tag = dialogs.prompt_text(
            "Tag Session",
            "Enter tag name for this session.",
            placeholder="primary",
        )
        if tag is None:
            context.console.print("Tag cancelled.")
            return True
        tag = tag.strip()
        if not tag:
            context.console.print("Tag cannot be empty.")
            return True
        args = [tag]
    context.store.tag_session(context.state.session_id, args[0])
    context.console.print(f"Tagged session as '{args[0]}'")
    return True


@REGISTRY.command(
    "/fork",
    "Fork current session: /fork [TAG] [--at TS] [--compact]",
)
def _cmd_fork(context: CommandContext, args: list[str]) -> bool:
    at_ts: str | None = None
    compact = False
    positional: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--at" and i + 1 < len(args):
            at_ts = args[i + 1]
            i += 2
        elif args[i] == "--compact":
            compact = True
            i += 1
        else:
            positional.append(args[i])
            i += 1
    tag = positional[0] if positional else None
    if not positional and not at_ts and context.prompt_func is not None:
        dialogs = DialogFactory(console=context.console, prompt_func=context.prompt_func)
        tag = dialogs.prompt_text(
            "Fork Session",
            "Enter optional tag for the forked session (blank to skip).",
            placeholder="forked",
            allow_empty=True,
        )
        if tag is None:
            context.console.print("Fork cancelled.")
            return True
        tag = tag.strip() or None
    if at_ts:
        new_session_id = context.store.fork_session_at(context.state.session_id, at_ts)
    else:
        new_session_id = context.store.fork_session(context.state.session_id)
    context.state.session_id = new_session_id
    if tag:
        context.store.tag_session(context.state.session_id, tag)
    if compact:
        import asyncio

        try:
            asyncio.run(context.store.compact_session(new_session_id, mode="partial"))
            context.console.print("Compacted forked session context.", style="dim")
        except Exception as exc:
            context.console.print(f"Compaction skipped: {exc}", style="yellow")
    context.console.print(f"Forked session: {context.state.session_id}")
    return True


@REGISTRY.command("/edit", "Edit the last user message and re-run: /edit [TEXT]")
def _cmd_edit(context: CommandContext, args: list[str]) -> bool:
    replacement = " ".join(args) if args else None
    if not replacement and context.prompt_func is not None:
        dialogs = DialogFactory(console=context.console, prompt_func=context.prompt_func)
        replacement = dialogs.prompt_text(
            "Edit Message",
            "Enter the replacement text for the last user message.",
        )
        if replacement is None:
            context.console.print("Edit cancelled.")
            return True
        replacement = replacement.strip()
    if not replacement:
        context.console.print("No replacement text provided.", style="yellow")
        return True
    try:
        query = context.runtime.resolve_recovery_query(
            context.state.session_id, "retry", replacement_text=replacement
        )
    except (ValueError, RuntimeError) as exc:
        context.console.print(f"Cannot edit: {exc}", style="yellow")
        return True
    context.console.print("Re-running with edited prompt...", style="cyan")
    task_queue = context.runtime.run_sync(
        user_query=query,
        session_id=context.state.session_id,
        model_name=context.state.model_name,
        tool_registry=context.tool_registry,
        mode=context.state.mode,
    )
    if task_queue.task_result:
        context.console.print(Panel(task_queue.task_result, title="Response"))
    elif task_queue.last_error:
        context.console.print(f"Edit failed: {task_queue.last_error}", style="red")
    return True


@REGISTRY.command("/plan", "Toggle plan display: /plan on|off")
def _cmd_plan(context: CommandContext, args: list[str]) -> bool:
    if not args:
        context.console.print(f"Plan display is {'on' if context.state.show_plan else 'off'}.")
        return True
    value = args[0].lower()
    if value in {"on", "true", "yes"}:
        context.state.show_plan = True
    elif value in {"off", "false", "no"}:
        context.state.show_plan = False
    else:
        context.console.print("Usage: /plan on|off")
    return True


@REGISTRY.command("/mode", "Set orchestration mode: /mode act|plan")
def _cmd_mode(context: CommandContext, args: list[str]) -> bool:
    if not args:
        context.console.print(f"Current mode: {context.state.mode}")
        return True
    value = args[0].lower()
    if value not in {"act", "plan"}:
        context.console.print("Usage: /mode act|plan")
        return True
    context.state.mode = value
    context.console.print(f"Mode set to: {context.state.mode}")
    return True


@REGISTRY.command("/skills", "List available skills (/skills [name])")
def _cmd_skills(context: CommandContext, args: list[str]) -> bool:
    from truss_core.plugins import load_all_plugin_components
    from truss_core.skills import SkillRegistry

    skill_registry = SkillRegistry()
    skill_registry.load()
    # Mirror the cli_master boot path so the listing matches what the
    # dispatcher will actually accept as ``/<skill>``.
    skill_registry.load_plugin_components(load_all_plugin_components())
    skills = skill_registry.list_all()

    if not skills:
        context.console.print("No skills discovered.")
        context.console.print(
            "Place SKILL.md files in ~/.claude/skills/<name>/ or .claude/skills/<name>/",
            style="dim",
        )
        return True

    # Detail view for a specific skill.
    if args:
        name = args[0].lstrip("/")
        skill = skill_registry.get(name)
        if skill is None:
            context.console.print(f"Unknown skill: {name}")
            return True
        detail = Text()
        detail.append(f"Name: {skill.name}\n", style="cyan bold")
        detail.append(f"Description: {skill.description}\n")
        detail.append(f"Source: {skill.source}\n", style="dim")
        if skill.allowed_tools:
            detail.append(f"Allowed tools: {', '.join(skill.allowed_tools)}\n", style="dim")
        if skill.context:
            detail.append(f"Context: {skill.context}\n", style="dim")
        if skill.disable_model_invocation:
            detail.append("Auto-invocation: disabled\n", style="dim")
        if not skill.user_invocable:
            detail.append("User-invocable: no (LLM only)\n", style="dim")
        context.console.print(Panel(detail, title=f"Skill: {skill.name}", border_style="cyan"))
        return True

    # List view.
    rows = Text()
    for skill in skills:
        rows.append(f"  /{skill.name}", style="cyan")
        rows.append(f"  {skill.description}", style="dim")
        badges: list[str] = [skill.source]
        if skill.context == "fork":
            badges.append("fork")
        if not skill.user_invocable:
            badges.append("LLM only")
        if skill.disable_model_invocation:
            badges.append("manual only")
        rows.append(f"  [{', '.join(badges)}]", style="dim italic")
        rows.append("\n")
    context.console.print(Panel(rows, title="Skills", border_style="cyan"))
    return True


@REGISTRY.command("/plugins", "Manage plugins (/plugins marketplace|install|uninstall)")
def _cmd_plugins(context: CommandContext, args: list[str]) -> bool:
    from truss_core.config import get_config
    from truss_core.plugins import (
        discover_installed_plugins,
        discover_marketplace_plugins,
        install_plugin,
        uninstall_plugin,
    )

    cfg = get_config().plugins
    sub = args[0] if args else ""

    if sub == "marketplace":
        available = discover_marketplace_plugins(marketplace_dirs=cfg.resolve_marketplace_dirs())
        if not available:
            context.console.print("[dim]No marketplace plugins available.[/dim]")
            return True
        table = Table(title="Marketplace Plugins", box=box.SIMPLE_HEAVY)
        table.add_column("Name", style="cyan")
        table.add_column("Description")
        table.add_column("Category", style="dim")
        table.add_column("Marketplace", style="dim")
        for p in available:
            table.add_row(
                p["name"],
                p.get("description", ""),
                p.get("category", ""),
                p.get("marketplace", ""),
            )
        context.console.print(table)
        return True

    if sub == "install":
        name = args[1] if len(args) > 1 else ""
        if not name:
            context.console.print("[red]Usage: /plugins install <name>[/red]")
            return True
        available = discover_marketplace_plugins(marketplace_dirs=cfg.resolve_marketplace_dirs())
        match = next((p for p in available if p["name"] == name), None)
        if not match:
            context.console.print(f"[red]Plugin '{name}' not found in any marketplace.[/red]")
            return True
        try:
            manifest = install_plugin(
                name,
                match["marketplace"],
                marketplace_dirs=cfg.resolve_marketplace_dirs(),
                install_base=cfg.resolve_install_dir(),
            )
            context.console.print(f"[green]Installed {manifest.name} v{manifest.version}[/green]")
        except Exception as exc:
            context.console.print(f"[red]Install failed: {exc}[/red]")
        return True

    if sub == "uninstall":
        name = args[1] if len(args) > 1 else ""
        if not name:
            context.console.print("[red]Usage: /plugins uninstall <name>[/red]")
            return True
        if uninstall_plugin(name, install_base=cfg.resolve_install_dir()):
            context.console.print(f"[green]Uninstalled '{name}'.[/green]")
        else:
            context.console.print(f"[red]Plugin '{name}' not found.[/red]")
        return True

    # Default: list installed plugins
    plugins = discover_installed_plugins(registry_paths=cfg.resolve_registry_paths())
    if not plugins:
        context.console.print("[dim]No plugins installed.[/dim]")
        return True

    table = Table(title="Installed Plugins", box=box.SIMPLE_HEAVY)
    table.add_column("Name", style="cyan")
    table.add_column("Version", style="dim")
    table.add_column("Marketplace", style="dim")
    table.add_column("Skills", justify="right")
    table.add_column("Agents", justify="right")
    table.add_column("Cmds", justify="right")
    table.add_column("MCP", justify="right")
    table.add_column("Hooks")
    for pc in plugins:
        if pc.manifest is None:
            continue
        table.add_row(
            pc.manifest.name,
            pc.manifest.version or "-",
            pc.manifest.marketplace or "-",
            str(len(pc.skill_dirs)),
            str(len(pc.agent_files)),
            str(len(pc.command_files)),
            str(len(pc.mcp_config or {})),
            "✓" if pc.hooks_config else "-",
        )
    context.console.print(table)
    return True


@REGISTRY.command("/mcp", "List MCP tools and servers (/mcp select|init)")
def _cmd_mcp(context: CommandContext, args: list[str]) -> bool:
    if args and args[0].lower() == "init":
        return _cmd_mcp_init(context, args[1:])
    selection_mode = not args or args[0].lower() in {"select", "filter"}
    all_specs = context.tool_registry.list_specs(include_disabled=True)
    mcp_specs = [spec for spec in all_specs if spec.kind == "mcp"]
    if selection_mode and mcp_specs:
        mcp_specs = _maybe_select_mcp_specs(context, mcp_specs) or mcp_specs
    _render_mcp(
        context.console,
        context.tool_registry,
        mcp_specs=mcp_specs,
        all_specs=all_specs,
    )
    return True


@REGISTRY.command("/config", "Manage config files (/config init)")
def _cmd_config(context: CommandContext, args: list[str]) -> bool:
    if args and args[0].lower() == "init":
        return _cmd_config_init(context, args[1:])
    context.console.print("Usage: /config init [--force]")
    return True


@REGISTRY.command("/init", "Scaffold both config and MCP example files")
def _cmd_init_all(context: CommandContext, args: list[str]) -> bool:
    return _cmd_config_init(context, args) and _cmd_mcp_init(context, args)


def _cmd_mcp_init(context: CommandContext, args: list[str]) -> bool:
    """Create a local MCP config file for quick setup."""
    target = get_mcp_config_path()
    force = False
    if args:
        force = "--force" in args or "--yes" in args
        candidate = args[0]
        if candidate.endswith(".json"):
            target = candidate

    if os.path.exists(target) and not force:
        context.console.print(f"MCP config already exists at {target}.")
        context.console.print("Use /mcp init --force to overwrite.")
        return True

    os.makedirs(os.path.dirname(target), exist_ok=True)
    payload = {
        "servers": {
            "codex_tools": {
                "transport": "streamable_http",
                "url": "http://127.0.0.1:6783/mcp/Codex-Tools-Personal",
                "headers": {"Authorization": "Bearer YOUR_MCP_TOKEN"},
            }
        }
    }
    example_path = os.path.join("configs", "mcp.example.json")
    if os.path.exists(example_path):
        try:
            with open(example_path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            pass
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")

    context.console.print(f"Created MCP config at {target}.")
    return True


def _cmd_config_init(context: CommandContext, args: list[str]) -> bool:
    """Create app config example file for quick setup."""
    force = "--force" in args or "--yes" in args
    from truss_core.config import _default_example_path, ensure_example_configs

    example_path = _default_example_path("app.example.json")
    if example_path.exists() and not force:
        context.console.print(f"Config example already exists at {example_path}.")
        context.console.print("Use /config init --force to overwrite.")
        return True

    app_path, _ = ensure_example_configs()
    context.console.print(f"Created config example at {app_path}.")
    return True


def _refresh_mcp_registry(context: CommandContext) -> None:
    target = get_mcp_config_path()
    if not os.path.exists(target):
        _cmd_mcp_init(context, [])
    refreshed = load_registry()
    context.tool_registry._tools = {}
    context.tool_registry._instances = {}
    for spec in refreshed.list_specs(include_disabled=True):
        context.tool_registry.register(spec)


def _maybe_select_mcp_specs(
    context: CommandContext,
    mcp_specs: list[ToolSpec],
) -> list[ToolSpec] | None:
    if context.prompt_func is None:
        return None
    dialogs = DialogFactory(console=context.console, prompt_func=context.prompt_func)
    refresh_label = "Refresh MCP config & manifest"
    all_label = "All MCP tools"
    options = [refresh_label, all_label, *[spec.tool_id for spec in mcp_specs]]
    selected = dialogs.select_one(
        "Select MCP Tool",
        options,
        subtitle="Use ↑/↓ and Enter to select. Esc to cancel.",
    )
    if selected == refresh_label:
        _refresh_mcp_registry(context)
        return [
            spec
            for spec in context.tool_registry.list_specs(include_disabled=True)
            if spec.kind == "mcp"
        ]
    if selected is None or selected == all_label:
        return None
    return [spec for spec in mcp_specs if spec.tool_id == selected]


@REGISTRY.command("/models", "Switch models using a local wizard")
def _cmd_models(context: CommandContext, args: list[str]) -> bool:
    del args
    if context.prompt_func is None:
        context.console.print("Model wizard is only available in interactive mode.")
        return True
    dialogs = DialogFactory(console=context.console, prompt_func=context.prompt_func)
    if dialogs.can_use_textual():
        try:
            models = _fetch_models()
        except RuntimeError as exc:
            context.console.print(f"Model lookup failed: {exc}")
            return True
        if not models:
            context.console.print("No models returned by the API.")
            return True
        choice = dialogs.select_one(
            "Select Model",
            models,
            subtitle="Use ↑/↓ and Enter to select. Esc to cancel.",
        )
        if not choice:
            context.console.print("Model selection cancelled.")
            return True
        context.state.model_name = choice
        context.console.print(f"Using model: {context.state.model_name}")
        return True
    _handle_model_wizard(context.console, context, context.prompt_func)
    return True


@REGISTRY.command("/automatic", "Auto-approve all tool actions")
def _cmd_automatic(context: CommandContext, args: list[str]) -> bool:
    force = False
    value = "on"
    if args:
        value = args[0].lower()
        force = any(arg in {"--yes", "--force"} for arg in args[1:]) or value in {
            "--yes",
            "--force",
        }
    if value in {"off", "disable", "no"}:
        context.state.auto_approve_all = False
        context.console.print("Automatic approvals disabled.")
        return True

    if not force:
        if context.prompt_func is None:
            context.console.print("Use /automatic --yes to confirm in non-interactive mode.")
            return True
        dialogs = DialogFactory(console=context.console, prompt_func=context.prompt_func)
        confirmed = dialogs.confirm(
            "Enable Automatic Approvals",
            "This will auto-approve all tool actions in this session. Continue?",
            default=False,
        )
        if not confirmed:
            context.console.print("Automatic approvals unchanged.")
            return True

    context.state.auto_approve_all = True
    context.console.print("Automatic approvals enabled for this session.")
    return True


def _resolve_cli_model(context: CommandContext) -> str:
    """Return the effective model name, falling back to config default.

    The CLI leaves ``state.model_name`` empty when ``--model`` was not
    supplied and relies on ``build_chat_model`` to pick the default at
    LLM-call time. Usage/budget helpers that resolve the context window
    via LiteLLM need an explicit name — feed them the same default.
    """
    name = context.state.model_name or ""
    if name:
        return name
    return str(get_config_value("llm", "default_model", default="") or "")


@REGISTRY.command("/tokens", "Show token usage and remaining context")
def _cmd_tokens(context: CommandContext, args: list[str]) -> bool:
    del args
    events = context.store.load_transcript(context.state.session_id)
    summary = context.store.load_summary(context.state.session_id)
    budget = get_token_budget(
        events,
        summary,
        _resolve_cli_model(context),
        last_input_tokens=read_last_input_tokens(events),
    )
    table = Table(title="Token Budget", show_lines=True)
    table.add_column("Metric", style="cyan")
    table.add_column("Value")
    table.add_row("Summary tokens", str(budget.summary_tokens))
    table.add_row("Event tokens", str(budget.event_tokens))
    table.add_row("Total tokens", str(budget.total_tokens))
    table.add_row("Context window", str(budget.context_window))
    table.add_row("Remaining", str(budget.remaining_tokens))
    table.add_row("Utilization", f"{budget.utilization:.1%}")
    table.add_row("Auto-compact threshold", f"{budget.threshold:.1%}")
    context.console.print(table)
    return True


@REGISTRY.command("/budget", "Show token usage and remaining context")
def _cmd_budget(context: CommandContext, args: list[str]) -> bool:
    return _cmd_tokens(context, args)


def _get_openai_base_url() -> str | None:
    return get_config_value("llm", "api_base")


def _fetch_models() -> list[str]:
    base_url = _get_openai_base_url()
    api_key = get_config_value("llm", "api_key")
    if not base_url:
        raise RuntimeError("llm.api_base is not set.")
    if not api_key:
        raise RuntimeError("llm.api_key is not set.")
    base_url = base_url.rstrip("/")
    if base_url.endswith("/v1"):
        url = f"{base_url}/models"
    else:
        url = f"{base_url}/v1/models"
    request = Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"Failed to fetch models: HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"Failed to fetch models: {exc.reason}") from exc
    data = payload.get("data", [])
    models = [item.get("id") for item in data if item.get("id")]
    return sorted(models)


def _handle_model_wizard(
    console: Console,
    context: CommandContext,
    prompt_func: Callable[[str], str],
) -> None:
    try:
        models = _fetch_models()
    except RuntimeError as exc:
        console.print(f"Model lookup failed: {exc}")
        return
    if not models:
        console.print("No models returned by the API.")
        return
    table = Table(
        title="Available Models",
        show_lines=False,
        padding=(0, 1),
        pad_edge=False,
        box=box.MINIMAL,
        border_style="dim",
    )
    table.add_column("Index", style="cyan")
    table.add_column("Model ID")
    for idx, model in enumerate(models, start=1):
        table.add_row(str(idx), model)
    console.print(table)
    console.print("Enter index or id (blank to cancel, 'q' to quit).")
    choice = prompt_func("Select model by index or id: ").strip()
    if not choice:
        console.print("Model selection cancelled.")
        return
    if choice.lower() in {"q", "quit", "exit", "cancel"}:
        console.print("Model selection cancelled.")
        return
    if choice.isdigit():
        index = int(choice)
        if 1 <= index <= len(models):
            context.state.model_name = models[index - 1]
            console.print(f"Using model: {context.state.model_name}")
            return
        console.print("Invalid model index.")
        return
    if choice in models:
        context.state.model_name = choice
        console.print(f"Using model: {context.state.model_name}")
        return
    console.print("Model not recognized.")


def _render_mcp(
    console: Console,
    tool_registry: ToolRegistry,
    mcp_specs: list[ToolSpec] | None = None,
    all_specs: list[ToolSpec] | None = None,
) -> None:
    config_path = get_mcp_config_path()
    if config_path and os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8") as handle:
                config = json.load(handle)
            servers = config.get("servers", {})
            if servers:
                server_lines: list[Text] = []
                for name, info in servers.items():
                    line = Text()
                    line.append(str(name), style="cyan")
                    transport = info.get("transport", "")
                    if transport:
                        line.append(" — ", style="dim")
                        line.append(str(transport))
                    server_lines.append(line)
                console.print(
                    Panel(
                        Group(*server_lines),
                        title="MCP Servers",
                        border_style="dim",
                    )
                )
        except (json.JSONDecodeError, OSError) as exc:
            console.print(f"Failed to read MCP config: {exc}")
    specs = mcp_specs
    if specs is None:
        specs = [
            spec for spec in tool_registry.list_specs(include_disabled=True) if spec.kind == "mcp"
        ]
    if not specs:
        console.print("No MCP tools configured.")
    if all_specs is None:
        all_specs = tool_registry.list_specs(include_disabled=True)
    local_specs = [spec for spec in all_specs if spec.kind != "mcp"]
    if local_specs:
        local_lines: list[Text] = []
        for spec in local_specs:
            line = Text()
            line.append(spec.tool_id, style="cyan")
            if spec.description:
                line.append(" — ", style="dim")
                line.append(spec.description)
            if not spec.enabled:
                disabled_reason = spec.metadata.get("disabled_reason")
                line.append(" — ", style="dim")
                line.append("disabled", style="yellow")
                if disabled_reason:
                    line.append(" • ", style="dim")
                    line.append(str(disabled_reason), style="dim")
            local_lines.append(line)
        console.print(
            Panel(
                Group(*local_lines),
                title="Built-in Tools",
                border_style="dim",
            )
        )
    if not specs:
        return
    tool_lines: list[Text] = []
    for spec in specs:
        server_name = spec.metadata.get("server", "")
        tool_name = spec.metadata.get("tool", "")
        line = Text()
        line.append(spec.tool_id, style="cyan")
        if server_name or tool_name:
            line.append(" — ", style="dim")
        if server_name:
            line.append(f"server:{server_name}")
        if tool_name:
            if server_name:
                line.append(" • ", style="dim")
            line.append(f"tool:{tool_name}")
        if not spec.enabled:
            disabled_reason = spec.metadata.get("disabled_reason")
            line.append(" — ", style="dim")
            line.append("disabled", style="yellow")
            if disabled_reason:
                line.append(" • ", style="dim")
                line.append(str(disabled_reason), style="dim")
        tool_lines.append(line)
    console.print(
        Panel(
            Group(*tool_lines),
            title="MCP Tools",
            border_style="dim",
        )
    )


def get_registry() -> CommandRegistry:
    """Return the singleton command registry."""
    return REGISTRY


__all__ = ["CommandContext", "CommandRegistry", "get_registry"]
