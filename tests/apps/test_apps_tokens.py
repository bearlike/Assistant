"""Contract tests for the Mewbo Apps render-token signer + its config secret.

Covers the ``AppReadTokenSigner`` mint/verify round-trip and rotation isolation
(a token minted under one secret must not verify under another), plus the
``api.apps_token_secret`` config override / master-token fallback wiring
(``_build_apps_token_signer``) — the security-default half of Phase 1 that
lets an operator rotate app-token signing independently of the master token.
Every clock read is an injected NOW.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mewbo_api.apps.tokens import AppReadTokenSigner

NOW = datetime(2026, 7, 17, 9, 0, 0, tzinfo=timezone.utc)


class TestSignerRoundTrip:
    def test_mint_then_verify_round_trips(self):
        signer = AppReadTokenSigner(secret="apps-secret")
        token = signer.mint("app-x", now=NOW)
        verified = signer.verify(token.token_id, now=NOW)
        assert verified is not None
        assert verified.app_id == "app-x"

    def test_token_from_one_secret_does_not_verify_under_another(self):
        # The whole point of a dedicated apps_token_secret: rotating it invalidates
        # tokens signed by the old secret (here, the master-token fallback).
        minted = AppReadTokenSigner(secret="old-secret").mint("app-x", now=NOW)
        assert AppReadTokenSigner(secret="new-secret").verify(minted.token_id, now=NOW) is None


class TestWriteScope:
    """Phase 2: scope folded into the signed blob, backward compatible."""

    def test_mint_defaults_to_read_scope(self):
        signer = AppReadTokenSigner(secret="s")
        token = signer.mint("app-x", now=NOW)
        assert token.scope == "read"
        verified = signer.verify(token.token_id, now=NOW)
        assert verified is not None and verified.scope == "read"

    def test_mint_write_scope_round_trips(self):
        signer = AppReadTokenSigner(secret="s")
        token = signer.mint("app-x", now=NOW, scope="write")
        assert token.scope == "write"
        verified = signer.verify(token.token_id, now=NOW)
        assert verified is not None and verified.scope == "write"

    def test_five_part_blob_with_tampered_scope_fails_hmac(self):
        # Flipping "read" -> "write" in an otherwise-valid blob must NOT verify —
        # the scope is inside the signed message, not appended after the fact.
        signer = AppReadTokenSigner(secret="s")
        token = signer.mint("app-x", now=NOW, scope="read")
        app_id, exp_s, _scope, nonce, sig = token.token_id.split(":")
        tampered = f"{app_id}:{exp_s}:write:{nonce}:{sig}"
        assert signer.verify(tampered, now=NOW) is None

    def test_garbage_scope_segment_rejected(self):
        signer = AppReadTokenSigner(secret="s")
        token = signer.mint("app-x", now=NOW)
        app_id, exp_s, _scope, nonce, sig = token.token_id.split(":")
        # Re-sign under the bogus scope so this exercises the scope-literal
        # check specifically, not just another HMAC mismatch.
        message = f"{app_id}:{exp_s}:admin:{nonce}"
        forged = f"{message}:{signer._sign(message)}"
        assert signer.verify(forged, now=NOW) is None

    def test_legacy_four_part_blob_verifies_as_read(self):
        # A token minted by the PRE-hase-2 signer (no scope segment) must
        # still verify post-upgrade, always as "read" — the only scope that
        # existed before write tokens, so an outstanding token survives a
        # mid-flight deploy without a hard cutover.
        signer = AppReadTokenSigner(secret="s")
        exp = int(NOW.timestamp()) + 1800
        nonce = "abc123"
        message = f"app-x:{exp}:{nonce}"
        legacy_token_id = f"{message}:{signer._sign(message)}"
        verified = signer.verify(legacy_token_id, now=NOW)
        assert verified is not None
        assert verified.app_id == "app-x"
        assert verified.scope == "read"

    def test_legacy_blob_still_expires(self):
        signer = AppReadTokenSigner(secret="s")
        exp = int(NOW.timestamp()) - 1  # already expired
        message = f"app-x:{exp}:nonce1"
        legacy_token_id = f"{message}:{signer._sign(message)}"
        assert signer.verify(legacy_token_id, now=NOW) is None

    def test_malformed_part_count_rejected(self):
        signer = AppReadTokenSigner(secret="s")
        assert signer.verify("only:three:parts", now=NOW) is None
        assert signer.verify("way:too:many:colon:separated:parts:here", now=NOW) is None


class TestTokenSecretFallback:
    """`_build_apps_token_signer` config override vs master-token fallback."""

    def test_configured_secret_is_used_when_set(self):
        from mewbo_api.backend import _build_apps_token_signer

        signer = _build_apps_token_signer("dedicated-apps-secret", "master-token")
        token = signer.mint("app-x", now=NOW)
        # It signs with the configured secret, NOT the master token.
        assert AppReadTokenSigner(secret="dedicated-apps-secret").verify(
            token.token_id, now=NOW
        ) is not None
        assert AppReadTokenSigner(secret="master-token").verify(token.token_id, now=NOW) is None

    def test_falls_back_to_master_token_when_unset(self):
        from mewbo_api.backend import _build_apps_token_signer

        signer = _build_apps_token_signer("", "master-token")
        token = signer.mint("app-x", now=NOW)
        assert AppReadTokenSigner(secret="master-token").verify(token.token_id, now=NOW) is not None

    def test_whitespace_only_secret_falls_back(self):
        from mewbo_api.backend import _build_apps_token_signer

        signer = _build_apps_token_signer("   ", "master-token")
        token = signer.mint("app-x", now=NOW + timedelta(minutes=1))
        assert AppReadTokenSigner(secret="master-token").verify(
            token.token_id, now=NOW + timedelta(minutes=1)
        ) is not None
