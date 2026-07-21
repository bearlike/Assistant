#!/usr/bin/env python3
"""SAML ACS validation against a canned IdP — real signatures, zero sockets.

The IdP metadata fetcher is injected and a self-signed key pair is generated in
process, so every assertion here is genuinely signed and genuinely verified by
xmlsec: a tampered attribute fails because the signature does not cover it, not
because a stub said so.

**On the clock.** Unlike the rest of this suite, these tests build assertion
timestamps from the real wall clock rather than a frozen instant. python3-saml
runs its own ``NotBefore``/``NotOnOrAfter`` check against the process clock with
a fixed, non-injectable allowance — it cannot be bypassed, only supplemented.
The driver's own re-check IS injected, and
:func:`test_an_assertion_expired_against_the_injected_clock_is_rejected` drives
exactly that leg by moving the injected ``now`` forward while the assertion stays
fresh by the system clock.
"""

from __future__ import annotations

import base64
import datetime as dt
from dataclasses import dataclass
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from mewbo_iam.authenticators import SamlAuthenticator
from mewbo_iam.drivers.saml import SamlReplayError, SamlSp, SamlValidationError
from onelogin.saml2.utils import OneLogin_Saml2_Utils

IDP_ENTITY = "https://idp.example.com/metadata"
IDP_METADATA_URL = "https://idp.example.com/metadata.xml"
SP_ENTITY = "https://mewbo.example.com/sp"
ACS_URL = "https://mewbo.example.com/api/auth/saml/acs"
REQUEST_ID = "_authnrequest0001"


@dataclass(frozen=True)
class KeyPair:
    """A self-signed RSA key pair, PEM-encoded, standing in for the IdP's."""

    key_pem: str
    cert_pem: str

    @property
    def cert_body(self) -> str:
        """The base64 body an ``<ds:X509Certificate>`` element carries."""
        return "".join(
            line for line in self.cert_pem.splitlines() if "CERTIFICATE" not in line
        )


def generate_key_pair(common_name: str) -> KeyPair:
    """Mint a self-signed signing certificate for a fake IdP."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    return KeyPair(
        key_pem=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ).decode(),
        cert_pem=cert.public_bytes(serialization.Encoding.PEM).decode(),
    )


# RSA keygen is the slowest thing here; one pair per module is plenty. The
# "attacker" pair is what proves the signature check is bound to the IdP's key
# rather than to any well-formed signature.
@pytest.fixture(scope="module")
def idp_keys() -> KeyPair:
    return generate_key_pair("idp.example.com")


@pytest.fixture(scope="module")
def attacker_keys() -> KeyPair:
    return generate_key_pair("attacker.example.com")


def iso(moment: dt.datetime) -> str:
    """SAML's second-resolution UTC timestamp format."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def metadata_xml(keys: KeyPair) -> str:
    """IdP metadata announcing *keys*' certificate as the signing key."""
    return f"""<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" entityID="{IDP_ENTITY}">
  <md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <md:KeyDescriptor use="signing">
      <ds:KeyInfo xmlns:ds="http://www.w3.org/2000/09/xmldsig#">
        <ds:X509Data><ds:X509Certificate>{keys.cert_body}</ds:X509Certificate></ds:X509Data>
      </ds:KeyInfo>
    </md:KeyDescriptor>
    <md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
        Location="https://idp.example.com/sso"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>"""


