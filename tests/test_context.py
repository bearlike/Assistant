"""Tests for context selection helpers."""

import types

from meeseeks_core.config import set_config_override
from meeseeks_core.context import ContextBuilder, event_payload_text, render_event_lines
from meeseeks_core.session_store import SessionStore


def _seed_long_session(store: SessionStore, *, original_task: str, extra_tool_results: int) -> str:
    """Create a transcript where the first user event predates ``recent_event_limit``
    trailing tool_results — the shape that previously dropped the original task.
    """
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": original_task}})
    for idx in range(extra_tool_results):
        store.append_event(
            session_id,
            {
                "type": "tool_result",
                "payload": {
                    "tool_id": "read_file",
                    "operation": "read",
                    "tool_input": f"file_{idx}.py",
                    "result": f"contents {idx}",
                },
            },
        )
    return session_id


def test_event_payload_text_variants():
    """Format payloads with and without dict structures."""
    assert event_payload_text({"type": "user", "payload": "hello"}) == "hello"
    text = event_payload_text(
        {"type": "tool_result", "payload": {"tool_input": {"a": 1}, "result": "ok"}}
    )
    assert "ok" in text
    fallback = event_payload_text({"type": "tool_result", "payload": {"foo": "bar"}})
    assert "foo" in fallback


def test_render_event_lines_skips_empty_payloads():
    """Skip empty event payload text in rendered lines."""
    events = [
        {"type": "user", "payload": ""},
        {"type": "assistant", "payload": {"text": "hi"}},
    ]
    rendered = render_event_lines(events)
    assert "assistant" in rendered
    assert "user" not in rendered


def test_select_context_events_empty_list(tmp_path):
    """Return empty list when there are no events to select."""
    builder = ContextBuilder(SessionStore(root_dir=str(tmp_path)))
    assert builder._select_context_events([], "query", "model") == []


def test_select_context_events_keep_ids(monkeypatch, tmp_path):
    """Return only events selected by the model."""
    selection = types.SimpleNamespace(keep_ids=[2], drop_ids=[])

    class DummyChain:
        def __init__(self, result):
            self._result = result

        def __or__(self, _other):
            return self

        def invoke(self, *_args, **_kwargs):
            return self._result

    class DummyPrompt:
        def __init__(self, result):
            self._result = result

        def __or__(self, _other):
            return DummyChain(self._result)

    monkeypatch.setattr(
        "meeseeks_core.context.ChatPromptTemplate", lambda *args, **kwargs: DummyPrompt(selection)
    )
    monkeypatch.setattr("meeseeks_core.context.build_chat_model", lambda **_k: object())
    builder = ContextBuilder(SessionStore(root_dir=str(tmp_path)))
    events = [
        {"type": "user", "payload": {"text": "one"}},
        {"type": "tool_result", "payload": {"text": "two"}},
        {"type": "assistant", "payload": {"text": "three"}},
    ]
    selected = builder._select_context_events(events, "query", "model")
    assert selected == [events[1]]


def test_select_context_events_empty_keep_ids(monkeypatch, tmp_path):
    """Fallback to the last three events when keep_ids is empty."""
    selection = types.SimpleNamespace(keep_ids=[], drop_ids=[])

    class DummyChain:
        def __init__(self, result):
            self._result = result

        def __or__(self, _other):
            return self

        def invoke(self, *_args, **_kwargs):
            return self._result

    class DummyPrompt:
        def __init__(self, result):
            self._result = result

        def __or__(self, _other):
            return DummyChain(self._result)

    monkeypatch.setattr(
        "meeseeks_core.context.ChatPromptTemplate", lambda *args, **kwargs: DummyPrompt(selection)
    )
    monkeypatch.setattr("meeseeks_core.context.build_chat_model", lambda **_k: object())
    builder = ContextBuilder(SessionStore(root_dir=str(tmp_path)))
    events = [
        {"type": "user", "payload": {"text": "one"}},
        {"type": "tool_result", "payload": {"text": "two"}},
        {"type": "assistant", "payload": {"text": "three"}},
        {"type": "assistant", "payload": {"text": "four"}},
    ]
    selected = builder._select_context_events(events, "query", "model")
    assert selected == events[-3:]


def test_select_context_events_empty_candidates(monkeypatch, tmp_path):
    """Return original events when there are no candidate lines."""
    selection = types.SimpleNamespace(keep_ids=[1], drop_ids=[])

    class DummyChain:
        def __init__(self, result):
            self._result = result

        def __or__(self, _other):
            return self

        def invoke(self, *_args, **_kwargs):
            return self._result

    class DummyPrompt:
        def __init__(self, result):
            self._result = result

        def __or__(self, _other):
            return DummyChain(self._result)

    monkeypatch.setattr(
        "meeseeks_core.context.ChatPromptTemplate", lambda *args, **kwargs: DummyPrompt(selection)
    )
    monkeypatch.setattr("meeseeks_core.context.build_chat_model", lambda **_k: object())
    builder = ContextBuilder(SessionStore(root_dir=str(tmp_path)))
    events = [
        {"type": "user", "payload": ""},
        {"type": "assistant", "payload": ""},
    ]
    selected = builder._select_context_events(events, "query", "model")
    assert selected == events


