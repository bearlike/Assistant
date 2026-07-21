#!/usr/bin/env python3
"""JWT signature + claims verification against a provider's JWKS.

Every fetch is injected, so the whole security-critical path — algorithm
pinning, signature, issuer, audience, expiry, key rotation — runs with zero
sockets. The clock is a fixed ``NOW`` passed as an argument, never patched.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from joserfc import jwt
from joserfc.jwk import OctKey, RSAKey
from mewbo_iam.authenticators import OidcAuthenticator
from mewbo_iam.drivers.discovery import DiscoveryCache
from mewbo_iam.drivers.jwks import (
    GroupsOverageError,
    JwksVerificationError,
    JwksVerifier,
)

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
ISSUER = "https://idp.example.com"
DISCOVERY_URL = "https://idp.example.com/.well-known/openid-configuration"
JWKS_URI = "https://idp.example.com/jwks"
CLIENT_ID = "mewbo-console"

# The driver's own clock-skew allowance. The boundary tests below bracket it.
LEEWAY_SECONDS = 45


class FakeIdp:
    """A canned OIDC provider: signing keys, a discovery doc, and a JWKS.

    Counts each fetch so a test can prove how many times the verifier went to
    the network — the rotation law is about fetch COUNT, not just the outcome.
    """

    def __init__(self, *, issuer: str = ISSUER) -> None:
        self.issuer = issuer
        self.keys: dict[str, RSAKey] = {}
        self.published: list[str] = []
        self.discovery_fetches = 0
        self.jwks_fetches = 0
        self.add_key("k1", publish=True)

    def add_key(self, kid: str, *, publish: bool) -> RSAKey:
        """Mint a signing key; ``publish`` puts it in the served JWKS."""
        key = RSAKey.generate_key(2048, parameters={"kid": kid})
        self.keys[kid] = key
        if publish:
            self.published.append(kid)
        return key

    def fetch_discovery(self, url: str) -> dict[str, Any]:
        """The discovery-document JSON GET the ``DiscoveryCache`` injects."""
        assert url == DISCOVERY_URL
        self.discovery_fetches += 1
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "jwks_uri": JWKS_URI,
            # A real document carries dozens of fields we never read; keeping one
            # here proves OidcDiscovery's extra="ignore" is load-bearing.
            "response_types_supported": ["code"],
        }

    def fetch_jwks(self, url: str) -> dict[str, Any]:
        """The JWKS JSON GET, serving only the currently published keys."""
        assert url == JWKS_URI
        self.jwks_fetches += 1
        return {"keys": [self.keys[kid].as_dict(private=False) for kid in self.published]}

    def issue(
        self,
        *,
        kid: str = "k1",
        alg: str = "RS256",
        issuer: str | None = None,
        audience: object = CLIENT_ID,
        expires_in: timedelta = timedelta(hours=1),
        not_before: timedelta | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """Sign a token with one of this provider's keys."""
        claims: dict[str, Any] = {
            "iss": self.issuer if issuer is None else issuer,
            "sub": "user-1",
            "iat": int(NOW.timestamp()),
            "exp": int((NOW + expires_in).timestamp()),
        }
        if audience is not None:
            claims["aud"] = audience
        if not_before is not None:
            claims["nbf"] = int((NOW + not_before).timestamp())
        claims.update(extra or {})
        return jwt.encode({"alg": alg, "kid": kid}, claims, self.keys[kid])


def make_authenticator(**overrides: Any) -> OidcAuthenticator:
    """An OIDC authenticator pointed at :class:`FakeIdp`."""
    fields: dict[str, Any] = {
        "name": "corp",
        "issuer": ISSUER,
        "discovery_url": DISCOVERY_URL,
        "client_id": CLIENT_ID,
        "client_secret": "s3cret",
    }
    fields.update(overrides)
    return OidcAuthenticator(**fields)


def make_verifier(idp: FakeIdp) -> JwksVerifier:
    """A verifier wired to *idp*'s injected fetchers — no sockets."""
    return JwksVerifier(
        discovery=DiscoveryCache(fetch=idp.fetch_discovery),
        fetch=idp.fetch_jwks,
    )


def admin_claims() -> dict[str, Any]:
    """Claims a forged token would carry — everything valid except the signature."""
    return {
        "iss": ISSUER,
        "sub": "admin",
        "aud": CLIENT_ID,
        "exp": int((NOW + timedelta(hours=1)).timestamp()),
    }


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


