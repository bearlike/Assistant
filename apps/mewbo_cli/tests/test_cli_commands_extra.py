"""Extra tests for CLI command handlers — targeting uncovered branches in cli_commands.py."""

# ruff: noqa: I001
import json
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from mewbo_core.config import set_config_override, set_mcp_config_path
from mewbo_core.session_runtime import SessionRuntime
from mewbo_core.session_store import SessionStore
from mewbo_core.tool_registry import ToolRegistry, ToolSpec

import mewbo_cli.cli_commands as cli_commands
from mewbo_cli.cli_commands import get_registry, _render_mcp, _handle_model_wizard
from mewbo_cli.cli_context import CliState, CommandContext


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _DummyQueue:
    """Minimal TaskQueue stub."""

    def __init__(self, result: str = "", error: str = "") -> None:
        self.task_result = result
        self.last_error = error


def _make_context(tmp_path, *, prompt_func=None) -> CommandContext:
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
        prompt_func=prompt_func or (lambda _: ""),
    )


class _AlwaysConfirmDialogs:
    """Dialog stub that confirms everything."""

    def __init__(self, *args, **kwargs):
        pass

    def confirm(self, *args, **kwargs):
        return True

    def can_use_textual(self):
        return False

    def prompt_text(self, *args, **kwargs):
        return "my-tag"

    def select_one(self, *args, **kwargs):
        return None


class _CancelDialogs:
    """Dialog stub that cancels every prompt."""

    def __init__(self, *args, **kwargs):
        pass

    def confirm(self, *args, **kwargs):
        return False

    def can_use_textual(self):
        return False

    def prompt_text(self, *args, **kwargs):
        return None

    def select_one(self, *args, **kwargs):
        return None


# ---------------------------------------------------------------------------
# /session command
# ---------------------------------------------------------------------------


def test_cmd_session_prints_id(tmp_path):
    """Print the current session ID."""
    ctx = _make_context(tmp_path)
    registry = get_registry()
    assert registry.execute("/session", ctx, []) is True
    assert ctx.state.session_id in ctx.console.export_text()


# ---------------------------------------------------------------------------
# /plan command — state toggle branches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["on"], True),
        (["yes"], True),
        (["true"], True),
        (["off"], False),
        (["no"], False),
        (["false"], False),
    ],
)
def test_cmd_plan_toggle(tmp_path, args, expected):
    """Toggle plan display with recognized on/off aliases."""
    ctx = _make_context(tmp_path)
    registry = get_registry()
    result = registry.execute("/plan", ctx, args)
    assert result is True
    assert ctx.state.show_plan is expected


def test_cmd_plan_no_args_prints_state(tmp_path):
    """/plan with no args prints current state."""
    ctx = _make_context(tmp_path)
    registry = get_registry()
    ctx.state.show_plan = True
    registry.execute("/plan", ctx, [])
    output = ctx.console.export_text()
    assert "on" in output


# ---------------------------------------------------------------------------
# /mode command
# ---------------------------------------------------------------------------


def test_cmd_mode_no_args_prints_current(tmp_path):
    """/mode with no args reports current mode."""
    ctx = _make_context(tmp_path)
    registry = get_registry()
    registry.execute("/mode", ctx, [])
    output = ctx.console.export_text()
    assert "act" in output


def test_cmd_mode_set_plan(tmp_path):
    """/mode plan switches orchestration mode."""
    ctx = _make_context(tmp_path)
    registry = get_registry()
    assert registry.execute("/mode", ctx, ["plan"]) is True
    assert ctx.state.mode == "plan"


def test_cmd_mode_set_act(tmp_path):
    """/mode act restores act mode."""
    ctx = _make_context(tmp_path)
    ctx.state.mode = "plan"
    registry = get_registry()
    assert registry.execute("/mode", ctx, ["act"]) is True
    assert ctx.state.mode == "act"


def test_cmd_mode_invalid_value(tmp_path):
    """/mode with invalid argument prints usage."""
    ctx = _make_context(tmp_path)
    registry = get_registry()
    assert registry.execute("/mode", ctx, ["fly"]) is True
    assert "act|plan" in ctx.console.export_text()


# ---------------------------------------------------------------------------
# /tag command — dialog path
# ---------------------------------------------------------------------------


def test_cmd_tag_no_args_with_dialog(monkeypatch, tmp_path):
    """/tag without args opens text-input dialog and tags session."""
    ctx = _make_context(tmp_path)
    monkeypatch.setattr(cli_commands, "DialogFactory", _AlwaysConfirmDialogs)
    registry = get_registry()
    assert registry.execute("/tag", ctx, []) is True
    assert ctx.store.resolve_tag("my-tag") == ctx.state.session_id


def test_cmd_tag_no_args_dialog_cancelled(monkeypatch, tmp_path):
    """/tag without args respects cancel from dialog."""
    ctx = _make_context(tmp_path)
    monkeypatch.setattr(cli_commands, "DialogFactory", _CancelDialogs)
    registry = get_registry()
    assert registry.execute("/tag", ctx, []) is True
    assert "Tag cancelled" in ctx.console.export_text()


def test_cmd_tag_no_args_no_prompt_func(tmp_path):
    """/tag without args and no prompt_func prints usage."""
    ctx = _make_context(tmp_path)
    ctx.prompt_func = None
    registry = get_registry()
    assert registry.execute("/tag", ctx, []) is True
    assert "Usage" in ctx.console.export_text()


def test_cmd_tag_empty_name_from_dialog(monkeypatch, tmp_path):
    """/tag rejects empty tag name from dialog."""

    class _EmptyDialogs:
        def __init__(self, *args, **kwargs):
            pass

        def prompt_text(self, *args, **kwargs):
            return "   "

    ctx = _make_context(tmp_path)
    monkeypatch.setattr(cli_commands, "DialogFactory", _EmptyDialogs)
    registry = get_registry()
    assert registry.execute("/tag", ctx, []) is True
    assert "empty" in ctx.console.export_text().lower()


# ---------------------------------------------------------------------------
# /fork command — --at, --compact, dialog
# ---------------------------------------------------------------------------


def test_cmd_fork_with_at_flag(tmp_path):
    """/fork --at TS forks at a specific timestamp."""
    ctx = _make_context(tmp_path)
    original = ctx.state.session_id
    registry = get_registry()
    assert registry.execute("/fork", ctx, ["--at", "2024-01-01T00:00:00"]) is True
    assert ctx.state.session_id != original


