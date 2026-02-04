"""Tests for CLI helpers and workflows."""

# ruff: noqa: I001
import json
import os
import types
from importlib.metadata import PackageNotFoundError

from meeseeks_core.classes import ActionStep, TaskQueue  # noqa: E402
from meeseeks_core.common import get_mock_speaker  # noqa: E402
from meeseeks_core.session_store import SessionStore  # noqa: E402
from meeseeks_core.tool_registry import ToolRegistry, ToolSpec, load_registry  # noqa: E402
from rich.console import Console  # noqa: E402

from meeseeks_cli.cli_commands import get_registry  # noqa: E402
from meeseeks_cli.cli_context import CliState, CommandContext  # noqa: E402
from meeseeks_cli.cli_master import (
    _build_approval_callback,
    _format_steps,
    _parse_command,
    _parse_verbosity,
    _resolve_session_id,
    _resolve_cli_version,
    _run_query,
    _truncate_middle,
    _verbosity_to_level,
    _bootstrap_cli_logging_env,
    _format_model,
    render_header,
    HeaderContext,
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

    monkeypatch.setattr("meeseeks_cli.cli_master.DialogFactory", DummyDialogs)

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
            action_consumer="home_assistant_tool",
            action_type="get",
            action_argument="hi",
        )
        task_queue = TaskQueue(action_steps=[step])
        task_queue.human_message = "hi"
        return task_queue

    def fake_orchestrate(*args, **kwargs):
        captured["tool_registry"] = kwargs.get("tool_registry")
        captured["session_id"] = kwargs.get("session_id")
        step = ActionStep(
            action_consumer="home_assistant_tool",
            action_type="get",
            action_argument="hi",
        )
        task_queue = TaskQueue(action_steps=[step])
        task_queue.task_result = "ok"
        return task_queue

    monkeypatch.setattr("meeseeks_cli.cli_master.generate_action_plan", fake_generate)
    monkeypatch.setattr("meeseeks_cli.cli_master.orchestrate_session", fake_orchestrate)

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


def test_run_query_renders_tool_output_and_response(monkeypatch, tmp_path):
    """Render tool output and final response in verbose mode."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    state = CliState(session_id=session_id, show_plan=False)
    tool_registry = ToolRegistry()
    tool_registry.register(
        ToolSpec(
            tool_id="mcp_tool",
            name="MCP Tool",
            description="Tool",
            factory=lambda: None,
            kind="mcp",
        )
    )
    console = Console(record=True)

    def fake_orchestrate(*_args, **_kwargs):
        step = ActionStep(
            action_consumer="mcp_tool",
            action_type="get",
            action_argument="payload",
        )
        step.result = get_mock_speaker()(content={"foo": "bar"})
        task_queue = TaskQueue(action_steps=[step])
        task_queue.task_result = "final response"
        return task_queue

    monkeypatch.setattr("meeseeks_cli.cli_master.orchestrate_session", fake_orchestrate)

    _run_query(
        console,
        store,
        state,
        tool_registry,
        "hi",
        types.SimpleNamespace(max_iters=1, verbose=1),
        prompt_func=lambda _: "y",
    )

    output = console.export_text()
    assert '"foo": "bar"' in output
    assert "final response" in output


def test_run_query_hides_output_when_not_verbose(monkeypatch, tmp_path):
    """Hide tool output in non-verbose mode."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    state = CliState(session_id=session_id, show_plan=False)
    tool_registry = ToolRegistry()
    tool_registry.register(
        ToolSpec(
            tool_id="mcp_tool",
            name="MCP Tool",
            description="Tool",
            factory=lambda: None,
            kind="mcp",
        )
    )
    console = Console(record=True)

    def fake_orchestrate(*_args, **_kwargs):
        step = ActionStep(
            action_consumer="mcp_tool",
            action_type="get",
            action_argument="payload",
        )
        step.result = get_mock_speaker()(content={"foo": "bar"})
        return TaskQueue(action_steps=[step])

    monkeypatch.setattr("meeseeks_cli.cli_master.orchestrate_session", fake_orchestrate)

    _run_query(
        console,
        store,
        state,
        tool_registry,
        "hi",
        types.SimpleNamespace(max_iters=1, verbose=0),
        prompt_func=lambda _: "y",
    )

    output = console.export_text()
    assert "output hidden" in output


