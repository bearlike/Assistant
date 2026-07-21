#!/usr/bin/env python3
"""RFC 7662 introspection — the opaque-access-token fallback.

The form POST is injected, so the whole path runs offline. The laws that matter
are fail-closed ones: an inactive token yields no claims, a provider with no
introspection endpoint cannot honor a token at all, and the cache must EXPIRE so
a revoked token stops being accepted within seconds rather than indefinitely.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from mewbo_iam.authenticators import OidcAuthenticator
from mewbo_iam.drivers.discovery import DiscoveryCache
from mewbo_iam.drivers.introspection import IntrospectionClient

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
ISSUER = "https://idp.example.com"
DISCOVERY_URL = "https://idp.example.com/.well-known/openid-configuration"
INTROSPECTION_URL = "https://idp.example.com/introspect"
CLIENT_ID = "mewbo-console"
CLIENT_SECRET = "s3cret"
OPAQUE_TOKEN = "AT-opaque-0123456789"

# The driver's own cache TTL. The expiry test brackets it.
TTL_SECONDS = 45


class FakeProvider:
    """A canned provider: a discovery doc plus an introspection endpoint.

    Records every POST so a test can prove the cache actually suppressed calls
    rather than merely returning the right answer.
    """

    def __init__(self, *, endpoint: str | None = INTROSPECTION_URL, active: bool = True) -> None:
        self.endpoint = endpoint
        self.active = active
        self.posts: list[dict[str, Any]] = []

    def fetch_discovery(self, url: str) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "jwks_uri": f"{ISSUER}/jwks",
        }
        if self.endpoint is not None:
            doc["introspection_endpoint"] = self.endpoint
        return doc

    def post_form(self, url: str, body: Any, *, auth: tuple[str, str]) -> dict[str, Any]:
        self.posts.append({"url": url, "body": dict(body), "auth": auth})
        if not self.active:
            return {"active": False}
        return {
            "active": True,
            "sub": "user-1",
            "username": "alice",
            "scope": "openid profile",
            "client_id": CLIENT_ID,
        }


def make_authenticator() -> OidcAuthenticator:
    return OidcAuthenticator(
        name="corp",
        issuer=ISSUER,
        discovery_url=DISCOVERY_URL,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        introspection=True,
    )


def make_client(provider: FakeProvider) -> IntrospectionClient:
    return IntrospectionClient(
        discovery=DiscoveryCache(fetch=provider.fetch_discovery),
        post=provider.post_form,
    )


@pytest.fixture
def authenticator() -> OidcAuthenticator:
    return make_authenticator()


# ── the happy path ────────────────────────────────────────────────────────────
def test_an_active_token_returns_its_claims(authenticator):
    """A provider-confirmed token yields the full response for the caller to map."""
    provider = FakeProvider()

    result = make_client(provider).introspect(OPAQUE_TOKEN, authenticator, now=NOW)

    assert result.active is True
    assert result.claims["sub"] == "user-1"
    assert result.claims["username"] == "alice"


def test_the_request_is_a_form_post_with_client_basic_auth(authenticator):
    """RFC 7662: the token in the body, the RP's own credentials as client auth."""
    provider = FakeProvider()

    make_client(provider).introspect(OPAQUE_TOKEN, authenticator, now=NOW)

    assert len(provider.posts) == 1
    call = provider.posts[0]
    assert call["url"] == INTROSPECTION_URL
    assert call["body"] == {"token": OPAQUE_TOKEN, "token_type_hint": "access_token"}
    assert call["auth"] == (CLIENT_ID, CLIENT_SECRET)


# ── fail-closed ───────────────────────────────────────────────────────────────
def test_an_inactive_token_yields_no_claims_at_all(authenticator):
    """``active`` is authoritative — an inactive token must carry nothing usable."""
    provider = FakeProvider(active=False)

    result = make_client(provider).introspect(OPAQUE_TOKEN, authenticator, now=NOW)

    assert result.active is False
    assert result.claims == {}, "an inactive token must not leak claims a caller might map"


def test_a_provider_advertising_no_introspection_endpoint_cannot_honor_a_token(authenticator):
    """Nothing to ask means the token is not honored — not that it is trusted."""
    provider = FakeProvider(endpoint=None)

    result = make_client(provider).introspect(OPAQUE_TOKEN, authenticator, now=NOW)

    assert result.active is False
    assert result.claims == {}
    assert provider.posts == [], "no endpoint means no request was even attempted"


def test_a_response_omitting_active_entirely_is_treated_as_inactive(authenticator):
    """A malformed response fails closed rather than being read as success."""
    provider = FakeProvider()
    provider.post_form = lambda url, body, *, auth: {"sub": "user-1"}  # no "active"

    result = make_client(provider).introspect(OPAQUE_TOKEN, authenticator, now=NOW)

    assert result.active is False
    assert result.claims == {}


# ── the cache: absorbs a burst, but expires ───────────────────────────────────
def test_repeated_calls_with_the_same_token_hit_the_provider_once(authenticator):
    """A burst of API calls bearing one token must not hammer the provider."""
    provider = FakeProvider()
    client = make_client(provider)

    for _ in range(5):
        assert client.introspect(OPAQUE_TOKEN, authenticator, now=NOW).active is True

    assert len(provider.posts) == 1


def test_distinct_tokens_are_introspected_separately(authenticator):
    """The cache keys on the token — two tokens are two questions."""
    provider = FakeProvider()
    client = make_client(provider)

    client.introspect(OPAQUE_TOKEN, authenticator, now=NOW)
    client.introspect("AT-a-different-token", authenticator, now=NOW)

    assert len(provider.posts) == 2


def test_a_revoked_token_stops_being_honored_once_the_cache_entry_expires(authenticator):
    """The TTL is what bounds how long a revoked token keeps working."""
    provider = FakeProvider()
    client = make_client(provider)

    assert client.introspect(OPAQUE_TOKEN, authenticator, now=NOW).active is True

    # The provider revokes the token; the cached result must not outlive the TTL.
    provider.active = False
    later = NOW + timedelta(seconds=TTL_SECONDS + 5)
    result = client.introspect(OPAQUE_TOKEN, authenticator, now=later)

    assert result.active is False
    assert result.claims == {}
    assert len(provider.posts) == 2, "the expired entry forced a fresh introspection"


def test_a_cached_result_is_still_served_just_inside_the_ttl(authenticator):
    """The boundary holds on the other side too — the cache is not a no-op."""
    provider = FakeProvider()
    client = make_client(provider)

    client.introspect(OPAQUE_TOKEN, authenticator, now=NOW)
    provider.active = False
    still_cached = client.introspect(
        OPAQUE_TOKEN, authenticator, now=NOW + timedelta(seconds=TTL_SECONDS - 5)
    )

    assert still_cached.active is True
    assert len(provider.posts) == 1


# ── the token is never held in the clear ──────────────────────────────────────
def test_the_cache_keys_on_a_hash_and_never_on_the_token_itself(authenticator):
    """A bearer token in a long-lived dict key is a credential at rest."""
    provider = FakeProvider()
    client = make_client(provider)
    client.introspect(OPAQUE_TOKEN, authenticator, now=NOW)

    keys = list(client._cache)
    assert OPAQUE_TOKEN not in keys
    assert keys == [hashlib.sha256(OPAQUE_TOKEN.encode("utf-8")).hexdigest()]
