#!/usr/bin/env python3
"""Terminal CLI for Meeseeks."""
# ruff: noqa: E402

import argparse
import json
import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from rich import box
from rich.columns import Columns
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.status import Status
from rich.syntax import Syntax
from rich.text import Text


def _verbosity_to_level(verbosity: int) -> str:
    if verbosity <= 0:
        return "WARNING"
    if verbosity == 1:
        return "DEBUG"
    return "TRACE"


def _parse_verbosity(argv: list[str]) -> int | None:
    count = 0
    for arg in argv[1:]:
        if arg in {"-v", "--verbose"}:
            count += 1
            continue
        if arg == "--debug":
            count = max(count, 1)
            continue
        if arg.startswith("--verbose="):
            raw = arg.split("=", 1)[1]
            try:
                count = max(count, int(raw))
            except ValueError:
                continue
            continue
        if arg.startswith("-v") and arg != "-v":
            tail = arg[1:]
            if tail and all(ch == "v" for ch in tail):
                count += len(tail)
    return count if count > 0 else None


def _bootstrap_cli_logging_env(argv: list[str]) -> None:
    """Configure logging environment for the CLI before core imports."""
    os.environ["MEESEEKS_CLI"] = "1"
    os.environ.setdefault("MEESEEKS_LOG_STYLE", "dark")
    verbosity = _parse_verbosity(argv)
    if verbosity is not None:
        os.environ["LOG_LEVEL"] = _verbosity_to_level(verbosity)
        return
    existing_level = os.getenv("LOG_LEVEL", "").upper()
    if not existing_level:
        os.environ["LOG_LEVEL"] = "WARNING"


_bootstrap_cli_logging_env(sys.argv)

from meeseeks_core.classes import ActionStep, TaskQueue
from meeseeks_core.common import MockSpeaker, format_action_argument, get_logger
from meeseeks_core.components import resolve_langfuse_status
from meeseeks_core.hooks import HookManager
from meeseeks_core.permissions import PermissionDecision
from meeseeks_core.session_store import SessionStore
from meeseeks_core.task_master import generate_action_plan, orchestrate_session
from meeseeks_core.tool_registry import ToolRegistry, load_registry
from meeseeks_tools.integration.mcp import (
    _load_mcp_config,
    mark_tool_auto_approved,
    save_mcp_config,
    tool_auto_approved,
)

from meeseeks_cli.cli_commands import get_registry
from meeseeks_cli.cli_context import CliState, CommandContext
from meeseeks_cli.cli_dialogs import DialogFactory

logging = get_logger(name="meeseeks.cli")


def _resolve_session_id(
    store: SessionStore,
    session_id: str | None,
    session_tag: str | None,
    fork_from: str | None,
) -> str:
    if fork_from:
        source_session_id = store.resolve_tag(fork_from) or fork_from
        session_id = store.fork_session(source_session_id)
    if session_tag and not session_id:
        resolved = store.resolve_tag(session_tag)
        session_id = resolved if resolved else None
    if not session_id:
        session_id = store.create_session()
    if session_tag:
        store.tag_session(session_id, session_tag)
    assert session_id is not None
    return session_id