def test_cmd_fork_with_compact_flag(monkeypatch, tmp_path):
    """/fork --compact calls compact_session on the forked session."""
    ctx = _make_context(tmp_path)

    async def _fake_compact(session_id, mode="partial"):
        pass

    monkeypatch.setattr(ctx.store, "compact_session", _fake_compact)
    registry = get_registry()
    assert registry.execute("/fork", ctx, ["--compact"]) is True
    assert "Compacted" in ctx.console.export_text()


def test_cmd_fork_with_compact_failure(monkeypatch, tmp_path):
    """/fork --compact warns when compaction raises."""
    ctx = _make_context(tmp_path)

    async def _fail_compact(session_id, mode="partial"):
        raise RuntimeError("mock compact error")

    monkeypatch.setattr(ctx.store, "compact_session", _fail_compact)
    registry = get_registry()
    assert registry.execute("/fork", ctx, ["--compact"]) is True
    assert "skipped" in ctx.console.export_text().lower()


def test_cmd_fork_dialog_tag(monkeypatch, tmp_path):
    """/fork without args opens dialog for optional tag."""
    ctx = _make_context(tmp_path)
    monkeypatch.setattr(cli_commands, "DialogFactory", _AlwaysConfirmDialogs)
    original = ctx.state.session_id
    registry = get_registry()
    assert registry.execute("/fork", ctx, []) is True
    assert ctx.state.session_id != original


def test_cmd_fork_dialog_cancelled(monkeypatch, tmp_path):
    """/fork without args dialog cancel keeps session unchanged."""
    ctx = _make_context(tmp_path)
    monkeypatch.setattr(cli_commands, "DialogFactory", _CancelDialogs)
    registry = get_registry()
    assert registry.execute("/fork", ctx, []) is True
    assert "cancelled" in ctx.console.export_text().lower()


def test_cmd_fork_positional_tag(tmp_path):
    """/fork TAG forks and tags the new session."""
    ctx = _make_context(tmp_path)
    original = ctx.state.session_id
    registry = get_registry()
    assert registry.execute("/fork", ctx, ["mytag"]) is True
    assert ctx.state.session_id != original
    assert ctx.store.resolve_tag("mytag") == ctx.state.session_id


# ---------------------------------------------------------------------------
# /edit command
# ---------------------------------------------------------------------------


def test_cmd_edit_with_text_arg(monkeypatch, tmp_path):
    """/edit TEXT re-runs with the replacement text."""
    ctx = _make_context(tmp_path)
    registry = get_registry()
    captured: list[str] = []

    def fake_resolve(session_id, action, replacement_text=None):
        captured.append(replacement_text or "")
        return "replacement query"

    def fake_run_sync(*args, **kwargs):
        return _DummyQueue(result="done")

    monkeypatch.setattr(ctx.runtime, "resolve_recovery_query", fake_resolve)
    monkeypatch.setattr(ctx.runtime, "run_sync", fake_run_sync)
    assert registry.execute("/edit", ctx, ["new", "message"]) is True
    assert captured[0] == "new message"
    assert "Response" in ctx.console.export_text()


def test_cmd_edit_no_arg_no_prompt(tmp_path):
    """/edit with no text and no prompt_func shows warning."""
    ctx = _make_context(tmp_path)
    ctx.prompt_func = None
    registry = get_registry()
    assert registry.execute("/edit", ctx, []) is True
    assert "No replacement" in ctx.console.export_text()


def test_cmd_edit_dialog_cancel(monkeypatch, tmp_path):
    """/edit dialog cancel prints cancelled."""
    ctx = _make_context(tmp_path)
    monkeypatch.setattr(cli_commands, "DialogFactory", _CancelDialogs)
    registry = get_registry()
    assert registry.execute("/edit", ctx, []) is True
    assert "Edit cancelled" in ctx.console.export_text()


def test_cmd_edit_resolve_raises(monkeypatch, tmp_path):
    """/edit handles ValueError from resolve_recovery_query."""
    ctx = _make_context(tmp_path)
    registry = get_registry()

    def _bad_resolve(session_id, action, replacement_text=None):
        raise ValueError("no history")

    monkeypatch.setattr(ctx.runtime, "resolve_recovery_query", _bad_resolve)
    assert registry.execute("/edit", ctx, ["new text"]) is True
    assert "Cannot edit" in ctx.console.export_text()


def test_cmd_edit_run_sync_error(monkeypatch, tmp_path):
    """/edit surfaces last_error when run_sync fails."""
    ctx = _make_context(tmp_path)
    registry = get_registry()

    def fake_resolve(session_id, action, replacement_text=None):
        return "the query"

    def fake_run_sync(*args, **kwargs):
        return _DummyQueue(error="LLM refused")

    monkeypatch.setattr(ctx.runtime, "resolve_recovery_query", fake_resolve)
    monkeypatch.setattr(ctx.runtime, "run_sync", fake_run_sync)
    assert registry.execute("/edit", ctx, ["something"]) is True
    assert "Edit failed: LLM refused" in ctx.console.export_text()


# ---------------------------------------------------------------------------
# /retry and /continue commands
# ---------------------------------------------------------------------------


def test_cmd_retry_success(monkeypatch, tmp_path):
    """/retry re-runs the last query and prints response."""
    ctx = _make_context(tmp_path)
    registry = get_registry()

    monkeypatch.setattr(ctx.runtime, "resolve_recovery_query", lambda *a, **kw: "re-query")
    monkeypatch.setattr(ctx.runtime, "run_sync", lambda *a, **kw: _DummyQueue(result="ok"))
    assert registry.execute("/retry", ctx, []) is True
    output = ctx.console.export_text()
    assert "Retrying" in output
    assert "Response" in output


def test_cmd_continue_success(monkeypatch, tmp_path):
    """/continue runs recovery and prints response."""
    ctx = _make_context(tmp_path)
    registry = get_registry()

    monkeypatch.setattr(ctx.runtime, "resolve_recovery_query", lambda *a, **kw: "recover-query")
    monkeypatch.setattr(ctx.runtime, "run_sync", lambda *a, **kw: _DummyQueue(result="recovered"))
    assert registry.execute("/continue", ctx, []) is True
    output = ctx.console.export_text()
    assert "Continuing" in output
    assert "Response" in output


