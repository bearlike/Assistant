"""Tests for session store persistence helpers."""
from core.compaction import should_compact, summarize_events
from core.session_store import SessionStore


def test_session_store_roundtrip(tmp_path):
    """Persist events and summaries in the session store."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()

    store.append_event(session_id, {"type": "user", "payload": {"text": "hello"}})
    store.append_event(session_id, {"type": "tool_result", "payload": {"text": "ok"}})

    events = store.load_transcript(session_id)
    assert len(events) == 2
    assert events[0]["type"] == "user"

    store.save_summary(session_id, "summary text")
    assert store.load_summary(session_id) == "summary text"


def test_session_store_tag_and_fork(tmp_path):
    """Tag sessions and fork transcripts for new sessions."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "hello"}})

    store.tag_session(session_id, "primary")
    assert store.resolve_tag("primary") == session_id

    forked = store.fork_session(session_id)
    assert forked != session_id
    assert store.load_transcript(forked)


def test_compaction_helpers():
    """Verify compaction helpers summarize and detect thresholds."""
    events = [{"type": "user", "payload": {"text": "hello"}}]
    summary = summarize_events(events)
    assert "user" in summary
    assert should_compact(events, threshold=1) is True
