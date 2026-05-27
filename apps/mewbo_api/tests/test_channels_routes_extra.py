"""Extra contract tests for channels/routes.py, channels/nextcloud_talk.py,
and channels/email_adapter.py.

Stubs only I/O (real network, SMTP, IMAP, Docker).  Exercises:
- _process_inbound pipeline: dedup gate, mention gate, session resolution
  (thread vs room scoped), slash-command dispatch (help/new/switch-project),
  completion hook, unknown-command fall-through to LLM
- webhook endpoint: missing runtime, unknown platform, HMAC failure,
  non-message event, full happy-path
- NextcloudTalkAdapter: send_response HMAC signing, host-header, reply_to
  vs thread_id preference, truncation, send error handling
- EmailAdapter: multipart body-extraction path, _poll_loop reconnect backoff,
  _handle_email marks-SEEN even on None parse, system_context content
- _normalize_origin: standard-port stripping, http/https, port preservation
- _build_help_text, _format_project_list
- _find_channel_context / _extract_final_answer
- init_channels: NC Talk + email branch wiring
"""

from __future__ import annotations

import hashlib
import hmac
import json
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

SECRET = "test-bot-secret-at-least-40-characters-long!"


def _nc_adapter(**kwargs: Any):
    from mewbo_api.channels.nextcloud_talk import NextcloudTalkAdapter

    defaults = dict(bot_secret=SECRET, nextcloud_url="https://nc.example.com")
    defaults.update(kwargs)
    return NextcloudTalkAdapter(**defaults)


def _sign(body: str, random: str = "abc123") -> tuple[str, str]:
    digest = hmac.new(SECRET.encode(), (random + body).encode(), hashlib.sha256).hexdigest()
    return digest, random


def _nc_payload(
    *,
    text: str = "@Mewbo hello",
    event_type: str = "Create",
    msg_id: str = "99",
    room: str = "room1",
    thread_id: int | None = None,
) -> bytes:
    obj: dict = {
        "type": "Note",
        "id": msg_id,
        "name": "message",
        "content": json.dumps({"message": text, "parameters": {}}),
        "mediaType": "text/markdown",
    }
    if thread_id is not None:
        obj["threadId"] = thread_id
    payload = {
        "type": event_type,
        "actor": {"type": "Person", "id": "users/alice", "name": "Alice"},
        "object": obj,
        "target": {"type": "Collection", "id": room, "name": "General"},
    }
    return json.dumps(payload).encode()


def _signed_headers(body: bytes, random: str = "abc123") -> dict:
    sig, _ = _sign(body.decode(), random)
    return {
        "X-Nextcloud-Talk-Signature": sig,
        "X-Nextcloud-Talk-Random": random,
        "X-Nextcloud-Talk-Backend": "https://nc.example.com",
    }


# ---------------------------------------------------------------------------
# _normalize_origin
# ---------------------------------------------------------------------------


class TestNormalizeOrigin:
    def test_strips_standard_https_port(self) -> None:
        from mewbo_api.channels.nextcloud_talk import _normalize_origin

        assert _normalize_origin("https://host:443/path") == "https://host"

    def test_strips_standard_http_port(self) -> None:
        from mewbo_api.channels.nextcloud_talk import _normalize_origin

        assert _normalize_origin("http://host:80/") == "http://host"

    def test_preserves_non_standard_port(self) -> None:
        from mewbo_api.channels.nextcloud_talk import _normalize_origin

        assert _normalize_origin("https://host:8443/") == "https://host:8443"

    def test_lowercases(self) -> None:
        from mewbo_api.channels.nextcloud_talk import _normalize_origin

        assert _normalize_origin("HTTPS://HOST.COM") == "https://host.com"


# ---------------------------------------------------------------------------
# NextcloudTalkAdapter.send_response
# ---------------------------------------------------------------------------


