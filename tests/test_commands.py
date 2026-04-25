"""Unit tests for the server-side command registry."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from mewbo_core.commands import (
    COMMANDS,
    CommandContext,
    CommandError,
    CommandRender,
    execute_command,
    list_commands,
)


def _make_ctx(tmp_path) -> CommandContext:
    from mewbo_core.session_store import create_session_store

    store = create_session_store(root_dir=str(tmp_path))
    sid = store.create_session()
    return CommandContext(session_id=sid, session_store=store)


class _RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def notify(self, **kwargs) -> None:
        self.calls.append(kwargs)


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------


def test_registry_contains_expected_commands() -> None:
    expected = {"compact", "skills", "tokens", "fork", "tag", "help"}
    assert set(COMMANDS.keys()) == expected


def test_list_commands_returns_serializable_metadata() -> None:
    metadata = list_commands()
    assert isinstance(metadata, list)
    names = {entry["name"] for entry in metadata}
    assert names == {"compact", "skills", "tokens", "fork", "tag", "help"}
    for entry in metadata:
        assert set(entry.keys()) == {"name", "description", "usage", "render"}
        assert entry["render"] in {"transcript", "dialog", "notification"}


def test_execute_unknown_command_raises_key_error(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    with pytest.raises(KeyError):
        asyncio.run(execute_command("not-a-command", [], ctx))


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------


def test_help_returns_dialog_listing_all_commands(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    result = asyncio.run(execute_command("help", [], ctx))
    assert result.render is CommandRender.DIALOG
    assert result.title == "Commands"
    for cmd_name in ("compact", "skills", "tokens", "fork", "tag", "help"):
        assert f"/{cmd_name}" in result.body


# ---------------------------------------------------------------------------
# /skills
# ---------------------------------------------------------------------------


def test_skills_returns_dialog_with_listed_skills(tmp_path) -> None:
    class FakeSkill:
        def __init__(self, name: str, description: str) -> None:
            self.name = name
            self.description = description

    class FakeRegistry:
        def list_all(self) -> list[FakeSkill]:
            return [
                FakeSkill("python", "Python expertise"),
                FakeSkill("debugging", "Systematic debugging"),
            ]

    ctx = _make_ctx(tmp_path)
    ctx.skill_registry = FakeRegistry()
    result = asyncio.run(execute_command("skills", [], ctx))
    assert result.render is CommandRender.DIALOG
    assert "python" in result.body
    assert "debugging" in result.body
    assert "Python expertise" in result.body


def test_skills_handles_missing_registry_gracefully(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)  # no skill_registry
    result = asyncio.run(execute_command("skills", [], ctx))
    assert result.render is CommandRender.DIALOG
    assert "no skills" in result.body.lower()


# ---------------------------------------------------------------------------
# /tokens
# ---------------------------------------------------------------------------


def test_tokens_renders_table_from_usage_provider(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    ctx.usage_provider = lambda sid: {
        "total_input_tokens": 1234,
        "total_output_tokens": 567,
    }
    result = asyncio.run(execute_command("tokens", [], ctx))
    assert result.render is CommandRender.DIALOG
    assert "1234" in result.body
    assert "567" in result.body
    assert "|" in result.body  # markdown table


def test_tokens_handles_missing_provider(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    result = asyncio.run(execute_command("tokens", [], ctx))
    assert result.render is CommandRender.DIALOG
    assert "not available" in result.body.lower()


# ---------------------------------------------------------------------------
# /fork
# ---------------------------------------------------------------------------


def test_fork_creates_new_session_and_notifies(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    notifier = _RecordingNotifier()
    ctx.notification_service = notifier
    ctx.session_store.append_event(
        ctx.session_id, {"type": "user", "payload": {"text": "hi"}}
    )

    result = asyncio.run(execute_command("fork", [], ctx))
    new_id = result.metadata["new_session_id"]
    assert result.render is CommandRender.NOTIFICATION
    assert new_id != ctx.session_id
    assert ctx.session_store.load_transcript(new_id)
    assert len(notifier.calls) == 1


def test_fork_with_tag_argument(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    notifier = _RecordingNotifier()
    ctx.notification_service = notifier
    result = asyncio.run(execute_command("fork", ["release-candidate"], ctx))
    new_id = result.metadata["new_session_id"]
    assert ctx.session_store.resolve_tag("release-candidate") == new_id
    assert result.metadata["tag"] == "release-candidate"


# ---------------------------------------------------------------------------
# /tag
# ---------------------------------------------------------------------------


def test_tag_sets_session_tag_and_notifies(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    notifier = _RecordingNotifier()
    ctx.notification_service = notifier

    result = asyncio.run(execute_command("tag", ["my-label"], ctx))
    assert result.render is CommandRender.NOTIFICATION
    assert result.metadata["tag"] == "my-label"
    assert ctx.session_store.resolve_tag("my-label") == ctx.session_id
    assert len(notifier.calls) == 1


def test_tag_without_argument_raises(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    with pytest.raises(CommandError, match="Usage"):
        asyncio.run(execute_command("tag", [], ctx))


# ---------------------------------------------------------------------------
# /compact
# ---------------------------------------------------------------------------


def test_compact_returns_transcript_render_with_short_body(
    tmp_path, monkeypatch
) -> None:
    """Handler returns a stable short body (chat-friendly) and stashes the
    full summary in metadata. The summary itself is the ``context_compacted``
    event recorded by ``record_compaction`` — clients render that via their
    compaction log component instead of dumping it into the chat."""
    from mewbo_core.compact import CompactionMode

    @dataclass
    class FakeCompactionResult:
        summary: str = "session-summary"
        tokens_saved: int = 100
        kept_events: list = None  # type: ignore[assignment]
        model: str = "test-model"
        events_summarized: int = 7

        def __post_init__(self) -> None:
            if self.kept_events is None:
                self.kept_events = []

    ctx = _make_ctx(tmp_path)

    async def fake_compact(self, session_id, mode=None, **kwargs):
        assert mode is CompactionMode.FULL
        self.save_summary(session_id, "session-summary")
        return FakeCompactionResult()

    monkeypatch.setattr(
        type(ctx.session_store), "compact_session", fake_compact, raising=True
    )

    result = asyncio.run(execute_command("compact", [], ctx))
    assert result.render is CommandRender.TRANSCRIPT
    # Body is short and points users at the Logs pane; full summary lives
    # on the marker event (and in metadata for non-event-aware clients).
    assert "Compaction complete" in result.body
    assert "Logs" in result.body
    assert "session-summary" not in result.body
    assert result.metadata["summary"] == "session-summary"
    assert result.metadata["tokens_saved"] == 100
    assert result.metadata["events_summarized"] == 7
    assert result.metadata["focus"] is None


def test_compact_emits_context_compacted_marker(tmp_path, monkeypatch) -> None:
    """User-triggered compaction must write the same marker the auto path
    writes, so ContextBuilder boundary detection and on_compact hooks
    treat both paths identically."""

    @dataclass
    class _Result:
        summary: str = "fresh"
        tokens_saved: int = 555
        kept_events: list = None  # type: ignore[assignment]
        model: str = "claude-sonnet-4-6"
        events_summarized: int = 12

        def __post_init__(self) -> None:
            if self.kept_events is None:
                self.kept_events = []

    ctx = _make_ctx(tmp_path)

    async def fake_compact(self, session_id, mode=None, **kwargs):
        self.save_summary(session_id, "fresh")
        return _Result()

    monkeypatch.setattr(
        type(ctx.session_store), "compact_session", fake_compact, raising=True
    )

    asyncio.run(execute_command("compact", [], ctx))

    events = ctx.session_store.load_transcript(ctx.session_id)
    markers = [e for e in events if e.get("type") == "context_compacted"]
    assert len(markers) == 1
    payload = markers[0]["payload"]
    assert payload["mode"] == "user"
    assert payload["model"] == "claude-sonnet-4-6"
    assert payload["tokens_saved"] == 555
    assert payload["events_summarized"] == 12
    assert payload["summary"] == "fresh"


def test_compact_runs_on_compact_hook_when_supplied(tmp_path, monkeypatch) -> None:
    """If the caller supplies a hook_manager (API path does), record_compaction
    must fire ``on_compact`` so external integrations stay informed."""
    from dataclasses import dataclass as _dc

    @_dc
    class _Result:
        summary: str = "summary-text"
        tokens_saved: int = 42
        kept_events: list = None  # type: ignore[assignment]
        model: str = "m"
        events_summarized: int = 3

        def __post_init__(self) -> None:
            if self.kept_events is None:
                self.kept_events = []

    calls: list[dict] = []

    class _Hooks:
        def run_on_compact(self, session_id, **kwargs):
            calls.append({"session_id": session_id, **kwargs})

    ctx = _make_ctx(tmp_path)
    ctx.hook_manager = _Hooks()

    async def fake_compact(self, session_id, mode=None, **kwargs):
        self.save_summary(session_id, "summary-text")
        return _Result()

    monkeypatch.setattr(
        type(ctx.session_store), "compact_session", fake_compact, raising=True
    )

    asyncio.run(execute_command("compact", [], ctx))
    assert len(calls) == 1
    assert calls[0]["session_id"] == ctx.session_id
    assert calls[0]["summary"] == "summary-text"
    assert calls[0]["tokens_saved"] == 42
    assert calls[0]["events_summarized"] == 3


def test_compact_forwards_focus_argument(tmp_path, monkeypatch) -> None:
    """``/compact <focus>`` joins all args and forwards them as ``focus_prompt``
    to ``compact_session`` so the summarizer can bias the output."""
    captured_kwargs: dict = {}

    @dataclass
    class _Result:
        summary: str = "s"
        tokens_saved: int = 0
        kept_events: list = None  # type: ignore[assignment]
        model: str = "m"
        events_summarized: int = 0

        def __post_init__(self) -> None:
            if self.kept_events is None:
                self.kept_events = []

    ctx = _make_ctx(tmp_path)

    async def fake_compact(self, session_id, mode=None, **kwargs):
        captured_kwargs.update(kwargs)
        self.save_summary(session_id, "s")
        return _Result()

    monkeypatch.setattr(
        type(ctx.session_store), "compact_session", fake_compact, raising=True
    )

    result = asyncio.run(
        execute_command("compact", ["focus on the API refactor"], ctx)
    )
    assert captured_kwargs.get("focus_prompt") == "focus on the API refactor"
    assert result.metadata["focus"] == "focus on the API refactor"


def test_compact_blank_focus_treated_as_none(tmp_path, monkeypatch) -> None:
    captured_kwargs: dict = {}

    @dataclass
    class _Result:
        summary: str = "s"
        tokens_saved: int = 0
        kept_events: list = None  # type: ignore[assignment]
        model: str = "m"
        events_summarized: int = 0

        def __post_init__(self) -> None:
            if self.kept_events is None:
                self.kept_events = []

    ctx = _make_ctx(tmp_path)

    async def fake_compact(self, session_id, mode=None, **kwargs):
        captured_kwargs.update(kwargs)
        self.save_summary(session_id, "s")
        return _Result()

    monkeypatch.setattr(
        type(ctx.session_store), "compact_session", fake_compact, raising=True
    )

    asyncio.run(execute_command("compact", ["   "], ctx))
    assert captured_kwargs.get("focus_prompt") is None
