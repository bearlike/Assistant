#!/usr/bin/env python3
"""The OIDC relying-party legs, driven against the REAL authlib session.

This suite exists because the opposite approach missed a live defect. Every other
driver test injects its transport and asserts on what the driver passed to it —
which proves the driver's own arithmetic and nothing about whether the library
accepts it. `OidcClient` has no injectable session by design (authlib IS the
client), so a fake here would leave the production path unexecuted, which is
exactly how the authorize URL came to carry no `code_challenge` while the
docstring promised one.

So: `authorization_url` and `end_session_url` are pure and run for real, and the
token exchange runs against a local HTTP server rather than a mocked session. The
socket is the boundary that gets stubbed, never authlib.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from mewbo_iam.authenticators import OidcAuthenticator
from mewbo_iam.drivers.discovery import DiscoveryCache
from mewbo_iam.drivers.oidc_client import OidcClient, OidcExchangeError

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
ISSUER = "https://idp.example.com"
REDIRECT_URI = "https://mewbo.example.com/auth/callback"
CODE_VERIFIER = "v" * 64


def discovery_document(**overrides: Any) -> dict[str, Any]:
    """A provider metadata document with every endpoint this client consumes."""
    document: dict[str, Any] = {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks",
        "userinfo_endpoint": f"{ISSUER}/userinfo",
        "end_session_endpoint": f"{ISSUER}/logout",
    }
    document.update(overrides)
    return document


def make_authenticator(**overrides: Any) -> OidcAuthenticator:
    """An OIDC authenticator pointed at the fake provider."""
    fields: dict[str, Any] = {
        "name": "corp-idp",
        "issuer": ISSUER,
        "discovery_url": f"{ISSUER}/.well-known/openid-configuration",
        "client_id": "mewbo-client",
        "client_secret": "client-secret",
    }
    fields.update(overrides)
    return OidcAuthenticator(**fields)


def make_client(document: dict[str, Any] | None = None) -> OidcClient:
    """A client whose discovery is canned — only the discovery GET is stubbed."""
    cache = DiscoveryCache(fetch=lambda url: document or discovery_document())
    return OidcClient(discovery=cache)


def query_of(url: str) -> dict[str, list[str]]:
    """The parsed query string of *url*."""
    return parse_qs(urlparse(url).query)


# ── PKCE: the check that a fake session cannot make ───────────────────────────
def test_the_authorize_url_carries_an_s256_code_challenge() -> None:
    """PKCE must reach the wire, not merely be intended.

    authlib attaches the challenge only when the SESSION was built with
    `code_challenge_method="S256"` — passing `code_verifier` to
    `create_authorization_url` is NOT enough on its own. A test asserting that
    the client forwarded the verifier would pass against a URL carrying no
    challenge at all, so this asserts on the emitted query instead.
    """
    url = make_client().authorization_url(
        make_authenticator(),
        redirect_uri=REDIRECT_URI,
        state="state-123",
        nonce="nonce-456",
        code_verifier=CODE_VERIFIER,
        now=NOW,
    )

    query = query_of(url)
    assert query["code_challenge_method"] == ["S256"]
    challenge = query["code_challenge"][0]
    assert challenge, "a code_challenge must be present"
    assert CODE_VERIFIER not in url, "the raw verifier must never ride the authorize URL"


def test_the_authorize_url_carries_state_nonce_and_the_code_response_type() -> None:
    """CSRF (`state`), replay (`nonce`) and the code flow itself."""
    url = make_client().authorization_url(
        make_authenticator(),
        redirect_uri=REDIRECT_URI,
        state="state-123",
        nonce="nonce-456",
        code_verifier=CODE_VERIFIER,
        now=NOW,
    )

    query = query_of(url)
    assert query["state"] == ["state-123"]
    assert query["nonce"] == ["nonce-456"]
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["mewbo-client"]
    assert query["redirect_uri"] == [REDIRECT_URI]


def test_the_configured_scopes_reach_the_authorize_url() -> None:
    """Scopes are joined into the single space-delimited `scope` parameter."""
    url = make_client().authorization_url(
        make_authenticator(scopes=("openid", "email", "groups")),
        redirect_uri=REDIRECT_URI,
        state="s",
        nonce="n",
        code_verifier=CODE_VERIFIER,
        now=NOW,
    )

    assert query_of(url)["scope"] == ["openid email groups"]


# ── the token exchange, against a real socket ─────────────────────────────────
class RecordingProvider:
    """A minimal token endpoint that records the form body it was POSTed.

    Stands in for the provider at the SOCKET, so authlib genuinely builds and
    sends the request — the part a mocked session would skip.
    """

    def __init__(self, response: dict[str, Any], status: int = 200) -> None:
        self.response = response
        self.status = status
        self.forms: list[dict[str, list[str]]] = []
        self.auth_headers: list[str | None] = []
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
                length = int(self.headers.get("Content-Length", 0))
                recorder.forms.append(parse_qs(self.rfile.read(length).decode()))
                recorder.auth_headers.append(self.headers.get("Authorization"))
                body = json.dumps(recorder.response).encode()
                self.send_response(recorder.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                """Silence the default stderr access log."""

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}/token"

    def __enter__(self) -> RecordingProvider:
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._server.shutdown()
        self._server.server_close()


def test_the_code_exchange_sends_the_pkce_verifier_and_returns_the_tokens() -> None:
    """The verifier travels on the EXCHANGE, where the provider re-derives the challenge."""
    tokens = {
        "access_token": "at-1",
        "id_token": "it-1",
        "token_type": "Bearer",
        "expires_in": 3600,
    }
    with RecordingProvider(tokens) as provider:
        client = make_client(discovery_document(token_endpoint=provider.url))

        bundle = client.exchange_code(
            make_authenticator(),
            code="auth-code-1",
            redirect_uri=REDIRECT_URI,
            code_verifier=CODE_VERIFIER,
            now=NOW,
        )

        assert bundle.id_token == "it-1"
        assert bundle.access_token == "at-1"
        assert bundle.token_type == "Bearer"
        assert bundle.raw["expires_in"] == 3600

        form = provider.forms[0]
        assert form["code"] == ["auth-code-1"]
        assert form["code_verifier"] == [CODE_VERIFIER], "PKCE proof must reach the provider"
        assert form["grant_type"] == ["authorization_code"]


def test_a_rejected_exchange_raises_the_normalized_error() -> None:
    """authlib raises a wide family; the driver presents one type to its caller."""
    with RecordingProvider({"error": "invalid_grant"}, status=400) as provider:
        client = make_client(discovery_document(token_endpoint=provider.url))

        with pytest.raises(OidcExchangeError):
            client.exchange_code(
                make_authenticator(),
                code="stale-code",
                redirect_uri=REDIRECT_URI,
                code_verifier=CODE_VERIFIER,
                now=NOW,
            )


# ── RP-initiated logout ───────────────────────────────────────────────────────
def test_logout_returns_none_when_the_provider_advertises_no_end_session_endpoint() -> None:
    """No endpoint means the caller just clears its own cookie — not a failure."""
    document = discovery_document()
    del document["end_session_endpoint"]

    url = make_client(document).end_session_url(
        make_authenticator(),
        id_token_hint="it-1",
        post_logout_redirect_uri="https://mewbo.example.com/",
        now=NOW,
    )

    assert url is None


def test_logout_appends_its_parameters_to_an_endpoint_that_already_has_a_query() -> None:
    """The separator must become `&`, or the endpoint's own query is corrupted."""
    document = discovery_document(end_session_endpoint=f"{ISSUER}/logout?tenant=acme")

    url = make_client(document).end_session_url(
        make_authenticator(),
        id_token_hint="it-1",
        post_logout_redirect_uri=None,
        now=NOW,
    )

    assert url is not None
    query = query_of(url)
    assert query["tenant"] == ["acme"], "the endpoint's own query survived"
    assert query["id_token_hint"] == ["it-1"]
