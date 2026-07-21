#!/usr/bin/env python3
"""The authenticated caller — ``Principal`` — plus the identity primitives.

A ``Principal`` is the resolved, request-scoped identity every guard reasons
about: who they are (``subject``), how they proved it (``auth_method``), what
roles they carry, which teams they belong to, and — for service accounts — the
optional signed ``scopes`` that narrow them. It is frozen: a principal is built
once per request and never mutated.

Models here never touch I/O. ``effective_permissions`` takes already-resolved
role records as an argument (the caller reads them from a role store); the
avatar strategy takes a policy object. The clock, HTTP headers and IdP claims
never appear inside these methods.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from mewbo_iam.roles import ADMIN_ROLE, RoleRecord
from mewbo_iam.teams import TeamMembership

# The two namespaces a subject can live in: ``user:<uuid>`` for a human,
# ``svc:<key-id>`` for a service account. Readers infer ``Principal.kind`` from
# this prefix, so the set is closed — a third namespace is a deliberate change
# here plus at every reader, never a new string invented at a call site.
SUBJECT_PREFIXES: tuple[str, ...] = ("user:", "svc:")

# Which authenticator proved this identity. ``password``/``system`` cover the
# local seeded-admin and internal-caller cases that no external authenticator
# handles.
AuthMethodKind = Literal[
    "api_key",
    "oidc",
    "trusted_header",
    "ldap",
    "saml",
    "password",
    "system",
]


class ExternalSubject(BaseModel):
    """A stable external identity: ``(issuer, subject)``.

    This pair is the JIT/SCIM join key — the one value that survives an email
    change or a display-name rename, so a returning user links to the same
    ``UserRecord`` instead of forking a new one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer: str
    subject: str


class AuthMethod(BaseModel):
    """How a principal authenticated, and (for federated methods) from where."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: AuthMethodKind
    issuer: str | None = None


class AvatarPolicy(BaseModel):
    """Deployment policy for avatar resolution.

    ``gravatar_enabled`` gates the Gravatar fallback (a privacy choice — it
    leaks a hashed email to a third party); ``fallback`` selects Gravatar's
    own default image when no profile picture exists there.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    gravatar_enabled: bool = True
    fallback: Literal["404", "identicon", "mp"] = "identicon"


class ProfileAvatarMixin(BaseModel):
    """Email-derived avatar behavior shared by ``Principal`` and ``UserRecord``.

    Both models carry an ``email`` and an optional IdP ``picture_url``; this
    mixin adds the two members that read them. Kept deliberately trivial — a
    hash property and a three-branch strategy — so sharing beats duplicating.
    """

    email: str | None = None
    picture_url: str | None = None

    @property
    def gravatar_hash(self) -> str | None:
        """SHA-256 hex of the normalized email, or ``None`` when no email.

        Normalization (strip + lowercase) matches Gravatar's own hashing so the
        digest resolves to the account the user registered.
        """
        if self.email is None:
            return None
        normalized = self.email.strip().lower()
        if not normalized:
            return None
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def gravatar_url(self, policy: AvatarPolicy) -> str | None:
        """The Gravatar URL for this email under *policy*, or ``None``.

        THE one place the Gravatar URL is constructed. ``None`` when the policy
        disables Gravatar (a privacy choice) or there is no email to hash.

        It is a member of its own rather than a step inside :meth:`avatar_url`
        because the two consumers need different shapes of the same fact: the
        precedence resolver wants one collapsed URL, while the ``/me`` route
        reports ``picture_url`` and ``gravatar_url`` as SEPARATE fields so a
        client can fall back on its own. Both now read this; before, the route
        re-spelled the URL by hand and the two copies were free to drift on the
        host or the fallback.
        """
        if not policy.gravatar_enabled:
            return None
        digest = self.gravatar_hash
        if digest is None:
            return None
        return f"https://www.gravatar.com/avatar/{digest}?d={policy.fallback}"

    def avatar_url(self, policy: AvatarPolicy) -> str | None:
        """Resolve an avatar URL by precedence: IdP picture → Gravatar → None.

        A ``None`` result is intentional: the client renders initials rather
        than a broken image.
        """
        if self.picture_url:
            return self.picture_url
        return self.gravatar_url(policy)


