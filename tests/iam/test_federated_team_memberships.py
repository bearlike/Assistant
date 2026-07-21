#!/usr/bin/env python3
"""Team memberships on a federated principal — the id-vs-slug law, both legs.

Two failure modes this covers, both of which type-check and store cleanly
before they go wrong:

* a group→team mapping writing a SLUG into ``Principal.team_memberships``,
  which then matches no ``OwnershipStamp.team_id`` — every resource the team
  owns silently invisible to its own members;
* a membership that exists right after login and vanishes on the next request,
  because the cookie-resolved path built its principal without the store.

Both legs assert against what an ownership stamp actually carries rather than
against a hand-written expectation, so a test cannot pass while the two
disagree.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from mewbo_api.auth.federated import FederatedRuntime, build_federated_runtime
from mewbo_api.auth.identity_flow import IdentityFlow
from mewbo_iam import AuthMethod, AvatarPolicy, ExternalSubject, RawIdentity, TeamRecord
from mewbo_iam.access import AccessDecider, AccessGrant, Grantee, OwnershipStamp
from mewbo_iam.mappings import GroupRoleMapping, GroupTeamMapping, MappingRule
from mewbo_iam.settings import AuthSettings, SessionSettings
from mewbo_iam.stores.teams import JsonTeamStore
from mewbo_iam.stores.users import JsonUserStore

NOW = datetime(2031, 3, 4, 12, 0, tzinfo=timezone.utc)
ISSUER = "https://idp.example.com"
OIDC_METHOD = AuthMethod(kind="oidc", issuer=ISSUER)


@pytest.fixture
def stores(tmp_path: Path) -> tuple[JsonUserStore, JsonTeamStore]:
    """Fresh json stores under ``tmp_path`` — never the developer's real home."""
    return JsonUserStore(tmp_path / "users.json"), JsonTeamStore(tmp_path / "teams.json")


def raw(*, subject: str = "sub-1", groups: tuple[str, ...] = ()) -> RawIdentity:
    """A verified assertion carrying the IdP's group names."""
    return RawIdentity(
        external_subject=ExternalSubject(issuer=ISSUER, subject=subject),
        email="engineer@example.com",
        display="An Engineer",
        groups=groups,
    )


def flow(
    stores: tuple[JsonUserStore, JsonTeamStore], *, rules: tuple[MappingRule, ...] = ()
) -> IdentityFlow:
    """A JIT flow whose group→team mapping is *rules*."""
    users, teams = stores
    return IdentityFlow(
        user_store=users,
        team_store=teams,
        role_mapping=GroupRoleMapping(),
        team_mapping=GroupTeamMapping(rules=rules),
        bootstrap=None,
        avatar_policy=AvatarPolicy(),
        clock=lambda: NOW,
    )


# ── the live defect: a mapped membership must carry a durable id ──────────────


def test_mapped_membership_matches_what_an_ownership_stamp_carries(
    stores: tuple[JsonUserStore, JsonTeamStore],
) -> None:
    """A group-mapped login can reach a resource shared with its team.

    The regression this pins: the mapping resolves SLUGS, so a principal built
    straight from ``GroupTeamMapping.resolve`` carried ``team_id="platform-eng"``
    while every grant and stamp carried ``team:...``. Both are strings, so
    nothing raised — access was simply denied forever. Asserting through
    ``AccessDecider`` rather than on the field means the two identities are
    compared the way production compares them.
    """
    _users, teams = stores
    team = teams.create(TeamRecord(id="team:abc123", slug="platform-eng", name="Platform Eng"))

    principal = flow(
        stores, rules=(MappingRule(match="eng-platform", target="platform-eng"),)
    ).provision(raw(groups=("eng-platform",)), auth_method=OIDC_METHOD)

    assert [m.team_id for m in principal.team_memberships] == [team.id]
    assert AccessDecider().decide(
        principal,
        OwnershipStamp(owner_subject="user:someone-else"),
        (
            AccessGrant(
                resource_kind="session",
                resource_id="sess-1",
                grantee=Grantee(kind="team", id=team.id),
                level="read",
            ),
        ),
        "read",
    )


