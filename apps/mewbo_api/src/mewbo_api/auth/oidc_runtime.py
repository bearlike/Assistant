#!/usr/bin/env python3
"""``OidcRuntime`` — the shared OIDC engine behind both request resolution and routes.

The relying-party drivers (discovery/JWKS/client/introspection) are stateful — the
JWKS cache in particular MUST persist across requests. So they live on ONE
long-lived object, built once at boot and shared by two consumers:

* the :class:`~mewbo_api.auth.AuthKit`, whose ``resolve`` turns a bearer JWT or a
  session cookie on an incoming request into a ``Principal``; and
* the auth routes, which drive the interactive login/callback/logout legs.

Sharing one runtime means one JWKS cache, one discovery cache — not two drifting
copies. It is app-side glue (composes the library drivers with the app's stores);
nothing here is reusable across apps, so it stays out of the kernel.

What is NOT here: JIT provisioning, the group mappings and the session cookie.
Those belong to every external-identity kind, not to OIDC, so they live on the
:class:`~mewbo_api.auth.federated.FederatedRuntime` this runtime COMPOSES — see
that module for why. This class adds only the OIDC-specific half: the discovery/
JWKS caches and the authorization-code handshake.

Import discipline: the heavy ``mewbo_iam.drivers`` (the ``[oidc]`` extra) is
imported LAZILY inside :func:`build_oidc_runtime`, never at module top — this
module is imported whenever the AuthKit is, including on an auth-disabled boot with
no extra installed, and must not drag the drivers in then.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from mewbo_core.common import get_logger
from mewbo_iam import (
    AuthMethod,
    AuthSettings,
    AvatarPolicy,
    Principal,
)
from mewbo_iam.authenticators import OidcAuthenticator

from mewbo_api.auth.cookie import LOGIN_STATE_TTL
from mewbo_api.auth.federated import FederatedRuntime
from mewbo_api.auth.identity_flow import DisabledUserError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mewbo_iam.drivers import (
        DiscoveryCache,
        IntrospectionClient,
        JwksVerifier,
        OidcClient,
    )

logging = get_logger(name="mewbo-api.auth.oidc")

# The login-state payload discriminator (domain separation on the ONE cookie
# secret — the session counterpart lives with the cookie itself, on the
# federated runtime).
_STATE_TYP = "state"

# The auth-method kind every principal this runtime resolves carries.
_OIDC_METHOD = "oidc"


class CallbackError(Exception):
    """A recoverable failure during the OIDC callback (bad state, no id_token, …).

    Distinct from a groups-overage (which the drivers raise as
    ``GroupsOverageError``) so the route can render the operator-actionable
    message for that case specifically.
    """


@dataclass(frozen=True)
class LoginStart:
    """The redirect URL plus the signed login-state cookie value to set."""

    authorization_url: str
    state_cookie: str


@dataclass(frozen=True)
class CallbackResult:
    """A completed callback: the provisioned principal + where to send them next."""

    principal: Principal
    return_to: str
    id_token: str | None


def _looks_like_jwt(token: str) -> bool:
    """Whether *token* is shaped like a compact JWS (three non-empty segments)."""
    segments = token.split(".")
    return len(segments) == 3 and all(segments)


class OidcRuntime:
    """The stateful OIDC engine: the RP drivers over a shared federated runtime.

    Atomic class — every collaborator is a DI'd field and the clock is injected.
    Holds no per-request state (the login handshake state rides a signed cookie),
    so one instance safely serves every request and worker thread. Provisioning
    and the session cookie are delegated to the composed ``FederatedRuntime``, so
    the OIDC and non-OIDC login paths share ONE identity flow and ONE cookie.
    """

    def __init__(
        self,
        *,
        settings: AuthSettings,
        authenticators: tuple[OidcAuthenticator, ...],
        discovery: DiscoveryCache,
        jwks: JwksVerifier,
        client: OidcClient,
        introspection: IntrospectionClient,
        federated: FederatedRuntime,
        clock: Callable[[], datetime],
    ) -> None:
        """Capture the RP drivers, the shared federated runtime, and the clock."""
        self._settings = settings
        self._authenticators = authenticators
        self._discovery = discovery
        self._jwks = jwks
        self._client = client
        self._introspection = introspection
        self._federated = federated
        self._clock = clock

    # ── config accessors (delegated — one cookie, one policy) ────────────────
    @property
    def federated(self) -> FederatedRuntime:
        """The shared federated runtime this OIDC engine provisions through."""
        return self._federated

    @property
    def session_cookie_name(self) -> str:
        """The configured session cookie name."""
        return self._federated.session_cookie_name

    @property
    def state_cookie_name(self) -> str:
        """The short-lived login-state cookie name (derived, never collides)."""
        return self._federated.state_cookie_name

    @property
    def avatar_policy(self) -> AvatarPolicy:
        """The deployment's avatar policy (for the ``/me`` avatar chain)."""
        return self._federated.avatar_policy

    def authenticator_by_name(self, name: object) -> OidcAuthenticator | None:
        """The enabled OIDC authenticator with this ``name``, or ``None``."""
        return next((a for a in self._authenticators if a.name == name), None)

    def default_authenticator(self) -> OidcAuthenticator | None:
        """The first enabled OIDC authenticator — the ``/login`` default target."""
        return self._authenticators[0] if self._authenticators else None

    # ── interactive login legs ───────────────────────────────────────────────
    def begin_login(
        self,
        authenticator: OidcAuthenticator,
        *,
        redirect_uri: str,
        return_to: str,
        now: datetime | None = None,
    ) -> LoginStart:
        """Mint PKCE/state/nonce, build the authorize URL, and sign the state cookie."""
        moment = now if now is not None else self._clock()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)  # 43-128 unreserved chars → valid PKCE
        url = self._client.authorization_url(
            authenticator,
            redirect_uri=redirect_uri,
            state=state,
            nonce=nonce,
            code_verifier=code_verifier,
            now=moment,
        )
        state_cookie = self._federated.state_signer.sign(
            {
                "typ": _STATE_TYP,
                "st": state,
                "no": nonce,
                "cv": code_verifier,
                "rt": self._federated.safe_return_to(return_to),
                "an": authenticator.name,
            },
            now=moment,
            ttl=LOGIN_STATE_TTL,
        )
        return LoginStart(authorization_url=url, state_cookie=state_cookie)

    def complete_callback(
        self,
        *,
        state_cookie: str | None,
        state_param: str | None,
        code: str | None,
        redirect_uri: str,
        now: datetime | None = None,
    ) -> CallbackResult:
        """Validate the callback, exchange the code, verify the ID token, provision."""
        moment = now if now is not None else self._clock()
        if not code:
            raise CallbackError("authorization code missing")
        payload = self._federated.state_signer.verify(state_cookie or "", now=moment)
        if payload is None or payload.get("typ") != _STATE_TYP:
            raise CallbackError("login state missing or expired")
        if not hmac.compare_digest(str(payload.get("st", "")), str(state_param or "")):
            raise CallbackError("state parameter mismatch")
        authenticator = self.authenticator_by_name(payload.get("an"))
        if authenticator is None:
            raise CallbackError("login state names an unknown authenticator")

        bundle = self._client.exchange_code(
            authenticator,
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=str(payload.get("cv", "")),
            now=moment,
        )
        if not bundle.id_token:
            raise CallbackError("token response carried no id_token")
        claims = self._jwks.verify(
            bundle.id_token,
            authenticator,
            now=moment,
            audience=authenticator.client_id,
            nonce=str(payload.get("no", "")) or None,
        )
        claims = self._merge_userinfo(authenticator, bundle, claims, moment)
        raw = authenticator.resolve(claims)
        if raw is None:
            raise CallbackError("verified token carried no identity claim")
        principal = self._federated.provision(
            raw,
            auth_method=AuthMethod(kind=_OIDC_METHOD, issuer=authenticator.issuer),
            now=moment,
        )
        return CallbackResult(
            principal=principal,
            return_to=self._federated.safe_return_to(payload.get("rt")),
            id_token=bundle.id_token,
        )

    def _merge_userinfo(
        self,
        authenticator: OidcAuthenticator,
        bundle: object,
        claims: dict,
        now: datetime,
    ) -> dict:
        """Augment ID-token claims with userinfo (when advertised), subjects-must-match.

        A lean ID token may omit ``groups``/``email``; userinfo fills them. The
        userinfo ``sub`` MUST equal the ID token's (an unrelated userinfo response
        is ignored) so a merge can never graft another user's claims on.
        """
        access_token = getattr(bundle, "access_token", None)
        if not access_token:
            return claims
        try:
            userinfo = self._client.fetch_userinfo(
                authenticator, access_token=access_token, now=now
            )
        except Exception:  # noqa: BLE001 - userinfo is best-effort enrichment
            logging.warning("userinfo fetch failed; using id_token claims only", exc_info=True)
            return claims
        if not userinfo:
            return claims
        if userinfo.get("sub") not in (None, claims.get("sub")):
            logging.warning("userinfo subject mismatch; ignoring userinfo response")
            return claims
        return {**claims, **userinfo}

    def end_session_url(
        self,
        *,
        id_token_hint: str | None,
        post_logout_redirect_uri: str | None,
        now: datetime | None = None,
    ) -> str | None:
        """RP-initiated logout URL for the default authenticator, or ``None``."""
        authenticator = self.default_authenticator()
        if authenticator is None:
            return None
        moment = now if now is not None else self._clock()
        return self._client.end_session_url(
            authenticator,
            id_token_hint=id_token_hint,
            post_logout_redirect_uri=post_logout_redirect_uri,
            now=moment,
        )

    # ── session cookie (delegated) + bearer resolution ───────────────────────
    def session_cookie(self, principal: Principal, *, now: datetime | None = None) -> str:
        """Mint the signed session cookie value for a just-authenticated principal."""
        return self._federated.session_cookie(principal, now=now, method=_OIDC_METHOD)

    def resolve_session_cookie(
        self, cookie_value: str, *, now: datetime | None = None
    ) -> Principal | None:
        """Resolve a session cookie to its principal, or ``None``.

        Kept as a delegating alias so the OIDC-era call sites keep working; the
        cookie itself is kind-agnostic and belongs to the federated runtime, which
        the AuthKit reads directly.
        """
        return self._federated.resolve_session_cookie(cookie_value, now=now)

    def resolve_bearer(self, token: str, *, now: datetime | None = None) -> Principal | None:
        """Resolve a bearer token (JWT, else opaque-via-introspection) to a principal.

        A well-formed JWT is verified against each enabled OIDC authenticator's
        JWKS; the first that verifies wins. A non-JWT (opaque) token is honored
        only for an authenticator that opts into RFC 7662 introspection. Any
        failure yields ``None`` — resolution never rejects; the guards do.
        """
        from mewbo_iam.drivers import GroupsOverageError, JwksVerificationError

        moment = now if now is not None else self._clock()
        is_jwt = _looks_like_jwt(token)
        for authenticator in self._authenticators:
            try:
                if is_jwt:
                    claims = self._jwks.verify(token, authenticator, now=moment)
                elif self._introspection_enabled(authenticator):
                    result = self._introspection.introspect(token, authenticator, now=moment)
                    if not result.active:
                        continue
                    claims = result.claims
                else:
                    continue
                principal = self._provision_bearer(authenticator, claims, moment)
            except GroupsOverageError:
                logging.warning(
                    "bearer JWT rejected: identity provider returned a groups overage "
                    "(configure inline group/app-role claims)"
                )
                return None
            except JwksVerificationError:
                continue
            except DisabledUserError:
                return None
            if principal is not None:
                return principal
        return None

    def _provision_bearer(
        self, authenticator: OidcAuthenticator, claims: dict, now: datetime
    ) -> Principal | None:
        """Map verified bearer claims to a JIT-provisioned principal, or ``None``."""
        raw = authenticator.resolve(claims)
        if raw is None:
            return None
        return self._federated.provision(
            raw,
            auth_method=AuthMethod(kind=_OIDC_METHOD, issuer=authenticator.issuer),
            now=now,
        )

    @staticmethod
    def _introspection_enabled(authenticator: OidcAuthenticator) -> bool:
        """Whether this authenticator opts into opaque-token introspection."""
        return authenticator.introspection


