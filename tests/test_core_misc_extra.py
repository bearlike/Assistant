"""Extra tests for the smaller mewbo_core modules:

  * skills.py (lines 110-163, 364-478, 516)
  * session_runtime.py (lines 63-73, 109, 157-172, 231, 269-314, 502-528, 603-674)
  * llm_resilience.py (lines 96-265-296, 316-432, 455, 502-506)
  * context.py (lines 93-96, 107-161, 185-261, 333-362)
  * tool_registry.py (lines 74-76, 110, 153-344, 488-547, 699-981)

Stub ONLY I/O: LLM via AsyncMock, subprocess, filesystem via tmp_path.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mewbo_core.context import (
    ContextBuilder,
    ContextSnapshot,
    _iter_attachments,
    _load_attachment_images,
    _load_attachment_texts,
    _read_text_capped,
    event_payload_text,
    render_event_lines,
)
from mewbo_core.llm_resilience import (
    CircuitBreaker,
    DoomLoopGuard,
    LlmResilienceExhausted,
    RetryAction,
    RetryBudget,
    RetryStrategy,
    repair_tool_pairing,
)
from mewbo_core.session_runtime import (
    RunRegistry,
    SessionRuntime,
    _filter_events,
    _parse_iso,
    parse_core_command,
)
from mewbo_core.session_store import SessionStore
from mewbo_core.skills import (
    SkillRegistry,
    SkillSpec,
    _parse_skill_file,
    _preprocess_shell,
    activate_skill,
)
from mewbo_core.tool_registry import (
    TOOL_SEARCH_TOOL_ID,
    ToolRegistry,
    ToolSpec,
    filter_specs,
    is_always_load,
    is_deferred,
)

# ---------------------------------------------------------------------------
# skills.py
# ---------------------------------------------------------------------------

# _parse_skill_file — error paths (lines 110-163)


def test_parse_skill_file_read_error(tmp_path: Path) -> None:
    """OSError on read → None."""
    missing = tmp_path / "SKILL.md"
    result = _parse_skill_file(missing, source="personal")
    assert result is None


def test_parse_skill_file_no_frontmatter(tmp_path: Path) -> None:
    """File without frontmatter → None."""
    p = tmp_path / "SKILL.md"
    p.write_text("no frontmatter here")
    result = _parse_skill_file(p, source="personal")
    assert result is None


def test_parse_skill_file_bad_yaml(tmp_path: Path) -> None:
    """Invalid YAML in frontmatter → None."""
    p = tmp_path / "SKILL.md"
    p.write_text("---\n: bad: yaml: [{\n---\nbody")
    result = _parse_skill_file(p, source="personal")
    assert result is None


def test_parse_skill_file_frontmatter_not_mapping(tmp_path: Path) -> None:
    """Frontmatter that parses to a non-dict (e.g. a list) → None."""
    p = tmp_path / "SKILL.md"
    p.write_text("---\n- item1\n- item2\n---\nbody")
    result = _parse_skill_file(p, source="personal")
    assert result is None


def test_parse_skill_file_missing_name_and_no_default(tmp_path: Path) -> None:
    """Missing 'name' with no default_name → None."""
    p = tmp_path / "SKILL.md"
    p.write_text("---\ndescription: Desc\n---\nbody")
    result = _parse_skill_file(p, source="personal", default_name=None)
    assert result is None


def test_parse_skill_file_missing_description(tmp_path: Path) -> None:
    """Missing 'description' → None."""
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: my-skill\n---\nbody")
    result = _parse_skill_file(p, source="personal")
    assert result is None


def test_parse_skill_file_invalid_name_chars(tmp_path: Path) -> None:
    """Name with uppercase chars fails regex → None."""
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: MySkill\ndescription: Test\n---\nbody")
    result = _parse_skill_file(p, source="personal")
    assert result is None


def test_parse_skill_file_allowed_tools_list(tmp_path: Path) -> None:
    """allowed-tools as a list is parsed correctly."""
    p = tmp_path / "SKILL.md"
    p.write_text(
        "---\nname: my-skill\ndescription: Test\nallowed-tools:\n  - tool-a\n  - tool-b\n---\nbody"
    )
    result = _parse_skill_file(p, source="personal")
    assert result is not None
    assert result.allowed_tools == ["tool-a", "tool-b"]


def test_parse_skill_file_allowed_tools_string(tmp_path: Path) -> None:
    """allowed-tools as a space-delimited string is split correctly."""
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: my-skill\ndescription: Test\nallowed-tools: tool-a tool-b\n---\nbody")
    result = _parse_skill_file(p, source="personal")
    assert result is not None
    assert result.allowed_tools == ["tool-a", "tool-b"]


def test_parse_skill_file_requires_capabilities_scalar(tmp_path: Path) -> None:
    """requires-capability (singular scalar key) is parsed into the tuple."""
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: my-skill\ndescription: Test\nrequires-capability: wiki\n---\nbody")
    result = _parse_skill_file(p, source="personal")
    assert result is not None
    assert "wiki" in result.requires_capabilities


def test_parse_skill_file_context_and_agent_fields(tmp_path: Path) -> None:
    """context and agent frontmatter fields are captured."""
    p = tmp_path / "SKILL.md"
    p.write_text(
        "---\nname: my-skill\ndescription: Test\ncontext: fork\nagent: sub-agent\n---\nbody"
    )
    result = _parse_skill_file(p, source="personal")
    assert result is not None
    assert result.context == "fork"
    assert result.agent == "sub-agent"


# _preprocess_shell (lines 449-480)


def test_preprocess_shell_substitutes_command_stdout(tmp_path: Path) -> None:
    """!`echo hello` is replaced with 'hello'."""
    result = _preprocess_shell("prefix: !`echo hello` suffix")
    assert result == "prefix: hello suffix"


def test_preprocess_shell_error_exit_code(tmp_path: Path) -> None:
    """A command that exits non-zero yields an [ERROR: ...] placeholder."""
    result = _preprocess_shell("!`exit 1`")
    assert "[ERROR:" in result


def test_preprocess_shell_timeout(monkeypatch) -> None:
    """A timeout is replaced with [ERROR: command timed out ...]."""
    import subprocess as sp_mod

    def _timeout(*args, **kwargs):
        raise sp_mod.TimeoutExpired(["sleep"], 30)

    monkeypatch.setattr("mewbo_core.skills.subprocess.run", _timeout)
    result = _preprocess_shell("!`sleep 1000`")
    assert "timed out" in result


def test_preprocess_shell_oserror(monkeypatch) -> None:
    """OSError from subprocess yields [ERROR: ...]."""

    def _oserror(*args, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr("mewbo_core.skills.subprocess.run", _oserror)
    result = _preprocess_shell("!`bad-command`")
    assert "[ERROR:" in result


# activate_skill (lines 488-518)


def _make_spec(allowed_tools: list[str] | None = None, body: str = "body $ARGUMENTS") -> SkillSpec:
    return SkillSpec(
        name="test-skill",
        description="Test",
        source_path="/tmp/SKILL.md",
        source="personal",
        body=body,
        allowed_tools=allowed_tools,
    )


def test_activate_skill_substitutes_arguments() -> None:
    """$ARGUMENTS is replaced with the passed args string."""
    spec = _make_spec(body="Run $ARGUMENTS now")
    result, _ = activate_skill(spec, args="foo bar")
    assert "foo bar" in result


def test_activate_skill_substitutes_positional_args() -> None:
    """$0, $1 are replaced with individual arg parts."""
    spec = _make_spec(body="first=$0 second=$1")
    result, _ = activate_skill(spec, args="alpha beta")
    assert "alpha" in result
    assert "beta" in result


def test_activate_skill_no_tool_scoping_when_no_allowed_tools() -> None:
    """When skill has no allowed_tools, scoped_specs equals the input list."""
    from mewbo_core.tool_registry import ToolSpec as TSpec

    spec = _make_spec(allowed_tools=None)
    tool = MagicMock(spec=TSpec)
    tool.tool_id = "some-tool"
    _, scoped = activate_skill(spec, tool_specs=[tool])
    assert scoped == [tool]


def test_activate_skill_scopes_tools_with_allowlist() -> None:
    """When skill has allowed_tools, only matching specs are returned."""
    from mewbo_core.tool_registry import ToolSpec as TSpec

    t1 = MagicMock(spec=TSpec)
    t1.tool_id = "allowed-tool"
    t1.metadata = {}
    t2 = MagicMock(spec=TSpec)
    t2.tool_id = "denied-tool"
    t2.metadata = {}
    spec = _make_spec(allowed_tools=["allowed-tool"])
    _, scoped = activate_skill(spec, tool_specs=[t1, t2])
    assert scoped is not None
    ids = [s.tool_id for s in scoped]
    assert "allowed-tool" in ids
    assert "denied-tool" not in ids


def test_activate_skill_none_tool_specs_returns_none() -> None:
    """When tool_specs is None and skill has no allowed_tools, returns None."""
    spec = _make_spec(allowed_tools=None)
    _, scoped = activate_skill(spec, tool_specs=None)
    assert scoped is None


# SkillRegistry.maybe_reload (lines 344-376)


def test_skill_registry_maybe_reload_detects_deleted_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a skill file is deleted, maybe_reload removes it from the registry."""
    # Isolate from real ~/.claude/skills/ by redirecting home to tmp_path
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    skill_dir = tmp_path / ".claude" / "skills" / "my-skill"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("---\nname: my-skill\ndescription: Test\n---\nbody")

    registry = SkillRegistry()
    registry.load(cwd=str(tmp_path))
    assert registry.get("my-skill") is not None

    skill_file.unlink()
    changed = registry.maybe_reload()
    assert changed is True
    assert registry.get("my-skill") is None


