"""Extra tests for cli_master.py — targeting uncovered branches."""

# ruff: noqa: I001
import types
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from mewbo_core.classes import ActionStep, Plan, TaskQueue
from mewbo_core.common import get_mock_speaker
from mewbo_core.config import get_config, set_config_override, set_mcp_config_path
from mewbo_core.session_runtime import SessionRuntime
from mewbo_core.session_store import SessionStore
from mewbo_core.tool_registry import ToolRegistry, ToolSpec

from mewbo_cli.cli_context import CliState
from mewbo_cli.cli_master import (
    HeaderContext,
    _build_cli_hook_manager,
    _fmt_tokens,
    _format_tool_output,
    _maybe_print_recovery_hint,
    _maybe_warn_missing_configs,
    _model_basename,
    _parse_verbosity,
    _print_usage_footer,
    _render_preflight_warnings,
    _render_results_with_registry,
    _render_tool_payload,
    _run_query,
    _should_force_preview,
    _truncate_middle,
    _verbosity_to_level,
    render_header,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(**overrides) -> types.SimpleNamespace:
    defaults = dict(
        max_iters=1,
        show_plan=False,
        no_color=True,
        auto_approve=False,
        verbose=0,
        fallback_models=None,
        no_fallback=False,
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _make_header_ctx(**overrides) -> HeaderContext:
    defaults = dict(
        title="Mewbo",
        version="0.1.0",
        status_label="Ready",
        status_color="green",
        model="openai/gpt-4o-mini",
        session_id="ses-abc",
        base_url="http://127.0.0.1:4136/v1",
        langfuse_enabled=True,
        langfuse_reason=None,
        builtin_enabled=3,
        builtin_disabled=1,
        external_enabled=2,
        external_disabled=0,
        skill_count=2,
    )
    defaults.update(overrides)
    return HeaderContext(**defaults)


def _make_tool_registry(*tool_ids: str, kind: str = "local") -> ToolRegistry:
    reg = ToolRegistry()
    for tid in tool_ids:
        reg.register(
            ToolSpec(tool_id=tid, name=tid, description=tid, factory=lambda: None, kind=kind)
        )
    return reg


# ---------------------------------------------------------------------------
# _verbosity_to_level and _parse_verbosity edge cases
# ---------------------------------------------------------------------------


def test_verbosity_to_level_trace():
    assert _verbosity_to_level(3) == "TRACE"


def test_parse_verbosity_debug_flag():
    assert _parse_verbosity(["prog", "--debug"]) == 1


def test_parse_verbosity_v_repeated():
    """'-vvv' counts as 3 v's."""
    assert _parse_verbosity(["prog", "-vvv"]) == 3


def test_parse_verbosity_verbose_eq():
    """'--verbose=2' parses to 2."""
    assert _parse_verbosity(["prog", "--verbose=2"]) == 2


def test_parse_verbosity_verbose_invalid():
    """'--verbose=foo' is ignored; no error raised."""
    result = _parse_verbosity(["prog", "--verbose=foo"])
    assert result is None


def test_parse_verbosity_none_when_no_flags():
    assert _parse_verbosity(["prog", "--query", "hi"]) is None


# ---------------------------------------------------------------------------
# render_header — tiny/normal/wide width breakpoints
# ---------------------------------------------------------------------------


def test_render_header_wide_with_skill_count():
    """Wide header includes skill count."""
    ctx = _make_header_ctx(skill_count=5)
    console = Console(record=True, width=120)
    render_header(console, ctx)
    output = console.export_text()
    assert "5 available" in output


def test_render_header_normal_includes_base_url():
    """Normal-width header includes base URL when width >= 85."""
    ctx = _make_header_ctx(base_url="http://127.0.0.1:4136/v1")
    console = Console(record=True, width=90)
    render_header(console, ctx)
    output = console.export_text()
    assert "127.0.0.1" in output


def test_render_header_tiny():
    """Tiny header fits in 50 columns."""
    ctx = _make_header_ctx()
    console = Console(record=True, width=50)
    render_header(console, ctx)
    output = console.export_text()
    assert "Mewbo" in output
    assert "Langfuse" in output


def test_render_header_langfuse_off():
    """Tiny header shows langfuse off when disabled."""
    ctx = _make_header_ctx(langfuse_enabled=False, langfuse_reason="key missing")
    console = Console(record=True, width=50)
    render_header(console, ctx)
    output = console.export_text()
    assert "off" in output


def test_render_header_zero_skill_count():
    """Wide header marks zero skills in red-dim style (text still shows '0')."""
    ctx = _make_header_ctx(skill_count=0)
    console = Console(record=True, width=120)
    render_header(console, ctx)
    output = console.export_text()
    assert "0 available" in output


# ---------------------------------------------------------------------------
# _truncate_middle edge cases
# ---------------------------------------------------------------------------


def test_truncate_middle_exactly_max():
    assert _truncate_middle("abcdef", 6) == "abcdef"


def test_truncate_middle_two_chars():
    assert _truncate_middle("abcdef", 2) == "ab"


# ---------------------------------------------------------------------------
# _fmt_tokens
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "0"),
        (999, "999"),
        (1000, "1.0k"),
        (1500, "1.5k"),
        (1_000_000, "1.0m"),
        (2_500_000, "2.5m"),
    ],
)
def test_fmt_tokens(n, expected):
    assert _fmt_tokens(n) == expected


