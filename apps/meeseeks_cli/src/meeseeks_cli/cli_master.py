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
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from rich import box
from rich.columns import Columns
from rich.console import Console, Group, RenderableType
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
    """Configure logging overrides for the CLI before core imports."""
    from meeseeks_core.config import set_config_override

    verbosity = _parse_verbosity(argv)
    if verbosity is not None:
        set_config_override({"runtime": {"log_level": _verbosity_to_level(verbosity)}})
        return
    set_config_override({"runtime": {"log_level": "WARNING"}})


_bootstrap_cli_logging_env(sys.argv)

from meeseeks_core.classes import ActionStep, TaskQueue
from meeseeks_core.common import MockSpeaker, format_action_argument, get_logger
from meeseeks_core.components import resolve_langfuse_status
from meeseeks_core.config import (
    AppConfig,
    get_app_config_path,
    get_config,
    get_config_value,
    get_mcp_config_path,
    start_preflight,
)
from meeseeks_core.hooks import HookManager
from meeseeks_core.permissions import PermissionDecision, auto_approve
from meeseeks_core.session_runtime import SessionRuntime
from meeseeks_core.session_store import SessionStore
from meeseeks_core.task_master import generate_action_plan
from meeseeks_core.tool_registry import ToolRegistry, load_registry
from meeseeks_tools.integration.mcp import (
    _load_mcp_config,
    mark_tool_auto_approved,
    save_mcp_config,
    tool_auto_approved,
)

from meeseeks_cli.aider_ui import (
    render_diff,
    render_dir_payload,
    render_file_payload,
    render_markdown,
    render_shell_payload,
)
from meeseeks_cli.cli_commands import get_registry
from meeseeks_cli.cli_context import CliState, CommandContext
from meeseeks_cli.cli_dialogs import DialogFactory, _confirm_aider, _confirm_rich_panel

logging = get_logger(name="meeseeks.cli")


def _resolve_session_id(
    runtime: SessionRuntime,
    session_id: str | None,
    session_tag: str | None,
    fork_from: str | None,
) -> str:
    return runtime.resolve_session(
        session_id=session_id,
        session_tag=session_tag,
        fork_from=fork_from,
    )


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


def _render_resume_hint(console: Console, session_id: str, session_dir: str | None) -> None:
    command = f"uv run meeseeks --session {session_id}"
    if session_dir:
        command = f"{command} --session-dir {session_dir}"
    console.print(f"Resume: {command}", style="dim")


def _parse_command(text: str) -> tuple[str, list[str]]:
    parts = text.strip().split()
    command = parts[0]
    return command, parts[1:]


def _resolve_display_model(model_name: str | None) -> str:
    return (
        model_name
        or get_config_value("llm", "action_plan_model")
        or get_config_value("llm", "default_model")
        or "gpt-5.2"
    )


