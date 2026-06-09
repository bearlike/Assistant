"""Tests for session provenance classification."""

import pytest
from mewbo_core.session_provenance import SessionOrigin
from mewbo_core.session_runtime import SessionRuntime
from mewbo_core.session_store import SessionStore


@pytest.mark.parametrize(
    ("tags", "context", "expected"),
    [
        # Tag prefix is the primary signal (covers old wiki jobs w/ empty context).
        (["wiki:job:abc"], {}, SessionOrigin.WIKI),
        (["wiki:qa:abc"], {}, SessionOrigin.WIKI),
        (["agentic_search:scg:abc"], {}, SessionOrigin.SEARCH),
        (["agentic_search:run:abc"], {}, SessionOrigin.SEARCH),
        (["nextcloud-talk:room:tok"], {}, SessionOrigin.CHANNEL),
        (["email:thread:chan:root"], {}, SessionOrigin.CHANNEL),
        # Context fallback when no tag is present.
        ([], {"client_capabilities": ["wiki"]}, SessionOrigin.WIKI),
        ([], {"client_capabilities": ["scg"]}, SessionOrigin.SEARCH),
        ([], {"source_platform": "nextcloud-talk"}, SessionOrigin.CHANNEL),
        # Manual console sessions and the empty default.
        ([], {"client_capabilities": ["stlite"]}, SessionOrigin.USER),
        ([], {}, SessionOrigin.USER),
        # Tag wins over a conflicting context capability.
        (["wiki:job:abc"], {"client_capabilities": ["stlite"]}, SessionOrigin.WIKI),
    ],
)
def test_classify(tags, context, expected):
    """classify maps tags + context to the right coarse origin."""
    assert SessionOrigin.classify(tags, context) == expected


def test_tags_for_session_round_trip(tmp_path):
    """tags_for_session is the reverse of resolve_tag and returns every match."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    store.tag_session(session_id, "wiki:job:1")
    store.tag_session(session_id, "extra-label")
    store.tag_session(store.create_session(), "other:room:x")
    assert sorted(store.tags_for_session(session_id)) == ["extra-label", "wiki:job:1"]


def test_summarize_session_sets_origin(tmp_path):
    """summarize_session classifies a tagged wiki session and a plain one."""
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)

    wiki_id = runtime.resolve_session(session_tag="wiki:job:42")
    store.append_event(wiki_id, {"type": "user", "payload": {"text": "index"}})
    assert runtime.summarize_session(wiki_id)["origin"] == "wiki"

    user_id = runtime.resolve_session()
    store.append_event(user_id, {"type": "user", "payload": {"text": "hi"}})
    assert runtime.summarize_session(user_id)["origin"] == "user"