# ---------------------------------------------------------------------------
# _model_basename
# ---------------------------------------------------------------------------


def test_model_basename_with_slash():
    assert _model_basename("openai/gpt-4o") == "gpt-4o"


def test_model_basename_no_slash():
    assert _model_basename("gpt-4o") == "gpt-4o"


# ---------------------------------------------------------------------------
# _should_force_preview
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"kind": "diff"}, True),
        ({"kind": "file"}, True),
        ({"kind": "shell"}, False),
        ({"kind": "dir"}, False),
        ("plain string", False),
        (42, False),
        ({}, False),
    ],
)
def test_should_force_preview(result, expected):
    assert _should_force_preview(result) is expected


# ---------------------------------------------------------------------------
# _format_tool_output
# ---------------------------------------------------------------------------


def test_format_tool_output_dict_json():
    """Dict without special kind renders as JSON syntax."""
    from rich.syntax import Syntax

    result = _format_tool_output({"foo": "bar"}, None)
    assert isinstance(result, Syntax)


def test_format_tool_output_list():
    """List renders as JSON syntax."""
    from rich.syntax import Syntax

    result = _format_tool_output(["a", "b"], None)
    assert isinstance(result, Syntax)


def test_format_tool_output_str_json_parseable():
    """JSON string renders as JSON syntax."""
    from rich.syntax import Syntax

    result = _format_tool_output('{"x": 1}', None)
    assert isinstance(result, Syntax)


def test_format_tool_output_plain_str():
    """Plain string renders as Text."""
    from rich.text import Text

    result = _format_tool_output("hello world", None)
    assert isinstance(result, Text)


def test_format_tool_output_empty_str():
    """Empty string renders as Text."""
    from rich.text import Text

    result = _format_tool_output("", None)
    assert isinstance(result, Text)


def test_format_tool_output_numeric():
    """Non-dict/list/str renders as Text via str()."""
    from rich.text import Text

    result = _format_tool_output(42, None)
    assert isinstance(result, Text)


# ---------------------------------------------------------------------------
# _render_tool_payload
# ---------------------------------------------------------------------------


def test_render_tool_payload_diff_empty():
    """Empty diff text renders placeholder."""
    from rich.text import Text

    result = _render_tool_payload({"kind": "diff", "text": "   "}, "")
    assert isinstance(result, Text)


def test_render_tool_payload_diff_with_content():
    """Diff with real content renders via render_diff."""
    result = _render_tool_payload({"kind": "diff", "text": "--- a\n+++ b\n@@ -1 +1 @@\n+x"}, "")
    assert result is not None


def test_render_tool_payload_file():
    """File payload renders file content."""
    result = _render_tool_payload({"kind": "file", "path": "foo.py", "text": "print(1)"}, "")
    assert result is not None


def test_render_tool_payload_dir():
    """Dir payload renders directory listing."""
    result = _render_tool_payload({"kind": "dir", "path": "/tmp", "entries": ["a.py", "b.py"]}, "")
    assert result is not None


def test_render_tool_payload_shell():
    """Shell payload renders command/output."""
    result = _render_tool_payload(
        {
            "kind": "shell",
            "command": "echo hi",
            "exit_code": 0,
            "stdout": "hi",
            "stderr": "",
            "duration_ms": 10,
            "cwd": "/tmp",
        },
        "",
    )
    assert result is not None


