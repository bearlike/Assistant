#!/usr/bin/env python3
"""RFC 7662 token introspection — the opaque-access-token fallback.

Some providers issue opaque (non-JWT) access tokens that a relying party cannot
verify locally; :class:`IntrospectionClient` asks the provider's introspection
endpoint whether such a token is active and, if so, returns its claims. Results
are cached for a SHORT TTL keyed by a hash of the token, so a burst of API calls
bearing the same token does not hammer the provider — while a revoked token still
falls out of the cache within seconds.

This is a fallback, gated per-authenticator by the caller: a JWT access token is
verified locally by the JWKS verifier and never reaches here.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from mewbo_iam.drivers._http import http_post_form
from mewbo_iam.drivers.discovery import DiscoveryCache

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mewbo_iam.authenticators import OidcAuthenticator

# A form POST with optional HTTP Basic client auth, returning the JSON response.
FormPost = Callable[..., dict[str, Any]]

# Short by design: long enough to absorb a burst of same-token API calls, short
# enough that a revoked token stops being honored within seconds.
_INTROSPECTION_TTL = timedelta(seconds=45)


@dataclass(frozen=True)
class IntrospectionResult:
    """The subset of an RFC 7662 response the identity layer consumes.

    ``active`` is authoritative — an inactive token yields ``active=False`` and no
    usable claims; ``claims`` is the full response for the caller to map (its
    ``sub``/``username``/scope/group fields) the same way it maps JWT claims.
    """

    active: bool
    claims: dict[str, Any]


@dataclass
class _CacheEntry:
    """One cached introspection result plus its expiry instant."""

    result: IntrospectionResult
    expires_at: datetime


class IntrospectionClient:
    """Introspects opaque access tokens (RFC 7662), with a short-TTL cache.

    Atomic class: the shared :class:`DiscoveryCache`, the injected form ``post``,
    and the cache ``ttl`` are its state; :meth:`introspect` is its behavior. The
    token is never logged or stored in the clear — the cache keys on its SHA-256.
    """

    def __init__(
        self,
        *,
        discovery: DiscoveryCache,
        post: FormPost = http_post_form,
        ttl: timedelta = _INTROSPECTION_TTL,
    ) -> None:
        """Capture the discovery cache, the form-POST transport, and the cache TTL."""
        self._discovery = discovery
        self._post = post
        self._ttl = ttl
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()

    def introspect(
        self,
        token: str,
        authenticator: OidcAuthenticator,
        *,
        now: datetime,
    ) -> IntrospectionResult:
        """Return whether *token* is active (+ its claims), cached briefly.

        Returns an inactive result when the provider advertises no introspection
        endpoint — there is nothing to ask, so the token cannot be honored.
        """
        cache_key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._lock:
            entry = self._cache.get(cache_key)
            if entry is not None and now < entry.expires_at:
                return entry.result
        result = self._introspect_now(token, authenticator, now)
        with self._lock:
            self._cache[cache_key] = _CacheEntry(result=result, expires_at=now + self._ttl)
        return result

    def _introspect_now(
        self, token: str, authenticator: OidcAuthenticator, now: datetime
    ) -> IntrospectionResult:
        """Perform the live introspection POST (uncached)."""
        discovery = self._discovery.get(authenticator.discovery_url, now=now)
        endpoint = discovery.introspection_endpoint
        if not endpoint:
            return IntrospectionResult(active=False, claims={})
        body: Mapping[str, str] = {"token": token, "token_type_hint": "access_token"}
        response = self._post(
            endpoint,
            body,
            auth=(authenticator.client_id, authenticator.client_secret),
        )
        active = bool(response.get("active"))
        return IntrospectionResult(active=active, claims=dict(response) if active else {})


__all__ = ["IntrospectionClient", "IntrospectionResult"]