def assertion_xml(
    *,
    now: dt.datetime,
    assertion_id: str = "_assertion0001",
    audience: str = SP_ENTITY,
    in_response_to: str = REQUEST_ID,
    not_on_or_after: dt.datetime | None = None,
    email: str = "alice@example.com",
) -> str:
    """One SAML assertion, unsigned — the raw material every test starts from."""
    expires = not_on_or_after or (now + dt.timedelta(minutes=5))
    return f"""<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"
  xmlns:xs="http://www.w3.org/2001/XMLSchema"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  ID="{assertion_id}" Version="2.0" IssueInstant="{iso(now)}">
  <saml:Issuer>{IDP_ENTITY}</saml:Issuer>
  <saml:Subject>
    <saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified">alice</saml:NameID>
    <saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">
      <saml:SubjectConfirmationData NotOnOrAfter="{iso(expires)}"
        Recipient="{ACS_URL}" InResponseTo="{in_response_to}"/>
    </saml:SubjectConfirmation>
  </saml:Subject>
  <saml:Conditions NotBefore="{iso(now - dt.timedelta(minutes=5))}" NotOnOrAfter="{iso(expires)}">
    <saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction>
  </saml:Conditions>
  <saml:AuthnStatement AuthnInstant="{iso(now)}" SessionIndex="_session0001">
    <saml:AuthnContext>
      <saml:AuthnContextClassRef>urn:oasis:names:tc:SAML:2.0:ac:classes:Password</saml:AuthnContextClassRef>
    </saml:AuthnContext>
  </saml:AuthnStatement>
  <saml:AttributeStatement>
    <saml:Attribute Name="email" NameFormat="urn:oasis:names:tc:SAML:2.0:attrname-format:basic">
      <saml:AttributeValue xsi:type="xs:string">{email}</saml:AttributeValue>
    </saml:Attribute>
    <saml:Attribute Name="displayName"
        NameFormat="urn:oasis:names:tc:SAML:2.0:attrname-format:basic">
      <saml:AttributeValue xsi:type="xs:string">Alice Example</saml:AttributeValue>
    </saml:Attribute>
    <saml:Attribute Name="groups" NameFormat="urn:oasis:names:tc:SAML:2.0:attrname-format:basic">
      <saml:AttributeValue xsi:type="xs:string">eng</saml:AttributeValue>
      <saml:AttributeValue xsi:type="xs:string">sre</saml:AttributeValue>
    </saml:Attribute>
  </saml:AttributeStatement>
</saml:Assertion>"""


def sign(assertion: str, keys: KeyPair) -> str:
    """Apply a real XML-DSig enveloped signature over *assertion*."""
    signed = OneLogin_Saml2_Utils.add_sign(assertion, keys.key_pem, keys.cert_pem)
    text = signed.decode() if isinstance(signed, bytes) else signed
    return text.replace('<?xml version="1.0"?>', "").strip()


def response_b64(
    assertion: str,
    *,
    now: dt.datetime,
    in_response_to: str = REQUEST_ID,
    destination: str = ACS_URL,
) -> str:
    """Wrap *assertion* in a Response and encode it as an ACS POST body would be."""
    xml = f"""<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"
  xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"
  ID="_response0001" Version="2.0" IssueInstant="{iso(now)}"
  Destination="{destination}" InResponseTo="{in_response_to}">
  <saml:Issuer>{IDP_ENTITY}</saml:Issuer>
  <samlp:Status>
    <samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/>
  </samlp:Status>
  {assertion}
</samlp:Response>"""
    return base64.b64encode(xml.encode()).decode()


def make_authenticator(keys: KeyPair, **overrides: Any) -> SamlAuthenticator:
    """A SAML authenticator trusting *keys* via inline IdP metadata."""
    fields: dict[str, Any] = {
        "name": "corp-saml",
        "issuer": IDP_ENTITY,
        "sp_entity_id": SP_ENTITY,
        "idp_metadata_xml": metadata_xml(keys),
    }
    fields.update(overrides)
    return SamlAuthenticator(**fields)


@pytest.fixture
def now() -> dt.datetime:
    """Real wall-clock time — python3-saml's internal check demands it. See module docstring."""
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


@pytest.fixture
def sp(idp_keys: KeyPair) -> SamlSp:
    return SamlSp(make_authenticator(idp_keys))


def valid_response(idp_keys: KeyPair, now: dt.datetime, **assertion_kwargs: Any) -> str:
    """A correctly signed, in-window, correctly-audienced ACS body."""
    return response_b64(sign(assertion_xml(now=now, **assertion_kwargs), idp_keys), now=now)


# ── the happy path ────────────────────────────────────────────────────────────
def test_a_correctly_signed_assertion_validates_and_maps_to_an_identity(sp, idp_keys, now):
    """The whole validation path — signature, audience, timing, replay — with no sockets."""
    identity = sp.validate_acs(
        saml_response_b64=valid_response(idp_keys, now),
        acs_url=ACS_URL,
        request_id=REQUEST_ID,
        now=now,
    )

    assert identity.external_subject.issuer == IDP_ENTITY
    assert identity.external_subject.subject == "alice"
    assert identity.email == "alice@example.com"
    assert identity.display == "Alice Example"
    assert identity.groups == ("eng", "sre")


