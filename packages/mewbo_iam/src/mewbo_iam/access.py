#!/usr/bin/env python3
"""Resource ownership, explicit grants, and the pure access decision.

Every ownable resource (a session, a wiki project, a search run, an app, a
trigger, a key) carries an ``OwnershipStamp``. Sharing beyond the owner happens
through ``AccessGrant`` records. ``AccessDecider`` folds the two into one pure
``decide`` — no store, no clock, no request — so the same rule is testable in
isolation and reused verbatim at every enforcement site.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from mewbo_iam.principal import Principal

ResourceKind = Literal["session", "wiki_project", "search_run", "app", "trigger", "key"]
AccessLevel = Literal["read", "write", "admin"]


class OwnershipStamp(BaseModel):
    """Who owns a resource: a subject, and optionally the team it belongs to."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_subject: str
    team_id: str | None = None

    @field_validator("owner_subject", "team_id")
    @classmethod
    def _require_identifier(cls, value: str | None) -> str | None:
        """Reject a blank id — absence of a team is ``None``, never ``""``.

        Load-bearing rather than hygiene: ``decide`` matches an owner by string
        equality, so a stamp that stored ``""`` would be one blank principal
        subject away from handing admin to the wrong caller. Rejecting it at
        definition means that comparison can never be reached.
        """
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("ownership identifiers must not be blank (use None for no team)")
        return stripped


class Grantee(BaseModel):
    """The target of a grant — a single user subject or a whole team."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["user", "team"]
    id: str

    @field_validator("id")
    @classmethod
    def _require_id(cls, value: str) -> str:
        """Reject a blank grantee id — see ``OwnershipStamp._require_identifier``."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("grantee id must not be blank")
        return stripped


class AccessGrant(BaseModel):
    """An explicit share of one resource to one grantee at one level."""

    model_config = ConfigDict(extra="forbid")

    # admin ⊇ write ⊇ read. Ranking the levels is intrinsic to what a level
    # MEANS, so it lives on the model that carries one rather than beside the
    # decider that happens to read it.
    _LEVEL_ORDER: ClassVar[Mapping[AccessLevel, int]] = {"read": 0, "write": 1, "admin": 2}

    resource_kind: ResourceKind
    resource_id: str
    grantee: Grantee
    level: AccessLevel

    def satisfies(self, need: AccessLevel) -> bool:
        """Whether this grant's level covers ``need`` (a higher level covers a lower)."""
        return self._LEVEL_ORDER[self.level] >= self._LEVEL_ORDER[need]


class AccessDecider(BaseModel):
    """The single authorization rule over ownership + grants.

    Precedence (first satisfied wins, all short-circuit to allow):

    1. **admin role** — a principal with the built-in admin role bypasses.
    2. **owner** — the resource's ``owner_subject`` gets admin-level access.
    3. **team admin** — a ``team_admin`` of the owning team gets admin-level.
    4. **matching grant** — a user/team grant whose level ⊇ the needed level.

    ``grants`` must already be the grants applicable to the resource in question
    (the caller reads them by ``resource_kind``/``resource_id`` from the grant
    store); ``decide`` compares only grantee and level.

    **This answers the resource question ONLY.** A ``True`` means the principal
    has a relationship to this resource, not that it may perform the operation:
    role permissions (``Principal.effective_permissions``) and the three-state
    ``Principal.scopes`` narrowing are separate gates a caller must apply as
    well. Treating ``decide`` as the whole authorization is how a scope-limited
    service key would reach an operation its key never granted — the two checks
    compose, neither replaces the other.
    """

    model_config = ConfigDict(extra="forbid")

    def decide(
        self,
        principal: Principal,
        stamp: OwnershipStamp,
        grants: Sequence[AccessGrant],
        need: AccessLevel,
    ) -> bool:
        """Whether ``principal`` may act on the stamped resource at ``need`` level."""
        if principal.is_admin:
            return True
        if stamp.owner_subject == principal.subject:
            return True
        if stamp.team_id is not None and any(
            m.team_id == stamp.team_id and m.team_role == "team_admin"
            for m in principal.team_memberships
        ):
            return True

        member_team_ids = {m.team_id for m in principal.team_memberships}
        for grant in grants:
            if not grant.satisfies(need):
                continue
            grantee = grant.grantee
            if grantee.kind == "user" and grantee.id == principal.subject:
                return True
            if grantee.kind == "team" and grantee.id in member_team_ids:
                return True
        return False


__all__ = [
    "ResourceKind",
    "AccessLevel",
    "OwnershipStamp",
    "Grantee",
    "AccessGrant",
    "AccessDecider",
]
