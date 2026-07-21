#!/usr/bin/env python3
"""JWKS fetch/cache + the pure JWT signature & claims verification (joserfc).

:class:`JwksVerifier` turns a raw JWT (an ID token at callback, or a bearer
access token on an API call) into its verified claims, or raises. The fetch (the
provider's JWKS) is isolated in :meth:`_resolve_key`; the crypto and claims
decision (:meth:`_verify_claims`) is pure — given a key and an instant it touches
no network, so the security-critical logic is exercised without one.

The verification follows the relying-party rules faithfully:

* **Pin the algorithm.** The token header's ``alg`` must be in an ASYMMETRIC
  allowlist — ``none`` and the symmetric ``HS*`` family are rejected before any
  crypto runs (an ``HS256`` forgery signed with the public key as an HMAC secret
  is the classic downgrade attack).
* **Refresh on an unknown ``kid``.** A signing key rotation lands as a ``kid`` the
  cache has not seen; that forces exactly one JWKS refetch before the token is
  rejected, so rotation heals without a restart and without refetching per call.
* **Validate signature, exact issuer, deliberate audience, and expiry** with a
  small clock leeway. Those three claims are REQUIRED — a token omitting any of
  them is rejected. ``nbf`` is checked only when the token carries one, since it
  is not declared essential; do not read this as a guarantee that a token must
  state its not-before. The issuer must equal the configured issuer AND the
  discovery document's own ``issuer`` (mix-up defense).
* **Fail loud on a groups overage.** Entra (Azure AD) drops the ``groups`` claim
  and emits ``_claim_names``/``_claim_sources`` pointing at the Graph API when a
  user is in too many groups. Silently reading that as "no groups" would strip a
  user's roles; :class:`GroupsOverageError` makes it an actionable operator error.
"""

from __future__ import annotations

import base64
import binascii
import json
import threading
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import Key, KeySet
from joserfc.jwt import JWTClaimsRegistry

from mewbo_iam.drivers._http import http_get_json
from mewbo_iam.drivers.discovery import DiscoveryCache, JsonFetch

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mewbo_iam.authenticators import OidcAuthenticator

