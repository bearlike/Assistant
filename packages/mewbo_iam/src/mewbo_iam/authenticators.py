#!/usr/bin/env python3
"""Authenticator specifications — the discriminated union of identity sources.

An ``AuthenticatorSpec`` is a durable config record for one way of proving
identity: a local API key, an OIDC provider, a trusted reverse-proxy header, an
LDAP directory, or a SAML IdP. Every kind owns its own validators and its own
pure claim/attribute → :class:`RawIdentity` resolution — there is deliberately
no service-side ``if kind ==`` dispatch, which would drift the moment a kind
gains a field.

Models never import I/O. The network legs (OIDC discovery + token exchange, the
LDAP bind/search, SAML signature verification) live in ``drivers/``; here, an
already-fetched claims/entry/attribute ``Mapping`` is handed to ``resolve`` as an
argument and mapped to a normalized identity.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from mewbo_iam.principal import ExternalSubject


class RawIdentity(BaseModel):
    """The normalized identity every authenticator's ``resolve`` yields.

    A provider-neutral intermediate: the JIT/SCIM join key plus the profile
    fields an authenticator could extract. The caller (the app-side AuthKit)
    turns it into a ``UserRecord``/``Principal`` — this model carries no roles
    or scopes, only what the IdP asserted.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    external_subject: ExternalSubject
    email: str | None = None
    email_verified: bool | None = None
    display: str | None = None
    groups: tuple[str, ...] = ()
    picture_url: str | None = None


