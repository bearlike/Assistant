#!/usr/bin/env python3
"""JIT provisioning — a verified ``RawIdentity`` becomes a durable ``Principal``.

This is the join between "the IdP asserted these claims" and "this is who they are
here". :class:`IdentityFlow` applies the deployment's group→role and group→team
mappings (plus the cold-start bootstrap-admin rule), upserts the ``UserRecord``
keyed by external subject (so a returning user links to the same record instead of
forking one), and assembles the request-scoped ``Principal``.

Every decision is pure and testable: the mappings, the bootstrap rule and the
avatar policy are kernel models that own their own logic; the clock is injected;
the store is the only collaborator that touches I/O.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime

from mewbo_iam import (
    AuthMethod,
    AvatarPolicy,
    BootstrapRule,
    ExternalSubject,
    GroupRoleMapping,
    GroupTeamMapping,
    Principal,
    RawIdentity,
    TeamStoreBase,
    UserRecord,
    UserStoreBase,
)
from mewbo_iam.principal import ADMIN_ROLE


class DisabledUserError(Exception):
    """A previously provisioned user has been disabled — login is refused.

    Raised, never swallowed: JIT upsert would otherwise re-activate a
    deliberately disabled account on its owner's next login.
    """


class IdentityFlow:
    """Turns a verified identity into a provisioned principal (JIT).

    Atomic class: the user and team stores, the two group mappings, the bootstrap
    rule, the avatar policy and the clock are DI'd fields; :meth:`provision` is
    its behavior. Roles are IdP-authoritative — they are re-derived from the
    mapping on every login, so a group change takes effect immediately; the
    record's ``status`` and identity (``id``/``created_at``) are the only things
    preserved across logins.

    Teams work the same way but merge with durable state rather than replacing
    it, because the two sources answer different questions: the mapping says
    "the IdP's groups put you here right now", the team store says "an operator
    or SCIM put you here, possibly as ``team_admin``". Only the store can
    express a role, so it wins a conflict — see
    ``Principal.with_team_memberships``.
    """

    def __init__(
        self,
        *,
        user_store: UserStoreBase,
        team_store: TeamStoreBase,
        role_mapping: GroupRoleMapping,
        team_mapping: GroupTeamMapping,
        bootstrap: BootstrapRule | None,
        avatar_policy: AvatarPolicy,
        clock: Callable[[], datetime],
    ) -> None:
        """Capture the stores and the pure mapping/bootstrap/avatar collaborators."""
        self._users = user_store
        self._teams = team_store
        self._role_mapping = role_mapping
        self._team_mapping = team_mapping
        self._bootstrap = bootstrap
        self._avatar_policy = avatar_policy
        self._clock = clock

    def provision(
        self,
        raw: RawIdentity,
        *,
        auth_method: AuthMethod,
        now: datetime | None = None,
    ) -> Principal:
        """Provision *raw* into a :class:`Principal`, upserting the durable record.

        Refuses a disabled account. Roles come from the group→role mapping (with
        its least-privilege default) plus the bootstrap-admin rule; teams from the
        team store's durable edges unioned with the group→team mapping. Email /
        verification / display / picture are refreshed from the assertion each
        login.
        """
        moment = now if now is not None else self._clock()
        existing = self._users.get_by_external(raw.external_subject)
        if existing is not None and existing.status == "disabled":
            raise DisabledUserError(f"user {existing.id} is disabled")

        roles = self._resolve_roles(raw)
        record = UserRecord(
            id=existing.id if existing is not None else f"user:{uuid.uuid4().hex}",
            external_identities=self._merge_identities(existing, raw.external_subject),
            email=raw.email,
            email_verified=raw.email_verified,
            display_name=raw.display,
            picture_url=raw.picture_url,
            status=existing.status if existing is not None else "active",
            roles=roles,
            created_at=existing.created_at if existing is not None else moment,
            updated_at=moment,
        )
        stored = self._users.upsert(record)
        return self._principal_from(stored, raw, auth_method)

    def _resolve_roles(self, raw: RawIdentity) -> tuple[str, ...]:
        """Group→role mapping (default-backed) plus the bootstrap-admin escalation."""
        roles = list(self._role_mapping.resolve(raw.groups))
        if self._bootstrap is not None and self._bootstrap.is_admin(
            raw.groups, raw.external_subject.subject
        ):
            if ADMIN_ROLE not in roles:
                roles.insert(0, ADMIN_ROLE)
        return tuple(roles)

    @staticmethod
    def _merge_identities(
        existing: UserRecord | None, external: ExternalSubject
    ) -> tuple[ExternalSubject, ...]:
        """Preserve any already-linked external identities, adding this one."""
        if existing is None:
            return (external,)
        if external in existing.external_identities:
            return existing.external_identities
        return (*existing.external_identities, external)

    def _principal_from(
        self, record: UserRecord, raw: RawIdentity, auth_method: AuthMethod
    ) -> Principal:
        """Assemble the request-scoped principal from the stored record + assertion.

        Teams arrive from BOTH sources and are merged by the kernel's one
        precedence seam. The group→team mapping yields SLUGS, so it resolves
        through ``memberships_for_slugs`` — writing a slug into a field named
        ``team_id`` type-checks and then matches no ownership stamp or team
        grant, making every resource the team owns invisible to its members.
        """
        mapped = self._teams.memberships_for_slugs(self._team_mapping.resolve(raw.groups))
        principal = Principal(
            subject=record.id,
            kind="user",
            display_name=record.display_name,
            email=record.email,
            email_verified=record.email_verified,
            picture_url=record.picture_url,
            roles=record.roles,
            scopes=None,
            auth_method=auth_method,
            external_subject=raw.external_subject,
        )
        return principal.with_team_memberships(self._teams.list_for_user(record.id), mapped=mapped)


__all__ = ["IdentityFlow", "DisabledUserError"]