class TestNcSendResponse:
    """send_response constructs the correct OCS Bot API request."""

    def _adapter(self, **kw) -> Any:
        return _nc_adapter(**kw)

    @patch("mewbo_api.channels.nextcloud_talk.urllib.request.urlopen")
    def test_send_response_201_returns_sent(self, mock_urlopen: MagicMock) -> None:
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.status = 201
        mock_urlopen.return_value = resp

        adapter = self._adapter()
        result = adapter.send_response("room1", "Hello!")
        assert result == "sent"
        mock_urlopen.assert_called_once()

    @patch("mewbo_api.channels.nextcloud_talk.urllib.request.urlopen")
    def test_send_response_non_201_returns_none(self, mock_urlopen: MagicMock) -> None:
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.status = 500
        mock_urlopen.return_value = resp

        adapter = self._adapter()
        result = adapter.send_response("room1", "Hello!")
        assert result is None

    @patch("mewbo_api.channels.nextcloud_talk.urllib.request.urlopen")
    def test_send_response_network_error_returns_none(self, mock_urlopen: MagicMock) -> None:
        import urllib.error

        mock_urlopen.side_effect = urllib.error.URLError("refused")
        adapter = self._adapter()
        result = adapter.send_response("room1", "Hello!")
        assert result is None

    @patch("mewbo_api.channels.nextcloud_talk.urllib.request.urlopen")
    def test_send_response_includes_reply_to(self, mock_urlopen: MagicMock) -> None:
        """reply_to takes precedence over thread_id in the JSON body."""
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.status = 201
        mock_urlopen.return_value = resp

        adapter = self._adapter()
        adapter.send_response("room1", "ok", reply_to="42", thread_id="10")

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode())
        assert body["replyTo"] == 42
        assert "threadId" not in body

    @patch("mewbo_api.channels.nextcloud_talk.urllib.request.urlopen")
    def test_send_response_thread_id_fallback(self, mock_urlopen: MagicMock) -> None:
        """thread_id used when reply_to is absent."""
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.status = 201
        mock_urlopen.return_value = resp

        adapter = self._adapter()
        adapter.send_response("room1", "ok", thread_id="7")

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode())
        assert body["threadId"] == 7
        assert "replyTo" not in body

    @patch("mewbo_api.channels.nextcloud_talk.urllib.request.urlopen")
    def test_send_response_host_header_injected(self, mock_urlopen: MagicMock) -> None:
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.status = 201
        mock_urlopen.return_value = resp

        adapter = self._adapter(host_header="public.nc.example.com")
        adapter.send_response("room1", "ok")

        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Host") == "public.nc.example.com"

    @patch("mewbo_api.channels.nextcloud_talk.urllib.request.urlopen")
    def test_send_response_truncates_long_text(self, mock_urlopen: MagicMock) -> None:
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.status = 201
        mock_urlopen.return_value = resp

        adapter = self._adapter()
        long_text = "x" * 50000
        adapter.send_response("room1", long_text)

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode())
        assert len(body["message"]) == 32000

    @patch("mewbo_api.channels.nextcloud_talk.urllib.request.urlopen")
    def test_send_response_hmac_signed(self, mock_urlopen: MagicMock) -> None:
        """Outbound request carries Bot-Signature and Bot-Random headers."""
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.status = 201
        mock_urlopen.return_value = resp

        adapter = self._adapter()
        adapter.send_response("room1", "check")

        req = mock_urlopen.call_args[0][0]
        sig_hdr = req.get_header("X-nextcloud-talk-bot-signature")
        rand_hdr = req.get_header("X-nextcloud-talk-bot-random")
        assert sig_hdr is not None
        assert rand_hdr is not None

    @patch("mewbo_api.channels.nextcloud_talk.urllib.request.urlopen")
    def test_send_response_invalid_reply_to_skipped(self, mock_urlopen: MagicMock) -> None:
        """Non-integer reply_to/thread_id is silently skipped (no replyTo key)."""
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.status = 201
        mock_urlopen.return_value = resp

        adapter = self._adapter()
        adapter.send_response("room1", "ok", reply_to="not-an-int")

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode())
        # ValueError path → no replyTo in body
        assert "replyTo" not in body


# ---------------------------------------------------------------------------
# NextcloudTalkAdapter.system_context
# ---------------------------------------------------------------------------


def test_nc_system_context_mentions_trigger() -> None:
    adapter = _nc_adapter(trigger_keyword="@Bot")
    ctx = adapter.system_context
    assert "@Bot" in ctx
    assert "Nextcloud Talk" in ctx


# ---------------------------------------------------------------------------
# Helpers: _extract_message_text with non-JSON content
# ---------------------------------------------------------------------------


def test_extract_message_text_plain_string() -> None:
    """Falls back to the raw string when content isn't JSON."""
    from mewbo_api.channels.nextcloud_talk import _extract_message_text

    assert _extract_message_text("plain text") == "plain text"


def test_extract_attachments_non_dict_params() -> None:
    """Non-dict params in content → empty attachments list."""
    from mewbo_api.channels.nextcloud_talk import _extract_attachments

    content = json.dumps({"message": "hi", "parameters": "not-a-dict"})
    assert _extract_attachments(content) == []


def test_extract_attachments_bad_json() -> None:
    from mewbo_api.channels.nextcloud_talk import _extract_attachments

    assert _extract_attachments("not json") == []


