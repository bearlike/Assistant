"""Tests for CLI command handlers."""

# ruff: noqa: I001
import json

import pytest
from rich.console import Console

from meeseeks_core.config import set_config_override, set_mcp_config_path  # noqa: E402
from meeseeks_core.session_runtime import SessionRuntime  # noqa: E402
from meeseeks_core.session_store import SessionStore  # noqa: E402
from meeseeks_core.tool_registry import ToolRegistry, ToolSpec  # noqa: E402

import meeseeks_cli.cli_commands as cli_commands
from meeseeks_cli.cli_commands import get_registry  # noqa: E402
from meeseeks_cli.cli_context import CliState, CommandContext  # noqa: E402


class DummyQueue:
    """Minimal task queue stub for compact command results."""

    def __init__(self, result: str):
        """Initialize the dummy queue with a result."""
        self.task_result = result


def _make_context(tmp_path):
    store = SessionStore(root_dir=str(tmp_path))
    state = CliState(session_id=store.create_session(), show_plan=True, model_name=None)
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            tool_id="dummy_tool",
            name="Dummy",
            description="Dummy",
            factory=lambda: None,
        )
    )
    console = Console(record=True)
    runtime = SessionRuntime(session_store=store)
    return CommandContext(
        console=console,
        store=store,
        state=state,
        tool_registry=registry,
        runtime=runtime,
        prompt_func=lambda _: "",
    )


def test_command_help(tmp_path):
    """Render help text for CLI commands."""
    context = _make_context(tmp_path)
    registry = get_registry()
    assert registry.execute("/help", context, []) is True
    assert registry.help_text()


def test_command_exit_quit(tmp_path):
    """Exit the CLI on /exit and /quit commands."""
    context = _make_context(tmp_path)
    registry = get_registry()
    assert registry.execute("/exit", context, []) is False
    assert registry.execute("/quit", context, []) is False


def test_command_new_session(tmp_path):
    """Create a new session using CLI command."""
    context = _make_context(tmp_path)
    registry = get_registry()
    old_id = context.state.session_id
    registry.execute("/new", context, [])
    assert context.state.session_id != old_id


def test_command_summary_and_tokens(tmp_path):
    """Render summary and token budget commands."""
    context = _make_context(tmp_path)
    context.store.save_summary(context.state.session_id, "summary")
    registry = get_registry()
    assert registry.execute("/summary", context, []) is True
    assert registry.execute("/tokens", context, []) is True
    assert registry.execute("/budget", context, []) is True


def test_command_tag_fork_plan(tmp_path):
    """Tag, fork, and toggle plan display commands."""
    context = _make_context(tmp_path)
    registry = get_registry()
    assert registry.execute("/tag", context, ["primary"]) is True
    assert context.store.resolve_tag("primary") == context.state.session_id
    original = context.state.session_id
    assert registry.execute("/fork", context, ["forked"]) is True
    assert context.state.session_id != original
    assert registry.execute("/plan", context, ["off"]) is True
    assert context.state.show_plan is False
    assert registry.execute("/plan", context, ["maybe"]) is True


def test_command_automatic(tmp_path):
    """Enable automatic approvals via command."""
    context = _make_context(tmp_path)
    registry = get_registry()
    assert registry.execute("/automatic", context, ["--yes"]) is True
    assert context.state.auto_approve_all is True


def test_command_automatic_confirm(monkeypatch, tmp_path):
    """Enable automatic approvals via confirmation prompt."""
    context = _make_context(tmp_path)
    registry = get_registry()

    class DummyDialogs:
        def __init__(self, *args, **kwargs):
            pass

        def confirm(self, *args, **kwargs):
            return True

    monkeypatch.setattr(cli_commands, "DialogFactory", DummyDialogs)
    assert registry.execute("/automatic", context, []) is True
    assert context.state.auto_approve_all is True


def test_command_status_and_terminate(tmp_path):
    """Handle status and terminate commands without errors."""
    context = _make_context(tmp_path)
    registry = get_registry()
    assert registry.execute("/status", context, []) is True
    assert registry.execute("/terminate", context, []) is True


def test_command_models(monkeypatch, tmp_path):
    """Select a model via the CLI wizard."""
    context = _make_context(tmp_path)
    registry = get_registry()

    monkeypatch.setattr(
        cli_commands,
        "_fetch_models",
        lambda: ["model-a", "model-b"],
    )
    context.prompt_func = lambda _: "1"
    assert registry.execute("/models", context, []) is True
    assert context.state.model_name == "model-a"


def test_command_models_textual_selection(monkeypatch, tmp_path):
    """Select a model via Textual dialog path."""
    context = _make_context(tmp_path)
    registry = get_registry()

    class DummyDialogs:
        def __init__(self, *args, **kwargs):
            pass

        def can_use_textual(self):
            return True

        def select_one(self, *args, **kwargs):
            return "model-b"

    monkeypatch.setattr(cli_commands, "DialogFactory", DummyDialogs)
    monkeypatch.setattr(cli_commands, "_fetch_models", lambda: ["model-a", "model-b"])

    assert registry.execute("/models", context, []) is True
    assert context.state.model_name == "model-b"


def test_command_models_invalid_choice(monkeypatch, tmp_path):
    """Ignore invalid model selection choices."""
    context = _make_context(tmp_path)
    registry = get_registry()

    monkeypatch.setattr(
        cli_commands,
        "_fetch_models",
        lambda: ["model-a"],
    )
    context.prompt_func = lambda _: "5"
    assert registry.execute("/models", context, []) is True
    assert context.state.model_name is None


def test_command_models_no_prompt(tmp_path):
    """Handle model command without interactive prompt."""
    context = _make_context(tmp_path)
    context.prompt_func = None
    registry = get_registry()
    assert registry.execute("/models", context, []) is True