def test_cmd_continue_reinjects_capability_context(monkeypatch, tmp_path):
    """F1: /continue re-injects capability-gating context before run_sync so a
    recovered wiki/QA/structured CLI session keeps its capability.
    """
    ctx = _make_context(tmp_path)
    registry = get_registry()
    sid = ctx.state.session_id
    ctx.runtime.append_context_event(sid, {"client_capabilities": ["wiki"]})
    ctx.runtime.append_context_event(sid, {"model": "gpt-4o"})  # later gating-less event

    monkeypatch.setattr(ctx.runtime, "resolve_recovery_query", lambda *a, **kw: "recover")
    observed = {}

    def fake_run_sync(*a, **kw):
        events = ctx.store.load_transcript(sid)
        last_ctx = next((e for e in reversed(events) if e.get("type") == "context"), None)
        observed["caps"] = last_ctx["payload"].get("client_capabilities") if last_ctx else None
        return _DummyQueue(result="ok")

    monkeypatch.setattr(ctx.runtime, "run_sync", fake_run_sync)
    assert registry.execute("/continue", ctx, []) is True
    assert observed["caps"] == ["wiki"]


def test_cmd_retry_resolve_raises(monkeypatch, tmp_path):
    """/retry handles ValueError from resolve_recovery_query."""
    ctx = _make_context(tmp_path)
    registry = get_registry()

    monkeypatch.setattr(
        ctx.runtime,
        "resolve_recovery_query",
        lambda *a, **kw: (_ for _ in ()).throw(ValueError("no last query")),
    )
    assert registry.execute("/retry", ctx, []) is True
    assert "Cannot retry" in ctx.console.export_text()


def test_cmd_retry_run_sync_error(monkeypatch, tmp_path):
    """/retry surfaces last_error when run_sync fails."""
    ctx = _make_context(tmp_path)
    registry = get_registry()

    monkeypatch.setattr(ctx.runtime, "resolve_recovery_query", lambda *a, **kw: "re-query")
    monkeypatch.setattr(ctx.runtime, "run_sync", lambda *a, **kw: _DummyQueue(error="boom"))
    assert registry.execute("/retry", ctx, []) is True
    assert "Retry failed: boom" in ctx.console.export_text()


# ---------------------------------------------------------------------------
# /automatic command — branches
# ---------------------------------------------------------------------------


def test_cmd_automatic_off(tmp_path):
    """/automatic off disables auto-approval."""
    ctx = _make_context(tmp_path)
    ctx.state.auto_approve_all = True
    registry = get_registry()
    assert registry.execute("/automatic", ctx, ["off"]) is True
    assert ctx.state.auto_approve_all is False


def test_cmd_automatic_disable_alias(tmp_path):
    """/automatic disable disables auto-approval."""
    ctx = _make_context(tmp_path)
    ctx.state.auto_approve_all = True
    registry = get_registry()
    assert registry.execute("/automatic", ctx, ["disable"]) is True
    assert ctx.state.auto_approve_all is False


def test_cmd_automatic_no_prompt_no_force(tmp_path):
    """/automatic without --yes and no prompt_func asks for --yes."""
    ctx = _make_context(tmp_path)
    ctx.prompt_func = None
    registry = get_registry()
    assert registry.execute("/automatic", ctx, []) is True
    assert "--yes" in ctx.console.export_text()


def test_cmd_automatic_dialog_cancel(monkeypatch, tmp_path):
    """/automatic dialog cancel leaves approval unchanged."""
    ctx = _make_context(tmp_path)
    monkeypatch.setattr(cli_commands, "DialogFactory", _CancelDialogs)
    registry = get_registry()
    assert registry.execute("/automatic", ctx, []) is True
    assert ctx.state.auto_approve_all is False
    assert "unchanged" in ctx.console.export_text()


# ---------------------------------------------------------------------------
# /skills command
# ---------------------------------------------------------------------------


def test_cmd_skills_no_skills(monkeypatch, tmp_path):
    """/skills shows message when no skills are found."""
    ctx = _make_context(tmp_path)
    registry = get_registry()

    class _EmptyRegistry:
        def load(self):
            pass

        def load_plugin_components(self, *a, **kw):
            pass

        def list_all(self):
            return []

        def get(self, name):
            return None

    monkeypatch.setattr("mewbo_core.skills.SkillRegistry", _EmptyRegistry)
    monkeypatch.setattr("mewbo_core.plugins.load_all_plugin_components", lambda: MagicMock())
    assert registry.execute("/skills", ctx, []) is True
    assert "No skills" in ctx.console.export_text()


def test_cmd_skills_unknown_skill(monkeypatch, tmp_path):
    """/skills name shows error for unknown skill."""
    ctx = _make_context(tmp_path)
    registry = get_registry()

    class _SkillRegWithItems:
        def load(self):
            pass

        def load_plugin_components(self, *a, **kw):
            pass

        def list_all(self):
            m = MagicMock()
            m.name = "fake"
            m.description = "x"
            m.source = "local"
            return [m]

        def get(self, name):
            return None

    monkeypatch.setattr("mewbo_core.skills.SkillRegistry", _SkillRegWithItems)
    monkeypatch.setattr("mewbo_core.plugins.load_all_plugin_components", lambda: MagicMock())
    assert registry.execute("/skills", ctx, ["nonexistent"]) is True
    assert "Unknown skill" in ctx.console.export_text()


def test_cmd_skills_detail_view(monkeypatch, tmp_path):
    """/skills <name> shows skill detail panel."""
    ctx = _make_context(tmp_path)
    registry = get_registry()

    skill = MagicMock()
    skill.name = "test-skill"
    skill.description = "A test skill"
    skill.source = "local"
    skill.allowed_tools = ["bash"]
    skill.context = None
    skill.disable_model_invocation = False
    skill.user_invocable = True

    class _SkillReg:
        def load(self):
            pass

        def load_plugin_components(self, *a, **kw):
            pass

        def list_all(self):
            return [skill]

        def get(self, name):
            return skill if name == "test-skill" else None

    monkeypatch.setattr("mewbo_core.skills.SkillRegistry", _SkillReg)
    monkeypatch.setattr("mewbo_core.plugins.load_all_plugin_components", lambda: MagicMock())
    assert registry.execute("/skills", ctx, ["test-skill"]) is True
    output = ctx.console.export_text()
    assert "test-skill" in output
    assert "A test skill" in output


