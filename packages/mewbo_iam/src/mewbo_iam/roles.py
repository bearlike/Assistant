#!/usr/bin/env python3
"""Roles — named permission bundles, plus the five built-in roles.

A ``RoleRecord`` is a set of permission ids validated against the closed
:class:`~mewbo_iam.permissions.PermissionCatalog`. Behavior intrinsic to the
record — "does this role grant X" — lives on the model as :meth:`RoleRecord.grants`,
never a service-side lookup. The built-in roles are data constants seeded by the
role store; they are immutable (the store refuses to update or delete them).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mewbo_iam.permissions import PermissionCatalog

# The role that bypasses every access check (see ``access.AccessDecider``) and
# is granted the whole catalog by the built-in ``admin`` role below.
ADMIN_ROLE = "admin"


class RoleRecord(BaseModel):
    """A named bundle of catalog permission ids.

    ``permissions`` is validated ⊆ :attr:`PermissionCatalog.ALL` at definition —
    an unknown id is a clean ``ValueError``, not a silently ineffective grant.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    permissions: frozenset[str] = Field(default_factory=frozenset)
    builtin: bool = False

    @field_validator("permissions")
    @classmethod
    def _validate_permissions(cls, value: frozenset[str]) -> frozenset[str]:
        unknown = value - PermissionCatalog.ALL
        if unknown:
            raise ValueError(f"unknown permissions: {sorted(unknown)}")
        return value

    def grants(self, perm: str) -> bool:
        """Whether this role confers permission ``perm``."""
        return perm in self.permissions


# --- built-in roles ---------------------------------------------------------
#
# The identity-governance verbs an operator deliberately lacks: managing users,
# teams, roles and configuration, and reading the security audit trail. An
# operator runs every workload but does not govern identity or read the audit
# log — that separation is what makes "operator" safe to hand out broadly.
_IDENTITY_GOVERNANCE: frozenset[str] = frozenset(
    {
        PermissionCatalog.USERS_ADMIN,
        PermissionCatalog.TEAMS_ADMIN,
        PermissionCatalog.ROLES_ADMIN,
        PermissionCatalog.CONFIG_WRITE,
        PermissionCatalog.AUDIT_READ,
    }
)

# What a plain member may do: create/use/write verbs on their own resources,
# plus self-service secrets (own API keys + git credentials, scoped later by
# ownership). No ``*.admin``, no ``sessions.read_all`` (the operator's
# cross-tenant view), no system-prompt writes, no audit access.
_MEMBER_PERMISSIONS: frozenset[str] = frozenset(
    {
        PermissionCatalog.SESSIONS_CREATE,
        PermissionCatalog.SESSIONS_READ,
        PermissionCatalog.SESSIONS_INTERACT,
        PermissionCatalog.SESSIONS_TERMINATE,
        PermissionCatalog.WIKI_READ,
        PermissionCatalog.WIKI_WRITE,
        PermissionCatalog.SEARCH_RUN,
        PermissionCatalog.APPS_READ,
        PermissionCatalog.APPS_USE,
        PermissionCatalog.APPS_SUBMIT,
        PermissionCatalog.TRIGGERS_READ,
        PermissionCatalog.TRIGGERS_ARM,
        PermissionCatalog.TRIGGERS_MANAGE,
        PermissionCatalog.PROJECTS_READ,
        PermissionCatalog.KEYS_MINT_OWN,
        PermissionCatalog.GIT_CREDENTIALS_READ,
        PermissionCatalog.GIT_CREDENTIALS_WRITE,
        PermissionCatalog.IDE_ACCESS,
        PermissionCatalog.PLUGINS_READ,
        PermissionCatalog.CONFIG_READ,
    }
)

# Read-only across the surfaces a viewer can see; no action, no secrets-metadata
# reads (git credentials / system instructions), no governance verbs.
_VIEWER_PERMISSIONS: frozenset[str] = frozenset(
    {
        PermissionCatalog.SESSIONS_READ,
        PermissionCatalog.WIKI_READ,
        PermissionCatalog.APPS_READ,
        PermissionCatalog.TRIGGERS_READ,
        PermissionCatalog.PROJECTS_READ,
        PermissionCatalog.PLUGINS_READ,
        PermissionCatalog.CONFIG_READ,
    }
)

BUILTIN_ROLES: tuple[RoleRecord, ...] = (
    RoleRecord(
        name=ADMIN_ROLE,
        description="Full control over every surface and all identity governance.",
        permissions=PermissionCatalog.ALL,
        builtin=True,
    ),
    RoleRecord(
        name="operator",
        description="Runs every workload; cannot govern identity or read the audit log.",
        permissions=PermissionCatalog.ALL - _IDENTITY_GOVERNANCE,
        builtin=True,
    ),
    RoleRecord(
        name="member",
        description="Create/use/write on own resources; no admin, no cross-tenant read.",
        permissions=_MEMBER_PERMISSIONS,
        builtin=True,
    ),
    RoleRecord(
        name="viewer",
        description="Read-only across visible surfaces.",
        permissions=_VIEWER_PERMISSIONS,
        builtin=True,
    ),
    RoleRecord(
        name="service",
        description="Service-account template — permissions come from the key's signed scopes.",
        permissions=frozenset(),
        builtin=True,
    ),
)

BUILTIN_ROLE_NAMES: frozenset[str] = frozenset(role.name for role in BUILTIN_ROLES)


__all__ = [
    "ADMIN_ROLE",
    "RoleRecord",
    "BUILTIN_ROLES",
    "BUILTIN_ROLE_NAMES",
]