@pytest.fixture
def verifier(idp: FakeIdp) -> JwksVerifier:
    return make_verifier(idp)


@pytest.fixture
def authenticator() -> OidcAuthenticator:
    return make_authenticator()


# ── the happy path ────────────────────────────────────────────────────────────
def test_validly_signed_token_verifies_and_returns_its_claims(idp, verifier, authenticator):
    """A token signed by the provider's published key verifies."""
    claims = verifier.verify(idp.issue(), authenticator, now=NOW)

    assert claims["sub"] == "user-1"
    assert claims["iss"] == ISSUER
    assert claims["aud"] == CLIENT_ID


def test_audience_may_arrive_as_a_list_containing_the_expected_value(idp, verifier, authenticator):
    """Providers legitimately emit ``aud`` as an array; the match is membership."""
    token = idp.issue(audience=[CLIENT_ID, "some-other-rp"])

    assert verifier.verify(token, authenticator, now=NOW)["sub"] == "user-1"


# ── signature ─────────────────────────────────────────────────────────────────
def test_token_signed_by_the_wrong_key_is_rejected(idp, verifier, authenticator):
    """A signature from a key that is not the provider's never verifies."""
    # An attacker's key published under a kid the provider legitimately serves:
    # the kid resolves, the signature does not.
    attacker = RSAKey.generate_key(2048, parameters={"kid": "k1"})
    forged = jwt.encode({"alg": "RS256", "kid": "k1"}, admin_claims(), attacker)

    with pytest.raises(JwksVerificationError, match="signature verification failed"):
        verifier.verify(forged, authenticator, now=NOW)


def test_token_whose_kid_is_absent_everywhere_is_rejected(idp, verifier, authenticator):
    """An unresolvable kid fails closed rather than falling back to any key."""
    idp.add_key("unpublished", publish=False)
    token = idp.issue(kid="unpublished")

    with pytest.raises(JwksVerificationError, match="no JWKS key"):
        verifier.verify(token, authenticator, now=NOW)


# ── algorithm pinning ─────────────────────────────────────────────────────────
def test_alg_none_is_rejected_before_any_crypto_runs(idp, verifier, authenticator):
    """``alg: none`` — an unsigned token — is never accepted."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps(admin_claims()).encode()).rstrip(b"=")
    unsigned = f"{header.decode()}.{payload.decode()}."

    with pytest.raises(JwksVerificationError, match="disallowed or missing JWT alg"):
        verifier.verify(unsigned, authenticator, now=NOW)


def test_symmetric_hs256_forgery_is_rejected_by_the_algorithm_allowlist(
    idp, verifier, authenticator
):
    """The classic downgrade: HMAC-sign with the provider's PUBLIC key as the secret."""
    public_material = json.dumps(idp.keys["k1"].as_dict(private=False), sort_keys=True)
    forged = jwt.encode(
        {"alg": "HS256", "kid": "k1"}, admin_claims(), OctKey.import_key(public_material)
    )

    with pytest.raises(JwksVerificationError, match="disallowed or missing JWT alg"):
        verifier.verify(forged, authenticator, now=NOW)


def test_an_algorithm_outside_the_configured_allowlist_is_rejected(idp, authenticator):
    """Narrowing ``algorithms`` narrows what verifies, even for a real signature."""
    verifier = JwksVerifier(
        discovery=DiscoveryCache(fetch=idp.fetch_discovery),
        fetch=idp.fetch_jwks,
        algorithms=["ES256"],
    )

    with pytest.raises(JwksVerificationError, match="disallowed or missing JWT alg"):
        verifier.verify(idp.issue(alg="RS256"), authenticator, now=NOW)


def test_a_header_without_an_alg_is_rejected(idp, verifier, authenticator):
    """A missing ``alg`` is not a default — it is a rejection."""
    header = base64.urlsafe_b64encode(json.dumps({"kid": "k1"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "x"}).encode()).rstrip(b"=")

    with pytest.raises(JwksVerificationError, match="disallowed or missing JWT alg"):
        verifier.verify(f"{header.decode()}.{payload.decode()}.sig", authenticator, now=NOW)


