"""Tests for CLI helpers and workflows."""
# ruff: noqa: I001
import json
import os
import sys
import types

test_dir = os.path.dirname(__file__)
sys.path.append(os.path.join(test_dir, ".."))
sys.path.append(os.path.join(test_dir, "..", ".."))

from core.classes import ActionStep, TaskQueue  # noqa: E402
from core.session_store import SessionStore  # noqa: E402
from core.tool_registry import ToolRegistry, ToolSpec, load_registry  # noqa: E402
from rich.console import Console  # noqa: E402

from cli_commands import get_registry  # noqa: E402
from cli_context import CliState, CommandContext  # noqa: E402
from cli_master import (  # noqa: E402
    _build_approval_callback,
    _format_steps,
    _parse_command,
    _resolve_session_id,
    _run_query,
)


class DummyStep:
    """Minimal action step stub for CLI formatting."""
    def __init__(self, tool: str, action: str, argument: str) -> None:
        """Initialize the dummy step."""
        self.action_consumer = tool
        self.action_type = action
        self.action_argument = argument


def test_parse_command():
    """Parse slash commands into command and arguments."""
    command, args = _parse_command("/tag primary")
    assert command == "/tag"
    assert args == ["primary"]


def test_format_steps():
    """Format action steps into display rows."""
    steps = [DummyStep("tool_a", "get", "status")]
    rows = _format_steps(steps)
    assert rows == [("tool_a", "get", "status")]


def test_resolve_session_id(tmp_path):
    """Resolve session IDs from tags or fork options."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = _resolve_session_id(store, None, "primary", None)
    assert session_id
    assert store.resolve_tag("primary") == session_id

    forked_id = _resolve_session_id(store, None, None, session_id)
    assert forked_id != session_id


def test_handle_new_session_command(tmp_path):
    """Create a new session via command registry."""
    store = SessionStore(root_dir=str(tmp_path))
    console = Console(record=True)
    tool_registry = load_registry()
    registry = get_registry()
    session_id = store.create_session()
    state = CliState(session_id=session_id, show_plan=True)
    context = CommandContext(
        console=console,
        store=store,
        state=state,
        tool_registry=tool_registry,
        prompt_func=None,
    )

    registry.execute("/new", context, [])

    assert state.session_id != session_id


def test_build_approval_callback_accepts():
    """Accept approval when prompt returns yes."""
    console = Console(record=True)
    state = CliState(session_id="s")
    registry = load_registry()
    callback = _build_approval_callback(lambda _: "y", console, state, registry)
    step = DummyStep("tool", "set", "arg")
    assert callback is not None
    assert callback(step) is True


def test_build_approval_callback_none():
    """Return None when approval prompting is disabled."""
    console = Console(record=True)
    state = CliState(session_id="s")
    registry = load_registry()
    assert _build_approval_callback(None, console, state, registry) is None


def test_build_approval_callback_auto_approve(tmp_path, monkeypatch):
    """Auto-approve when session flag is enabled."""
    console = Console(record=True)
    state = CliState(session_id="s", auto_approve_all=True)
    registry = load_registry()

    def _boom(_prompt):
        raise AssertionError("prompt should not be called")

    callback = _build_approval_callback(_boom, console, state, registry)
    step = DummyStep("tool", "set", "arg")
    assert callback is not None
    assert callback(step) is True


def test_mcp_yes_always_updates_config(tmp_path, monkeypatch):
    """Persist MCP auto-approve when user selects Yes, always."""
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        '{"servers": {"srv": {"transport": "http", "url": "http://example"}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("MESEEKS_MCP_CONFIG", str(config_path))

    class DummyDialogs:
        def __init__(self, *args, **kwargs):
            pass

        def can_use_textual(self):
            return True

        def select_one(self, *args, **kwargs):
            return "Yes, always"

    monkeypatch.setattr("cli_master.DialogFactory", DummyDialogs)

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="mcp_srv_tool",
            name="Tool",
            description="Tool",
            factory=lambda: None,
            kind="mcp",
            metadata={"server": "srv", "tool": "tool"},
        )
    )
    console = Console(record=True)
    state = CliState(session_id="s")
    callback = _build_approval_callback(lambda _: "y", console, state, registry)
    step = DummyStep("mcp_srv_tool", "set", "arg")
    assert callback is not None
    assert callback(step) is True
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert "tool" in config["servers"]["srv"]["auto_approve_tools"]


def test_run_query(monkeypatch, tmp_path):
    """Run a CLI query with a generated plan."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    state = CliState(session_id=session_id, show_plan=True)
    tool_registry = load_registry()
    console = Console(record=True)
    captured: dict[str, object] = {}

    def fake_generate(*args, **kwargs):
        step = ActionStep(
            action_consumer="talk_to_user_tool",
            action_type="set",
            action_argument="hi",
        )
        task_queue = TaskQueue(action_steps=[step])
        task_queue.human_message = "hi"
        return task_queue

    def fake_orchestrate(*args, **kwargs):
        captured["tool_registry"] = kwargs.get("tool_registry")
        captured["session_id"] = kwargs.get("session_id")
        step = ActionStep(
            action_consumer="talk_to_user_tool",
            action_type="set",
            action_argument="hi",
        )
        task_queue = TaskQueue(action_steps=[step])
        task_queue.task_result = "ok"
        return task_queue

    monkeypatch.setattr("cli_master.generate_action_plan", fake_generate)
    monkeypatch.setattr("cli_master.orchestrate_session", fake_orchestrate)

    _run_query(
        console,
        store,
        state,
        tool_registry,
        "hi",
        types.SimpleNamespace(max_iters=1),
        prompt_func=lambda _: "y",
    )
    assert captured["tool_registry"] is tool_registry
    assert captured["session_id"] == session_id


def test_run_cli_single_query(monkeypatch, tmp_path):
    """Execute CLI in single-query mode."""
    from cli_master import run_cli

    args = types.SimpleNamespace(
        query="hi",
        model=None,
        max_iters=1,
        show_plan=False,
        no_color=True,
        session=None,
        tag=None,
        fork=None,
        session_dir=str(tmp_path),
        history_file=str(tmp_path / "history"),
        auto_approve=False,
    )

    def fake_orchestrate(*args, **kwargs):
        step = ActionStep(
            action_consumer="talk_to_user_tool",
            action_type="set",
            action_argument="hi",
        )
        task_queue = TaskQueue(action_steps=[step])
        task_queue.task_result = "ok"
        return task_queue

    monkeypatch.setattr("cli_master.orchestrate_session", fake_orchestrate)
    assert run_cli(args) == 0


def test_run_cli_interactive_quit(monkeypatch, tmp_path):
    """Exit interactive CLI when /quit is entered."""
    from cli_master import run_cli

    args = types.SimpleNamespace(
        query=None,
        model=None,
        max_iters=1,
        show_plan=False,
        no_color=True,
        session=None,
        tag=None,
        fork=None,
        session_dir=str(tmp_path),
        history_file=str(tmp_path / "history"),
        auto_approve=False,
    )

    class DummyHistory:
        def __init__(self, *args, **kwargs):
            pass

    class DummySession:
        def __init__(self, *args, **kwargs):
            self.calls = 0

        def prompt(self, *args, **kwargs):
            self.calls += 1
            return "/quit"

    monkeypatch.setattr("cli_master.FileHistory", DummyHistory)
    monkeypatch.setattr("cli_master.PromptSession", lambda *args, **kwargs: DummySession())
    assert run_cli(args) == 0
