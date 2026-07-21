"""Session ownership: the stamp at creation and the authority filter on listing.

Both store drivers are exercised, because the two express the same three rules
in completely different mechanics — a JSON index bucket versus a Mongo document
field — and the migration rule in particular relies on a Mongo-specific quirk
(equality to ``null`` matching a missing field) that a filesystem test can never
cover.
"""

from unittest.mock import patch

import mongomock
import pytest
from mewbo_core.session_runtime import SessionRuntime
from mewbo_core.session_store import SessionStore
from mewbo_core.session_store_mongo import MongoSessionStore

ALICE = "user:alice"
BOB = "user:bob"


@pytest.fixture()
def store(tmp_path):
    """A filesystem-backed store rooted in a throwaway directory."""
    return SessionStore(root_dir=str(tmp_path / "sessions"))


@pytest.fixture()
def mongo_store(tmp_path):
    """A Mongo-backed store on mongomock, mirroring ``test_session_store_mongo``."""
    with patch(
        "mewbo_core.session_store_mongo.MongoClient",
        mongomock.MongoClient,
    ):
        return MongoSessionStore(
            root_dir=str(tmp_path / "mongo"),
            uri="mongodb://localhost:27017",
            database="test_ownership",
        )


@pytest.fixture(params=["json", "mongodb"])
def any_store(request, store, mongo_store):
    """Every ownership rule, once per driver."""
    return store if request.param == "json" else mongo_store


# ---------------------------------------------------------------------------
# The stamp
# ---------------------------------------------------------------------------


def test_create_session_stamps_the_owner(any_store):
    """A session created by an identified caller records that subject."""
    session_id = any_store.create_session(ALICE)
    assert any_store.get_owner(session_id) == ALICE


def test_create_session_without_an_owner_is_unowned(any_store):
    """No principal means no stamp — not a stamp of the empty string."""
    session_id = any_store.create_session()
    assert any_store.get_owner(session_id) is None


def test_the_stamp_is_set_once(any_store):
    """A replayed ``ensure_session`` cannot re-point an owned session.

    ``ensure_session`` is idempotent and reachable by any caller holding an id,
    so a last-write-wins stamp would make ownership takeover a side effect of a
    documented no-op.
    """
    session_id = any_store.create_session(ALICE)
    any_store.ensure_session(session_id, BOB)
    assert any_store.get_owner(session_id) == ALICE


def test_an_unowned_session_cannot_be_claimed_later(any_store):
    """Set-once cuts both ways: a legacy session stays unowned, not claimable.

    Were this to succeed, the first caller to touch any pre-existing session
    would take it over — and because the filter treats unowned as visible to
    everyone, that caller is not necessarily its creator.
    """
    session_id = any_store.create_session()
    any_store.ensure_session(session_id, BOB)
    assert any_store.get_owner(session_id) is None


# ---------------------------------------------------------------------------
# The filter
# ---------------------------------------------------------------------------


def test_unfiltered_listing_returns_every_session(any_store):
    """``owner=None`` is the read-all authority — and the historical behaviour."""
    mine = any_store.create_session(ALICE)
    theirs = any_store.create_session(BOB)
    legacy = any_store.create_session()

    assert set(any_store.list_sessions()) == {mine, theirs, legacy}


def test_filtered_listing_hides_another_subjects_sessions(any_store):
    """A subject-scoped listing excludes what someone else owns."""
    mine = any_store.create_session(ALICE)
    theirs = any_store.create_session(BOB)

    listed = any_store.list_sessions(ALICE)
    assert mine in listed
    assert theirs not in listed


def test_a_preexisting_unowned_session_stays_visible_to_everyone(any_store):
    """The migration rule, stated as the behaviour that must not regress.

    Sessions predating the stamp carry no subject and none can be reconstructed
    for them. Hiding them would erase a user's existing work from their own
    list; showing them discloses nothing that was not already listable before
    ownership existed.
    """
    legacy = any_store.create_session()

    assert legacy in any_store.list_sessions(ALICE)
    assert legacy in any_store.list_sessions(BOB)


def test_filtering_is_not_a_prefix_or_substring_match(any_store):
    """The owner comparison is exact — a subject is not a namespace."""
    theirs = any_store.create_session("user:alice-two")

    assert theirs not in any_store.list_sessions(ALICE)


def test_a_filtered_listing_stays_sorted(any_store):
    """Narrowing must not disturb the ordering callers already rely on."""
    for _ in range(5):
        any_store.create_session(ALICE)
        any_store.create_session(BOB)

    listed = any_store.list_sessions(ALICE)
    assert listed == sorted(listed)


# ---------------------------------------------------------------------------
# Forking
# ---------------------------------------------------------------------------


def test_a_fork_belongs_to_whoever_forked_it(any_store):
    """A fork is a new session, stamped for its creator, not for the source's."""
    source = any_store.create_session(ALICE)
    any_store.append_event(source, {"type": "user", "payload": {"text": "hi"}})

    forked = any_store.fork_session(source, BOB)

    assert any_store.get_owner(forked) == BOB
    assert any_store.get_owner(source) == ALICE


