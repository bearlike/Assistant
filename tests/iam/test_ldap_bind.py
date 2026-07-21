#!/usr/bin/env python3
"""LDAP authentication against a fake directory — no server, no sockets.

The connection factory is injected, so every bind and search is recorded and
asserted. The law under test is that **the rebind as the user DN IS the password
check**: a search that merely finds the user proves the account exists and is a
classic authentication bypass if mistaken for verification. Every test that
accepts a credential therefore asserts the rebind was actually attempted.

The TLS-trust tests at the bottom are the exception to "no server": they drive the
REAL connection factory and assert on the real ``ldap3.Server`` it builds, because
the defect they guard lives in that object's default. Only the socket-owning
``Connection`` is stubbed there.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass, field
from typing import Any

import ldap3
import pytest
from ldap3.core.exceptions import (
    LDAPException,
    LDAPInvalidCredentialsResult,
    LDAPSocketOpenError,
    LDAPStartTLSError,
)
from mewbo_iam.authenticators import LdapAuthenticator
from mewbo_iam.drivers.ldap import (
    _DEFAULT_TIMEOUT,
    LdapBinder,
    LdapBindError,
    _default_connection_factory,
)
from pydantic import ValidationError

SERVER = "ldap://directory.example.com"
BASE_DN = "ou=people,dc=example,dc=com"
GROUP_BASE_DN = "ou=groups,dc=example,dc=com"
ALICE_DN = "uid=alice,ou=people,dc=example,dc=com"
ALICE_PASSWORD = "correct horse battery staple"
SERVICE_DN = "cn=svc,dc=example,dc=com"
SERVICE_PASSWORD = "svc-secret"


@dataclass
class FakeAttribute:
    """An ldap3 ``Attribute`` stand-in: always exposes ``.values`` as a list."""

    values: list[Any]


@dataclass
class FakeEntry:
    """An ldap3 entry stand-in — a DN plus attribute lookups by name."""

    entry_dn: str
    attributes: dict[str, list[Any]] = field(default_factory=dict)

    def __getitem__(self, name: str) -> FakeAttribute:
        if name not in self.attributes:
            raise KeyError(name)
        return FakeAttribute(values=list(self.attributes[name]))


@dataclass
class BindAttempt:
    """One call into the connection factory — who tried to bind, and with what.

    Carries the TLS trust settings too: they are per-CONNECTION, so recording them
    per attempt is what lets a test prove the user rebind was protected and not
    just the service bind.
    """

    user: str | None
    password: str | None
    start_tls: bool
    tls_verify: bool
    tls_ca_file: str | None


@dataclass
class SearchCall:
    """One search issued on an open connection."""

    base: str
    filter: str


class FakeDirectory:
    """A canned directory: DN → password, plus what each search returns.

    Records every bind attempt and every search so a test can assert on the
    SEQUENCE, not just the outcome. ``unreachable`` makes every connection
    attempt raise, modelling an outage.
    """

    def __init__(
        self,
        *,
        passwords: dict[str, str] | None = None,
        user_results: list[FakeEntry] | None = None,
        group_results: list[FakeEntry] | None = None,
        unreachable: bool = False,
        search_raises: bool = False,
        group_search_raises: bool = False,
        rebind_error: LDAPException | None = None,
    ) -> None:
        self.passwords = passwords or {SERVICE_DN: SERVICE_PASSWORD, ALICE_DN: ALICE_PASSWORD}
        self.user_results = user_results if user_results is not None else [alice_entry()]
        self.group_results = group_results or []
        self.unreachable = unreachable
        self.search_raises = search_raises
        self.group_search_raises = group_search_raises
        self.rebind_error = rebind_error
        self.binds: list[BindAttempt] = []
        self.searches: list[SearchCall] = []

    # ── the injected factory ────────────────────────────────────────────────
    def connect(
        self,
        server_url: str,
        *,
        user: str | None,
        password: str | None,
        start_tls: bool,
        tls_verify: bool,
        tls_ca_file: str | None,
        timeout: float,
    ) -> FakeConnection:
        """Stand in for ``_default_connection_factory``: bind or raise."""
        assert server_url == SERVER
        self.binds.append(
            BindAttempt(
                user=user,
                password=password,
                start_tls=start_tls,
                tls_verify=tls_verify,
                tls_ca_file=tls_ca_file,
            )
        )
        if self.unreachable:
            raise LDAPException("directory unreachable")
        # A fault that strikes only the user-rebind leg, after the service bind
        # and the search have already succeeded.
        if self.rebind_error is not None and user == ALICE_DN:
            raise self.rebind_error
        # An anonymous bind (no DN) always succeeds, exactly like a real server.
        if user is not None and self.passwords.get(user) != password:
            # The SPECIFIC result a directory returns for a bad password (LDAP
            # result 49), not a bare LDAPException: the driver treats only this
            # one as "wrong password" and everything else as an operator fault,
            # so a fake raising the generic base would test the wrong branch.
            raise LDAPInvalidCredentialsResult(result=49, description="invalidCredentials")
        return FakeConnection(self)

    # ── convenience assertions ──────────────────────────────────────────────
    def rebinds_as(self, dn: str) -> list[BindAttempt]:
        """Every bind attempt made as *dn* that was not the service bind."""
        return [b for b in self.binds if b.user == dn and b.password != SERVICE_PASSWORD]


class FakeConnection:
    """An open, bound connection: search results land on ``.entries``."""

    def __init__(self, directory: FakeDirectory) -> None:
        self._directory = directory
        self.entries: list[FakeEntry] = []
        self.unbound = False

    def search(
        self,
        *,
        search_base: str,
        search_filter: str,
        search_scope: Any,
        attributes: Any,
    ) -> None:
        """Record the search and publish whichever canned result set applies."""
        self._directory.searches.append(SearchCall(base=search_base, filter=search_filter))
        if self._directory.search_raises:
            raise LDAPException("search failed")
        if search_base == GROUP_BASE_DN:
            if self._directory.group_search_raises:
                raise LDAPException("insufficient access rights")
            self.entries = list(self._directory.group_results)
        else:
            self.entries = list(self._directory.user_results)

    def unbind(self) -> None:
        self.unbound = True


def alice_entry(**attribute_overrides: list[Any]) -> FakeEntry:
    """The canonical directory entry for the test user."""
    attributes: dict[str, list[Any]] = {
        "uid": ["alice"],
        "mail": ["alice@example.com"],
        "cn": ["Alice Example"],
        "memberOf": [],
    }
    attributes.update(attribute_overrides)
    return FakeEntry(entry_dn=ALICE_DN, attributes=attributes)


def make_authenticator(**overrides: Any) -> LdapAuthenticator:
    """An LDAP authenticator pointed at :class:`FakeDirectory`."""
    fields: dict[str, Any] = {
        "name": "corp-ldap",
        "server_url": SERVER,
        "base_dn": BASE_DN,
        "bind_dn": SERVICE_DN,
        "bind_password": SERVICE_PASSWORD,
    }
    fields.update(overrides)
    return LdapAuthenticator(**fields)


def make_binder(directory: FakeDirectory) -> LdapBinder:
    """A binder wired to *directory* — the only I/O boundary stubbed."""
    return LdapBinder(connection_factory=directory.connect)


# ── the password check IS the rebind ──────────────────────────────────────────
def test_a_correct_password_authenticates_and_the_rebind_was_actually_attempted():
    """The identity is built only after a successful rebind as the user's DN.

    A search hit alone proves the account exists, never that the presented
    password is right — so this asserts the rebind happened with that password.
    """
    directory = FakeDirectory()

    identity = make_binder(directory).authenticate(
        make_authenticator(), username="alice", password=ALICE_PASSWORD
    )

    assert identity is not None
    assert identity.external_subject.subject == "alice"
    assert identity.email == "alice@example.com"
    assert identity.display == "Alice Example"

    rebinds = directory.rebinds_as(ALICE_DN)
    assert len(rebinds) == 1, "the password check is the rebind — it must happen exactly once"
    assert rebinds[0].password == ALICE_PASSWORD


def test_the_service_account_binds_first_and_the_user_rebind_follows():
    """Ordering: service bind (to search) → rebind as the found DN (to verify)."""
    directory = FakeDirectory()

    make_binder(directory).authenticate(
        make_authenticator(), username="alice", password=ALICE_PASSWORD
    )

    assert [b.user for b in directory.binds] == [SERVICE_DN, ALICE_DN]
    assert directory.binds[0].password == SERVICE_PASSWORD


def test_a_wrong_password_is_rejected_even_though_the_search_found_the_user():
    """The search succeeds and the rebind fails — the rebind is what decides."""
    directory = FakeDirectory()

    identity = make_binder(directory).authenticate(
        make_authenticator(), username="alice", password="wrong"
    )

    assert identity is None
    assert directory.searches, "the user search still ran"
    assert directory.rebinds_as(ALICE_DN), "the rebind was attempted and rejected"


# ── the unauthenticated-bind trap ─────────────────────────────────────────────
def test_an_empty_password_is_rejected_without_opening_a_single_connection():
    """An empty password is an ANONYMOUS bind on the wire, and those SUCCEED.

    Refusing before any connection is opened is what stops a login form from
    authenticating anyone who merely knows a username.
    """
    directory = FakeDirectory()

    identity = make_binder(directory).authenticate(
        make_authenticator(), username="alice", password=""
    )

    assert identity is None
    assert directory.binds == [], "no connection may be opened for an empty password"


def test_an_empty_username_is_rejected_without_opening_a_single_connection():
    """The same refusal covers a missing login name."""
    directory = FakeDirectory()

    identity = make_binder(directory).authenticate(
        make_authenticator(), username="", password=ALICE_PASSWORD
    )

    assert identity is None
    assert directory.binds == []


# ── unknown user and ambiguity ────────────────────────────────────────────────
def test_an_unknown_user_is_rejected_and_never_reaches_a_rebind():
    """No search hit means no DN to rebind as — the flow stops there."""
    directory = FakeDirectory(user_results=[])

    identity = make_binder(directory).authenticate(
        make_authenticator(), username="nobody", password="whatever"
    )

    assert identity is None
    assert [b.user for b in directory.binds] == [SERVICE_DN]


def test_an_ambiguous_filter_matching_two_entries_is_refused():
    """Picking "the first" of two matches makes identity depend on server ordering."""
    second = FakeEntry(entry_dn="uid=alice,ou=contractors,dc=example,dc=com", attributes={})
    directory = FakeDirectory(user_results=[alice_entry(), second])

    identity = make_binder(directory).authenticate(
        make_authenticator(), username="alice", password=ALICE_PASSWORD
    )

    assert identity is None
    assert directory.rebinds_as(ALICE_DN) == [], "an ambiguous match must never authenticate"


# ── filter injection ──────────────────────────────────────────────────────────
def test_a_wildcard_username_is_escaped_and_does_not_widen_the_filter():
    """An unescaped ``*`` rewrites the filter it lands in — LDAP's SQL injection."""
    directory = FakeDirectory(user_results=[])

    make_binder(directory).authenticate(make_authenticator(), username="*", password="x")

    assert directory.searches[0].filter == "(uid=\\2a)"
    assert "(uid=*)" not in directory.searches[0].filter


