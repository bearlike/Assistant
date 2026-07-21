#!/usr/bin/env python3
"""The authentication/authorization audit trail — a discriminated event union.

Every security-relevant identity event is a typed ``AuthAuditEvent`` sharing one
envelope (``ts``, ``actor_subject``, ``source``) and carrying its own minimal
payload. The union is the append-only record the audit store persists and
``audit.read`` exposes. Like the trigger and authenticator families, each type
owns its own fields — no ``if type ==`` fan-out — and there is one parse seam.

Timestamps arrive as arguments; nothing here reads a clock.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

AuditSource = Literal["api", "console", "mcp", "scim"]


class AuthAuditEvent(BaseModel):
    """Shared envelope for every audit event kind.

    Deliberately does NOT declare the ``type`` discriminator — each concrete
    kind declares its own ``type: Literal[...]`` (see
    :data:`AuthAuditEventUnion`). A base ``type: str`` narrowed to a ``Literal``
    by each variant is an incompatible mutable-field override; keeping the
    discriminator only on the members avoids it, and consumers type against the
    union where ``.type`` is available. ``actor_subject`` is optional because
    some events (a failed login) have no established actor yet.
    """

    model_config = ConfigDict(extra="forbid")

    ts: datetime
    actor_subject: str | None = None
    source: AuditSource

    # Lazily built on first ``parse`` and cached on the base — every kind
    # resolves through one adapter (mirrors ``triggers.TriggerSpec``).
    _adapter: ClassVar[TypeAdapter | None] = None

    @classmethod
    def parse(cls, data: Mapping[str, object]) -> AuthAuditEventUnion:
        """Parse a raw dict (JSON-decoded or stored doc) into its concrete kind."""
        if AuthAuditEvent._adapter is None:
            AuthAuditEvent._adapter = TypeAdapter(AuthAuditEventUnion)
        return AuthAuditEvent._adapter.validate_python(data)


class LoginSuccessEvent(AuthAuditEvent):
    """A principal authenticated successfully."""

    type: Literal["login_success"] = "login_success"
    subject: str
    method: str
    issuer: str | None = None


class LoginFailureEvent(AuthAuditEvent):
    """An authentication attempt was rejected."""

    type: Literal["login_failure"] = "login_failure"
    method: str
    reason: str
    attempted_identifier: str | None = None


class KeyMintedEvent(AuthAuditEvent):
    """An API/service key was created."""

    type: Literal["key_minted"] = "key_minted"
    key_id: str
    subject: str
    label: str | None = None


class KeyRevokedEvent(AuthAuditEvent):
    """An API/service key was revoked."""

    type: Literal["key_revoked"] = "key_revoked"
    key_id: str
    subject: str


class RoleChangedEvent(AuthAuditEvent):
    """A subject's role assignments changed."""

    type: Literal["role_changed"] = "role_changed"
    subject: str
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


class TeamChangedEvent(AuthAuditEvent):
    """A subject's membership in a team changed."""

    type: Literal["team_changed"] = "team_changed"
    subject: str
    team_id: str
    change: Literal["added", "removed", "role_changed"]


class UserStatusChangedEvent(AuthAuditEvent):
    """A user account was disabled or re-enabled.

    Disabling an account is the one administrative action that revokes a
    person's access outright, so it belongs in the trail beside the role and
    team changes that merely reshape it — whether an operator did it from the
    console or an identity provider did it through provisioning.
    """

    type: Literal["user_status_changed"] = "user_status_changed"
    subject: str
    status: Literal["active", "disabled"]


class AccessDeniedEvent(AuthAuditEvent):
    """An authorization check refused an action on a resource."""

    type: Literal["access_denied"] = "access_denied"
    subject: str | None = None
    resource_kind: str
    resource_id: str | None = None
    need: str
    permission: str | None = None


class ScimProvisionedEvent(AuthAuditEvent):
    """A user was provisioned or updated through SCIM."""

    type: Literal["scim_provisioned"] = "scim_provisioned"
    user_id: str
    issuer: str
    external_subject: str


class ScimDeprovisionedEvent(AuthAuditEvent):
    """A user was deprovisioned (disabled/removed) through SCIM."""

    type: Literal["scim_deprovisioned"] = "scim_deprovisioned"
    user_id: str


AuthAuditEventUnion = Annotated[
    LoginSuccessEvent
    | LoginFailureEvent
    | KeyMintedEvent
    | KeyRevokedEvent
    | RoleChangedEvent
    | TeamChangedEvent
    | UserStatusChangedEvent
    | AccessDeniedEvent
    | ScimProvisionedEvent
    | ScimDeprovisionedEvent,
    Field(discriminator="type"),
]

# The ONE parse seam — thin module-level delegation to the class-owned lazy
# adapter, kept for the frozen public API.
parse_audit_event = AuthAuditEvent.parse


__all__ = [
    "AuditSource",
    "AuthAuditEvent",
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
    "AuthAuditEventUnion",
    "parse_audit_event",
]
