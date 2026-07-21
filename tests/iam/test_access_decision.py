#!/usr/bin/env python3
"""``AccessDecider`` — the single authorization rule, and its four precedence legs.

Every enforcement site in the product reuses this one ``decide``, so each leg
gets its positive AND its negative here: a rule that only ever allows is
indistinguishable from ``return True``. The decision is pure — no store, no
clock, no request — so these drive it directly.
"""

from __future__ import annotations

from typing import Any

import pytest
from mewbo_iam.access import AccessDecider, AccessGrant, Grantee, OwnershipStamp
from mewbo_iam.principal import AuthMethod, Principal
from mewbo_iam.roles import ADMIN_ROLE
from mewbo_iam.teams import TeamMembership
from pydantic import ValidationError

OWNER = "user:owner"
STRANGER = "user:stranger"
TEAM = "team:platform"
OTHER_TEAM = "team:marketing"
RESOURCE_ID = "sess-1"

LEVELS = ("read", "write", "admin")


def principal(subject: str = STRANGER, **overrides: Any) -> Principal:
    """A plain user principal — no roles, no teams — unless overridden."""
    fields: dict[str, Any] = {
        "subject": subject,
        "kind": "user",
        "auth_method": AuthMethod(kind="oidc", issuer="https://idp.example.com"),
    }
    fields.update(overrides)
    return Principal(**fields)


def stamp(*, team_id: str | None = None) -> OwnershipStamp:
    """A resource owned by :data:`OWNER`, optionally belonging to a team."""
    return OwnershipStamp(owner_subject=OWNER, team_id=team_id)


def grant(*, kind: str, id: str, level: str) -> AccessGrant:
    """One share of the resource under test."""
    return AccessGrant(
        resource_kind="session",
        resource_id=RESOURCE_ID,
        grantee=Grantee(kind=kind, id=id),
        level=level,
    )


@pytest.fixture
def decider() -> AccessDecider:
    return AccessDecider()


# ── rule 1: admin bypass ──────────────────────────────────────────────────────
@pytest.mark.parametrize("need", LEVELS)
def test_an_admin_is_allowed_on_a_resource_they_neither_own_nor_were_granted(decider, need):
    """The built-in admin role bypasses ownership and grants at every level."""
    admin = principal(roles=(ADMIN_ROLE,))

    assert decider.decide(admin, stamp(), [], need) is True


@pytest.mark.parametrize("need", LEVELS)
def test_a_non_admin_role_grants_nothing_by_itself(decider, need):
    """Only the built-in admin role bypasses — an arbitrary role does not."""
    almost = principal(roles=("editor", "auditor", "admins"))

    assert decider.decide(almost, stamp(), [], need) is False


# ── rule 2: owner ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("need", LEVELS)
def test_the_owner_is_allowed_at_every_level(decider, need):
    """Owning a resource confers admin-level access to it."""
    assert decider.decide(principal(OWNER), stamp(), [], need) is True


@pytest.mark.parametrize("need", LEVELS)
def test_a_non_owner_with_no_grant_is_refused_at_every_level(decider, need):
    """The default is deny — the rule is not a formality."""
    assert decider.decide(principal(STRANGER), stamp(), [], need) is False


def test_ownership_is_matched_on_the_exact_subject(decider):
    """A subject that merely resembles the owner's is not the owner."""
    assert decider.decide(principal("user:owner2"), stamp(), [], "read") is False
    assert decider.decide(principal("user:own"), stamp(), [], "read") is False


# ── rule 3: team admin of the OWNING team ─────────────────────────────────────
@pytest.mark.parametrize("need", LEVELS)
def test_a_team_admin_of_the_owning_team_is_allowed(decider, need):
    """A team admin gets admin-level access to their team's resources."""
    lead = principal(team_memberships=(TeamMembership(team_id=TEAM, team_role="team_admin"),))

    assert decider.decide(lead, stamp(team_id=TEAM), [], need) is True


def test_a_team_admin_of_a_DIFFERENT_team_is_refused(decider):
    """Team admin is scoped to the owning team, not to team-admin-ness in general."""
    lead_elsewhere = principal(
        team_memberships=(TeamMembership(team_id=OTHER_TEAM, team_role="team_admin"),)
    )

    assert decider.decide(lead_elsewhere, stamp(team_id=TEAM), [], "read") is False


def test_a_plain_member_of_the_owning_team_is_refused(decider):
    """Membership alone is not authority — the role inside the team decides."""
    member = principal(team_memberships=(TeamMembership(team_id=TEAM, team_role="team_member"),))

    assert decider.decide(member, stamp(team_id=TEAM), [], "read") is False


def test_a_team_admin_is_refused_on_a_resource_with_no_owning_team(decider):
    """An unstamped team on the resource means the team leg cannot apply."""
    lead = principal(team_memberships=(TeamMembership(team_id=TEAM, team_role="team_admin"),))

    assert decider.decide(lead, stamp(team_id=None), [], "read") is False


