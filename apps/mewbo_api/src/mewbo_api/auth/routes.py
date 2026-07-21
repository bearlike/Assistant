#!/usr/bin/env python3
"""The auth routes — ``/api/auth/{login,callback,logout,me}``.

An :class:`AuthRoutesController` (the ``TriggerRoutesController`` idiom: injected
collaborators as fields, every rule a method) drives the interactive OIDC login
legs and the ``/me`` profile. It is built ONCE at boot and closed over by the
Blueprint's view functions in :func:`init_auth_routes` — a LONG-LIVED controller,
not per-request, because the ``OidcRuntime`` it holds owns the JWKS/discovery
caches that must persist across requests. The request path still reads zero
mutable module state: the controller is injected via the closure.

Byte-identical-when-disabled: with auth off there is no ``OidcRuntime`` — the
login/callback/logout routes answer a clean "not configured" and set NO cookie,
and ``/me`` returns the legacy full-power principal shape so the console renders
unconditionally.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from flask import Blueprint, Flask, jsonify, make_response, redirect, request
from mewbo_core.common import get_logger
from mewbo_iam import AuthSettings, Principal
from pydantic import BaseModel, ConfigDict, ValidationError

from mewbo_api.auth.browser_surface import BrowserLoginSurface
from mewbo_api.auth.guard_registry import guard
from mewbo_api.auth.identity_flow import DisabledUserError
from mewbo_api.auth.kit import current_principal, utcnow
from mewbo_api.auth.oidc_runtime import CallbackError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from werkzeug.wrappers import Response

    from mewbo_api.auth.kit import AuthKit
    from mewbo_api.auth.oidc_runtime import OidcRuntime

logging = get_logger(name="mewbo-api.auth.routes")


def _parse_bool(value: str | None) -> bool:
    """Interpret a query flag as a boolean (mirrors backend's ``_parse_bool``)."""
    if not value:
        return False
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


class PasswordLoginBody(BaseModel):
    """The username/password login body — validated AT the trust boundary."""

    model_config = ConfigDict(extra="forbid")

    username: str
    password: str


class AuthRoutesController:
    """Serves the auth routes over the shared :class:`OidcRuntime`.

    Collaborators are DI'd fields; the clock is injected. Holds no mutable request
    state — one instance serves every request.
    """

    def __init__(
        self,
        *,
        oidc_runtime: OidcRuntime | None,
        settings: AuthSettings,
        kit: AuthKit,
        browser: BrowserLoginSurface,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        """Capture the OIDC runtime, settings, kit, browser surface, and clock."""
        self._oidc = oidc_runtime
        self._settings = settings
        self._kit = kit
        self._browser = browser
        self._clock = clock

    @classmethod
    def from_kit(
        cls, kit: AuthKit, *, clock: Callable[[], datetime] = utcnow
    ) -> AuthRoutesController:
        """Build the controller from the one AuthKit (shares its OIDC runtime)."""
        return cls(
            oidc_runtime=kit.oidc_runtime,
            settings=kit.settings,
            kit=kit,
            browser=BrowserLoginSurface(settings=kit.settings),
            clock=clock,
        )

    # ── POST /api/auth/login/password ────────────────────────────────────────
    @guard.public("the password login form itself; it MINTS the credential a guard would demand")
    def login_with_password(self) -> Response:
        """Verify directory credentials and set the session cookie (200), else 401."""
        kit = self._kit
        federated = kit.federated_runtime
        if not kit.password_login_enabled or federated is None:
            return make_response(jsonify({"message": "password login is not configured"}), 404)
        try:
            body = PasswordLoginBody.model_validate(request.get_json(silent=True) or {})
        except ValidationError:
            return make_response(jsonify({"message": "username and password are required"}), 400)
        principal = kit.login_with_password(body.username, body.password)
        if principal is None:
            # ONE message for every failure — no username oracle, no
            # "account disabled" disclosure. The audit trail carries the reason.
            return make_response(jsonify({"message": "Unauthorized"}), 401)
        response = make_response(jsonify(self._profile(principal)), 200)
        self._browser.set_session_cookie(
            response,
            federated.session_cookie_name,
            federated.session_cookie(principal, now=self._clock(), method="ldap"),
        )
        return response

    # ── GET /api/auth/login ──────────────────────────────────────────────────
    @guard.public("entry point of the browser login flow; no principal exists yet by definition")
    def login(self) -> Response:
        """Redirect to the identity provider (302), setting the login-state cookie."""
        runtime = self._oidc
        if runtime is None:
            return make_response(jsonify({"message": "OIDC login is not configured"}), 404)
        name = request.args.get("authenticator")
        authenticator = (
            runtime.authenticator_by_name(name) if name else runtime.default_authenticator()
        )
        if authenticator is None:
            return make_response(jsonify({"message": "no matching OIDC authenticator"}), 404)
        try:
            start = runtime.begin_login(
                authenticator,
                redirect_uri=self._callback_uri(),
                return_to=request.args.get("return_to", "/"),
                now=self._clock(),
            )
        except Exception:  # noqa: BLE001 - provider/discovery errors → clean 502
            logging.warning("failed to start OIDC login", exc_info=True)
            return make_response(jsonify({"message": "could not start login"}), 502)
        response = redirect(start.authorization_url)
        self._browser.set_login_state_cookie(
            response, runtime.state_cookie_name, start.state_cookie
        )
        return response

    # ── GET /api/auth/callback ───────────────────────────────────────────────
    @guard.public("identity-provider redirect back; the signed state cookie is the proof")
    def callback(self) -> Response:
        """Validate the callback, provision, set the session cookie, redirect home."""
        runtime = self._oidc
        if runtime is None:
            return make_response(jsonify({"message": "OIDC login is not configured"}), 404)
        from mewbo_iam.drivers import GroupsOverageError

        provider_error = request.args.get("error")
        if provider_error:
            logging.warning("identity provider returned an error: {}", provider_error)
            return self._browser.error_redirect("provider_error")
        try:
            result = runtime.complete_callback(
                state_cookie=request.cookies.get(runtime.state_cookie_name),
                state_param=request.args.get("state"),
                code=request.args.get("code"),
                redirect_uri=self._callback_uri(),
                now=self._clock(),
            )
        except GroupsOverageError as exc:
            # Operator-actionable, logged LOUD; the user sees a generic failure.
            logging.error("OIDC login blocked by a groups overage: {}", exc)
            return self._browser.error_redirect("groups_overage")
        except DisabledUserError:
            return self._browser.error_redirect("account_disabled")
        except CallbackError as exc:
            logging.warning("OIDC callback rejected: {}", exc)
            return self._browser.error_redirect("login_failed")
        except Exception:  # noqa: BLE001 - never 500 the callback; degrade to an error page
            logging.warning("OIDC callback error", exc_info=True)
            return self._browser.error_redirect("login_failed")
        response = redirect(result.return_to)
        self._browser.set_session_cookie(
            response,
            runtime.session_cookie_name,
            runtime.session_cookie(result.principal, now=self._clock()),
        )
        self._browser.clear_cookie(response, runtime.state_cookie_name)
        return response

    # ── POST /api/auth/logout ────────────────────────────────────────────────
    @guard.public("clearing one's own cookie is idempotent and must never itself 401")
    def logout(self) -> Response:
        """Clear the session cookie; optionally redirect to the IdP end-session URL."""
        runtime = self._oidc
        redirect_to: str | None = None
        if runtime is not None and _parse_bool(request.args.get("idp")):
            try:
                redirect_to = runtime.end_session_url(
                    id_token_hint=None,
                    post_logout_redirect_uri=self._browser.origin(),
                    now=self._clock(),
                )
            except Exception:  # noqa: BLE001 - end-session is best-effort
                logging.warning("failed to build IdP end-session URL", exc_info=True)
        if redirect_to:
            response = redirect(redirect_to)
        else:
            response = make_response(jsonify({"ok": True}), 200)
        if runtime is not None:
            self._browser.clear_cookie(response, runtime.session_cookie_name)
            self._browser.clear_cookie(response, runtime.state_cookie_name)
        return response

    # ── GET /api/auth/me ─────────────────────────────────────────────────────
    @guard.public("identity probe; must be reachable anonymously to answer its own 401")
    def me(self) -> Response:
        """Return the caller's profile (200); 401 only when auth is on and anonymous.

        With auth disabled, ``current_principal`` is the legacy full-power
        principal, so this returns that shape and the console renders as if a
        full-access user is signed in — the byte-identical-when-disabled contract.
        """
        principal = current_principal()
        if principal is None:
            return make_response(
                jsonify({"authenticated": False, "auth_enabled": self._settings.enabled}), 401
            )
        return make_response(jsonify(self._profile(principal)), 200)

    # ── serialization ────────────────────────────────────────────────────────
    def _profile(self, principal: Principal) -> dict:
        """The ``/me`` body: identity, roles, resolved permissions, teams, avatar.

        ``permissions`` is the RESOLVED set, not something a client could derive
        from ``roles``: a custom role's grants live only in the role store, so a
        client resolving names itself would both duplicate the catalog and miss
        every role an operator defines. Sending the resolved set is what keeps a
        surface the API accepts from being hidden by the console.
        """
        return {
            "authenticated": True,
            "auth_enabled": self._settings.enabled,
            "subject": principal.subject,
            "kind": principal.kind,
            "display": principal.display_name,
            "email": principal.email,
            "email_verified": principal.email_verified,
            "roles": list(principal.roles),
            "permissions": sorted(self._kit.permissions_for(principal)),
            "teams": [
                {"team_id": member.team_id, "team_role": member.team_role}
                for member in principal.team_memberships
            ],
            "avatar": self._avatar(principal),
            "auth_method": {
                "kind": principal.auth_method.kind,
                "issuer": principal.auth_method.issuer,
            },
            "scopes": None if principal.scopes is None else list(principal.scopes),
        }

    def _avatar(self, principal: Principal) -> dict:
        """The avatar chain: the IdP picture and a Gravatar URL (or null per policy).

        Both legs are reported SEPARATELY rather than collapsed through
        ``principal.avatar_url`` — the console falls back on its own, so it needs
        to see which of the two exists. The Gravatar URL itself is still built by
        the model, so this route cannot drift from the precedence resolver on the
        host or the fallback.
        """
        policy = self._oidc.avatar_policy if self._oidc is not None else self._settings.avatars
        return {
            "picture_url": principal.picture_url,
            "gravatar_url": principal.gravatar_url(policy),
        }

    # ── the one OIDC callback address ────────────────────────────────────────
    def _callback_uri(self) -> str:
        """The exact redirect URI (must match the IdP registration + the auth request)."""
        return self._browser.absolute_url("/api/auth/callback")


def init_auth_routes(app: Flask, controller: AuthRoutesController) -> None:
    """Register the auth Blueprint, closing over the one injected *controller*.

    Closure injection (not a module global): the view functions capture the
    long-lived controller, so the request path reads no mutable module state while
    the JWKS/discovery caches on the controller's runtime persist across requests.
    """
    blueprint = Blueprint("auth", __name__)
    blueprint.add_url_rule("/api/auth/login", view_func=controller.login, methods=["GET"])
    blueprint.add_url_rule(
        "/api/auth/login/password",
        view_func=controller.login_with_password,
        methods=["POST"],
    )
    blueprint.add_url_rule("/api/auth/callback", view_func=controller.callback, methods=["GET"])
    blueprint.add_url_rule("/api/auth/logout", view_func=controller.logout, methods=["POST"])
    blueprint.add_url_rule("/api/auth/me", view_func=controller.me, methods=["GET"])
    app.register_blueprint(blueprint)


__all__ = ["AuthRoutesController", "init_auth_routes"]
