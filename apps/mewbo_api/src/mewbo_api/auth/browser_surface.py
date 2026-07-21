#!/usr/bin/env python3
"""``BrowserLoginSurface`` — the HTTP edge every browser login flow shares.

A browser login is not an OIDC concern or a SAML concern. Where this deployment
lives (its scheme + host, possibly behind a forwarding proxy), how a session
cookie is marked, how long each cookie lives, and where a failed login sends the
user are properties of the BROWSER SESSION — identical whichever protocol
minted it. They were duplicated verbatim across the OIDC and SAML route
controllers, which is two definitions of one policy: a change to the cookie's
``SameSite`` or to the proxy-header handling would have had to be made twice, and
the second copy is the one that gets missed.

This class owns them once, and both controllers hold it as an injected field.

**Why it is not on** :class:`~mewbo_api.auth.federated.FederatedRuntime`, which
already owns the cookie NAMES and the signers: that runtime is deliberately
Flask-free and clock-injected, so every one of its tests runs with no request
context. These methods read the live Flask ``request`` and mutate a ``Response``
— they are the I/O edge. Folding them in would put a request context in the
identity runtime's whole test surface to buy nothing. The split is the same one
``FederatedRuntime`` itself draws: it decides WHAT a cookie says, this decides
HOW it is transported.
"""

from __future__ import annotations

from flask import redirect, request
from mewbo_iam import AuthSettings
from werkzeug.wrappers import Response

from mewbo_api.auth.cookie import LOGIN_STATE_TTL


class BrowserLoginSurface:
    """Origin resolution, cookie transport, and the login-failure redirect.

    Atomic class: the deployment's :class:`~mewbo_iam.AuthSettings` is its DI'd
    state (the session cookie's lifetime comes from ``session.ttl_seconds``), and
    every method is behavior over the in-flight request. Holds no per-request
    state, so one instance serves every request.
    """

    def __init__(self, *, settings: AuthSettings) -> None:
        """Capture the settings the cookie lifetime is read from."""
        self._settings = settings

    # ── origin ───────────────────────────────────────────────────────────────
    def origin(self) -> str:
        """This deployment's scheme+host origin, honoring a forwarding proxy."""
        proto = request.headers.get("X-Forwarded-Proto")
        host = request.headers.get("X-Forwarded-Host") or request.host
        if proto:
            return f"{proto}://{host}"
        return request.url_root.rstrip("/")

    def absolute_url(self, path: str) -> str:
        """*path* resolved against this deployment's origin.

        Used for the redirect/ACS URIs, which must match BOTH the identity
        provider's registration and the value sent in the request itself — so
        both legs of a handshake derive them from this one method.
        """
        return f"{self.origin()}{path}"

    # ── cookie transport ─────────────────────────────────────────────────────
    def cookie_secure(self) -> bool:
        """Whether to mark cookies ``Secure`` (https, direct or via a proxy)."""
        return request.is_secure or request.headers.get("X-Forwarded-Proto") == "https"

    def set_session_cookie(self, response: Response, name: str, value: str) -> None:
        """Set the signed session cookie, with the configured session lifetime."""
        self._set(response, name, value, max_age=self._settings.session.ttl_seconds)

    def set_login_state_cookie(self, response: Response, name: str, value: str) -> None:
        """Set the short-lived login-state cookie that bridges the handshake legs."""
        self._set(response, name, value, max_age=int(LOGIN_STATE_TTL.total_seconds()))

    def clear_cookie(self, response: Response, name: str) -> None:
        """Expire a cookie immediately.

        The flags are repeated on the clear because a browser matches a
        deletion to an existing cookie by name, path and domain — clearing with
        a different ``path`` leaves the original cookie in place.
        """
        self._set(response, name, "", max_age=0, expires=0)

    def _set(
        self,
        response: Response,
        name: str,
        value: str,
        *,
        max_age: int,
        expires: int | None = None,
    ) -> None:
        """The ONE cookie policy: httpOnly, SameSite=Lax, Secure under https."""
        response.set_cookie(
            name,
            value,
            max_age=max_age,
            expires=expires,
            httponly=True,
            samesite="Lax",
            secure=self.cookie_secure(),
            path="/",
        )

    # ── failure redirect ─────────────────────────────────────────────────────
    @staticmethod
    def error_redirect(slug: str) -> Response:
        """Redirect the browser to the console root with an ``auth_error`` flag.

        A failed login must land the user on a page that can explain itself, not
        on a JSON body — so every browser-login failure leaves through here, and
        the console reads the one ``auth_error`` query flag.
        """
        return redirect(f"/?auth_error={slug}")


__all__ = ["BrowserLoginSurface"]
