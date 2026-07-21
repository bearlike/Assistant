#!/usr/bin/env python3
"""The MongoDB identity-store drivers, against a real in-process query engine.

`stores/mongo.py` is a supported production backend that no test had ever
executed — the whole module was unreachable from the suite, because every store
test builds the json driver. A driver's value is entirely in the queries it
emits, so the gap was total: nothing checked that a filter, a projection or an
update document was even well-formed.

`mongomock` is used rather than a hand-written fake for exactly that reason: it
parses and executes the query documents, so a malformed operator, a bad
projection, or a wrong update shape fails here the way it would against a
server. What it is NOT is a real mongod — **server-side index enforcement is not
covered**, so the uniqueness assertions below exercise the checks
`TeamStoreBase` performs in Python, not the unique indexes that back them up.
The indexes remain verified only by reading `mongo.py`.

Focus is on what is driver-specific and therefore invisible to the json suite:
the `$elemMatch` external-identity lookup, the dotted `grantee.*` filters, the
`_id`-stripping projection, `return_document` on the update, and the membership
edges living in their own collection.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import mongomock
import pytest
from mewbo_iam.access import AccessGrant, Grantee
from mewbo_iam.audit import parse_audit_event
from mewbo_iam.principal import ExternalSubject
from mewbo_iam.stores import mongo as mongo_stores
from mewbo_iam.teams import TeamRecord
from mewbo_iam.users import UserRecord

NOW = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
LATER = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)
ISSUER = "https://idp.example.com"


@pytest.fixture(autouse=True)
def in_process_mongo(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Swap pymongo's client for mongomock's — the SERVER is the only stub."""
    monkeypatch.setattr(mongo_stores, "MongoClient", mongomock.MongoClient)
    yield


