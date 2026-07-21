#!/usr/bin/env python3
"""The drivers' default HTTP transport — thin JSON GET / form-POST over requests.

Every driver takes its fetcher as an injected callable and defaults it to a bound
method of the module-level :data:`_http` instance. Keeping the transport a
single, overridable seam is what lets the pure verifiers be exercised without a
network: a test injects a fake that returns a canned discovery/JWKS/introspection
payload, and production wires these. Both verbs raise on a non-2xx or a
non-object body, so a caller never has to guess whether a response is usable.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import requests

# Bounded so a wedged IdP endpoint fails the login/verify leg promptly rather
# than tying up a gunicorn worker thread on an open socket.
_DEFAULT_TIMEOUT = 10.0


class JsonHttp:
    """The drivers' JSON transport — a GET and a form-POST, both object-checked.

    Atomic class: the request timeout is its only state, and the shared
    ``_object_body`` guard is the single place a response is admitted. Every
    per-call value (URL, form body, basic auth) arrives as a method argument, so
    ONE instance serves every driver.
    """

    def __init__(self, *, timeout: float = _DEFAULT_TIMEOUT) -> None:
        """Capture the per-request network timeout."""
        self._timeout = timeout

    def get(self, url: str) -> dict[str, Any]:
        """GET *url* and return the parsed JSON object."""
        response = requests.get(
            url, timeout=self._timeout, headers={"Accept": "application/json"}
        )
        return self._object_body(response, url)

    def post_form(
        self, url: str, data: Mapping[str, str], *, auth: tuple[str, str] | None = None
    ) -> dict[str, Any]:
        """POST a form body to *url* (optional HTTP Basic *auth*) and return JSON."""
        response = requests.post(
            url,
            data=dict(data),
            auth=auth,
            timeout=self._timeout,
            headers={"Accept": "application/json"},
        )
        return self._object_body(response, url)

    @staticmethod
    def _object_body(response: requests.Response, url: str) -> dict[str, Any]:
        """Raise on a non-2xx or a non-object body; otherwise return the object."""
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError(f"expected a JSON object from {url}, got {type(body).__name__}")
        return body


# The default transport every driver falls back to. Kept module-level so a
# driver's parameter default stays a plain callable — that injected-callable
# seam is what makes the verifiers testable without a network.
_http = JsonHttp()

http_get_json = _http.get
http_post_form = _http.post_form


__all__ = ["JsonHttp", "http_get_json", "http_post_form"]
