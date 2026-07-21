#!/usr/bin/env python3
"""MongoDB-backed identity store drivers.

One collection per store, keyed on each model's natural key (``id`` for users
and teams, ``name`` for roles; grants and audit events are keyless value/append
records). The team store is the one exception: it owns a second
``iam_team_members`` collection for the membership edges, because a team delete
has to drop them and only their owner can guarantee that.
``pymongo`` is imported here and only here — each ``create_*_store``
factory imports this module lazily, so a json-driver install never needs a
reachable MongoDB (mirrors ``key_store_mongo``/``triggers.store_mongo``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from mewbo_core.config import get_config_value
from pymongo import ASCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from mewbo_iam.access import AccessGrant, Grantee
from mewbo_iam.audit import AuthAuditEvent
from mewbo_iam.principal import ExternalSubject
from mewbo_iam.roles import RoleRecord
from mewbo_iam.stores.audit import AuthAuditStoreBase
from mewbo_iam.stores.grants import AccessGrantStoreBase
from mewbo_iam.stores.roles import RoleStoreBase
from mewbo_iam.stores.teams import TeamStoreBase
from mewbo_iam.stores.users import UserStoreBase
from mewbo_iam.teams import TeamMembershipRecord, TeamRecord
from mewbo_iam.users import UserRecord

# Projection that drops Mongo's surrogate ``_id`` from every read (the models
# carry their own natural keys and forbid extra fields).
_NO_ID: dict[str, int] = {"_id": 0}


class _MongoCollectionStore:
    """Shared connection + collection handle for the identity Mongo drivers."""

    def __init__(
        self,
        collection_name: str,
        *,
        uri: str | None = None,
        database: str | None = None,
    ) -> None:
        """Connect, fail fast on an unreachable server, and bind the collection."""
        if uri is None:
            uri = get_config_value("storage", "mongodb", "uri", default="mongodb://localhost:27017")
        if database is None:
            database = get_config_value("storage", "mongodb", "database", default="mewbo")

        self._client: MongoClient = MongoClient(
            uri, maxPoolSize=10, minPoolSize=2, serverSelectionTimeoutMS=5000
        )
        self._db: Database = self._client[database]
        try:
            self._client.admin.command("ping")
        except Exception as exc:
            raise ConnectionError(
                f"MongoDB is unreachable at the configured URI. "
                f"Check MEWBO_MONGODB_URI and ensure MongoDB is running. Error: {exc}"
            ) from exc
        self._col: Collection = self._db[collection_name]


class MongoUserStore(_MongoCollectionStore, UserStoreBase):
    """MongoDB-backed user store — one ``iam_users`` collection."""

    def __init__(self, *, uri: str | None = None, database: str | None = None) -> None:
        """Initialize the connection and ensure lookup indexes."""
        super().__init__("iam_users", uri=uri, database=database)
        self._col.create_index([("id", ASCENDING)], name="ix_iam_users_id", unique=True)
        self._col.create_index(
            [("external_identities.issuer", ASCENDING), ("external_identities.subject", ASCENDING)],
            name="ix_iam_users_external",
        )

    def create(self, user: UserRecord) -> UserRecord:
        """Insert a new user document."""
        self._col.insert_one(user.model_dump(mode="json"))
        return user

    def get(self, user_id: str) -> UserRecord | None:
        """Look up a user by id."""
        doc = self._col.find_one({"id": user_id}, _NO_ID)
        return UserRecord.model_validate(doc) if doc else None

    def get_by_external(self, external: ExternalSubject) -> UserRecord | None:
        """Look up a user by one of its external identities."""
        doc = self._col.find_one(
            {
                "external_identities": {
                    "$elemMatch": {"issuer": external.issuer, "subject": external.subject}
                }
            },
            _NO_ID,
        )
        return UserRecord.model_validate(doc) if doc else None

    def update(self, user: UserRecord) -> UserRecord:
        """Full-document replace by id."""
        result = self._col.find_one_and_update(
            {"id": user.id},
            {"$set": user.model_dump(mode="json")},
            projection=_NO_ID,
            return_document=True,
        )
        if not result:
            raise KeyError(f"User {user.id} not found")
        return UserRecord.model_validate(result)

    def list(self) -> list[UserRecord]:
        """Return all user records."""
        return [UserRecord.model_validate(doc) for doc in self._col.find({}, _NO_ID)]

    def delete(self, user_id: str) -> bool:
        """Delete a user by id."""
        return self._col.delete_one({"id": user_id}).deleted_count > 0


class MongoTeamStore(_MongoCollectionStore, TeamStoreBase):
    """MongoDB-backed team store — ``iam_teams`` plus its ``iam_team_members`` edges.

    Every method here is a private driver primitive or a direct lookup; the
    rules (uniqueness, the delete cascade, edge ordering, the ``Principal``
    projection) stay on ``TeamStoreBase`` so this driver and the json one
    cannot diverge. The membership methods are overridden nowhere — only the
    reads underneath them are, and only because a per-team/per-user filter is
    genuine query pushdown rather than a re-implementation.
    """

    def __init__(self, *, uri: str | None = None, database: str | None = None) -> None:
        """Initialize the connection and ensure lookup indexes."""
        super().__init__("iam_teams", uri=uri, database=database)
        self._col.create_index([("id", ASCENDING)], name="ix_iam_teams_id", unique=True)
        self._col.create_index([("slug", ASCENDING)], name="ix_iam_teams_slug", unique=True)
        # ``external_id`` is null on every team no directory provisioned, so a
        # plain unique index would collide on the second such team. The partial
        # filter restricts uniqueness to the documents that actually carry one.
        self._col.create_index(
            [("external_id", ASCENDING)],
            name="ix_iam_teams_external_id",
            unique=True,
            partialFilterExpression={"external_id": {"$type": "string"}},
        )
        self._members: Collection = self._db["iam_team_members"]
        self._members.create_index(
            [("team_id", ASCENDING), ("user_id", ASCENDING)],
            name="ix_iam_team_members_edge",
            unique=True,
        )
        self._members.create_index([("user_id", ASCENDING)], name="ix_iam_team_members_user")

    def _insert_team(self, team: TeamRecord) -> TeamRecord:
        """Insert a new team document."""
        self._col.insert_one(team.model_dump(mode="json"))
        return team

    def get(self, team_id: str) -> TeamRecord | None:
        """Look up a team by id."""
        doc = self._col.find_one({"id": team_id}, _NO_ID)
        return TeamRecord.model_validate(doc) if doc else None

    def get_by_slug(self, slug: str) -> TeamRecord | None:
        """Look up a team by slug."""
        doc = self._col.find_one({"slug": slug}, _NO_ID)
        return TeamRecord.model_validate(doc) if doc else None

    def get_by_external_id(self, external_id: str) -> TeamRecord | None:
        """Look up a team by the id its directory knows it as."""
        doc = self._col.find_one({"external_id": external_id}, _NO_ID)
        return TeamRecord.model_validate(doc) if doc else None

    def _replace_team(self, team: TeamRecord) -> TeamRecord:
        """Full-document replace by id."""
        result = self._col.find_one_and_update(
            {"id": team.id},
            {"$set": team.model_dump(mode="json")},
            projection=_NO_ID,
            return_document=True,
        )
        if not result:
            raise KeyError(f"Team {team.id} not found")
        return TeamRecord.model_validate(result)

    def list(self) -> list[TeamRecord]:
        """Return all teams."""
        return [TeamRecord.model_validate(doc) for doc in self._col.find({}, _NO_ID)]

    def _remove_team(self, team_id: str) -> bool:
        """Delete a team document by id."""
        return self._col.delete_one({"id": team_id}).deleted_count > 0

    def _put_membership(self, record: TeamMembershipRecord) -> None:
        """Upsert one edge on its ``(team_id, user_id)`` key."""
        self._members.update_one(
            {"team_id": record.team_id, "user_id": record.user_id},
            {"$set": record.model_dump(mode="json")},
            upsert=True,
        )

    def _drop_memberships(self, *, team_id: str, user_id: str | None = None) -> int:
        """Delete a team's edges, or one user's edge in it."""
        query: dict[str, Any] = {"team_id": team_id}
        if user_id is not None:
            query["user_id"] = user_id
        return self._members.delete_many(query).deleted_count

    def _membership_records_for_team(self, team_id: str) -> Sequence[TeamMembershipRecord]:
        """Every edge on one team, unsorted — the base orders them."""
        return self._member_matches({"team_id": team_id})

    def _membership_records_for_user(self, user_id: str) -> Sequence[TeamMembershipRecord]:
        """Every edge held by one user, unsorted — the base orders them."""
        return self._member_matches({"user_id": user_id})

    def _member_matches(self, query: Mapping[str, Any]) -> Sequence[TeamMembershipRecord]:
        """Parse every edge document matching ``query``.

        Annotated ``Sequence`` rather than ``list``: this class defines a
        ``list`` method, which shadows the builtin for every annotation below
        it in the class body.
        """
        return [
            TeamMembershipRecord.model_validate(doc) for doc in self._members.find(query, _NO_ID)
        ]


class MongoRoleStore(_MongoCollectionStore, RoleStoreBase):
    """MongoDB-backed role store — one ``iam_roles`` collection."""

    def __init__(self, *, uri: str | None = None, database: str | None = None) -> None:
        """Initialize the connection and ensure the name index."""
        super().__init__("iam_roles", uri=uri, database=database)
        self._col.create_index([("name", ASCENDING)], name="ix_iam_roles_name", unique=True)

    def _insert(self, role: RoleRecord) -> RoleRecord:
        """Insert a new role document."""
        self._col.insert_one(role.model_dump(mode="json"))
        return role

    def _replace(self, role: RoleRecord) -> RoleRecord:
        """Overwrite an existing role by name."""
        result = self._col.find_one_and_update(
            {"name": role.name},
            {"$set": role.model_dump(mode="json")},
            projection=_NO_ID,
            return_document=True,
        )
        if not result:
            raise KeyError(f"Role {role.name} not found")
        return RoleRecord.model_validate(result)

    def _remove(self, name: str) -> bool:
        """Delete a role by name."""
        return self._col.delete_one({"name": name}).deleted_count > 0

    def get(self, name: str) -> RoleRecord | None:
        """Look up a role by name."""
        doc = self._col.find_one({"name": name}, _NO_ID)
        return RoleRecord.model_validate(doc) if doc else None

    def list(self) -> list[RoleRecord]:
        """Return all roles."""
        return [RoleRecord.model_validate(doc) for doc in self._col.find({}, _NO_ID)]


class MongoAccessGrantStore(_MongoCollectionStore, AccessGrantStoreBase):
    """MongoDB-backed access-grant store — one ``iam_access_grants`` collection."""

    def __init__(self, *, uri: str | None = None, database: str | None = None) -> None:
        """Initialize the connection and ensure the resource index."""
        super().__init__("iam_access_grants", uri=uri, database=database)
        self._col.create_index(
            [("resource_kind", ASCENDING), ("resource_id", ASCENDING)],
            name="ix_iam_grants_resource",
        )

    @staticmethod
    def _match(grant: AccessGrant) -> dict[str, Any]:
        """Exact value filter identifying a single grant."""
        return {
            "resource_kind": grant.resource_kind,
            "resource_id": grant.resource_id,
            "grantee.kind": grant.grantee.kind,
            "grantee.id": grant.grantee.id,
            "level": grant.level,
        }

    def grant(self, grant: AccessGrant) -> AccessGrant:
        """Insert a grant, skipping an exact duplicate (idempotent)."""
        if self._col.count_documents(self._match(grant), limit=1) == 0:
            self._col.insert_one(grant.model_dump(mode="json"))
        return grant

    def revoke(self, grant: AccessGrant) -> bool:
        """Remove a grant matching the given value exactly."""
        return self._col.delete_one(self._match(grant)).deleted_count > 0

    def list_for_resource(self, resource_kind: str, resource_id: str) -> list[AccessGrant]:
        """Every grant on one resource."""
        cursor = self._col.find(
            {"resource_kind": resource_kind, "resource_id": resource_id}, _NO_ID
        )
        return [AccessGrant.model_validate(doc) for doc in cursor]

    def list_for_grantee(self, grantee: Grantee) -> list[AccessGrant]:
        """Every grant held by one user or team."""
        cursor = self._col.find({"grantee.kind": grantee.kind, "grantee.id": grantee.id}, _NO_ID)
        return [AccessGrant.model_validate(doc) for doc in cursor]


class MongoAuthAuditStore(_MongoCollectionStore, AuthAuditStoreBase):
    """MongoDB-backed, append-only auth-audit store — ``iam_auth_audit``."""

    def __init__(self, *, uri: str | None = None, database: str | None = None) -> None:
        """Initialize the connection and ensure the timestamp index."""
        super().__init__("iam_auth_audit", uri=uri, database=database)
        self._col.create_index([("ts", ASCENDING)], name="ix_iam_audit_ts")

    def append(self, event: AuthAuditEvent) -> AuthAuditEvent:
        """Append one event to the trail."""
        self._col.insert_one(event.model_dump(mode="json"))
        return event

    def _stored_records(
        self, *, actor_subject: str | None, type: str | None
    ) -> Sequence[Mapping[str, Any]]:
        """Every matching document, unsorted — the base orders and windows them.

        Deliberately NO ``.sort("ts", -1)``: ``ts`` is stored as the ISO string
        ``model_dump(mode="json")`` produced, so a server-side sort on it is
        lexicographic and disagrees with the json driver the moment two records
        spell the same offset differently (``...Z`` vs ``+00:00``). The ``ts``
        index still serves the equality filters and any future range query.
        """
        query: dict[str, Any] = {}
        if actor_subject is not None:
            query["actor_subject"] = actor_subject
        if type is not None:
            query["type"] = type
        return list(self._col.find(query, _NO_ID))


__all__ = [
    "MongoUserStore",
    "MongoTeamStore",
    "MongoRoleStore",
    "MongoAccessGrantStore",
    "MongoAuthAuditStore",
]
