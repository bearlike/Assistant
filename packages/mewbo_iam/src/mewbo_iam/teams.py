#!/usr/bin/env python3
"""Teams — the group ownership unit a principal can belong to.

A ``TeamRecord`` is a durable, slug-addressable group; a ``TeamMembership`` is
the (team, role-within-team) edge carried on a ``Principal``. Team membership
feeds two decisions elsewhere: a ``team_admin`` gains admin over resources their
team owns (see ``access.AccessDecider``), and team-scoped access grants match on
the member's team ids.

Membership has two shapes on purpose. ``TeamMembershipRecord`` is what the team
store persists — it names the user, because the store must answer "who is in
this team". ``TeamMembership`` is the projection a ``Principal`` carries, which
drops the user id as redundant: the principal already IS that user. The store
projects one onto the other in exactly one place
(``TeamStoreBase.list_for_user``), so the two shapes cannot drift.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

TeamRole = Literal["team_admin", "team_member"]

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class TeamMembership(BaseModel):
    """A principal's membership in one team, with its role inside that team.

    ``team_id`` holds the team's durable ``TeamRecord.id`` — NEVER its slug.
    That distinction is load-bearing rather than cosmetic: ``AccessDecider``
    matches this field against ``OwnershipStamp.team_id`` and against a team
    grantee's id, both of which carry durable ids. A slug here type-checks,
    stores fine, and then silently matches nothing, so every resource a team
    owns becomes invisible to its own members. A group→team mapping resolves
    its slugs through ``TeamStoreBase.memberships_for_slugs`` for exactly this
    reason.

    Frozen because it rides inside ``Principal``'s immutable membership tuple —
    a membership is replaced wholesale, never mutated in place.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    team_id: str
    team_role: TeamRole = "team_member"


class TeamMembershipRecord(BaseModel):
    """The durable ``(team, user, role)`` edge the team store persists.

    Keyed on ``(team_id, user_id)``: a user holds exactly one role in a team,
    so re-adding an existing member re-roles them rather than storing a second
    edge. Frozen for the same reason as ``TeamMembership`` — an edge is
    replaced wholesale, never mutated in place.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    team_id: str
    user_id: str
    team_role: TeamRole = "team_member"

    @field_validator("team_id", "user_id")
    @classmethod
    def _require_id(cls, value: str) -> str:
        """Reject a blank id — an edge naming nothing is corrupt, not empty."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("team membership ids must not be blank")
        return stripped

    def as_membership(self) -> TeamMembership:
        """Project onto the principal-scoped shape, dropping the redundant user id.

        Lives here rather than beside the store that reads it: turning the
        durable edge into the shape a ``Principal`` carries is intrinsic to
        what the edge means.
        """
        return TeamMembership(team_id=self.team_id, team_role=self.team_role)


class TeamRecord(BaseModel):
    """A durable team: a stable id plus a human-facing slug/name/description.

    ``external_id`` is the identity an external directory knows this team by —
    a SCIM ``Group``'s ``externalId``. It is what lets provisioning look a team
    up before creating one: matching on the IdP's own id survives a group
    rename, whereas matching on a name-derived slug forks a duplicate team the
    moment the directory renames the group.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    slug: str
    name: str
    description: str = ""
    external_id: str | None = None

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, value: str) -> str:
        """Require a lowercase kebab-case slug (url- and mention-safe)."""
        if not _SLUG_RE.match(value):
            raise ValueError(
                f"team slug must be lowercase kebab-case (e.g. 'platform-eng'), got {value!r}"
            )
        return value

    @field_validator("external_id")
    @classmethod
    def _validate_external_id(cls, value: str | None) -> str | None:
        """Reject a blank ``externalId``: absence is ``None``, never ``""``.

        The two are not interchangeable — ``None`` means the directory sent no
        id and the caller must fall back to the derived slug, while a stored
        ``""`` would match every other team that also sent nothing.
        """
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("team external_id must not be blank (use None when absent)")
        return stripped


__all__ = ["TeamRole", "TeamMembership", "TeamMembershipRecord", "TeamRecord"]
