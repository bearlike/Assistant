#!/usr/bin/env python3
"""``SamlSp`` — the ``[saml]`` extra's XML + crypto leg (python3-saml).

The kernel's ``SamlAuthenticator`` model is pure: an already-validated
assertion's attribute statement arrives as a ``Mapping`` and maps to a
``RawIdentity`` with no I/O. THIS module is the other half — it builds the SP
config from an authenticator, fetches/parses the IdP metadata (an INJECTED
fetcher, so the whole flow is testable offline), produces the AuthnRequest
redirect, and — the part that matters — validates an ACS POST: signature,
``Destination``/``Audience``/``InResponseTo``, timestamp, and replay. An
unsigned or wrongly-audienced assertion is REJECTED, never best-effort
accepted; that is this file's entire reason to exist.

**No self-guard here, by design.** This module imports ``onelogin.saml2.*``
unconditionally at the top, exactly like ``drivers/jwks.py`` imports
``joserfc`` and ``drivers/ldap.py`` imports ``ldap3`` — the actionable
"install the extra" ``ImportError`` is produced ONCE, centrally, by
``drivers/__init__.py``'s lazy ``__getattr__`` (PEP 562), which probes this
module's dependency before ever importing it. Importing ``mewbo_iam`` itself
never reaches this file.

**Two things python3-saml does NOT give us, made explicit:**

* Its own ``NotBefore``/``NotOnOrAfter`` check runs against the process wall
  clock with a fixed, non-injectable ~5-minute allowance — it cannot be
  bypassed via an injected ``now`` here, only SUPPLEMENTED. :meth:`SamlSp
  .validate_acs` re-checks the assertion's expiry against the caller's own
  clock as defense-in-depth and to make the expiry path unit-testable without
  patching system time.
* It has no replay defense at all — a captured, still-unexpired
  ``SAMLResponse`` would otherwise replay cleanly. :class:`_ReplayGuard`
  closes that with a bounded, TTL-pruned, in-memory set of consumed assertion
  IDs. **Known limitation, stated plainly:** this set is per PROCESS. A
  multi-worker deployment (gunicorn with more than one worker) does not share
  it, so a replay routed to a different worker than the original request
  within the assertion's validity window is NOT caught. Closing that
  requires a shared store (Redis/Mongo) and is out of scope for this phase.

**Bridge documentation (the lighter-weight alternative).** A SAML-only shop
that would rather not run a SAML SP inside Mewbo at all can front it with a
reverse proxy that already speaks SAML (a Shibboleth SP, SimpleSAMLphp, or an
auth-proxy sidecar) and terminate the result into the ALREADY-SHIPPED
``TrustedHeaderAuthenticator`` instead: the proxy performs the SAML handshake
and asserts the resulting identity via trusted headers, and this driver is
never exercised. That path needs no ``[saml]`` extra, no xmlsec, and no code
here — it is the right choice for an operator who already runs such a proxy.
This module exists for the deployments that want Mewbo itself to be the SP.

SP-side signing (AuthnRequest / SP metadata) is deliberately NOT implemented:
``SamlAuthenticator`` carries no SP private key, and an unsigned AuthnRequest
is spec-legal — most IdPs do not require one. Only the security-critical
direction, verifying the IdP's signature on the response/assertion, is
implemented.
"""

from __future__ import annotations

import threading
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from onelogin.saml2.authn_request import OneLogin_Saml2_Authn_Request
from onelogin.saml2.constants import OneLogin_Saml2_Constants
from onelogin.saml2.errors import OneLogin_Saml2_Error, OneLogin_Saml2_ValidationError
from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser
from onelogin.saml2.response import OneLogin_Saml2_Response
from onelogin.saml2.settings import OneLogin_Saml2_Settings
from onelogin.saml2.utils import OneLogin_Saml2_Utils

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mewbo_iam.authenticators import RawIdentity, SamlAuthenticator

# A URL → raw XML text fetch. The default hits the network with the stdlib
# (no extra dependency beyond python3-saml itself); a test injects a fake that
# returns canned IdP metadata — the whole validation path is then exercised
# with zero sockets.
MetadataFetch = Callable[[str], str]

_DEFAULT_HTTP_TIMEOUT = 10.0
_DEFAULT_METADATA_TTL = timedelta(hours=24)
_DEFAULT_CLOCK_SKEW = timedelta(seconds=90)
_DEFAULT_REPLAY_MAX_ENTRIES = 10_000
# Fallback replay-entry lifetime when an assertion carries no
# SubjectConfirmationData NotOnOrAfter to key eviction off of.
_DEFAULT_REPLAY_TTL = timedelta(minutes=10)


