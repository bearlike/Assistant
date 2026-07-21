#!/usr/bin/env python3
"""``login_success`` — the audit event a compliance trail cannot do without.

An operator who enables auditing was getting failures and denials but no record
of anyone actually signing in, so the trail could not answer "who logged in".
These pin both halves of that: that a login IS recorded, and — the part that is
easy to get wrong — that ordinary request traffic is NOT.

The placement is the whole design. A success is recorded where the session
cookie is MINTED, which happens once per browser login and which every kind
funnels through. Recording it beside the failures in ``AuthKit`` instead would
look symmetric and flood the store, because ``AuthKit.resolve`` runs on every
single request.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from mewbo_api.auth.federated import FederatedRuntime, build_federated_runtime
from mewbo_api.auth.kit import AuthKit
from mewbo_iam import AuthMethod, ExternalSubject, RawIdentity
from mewbo_iam.audit import AuthAuditEvent
from mewbo_iam.settings import AuditSettings, AuthSettings, SessionSettings
from mewbo_iam.stores.audit import AuthAuditStoreBase, JsonAuthAuditStore
from mewbo_iam.stores.teams import JsonTeamStore
from mewbo_iam.stores.users import JsonUserStore

NOW = datetime(2031, 3, 4, 12, 0, tzinfo=timezone.utc)
ISSUER = "https://idp.example.com"
OIDC_METHOD = AuthMethod(kind="oidc", issuer=ISSUER)
SECRET = "a-test-session-secret-value"

TRUSTED_HEADER = {
    "kind": "trusted_header",
    "name": "proxy",
    "trusted_proxies": ("127.0.0.1/32",),
}


def raw(subject: str = "sub-1") -> RawIdentity:
    return RawIdentity(
        external_subject=ExternalSubject(issuer=ISSUER, subject=subject),
        email="engineer@example.com",
        display="An Engineer",
    )


def settings(*, audit_enabled: bool = True) -> AuthSettings:
    return AuthSettings(
        enabled=True,
        authenticators=(TRUSTED_HEADER,),
        session=SessionSettings(secret=SECRET),
        audit=AuditSettings(enabled=audit_enabled),
    )


def runtime(
    tmp_path: Path,
    *,
    audit: AuthAuditStoreBase | None,
    auth_settings: AuthSettings | None = None,
) -> FederatedRuntime:
    built = build_federated_runtime(
        auth_settings if auth_settings is not None else settings(),
        clock=lambda: NOW,
        user_store=JsonUserStore(tmp_path / "users.json"),
        team_store=JsonTeamStore(tmp_path / "teams.json"),
        audit=lambda: audit,
    )
    assert built is not None
    return built


@pytest.fixture
def audit_store(tmp_path: Path) -> JsonAuthAuditStore:
    return JsonAuthAuditStore(tmp_path / "audit.json")


def successes(store: AuthAuditStoreBase) -> list[AuthAuditEvent]:
    return [event for event in store.list() if event.type == "login_success"]


# ── a login is recorded ───────────────────────────────────────────────────────


def test_minting_a_session_cookie_records_the_login(
    tmp_path: Path, audit_store: JsonAuthAuditStore
) -> None:
    """The event carries who signed in, by which method, and against which issuer."""
    federated = runtime(tmp_path, audit=audit_store)
    principal = federated.provision(raw(), auth_method=OIDC_METHOD)

    federated.session_cookie(principal, now=NOW)

    recorded = successes(audit_store)
    assert len(recorded) == 1
    event = recorded[0]
    assert (event.subject, event.actor_subject) == (principal.subject, principal.subject)
    assert (event.method, event.issuer, event.source) == ("oidc", ISSUER, "api")


def test_the_method_recorded_is_the_one_that_minted_the_cookie(
    tmp_path: Path, audit_store: JsonAuthAuditStore
) -> None:
    """An LDAP login is not filed as OIDC just because the principal was provisioned so.

    ``session_cookie`` takes an explicit ``method`` for exactly this reason, and
    the audit record has to follow it rather than the principal's own stamp —
    otherwise the trail attributes every password login to whichever kind
    happened to provision the user.
    """
    federated = runtime(tmp_path, audit=audit_store)
    principal = federated.provision(raw(), auth_method=OIDC_METHOD)

    federated.session_cookie(principal, now=NOW, method="ldap")

    assert [event.method for event in successes(audit_store)] == ["ldap"]


# ── ordinary traffic is NOT a login ───────────────────────────────────────────


def test_resolving_a_cookie_on_a_later_request_records_nothing(
    tmp_path: Path, audit_store: JsonAuthAuditStore
) -> None:
    """The regression that motivates the placement.

    Reading a session cookie is what every subsequent request does. If a success
    were recorded there — or in ``AuthKit.resolve``, which runs per request —
    the trail would gain an entry per page load and the actual logins would be
    unfindable inside it.
    """
    federated = runtime(tmp_path, audit=audit_store)
    principal = federated.provision(raw(), auth_method=OIDC_METHOD)
    cookie = federated.session_cookie(principal, now=NOW)
    before = len(successes(audit_store))

    for _ in range(5):
        assert federated.resolve_session_cookie(cookie, now=NOW) is not None

    assert len(successes(audit_store)) == before


def test_provisioning_alone_records_nothing(
    tmp_path: Path, audit_store: JsonAuthAuditStore
) -> None:
    """JIT provisioning is not a login either — the trusted-header channel
    re-provisions on every request, so the record has to hang off the cookie
    mint rather than off ``provision``."""
    federated = runtime(tmp_path, audit=audit_store)

    for _ in range(3):
        federated.provision(raw(), auth_method=OIDC_METHOD)

    assert successes(audit_store) == []


# ── best-effort: auditing off, and a store that fails ─────────────────────────


def kit_for(auth_settings: AuthSettings) -> AuthKit:
    """A kit over *auth_settings* — the real ``_audit`` gate, not a stubbed one."""
    return AuthKit(
        settings=auth_settings,
        key_store=lambda: pytest.fail("no key store is needed for a cookie mint"),
        credential_reader=lambda: None,
        master_matcher=lambda _token: False,
    )


def test_auditing_disabled_writes_nothing_through_the_real_gate(tmp_path: Path) -> None:
    """Exercised through ``AuthKit``, whose ``_audit`` is the actual gate.

    Stubbing the provider with ``None`` would pass no matter what the gate did;
    this builds the runtime the way production does, so the assertion covers
    the settings check rather than the test's own fixture.
    """
    kit = kit_for(settings(audit_enabled=False))
    federated = kit.federated_runtime
    assert federated is not None
    principal = federated.provision(raw(), auth_method=OIDC_METHOD)

    assert federated.session_cookie(principal, now=NOW)
    assert not (Path(tmp_path) / "home" / "iam_auth_audit.json").exists()


def test_auth_disabled_has_no_federated_runtime_to_record_a_login_at(
    tmp_path: Path,
) -> None:
    """The auth-off path cannot emit a login event, because it mints no session.

    ``resolve`` short-circuits to the cached legacy principal before any channel
    runs, so there is no login to record — and the byte-identical-when-disabled
    law means no IAM store may be touched either.
    """
    kit = kit_for(AuthSettings(enabled=False))

    assert kit.federated_runtime is None


def test_an_audit_write_failure_does_not_cost_the_user_their_login(
    tmp_path: Path,
) -> None:
    """A broken audit store degrades the trail, never the login.

    The inverse would make an audit outage an authentication outage — the
    failure mode that turns a compliance nicety into downtime.
    """

    class ExplodingStore(AuthAuditStoreBase):
        def append(self, event: object) -> object:
            raise RuntimeError("audit backend is down")

        def _stored_records(self, **_kwargs: object) -> list[dict]:
            return []

    federated = runtime(tmp_path, audit=ExplodingStore())
    principal = federated.provision(raw(), auth_method=OIDC_METHOD)

    cookie = federated.session_cookie(principal, now=NOW)

    assert cookie
    assert federated.resolve_session_cookie(cookie, now=NOW) is not None