def test_cmd_skills_list_view(monkeypatch, tmp_path):
    """/skills list view shows all skills."""
    ctx = _make_context(tmp_path)
    registry = get_registry()

    skill1 = MagicMock()
    skill1.name = "alpha"
    skill1.description = "Alpha skill"
    skill1.source = "local"
    skill1.context = None
    skill1.user_invocable = True
    skill1.disable_model_invocation = False

    skill2 = MagicMock()
    skill2.name = "beta"
    skill2.description = "Beta skill"
    skill2.source = "plugin"
    skill2.context = "fork"
    skill2.user_invocable = False
    skill2.disable_model_invocation = True

    class _SkillReg:
        def load(self):
            pass

        def load_plugin_components(self, *a, **kw):
            pass

        def list_all(self):
            return [skill1, skill2]

        def get(self, name):
            return None

    monkeypatch.setattr("mewbo_core.skills.SkillRegistry", _SkillReg)
    monkeypatch.setattr("mewbo_core.plugins.load_all_plugin_components", lambda: MagicMock())
    assert registry.execute("/skills", ctx, []) is True
    output = ctx.console.export_text()
    assert "alpha" in output
    assert "beta" in output
    assert "fork" in output
    assert "LLM only" in output
    assert "manual only" in output


# ---------------------------------------------------------------------------
# /plugins command
# ---------------------------------------------------------------------------


def test_cmd_plugins_no_plugins(monkeypatch, tmp_path):
    """/plugins shows no-plugins message when nothing installed."""
    ctx = _make_context(tmp_path)
    registry = get_registry()

    monkeypatch.setattr("mewbo_core.plugins.discover_installed_plugins", lambda **kw: [])
    monkeypatch.setattr("mewbo_core.config.get_config", MagicMock(return_value=MagicMock()))
    assert registry.execute("/plugins", ctx, []) is True
    assert "No plugins" in ctx.console.export_text()


def test_cmd_plugins_list(monkeypatch, tmp_path):
    """/plugins lists installed plugins with details."""
    ctx = _make_context(tmp_path)
    registry = get_registry()

    pc = MagicMock()
    pc.manifest = MagicMock()
    pc.manifest.name = "myplugin"
    pc.manifest.version = "1.0.0"
    pc.manifest.marketplace = "local"
    pc.skill_dirs = ["skills/"]
    pc.agent_files = []
    pc.command_files = []
    pc.mcp_config = {}
    pc.hooks_config = None

    monkeypatch.setattr("mewbo_core.plugins.discover_installed_plugins", lambda **kw: [pc])
    cfg_mock = MagicMock()
    cfg_mock.plugins = MagicMock()
    cfg_mock.plugins.resolve_registry_paths.return_value = []
    monkeypatch.setattr("mewbo_core.config.get_config", lambda: cfg_mock)
    assert registry.execute("/plugins", ctx, []) is True
    assert "myplugin" in ctx.console.export_text()


def test_cmd_plugins_marketplace_empty(monkeypatch, tmp_path):
    """/plugins marketplace shows no-plugins message when empty."""
    ctx = _make_context(tmp_path)
    registry = get_registry()

    monkeypatch.setattr("mewbo_core.plugins.discover_marketplace_plugins", lambda **kw: [])
    cfg_mock = MagicMock()
    cfg_mock.plugins = MagicMock()
    monkeypatch.setattr("mewbo_core.config.get_config", lambda: cfg_mock)
    assert registry.execute("/plugins", ctx, ["marketplace"]) is True
    assert "No marketplace" in ctx.console.export_text()


def test_cmd_plugins_marketplace_listing(monkeypatch, tmp_path):
    """/plugins marketplace lists available plugins."""
    ctx = _make_context(tmp_path)
    registry = get_registry()

    available = [
        {"name": "plugin-a", "description": "Plugin A", "category": "tools", "marketplace": "hub"},
    ]
    monkeypatch.setattr("mewbo_core.plugins.discover_marketplace_plugins", lambda **kw: available)
    cfg_mock = MagicMock()
    cfg_mock.plugins = MagicMock()
    monkeypatch.setattr("mewbo_core.config.get_config", lambda: cfg_mock)
    assert registry.execute("/plugins", ctx, ["marketplace"]) is True
    assert "plugin-a" in ctx.console.export_text()


def test_cmd_plugins_install_no_name(monkeypatch, tmp_path):
    """/plugins install without name shows usage."""
    ctx = _make_context(tmp_path)
    registry = get_registry()
    cfg_mock = MagicMock()
    cfg_mock.plugins = MagicMock()
    monkeypatch.setattr("mewbo_core.config.get_config", lambda: cfg_mock)
    assert registry.execute("/plugins", ctx, ["install"]) is True
    assert "Usage" in ctx.console.export_text()


def test_cmd_plugins_install_not_found(monkeypatch, tmp_path):
    """/plugins install missing plugin shows error."""
    ctx = _make_context(tmp_path)
    registry = get_registry()
    monkeypatch.setattr("mewbo_core.plugins.discover_marketplace_plugins", lambda **kw: [])
    cfg_mock = MagicMock()
    cfg_mock.plugins = MagicMock()
    monkeypatch.setattr("mewbo_core.config.get_config", lambda: cfg_mock)
    assert registry.execute("/plugins", ctx, ["install", "no-such"]) is True
    assert "not found" in ctx.console.export_text()


def test_cmd_plugins_install_success(monkeypatch, tmp_path):
    """/plugins install installs from marketplace and confirms."""
    ctx = _make_context(tmp_path)
    registry = get_registry()

    available = [{"name": "plugin-a", "marketplace": "hub"}]
    installed_manifest = MagicMock()
    installed_manifest.name = "plugin-a"
    installed_manifest.version = "1.0"

    monkeypatch.setattr("mewbo_core.plugins.discover_marketplace_plugins", lambda **kw: available)
    monkeypatch.setattr("mewbo_core.plugins.install_plugin", lambda *a, **kw: installed_manifest)
    cfg_mock = MagicMock()
    cfg_mock.plugins = MagicMock()
    monkeypatch.setattr("mewbo_core.config.get_config", lambda: cfg_mock)
    assert registry.execute("/plugins", ctx, ["install", "plugin-a"]) is True
    assert "Installed" in ctx.console.export_text()


