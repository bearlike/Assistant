#!/usr/bin/env python3
"""The authorization-code + PKCE relying-party flow (authlib).

:class:`OidcClient` drives the three provider round-trips of a browser login: it
builds the authorize-endpoint redirect (with PKCE ``code_challenge``, ``state``
and ``nonce``), exchanges the returned code for tokens, and — when the provider
advertises a userinfo endpoint — fetches the userinfo claims. The ID token is
NEVER used to call an API; it is an assertion about the user, verified by
:class:`~mewbo_iam.drivers.jwks.JwksVerifier` at the callback.

The caller mints and holds ``state``/``nonce``/``code_verifier`` (in a short-lived
signed cookie) and hands them back here — this client keeps no per-login state, so
it is safe to share across requests and workers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from authlib.integrations.requests_client import OAuth2Session

from mewbo_iam.drivers.discovery import DiscoveryCache

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mewbo_iam.authenticators import OidcAuthenticator

# Bounds the token-exchange / userinfo legs so a wedged provider fails the login
# promptly instead of holding a worker thread on an open socket.
_DEFAULT_TIMEOUT = 10.0


class OidcExchangeError(Exception):
    """The authorization-code exchange or userinfo fetch failed."""


@dataclass(frozen=True)
class TokenBundle:
    """The tokens returned by the code exchange.

    ``id_token`` is the verified-elsewhere identity assertion; ``access_token`` is
    what a userinfo/introspection call presents. ``raw`` keeps the full token
    response for anything a later phase needs (refresh token, ``expires_in``).
    """

    id_token: str | None
    access_token: str | None
    token_type: str
    raw: dict[str, Any]


class OidcClient:
    """Builds the authorize redirect and runs the code exchange + userinfo fetch.

    Atomic class: the shared :class:`DiscoveryCache` and the network ``timeout``
    are its only state; every per-login value (redirect URI, PKCE verifier, state,
    nonce) arrives as a method argument. ONE client serves every OIDC
    authenticator.
    """

    def __init__(self, *, discovery: DiscoveryCache, timeout: float = _DEFAULT_TIMEOUT) -> None:
        """Capture the discovery cache and the per-request network timeout."""
        self._discovery = discovery
        self._timeout = timeout

    def _session(self, authenticator: OidcAuthenticator, *, redirect_uri: str) -> OAuth2Session:
        """Build a per-call authlib session for *authenticator*.

        ``code_challenge_method`` is REQUIRED for PKCE and is not implied by
        passing a ``code_verifier`` later: authlib attaches the challenge only
        when ``code_verifier and response_type == "code" and
        self.code_challenge_method == "S256"``, so a session without it builds an
        authorize URL carrying no ``code_challenge`` at all — silently, with the
        verifier still sent on the exchange as though the flow were protected.
        """
        return OAuth2Session(
            client_id=authenticator.client_id,
            client_secret=authenticator.client_secret,
            scope=" ".join(authenticator.scopes),
            redirect_uri=redirect_uri,
            default_timeout=self._timeout,
            code_challenge_method="S256",
        )

    def authorization_url(
        self,
        authenticator: OidcAuthenticator,
        *,
        redirect_uri: str,
        state: str,
        nonce: str,
        code_verifier: str,
        now: datetime,
    ) -> str:
        """Return the provider authorize URL for a code+PKCE login.

        ``code_verifier`` becomes the S256 ``code_challenge`` on the query — but
        only because :meth:`_session` sets ``code_challenge_method``; see there
        for why passing the verifier alone is not enough. ``state`` (CSRF) and
        ``nonce`` (replay) ride the query and are echoed back for the callback
        to check.
        """
        discovery = self._discovery.get(authenticator.discovery_url, now=now)
        session = self._session(authenticator, redirect_uri=redirect_uri)
        uri, _state = session.create_authorization_url(
            discovery.authorization_endpoint,
            state=state,
            code_verifier=code_verifier,
            nonce=nonce,
        )
        return str(uri)

    def exchange_code(
        self,
        authenticator: OidcAuthenticator,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str,
        now: datetime,
    ) -> TokenBundle:
        """Exchange an authorization *code* (with the PKCE verifier) for tokens."""
        discovery = self._discovery.get(authenticator.discovery_url, now=now)
        session = self._session(authenticator, redirect_uri=redirect_uri)
        try:
            token = session.fetch_token(
                discovery.token_endpoint,
                code=code,
                code_verifier=code_verifier,
                redirect_uri=redirect_uri,
                grant_type="authorization_code",
            )
        except Exception as exc:  # noqa: BLE001 - authlib raises a wide family; normalize
            raise OidcExchangeError("authorization-code exchange failed") from exc
        raw = dict(token)
        return TokenBundle(
            id_token=raw.get("id_token"),
            access_token=raw.get("access_token"),
            token_type=str(raw.get("token_type") or "Bearer"),
            raw=raw,
        )

    def fetch_userinfo(
        self,
        authenticator: OidcAuthenticator,
        *,
        access_token: str,
        now: datetime,
    ) -> dict[str, Any]:
        """Fetch userinfo claims, or ``{}`` when the provider advertises no endpoint.

        Some providers omit ``groups``/``email`` from a lean ID token and only
        expose them here; the caller merges the two claim sets (guarding that the
        subjects match) before mapping to an identity.
        """
        discovery = self._discovery.get(authenticator.discovery_url, now=now)
        if not discovery.userinfo_endpoint:
            return {}
        session = OAuth2Session(
            token={"access_token": access_token, "token_type": "Bearer"},
            default_timeout=self._timeout,
        )
        try:
            response = session.get(discovery.userinfo_endpoint)
            response.raise_for_status()
            claims = response.json()
        except Exception as exc:  # noqa: BLE001 - normalize the transport error family
            raise OidcExchangeError("userinfo fetch failed") from exc
        return dict(claims) if isinstance(claims, dict) else {}

    def end_session_url(
        self,
        authenticator: OidcAuthenticator,
        *,
        id_token_hint: str | None,
        post_logout_redirect_uri: str | None,
        now: datetime,
    ) -> str | None:
        """Return the RP-initiated logout URL, or ``None`` when unsupported.

        Only built when the discovery document advertises an
        ``end_session_endpoint`` (RP-Initiated Logout); otherwise the caller just
        clears the local session cookie.
        """
        discovery = self._discovery.get(authenticator.discovery_url, now=now)
        endpoint = discovery.end_session_endpoint
        if not endpoint:
            return None
        params: dict[str, str] = {}
        if id_token_hint:
            params["id_token_hint"] = id_token_hint
        if post_logout_redirect_uri:
            params["post_logout_redirect_uri"] = post_logout_redirect_uri
        if not params:
            return endpoint
        from urllib.parse import urlencode

        separator = "&" if "?" in endpoint else "?"
        return f"{endpoint}{separator}{urlencode(params)}"


__all__ = ["OidcClient", "OidcExchangeError", "TokenBundle"]
