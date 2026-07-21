#!/usr/bin/env python3
"""SCIM Group provisioning — durable membership and rename-safe identity.

Driven through a real Flask app with the blueprint mounted, because the
controller reads the request for its body, filters and paging: exercising it
through the wire is the only way these assertions cover what an IdP actually
gets back. The stores are real json stores under ``tmp_path``; nothing here is
mocked but the deprovision callback, which is an out-of-process side effect.

The two regressions worth naming, both silent:

* member ids were validated and then DISCARDED, so every Group response
  reported ``members: []`` — an IdP reconciling that against its own roster
  reads it as "remove everyone";
* groups were matched by a ``displayName``-derived slug, so renaming a group at
  the IdP forked a second team and stranded the first one's roster.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from mewbo_api.scim.routes import ScimRoutesController, register
from mewbo_iam import ExternalSubject, UserRecord
from mewbo_iam.settings import AuthSettings, ScimSettings
from mewbo_iam.stores.teams import JsonTeamStore
from mewbo_iam.stores.users import JsonUserStore

NOW = datetime(2031, 3, 4, 12, 0, tzinfo=timezone.utc)
SECRET = "a-test-scim-bearer-secret"
AUTH = {"Authorization": f"Bearer {SECRET}"}
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"


@pytest.fixture
def teams(tmp_path: Path) -> JsonTeamStore:
    return JsonTeamStore(tmp_path / "teams.json")


@pytest.fixture
def users(tmp_path: Path) -> JsonUserStore:
    return JsonUserStore(tmp_path / "users.json")


@pytest.fixture
def client(users: JsonUserStore, teams: JsonTeamStore) -> Any:
    """A test client over the real blueprint, SCIM enabled and authenticated."""
    app = Flask(__name__)
    register(
        app,
        ScimRoutesController(
            user_store=users,
            team_store=teams,
            audit_store=None,
            settings=AuthSettings(
                enabled=True,
                scim=ScimSettings(enabled=True, secret=SECRET),
            ),
            deprovision=lambda subject: None,
            clock=lambda: NOW,
        ),
    )
    return app.test_client()


def make_user(users: JsonUserStore, user_id: str, display: str) -> UserRecord:
    """A provisioned user the group payloads can legitimately reference."""
    return users.create(
        UserRecord(
            id=user_id,
            external_identities=(ExternalSubject(issuer="scim", subject=user_id),),
            display_name=display,
            created_at=NOW,
            updated_at=NOW,
        )
    )


def group_payload(display_name: str, *, external_id: str | None = None, members: Any = ()) -> dict:
    return {
        "schemas": [GROUP_SCHEMA],
        "displayName": display_name,
        **({"externalId": external_id} if external_id else {}),
        "members": [{"value": m} for m in members],
    }


# ── membership is durable ─────────────────────────────────────────────────────


def test_group_create_then_get_reports_its_members(
    client: Any, users: JsonUserStore, teams: JsonTeamStore
) -> None:
    """The roster pushed on create comes back on the subsequent GET.

    Before this the ids were validated and dropped, so an IdP saw ``members:
    []`` immediately after successfully pushing a full roster.
    """
    make_user(users, "user:alice", "Alice")
    make_user(users, "user:bob", "Bob")

    created = client.post(
        "/api/scim/v2/Groups",
        json=group_payload(
            "Platform Eng", external_id="idp-g-1", members=("user:alice", "user:bob")
        ),
        headers=AUTH,
    )
    assert created.status_code == 201
    group_id = created.get_json()["id"]

    fetched = client.get(f"/api/scim/v2/Groups/{group_id}", headers=AUTH)

    assert fetched.status_code == 200
    assert sorted(m["value"] for m in fetched.get_json()["members"]) == ["user:alice", "user:bob"]
    assert {r.user_id for r in teams.list_members(group_id)} == {"user:alice", "user:bob"}


def test_group_list_reports_the_same_roster_as_a_single_get(
    client: Any, users: JsonUserStore
) -> None:
    """A list page and a single GET must not disagree about membership.

    They are separate projections; if the list kept reporting ``[]`` an IdP
    reconciling against it would issue spurious removals.
    """
    make_user(users, "user:alice", "Alice")
    created = client.post(
        "/api/scim/v2/Groups",
        json=group_payload("Platform Eng", external_id="idp-g-1", members=("user:alice",)),
        headers=AUTH,
    )
    group_id = created.get_json()["id"]

    listed = client.get("/api/scim/v2/Groups", headers=AUTH).get_json()["Resources"]
    single = client.get(f"/api/scim/v2/Groups/{group_id}", headers=AUTH).get_json()

    assert [g["members"] for g in listed] == [single["members"]]


def test_patch_add_and_remove_drive_real_membership_writes(
    client: Any, users: JsonUserStore, teams: JsonTeamStore
) -> None:
    """PATCH add/remove persist — ``delta.remove`` in particular was computed and dropped."""
    make_user(users, "user:alice", "Alice")
    make_user(users, "user:bob", "Bob")
    group_id = client.post(
        "/api/scim/v2/Groups",
        json=group_payload("Platform Eng", external_id="idp-g-1", members=("user:alice",)),
        headers=AUTH,
    ).get_json()["id"]

    client.patch(
        f"/api/scim/v2/Groups/{group_id}",
        json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [
                {"op": "add", "path": "members", "value": [{"value": "user:bob"}]},
                {"op": "remove", "path": 'members[value eq "user:alice"]'},
            ],
        },
        headers=AUTH,
    )

    assert {r.user_id for r in teams.list_members(group_id)} == {"user:bob"}


def test_patch_replace_preserves_a_retained_members_team_admin_role(
    client: Any, users: JsonUserStore, teams: JsonTeamStore
) -> None:
    """A SCIM replace must not demote a team admin who stays in the group.

    A SCIM member list carries no roles, so the obvious drop-then-re-add
    implementation silently strips ``team_admin`` from everyone on every sync.
    """
    make_user(users, "user:alice", "Alice")
    make_user(users, "user:bob", "Bob")
    group_id = client.post(
        "/api/scim/v2/Groups",
        json=group_payload("Platform Eng", external_id="idp-g-1", members=("user:alice",)),
        headers=AUTH,
    ).get_json()["id"]
    teams.add_member(group_id, "user:alice", "team_admin")

    client.patch(
        f"/api/scim/v2/Groups/{group_id}",
        json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [
                {
                    "op": "replace",
                    "path": "members",
                    "value": [{"value": "user:alice"}, {"value": "user:bob"}],
                }
            ],
        },
        headers=AUTH,
    )

    roles = {r.user_id: r.team_role for r in teams.list_members(group_id)}
    assert roles == {"user:alice": "team_admin", "user:bob": "team_member"}


# ── unresolvable ids degrade, never fail the batch ────────────────────────────


def test_unknown_member_id_is_skipped_not_fatal(
    client: Any, users: JsonUserStore, teams: JsonTeamStore
) -> None:
    """One id the user store does not know must not sink the whole roster push.

    IdPs retry membership syncs aggressively and routinely reference a user
    they have not pushed yet; failing the batch would stall the sync on it.
    """
    make_user(users, "user:alice", "Alice")

    created = client.post(
        "/api/scim/v2/Groups",
        json=group_payload(
            "Platform Eng", external_id="idp-g-1", members=("user:alice", "user:ghost")
        ),
        headers=AUTH,
    )

    assert created.status_code == 201
    group_id = created.get_json()["id"]
    assert [m["value"] for m in created.get_json()["members"]] == ["user:alice"]
    assert {r.user_id for r in teams.list_members(group_id)} == {"user:alice"}


def test_an_edge_whose_user_disappeared_is_omitted_from_the_response(
    client: Any, users: JsonUserStore, teams: JsonTeamStore
) -> None:
    """A stranded edge is inert on read, not a crash.

    Deleting a user deliberately does NOT cascade into the team store, so an
    edge can outlive its user; the read side is where it disappears.
    """
    make_user(users, "user:alice", "Alice")
    group_id = client.post(
        "/api/scim/v2/Groups",
        json=group_payload("Platform Eng", external_id="idp-g-1", members=("user:alice",)),
        headers=AUTH,
    ).get_json()["id"]
    users.delete("user:alice")

    fetched = client.get(f"/api/scim/v2/Groups/{group_id}", headers=AUTH)

    assert fetched.status_code == 200
    assert fetched.get_json()["members"] == []
    assert {r.user_id for r in teams.list_members(group_id)} == {"user:alice"}


def test_removing_a_member_whose_user_was_deleted_still_clears_the_edge(
    client: Any, users: JsonUserStore, teams: JsonTeamStore
) -> None:
    """A removal must not be filtered through the unknown-id skip.

    Applying the add-side validation to a removal would refuse to detach a
    deleted user — leaving behind exactly the stranded edge the IdP sent the
    removal to clear.
    """
    make_user(users, "user:alice", "Alice")
    group_id = client.post(
        "/api/scim/v2/Groups",
        json=group_payload("Platform Eng", external_id="idp-g-1", members=("user:alice",)),
        headers=AUTH,
    ).get_json()["id"]
    users.delete("user:alice")

    client.patch(
        f"/api/scim/v2/Groups/{group_id}",
        json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "remove", "path": 'members[value eq "user:alice"]'}],
        },
        headers=AUTH,
    )

    assert teams.list_members(group_id) == ()


# ── externalId is the rename-safe identity ────────────────────────────────────


def test_idp_side_rename_updates_the_existing_team(
    client: Any, users: JsonUserStore, teams: JsonTeamStore
) -> None:
    """Same ``externalId``, new ``displayName`` → one team, renamed in place.

    Matching on the derived slug instead forks a second team, which strands
    the original's roster and every ownership stamp pointing at it.
    """
    make_user(users, "user:alice", "Alice")
    first = client.post(
        "/api/scim/v2/Groups",
        json=group_payload("Platform Eng", external_id="idp-g-1", members=("user:alice",)),
        headers=AUTH,
    ).get_json()

    renamed = client.post(
        "/api/scim/v2/Groups",
        json=group_payload(
            "Developer Experience", external_id="idp-g-1", members=("user:alice",)
        ),
        headers=AUTH,
    )

    assert renamed.status_code == 200
    assert renamed.get_json()["id"] == first["id"]
    assert len(teams.list()) == 1
    assert teams.get(first["id"]).name == "Developer Experience"
    assert {r.user_id for r in teams.list_members(first["id"])} == {"user:alice"}


def test_a_team_created_without_an_external_id_is_adopted_by_slug(
    client: Any, teams: JsonTeamStore
) -> None:
    """A pre-existing team is claimed and stamped, not duplicated.

    The slug fallback still runs when ``externalId`` matches nothing, so a team
    an operator created before the IdP knew about it gets linked on first push.
    """
    client.post("/api/scim/v2/Groups", json=group_payload("Platform Eng"), headers=AUTH)

    adopted = client.post(
        "/api/scim/v2/Groups",
        json=group_payload("Platform Eng", external_id="idp-g-1"),
        headers=AUTH,
    )

    assert adopted.status_code == 200
    assert len(teams.list()) == 1
    assert teams.list()[0].external_id == "idp-g-1"
