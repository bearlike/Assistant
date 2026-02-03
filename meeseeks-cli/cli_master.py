#!/usr/bin/env python3
"""Terminal CLI for Meeseeks."""

import argparse
import os
import sys
from collections.abc import Callable, Iterable

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from rich.columns import Columns
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text


def _verbosity_to_level(verbosity: int) -> str:
    if verbosity <= 0:
        return "INFO"
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
    os.environ.setdefault("MEESEEKS_CLI_LOG_STYLE", "dark")
    verbosity = _parse_verbosity(argv)
    if verbosity is not None:
        os.environ["LOG_LEVEL"] = _verbosity_to_level(verbosity)
        return
    existing_level = os.getenv("LOG_LEVEL", "").upper()
    if not existing_level:
        os.environ["LOG_LEVEL"] = "INFO"


_bootstrap_cli_logging_env(sys.argv)

# Add repo root to path for core imports
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from cli_commands import get_registry  # noqa: E402
from cli_context import CliState, CommandContext  # noqa: E402
from cli_dialogs import DialogFactory  # noqa: E402

from core.classes import ActionStep, TaskQueue  # noqa: E402
from core.permissions import PermissionDecision  # noqa: E402
from core.session_store import SessionStore  # noqa: E402
from core.task_master import generate_action_plan, orchestrate_session  # noqa: E402
from core.tool_registry import ToolRegistry, load_registry  # noqa: E402
from tools.integration.mcp import (  # noqa: E402
    _load_mcp_config,
    mark_tool_auto_approved,
    save_mcp_config,
    tool_auto_approved,
)


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
    return session_id


def _format_steps(steps: Iterable[ActionStep]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for step in steps:
        rows.append((step.action_consumer, step.action_type, step.action_argument))
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


def run_cli(args: argparse.Namespace) -> int:
    """Run the CLI application loop.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code for the CLI process.
    """
    console = Console(color_system=None if args.no_color else "auto")
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
    ready_lines = [
        Text("Meeseeks CLI ready"),
        Text(f"Model: {_resolve_display_model(state.model_name)}", style="dim"),
        Text(f"Base URL: {base_url or '(not set)'}", style="dim"),
    ]
    console.print(Panel(Group(*ready_lines), title="Meeseeks"))
    console.print(f"Session: {state.session_id}")
    console.print("Type /help for commands.\n")

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
    task_queue = orchestrate_session(
        user_query=query,
        model_name=state.model_name,
        max_iters=args.max_iters,
        initial_task_queue=initial_task_queue,
        session_id=state.session_id,
        session_store=store,
        tool_registry=tool_registry,
        approval_callback=approval_callback,
    )

    _render_results_with_registry(console, task_queue, tool_registry)
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
        is_latest = index == last_index
        content_style = None if is_latest else "dim"
        border_style = "magenta" if is_latest else "dim magenta"
        content = Text(
            str(result) if result is not None else "(no result)",
            style=content_style,
        )
        panels.append(
            Panel(
                content,
                title=label,
                border_style=border_style,
                padding=(0, 1),
            )
        )
    if not panels:
        console.print(Text("No tool results.", style="dim"))
        return
    if len(panels) == 1:
        console.print(panels[0])
        return
    console.print(Columns(panels, expand=True))


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
                    f"({action_step.action_argument})"
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
            f"({action_step.action_argument})? [y/N/a] "
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
