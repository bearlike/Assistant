"""Tests for shared session runtime helpers."""

import time

import pytest
from meeseeks_core.session_runtime import SessionRuntime, parse_core_command
from meeseeks_core.session_store import SessionStore


def test_parse_core_command():
    """Detect supported core commands."""
    assert parse_core_command("/compact") == "/compact"
    assert parse_core_command("/terminate now") == "/terminate"
    assert parse_core_command("/status") == "/status"
    assert parse_core_command("/unknown") is None


def test_runtime_resolve_and_summarize(tmp_path):
    """Resolve sessions and return summaries."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session(session_tag="primary")
    assert store.resolve_tag("primary") == session_id
    runtime.session_store.append_event(session_id, {"type": "user", "payload": {"text": "hello"}})
    summary = runtime.summarize_session(session_id)
    assert summary["session_id"] == session_id
    assert summary["title"] == "hello"


def test_runtime_summarize_prefers_stored_title(tmp_path):
    """summarize_session prefers a stored title over first-user-message."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    runtime.session_store.append_event(session_id, {"type": "user", "payload": {"text": "hello"}})
    store.save_title(session_id, "curated title")
    summary = runtime.summarize_session(session_id)
    assert summary["title"] == "curated title"


def test_runtime_summarize_title_fallback_to_session_id(tmp_path):
    """summarize_session falls back to Session {id[:8]} when no user/title."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    # Only a context event, no user event
    runtime.session_store.append_event(session_id, {"type": "context", "payload": {}})
    summary = runtime.summarize_session(session_id)
    assert summary["title"] == f"Session {session_id[:8]}"


def test_runtime_load_events_filters_by_after(tmp_path):
    """Filter events by timestamp when loading."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    runtime.session_store.append_event(session_id, {"type": "user", "payload": {"text": "one"}})
    time.sleep(0.002)
    runtime.session_store.append_event(session_id, {"type": "user", "payload": {"text": "two"}})
    events = runtime.load_events(session_id)
    after = events[0]["ts"]
    filtered = runtime.load_events(session_id, after)
    assert len(filtered) == 1
    assert filtered[0]["payload"]["text"] == "two"


def test_runtime_start_async_and_cancel(tmp_path):
    """Start and cancel an async run."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)

    def fake_run_sync(*, session_id, user_query, should_cancel=None, **_kwargs):
        runtime.session_store.append_event(
            session_id, {"type": "user", "payload": {"text": user_query}}
        )
        while should_cancel and not should_cancel():
            time.sleep(0.01)
        runtime.session_store.append_event(
            session_id,
            {
                "type": "completion",
                "payload": {"done": True, "done_reason": "canceled", "task_result": None},
            },
        )

    runtime.run_sync = fake_run_sync
    session_id = runtime.resolve_session()
    assert runtime.start_async(session_id=session_id, user_query="hello") is True
    assert runtime.is_running(session_id) is True
    assert runtime.cancel(session_id) is True

    deadline = time.time() + 2.0
    while time.time() < deadline:
        if not runtime.is_running(session_id):
            break
        time.sleep(0.01)
    assert runtime.is_running(session_id) is False


def test_runtime_list_sessions_skips_empty(tmp_path):
    """Exclude sessions with no events from list output."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    empty_session = store.create_session()
    store.append_event(empty_session, {"type": "session", "payload": {"event": "created"}})
    filled_session = store.create_session()
    store.append_event(filled_session, {"type": "user", "payload": {"text": "hello"}})

    sessions = runtime.list_sessions()
    session_ids = {session["session_id"] for session in sessions}
    assert filled_session in session_ids
    assert empty_session not in session_ids