def _default_metadata_fetch(url: str) -> str:
    """GET *url* and return the response body as text.

    *url* is deployment config (``idp_metadata_url``), never end-user input, so
    an unguarded ``urlopen`` on it carries the same trust level as the rest of
    this file's operator-supplied settings.
    """
    request = urllib.request.Request(  # noqa: S310 - url is trusted deployment config
        url, headers={"Accept": "application/samlmetadata+xml, application/xml, text/xml"}
    )
    with urllib.request.urlopen(request, timeout=_DEFAULT_HTTP_TIMEOUT) as response:  # noqa: S310
        return response.read().decode("utf-8")


class SamlValidationError(Exception):
    """An ACS assertion failed validation.

    Signature, audience, destination, timestamp, replay, or shape. Raised,
    never swallowed into a best-effort accept: SAML's entire security model
    rests on every one of these checks holding.
    """


class SamlReplayError(SamlValidationError):
    """An assertion ID was already consumed — a replayed ``SAMLResponse``."""


@dataclass(frozen=True)
class SamlLoginStart:
    """The IdP redirect URL plus the AuthnRequest ID (for ``InResponseTo`` binding)."""

    redirect_url: str
    request_id: str


class _ReplayGuard:
    """A bounded, TTL-pruned, in-memory set of consumed SAML assertion IDs.

    Atomic class: ``max_entries`` is its only configuration; the seen-map and
    its lock are its state. **Process-local, by construction** — see this
    module's docstring for the multi-worker limitation this implies. Entries
    are pruned opportunistically (on every call) against their OWN expiry
    (the assertion's ``NotOnOrAfter``), so the set never grows past the
    number of distinct assertions seen within one validity window; the
    ``max_entries`` cap is a hard backstop against a pathological burst, not
    the normal eviction path.
    """

    def __init__(self, *, max_entries: int = _DEFAULT_REPLAY_MAX_ENTRIES) -> None:
        self._max_entries = max_entries
        self._seen: dict[str, datetime] = {}
        self._lock = threading.Lock()

    def check_and_remember(self, assertion_id: str, *, expires_at: datetime, now: datetime) -> bool:
        """Return ``True`` iff *assertion_id* is fresh (and remember it); ``False`` on replay."""
        with self._lock:
            self._prune(now)
            if assertion_id in self._seen:
                return False
            if len(self._seen) >= self._max_entries:
                soonest = min(self._seen, key=self._seen.__getitem__)
                del self._seen[soonest]
            self._seen[assertion_id] = expires_at
            return True

    def _prune(self, now: datetime) -> None:
        expired = [aid for aid, exp in self._seen.items() if exp <= now]
        for aid in expired:
            del self._seen[aid]