def _resolve_query_mode(query: str, state: CliState) -> str:
    lowered = query.strip().lower()
    plan_triggers = [
        "make a plan",
        "create a plan",
        "draft a plan",
        "plan the",
        "plan for",
        "planning",
    ]
    if any(trigger in lowered for trigger in plan_triggers):
        return "plan"
    return state.mode if state.mode in {"plan", "act"} else "act"


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
    configured = get_config_value("runtime", "version")
    if configured:
        return str(configured)
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
    config = get_config()
    logging.info(
        "Config paths: app={} mcp={}",
        get_app_config_path(),
        get_mcp_config_path() or "(disabled)",
    )
    logging.info(
        "LLM config: default={} action_plan={} tool={} api_base={} model_override={}",
        get_config_value("llm", "default_model", default=""),
        get_config_value("llm", "action_plan_model", default=""),
        get_config_value("llm", "tool_model", default=""),
        get_config_value("llm", "api_base", default=""),
        args.model or "",
    )
    if config.runtime.preflight_enabled:
        start_preflight(
            config,
            on_complete=lambda results: _render_preflight_warnings(console, results),
        )
    log_level = str(get_config_value("runtime", "log_level", default="INFO")).upper()
    verbosity = getattr(args, "verbose", 0)
    if verbosity > 0:
        logging.info(
            "CLI logging set to {} via --verbose (count={}).",
            log_level,
            verbosity,
        )
    store = SessionStore(root_dir=args.session_dir)
    runtime = SessionRuntime(session_store=store)
    session_id = _resolve_session_id(runtime, args.session, args.tag, args.fork)
    state = CliState(
        session_id=session_id,
        show_plan=args.show_plan,
        model_name=args.model,
        auto_approve_all=args.auto_approve,
    )
    tool_registry = load_registry()
    registry = get_registry()

    base_url = get_config_value("llm", "api_base") or ""
    model_name = _resolve_display_model(state.model_name)
    langfuse_status = resolve_langfuse_status()
    all_specs = tool_registry.list_specs(include_disabled=True)
    builtin_enabled = sum(1 for spec in all_specs if spec.kind == "local" and spec.enabled)
    builtin_disabled = sum(1 for spec in all_specs if spec.kind == "local" and not spec.enabled)
    external_enabled = sum(1 for spec in all_specs if spec.kind == "mcp" and spec.enabled)
    external_disabled = sum(1 for spec in all_specs if spec.kind == "mcp" and not spec.enabled)
    try:
        mcp_config = _load_mcp_config()
        configured_servers = set(mcp_config.get("servers", {}).keys())
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
    _maybe_warn_missing_configs(console, tool_registry, config)
    console.print("Meeseeks CLI ready")
    console.print(f"Session: {state.session_id}")
    console.print("Type /help for commands.", style=f"dim {HEADER_STYLE}")
    console.print()

    if args.query:
        return _run_single_query(console, store, runtime, state, tool_registry, args.query, args)

    history_path = _ensure_history_path(args.history_file)
    session: PromptSession[str] = PromptSession(history=FileHistory(history_path))

    while True:
        try:
            user_input = session.prompt("meeseeks> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nBye.")
            _render_resume_hint(console, state.session_id, args.session_dir)
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
                runtime=runtime,
                prompt_func=session.prompt,
            )
            if not registry.execute(command, context, cmd_args):
                _render_resume_hint(console, state.session_id, args.session_dir)
                return 0
            continue

        _run_query(
            console,
            store,
            runtime,
            state,
            tool_registry,
            user_input,
            args,
            session.prompt,
        )


def _run_single_query(
    console: Console,
    store: SessionStore,
    runtime: SessionRuntime,
    state: CliState,
    tool_registry: ToolRegistry,
    query: str,
    args: argparse.Namespace,
) -> int:
    _run_query(console, store, runtime, state, tool_registry, query, args, None)
    return 0


def _run_query(
    console: Console,
    store: SessionStore,
    runtime: SessionRuntime,
    state: CliState,
    tool_registry: ToolRegistry,
    query: str,
    args: argparse.Namespace,
    prompt_func: Callable[[str], str] | None,
) -> None:
    initial_task_queue = None
    mode = _resolve_query_mode(query, state)
    if state.show_plan:
        initial_task_queue = generate_action_plan(
            user_query=query,
            model_name=state.model_name,
            session_summary=store.load_summary(state.session_id),
            mode=mode,
        )
        _render_plan_with_registry(console, initial_task_queue, tool_registry)

    auto_approve_enabled = bool(
        state.auto_approve_all or getattr(args, "auto_approve", False) or prompt_func is None
    )
    logging.debug(
        "Auto-approve resolved: {} (state={}, args={}, prompt_func_none={})",
        auto_approve_enabled,
        state.auto_approve_all,
        getattr(args, "auto_approve", False),
        prompt_func is None,
    )
    approval_callback = _build_approval_callback(
        prompt_func,
        console,
        state,
        tool_registry,
        auto_approve_enabled=auto_approve_enabled,
    )
    if approval_callback is None and prompt_func is None:
        logging.debug("Forcing auto-approve for headless query execution.")
        approval_callback = auto_approve
    hook_manager = _build_cli_hook_manager(console, tool_registry)
    task_queue = runtime.run_sync(
        user_query=query,
        model_name=state.model_name,
        max_iters=args.max_iters,
        initial_task_queue=initial_task_queue,
        session_id=state.session_id,
        tool_registry=tool_registry,
        approval_callback=approval_callback,
        hook_manager=hook_manager,
        mode=mode,
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
                render_markdown(task_queue.task_result),
                title=":speech_balloon: Response",
                border_style="bold green",
            )
        )