def test_cmd_plugins_install_error(monkeypatch, tmp_path):
    """/plugins install shows error when install_plugin raises."""
    ctx = _make_context(tmp_path)
    registry = get_registry()

    available = [{"name": "plugin-a", "marketplace": "hub"}]
    monkeypatch.setattr("mewbo_core.plugins.discover_marketplace_plugins", lambda **kw: available)

    def _fail(*a, **kw):
        raise RuntimeError("fail")

    monkeypatch.setattr("mewbo_core.plugins.install_plugin", _fail)
    cfg_mock = MagicMock()
    cfg_mock.plugins = MagicMock()
    monkeypatch.setattr("mewbo_core.config.get_config", lambda: cfg_mock)
    assert registry.execute("/plugins", ctx, ["install", "plugin-a"]) is True
    assert "Install failed" in ctx.console.export_text()


def test_cmd_plugins_uninstall_no_name(monkeypatch, tmp_path):
    """/plugins uninstall without name shows usage."""
    ctx = _make_context(tmp_path)
    registry = get_registry()
    cfg_mock = MagicMock()
    cfg_mock.plugins = MagicMock()
    monkeypatch.setattr("mewbo_core.config.get_config", lambda: cfg_mock)
    assert registry.execute("/plugins", ctx, ["uninstall"]) is True
    assert "Usage" in ctx.console.export_text()


def test_cmd_plugins_uninstall_success(monkeypatch, tmp_path):
    """/plugins uninstall removes plugin and confirms."""
    ctx = _make_context(tmp_path)
    registry = get_registry()
    monkeypatch.setattr("mewbo_core.plugins.uninstall_plugin", lambda *a, **kw: True)
    cfg_mock = MagicMock()
    cfg_mock.plugins = MagicMock()
    monkeypatch.setattr("mewbo_core.config.get_config", lambda: cfg_mock)
    assert registry.execute("/plugins", ctx, ["uninstall", "plugin-a"]) is True
    assert "Uninstalled" in ctx.console.export_text()


def test_cmd_plugins_uninstall_not_found(monkeypatch, tmp_path):
    """/plugins uninstall shows error when plugin not found."""
    ctx = _make_context(tmp_path)
    registry = get_registry()
    monkeypatch.setattr("mewbo_core.plugins.uninstall_plugin", lambda *a, **kw: False)
    cfg_mock = MagicMock()
    cfg_mock.plugins = MagicMock()
    monkeypatch.setattr("mewbo_core.config.get_config", lambda: cfg_mock)
    assert registry.execute("/plugins", ctx, ["uninstall", "missing"]) is True
    assert "not found" in ctx.console.export_text()


# ---------------------------------------------------------------------------
# /mcp — select path and init
# ---------------------------------------------------------------------------


def test_cmd_mcp_select_no_mcp_tools(tmp_path):
    """/mcp with no MCP specs registered renders 'No MCP tools configured'."""
    ctx = _make_context(tmp_path)
    registry = get_registry()
    # No MCP ToolSpec registered in _make_context — tool_registry has only 'dummy_tool'
    assert registry.execute("/mcp", ctx, []) is True
    assert "No MCP tools configured" in ctx.console.export_text()


def test_cmd_mcp_select_with_mcp_tool(tmp_path):
    """/mcp with a registered MCP spec renders the tool in the output."""
    ctx = _make_context(tmp_path)
    ctx.tool_registry.register(
        ToolSpec(
            tool_id="mcp_test_tool",
            name="Test MCP Tool",
            description="A test MCP tool",
            factory=lambda: None,
            kind="mcp",
            metadata={"server": "test_server", "tool": "test_tool"},
        )
    )
    registry = get_registry()
    assert registry.execute("/mcp", ctx, []) is True
    output = ctx.console.export_text()
    assert "mcp_test_tool" in output


def test_cmd_mcp_init_already_exists(tmp_path):
    """/mcp init does not overwrite existing config without --force."""
    ctx = _make_context(tmp_path)
    config_path = tmp_path / "mcp.json"
    config_path.write_text('{"servers":{}}')
    set_mcp_config_path(config_path)
    registry = get_registry()
    assert registry.execute("/mcp", ctx, ["init"]) is True
    assert "already exists" in ctx.console.export_text()


def test_cmd_mcp_init_force_overwrites(tmp_path):
    """/mcp init --force overwrites existing config."""
    ctx = _make_context(tmp_path)
    config_path = tmp_path / "mcp.json"
    config_path.write_text('{"old": true}')
    set_mcp_config_path(config_path)
    registry = get_registry()
    assert registry.execute("/mcp", ctx, ["init", "--force"]) is True
    assert "servers" in json.loads(config_path.read_text())


def test_cmd_config_no_sub(tmp_path):
    """/config without subcommand prints usage."""
    ctx = _make_context(tmp_path)
    registry = get_registry()
    assert registry.execute("/config", ctx, []) is True
    assert "Usage" in ctx.console.export_text()


def test_cmd_init_all(monkeypatch, tmp_path):
    """/init runs both config and mcp init."""
    ctx = _make_context(tmp_path)
    registry = get_registry()
    calls: list[str] = []

    def fake_config_init(context, args):
        calls.append("config")
        return True

    def fake_mcp_init(context, args):
        calls.append("mcp")
        return True

    monkeypatch.setattr(cli_commands, "_cmd_config_init", fake_config_init)
    monkeypatch.setattr(cli_commands, "_cmd_mcp_init", fake_mcp_init)
    assert registry.execute("/init", ctx, []) is True
    assert "config" in calls
    assert "mcp" in calls


# ---------------------------------------------------------------------------
# _maybe_select_mcp_specs
# ---------------------------------------------------------------------------


def test_maybe_select_mcp_specs_no_prompt_func(tmp_path):
    """Return None when no prompt_func is available."""
    ctx = _make_context(tmp_path)
    ctx.prompt_func = None
    from mewbo_cli.cli_commands import _maybe_select_mcp_specs

    result = _maybe_select_mcp_specs(ctx, [])
    assert result is None