# ---------------------------------------------------------------------------
# EmailAdapter: _extract_text_body (non-multipart path)
# ---------------------------------------------------------------------------


def test_extract_text_body_non_multipart() -> None:
    from email.mime.text import MIMEText

    from mewbo_api.channels.email_adapter import _extract_text_body

    msg = MIMEText("simple body", "plain", "utf-8")
    assert _extract_text_body(msg) == "simple body"


def test_extract_text_body_multipart_skips_attachment() -> None:
    """text/plain with attachment disposition is skipped."""
    from mewbo_api.channels.email_adapter import _extract_text_body

    msg = MIMEMultipart("mixed")
    plain = MIMEText("the body", "plain", "utf-8")
    attachment = MIMEText("attachment content", "plain", "utf-8")
    attachment.add_header("Content-Disposition", "attachment", filename="file.txt")
    msg.attach(plain)
    msg.attach(attachment)
    # First text/plain without attachment disposition should win
    assert _extract_text_body(msg) == "the body"


# ---------------------------------------------------------------------------
# EmailPoller: _poll_loop reconnect backoff and graceful stop
# ---------------------------------------------------------------------------


def _make_email_adapter() -> Any:
    from mewbo_api.channels.email_adapter import EmailAdapter

    return EmailAdapter(
        smtp_host="smtp.example.com",
        username="bot@example.com",
        password="pw",
        allowed_senders=["alice@example.com"],
    )


def test_email_poller_start_idempotent() -> None:
    """Calling start() twice with the thread still alive doesn't spawn a second thread."""
    from mewbo_api.channels.email_adapter import EmailPoller

    adapter = _make_email_adapter()
    process_fn = MagicMock()
    poller = EmailPoller(
        adapter=adapter,
        imap_host="imap.example.com",
        username="bot@example.com",
        password="pw",
        process_fn=process_fn,
    )
    stop_event = poller._stop_event

    def _looping() -> None:
        stop_event.wait()  # Block until stop() is called

    with patch.object(poller, "_poll_loop", side_effect=_looping):
        poller.start()
        first_thread = poller._thread
        assert first_thread is not None
        assert first_thread.is_alive()
        # Second start() call: thread is alive → no new thread created
        poller.start()
        assert poller._thread is first_thread
        poller.stop()


def test_email_poller_handle_email_marks_seen_on_reject() -> None:
    """_handle_email marks the UID SEEN even when parse_email returns None."""
    from mewbo_api.channels.email_adapter import EmailPoller

    adapter = _make_email_adapter()
    process_fn = MagicMock()
    poller = EmailPoller(
        adapter=adapter,
        imap_host="imap.example.com",
        username="bot@example.com",
        password="pw",
        process_fn=process_fn,
    )

    from email.mime.text import MIMEText

    # Build an email from a disallowed sender → parse_email returns None
    raw = MIMEText("hi", "plain")
    raw["From"] = "stranger@evil.com"
    raw["To"] = "bot@example.com"
    raw["Message-ID"] = "<x@evil.com>"
    raw["Subject"] = "spam"
    raw_bytes = raw.as_bytes()

    mock_client = MagicMock()
    poller._handle_email(42, {b"RFC822": raw_bytes}, mock_client)

    # process_fn NOT called (rejected), but SEEN flag still set
    process_fn.assert_not_called()
    mock_client.add_flags.assert_called_once()


def test_email_poller_handle_email_no_rfc822() -> None:
    """_handle_email returns early when RFC822 data is missing."""
    from mewbo_api.channels.email_adapter import EmailPoller

    adapter = _make_email_adapter()
    process_fn = MagicMock()
    poller = EmailPoller(
        adapter=adapter,
        imap_host="imap.example.com",
        username="bot@example.com",
        password="pw",
        process_fn=process_fn,
    )
    mock_client = MagicMock()
    poller._handle_email(1, {}, mock_client)
    process_fn.assert_not_called()
    mock_client.add_flags.assert_not_called()


def test_email_poller_poll_loop_backoff_on_connect_error() -> None:
    """_poll_loop doubles backoff on repeated IMAP errors up to 60s."""
    from mewbo_api.channels.email_adapter import EmailPoller

    adapter = _make_email_adapter()
    process_fn = MagicMock()
    poller = EmailPoller(
        adapter=adapter,
        imap_host="imap.example.com",
        username="bot@example.com",
        password="pw",
        process_fn=process_fn,
    )

    call_count = {"n": 0}

    def fake_connect() -> None:
        call_count["n"] += 1
        if call_count["n"] <= 2:
            raise ConnectionRefusedError("no IMAP")
        # After 2 failures, stop the poller
        poller._stop_event.set()
        raise ConnectionRefusedError("stop requested")

    poller._connect = fake_connect  # type: ignore[method-assign]
    poller._stop_event.wait = MagicMock(return_value=None)  # don't actually sleep

    poller._poll_loop()
    # Should have tried to connect at least twice
    assert call_count["n"] >= 2