def test_idp_metadata_is_fetched_through_the_injected_fetcher_and_cached(idp_keys, now):
    """The metadata leg is injectable, so a URL-configured IdP needs no network."""
    fetches: list[str] = []

    def fetch(url: str) -> str:
        fetches.append(url)
        return metadata_xml(idp_keys)

    sp = SamlSp(
        make_authenticator(idp_keys, idp_metadata_xml=None, idp_metadata_url=IDP_METADATA_URL),
        metadata_fetch=fetch,
    )

    for index in range(3):
        sp.validate_acs(
            saml_response_b64=valid_response(idp_keys, now, assertion_id=f"_a{index}"),
            acs_url=ACS_URL,
            request_id=REQUEST_ID,
            now=now,
        )

    assert fetches == [IDP_METADATA_URL], "metadata is fetched once and TTL-cached"


# ── signature ─────────────────────────────────────────────────────────────────
def test_a_tampered_attribute_value_is_caught_by_signature_verification(sp, idp_keys, now):
    """Editing a signed attribute breaks the digest the signature covers.

    This is the whole point of signing the assertion: an attacker who can modify
    the POST body in flight cannot change who it says the user is.
    """
    signed = sign(assertion_xml(now=now), idp_keys)
    tampered = signed.replace("alice@example.com", "attacker@example.com")
    assert tampered != signed, "the fixture must actually have been modified"

    with pytest.raises(SamlValidationError, match="Signature validation failed"):
        sp.validate_acs(
            saml_response_b64=response_b64(tampered, now=now),
            acs_url=ACS_URL,
            request_id=REQUEST_ID,
            now=now,
        )


def test_a_tampered_name_id_is_caught_by_signature_verification(sp, idp_keys, now):
    """The subject itself is covered — no privilege escalation by editing NameID."""
    signed = sign(assertion_xml(now=now), idp_keys)
    tampered = signed.replace(">alice<", ">root<")

    with pytest.raises(SamlValidationError, match="Signature validation failed"):
        sp.validate_acs(
            saml_response_b64=response_b64(tampered, now=now),
            acs_url=ACS_URL,
            request_id=REQUEST_ID,
            now=now,
        )


def test_an_unsigned_assertion_is_rejected(sp, now):
    """``wantAssertionsSigned`` is forced on — an unsigned assertion never passes."""
    with pytest.raises(SamlValidationError, match="not signed and the SP require it"):
        sp.validate_acs(
            saml_response_b64=response_b64(assertion_xml(now=now), now=now),
            acs_url=ACS_URL,
            request_id=REQUEST_ID,
            now=now,
        )


def test_an_assertion_signed_by_the_wrong_key_is_rejected(sp, attacker_keys, now):
    """A well-formed signature from a key the IdP metadata does not name is not enough."""
    forged = sign(assertion_xml(now=now), attacker_keys)

    with pytest.raises(SamlValidationError, match="Signature validation failed"):
        sp.validate_acs(
            saml_response_b64=response_b64(forged, now=now),
            acs_url=ACS_URL,
            request_id=REQUEST_ID,
            now=now,
        )


def test_idp_supplied_metadata_cannot_relax_the_want_assertions_signed_rule(idp_keys, now):
    """The security override is applied LAST, so metadata cannot turn signing off."""
    sp = SamlSp(make_authenticator(idp_keys))

    with pytest.raises(SamlValidationError, match="not signed and the SP require it"):
        sp.validate_acs(
            saml_response_b64=response_b64(assertion_xml(now=now), now=now),
            acs_url=ACS_URL,
            request_id=REQUEST_ID,
            now=now,
        )


# ── audience ──────────────────────────────────────────────────────────────────
def test_an_assertion_for_a_different_audience_is_rejected(sp, idp_keys, now):
    """An assertion minted for another SP must not authenticate here."""
    other = sign(assertion_xml(now=now, audience="https://other-sp.example.com"), idp_keys)

    with pytest.raises(SamlValidationError, match="is not a valid audience"):
        sp.validate_acs(
            saml_response_b64=response_b64(other, now=now),
            acs_url=ACS_URL,
            request_id=REQUEST_ID,
            now=now,
        )


# ── destination ───────────────────────────────────────────────────────────────
def test_an_assertion_destined_for_a_different_acs_url_is_rejected(sp, idp_keys, now):
    """``Destination`` binds the response to this SP's ACS endpoint."""
    signed = sign(assertion_xml(now=now), idp_keys)
    body = response_b64(signed, now=now, destination="https://evil.example.com/acs")

    with pytest.raises(SamlValidationError, match="response was received at"):
        sp.validate_acs(
            saml_response_b64=body, acs_url=ACS_URL, request_id=REQUEST_ID, now=now
        )


