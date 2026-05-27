"""Token pass-through authentication for the MCP server.

The MCP facade is *curation*, not a security boundary — keys go only to
trusted agents. Each tool call carries the caller's own Bearer token in the
incoming HTTP ``Authorization`` header. We validate that token locally (via
the shared :class:`KeyStore`, or against the master token) and then forward
the *same* token to the REST API as ``X-API-Key``. There is no privileged
service identity and the master token is never placed on the wire by us.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import Context
from mewbo_core.config import get_config_value
from mewbo_core.key_store import KeyStoreBase, create_key_store


class AuthError(Exception):
    """Raised when a caller's Bearer token is missing or invalid."""


def _master_token() -> str:
    """Return the configured master token (env override wins, as in the API)."""
    return os.environ.get("MASTER_API_TOKEN") or str(
        get_config_value("api", "master_token", default="msk-strong-password")
    )


def extract_bearer_token(ctx: Context) -> str:
    """Pull the Bearer token from the incoming MCP HTTP request.

    Accepts the standard ``Authorization: Bearer <token>`` header. Raises
    :class:`AuthError` when the header is absent or malformed.
    """
    request = getattr(ctx.request_context, "request", None)
    headers = getattr(request, "headers", None)
    if headers is None:
        raise AuthError("No HTTP request context available for this call.")
    raw = headers.get("authorization") or headers.get("Authorization")
    if not raw:
        raise AuthError("Missing Authorization header.")
    parts = raw.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise AuthError("Authorization header must be 'Bearer <token>'.")
    return parts[1].strip()


def validate_token(token: str, *, key_store: KeyStoreBase | None = None) -> None:
    """Validate *token* locally; raise :class:`AuthError` if it is not valid.

    A token is valid when it equals the master token (break-glass) OR matches
    a non-revoked stored key via the shared :class:`KeyStore`. This mirrors
    the REST API's ``_require_api_key`` so the MCP server rejects bad tokens
    before ever issuing a downstream request.
    """
    if not token:
        raise AuthError("Empty token.")
    if token == _master_token():
        return
    store = key_store if key_store is not None else create_key_store()
    if store.verify_key(token) is not None:
        return
    raise AuthError("Unauthorized: token is not a valid Mewbo API key.")


def authenticate(ctx: Context, *, key_store: KeyStoreBase | None = None) -> str:
    """Extract and validate the caller's token, returning it for pass-through.

    The returned plaintext is forwarded verbatim to REST as ``X-API-Key``.
    """
    token = extract_bearer_token(ctx)
    validate_token(token, key_store=key_store)
    return token


__all__ = ["AuthError", "authenticate", "extract_bearer_token", "validate_token"]