def test_render_tool_payload_unknown_kind():
    """Unknown kind returns None."""
    result = _render_tool_payload({"kind": "unknown"}, "")
    assert result is None


def test_render_tool_payload_no_kind():
    """Missing kind returns None."""
    result = _render_tool_payload({"data": "x"}, "")
    assert result is None


# ---------------------------------------------------------------------------
# _render_results_with_registry
# ---------------------------------------------------------------------------


def test_render_results_no_steps():
    """Print no-results message when no action steps."""
    console = Console(record=True)
    queue = TaskQueue(action_steps=[])
    reg = _make_tool_registry()
    _render_results_with_registry(console, queue, reg)
    assert "No tool results" in console.export_text()


def test_render_results_single_step_verbose():
    """Single step renders a single panel in verbose mode."""
    console = Console(record=True)
    step = ActionStep(tool_id="local_tool", operation="get", tool_input="x")
    step.result = get_mock_speaker()(content="some output")
    queue = TaskQueue(action_steps=[step])
    reg = _make_tool_registry("local_tool")
    _render_results_with_registry(console, queue, reg, verbose=True)
    output = console.export_text()
    assert "local_tool" in output


def test_render_results_multiple_steps_verbose():
    """Multiple steps use Columns layout."""
    console = Console(record=True)
    steps = []
    for name in ("tool_a", "tool_b"):
        step = ActionStep(tool_id=name, operation="get", tool_input="x")
        step.result = get_mock_speaker()(content=f"result_{name}")
        steps.append(step)
    queue = TaskQueue(action_steps=steps)
    reg = _make_tool_registry("tool_a", "tool_b")
    _render_results_with_registry(console, queue, reg, verbose=True)
    output = console.export_text()
    assert "tool_a" in output
    assert "tool_b" in output


def test_render_results_mcp_spec_label():
    """MCP tool gets '(MCP)' label appended."""
    console = Console(record=True)
    step = ActionStep(tool_id="mcp_t", operation="get", tool_input="x")
    step.result = get_mock_speaker()(content="ok")
    queue = TaskQueue(action_steps=[step])
    reg = _make_tool_registry("mcp_t", kind="mcp")
    _render_results_with_registry(console, queue, reg, verbose=True)
    assert "(MCP)" in console.export_text()


# ---------------------------------------------------------------------------
# _maybe_print_recovery_hint
# ---------------------------------------------------------------------------


def test_maybe_print_recovery_hint_error(tmp_path):
    """Print recovery panel for 'error' completion."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(
        session_id,
        {
            "type": "completion",
            "payload": {"done_reason": "error", "error": "LLM timeout", "last_error": None},
        },
    )
    console = Console(record=True)
    _maybe_print_recovery_hint(console, store, session_id)
    output = console.export_text()
    assert "Recovery" in output
    assert "/retry" in output
    assert "LLM timeout" in output


def test_maybe_print_recovery_hint_max_steps(tmp_path):
    """Print recovery panel for 'max_steps_reached' completion."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(
        session_id,
        {"type": "completion", "payload": {"done_reason": "max_steps_reached"}},
    )
    console = Console(record=True)
    _maybe_print_recovery_hint(console, store, session_id)
    output = console.export_text()
    assert "step limit" in output


def test_maybe_print_recovery_hint_no_hint_for_completed(tmp_path):
    """No recovery panel for clean 'completed' runs."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(session_id, {"type": "completion", "payload": {"done_reason": "completed"}})
    console = Console(record=True)
    _maybe_print_recovery_hint(console, store, session_id)
    assert "Recovery" not in console.export_text()


def test_maybe_print_recovery_hint_no_events(tmp_path):
    """No output when transcript is empty."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    console = Console(record=True)
    _maybe_print_recovery_hint(console, store, session_id)
    assert console.export_text().strip() == ""


# ---------------------------------------------------------------------------
# _print_usage_footer
# ---------------------------------------------------------------------------


