#!/usr/bin/env python3
"""Stateless, signed session cookies — the HMAC-blob pattern, generalized.

Mirrors ``apps/tokens.py``'s ``AppReadTokenSigner``: a cookie value is a
self-describing, HMAC-SHA256-tagged blob keyed by a server secret, verified by
recomputing the tag in constant time. No store — validity (expiry) is carried in
the signed payload and decided against an injected ``now``.

Two cookies ride this one signer, distinguished by a ``typ`` field the caller
sets and checks (domain separation, so a login-state blob can never be replayed
as a logged-in session):

* the **session** cookie — ``{typ: "session", sub: <principal subject>}`` with the
  configured session TTL, set at a successful callback and read on every request;
* the **login-state** cookie — the short-lived PKCE ``code_verifier`` + ``state`` +
  ``nonce`` + return-to that bridge ``/login`` and ``/callback``.

Unlike ``AppReadTokenSigner`` (whose ``app_id`` is a colon-free slug so a
``split(":")`` recovers the parts), a principal ``subject`` contains a colon
(``user:<uuid>``), so the payload is a base64url-encoded JSON object rather than a
delimited string — unambiguous and trivially extensible.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

# Session cookie defaults (overridable from ``AuthSettings.session``). The state
# cookie is minted with a short explicit TTL per login, not this default.
_DEFAULT_SESSION_TTL = timedelta(hours=8)

# Login handshakes are seconds-to-a-minute affairs; the state cookie must not
# outlive one, so a stale/replayed login attempt simply expires.
LOGIN_STATE_TTL = timedelta(minutes=10)


class CookieSigner:
    """Signs + verifies stateless cookie payloads (HMAC-SHA256 over base64url JSON).

    Atomic feature class: the signing ``secret`` and default ``ttl`` are its DI'd
    state; :meth:`sign`/:meth:`verify` are its behavior. The clock is always a
    method argument — a test pins ``now`` instead of patching a wall clock.
    """

    def __init__(self, *, secret: str | bytes, ttl: timedelta = _DEFAULT_SESSION_TTL) -> None:
        """Capture the server *secret* and the default cookie *ttl*."""
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        self._ttl = ttl

    def _sign(self, body: str) -> str:
        digest = hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def sign(
        self,
        payload: Mapping[str, Any],
        *,
        now: datetime,
        ttl: timedelta | None = None,
    ) -> str:
        """Return a signed ``<body>.<sig>`` blob carrying *payload* plus iat/exp.

        Expiry is truncated to whole seconds so the signed instant round-trips
        exactly. The caller supplies a ``typ`` in *payload* for domain separation.
        """
        expires_at = now + (ttl if ttl is not None else self._ttl)
        full = {**payload, "iat": int(now.timestamp()), "exp": int(expires_at.timestamp())}
        raw = json.dumps(full, sort_keys=True, separators=(",", ":")).encode("utf-8")
        body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        return f"{body}.{self._sign(body)}"

    def verify(self, token: str, *, now: datetime) -> dict[str, Any] | None:
        """Return the payload if the blob is authentic and unexpired, else ``None``.

        A malformed, forged, or expired blob all return ``None`` — the caller maps
        that to "unauthenticated" with no oracle about which failed.
        """
        parts = token.split(".")
        if len(parts) != 2:
            return None
        body, sig = parts
        if not hmac.compare_digest(sig, self._sign(body)):
            return None
        padding = "=" * (-len(body) % 4)
        try:
            decoded = base64.urlsafe_b64decode(body + padding)
            payload = json.loads(decoded)
        except (binascii.Error, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)) or int(now.timestamp()) >= int(exp):
            return None
        return payload


__all__ = ["CookieSigner", "LOGIN_STATE_TTL"]