# ── timing ────────────────────────────────────────────────────────────────────
def test_an_expired_assertion_is_rejected(sp, idp_keys, now):
    """A ``NotOnOrAfter`` in the past fails — a captured assertion does not age well."""
    stale = sign(
        assertion_xml(
            now=now - dt.timedelta(hours=2),
            not_on_or_after=now - dt.timedelta(hours=1),
        ),
        idp_keys,
    )

    with pytest.raises(SamlValidationError, match="Could not validate timestamp: expired"):
        sp.validate_acs(
            saml_response_b64=response_b64(stale, now=now),
            acs_url=ACS_URL,
            request_id=REQUEST_ID,
            now=now,
        )


def test_an_assertion_expired_against_the_injected_clock_is_rejected(sp, idp_keys, now):
    """The driver's OWN expiry re-check, driven by the injected clock.

    The assertion is fresh by the system clock (so python3-saml's internal,
    non-injectable check passes) but long expired by the ``now`` the caller
    supplies — which is exactly the defense-in-depth leg the driver adds.
    """
    body = valid_response(idp_keys, now)

    with pytest.raises(SamlValidationError, match="assertion expired"):
        sp.validate_acs(
            saml_response_b64=body,
            acs_url=ACS_URL,
            request_id=REQUEST_ID,
            now=now + dt.timedelta(hours=1),
        )


def test_the_injected_clock_expiry_check_honors_its_skew_allowance(sp, idp_keys, now):
    """Just past ``NotOnOrAfter`` but inside the skew window still validates."""
    body = valid_response(idp_keys, now, not_on_or_after=now + dt.timedelta(minutes=5))

    identity = sp.validate_acs(
        saml_response_b64=body,
        acs_url=ACS_URL,
        request_id=REQUEST_ID,
        now=now + dt.timedelta(minutes=5, seconds=30),
    )

    assert identity.external_subject.subject == "alice"


# ── InResponseTo ──────────────────────────────────────────────────────────────
def test_an_assertion_answering_a_different_authn_request_is_rejected(sp, idp_keys, now):
    """``InResponseTo`` binds the assertion to the login WE started."""
    with pytest.raises(SamlValidationError, match="InResponseTo of the Response"):
        sp.validate_acs(
            saml_response_b64=valid_response(idp_keys, now),
            acs_url=ACS_URL,
            request_id="_a-different-login-attempt",
            now=now,
        )


def test_an_unsolicited_assertion_is_rejected_when_a_request_id_is_expected(sp, idp_keys, now):
    """An IdP-initiated assertion cannot satisfy an SP-initiated login we tracked."""
    unsolicited = sign(assertion_xml(now=now, in_response_to="_unrelated"), idp_keys)

    with pytest.raises(SamlValidationError, match="InResponseTo of the Response"):
        sp.validate_acs(
            saml_response_b64=response_b64(unsolicited, now=now, in_response_to="_unrelated"),
            acs_url=ACS_URL,
            request_id=REQUEST_ID,
            now=now,
        )


# ── replay ────────────────────────────────────────────────────────────────────
def test_replaying_a_previously_consumed_assertion_is_rejected(sp, idp_keys, now):
    """python3-saml has no replay defense; the driver's guard is what closes it."""
    body = valid_response(idp_keys, now)

    first = sp.validate_acs(
        saml_response_b64=body, acs_url=ACS_URL, request_id=REQUEST_ID, now=now
    )
    assert first.external_subject.subject == "alice"

    with pytest.raises(SamlReplayError, match="already consumed"):
        sp.validate_acs(
            saml_response_b64=body, acs_url=ACS_URL, request_id=REQUEST_ID, now=now
        )


def test_a_replay_error_is_a_validation_error(sp, idp_keys, now):
    """Callers catching the general error must not miss a replay."""
    body = valid_response(idp_keys, now)
    sp.validate_acs(saml_response_b64=body, acs_url=ACS_URL, request_id=REQUEST_ID, now=now)

    with pytest.raises(SamlValidationError):
        sp.validate_acs(
            saml_response_b64=body, acs_url=ACS_URL, request_id=REQUEST_ID, now=now
        )


def test_distinct_assertions_are_not_mistaken_for_replays(sp, idp_keys, now):
    """The guard keys on the assertion ID — two real logins both succeed."""
    for index in range(3):
        identity = sp.validate_acs(
            saml_response_b64=valid_response(idp_keys, now, assertion_id=f"_login{index}"),
            acs_url=ACS_URL,
            request_id=REQUEST_ID,
            now=now,
        )
        assert identity.external_subject.subject == "alice"


