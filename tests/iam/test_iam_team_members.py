#!/usr/bin/env python3
"""IAM admin team-membership routes — ``/api/iam/teams/<id>/members``.

Driven over real HTTP through the blueprint, because these handlers read the
request for their body and paging; the stores are real json stores under
``tmp_path``. Two things are stubbed, both genuine I/O boundaries: the
credential reader (there is no live key store) and the principal resolver
(identity resolution is the AuthKit's job, mounted by ``backend.py``, and is
covered where it lives rather than re-proven here).

The guard is process-wide, so every test rebinds it and the fixture restores
the previous kit — a leaked binding would silently re-authorize unrelated
suites.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import flask
import pytest
from flask import Flask
from mewbo_api.auth import guard_registry
from mewbo_api.auth.kit import AuthKit
from mewbo_api.iam.routes import IamRoutesController, register
from mewbo_iam import AuthMethod, ExternalSubject, Principal, TeamRecord, UserRecord
from mewbo_iam.roles import ADMIN_ROLE
from mewbo_iam.settings import AuthSettings
from mewbo_iam.stores.audit import JsonAuthAuditStore
from mewbo_iam.stores.roles import JsonRoleStore
from mewbo_iam.stores.teams import JsonTeamStore
from mewbo_iam.stores.users import JsonUserStore

NOW = datetime(2031, 3, 4, 12, 0, tzinfo=timezone.utc)
TOKEN = "a-test-master-token"
AUTH = {"X-API-KEY": TOKEN}
TEAM_ID = "team:abc123"


@pytest.fixture
def teams(tmp_path: Path) -> JsonTeamStore:
    return JsonTeamStore(tmp_path / "teams.json")


@pytest.fixture
def users(tmp_path: Path) -> JsonUserStore:
    return JsonUserStore(tmp_path / "users.json")


def admin_principal() -> Principal:
    """The acting operator — admin, so the permission leg passes by role."""
    return Principal(
        subject="user:operator",
        kind="user",
        roles=(ADMIN_ROLE,),
        auth_method=AuthMethod(kind="api_key", issuer=None),
    )


@pytest.fixture
def client(
    users: JsonUserStore, teams: JsonTeamStore, tmp_path: Path
) -> Iterator[Any]:
    """A test client over the real blueprint, authenticated as an admin."""
    settings = AuthSettings(enabled=True)
    kit = AuthKit(
        settings=settings,
        key_store=lambda: pytest.fail("the master token should short-circuit the key store"),
        credential_reader=lambda: flask.request.headers.get("X-API-KEY"),
        master_matcher=lambda token: token == TOKEN,
    )
    app = Flask(__name__)

    @app.before_request
    def _resolve() -> None:
        flask.g.principal = admin_principal()

    register(
        app,
        IamRoutesController(
            user_store=users,
            team_store=teams,
            role_store=JsonRoleStore(tmp_path / "roles.json"),
            audit_store=None,
            settings=settings,
            principal_reader=admin_principal,
            clock=lambda: NOW,
        ),
    )
    # The registry is process-wide; ``rebound`` restores whatever the rest of
    # the suite had bound, so this file cannot leak its kit into later tests.
    with guard_registry.guard_registry.rebound(kit):
        yield app.test_client()


def make_team(teams: JsonTeamStore) -> TeamRecord:
    return teams.create(TeamRecord(id=TEAM_ID, slug="platform-eng", name="Platform Eng"))


def make_user(users: JsonUserStore, user_id: str, display: str) -> UserRecord:
    return users.create(
        UserRecord(
            id=user_id,
            external_identities=(ExternalSubject(issuer="oidc", subject=user_id),),
            display_name=display,
            email=f"{display.lower()}@example.com",
            created_at=NOW,
            updated_at=NOW,
        )
    )


# ── the roster round-trip ─────────────────────────────────────────────────────


def test_put_then_list_reports_the_member_with_a_resolved_profile(
    client: Any, users: JsonUserStore, teams: JsonTeamStore
) -> None:
    """A member added through the admin surface comes back with their profile."""
    make_team(teams)
    make_user(users, "user:alice", "Alice")

    added = client.put(
        f"/api/iam/teams/{TEAM_ID}/members/user:alice",
        json={"team_role": "team_admin"},
        headers=AUTH,
    )
    assert added.status_code == 200

    listed = client.get(f"/api/iam/teams/{TEAM_ID}/members", headers=AUTH).get_json()

    assert listed["total"] == 1
    assert listed["items"] == [
        {
            "user_id": "user:alice",
            "team_role": "team_admin",
            "display_name": "Alice",
            "email": "alice@example.com",
            "status": "active",
        }
    ]


def test_put_is_idempotent_and_re_roles_rather_than_duplicating(
    client: Any, users: JsonUserStore, teams: JsonTeamStore
) -> None:
    """A second PUT replaces the role — two edges would make team_admin read-order dependent."""
    make_team(teams)
    make_user(users, "user:alice", "Alice")
    client.put(f"/api/iam/teams/{TEAM_ID}/members/user:alice", json={}, headers=AUTH)

    client.put(
        f"/api/iam/teams/{TEAM_ID}/members/user:alice",
        json={"team_role": "team_admin"},
        headers=AUTH,
    )

    assert [(r.user_id, r.team_role) for r in teams.list_members(TEAM_ID)] == [
        ("user:alice", "team_admin")
    ]


def test_put_defaults_to_team_member(
    client: Any, users: JsonUserStore, teams: JsonTeamStore
) -> None:
    """An omitted role is the least-privileged one, not an error."""
    make_team(teams)
    make_user(users, "user:alice", "Alice")

    response = client.put(f"/api/iam/teams/{TEAM_ID}/members/user:alice", json={}, headers=AUTH)

    assert response.status_code == 200
    assert response.get_json()["team_role"] == "team_member"


def test_delete_removes_the_edge_and_repeats_cleanly(
    client: Any, users: JsonUserStore, teams: JsonTeamStore
) -> None:
    """Removal is idempotent: the desired state holds whether or not an edge existed."""
    make_team(teams)
    make_user(users, "user:alice", "Alice")
    client.put(f"/api/iam/teams/{TEAM_ID}/members/user:alice", json={}, headers=AUTH)

    first = client.delete(f"/api/iam/teams/{TEAM_ID}/members/user:alice", headers=AUTH)
    second = client.delete(f"/api/iam/teams/{TEAM_ID}/members/user:alice", headers=AUTH)

    assert (first.status_code, second.status_code) == (204, 204)
    assert teams.list_members(TEAM_ID) == ()


# ── refusals ──────────────────────────────────────────────────────────────────


def test_unknown_user_is_refused_rather_than_stored(
    client: Any, teams: JsonTeamStore
) -> None:
    """An operator naming a user that does not exist gets told, not a silent edge.

    This is the deliberate opposite of the SCIM surface, which skips an unknown
    member id so one bad entry cannot fail an IdP's bulk roster push.
    """
    make_team(teams)

    response = client.put(f"/api/iam/teams/{TEAM_ID}/members/user:ghost", json={}, headers=AUTH)

    assert response.status_code == 404
    assert teams.list_members(TEAM_ID) == ()


def test_member_routes_404_on_an_unknown_team(client: Any) -> None:
    """Every verb resolves the team first, so none of them can strand an edge."""
    listed = client.get("/api/iam/teams/team:nope/members", headers=AUTH)
    added = client.put("/api/iam/teams/team:nope/members/user:alice", json={}, headers=AUTH)
    removed = client.delete("/api/iam/teams/team:nope/members/user:alice", headers=AUTH)

    assert [listed.status_code, added.status_code, removed.status_code] == [404, 404, 404]


def test_an_unknown_body_field_is_refused(
    client: Any, users: JsonUserStore, teams: JsonTeamStore
) -> None:
    """``extra="forbid"`` at the boundary — a smuggled field is a 400, not a no-op."""
    make_team(teams)
    make_user(users, "user:alice", "Alice")

    response = client.put(
        f"/api/iam/teams/{TEAM_ID}/members/user:alice",
        json={"team_role": "team_member", "user_id": "user:someone-else"},
        headers=AUTH,
    )

    assert response.status_code == 400


def test_an_unknown_role_is_refused(
    client: Any, users: JsonUserStore, teams: JsonTeamStore
) -> None:
    """``team_role`` is a closed set; an invented role must not reach the store."""
    make_team(teams)
    make_user(users, "user:alice", "Alice")

    response = client.put(
        f"/api/iam/teams/{TEAM_ID}/members/user:alice",
        json={"team_role": "owner"},
        headers=AUTH,
    )

    assert response.status_code == 400
    assert teams.list_members(TEAM_ID) == ()


def test_member_routes_require_a_credential(
    client: Any, users: JsonUserStore, teams: JsonTeamStore
) -> None:
    """The guard is wired: no credential, no roster — on the read and the write."""
    make_team(teams)
    make_user(users, "user:alice", "Alice")

    listed = client.get(f"/api/iam/teams/{TEAM_ID}/members")
    added = client.put(f"/api/iam/teams/{TEAM_ID}/members/user:alice", json={})

    assert listed.status_code == 401
    assert added.status_code == 401
    assert teams.list_members(TEAM_ID) == ()


# ── the stranded edge an operator needs to be able to see and clear ──────────


def test_a_member_whose_user_is_gone_is_still_listed_and_removable(
    client: Any, users: JsonUserStore, teams: JsonTeamStore
) -> None:
    """The admin roster reports an unresolvable edge instead of hiding it.

    The SCIM projection SKIPS such a member, which is right for an IdP
    reconciling a roster. Here it would leave an edge that DELETE can remove
    but nothing can discover, so the row survives with its profile null.
    """
    make_team(teams)
    make_user(users, "user:alice", "Alice")
    client.put(f"/api/iam/teams/{TEAM_ID}/members/user:alice", json={}, headers=AUTH)
    users.delete("user:alice")

    listed = client.get(f"/api/iam/teams/{TEAM_ID}/members", headers=AUTH).get_json()
    removed = client.delete(f"/api/iam/teams/{TEAM_ID}/members/user:alice", headers=AUTH)

    assert listed["items"] == [
        {
            "user_id": "user:alice",
            "team_role": "team_member",
            "display_name": None,
            "email": None,
            "status": None,
        }
    ]
    assert removed.status_code == 204
    assert teams.list_members(TEAM_ID) == ()


def test_deleting_a_team_clears_its_roster(
    client: Any, users: JsonUserStore, teams: JsonTeamStore
) -> None:
    """The store's cascade reaches through this surface — no edge outlives its team."""
    make_team(teams)
    make_user(users, "user:alice", "Alice")
    client.put(f"/api/iam/teams/{TEAM_ID}/members/user:alice", json={}, headers=AUTH)

    client.delete(f"/api/iam/teams/{TEAM_ID}", headers=AUTH)

    assert teams.list_members(TEAM_ID) == ()


