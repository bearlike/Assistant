#!/usr/bin/env python3
"""The SAML SP routes — ``/api/auth/saml/{metadata,login,acs}``.

A :class:`SamlRoutesController` (the ``AuthRoutesController`` idiom: injected
collaborators as fields, every rule a method) drives the SAML SP legs. Built
ONCE at boot and closed over by the Blueprint's view functions — a LONG-LIVED
controller, not per-request, because the ``SamlRuntime`` it holds owns the
per-authenticator IdP-metadata caches that must persist across requests.

Byte-identical-when-disabled: with no SAML authenticator configured, every
route answers a clean 404 and sets no cookie.

Never logs an assertion or its attributes — only the (already
attribute-free) structural failure reason ``SamlCallbackError``/
``SamlValidationError`` carry.

**Lighter-weight alternative.** A SAML-only shop that already runs a
SAML-speaking reverse proxy (Shibboleth SP, SimpleSAMLphp, an auth-proxy
sidecar) can terminate the handshake there and assert the result through the
already-shipped ``TrustedHeaderAuthenticator`` instead of these routes —
see ``mewbo_iam.drivers.saml``'s module docstring for the full trade-off.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from flask import Blueprint, Flask, jsonify, make_response, redirect, request
from mewbo_core.common import get_logger
from mewbo_iam import AuthSettings

from mewbo_api.auth.browser_surface import BrowserLoginSurface
from mewbo_api.auth.guard_registry import guard
from mewbo_api.auth.identity_flow import DisabledUserError
from mewbo_api.auth.kit import utcnow
from mewbo_api.auth.saml_runtime import SamlCallbackError, SamlResponseMissingError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mewbo_iam.authenticators import SamlAuthenticator
    from werkzeug.wrappers import Response

    from mewbo_api.auth.kit import AuthKit
    from mewbo_api.auth.saml_runtime import SamlRuntime

logging = get_logger(name="mewbo-api.auth.saml.routes")


class SamlRoutesController:
    """Serves the SAML SP routes over the shared :class:`SamlRuntime`.

    Collaborators are DI'd fields; the clock is injected. Holds no mutable
    request state — one instance serves every request.
    """

    def __init__(
        self,
        *,
        saml_runtime: SamlRuntime | None,
        settings: AuthSettings,
        browser: BrowserLoginSurface,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        """Capture the SAML runtime, the settings, the browser surface, and the clock."""
        self._saml = saml_runtime
        self._settings = settings
        self._browser = browser
        self._clock = clock

    @classmethod
    def from_kit(
        cls, kit: AuthKit, *, clock: Callable[[], datetime] = utcnow
    ) -> SamlRoutesController:
        """Build the controller from the one AuthKit (shares its SAML runtime).

        Reads ``kit.saml_runtime``, the same lazily-built property the OIDC
        controller takes its runtime from, so both kinds provision through the
        one federated runtime and mint the one session cookie.
        """
        return cls(
            saml_runtime=kit.saml_runtime,
            settings=kit.settings,
            browser=BrowserLoginSurface(settings=kit.settings),
            clock=clock,
        )

    # ── GET /api/auth/saml/metadata ──────────────────────────────────────────
    @guard.public("SP metadata is published to identity-provider administrators by design")
    def metadata(self) -> Response:
        """SP metadata XML — hand this endpoint's output to the IdP administrator."""
        if self._saml is None:
            return make_response(jsonify({"message": "SAML login is not configured"}), 404)
        authenticator = self._authenticator_or_none()
        if authenticator is None:
            return make_response(jsonify({"message": "no matching SAML authenticator"}), 404)
        from mewbo_iam.drivers import SamlValidationError

        try:
            xml = self._saml.metadata_xml(authenticator, acs_url=self._acs_uri())
        except SamlValidationError as exc:
            logging.warning("failed to build SAML SP metadata: {}", exc)
            return make_response(jsonify({"message": "could not build SP metadata"}), 502)
        response = make_response(xml, 200)
        response.headers["Content-Type"] = "application/samlmetadata+xml"
        return response

    # ── GET /api/auth/saml/login ─────────────────────────────────────────────
    @guard.public("entry point of the browser login flow; no principal exists yet by definition")
    def login(self) -> Response:
        """Redirect to the identity provider (302), setting the login-state cookie.

        RelayState carries only an opaque id — never the ``return_to`` path
        itself, which rides the signed state cookie instead (see
        ``SamlRuntime.begin_login``).
        """
        if self._saml is None:
            return make_response(jsonify({"message": "SAML login is not configured"}), 404)
        authenticator = self._authenticator_or_none()
        if authenticator is None:
            return make_response(jsonify({"message": "no matching SAML authenticator"}), 404)
        try:
            start = self._saml.begin_login(
                authenticator,
                acs_url=self._acs_uri(),
                return_to=request.args.get("return_to", "/"),
                now=self._clock(),
            )
        except Exception:  # noqa: BLE001 - IdP metadata fetch/parse errors → clean 502
            logging.warning("failed to start SAML login", exc_info=True)
            return make_response(jsonify({"message": "could not start login"}), 502)
        response = redirect(start.redirect_url)
        self._browser.set_login_state_cookie(
            response, self._saml.state_cookie_name, start.state_cookie
        )
        return response

    # ── POST /api/auth/saml/acs ──────────────────────────────────────────────
    @guard.public("identity-provider assertion POST-back; the signed assertion is the proof")
    def acs(self) -> Response:
        """Validate the assertion, provision, set the session cookie, redirect."""
        if self._saml is None:
            return make_response(jsonify({"message": "SAML login is not configured"}), 404)
        try:
            result = self._saml.complete_acs(
                state_cookie=request.cookies.get(self._saml.state_cookie_name),
                saml_response_b64=request.form.get("SAMLResponse"),
                relay_state=request.form.get("RelayState"),
                acs_url=self._acs_uri(),
                now=self._clock(),
            )
        except DisabledUserError:
            return self._browser.error_redirect("account_disabled")
        except SamlResponseMissingError as exc:
            # Nothing arrived to validate — analogous to OIDC's provider-side
            # ``?error=``. Never logs assertion content (there isn't any).
            logging.warning("SAML ACS: {}", exc)
            return self._browser.error_redirect("provider_error")
        except SamlCallbackError as exc:
            # A structural failure reason only (bad state/RelayState/unknown
            # authenticator/signature/audience/expiry/replay) — never the
            # assertion or its attributes.
            logging.warning("SAML ACS rejected: {}", exc)
            return self._browser.error_redirect("login_failed")
        except Exception:  # noqa: BLE001 - never 500 the ACS leg
            logging.warning("SAML ACS error", exc_info=True)
            return self._browser.error_redirect("login_failed")
        response = redirect(result.return_to)
        self._browser.set_session_cookie(
            response,
            self._saml.session_cookie_name,
            self._saml.session_cookie(result.principal, now=self._clock()),
        )
        self._browser.clear_cookie(response, self._saml.state_cookie_name)
        return response

    # ── authenticator lookup ─────────────────────────────────────────────────
    def _authenticator_or_none(self) -> SamlAuthenticator | None:
        """The requested (``?authenticator=``) or default SAML authenticator."""
        assert self._saml is not None
        name = request.args.get("authenticator")
        if name:
            return self._saml.authenticator_by_name(name)
        return self._saml.default_authenticator()

    # ── the one ACS address ──────────────────────────────────────────────────
    def _acs_uri(self) -> str:
        """The exact ACS URL (must match the IdP registration + the AuthnRequest)."""
        return self._browser.absolute_url("/api/auth/saml/acs")


def init_saml_routes(app: Flask, controller: SamlRoutesController) -> None:
    """Register the SAML Blueprint, closing over the one injected *controller*.

    Closure injection (not a module global): the view functions capture the
    long-lived controller, so the request path reads no mutable module state
    while the IdP-metadata caches on the controller's runtime persist across
    requests.
    """
    blueprint = Blueprint("auth_saml", __name__)
    blueprint.add_url_rule(
        "/api/auth/saml/metadata", view_func=controller.metadata, methods=["GET"]
    )
    blueprint.add_url_rule("/api/auth/saml/login", view_func=controller.login, methods=["GET"])
    blueprint.add_url_rule("/api/auth/saml/acs", view_func=controller.acs, methods=["POST"])
    app.register_blueprint(blueprint)


__all__ = ["SamlRoutesController", "init_saml_routes"]
