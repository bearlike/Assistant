#!/usr/bin/env python3
"""``LdapLoginService`` — username/password login against the configured directories.

LDAP is the odd one out among the federated kinds: OIDC and SAML hand the browser
to the provider and get an assertion back, while LDAP has no redirect leg at all —
the credentials arrive at THIS server, on a form post. So there is no handshake
state, no callback and no provider round-trip to model; the whole flow is "verify,
then provision", which is what this service is.

It composes rather than duplicates: the bind/search/rebind is the kernel's
:class:`~mewbo_iam.drivers.ldap.LdapBinder`, and the provisioning half — group
mappings, bootstrap admin, JIT upsert, the session cookie — is the shared
:class:`~mewbo_api.auth.federated.FederatedRuntime` that OIDC also provisions
through. This module adds only the ordering between them.

Import discipline: the heavy ``mewbo_iam.drivers`` (the ``[ldap]`` extra) is
imported LAZILY inside :func:`build_ldap_login`, never at module top.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from mewbo_core.common import get_logger
from mewbo_iam import AuthMethod, AuthSettings, Principal
from mewbo_iam.authenticators import LdapAuthenticator

from mewbo_api.auth.federated import FederatedRuntime

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mewbo_iam.drivers import LdapBinder

logging = get_logger(name="mewbo-api.auth.ldap")

# The auth-method kind a directory login stamps on its principal and cookie.
_LDAP_METHOD = "ldap"


class LdapLoginService:
    """Verifies form credentials against the directories and provisions the user.

    Atomic class — the binder, the authenticators, the shared federated runtime
    and the clock are DI'd fields. Holds no per-login state, so one instance
    serves every request and worker thread.
    """

    def __init__(
        self,
        *,
        authenticators: tuple[LdapAuthenticator, ...],
        binder: LdapBinder,
        federated: FederatedRuntime,
        clock: Callable[[], datetime],
    ) -> None:
        """Capture the directory configs, the bind driver, the runtime and the clock."""
        self._authenticators = authenticators
        self._binder = binder
        self._federated = federated
        self._clock = clock

    @property
    def federated(self) -> FederatedRuntime:
        """The shared federated runtime (the caller mints its cookie from this)."""
        return self._federated

    @property
    def authenticator_names(self) -> tuple[str, ...]:
        """The configured directory names, in order — for an operator-facing list."""
        return tuple(a.name for a in self._authenticators)

    def authenticate(
        self, username: str, password: str, *, now: datetime | None = None
    ) -> Principal | None:
        """Verify the credentials against each directory in turn, first match wins.

        ``None`` when no directory accepts them. A directory that is unreachable or
        misconfigured is logged and SKIPPED rather than failing the whole login —
        with several directories configured, one being down must not lock out users
        who authenticate against another. Raises
        :class:`~mewbo_api.auth.identity_flow.DisabledUserError` straight through:
        a disabled account is a deliberate decision, and swallowing it would let
        the JIT upsert quietly re-activate them.
        """
        from mewbo_iam.drivers import LdapBindError

        moment = now if now is not None else self._clock()
        for authenticator in self._authenticators:
            try:
                raw = self._binder.authenticate(
                    authenticator, username=username, password=password
                )
            except LdapBindError:
                logging.warning(
                    "directory {} is unavailable for password login; skipping it",
                    authenticator.name,
                    exc_info=True,
                )
                continue
            if raw is None:
                continue
            return self._federated.provision(
                raw,
                auth_method=AuthMethod(kind=_LDAP_METHOD, issuer=authenticator.server_url),
                now=moment,
            )
        return None

    def session_cookie(self, principal: Principal, *, now: datetime | None = None) -> str:
        """Mint the session cookie for a just-authenticated directory principal."""
        return self._federated.session_cookie(principal, now=now, method=_LDAP_METHOD)


def build_ldap_login(
    settings: AuthSettings,
    *,
    clock: Callable[[], datetime],
    federated: FederatedRuntime | None,
) -> LdapLoginService | None:
    """Build the LDAP login service, or ``None`` when no LDAP directory is enabled.

    Lazily imports the ``[ldap]`` driver — the AuthKit's boot dependency check has
    already guaranteed ``ldap3`` is installed when an LDAP authenticator is
    enabled, so reaching this import without it cannot happen on a booted server.
    """
    ldap_authenticators = tuple(
        a for a in settings.authenticators if isinstance(a, LdapAuthenticator) and a.enabled
    )
    if not settings.enabled or not ldap_authenticators or federated is None:
        return None

    from mewbo_iam.drivers import LdapBinder

    return LdapLoginService(
        authenticators=ldap_authenticators,
        binder=LdapBinder(),
        federated=federated,
        clock=clock,
    )


__all__ = ["LdapLoginService", "build_ldap_login"]