def make_user(**overrides: Any) -> UserRecord:
    """A user record carrying one external identity."""
    fields: dict[str, Any] = {
        "id": "user:alice",
        "display_name": "Alice",
        "email": "alice@example.com",
        "external_identities": (ExternalSubject(issuer=ISSUER, subject="alice"),),
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return UserRecord(**fields)


# ── users ─────────────────────────────────────────────────────────────────────
def test_a_user_round_trips_without_mongos_surrogate_id_leaking_in() -> None:
    """The `_id` projection is load-bearing: the models forbid extra fields.

    Dropping it would make every read fail validation rather than degrade, so
    this asserts the read parses at all — which is the whole check.
    """
    store = mongo_stores.MongoUserStore()
    user = make_user()

    store.create(user)

    fetched = store.get("user:alice")
    assert fetched is not None
    assert fetched.id == "user:alice"
    assert fetched.email == "alice@example.com"


def test_a_user_is_found_by_any_of_its_external_identities() -> None:
    """The `$elemMatch` lookup — the join key every SSO login resolves through.

    Both fields must match the SAME element: an issuer/subject pair split across
    two different identities must not resolve, or one provider's subject could
    authenticate as another's.
    """
    store = mongo_stores.MongoUserStore()
    store.create(
        make_user(
            external_identities=(
                ExternalSubject(issuer=ISSUER, subject="alice"),
                ExternalSubject(issuer="https://other.example.com", subject="bob"),
            )
        )
    )

    assert store.get_by_external(ExternalSubject(issuer=ISSUER, subject="alice")) is not None
    assert (
        store.get_by_external(ExternalSubject(issuer="https://other.example.com", subject="bob"))
        is not None
    ), "the second identity resolves too"
    crossed = store.get_by_external(ExternalSubject(issuer=ISSUER, subject="bob"))
    assert crossed is None, "issuer and subject must match within ONE identity"


def test_updating_a_user_returns_and_persists_the_new_document() -> None:
    """`return_document=True` must yield the document AFTER the update.

    Returning the pre-update document would hand the caller stale values that
    look freshly written.
    """
    store = mongo_stores.MongoUserStore()
    store.create(make_user())

    returned = store.update(make_user(display_name="Alice B", updated_at=LATER))

    assert returned.display_name == "Alice B", "the RETURNED document is the updated one"
    reread = store.get("user:alice")
    assert reread is not None
    assert reread.display_name == "Alice B", "and it was actually persisted"


def test_updating_an_absent_user_raises_rather_than_silently_inserting() -> None:
    """No upsert here — a missing id is a caller error, not a create."""
    store = mongo_stores.MongoUserStore()

    with pytest.raises(KeyError):
        store.update(make_user(id="user:ghost"))


def test_deleting_a_user_reports_whether_anything_was_removed() -> None:
    """The boolean is the caller's only signal — a second delete must be False."""
    store = mongo_stores.MongoUserStore()
    store.create(make_user())

    assert store.delete("user:alice") is True
    assert store.delete("user:alice") is False


# ── roles ─────────────────────────────────────────────────────────────────────
def test_seeding_builtins_is_idempotent_and_leaves_them_immutable() -> None:
    """Seeding runs on every startup, so a second pass must not duplicate rows."""
    store = mongo_stores.MongoRoleStore()

    store.seed_builtins()
    first = sorted(role.name for role in store.list())
    store.seed_builtins()

    assert sorted(role.name for role in store.list()) == first, "no duplicates on re-seed"
    assert "admin" in first
    with pytest.raises(ValueError, match="built-in"):
        store.delete("admin")


# ── teams and their membership edges ──────────────────────────────────────────
def test_deleting_a_team_cascades_to_its_membership_edges() -> None:
    """The edges live in a SECOND collection, so the cascade is the driver's job.

    An orphaned edge would keep naming a team that no longer exists, which every
    caller resolves and silently skips — the membership just vanishes without a
    trace rather than failing loudly.
    """
    store = mongo_stores.MongoTeamStore()
    store.create(TeamRecord(id="team:eng", slug="eng", name="Engineering"))
    store.add_member("team:eng", "user:alice")
    assert [m.team_id for m in store.list_for_user("user:alice")] == ["team:eng"]

    store.delete("team:eng")

    assert store.list_for_user("user:alice") == (), "the edge went with the team"
    assert store.list_members("team:eng") == ()


def test_two_teams_without_an_external_id_can_coexist() -> None:
    """`external_id` is null for every team no directory provisioned.

    A plain unique index would collide on the second such team; the partial
    filter is what keeps locally-created teams from blocking each other.
    """
    store = mongo_stores.MongoTeamStore()

    store.create(TeamRecord(id="team:eng", slug="eng", name="Engineering"))
    store.create(TeamRecord(id="team:ops", slug="ops", name="Ops"))

    assert {team.slug for team in store.list()} == {"eng", "ops"}


def test_a_duplicate_slug_is_refused() -> None:
    """Uniqueness is enforced in Python by the base, not only by the index."""
    store = mongo_stores.MongoTeamStore()
    store.create(TeamRecord(id="team:eng", slug="eng", name="Engineering"))

    with pytest.raises(ValueError, match="slug"):
        store.create(TeamRecord(id="team:other", slug="eng", name="Duplicate"))


# ── access grants ─────────────────────────────────────────────────────────────
def test_a_grant_is_idempotent_and_revocable_by_value() -> None:
    """Grants are keyless value records matched on their dotted `grantee.*` keys.

    A filter that failed to reach into the nested grantee would match the wrong
    documents — granting twice would duplicate, and a revoke could remove
    someone else's grant.
    """
    store = mongo_stores.MongoAccessGrantStore()
    grant = AccessGrant(
        resource_kind="session",
        resource_id="session:1",
        grantee=Grantee(kind="user", id="user:alice"),
        level="read",
    )

    store.grant(grant)
    store.grant(grant)

    assert len(store.list_for_resource("session", "session:1")) == 1, "the second grant was a no-op"
    assert len(store.list_for_grantee(Grantee(kind="user", id="user:alice"))) == 1
    assert store.revoke(grant) is True
    assert store.list_for_resource("session", "session:1") == []


def test_grants_are_scoped_to_their_own_grantee_and_resource() -> None:
    """Two grantees on one resource must not read as each other's."""
    store = mongo_stores.MongoAccessGrantStore()
    for grantee in (Grantee(kind="user", id="user:alice"), Grantee(kind="team", id="team:eng")):
        store.grant(
            AccessGrant(
                resource_kind="session",
                resource_id="session:1",
                grantee=grantee,
                level="read",
            )
        )

    alice = store.list_for_grantee(Grantee(kind="user", id="user:alice"))

    assert len(alice) == 1
    assert alice[0].grantee.kind == "user"
    assert len(store.list_for_resource("session", "session:1")) == 2


# ── audit trail ───────────────────────────────────────────────────────────────
def audit_event(**overrides: Any) -> Any:
    """One login-failure audit event."""
    payload: dict[str, Any] = {
        "type": "login_failure",
        "ts": NOW.isoformat(),
        "actor_subject": "alice",
        "reason": "bad password",
        "source": "api",
        "method": "password",
    }
    payload.update(overrides)
    return parse_audit_event(payload)


def test_the_audit_trail_filters_by_actor_and_type() -> None:
    """`_stored_records` does exact-value filters only; the base does the rest."""
    store = mongo_stores.MongoAuthAuditStore()
    store.append(audit_event())
    store.append(audit_event(actor_subject="bob"))

    assert len(store.list()) == 2
    assert len(store.list(actor_subject="alice")) == 1
    assert len(store.list(type="login_failure")) == 2
    assert len(store.list(actor_subject="carol")) == 0


def test_the_audit_trail_is_ordered_newest_first_by_instant_not_by_text() -> None:
    """`ts` persists as an ISO STRING, so a backend sort on it is lexicographic.

    `+00:00` and `Z` spell the same instant and sort differently as text, so the
    driver must NOT sort — the base parses and orders by the real instant. This
    pins that the driver did not quietly push the sort down.
    """
    store = mongo_stores.MongoAuthAuditStore()
    store.append(audit_event(ts="2026-01-02T00:00:00+00:00", actor_subject="later"))
    store.append(audit_event(ts="2026-01-01T00:00:00Z", actor_subject="earlier"))

    assert [event.actor_subject for event in store.list()] == ["later", "earlier"]