def test_maybe_select_mcp_specs_select_all(monkeypatch, tmp_path):
    """Return None when user selects 'All MCP tools'."""
    ctx = _make_context(tmp_path)

    class _AllDialogs:
        def __init__(self, *args, **kwargs):
            pass

        def select_one(self, title, options, **kw):
            return "All MCP tools"

    monkeypatch.setattr(cli_commands, "DialogFactory", _AllDialogs)
    from mewbo_cli.cli_commands import _maybe_select_mcp_specs

    result = _maybe_select_mcp_specs(ctx, [])
    assert result is None


def test_maybe_select_mcp_specs_filter_one(monkeypatch, tmp_path):
    """Return filtered list matching selected tool_id."""
    ctx = _make_context(tmp_path)

    spec_a = ToolSpec(tool_id="mcp_a", name="A", description="A", factory=lambda: None, kind="mcp")
    spec_b = ToolSpec(tool_id="mcp_b", name="B", description="B", factory=lambda: None, kind="mcp")

    class _PickA:
        def __init__(self, *args, **kwargs):
            pass

        def select_one(self, title, options, **kw):
            return "mcp_a"

    monkeypatch.setattr(cli_commands, "DialogFactory", _PickA)
    from mewbo_cli.cli_commands import _maybe_select_mcp_specs

    result = _maybe_select_mcp_specs(ctx, [spec_a, spec_b])
    assert result == [spec_a]


# ---------------------------------------------------------------------------
# _fetch_models — error paths
# ---------------------------------------------------------------------------


def test_fetch_models_http_error(monkeypatch):
    """Raise RuntimeError on HTTP error response."""
    from urllib.error import HTTPError

    set_config_override({"llm": {"api_base": "http://example.com/v1", "api_key": "k"}})

    def _bad_urlopen(*args, **kwargs):
        raise HTTPError("url", 403, "Forbidden", {}, None)

    monkeypatch.setattr(cli_commands, "urlopen", _bad_urlopen)
    with pytest.raises(RuntimeError, match="HTTP 403"):
        cli_commands._fetch_models()


def test_fetch_models_url_error(monkeypatch):
    """Raise RuntimeError on URL/connection error."""
    from urllib.error import URLError

    set_config_override({"llm": {"api_base": "http://example.com/v1", "api_key": "k"}})

    def _bad_urlopen(*args, **kwargs):
        raise URLError("connection refused")

    monkeypatch.setattr(cli_commands, "urlopen", _bad_urlopen)
    with pytest.raises(RuntimeError, match="connection refused"):
        cli_commands._fetch_models()


def test_fetch_models_missing_api_key():
    """Raise when api_key is absent."""
    set_config_override({"llm": {"api_base": "http://example.com/v1", "api_key": ""}}, replace=True)
    with pytest.raises(RuntimeError, match="api_key"):
        cli_commands._fetch_models()


def test_fetch_models_v1_suffix(monkeypatch):
    """Append /models correctly when base URL already ends with /v1."""
    set_config_override({"llm": {"api_base": "http://example.com/v1", "api_key": "key"}})
    called_urls: list[str] = []

    class DummyResponse:
        def read(self):
            return b'{"data": [{"id": "m1"}]}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        called_urls.append(req.full_url)
        return DummyResponse()

    monkeypatch.setattr(cli_commands, "urlopen", _fake_urlopen)
    models = cli_commands._fetch_models()
    assert called_urls[0].endswith("/v1/models")
    assert models == ["m1"]


# ---------------------------------------------------------------------------
# _handle_model_wizard — all branches
# ---------------------------------------------------------------------------


def test_handle_model_wizard_no_models(monkeypatch, tmp_path):
    """Print message when no models returned."""
    ctx = _make_context(tmp_path)
    monkeypatch.setattr(cli_commands, "_fetch_models", lambda: [])
    _handle_model_wizard(ctx.console, ctx, lambda _: "1")
    assert "No models" in ctx.console.export_text()


def test_handle_model_wizard_fetch_error(monkeypatch, tmp_path):
    """Print error message on fetch failure."""
    ctx = _make_context(tmp_path)

    def _bad_fetch():
        raise RuntimeError("api down")

    monkeypatch.setattr(cli_commands, "_fetch_models", _bad_fetch)
    _handle_model_wizard(ctx.console, ctx, lambda _: "1")
    assert "Model lookup failed" in ctx.console.export_text()


def test_handle_model_wizard_quit(monkeypatch, tmp_path):
    """Cancel wizard on q/quit input."""
    ctx = _make_context(tmp_path)
    monkeypatch.setattr(cli_commands, "_fetch_models", lambda: ["model-a"])
    _handle_model_wizard(ctx.console, ctx, lambda _: "q")
    assert ctx.state.model_name is None
    assert "cancelled" in ctx.console.export_text().lower()


def test_handle_model_wizard_id_selection(monkeypatch, tmp_path):
    """Select model by exact ID string."""
    ctx = _make_context(tmp_path)
    monkeypatch.setattr(cli_commands, "_fetch_models", lambda: ["model-a", "model-b"])
    _handle_model_wizard(ctx.console, ctx, lambda _: "model-b")
    assert ctx.state.model_name == "model-b"


def test_handle_model_wizard_unrecognized(monkeypatch, tmp_path):
    """Print error when input does not match any model."""
    ctx = _make_context(tmp_path)
    monkeypatch.setattr(cli_commands, "_fetch_models", lambda: ["model-a"])
    _handle_model_wizard(ctx.console, ctx, lambda _: "unknown-model-xyz")
    assert ctx.state.model_name is None
    assert "not recognized" in ctx.console.export_text().lower()


# ---------------------------------------------------------------------------
# _render_mcp — local/disabled spec branches
# ---------------------------------------------------------------------------


def test_render_mcp_disabled_local_spec(tmp_path):
    """Show disabled local specs with disabled_reason."""
    ctx = _make_context(tmp_path)
    ctx.tool_registry.register(
        ToolSpec(
            tool_id="local_disabled",
            name="Local Disabled",
            description="x",
            factory=lambda: None,
            enabled=False,
            metadata={"disabled_reason": "init_failed"},
        )
    )
    _render_mcp(ctx.console, ctx.tool_registry)
    output = ctx.console.export_text()
    assert "local_disabled" in output
    assert "disabled" in output
    assert "init_failed" in output