def test_runtime_list_sessions_filters_archived(tmp_path):
    """Filter archived sessions unless requested."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    active_session = store.create_session()
    archived_session = store.create_session()
    store.append_event(active_session, {"type": "user", "payload": {"text": "hello"}})
    store.append_event(archived_session, {"type": "user", "payload": {"text": "bye"}})
    store.archive_session(archived_session)

    sessions = runtime.list_sessions()
    session_ids = {session["session_id"] for session in sessions}
    assert active_session in session_ids
    assert archived_session not in session_ids

    sessions_with_archived = runtime.list_sessions(include_archived=True)
    all_ids = {session["session_id"] for session in sessions_with_archived}
    assert archived_session in all_ids


# ---------------------------------------------------------------------------
# resolve_recovery_query (Fix E — retry/continue recovery primitive)
# ---------------------------------------------------------------------------


def test_resolve_recovery_query_retry_truncates_failed_turn(tmp_path):
    """``retry`` deletes the failed turn so the session looks like it
    ended right before the failed user message was sent. The returned
    text is the failed user's original query, ready for a fresh run.
    """
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "first"}})
    store.append_event(
        session_id,
        {
            "type": "completion",
            "payload": {"done": True, "done_reason": "completed", "task_result": "ok"},
        },
    )
    store.append_event(session_id, {"type": "user", "payload": {"text": "second"}})
    store.append_event(
        session_id,
        {"type": "tool_result", "payload": {"tool_id": "x", "operation": "get",
                                             "tool_input": "", "result": "ok"}},
    )
    store.append_event(
        session_id,
        {
            "type": "completion",
            "payload": {
                "done": True,
                "done_reason": "error",
                "task_result": None,
                "error": "boom",
            },
        },
    )

    query = runtime.resolve_recovery_query(session_id, "retry")
    assert query == "second"
    # The failed turn (user "second" + tool_result + completion) must be gone.
    # Only the first successful turn (user "first" + completion) remains.
    transcript = store.load_transcript(session_id)
    types = [e["type"] for e in transcript]
    assert "user" in types
    user_events = [e for e in transcript if e["type"] == "user"]
    assert len(user_events) == 1
    assert user_events[0]["payload"]["text"] == "first"
    # No recovery audit event — retry deletes, doesn't append.
    assert not any(e["type"] == "recovery" for e in transcript)


def test_resolve_recovery_query_continue_generic_prompt(tmp_path):
    """``continue`` returns a generic recovery prompt without embedding the original task.

    The original task is already in conversation context (directly or via
    compaction summary). Repeating it verbatim wastes tokens.
    """
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    original_task = "build a KISS-compliant auth layer"
    store.append_event(
        session_id, {"type": "user", "payload": {"text": original_task}}
    )

    query = runtime.resolve_recovery_query(session_id, "continue")
    assert "interrupted" in query.lower()
    assert original_task not in query  # task is NOT re-embedded; already in context
    assert "continue from where you left off" in query.lower()
    transcript = store.load_transcript(session_id)
    recovery_events = [e for e in transcript if e.get("type") == "recovery"]
    assert recovery_events[-1]["payload"] == {"action": "continue"}


def test_resolve_recovery_query_continue_falls_back_without_original(tmp_path):
    """Empty original text ⇒ generic recovery prompt (no crash)."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    # Whitespace-only original user event — retry would refuse this but
    # continue must degrade gracefully to a generic recovery prompt.
    store.append_event(
        session_id, {"type": "user", "payload": {"text": "   "}}
    )

    query = runtime.resolve_recovery_query(session_id, "continue")
    assert "interrupted" in query.lower()
    assert "continue from where you left off" in query.lower()


def test_resolve_recovery_query_rejects_unknown_action(tmp_path):
    """Bad action raises ValueError — nothing appended."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})

    with pytest.raises(ValueError, match="unknown recovery action"):
        runtime.resolve_recovery_query(session_id, "nuke")
    # No recovery event appended on failure.
    assert not any(
        e.get("type") == "recovery" for e in store.load_transcript(session_id)
    )


def test_resolve_recovery_query_rejects_session_with_no_user_message(tmp_path):
    """No prior user event ⇒ ValueError."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "context", "payload": {"x": 1}})

    with pytest.raises(ValueError, match="no prior user message"):
        runtime.resolve_recovery_query(session_id, "retry")


def test_resolve_recovery_query_refuses_running_session(tmp_path):
    """Attempting recovery on a running session raises RuntimeError."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)

    def fake_run_sync(*, session_id, user_query, should_cancel=None, **_kwargs):
        while should_cancel and not should_cancel():
            time.sleep(0.01)

    runtime.run_sync = fake_run_sync
    session_id = runtime.resolve_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})
    runtime.start_async(session_id=session_id, user_query="hi")
    try:
        with pytest.raises(RuntimeError, match="running"):
            runtime.resolve_recovery_query(session_id, "retry")
    finally:
        runtime.cancel(session_id)
        deadline = time.time() + 2.0
        while time.time() < deadline and runtime.is_running(session_id):
            time.sleep(0.01)
