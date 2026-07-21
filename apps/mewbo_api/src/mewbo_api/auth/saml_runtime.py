#!/usr/bin/env python3
"""``SamlRuntime`` — the SAML-specific engine layered over the shared federated runtime.

JIT provisioning, the group mappings and the session cookie are NOT properties
of SAML; they are properties of "an external directory asserted this
identity", and already live on :class:`~mewbo_api.auth.federated.FederatedRuntime`
(the same seam :class:`~mewbo_api.auth.oidc_runtime.OidcRuntime` and
``LdapLoginService`` compose). This module adds only what IS specific to SAML:
one :class:`~mewbo_iam.drivers.saml.SamlSp` per configured authenticator (each
caching its own IdP metadata), and the SP-initiated redirect handshake —
mint an ``AuthnRequest``, bind its ID + an opaque RelayState into a signed
login-state cookie (mirroring the OIDC ``state``/``nonce`` cookie), and bind
them back on the ACS POST before ever handing the assertion to the SP driver.

Import discipline: the heavy ``mewbo_iam.drivers`` (the ``[saml]`` extra) is
imported LAZILY inside :func:`build_saml_runtime`, never at module top — this
module is imported whenever the AuthKit is, including on an auth-disabled
boot with no extra installed.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from mewbo_core.common import get_logger
from mewbo_iam import AuthMethod, AuthSettings, Principal
from mewbo_iam.authenticators import SamlAuthenticator

from mewbo_api.auth.cookie import LOGIN_STATE_TTL
from mewbo_api.auth.federated import FederatedRuntime

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mewbo_iam.drivers.saml import SamlSp

logging = get_logger(name="mewbo-api.auth.saml")

# The login-state payload discriminator (domain separation on the ONE cookie
# secret, and distinct from OIDC's "state" typ so a tampered-but-still-valid
# cookie of one kind can never be replayed as the other's).
_STATE_TYP = "saml_state"

# The auth-method kind every principal this runtime resolves carries.
_SAML_METHOD = "saml"


class SamlCallbackError(Exception):
    """A recoverable failure completing the ACS leg.

    Bad state, RelayState mismatch, an unknown authenticator, or an
    assertion that failed validation. Never carries the assertion or its
    attributes.
    """


class SamlResponseMissingError(SamlCallbackError):
    """The ACS POST carried no ``SAMLResponse`` at all.

    Distinct from every other :class:`SamlCallbackError` because it is the
    one case analogous to OIDC's provider-side ``?error=`` — nothing to
    validate arrived, as opposed to something arriving and failing
    validation — so the route maps it to a different ``auth_error`` slug.
    """


@dataclass(frozen=True)
class SamlLoginStart:
    """The IdP redirect URL plus the signed login-state cookie value to set."""

    redirect_url: str
    state_cookie: str


@dataclass(frozen=True)
class SamlCallbackResult:
    """A completed ACS leg: the provisioned principal + where to send them next."""

    principal: Principal
    return_to: str


class SamlRuntime:
    """The stateful SAML engine: per-authenticator SP drivers over a shared federated runtime.

    Atomic class — every collaborator is a DI'd field and the clock is
    injected. Holds no per-request state (the login handshake rides a signed
    cookie), so one instance safely serves every request and worker thread.
    Provisioning and the session cookie are delegated to the composed
    ``FederatedRuntime``, so the SAML and non-SAML login paths share ONE
    identity flow and ONE cookie.
    """

    def __init__(
        self,
        *,
        settings: AuthSettings,
        authenticators: tuple[SamlAuthenticator, ...],
        sps: dict[str, SamlSp],
        federated: FederatedRuntime,
        clock: Callable[[], datetime],
    ) -> None:
        """Capture the per-authenticator SP drivers, the shared federated runtime, and the clock."""
        self._settings = settings
        self._authenticators = authenticators
        self._sps = sps
        self._federated = federated
        self._clock = clock

    # ── config accessors (delegated — one cookie, one policy) ────────────────
    @property
    def federated(self) -> FederatedRuntime:
        """The shared federated runtime this SAML engine provisions through."""
        return self._federated

    @property
    def session_cookie_name(self) -> str:
        """The configured session cookie name."""
        return self._federated.session_cookie_name

    @property
    def state_cookie_name(self) -> str:
        """The SAML login-state cookie name.

        Distinct from OIDC's, so a deployment running both kinds never
        collides an in-flight handshake.
        """
        return f"{self._federated.session_cookie_name}_saml_login"

    def authenticator_by_name(self, name: object) -> SamlAuthenticator | None:
        """The enabled SAML authenticator with this ``name``, or ``None``."""
        return next((a for a in self._authenticators if a.name == name), None)

    def default_authenticator(self) -> SamlAuthenticator | None:
        """The first enabled SAML authenticator — the ``/login`` default target."""
        return self._authenticators[0] if self._authenticators else None

    # ── SP metadata ────────────────────────────────────────────────────────────
    def metadata_xml(self, authenticator: SamlAuthenticator, *, acs_url: str) -> str:
        """The SP metadata XML for *authenticator*, for the IdP admin to import."""
        return self._sps[authenticator.name].metadata_xml(acs_url=acs_url)

    # ── interactive login legs ───────────────────────────────────────────────
    def begin_login(
        self,
        authenticator: SamlAuthenticator,
        *,
        acs_url: str,
        return_to: str,
        now: datetime | None = None,
    ) -> SamlLoginStart:
        """Mint the AuthnRequest, bind its ID via RelayState, and sign the state cookie.

        The RelayState carries only an opaque, unguessable id — never the
        real return path — because RelayState is echoed back by the IdP on
        an untrusted channel (the browser); the trustworthy return-to lives
        only in the signed cookie, and the two are bound by comparing this id
        against what the cookie recorded.
        """
        moment = now if now is not None else self._clock()
        relay_state = secrets.token_urlsafe(16)
        sp = self._sps[authenticator.name]
        start = sp.begin_login(acs_url=acs_url, relay_state=relay_state, now=moment)
        state_cookie = self._federated.state_signer.sign(
            {
                "typ": _STATE_TYP,
                "rid": start.request_id,
                "rs": relay_state,
                "rt": self._federated.safe_return_to(return_to),
                "an": authenticator.name,
            },
            now=moment,
            ttl=LOGIN_STATE_TTL,
        )
        return SamlLoginStart(redirect_url=start.redirect_url, state_cookie=state_cookie)

    def complete_acs(
        self,
        *,
        state_cookie: str | None,
        saml_response_b64: str | None,
        relay_state: str | None,
        acs_url: str,
        now: datetime | None = None,
    ) -> SamlCallbackResult:
        """Validate the ACS POST, provision, and report where to redirect next."""
        moment = now if now is not None else self._clock()
        if not saml_response_b64:
            raise SamlResponseMissingError("SAMLResponse missing from the ACS POST")
        payload = self._federated.state_signer.verify(state_cookie or "", now=moment)
        if payload is None or payload.get("typ") != _STATE_TYP:
            raise SamlCallbackError("login state missing or expired")
        if not hmac.compare_digest(str(payload.get("rs", "")), str(relay_state or "")):
            raise SamlCallbackError("RelayState mismatch")
        authenticator = self.authenticator_by_name(payload.get("an"))
        if authenticator is None:
            raise SamlCallbackError("login state names an unknown authenticator")

        from mewbo_iam.drivers import SamlValidationError

        sp = self._sps[authenticator.name]
        try:
            raw = sp.validate_acs(
                saml_response_b64=saml_response_b64,
                acs_url=acs_url,
                request_id=str(payload.get("rid") or "") or None,
                now=moment,
            )
        except SamlValidationError as exc:
            raise SamlCallbackError(str(exc)) from exc

        principal = self._federated.provision(
            raw, auth_method=AuthMethod(kind=_SAML_METHOD, issuer=authenticator.issuer), now=moment
        )
        return SamlCallbackResult(
            principal=principal,
            return_to=self._federated.safe_return_to(payload.get("rt")),
        )

    # ── session cookie (delegated) ────────────────────────────────────────────
    def session_cookie(self, principal: Principal, *, now: datetime | None = None) -> str:
        """Mint the signed session cookie value for a just-authenticated principal."""
        return self._federated.session_cookie(principal, now=now, method=_SAML_METHOD)


def build_saml_runtime(
    settings: AuthSettings,
    *,
    clock: Callable[[], datetime],
    federated: FederatedRuntime | None,
) -> SamlRuntime | None:
    """Build the shared SAML runtime, or ``None`` when no SAML authenticator is enabled.

    Lazily imports the ``[saml]`` driver (the AuthKit's boot dependency check
    has already guaranteed it is installed when a SAML authenticator is
    enabled). *federated* is the ONE shared runtime the AuthKit already
    built — passed in rather than constructed here so SAML, OIDC,
    trusted-header and LDAP all provision through one identity flow and one
    user store, never a private copy.
    """
    saml_authenticators = tuple(
        a for a in settings.authenticators if isinstance(a, SamlAuthenticator) and a.enabled
    )
    if not settings.enabled or not saml_authenticators or federated is None:
        return None

    from mewbo_iam.drivers import SamlSp

    return SamlRuntime(
        settings=settings,
        authenticators=saml_authenticators,
        sps={a.name: SamlSp(a) for a in saml_authenticators},
        federated=federated,
        clock=clock,
    )


__all__ = [
    "SamlRuntime",
    "build_saml_runtime",
    "SamlCallbackError",
    "SamlResponseMissingError",
    "SamlLoginStart",
    "SamlCallbackResult",
]