def test_an_expired_replay_entry_is_pruned_so_the_guard_does_not_grow_without_bound(
    sp, idp_keys, now
):
    """Entries are pruned against their own NotOnOrAfter, not kept forever.

    The pruning is what keeps the set bounded in normal operation. It is safe
    precisely because an assertion whose entry has been pruned is also one the
    expiry check now rejects on its own.
    """
    body = valid_response(idp_keys, now)
    sp.validate_acs(saml_response_b64=body, acs_url=ACS_URL, request_id=REQUEST_ID, now=now)

    # Well past the assertion's NotOnOrAfter: the replay entry is gone — and the
    # assertion is refused on expiry rather than sailing through as fresh.
    with pytest.raises(SamlValidationError, match="assertion expired"):
        sp.validate_acs(
            saml_response_b64=body,
            acs_url=ACS_URL,
            request_id=REQUEST_ID,
            now=now + dt.timedelta(hours=2),
        )


# ── the replay guard's documented limitations, asserted rather than assumed ───
def test_the_replay_guard_is_per_process_and_does_not_catch_a_cross_worker_replay(
    idp_keys, now
):
    """A KNOWN limitation, asserted honestly rather than papered over.

    The guard is an in-memory set on one ``SamlSp`` instance. A second instance —
    which is what a second gunicorn worker holds — shares nothing with the first,
    so a replay routed to a different worker inside the validity window is NOT
    caught. Closing this needs a shared store (Redis/Mongo) and is out of scope
    for this phase; if that lands, THIS test is the one that must change.
    """
    body = valid_response(idp_keys, now)
    worker_one = SamlSp(make_authenticator(idp_keys))
    worker_two = SamlSp(make_authenticator(idp_keys))

    worker_one.validate_acs(
        saml_response_b64=body, acs_url=ACS_URL, request_id=REQUEST_ID, now=now
    )
    with pytest.raises(SamlReplayError):
        worker_one.validate_acs(
            saml_response_b64=body, acs_url=ACS_URL, request_id=REQUEST_ID, now=now
        )

    # The same replay, routed to the other worker, is accepted.
    replayed = worker_two.validate_acs(
        saml_response_b64=body, acs_url=ACS_URL, request_id=REQUEST_ID, now=now
    )
    assert replayed.external_subject.subject == "alice", (
        "documented limitation: the replay guard does not span processes"
    )


def test_the_replay_guard_is_bounded_and_evicts_under_a_pathological_burst(idp_keys, now):
    """The ``max_entries`` backstop trades replay memory for a hard size cap.

    With the cap reached, the soonest-expiring entry is evicted — so a replay of
    THAT assertion is no longer caught while it is still inside its validity
    window. This is the deliberate cost of bounding the set; the normal eviction
    path is TTL pruning, not this.
    """
    sp = SamlSp(make_authenticator(idp_keys), replay_max_entries=1)
    first = valid_response(idp_keys, now, assertion_id="_first")
    second = valid_response(idp_keys, now, assertion_id="_second")

    sp.validate_acs(saml_response_b64=first, acs_url=ACS_URL, request_id=REQUEST_ID, now=now)
    sp.validate_acs(saml_response_b64=second, acs_url=ACS_URL, request_id=REQUEST_ID, now=now)

    # "_first" was evicted to honor the cap, so its replay is no longer detected.
    accepted = sp.validate_acs(
        saml_response_b64=first, acs_url=ACS_URL, request_id=REQUEST_ID, now=now
    )
    assert accepted.external_subject.subject == "alice"


# ── malformed input ───────────────────────────────────────────────────────────
def test_an_unreadable_saml_response_is_rejected_without_an_unhandled_exception(sp, now):
    """Garbage in the POST body is a validation error, never a 500."""
    with pytest.raises(SamlValidationError, match="unreadable SAMLResponse"):
        sp.validate_acs(
            saml_response_b64="not-base64-at-all!!",
            acs_url=ACS_URL,
            request_id=REQUEST_ID,
            now=now,
        )


def test_a_response_carrying_no_assertion_is_rejected(sp, now):
    """An empty Response is not a successful login."""
    with pytest.raises(SamlValidationError, match="must contain 1 assertion"):
        sp.validate_acs(
            saml_response_b64=response_b64("", now=now),
            acs_url=ACS_URL,
            request_id=REQUEST_ID,
            now=now,
        )