def test_select_context_events_without_model(monkeypatch, tmp_path):
    """Return events when no selector model is configured."""
    set_config_override(
        {
            "context": {"context_selector_model": ""},
            "llm": {"default_model": "", "action_plan_model": ""},
        }
    )
    builder = ContextBuilder(SessionStore(root_dir=str(tmp_path)))
    events = [{"type": "user", "payload": {"text": "one"}}]
    selected = builder._select_context_events(events, "query", None)
    assert selected == events


# ---------------------------------------------------------------------------
# First-user anchor — continuity for long sessions
# ---------------------------------------------------------------------------


def test_context_builder_anchors_first_user_on_long_transcript(tmp_path):
    """Long transcripts keep the original user task anchored in recent_events.

    Without the anchor, FIFO eviction (recent_event_limit) drops the
    original task before the recovery turn runs — breaking continuity.
    """
    set_config_override(
        {"context": {"recent_event_limit": 4, "selection_enabled": False}, "llm": {}}
    )
    store = SessionStore(root_dir=str(tmp_path))
    builder = ContextBuilder(store)
    original_task = "build a KISS-compliant auth layer"
    session_id = _seed_long_session(store, original_task=original_task, extra_tool_results=12)

    snapshot = builder.build(session_id, "continue recovery", model_name=None)

    assert snapshot.recent_events, "recent_events must not be empty"
    first = snapshot.recent_events[0]
    assert first.get("type") == "user"
    assert first.get("payload", {}).get("text") == original_task
    # Anchor plus recent tool_results == recent_limit + 1.
    assert len(snapshot.recent_events) == 5


def test_context_builder_no_duplicate_when_first_user_is_recent(tmp_path):
    """Short transcripts do not duplicate the first user event."""
    set_config_override(
        {"context": {"recent_event_limit": 8, "selection_enabled": False}, "llm": {}}
    )
    store = SessionStore(root_dir=str(tmp_path))
    builder = ContextBuilder(store)
    session_id = _seed_long_session(store, original_task="short task", extra_tool_results=3)

    snapshot = builder.build(session_id, "continue", model_name=None)

    user_events = [e for e in snapshot.recent_events if e.get("type") == "user"]
    assert len(user_events) == 1
    # No prefix duplication — the first entry is still the original user event.
    assert snapshot.recent_events[0]["payload"]["text"] == "short task"


def test_context_builder_anchor_survives_filtered_event_types(tmp_path):
    """Non-context event types (permission, sub_agent, …) do not displace the anchor."""
    set_config_override(
        {"context": {"recent_event_limit": 3, "selection_enabled": False}, "llm": {}}
    )
    store = SessionStore(root_dir=str(tmp_path))
    builder = ContextBuilder(store)
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "the real task"}})
    # Non-context event types are filtered out by ContextBuilder — they must
    # not displace the anchor nor inflate recent_events.
    for _ in range(10):
        store.append_event(
            session_id, {"type": "llm_call_end", "payload": {"agent_id": "a", "step": 1}}
        )
    for idx in range(5):
        store.append_event(
            session_id,
            {
                "type": "tool_result",
                "payload": {
                    "tool_id": "read_file",
                    "operation": "read",
                    "tool_input": f"f{idx}",
                    "result": "x",
                },
            },
        )

    snapshot = builder.build(session_id, "recover", model_name=None)

    assert snapshot.recent_events[0].get("payload", {}).get("text") == "the real task"
    # Anchor (1) + recent_limit (3) = 4 entries.
    assert len(snapshot.recent_events) == 4


def test_recovery_continue_rendered_context_contains_original_task(tmp_path):
    """End-to-end: after recovery, the rendered system-prompt context bullet
    list includes the original user task.

    This is the regression test for the ccb8974d… session failure: long
    transcripts triggered a recovery turn whose system prompt dropped the
    original task. The anchor keeps it visible.
    """
    set_config_override(
        {"context": {"recent_event_limit": 4, "selection_enabled": False}, "llm": {}}
    )
    from meeseeks_core.session_runtime import SessionRuntime

    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    original_task = "restructure the Meeseeks landing page layout"
    session_id = _seed_long_session(store, original_task=original_task, extra_tool_results=20)
    store.append_event(
        session_id,
        {"type": "completion", "payload": {"done": True, "done_reason": "error"}},
    )

    query = runtime.resolve_recovery_query(session_id, "continue")
    snapshot = ContextBuilder(store).build(session_id, query, model_name=None)
    rendered = render_event_lines(snapshot.recent_events)

    assert original_task in rendered, (
        f"recovery turn lost the original task from context; rendered:\n{rendered}"
    )