def test_skill_registry_maybe_reload_detects_changed_file(tmp_path: Path) -> None:
    """If a skill file's mtime changes, maybe_reload reloads it."""
    skill_dir = tmp_path / ".claude" / "skills" / "my-skill"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("---\nname: my-skill\ndescription: Original\n---\nbody")

    registry = SkillRegistry()
    registry.load(cwd=str(tmp_path))
    original_spec = registry.get("my-skill")
    assert original_spec is not None

    # Overwrite with a different description and update mtime
    time.sleep(0.01)
    skill_file.write_text("---\nname: my-skill\ndescription: Updated\n---\nnew body")

    changed = registry.maybe_reload()
    assert changed is True
    updated = registry.get("my-skill")
    assert updated is not None
    assert updated.description == "Updated"


def test_skill_registry_maybe_reload_discovers_new_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Newly added skill files are discovered on maybe_reload."""
    # Isolate from real ~/.claude/skills/ by redirecting home to tmp_path
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    registry = SkillRegistry()
    registry.load(cwd=str(tmp_path))
    assert registry.list_all() == []

    skill_dir = tmp_path / ".claude" / "skills" / "new-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: new-skill\ndescription: New\n---\nbody")

    changed = registry.maybe_reload()
    assert changed is True
    assert registry.get("new-skill") is not None


def test_skill_registry_maybe_reload_no_change_returns_false(tmp_path: Path) -> None:
    """No file changes → maybe_reload returns False."""
    skill_dir = tmp_path / ".claude" / "skills" / "stable"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: stable\ndescription: Stable\n---\nbody")

    registry = SkillRegistry()
    registry.load(cwd=str(tmp_path))

    changed = registry.maybe_reload()
    assert changed is False


# SkillRegistry.load_plugin_components


def test_skill_registry_load_plugin_components(tmp_path: Path) -> None:
    """load_plugin_components wires skill_dirs and command_files from PluginFanOut."""

    from mewbo_core.plugins import PluginComponents, PluginFanOut, PluginManifest

    skill_dir = tmp_path / "skills" / "helper"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: helper\ndescription: Help\n---\nbody")

    cmd_file = tmp_path / "commands" / "deploy.md"
    cmd_file.parent.mkdir(parents=True)
    cmd_file.write_text("---\nname: deploy\ndescription: Deploy\n---\nbody")

    manifest = PluginManifest(name="test-plugin", requires_capabilities=())
    pc = PluginComponents(manifest=manifest, skill_dirs=[str(tmp_path / "skills")])
    cmd_pc = PluginComponents(manifest=manifest, command_files=[str(cmd_file)])

    fanout = PluginFanOut(
        components=[pc, cmd_pc],
        skill_dirs=[str(tmp_path / "skills")],
        command_files=[str(cmd_file)],
        agent_files=[],
        mcp_servers={},
        hooks_configs=[],
    )

    registry = SkillRegistry()
    registry.load_plugin_components(fanout)
    assert registry.get("helper") is not None
    assert registry.get("deploy") is not None


# ---------------------------------------------------------------------------
# session_runtime.py
# ---------------------------------------------------------------------------

# _parse_iso (lines 63-67)


def test_parse_iso_none_input() -> None:
    assert _parse_iso(None) is None


def test_parse_iso_empty_string() -> None:
    assert _parse_iso("") is None


def test_parse_iso_invalid_format() -> None:
    assert _parse_iso("not-a-date") is None


def test_parse_iso_valid() -> None:

    dt = _parse_iso("2024-01-01T00:00:00+00:00")
    assert dt is not None
    assert dt.year == 2024


# parse_core_command (lines 70-75)


def test_parse_core_command_empty_string() -> None:
    assert parse_core_command("") is None


# _filter_events (lines 167-178)


def test_filter_events_no_cutoff() -> None:
    events = [{"type": "user", "ts": "2024-01-01T00:00:00+00:00"}]
    assert _filter_events(events, None) == events


def test_filter_events_invalid_cutoff() -> None:
    events = [{"type": "user", "ts": "2024-01-01T00:00:00+00:00"}]
    assert _filter_events(events, "not-a-date") == events


def test_filter_events_skips_events_with_no_ts() -> None:
    events = [
        {"type": "user"},  # No ts field
        {"type": "user", "ts": "2024-01-02T00:00:00+00:00"},
    ]
    result = _filter_events(events, "2024-01-01T00:00:00+00:00")
    assert len(result) == 1
    assert result[0]["ts"] == "2024-01-02T00:00:00+00:00"


# RunRegistry (lines 109, 157-165)


def test_run_registry_start_returns_false_when_already_running(tmp_path: Path) -> None:
    """start() returns False if a thread is already alive for the session."""
    registry = RunRegistry()
    barrier = threading.Event()
    done = threading.Event()

    def _long_run(cancel_event):
        barrier.set()
        cancel_event.wait(timeout=2.0)
        done.set()

    started = registry.start("s1", target=_long_run)
    assert started is True
    barrier.wait(timeout=1.0)
    # Second start while first is alive → False
    started2 = registry.start("s1", target=lambda e: None)
    assert started2 is False

    # Cleanup
    registry.cancel("s1")
    done.wait(timeout=2.0)


def test_run_registry_get_cancel_event_returns_none_when_no_run() -> None:
    registry = RunRegistry()
    assert registry.get_cancel_event("unknown") is None


def test_run_registry_get_handle_returns_none_when_no_run() -> None:
    registry = RunRegistry()
    assert registry.get_handle("unknown") is None


def test_run_registry_cancel_returns_false_when_no_run() -> None:
    registry = RunRegistry()
    assert registry.cancel("ghost") is False


# SessionRuntime.resolve_session — fork_from without tag (line 213-226)


def test_resolve_session_fork_from_id_directly(tmp_path: Path) -> None:
    """fork_from= with a bare session_id (no tag) still works."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    source = store.create_session()
    store.append_event(source, {"type": "user", "payload": {"text": "hello"}})

    forked = runtime.resolve_session(fork_from=source)
    assert forked != source
    events = store.load_transcript(forked)
    assert len(events) == 1