@pytest.mark.parametrize(
    ("username", "expected"),
    [
        ("a)(uid=admin", "(uid=a\\29\\28uid=admin)"),
        ("a*b", "(uid=a\\2ab)"),
        ("back\\slash", "(uid=back\\5cslash)"),
    ],
)
def test_filter_metacharacters_in_a_username_are_escaped(username, expected):
    """Every RFC 4515 metacharacter is neutralized before interpolation."""
    directory = FakeDirectory(user_results=[])

    make_binder(directory).authenticate(make_authenticator(), username=username, password="x")

    assert directory.searches[0].filter == expected


# ── a directory outage is never a credential verdict ──────────────────────────
def test_an_unreachable_directory_raises_rather_than_reporting_a_wrong_password():
    """An outage is an operator fault; reporting it as "wrong password" misleads."""
    directory = FakeDirectory(unreachable=True)

    with pytest.raises(LdapBindError, match="could not bind"):
        make_binder(directory).authenticate(
            make_authenticator(), username="alice", password=ALICE_PASSWORD
        )


def test_a_failing_user_search_raises_rather_than_reporting_a_wrong_password():
    """The same distinction holds when the search leg is what breaks."""
    directory = FakeDirectory(search_raises=True)

    with pytest.raises(LdapBindError, match="directory search failed"):
        make_binder(directory).authenticate(
            make_authenticator(), username="alice", password=ALICE_PASSWORD
        )


