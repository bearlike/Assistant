#!/usr/bin/env python3
"""``AuthKit`` — the one place every REST request resolves to a ``Principal``.

The kit is the app-side bridge between the identity kernel (``mewbo_iam``) and
the Flask request cycle. It does three jobs:

1. **Resolve** a request to a :class:`~mewbo_iam.Principal` (``resolve``), stored
   on ``flask.g.principal`` by one ``before_request`` mount. This NEVER rejects —
   rejection stays in the per-route guards, so the deliberately-public routes
   (``GET /api/share/<token>``, the trigger hook, channel webhooks, CORS
   preflight) keep working untouched.
2. Back the key-auth guards (``require_api_key`` / ``require_master_token``) with
   byte-for-byte the same wire contract they had as bare functions, so the ~90
   call sites and every DI'd subsystem keep working unchanged.
3. Expose a ``require_permission(perm)`` guard FACTORY, which
   :class:`~mewbo_api.auth.permission_guard.PermissionGuard` composes to enforce
   the per-route requirement every guarded handler declares.

Every refusal this module returns is minted by the typed error classes in
``mewbo_api.errors`` rather than spelled inline, so the three legacy auth bodies
have exactly ONE definition each and a second surface cannot invent a third
spelling of them.

Design: a PLAIN atomic class (not Pydantic). It holds live, in-process
collaborators (the key store, IAM stores, the request's credential reader, a
clock) and runs on the hot path — hot runtime state that crosses no trust
boundary, the documented carve-out from the house Pydantic rule. Collaborators
are injected as fields; the IAM stores are created LAZILY on first use, so a
disabled deployment does ZERO store I/O and never writes an ``iam_*.json`` file.

When auth is disabled (the default), ``resolve`` returns a cached, full-power
legacy principal and the guards behave exactly as before IAM existed — the
byte-identical-when-disabled law.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from flask import g as flask_g
from mewbo_core.common import get_logger
from mewbo_core.key_store import KeyScopes
from mewbo_iam import (
    ADMIN_ROLE,
    AccessDeniedEvent,
    AuthAuditStoreBase,
    AuthMethod,
    AuthSettings,
    KeyMintedEvent,
    KeyRevokedEvent,
    LoginFailureEvent,
    PermissionCatalog,
    Principal,
    RoleRecord,
    RoleStoreBase,
    TrustedHeaderAuthenticator,
    UserStoreBase,
    create_auth_audit_store,
    create_role_store,
    create_user_store,
)

from mewbo_api.auth.identity_flow import DisabledUserError
from mewbo_api.errors import AuthenticationRequired, PermissionDenied

if TYPE_CHECKING:  # pragma: no cover - typing only
    from flask import Request
    from mewbo_core.config import AppConfig
    from mewbo_core.key_store import KeyStoreBase, PublicKeyRecord

    from mewbo_api.auth.federated import FederatedRuntime
    from mewbo_api.auth.ldap_login import LdapLoginService
    from mewbo_api.auth.oidc_runtime import OidcRuntime
    from mewbo_api.auth.saml_runtime import SamlRuntime

logging = get_logger(name="mewbo-api.auth")

# A zero-arg body+status guard, matching the existing ``_require_api_key`` shape
# (``None`` on success, a ``(body, status)`` tuple on rejection). The body is
# spelled ``dict[str, Any]`` — the same spelling ``mewbo_api.errors`` uses for a
# rendered refusal, which is where every body a guard returns is now minted.
Guard = Callable[[], "tuple[dict[str, Any], int] | None"]

# The one authenticator-kind → (probe modules, extra) table used to fail boot
# loudly when auth is enabled with a kind whose optional driver isn't installed.
# api_key and trusted_header need no extra — they have no driver to install. Every
# module in the tuple must import — oidc needs authlib (the RP flow) AND joserfc
# (JWT verification), so a partial install still fails loud.
_KIND_DRIVER_DEPS: dict[str, tuple[tuple[str, ...], str]] = {
    "oidc": (("authlib", "joserfc"), "oidc"),
    "ldap": (("ldap3",), "ldap"),
    "saml": (("onelogin.saml2",), "saml"),
}

# Authenticator kinds that run a browser redirect flow and mint a session cookie.
# THE one definition: ``validate_deployment`` refuses to pair them with a wildcard
# CORS origin (credentialed cross-origin requests cannot use one), and the IAM
# authenticator-discovery route lists exactly these as buttons a login screen may
# render. Two copies of this set would let those two answers disagree.
BROWSER_LOGIN_KINDS: frozenset[str] = frozenset({"oidc", "saml"})

# Auth method every key-based principal carries (local keys assert no issuer).
_API_KEY_METHOD = AuthMethod(kind="api_key", issuer=None)

# Cached, frozen principals for the two no-I/O paths. Principal is frozen, so a
# module constant is safe to share across every request.
#
# * legacy — the identity every request resolves to while auth is DISABLED:
#   full-power (admin), unrestricted scopes (None, never ()), so behavior is
#   identical to the pre-IAM server.
# * master — the break-glass identity for a request bearing the master token
#   while auth is ENABLED.
_LEGACY_PRINCIPAL = Principal(
    subject="svc:legacy",
    kind="service",
    roles=(ADMIN_ROLE,),
    scopes=None,
    auth_method=_API_KEY_METHOD,
)
_MASTER_PRINCIPAL = Principal(
    subject="svc:master",
    kind="service",
    roles=(ADMIN_ROLE,),
    scopes=None,
    auth_method=_API_KEY_METHOD,
)


def utcnow() -> datetime:
    """The default clock for every auth surface — a tz-aware UTC now.

    THE one definition. Each controller and runtime takes it as an injected
    ``clock`` field so a test pins the instant instead of patching a wall clock;
    this is only what they default to when nobody injects one. It lives here
    because the kit is the module every other auth surface already imports.
    """
    return datetime.now(timezone.utc)


def current_principal() -> Principal | None:
    """The principal resolved for the in-flight request, or ``None``.

    Reads ``flask.g.principal`` (set by the AuthKit ``before_request`` mount).
    ``None`` means unresolved (outside a request) or unauthenticated with auth
    enabled. The one accessor every surface reads the caller's identity through.
    """
    return getattr(flask_g, "principal", None)


class AuthKit:
    """Resolves each request to a principal and backs the key-auth guards.

    Construct once at app boot via :meth:`from_config`. ``credential_reader`` is
    the request's ``X-API-Key`` header / ``api_key`` query reader and
    ``master_matcher`` is the constant-time master-token compare — both injected
    (they read the Flask request context) so the kit reuses the ONE documented
    credential contract instead of re-deriving it, and a test can inject fakes.
    """

    def __init__(
        self,
        *,
        settings: AuthSettings,
        key_store: Callable[[], KeyStoreBase],
        credential_reader: Callable[..., str | None],
        master_matcher: Callable[[str], bool],
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        """Capture collaborators. IAM stores are created lazily, never here.

        ``key_store`` is a PROVIDER, not the store: the kit is built once at
        import and must resolve the store the app is using at REQUEST time, not
        the one that existed at construction. Capturing the instance made the
        kit unreachable by a caller that substitutes the store afterwards.
        """
        self._settings = settings
        self._key_store_provider = key_store
        self._credential_reader = credential_reader
        self._master_matcher = master_matcher
        self._clock = clock
        # Lazily built on first use so a disabled kit touches no IAM store.
        self._role_store: RoleStoreBase | None = None
        self._audit_store: AuthAuditStoreBase | None = None
        self._user_store: UserStoreBase | None = None
        # The shared federated runtime (JIT flow + user store + cookie signers),
        # built on first access — ``None`` when auth is off or only local api-key
        # auth is configured. Every non-key authenticator provisions through it.
        self._federated_runtime: FederatedRuntime | None = None
        self._federated_built = False
        # The OIDC engine (RP drivers) composed over the federated runtime.
        self._oidc_runtime: OidcRuntime | None = None
        self._oidc_built = False
        self._saml_runtime: SamlRuntime | None = None
        self._saml_built = False
        # The LDAP username/password login service, built on first access.
        self._ldap_login: LdapLoginService | None = None
        self._ldap_built = False

    # ── construction ────────────────────────────────────────────────────────
    @classmethod
    def from_config(
        cls,
        config: AppConfig,
        *,
        key_store: Callable[[], KeyStoreBase],
        credential_reader: Callable[..., str | None],
        master_matcher: Callable[[str], bool],
        clock: Callable[[], datetime] = utcnow,
    ) -> AuthKit:
        """Build the kit from the app config, validating the ``api.auth`` block.

        Enabled: an invalid block or a configured authenticator whose optional
        driver is missing is a HARD boot failure (raises). Disabled: an invalid
        block is logged and IGNORED (a disabled deployment must never fail to
        boot over auth config it hasn't turned on) — the kit falls back to the
        all-off default, preserving the byte-identical-when-disabled law.
        """
        block = config.api.auth
        raw = block.model_dump()
        if not block.enabled:
            try:
                settings = AuthSettings.from_config_block(raw)
            except Exception as exc:  # noqa: BLE001 - report + degrade, never crash
                logging.warning(
                    "api.auth is disabled but its config is invalid; ignoring it: {}", exc
                )
                settings = AuthSettings()
            return cls(
                settings=settings,
                key_store=key_store,
                credential_reader=credential_reader,
                master_matcher=master_matcher,
                clock=clock,
            )
        # Enabled: fail loud on invalid config or a missing driver dependency.
        settings = AuthSettings.from_config_block(raw)
        kit = cls(
            settings=settings,
            key_store=key_store,
            credential_reader=credential_reader,
            master_matcher=master_matcher,
            clock=clock,
        )
        kit._check_authenticator_deps()
        return kit

    def _check_authenticator_deps(self) -> None:
        """Raise when an enabled authenticator's optional driver isn't installed."""
        missing: list[tuple[str, str, str]] = []
        for authenticator in self._settings.authenticators:
            if not authenticator.enabled:
                continue
            dep = _KIND_DRIVER_DEPS.get(authenticator.kind)
            if dep is None:
                continue
            modules, extra = dep
            for module in modules:
                if importlib.util.find_spec(module) is None:
                    missing.append((authenticator.kind, extra, module))
        if missing:
            details = "; ".join(
                f"{kind} authenticator requires the '{extra}' extra (module '{mod}' "
                "is not importable)"
                for kind, extra, mod in missing
            )
            raise RuntimeError(
                "api.auth.enabled is true but authenticator driver dependencies are "
                f"missing: {details}. Install e.g. `pip install mewbo-iam[{missing[0][1]}]`."
            )

    # ── accessors ───────────────────────────────────────────────────────────
    @property
    def settings(self) -> AuthSettings:
        """The parsed, validated ``api.auth`` settings (auth disabled by default)."""
        return self._settings

    @property
    def enabled(self) -> bool:
        """Whether identity & access management is switched on."""
        return self._settings.enabled

    @property
    def federated_runtime(self) -> FederatedRuntime | None:
        """The shared federated runtime, built once on first access.

        ``None`` when auth is off or the only configured authenticator is the
        local api-key kind — such a deployment provisions nobody, so no user store
        is created. Every other kind (OIDC, trusted-header, LDAP, and SAML when it
        lands) provisions and mints its session cookie through THIS instance, so
        they share one identity flow, one user store and one cookie.
        """
        if not self._federated_built:
            if self._settings.enabled:
                from mewbo_api.auth.federated import build_federated_runtime

                # ``_audit`` is passed as the PROVIDER, not its result: it builds
                # the store on first use, so handing over the bound method keeps
                # a disabled-auditing deployment from constructing one at all,
                # and keeps the runtime writing to the kit's single instance.
                self._federated_runtime = build_federated_runtime(
                    self._settings, clock=self._clock, audit=self._audit
                )
            self._federated_built = True
        return self._federated_runtime

    @property
    def oidc_runtime(self) -> OidcRuntime | None:
        """The shared OIDC runtime, built once on first access (``None`` when off).

        Built lazily and network-free — the ``[oidc]`` drivers are imported only
        here (the boot dependency check already guaranteed they are installed for
        an enabled OIDC authenticator). The auth routes read the SAME instance so
        the JWKS/discovery caches are shared, not duplicated.
        """
        if not self._oidc_built:
            if self._settings.enabled:
                from mewbo_api.auth.oidc_runtime import build_oidc_runtime

                self._oidc_runtime = build_oidc_runtime(
                    self._settings, clock=self._clock, federated=self.federated_runtime
                )
            self._oidc_built = True
        return self._oidc_runtime

    @property
    def saml_runtime(self) -> SamlRuntime | None:
        """The shared SAML runtime, built once on first access (``None`` when off).

        Mirrors :attr:`oidc_runtime`: lazy, network-free, and sharing the one
        federated runtime so a SAML login provisions through the same identity
        flow every other authenticator kind uses.
        """
        if not self._saml_built:
            if self._settings.enabled:
                from mewbo_api.auth.saml_runtime import build_saml_runtime

                self._saml_runtime = build_saml_runtime(
                    self._settings, clock=self._clock, federated=self.federated_runtime
                )
            self._saml_built = True
        return self._saml_runtime

    @property
    def ldap_login(self) -> LdapLoginService | None:
        """The LDAP username/password login service (``None`` when not configured).

        Built lazily; the ``[ldap]`` driver is imported only here, and only when an
        enabled LDAP authenticator exists (the boot dependency check already
        guaranteed ``ldap3`` is installed in that case).
        """
        if not self._ldap_built:
            if self._settings.enabled:
                from mewbo_api.auth.ldap_login import build_ldap_login

                self._ldap_login = build_ldap_login(
                    self._settings, clock=self._clock, federated=self.federated_runtime
                )
            self._ldap_built = True
        return self._ldap_login

    @property
    def password_login_enabled(self) -> bool:
        """Whether a username/password login route should be served at all."""
        return self.ldap_login is not None

    def validate_deployment(self, cors_origin: str) -> None:
        """Refuse a browser-login deployment behind a wildcard CORS origin.

        When auth is enabled with any authenticator that mints a session cookie
        (OIDC/SAML), a wildcard ``Access-Control-Allow-Origin`` is invalid: a
        browser will not send credentials to, nor accept a credentialed response
        from, a wildcard origin. Called once at boot with the resolved
        ``CORS_ORIGIN``; a no-op when auth is off or only header/key
        authenticators are configured.
        """
        if not self._settings.enabled:
            return
        browser = sorted(
            {
                authenticator.kind
                for authenticator in self._settings.authenticators
                if authenticator.enabled and authenticator.kind in BROWSER_LOGIN_KINDS
            }
        )
        if browser and cors_origin.strip() == "*":
            raise RuntimeError(
                "api.auth is enabled with a browser-login authenticator "
                f"({', '.join(browser)}), which mints a session cookie, but the CORS "
                "origin is the wildcard '*'. A credentialed cross-origin request "
                "cannot use a wildcard origin — set CORS_ORIGIN to the console's "
                "exact origin (e.g. https://console.example.com)."
            )

    # ── principal resolution (never rejects) ────────────────────────────────
    def resolve(self, request: Request | None = None) -> Principal | None:
        """Resolve *request* to a principal — the value stored on ``g.principal``.

        Ordered channels (auth enabled): (1) the **api-key** channel — the
        ``X-API-Key`` header / ``api_key`` query, matching the master token or a
        stored key, its wire contract unchanged; (2) the **bearer** channel —
        ``Authorization: Bearer <token>``, a JWT verified against each enabled OIDC
        authenticator's JWKS (an opaque token via introspection when an
        authenticator opts in); (3) the **session cookie** channel — a browser's
        signed session cookie resolved to its user record; (4) the
        **trusted-header** channel — identity asserted by a reverse proxy, gated
        on the direct peer's address. Auth disabled short-circuits to the cached
        legacy full-power principal (no reads, no store I/O). This never REJECTS —
        an unauthenticated request resolves to ``None`` and the per-route guards
        decide.

        The ordering encodes one rule: an EXPLICIT credential outranks the ambient
        proxy assertion, so a caller who presents a key or a token is never
        silently re-identified as whoever the proxy says is browsing. A presented
        api key or bearer that fails to resolve stops here rather than falling
        through — a bad explicit credential is an error, not an invitation to try
        something weaker. A *stale* session cookie does fall through, because
        expiry is routine and the channel below it is strictly more gated.

        A key whose ``owner_subject`` names a DISABLED user resolves to ``None``
        here too — the same "does not resolve" outcome a bad key produces, and
        the same outcome the other three channels already give a disabled
        account (the session-cookie and both federated paths raise
        ``DisabledUserError``). The key itself stays valid (unexpired,
        unrevoked); only its owner's status changed, so this is a fact about the
        PRINCIPAL, not about the credential — checked in
        :meth:`_principal_from_record`, not in :meth:`require_api_key`.
        """
        if not self._settings.enabled:
            return _LEGACY_PRINCIPAL
        # 1. api-key channel — the exact pre-OIDC contract.
        token = (
            self._credential_reader(request) if request is not None else self._credential_reader()
        )
        if token:
            if self._master_matcher(token):
                return _MASTER_PRINCIPAL
            record = self._key_store_provider().resolve_key(token)
            if record is not None:
                principal = self._principal_from_record(record)
                if principal is not None:
                    return principal
                self._record_login_failure(reason="account_disabled", method="api_key")
                return None
            return None  # an explicitly-presented bad api key is not retried elsewhere
        if request is None:
            return None
        # Channels 2-4 all need the shared federated runtime.
        federated = self.federated_runtime
        if federated is None:
            return None
        # 2. bearer channel — OIDC is the only kind that issues one.
        oidc = self.oidc_runtime
        if oidc is not None:
            bearer = self._read_bearer(request)
            if bearer is not None:
                return oidc.resolve_bearer(bearer, now=self._clock())
        # 3. session cookie — kind-agnostic; any federated login mints it.
        cookie = self._read_cookie(request, federated.session_cookie_name)
        if cookie:
            principal = federated.resolve_session_cookie(cookie, now=self._clock())
            if principal is not None:
                return principal
        # 4. trusted proxy header — the ambient fallback.
        return self._resolve_trusted_header(request, federated)

    # ── trusted-header channel ──────────────────────────────────────────────
    def _trusted_header_authenticators(self) -> tuple[TrustedHeaderAuthenticator, ...]:
        """The enabled trusted-header authenticators, in configured order."""
        return tuple(
            a
            for a in self._settings.authenticators
            if isinstance(a, TrustedHeaderAuthenticator) and a.enabled
        )

    def _resolve_trusted_header(
        self, request: Any, federated: FederatedRuntime
    ) -> Principal | None:
        """Resolve proxy-asserted identity headers — ONLY from a trusted peer.

        THE LAW, and the reason this method exists rather than a call to
        ``authenticator.resolve(request.headers)`` at the call site: identity
        headers are trivially forgeable by anyone who can reach the server
        directly, so they are read only after ``is_trusted_source`` accepts the
        DIRECT peer address. A request carrying them from anywhere else is
        ignored, audited, and allowed to continue as anonymous — never trusted,
        never a 500.

        Peer address = ``request.remote_addr``, strictly. Werkzeug populates it
        from the actual socket, so it cannot be spoofed by a header. This
        deliberately does NOT parse ``X-Forwarded-For``: choosing a hop out of a
        client-appendable list is exactly where forward-auth deployments grow
        bypasses. A deployment with extra proxies in front must therefore either
        allowlist the last hop that actually connects to this server, or rewrite
        ``remote_addr`` at the WSGI layer with a hop-counting middleware
        (werkzeug's ``ProxyFix``) — a transport concern, settled once at deploy
        time, not re-litigated per request here.
        """
        authenticators = self._trusted_header_authenticators()
        if not authenticators:
            return None
        headers = getattr(request, "headers", None)
        if headers is None:
            return None
        remote_addr = getattr(request, "remote_addr", None) or ""
        now = self._clock()
        for authenticator in authenticators:
            if not authenticator.is_trusted_source(remote_addr):
                # Presence probe ONLY — whether the header exists, never its
                # value, so an untrusted caller's assertion is not read at all.
                if headers.get(authenticator.user_header):
                    logging.warning(
                        "ignoring trusted-header identity from untrusted source {}",
                        remote_addr or "<unknown>",
                    )
                    self._record_login_failure(
                        reason="untrusted_proxy_source", method="trusted_header"
                    )
                continue
            raw = authenticator.resolve(headers)
            if raw is None:
                continue
            try:
                return federated.provision(
                    raw,
                    auth_method=AuthMethod(kind="trusted_header", issuer=authenticator.issuer),
                    now=now,
                )
            except DisabledUserError:
                self._record_login_failure(reason="account_disabled", method="trusted_header")
                return None
        return None

    @staticmethod
    def _read_bearer(request: Request) -> str | None:
        """The ``Authorization: Bearer <token>`` value, or ``None``."""
        header = request.headers.get("Authorization", "")
        if header[:7].lower() == "bearer ":
            return header[7:].strip() or None
        return None

    @staticmethod
    def _read_cookie(request: Request, name: str) -> str | None:
        """The value of cookie *name* on the request, or ``None``."""
        return request.cookies.get(name)

    def _principal_from_record(self, record: PublicKeyRecord) -> Principal | None:
        """Map a resolved key record to a principal, honoring the three-state law.

        Absent identity fields ⇒ a legacy full-power key: roles default to
        ``("admin",)`` and scopes to ``None`` (unrestricted). ``scopes`` keeps
        its three states — absent/``None`` → ``None`` (unrestricted), ``[]`` →
        ``()`` (explicitly none); the two are NEVER collapsed. ``team_id`` is
        carried on the record but is not projected into team memberships — a
        key's principal therefore holds no team, and any ownership check that
        needs one must read the user store rather than trust this principal.

        Returns ``None`` when ``owner_subject`` names a user record whose
        ``status`` is ``"disabled"`` — offboarding a user must revoke the
        authority of every key it owns, not only future logins on other
        channels. A key with no owner, or whose owner is not a user record (a
        pure ``svc:``-prefixed service key), is never looked up: a service
        key's authority does not depend on any user's account status, and the
        common service-key request pays no store read for it.
        """
        owner = record.get("owner_subject")
        raw_roles = record.get("roles")
        raw_scopes = record.get("scopes")
        if owner:
            subject = owner
            kind = "user" if owner.startswith("user:") else "service"
        else:
            subject = f"svc:{record['id']}"
            kind = "service"
        if kind == "user" and self._user_is_disabled(owner):
            return None
        roles = (ADMIN_ROLE,) if raw_roles is None else tuple(raw_roles)
        scopes = None if raw_scopes is None else tuple(raw_scopes)
        return Principal(
            subject=subject,
            kind=kind,
            roles=roles,
            scopes=scopes,
            auth_method=_API_KEY_METHOD,
        )

    # ── key-auth guards (exact legacy wire contract) ────────────────────────
    def require_api_key(self) -> tuple[dict[str, Any], int] | None:
        """Authorize a protected route — the drop-in for the old ``_require_api_key``.

        A request is authorized by the master token (break-glass) OR a non-revoked,
        unexpired stored key. Same bodies and statuses as before: missing
        credential → ``401 {"message": "API token is not provided."}``; a bad
        credential → ``401 {"message": "Unauthorized"}``; success → ``None``.
        Uses the expiry-aware ``resolve_key`` (identical to the old ``verify_key``
        for legacy keys, which carry no expiry).

        Both bodies are minted by :class:`~mewbo_api.errors.AuthenticationRequired`
        rather than spelled here, so the strings have ONE definition. The guard
        RETURNS the rendered tuple instead of raising: ~90 call sites and every
        DI'd subsystem consume the ``(body, status) | None`` shape directly, and a
        guard that raised would bypass every one of them.
        """
        token = self._credential_reader()
        if token is None:
            return AuthenticationRequired.missing_credential().response()
        if self._master_matcher(token):
            return None
        if self._key_store_provider().resolve_key(token) is not None:
            return None
        logging.warning("Unauthorized API call attempt.")
        self._record_login_failure(reason="invalid_key", method="api_key")
        return AuthenticationRequired.invalid_credential().response()

    def require_master_token(self) -> tuple[dict[str, Any], int] | None:
        """Authorize a master-token-only route — the drop-in for ``_require_master_token``.

        Issued keys are deliberately rejected here (a leaked key must not manage
        keys). Same bodies/statuses as :meth:`require_api_key`, from the same
        classmethods.
        """
        token = self._credential_reader()
        if token is None:
            return AuthenticationRequired.missing_credential().response()
        if not self._master_matcher(token):
            logging.warning("Unauthorized key-management attempt.")
            self._record_login_failure(reason="master_required", method="api_key")
            return AuthenticationRequired.invalid_credential().response()
        return None

    # ── username/password login (LDAP) ──────────────────────────────────────
    def login_with_password(self, username: str, password: str) -> Principal | None:
        """Verify *username*/*password* against the directory, provisioning on success.

        ``None`` for every failure — unconfigured, bad credentials, a disabled
        account, or an unreachable directory — with no oracle distinguishing them
        at the wire; the audit trail carries the real reason. The caller mints the
        session cookie from ``federated_runtime`` (this method deliberately does
        not touch the response, so it stays usable outside a browser login).

        NOT rate-limited: this deployment has no lockout or throttle plane yet, so
        a directory's own lockout policy is the only brake on password guessing.
        A per-attempt sleep was considered and rejected — it blocks a worker
        thread, which turns the login route into a cheap denial-of-service lever.
        """
        service = self.ldap_login
        if service is None:
            return None
        try:
            principal = service.authenticate(username, password)
        except DisabledUserError:
            self._record_login_failure(reason="account_disabled", method="ldap")
            return None
        except Exception:  # noqa: BLE001 - a directory outage must not 500 a login
            logging.warning("password login failed against the directory", exc_info=True)
            self._record_login_failure(reason="directory_error", method="ldap")
            return None
        if principal is None:
            self._record_login_failure(reason="invalid_credentials", method="ldap")
        return principal

    # ── permission guard factory ────────────────────────────────────────────
    def require_permission(self, permission: str) -> Guard:
        """Return a zero-arg guard enforcing *permission* on ``g.principal``.

        The guard: auth disabled → PASS; no principal → ``401 {"message":
        "Unauthorized"}``; otherwise *permission* must be held by the ROLE
        (the built-in admin role bypasses the role half) AND, if the key
        carries scopes, be within them — else ``403 {"message": "insufficient
        role"}`` plus a best-effort ``access_denied`` audit event. Mirrors the
        existing guards' zero-arg, ``(body, status) | None`` shape so it drops
        into a route the same way.

        The disabled check is FIRST, and deliberately reads settings rather than
        the request: "auth off ⇒ every permission passes" must be a property of
        the configuration, not of whether a given app mounted the principal
        resolver. ``current_principal()`` reads ``g.principal``, which only
        exists where the kit's ``before_request`` runs — so a blueprint mounted
        on a bare app (the wiki blueprint's tests do exactly this) would
        otherwise find no principal and reject EVERY request, turning a disabled
        deployment into a locked one and breaking the byte-identical-when-disabled
        law. Note the asymmetry: with auth ENABLED an absent principal still
        fails closed (401), which is the correct reading of a surface that
        enforces permissions without resolving identity.

        **Scopes NARROW the role grant; they never widen it, and admin is not
        exempt from the narrowing.** A key minted with ``roles=["admin"]`` and
        ``scopes=[]`` (explicitly none) must authorize NOTHING — that is the
        self-mint escalation this closes: since the intersection can only
        subtract from what the role already grants, a broader `scopes` value
        can never manufacture a permission the role does not hold. `scopes is
        None` (unrestricted-legacy — the disabled-auth and master principals,
        and any key minted before scoping existed) is a no-op here, so this is
        byte-identical for every principal that never carried scopes to begin
        with. `KeyScopes` is the ONE matcher (`mewbo_core.key_store`) every
        scope check in the codebase shares, including the self-mint delegation
        check this mirrors — never re-derive the three-state/wildcard rule here.
        """

        def guard() -> tuple[dict[str, Any], int] | None:
            if not self._settings.enabled:
                return None
            principal = current_principal()
            if principal is None:
                return AuthenticationRequired.invalid_credential().response()
            role_granted = principal.is_admin or permission in principal.effective_permissions(
                self._role_records()
            )
            if role_granted and KeyScopes(principal.scopes).matches(permission):
                return None
            self._record_access_denied(principal, permission)
            return PermissionDenied.insufficient_role(permission).response()

        return guard

    def permissions_for(self, principal: Principal) -> frozenset[str]:
        """Every permission *principal* effectively holds, custom roles included.

        Resolving a role name to its grants is store knowledge — a custom role
        exists nowhere else — so this is the only honest source for a client
        that needs to know what it may do. An admin holds the whole catalog,
        narrowed by scopes exactly like :meth:`require_permission` — this must
        stay in lockstep with that guard's decision, else a client is told it
        may do something the guard would actually refuse.
        """
        role_granted = (
            PermissionCatalog.ALL
            if principal.is_admin
            else principal.effective_permissions(self._role_records())
        )
        scopes = KeyScopes(principal.scopes)
        if scopes.unrestricted:
            return frozenset(role_granted)
        return frozenset(p for p in role_granted if scopes.matches(p))

    # ── lazy IAM stores ─────────────────────────────────────────────────────
    def _roles(self) -> RoleStoreBase:
        """The role store, created (and built-ins seeded) on first use."""
        if self._role_store is None:
            self._role_store = create_role_store()
        return self._role_store

    def _role_records(self) -> Mapping[str, RoleRecord]:
        """All roles as a ``name → record`` map for ``effective_permissions``."""
        return {record.name: record for record in self._roles().list()}

    def _users(self) -> UserStoreBase:
        """The user store, created on first use."""
        if self._user_store is None:
            self._user_store = create_user_store()
        return self._user_store

    def _user_is_disabled(self, user_id: str) -> bool:
        """Whether *user_id* names a user record whose ``status`` is ``disabled``.

        A MISSING record is not disabled — an api-key ``owner_subject`` naming a
        user that was never provisioned, or was deleted, must not be refused for
        a status it never carried; that is a different, unrelated gap. Re-reads
        the store on every call rather than caching, matching
        :meth:`_role_records`'s existing per-request re-read: an operator who
        disables a user expects the very next request to reflect it, the same
        immediacy role revocation already has.
        """
        record = self._users().get(user_id)
        return record is not None and record.status == "disabled"

    def _audit(self) -> AuthAuditStoreBase | None:
        """The audit store, or ``None`` when auth or auditing is off (no I/O)."""
        if not (self._settings.enabled and self._settings.audit.enabled):
            return None
        if self._audit_store is None:
            self._audit_store = create_auth_audit_store()
        return self._audit_store

    def _record_login_failure(self, *, reason: str, method: str) -> None:
        """Append a ``login_failure`` audit event — best-effort, never raises."""
        store = self._audit()
        if store is None:
            return
        try:
            store.append(
                LoginFailureEvent(ts=self._clock(), source="api", method=method, reason=reason)
            )
        except Exception:  # noqa: BLE001 - audit is best-effort, never breaks the request
            logging.warning("auth audit (login_failure) write failed", exc_info=True)

    def record_key_minted(self, *, key_id: str, subject: str, label: str | None) -> None:
        """Append a ``key_minted`` event — best-effort, never raises.

        Public because the mint route owns the key store, not this kit; the kit
        owns the audit store, so the emission belongs here and the caller hands
        over what only it knows. ``subject`` is the key's OWNER — who gained the
        credential — while ``actor_subject`` is whoever minted it. On a
        self-mint they are the same principal; on an admin mint they are not,
        and a trail that recorded only one of them cannot answer who armed whom.
        """
        self._record_key_event(KeyMintedEvent, key_id=key_id, subject=subject, label=label)

    def record_key_revoked(self, *, key_id: str, subject: str) -> None:
        """Append a ``key_revoked`` event — best-effort, never raises.

        The mirror of :meth:`record_key_minted`: revocation is the one action
        that takes a credential away, so a trail carrying mints without revokes
        would show access that appears never to have been withdrawn.
        """
        self._record_key_event(KeyRevokedEvent, key_id=key_id, subject=subject)

    def _record_key_event(
        self, event: type[KeyMintedEvent] | type[KeyRevokedEvent], **fields: str | None
    ) -> None:
        """Shared body for the two key-lifecycle writes."""
        store = self._audit()
        if store is None:
            return
        principal = current_principal()
        try:
            store.append(
                event(
                    ts=self._clock(),
                    source="api",
                    actor_subject=principal.subject if principal is not None else None,
                    **fields,
                )
            )
        except Exception:  # noqa: BLE001 - audit is best-effort, never breaks the request
            logging.warning("auth audit (key lifecycle) write failed", exc_info=True)

    def _record_access_denied(self, principal: Principal, permission: str) -> None:
        """Append an ``access_denied`` audit event — best-effort, never raises."""
        store = self._audit()
        if store is None:
            return
        try:
            store.append(
                AccessDeniedEvent(
                    ts=self._clock(),
                    source="api",
                    actor_subject=principal.subject,
                    subject=principal.subject,
                    resource_kind="permission",
                    need=permission,
                    permission=permission,
                )
            )
        except Exception:  # noqa: BLE001 - audit is best-effort, never breaks the request
            logging.warning("auth audit (access_denied) write failed", exc_info=True)


__all__ = ["BROWSER_LOGIN_KINDS", "AuthKit", "Guard", "current_principal", "utcnow"]
