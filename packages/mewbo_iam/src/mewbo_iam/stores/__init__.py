"""Identity stores — abstract bases, JSON drivers, and lazy Mongo factories.

Each store follows ``key_store``/``triggers.store``: an atomic ABC, a JSON
file-backed driver, and a ``create_*_store`` factory that returns the configured
driver (json or mongodb). The Mongo drivers live in :mod:`mewbo_iam.stores.mongo`
and are imported only when the ``storage.driver`` config selects mongodb, so a
default install never requires a reachable MongoDB.
"""

from mewbo_iam.stores.audit import (
    AuthAuditStoreBase,
    JsonAuthAuditStore,
    create_auth_audit_store,
)
from mewbo_iam.stores.grants import (
    AccessGrantStoreBase,
    JsonAccessGrantStore,
    create_access_grant_store,
)
from mewbo_iam.stores.roles import JsonRoleStore, RoleStoreBase, create_role_store
from mewbo_iam.stores.teams import JsonTeamStore, TeamStoreBase, create_team_store
from mewbo_iam.stores.users import JsonUserStore, UserStoreBase, create_user_store

__all__ = [
    "UserStoreBase",
    "JsonUserStore",
    "create_user_store",
    "TeamStoreBase",
    "JsonTeamStore",
    "create_team_store",
    "RoleStoreBase",
    "JsonRoleStore",
    "create_role_store",
    "AccessGrantStoreBase",
    "JsonAccessGrantStore",
    "create_access_grant_store",
    "AuthAuditStoreBase",
    "JsonAuthAuditStore",
    "create_auth_audit_store",
]
