"""Tests for shared session runtime helpers."""

import time

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

    runtime.run_sync = fake_run_sync  # type: ignore[method-assign]
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
