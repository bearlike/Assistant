#!/usr/bin/env python3
"""OIDC discovery document — fetch + TTL cache of the provider's metadata.

Both the JWKS verifier and the relying-party client need the same handful of
endpoints out of the provider's ``.well-known/openid-configuration`` document.
:class:`DiscoveryCache` fetches it once per provider and caches it, so the two
drivers share one fetch instead of each pulling their own — the caches are the
one place OIDC network state lives.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict

from mewbo_iam.drivers._http import http_get_json

# A JSON GET: given a URL, return the parsed object. The default is the
# requests-backed ``http_get_json``; a test injects a fake with the same shape.
JsonFetch = Callable[[str], dict[str, Any]]

# Discovery metadata changes rarely (endpoints, not keys); a long TTL is fine and
# a provider migration is an operator event, not a per-request concern. Key
# rotation is handled by the JWKS cache's kid-miss refresh, not here.
_DISCOVERY_TTL = timedelta(hours=24)


class OidcDiscovery(BaseModel):
    """The endpoints this relying party consumes out of the discovery document.

    ``extra="ignore"`` is deliberate and NOT a relaxation of the house
    forbid-extras rule: an OIDC provider-metadata document (RFC 8414) is an open,
    spec-extensible object — every provider ships dozens of fields we do not read
    (``response_types_supported``, ``claims_supported``, …). Forbidding extras
    here would reject every real provider; we validate the fields we actually use
    and let the provider keep the rest.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str | None = None
    introspection_endpoint: str | None = None
    end_session_endpoint: str | None = None


@dataclass
class _DiscoveryEntry:
    """One cached discovery document plus the instant it was fetched."""

    doc: OidcDiscovery
    fetched_at: datetime


class DiscoveryCache:
    """Fetches and caches OIDC discovery documents, keyed by discovery URL.

    Atomic class: the injected ``fetch`` (a JSON GET) and the cache ``ttl`` are
    its state; :meth:`get` is its behavior. The clock arrives as a method
    argument (``now``), never read inside — the drivers are the I/O layer but the
    freshness DECISION stays testable with an injected instant.
    """

    def __init__(
        self,
        *,
        fetch: JsonFetch = http_get_json,
        ttl: timedelta = _DISCOVERY_TTL,
    ) -> None:
        """Capture the JSON ``fetch`` callable and the cache ``ttl``."""
        self._fetch = fetch
        self._ttl = ttl
        self._entries: dict[str, _DiscoveryEntry] = {}
        self._lock = threading.Lock()

    def get(self, discovery_url: str, *, now: datetime, force: bool = False) -> OidcDiscovery:
        """Return the (cached) discovery document for *discovery_url*.

        A fresh cache entry is served without a fetch; a missing or TTL-expired
        one (or ``force=True``) refetches. The fetch runs OUTSIDE the lock so a
        slow provider never serializes every other caller — a concurrent cold
        miss may fetch twice, which is idempotent and cheap.
        """
        with self._lock:
            entry = self._entries.get(discovery_url)
            if entry is not None and not force and now - entry.fetched_at < self._ttl:
                return entry.doc
        raw = self._fetch(discovery_url)
        doc = OidcDiscovery.model_validate(raw)
        with self._lock:
            self._entries[discovery_url] = _DiscoveryEntry(doc=doc, fetched_at=now)
        return doc


__all__ = ["OidcDiscovery", "DiscoveryCache"]