# ── group resolution ──────────────────────────────────────────────────────────
def test_groups_are_read_from_member_of_when_no_group_subtree_is_configured():
    """The default strategy: whatever the user's own entry already carried."""
    directory = FakeDirectory(
        user_results=[
            alice_entry(memberOf=["cn=eng,ou=groups,dc=example,dc=com", "cn=oncall,ou=groups"])
        ]
    )

    identity = make_binder(directory).authenticate(
        make_authenticator(), username="alice", password=ALICE_PASSWORD
    )

    assert identity is not None
    assert identity.groups == ("cn=eng,ou=groups,dc=example,dc=com", "cn=oncall,ou=groups")
    assert [s.base for s in directory.searches] == [BASE_DN], "no group subtree search"


def test_groups_are_resolved_by_subtree_search_when_a_group_base_dn_is_set():
    """With a group subtree, membership comes from groups listing the user."""
    directory = FakeDirectory(
        group_results=[
            FakeEntry(entry_dn="cn=eng,ou=groups,dc=example,dc=com", attributes={"cn": ["eng"]}),
            FakeEntry(entry_dn="cn=sre,ou=groups,dc=example,dc=com", attributes={"cn": ["sre"]}),
        ]
    )

    identity = make_binder(directory).authenticate(
        make_authenticator(group_base_dn=GROUP_BASE_DN, group_name_attribute="cn"),
        username="alice",
        password=ALICE_PASSWORD,
    )

    assert identity is not None
    assert identity.groups == ("eng", "sre")
    group_search = directory.searches[1]
    assert group_search.base == GROUP_BASE_DN
    assert group_search.filter == f"(member={ALICE_DN})"