def test_render_mcp_mcp_spec_server_and_tool(tmp_path):
    """Show MCP specs with server and tool metadata."""
    ctx = _make_context(tmp_path)
    ctx.tool_registry.register(
        ToolSpec(
            tool_id="mcp_srv_tool",
            name="Tool",
            description="t",
            factory=lambda: None,
            kind="mcp",
            metadata={"server": "my_server", "tool": "my_tool"},
        )
    )
    _render_mcp(ctx.console, ctx.tool_registry)
    output = ctx.console.export_text()
    assert "server:my_server" in output
    assert "tool:my_tool" in output


def test_render_mcp_disabled_mcp_spec(tmp_path):
    """Show disabled MCP specs with disabled_reason."""
    ctx = _make_context(tmp_path)
    ctx.tool_registry.register(
        ToolSpec(
            tool_id="mcp_bad",
            name="Bad",
            description="b",
            factory=lambda: None,
            kind="mcp",
            enabled=False,
            metadata={"server": "bad_srv", "tool": "t", "disabled_reason": "Unreachable"},
        )
    )
    _render_mcp(ctx.console, ctx.tool_registry)
    output = ctx.console.export_text()
    assert "mcp_bad" in output
    assert "disabled" in output
    assert "Unreachable" in output


def test_render_mcp_no_mcp_tools(tmp_path):
    """Show no-tools message when no MCP specs are present."""
    ctx = _make_context(tmp_path)
    _render_mcp(ctx.console, ctx.tool_registry)
    assert "No MCP tools configured" in ctx.console.export_text()


# ---------------------------------------------------------------------------
# /models — non-textual fallback path via _handle_model_wizard
# ---------------------------------------------------------------------------


def test_cmd_models_non_textual_fallback(monkeypatch, tmp_path):
    """/models falls through to _handle_model_wizard when Textual unavailable."""
    ctx = _make_context(tmp_path)
    registry = get_registry()
    called: list[bool] = []

    class _NoTextualDialogs:
        def __init__(self, *args, **kwargs):
            pass

        def can_use_textual(self):
            return False

    def _fake_wizard(console, context, prompt_func):
        called.append(True)

    monkeypatch.setattr(cli_commands, "DialogFactory", _NoTextualDialogs)
    monkeypatch.setattr(cli_commands, "_handle_model_wizard", _fake_wizard)
    assert registry.execute("/models", ctx, []) is True
    assert called


def test_cmd_models_textual_no_choice(monkeypatch, tmp_path):
    """/models Textual path: cancellation (no choice) prints cancel."""
    ctx = _make_context(tmp_path)
    registry = get_registry()

    class _TextualDialogs:
        def __init__(self, *args, **kwargs):
            pass

        def can_use_textual(self):
            return True

        def select_one(self, *args, **kwargs):
            return None

    monkeypatch.setattr(cli_commands, "DialogFactory", _TextualDialogs)
    monkeypatch.setattr(cli_commands, "_fetch_models", lambda: ["model-a"])
    assert registry.execute("/models", ctx, []) is True
    assert "cancelled" in ctx.console.export_text().lower()


def test_cmd_models_textual_fetch_error(monkeypatch, tmp_path):
    """/models Textual path: fetch failure prints error."""
    ctx = _make_context(tmp_path)
    registry = get_registry()

    class _TextualDialogs:
        def __init__(self, *args, **kwargs):
            pass

        def can_use_textual(self):
            return True

    def _bad_fetch():
        raise RuntimeError("down")

    monkeypatch.setattr(cli_commands, "DialogFactory", _TextualDialogs)
    monkeypatch.setattr(cli_commands, "_fetch_models", _bad_fetch)
    assert registry.execute("/models", ctx, []) is True
    assert "Model lookup failed" in ctx.console.export_text()


def test_cmd_models_textual_no_models(monkeypatch, tmp_path):
    """/models Textual path: empty model list prints no-models message."""
    ctx = _make_context(tmp_path)
    registry = get_registry()

    class _TextualDialogs:
        def __init__(self, *args, **kwargs):
            pass

        def can_use_textual(self):
            return True

    monkeypatch.setattr(cli_commands, "DialogFactory", _TextualDialogs)
    monkeypatch.setattr(cli_commands, "_fetch_models", lambda: [])
    assert registry.execute("/models", ctx, []) is True
    assert "No models" in ctx.console.export_text()


# ---------------------------------------------------------------------------
# /terminate — cancel returns True branch
# ---------------------------------------------------------------------------


def test_cmd_terminate_when_canceled(monkeypatch, tmp_path):
    """/terminate prints 'Cancellation requested' when cancel returns True."""
    ctx = _make_context(tmp_path)
    registry = get_registry()
    monkeypatch.setattr(ctx.runtime, "cancel", lambda _sid: True)
    assert registry.execute("/terminate", ctx, []) is True
    assert "Cancellation requested" in ctx.console.export_text()


# ---------------------------------------------------------------------------
# /edit — dialog returns value (strip branch)
# ---------------------------------------------------------------------------


def test_cmd_edit_dialog_returns_value(monkeypatch, tmp_path):
    """/edit dialog returns non-None value, runs replacement after stripping."""
    ctx = _make_context(tmp_path)
    registry = get_registry()

    class _TypedDialogs:
        def __init__(self, *args, **kwargs):
            pass

        def prompt_text(self, *args, **kwargs):
            return "  new message  "

    monkeypatch.setattr(cli_commands, "DialogFactory", _TypedDialogs)
    monkeypatch.setattr(ctx.runtime, "resolve_recovery_query", lambda *a, **kw: "query")
    monkeypatch.setattr(ctx.runtime, "run_sync", lambda *a, **kw: _DummyQueue(result="done"))
    assert registry.execute("/edit", ctx, []) is True
    assert "Response" in ctx.console.export_text()


# ---------------------------------------------------------------------------
# /skills — skill detail with context + disable_model_invocation + not user_invocable
# ---------------------------------------------------------------------------


