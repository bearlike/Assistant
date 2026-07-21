#!/usr/bin/env python3
"""The closed permission catalog — every authorizable verb, as a narrow id.

A permission id is a ``<domain>.<verb>`` string derived from the real REST
route families. The catalog is *closed*: a role may only grant an id that
appears in :attr:`PermissionCatalog.ALL`, enforced by ``RoleRecord``'s
validator. Keeping the ids as ``Final[Literal[...]]`` constants (not free
strings) means a typo is a type error, and the set stays small and auditable —
new verbs are added here deliberately, never invented at a call site.

Every id maps to a live consumer — a guarded route family, or the console
surface that reads it. The catalog carries no reserved or unused verbs.
"""

from __future__ import annotations

from typing import ClassVar, Final, Literal, final


@final
class PermissionCatalog:
    """Closed catalog of permission ids, grouped by product domain.

    Read verbs are least privilege; ``*.admin`` and the identity-governance
    verbs (``users.admin``/``teams.admin``/``roles.admin``/``config.write``/
    ``audit.read``) are the most. ``sessions.read`` (a single session) vs
    ``sessions.read_all`` (the cross-tenant list) is the seam that separates a
    member from an operator.
    """

    # -- session lifecycle -------------------------------------------------
    SESSIONS_CREATE: Final[Literal["sessions.create"]] = "sessions.create"
    SESSIONS_READ: Final[Literal["sessions.read"]] = "sessions.read"
    SESSIONS_READ_ALL: Final[Literal["sessions.read_all"]] = "sessions.read_all"
    SESSIONS_INTERACT: Final[Literal["sessions.interact"]] = "sessions.interact"
    SESSIONS_TERMINATE: Final[Literal["sessions.terminate"]] = "sessions.terminate"

    # -- wiki --------------------------------------------------------------
    WIKI_READ: Final[Literal["wiki.read"]] = "wiki.read"
    WIKI_WRITE: Final[Literal["wiki.write"]] = "wiki.write"
    WIKI_ADMIN: Final[Literal["wiki.admin"]] = "wiki.admin"

    # -- agentic search ----------------------------------------------------
    SEARCH_RUN: Final[Literal["search.run"]] = "search.run"

    # -- apps --------------------------------------------------------------
    APPS_READ: Final[Literal["apps.read"]] = "apps.read"
    APPS_USE: Final[Literal["apps.use"]] = "apps.use"
    APPS_SUBMIT: Final[Literal["apps.submit"]] = "apps.submit"
    APPS_ADMIN: Final[Literal["apps.admin"]] = "apps.admin"

    # -- reverse-invocation triggers --------------------------------------
    TRIGGERS_READ: Final[Literal["triggers.read"]] = "triggers.read"
    TRIGGERS_ARM: Final[Literal["triggers.arm"]] = "triggers.arm"
    TRIGGERS_MANAGE: Final[Literal["triggers.manage"]] = "triggers.manage"

    # -- managed projects + worktrees --------------------------------------
    PROJECTS_READ: Final[Literal["projects.read"]] = "projects.read"
    PROJECTS_WRITE: Final[Literal["projects.write"]] = "projects.write"
    PROJECTS_ADMIN: Final[Literal["projects.admin"]] = "projects.admin"

    # -- API keys ----------------------------------------------------------
    KEYS_MINT_OWN: Final[Literal["keys.mint_own"]] = "keys.mint_own"
    KEYS_ADMIN: Final[Literal["keys.admin"]] = "keys.admin"

    # -- git credential registry -------------------------------------------
    GIT_CREDENTIALS_READ: Final[Literal["git_credentials.read"]] = "git_credentials.read"
    GIT_CREDENTIALS_WRITE: Final[Literal["git_credentials.write"]] = "git_credentials.write"

    # -- web IDE -----------------------------------------------------------
    IDE_ACCESS: Final[Literal["ide.access"]] = "ide.access"

    # -- plugins -----------------------------------------------------------
    PLUGINS_READ: Final[Literal["plugins.read"]] = "plugins.read"
    PLUGINS_ADMIN: Final[Literal["plugins.admin"]] = "plugins.admin"

    # -- automated forge pickup -------------------------------------------
    AUTOMATION_PICKUP: Final[Literal["automation.pickup"]] = "automation.pickup"

    # -- system instructions (per-session prompt) --------------------------
    SYSTEM_INSTRUCTIONS_READ: Final[Literal["system_instructions.read"]] = (
        "system_instructions.read"
    )
    SYSTEM_INSTRUCTIONS_WRITE: Final[Literal["system_instructions.write"]] = (
        "system_instructions.write"
    )

    # -- identity governance ------------------------------------------------
    USERS_ADMIN: Final[Literal["users.admin"]] = "users.admin"
    TEAMS_ADMIN: Final[Literal["teams.admin"]] = "teams.admin"
    ROLES_ADMIN: Final[Literal["roles.admin"]] = "roles.admin"

    # -- configuration -----------------------------------------------------
    CONFIG_READ: Final[Literal["config.read"]] = "config.read"
    CONFIG_WRITE: Final[Literal["config.write"]] = "config.write"

    # -- audit trail --------------------------------------------------------
    AUDIT_READ: Final[Literal["audit.read"]] = "audit.read"

    # The closed set of every id above, assembled from the declared constants
    # so it can never drift from a hand-maintained second list (see below).
    ALL: ClassVar[frozenset[str]]

    @classmethod
    def is_valid(cls, perm: str) -> bool:
        """Whether ``perm`` is a known catalog id."""
        return perm in cls.ALL


# Derive ALL from the uppercase string constants declared above. This is data
# assembly (not logic): a new permission is a new constant and lands in ALL for
# free, keeping the catalog closed with a single source of truth.
PermissionCatalog.ALL = frozenset(
    value
    for name, value in vars(PermissionCatalog).items()
    if name.isupper() and name != "ALL" and isinstance(value, str)
)


__all__ = ["PermissionCatalog"]