# Asymmetric signature algorithms only. ``none`` (unsigned) and the symmetric
# ``HS*`` family are never accepted — both enable trivial forgeries against a
# relying party that holds only the provider's public keys.
_DEFAULT_ALGORITHMS: frozenset[str] = frozenset(
    {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"}
)

# Clock skew tolerance for exp/nbf/iat — inside the 30-60s the research settled on.
_LEEWAY_SECONDS = 45

# JWKS are cached this long; a kid the cache has not seen forces an out-of-band
# refresh regardless, so this bound only governs proactive re-fetching.
_JWKS_TTL = timedelta(hours=6)


class JwksVerificationError(Exception):
    """A JWT failed verification (bad signature, claim, algorithm, or key)."""


class GroupsOverageError(JwksVerificationError):
    """The provider emitted a groups OVERAGE instead of the groups claim.

    Raised, never swallowed: treating an overage as "no groups" would silently
    strip every group-derived role from an affected user. The operator must
    configure group emission (a groups assignment / an app-role claim) so the
    token carries them inline.
    """


@dataclass
class _JwksEntry:
    """One cached JWKS plus the instant it was fetched."""

    keyset: KeySet
    fetched_at: datetime


class JwksVerifier:
    """Verifies JWTs against a provider's JWKS, refreshing on key rotation.

    Atomic class: the shared :class:`DiscoveryCache`, the injected JWKS ``fetch``,
    the cache ``ttl``, the clock ``leeway``, and the ``algorithms`` allowlist are
    its state; :meth:`verify` is its behavior. ONE verifier serves every OIDC
    authenticator — the authenticator (its issuer, audience, discovery URL)
    arrives as a method argument, and caches key by the provider's URLs.
    """

    def __init__(
        self,
        *,
        discovery: DiscoveryCache,
        fetch: JsonFetch = http_get_json,
        ttl: timedelta = _JWKS_TTL,
        leeway: int = _LEEWAY_SECONDS,
        algorithms: Collection[str] = _DEFAULT_ALGORITHMS,
    ) -> None:
        """Capture the discovery cache, the JWKS fetcher, and the crypto policy."""
        self._discovery = discovery
        self._fetch = fetch
        self._ttl = ttl
        self._leeway = leeway
        self._algorithms = frozenset(algorithms)
        self._entries: dict[str, _JwksEntry] = {}  # keyed by jwks_uri
        self._lock = threading.Lock()

    # ── public API ───────────────────────────────────────────────────────────
    def verify(
        self,
        token: str,
        authenticator: OidcAuthenticator,
        *,
        now: datetime,
        audience: str | None = None,
        nonce: str | None = None,
    ) -> dict[str, Any]:
        """Return the verified claims of *token*, or raise :class:`JwksVerificationError`.

        *audience* overrides the expected ``aud`` (the callback passes the
        client id for an ID token; a bearer access token defaults to the
        authenticator's configured ``audience``, then the client id). *nonce*, when
        given, must equal the token's ``nonce`` claim (ID-token replay defense).
        """
        header = self._parse_header(token)
        alg = header.get("alg")
        if not isinstance(alg, str) or alg not in self._algorithms:
            raise JwksVerificationError(f"disallowed or missing JWT alg: {alg!r}")
        discovery = self._discovery.get(authenticator.discovery_url, now=now)
        if discovery.issuer != authenticator.issuer:
            raise JwksVerificationError(
                "discovery issuer does not match the configured issuer "
                f"({discovery.issuer!r} != {authenticator.issuer!r})"
            )
        key = self._resolve_key(discovery.jwks_uri, header.get("kid"), now)
        return self._verify_claims(token, key, authenticator, now, audience, nonce)

    # ── fetch leg (isolated I/O) ──────────────────────────────────────────────
    def _resolve_key(self, jwks_uri: str, kid: object, now: datetime) -> Key:
        """Return the signing key for *kid*, refreshing the JWKS on an unknown one.

        A missing or TTL-expired cache entry is (re)fetched; if the key id is
        still absent the JWKS is refetched exactly once more (the rotation path)
        before giving up. ``kid`` may be ``None`` — a single-key set resolves it.
        """
        keyset = self._keyset(jwks_uri, now, force=False)
        try:
            return keyset.get_by_kid(kid if isinstance(kid, str) else None)
        except JoseError:
            keyset = self._keyset(jwks_uri, now, force=True)
        try:
            return keyset.get_by_kid(kid if isinstance(kid, str) else None)
        except JoseError as exc:
            raise JwksVerificationError(f"no JWKS key for kid {kid!r}") from exc

    def _keyset(self, jwks_uri: str, now: datetime, *, force: bool) -> KeySet:
        """The cached JWKS for *jwks_uri*, fetched on a miss/expiry/``force``."""
        with self._lock:
            entry = self._entries.get(jwks_uri)
            if entry is not None and not force and now - entry.fetched_at < self._ttl:
                return entry.keyset
        raw = self._fetch(jwks_uri)
        keyset = KeySet.import_key_set(raw)
        with self._lock:
            self._entries[jwks_uri] = _JwksEntry(keyset=keyset, fetched_at=now)
        return keyset

    # ── pure verification (no I/O) ────────────────────────────────────────────
    @staticmethod
    def _parse_header(token: str) -> dict[str, Any]:
        """Decode the JOSE header without verifying — for ``alg``/``kid`` only.

        Reading the header selects a key and gates the algorithm; nothing here is
        trusted until :meth:`_verify_claims` checks the signature over it.
        """
        segments = token.split(".")
        if len(segments) != 3:
            raise JwksVerificationError("not a compact JWS (expected three segments)")
        raw = segments[0]
        padding = "=" * (-len(raw) % 4)
        try:
            decoded = base64.urlsafe_b64decode(raw + padding)
            header = json.loads(decoded)
        except (binascii.Error, ValueError) as exc:
            raise JwksVerificationError("unreadable JWT header") from exc
        if not isinstance(header, dict):
            raise JwksVerificationError("JWT header is not an object")
        return header

    def _verify_claims(
        self,
        token: str,
        key: Key,
        authenticator: OidcAuthenticator,
        now: datetime,
        audience: str | None,
        nonce: str | None,
    ) -> dict[str, Any]:
        """Verify the signature, then the standard claims. Pure — no network."""
        try:
            decoded = jwt.decode(token, key, algorithms=list(self._algorithms))
        except (JoseError, ValueError) as exc:
            raise JwksVerificationError("JWT signature verification failed") from exc
        claims = dict(decoded.claims)
        if "_claim_names" in claims or "_claim_sources" in claims:
            raise GroupsOverageError(
                "the identity provider returned a groups OVERAGE (_claim_names / "
                "_claim_sources) instead of the groups inline: the user is in more "
                "groups than the token can carry. Configure the provider to emit "
                "group or app-role claims directly, or reduce group membership."
            )
        expected_aud = audience or authenticator.audience or authenticator.client_id
        registry = JWTClaimsRegistry(
            now=int(now.timestamp()),
            leeway=self._leeway,
            iss={"essential": True, "value": authenticator.issuer},
            aud={"essential": True, "value": expected_aud},
            exp={"essential": True},
        )
        try:
            registry.validate(claims)
        except JoseError as exc:
            raise JwksVerificationError(f"JWT claim validation failed: {exc}") from exc
        if nonce is not None and claims.get("nonce") != nonce:
            raise JwksVerificationError("JWT nonce does not match the login request")
        return claims


__all__ = ["JwksVerifier", "JwksVerificationError", "GroupsOverageError"]