class SamlSp:
    """Builds SP/IdP settings for ONE ``SamlAuthenticator`` and drives the SP protocol.

    Atomic class: the authenticator, the injected metadata ``fetch``, the
    clock-skew tolerance and the replay guard are DI'd/owned fields; the
    fetched-and-parsed IdP metadata is cached with a TTL (mirrors
    ``DiscoveryCache``). Every per-call value that depends on the incoming
    request (the ACS URL, ``now``) arrives as a method argument — ONE
    long-lived instance serves every request for this authenticator.
    """

    def __init__(
        self,
        authenticator: SamlAuthenticator,
        *,
        metadata_fetch: MetadataFetch = _default_metadata_fetch,
        metadata_ttl: timedelta = _DEFAULT_METADATA_TTL,
        clock_skew: timedelta = _DEFAULT_CLOCK_SKEW,
        replay_max_entries: int = _DEFAULT_REPLAY_MAX_ENTRIES,
    ) -> None:
        """Capture the authenticator config and the injectable collaborators."""
        self._authenticator = authenticator
        self._metadata_fetch = metadata_fetch
        self._metadata_ttl = metadata_ttl
        self._clock_skew = clock_skew
        self._replay = _ReplayGuard(max_entries=replay_max_entries)
        self._idp_settings: dict[str, Any] | None = None
        self._idp_fetched_at: datetime | None = None
        self._cache_lock = threading.Lock()

    @property
    def authenticator(self) -> SamlAuthenticator:
        """The authenticator this driver instance serves."""
        return self._authenticator

    # ── SP metadata (no IdP fetch — available even while the IdP is down) ────
    def metadata_xml(self, *, acs_url: str) -> str:
        """The SP metadata XML an IdP administrator imports to register this SP."""
        settings_dict = {"strict": True, **self._sp_settings(acs_url=acs_url)}
        try:
            settings = OneLogin_Saml2_Settings(settings=settings_dict, sp_validation_only=True)
            metadata = settings.get_sp_metadata()
            errors = settings.validate_metadata(metadata)
        except OneLogin_Saml2_Error as exc:
            raise SamlValidationError(f"could not build SP metadata: {exc}") from exc
        if errors:
            raise SamlValidationError(
                f"generated SP metadata failed self-validation: {', '.join(errors)}"
            )
        return metadata.decode("utf-8") if isinstance(metadata, bytes) else metadata

    # ── SP-initiated login ────────────────────────────────────────────────────
    def begin_login(self, *, acs_url: str, relay_state: str, now: datetime) -> SamlLoginStart:
        """Build the AuthnRequest and return the IdP redirect URL + its request ID.

        Unsigned (see the module docstring: no SP key material is modelled
        yet) — spec-legal, and most IdPs do not require a signed request.
        """
        settings_dict = self._full_settings(acs_url=acs_url, now=now)
        try:
            settings = OneLogin_Saml2_Settings(settings=settings_dict, sp_validation_only=False)
        except OneLogin_Saml2_Error as exc:
            raise SamlValidationError(f"invalid SAML SP/IdP settings: {exc}") from exc
        authn_request = OneLogin_Saml2_Authn_Request(settings)
        parameters = {"SAMLRequest": authn_request.get_request(), "RelayState": relay_state}
        sso_url = settings_dict["idp"]["singleSignOnService"]["url"]
        redirect_url = OneLogin_Saml2_Utils.redirect(sso_url, parameters, {})
        return SamlLoginStart(redirect_url=redirect_url, request_id=authn_request.get_id())

    # ── ACS validation — the security-critical direction ─────────────────────
    def validate_acs(
        self,
        *,
        saml_response_b64: str,
        acs_url: str,
        request_id: str | None,
        now: datetime,
    ) -> RawIdentity:
        """Validate an ACS POST body and map it to a :class:`RawIdentity`.

        Signature, ``Destination``, ``Audience`` and ``InResponseTo`` are
        enforced by python3-saml's own ``is_valid`` (called with
        ``raise_exceptions=True`` so a failure carries the precise reason);
        expiry is RE-checked against the injected clock; the assertion ID is
        checked against the replay guard. Any failure raises
        :class:`SamlValidationError` — this method never returns a partial or
        best-effort identity.
        """
        settings_dict = self._full_settings(acs_url=acs_url, now=now)
        try:
            settings = OneLogin_Saml2_Settings(settings=settings_dict, sp_validation_only=False)
        except OneLogin_Saml2_Error as exc:
            raise SamlValidationError(f"invalid SAML SP/IdP settings: {exc}") from exc

        try:
            response = OneLogin_Saml2_Response(settings, saml_response_b64)
        except Exception as exc:  # noqa: BLE001 - malformed base64/XML, never a silent accept
            raise SamlValidationError("unreadable SAMLResponse") from exc

        request_data = self._request_data(acs_url=acs_url)
        try:
            valid = response.is_valid(request_data, request_id=request_id, raise_exceptions=True)
        except OneLogin_Saml2_ValidationError as exc:
            raise SamlValidationError(f"SAML response failed validation: {exc}") from exc
        except OneLogin_Saml2_Error as exc:
            raise SamlValidationError(f"SAML response could not be processed: {exc}") from exc
        if not valid:  # belt-and-suspenders; raise_exceptions=True should have raised already
            raise SamlValidationError(response.get_error() or "SAML response failed validation")

        self._check_expiry(response, now=now)
        assertion_id = self._check_replay(response, now=now)

        identity = self._authenticator.resolve(self._attributes(response))
        if identity is None:
            raise SamlValidationError(
                f"assertion {assertion_id} carried no usable subject (NameID)"
            )
        return identity

    # ── defense-in-depth checks ───────────────────────────────────────────────
    def _check_expiry(self, response: OneLogin_Saml2_Response, *, now: datetime) -> None:
        """Re-check the assertion's expiry against the INJECTED clock.

        Supplements, never replaces, python3-saml's own internal check (which
        runs against the system wall clock with its own fixed allowance — see
        the module docstring). This is what makes the expiry path
        unit-testable without patching system time.
        """
        not_on_or_after = response.get_assertion_not_on_or_after()
        if not not_on_or_after:
            return
        expires_at = datetime.fromtimestamp(not_on_or_after, tz=timezone.utc)
        if now > expires_at + self._clock_skew:
            raise SamlValidationError("assertion expired (NotOnOrAfter exceeded)")

    def _check_replay(self, response: OneLogin_Saml2_Response, *, now: datetime) -> str:
        """Guard against a replayed assertion ID; returns the ID for error messages."""
        assertion_id = response.get_assertion_id()
        if not assertion_id:
            raise SamlValidationError("assertion carried no ID; cannot guard against replay")
        not_on_or_after = response.get_assertion_not_on_or_after()
        expires_at = (
            datetime.fromtimestamp(not_on_or_after, tz=timezone.utc)
            if not_on_or_after
            else now + self._clock_skew + _DEFAULT_REPLAY_TTL
        )
        if not self._replay.check_and_remember(assertion_id, expires_at=expires_at, now=now):
            raise SamlReplayError(f"assertion {assertion_id} was already consumed (replay)")
        return assertion_id

    # ── attribute mapping ──────────────────────────────────────────────────────
    @staticmethod
    def _attributes(response: OneLogin_Saml2_Response) -> dict[str, Any]:
        """Flatten onelogin's ``{name: [values]}`` shape into what ``resolve`` expects.

        Scalar fields (subject/email/display) are read via ``str(value)`` on
        the authenticator side — a single-valued list must collapse to its
        element, or that call stringifies the whole list. Multi-valued
        attributes (groups) are left as a list; the authenticator's own
        group normalizer already accepts one. ``NameID`` is not an Attribute
        statement at all in SAML, so it is injected here under that key —
        the default ``subject_attribute`` the authenticator reads.
        """
        flattened: dict[str, Any] = {}
        for name, values in (response.get_attributes() or {}).items():
            flattened[name] = values[0] if isinstance(values, list) and len(values) == 1 else values
        flattened["NameID"] = response.get_nameid()
        return flattened

    # ── settings assembly ──────────────────────────────────────────────────────
    def _sp_settings(self, *, acs_url: str) -> dict[str, Any]:
        """The ``sp`` settings block — no IdP dependency, safe for metadata-only use."""
        return {
            "sp": {
                "entityId": self._authenticator.sp_entity_id,
                "assertionConsumerService": {
                    "url": acs_url,
                    "binding": OneLogin_Saml2_Constants.BINDING_HTTP_POST,
                },
                "NameIDFormat": OneLogin_Saml2_Constants.NAMEID_UNSPECIFIED,
            },
        }

    def _idp_metadata_settings(self, *, now: datetime) -> dict[str, Any]:
        """The parsed IdP metadata settings dict, fetched once and TTL-cached."""
        with self._cache_lock:
            if (
                self._idp_settings is not None
                and self._idp_fetched_at is not None
                and now - self._idp_fetched_at < self._metadata_ttl
            ):
                return self._idp_settings
        xml = self._resolve_metadata_xml()
        parsed = OneLogin_Saml2_IdPMetadataParser.parse(xml)
        with self._cache_lock:
            self._idp_settings = parsed
            self._idp_fetched_at = now
        return parsed

    def _resolve_metadata_xml(self) -> str:
        """The IdP metadata XML — inline config, or an injected-fetcher URL GET."""
        if self._authenticator.idp_metadata_xml is not None:
            return self._authenticator.idp_metadata_xml
        url = self._authenticator.idp_metadata_url
        if url is None:
            # Unreachable while SamlAuthenticator's exactly-one-source validator
            # holds. Raised rather than asserted: `python -O` strips asserts, and
            # this guards which IdP metadata the SP trusts.
            raise SamlValidationError(
                "SAML authenticator has neither idp_metadata_url nor idp_metadata_xml"
            )
        return self._metadata_fetch(url)

    def _full_settings(self, *, acs_url: str, now: datetime) -> dict[str, Any]:
        """SP settings merged with the (cached) IdP metadata, security overrides applied last."""
        base = {"strict": True, **self._sp_settings(acs_url=acs_url)}
        merged = OneLogin_Saml2_IdPMetadataParser.merge_settings(
            base, self._idp_metadata_settings(now=now)
        )
        merged["strict"] = True
        security = dict(merged.get("security", {}))
        # Never let IdP-supplied metadata relax the one check that matters most:
        # the assertion itself must carry a verified signature.
        security["wantAssertionsSigned"] = True
        merged["security"] = security
        return merged

    @staticmethod
    def _request_data(*, acs_url: str) -> dict[str, Any]:
        """The minimal ``request_data`` python3-saml needs for Destination checking."""
        parsed = urlparse(acs_url)
        return {
            "https": "on" if parsed.scheme == "https" else "off",
            "http_host": parsed.netloc,
            "script_name": parsed.path,
        }


__all__ = [
    "MetadataFetch",
    "SamlLoginStart",
    "SamlSp",
    "SamlValidationError",
    "SamlReplayError",
]
