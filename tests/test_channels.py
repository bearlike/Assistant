"""Tests for channel adapter framework and Nextcloud Talk adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from meeseeks_api.channels.base import (
    ChannelAdapter,
    ChannelRegistry,
    DeduplicationGuard,
)
from meeseeks_api.channels.nextcloud_talk import NextcloudTalkAdapter
from meeseeks_api.channels.routes import _COMMAND_RE

# ------------------------------------------------------------------
# DeduplicationGuard
# ------------------------------------------------------------------


class TestDeduplicationGuard:
    """Test the replay guard."""

    def test_first_seen_is_not_duplicate(self) -> None:
        guard = DeduplicationGuard(ttl=10.0)
        assert guard.is_duplicate("msg-1") is False

    def test_second_seen_is_duplicate(self) -> None:
        guard = DeduplicationGuard(ttl=10.0)
        guard.is_duplicate("msg-1")
        assert guard.is_duplicate("msg-1") is True

    def test_different_keys_not_duplicate(self) -> None:
        guard = DeduplicationGuard(ttl=10.0)
        guard.is_duplicate("msg-1")
        assert guard.is_duplicate("msg-2") is False

    def test_expired_entry_not_duplicate(self) -> None:
        guard = DeduplicationGuard(ttl=0.01)  # 10ms TTL
        guard.is_duplicate("msg-1")
        time.sleep(0.02)
        assert guard.is_duplicate("msg-1") is False


# ------------------------------------------------------------------
# ChannelRegistry
# ------------------------------------------------------------------


class TestChannelRegistry:
    """Test adapter lookup."""

    def test_register_and_get(self) -> None:
        registry = ChannelRegistry()
        adapter = NextcloudTalkAdapter(
            bot_secret="test-secret-that-is-long-enough-for-nc",
            nextcloud_url="https://nc.example.com",
        )
        registry.register(adapter)
        assert registry.get("nextcloud-talk") is adapter
        assert registry.get("slack") is None

    def test_platforms_list(self) -> None:
        registry = ChannelRegistry()
        assert registry.platforms() == []
        adapter = NextcloudTalkAdapter(
            bot_secret="test-secret-that-is-long-enough-for-nc",
            nextcloud_url="https://nc.example.com",
        )
        registry.register(adapter)
        assert registry.platforms() == ["nextcloud-talk"]


# ------------------------------------------------------------------
# NextcloudTalkAdapter — HMAC verification
# ------------------------------------------------------------------

SECRET = "test-bot-secret-at-least-40-characters-long!"


def _sign(body: str, random: str = "abc123") -> tuple[str, str, str]:
    """Compute HMAC-SHA256 signature like Nextcloud Talk does."""
    digest = hmac.new(
        SECRET.encode(),
        (random + body).encode(),
        hashlib.sha256,
    ).hexdigest()
    return digest, random, "https://nc.example.com"


def _make_webhook_payload(
    *,
    message_text: str = "Hello bot",
    message_id: str = "100",
    sender_id: str = "users/alice",
    sender_name: str = "Alice",
    room_token: str = "abc123room",
    room_name: str = "General",
    thread_id: int | None = None,
    event_type: str = "Create",
) -> str:
    """Build a Nextcloud Talk ActivityStreams webhook payload."""
    obj: dict[str, object] = {
        "type": "Note",
        "id": message_id,
        "name": "message",
        "content": json.dumps({"message": message_text, "parameters": {}}),
        "mediaType": "text/markdown",
    }
    if thread_id is not None:
        obj["threadId"] = thread_id
    return json.dumps(
        {
            "type": event_type,
            "actor": {"type": "Person", "id": sender_id, "name": sender_name},
            "object": obj,
            "target": {"type": "Collection", "id": room_token, "name": room_name},
        }
    )


class TestNextcloudTalkVerify:
    """Test HMAC signature verification."""

    def _adapter(self, **kwargs: object) -> NextcloudTalkAdapter:
        return NextcloudTalkAdapter(
            bot_secret=SECRET,
            nextcloud_url="https://nc.example.com",
            **kwargs,
        )

    def test_valid_signature(self) -> None:
        adapter = self._adapter()
        body = _make_webhook_payload()
        sig, random, backend = _sign(body)
        headers = {
            "X-Nextcloud-Talk-Signature": sig,
            "X-Nextcloud-Talk-Random": random,
            "X-Nextcloud-Talk-Backend": backend,
        }
        assert adapter.verify_request(headers, body.encode()) is True

    def test_invalid_signature(self) -> None:
        adapter = self._adapter()
        body = _make_webhook_payload()
        headers = {
            "X-Nextcloud-Talk-Signature": "deadbeef" * 8,
            "X-Nextcloud-Talk-Random": "abc123",
            "X-Nextcloud-Talk-Backend": "https://nc.example.com",
        }
        assert adapter.verify_request(headers, body.encode()) is False

    def test_missing_headers(self) -> None:
        adapter = self._adapter()
        assert adapter.verify_request({}, b"{}") is False

    def test_backend_allowlist_rejects_wrong_origin(self) -> None:
        adapter = self._adapter(allowed_backends=["https://nc.example.com"])
        body = _make_webhook_payload()
        sig, random, _ = _sign(body)
        headers = {
            "X-Nextcloud-Talk-Signature": sig,
            "X-Nextcloud-Talk-Random": random,
            "X-Nextcloud-Talk-Backend": "https://evil.example.com",
        }
        assert adapter.verify_request(headers, body.encode()) is False

    def test_backend_allowlist_accepts_correct_origin(self) -> None:
        adapter = self._adapter(allowed_backends=["https://nc.example.com"])
        body = _make_webhook_payload()
        sig, random, backend = _sign(body)
        headers = {
            "X-Nextcloud-Talk-Signature": sig,
            "X-Nextcloud-Talk-Random": random,
            "X-Nextcloud-Talk-Backend": backend,
        }
        assert adapter.verify_request(headers, body.encode()) is True

    def test_backend_allowlist_rejects_missing_backend_header(self) -> None:
        adapter = self._adapter(allowed_backends=["https://nc.example.com"])
        body = _make_webhook_payload()
        sig, random, _ = _sign(body)
        headers = {
            "X-Nextcloud-Talk-Signature": sig,
            "X-Nextcloud-Talk-Random": random,
            # No X-Nextcloud-Talk-Backend header
        }
        assert adapter.verify_request(headers, body.encode()) is False


# ------------------------------------------------------------------
# NextcloudTalkAdapter — payload parsing
# ------------------------------------------------------------------


class TestNextcloudTalkParse:
    """Test ActivityStreams payload parsing."""

    def _adapter(self) -> NextcloudTalkAdapter:
        return NextcloudTalkAdapter(
            bot_secret=SECRET,
            nextcloud_url="https://nc.example.com",
        )

    def test_parse_create_message(self) -> None:
        adapter = self._adapter()
        body = _make_webhook_payload(
            message_text="Help me",
            message_id="42",
            sender_name="Bob",
            room_token="room1",
            room_name="Dev",
        )
        msg = adapter.parse_inbound({}, body.encode())
        assert msg is not None
        assert msg.platform == "nextcloud-talk"
        assert msg.channel_id == "room1"
        assert msg.message_id == "42"
        assert msg.sender_name == "Bob"
        assert msg.text == "Help me"
        assert msg.room_name == "Dev"
        assert msg.thread_id is None

    def test_parse_create_with_thread_id(self) -> None:
        adapter = self._adapter()
        body = _make_webhook_payload(thread_id=99)
        msg = adapter.parse_inbound({}, body.encode())
        assert msg is not None
        assert msg.thread_id == "99"

    def test_parse_update_returns_none(self) -> None:
        adapter = self._adapter()
        body = _make_webhook_payload(event_type="Update")
        assert adapter.parse_inbound({}, body.encode()) is None

    def test_parse_delete_returns_none(self) -> None:
        adapter = self._adapter()
        body = _make_webhook_payload(event_type="Delete")
        assert adapter.parse_inbound({}, body.encode()) is None

    def test_parse_invalid_json_returns_none(self) -> None:
        adapter = self._adapter()
        assert adapter.parse_inbound({}, b"not json") is None

    def test_rich_object_placeholders_stripped(self) -> None:
        adapter = self._adapter()
        content = json.dumps(
            {
                "message": "Hello {mention-user1}, check {file-1}",
                "parameters": {
                    "mention-user1": {"type": "user", "id": "alice", "name": "Alice"},
                    "file-1": {
                        "type": "file",
                        "id": "55",
                        "name": "report.pdf",
                        "mimetype": "application/pdf",
                        "size": "1024",
                        "link": "/f/55",
                    },
                },
            }
        )
        payload = json.dumps(
            {
                "type": "Create",
                "actor": {"type": "Person", "id": "users/alice", "name": "Alice"},
                "object": {
                    "type": "Note",
                    "id": "200",
                    "name": "message",
                    "content": content,
                    "mediaType": "text/markdown",
                },
                "target": {"type": "Collection", "id": "room1", "name": "Room"},
            }
        )
        msg = adapter.parse_inbound({}, payload.encode())
        assert msg is not None
        assert msg.text == "Hello , check"
        assert len(msg.attachments) == 1
        assert msg.attachments[0]["name"] == "report.pdf"


# ------------------------------------------------------------------
# Protocol compliance
# ------------------------------------------------------------------


# ------------------------------------------------------------------
# Command regex parsing
# ------------------------------------------------------------------


class TestCommandRegex:
    """Test slash command parsing from message text."""

    def test_simple_command(self) -> None:
        m = _COMMAND_RE.match("/help")
        assert m is not None
        assert m.group(1) == "help"
        assert m.group(2).strip() == ""

    def test_command_with_args(self) -> None:
        m = _COMMAND_RE.match("/switch-project personal-assistant")
        assert m is not None
        assert m.group(1) == "switch-project"
        assert m.group(2).strip() == "personal-assistant"

    def test_command_in_email_reply_ignores_quoted_text(self) -> None:
        """Quoted reply text below the command must not leak into args."""
        text = (
            "/switch-project personal-assistant\n"
            "\n"
            "On Mon, Apr 7, 2026 at 5:53 PM, Meeseeks wrote:\n"
            "> Hey! Going great, thanks for asking!\n"
        )
        m = _COMMAND_RE.match(text.strip())
        assert m is not None
        assert m.group(1) == "switch-project"
        assert m.group(2).strip() == "personal-assistant"

    def test_command_without_args_in_email_reply(self) -> None:
        text = (
            "/help\n\nOn Mon, Apr 7, 2026 at 5:53 PM, Meeseeks wrote:\n> Commands: /help, /usage\n"
        )
        m = _COMMAND_RE.match(text.strip())
        assert m is not None
        assert m.group(1) == "help"
        assert m.group(2).strip() == ""

    def test_non_command_text_does_not_match(self) -> None:
        assert _COMMAND_RE.match("Hello, how are you?") is None

    def test_command_with_hyphen(self) -> None:
        m = _COMMAND_RE.match("/switch-project foo")
        assert m is not None
        assert m.group(1) == "switch-project"


class TestProtocolCompliance:
    """Verify NextcloudTalkAdapter satisfies ChannelAdapter protocol."""

    def test_is_channel_adapter(self) -> None:
        adapter = NextcloudTalkAdapter(
            bot_secret=SECRET,
            nextcloud_url="https://nc.example.com",
        )
        assert isinstance(adapter, ChannelAdapter)