@pytest.fixture
def audited(
    users: JsonUserStore, teams: JsonTeamStore, tmp_path: Path
) -> Iterator[tuple[Any, JsonAuthAuditStore]]:
    """The same surface with a real audit store, to observe what it records."""
    settings = AuthSettings(enabled=True)
    store = JsonAuthAuditStore(tmp_path / "audit.json")
    kit = AuthKit(
        settings=settings,
        key_store=lambda: pytest.fail("the master token should short-circuit the key store"),
        credential_reader=lambda: flask.request.headers.get("X-API-KEY"),
        master_matcher=lambda token: token == TOKEN,
    )
    app = Flask(__name__)

    @app.before_request
    def _resolve() -> None:
        flask.g.principal = admin_principal()

    register(
        app,
        IamRoutesController(
            user_store=users,
            team_store=teams,
            role_store=JsonRoleStore(tmp_path / "roles.json"),
            audit_store=store,
            settings=settings,
            principal_reader=admin_principal,
            clock=lambda: NOW,
        ),
    )
    with guard_registry.guard_registry.rebound(kit):
        yield app.test_client(), store


def test_membership_changes_reach_the_audit_trail(
    audited: tuple[Any, JsonAuthAuditStore], users: JsonUserStore, teams: JsonTeamStore
) -> None:
    """Add, re-role and remove each land, and each names who did it.

    The actor matters as much as the subject: an entry recording that Alice
    gained team_admin, without recording who granted it, cannot answer the
    question an audit trail is read to answer.
    """
    client, store = audited
    make_team(teams)
    make_user(users, "user:alice", "Alice")
    path = f"/api/iam/teams/{TEAM_ID}/members/user:alice"

    client.put(path, json={}, headers=AUTH)
    client.put(path, json={"team_role": "team_admin"}, headers=AUTH)
    client.delete(path, headers=AUTH)

    # The clock is frozen, so all three share an instant and "newest-first"
    # degrades to insertion order. Asserting a reversal here would be asserting
    # sort stability, not the trail's ordering — that property belongs to a test
    # with a moving clock, and lives on the audit store where the sort does.
    events = store.list(limit=10)
    assert [(e.change, e.subject, e.team_id) for e in events] == [
        ("added", "user:alice", TEAM_ID),
        ("role_changed", "user:alice", TEAM_ID),
        ("removed", "user:alice", TEAM_ID),
    ]
    assert {e.actor_subject for e in events} == {"user:operator"}


def test_a_membership_write_that_changes_nothing_records_nothing(
    audited: tuple[Any, JsonAuthAuditStore], users: JsonUserStore, teams: JsonTeamStore
) -> None:
    """Guarded on an actual change, not on the route being called.

    A PUT re-asserting the role a member already holds, and a DELETE of an edge
    that was never there, are both no-ops. Recording them would bury the real
    grants under retries — SCIM and any syncing client replay these constantly.
    """
    client, store = audited
    make_team(teams)
    make_user(users, "user:alice", "Alice")
    path = f"/api/iam/teams/{TEAM_ID}/members/user:alice"
    client.put(path, json={}, headers=AUTH)

    client.put(path, json={}, headers=AUTH)  # same role again
    client.delete(f"/api/iam/teams/{TEAM_ID}/members/user:ghost", headers=AUTH)

    assert [e.change for e in store.list(limit=10)] == ["added"]