def _format_steps(steps: Iterable[ActionStep]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for step in steps:
        rows.append(
            (
                step.action_consumer,
                step.action_type,
                format_action_argument(step.action_argument),
            )
        )
    return rows


def _ensure_history_path(path: str) -> str:
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def _parse_command(text: str) -> tuple[str, list[str]]:
    parts = text.strip().split()
    command = parts[0]
    return command, parts[1:]


def _resolve_display_model(model_name: str | None) -> str:
    return (
        model_name
        or os.getenv("ACTION_PLAN_MODEL")
        or os.getenv("DEFAULT_MODEL")
        or "gpt-3.5-turbo"
    )


@dataclass(frozen=True)
class HeaderContext:
    """Structured data needed to render the CLI header."""

    title: str
    version: str
    status_label: str
    status_color: str
    model: str
    session_id: str
    base_url: str
    langfuse_enabled: bool
    langfuse_reason: str | None
    builtin_enabled: int
    builtin_disabled: int
    external_enabled: int
    external_disabled: int


def _truncate_middle(text: str, max_len: int) -> str:
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    keep = max_len - 3
    head = max(1, keep // 2)
    tail = keep - head
    return f"{text[:head]}...{text[-tail:]}"


def _short_model(model: str, max_len: int = 28) -> str:
    return _truncate_middle(model, max_len)


def _short_url(base_url: str, max_len: int = 36) -> str:
    return _truncate_middle(base_url, max_len)


def _format_model(model: str, max_len: int) -> Text:
    shortened = _short_model(model, max_len)
    if "/" not in shortened:
        return Text(shortened, style="bright_white")
    provider, name = shortened.split("/", 1)
    text = Text()
    text.append(provider, style="cyan")
    text.append("/", style="dim")
    text.append(name, style="bright_white")
    return text


def _resolve_cli_version() -> str:
    env_version = os.getenv("VERSION")
    if env_version:
        return env_version
    try:
        return version("meeseeks-cli")
    except PackageNotFoundError:
        return "0.0.0"


def _brand_line(ctx: HeaderContext, width: int) -> Text:
    title = f"■ {ctx.title} v{ctx.version}"
    status = f"o {ctx.status_label}"
    spacing = max(1, width - len(title) - len(status))
    line = Text()
    line.append(title, style="bold bright_cyan")
    line.append(" " * spacing)
    line.append(status, style=f"bold {ctx.status_color}")
    return line


def _kv_line(label: str, value: Text | str, label_width: int) -> Text:
    line = Text()
    line.append(label.ljust(label_width), style="dim")
    line.append(" ")
    if isinstance(value, Text):
        line.append_text(value)
    else:
        line.append(value)
    return line


def _langfuse_value(ctx: HeaderContext) -> Text:
    status = Text()
    status.append("o ", style="green" if ctx.langfuse_enabled else "red")
    status.append("on" if ctx.langfuse_enabled else "off", style="dim")
    return status


def _tools_value(ctx: HeaderContext) -> Text:
    text = Text()
    label_builtin = "built-in"
    label_external = "external"

    text.append(f"{label_builtin} ", style="dim")
    text.append("o", style="green")
    text.append(f" {ctx.builtin_enabled}", style="dim")
    text.append(" (", style="dim")
    text.append("o", style="red")
    text.append(f" {ctx.builtin_disabled}", style="dim")
    text.append(") ", style="dim")

    text.append("• ", style="dim")
    text.append(f"{label_external} ", style="dim")
    text.append("o", style="green")
    text.append(f" {ctx.external_enabled}", style="dim")
    text.append(" (", style="dim")
    text.append("o", style="red")
    text.append(f" {ctx.external_disabled}", style="dim")
    text.append(")", style="dim")
    return text


HEADER_STYLE = "on #0e0e0e"


def _render_header_wide(console: Console, ctx: HeaderContext) -> None:
    console.print()
    console.print(Rule(style="dim"), style=HEADER_STYLE)
    console.print(_brand_line(ctx, console.width), style=HEADER_STYLE)

    fields: list[tuple[str, Text | str]] = [
        ("model", _format_model(ctx.model, 40)),
        ("session", ctx.session_id or "(not set)"),
        ("base", _short_url(ctx.base_url, 60) if ctx.base_url else "(not set)"),
        ("langfuse", _langfuse_value(ctx)),
        ("tools", _tools_value(ctx)),
    ]
    label_width = max(len(label) for label, _ in fields)
    for label, value in fields:
        console.print(_kv_line(label, value, label_width), style=HEADER_STYLE)
    console.print(Rule(style="dim"), style=HEADER_STYLE)
    console.print()


def _render_header_normal(console: Console, ctx: HeaderContext) -> None:
    console.print()
    console.print(Rule(style="dim"), style=HEADER_STYLE)
    console.print(_brand_line(ctx, console.width), style=HEADER_STYLE)

    fields: list[tuple[str, Text | str]] = [
        ("model", _format_model(ctx.model, 34)),
        ("session", ctx.session_id or "(not set)"),
        ("langfuse", _langfuse_value(ctx)),
        ("tools", _tools_value(ctx)),
    ]
    if ctx.base_url and console.width >= 85:
        fields.append(("base", _short_url(ctx.base_url, 40)))
    label_width = max(len(label) for label, _ in fields)
    for label, value in fields:
        console.print(_kv_line(label, value, label_width), style=HEADER_STYLE)
    console.print(Rule(style="dim"), style=HEADER_STYLE)
    console.print()


def _render_header_tiny(console: Console, ctx: HeaderContext) -> None:
    model = _format_model(ctx.model, 22)
    line = Text("- ", style="dim")
    line.append(f"■ {ctx.title} v{ctx.version}", style="bold bright_cyan")
    line.append(" ")
    line.append("o", style=ctx.status_color)
    line.append(f" {ctx.status_label} ", style="dim")
    line.append_text(model)
    console.print()
    console.print(line, style=HEADER_STYLE)

    detail = Text("  Langfuse: ", style="dim")
    detail.append("o", style="green" if ctx.langfuse_enabled else "red")
    detail.append(" on" if ctx.langfuse_enabled else " off", style="dim")
    console.print(detail, style=HEADER_STYLE)

    tools_line = Text("  Tools: ", style="dim")
    tools_line.append_text(_tools_value(ctx))
    console.print(tools_line, style=HEADER_STYLE)


def render_header(console: Console, ctx: HeaderContext) -> None:
    """Render the CLI header based on terminal width."""
    width = console.width or 80
    if width >= 100:
        _render_header_wide(console, ctx)
    elif width >= 70:
        _render_header_normal(console, ctx)
    else:
        _render_header_tiny(console, ctx)


def run_cli(args: argparse.Namespace) -> int:
    """Run the CLI application loop.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code for the CLI process.
    """
    console = Console(color_system=None if args.no_color else "auto")
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    verbosity = getattr(args, "verbose", 0)
    if verbosity > 0:
        logging.info(
            "CLI logging set to {} via --verbose (count={}).",
            log_level,
            verbosity,
        )
    store = SessionStore(root_dir=args.session_dir)
    session_id = _resolve_session_id(store, args.session, args.tag, args.fork)
    state = CliState(
        session_id=session_id,
        show_plan=args.show_plan,
        model_name=args.model,
        auto_approve_all=args.auto_approve,
    )
    tool_registry = load_registry()
    registry = get_registry()

    base_url = os.getenv("OPENAI_API_BASE") or os.getenv("OPENAI_BASE_URL")
    model_name = _resolve_display_model(state.model_name)
    langfuse_status = resolve_langfuse_status()
    all_specs = tool_registry.list_specs(include_disabled=True)
    builtin_enabled = sum(1 for spec in all_specs if spec.kind == "local" and spec.enabled)
    builtin_disabled = sum(1 for spec in all_specs if spec.kind == "local" and not spec.enabled)
    external_enabled = sum(1 for spec in all_specs if spec.kind == "mcp" and spec.enabled)
    external_disabled = sum(1 for spec in all_specs if spec.kind == "mcp" and not spec.enabled)
    try:
        config = _load_mcp_config()
        configured_servers = set(config.get("servers", {}).keys())
        discovered_servers = {
            spec.metadata.get("server")
            for spec in all_specs
            if spec.kind == "mcp" and spec.metadata.get("server")
        }
        missing_servers = configured_servers - discovered_servers
        if missing_servers:
            external_disabled += len(missing_servers)
    except Exception:
        pass
    version = _resolve_cli_version()
    header_ctx = HeaderContext(
        title="Meeseeks",
        version=version,
        status_label="Ready",
        status_color="green",
        model=model_name,
        session_id=session_id,
        base_url=base_url or "",
        langfuse_enabled=langfuse_status.enabled,
        langfuse_reason=langfuse_status.reason,
        builtin_enabled=builtin_enabled,
        builtin_disabled=builtin_disabled,
        external_enabled=external_enabled,
        external_disabled=external_disabled,
    )
    render_header(console, header_ctx)
    console.print("Meeseeks CLI ready")
    console.print(f"Session: {state.session_id}")
    console.print("Type /help for commands.", style=f"dim {HEADER_STYLE}")
    console.print()

    if args.query:
        return _run_single_query(console, store, state, tool_registry, args.query, args)

    history_path = _ensure_history_path(args.history_file)
    session: PromptSession[str] = PromptSession(history=FileHistory(history_path))

    while True:
        try:
            user_input = session.prompt("meeseeks> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nBye.")
            return 0

        if not user_input:
            continue
        if user_input.startswith("/"):
            command, cmd_args = _parse_command(user_input)
            context = CommandContext(
                console=console,
                store=store,
                state=state,
                tool_registry=tool_registry,
                prompt_func=session.prompt,
            )
            if not registry.execute(command, context, cmd_args):
                return 0
            continue

        _run_query(console, store, state, tool_registry, user_input, args, session.prompt)


def _run_single_query(
    console: Console,
    store: SessionStore,
    state: CliState,
    tool_registry: ToolRegistry,
    query: str,
    args: argparse.Namespace,
) -> int:
    _run_query(console, store, state, tool_registry, query, args, None)
    return 0


def _run_query(
    console: Console,
    store: SessionStore,
    state: CliState,
    tool_registry: ToolRegistry,
    query: str,
    args: argparse.Namespace,
    prompt_func: Callable[[str], str] | None,
) -> None:
    initial_task_queue = None
    if state.show_plan:
        initial_task_queue = generate_action_plan(
            user_query=query,
            model_name=state.model_name,
            session_summary=store.load_summary(state.session_id),
        )
        _render_plan_with_registry(console, initial_task_queue, tool_registry)

    approval_callback = _build_approval_callback(
        prompt_func,
        console,
        state,
        tool_registry,
    )
    hook_manager = _build_cli_hook_manager(console, tool_registry)
    task_queue = orchestrate_session(
        user_query=query,
        model_name=state.model_name,
        max_iters=args.max_iters,
        initial_task_queue=initial_task_queue,
        session_id=state.session_id,
        session_store=store,
        tool_registry=tool_registry,
        approval_callback=approval_callback,
        hook_manager=hook_manager,
    )

    _render_results_with_registry(
        console,
        task_queue,
        tool_registry,
        highlight_latest=not bool(task_queue.task_result),
        verbose=getattr(args, "verbose", 0) > 0,
    )
    if task_queue.task_result:
        console.print(
            Panel(
                Markdown(task_queue.task_result),
                title=":speech_balloon: Response",
                border_style="bold green",
            )
        )


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the CLI."""
    parser = argparse.ArgumentParser(description="Meeseeks terminal CLI")
    parser.add_argument("--query", help="Run a single query and exit")
    parser.add_argument("--model", help="Override model name")
    parser.add_argument("--max-iters", type=int, default=3)
    parser.add_argument("--show-plan", action="store_true", default=True)
    parser.add_argument("--no-plan", action="store_false", dest="show_plan")
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase log verbosity (-v=debug, -vv=trace)",
    )
    parser.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--session", help="Existing session id")
    parser.add_argument("--tag", help="Session tag to resume or create")
    parser.add_argument("--fork", help="Session id or tag to fork from")
    parser.add_argument(
        "--session-dir",
        default=None,
        help="Override session storage directory",
    )
    parser.add_argument(
        "--history-file",
        default="~/.meeseeks/cli_history",
        help="Path to CLI history file",
    )
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Automatically approve all permission prompts",
    )
    return parser


def main() -> int:
    """Entry point for the CLI executable."""
    parser = build_parser()
    args = parser.parse_args()
    return run_cli(args)


def _tool_specs_by_id(tool_registry: ToolRegistry) -> dict[str, object]:
    return {spec.tool_id: spec for spec in tool_registry.list_specs()}


def _render_plan_with_registry(
    console: Console,
    task_queue: TaskQueue,
    tool_registry: ToolRegistry,
) -> None:
    specs = _tool_specs_by_id(tool_registry)
    lines: list[Text] = []
    for index, (tool, action, argument) in enumerate(
        _format_steps(task_queue.action_steps), start=1
    ):
        spec = specs.get(tool)
        step = task_queue.action_steps[index - 1]
        label = step.title or tool
        if spec is not None and getattr(spec, "kind", "") == "mcp":
            label = f"{label} (MCP)"
        line = Text()
        line.append("[ ] ", style="dim")
        line.append(f"{index}. ", style="bold")
        line.append(label, style="cyan")
        line.append(" • ", style="dim")
        line.append(action, style="magenta")
        if argument:
            line.append(" — ", style="dim")
            line.append(argument)
        lines.append(line)
    if not lines:
        lines.append(Text("No planned steps.", style="dim"))
    console.print(
        Panel(
            Group(*lines),
            title=":clipboard: Action Plan",
            border_style="cyan",
        )
    )


def _render_results_with_registry(
    console: Console,
    task_queue: TaskQueue,
    tool_registry: ToolRegistry,
    highlight_latest: bool = True,
    verbose: bool = False,
) -> None:
    specs = _tool_specs_by_id(tool_registry)
    panels: list[Panel] = []
    steps = task_queue.action_steps
    last_index = len(steps) - 1
    for index, step in enumerate(steps):
        spec = specs.get(step.action_consumer)
        label = step.action_consumer
        if spec is not None and getattr(spec, "kind", "") == "mcp":
            label = f"{label} (MCP)"
        label = f":wrench: {label}"
        result = None
        if step.result is not None:
            result = getattr(step.result, "content", step.result)
        is_latest = highlight_latest and index == last_index
        content_style = None if is_latest else "dim"
        border_style = "magenta" if is_latest else "dim magenta"
        renderable: Text | Syntax
        if result is None:
            renderable = Text("(no result)", style="dim")
        elif not verbose:
            renderable = Text("(output hidden; use -v/--verbose)", style="dim")
        else:
            renderable = _format_tool_output(result, content_style)
        panels.append(
            Panel(
                renderable,
                title=label,
                border_style=border_style,
                padding=(0, 0),
                box=box.MINIMAL,
            )
        )
    if not panels:
        console.print(Text("No tool results.", style="dim"))
        return
    if len(panels) == 1:
        console.print(panels[0])
        return
    console.print(Columns(panels, expand=True))


def _format_tool_output(result: object, content_style: str | None) -> Text | Syntax:
    style = content_style or ""
    if isinstance(result, dict | list):
        return Syntax(
            json.dumps(result, indent=2, ensure_ascii=True),
            "json",
            theme="ansi_dark",
            word_wrap=True,
        )
    if isinstance(result, str):
        stripped = result.strip()
        if stripped:
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict | list):
                return Syntax(
                    json.dumps(parsed, indent=2, ensure_ascii=True),
                    "json",
                    theme="ansi_dark",
                    word_wrap=True,
                )
        return Text(result, style=style)
    return Text(str(result), style=style)


def _build_cli_hook_manager(
    console: Console,
    tool_registry: ToolRegistry,
) -> HookManager:
    status_holder: dict[str, Status] = {}
    specs = _tool_specs_by_id(tool_registry)

    def _start_spinner(action_step: ActionStep) -> ActionStep:
        spec = specs.get(action_step.action_consumer)
        label = action_step.action_consumer
        if spec is not None and getattr(spec, "kind", "") == "mcp":
            label = f"{label} (MCP)"
        status = console.status(f"Running {label}...", spinner="dots")
        status.start()
        status_holder["status"] = status
        return action_step

    def _stop_spinner(action_step: ActionStep, result: MockSpeaker) -> MockSpeaker:
        status = status_holder.pop("status", None)
        if status is not None:
            status.stop()
        return result

    return HookManager(
        pre_tool_use=[_start_spinner],
        post_tool_use=[_stop_spinner],
    )


def _build_approval_callback(
    prompt_func: Callable[[str], str] | None,
    console: Console,
    state: CliState,
    tool_registry: ToolRegistry,
) -> Callable[[ActionStep], bool] | None:
    if prompt_func is None:
        return None
    dialogs = DialogFactory(console=console, prompt_func=prompt_func)
    specs_by_id = _tool_specs_by_id(tool_registry)

    def _approve(action_step: ActionStep) -> bool:
        if state.auto_approve_all:
            return True

        spec = specs_by_id.get(action_step.action_consumer)
        is_mcp = spec is not None and getattr(spec, "kind", "") == "mcp"
        server_name = None
        tool_name = None
        if is_mcp:
            metadata = getattr(spec, "metadata", {}) or {}
            server_name = metadata.get("server")
            tool_name = metadata.get("tool")
            if server_name and tool_name:
                try:
                    config = _load_mcp_config()
                    if tool_auto_approved(config, server_name, tool_name):
                        return True
                except Exception:
                    pass

        if dialogs.can_use_textual():
            choice = dialogs.select_one(
                "Approve tool use",
                ["Yes", "No", "Yes, always"],
                subtitle=(
                    f"{action_step.action_consumer}:{action_step.action_type} "
                    f"({format_action_argument(action_step.action_argument)})"
                ),
            )
            if choice is None or choice == "No":
                return False
            if choice == "Yes, always" and is_mcp and server_name and tool_name:
                try:
                    config = _load_mcp_config()
                    config = mark_tool_auto_approved(config, server_name, tool_name)
                    save_mcp_config(config)
                except Exception as exc:
                    console.print(f"Failed to persist auto-approve: {exc}")
            return True

        prompt = (
            "Approve "
            f"{action_step.action_consumer}:{action_step.action_type} "
            f"({format_action_argument(action_step.action_argument)})? [y/N/a] "
        )
        try:
            response = prompt_func(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\nApproval denied.")
            return False
        if response in {"a", "always"} and is_mcp and server_name and tool_name:
            try:
                config = _load_mcp_config()
                config = mark_tool_auto_approved(config, server_name, tool_name)
                save_mcp_config(config)
            except Exception as exc:
                console.print(f"Failed to persist auto-approve: {exc}")
            return True
        decision = PermissionDecision.ALLOW if response in {"y", "yes"} else PermissionDecision.DENY
        return decision == PermissionDecision.ALLOW

    return _approve


if __name__ == "__main__":
    raise SystemExit(main())