def build_oidc_runtime(
    settings: AuthSettings,
    *,
    clock: Callable[[], datetime],
    federated: FederatedRuntime | None,
) -> OidcRuntime | None:
    """Build the shared OIDC runtime, or ``None`` when no OIDC authenticator is enabled.

    Lazily imports the ``[oidc]`` drivers (the AuthKit's boot dependency check has
    already guaranteed they are installed when an OIDC authenticator is enabled).
    *federated* is the ONE shared runtime the AuthKit already built — it is passed
    in rather than constructed here so the OIDC, trusted-header and LDAP paths
    provision through one identity flow and one user store, never a private copy.
    """
    oidc_authenticators = tuple(
        a for a in settings.authenticators if isinstance(a, OidcAuthenticator) and a.enabled
    )
    if not settings.enabled or not oidc_authenticators or federated is None:
        return None

    from mewbo_iam.drivers import (
        DiscoveryCache,
        IntrospectionClient,
        JwksVerifier,
        OidcClient,
    )

    discovery = DiscoveryCache()
    return OidcRuntime(
        settings=settings,
        authenticators=oidc_authenticators,
        discovery=discovery,
        jwks=JwksVerifier(discovery=discovery),
        client=OidcClient(discovery=discovery),
        introspection=IntrospectionClient(discovery=discovery),
        federated=federated,
        clock=clock,
    )


__all__ = [
    "OidcRuntime",
    "build_oidc_runtime",
    "CallbackError",
    "CallbackResult",
    "LoginStart",
]
