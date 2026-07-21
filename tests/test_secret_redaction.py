"""Tests for the log secret redactor and its loguru sink seam.

Fixture secret values are OBVIOUSLY fake (repeated ``FAKE``/single-char runs) so
no realistic-looking credential ever enters the tracked tree, while still
matching the vendor token shapes the redactor recognises.
"""

import io
import logging

import pytest
from loguru import logger
from mewbo_core.secret_redaction import (
    REDACTION_ERROR_MARKER,
    SecretRedactor,
    install_log_redaction,
    install_stdlib_log_bridge,
)

# --- fixture values ------------------------------------------------------

FAKE_SECRET = "FAKE" * 10  # 40 chars, no recognisable shape
FAKE_GHP = "ghp_" + "A" * 36  # GitHub classic PAT shape (40 chars)
FAKE_FINE_PAT = "github_pat_" + "B" * 30
FAKE_SK = "sk-" + "C" * 40  # OpenAI / Anthropic-style key
FAKE_SLACK = "xoxb-" + "1" * 24
FAKE_AWS = "AKIA" + "D" * 16
FAKE_GOOGLE = "AIza" + "E" * 35
FAKE_JWT = "eyJ" + "A" * 20 + "." + "B" * 20 + "." + "C" * 20


@pytest.fixture
def redactor() -> SecretRedactor:
    return SecretRedactor()


# --- key-name heuristic --------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "api_key",
        "API_KEY",
        "apiKey",
        "apikey",
        "access_token",
        "authorization",
        "Authorization",
        "password",
        "passwd",
        "client_secret",
        "clientSecret",
        "BEARER_TOKEN",
        "credential",
        "credentials",
        "private_key",
        "GITHUB_TOKEN",
        "x-api-key",
        "Cookie",
        "Set-Cookie",
    ],
)
def test_sensitive_keys_are_redacted(redactor: SecretRedactor, key: str) -> None:
    out = redactor.redact_mapping({key: FAKE_SECRET})
    assert out[key] == "<redacted len=40>"
    assert FAKE_SECRET not in str(out)


@pytest.mark.parametrize(
    "key",
    ["monkey", "keyboard", "turkey", "name", "session_id", "count", "username", "author"],
)
def test_benign_keys_pass_through(redactor: SecretRedactor, key: str) -> None:
    """A key whose name merely contains 'key' as part of a word is not secret."""
    out = redactor.redact_mapping({key: "plain-value"})
    assert out[key] == "plain-value"


# --- value-shape heuristic -----------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [FAKE_GHP, FAKE_FINE_PAT, FAKE_SK, FAKE_SLACK, FAKE_AWS, FAKE_GOOGLE, FAKE_JWT],
)
def test_token_shapes_are_redacted_in_free_text(redactor: SecretRedactor, secret: str) -> None:
    """A known token shape is scrubbed even under a non-secret key name."""
    message = f"connecting with credential {secret} now"
    out = redactor.redact_text(message)
    assert secret not in out
    assert f"<redacted len={len(secret)}>" in out


def test_bearer_scheme_keeps_word_redacts_credential(redactor: SecretRedactor) -> None:
    out = redactor.redact_text(f"Authorization: Bearer {FAKE_SECRET}")
    assert FAKE_SECRET not in out
    assert "Bearer <redacted len=40>" in out


def test_stringified_env_dump_is_redacted(redactor: SecretRedactor) -> None:
    """A dict flattened into a message still has its secret values scrubbed."""
    payload = {"GITHUB_TOKEN": FAKE_GHP, "MCP_BEARER": FAKE_SECRET, "PATH": "/usr/bin"}
    out = redactor.redact_text(f"environment: {payload}")
    assert FAKE_GHP not in out
    assert FAKE_SECRET not in out
    assert "/usr/bin" in out  # non-secret value preserved


# --- length output & passthrough -----------------------------------------


def test_redaction_reveals_only_length(redactor: SecretRedactor) -> None:
    out = redactor.redact_mapping({"token": "abcdefghij"})
    assert out["token"] == "<redacted len=10>"


def test_none_value_is_left_untouched(redactor: SecretRedactor) -> None:
    out = redactor.redact_mapping({"token": None})
    assert out["token"] is None


def test_normal_message_is_byte_identical(redactor: SecretRedactor) -> None:
    message = "Connected to MCP server 'gitea' (19 tools) in 42ms"
    assert redactor.redact_text(message) == message