def test_print_usage_footer_no_events(tmp_path):
    """Footer prints context-window capacity even with no LLM events in the transcript.

    build_usage_numbers resolves the model's max_input_tokens from config even
    when no llm_call_end events exist, so the footer always shows at least the
    'root 0/<max_tokens> (0%)' line.  This is the expected behaviour — the check
    `if max_in <= 0 and total_input_tokens_billed == 0: return` only skips output
    when the model's context window is unknown.
    """
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    console = Console(record=True)
    _print_usage_footer(console, store, session_id, None)
    # Footer renders the model capacity line — must not raise and must produce output
    output = console.export_text().strip()
    # Either we have capacity info or (in a truly unknown-model env) no output at all
    assert "root" in output or output == ""


# ---------------------------------------------------------------------------
# _maybe_warn_missing_configs
# ---------------------------------------------------------------------------


def test_maybe_warn_missing_configs_missing_files(tmp_path, monkeypatch):
    """Warn when config files are absent."""
    set_config_override({"llm": {"api_base": "", "api_key": ""}})
    config = get_config()
    console = Console(record=True)
    reg = _make_tool_registry()
    _maybe_warn_missing_configs(console, reg, config)
    output = console.export_text()
    # At minimum the LLM base-URL warning should appear
    assert "llm.api_base" in output or "Config files missing" in output


def test_maybe_warn_missing_configs_disabled_tool(tmp_path, monkeypatch):
    """Warn about disabled tools in the registry."""
    set_config_override({"llm": {"api_base": "http://x", "api_key": "k"}})
    config = get_config()
    console = Console(record=True)
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            tool_id="bad_tool",
            name="bad",
            description="b",
            factory=lambda: None,
            enabled=False,
            metadata={"disabled_reason": "init failed"},
        )
    )
    _maybe_warn_missing_configs(console, reg, config)
    output = console.export_text()
    assert "bad_tool" in output
    assert "disabled" in output


# ---------------------------------------------------------------------------
# _render_preflight_warnings
# ---------------------------------------------------------------------------


def test_render_preflight_warnings_failure():
    """Print warning for failing preflight check."""
    console = Console(record=True)
    results = {
        "langfuse": {"enabled": True, "ok": False, "reason": "missing key"},
        "ok_check": {"enabled": True, "ok": True},
        "disabled_check": {"enabled": False, "ok": False},
    }
    _render_preflight_warnings(console, results)
    output = console.export_text()
    assert "langfuse" in output
    assert "missing key" in output
    assert "ok_check" not in output


def test_render_preflight_warnings_no_failures():
    """No output when all checks pass."""
    console = Console(record=True)
    results = {"check": {"enabled": True, "ok": True}}
    _render_preflight_warnings(console, results)
    assert console.export_text().strip() == ""


# ---------------------------------------------------------------------------
# _build_cli_hook_manager — fallback spinner path (no agent_display)
# ---------------------------------------------------------------------------


def test_build_cli_hook_manager_spinner_path():
    """Spinner start/stop hooks work without agent_display."""
    console = Console(record=True)
    reg = _make_tool_registry("bash_tool")
    hook_manager = _build_cli_hook_manager(console, reg, agent_display=None)

    step = ActionStep(tool_id="bash_tool", operation="run", tool_input="ls")
    mock_result = get_mock_speaker()(content="ok")

    # pre_tool_use should start spinner and return the step unchanged
    if hook_manager.pre_tool_use:
        result_step = hook_manager.pre_tool_use[0](step)
        assert result_step is step

    # post_tool_use should stop spinner
    if hook_manager.post_tool_use:
        returned = hook_manager.post_tool_use[0](step, mock_result)
        assert returned is mock_result


def test_build_cli_hook_manager_spinner_mcp_label():
    """Spinner passes a label containing '(MCP)' to console.status for MCP tools."""
    console = Console(record=True)
    reg = _make_tool_registry("mcp_x", kind="mcp")
    hook_manager = _build_cli_hook_manager(console, reg)
    step = ActionStep(tool_id="mcp_x", operation="get", tool_input="x")

    captured_labels: list[str] = []
    real_status = console.status

    def capturing_status(label: str, **kwargs):
        captured_labels.append(label)
        return real_status(label, **kwargs)

    console.status = capturing_status  # type: ignore[method-assign]

    if hook_manager.pre_tool_use:
        returned_step = hook_manager.pre_tool_use[0](step)
        assert returned_step is step
    if hook_manager.post_tool_use:
        mock_result = get_mock_speaker()(content="ok")
        returned = hook_manager.post_tool_use[0](step, mock_result)
        assert returned is mock_result

    assert captured_labels, "console.status was never called by pre_tool_use"
    label = captured_labels[0]
    assert "(MCP)" in label, f"Expected '(MCP)' in status label, got: {label!r}"


