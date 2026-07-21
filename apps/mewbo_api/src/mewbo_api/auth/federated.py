#!/usr/bin/env python3
"""``FederatedRuntime`` — what every non-api-key authenticator shares.

JIT provisioning, the group→role/team mappings, the bootstrap-admin rule and the
signed session cookie are NOT properties of OpenID Connect; they are properties
of "an external directory asserted this identity". OIDC was simply the first kind
to need them. This module owns that common half so a deployment configured with
ONLY a trusted-header proxy, or ONLY an LDAP directory, gets provisioning and
browser sessions without an OIDC provider anywhere in its config.

The split:

* :class:`FederatedRuntime` — kind-agnostic. Owns the :class:`IdentityFlow`, the
  user store and both cookie signers, and exposes the three operations every kind
  performs: ``provision`` a verified identity, ``session_cookie`` to mint a
  browser session, and ``resolve_session_cookie`` to read one back.
* the per-kind runtimes — :class:`~mewbo_api.auth.oidc_runtime.OidcRuntime` and
  friends — COMPOSE one of these and add only what is specific to their protocol
  (the OIDC discovery/JWKS caches and the redirect handshake; the LDAP bind).

The session cookie records WHICH kind minted it (``am``), so a cookie issued by an
LDAP password login resolves back to an ``ldap`` auth method rather than being
mislabelled as the first kind that happened to be wired.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from mewbo_core.common import get_logger
from mewbo_iam import (
    AuthAuditStoreBase,
    AuthMethod,
    AuthMethodKind,
    AuthSettings,
    AvatarPolicy,
    LoginSuccessEvent,
    Principal,
    RawIdentity,
    TeamStoreBase,
    UserRecord,
    UserStoreBase,
    create_team_store,
    create_user_store,
)

from mewbo_api.auth.cookie import LOGIN_STATE_TTL, CookieSigner
from mewbo_api.auth.identity_flow import IdentityFlow

logging = get_logger(name="mewbo-api.auth.federated")

# Payload discriminator for the session cookie (domain separation on the ONE
# cookie secret — a login-state blob must never replay as a logged-in session).
SESSION_TYP = "session"

# A session cookie minted before the auth-method stamp existed resolves as OIDC,
# the only kind that could have issued one.
_DEFAULT_COOKIE_METHOD = "oidc"


class FederatedRuntime:
    """Provisioning + browser sessions for any external-identity authenticator.

    Atomic class: the JIT flow, the user store, both cookie signers and the clock
    are DI'd fields. Holds no per-request state (the login handshake rides a
    signed cookie), so one instance serves every request and worker thread.
    """

    def __init__(
        self,
        *,
        settings: AuthSettings,
        identity_flow: IdentityFlow,
        user_store: UserStoreBase,
        team_store: TeamStoreBase,
        session_signer: CookieSigner,
        state_signer: CookieSigner,
        clock: Callable[[], datetime],
        audit: Callable[[], AuthAuditStoreBase | None] = lambda: None,
    ) -> None:
        """Capture the JIT flow, the stores, the cookie signers and the clock.

        ``audit`` is a PROVIDER rather than a store, for the same reason the
        kit's key store is: the audit store is built lazily on first use, so a
        deployment with auditing off never constructs one. Defaulting it to
        "no store" keeps every existing construction site valid and inert.
        """
        self._settings = settings
        self._identity_flow = identity_flow
        self._users = user_store
        self._teams = team_store
        self._audit = audit
        self._session_signer = session_signer
        self._state_signer = state_signer
        self._clock = clock

    # ── config accessors ─────────────────────────────────────────────────────
    @property
    def session_cookie_name(self) -> str:
        """The configured session cookie name."""
        return self._settings.session.cookie_name

    @property
    def state_cookie_name(self) -> str:
        """The short-lived login-state cookie name (derived, never collides)."""
        return f"{self._settings.session.cookie_name}_login"

    @property
    def avatar_policy(self) -> AvatarPolicy:
        """The deployment's avatar policy (for the ``/me`` avatar chain)."""
        return self._settings.avatars

    @property
    def state_signer(self) -> CookieSigner:
        """The login-state signer, for a kind that runs a redirect handshake."""
        return self._state_signer

    @property
    def user_store(self) -> UserStoreBase:
        """The durable user store this runtime provisions into."""
        return self._users

    # ── post-login redirect safety ───────────────────────────────────────────
    @staticmethod
    def safe_return_to(value: object) -> str:
        """Constrain a post-login redirect to a same-origin relative path.

        Open-redirect defense: only a path beginning with a single ``/`` (not
        ``//``, carrying no scheme and no backslash) is honored; anything else
        falls back to the console root. An attacker-supplied ``return_to`` can
        therefore never bounce the user to another origin after login.

        It lives HERE, on the runtime every browser-login kind already composes,
        because it is a security control and a second copy is a copy that misses
        the next fix. The OIDC and SAML handshakes each fed it an untrusted
        ``return_to`` from their own module; they now share this one.
        """
        if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
            return "/"
        if "\\" in value or "://" in value:
            return "/"
        return value

    # ── provisioning ─────────────────────────────────────────────────────────
    def provision(
        self, raw: RawIdentity, *, auth_method: AuthMethod, now: datetime | None = None
    ) -> Principal:
        """JIT-provision a verified identity into a principal.

        Raises :class:`~mewbo_api.auth.identity_flow.DisabledUserError` for a
        deliberately disabled account — the caller must NOT swallow it into a
        generic failure, or a disabled user's next login silently re-activates them.
        """
        moment = now if now is not None else self._clock()
        return self._identity_flow.provision(raw, auth_method=auth_method, now=moment)

    # ── session cookie mint + resolution ─────────────────────────────────────
    def session_cookie(
        self,
        principal: Principal,
        *,
        now: datetime | None = None,
        method: AuthMethodKind | None = None,
    ) -> str:
        """Mint the signed session cookie for a just-authenticated principal.

        The cookie carries only the subject and the auth-method kind — never roles
        or profile, which are read live from the store on each request so a role
        change or a disable takes effect immediately rather than at cookie expiry.

        **This is also where ``login_success`` is audited, and the placement is
        the whole reason the trail is trustworthy.** Minting a cookie is the one
        event that happens exactly once per browser login, and every kind that
        performs one — OIDC, SAML, LDAP — funnels through here. The obvious
        alternative, recording a success beside the failures in ``AuthKit``,
        looks symmetric and is wrong: ``AuthKit.resolve`` runs on EVERY request,
        so it would append an event per API call and per page load, burying the
        logins it exists to record. A failure has no such seam — there is no
        session to mint — which is why the two are not mirror images.
        """
        moment = now if now is not None else self._clock()
        kind = method if method is not None else principal.auth_method.kind
        self._record_login_success(principal, method=kind, now=moment)
        return self._session_signer.sign(
            {"typ": SESSION_TYP, "sub": principal.subject, "am": kind}, now=moment
        )

    def _record_login_success(
        self, principal: Principal, *, method: AuthMethodKind, now: datetime
    ) -> None:
        """Append a ``login_success`` audit event — best-effort, never raises.

        Mirrors ``AuthKit._record_login_failure``: a ``None`` store (auth or
        auditing off) short-circuits, and a write failure is logged rather than
        raised. An audit outage must never cost a user their login — failing the
        request here would turn a degraded trail into an outage.
        """
        store = self._audit()
        if store is None:
            return
        try:
            store.append(
                LoginSuccessEvent(
                    ts=now,
                    source="api",
                    actor_subject=principal.subject,
                    subject=principal.subject,
                    method=method,
                    issuer=principal.auth_method.issuer,
                )
            )
        except Exception:  # noqa: BLE001 - audit is best-effort, never breaks the login
            logging.warning("auth audit (login_success) write failed", exc_info=True)

    def resolve_session_cookie(
        self, cookie_value: str, *, now: datetime | None = None
    ) -> Principal | None:
        """Resolve a session cookie to its principal, or ``None``.

        ``None`` covers every failure — forged, malformed, expired, unknown
        subject, disabled account — with no oracle about which.
        """
        moment = now if now is not None else self._clock()
        payload = self._session_signer.verify(cookie_value, now=moment)
        if payload is None or payload.get("typ") != SESSION_TYP:
            return None
        subject = payload.get("sub")
        if not isinstance(subject, str):
            return None
        record = self._users.get(subject)
        if record is None or record.status == "disabled":
            return None
        raw_method = payload.get("am")
        method = raw_method if isinstance(raw_method, str) else _DEFAULT_COOKIE_METHOD
        return self.principal_from_record(record, method=method)

    def principal_from_record(self, record: UserRecord, *, method: str) -> Principal:
        """Build a principal from a stored user record.

        The issuer comes from the record's first linked external identity rather
        than from the cookie, so it tracks the directory the user is actually
        linked to. An auth-method kind the kernel does not know (a cookie minted
        by a newer build, or a tampered one that still verifies) degrades to
        ``system`` rather than failing the request.

        Team memberships are read live from the store on every call, for the
        same reason roles and profile are: the cookie carries only a subject, so
        a membership change takes effect on the next request rather than at
        cookie expiry.

        **Only DURABLE memberships reach this path, and that is a real
        asymmetry with a fresh login rather than an oversight.** The group→team
        mapping needs the IdP's asserted groups, which exist only during the
        login handshake — a cookie carries none, and putting them in one would
        pin them until expiry, defeating the live-read rule above. Persisting
        them instead is worse: a stored edge has no provenance field, so a
        mapping-derived membership could never be revoked when the IdP drops
        the group. Config-mapped teams are therefore login-scoped by
        construction; a deployment that needs them to outlive the handshake
        should grant the membership durably (SCIM or the admin surface).
        """
        external = record.external_identities[0] if record.external_identities else None
        issuer = external.issuer if external is not None else None
        principal = Principal(
            subject=record.id,
            kind="user",
            display_name=record.display_name,
            email=record.email,
            email_verified=record.email_verified,
            picture_url=record.picture_url,
            roles=record.roles,
            scopes=None,
            auth_method=AuthMethod(kind=_coerce_method(method), issuer=issuer),
            external_subject=external,
        )
        return principal.with_team_memberships(self._teams.list_for_user(record.id))