def test_benign_key_value_pair_is_not_mangled(redactor: SecretRedactor) -> None:
    assert redactor.redact_text("count=5 status=ok") == "count=5 status=ok"


# --- recursion ------------------------------------------------------------


def test_redact_mapping_recurses_into_nested_structures(redactor: SecretRedactor) -> None:
    data = {
        "headers": {"Authorization": FAKE_SECRET, "Accept": "application/json"},
        "servers": [{"api_key": FAKE_SECRET}, {"name": "public"}],
    }
    out = redactor.redact_mapping(data)
    assert out["headers"]["Authorization"] == "<redacted len=40>"
    assert out["headers"]["Accept"] == "application/json"
    assert out["servers"][0]["api_key"] == "<redacted len=40>"
    assert out["servers"][1]["name"] == "public"
    assert FAKE_SECRET not in str(out)


# --- fail-safe ------------------------------------------------------------


def test_patch_degrades_safely_on_internal_error(
    redactor: SecretRedactor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising redactor drops the payload rather than emitting it raw."""

    def boom(_text: str) -> str:
        raise RuntimeError("redactor bug")

    monkeypatch.setattr(redactor, "redact_text", boom)
    record = {"message": f"leak {FAKE_GHP}", "extra": {"name": "core.mod", "token": FAKE_SECRET}}
    redactor.patch(record)

    assert record["message"] == REDACTION_ERROR_MARKER
    assert record["extra"] == {"name": "core.mod"}
    assert FAKE_GHP not in str(record)
    assert FAKE_SECRET not in str(record)


# --- loguru sink integration (the real seam) -----------------------------


def test_installed_patcher_scrubs_live_sink_output() -> None:
    """Drive the production install path: a buffer sink must never see a secret.

    Asserts the PROPERTY (secret absent, length placeholder present) through the
    real ``install_log_redaction`` seam — this would fail if the patcher were
    removed, so it is not a namesake test.
    """
    buffer = io.StringIO()
    install_log_redaction(logger)
    sink_id = logger.add(buffer, format="{message} | {extra}", level="DEBUG")
    try:
        # 1. Secret bound into `extra` under a sensitive key (arbitrary shape).
        logger.bind(authorization=FAKE_SECRET).info("handshake complete")
        # 2. Secret interpolated into the message via a known token shape.
        logger.info("token is {}", FAKE_GHP)
        # 3. Whole env-style mapping flattened into the message.
        logger.info("env {}", {"MCP_TOKEN": FAKE_SECRET, "HOME": "/home/app"})
    finally:
        logger.remove(sink_id)

    out = buffer.getvalue()
    assert FAKE_SECRET not in out
    assert FAKE_GHP not in out
    assert "<redacted len=40>" in out
    assert "/home/app" in out  # non-secret context preserved


# --- stdlib logging bridge (third-party libraries) -----------------------


def test_stdlib_logging_is_bridged_and_redacted() -> None:
    """A stdlib logger's record flows through loguru and gets scrubbed.

    Covers third-party libraries (litellm/langfuse/httpx/…) that log via the
    standard library rather than loguru.
    """
    buffer = io.StringIO()
    install_log_redaction(logger)
    install_stdlib_log_bridge(logger)
    sink_id = logger.add(buffer, format="{message}", level="DEBUG")
    third_party = logging.getLogger("mewbo_test.thirdparty.redaction")
    prior_level = third_party.level
    third_party.setLevel(logging.DEBUG)
    try:
        third_party.warning(
            "upstream failed token=%s Authorization: Bearer %s", FAKE_GHP, FAKE_SECRET
        )
    finally:
        logger.remove(sink_id)
        third_party.setLevel(prior_level)

    out = buffer.getvalue()
    assert FAKE_GHP not in out
    assert FAKE_SECRET not in out
    assert "<redacted len=" in out


def test_benign_stdlib_record_passes_through() -> None:
    buffer = io.StringIO()
    install_log_redaction(logger)
    install_stdlib_log_bridge(logger)
    sink_id = logger.add(buffer, format="{message}", level="DEBUG")
    third_party = logging.getLogger("mewbo_test.thirdparty.benign")
    prior_level = third_party.level
    third_party.setLevel(logging.DEBUG)
    try:
        third_party.info("connection established to pool")
    finally:
        logger.remove(sink_id)
        third_party.setLevel(prior_level)

    assert "connection established to pool" in buffer.getvalue()
