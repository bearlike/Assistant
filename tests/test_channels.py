"""Tests for channel adapter framework and Nextcloud Talk adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from mewbo_api.channels import routes as channels_routes
from mewbo_api.channels.base import (
    ChannelAdapter,
    ChannelRegistry,
    DeduplicationGuard,
)
from mewbo_api.channels.nextcloud_talk import NextcloudTalkAdapter
from mewbo_api.channels.routes import _COMMAND_RE, extract_final_answer
from mewbo_core.session_runtime import SessionRuntime
from mewbo_core.session_store import SessionStore

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
            "On Mon, Apr 7, 2026 at 5:53 PM, Mewbo wrote:\n"
            "> Hey! Going great, thanks for asking!\n"
        )
        m = _COMMAND_RE.match(text.strip())
        assert m is not None
        assert m.group(1) == "switch-project"
        assert m.group(2).strip() == "personal-assistant"

    def test_command_without_args_in_email_reply(self) -> None:
        text = (
            "/help\n\nOn Mon, Apr 7, 2026 at 5:53 PM, Mewbo wrote:\n> Commands: /help, /usage\n"
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


# ------------------------------------------------------------------
# extract_final_answer — honest framing for blocked/unmet_goal outcomes
# ------------------------------------------------------------------


class TestExtractFinalAnswerHonesty:
    """A blocked/unmet_goal outcome must never read as a clean success."""

    @staticmethod
    def _events(task_result: str | None) -> list[dict]:
        events: list[dict] = [{"type": "user", "payload": {"text": "do the thing"}}]
        if task_result is not None:
            events.append({"type": "completion", "payload": {"task_result": task_result}})
        return events

    def test_error_branch_is_unchanged(self) -> None:
        text = extract_final_answer(
            self._events("all done"), "boom", outcome={"status": "completed"}
        )
        assert text == "Session ended with an error: boom"

    def test_omitted_outcome_matches_legacy_behavior(self) -> None:
        assert extract_final_answer(self._events("all done"), None) == "all done"

    def test_completed_outcome_passes_through_unchanged(self) -> None:
        text = extract_final_answer(self._events("all done"), None, outcome={"status": "completed"})
        assert text == "all done"

    def test_blocked_outcome_names_the_wall(self) -> None:
        outcome = {"status": "blocked", "blocked_code": "repo_access"}
        text = extract_final_answer(
            self._events("partial progress before the wall"), None, outcome=outcome
        )
        assert "blocked" in text.lower()
        assert "repository access" in text
        assert "partial progress before the wall" in text
        assert not text.startswith("partial progress")  # not a bare success string

    def test_blocked_outcome_with_no_partial_text_still_replies(self) -> None:
        outcome = {"status": "blocked", "blocked_code": "network"}
        text = extract_final_answer(self._events(None), None, outcome=outcome)
        assert text
        assert "network" in text.lower()

    def test_unmet_goal_outcome_states_the_shortfall(self) -> None:
        outcome = {"status": "unmet_goal", "unmet_goal_reason": "verification failed twice"}
        text = extract_final_answer(self._events("looked done"), None, outcome=outcome)
        assert "goal" in text.lower()
        assert "verification failed twice" in text
        assert "looked done" in text
        assert not text.startswith("looked done")

    def test_unmet_goal_falls_back_to_done_reason(self) -> None:
        outcome = {"status": "unmet_goal", "done_reason": "halted_no_progress"}
        text = extract_final_answer(self._events(None), None, outcome=outcome)
        assert "halted_no_progress" in text


# ------------------------------------------------------------------
# _channel_completion_hook — contract test from the hook's own seam
# ------------------------------------------------------------------


class _RecordingAdapter:
    """Minimal ``ChannelAdapter`` that records what it was asked to send."""

    platform = "nextcloud-talk"
    supports_webhook = True

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    def verify_request(self, headers: dict[str, str], body: bytes) -> bool:
        return True

    def parse_inbound(self, headers: dict[str, str], body: bytes) -> None:
        return None

    def send_response(
        self,
        channel_id: str,
        text: str,
        thread_id: str | None = None,
        reply_to: str | None = None,
    ) -> str | None:
        self.sent.append(
            {"channel_id": channel_id, "text": text, "thread_id": thread_id, "reply_to": reply_to}
        )
        return "sent-1"

    @property
    def system_context(self) -> str:
        return "test adapter"


class TestChannelCompletionHookHonesty:
    """The completion hook must consult the session's derived outcome, not
    just the ``error`` argument, before posting a reply."""

    def _wire(self, tmp_path, monkeypatch) -> tuple[SessionRuntime, _RecordingAdapter]:
        store = SessionStore(root_dir=str(tmp_path))
        runtime = SessionRuntime(session_store=store)
        registry = ChannelRegistry()
        adapter = _RecordingAdapter()
        registry.register(adapter)
        monkeypatch.setattr(channels_routes, "_runtime", runtime)
        monkeypatch.setattr(channels_routes, "_registry", registry)
        return runtime, adapter

    @staticmethod
    def _seed_session(runtime: SessionRuntime) -> str:
        session_id = runtime.resolve_session()
        runtime.session_store.append_event(
            session_id, {"type": "user", "payload": {"text": "please fix the bug"}}
        )
        runtime.session_store.append_event(
            session_id,
            {
                "type": "context",
                "payload": {
                    "source_platform": "nextcloud-talk",
                    "channel_id": "room-1",
                    "thread_id": None,
                    "reply_to_message_id": "msg-100",
                },
            },
        )
        return session_id

    def test_blocked_session_reply_is_not_framed_as_success(self, tmp_path, monkeypatch) -> None:
        runtime, adapter = self._wire(tmp_path, monkeypatch)
        session_id = self._seed_session(runtime)
        runtime.session_store.append_event(
            session_id,
            {
                "type": "completion",
                "payload": {
                    "done": True,
                    "done_reason": "completed",
                    "task_result": "Pushed a partial fix",
                    "blocked_code": "network",
                },
            },
        )
        channels_routes._channel_completion_hook(session_id)
        assert len(adapter.sent) == 1
        text = str(adapter.sent[0]["text"])
        assert "blocked" in text.lower()
        assert "network" in text.lower()
        assert "Pushed a partial fix" in text
        assert not text.startswith("Pushed a partial fix")

    def test_unmet_goal_session_reply_states_the_shortfall(self, tmp_path, monkeypatch) -> None:
        runtime, adapter = self._wire(tmp_path, monkeypatch)
        session_id = self._seed_session(runtime)
        runtime.session_store.append_event(
            session_id,
            {
                "type": "completion",
                "payload": {
                    "done": True,
                    "done_reason": "verification_failed",
                    "task_result": "Looks correct to me",
                },
            },
        )
        channels_routes._channel_completion_hook(session_id)
        assert len(adapter.sent) == 1
        text = str(adapter.sent[0]["text"])
        assert "goal" in text.lower()
        assert "Looks correct to me" in text
        assert not text.startswith("Looks correct to me")

    def test_genuinely_successful_session_reply_is_unchanged(self, tmp_path, monkeypatch) -> None:
        runtime, adapter = self._wire(tmp_path, monkeypatch)
        session_id = self._seed_session(runtime)
        runtime.session_store.append_event(
            session_id,
            {
                "type": "completion",
                "payload": {
                    "done": True,
                    "done_reason": "completed",
                    "task_result": "All done, fix merged.",
                },
            },
        )
        channels_routes._channel_completion_hook(session_id)
        assert len(adapter.sent) == 1
        assert adapter.sent[0]["text"] == "All done, fix merged."
