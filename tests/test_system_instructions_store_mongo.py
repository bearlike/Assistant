"""Tests for MongoSystemInstructionsStore (mewbo_core.system_instructions.store_mongo).

Backed by mongomock, following the tests/test_triggers_store_mongo.py pattern:
patch MongoClient at the module import site before constructing the store. This
is the direct Mongo-driver mirror of TestJsonSystemInstructionsStore in
tests/test_system_instructions.py — same contract, different backend, so a
regression in the driver that ships to production (storage.driver=mongodb) no
longer hides behind "only the JSON store is exercised".
"""

from __future__ import annotations

from unittest.mock import patch

import mongomock
import pytest
from mewbo_core.system_instructions.spec import GLOBAL_INSTRUCTIONS_ID, SystemInstructionsDoc
from mewbo_core.system_instructions.store import SystemInstructionsStoreBase
from mewbo_core.system_instructions.store_mongo import MongoSystemInstructionsStore


@pytest.fixture
def store():
    """A MongoSystemInstructionsStore backed by mongomock."""
    with patch("mewbo_core.system_instructions.store_mongo.MongoClient", mongomock.MongoClient):
        return MongoSystemInstructionsStore(
            uri="mongodb://localhost:27017", database="test_system_instructions"
        )


def test_isinstance(store):
    """MongoSystemInstructionsStore satisfies SystemInstructionsStoreBase."""
    assert isinstance(store, SystemInstructionsStoreBase)


def test_get_missing_returns_none(store):
    """get() of an absent document returns None, not a default-constructed doc."""
    assert store.get() is None
    assert store.get("nope") is None


def test_put_then_get_round_trips_every_field(store):
    """put() persists; get() recovers every field, not just the template."""
    doc = SystemInstructionsDoc(
        id=GLOBAL_INSTRUCTIONS_ID,
        template="Hello {{ surface }}.",
        enabled=False,
        last_error="a prior render error",
    )

    stamped = store.put(doc)
    fetched = store.get()

    assert fetched is not None
    assert fetched.id == doc.id
    assert fetched.template == doc.template
    assert fetched.enabled == doc.enabled
    assert fetched.last_error == doc.last_error
    # updated_at is server-stamped fresh on every put(), not the caller's value.
    assert fetched.updated_at == stamped.updated_at
    assert fetched.updated_at


def test_put_stamps_updated_at_fresh(store):
    """put() overwrites a caller-supplied updated_at with its own stamp."""
    doc = SystemInstructionsDoc(template="hi", updated_at="2020-01-01T00:00:00+00:00")

    stamped = store.put(doc)

    assert stamped.updated_at != "2020-01-01T00:00:00+00:00"
    assert store.get().updated_at == stamped.updated_at


def test_put_is_an_upsert_overwrites_not_duplicates(store):
    """A second put() on the same id replaces the document, never inserts a sibling."""
    store.put(SystemInstructionsDoc(id=GLOBAL_INSTRUCTIONS_ID, template="v1"))
    store.put(SystemInstructionsDoc(id=GLOBAL_INSTRUCTIONS_ID, template="v2"))

    assert store._col.count_documents({}) == 1
    assert store.get().template == "v2"


def test_record_error_sets_last_error_without_clobbering_the_template(store):
    """record_error() (inherited template method) is a targeted field update."""
    store.put(SystemInstructionsDoc(template="my template", enabled=True))

    store.record_error(GLOBAL_INSTRUCTIONS_ID, "boom")

    fetched = store.get()
    assert fetched.last_error == "boom"
    assert fetched.template == "my template"
    assert fetched.enabled is True


def test_record_error_clears_a_stale_error_on_success(store):
    """A healthy render (error=None) clears a previously recorded failure."""
    store.put(SystemInstructionsDoc(template="fine now", last_error="stale failure"))

    store.record_error(GLOBAL_INSTRUCTIONS_ID, None)

    assert store.get().last_error is None


def test_record_error_on_a_missing_doc_is_a_no_op(store):
    """No document to update ⇒ record_error() does nothing and does not raise."""
    store.record_error(GLOBAL_INSTRUCTIONS_ID, "boom")
    assert store.get() is None


def test_record_error_swallows_a_store_exception(store):
    """Best-effort: a failing get()/put() inside record_error() must never raise
    into the run's critical path."""
    with patch.object(store, "get", side_effect=ConnectionError("mongo is down")):
        store.record_error(GLOBAL_INSTRUCTIONS_ID, "boom")  # must not raise


def test_id_index_is_unique(store):
    """The 'id' index enforces uniqueness at the Mongo layer.

    put() itself never collides (it's an upsert keyed on id), so uniqueness is
    exercised the same way the triggers mongo test does: a raw insert_one that
    bypasses the upsert path.
    """
    doc = SystemInstructionsDoc(template="x")
    store.put(doc)
    with pytest.raises(Exception):  # duplicate key error from mongomock
        store._col.insert_one(doc.model_dump(mode="json"))