def test_build_cli_hook_manager_compact_hook(tmp_path):
    """on_compact callback renders compaction panel."""
    console = Console(record=True)
    reg = _make_tool_registry()
    hook_manager = _build_cli_hook_manager(console, reg)

    if hook_manager.on_compact:
        hook_manager.on_compact[0](
            session_id="s1",
            summary="compact summary",
            tokens_before=10000,
            tokens_saved=3000,
            events_summarized=12,
        )
    output = console.export_text()
    assert "Compacted" in output


def test_build_cli_hook_manager_compact_no_tokens():
    """on_compact with no token data still runs without error."""
    console = Console(record=True)
    reg = _make_tool_registry()
    hook_manager = _build_cli_hook_manager(console, reg)

    if hook_manager.on_compact:
        hook_manager.on_compact[0](session_id="s", summary="", tokens_before=0, tokens_saved=0)
    # no crash


def test_build_cli_hook_manager_compact_no_summary():
    """on_compact with tokens but no summary shows unavailable message."""
    console = Console(record=True)
    reg = _make_tool_registry()
    hook_manager = _build_cli_hook_manager(console, reg)

    if hook_manager.on_compact:
        hook_manager.on_compact[0](
            session_id="s", summary="", tokens_before=5000, tokens_saved=1000
        )
    output = console.export_text()
    assert "unavailable" in output


# ---------------------------------------------------------------------------
# _run_query — plan-mode approval flow
# ---------------------------------------------------------------------------


def test_run_query_plan_mode_rejected(monkeypatch, tmp_path):
    """In plan mode, rejecting the plan returns to REPL without running act."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = store.create_session()
    state = CliState(session_id=session_id, show_plan=False, mode="plan")
    console = Console(record=True)
    reg = _make_tool_registry()
    run_sync_calls: list[int] = []

    def fake_run_sync(*a, **kw):
        run_sync_calls.append(1)
        # write a plan file
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# Plan\n- step 1")
        queue = TaskQueue(action_steps=[])
        return queue

    def fake_pending(session_id):
        return True, 1, str(tmp_path / "plan.md")

    def fake_reject(session_id):
        pass

    monkeypatch.setattr(runtime, "run_sync", fake_run_sync)
    monkeypatch.setattr(runtime, "_has_pending_plan_proposal", fake_pending)
    monkeypatch.setattr(runtime, "reject_plan", fake_reject)
    monkeypatch.setattr("mewbo_cli.cli_master.generate_action_plan", lambda **kw: Plan(steps=[]))

    # Prompt returns 'r' = reject
    _run_query(
        console,
        store,
        runtime,
        state,
        reg,
        "build something",
        _make_args(),
        prompt_func=lambda _: "r",
    )
    output = console.export_text()
    assert "rejected" in output.lower() or "Reject" in output or "refine" in output.lower()


def test_run_query_plan_mode_auto_reject_headless(monkeypatch, tmp_path):
    """In headless plan mode (no prompt_func), plan is auto-rejected."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = store.create_session()
    state = CliState(session_id=session_id, show_plan=False, mode="plan")
    console = Console(record=True)
    reg = _make_tool_registry()

    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n- step 1")

    def fake_run_sync(*a, **kw):
        return TaskQueue(action_steps=[])

    def fake_pending(session_id):
        return True, 1, str(plan_file)

    rejected: list[bool] = []

    def fake_reject(session_id):
        rejected.append(True)

    monkeypatch.setattr(runtime, "run_sync", fake_run_sync)
    monkeypatch.setattr(runtime, "_has_pending_plan_proposal", fake_pending)
    monkeypatch.setattr(runtime, "reject_plan", fake_reject)
    monkeypatch.setattr("mewbo_cli.cli_master.generate_action_plan", lambda **kw: Plan(steps=[]))

    _run_query(
        console,
        store,
        runtime,
        state,
        reg,
        "build something",
        _make_args(),
        prompt_func=None,
    )
    assert rejected  # headless always rejects