def test_nested_groups_apply_the_matching_rule_in_chain_oid():
    """Nesting is expanded server-side by AD's LDAP_MATCHING_RULE_IN_CHAIN."""
    all_staff = FakeEntry(
        entry_dn="cn=all-staff,ou=groups,dc=example,dc=com", attributes={"cn": ["all-staff"]}
    )
    directory = FakeDirectory(group_results=[all_staff])

    identity = make_binder(directory).authenticate(
        make_authenticator(
            group_base_dn=GROUP_BASE_DN, group_name_attribute="cn", nested_groups=True
        ),
        username="alice",
        password=ALICE_PASSWORD,
    )

    assert identity is not None
    assert identity.groups == ("all-staff",)
    assert "member:1.2.840.113556.1.4.1941:=" in directory.searches[1].filter


def test_group_resolution_falls_back_to_the_group_dn_with_no_name_attribute():
    """``group_name_attribute=None`` uses the DN, matching what memberOf returns."""
    directory = FakeDirectory(
        group_results=[FakeEntry(entry_dn="cn=eng,ou=groups,dc=example,dc=com", attributes={})]
    )

    identity = make_binder(directory).authenticate(
        make_authenticator(group_base_dn=GROUP_BASE_DN),
        username="alice",
        password=ALICE_PASSWORD,
    )

    assert identity is not None
    assert identity.groups == ("cn=eng,ou=groups,dc=example,dc=com",)


def test_a_refused_group_search_degrades_to_no_groups_rather_than_failing_the_login():
    """Best-effort by design: least privilege is the safe direction, not fail-open."""
    directory = FakeDirectory(group_search_raises=True)

    identity = make_binder(directory).authenticate(
        make_authenticator(group_base_dn=GROUP_BASE_DN, group_name_attribute="cn"),
        username="alice",
        password=ALICE_PASSWORD,
    )

    assert identity is not None, "the login still succeeds"
    assert identity.groups == (), "but the user degrades to the least-privilege default"


# ── TLS trust: encryption is not authentication ───────────────────────────────
# ldap3's own default Tls is ssl.CERT_NONE. Because the password check is a bind,
# an unverified certificate does not merely weaken the channel — it hands the
# user's password to whoever answered the socket. These tests drive the REAL
# connection factory and assert on the REAL ldap3 Server it builds; only the
# socket-owning Connection is stubbed.
@dataclass
class BuiltConnection:
    """What the real factory handed to ldap3: the server, and the ldap3 kwargs."""

    server: Any
    kwargs: dict[str, Any]