# ── rule 4: an explicit grant ─────────────────────────────────────────────────
@pytest.mark.parametrize("level", LEVELS)
def test_a_matching_user_grant_satisfies_its_own_level(decider, level):
    """A user grant at a level authorizes exactly that level."""
    grants = [grant(kind="user", id=STRANGER, level=level)]

    assert decider.decide(principal(STRANGER), stamp(), grants, level) is True


@pytest.mark.parametrize("level", LEVELS)
def test_a_matching_team_grant_satisfies_its_own_level(decider, level):
    """A team grant authorizes any member of that team at that level."""
    member = principal(team_memberships=(TeamMembership(team_id=TEAM, team_role="team_member"),))
    grants = [grant(kind="team", id=TEAM, level=level)]

    assert decider.decide(member, stamp(), grants, level) is True


def test_a_grant_to_a_different_user_does_not_apply(decider):
    """Grants are matched on the grantee id, not merely counted."""
    grants = [grant(kind="user", id="user:someone-else", level="admin")]

    assert decider.decide(principal(STRANGER), stamp(), grants, "read") is False


def test_a_grant_to_a_team_the_principal_does_not_belong_to_does_not_apply(decider):
    """A team grant reaches members of THAT team only."""
    outsider = principal(
        team_memberships=(TeamMembership(team_id=OTHER_TEAM, team_role="team_admin"),)
    )
    grants = [grant(kind="team", id=TEAM, level="admin")]

    assert decider.decide(outsider, stamp(), grants, "read") is False


def test_a_user_grant_does_not_match_a_team_of_the_same_id(decider):
    """The grantee KIND is load-bearing — a user grant is not a team grant."""
    member = principal(team_memberships=(TeamMembership(team_id=TEAM, team_role="team_member"),))
    grants = [grant(kind="user", id=TEAM, level="admin")]

    assert decider.decide(member, stamp(), grants, "read") is False


# ── level ordering: admin ⊇ write ⊇ read ──────────────────────────────────────
def test_a_read_grant_does_not_satisfy_a_write_need(decider):
    """The escalation this rule exists to prevent: read never becomes write."""
    grants = [grant(kind="user", id=STRANGER, level="read")]

    assert decider.decide(principal(STRANGER), stamp(), grants, "write") is False


def test_a_read_grant_does_not_satisfy_an_admin_need(decider):
    """Nor does read reach admin."""
    grants = [grant(kind="user", id=STRANGER, level="read")]

    assert decider.decide(principal(STRANGER), stamp(), grants, "admin") is False


def test_a_write_grant_does_not_satisfy_an_admin_need(decider):
    """Write stops short of admin."""
    grants = [grant(kind="user", id=STRANGER, level="write")]

    assert decider.decide(principal(STRANGER), stamp(), grants, "admin") is False


def test_a_write_grant_satisfies_a_read_need(decider):
    """A higher level covers a lower one — write implies read."""
    grants = [grant(kind="user", id=STRANGER, level="write")]

    assert decider.decide(principal(STRANGER), stamp(), grants, "read") is True


@pytest.mark.parametrize("need", LEVELS)
def test_an_admin_grant_satisfies_every_level(decider, need):
    """Admin is the top of the ordering and covers all three needs."""
    grants = [grant(kind="user", id=STRANGER, level="admin")]

    assert decider.decide(principal(STRANGER), stamp(), grants, need) is True


@pytest.mark.parametrize(
    ("held", "need", "expected"),
    [
        ("read", "read", True),
        ("read", "write", False),
        ("read", "admin", False),
        ("write", "read", True),
        ("write", "write", True),
        ("write", "admin", False),
        ("admin", "read", True),
        ("admin", "write", True),
        ("admin", "admin", True),
    ],
)
def test_the_full_level_ordering_matrix(held, need, expected):
    """``AccessGrant.satisfies`` ranks the levels — the whole 3×3 truth table."""
    assert grant(kind="user", id=STRANGER, level=held).satisfies(need) is expected


# ── a grant list is scanned, not sampled ──────────────────────────────────────
def test_a_sufficient_grant_later_in_the_list_still_applies(decider):
    """Every grant is considered — an early non-match does not end the scan."""
    grants = [
        grant(kind="user", id="user:someone-else", level="admin"),
        grant(kind="team", id=OTHER_TEAM, level="admin"),
        grant(kind="user", id=STRANGER, level="write"),
    ]

    assert decider.decide(principal(STRANGER), stamp(), grants, "write") is True