def test_email_adapter_system_context_content() -> None:
    from mewbo_api.channels.email_adapter import EmailAdapter

    adapter = EmailAdapter(
        smtp_host="smtp.example.com",
        username="bot@example.com",
        password="pw",
        from_address="mewbo@example.com",
    )
    ctx = adapter.system_context
    assert "email" in ctx.lower()
    assert "mewbo@example.com" in ctx
    assert "markdown" in ctx.lower()


# ---------------------------------------------------------------------------
# routes._process_inbound pipeline
# ---------------------------------------------------------------------------


def _make_runtime() -> MagicMock:
    rt = MagicMock()
    rt.session_store.resolve_tag.return_value = None
    rt.session_store.create_session.return_value = "sess-aabbccdd" * 4
    rt.is_running.return_value = False
    return rt


def _make_inbound(
    *,
    platform: str = "nextcloud-talk",
    text: str = "@Mewbo hello",
    msg_id: str = "1",
    channel_id: str = "room1",
    thread_id: str | None = None,
    sender_name: str = "Alice",
) -> Any:
    from mewbo_api.channels.base import InboundMessage

    return InboundMessage(
        platform=platform,
        channel_id=channel_id,
        thread_id=thread_id,
        message_id=msg_id,
        sender_id="users/alice",
        sender_name=sender_name,
        text=text,
        timestamp="",
        room_name="General",
    )


@pytest.fixture()
def route_env(monkeypatch: pytest.MonkeyPatch):
    """Patch module-level globals in channels.routes for isolated pipeline tests."""
    import mewbo_api.channels.routes as routes

    rt = _make_runtime()
    adapter = _nc_adapter()

    # Register the adapter in a fresh registry
    from mewbo_api.channels.base import ChannelRegistry, DeduplicationGuard

    registry = ChannelRegistry()
    registry.register(adapter)
    dedup = DeduplicationGuard(ttl=60.0)

    monkeypatch.setattr(routes, "_runtime", rt)
    monkeypatch.setattr(routes, "_registry", registry)
    monkeypatch.setattr(routes, "_dedup", dedup)

    return rt, adapter, registry


