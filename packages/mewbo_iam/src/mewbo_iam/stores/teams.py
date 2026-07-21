#!/usr/bin/env python3
"""Team store — durable ``TeamRecord`` persistence plus the membership edge.

This store owns BOTH sides of team membership: the ``TeamRecord`` and the
``TeamMembershipRecord`` edges pointing at it. That pairing is deliberate — a
team delete has to drop its memberships, and only the store that persists them
can guarantee it (see :meth:`TeamStoreBase.delete`). The edges live in their own
collection rather than inside the team document, so a full-document
:meth:`TeamStoreBase.update` (which SCIM performs on every group rename) cannot
silently clear a team's roster.

The base keeps every rule that is not storage: uniqueness, the delete cascade,
membership upsert semantics, edge ordering, and the durable-edge → ``Principal``
projection. Drivers implement only the private reads/writes underneath, so the
two of them cannot disagree about behavior — the json and mongodb backends
differ in storage mechanism and nothing else.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from mewbo_core.common import get_logger

from mewbo_iam.stores._json import _JsonCollectionStore
from mewbo_iam.teams import TeamMembership, TeamMembershipRecord, TeamRecord, TeamRole

logging = get_logger(name="iam.stores.teams")

_DATA_FILENAME = "iam_teams.json"
_MEMBERS_FILENAME = "iam_team_members.json"


class TeamStoreBase(abc.ABC):
    """Abstract interface for team storage backends."""

    # -- team records ------------------------------------------------------

    def create(self, team: TeamRecord) -> TeamRecord:
        """Persist a new team and return it.

        Raises ``ValueError`` if the id, slug or ``external_id`` is already
        taken. The check is concrete so both drivers reject a collision the
        same way: mongodb alone would raise a driver-specific duplicate-key
        error while json silently stored the second copy, and a duplicated
        ``external_id`` makes SCIM's lookup-before-create pick an arbitrary
        team. The unique indexes remain as the backstop for the read-then-write
        race this check cannot close.
        """
        self._assert_unique(team)
        return self._insert_team(team)

    def update(self, team: TeamRecord) -> TeamRecord:
        """Full-document replace by id. Raises ``KeyError`` if absent.

        Uniqueness is re-checked against the OTHER teams for the same reason
        :meth:`create` checks it. Memberships are untouched: they are separate
        records precisely so a replace cannot clear them.
        """
        self._assert_unique(team, replacing=team.id)
        return self._replace_team(team)

    def _assert_unique(self, team: TeamRecord, *, replacing: str | None = None) -> None:
        """Reject a team colliding with a stored one on id, slug or external id."""
        for stored in self.list():
            if stored.id == replacing:
                continue
            if stored.id == team.id:
                raise ValueError(f"team id {team.id!r} already exists")
            if stored.slug == team.slug:
                raise ValueError(f"team slug {team.slug!r} already exists")
            if team.external_id is not None and stored.external_id == team.external_id:
                raise ValueError(f"team external_id {team.external_id!r} already exists")

    @abc.abstractmethod
    def get(self, team_id: str) -> TeamRecord | None:
        """Return a team by id, or ``None``."""

    @abc.abstractmethod
    def get_by_slug(self, slug: str) -> TeamRecord | None:
        """Return a team by slug, or ``None`` — the mapping-target lookup."""

    @abc.abstractmethod
    def get_by_external_id(self, external_id: str) -> TeamRecord | None:
        """Return the team a directory knows by ``external_id``, or ``None``.

        The SCIM lookup-before-create identity. Prefer it over
        :meth:`get_by_slug` whenever the directory sent an ``externalId``: the
        slug is derived from the group's display name, so a rename in the IdP
        would otherwise fork a second team instead of updating the first.
        """

    @abc.abstractmethod
    def list(self) -> list[TeamRecord]:
        """Return all teams."""

    def delete(self, team_id: str) -> bool:
        """Delete a team and every membership edge pointing at it.

        Returns ``True`` if a team was removed. The cascade is concrete so no
        driver can forget it: a stranded edge would keep surfacing in
        :meth:`list_for_user` as a team that no longer exists, and a later team
        reusing the id would silently inherit the old roster.
        """
        removed = self._remove_team(team_id)
        if removed:
            self._drop_memberships(team_id=team_id)
        return removed

    @abc.abstractmethod
    def _insert_team(self, team: TeamRecord) -> TeamRecord:
        """Store a new team document, uniqueness already checked."""

    @abc.abstractmethod
    def _replace_team(self, team: TeamRecord) -> TeamRecord:
        """Overwrite an existing team by id. Raises ``KeyError`` if absent."""

    @abc.abstractmethod
    def _remove_team(self, team_id: str) -> bool:
        """Delete just the team document, without its edges. See :meth:`delete`."""

    # -- membership --------------------------------------------------------

    def add_member(
        self, team_id: str, user_id: str, team_role: TeamRole = "team_member"
    ) -> TeamMembershipRecord:
        """Add a user to a team, or re-role one already in it.

        Idempotent on ``(team_id, user_id)``: a second call with a different
        role replaces the role rather than storing a second edge, so a
        principal can never hold two roles in one team (which would make
        ``AccessDecider``'s ``team_admin`` rule depend on read order).

        Raises ``KeyError`` if the team does not exist — admitting an edge to a
        missing team would recreate exactly the stranded rows :meth:`delete`
        exists to prevent. The user id is deliberately NOT checked: the user
        store is a sibling this one must not import (see :meth:`list_members`
        for what that means for a deleted user).
        """
        if self.get(team_id) is None:
            raise KeyError(f"Team {team_id} not found")
        record = TeamMembershipRecord(team_id=team_id, user_id=user_id, team_role=team_role)
        self._put_membership(record)
        return record

    def remove_member(self, team_id: str, user_id: str) -> bool:
        """Remove one user's membership in one team.

        Returns ``True`` if an edge was removed. Removing a member from a team
        they are not in is a no-op, not an error — SCIM replays the same
        remove on every sync.
        """
        return self._drop_memberships(team_id=team_id, user_id=user_id) > 0

    def list_members(self, team_id: str) -> tuple[TeamMembershipRecord, ...]:
        """Every membership edge on one team, ordered.

        Returns edges, not users: resolving each ``user_id`` into a profile is
        the caller's job, because the user store is a separate collaborator
        this store does not import.

        That resolution is also where a STRANDED edge disappears. Deleting a
        user does NOT cascade here — the user store cannot reach this one
        without coupling every user write to team storage — so an edge can
        outlive its user. Callers resolve ids and skip what no longer exists
        (the SCIM group projection already works this way), which makes a
        stranded edge inert rather than a broken read. In normal operation it
        does not arise at all: the product disables users, it does not delete
        them.
        """
        return self._ordered(self._membership_records_for_team(team_id))

    def replace_members(
        self, team_id: str, user_ids: Sequence[str]
    ) -> tuple[TeamMembershipRecord, ...]:
        """Set a team's roster to exactly ``user_ids``, and return it.

        The SCIM ``replace`` verb, done once here rather than hand-rolled as a
        diff at the call site — because the obvious hand-rolled version
        (``drop everything, add each id``) DEMOTES every ``team_admin`` in the
        team to ``team_member``. SCIM's member list carries no roles, so a
        retained member's existing role is preserved; only genuinely new
        members are added, as ``team_member``.

        Raises ``KeyError`` if the team does not exist. An empty ``user_ids``
        clears the roster, which is what an empty SCIM replace means.
        """
        if self.get(team_id) is None:
            raise KeyError(f"Team {team_id} not found")
        roles = {record.user_id: record.team_role for record in self.list_members(team_id)}
        wanted = list(dict.fromkeys(user_ids))
        for user_id in roles:
            if user_id not in wanted:
                self.remove_member(team_id, user_id)
        for user_id in wanted:
            self.add_member(team_id, user_id, roles.get(user_id, "team_member"))
        return self.list_members(team_id)

    def memberships_for_slugs(self, slugs: Sequence[str]) -> tuple[TeamMembership, ...]:
        """Resolve team SLUGS onto memberships carrying durable team ids.

        The seam a group→team mapping goes through: ``GroupTeamMapping.resolve``
        yields slugs, while everything downstream (``AccessDecider``,
        ``OwnershipStamp.team_id``, team grants) matches on durable ids. Doing
        the lookup here is what stops a slug being written into a field named
        ``team_id`` (see ``TeamMembership``).

        **A slug naming no stored team is SKIPPED, not created.** Auto-creating
        one would make login a write path — minting a durable, resource-owning
        team from a config typo, and racing two concurrent logins into a
        uniqueness failure that fails the login itself. Teams are created
        deliberately, through SCIM or the admin surface. An unmatched rule
        degrades the principal instead of breaking the request, matching the
        way an unknown role name is inert in ``Principal.effective_permissions``.
        Each miss is logged, because a mapping that silently resolves to
        nothing is otherwise an ops mystery.

        Every membership returned is a plain ``team_member``: a mapping cannot
        express a role. See ``Principal.with_team_memberships`` for why that
        makes stored memberships win a conflict.
        """
        memberships: list[TeamMembership] = []
        seen: set[str] = set()
        for slug in slugs:
            team = self.get_by_slug(slug)
            if team is None:
                logging.warning(
                    "Group→team mapping targets team slug {!r}, which does not exist; skipping.",
                    slug,
                )
                continue
            if team.id in seen:
                continue
            seen.add(team.id)
            memberships.append(TeamMembership(team_id=team.id))
        return tuple(memberships)

    def list_for_user(self, user_id: str) -> tuple[TeamMembership, ...]:
        """Every team this user belongs to, in the shape a ``Principal`` carries.

        The ONE place the durable edge is projected onto the request-scoped
        ``TeamMembership``, so the drivers cannot disagree about it. Feed the
        result straight to ``Principal.with_team_memberships``.
        """
        return tuple(
            record.as_membership()
            for record in self._ordered(self._membership_records_for_user(user_id))
        )

    @staticmethod
    def _ordered(
        records: Sequence[TeamMembershipRecord],
    ) -> tuple[TeamMembershipRecord, ...]:
        """Sort edges by ``(team_id, user_id)`` — the ONE place they are ordered.

        Mirrors the audit store's rule: a driver returns matching records in
        whatever order its backend produced (json insertion order, mongodb
        natural order), and the base imposes the single ordering both callers
        see. Never push this sort down — that is how the two drivers drift.
        """
        return tuple(sorted(records, key=lambda record: (record.team_id, record.user_id)))

    @abc.abstractmethod
    def _put_membership(self, record: TeamMembershipRecord) -> None:
        """Upsert one edge, keyed on ``(team_id, user_id)``. See :meth:`add_member`."""

    @abc.abstractmethod
    def _drop_memberships(self, *, team_id: str, user_id: str | None = None) -> int:
        """Delete a team's edges, or one user's edge in it. Returns the count."""

    @abc.abstractmethod
    def _membership_records_for_team(self, team_id: str) -> Sequence[TeamMembershipRecord]:
        """Every edge on one team, unordered — the base orders them."""

    @abc.abstractmethod
    def _membership_records_for_user(self, user_id: str) -> Sequence[TeamMembershipRecord]:
        """Every edge held by one user, unordered — the base orders them."""


class _JsonMemberFile(_JsonCollectionStore):
    """The membership sidecar file ``JsonTeamStore`` owns beside its team file.

    Carries the second file's path and lock, and the edge operations over it,
    so the team store composes one collaborator instead of reaching into a
    second file's internals. It is never driver-resolved on its own — the
    mongodb half of these edges is a second collection on ``MongoTeamStore`` —
    so it declares no ``_MONGO_DRIVER``.
    """

    _FILENAME = _MEMBERS_FILENAME

    def put(self, record: TeamMembershipRecord) -> None:
        """Replace any edge for this ``(team, user)``, then append the new one."""
        with self._lock:
            records = self._read()
            remaining = [r for r in records if not self._is_edge(r, record.team_id, record.user_id)]
            remaining.append(record.model_dump(mode="json"))
            self._write(remaining)

    def drop(self, *, team_id: str, user_id: str | None = None) -> int:
        """Delete a team's edges, or one user's edge in it. Returns the count."""
        with self._lock:
            records = self._read()
            remaining = [r for r in records if not self._is_edge(r, team_id, user_id)]
            dropped = len(records) - len(remaining)
            if dropped:
                self._write(remaining)
        return dropped

    def matching(self, field: str, value: str) -> list[TeamMembershipRecord]:
        """Every edge whose ``field`` equals ``value``."""
        with self._lock:
            records = self._read()
        return [
            TeamMembershipRecord.model_validate(record)
            for record in records
            if record.get(field) == value
        ]

    @staticmethod
    def _is_edge(record: dict[str, Any], team_id: str, user_id: str | None) -> bool:
        """Whether a stored edge belongs to ``team_id`` (and ``user_id`` if given)."""
        if record.get("team_id") != team_id:
            return False
        return user_id is None or record.get("user_id") == user_id


class JsonTeamStore(_JsonCollectionStore, TeamStoreBase):
    """JSON file-backed team store."""

    _FILENAME = _DATA_FILENAME
    _MONGO_DRIVER = "MongoTeamStore"

    def __init__(self, path: str | Path | None = None) -> None:
        """Bind the team file, and the membership sidecar beside it.

        The sidecar's path is derived from this store's own rather than
        resolved independently, so a test pointing the store at a temp
        directory gets both files there.
        """
        super().__init__(path)
        self._members = _JsonMemberFile(path=self._path.with_name(_MEMBERS_FILENAME))

    def _insert_team(self, team: TeamRecord) -> TeamRecord:
        """Append a new team record."""
        with self._lock:
            records = self._read()
            records.append(team.model_dump(mode="json"))
            self._write(records)
        return team

    def get(self, team_id: str) -> TeamRecord | None:
        """Look up a team by id."""
        return self._find("id", team_id)

    def get_by_slug(self, slug: str) -> TeamRecord | None:
        """Look up a team by slug."""
        return self._find("slug", slug)

    def get_by_external_id(self, external_id: str) -> TeamRecord | None:
        """Look up a team by the id its directory knows it as."""
        return self._find("external_id", external_id)

    def _find(self, field: str, value: str) -> TeamRecord | None:
        """First team whose ``field`` equals ``value``, or ``None``."""
        with self._lock:
            records = self._read()
        for record in records:
            if record.get(field) == value:
                return TeamRecord.model_validate(record)
        return None

    def _replace_team(self, team: TeamRecord) -> TeamRecord:
        """Full-document replace by id."""
        with self._lock:
            records = self._read()
            for index, record in enumerate(records):
                if record.get("id") == team.id:
                    records[index] = team.model_dump(mode="json")
                    self._write(records)
                    return team
        raise KeyError(f"Team {team.id} not found")

    def list(self) -> list[TeamRecord]:
        """Return all teams."""
        with self._lock:
            records = self._read()
        return [TeamRecord.model_validate(record) for record in records]

    def _remove_team(self, team_id: str) -> bool:
        """Delete a team document by id."""
        with self._lock:
            records = self._read()
            remaining = [r for r in records if r.get("id") != team_id]
            if len(remaining) == len(records):
                return False
            self._write(remaining)
        return True

    def _put_membership(self, record: TeamMembershipRecord) -> None:
        """Upsert one edge into the sidecar file."""
        self._members.put(record)

    def _drop_memberships(self, *, team_id: str, user_id: str | None = None) -> int:
        """Delete a team's edges, or one user's edge in it."""
        return self._members.drop(team_id=team_id, user_id=user_id)

    def _membership_records_for_team(self, team_id: str) -> Sequence[TeamMembershipRecord]:
        """Every edge on one team."""
        return self._members.matching("team_id", team_id)

    def _membership_records_for_user(self, user_id: str) -> Sequence[TeamMembershipRecord]:
        """Every edge held by one user."""
        return self._members.matching("user_id", user_id)


def create_team_store(path: str | Path | None = None) -> TeamStoreBase:
    """Return the configured team store driver (json or mongodb)."""
    return JsonTeamStore.resolve_driver(path)


__all__ = ["TeamStoreBase", "JsonTeamStore", "create_team_store"]