def test_resolve_session_tag_resolves_existing(tmp_path: Path) -> None:
    """resolve_session with only session_tag returns the tagged session."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    sid = runtime.resolve_session(session_tag="my-tag")
    # A second call with the same tag must return the SAME session
    sid2 = runtime.resolve_session(session_tag="my-tag")
    assert sid == sid2


def test_resolve_session_new_session_when_no_tag_and_no_id(tmp_path: Path) -> None:
    """No tag, no id → creates a fresh session."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    sid = runtime.resolve_session()
    assert sid


def test_append_context_event_noop_on_empty_dict(tmp_path: Path) -> None:
    """append_context_event is a no-op when context is empty."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    sid = runtime.resolve_session()
    runtime.append_context_event(sid, {})
    assert store.load_transcript(sid) == []


# summarize_session — status branches (lines 269-299)


def test_summarize_session_status_awaiting_approval(tmp_path: Path) -> None:
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    sid = store.create_session()
    store.append_event(sid, {"type": "user", "payload": {"text": "hi"}})
    store.append_event(
        sid,
        {"type": "completion", "payload": {"done": True, "done_reason": "awaiting_approval"}},
    )
    summary = runtime.summarize_session(sid)
    assert summary["status"] == "awaiting_approval"


def test_summarize_session_status_compact_failed(tmp_path: Path) -> None:
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    sid = store.create_session()
    store.append_event(sid, {"type": "user", "payload": {"text": "hi"}})
    store.append_event(
        sid,
        {"type": "completion", "payload": {"done": True, "done_reason": "compact_failed"}},
    )
    summary = runtime.summarize_session(sid)
    assert summary["status"] == "failed"


def test_summarize_session_status_command_failed(tmp_path: Path) -> None:
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    sid = store.create_session()
    store.append_event(sid, {"type": "user", "payload": {"text": "hi"}})
    store.append_event(
        sid,
        {"type": "completion", "payload": {"done": True, "done_reason": "command_failed:/compact"}},
    )
    summary = runtime.summarize_session(sid)
    assert summary["status"] == "failed"


def test_summarize_session_max_steps_reached(tmp_path: Path) -> None:
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    sid = store.create_session()
    store.append_event(sid, {"type": "user", "payload": {"text": "hi"}})
    store.append_event(
        sid,
        {"type": "completion", "payload": {"done": False, "done_reason": "max_steps_reached"}},
    )
    summary = runtime.summarize_session(sid)
    assert summary["status"] == "incomplete"


def test_summarize_session_merges_context_events(tmp_path: Path) -> None:
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    sid = store.create_session()
    store.append_event(sid, {"type": "context", "payload": {"key1": "val1"}})
    store.append_event(sid, {"type": "context", "payload": {"key2": "val2"}})
    store.append_event(sid, {"type": "user", "payload": {"text": "hi"}})
    summary = runtime.summarize_session(sid)
    assert summary["context"]["key1"] == "val1"
    assert summary["context"]["key2"] == "val2"


# list_sessions — archived edge cases (lines 302-319)


def test_list_sessions_excludes_sessions_with_only_context_events(tmp_path: Path) -> None:
    """Sessions with only 'session'/'context' events are hidden by default."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    invisible = store.create_session()
    store.append_event(invisible, {"type": "context", "payload": {"mode": "act"}})
    visible = store.create_session()
    store.append_event(visible, {"type": "user", "payload": {"text": "hi"}})

    sessions = runtime.list_sessions()
    ids = {s["session_id"] for s in sessions}
    assert visible in ids
    assert invisible not in ids


# resolve_recovery_query — retry from a specific timestamp (lines 502-507)


def test_resolve_recovery_query_retry_from_ts(tmp_path: Path) -> None:
    """retry with from_ts looks up the user event at that exact timestamp."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    sid = runtime.resolve_session()
    store.append_event(sid, {"type": "user", "payload": {"text": "first-query"}})
    events = store.load_transcript(sid)
    first_ts = events[0]["ts"]

    # Add another user event
    time.sleep(0.01)
    store.append_event(sid, {"type": "user", "payload": {"text": "second-query"}})

    query = runtime.resolve_recovery_query(sid, "retry", from_ts=first_ts)
    assert query == "first-query"


def test_resolve_recovery_query_retry_from_ts_not_found(tmp_path: Path) -> None:
    """retry with from_ts that doesn't match any event raises ValueError."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    sid = runtime.resolve_session()
    store.append_event(sid, {"type": "user", "payload": {"text": "hi"}})

    with pytest.raises(ValueError, match="no user event at ts="):
        runtime.resolve_recovery_query(sid, "retry", from_ts="9999-01-01T00:00:00+00:00")


