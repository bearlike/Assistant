"""Mewbo IAM — the identity kernel.

Pure, data-owned models for identity and authorization plus the stores that
persist them: principals and their authenticators, the closed permission
catalog and roles, teams, resource ownership + grants, group→role/team
mappings, durable user records, the SCIM 2.0 resource subset, the parsed
``api.auth`` settings block, and an append-only auth audit trail. Every model
owns its own behavior and never imports I/O (clocks, headers and IdP claims
arrive as method arguments).

The provider integrations those models describe — OIDC discovery/JWKS, the LDAP
bind, the SAML SP — live in :mod:`mewbo_iam.drivers` behind the
``oidc``/``ldap``/``saml`` extras, each name guarded lazily so installing one
extra never requires another. Nothing here imports up into a driver, so a bare
``import mewbo_iam`` stays dependency-light. This library imports down into
``mewbo-core`` and pydantic only — apps consume it, it consumes no app.
"""

from mewbo_iam.access import (
    AccessDecider,
    AccessGrant,
    AccessLevel,
    Grantee,
    OwnershipStamp,
    ResourceKind,
)
from mewbo_iam.audit import (
    AccessDeniedEvent,
    AuditSource,
    AuthAuditEvent,
    AuthAuditEventUnion,
    KeyMintedEvent,
    KeyRevokedEvent,
    LoginFailureEvent,
    LoginSuccessEvent,
    RoleChangedEvent,
    ScimDeprovisionedEvent,
    ScimProvisionedEvent,
    TeamChangedEvent,
    UserStatusChangedEvent,
    parse_audit_event,
)
from mewbo_iam.authenticators import (
    ApiKeyAuthenticator,
    AuthenticatorSpec,
    AuthenticatorUnion,
    LdapAuthenticator,
    OidcAuthenticator,
    RawIdentity,
    SamlAuthenticator,
    TrustedHeaderAuthenticator,
    parse_authenticator,
)
from mewbo_iam.mappings import (
    BootstrapRule,
    GroupRoleMapping,
    GroupTeamMapping,
    MappingRule,
)
from mewbo_iam.permissions import PermissionCatalog
from mewbo_iam.principal import (
    AuthMethod,
    AuthMethodKind,
    AvatarPolicy,
    ExternalSubject,
    Principal,
    ProfileAvatarMixin,
)
from mewbo_iam.roles import (
    ADMIN_ROLE,
    BUILTIN_ROLE_NAMES,
    BUILTIN_ROLES,
    RoleRecord,
)
from mewbo_iam.settings import (
    AuditSettings,
    AuthSettings,
    ScimSettings,
    SessionSettings,
)
from mewbo_iam.stores import (
    AccessGrantStoreBase,
    AuthAuditStoreBase,
    JsonAccessGrantStore,
    JsonAuthAuditStore,
    JsonRoleStore,
    JsonTeamStore,
    JsonUserStore,
    RoleStoreBase,
    TeamStoreBase,
    UserStoreBase,
    create_access_grant_store,
    create_auth_audit_store,
    create_role_store,
    create_team_store,
    create_user_store,
)
from mewbo_iam.teams import TeamMembership, TeamMembershipRecord, TeamRecord, TeamRole
from mewbo_iam.users import UserRecord

__all__ = [
    # principal + identity primitives
    "Principal",
    "AuthMethod",
    "AuthMethodKind",
    "ExternalSubject",
    "AvatarPolicy",
    "ProfileAvatarMixin",
    # authenticators
    "AuthenticatorSpec",
    "AuthenticatorUnion",
    "ApiKeyAuthenticator",
    "OidcAuthenticator",
    "TrustedHeaderAuthenticator",
    "LdapAuthenticator",
    "SamlAuthenticator",
    "RawIdentity",
    "parse_authenticator",
    # permissions + roles
    "PermissionCatalog",
    "RoleRecord",
    "ADMIN_ROLE",
    "BUILTIN_ROLES",
    "BUILTIN_ROLE_NAMES",
    # teams
    "TeamRecord",
    "TeamMembership",
    "TeamMembershipRecord",
    "TeamRole",
    # ownership + access
    "OwnershipStamp",
    "AccessGrant",
    "Grantee",
    "AccessDecider",
    "ResourceKind",
    "AccessLevel",
    # mappings
    "MappingRule",
    "GroupRoleMapping",
    "GroupTeamMapping",
    "BootstrapRule",
    # settings (the parsed api.auth block)
    "AuthSettings",
    "SessionSettings",
    "ScimSettings",
    "AuditSettings",
    # users
    "UserRecord",
    # audit
    "AuthAuditEvent",
    "AuthAuditEventUnion",
    "AuditSource",
    "LoginSuccessEvent",
    "LoginFailureEvent",
    "KeyMintedEvent",
    "KeyRevokedEvent",
    "RoleChangedEvent",
    "TeamChangedEvent",
    "UserStatusChangedEvent",
    "AccessDeniedEvent",
    "ScimProvisionedEvent",
    "ScimDeprovisionedEvent",
    "parse_audit_event",
    # stores
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