def test_mapping_to_an_unknown_slug_does_not_break_the_login(
    stores: tuple[JsonUserStore, JsonTeamStore],
) -> None:
    """A rule naming no stored team degrades the principal, never fails login.

    A config typo must not be a denial of service for every user it matches,
    and it must not auto-create a resource-owning team either.
    """
    _users, teams = stores

    principal = flow(
        stores, rules=(MappingRule(match="eng-platform", target="typo-team"),)
    ).provision(raw(groups=("eng-platform",)), auth_method=OIDC_METHOD)

    assert principal.team_memberships == ()
    assert teams.list() == []


def test_stored_team_admin_survives_a_login_that_also_maps_the_team(
    stores: tuple[JsonUserStore, JsonTeamStore],
) -> None:
    """Durable wins the conflict — otherwise every login demotes a team admin.

    Mapping cannot express a role, so if it won, a stored ``team_admin`` who
    also matches a group rule would silently drop to ``team_member`` on each
    login and lose admin over their team's resources.
    """
    _users, teams = stores
    teams.create(TeamRecord(id="team:abc123", slug="platform-eng", name="Platform Eng"))
    rules = (MappingRule(match="eng-platform", target="platform-eng"),)

    first = flow(stores, rules=rules).provision(
        raw(groups=("eng-platform",)), auth_method=OIDC_METHOD
    )
    teams.add_member("team:abc123", first.subject, "team_admin")

    second = flow(stores, rules=rules).provision(
        raw(groups=("eng-platform",)), auth_method=OIDC_METHOD
    )

    assert [(m.team_id, m.team_role) for m in second.team_memberships] == [
        ("team:abc123", "team_admin")
    ]


# ── the cookie leg: memberships must survive the next request ─────────────────


def runtime(stores: tuple[JsonUserStore, JsonTeamStore]) -> FederatedRuntime:
    """A federated runtime over *stores*, with no group→team rules configured."""
    users, teams = stores
    built = build_federated_runtime(
        AuthSettings(
            enabled=True,
            authenticators=(
                {
                    "kind": "trusted_header",
                    "name": "proxy",
                    "trusted_proxies": ("127.0.0.1/32",),
                },
            ),
            session=SessionSettings(secret="a-test-session-secret-value"),
        ),
        clock=lambda: NOW,
        user_store=users,
        team_store=teams,
    )
    assert built is not None
    return built


def test_membership_survives_a_cookie_resolved_request(
    stores: tuple[JsonUserStore, JsonTeamStore],
) -> None:
    """``/me`` reports the same teams on request two as it did at login.

    Before this, ``principal_from_record`` hardcoded an empty membership tuple,
    so a team appeared once at login and vanished on every subsequent
    cookie-served request — user-visible, and it made team-scoped access
    depend on which leg answered the request.
    """
    _users, teams = stores
    federated = runtime(stores)
    teams.create(TeamRecord(id="team:abc123", slug="platform-eng", name="Platform Eng"))

    logged_in = federated.provision(raw(), auth_method=OIDC_METHOD)
    teams.add_member("team:abc123", logged_in.subject, "team_admin")

    resolved = federated.resolve_session_cookie(federated.session_cookie(logged_in), now=NOW)

    assert resolved is not None
    assert [(m.team_id, m.team_role) for m in resolved.team_memberships] == [
        ("team:abc123", "team_admin")
    ]


def test_cookie_path_reflects_a_membership_revoked_after_the_cookie_was_minted(
    stores: tuple[JsonUserStore, JsonTeamStore],
) -> None:
    """Memberships are read live, not baked into the cookie.

    The same rule roles and profile already follow: a revocation must take
    effect on the next request rather than at cookie expiry. Reading the store
    is what buys that, so the negative leg is worth pinning — a cookie-carried
    membership would pass the test above and fail this one.
    """
    _users, teams = stores
    federated = runtime(stores)
    teams.create(TeamRecord(id="team:abc123", slug="platform-eng", name="Platform Eng"))
    logged_in = federated.provision(raw(), auth_method=OIDC_METHOD)
    teams.add_member("team:abc123", logged_in.subject)
    cookie = federated.session_cookie(logged_in)

    teams.remove_member("team:abc123", logged_in.subject)
    resolved = federated.resolve_session_cookie(cookie, now=NOW)

    assert resolved is not None
    assert resolved.team_memberships == ()