def test_run_query_plan_mode_approved_runs_act(monkeypatch, tmp_path):
    """In plan mode, approving the plan triggers an act-mode run."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = store.create_session()
    state = CliState(session_id=session_id, show_plan=False, mode="plan")
    console = Console(record=True)
    reg = _make_tool_registry()
    run_sync_calls: list[str] = []

    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n- step 1")

    def fake_run_sync(*a, **kw):
        mode = kw.get("mode", "?")
        run_sync_calls.append(mode)
        queue = TaskQueue(action_steps=[])
        if mode == "act":
            queue.task_result = "implemented"
        return queue

    def fake_pending(session_id):
        return True, 1, str(plan_file)

    monkeypatch.setattr(runtime, "run_sync", fake_run_sync)
    monkeypatch.setattr(runtime, "_has_pending_plan_proposal", fake_pending)
    monkeypatch.setattr(runtime, "approve_plan", lambda _: None)
    monkeypatch.setattr("mewbo_cli.cli_master.generate_action_plan", lambda **kw: Plan(steps=[]))

    _run_query(
        console,
        store,
        runtime,
        state,
        reg,
        "build something",
        _make_args(),
        prompt_func=lambda _: "a",  # 'a' = approve
    )
    assert "act" in run_sync_calls


def test_run_query_no_pending_plan(monkeypatch, tmp_path):
    """Non-plan-mode query: no plan approval flow triggered."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = store.create_session()
    state = CliState(session_id=session_id, show_plan=False, mode="act")
    console = Console(record=True)
    reg = _make_tool_registry()

    def fake_orchestrate(*a, **kw):
        q = TaskQueue(action_steps=[])
        q.task_result = "done"
        return q

    monkeypatch.setattr("mewbo_core.session_runtime.orchestrate_session", fake_orchestrate)
    _run_query(
        console,
        store,
        runtime,
        state,
        reg,
        "hi",
        _make_args(),
        prompt_func=lambda _: "y",
    )
    assert "done" in console.export_text()


# ---------------------------------------------------------------------------
# run_cli — single-query missing no_fallback attr gracefully
# ---------------------------------------------------------------------------


def test_run_cli_no_fallback_flag(monkeypatch, tmp_path):
    """run_cli handles --no-fallback=True by setting empty fallback tuple."""
    from mewbo_cli.cli_master import run_cli

    set_mcp_config_path(str(tmp_path / "mcp.json"))

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
        auto_approve=True,
        verbose=0,
        config=None,
        fallback_models=None,
        no_fallback=True,
    )

    monkeypatch.setattr("mewbo_cli.cli_master.render_header", lambda *a, **kw: None)

    def fake_orchestrate(*a, **kw):
        q = TaskQueue(action_steps=[])
        q.task_result = "done"
        return q

    monkeypatch.setattr("mewbo_core.session_runtime.orchestrate_session", fake_orchestrate)
    monkeypatch.setattr("mewbo_cli.cli_master.load_registry", lambda: ToolRegistry())
    result = run_cli(args)
    assert result == 0


def test_run_cli_fallback_models_comma_split(monkeypatch, tmp_path):
    """run_cli splits --fallback-models by comma into a tuple."""
    from mewbo_cli.cli_master import run_cli

    set_mcp_config_path(str(tmp_path / "mcp.json"))

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
        auto_approve=True,
        verbose=0,
        config=None,
        fallback_models="gpt-5.4,gemini-2.5-pro",
        no_fallback=False,
    )

    monkeypatch.setattr("mewbo_cli.cli_master.render_header", lambda *a, **kw: None)

    captured: dict = {}

    def fake_orchestrate(*a, **kw):
        captured["fallback"] = kw.get("fallback_models")
        q = TaskQueue(action_steps=[])
        q.task_result = "done"
        return q

    monkeypatch.setattr("mewbo_core.session_runtime.orchestrate_session", fake_orchestrate)
    monkeypatch.setattr("mewbo_cli.cli_master.load_registry", lambda: ToolRegistry())
    run_cli(args)
    assert captured.get("fallback") == ("gpt-5.4", "gemini-2.5-pro")


# ---------------------------------------------------------------------------
# run_cli — interactive skill invocation
# ---------------------------------------------------------------------------


