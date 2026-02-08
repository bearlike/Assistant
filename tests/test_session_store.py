"""Tests for session store persistence helpers."""

import os
import shutil

from meeseeks_core.compaction import should_compact, summarize_events
from meeseeks_core.session_store import SessionStore


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


def test_session_store_recent_events_and_filters(tmp_path):
    """Filter recent events by type and respect zero limits."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "hello"}})
    store.append_event(session_id, {"type": "assistant", "payload": {"text": "hi"}})
    store.append_event(session_id, {"type": "tool_result", "payload": {"text": "ok"}})

    assert store.load_recent_events(session_id, limit=0) == []
    filtered = store.load_recent_events(session_id, limit=5, include_types={"tool_result"})
    assert len(filtered) == 1
    assert filtered[0]["type"] == "tool_result"


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


def test_session_store_load_transcript_skips_bad_lines(tmp_path):
    """Skip malformed transcript lines without failing."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    paths = store._paths(session_id)
    paths.session_dir and paths.transcript_path  # touch for coverage
    with open(paths.transcript_path, "w", encoding="utf-8") as handle:
        handle.write("{invalid}\n")
        handle.write('{"type": "user", "payload": {"text": "ok"}, "ts": "1"}\n')
    events = store.load_transcript(session_id)
    assert len(events) == 1
    assert events[0]["type"] == "user"


def test_session_store_list_sessions_missing_root(tmp_path):
    """Return empty list when session root is missing."""
    store = SessionStore(root_dir=str(tmp_path))
    root = store.root_dir
    if os.path.exists(root):
        shutil.rmtree(root)
    assert store.list_sessions() == []


def test_session_store_list_tags(tmp_path):
    """List stored tags for sessions."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.tag_session(session_id, "primary")
    tags = store.list_tags()
    assert tags["primary"] == session_id


def test_session_store_archive_roundtrip(tmp_path):
    """Archive and unarchive sessions."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    assert store.is_archived(session_id) is False
    store.archive_session(session_id)
    assert store.is_archived(session_id) is True
    store.unarchive_session(session_id)
    assert store.is_archived(session_id) is False


def test_compaction_helpers():
    """Verify compaction helpers summarize and detect thresholds."""
    events = [{"type": "user", "payload": {"text": "hello"}}]
    summary = summarize_events(events)
    assert "user" in summary
    assert should_compact(events, threshold=1) is True