def _coerce_method(value: str) -> AuthMethodKind:
    """Narrow a cookie-carried method string to a known kind, else ``system``."""
    known: tuple[AuthMethodKind, ...] = (
        "api_key",
        "oidc",
        "trusted_header",
        "ldap",
        "saml",
        "password",
        "system",
    )
    return value if value in known else "system"


def build_federated_runtime(
    settings: AuthSettings,
    *,
    clock: Callable[[], datetime],
    user_store: UserStoreBase | None = None,
    team_store: TeamStoreBase | None = None,
    audit: Callable[[], AuthAuditStoreBase | None] = lambda: None,
) -> FederatedRuntime | None:
    """Build the shared federated runtime, or ``None`` when no kind needs one.

    ``None`` whenever auth is off or the only configured authenticator is the
    local api-key kind — that deployment provisions nobody and mints no cookies,
    so it must not create a user store or write an ``iam_users.json``. The same
    byte-zero rule covers the team store, which is why both are constructed only
    past that guard. The session secret is guaranteed present by
    ``AuthSettings``' federated-secret validator, so signing is never keyless.
    """
    federated = tuple(
        a for a in settings.authenticators if a.kind != "api_key" and a.enabled
    )
    if not settings.enabled or not federated:
        return None
    store = user_store if user_store is not None else create_user_store()
    teams = team_store if team_store is not None else create_team_store()
    secret = settings.session.secret or ""
    return FederatedRuntime(
        settings=settings,
        identity_flow=IdentityFlow(
            user_store=store,
            team_store=teams,
            role_mapping=settings.role_mappings,
            team_mapping=settings.team_mappings,
            bootstrap=settings.bootstrap,
            avatar_policy=settings.avatars,
            clock=clock,
        ),
        user_store=store,
        team_store=teams,
        audit=audit,
        session_signer=CookieSigner(
            secret=secret, ttl=timedelta(seconds=settings.session.ttl_seconds)
        ),
        state_signer=CookieSigner(secret=secret, ttl=LOGIN_STATE_TTL),
        clock=clock,
    )


__all__ = ["FederatedRuntime", "build_federated_runtime", "SESSION_TYP"]