def test_a_token_that_is_not_a_compact_jws_is_rejected(idp, verifier, authenticator):
    """A non-JWT string fails on shape, not with an unhandled exception."""
    with pytest.raises(JwksVerificationError, match="three segments"):
        verifier.verify("not-a-token", authenticator, now=NOW)


# ── expiry / not-before, and the leeway boundary ──────────────────────────────
def test_an_expired_token_is_rejected(idp, verifier, authenticator):
    """``exp`` well in the past fails claim validation."""
    with pytest.raises(JwksVerificationError, match="claim validation failed"):
        verifier.verify(idp.issue(expires_in=timedelta(hours=-2)), authenticator, now=NOW)


def test_a_token_expired_within_the_clock_skew_leeway_still_verifies(idp, verifier, authenticator):
    """Just inside the leeway, a stale ``exp`` is tolerated — clocks drift."""
    token = idp.issue(expires_in=timedelta(seconds=-(LEEWAY_SECONDS - 5)))

    assert verifier.verify(token, authenticator, now=NOW)["sub"] == "user-1"


def test_a_token_expired_beyond_the_clock_skew_leeway_is_rejected(idp, verifier, authenticator):
    """Just outside the leeway, the same staleness is fatal — the boundary holds."""
    token = idp.issue(expires_in=timedelta(seconds=-(LEEWAY_SECONDS + 5)))

    with pytest.raises(JwksVerificationError, match="claim validation failed"):
        verifier.verify(token, authenticator, now=NOW)


def test_a_not_yet_valid_token_is_rejected(idp, verifier, authenticator):
    """An ``nbf`` in the future fails — a pre-dated token is not usable early."""
    token = idp.issue(not_before=timedelta(hours=1))

    with pytest.raises(JwksVerificationError, match="claim validation failed"):
        verifier.verify(token, authenticator, now=NOW)


def test_a_not_before_inside_the_leeway_still_verifies(idp, verifier, authenticator):
    """The same skew allowance applies to ``nbf``."""
    token = idp.issue(not_before=timedelta(seconds=LEEWAY_SECONDS - 5))

    assert verifier.verify(token, authenticator, now=NOW)["sub"] == "user-1"


# ── issuer ────────────────────────────────────────────────────────────────────
def test_a_token_from_the_wrong_issuer_is_rejected(idp, verifier, authenticator):
    """``iss`` must equal the configured issuer exactly."""
    token = idp.issue(issuer="https://evil.example.com")

    with pytest.raises(JwksVerificationError, match="claim validation failed"):
        verifier.verify(token, authenticator, now=NOW)


def test_a_discovery_document_disagreeing_with_the_configured_issuer_is_rejected(idp, verifier):
    """Mix-up defense: the discovery doc's own ``issuer`` must match the config too."""
    # The authenticator claims one issuer; the document served at its discovery
    # URL announces another. Trusting that document's jwks_uri would let whoever
    # controls it sign tokens for us.
    mismatched = make_authenticator(issuer="https://other.example.com")

    with pytest.raises(JwksVerificationError, match="discovery issuer does not match"):
        verifier.verify(idp.issue(issuer="https://other.example.com"), mismatched, now=NOW)


# ── audience ──────────────────────────────────────────────────────────────────
def test_a_token_for_a_different_audience_is_rejected(idp, verifier, authenticator):
    """A token minted for another relying party must not authenticate here."""
    with pytest.raises(JwksVerificationError, match="claim validation failed"):
        verifier.verify(idp.issue(audience="some-other-rp"), authenticator, now=NOW)


def test_a_token_with_no_audience_at_all_is_rejected(idp, verifier, authenticator):
    """``aud`` is essential — an audience-less token is not deliberately scoped."""
    with pytest.raises(JwksVerificationError, match="claim validation failed"):
        verifier.verify(idp.issue(audience=None), authenticator, now=NOW)


def test_the_audience_argument_overrides_the_configured_one(idp, verifier):
    """The callback passes the client id for an ID token; a bearer token defaults."""
    # Configured audience is the API; the ID token at callback carries the client id.
    configured = make_authenticator(audience="https://api.example.com")
    id_token = idp.issue(audience=CLIENT_ID)

    assert verifier.verify(id_token, configured, now=NOW, audience=CLIENT_ID)["sub"] == "user-1"
    with pytest.raises(JwksVerificationError, match="claim validation failed"):
        verifier.verify(id_token, configured, now=NOW)


