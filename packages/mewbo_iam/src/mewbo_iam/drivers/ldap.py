#!/usr/bin/env python3
"""The LDAP bind + search leg — the ``[ldap]`` extra's network half.

The kernel's ``LdapAuthenticator`` model is pure: an already-fetched directory
entry arrives as a ``Mapping`` and maps to a ``RawIdentity``. THIS module is the
other half — :class:`LdapBinder` actually talks to the directory.

**The password check is the REBIND, and nothing else.** The sequence is: bind as
the service account (or anonymously) → search for the login name → rebind as the
DN that search returned, using the presented password. Only that last step proves
the password. A search that finds the user proves the account exists and is a
classic authentication bypass if mistaken for verification, so the two are kept
visibly separate here: :meth:`_find_user` returns a DN and an entry, and it is
:meth:`authenticate` that refuses to build an identity until the rebind succeeds.

Three adjacent traps this module closes deliberately:

* **The unverified certificate.** ldap3's default ``Tls`` is ``ssl.CERT_NONE``, so
  ``ldaps://`` and StartTLS encrypt the connection without ever authenticating the
  server. That is not a lesser form of security here: because the password check
  is a bind, an attacker on the network path who presents any certificate at all
  is handed the user's password. The ``Tls`` object is therefore always built
  explicitly, and it reaches BOTH the service connection and the user rebind.
* **The unauthenticated bind.** LDAP treats a bind with a DN and an EMPTY password
  as an anonymous bind — which SUCCEEDS. A login form that passes an empty password
  straight through therefore authenticates anyone who knows a username. Empty
  passwords are rejected before any connection is opened.
* **The ambiguous filter.** A ``user_filter`` matching more than one entry is
  refused rather than resolved to the first hit; which entry "first" means is a
  server-ordering detail, not an identity decision.

The connection factory is injected, so the whole flow is testable against a fake
directory with no server and no sockets.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

import ldap3
from ldap3.core.exceptions import LDAPException, LDAPInvalidCredentialsResult

from mewbo_iam.authenticators import AuthenticatorSpec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mewbo_iam.authenticators import LdapAuthenticator, RawIdentity

# Bounds the connect/bind/search legs so an unreachable or wedged directory fails
# the login promptly instead of holding a worker thread on an open socket.
_DEFAULT_TIMEOUT = 10.0

# Active Directory's LDAP_MATCHING_RULE_IN_CHAIN. Applied to the group-member
# attribute, it makes the server walk nested group membership transitively — one
# search instead of a recursive client-side crawl. AD-family servers only; other
# directories ignore or reject it, which is why it is opt-in via ``nested_groups``.
_MATCHING_RULE_IN_CHAIN = "1.2.840.113556.1.4.1941"

# A connection factory: (server_url, bind DN or None, password or None, …) → a
# BOUND connection. Injected so tests substitute a fake directory.
ConnectionFactory = Callable[..., Any]


class LdapBindError(Exception):
    """The directory could not be reached, or a service-account bind failed.

    Deliberately distinct from "wrong password": a wrong password is a normal
    ``None`` return, while this is an operator-facing fault (bad service
    credentials, TLS failure, unreachable host) that must not be reported to the
    user as a credential problem.
    """


# RFC 4515 filter metacharacters → their escaped hex form. Interpolating an
# unescaped ``*``, ``(``, ``)`` or NUL rewrites the filter it lands in — the
# LDAP analogue of SQL injection. Backslash is listed FIRST only for readability;
# ``str.translate`` maps each character independently, so it cannot double-escape.
_FILTER_ESCAPES = str.maketrans(
    {
        "\\": "\\5c",
        "*": "\\2a",
        "(": "\\28",
        ")": "\\29",
        "\0": "\\00",
        "/": "\\2f",
    }
)


def _default_connection_factory(
    server_url: str,
    *,
    user: str | None,
    password: str | None,
    start_tls: bool,
    tls_verify: bool,
    tls_ca_file: str | None,
    timeout: float,
) -> Any:
    """Open and BIND a real ldap3 connection, raising on failure.

    ``raise_exceptions=True`` makes a rejected bind raise rather than return a
    falsy connection, so a caller cannot mistake a failed bind for a successful
    one by forgetting to check a return value.

    The ``Tls`` object is built EXPLICITLY because ldap3's default one validates
    nothing (``ssl.CERT_NONE``): without it, ``ldaps://`` and StartTLS encrypt the
    connection but never authenticate the server, and since the password check is
    a rebind, whoever answers the socket receives the user's password. At
    ``CERT_REQUIRED`` ldap3 also checks the certificate against the host in
    ``server_url``, so a valid certificate for the wrong name is refused too.
    ``ca_certs_file=None`` leaves ldap3 loading the system trust store.
    """
    tls = ldap3.Tls(
        validate=ssl.CERT_REQUIRED if tls_verify else ssl.CERT_NONE,
        ca_certs_file=tls_ca_file,
    )
    server = ldap3.Server(server_url, get_info=ldap3.NONE, connect_timeout=timeout, tls=tls)
    connection = ldap3.Connection(
        server,
        user=user,
        password=password,
        auto_bind=False,
        raise_exceptions=True,
        # INT, not the float the rest of this module passes around: on POSIX ldap3
        # sets SO_RCVTIMEO via ``struct.pack('LL', receive_timeout, 0)``, which
        # rejects a float outright — a float fails EVERY connection with
        # ``struct.error`` before TLS is negotiated. Floored at 1 because POSIX
        # reads a zero receive timeout as "block forever".
        receive_timeout=max(1, int(timeout)),
    )
    if start_tls:
        connection.start_tls()
    connection.bind()
    return connection


class LdapBinder:
    """Verifies a username/password against a directory and reads the user's groups.

    Atomic class: the connection factory and the network timeout are its only
    state; everything per-login (the authenticator config, the credentials)
    arrives as a method argument. ONE binder serves every LDAP authenticator.
    """

    def __init__(
        self,
        *,
        connection_factory: ConnectionFactory = _default_connection_factory,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        """Capture the connection factory and the per-connection timeout."""
        self._connect = connection_factory
        self._timeout = timeout

    @staticmethod
    def _escape_filter_value(value: str) -> str:
        """Escape a value for use inside an LDAP filter (RFC 4515).

        The one place untrusted input meets filter syntax in this module — every
        interpolation into a search filter goes through it.
        """
        return value.translate(_FILTER_ESCAPES)

    def authenticate(
        self, authenticator: LdapAuthenticator, *, username: str, password: str
    ) -> RawIdentity | None:
        """Verify the credentials and return the resulting identity, or ``None``.

        ``None`` means "these credentials are not valid here" — no such user, an
        ambiguous match, or a failed rebind — and never distinguishes them, so the
        caller cannot turn this into a username oracle. A directory-side fault
        raises :class:`LdapBindError` instead, because that is an operator problem,
        not a credential one.
        """
        if not username or not password:
            # An empty password is an ANONYMOUS bind on the wire, and anonymous
            # binds succeed. Refuse before opening a connection.
            return None

        service = self._open(
            authenticator, user=authenticator.bind_dn, password=authenticator.bind_password
        )
        try:
            found = self._find_user(service, authenticator, username=username)
            if found is None:
                return None
            user_dn, entry = found
            if not self._verify_password(authenticator, user_dn=user_dn, password=password):
                return None
            groups = self._read_groups(service, authenticator, user_dn=user_dn, entry=entry)
        finally:
            self._close(service)

        return authenticator.resolve({**entry, authenticator.group_attribute: groups})

    # ── connection helpers ──────────────────────────────────────────────────
    def _transport(self, authenticator: LdapAuthenticator) -> dict[str, Any]:
        """Transport settings shared by EVERY connection this binder opens.

        One builder on purpose: the service bind and the user rebind are opened by
        separate call sites, and a rebind that quietly skipped certificate
        validation would hand the user's password to an unauthenticated peer. With
        the settings assembled in a single place the two legs cannot drift apart.
        """
        return {
            "start_tls": authenticator.start_tls,
            "tls_verify": authenticator.tls_verify,
            "tls_ca_file": authenticator.tls_ca_file,
            "timeout": self._timeout,
        }

    def _open(
        self, authenticator: LdapAuthenticator, *, user: str | None, password: str | None
    ) -> Any:
        """Open a bound connection, normalizing the transport error family."""
        try:
            return self._connect(
                authenticator.server_url,
                user=user,
                password=password,
                **self._transport(authenticator),
            )
        except LDAPException as exc:
            raise LdapBindError(f"could not bind to {authenticator.server_url}") from exc

    @staticmethod
    def _close(connection: Any) -> None:
        """Unbind, best-effort — a failed teardown must not mask the outcome."""
        try:
            connection.unbind()
        except Exception:  # noqa: BLE001 - teardown is best-effort by contract
            pass

    # ── the three legs ──────────────────────────────────────────────────────
    def _find_user(
        self, connection: Any, authenticator: LdapAuthenticator, *, username: str
    ) -> tuple[str, dict[str, object]] | None:
        """Search for *username*, returning ``(dn, attributes)`` for the ONE match.

        ``None`` when no entry matches, or when more than one does — an ambiguous
        filter must never authenticate, because picking "the first" entry makes the
        identity depend on server result ordering.
        """
        search_filter = authenticator.user_filter.format(
            username=self._escape_filter_value(username)
        )
        attributes = [
            authenticator.uid_attribute,
            authenticator.mail_attribute,
            authenticator.name_attribute,
            authenticator.group_attribute,
        ]
        try:
            connection.search(
                search_base=authenticator.base_dn,
                search_filter=search_filter,
                search_scope=ldap3.SUBTREE,
                attributes=attributes,
            )
        except LDAPException as exc:
            raise LdapBindError("directory search failed") from exc
        entries = list(getattr(connection, "entries", ()) or ())
        if len(entries) != 1:
            return None
        entry = entries[0]
        return str(entry.entry_dn), self._entry_attributes(entry, attributes)

    def _verify_password(
        self, authenticator: LdapAuthenticator, *, user_dn: str, password: str
    ) -> bool:
        """THE password check: bind as *user_dn* with the presented password.

        ONLY the directory answering "invalidCredentials" (LDAP result 49) is a
        wrong password (``False``). Everything else — the socket never opening,
        the certificate being refused, the session dying mid-bind — is an operator
        fault and propagates as :class:`LdapBindError`.

        The distinction is security-critical, not cosmetic. Reporting a REFUSED
        CERTIFICATE as "wrong password" tells the user to retype their password
        into whatever is intercepting the connection: certificate validation
        turns a silent compromise into a signal, and collapsing that signal back
        into a credential prompt is worse than not having raised it. The split is
        structural rather than a list of error names — a result-code exception
        means the directory answered, and only ``LDAPInvalidCredentialsResult``
        means it answered "no".

        Active Directory reports a locked or expired account as result 49 too, so
        those read as a wrong password here. That is deliberate: reporting them
        apart would make this an account-status oracle for an unauthenticated
        caller.

        An exception that is not an ``LDAPException`` at all is left to propagate
        untouched — a bug in this driver must surface as one, not be relabelled an
        operator fault.
        """
        try:
            connection = self._connect(
                authenticator.server_url,
                user=user_dn,
                password=password,
                **self._transport(authenticator),
            )
        except LDAPInvalidCredentialsResult:
            return False
        except LDAPException as exc:
            raise LdapBindError(
                f"the rebind as {user_dn} failed for a non-credential reason"
            ) from exc
        self._close(connection)
        return True

    def _read_groups(
        self,
        connection: Any,
        authenticator: LdapAuthenticator,
        *,
        user_dn: str,
        entry: dict[str, object],
    ) -> tuple[str, ...]:
        """Resolve the user's groups by whichever strategy the config selected.

        With no ``group_base_dn`` this is just what the user entry already carried
        (``memberOf``). With one, the group subtree is searched for entries listing
        the user as a member — the only strategy that can expand nesting, via the
        AD matching-rule OID when ``nested_groups`` is on.

        Group resolution is best-effort: a directory that refuses the group search
        yields NO groups rather than failing the login. That degrades a user to the
        mapping's least-privilege default, which is the safe direction — the
        alternative, failing open on the roles they last had, is not.
        """
        if not authenticator.group_base_dn:
            return AuthenticatorSpec.normalize_groups(entry.get(authenticator.group_attribute))

        attribute = authenticator.group_member_attribute
        if authenticator.nested_groups:
            attribute = f"{attribute}:{_MATCHING_RULE_IN_CHAIN}:"
        wanted = authenticator.group_name_attribute
        try:
            connection.search(
                search_base=authenticator.group_base_dn,
                search_filter=f"({attribute}={self._escape_filter_value(user_dn)})",
                search_scope=ldap3.SUBTREE,
                attributes=[wanted] if wanted else [],
            )
        except LDAPException:
            return ()
        groups: list[str] = []
        for group in list(getattr(connection, "entries", ()) or ()):
            if wanted:
                value = self._attribute_value(group, wanted)
                groups.extend(AuthenticatorSpec.normalize_groups(value))
            else:
                groups.append(str(group.entry_dn))
        return tuple(groups)

    # ── ldap3 entry unwrapping ──────────────────────────────────────────────
    def _entry_attributes(self, entry: Any, names: Sequence[str]) -> dict[str, object]:
        """Flatten an ldap3 entry's requested attributes into a plain dict.

        The kernel model reads a plain ``Mapping``, so the ldap3 ``Attribute``
        wrappers stop here — the model never learns what library fetched the entry.
        """
        return {name: self._attribute_value(entry, name) for name in names}

    @staticmethod
    def _attribute_value(entry: Any, name: str) -> object:
        """One attribute's value: a scalar when single-valued, a list when not.

        ldap3 exposes ``.value`` (collapsed) and ``.values`` (always a list). A
        missing attribute yields ``None``, which the kernel model already treats as
        absent.
        """
        try:
            attribute = entry[name]
        except (KeyError, LDAPException):
            return None
        values = list(getattr(attribute, "values", ()) or ())
        if not values:
            return None
        return values if len(values) > 1 else values[0]


__all__ = ["LdapBinder", "LdapBindError", "ConnectionFactory"]
