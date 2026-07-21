"""Short-lived, render-scoped read/write tokens for a served app.

Spec §2.7 / §7; write scope + pipeline invocation added in Phase 2.

When the console/Aura opens an app, the platform mints a token carrying ONLY
the ``app_id`` and a scope, with a near-term expiry. The served frontend's
injected SDK presents it on the read-only data/system endpoints (and, with a
``write``-scoped token, the pipeline-invoke POST); the master key never enters
the browser/WebView.

The token is **stateless** — it needs no token store. It is an HMAC-SHA256 tag
(stdlib ``hmac``, no new dependency) over ``app_id:expiry:scope:token_id`` (see
below) keyed by a server secret, so the API can verify it by recomputing the
tag and comparing in constant time. Validity (expiry) is decided by
:meth:`AppReadToken.is_valid` on the model — the clock always arrives as a
method argument, per the strategy-on-model rule this package follows (see
``models.py``).

``AppReadTokenSigner`` is the one atomic home: the secret + default TTL are its
injected state; :meth:`mint` and :meth:`verify` are its behavior. Wire it with
the deployment's server secret (the API master token) at startup.

The token's ``token_id`` field IS the presented bearer credential (the cross-stream
contract the console/Aura/SDK landed against): the whole signed blob lives in
``token_id``, the client sends it back verbatim in the ``X-Mewbo-App-Token``
header, and :meth:`verify` re-derives ``app_id`` + expiry (+ scope) from it. So
there is no separate opaque token string — ``AppReadToken`` is fully
self-describing on the wire.

**Wire format (Phase 2 — additive, backward compatible).** A freshly
minted token is always 5 colon-separated parts:
``<app_id>:<exp_epoch>:<scope>:<nonce>:<sig>``. A token minted by the legacy
signer is 4 parts (no ``scope`` segment): ``<app_id>:<exp_epoch>:<nonce>:<sig>``
— :meth:`verify` still accepts it and treats it as ``scope="read"`` (the only
scope that ever existed before write tokens), so an outstanding token survives
a mid-flight deploy without a hard cutover. Both shapes stay unambiguous under
a plain ``split(":")`` because ``app_id`` is a colon-free slug, ``scope`` is
one of the fixed literals, and ``nonce``/``sig`` are colon-free (hex / urlsafe
base64).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from .models import AppReadToken

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .models import AppReadTokenScope

# Default lifetime of a render token — long enough to open + read an app, short
# enough that a leaked token expires quickly. Deployment-tunable via the ``ttl``
# constructor arg.
_DEFAULT_TTL = timedelta(minutes=30)


class AppReadTokenSigner:
    """Mints + verifies stateless, app-scoped read tokens (HMAC-SHA256).

    Atomic feature class: the signing ``secret`` and default ``ttl`` are its
    DI'd state; :meth:`mint`/:meth:`verify` are its behavior. No store, no
    module-level logic — a token is self-describing and self-verifying.

    The ``token_id`` IS the signed blob (the presented credential): format
    ``<app_id>:<exp_epoch>:<scope>:<nonce>:<sig>`` where ``sig`` is the
    urlsafe-base64 (unpadded) HMAC-SHA256 tag over
    ``<app_id>:<exp_epoch>:<scope>:<nonce>``. ``app_id`` is a colon-free slug,
    ``scope`` one of the fixed literals, and ``nonce`` is hex, so a plain
    ``split(":")`` recovers the five parts unambiguously. :meth:`verify` also
    accepts the legacy 4-part (no ``scope``) blob — see the module docstring.
    """

    def __init__(self, *, secret: str | bytes, ttl: timedelta = _DEFAULT_TTL) -> None:
        """Capture the server *secret* and the default token *ttl*."""
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        self._ttl = ttl

    def _sign(self, message: str) -> str:
        digest = hmac.new(self._secret, message.encode("utf-8"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def mint(
        self,
        app_id: str,
        *,
        now: datetime,
        ttl: timedelta | None = None,
        scope: AppReadTokenScope = "read",
    ) -> AppReadToken:
        """Mint an :class:`AppReadToken` whose ``token_id`` IS the signed credential.

        Expiry is truncated to whole seconds so the signed epoch and the model's
        ``expires_at`` describe the SAME instant a later :meth:`verify`
        reconstructs. The client presents ``token_id`` verbatim; the whole model
        (``{token_id, app_id, scope, expires_at}``) is the mint wire response.
        *scope* defaults to ``"read"`` (unchanged) — minting ``"write"`` is a
        route-level (master-key-only) decision, not this signer's to gate.
        """
        expires_at = now + (ttl if ttl is not None else self._ttl)
        exp = int(expires_at.timestamp())
        # Reconstruct the exact (second-truncated) instant we signed, so the
        # model's expiry matches the token's to the second.
        exp_dt = datetime.fromtimestamp(exp, tz=timezone.utc)
        nonce = uuid.uuid4().hex
        message = f"{app_id}:{exp}:{scope}:{nonce}"
        token_id = f"{message}:{self._sign(message)}"
        return AppReadToken(token_id=token_id, app_id=app_id, scope=scope, expires_at=exp_dt)

    def verify(self, token_id: str, *, now: datetime) -> AppReadToken | None:
        """Return the token's :class:`AppReadToken` if authentic + unexpired, else ``None``.

        *token_id* is the presented credential (the signed blob) — either the
        current 5-part ``scope``-carrying format or a legacy 4-part blob (no
        ``scope`` segment, treated as ``"read"`` — see the module docstring).
        Authenticity is a constant-time HMAC compare over the exact message that
        was signed; expiry is delegated to :meth:`AppReadToken.is_valid` (``now``
        as an argument, never the wall clock). A malformed, forged, or expired
        token all return ``None`` — the caller maps that to a uniform 401 with no
        oracle.
        """
        parts = token_id.split(":")
        if len(parts) == 5:
            app_id, exp_s, scope, nonce, sig = parts
            if scope not in ("read", "write"):
                return None
            message = f"{app_id}:{exp_s}:{scope}:{nonce}"
        elif len(parts) == 4:
            app_id, exp_s, nonce, sig = parts
            scope = "read"  # legacy blob, minted before write scope existed
            message = f"{app_id}:{exp_s}:{nonce}"
        else:
            return None
        if not hmac.compare_digest(sig, self._sign(message)):
            return None
        try:
            exp = int(exp_s)
        except ValueError:
            return None
        try:
            model = AppReadToken(
                token_id=token_id,
                app_id=app_id,
                scope=scope,  # type: ignore[arg-type]  # narrowed to the literal above
                expires_at=datetime.fromtimestamp(exp, tz=timezone.utc),
            )
        except (ValueError, OverflowError, OSError):
            return None
        return model if model.is_valid(now) else None


__all__ = ["AppReadTokenSigner"]