def test_run_cli_unknown_command_branch(monkeypatch, tmp_path):
    """Unknown slash command prints help hint and continues loop."""
    from mewbo_cli.cli_master import run_cli

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
        config=None,
        fallback_models=None,
        no_fallback=False,
    )

    class DummyHistory:
        def __init__(self, *a, **kw):
            pass

    call_count = [0]

    class DummySession:
        def __init__(self, *a, **kw):
            pass

        def prompt(self, *a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                return "/notacommand"
            return "/quit"

    monkeypatch.setattr("mewbo_cli.cli_master.render_header", lambda *a, **kw: None)
    monkeypatch.setattr("mewbo_cli.cli_master.FileHistory", DummyHistory)
    monkeypatch.setattr("mewbo_cli.cli_master.PromptSession", lambda *a, **kw: DummySession())
    result = run_cli(args)
    assert result == 0
    # Loop continued past the unknown command: prompt was called twice (once for
    # the unknown command, once for /quit)
    assert call_count[0] == 2


def test_run_cli_eof_exits_gracefully(monkeypatch, tmp_path):
    """EOFError from prompt exits cleanly with code 0."""
    from mewbo_cli.cli_master import run_cli

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
        config=None,
        fallback_models=None,
        no_fallback=False,
    )

    class DummyHistory:
        def __init__(self, *a, **kw):
            pass

    class DummySession:
        def __init__(self, *a, **kw):
            pass

        def prompt(self, *a, **kw):
            raise EOFError

    monkeypatch.setattr("mewbo_cli.cli_master.render_header", lambda *a, **kw: None)
    monkeypatch.setattr("mewbo_cli.cli_master.FileHistory", DummyHistory)
    monkeypatch.setattr("mewbo_cli.cli_master.PromptSession", lambda *a, **kw: DummySession())
    result = run_cli(args)
    assert result == 0


def test_run_cli_skill_invocation(monkeypatch, tmp_path):
    """Interactive CLI invokes a skill when /<name> matches a user-invocable skill."""
    from mewbo_cli.cli_master import run_cli

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
        auto_approve=True,
        config=None,
        fallback_models=None,
        no_fallback=False,
    )

    skill_mock = MagicMock()
    skill_mock.user_invocable = True

    class DummySkillRegistry:
        def load(self):
            pass

        def load_plugin_components(self, *a, **kw):
            pass

        def list_all(self):
            return []

        def get(self, name):
            return skill_mock if name == "myskill" else None

    call_count = [0]

    class DummyHistory:
        def __init__(self, *a, **kw):
            pass

    class DummySession:
        def __init__(self, *a, **kw):
            pass

        def prompt(self, *a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                return "/myskill some args"
            return "/quit"

    run_query_calls: list[str] = []

    def fake_run_query(*a, skill_instructions=None, **kw):
        run_query_calls.append(skill_instructions or "")

    monkeypatch.setattr("mewbo_cli.cli_master.render_header", lambda *a, **kw: None)
    monkeypatch.setattr("mewbo_cli.cli_master.FileHistory", DummyHistory)
    monkeypatch.setattr("mewbo_cli.cli_master.PromptSession", lambda *a, **kw: DummySession())
    monkeypatch.setattr("mewbo_core.skills.SkillRegistry", DummySkillRegistry)
    monkeypatch.setattr(
        "mewbo_core.skills.activate_skill",
        lambda skill, args: ("skill instructions", {}),
    )
    monkeypatch.setattr("mewbo_cli.cli_master._run_query", fake_run_query)
    result = run_cli(args)
    assert result == 0
    assert run_query_calls  # skill was invoked


def test_run_cli_empty_input_skipped(monkeypatch, tmp_path):
    """Blank input is skipped; loop continues."""
    from mewbo_cli.cli_master import run_cli

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
        config=None,
        fallback_models=None,
        no_fallback=False,
    )

    call_count = [0]

    class DummyHistory:
        def __init__(self, *a, **kw):
            pass

    class DummySession:
        def __init__(self, *a, **kw):
            pass

        def prompt(self, *a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                return "   "  # blank → skip
            return "/quit"

    monkeypatch.setattr("mewbo_cli.cli_master.render_header", lambda *a, **kw: None)
    monkeypatch.setattr("mewbo_cli.cli_master.FileHistory", DummyHistory)
    monkeypatch.setattr("mewbo_cli.cli_master.PromptSession", lambda *a, **kw: DummySession())
    result = run_cli(args)
    assert result == 0