def test_resolve_recovery_query_retry_first_event_deletes_all(tmp_path: Path) -> None:
    """When the user event is the very first event, truncate nukes everything."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    sid = runtime.resolve_session()
    store.append_event(sid, {"type": "user", "payload": {"text": "only-event"}})

    query = runtime.resolve_recovery_query(sid, "retry")
    assert query == "only-event"
    # Everything should be gone (truncated from the impossibly-early ts)
    assert store.load_transcript(sid) == []


# approve_plan / reject_plan (lines 619-683)


def test_approve_plan_returns_false_when_no_pending(tmp_path: Path) -> None:
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    sid = store.create_session()
    assert runtime.approve_plan(sid) is False


def test_approve_plan_appends_events(tmp_path: Path) -> None:
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    sid = store.create_session()
    store.append_event(
        sid, {"type": "plan_proposed", "payload": {"plan_path": "/p/plan.md", "revision": 1}}
    )
    result = runtime.approve_plan(sid)
    assert result is True
    events = store.load_transcript(sid)
    types = [e["type"] for e in events]
    assert "plan_approved" in types
    assert "context" in types


def test_approve_plan_returns_false_when_running(tmp_path: Path) -> None:
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)

    def _long_run(cancel_event):
        cancel_event.wait(timeout=2.0)

    sid = store.create_session()
    store.append_event(
        sid, {"type": "plan_proposed", "payload": {"plan_path": "/p.md", "revision": 1}}
    )
    runtime._run_registry.start(sid, target=_long_run)
    try:
        result = runtime.approve_plan(sid)
        assert result is False
    finally:
        runtime.cancel(sid)
        deadline = time.time() + 2.0
        while time.time() < deadline and runtime.is_running(sid):
            time.sleep(0.01)


def test_reject_plan_returns_false_when_no_pending(tmp_path: Path) -> None:
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    sid = store.create_session()
    assert runtime.reject_plan(sid) is False


def test_reject_plan_appends_event(tmp_path: Path) -> None:
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    sid = store.create_session()
    store.append_event(
        sid, {"type": "plan_proposed", "payload": {"plan_path": "/p/plan.md", "revision": 2}}
    )
    result = runtime.reject_plan(sid)
    assert result is True
    events = store.load_transcript(sid)
    types = [e["type"] for e in events]
    assert "plan_rejected" in types


def test_reject_plan_returns_false_when_running(tmp_path: Path) -> None:
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)

    def _long_run(cancel_event):
        cancel_event.wait(timeout=2.0)

    sid = store.create_session()
    store.append_event(
        sid, {"type": "plan_proposed", "payload": {"plan_path": "/p.md", "revision": 1}}
    )
    runtime._run_registry.start(sid, target=_long_run)
    try:
        result = runtime.reject_plan(sid)
        assert result is False
    finally:
        runtime.cancel(sid)
        deadline = time.time() + 2.0
        while time.time() < deadline and runtime.is_running(sid):
            time.sleep(0.01)


# interrupt_step — returns False when no handle (line 603)


def test_interrupt_step_returns_false_when_idle(tmp_path: Path) -> None:
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    sid = store.create_session()
    assert runtime.interrupt_step(sid) is False


# ---------------------------------------------------------------------------
# llm_resilience.py
# ---------------------------------------------------------------------------

# RetryBudget (lines 99-132)


def test_retry_budget_starts_full() -> None:
    budget = RetryBudget(capacity=10.0)
    assert budget.tokens == 10.0
    assert budget.can_retry() is True


def test_retry_budget_charge_drains() -> None:
    budget = RetryBudget(capacity=10.0, retry_cost=3.0)
    budget.charge()
    assert budget.tokens == 7.0


def test_retry_budget_cannot_retry_below_half() -> None:
    budget = RetryBudget(capacity=10.0, retry_cost=1.0)
    # Drain below half
    for _ in range(6):
        budget.charge()
    assert budget.can_retry() is False


def test_retry_budget_credit_refills_capped() -> None:
    budget = RetryBudget(capacity=10.0, success_credit=1.0)
    budget.credit()  # already full, still capped
    assert budget.tokens == 10.0


# CircuitBreaker (lines 135-173)


def test_circuit_breaker_is_open_false_initially() -> None:
    cb = CircuitBreaker()
    assert cb.is_open("my-model") is False


def test_circuit_breaker_trips_at_threshold() -> None:
    cb = CircuitBreaker(threshold=2, cooldown=60.0)
    cb.record_failure("model-a")
    assert cb.is_open("model-a") is False
    cb.record_failure("model-a")
    assert cb.is_open("model-a") is True


def test_circuit_breaker_half_opens_after_cooldown() -> None:
    clock = [0.0]
    cb = CircuitBreaker(threshold=1, cooldown=10.0, clock=lambda: clock[0])
    cb.record_failure("model")
    assert cb.is_open("model") is True
    clock[0] = 11.0  # past cooldown
    assert cb.is_open("model") is False  # half-open


def test_circuit_breaker_record_success_clears_failure_count() -> None:
    cb = CircuitBreaker(threshold=3, cooldown=60.0)
    cb.record_failure("m")
    cb.record_success("m")
    assert not cb.is_open("m")


def test_circuit_breaker_zero_threshold_never_trips() -> None:
    cb = CircuitBreaker(threshold=0)
    for _ in range(100):
        cb.record_failure("m")
    assert cb.is_open("m") is False


# RetryStrategy.classify (lines 281-334) — 3-way classification


def test_classify_cancelled_is_fatal() -> None:
    decision = RetryStrategy.classify(asyncio.CancelledError())
    assert decision.action is RetryAction.FATAL
    assert decision.reason == "cancelled"


def test_classify_timeout_is_retry_same() -> None:
    decision = RetryStrategy.classify(asyncio.TimeoutError())
    assert decision.action is RetryAction.RETRY_SAME
    assert decision.reason == "timeout"


def test_classify_deterministic_without_litellm(monkeypatch) -> None:
    """Without litellm, ValueError is FATAL (deterministic)."""
    import builtins

    real_import = builtins.__import__

    def _mock_import(name, *args, **kwargs):
        if name == "litellm.exceptions":
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_mock_import):
        # classify catches ImportError and uses _DETERMINISTIC fallback
        decision = RetryStrategy.classify(ValueError("bad input"))
    assert decision.action is RetryAction.FATAL


def test_classify_unknown_error_without_litellm(monkeypatch) -> None:
    """Without litellm, unknown errors default to RETRY_SAME."""
    import builtins

    real_import = builtins.__import__

    def _mock_import(name, *args, **kwargs):
        if name == "litellm.exceptions":
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_mock_import):
        decision = RetryStrategy.classify(RuntimeError("transient"))
    assert decision.action is RetryAction.RETRY_SAME


def _mk_lx(cls, message: str):
    """Construct a litellm exception, trying multiple constructor patterns.

    Mirrors the _mk() helper in test_llm_resilience.py — some openai-derived
    errors (e.g. PermissionDeniedError) require an httpx.Response; for those
    we bypass __init__ and set .message directly so that classify() only needs
    the exception type.
    """
    for attempt in (
        lambda: cls(message=message, llm_provider="openai", model="test-model"),
        lambda: cls(message, "openai", "test-model"),
        lambda: cls(message),
    ):
        try:
            return attempt()
        except TypeError:
            continue
    # Fallback: bypass __init__ for exceptions requiring extra args (e.g. httpx.Response)
    inst = cls.__new__(cls)
    inst.message = message  # type: ignore[attr-defined]
    return inst


def test_classify_with_litellm_rate_limit_with_switch_hint() -> None:
    """RateLimitError with a quota-exhausted message → SWITCH_MODEL."""
    try:
        import litellm.exceptions as lx
    except ImportError:
        pytest.skip("litellm not installed")

    exc = _mk_lx(lx.RateLimitError, "out of extra usage for this period")
    decision = RetryStrategy.classify(exc)
    assert decision.action is RetryAction.SWITCH_MODEL


def test_classify_with_litellm_rate_limit_plain() -> None:
    """Plain RateLimitError (no switch hint) → RETRY_SAME."""
    try:
        import litellm.exceptions as lx
    except ImportError:
        pytest.skip("litellm not installed")

    exc = _mk_lx(lx.RateLimitError, "too many requests")
    decision = RetryStrategy.classify(exc)
    assert decision.action is RetryAction.RETRY_SAME


def test_classify_with_litellm_context_window() -> None:
    """ContextWindowExceededError → SWITCH_MODEL."""
    try:
        import litellm.exceptions as lx
    except ImportError:
        pytest.skip("litellm not installed")

    exc = _mk_lx(lx.ContextWindowExceededError, "context window exceeded")
    decision = RetryStrategy.classify(exc)
    assert decision.action is RetryAction.SWITCH_MODEL
    assert decision.reason == "context_window"


def test_classify_with_litellm_auth_error() -> None:
    """AuthenticationError → SWITCH_MODEL."""
    try:
        import litellm.exceptions as lx
    except ImportError:
        pytest.skip("litellm not installed")

    exc = _mk_lx(lx.AuthenticationError, "invalid key")
    decision = RetryStrategy.classify(exc)
    assert decision.action is RetryAction.SWITCH_MODEL
    assert decision.reason == "auth"


def test_classify_with_litellm_permission_denied() -> None:
    """PermissionDeniedError → FATAL."""
    try:
        import litellm.exceptions as lx
    except ImportError:
        pytest.skip("litellm not installed")

    exc = _mk_lx(lx.PermissionDeniedError, "permission denied")
    decision = RetryStrategy.classify(exc)
    assert decision.action is RetryAction.FATAL
    assert decision.reason == "permission_denied"


def test_classify_with_litellm_internal_server_error() -> None:
    """InternalServerError → RETRY_SAME."""
    try:
        import litellm.exceptions as lx
    except ImportError:
        pytest.skip("litellm not installed")

    exc = _mk_lx(lx.InternalServerError, "server error")
    decision = RetryStrategy.classify(exc)
    assert decision.action is RetryAction.RETRY_SAME
    assert decision.reason == "server_error"


def test_classify_with_litellm_bad_request_with_switch_hint() -> None:
    """BadRequestError with quota message → SWITCH_MODEL."""
    try:
        import litellm.exceptions as lx
    except ImportError:
        pytest.skip("litellm not installed")

    exc = _mk_lx(lx.BadRequestError, "exceeded your current quota")
    decision = RetryStrategy.classify(exc)
    assert decision.action is RetryAction.SWITCH_MODEL
    assert decision.reason == "quota_exhausted"


def test_classify_with_litellm_bad_request_plain() -> None:
    """BadRequestError without quota hint → FATAL."""
    try:
        import litellm.exceptions as lx
    except ImportError:
        pytest.skip("litellm not installed")

    exc = _mk_lx(lx.BadRequestError, "malformed request")
    decision = RetryStrategy.classify(exc)
    assert decision.action is RetryAction.FATAL
    assert decision.reason == "bad_request"


def test_classify_with_litellm_connection_error() -> None:
    """APIConnectionError → RETRY_SAME."""
    try:
        import litellm.exceptions as lx
    except ImportError:
        pytest.skip("litellm not installed")

    exc = _mk_lx(lx.APIConnectionError, "connection failed")
    decision = RetryStrategy.classify(exc)
    assert decision.action is RetryAction.RETRY_SAME
    assert decision.reason == "connection"


# RetryStrategy.backoff (line 336-345)


def test_backoff_with_retry_after_floor() -> None:
    """Server Retry-After is used as a floor when larger than jitter."""
    strategy = RetryStrategy(
        backoff_base=0.001,
        backoff_cap=0.001,
        retry_after_cap=120.0,
        rng=lambda: 0.0,  # deterministic: 0 jitter
    )
    delay = strategy.backoff(1, retry_after=5.0)
    assert delay == pytest.approx(5.0, abs=0.01)


def test_backoff_retry_after_capped() -> None:
    """Retry-After is capped to retry_after_cap."""
    strategy = RetryStrategy(
        backoff_base=0.001,
        backoff_cap=0.001,
        retry_after_cap=10.0,
        rng=lambda: 0.0,
    )
    delay = strategy.backoff(1, retry_after=9999.0)
    assert delay == pytest.approx(10.0, abs=0.01)


def test_backoff_zero_retry_after_uses_jitter() -> None:
    """retry_after=0 does not override the jitter delay."""
    strategy = RetryStrategy(
        backoff_base=2.0,
        backoff_cap=10.0,
        retry_after_cap=60.0,
        rng=lambda: 1.0,  # deterministic: max jitter
    )
    delay = strategy.backoff(2, retry_after=0.0)
    # ceiling = min(10, 2 * 2^1) = 4; delay = 1.0 * 4 = 4
    assert delay == pytest.approx(4.0, abs=0.01)


# RetryStrategy._retry_after (lines 260-270)


def test_retry_after_reads_header() -> None:
    exc = RuntimeError("rate limited")
    exc.response = SimpleNamespace(headers={"retry-after": "30"})
    result = RetryStrategy._retry_after(exc)
    assert result == 30.0


def test_retry_after_returns_none_on_no_response() -> None:
    exc = RuntimeError("no response attr")
    result = RetryStrategy._retry_after(exc)
    assert result is None


def test_retry_after_returns_none_on_bad_value() -> None:
    exc = RuntimeError("bad header")
    exc.response = SimpleNamespace(headers={"retry-after": "not-a-number"})
    result = RetryStrategy._retry_after(exc)
    assert result is None


# DoomLoopGuard (lines 469-519)


def test_doom_loop_guard_not_stuck_below_threshold() -> None:
    guard = DoomLoopGuard(threshold=3)
    guard.observe([{"name": "tool", "args": {"x": 1}}])
    guard.observe([{"name": "tool", "args": {"x": 1}}])
    assert guard.is_stuck() is False


def test_doom_loop_guard_detects_repeated_identical_calls() -> None:
    guard = DoomLoopGuard(threshold=3)
    call = [{"name": "tool", "args": {"x": 1}}]
    guard.observe(call)
    guard.observe(call)
    guard.observe(call)
    assert guard.is_stuck() is True


def test_doom_loop_guard_not_stuck_if_calls_vary() -> None:
    guard = DoomLoopGuard(threshold=3)
    guard.observe([{"name": "tool", "args": {"x": 1}}])
    guard.observe([{"name": "tool", "args": {"x": 2}}])
    guard.observe([{"name": "tool", "args": {"x": 1}}])
    assert guard.is_stuck() is False


def test_doom_loop_guard_empty_signature_not_stuck() -> None:
    """Empty tool calls produce an empty signature; loop on empty is ignored."""
    guard = DoomLoopGuard(threshold=3)
    guard.observe([])
    guard.observe([])
    guard.observe([])
    # Empty signatures: all equal but the guard should NOT flag empty batches
    assert guard.is_stuck() is False


def test_doom_loop_guard_signature_object_style() -> None:
    """Tool call objects with .name/.args attributes are also hashed."""
    tc = SimpleNamespace(name="tool", args={"k": "v"}, id="xyz")
    sig = DoomLoopGuard.signature([tc])
    assert "tool" in sig
    assert '"k"' in sig


# repair_tool_pairing (lines 522-565)


def test_repair_tool_pairing_no_repair_needed() -> None:
    from langchain_core.messages import AIMessage, ToolMessage

    ai = AIMessage(content="", tool_calls=[{"id": "t1", "name": "tool", "args": {}}])
    tm = ToolMessage(content="ok", tool_call_id="t1")
    messages = [ai, tm]
    count = repair_tool_pairing(messages)
    assert count == 0
    assert len(messages) == 2


def test_repair_tool_pairing_drops_orphan_tool_result() -> None:
    """A ToolMessage without a matching AIMessage tool_call is dropped."""
    from langchain_core.messages import ToolMessage

    orphan = ToolMessage(content="orphan", tool_call_id="no-such-id")
    messages = [orphan]
    count = repair_tool_pairing(messages)
    assert count == 1
    assert messages == []


def test_repair_tool_pairing_synthesizes_interrupted_result() -> None:
    """An AIMessage tool_call without a matching ToolMessage gets a synthetic result."""
    from langchain_core.messages import AIMessage, ToolMessage

    ai = AIMessage(content="", tool_calls=[{"id": "t1", "name": "tool", "args": {}}])
    messages = [ai]
    count = repair_tool_pairing(messages)
    assert count == 1
    assert len(messages) == 2
    synthetic = messages[1]
    assert isinstance(synthetic, ToolMessage)
    assert synthetic.tool_call_id == "t1"
    assert "interrupted" in synthetic.content.lower()


def test_repair_tool_pairing_mixed_scenario() -> None:
    """Orphan result is dropped AND unanswered call gets a synthetic result."""
    from langchain_core.messages import AIMessage, ToolMessage

    ai = AIMessage(content="", tool_calls=[{"id": "t1", "name": "tool", "args": {}}])
    orphan = ToolMessage(content="orphan", tool_call_id="ghost-id")
    messages = [ai, orphan]
    count = repair_tool_pairing(messages)
    assert count == 2
    # orphan dropped, synthetic for t1 appended
    ids = [m.tool_call_id for m in messages if isinstance(m, ToolMessage)]
    assert "t1" in ids
    assert "ghost-id" not in ids


# RetryStrategy.run — async state machine (lines 351-466)
# Use asyncio.run() for sync test execution (no pytest-asyncio installed)


def test_retry_strategy_run_success_on_first_attempt() -> None:
    from langchain_core.messages import AIMessage

    response = AIMessage(content="hello")
    strategy = RetryStrategy(timeout=5.0, primary_retries=2, turn_deadline=0)

    async def _invoke(model, is_fallback):
        return response

    async def _run():
        emitted: list = []
        result, model = await strategy.run(
            models=["gpt-4"],
            invoke=_invoke,
            emit=emitted.append,
            compact=AsyncMock(return_value=False),
            agent_id="a1",
            depth=0,
            step=1,
        )
        assert result is response
        assert model == "gpt-4"
        assert emitted == []

    asyncio.run(_run())


def test_retry_strategy_run_retries_on_transient_error() -> None:
    from langchain_core.messages import AIMessage

    call_count = [0]

    async def _invoke(model, is_fallback):
        call_count[0] += 1
        if call_count[0] < 2:
            raise RuntimeError("transient")
        return AIMessage(content="ok")

    strategy = RetryStrategy(
        timeout=5.0,
        primary_retries=3,
        turn_deadline=0,
        backoff_base=0.0,
        backoff_cap=0.0,
        rng=lambda: 0.0,
    )

    async def _run():
        emitted: list = []
        result, _ = await strategy.run(
            models=["gpt-4"],
            invoke=_invoke,
            emit=emitted.append,
            compact=AsyncMock(return_value=False),
            agent_id="a1",
            depth=0,
            step=1,
        )
        assert "ok" in result.content
        retry_events = [e for e in emitted if e.get("type") == "llm_retry"]
        assert len(retry_events) >= 1

    asyncio.run(_run())


def test_retry_strategy_run_exhaustion_raises() -> None:
    async def _always_fail(model, is_fallback):
        raise RuntimeError("always fails")

    strategy = RetryStrategy(
        timeout=5.0,
        primary_retries=2,
        turn_deadline=0,
        backoff_base=0.0,
        backoff_cap=0.0,
        rng=lambda: 0.0,
    )

    async def _run():
        with pytest.raises(LlmResilienceExhausted):
            await strategy.run(
                models=["gpt-4"],
                invoke=_always_fail,
                emit=lambda _: None,
                compact=AsyncMock(return_value=False),
                agent_id="a1",
                depth=0,
                step=1,
            )

    asyncio.run(_run())


def test_retry_strategy_run_fallback_model_used() -> None:
    from langchain_core.messages import AIMessage

    tried: list[str] = []

    async def _invoke(model, is_fallback):
        tried.append(model)
        if model == "primary":
            raise RuntimeError("primary down")
        return AIMessage(content="fallback ok")

    strategy = RetryStrategy(
        timeout=5.0,
        primary_retries=1,
        fallback_retries=1,
        turn_deadline=0,
        backoff_base=0.0,
        backoff_cap=0.0,
        rng=lambda: 0.0,
    )

    async def _run():
        emitted: list = []
        result, used_model = await strategy.run(
            models=["primary", "fallback"],
            invoke=_invoke,
            emit=emitted.append,
            compact=AsyncMock(return_value=False),
            agent_id="a1",
            depth=0,
            step=1,
        )
        assert used_model == "fallback"
        assert any(e.get("type") == "llm_fallback" for e in emitted)

    asyncio.run(_run())


def test_retry_strategy_run_cancellation_propagates() -> None:
    async def _cancel(model, is_fallback):
        raise asyncio.CancelledError()

    strategy = RetryStrategy(timeout=5.0, primary_retries=3, turn_deadline=0)

    async def _run():
        with pytest.raises(asyncio.CancelledError):
            await strategy.run(
                models=["gpt-4"],
                invoke=_cancel,
                emit=lambda _: None,
                compact=AsyncMock(return_value=False),
                agent_id="a1",
                depth=0,
                step=1,
            )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# context.py
# ---------------------------------------------------------------------------

# event_payload_text (lines 55-65)


def test_event_payload_text_plain_string() -> None:
    event = {"type": "user", "payload": "hello"}
    assert event_payload_text(event) == "hello"


def test_event_payload_text_dict_with_text() -> None:
    event = {"type": "user", "payload": {"text": "msg"}}
    assert event_payload_text(event) == "msg"


def test_event_payload_text_dict_with_tool_input() -> None:
    event = {"type": "tool_result", "payload": {"tool_input": {"x": 1}, "result": "done"}}
    text = event_payload_text(event)
    assert "done" in text


def test_event_payload_text_dict_with_message() -> None:
    event = {"type": "assistant", "payload": {"message": "hi there"}}
    assert event_payload_text(event) == "hi there"


def test_event_payload_text_dict_with_result() -> None:
    event = {"type": "tool_result", "payload": {"result": "success"}}
    assert event_payload_text(event) == "success"


def test_event_payload_text_empty_dict() -> None:
    event = {"type": "unknown", "payload": {}}
    result = event_payload_text(event)
    # Should not raise; returns str repr of empty dict
    assert isinstance(result, str)


# render_event_lines (lines 68-76)


def test_render_event_lines_skips_empty_payloads() -> None:
    events = [
        {"type": "user", "payload": {"text": "hello"}},
        # Payload dict with no text/message/result renders as the dict repr which is non-empty,
        # so we use a literal empty string payload instead
        {"type": "user", "payload": ""},
    ]
    lines = render_event_lines(events)
    assert "hello" in lines
    count = lines.count("user:")
    assert count == 1


# _iter_attachments (lines 86-99)


def test_iter_attachments_yields_att_dicts() -> None:
    events = [
        {
            "type": "context",
            "payload": {"attachments": [{"stored_name": "f.txt", "filename": "f.txt"}]},
        },
        {"type": "user", "payload": {"text": "hi"}},
    ]
    atts = list(_iter_attachments(events))
    assert len(atts) == 1
    assert atts[0]["stored_name"] == "f.txt"


def test_iter_attachments_skips_non_context_events() -> None:
    events = [
        {"type": "user", "payload": {"attachments": [{"stored_name": "f.txt"}]}},
    ]
    assert list(_iter_attachments(events)) == []


def test_iter_attachments_skips_non_list_attachments() -> None:
    events = [
        {"type": "context", "payload": {"attachments": "not-a-list"}},
    ]
    assert list(_iter_attachments(events)) == []


# _read_text_capped (lines 102-108)


def test_read_text_capped_returns_content(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("hello world")
    result = _read_text_capped(str(f), 1000)
    assert result == "hello world"


def test_read_text_capped_caps_at_limit(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("abcdefghij")
    result = _read_text_capped(str(f), 5)
    assert result == "abcde"


def test_read_text_capped_returns_none_on_error(tmp_path: Path) -> None:
    result = _read_text_capped(str(tmp_path / "missing.txt"), 100)
    assert result is None


# _load_attachment_texts (lines 111-164)


def test_load_attachment_texts_skips_missing_stored_name(tmp_path: Path) -> None:
    events = [{"type": "context", "payload": {"attachments": [{"filename": "f.txt"}]}}]
    result = _load_attachment_texts(str(tmp_path), events, model_name=None)
    assert result == []


def test_load_attachment_texts_loads_plain_text_file(tmp_path: Path) -> None:
    att_dir = tmp_path / "attachments"
    att_dir.mkdir()
    (att_dir / "doc.txt").write_text("file content", encoding="utf-8")
    events = [
        {
            "type": "context",
            "payload": {
                "attachments": [
                    {
                        "stored_name": "doc.txt",
                        "filename": "doc.txt",
                        "content_type": "text/plain",
                        "size_bytes": 12,
                    }
                ]
            },
        }
    ]
    result = _load_attachment_texts(str(tmp_path), events, model_name=None)
    assert any("file content" in t for t in result)


def test_load_attachment_texts_skips_missing_file(tmp_path: Path) -> None:
    events = [
        {
            "type": "context",
            "payload": {
                "attachments": [
                    {
                        "stored_name": "missing.txt",
                        "filename": "missing.txt",
                        "content_type": "text/plain",
                        "size_bytes": 10,
                    }
                ]
            },
        }
    ]
    result = _load_attachment_texts(str(tmp_path), events, model_name=None)
    assert result == []


def test_load_attachment_texts_image_non_vision_model_warns(tmp_path: Path) -> None:
    """Image attachment on non-vision model produces a skip warning."""
    att_dir = tmp_path / "attachments"
    att_dir.mkdir()
    (att_dir / "photo.png").write_bytes(b"\x89PNG")
    events = [
        {
            "type": "context",
            "payload": {
                "attachments": [
                    {
                        "stored_name": "photo.png",
                        "filename": "photo.png",
                        "content_type": "image/png",
                        "size_bytes": 4,
                    }
                ]
            },
        }
    ]
    result = _load_attachment_texts(str(tmp_path), events, model_name=None)
    assert any(
        "skipped" in t.lower() or "vision" in t.lower() or "model" in t.lower() for t in result
    )


def test_load_attachment_texts_too_large_inline(tmp_path: Path) -> None:
    """File exceeding per-file cap gets a [Attachment ... too large] placeholder."""
    att_dir = tmp_path / "attachments"
    att_dir.mkdir()
    (att_dir / "big.txt").write_text("x" * 1000, encoding="utf-8")
    events = [
        {
            "type": "context",
            "payload": {
                "attachments": [
                    {
                        "stored_name": "big.txt",
                        "filename": "big.txt",
                        "content_type": "text/plain",
                        "size_bytes": 300_001,  # Exceeds _MAX_ATTACHMENT_BYTES
                    }
                ]
            },
        }
    ]
    result = _load_attachment_texts(str(tmp_path), events, model_name=None)
    assert any("too large" in t.lower() for t in result)


def test_load_attachment_texts_aggregate_limit(tmp_path: Path) -> None:
    """When aggregate total exceeds the cap, additional files are skipped."""
    att_dir = tmp_path / "attachments"
    att_dir.mkdir()
    # Each file is just under the per-file cap but together exceed the aggregate
    chunk = "x" * 200_000
    for i in range(6):
        (att_dir / f"file{i}.txt").write_text(chunk, encoding="utf-8")
    events = [
        {
            "type": "context",
            "payload": {
                "attachments": [
                    {
                        "stored_name": f"file{i}.txt",
                        "filename": f"file{i}.txt",
                        "content_type": "text/plain",
                        "size_bytes": len(chunk),
                    }
                    for i in range(6)
                ]
            },
        }
    ]
    result = _load_attachment_texts(str(tmp_path), events, model_name=None)
    assert any("aggregate size limit" in t.lower() for t in result)


# _load_attachment_images (lines 167-191)


def test_load_attachment_images_returns_empty_for_non_vision_model(tmp_path: Path) -> None:
    """Non-vision model → empty list, no file reads."""
    events = [
        {
            "type": "context",
            "payload": {"attachments": [{"stored_name": "photo.png", "content_type": "image/png"}]},
        }
    ]
    result = _load_attachment_images(str(tmp_path), events, model_name=None)
    assert result == []


# ContextBuilder.build — wires session_store correctly (lines 194-277)


def test_context_builder_build_returns_snapshot(tmp_path: Path) -> None:
    from mewbo_core.session_store import SessionStore

    store = SessionStore(root_dir=str(tmp_path))
    sid = store.create_session()
    store.append_event(sid, {"type": "user", "payload": {"text": "hello"}})

    builder = ContextBuilder(session_store=store)
    snapshot = builder.build(sid, user_query="hello", model_name=None)
    assert isinstance(snapshot, ContextSnapshot)
    assert snapshot.summary is None  # No summary written
    assert any(e.get("type") == "user" for e in snapshot.events)


def test_context_builder_build_anchors_first_user_event(tmp_path: Path) -> None:
    """The first user event is anchored in recent_events even when it falls outside the window."""
    from mewbo_core.config import reset_config, set_config_override
    from mewbo_core.session_store import SessionStore

    set_config_override({"context": {"recent_event_limit": 1, "selection_enabled": False}})
    try:
        store = SessionStore(root_dir=str(tmp_path))
        sid = store.create_session()
        # Add multiple user events so the first one is outside the recent window
        for i in range(5):
            store.append_event(sid, {"type": "user", "payload": {"text": f"q{i}"}})
        store.append_event(sid, {"type": "assistant", "payload": {"text": "ans"}})

        builder = ContextBuilder(session_store=store)
        snapshot = builder.build(sid, user_query="q4", model_name=None)
        # First user event must be present in recent_events
        user_texts = [
            e.get("payload", {}).get("text", "")
            for e in snapshot.recent_events
            if e.get("type") == "user"
        ]
        assert "q0" in user_texts
    finally:
        reset_config()


# ---------------------------------------------------------------------------
# tool_registry.py
# ---------------------------------------------------------------------------

# ToolSpec.is_plan_safe (lines 74-76)


def test_tool_spec_is_plan_safe_via_read_only() -> None:
    spec = ToolSpec(tool_id="t", name="T", description="", factory=lambda: None, read_only=True)
    assert spec.is_plan_safe() is True


def test_tool_spec_is_plan_safe_via_metadata() -> None:
    spec = ToolSpec(
        tool_id="t", name="T", description="", factory=lambda: None, metadata={"plan_safe": True}
    )
    assert spec.is_plan_safe() is True


def test_tool_spec_is_not_plan_safe_by_default() -> None:
    spec = ToolSpec(tool_id="t", name="T", description="", factory=lambda: None)
    assert spec.is_plan_safe() is False


# ToolRegistry.disable — removes cached instance (line 109-110)


def test_tool_registry_disable_removes_cached_instance() -> None:
    registry = ToolRegistry()
    instance = MagicMock()
    spec = ToolSpec(tool_id="t", name="T", description="", factory=lambda: instance)
    registry.register(spec)
    _ = registry.get("t")  # Instantiate and cache
    assert "t" in registry._instances
    registry.disable("t", "testing")
    assert "t" not in registry._instances
    assert registry.get("t") is None


def test_tool_registry_disable_noop_for_unknown() -> None:
    registry = ToolRegistry()
    registry.disable("unknown", "reason")  # Must not raise


# ToolRegistry.list_specs_for_mode (lines 148-153)


def test_list_specs_for_mode_plan_filters_to_plan_safe() -> None:
    registry = ToolRegistry()
    read_only_spec = ToolSpec(
        tool_id="safe", name="Safe", description="", factory=lambda: None, read_only=True
    )
    write_spec = ToolSpec(
        tool_id="unsafe", name="Unsafe", description="", factory=lambda: None, read_only=False
    )
    registry.register(read_only_spec)
    registry.register(write_spec)
    plan_specs = registry.list_specs_for_mode("plan")
    ids = {s.tool_id for s in plan_specs}
    assert "safe" in ids
    assert "unsafe" not in ids


def test_list_specs_for_mode_non_plan_returns_all_enabled() -> None:
    registry = ToolRegistry()
    for i in range(3):
        registry.register(
            ToolSpec(tool_id=f"t{i}", name=f"T{i}", description="", factory=lambda: None)
        )
    act_specs = registry.list_specs_for_mode("act")
    assert len(act_specs) == 3


# is_always_load / is_deferred (lines 178-204)


def test_is_always_load_true() -> None:
    spec = ToolSpec(
        tool_id="t", name="T", description="", factory=lambda: None, metadata={"always_load": True}
    )
    assert is_always_load(spec) is True


def test_is_always_load_false_by_default() -> None:
    spec = ToolSpec(tool_id="t", name="T", description="", factory=lambda: None)
    assert is_always_load(spec) is False


def test_is_deferred_false_for_always_load() -> None:
    spec = ToolSpec(
        tool_id="t",
        name="T",
        description="",
        factory=lambda: None,
        metadata={"always_load": True, "deferred": True},
    )
    assert is_deferred(spec) is False


def test_is_deferred_false_for_tool_search_itself() -> None:
    spec = ToolSpec(tool_id=TOOL_SEARCH_TOOL_ID, name="TS", description="", factory=lambda: None)
    assert is_deferred(spec) is False


def test_is_deferred_true_for_mcp_kind() -> None:
    spec = ToolSpec(
        tool_id="mcp_foo_bar", name="Foo", description="", factory=lambda: None, kind="mcp"
    )
    assert is_deferred(spec) is True


def test_is_deferred_true_via_metadata() -> None:
    spec = ToolSpec(
        tool_id="t", name="T", description="", factory=lambda: None, metadata={"deferred": True}
    )
    assert is_deferred(spec) is True


def test_is_deferred_false_by_default_for_local() -> None:
    spec = ToolSpec(tool_id="t", name="T", description="", factory=lambda: None, kind="local")
    assert is_deferred(spec) is False


# filter_specs (lines 961-987)


def test_filter_specs_allowed_only() -> None:
    from mewbo_core.config import reset_config, set_config_override

    set_config_override({"agent": {"default_denied_tools": []}})
    try:
        specs = [
            ToolSpec(tool_id="a", name="A", description="", factory=lambda: None),
            ToolSpec(tool_id="b", name="B", description="", factory=lambda: None),
        ]
        result = filter_specs(specs, allowed=["a"])
        assert [s.tool_id for s in result] == ["a"]
    finally:
        reset_config()


def test_filter_specs_denied_overrides_allowed() -> None:
    from mewbo_core.config import reset_config, set_config_override

    set_config_override({"agent": {"default_denied_tools": []}})
    try:
        specs = [
            ToolSpec(tool_id="a", name="A", description="", factory=lambda: None),
            ToolSpec(tool_id="b", name="B", description="", factory=lambda: None),
        ]
        result = filter_specs(specs, allowed=["a", "b"], denied=["b"])
        assert [s.tool_id for s in result] == ["a"]
    finally:
        reset_config()


def test_filter_specs_config_denied_applied(monkeypatch) -> None:
    """Config-level default_denied_tools are also applied."""
    from mewbo_core.config import reset_config, set_config_override

    set_config_override({"agent": {"default_denied_tools": ["b"]}})
    try:
        specs = [
            ToolSpec(tool_id="a", name="A", description="", factory=lambda: None),
            ToolSpec(tool_id="b", name="B", description="", factory=lambda: None),
        ]
        result = filter_specs(specs)
        ids = [s.tool_id for s in result]
        assert "b" not in ids
        assert "a" in ids
    finally:
        reset_config()


def test_filter_specs_config_denied_as_comma_string(monkeypatch) -> None:
    """Config default_denied_tools accepts a comma-delimited string."""
    from mewbo_core.config import reset_config, set_config_override

    set_config_override({"agent": {"default_denied_tools": "b,c"}})
    try:
        specs = [
            ToolSpec(tool_id="a", name="A", description="", factory=lambda: None),
            ToolSpec(tool_id="b", name="B", description="", factory=lambda: None),
            ToolSpec(tool_id="c", name="C", description="", factory=lambda: None),
        ]
        result = filter_specs(specs)
        ids = [s.tool_id for s in result]
        assert "a" in ids
        assert "b" not in ids
        assert "c" not in ids
    finally:
        reset_config()


def test_filter_specs_no_filters_returns_all() -> None:
    from mewbo_core.config import reset_config, set_config_override

    set_config_override({"agent": {"default_denied_tools": []}})
    try:
        specs = [
            ToolSpec(tool_id="a", name="A", description="", factory=lambda: None),
            ToolSpec(tool_id="b", name="B", description="", factory=lambda: None),
        ]
        result = filter_specs(specs)
        assert len(result) == 2
    finally:
        reset_config()