def build_connection(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> BuiltConnection:
    """Run the real factory and capture what it built, opening no socket.

    The ``Server`` and its ``Tls`` are genuinely constructed — that object is what
    carries the trust settings onto the wire, so faking it would test nothing.
    """
    captured: list[BuiltConnection] = []

    class SocketlessConnection:
        """An ldap3 ``Connection`` that records its arguments and opens nothing."""

        def __init__(self, server: Any, **kwargs: Any) -> None:
            captured.append(BuiltConnection(server=server, kwargs=kwargs))

        def start_tls(self) -> None:
            """StartTLS is a socket operation; there is no socket here."""

        def bind(self) -> None:
            """Binding is a socket operation; there is no socket here."""

    monkeypatch.setattr(ldap3, "Connection", SocketlessConnection)
    arguments: dict[str, Any] = {
        "user": SERVICE_DN,
        "password": SERVICE_PASSWORD,
        "start_tls": False,
        "tls_verify": True,
        "tls_ca_file": None,
        "timeout": _DEFAULT_TIMEOUT,
    }
    arguments.update(overrides)
    _default_connection_factory(SERVER, **arguments)
    return captured[0]


def test_a_config_that_mentions_no_tls_settings_still_validates_the_certificate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Secure by DEFAULT: omitting both fields must not inherit ldap3's CERT_NONE.

    Driven through the model's own defaults rather than literals, so the test
    fails if a later edit flips the default rather than only if the driver breaks.
    """
    authenticator = make_authenticator()
    assert authenticator.tls_verify is True, "validation is the default"
    assert authenticator.tls_ca_file is None, "with no CA bundle configured"

    server = build_connection(
        monkeypatch,
        tls_verify=authenticator.tls_verify,
        tls_ca_file=authenticator.tls_ca_file,
    ).server

    assert server.tls.validate == ssl.CERT_REQUIRED
    assert server.tls.ca_certs_file is None, "unset means the system trust store"


def test_a_private_ca_bundle_reaches_the_tls_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The enterprise case: an internal CA the system trust store does not know."""
    ca_file = tmp_path / "corp-root-ca.pem"
    ca_file.write_text("-- a CA bundle --")

    server = build_connection(monkeypatch, tls_ca_file=str(ca_file)).server

    assert server.tls.ca_certs_file == str(ca_file)
    assert server.tls.validate == ssl.CERT_REQUIRED, "a CA bundle does not relax validation"


def test_an_unreadable_ca_bundle_fails_loudly_instead_of_falling_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A mistyped CA path must not silently degrade to the system trust store.

    That fallback would be the quiet version of the whole defect: the operator
    reads a config pinning their internal CA while the directory is trusted on
    entirely different grounds. Pins ldap3's existence check, which this design
    relies on — ``tls_ca_file`` is only trustworthy because of it.
    """
    with pytest.raises(LDAPException):
        build_connection(monkeypatch, tls_ca_file=str(tmp_path / "absent.pem"))


def test_the_opt_out_produces_an_unvalidated_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``tls_verify=False`` is a real opt-out — deliberate, and this is its effect."""
    server = build_connection(monkeypatch, tls_verify=False).server

    assert server.tls.validate == ssl.CERT_NONE


def test_tls_verify_false_alongside_a_ca_file_is_refused_at_definition() -> None:
    """The pair is incoherent: a CA bundle is only consulted while validating.

    Refused rather than resolved — either winner leaves a config that READS as
    the other, and the two differ by whether the directory is authenticated.
    """
    with pytest.raises(ValidationError) as raised:
        make_authenticator(tls_verify=False, tls_ca_file="/etc/ssl/certs/corp-root-ca.pem")

    assert "tls_ca_file is only consulted when tls_verify is true" in str(raised.value)


def test_the_user_rebind_is_protected_by_the_same_trust_settings_as_the_service_bind() -> None:
    """The rebind is where the PASSWORD travels — it must not be the unguarded leg.

    Asserting on every recorded attempt rather than on one index is what makes a
    future third connection inherit the requirement instead of escaping it.
    """
    directory = FakeDirectory()
    ca_file = "/etc/ssl/certs/corp-root-ca.pem"

    identity = make_binder(directory).authenticate(
        make_authenticator(tls_ca_file=ca_file),
        username="alice",
        password=ALICE_PASSWORD,
    )

    assert identity is not None
    assert directory.rebinds_as(ALICE_DN), "the rebind happened"
    assert len(directory.binds) == 2, "the service bind and the rebind"
    for attempt in directory.binds:
        assert attempt.tls_verify is True, f"bind as {attempt.user} skipped validation"
        assert attempt.tls_ca_file == ca_file, f"bind as {attempt.user} lost the CA bundle"


# ── a fault on the REBIND leg is an operator fault, not a wrong password ──────
# The rebind is the leg the password crosses, so what it reports when it fails is
# a security decision. Only "invalidCredentials" (LDAP result 49) means the
# password was wrong; every other failure means the directory never got to judge
# it, and saying "wrong password" there sends the user to retype a credential
# that was never the problem.


def test_an_outage_on_the_rebind_leg_raises_rather_than_denying_the_credential():
    """A directory that dies between the search and the rebind is an operator fault.

    Reported as ``None`` it would reach the user as "wrong password", sending
    them to reset a credential that was never wrong.
    """
    directory = FakeDirectory(rebind_error=LDAPSocketOpenError("connection refused"))

    with pytest.raises(LdapBindError):
        make_binder(directory).authenticate(
            make_authenticator(), username="alice", password=ALICE_PASSWORD
        )


def test_a_tls_failure_on_the_rebind_leg_raises_rather_than_denying_the_credential():
    """The security-relevant half: a REFUSED CERTIFICATE must not read as a typo.

    Certificate validation failing on the rebind is the signal that something is
    intercepting the connection the password is about to cross. Returning "wrong
    password" would invite the user to retype it — into whatever is intercepting.
    That turns the detection signal certificate validation exists to produce back
    into a credential-retry loop.
    """
    directory = FakeDirectory(rebind_error=LDAPStartTLSError("certificate verify failed"))

    with pytest.raises(LdapBindError):
        make_binder(directory).authenticate(
            make_authenticator(start_tls=True), username="alice", password=ALICE_PASSWORD
        )


def test_only_an_invalid_credentials_result_is_reported_as_a_wrong_password():
    """The other side of the split: result 49 must still deny quietly.

    Widening the operator-fault branch until it swallowed this one would turn
    every mistyped password into a 500, so the two are asserted together.
    """
    directory = FakeDirectory(
        rebind_error=LDAPInvalidCredentialsResult(result=49, description="invalidCredentials")
    )

    identity = make_binder(directory).authenticate(
        make_authenticator(), username="alice", password=ALICE_PASSWORD
    )

    assert identity is None, "a rejected credential denies, and does not raise"


def test_the_receive_timeout_handed_to_ldap3_is_an_integer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A float receive timeout fails EVERY connection before TLS is negotiated.

    On POSIX ldap3 sets SO_RCVTIMEO via ``struct.pack('LL', receive_timeout, 0)``,
    which rejects a float with ``struct.error`` — and that is not an
    ``LDAPException``, so it escapes the driver's error normalization entirely.
    The module's own default timeout is a float, so this asserts the coercion
    against that default rather than against a literal chosen to pass.
    """
    assert isinstance(_DEFAULT_TIMEOUT, float), "the module default is a float — hence the coercion"

    built = build_connection(monkeypatch, timeout=_DEFAULT_TIMEOUT)

    assert isinstance(built.kwargs["receive_timeout"], int)
    assert built.kwargs["receive_timeout"] == int(_DEFAULT_TIMEOUT)


def test_a_sub_second_timeout_does_not_collapse_to_a_blocking_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POSIX reads a zero receive timeout as "block forever" — the opposite intent.

    Truncating 0.5 to 0 would turn the tightest configured timeout into no timeout
    at all, so the floor is what keeps a small value meaning "give up quickly".
    """
    built = build_connection(monkeypatch, timeout=0.5)

    assert built.kwargs["receive_timeout"] == 1