def _maybe_warn_missing_configs(
    console: Console,
    tool_registry: ToolRegistry,
    config: AppConfig,
) -> None:
    config_path = Path("configs/app.json")
    mcp_path = Path(get_mcp_config_path())
    missing: list[str] = []
    if not config_path.exists():
        missing.append("configs/app.json")
    if not mcp_path.exists():
        missing.append("configs/mcp.json")
    if not missing:
        pass
    else:
        console.print(
            "Config files missing: "
            + ", ".join(missing)
            + ". Run /config init, /mcp init, or /init to scaffold examples.",
            style="yellow",
        )

    llm_api_base = get_config_value("llm", "api_base", default="")
    llm_api_key = get_config_value("llm", "api_key", default="")
    if not llm_api_base:
        console.print("LLM base URL is not set (llm.api_base).", style="yellow")
    if not llm_api_key:
        console.print("LLM API key is not set (llm.api_key).", style="yellow")

    langfuse_enabled, langfuse_reason, _ = config.langfuse.evaluate()
    if config.langfuse.enabled and not langfuse_enabled:
        console.print(f"Langfuse disabled: {langfuse_reason}", style="yellow")

    ha_enabled, ha_reason, _ = config.home_assistant.evaluate()
    if config.home_assistant.enabled and not ha_enabled:
        console.print(f"Home Assistant disabled: {ha_reason}", style="yellow")

    disabled_tools = [
        spec for spec in tool_registry.list_specs(include_disabled=True) if not spec.enabled
    ]
    for spec in disabled_tools:
        reason = spec.metadata.get("disabled_reason") or "disabled"
        console.print(f"Tool {spec.tool_id} is disabled: {reason}", style="yellow")

    try:
        from meeseeks_tools.integration import mcp as mcp_module

        failures = mcp_module.get_last_discovery_failures()
        for server, reason in failures.items():
            console.print(f"MCP server {server} unreachable: {reason}", style="yellow")
    except Exception:
        pass


def _render_preflight_warnings(console: Console, results: dict[str, dict[str, object]]) -> None:
    failures = [
        (name, info) for name, info in results.items() if info.get("enabled") and not info.get("ok")
    ]
    for name, info in failures:
        reason = info.get("reason") or "unknown failure"
        console.print(f"Preflight failed for {name}: {reason}", style="yellow")


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
        renderable: RenderableType
        if result is None:
            renderable = Text("(no result)", style="dim")
        elif not verbose and not _should_force_preview(result):
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


def _should_force_preview(result: object) -> bool:
    if not isinstance(result, dict):
        return False
    kind_raw = result.get("kind")
    kind = kind_raw if isinstance(kind_raw, str) else str(kind_raw or "")
    kind = kind.strip().lower()
    return kind in {"diff", "file"}


def _format_tool_output(result: object, content_style: str | None) -> RenderableType:
    style = content_style or ""
    if isinstance(result, dict):
        renderable = _render_tool_payload(result, style)
        if renderable is not None:
            return renderable
        return Syntax(
            json.dumps(result, indent=2, ensure_ascii=True),
            "json",
            theme="ansi_dark",
            word_wrap=True,
        )
    if isinstance(result, list):
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