def test_run_query_renders_partial_tool_results(monkeypatch, tmp_path):
    """Render mixed tool results when a step fails and a later step succeeds."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    state = CliState(session_id=session_id, show_plan=False)
    tool_registry = ToolRegistry()
    tool_registry.register(
        ToolSpec(
            tool_id="tool_one",
            name="Tool One",
            description="Tool One",
            factory=lambda: None,
        )
    )
    tool_registry.register(
        ToolSpec(
            tool_id="tool_two",
            name="Tool Two",
            description="Tool Two",
            factory=lambda: None,
        )
    )
    console = Console(record=True)

    def fake_orchestrate(*_args, **_kwargs):
        first = ActionStep(
            action_consumer="tool_one",
            action_type="get",
            action_argument="payload",
        )
        second = ActionStep(
            action_consumer="tool_two",
            action_type="get",
            action_argument="payload",
        )
        second.result = get_mock_speaker()(content="ok")
        queue = TaskQueue(action_steps=[first, second])
        queue.task_result = "final response"
        return queue

    monkeypatch.setattr("meeseeks_cli.cli_master.orchestrate_session", fake_orchestrate)

    _run_query(
        console,
        store,
        state,
        tool_registry,
        "hi",
        types.SimpleNamespace(max_iters=1, verbose=1),
        prompt_func=lambda _: "y",
    )

    output = console.export_text()
    assert "(no result)" in output
    assert "ok" in output


def test_run_query_dims_tool_panels_after_response(monkeypatch, tmp_path):
    """Disable highlight when a final response exists."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    state = CliState(session_id=session_id, show_plan=False)
    tool_registry = ToolRegistry()
    console = Console(record=True)
    captured: dict[str, object] = {}

    def fake_orchestrate(*_args, **_kwargs):
        step = ActionStep(
            action_consumer="tool",
            action_type="get",
            action_argument="payload",
        )
        step.result = get_mock_speaker()(content="ok")
        queue = TaskQueue(action_steps=[step])
        queue.task_result = "final"
        return queue

    def fake_render(*_args, **kwargs):
        captured["highlight_latest"] = kwargs.get("highlight_latest")

    monkeypatch.setattr("meeseeks_cli.cli_master.orchestrate_session", fake_orchestrate)
    monkeypatch.setattr("meeseeks_cli.cli_master._render_results_with_registry", fake_render)

    _run_query(
        console,
        store,
        state,
        tool_registry,
        "hi",
        types.SimpleNamespace(max_iters=1, verbose=0),
        prompt_func=lambda _: "y",
    )

    assert captured["highlight_latest"] is False


def test_render_header_responsive_modes():
    """Render header across width breakpoints."""
    ctx = HeaderContext(
        title="Meeseeks",
        version="0.1.0",
        status_label="Ready",
        status_color="green",
        model="openai/gpt-4o-mini",
        session_id="session-123",
        base_url="http://127.0.0.1:4136/v1",
        langfuse_enabled=False,
        langfuse_reason="disabled",
        builtin_enabled=1,
        builtin_disabled=2,
        external_enabled=3,
        external_disabled=1,
    )

    cases = [
        (120, ["model", "session", "base"]),
        (90, ["model", "session", "base"]),
        (60, ["Langfuse:", "Tools:"]),
    ]
    for width, expected in cases:
        console = Console(record=True, width=width)
        render_header(console, ctx)
        output = console.export_text()
        for token in expected:
            assert token in output


def test_cli_bootstrap_and_format_helpers(monkeypatch):
    """Cover CLI helper branches in a single flow."""
    assert _truncate_middle("abcdef", 0) == ""
    assert _truncate_middle("abcdef", 3) == "abc"
    assert _truncate_middle("abcdef", 4) == "a...abcdef"
    assert _parse_verbosity(["prog", "-vv"]) == 2
    assert _parse_verbosity(["prog", "--verbose=3"]) == 3
    assert _verbosity_to_level(0) == "WARNING"
    assert _verbosity_to_level(1) == "DEBUG"
    assert _verbosity_to_level(2) == "TRACE"

    monkeypatch.delenv("LOG_LEVEL", raising=False)
    _bootstrap_cli_logging_env(["prog"])
    assert os.getenv("LOG_LEVEL") == "WARNING"
    _bootstrap_cli_logging_env(["prog", "-vv"])
    assert os.getenv("LOG_LEVEL") == "TRACE"

    monkeypatch.setenv("VERSION", "9.9.9")
    assert _resolve_cli_version() == "9.9.9"
    monkeypatch.delenv("VERSION", raising=False)
    assert _resolve_cli_version()

    def _raise_missing(_name):
        raise PackageNotFoundError()

    monkeypatch.setattr(
        "meeseeks_cli.cli_master.version",
        _raise_missing,
    )
    assert _resolve_cli_version() == "0.0.0"
    assert _format_model("gpt-4o", 10).plain == "gpt-4o"


def test_run_cli_single_query(monkeypatch, tmp_path):
    """Execute CLI in single-query mode."""
    from meeseeks_cli.cli_master import run_cli

    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        '{"servers": {"missing": {"transport": "http", "url": "http://example"}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("MESEEKS_MCP_CONFIG", str(config_path))

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
        verbose=1,
    )

    def fake_header(*args, **kwargs):
        return None

    def fake_orchestrate(*args, **kwargs):
        step = ActionStep(
            action_consumer="home_assistant_tool",
            action_type="get",
            action_argument="hi",
        )
        task_queue = TaskQueue(action_steps=[step])
        task_queue.task_result = "ok"
        return task_queue

    monkeypatch.setattr("meeseeks_cli.cli_master.render_header", fake_header)
    monkeypatch.setattr("meeseeks_cli.cli_master.orchestrate_session", fake_orchestrate)
    monkeypatch.setattr("meeseeks_cli.cli_master.load_registry", lambda: ToolRegistry())
    assert run_cli(args) == 0


def test_run_cli_interactive_quit(monkeypatch, tmp_path):
    """Exit interactive CLI when /quit is entered."""
    from meeseeks_cli.cli_master import run_cli

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

    monkeypatch.setattr("meeseeks_cli.cli_master.render_header", lambda *args, **kwargs: None)
    monkeypatch.setattr("meeseeks_cli.cli_master.FileHistory", DummyHistory)
    monkeypatch.setattr(
        "meeseeks_cli.cli_master.PromptSession",
        lambda *args, **kwargs: DummySession(),
    )
    assert run_cli(args) == 0