def test_fetch_models_success(monkeypatch):
    """Fetch available models from the configured endpoint."""
    set_config_override({"llm": {"api_base": "http://example.com/v1", "api_key": "key"}})

    class DummyResponse:
        def read(self):
            return b'{"data": [{"id": "model-x"}]}'

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(cli_commands, "urlopen", lambda *args, **kwargs: DummyResponse())
    models = cli_commands._fetch_models()
    assert models == ["model-x"]


def test_fetch_models_missing_env():
    """Raise when model fetch env vars are missing."""
    set_config_override({"llm": {"api_base": "", "api_key": "key"}}, replace=True)
    with pytest.raises(RuntimeError):
        cli_commands._fetch_models()


def test_handle_model_wizard_cancel(monkeypatch, tmp_path):
    """Cancel the model wizard without selecting a model."""
    context = _make_context(tmp_path)
    console = context.console

    monkeypatch.setattr(cli_commands, "_fetch_models", lambda: ["model-a"])
    cli_commands._handle_model_wizard(console, context, lambda _: "")
    assert context.state.model_name is None


def test_command_mcp(tmp_path):
    """Render MCP command output with valid config."""
    context = _make_context(tmp_path)
    registry = get_registry()

    config_path = tmp_path / "mcp.json"
    config_path.write_text(json.dumps({"servers": {"srv": {"transport": "stdio"}}}))
    set_mcp_config_path(config_path)

    registry.execute("/mcp", context, [])
    assert "MCP" in context.console.export_text()


def test_command_mcp_shows_servers_without_tools(tmp_path):
    """Render configured MCP servers even when no tools are discovered."""
    context = _make_context(tmp_path)
    registry = get_registry()

    config_path = tmp_path / "mcp.json"
    config_path.write_text(json.dumps({"servers": {"srv": {"transport": "stdio"}}}))
    set_mcp_config_path(config_path)

    registry.execute("/mcp", context, [])
    output = context.console.export_text()
    assert "srv" in output
    assert "No MCP tools configured." in output
    assert "Built-in Tools" in output


def test_command_mcp_selects_single_tool(monkeypatch, tmp_path):
    """Filter MCP tools via interactive selection."""
    context = _make_context(tmp_path)
    registry = get_registry()

    context.tool_registry.register(
        ToolSpec(
            tool_id="mcp_tool_one",
            name="Tool One",
            description="Tool One",
            factory=lambda: None,
            kind="mcp",
            metadata={"server": "srv", "tool": "tool_one"},
        )
    )
    context.tool_registry.register(
        ToolSpec(
            tool_id="mcp_tool_two",
            name="Tool Two",
            description="Tool Two",
            factory=lambda: None,
            kind="mcp",
            metadata={"server": "srv", "tool": "tool_two"},
        )
    )

    class DummyDialogs:
        def __init__(self, *args, **kwargs):
            pass

        def select_one(self, _title, _options, **_kwargs):
            return "mcp_tool_two"

    monkeypatch.setattr(cli_commands, "DialogFactory", DummyDialogs)
    registry.execute("/mcp", context, [])
    output = context.console.export_text()
    assert "mcp_tool_two" in output
    assert "mcp_tool_one" not in output


def test_command_mcp_lists_disabled_tools(tmp_path):
    """Include disabled MCP tools in the output."""
    context = _make_context(tmp_path)
    registry = get_registry()

    context.tool_registry.register(
        ToolSpec(
            tool_id="mcp_tool_disabled",
            name="Tool Disabled",
            description="Tool Disabled",
            factory=lambda: None,
            kind="mcp",
            enabled=False,
            metadata={
                "server": "srv",
                "tool": "tool_disabled",
                "disabled_reason": "Discovery failed",
            },
        )
    )

    registry.execute("/mcp", context, [])
    output = context.console.export_text()
    assert "mcp_tool_disabled" in output
    assert "disabled" in output
    assert "Built-in Tools" in output


def test_render_mcp_invalid_json(tmp_path):
    """Handle invalid MCP config JSON gracefully."""
    context = _make_context(tmp_path)
    config_path = tmp_path / "mcp.json"
    config_path.write_text("{bad json")
    set_mcp_config_path(config_path)
    cli_commands._render_mcp(context.console, context.tool_registry)
    assert "Failed to read MCP config" in context.console.export_text()


def test_command_config_init_creates_example(monkeypatch, tmp_path):
    """Create example config file via CLI command."""
    context = _make_context(tmp_path)
    registry = get_registry()

    example_path = tmp_path / "app.example.json"
    monkeypatch.setattr("meeseeks_core.config._APP_EXAMPLE_PATH", example_path)

    registry.execute("/config", context, ["init", "--force"])
    assert example_path.exists()
    assert "Created config example" in context.console.export_text()


def test_command_mcp_init_creates_config(tmp_path):
    """Create MCP config file via CLI command."""
    context = _make_context(tmp_path)
    registry = get_registry()

    config_path = tmp_path / "mcp.json"
    set_mcp_config_path(config_path)

    registry.execute("/mcp", context, ["init", "--force"])
    assert config_path.exists()
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert "servers" in payload


def test_command_compact(monkeypatch, tmp_path):
    """Compact sessions via CLI command."""
    context = _make_context(tmp_path)
    registry = get_registry()

    def fake_run_sync(*args, **kwargs):
        return DummyQueue("compacted")

    monkeypatch.setattr(context.runtime, "run_sync", fake_run_sync)
    assert registry.execute("/compact", context, []) is True


def test_unknown_command(tmp_path):
    """Return help for unknown command tokens."""
    context = _make_context(tmp_path)
    registry = get_registry()
    assert registry.execute("/unknown", context, []) is True