# ── nonce (ID-token replay defense) ───────────────────────────────────────────
def test_a_mismatched_nonce_is_rejected(idp, verifier, authenticator):
    """The ID token's nonce must equal the one the login request generated."""
    token = idp.issue(extra={"nonce": "from-a-different-login"})

    with pytest.raises(JwksVerificationError, match="nonce does not match"):
        verifier.verify(token, authenticator, now=NOW, nonce="this-login")


def test_a_matching_nonce_verifies(idp, verifier, authenticator):
    """The nonce round-trips when it is the one we issued."""
    token = idp.issue(extra={"nonce": "this-login"})
    claims = verifier.verify(token, authenticator, now=NOW, nonce="this-login")

    assert claims["nonce"] == "this-login"


# ── key rotation: exactly one refetch, and not one per request ────────────────
def test_an_unknown_kid_triggers_exactly_one_jwks_refetch_and_then_verifies(
    idp, verifier, authenticator
):
    """Key rotation heals without a restart — and without refetching per call."""
    verifier.verify(idp.issue(kid="k1"), authenticator, now=NOW)
    assert idp.jwks_fetches == 1

    # The provider rotates: a new key appears in the served JWKS, and tokens
    # start arriving signed by it. The cached keyset has never seen this kid.
    idp.add_key("k2", publish=True)
    assert verifier.verify(idp.issue(kid="k2"), authenticator, now=NOW)["sub"] == "user-1"
    assert idp.jwks_fetches == 2, "an unknown kid must force exactly one refetch"

    # The refreshed cache now serves k2 — a second token must NOT refetch.
    verifier.verify(idp.issue(kid="k2"), authenticator, now=NOW)
    verifier.verify(idp.issue(kid="k2"), authenticator, now=NOW)
    assert idp.jwks_fetches == 2, "a resolvable kid must never trigger a refetch"


def test_an_unresolvable_kid_refetches_once_and_then_gives_up(idp, verifier, authenticator):
    """The rotation path costs one refetch per request, never an unbounded retry."""
    idp.add_key("ghost", publish=False)
    token = idp.issue(kid="ghost")

    with pytest.raises(JwksVerificationError, match="no JWKS key"):
        verifier.verify(token, authenticator, now=NOW)
    assert idp.jwks_fetches == 2, "one cold fetch plus exactly one rotation refetch"


def test_the_discovery_document_is_fetched_once_and_cached(idp, verifier, authenticator):
    """Discovery is per-provider state, not per-request work."""
    for _ in range(3):
        verifier.verify(idp.issue(), authenticator, now=NOW)

    assert idp.discovery_fetches == 1


# ── the deliberate fail-loud: Entra groups overage ────────────────────────────
def test_an_entra_groups_overage_raises_rather_than_yielding_no_groups(
    idp, verifier, authenticator
):
    """A groups OVERAGE is an operator error, never a silent "user has no groups".

    Entra drops ``groups`` and emits ``_claim_names``/``_claim_sources`` pointing
    at the Graph API when a user is in too many groups. Reading that as "no
    groups" would strip every group-derived role from the affected user.
    """
    token = idp.issue(
        extra={
            "_claim_names": {"groups": "src1"},
            "_claim_sources": {"src1": {"endpoint": "https://graph.example.com/members"}},
        }
    )

    with pytest.raises(GroupsOverageError):
        verifier.verify(token, authenticator, now=NOW)


def test_the_groups_overage_error_is_a_verification_error(idp, verifier, authenticator):
    """Callers catching the general error must not miss an overage."""
    token = idp.issue(extra={"_claim_names": {"groups": "src1"}})

    with pytest.raises(JwksVerificationError):
        verifier.verify(token, authenticator, now=NOW)


def test_the_claim_sources_half_alone_still_raises(idp, verifier, authenticator):
    """Either overage marker is enough — neither is a normal claim."""
    token = idp.issue(extra={"_claim_sources": {"src1": {"endpoint": "https://graph.example.com/x"}}})

    with pytest.raises(GroupsOverageError):
        verifier.verify(token, authenticator, now=NOW)


def test_inline_groups_verify_normally(idp, verifier, authenticator):
    """The non-overage path is unaffected: groups sent inline pass through."""
    claims = verifier.verify(idp.issue(extra={"groups": ["eng", "admins"]}), authenticator, now=NOW)

    assert claims["groups"] == ["eng", "admins"]