def test_forking_does_not_launder_a_session_out_of_ownership(any_store):
    """An unstamped fork of an owned session would be visible to every lister."""
    source = any_store.create_session(ALICE)
    forked = any_store.fork_session_at(source, "9999-01-01T00:00:00Z", ALICE)

    assert forked not in any_store.list_sessions(BOB)


# ---------------------------------------------------------------------------
# The runtime seam
# ---------------------------------------------------------------------------


def _with_user_turn(runtime: SessionRuntime, owner: str | None) -> str:
    """Mint a session carrying one user event so it survives the list filters."""
    session_id = runtime.resolve_session(owner=owner)
    runtime.session_store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})
    return session_id


def test_runtime_resolve_session_stamps_a_new_session(store):
    """``resolve_session`` is the one place a session is minted — stamp it there."""
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session(owner=ALICE)

    assert store.get_owner(session_id) == ALICE


def test_runtime_resolve_session_never_restamps_an_existing_one(store):
    """Re-engaging someone else's session must not quietly transfer it."""
    runtime = SessionRuntime(session_store=store)
    session_id = runtime.resolve_session(owner=ALICE)

    runtime.resolve_session(session_id=session_id, owner=BOB)

    assert store.get_owner(session_id) == ALICE


def test_runtime_list_sessions_narrows_by_owner(store):
    """The runtime listing carries the filter down to the store."""
    runtime = SessionRuntime(session_store=store)
    mine = _with_user_turn(runtime, ALICE)
    theirs = _with_user_turn(runtime, BOB)
    legacy = _with_user_turn(runtime, None)

    listed = {str(s["session_id"]) for s in runtime.list_sessions(owner=ALICE)}

    assert listed == {mine, legacy}
    assert theirs not in listed


def test_runtime_list_sessions_unfiltered_sees_everything(store):
    """A read-all caller passes no owner and the listing is unchanged."""
    runtime = SessionRuntime(session_store=store)
    mine = _with_user_turn(runtime, ALICE)
    theirs = _with_user_turn(runtime, BOB)

    listed = {str(s["session_id"]) for s in runtime.list_sessions()}

    assert {mine, theirs} <= listed


# ---------------------------------------------------------------------------
# The summary shape — the byte-identical law
# ---------------------------------------------------------------------------

# Frozen deliberately rather than derived from the code under test: this tuple
# IS the pre-ownership wire contract, so a test that recomputed it from the
# implementation would agree with any regression it was written to catch.
SUMMARY_KEYS_BEFORE_OWNERSHIP = (
    "session_id",
    "title",
    "created_at",
    "status",
    "done_reason",
    "running",
    "recoverable",
    "context",
    "origin",
    "capabilities",
    "workspace",
    "archived",
    "terminated",
    "terminated_at",
)


def test_an_unowned_summary_is_unchanged_key_for_key_and_in_order(store):
    """With no identity configured nothing is stamped, so nothing appears.

    This is the byte-identical law at the seam that produces the wire body: an
    absent owner must not surface as an explicit null, because that is still a
    new key in a response clients did not expect one in.
    """
    runtime = SessionRuntime(session_store=store)
    session_id = _with_user_turn(runtime, None)

    summary = runtime.summarize_session(session_id)

    assert tuple(summary) == SUMMARY_KEYS_BEFORE_OWNERSHIP
    assert "owner" not in summary


def test_an_owned_summary_appends_owner_and_disturbs_nothing_before_it(store):
    """The new key is strictly appended — never inserted mid-body."""
    runtime = SessionRuntime(session_store=store)
    session_id = _with_user_turn(runtime, ALICE)

    summary = runtime.summarize_session(session_id)

    assert tuple(summary) == (*SUMMARY_KEYS_BEFORE_OWNERSHIP, "owner")
    assert summary["owner"] == ALICE


def test_listing_reports_ownership_per_row(store):
    """A client can render who owns each row without a second round-trip."""
    runtime = SessionRuntime(session_store=store)
    mine = _with_user_turn(runtime, ALICE)
    legacy = _with_user_turn(runtime, None)

    by_id = {str(s["session_id"]): s for s in runtime.list_sessions()}

    assert by_id[mine]["owner"] == ALICE
    assert "owner" not in by_id[legacy]


# ---------------------------------------------------------------------------
# Driver-specific: the on-disk / on-document migration surface
# ---------------------------------------------------------------------------


def test_json_index_written_before_ownership_still_loads(store):
    """An ``index.json`` with no ``owners`` bucket must not fault the filter."""
    session_id = store.create_session()
    store._save_index({"tags": {}, "archived": {}, "terminated": {}})

    assert store.get_owner(session_id) is None
    assert store.list_sessions(ALICE) == [session_id]


def test_mongo_document_predating_the_field_is_treated_as_unowned(mongo_store):
    """A doc with no ``owner_subject`` at all — the real shape on the deployment.

    ``$setOnInsert`` only stamps documents this code created, so every session
    already in the collection lacks the field entirely rather than holding a
    null. The filter must match those, which is why it compares to ``None``
    rather than testing existence.
    """
    session_id = mongo_store.create_session(ALICE)
    mongo_store._col("sessions").update_one(
        {"_id": session_id},
        {"$unset": {"owner_subject": ""}},
    )

    assert mongo_store.get_owner(session_id) is None
    assert session_id in mongo_store.list_sessions(BOB)