class Principal(ProfileAvatarMixin):
    """The resolved, request-scoped identity a guard authorizes.

    ``scopes`` follows a deliberate three-state law:

    * ``None`` — unrestricted-legacy: an identity minted before scoping existed,
      or a full-power user key. Scopes impose no narrowing.
    * ``()`` — explicitly none: a service account whose signed key granted zero
      scopes. It can do nothing scope-gated.
    * a non-empty tuple — the exact set of scope-narrowed capabilities.

    Do not collapse ``None`` and ``()`` — that collapse is the classic
    fail-open bug (a scopeless service key silently becoming unrestricted).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str  # ``user:<uuid>`` for humans, ``svc:<key-id>`` for services
    kind: Literal["user", "service"]

    display_name: str | None = None
    email_verified: bool | None = None
    roles: tuple[str, ...] = ()
    team_memberships: tuple[TeamMembership, ...] = ()
    scopes: tuple[str, ...] | None = None
    auth_method: AuthMethod
    external_subject: ExternalSubject | None = None

    @field_validator("subject")
    @classmethod
    def validate_subject(cls, value: str) -> str:
        """Require ``user:``/``svc:`` and something after it.

        The prefix is not decoration — callers INFER ``kind`` from it rather
        than reading the field, so an unrecognized prefix does not fail
        anywhere: it silently resolves to a *service* principal. That makes a
        malformed subject a quiet authorization result instead of an error,
        which is why this is checked at definition rather than trusted at each
        reader. A blank subject is the same bug at its most visible.

        The remainder must be non-empty too: ``"user:"`` alone identifies no
        one, yet compares equal to another ``"user:"`` — enough for an owner
        check to match the wrong caller.

        PUBLIC on purpose, and callable without building a ``Principal``: a
        subject is also WRITTEN — an admin naming a key's ``owner_subject`` —
        and a write that skips this law persists a record that only fails much
        later, when something tries to resolve it into a principal. A mint
        boundary calls this so the refusal lands on the request that made the
        mistake. Reusing the validator rather than re-checking the prefix at
        the boundary is what keeps the two from drifting.

        ``mewbo_core.triggers.spec.TriggerAuthority`` mirrors this rule on the
        persisted side; the two are deliberately separate because core must
        not import this kernel.
        """
        prefix = next((p for p in SUBJECT_PREFIXES if value.startswith(p)), None)
        if prefix is None or not value[len(prefix) :].strip():
            expected = " or ".join(f"{p}<id>" for p in SUBJECT_PREFIXES)
            raise ValueError(f"principal subject must be {expected}, got {value!r}")
        return value

    @property
    def is_admin(self) -> bool:
        """Whether this principal carries the built-in admin role."""
        return ADMIN_ROLE in self.roles

    def with_team_memberships(
        self,
        durable: Sequence[TeamMembership],
        *,
        mapped: Sequence[TeamMembership] = (),
    ) -> Principal:
        """Return a copy carrying this principal's teams — the projection seam.

        Memberships reach a principal from two sources with different
        authority, so both arrive here and the precedence is applied in ONE
        place:

        * *durable* — what the team store holds
          (``TeamStoreBase.list_for_user``). Written by SCIM and the admin
          surface, and the only source that can express ``team_admin``.
        * *mapped* — derived from the IdP's groups through
          ``GroupTeamMapping`` and recomputed on every login. It carries no
          role, so every membership it produces is a plain ``team_member``.

        **Durable wins when both name the same team**, and the reason is a
        privilege bug rather than a preference: a stored ``team_admin`` who
        also matches a group rule would otherwise be demoted to ``team_member``
        on every single login, silently losing admin over their team's
        resources. The reverse cannot happen — mapping never confers a role
        durable storage lacks — so there is no case where letting *mapped* win
        gains anything.

        Both sequences must carry durable team ids; see ``TeamMembership``.

        A copy, not a mutation — ``Principal`` is frozen because it is built
        once per request and read by every guard afterwards.
        """
        merged: list[TeamMembership] = []
        seen: set[str] = set()
        for membership in (*durable, *mapped):
            if membership.team_id in seen:
                continue
            seen.add(membership.team_id)
            merged.append(membership)
        return self.model_copy(update={"team_memberships": tuple(merged)})

    def effective_permissions(self, role_records: Mapping[str, RoleRecord]) -> frozenset[str]:
        """Union of catalog permissions across this principal's roles.

        ``role_records`` maps role name → resolved record; the caller reads it
        from a role store (the model never touches I/O). A role name with no
        matching record contributes nothing — an unknown role is inert, not an
        error, so a deleted role degrades a principal rather than breaking the
        request.
        """
        perms: set[str] = set()
        for name in self.roles:
            record = role_records.get(name)
            if record is not None:
                perms |= record.permissions
        return frozenset(perms)


__all__ = [
    "AuthMethodKind",
    "ExternalSubject",
    "AuthMethod",
    "AvatarPolicy",
    "ProfileAvatarMixin",
    "Principal",
]
