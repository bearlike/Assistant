#!/usr/bin/env python3
"""``AuthSettings`` — the validated, boot-time shape of the ``api.auth`` block.

Core's ``config.py`` may only carry the auth block as opaque ``dict``/``list``
fields (it must never import this kernel — the DAG flows core → iam → apps), so
the deep, typed validation of that block lives HERE, at the trust boundary the
app crosses at boot. :meth:`AuthSettings.from_config_block` turns the raw config
dict into typed settings — the full authenticator discriminated union, the
group→role/team mappings, the bootstrap rule, and the session/avatar/scim/audit
sub-settings — with ``extra="forbid"`` so a smuggled or misspelled key is a clean
error, not a silent no-op. The AuthKit validates the raw block through this model
at app construction and refuses to boot when it does not parse.

Models never import I/O: this file parses config data only. Runtime probing
(whether an authenticator kind's optional driver dependency is installed) is the
AuthKit's job, not the model's.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mewbo_iam.authenticators import AuthenticatorUnion
from mewbo_iam.mappings import BootstrapRule, GroupRoleMapping, GroupTeamMapping
from mewbo_iam.principal import AvatarPolicy


class SessionSettings(BaseModel):
    """Browser session/cookie settings.

    ``secret`` is required once any non-``api_key`` authenticator is configured —
    a federated login mints a signed session cookie, and an unsigned one is
    forgeable. The ``AuthSettings`` model enforces that cross-field rule.
    """

    model_config = ConfigDict(extra="forbid")

    cookie_name: str = "mewbo_session"
    ttl_seconds: int = Field(default=28800, ge=1)  # 8 hours
    secret: str | None = None


class ScimSettings(BaseModel):
    """SCIM 2.0 provisioning settings — gates the SCIM endpoint and signs its bearer token."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    secret: str | None = None


class AuditSettings(BaseModel):
    """Auth-audit trail settings. On by default once auth itself is enabled."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class AuthSettings(BaseModel):
    """The parsed, validated ``api.auth`` block.

    ``enabled`` is the master switch: while it is ``False`` (the default), the
    AuthKit resolves every request to the legacy full-power principal and the
    server behaves byte-for-byte as it did before IAM existed. ``authenticators``
    is the full discriminated union — each entry parses to its concrete kind with
    that kind's own validators — so the config seam accepts every authenticator
    the kernel knows, independently of whether that kind's optional driver extra
    is installed (the AuthKit probes for that at runtime, not the model).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    authenticators: tuple[AuthenticatorUnion, ...] = ()
    role_mappings: GroupRoleMapping = Field(default_factory=GroupRoleMapping)
    team_mappings: GroupTeamMapping = Field(default_factory=GroupTeamMapping)
    bootstrap: BootstrapRule | None = None
    session: SessionSettings = Field(default_factory=SessionSettings)
    avatars: AvatarPolicy = Field(default_factory=AvatarPolicy)
    scim: ScimSettings = Field(default_factory=ScimSettings)
    audit: AuditSettings = Field(default_factory=AuditSettings)

    @model_validator(mode="after")
    def _require_session_secret_for_federated(self) -> AuthSettings:
        """A federated (non-api_key) authenticator requires a session secret.

        The one boot-validation rule this phase enforces: a login that mints a
        signed session cookie is meaningless without a secret to sign it, so
        configuring any OIDC/LDAP/SAML/trusted-header authenticator without
        ``session.secret`` is refused at parse time rather than failing later.
        """
        federated = sorted({a.kind for a in self.authenticators if a.kind != "api_key"})
        if federated and not self.session.secret:
            raise ValueError(
                "api.auth.session.secret is required when a non-api_key authenticator "
                f"is configured (found: {', '.join(federated)})"
            )
        return self

    @classmethod
    def from_config_block(cls, raw: Mapping[str, Any] | None) -> AuthSettings:
        """Validate a raw ``api.auth`` dict into typed settings.

        ``None``/empty yields the all-off default (auth disabled). Keys whose
        value is ``None`` are dropped so a config that leaves a sub-block unset
        gets this model's own defaults rather than a null that would fail the
        typed field. A malformed block raises ``pydantic.ValidationError`` — the
        AuthKit turns that into a hard boot failure.
        """
        if not raw:
            return cls()
        data = {key: value for key, value in dict(raw).items() if value is not None}
        return cls.model_validate(data)


__all__ = [
    "SessionSettings",
    "ScimSettings",
    "AuditSettings",
    "AuthSettings",
]