class AuthenticatorSpec(BaseModel):
    """Shared envelope for every authenticator kind.

    Deliberately does NOT declare the ``kind`` discriminator — each concrete
    kind declares its own ``kind: Literal[...]`` (see :data:`AuthenticatorUnion`).
    A base ``kind: str`` that variants narrow to a ``Literal`` is an incompatible
    mutable-field override; keeping the discriminator only on the members avoids
    it and lets consumers type against the union, where ``.kind`` is available.
    Holds the common config contract and the dotted-path claim reader.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    enabled: bool = True

    # Lazily built on first ``parse`` and cached on the base — every kind
    # resolves through one adapter. Left unparameterized so this ClassVar does
    # not force ``AuthenticatorUnion`` to exist at class-body evaluation time.
    _adapter: ClassVar[TypeAdapter | None] = None

    @classmethod
    def parse(cls, data: Mapping[str, object]) -> AuthenticatorUnion:
        """Parse a raw dict (config entry or stored doc) into its concrete kind."""
        if AuthenticatorSpec._adapter is None:
            AuthenticatorSpec._adapter = TypeAdapter(AuthenticatorUnion)
        return AuthenticatorSpec._adapter.validate_python(data)

    @staticmethod
    def _dig(claims: Mapping[str, object], path: str) -> object | None:
        """Read a dotted-path value out of a nested claims mapping.

        ``"a.b.c"`` descends ``claims["a"]["b"]["c"]``; a missing segment or a
        non-mapping mid-path yields ``None`` rather than raising, so a provider
        omitting an optional claim degrades cleanly.
        """
        current: object = claims
        for part in path.split("."):
            if isinstance(current, Mapping):
                current = current.get(part)
            else:
                return None
        return current

    @staticmethod
    def normalize_groups(value: object, delimiter: str | None = None) -> tuple[str, ...]:
        """Coerce a claim/attribute value into a tuple of group names.

        Accepts the three shapes providers actually emit: a list/tuple of names,
        a single delimited string, or a scalar. Anything else (or ``None``)
        yields an empty tuple. Not a business rule — pure shape coercion, shared
        by every kind whose group source is free-form, and by the LDAP driver,
        which reads the same attribute shapes off a directory entry.
        """
        if value is None:
            return ()
        if isinstance(value, (list, tuple)):
            return tuple(str(item) for item in value if str(item))
        text = str(value)
        if delimiter:
            return tuple(part.strip() for part in text.split(delimiter) if part.strip())
        return (text,) if text else ()


class ApiKeyAuthenticator(AuthenticatorSpec):
    """Local API-key authentication.

    A marker record: keys carry no external claims, so this kind has no
    ``resolve`` — a presented key is verified against the key store and its
    identity comes from the store, not from a claims mapping. It exists in the
    union so "local keys are enabled" is a first-class, configurable authenticator.
    """

    kind: Literal["api_key"] = "api_key"
    issuer: str = "mewbo"


class OidcAuthenticator(AuthenticatorSpec):
    """OpenID Connect relying-party configuration.

    Claim paths are dotted strings so a provider that nests (e.g.
    ``resource_access.mewbo.roles``) is reachable without code changes.

    ``introspection`` selects how an ACCESS token is verified: off (the default)
    means JWT-only, verified locally against the provider's JWKS; on adds the
    RFC 7662 fallback for providers that issue opaque tokens. It does not affect
    the ID token, which is always a JWT.
    """

    kind: Literal["oidc"] = "oidc"
    issuer: str
    discovery_url: str
    client_id: str
    client_secret: str
    audience: str | None = None
    scopes: tuple[str, ...] = ("openid", "email", "profile")
    introspection: bool = Field(
        default=False,
        description=(
            "Verify opaque (non-JWT) access tokens by calling the provider's RFC 7662 "
            "introspection endpoint, which is read from the issuer's discovery metadata "
            "— set this only if that document advertises `introspection_endpoint`. "
            "Leave off for providers that issue JWT access tokens: those are verified "
            "locally against the JWKS, which needs no call to the provider. Turning it "
            "on trades a short-lived network call per distinct token for near-immediate "
            "recognition of a revoked one."
        ),
    )
    identity_claim: str = "sub"
    email_claim: str = "email"
    email_verified_claim: str = "email_verified"
    name_claim: str = "name"
    groups_claim: str = "groups"
    picture_claim: str = "picture"

    def resolve(self, claims: Mapping[str, object]) -> RawIdentity | None:
        """Map verified ID-token/userinfo claims to a normalized identity.

        Returns ``None`` when the identity claim is absent — an unusable
        assertion, not a partial identity. ``email_verified`` is read only when
        the provider sends a boolean; anything else leaves it unknown (``None``).
        """
        subject = self._dig(claims, self.identity_claim)
        if subject is None:
            return None
        verified = self._dig(claims, self.email_verified_claim)
        email = self._dig(claims, self.email_claim)
        display = self._dig(claims, self.name_claim)
        picture = self._dig(claims, self.picture_claim)
        return RawIdentity(
            external_subject=ExternalSubject(issuer=self.issuer, subject=str(subject)),
            email=str(email) if email is not None else None,
            email_verified=verified if isinstance(verified, bool) else None,
            display=str(display) if display is not None else None,
            groups=self.normalize_groups(self._dig(claims, self.groups_claim)),
            picture_url=str(picture) if picture is not None else None,
        )


class TrustedHeaderAuthenticator(AuthenticatorSpec):
    """Identity asserted by a trusted reverse proxy via request headers.

    Only safe when the proxy is the sole ingress and strips these headers from
    client input — hence ``trusted_proxies`` is required and non-empty, and
    :meth:`is_trusted_source` must gate any use of the parsed identity.
    """

    kind: Literal["trusted_header"] = "trusted_header"
    issuer: str = "trusted-header"
    user_header: str = "X-Forwarded-User"
    groups_header: str = "X-Forwarded-Groups"
    email_header: str = "X-Forwarded-Email"
    name_header: str = "X-Forwarded-Preferred-Username"
    picture_header: str | None = None
    group_delimiter: Literal[",", "|"] = ","
    trusted_proxies: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_proxies(self) -> TrustedHeaderAuthenticator:
        if not self.trusted_proxies:
            raise ValueError("trusted_proxies must be non-empty for a trusted-header authenticator")
        for cidr in self.trusted_proxies:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as exc:
                raise ValueError(f"invalid trusted-proxy CIDR {cidr!r}: {exc}") from exc
        return self

    def is_trusted_source(self, remote_addr: str) -> bool:
        """Whether ``remote_addr`` falls inside a configured trusted-proxy range."""
        try:
            addr = ipaddress.ip_address(remote_addr)
        except ValueError:
            return False
        return any(
            addr in ipaddress.ip_network(cidr, strict=False) for cidr in self.trusted_proxies
        )

    def resolve(self, headers: Mapping[str, str]) -> RawIdentity | None:
        """Parse the proxy-set headers into a normalized identity.

        Caller MUST have already confirmed :meth:`is_trusted_source` — this
        method trusts the headers unconditionally. Returns ``None`` when the
        user header is absent.
        """
        subject = headers.get(self.user_header)
        if not subject:
            return None
        picture = headers.get(self.picture_header) if self.picture_header else None
        return RawIdentity(
            external_subject=ExternalSubject(issuer=self.issuer, subject=subject),
            email=headers.get(self.email_header),
            display=headers.get(self.name_header),
            groups=self.normalize_groups(headers.get(self.groups_header), self.group_delimiter),
            picture_url=picture,
        )


class LdapAuthenticator(AuthenticatorSpec):
    """LDAP / Active Directory directory configuration.

    The model maps an *already-fetched* directory entry to an identity; the
    bind + search that produce that entry are the app-side driver's job (behind
    the ``ldap`` extra). ``user_filter`` is a template the driver formats with
    the login name (e.g. ``(uid={username})``).

    Two ways to read group membership, chosen by whether ``group_base_dn`` is
    set: absent ⇒ read ``group_attribute`` (``memberOf``) straight off the user's
    entry; present ⇒ search that subtree for groups listing the user as a member.
    Only the second can expand NESTED groups, which is why ``nested_groups``
    requires it.
    """

    kind: Literal["ldap"] = "ldap"
    server_url: str
    base_dn: str
    user_filter: str = "(uid={username})"
    uid_attribute: str = "uid"
    mail_attribute: str = "mail"
    name_attribute: str = "cn"
    group_attribute: str = "memberOf"
    nested_groups: bool = False
    # Service account used for the user SEARCH only. The password check is always
    # a rebind as the user; a search hit proves the account exists, never that the
    # presented password is right.
    bind_dn: str | None = None
    bind_password: str | None = None
    # Upgrade a plain ``ldap://`` connection with StartTLS. Ignored (already
    # encrypted) for ``ldaps://``.
    start_tls: bool = False
    # TLS TRUST. Encryption alone proves nothing about WHO answered the socket,
    # and the password check is a rebind — an unauthenticated peer is handed the
    # user's password directly. These two fields are what make an ``ldaps://`` or
    # StartTLS connection authenticate the directory rather than merely encrypt.
    tls_verify: bool = Field(
        default=True,
        description=(
            "Verify the directory's TLS certificate: check it chains to a trusted CA "
            "and that it was issued for the host in `server_url`. Applies to both "
            "`ldaps://` and StartTLS. Leave this on. With it off the connection is "
            "still encrypted, but the server is never authenticated — anyone able to "
            "intercept traffic between Mewbo and the directory can present a "
            "certificate of their own, and because the password check is a bind "
            "against the directory, every password your users type is sent straight "
            "to them. If the certificate is signed by a private CA, set `tls_ca_file` "
            "rather than turning this off."
        ),
    )
    tls_ca_file: str | None = Field(
        default=None,
        description=(
            "Path to a PEM bundle containing the CA certificate that signed the "
            "directory's certificate. Set this when the directory presents a "
            "certificate from a private or internal CA — the usual case for an "
            "in-house Active Directory or OpenLDAP server — which the operating "
            "system's trust store does not know about. A self-signed certificate "
            "works here too: put the certificate itself in the bundle. Left unset, "
            "the system trust store is used, which is what a certificate from a "
            "public CA needs."
        ),
    )
    # Reverse group search: the subtree to search and the attribute on a group
    # that lists its members.
    group_base_dn: str | None = None
    group_member_attribute: str = "member"
    # Which group attribute becomes the group NAME. ``None`` uses the group's DN,
    # matching what ``memberOf`` returns, so both strategies agree by default.
    group_name_attribute: str | None = None

    @model_validator(mode="after")
    def _validate_directory(self) -> LdapAuthenticator:
        scheme = self.server_url.split("://", 1)[0].lower()
        if scheme not in {"ldap", "ldaps"}:
            raise ValueError(
                f"server_url must be an ldap:// or ldaps:// URL, got {self.server_url!r}"
            )
        if "{username}" not in self.user_filter:
            raise ValueError("user_filter must contain the '{username}' placeholder")
        if (self.bind_dn is None) != (self.bind_password is None):
            raise ValueError("bind_dn and bind_password must be set together")
        if not self.tls_verify and self.tls_ca_file is not None:
            # A CA bundle is only ever consulted while validating, so this pair
            # states two different intentions and one of them is silently lost.
            # Refused rather than resolved: picking either winner leaves a config
            # that READS as the other, and the two differ by whether the
            # directory is authenticated at all.
            raise ValueError(
                "tls_ca_file is only consulted when tls_verify is true — set tls_verify "
                "true to trust that CA, or drop tls_ca_file to state plainly that the "
                "directory's certificate is not checked"
            )
        if self.nested_groups and not self.group_base_dn:
            raise ValueError(
                "nested_groups requires group_base_dn — nesting is expanded by a group "
                "subtree search, which the memberOf attribute cannot do"
            )
        return self

    def resolve(self, entry: Mapping[str, object]) -> RawIdentity | None:
        """Map a directory entry's attributes to a normalized identity.

        Returns ``None`` when the uid attribute is missing. Group values are
        typically member DNs; higher layers translate them via a group→role/team
        mapping.
        """
        uid = entry.get(self.uid_attribute)
        if uid is None:
            return None
        mail = entry.get(self.mail_attribute)
        name = entry.get(self.name_attribute)
        return RawIdentity(
            external_subject=ExternalSubject(issuer=self.server_url, subject=str(uid)),
            email=str(mail) if mail is not None else None,
            display=str(name) if name is not None else None,
            groups=self.normalize_groups(entry.get(self.group_attribute)),
        )


class SamlAuthenticator(AuthenticatorSpec):
    """SAML 2.0 identity-provider configuration.

    Exactly one metadata source (URL or inline XML) is required. The model maps
    an already-validated assertion's attribute statement to an identity; parsing
    and signature-checking the assertion is ``drivers/saml.py``'s job, behind the
    ``saml`` extra.
    """

    kind: Literal["saml"] = "saml"
    issuer: str
    sp_entity_id: str
    idp_metadata_url: str | None = None
    idp_metadata_xml: str | None = None
    subject_attribute: str = "NameID"
    email_attribute: str = "email"
    name_attribute: str = "displayName"
    groups_attribute: str = "groups"

    @model_validator(mode="after")
    def _exactly_one_metadata(self) -> SamlAuthenticator:
        if (self.idp_metadata_url is None) == (self.idp_metadata_xml is None):
            raise ValueError("provide exactly one of idp_metadata_url or idp_metadata_xml")
        return self

    def resolve(self, attributes: Mapping[str, object]) -> RawIdentity | None:
        """Map a validated assertion's attributes to a normalized identity.

        Returns ``None`` when the subject attribute is absent. Multi-valued
        attributes (groups) are normalized to a tuple.
        """
        subject = attributes.get(self.subject_attribute)
        if subject is None:
            return None
        email = attributes.get(self.email_attribute)
        name = attributes.get(self.name_attribute)
        return RawIdentity(
            external_subject=ExternalSubject(issuer=self.issuer, subject=str(subject)),
            email=str(email) if email is not None else None,
            display=str(name) if name is not None else None,
            groups=self.normalize_groups(attributes.get(self.groups_attribute)),
        )


AuthenticatorUnion = Annotated[
    ApiKeyAuthenticator
    | OidcAuthenticator
    | TrustedHeaderAuthenticator
    | LdapAuthenticator
    | SamlAuthenticator,
    Field(discriminator="kind"),
]

# The ONE parse seam (mirrors ``triggers.parse_trigger``): thin module-level
# delegation to the class-owned lazy adapter, kept for the frozen public API.
parse_authenticator = AuthenticatorSpec.parse


__all__ = [
    "RawIdentity",
    "AuthenticatorSpec",
    "ApiKeyAuthenticator",
    "OidcAuthenticator",
    "TrustedHeaderAuthenticator",
    "LdapAuthenticator",
    "SamlAuthenticator",
    "AuthenticatorUnion",
    "parse_authenticator",
]
