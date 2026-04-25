"""Tests for session store persistence helpers."""

import os
import shutil
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from truss_core.config import StorageConfig
from truss_core.session_store import SessionStore, SessionStoreBase, create_session_store


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


def test_create_session_store_default_json(tmp_path):
    """Factory returns SessionStore (json) when no driver is configured."""
    with patch("truss_core.session_store.get_config_value", return_value="json"):
        store = create_session_store(root_dir=str(tmp_path))
    assert isinstance(store, SessionStore)
    assert isinstance(store, SessionStoreBase)


def test_create_session_store_mongodb(tmp_path):
    """Factory returns MongoSessionStore when driver is 'mongodb'."""
    import mongomock
    from truss_core.session_store_mongo import MongoSessionStore

    with (
        patch("truss_core.session_store.get_config_value", return_value="mongodb"),
        patch("truss_core.session_store_mongo.MongoClient", mongomock.MongoClient),
    ):
        store = create_session_store(root_dir=str(tmp_path))
    assert isinstance(store, MongoSessionStore)
    assert isinstance(store, SessionStoreBase)


def test_base_class_template_fork(tmp_path):
    """Verify fork_session works through the base class template method."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "hello"}})
    store.save_summary(session_id, "summary text")
    store.save_title(session_id, "my title")

    forked_id = store.fork_session(session_id)
    assert forked_id != session_id
    events = store.load_transcript(forked_id)
    assert len(events) == 1
    assert store.load_summary(forked_id) == "summary text"
    assert store.load_title(forked_id) == "my title"


def test_fork_session_at(tmp_path):
    """Fork only events up to cutoff_ts and clear stale summary."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"text": "q1"}})
    store.append_event(session_id, {"type": "assistant", "payload": {"text": "a1"}})
    store.append_event(session_id, {"type": "user", "payload": {"text": "q2"}})
    store.append_event(session_id, {"type": "assistant", "payload": {"text": "a2"}})
    store.save_summary(session_id, "full session summary")
    store.save_title(session_id, "my title")

    events = store.load_transcript(session_id)
    assert len(events) == 4
    # Fork at the first assistant response (keep first 2 events)
    cutoff_ts = events[1]["ts"]
    forked_id = store.fork_session_at(session_id, cutoff_ts)

    assert forked_id != session_id
    forked_events = store.load_transcript(forked_id)
    assert len(forked_events) == 2
    assert forked_events[0]["payload"]["text"] == "q1"
    assert forked_events[1]["payload"]["text"] == "a1"
    # Summary should be cleared (stale after truncation)
    assert store.load_summary(forked_id) == ""
    # Title is preserved
    assert store.load_title(forked_id) == "my title"
    # Source session is unmodified
    assert len(store.load_transcript(session_id)) == 4
    assert store.load_summary(session_id) == "full session summary"


def test_session_store_title_roundtrip(tmp_path):
    """Persist and reload session titles."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    assert store.load_title(session_id) is None
    store.save_title(session_id, "a concise title")
    assert store.load_title(session_id) == "a concise title"
    # Overwrite semantics
    store.save_title(session_id, "edited title")
    assert store.load_title(session_id) == "edited title"


def test_session_store_load_title_missing(tmp_path):
    """Return None when no title was ever saved."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    assert store.load_title(session_id) is None


def test_session_store_load_title_empty_string(tmp_path):
    """Treat an empty stored title as absent."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.save_title(session_id, "")
    assert store.load_title(session_id) is None


def test_unknown_storage_driver_raises():
    """Unknown storage driver should raise, not silently fall back to json."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("TRUSS_STORAGE_DRIVER", None)
        with pytest.raises(ValidationError, match="Unknown storage driver"):
            StorageConfig(driver="postgres")


def test_create_session_store_mongodb_unreachable(tmp_path):
    """Factory raises RuntimeError when MongoDB is unreachable."""
    with (
        patch("truss_core.session_store.get_config_value", return_value="mongodb"),
        patch(
            "truss_core.session_store_mongo.MongoClient",
            side_effect=Exception("connection refused"),
        ),
    ):
        with pytest.raises(RuntimeError, match="not available"):
            create_session_store(root_dir=str(tmp_path))