def _render_tool_payload(payload: dict[str, object], style: str) -> RenderableType | None:
    kind_raw = payload.get("kind")
    kind = kind_raw if isinstance(kind_raw, str) else str(kind_raw or "")
    kind = kind.strip().lower()
    if kind == "diff":
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return Text("(empty diff)", style="dim")
        return render_diff(text)
    if kind == "file":
        path = payload.get("path")
        text = payload.get("text")
        if isinstance(path, str) and isinstance(text, str):
            return render_file_payload(path, text)
    if kind == "dir":
        path = payload.get("path")
        entries = payload.get("entries")
        if isinstance(path, str) and isinstance(entries, list):
            return render_dir_payload(path, [str(item) for item in entries])
    if kind == "shell":
        command = payload.get("command")
        exit_code = payload.get("exit_code")
        stdout = payload.get("stdout")
        stderr = payload.get("stderr")
        duration_ms = payload.get("duration_ms")
        cwd = payload.get("cwd")
        return render_shell_payload(
            command if isinstance(command, str) else None,
            stdout if isinstance(stdout, str) else None,
            stderr if isinstance(stderr, str) else None,
            exit_code if isinstance(exit_code, int) else None,
            duration_ms if isinstance(duration_ms, int) else None,
            cwd if isinstance(cwd, str) else None,
        )
    return None


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
    *,
    auto_approve_enabled: bool,
) -> Callable[[ActionStep], bool] | None:
    logging.debug(
        "Approval callback build: auto_approve_enabled={}, prompt_func_none={}",
        auto_approve_enabled,
        prompt_func is None,
    )
    if auto_approve_enabled:
        logging.debug("Approval callback: auto-approve enabled.")
        return auto_approve
    if prompt_func is None:
        logging.debug("Approval callback: prompt disabled, returning None.")
        return None
    dialogs = DialogFactory(console=console, prompt_func=prompt_func, prefer_inline=True)
    approval_style = str(get_config_value("cli", "approval_style", default="inline")).strip()
    approval_style = approval_style.lower()
    specs_by_id = _tool_specs_by_id(tool_registry)

    def _approve(action_step: ActionStep) -> bool:
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

        subject = (
            f"{action_step.action_consumer}:{action_step.action_type} "
            f"({format_action_argument(action_step.action_argument)})"
        )
        if approval_style in {"aider", "inline", "rich"}:
            rich_decision = _confirm_rich_panel(
                console,
                prompt_func,
                "Approve tool use?",
                subject=subject,
                default=False,
                allow_always=bool(is_mcp),
                allow_session=True,
            )
            if rich_decision == "always":
                if is_mcp and server_name and tool_name:
                    try:
                        config = _load_mcp_config()
                        config = mark_tool_auto_approved(config, server_name, tool_name)
                        save_mcp_config(config)
                    except Exception as exc:
                        console.print(f"Failed to persist auto-approve: {exc}")
                return True
            if rich_decision == "session":
                state.auto_approve_all = True
                return True
            if rich_decision == "yes":
                if is_mcp and server_name and tool_name:
                    try:
                        config = _load_mcp_config()
                        config = mark_tool_auto_approved(config, server_name, tool_name)
                        save_mcp_config(config)
                    except Exception as exc:
                        console.print(f"Failed to persist auto-approve: {exc}")
                return True
            if rich_decision == "no":
                return False

            if approval_style == "aider":
                decision = _confirm_aider(
                    "Approve tool use?",
                    default=False,
                    subject=subject,
                    prompt_func=prompt_func,
                )
                if decision is not None:
                    return decision

        if approval_style == "textual" and dialogs.can_use_textual():
            choice = dialogs.select_one(
                "Approve tool use",
                ["Yes", "No", "Yes, always", "Yes, this session"],
                subtitle=subject,
            )
            if choice is None or choice == "No":
                return False
            if choice == "Yes, this session":
                state.auto_approve_all = True
                return True
            if choice == "Yes, always" and is_mcp and server_name and tool_name:
                try:
                    config = _load_mcp_config()
                    config = mark_tool_auto_approved(config, server_name, tool_name)
                    save_mcp_config(config)
                except Exception as exc:
                    console.print(f"Failed to persist auto-approve: {exc}")
            return True

        prompt = f"Approve {subject}? [y/N/a/s] "
        try:
            response = prompt_func(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\nApproval denied.")
            return False
        if response in {"s", "session"}:
            state.auto_approve_all = True
            return True
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
