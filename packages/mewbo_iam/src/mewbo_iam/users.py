#!/usr/bin/env python3
"""The persisted user — the durable side of a ``Principal``.

A ``UserRecord`` is what the user store keeps between requests: the stable
identity, the external identities it has been linked to (the JIT/SCIM join
keys), profile fields, status, and assigned roles. A ``Principal`` is assembled
from a ``UserRecord`` (plus the request's auth method and any key scopes) once
per request; the record itself carries no request state.

Timestamps arrive as arguments — the store passes ``created_at``/``updated_at``;
the model never calls a clock.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import ConfigDict

from mewbo_iam.principal import ExternalSubject, ProfileAvatarMixin


class UserRecord(ProfileAvatarMixin):
    """A durable user identity, keyed by ``id`` and joined by external subject.

    ``external_identities`` holds every ``(issuer, subject)`` this user has
    authenticated as — a user may link more than one IdP. Lookups for JIT
    provisioning match on any of them.

    ``external_groups`` holds the group names the IdP asserted at the most
    recent login, stored so the group→team mapping can be RE-RESOLVED on
    requests that carry no assertion. A browser-cookie request has no IdP
    claims, so without this a config-mapped team exists only for the login
    response and vanishes on every request after it.

    Storing the mapping's INPUT rather than its output is the point. The teams
    are recomputed per request, so editing ``team_mappings`` takes effect
    immediately, and no membership edge has to be invented, marked with a
    provenance field, and later reconciled when the IdP drops a group. It also
    keeps the durable membership table meaning one thing only: someone
    deliberately granted this.

    The groups themselves are a snapshot and go stale until the next login —
    the same trade-off ``roles`` already makes, since a role resolved from an
    IdP group is likewise frozen into the record at login. Anything needing
    revocation faster than a session lifetime belongs in durable memberships,
    which are read live.

    The list is replaced wholesale at each login, so it reflects the IdP used
    most recently rather than the union across linked providers.
    """

    model_config = ConfigDict(extra="forbid")

    id: str  # ``user:<uuid>``
    external_identities: tuple[ExternalSubject, ...] = ()
    external_groups: tuple[str, ...] = ()
    email_verified: bool | None = None
    display_name: str | None = None
    status: Literal["active", "disabled"] = "active"
    roles: tuple[str, ...] = ()
    created_at: datetime
    updated_at: datetime


__all__ = ["UserRecord"]
