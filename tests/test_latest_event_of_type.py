#!/usr/bin/env python3
"""``latest_event_of_type`` must be bounded by the TYPE, not by a window.

The primitive exists because the alternative — a caller wanting ONE event out
of a session's tail reading the whole transcript to get it — costs 10,296
documents and 14.5 MB to produce a single project name on the deployed store's
largest session.

Two properties are load-bearing and neither is visible in a diff:

**The bound is the type, never a count.** A tail scan of the last N events is
the tempting cheap answer. It is not merely slower to get right — it is WRONG,
because the newest event of a sparse type sits arbitrarily far back in a session
that has run since. A count-bounded implementation returns ``None`` and the
caller renders that as "this session has no project", which is a refusal, not a
delay. ``test_survives_a_tail_longer_than_any_window`` is the case that
matters: it buries the match under more events than any plausible window.

**The two drivers must agree.** The base tests each event in Python and Mongo
restates the same rule in query language, so they can only be held together by
running one corpus through both — reading one and reasoning about the other is
exactly how they diverge.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import mongomock
import pytest
from mewbo_core.contracts.types import EventRecord
from mewbo_core.session.session_store import SessionStore, SessionStoreBase
from mewbo_core.session.session_store_mongo import MongoSessionStore

#: Comfortably past any window a tail scan would plausibly pick (the existing
#: ``load_recent_events`` default is 8; the run sweep uses tens).
TAIL_LENGTH = 512


@pytest.fixture(params=["json", "mongo"])
def store(request: pytest.FixtureRequest, tmp_path: Any) -> SessionStoreBase:
    """One corpus, both drivers — the only way the two spellings stay honest."""
    if request.param == "json":
        return SessionStore(root_dir=str(tmp_path))
    with patch(
        "mewbo_core.session.session_store_mongo.MongoClient",
        mongomock.MongoClient,
    ):
        return MongoSessionStore(
            root_dir=str(tmp_path),
            uri="mongodb://localhost:27017",
            database="test_latest_event",
        )


def _append_filler(store: SessionStoreBase, session_id: str, count: int) -> None:
    """Append *count* events of a type the reads under test never match."""
    for index in range(count):
        store.append_event(session_id, {"type": "assistant", "payload": {"text": str(index)}})


def test_returns_the_newest_matching_event(store: SessionStoreBase) -> None:
    session_id = store.create_session()
    store.append_event(session_id, {"type": "context", "payload": {"project": "older"}})
    _append_filler(store, session_id, 3)
    store.append_event(session_id, {"type": "context", "payload": {"project": "newer"}})

    event = store.latest_event_of_type(session_id, "context")
    assert event is not None
    assert event["payload"]["project"] == "newer"


def test_returns_the_whole_event_not_a_field(store: SessionStoreBase) -> None:
    """A second caller wants a different key off the same read, so return the event."""
    session_id = store.create_session()
    store.append_event(
        session_id,
        {"type": "context", "payload": {"project": "demo", "cwd": "/srv/demo"}},
    )

    event = store.latest_event_of_type(session_id, "context")
    assert event is not None
    assert event["type"] == "context"
    assert event["payload"]["cwd"] == "/srv/demo"
    assert "ts" in event


def test_none_when_no_event_of_that_type(store: SessionStoreBase) -> None:
    session_id = store.create_session()
    _append_filler(store, session_id, 4)
    assert store.latest_event_of_type(session_id, "context") is None


def test_none_for_a_session_with_no_events(store: SessionStoreBase) -> None:
    session_id = store.create_session()
    assert store.latest_event_of_type(session_id, "context") is None


def test_survives_a_tail_longer_than_any_window(store: SessionStoreBase) -> None:
    """A count-bounded tail scan returns ``None`` here.

    The match is buried under ``TAIL_LENGTH`` newer events, which is what a long
    run since the last re-engagement looks like. An implementation bounded by a
    window instead of by the type reports "no context event" — a wrong answer
    that the IDE launch path turns into a ``409`` on a perfectly launchable
    session.
    """
    session_id = store.create_session()
    store.append_event(session_id, {"type": "context", "payload": {"project": "buried"}})
    _append_filler(store, session_id, TAIL_LENGTH)

    event = store.latest_event_of_type(session_id, "context")
    assert event is not None, "the match was bounded away by a window instead of by the type"
    assert event["payload"]["project"] == "buried"


def test_payload_key_skips_a_newer_event_that_omits_it(store: SessionStoreBase) -> None:
    """Context events merge key-by-key, so the newest one need not carry the key.

    Measured on the deployed store: 15 of the 204 sessions holding a ``project``
    anywhere in context had a newer context event without one. Without this
    narrowing the caller reads ``None`` and refuses those sessions.
    """
    session_id = store.create_session()
    store.append_event(session_id, {"type": "context", "payload": {"project": "demo"}})
    store.append_event(session_id, {"type": "context", "payload": {"client_capabilities": ["x"]}})

    assert store.latest_event_of_type(session_id, "context")["payload"].get("project") is None

    event = store.latest_event_of_type(session_id, "context", payload_key="project")
    assert event is not None
    assert event["payload"]["project"] == "demo"


@pytest.mark.parametrize("value", [None, ""])
def test_payload_key_treats_null_and_empty_as_unset(
    store: SessionStoreBase, value: object
) -> None:
    """Both drivers must agree on which values count as present.

    The base tests this in Python and Mongo pushes ``$nin: [None, ""]`` down;
    running the same corpus through both is what keeps the two from drifting.
    """
    session_id = store.create_session()
    store.append_event(session_id, {"type": "context", "payload": {"project": "demo"}})
    store.append_event(session_id, {"type": "context", "payload": {"project": value}})

    event = store.latest_event_of_type(session_id, "context", payload_key="project")
    assert event is not None
    assert event["payload"]["project"] == "demo"


def test_payload_key_treats_an_empty_collection_as_SET(store: SessionStoreBase) -> None:
    """An empty list is a DECLARATION of none, not an absent key.

    The distinction is load-bearing for any caller reading a declaration back:
    ``device_tools: []`` is a client saying it advertises nothing, which must
    de-register, while a context event silent on the key says nothing at all and
    must not. ``payload_key_is_set`` deliberately stops at "not ``None``, not the
    empty string" and leaves what counts as USABLE to the caller — so both
    drivers have to agree that ``[]`` clears the bar, and Mongo's ``$nin`` is a
    separate spelling of the same rule.
    """
    session_id = store.create_session()
    store.append_event(session_id, {"type": "context", "payload": {"device_tools": ["a"]}})
    store.append_event(session_id, {"type": "context", "payload": {"device_tools": []}})

    event = store.latest_event_of_type(session_id, "context", payload_key="device_tools")
    assert event is not None
    assert event["payload"]["device_tools"] == []


def test_payload_key_none_when_no_event_carries_it(store: SessionStoreBase) -> None:
    session_id = store.create_session()
    store.append_event(session_id, {"type": "context", "payload": {"client_capabilities": []}})
    assert store.latest_event_of_type(session_id, "context", payload_key="project") is None


def test_other_sessions_are_not_read(store: SessionStoreBase) -> None:
    """The session filter is part of the query, not something a caller re-checks."""
    other = store.create_session()
    store.append_event(other, {"type": "context", "payload": {"project": "not-mine"}})
    session_id = store.create_session()
    store.append_event(session_id, {"type": "context", "payload": {"project": "mine"}})

    event = store.latest_event_of_type(other, "context")
    assert event is not None
    assert event["payload"]["project"] == "not-mine"


class _CountingStore(SessionStore):
    """Real JSON store that tallies which read primitive the base template used.

    Counting store reads is exact and names the defect in its own assertion;
    a wall-clock assertion would measure the machine and flake under a parallel
    suite (``session/CLAUDE.md`` -> "Asserting cost in a test").
    """

    def __init__(self, root_dir: str) -> None:
        super().__init__(root_dir=root_dir)
        self.load_transcript_calls = 0
        self.stream_transcript_calls = 0

    def load_transcript(self, session_id: str) -> list[EventRecord]:
        self.load_transcript_calls += 1
        return super().load_transcript(session_id)

    def stream_transcript(self, session_id: str) -> Iterator[EventRecord]:
        self.stream_transcript_calls += 1
        yield from super().stream_transcript(session_id)


def test_base_streams_rather_than_materialising(tmp_path: Any) -> None:
    """The base is ``O(one session)`` in TIME and ``O(1)`` in MEMORY.

    It has no reverse read, so it cannot avoid walking the session — but it goes
    through ``stream_transcript`` and retains one event, so a line-oriented
    backend does not have to buffer a 10k-event transcript to answer a question
    about one of them. ``load_transcript`` is the materialising sibling; a single
    call to it here would mean the primitive holds the whole session in memory.
    """
    store = _CountingStore(str(tmp_path))
    session_id = store.create_session()
    store.append_event(session_id, {"type": "context", "payload": {"project": "demo"}})
    _append_filler(store, session_id, 8)
    store.load_transcript_calls = 0
    store.stream_transcript_calls = 0

    assert store.latest_event_of_type(session_id, "context") is not None
    assert store.stream_transcript_calls == 1, "the base must make exactly one pass"
    assert store.load_transcript_calls == 0, "the base must not materialise the transcript"