def test_cmd_skills_detail_all_badges(monkeypatch, tmp_path):
    """/skills detail shows context, auto-invocation-disabled, and LLM-only badges."""
    ctx = _make_context(tmp_path)
    registry = get_registry()

    skill = MagicMock()
    skill.name = "adv-skill"
    skill.description = "Advanced"
    skill.source = "plugin"
    skill.allowed_tools = []
    skill.context = "fork"
    skill.disable_model_invocation = True
    skill.user_invocable = False

    class _SkillReg:
        def load(self):
            pass

        def load_plugin_components(self, *a, **kw):
            pass

        def list_all(self):
            return [skill]

        def get(self, name):
            return skill if name == "adv-skill" else None

    monkeypatch.setattr("mewbo_core.skills.SkillRegistry", _SkillReg)
    monkeypatch.setattr("mewbo_core.plugins.load_all_plugin_components", lambda: MagicMock())
    assert registry.execute("/skills", ctx, ["adv-skill"]) is True
    output = ctx.console.export_text()
    assert "Context: fork" in output
    assert "Auto-invocation: disabled" in output
    assert "User-invocable: no" in output


# ---------------------------------------------------------------------------
# /plugins — plugin with null manifest is skipped
# ---------------------------------------------------------------------------


def test_cmd_plugins_list_skips_null_manifest(monkeypatch, tmp_path):
    """/plugins list view skips entries with null manifest."""
    ctx = _make_context(tmp_path)
    registry = get_registry()

    pc_null = MagicMock()
    pc_null.manifest = None

    pc_ok = MagicMock()
    pc_ok.manifest = MagicMock()
    pc_ok.manifest.name = "good-plugin"
    pc_ok.manifest.version = "1.0"
    pc_ok.manifest.marketplace = "hub"
    pc_ok.skill_dirs = []
    pc_ok.agent_files = []
    pc_ok.command_files = []
    pc_ok.mcp_config = {}
    pc_ok.hooks_config = None

    monkeypatch.setattr(
        "mewbo_core.plugins.discover_installed_plugins", lambda **kw: [pc_null, pc_ok]
    )
    cfg_mock = MagicMock()
    cfg_mock.plugins = MagicMock()
    monkeypatch.setattr("mewbo_core.config.get_config", lambda: cfg_mock)
    assert registry.execute("/plugins", ctx, []) is True
    output = ctx.console.export_text()
    assert "good-plugin" in output


# ---------------------------------------------------------------------------
# _cmd_mcp_init — custom .json path from args
# ---------------------------------------------------------------------------


def test_cmd_mcp_init_custom_json_path(tmp_path):
    """_cmd_mcp_init uses first arg as target path when it ends with .json."""
    custom = tmp_path / "custom.json"
    from mewbo_cli.cli_commands import _cmd_mcp_init

    ctx = _make_context(tmp_path)
    _cmd_mcp_init(ctx, [str(custom)])
    assert custom.exists()
    assert "servers" in json.loads(custom.read_text())


# ---------------------------------------------------------------------------
# _cmd_config_init — already exists without --force
# ---------------------------------------------------------------------------


def test_cmd_config_init_already_exists(monkeypatch, tmp_path):
    """_cmd_config_init reports existing file when --force is absent."""
    from mewbo_cli.cli_commands import _cmd_config_init

    existing = tmp_path / "app.example.json"
    existing.write_text("{}")

    monkeypatch.setattr("mewbo_core.config._default_example_path", lambda name: existing)
    ctx = _make_context(tmp_path)
    result = _cmd_config_init(ctx, [])
    assert result is True
    assert "already exists" in ctx.console.export_text()


# ---------------------------------------------------------------------------
# _refresh_mcp_registry — when config does not exist (triggers init)
# ---------------------------------------------------------------------------


def test_refresh_mcp_registry_creates_config_when_missing(tmp_path, monkeypatch):
    """_refresh_mcp_registry calls _cmd_mcp_init when config path is absent."""
    from mewbo_cli.cli_commands import _refresh_mcp_registry

    config_path = tmp_path / "mcp.json"
    set_mcp_config_path(config_path)  # does not exist yet

    ctx = _make_context(tmp_path)
    # load_registry just returns an empty one
    monkeypatch.setattr(cli_commands, "load_registry", lambda: ToolRegistry())
    _refresh_mcp_registry(ctx)
    # After refresh, config was created
    assert config_path.exists()


# ---------------------------------------------------------------------------
# _maybe_select_mcp_specs — refresh path
# ---------------------------------------------------------------------------


def test_maybe_select_mcp_specs_refresh(monkeypatch, tmp_path):
    """Selecting 'Refresh MCP config & manifest' calls _refresh_mcp_registry."""
    from mewbo_cli.cli_commands import _maybe_select_mcp_specs

    ctx = _make_context(tmp_path)
    refreshed: list[bool] = []

    class _RefreshDialogs:
        def __init__(self, *args, **kwargs):
            pass

        def select_one(self, title, options, **kw):
            return "Refresh MCP config & manifest"

    monkeypatch.setattr(cli_commands, "DialogFactory", _RefreshDialogs)
    monkeypatch.setattr(cli_commands, "_refresh_mcp_registry", lambda ctx: refreshed.append(True))
    _maybe_select_mcp_specs(ctx, [])
    assert refreshed


# ---------------------------------------------------------------------------
# _resolve_cli_model — falls back to config default
# ---------------------------------------------------------------------------


def test_resolve_cli_model_falls_back_to_config(monkeypatch, tmp_path):
    """_resolve_cli_model returns config default when state has no model."""
    from mewbo_cli.cli_commands import _resolve_cli_model

    ctx = _make_context(tmp_path)
    ctx.state.model_name = None
    monkeypatch.setattr(cli_commands, "get_config_value", lambda *a, **kw: "config-default-model")
    result = _resolve_cli_model(ctx)
    assert result == "config-default-model"


# ---------------------------------------------------------------------------
# _handle_model_wizard — invalid numeric index
# ---------------------------------------------------------------------------


def test_handle_model_wizard_invalid_index(monkeypatch, tmp_path):
    """_handle_model_wizard prints error for out-of-range numeric index."""
    ctx = _make_context(tmp_path)
    monkeypatch.setattr(cli_commands, "_fetch_models", lambda: ["model-a", "model-b"])
    _handle_model_wizard(ctx.console, ctx, lambda _: "99")
    assert "Invalid model index" in ctx.console.export_text()
    assert ctx.state.model_name is None