def test_an_insufficient_grant_does_not_shadow_a_sufficient_one(decider):
    """Holding both read and write, the write grant is what answers a write need."""
    grants = [
        grant(kind="user", id=STRANGER, level="read"),
        grant(kind="user", id=STRANGER, level="write"),
    ]

    assert decider.decide(principal(STRANGER), stamp(), grants, "write") is True


# ── service principals go through the same rule ───────────────────────────────
def test_a_service_principal_is_subject_to_the_same_rule(decider):
    """Nothing about ``kind="service"`` shortcuts the decision either way."""
    service = principal(
        "svc:key-1", kind="service", auth_method=AuthMethod(kind="api_key")
    )

    assert decider.decide(service, stamp(), [], "read") is False
    granted = [grant(kind="user", id="svc:key-1", level="read")]
    assert decider.decide(service, stamp(), granted, "read") is True


def test_the_decision_is_pure_and_leaves_its_inputs_untouched(decider):
    """No hidden mutation: the same call answers the same way every time."""
    subject = principal(STRANGER)
    grants = [grant(kind="user", id=STRANGER, level="write")]
    ownership = stamp(team_id=TEAM)

    first = decider.decide(subject, ownership, grants, "write")

    assert decider.decide(subject, ownership, grants, "write") is first
    assert len(grants) == 1
    assert subject.team_memberships == ()


# ── blank identifiers cannot reach the equality comparison ────────────────────
# ``decide`` matches an owner by plain string equality and ``Principal.subject``
# does NOT reject a blank value. The stamp/grantee validators are therefore the
# only thing standing between a blank-subject principal and ownership of every
# resource whose stamp stored ``""``. These pin that they hold at DEFINITION, so
# the dangerous comparison is unreachable rather than merely unlikely.


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
def test_a_blank_owner_subject_is_refused_at_definition(blank):
    """A stamp naming nobody is corrupt, not a resource owned by the empty subject."""
    with pytest.raises(ValidationError, match="must not be blank"):
        OwnershipStamp(owner_subject=blank)


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_team_id_is_refused_because_absence_is_none(blank):
    """No owning team is ``None``; ``""`` would be a team every teamless caller shares."""
    with pytest.raises(ValidationError, match="must not be blank"):
        OwnershipStamp(owner_subject=OWNER, team_id=blank)


@pytest.mark.parametrize("blank", ["", "   "])
@pytest.mark.parametrize("kind", ["user", "team"])
def test_a_blank_grantee_id_is_refused_at_definition(kind, blank):
    """A grant to nobody would match any principal carrying a blank subject."""
    with pytest.raises(ValidationError, match="must not be blank"):
        Grantee(kind=kind, id=blank)


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_subject_principal_cannot_be_built_at_all(blank):
    """Both sides of the owner comparison refuse a blank — belt and braces.

    The principal side is the stronger guard: a blank subject cannot be
    constructed, so the dangerous equality is unreachable rather than merely
    losing. The stamp side is kept because a blank can enter a stamp with no
    principal involved — a corrupt stored record, or a future writer — so the
    two close different doors and neither retires the other.
    """
    with pytest.raises(ValidationError):
        principal(blank)

    with pytest.raises(ValidationError):
        OwnershipStamp(owner_subject=blank)


def test_a_subject_without_a_known_prefix_is_refused(decider):
    """``kind`` is INFERRED from the prefix, so an unknown one is not inert.

    Two call sites derive ``kind`` as "user if it starts with ``user:`` else
    service", which means a malformed subject does not fail — it silently
    resolves as a SERVICE principal. Blank is only the most visible member of
    that class, so the validator pins the prefix, not merely non-emptiness.
    """
    for malformed in ("owner", "user", "admin:root", ":user:owner"):
        with pytest.raises(ValidationError):
            principal(malformed)


def test_surrounding_whitespace_is_stripped_from_an_identifier():
    """Identifiers normalize, so one stored id has exactly one spelling."""
    assert OwnershipStamp(owner_subject="  user:owner  ").owner_subject == OWNER
    assert OwnershipStamp(owner_subject=OWNER, team_id="  team:platform ").team_id == TEAM
    assert Grantee(kind="user", id=f"  {STRANGER} ").id == STRANGER


def test_a_padded_principal_subject_never_grants_ownership(decider):
    """Whitespace must never be the difference between denied and owner.

    The stamp normalizes its identifier. Whether ``Principal`` also normalizes
    or refuses the padded form outright, the one outcome that must never occur
    is a padded subject silently matching a stripped stamp on some other path.
    This pins the safe direction so a future "make these consistent" change
    cannot quietly invert it into a grant.
    """
    try:
        padded = principal(f"  {OWNER}  ")
    except ValidationError:
        return  # refused at definition — the dangerous comparison is unreachable

    # Accepted, so it must normalize to the same identity the stamp stores;
    # anything else would mean two spellings of one owner.
    assert padded.subject == OWNER
    assert decider.decide(padded, stamp(), [], "read") is True