class TestProcessInbound:
    """Exercise _process_inbound branches."""

    def test_dedup_blocks_replay(self, route_env: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
        import mewbo_api.channels.routes as routes

        rt, adapter, registry = route_env
        msg = _make_inbound(msg_id="dup-1")

        # First call → real path
        routes._process_inbound(adapter, msg)
        first_start_count = rt.start_async.call_count

        # Second call with same message_id → dedup kicks in, no new session
        routes._process_inbound(adapter, msg)
        assert rt.start_async.call_count == first_start_count

    def test_mention_gate_blocks_unmentioned(
        self, route_env: tuple, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import mewbo_api.channels.routes as routes

        rt, adapter, _ = route_env
        msg = _make_inbound(text="just a regular message without the trigger")
        routes._process_inbound(adapter, msg)
        rt.start_async.assert_not_called()

    def test_thread_scoped_tag(self, route_env: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
        import mewbo_api.channels.routes as routes

        rt, adapter, _ = route_env
        msg = _make_inbound(thread_id="tid-9", msg_id="m-thread")
        rt.session_store.resolve_tag.return_value = None

        routes._process_inbound(adapter, msg)

        tag_calls = rt.session_store.tag_session.call_args_list
        assert any("thread" in str(c) for c in tag_calls)

    def test_room_scoped_tag_when_no_thread(
        self, route_env: tuple, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import mewbo_api.channels.routes as routes

        rt, adapter, _ = route_env
        msg = _make_inbound(thread_id=None, msg_id="m-room")
        rt.session_store.resolve_tag.return_value = None

        routes._process_inbound(adapter, msg)

        tag_calls = rt.session_store.tag_session.call_args_list
        assert any("room" in str(c) for c in tag_calls)

    def test_existing_session_reused(
        self, route_env: tuple, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import mewbo_api.channels.routes as routes

        rt, adapter, _ = route_env
        rt.session_store.resolve_tag.return_value = "existing-sess"
        msg = _make_inbound(msg_id="m-reuse")

        routes._process_inbound(adapter, msg)

        # No new session created
        rt.session_store.create_session.assert_not_called()

    def test_running_session_enqueues_message(
        self, route_env: tuple, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import mewbo_api.channels.routes as routes

        rt, adapter, _ = route_env
        rt.is_running.return_value = True
        rt.session_store.resolve_tag.return_value = "running-sess"
        msg = _make_inbound(msg_id="m-running")

        routes._process_inbound(adapter, msg)

        rt.enqueue_message.assert_called_once()
        rt.start_async.assert_not_called()

    def test_slash_command_help_dispatched(
        self, route_env: tuple, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import mewbo_api.channels.routes as routes

        rt, adapter, _ = route_env
        rt.session_store.resolve_tag.return_value = "sess-x"
        # Must include trigger keyword to pass mention gate; after stripping @Mewbo,
        # the remaining text "/help" will match the command regex.
        msg = _make_inbound(text="@Mewbo /help", msg_id="m-help")

        # Patch send_response so we don't try real HTTP
        adapter.send_response = MagicMock(return_value="sent")
        routes._process_inbound(adapter, msg)

        adapter.send_response.assert_called_once()
        reply_text = adapter.send_response.call_args[1]["text"]
        assert "Commands" in reply_text
        rt.start_async.assert_not_called()

    def test_slash_command_new_creates_fresh_session(
        self, route_env: tuple, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import mewbo_api.channels.routes as routes

        rt, adapter, _ = route_env
        rt.session_store.resolve_tag.return_value = "old-sess"
        msg = _make_inbound(text="@Mewbo /new", msg_id="m-new")
        rt.session_store.create_session.return_value = "new-sess"
        adapter.send_response = MagicMock(return_value="sent")

        routes._process_inbound(adapter, msg)

        rt.session_store.create_session.assert_called()
        adapter.send_response.assert_called_once()
        rt.start_async.assert_not_called()

    def test_unknown_command_falls_through_to_llm(
        self, route_env: tuple, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import mewbo_api.channels.routes as routes

        rt, adapter, _ = route_env
        rt.session_store.resolve_tag.return_value = "sess-x"
        msg = _make_inbound(text="@Mewbo /unknown-cmd", msg_id="m-unk")
        adapter.send_response = MagicMock()

        routes._process_inbound(adapter, msg)

        adapter.send_response.assert_not_called()
        rt.start_async.assert_called_once()

    def test_send_reaction_called_when_adapter_has_it(
        self, route_env: tuple, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import mewbo_api.channels.routes as routes

        rt, adapter, _ = route_env
        rt.session_store.resolve_tag.return_value = "sess-r"
        msg = _make_inbound(msg_id="m-react")
        adapter.send_reaction = MagicMock()
        adapter.send_response = MagicMock()

        routes._process_inbound(adapter, msg)

        adapter.send_reaction.assert_called_once()

    def test_switch_project_no_args_returns_list(
        self, route_env: tuple, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import mewbo_api.channels.routes as routes

        rt, adapter, _ = route_env
        rt.session_store.resolve_tag.return_value = "sess-sw"
        msg = _make_inbound(text="@Mewbo /switch-project", msg_id="m-sw")
        adapter.send_response = MagicMock(return_value="sent")

        # Patch get_config() to return empty projects
        with patch("mewbo_api.channels.routes.get_config") as mock_cfg:
            mock_cfg.return_value.projects = {}
            routes._process_inbound(adapter, msg)

        adapter.send_response.assert_called_once()
        text = adapter.send_response.call_args[1]["text"]
        assert "Usage" in text or "switch-project" in text

    def test_switch_project_valid_appends_event(
        self, route_env: tuple, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        import mewbo_api.channels.routes as routes

        rt, adapter, _ = route_env
        rt.session_store.resolve_tag.return_value = "sess-sw2"
        msg = _make_inbound(text="@Mewbo /switch-project myproject", msg_id="m-sw2")
        adapter.send_response = MagicMock(return_value="sent")

        fake_project = MagicMock()
        fake_project.path = "/tmp"  # must exist
        fake_project.description = "My project"

        with patch("mewbo_api.channels.routes.get_config") as mock_cfg:
            mock_cfg.return_value.projects = {"myproject": fake_project}
            with patch("os.path.isdir", return_value=True):
                routes._process_inbound(adapter, msg)

        adapter.send_response.assert_called_once()
        text = adapter.send_response.call_args[1]["text"]
        assert "myproject" in text

    def test_switch_project_unknown_returns_list(
        self, route_env: tuple, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import mewbo_api.channels.routes as routes

        rt, adapter, _ = route_env
        rt.session_store.resolve_tag.return_value = "sess-sw3"
        msg = _make_inbound(text="@Mewbo /switch-project nonexistent", msg_id="m-sw3")
        adapter.send_response = MagicMock(return_value="sent")

        with patch("mewbo_api.channels.routes.get_config") as mock_cfg:
            mock_cfg.return_value.projects = {}
            routes._process_inbound(adapter, msg)

        adapter.send_response.assert_called_once()
        text = adapter.send_response.call_args[1]["text"]
        # Should mention the unknown project name
        assert "nonexistent" in text or "Unknown" in text


# ---------------------------------------------------------------------------
# routes._cmd_usage
# ---------------------------------------------------------------------------


def test_cmd_usage_formats_token_budget(route_env: tuple) -> None:
    import mewbo_api.channels.routes as routes
    from mewbo_api.channels.routes import CommandContext

    rt, adapter, _ = route_env
    rt.session_store.load_transcript.return_value = []
    rt.session_store.load_summary.return_value = None

    msg = _make_inbound(text="@Mewbo /usage", msg_id="m-usage")

    with patch("mewbo_api.channels.routes.get_token_budget") as mock_budget:
        mock_budget.return_value = MagicMock(
            total_tokens=1000,
            context_window=100000,
            utilization=0.01,
            threshold=0.9,
            needs_compact=False,
        )
        ctx = CommandContext(
            session_id="sess-usage",
            args="",
            message=msg,
            tag="nextcloud-talk:room:room1",
        )
        result = routes._cmd_usage(ctx)

    assert "Events" in result
    assert "Tokens" in result


# ---------------------------------------------------------------------------
# _find_channel_context / _extract_final_answer
# ---------------------------------------------------------------------------


class TestChannelContextHelpers:
    def test_find_channel_context_returns_latest(self) -> None:
        from mewbo_api.channels.routes import _find_channel_context

        events = [
            {
                "type": "context",
                "payload": {"source_platform": "nextcloud-talk", "channel_id": "r1"},
            },  # noqa: E501
            {"type": "tool_result", "payload": {}},
            {
                "type": "context",
                "payload": {"source_platform": "nextcloud-talk", "channel_id": "r2"},
            },  # noqa: E501
        ]
        ctx = _find_channel_context(events)
        assert ctx is not None
        assert ctx["channel_id"] == "r2"

    def test_find_channel_context_ignores_no_source_platform(self) -> None:
        from mewbo_api.channels.routes import _find_channel_context

        events = [
            {"type": "context", "payload": {"active_project": "foo"}},
        ]
        assert _find_channel_context(events) is None

    def test_extract_final_answer_on_error(self) -> None:
        from mewbo_api.channels.routes import _extract_final_answer

        result = _extract_final_answer([], "Something went wrong")
        assert "error" in result.lower()

    def test_extract_final_answer_from_completion(self) -> None:
        from mewbo_api.channels.routes import _extract_final_answer

        events = [
            {"type": "completion", "payload": {"task_result": "Done!"}},
        ]
        assert _extract_final_answer(events, None) == "Done!"

    def test_extract_final_answer_from_assistant(self) -> None:
        from mewbo_api.channels.routes import _extract_final_answer

        events = [
            {"type": "assistant", "payload": {"text": "Here is my answer."}},
        ]
        assert _extract_final_answer(events, None) == "Here is my answer."

    def test_extract_final_answer_empty_events_no_error(self) -> None:
        from mewbo_api.channels.routes import _extract_final_answer

        assert _extract_final_answer([], None) == ""


# ---------------------------------------------------------------------------
# _channel_completion_hook
# ---------------------------------------------------------------------------


class TestChannelCompletionHook:
    def test_hook_no_runtime_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import mewbo_api.channels.routes as routes

        monkeypatch.setattr(routes, "_runtime", None)
        # Must not raise
        routes._channel_completion_hook("any-session")

    def test_hook_sends_reply(self, route_env: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
        import mewbo_api.channels.routes as routes

        rt, adapter, _ = route_env
        rt.session_store.load_transcript.return_value = [
            {
                "type": "context",
                "payload": {
                    "source_platform": "nextcloud-talk",
                    "channel_id": "room1",
                    "thread_id": None,
                    "reply_to_message_id": "42",
                },
            },
            {"type": "completion", "payload": {"task_result": "All done!"}},
        ]
        adapter.send_response = MagicMock(return_value="sent")

        routes._channel_completion_hook("sess-complete")

        adapter.send_response.assert_called_once()
        # Use .kwargs (not the fragile positional [1] index) so the assertion
        # is robust against call_args tuple layout differences.
        sent_text = adapter.send_response.call_args.kwargs.get("text")
        assert sent_text == "All done!"

    def test_hook_skips_non_channel_session(
        self, route_env: tuple, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import mewbo_api.channels.routes as routes

        rt, adapter, _ = route_env
        rt.session_store.load_transcript.return_value = [
            {"type": "context", "payload": {"active_project": "foo"}},
        ]
        adapter.send_response = MagicMock()

        routes._channel_completion_hook("not-a-channel-sess")
        adapter.send_response.assert_not_called()

    def test_hook_skips_unknown_platform(
        self, route_env: tuple, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import mewbo_api.channels.routes as routes

        rt, _, registry = route_env
        rt.session_store.load_transcript.return_value = [
            {
                "type": "context",
                "payload": {
                    "source_platform": "unknown-platform",
                    "channel_id": "x",
                    "reply_to_message_id": "1",
                },
            },
            {"type": "completion", "payload": {"task_result": "reply"}},
        ]
        # Should not raise even if adapter not found
        routes._channel_completion_hook("sess-unknown-platform")

    def test_hook_skips_when_no_final_text(
        self, route_env: tuple, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import mewbo_api.channels.routes as routes

        rt, adapter, _ = route_env
        rt.session_store.load_transcript.return_value = [
            {
                "type": "context",
                "payload": {
                    "source_platform": "nextcloud-talk",
                    "channel_id": "room1",
                    "thread_id": None,
                    "reply_to_message_id": "42",
                },
            },
            # No completion or assistant event → no final text
        ]
        adapter.send_response = MagicMock()
        routes._channel_completion_hook("sess-no-text")
        adapter.send_response.assert_not_called()


# ---------------------------------------------------------------------------
# webhook_receive endpoint
# ---------------------------------------------------------------------------


@pytest.fixture()
def webhook_client(monkeypatch: pytest.MonkeyPatch):
    """Thin Flask test client with channels blueprint registered."""
    import mewbo_api.channels.routes as routes
    from flask import Flask
    from mewbo_api.channels.base import ChannelRegistry, DeduplicationGuard

    rt = _make_runtime()
    rt.session_store.load_transcript.return_value = []
    adapter = _nc_adapter()
    adapter.send_response = MagicMock(return_value="sent")

    registry = ChannelRegistry()
    registry.register(adapter)

    monkeypatch.setattr(routes, "_runtime", rt)
    monkeypatch.setattr(routes, "_registry", registry)
    monkeypatch.setattr(routes, "_dedup", DeduplicationGuard(ttl=60.0))
    monkeypatch.setattr(routes, "_hook_manager", MagicMock())

    app = Flask("webhook-test")
    app.register_blueprint(routes.channel_bp)
    app.config["TESTING"] = True

    return app.test_client(), rt, adapter


class TestWebhookEndpoint:
    def test_no_runtime_returns_500(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import mewbo_api.channels.routes as routes
        from flask import Flask
        from mewbo_api.channels.base import ChannelRegistry, DeduplicationGuard

        monkeypatch.setattr(routes, "_runtime", None)
        monkeypatch.setattr(routes, "_registry", ChannelRegistry())
        monkeypatch.setattr(routes, "_dedup", DeduplicationGuard())

        app = Flask("no-rt-test")
        app.register_blueprint(routes.channel_bp)
        app.config["TESTING"] = True

        body = _nc_payload()
        resp = app.test_client().post(
            "/api/webhooks/nextcloud-talk",
            data=body,
            content_type="application/json",
        )
        assert resp.status_code == 500

    def test_unknown_platform_returns_404(self, webhook_client: tuple) -> None:
        c, _, _ = webhook_client
        resp = c.post("/api/webhooks/slack", data=b"{}", content_type="application/json")
        assert resp.status_code == 404

    def test_invalid_hmac_returns_401(self, webhook_client: tuple) -> None:
        c, _, _ = webhook_client
        body = _nc_payload()
        resp = c.post(
            "/api/webhooks/nextcloud-talk",
            data=body,
            content_type="application/json",
            headers={
                "X-Nextcloud-Talk-Signature": "badhash",
                "X-Nextcloud-Talk-Random": "rand",
                "X-Nextcloud-Talk-Backend": "https://nc.example.com",
            },
        )
        assert resp.status_code == 401

    def test_valid_request_returns_200(self, webhook_client: tuple) -> None:
        c, rt, _ = webhook_client
        body = _nc_payload()
        headers = _signed_headers(body)
        resp = c.post(
            "/api/webhooks/nextcloud-talk",
            data=body,
            content_type="application/json",
            headers=headers,
        )
        assert resp.status_code == 200

    def test_non_create_event_returns_200_silently(self, webhook_client: tuple) -> None:
        """Update/Delete events produce a None parse_inbound → 200 with no LLM."""
        c, rt, _ = webhook_client
        body = _nc_payload(event_type="Delete")
        headers = _signed_headers(body)
        resp = c.post(
            "/api/webhooks/nextcloud-talk",
            data=body,
            content_type="application/json",
            headers=headers,
        )
        assert resp.status_code == 200
        rt.start_async.assert_not_called()


# ---------------------------------------------------------------------------
# _build_help_text / _format_project_list
# ---------------------------------------------------------------------------


def test_build_help_text_includes_all_commands() -> None:
    from mewbo_api.channels.routes import _COMMANDS, _build_help_text

    text = _build_help_text()
    for cmd_name in _COMMANDS:
        assert f"/{cmd_name}" in text


def test_format_project_list_empty() -> None:
    from mewbo_api.channels.routes import _format_project_list

    text = _format_project_list({}, "Header")
    assert "Header" in text
    assert "none" in text


def test_format_project_list_with_projects() -> None:
    from mewbo_api.channels.routes import _format_project_list

    proj = MagicMock()
    proj.description = "A cool project"
    text = _format_project_list({"myproj": proj}, "Header")
    assert "myproj" in text
    assert "A cool project" in text


# ---------------------------------------------------------------------------
# init_channels wiring
# ---------------------------------------------------------------------------


def test_init_channels_registers_nextcloud_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mewbo_api.channels.routes as routes
    from mewbo_api.channels.base import ChannelRegistry, DeduplicationGuard

    monkeypatch.setattr(routes, "_registry", ChannelRegistry())
    monkeypatch.setattr(routes, "_dedup", DeduplicationGuard())

    rt = MagicMock()
    hm = MagicMock()
    hm.on_session_end = []

    cfg = MagicMock()
    cfg.channels = {
        "nextcloud-talk": {
            "enabled": True,
            "bot_secret": SECRET,
            "nextcloud_url": "https://nc.example.com",
        }
    }

    from flask import Flask

    app = Flask("init-test")
    routes.init_channels(app, rt, hm, cfg)

    assert routes._registry.get("nextcloud-talk") is not None
    assert len(hm.on_session_end) == 1  # completion hook appended


def test_init_channels_noop_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mewbo_api.channels.routes as routes
    from mewbo_api.channels.base import ChannelRegistry, DeduplicationGuard

    monkeypatch.setattr(routes, "_registry", ChannelRegistry())
    monkeypatch.setattr(routes, "_dedup", DeduplicationGuard())

    rt = MagicMock()
    hm = MagicMock()
    hm.on_session_end = []

    cfg = MagicMock()
    cfg.channels = {"nextcloud-talk": {"enabled": False, "bot_secret": SECRET}}

    from flask import Flask

    app = Flask("init-test-disabled")
    routes.init_channels(app, rt, hm, cfg)

    assert routes._registry.get("nextcloud-talk") is None


def test_init_channels_email_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Email adapter is registered when email channel is enabled."""
    import mewbo_api.channels.routes as routes
    from mewbo_api.channels.base import ChannelRegistry, DeduplicationGuard

    monkeypatch.setattr(routes, "_registry", ChannelRegistry())
    monkeypatch.setattr(routes, "_dedup", DeduplicationGuard())

    rt = MagicMock()
    hm = MagicMock()
    hm.on_session_end = []

    cfg = MagicMock()
    cfg.channels = {
        "email": {
            "enabled": True,
            "smtp_host": "smtp.example.com",
            "imap_host": "imap.example.com",
            "username": "bot@example.com",
            "password": "pw",
        }
    }

    from flask import Flask

    app = Flask("init-email-test")
    # Patch EmailPoller.start to avoid real IMAP thread
    with patch("mewbo_api.channels.email_adapter.EmailPoller.start"):
        routes.init_channels(app, rt, hm, cfg)

    assert routes._registry.get("email") is not None


# ---------------------------------------------------------------------------
# _get_active_project_cwd
# ---------------------------------------------------------------------------


def test_get_active_project_cwd_returns_cwd(route_env: tuple) -> None:
    import mewbo_api.channels.routes as routes

    rt, _, _ = route_env
    rt.session_store.load_transcript.return_value = [
        {"type": "context", "payload": {"active_project_cwd": "/workspace/myproject"}},
    ]
    cwd = routes._get_active_project_cwd("any-sess")
    assert cwd == "/workspace/myproject"


def test_get_active_project_cwd_returns_none_when_absent(route_env: tuple) -> None:
    import mewbo_api.channels.routes as routes

    rt, _, _ = route_env
    rt.session_store.load_transcript.return_value = [
        {"type": "context", "payload": {"active_project": "foo"}},
    ]
    assert routes._get_active_project_cwd("any-sess") is None
