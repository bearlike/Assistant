"""Tests for the email channel adapter, poller, and markdown rendering."""

from __future__ import annotations

import email as email_stdlib
import email.utils
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import MagicMock, patch

from meeseeks_api.channels.base import ChannelAdapter, InboundMessage
from meeseeks_api.channels.email_adapter import (
    EmailAdapter,
    EmailPoller,
    _derive_thread_id,
    render_markdown_html,
)

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_email(
    *,
    from_addr: str = "alice@example.com",
    to_addr: str = "meeseeks@example.com",
    cc: str | None = None,
    subject: str = "Test Subject",
    body: str = "Hello, Meeseeks!",
    message_id: str = "<msg-001@example.com>",
    in_reply_to: str | None = None,
    references: str | None = None,
) -> email_stdlib.message.Message:
    """Build a realistic email message for testing."""
    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg["From"] = f"Alice <{from_addr}>"
    msg["To"] = to_addr
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = email.utils.formatdate(localtime=True)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    return msg


def _make_adapter(**kwargs) -> EmailAdapter:
    """Create an EmailAdapter with test defaults."""
    defaults = {
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "username": "meeseeks@example.com",
        "password": "test-password",
        "allowed_senders": ["alice@example.com", "bob@example.com"],
    }
    defaults.update(kwargs)
    return EmailAdapter(**defaults)


# ------------------------------------------------------------------
# EmailAdapter.parse_email
# ------------------------------------------------------------------


class TestParseEmail:
    """Test MIME email → InboundMessage conversion."""

    def test_basic_email(self) -> None:
        adapter = _make_adapter()
        msg = _make_email()
        result = adapter.parse_email(msg)

        assert result is not None
        assert result.platform == "email"
        assert result.channel_id == "alice@example.com"
        assert result.sender_id == "alice@example.com"
        assert result.sender_name == "Alice"
        assert result.text == "Hello, Meeseeks!"
        assert result.room_name == "Test Subject"
        assert result.message_id == "<msg-001@example.com>"

    def test_allowed_senders_rejects_unlisted(self) -> None:
        adapter = _make_adapter()
        msg = _make_email(from_addr="stranger@evil.com")
        result = adapter.parse_email(msg)
        assert result is None

    def test_allowed_senders_case_insensitive(self) -> None:
        adapter = _make_adapter()
        msg = _make_email(from_addr="Alice@Example.COM")
        result = adapter.parse_email(msg)
        assert result is not None

    def test_empty_allowed_senders_accepts_all(self) -> None:
        adapter = _make_adapter(allowed_senders=[])
        msg = _make_email(from_addr="anyone@anywhere.com")
        result = adapter.parse_email(msg)
        assert result is not None

    def test_allowed_recipients_rejects_wrong_address(self) -> None:
        adapter = _make_adapter(
            allowed_recipients=["meeseeks@example.com"],
        )
        msg = _make_email(to_addr="someone-else@example.com")
        result = adapter.parse_email(msg)
        assert result is None

    def test_allowed_recipients_accepts_matching_address(self) -> None:
        adapter = _make_adapter(
            allowed_recipients=["meeseeks@example.com"],
        )
        msg = _make_email(to_addr="meeseeks@example.com")
        result = adapter.parse_email(msg)
        assert result is not None

    def test_allowed_recipients_matches_cc(self) -> None:
        adapter = _make_adapter(
            allowed_recipients=["meeseeks@example.com"],
        )
        msg = _make_email(
            to_addr="someone@example.com",
            cc="meeseeks@example.com",
        )
        result = adapter.parse_email(msg)
        assert result is not None

    def test_allowed_recipients_case_insensitive(self) -> None:
        adapter = _make_adapter(
            allowed_recipients=["meeseeks@example.com"],
        )
        msg = _make_email(to_addr="Meeseeks@Example.COM")
        result = adapter.parse_email(msg)
        assert result is not None

    def test_empty_allowed_recipients_accepts_all(self) -> None:
        adapter = _make_adapter(allowed_recipients=[])
        msg = _make_email(to_addr="random@other.com")
        result = adapter.parse_email(msg)
        assert result is not None

    def test_multipart_extracts_text_plain(self) -> None:
        adapter = _make_adapter()
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText("plain text body", "plain", "utf-8"))
        msg.attach(MIMEText("<p>html body</p>", "html", "utf-8"))
        msg["From"] = "alice@example.com"
        msg["To"] = "meeseeks@example.com"
        msg["Subject"] = "Multi"
        msg["Message-ID"] = "<multi@example.com>"
        msg["Date"] = email.utils.formatdate()

        result = adapter.parse_email(msg)
        assert result is not None
        assert result.text == "plain text body"

    def test_recipients_in_raw(self) -> None:
        adapter = _make_adapter()
        msg = _make_email(
            to_addr="meeseeks@example.com",
            cc="bob@example.com, charlie@example.com",
        )
        result = adapter.parse_email(msg)
        assert result is not None
        recipients = result.raw["recipients"]
        assert "meeseeks@example.com" in recipients
        assert "bob@example.com" in recipients
        assert "charlie@example.com" in recipients


# ------------------------------------------------------------------
# Thread ID extraction
# ------------------------------------------------------------------


class TestThreadIdExtraction:
    """Test email thread tracking via Message-ID / References headers."""

    def test_new_email_uses_own_message_id(self) -> None:
        msg = _make_email(message_id="<new-msg@example.com>")
        assert _derive_thread_id(msg) == "<new-msg@example.com>"

    def test_reply_uses_in_reply_to(self) -> None:
        msg = _make_email(
            message_id="<reply@example.com>",
            in_reply_to="<original@example.com>",
        )
        assert _derive_thread_id(msg) == "<original@example.com>"

    def test_deep_thread_uses_first_reference(self) -> None:
        msg = _make_email(
            message_id="<msg-3@example.com>",
            in_reply_to="<msg-2@example.com>",
            references="<msg-1@example.com> <msg-2@example.com>",
        )
        # First reference is the thread root
        assert _derive_thread_id(msg) == "<msg-1@example.com>"

    def test_thread_id_populates_session_tag(self) -> None:
        adapter = _make_adapter()
        root_msg = _make_email(message_id="<root@example.com>")
        reply_msg = _make_email(
            message_id="<reply@example.com>",
            in_reply_to="<root@example.com>",
            references="<root@example.com>",
        )

        root_result = adapter.parse_email(root_msg)
        reply_result = adapter.parse_email(reply_msg)

        assert root_result is not None
        assert reply_result is not None
        # Both should use the root message ID as thread_id
        assert root_result.thread_id == "<root@example.com>"
        assert reply_result.thread_id == "<root@example.com>"


# ------------------------------------------------------------------
# Mention gating (requires_mention)
# ------------------------------------------------------------------


class TestRequiresMention:
    """Test multi-party vs 1-to-1 mention logic."""

    def test_single_recipient_no_mention_needed(self) -> None:
        adapter = _make_adapter()
        msg = _make_email(to_addr="meeseeks@example.com")
        inbound = adapter.parse_email(msg)
        assert inbound is not None
        assert adapter.requires_mention(inbound) is False

    def test_multiple_recipients_mention_required(self) -> None:
        adapter = _make_adapter()
        msg = _make_email(
            to_addr="meeseeks@example.com",
            cc="bob@example.com",
        )
        inbound = adapter.parse_email(msg)
        assert inbound is not None
        assert adapter.requires_mention(inbound) is True

    def test_trigger_keyword_is_at_meeseeks(self) -> None:
        adapter = _make_adapter()
        assert adapter.trigger_keyword == "@Meeseeks"


# ------------------------------------------------------------------
# send_response
# ------------------------------------------------------------------


class TestSendResponse:
    """Test SMTP email sending with proper threading headers."""

    @patch("meeseeks_api.channels.email_adapter.smtplib.SMTP")
    def test_sends_html_email(self, mock_smtp_cls: MagicMock) -> None:
        mock_smtp = MagicMock()
        mock_smtp_cls.return_value = mock_smtp

        adapter = _make_adapter()
        # Populate thread metadata
        msg = _make_email(subject="Important Task")
        adapter.parse_email(msg)

        result = adapter.send_response(
            channel_id="alice@example.com",
            text="**Done!** Task completed.",
            thread_id="<msg-001@example.com>",
            reply_to="<msg-001@example.com>",
        )

        assert result is not None
        mock_smtp.login.assert_called_once()
        mock_smtp.sendmail.assert_called_once()

        # Parse the sent MIME message to verify content
        sent_args = mock_smtp.sendmail.call_args
        raw_email = sent_args[0][2]
        parsed = email_stdlib.message_from_string(raw_email)
        assert parsed["Subject"] == "Re: Important Task"
        assert parsed["In-Reply-To"] == "<msg-001@example.com>"
        assert "<msg-001@example.com>" in parsed.get("References", "")

        # Check decoded HTML part
        parts = list(parsed.walk())
        html_parts = [p for p in parts if p.get_content_type() == "text/html"]
        assert html_parts, "No text/html part found"
        html_body = html_parts[0].get_payload(decode=True).decode()
        assert "<strong>Done!</strong>" in html_body

    @patch("meeseeks_api.channels.email_adapter.smtplib.SMTP")
    def test_plain_text_fallback_included(self, mock_smtp_cls: MagicMock) -> None:
        mock_smtp = MagicMock()
        mock_smtp_cls.return_value = mock_smtp

        adapter = _make_adapter()
        msg = _make_email()
        adapter.parse_email(msg)

        adapter.send_response(
            channel_id="alice@example.com",
            text="Plain markdown text",
            thread_id="<msg-001@example.com>",
        )

        raw_email = mock_smtp.sendmail.call_args[0][2]
        parsed = email_stdlib.message_from_string(raw_email)
        plain_parts = [p for p in parsed.walk() if p.get_content_type() == "text/plain"]
        assert plain_parts, "No text/plain part found"
        plain_body = plain_parts[0].get_payload(decode=True).decode()
        assert "Plain markdown text" in plain_body

    @patch("meeseeks_api.channels.email_adapter.smtplib.SMTP")
    def test_smtp_failure_returns_none(self, mock_smtp_cls: MagicMock) -> None:
        mock_smtp_cls.side_effect = ConnectionRefusedError("no SMTP")
        adapter = _make_adapter()
        result = adapter.send_response(
            channel_id="alice@example.com",
            text="test",
        )
        assert result is None

    @patch("meeseeks_api.channels.email_adapter.smtplib.SMTP_SSL")
    def test_ssl_mode(self, mock_smtp_ssl_cls: MagicMock) -> None:
        mock_smtp = MagicMock()
        mock_smtp_ssl_cls.return_value = mock_smtp

        adapter = _make_adapter(smtp_ssl=True, smtp_starttls=False)
        adapter.send_response(
            channel_id="alice@example.com",
            text="test",
        )
        mock_smtp_ssl_cls.assert_called_once()
        mock_smtp.starttls.assert_not_called()


# ------------------------------------------------------------------
# send_reaction (Gmail emoji reaction)
# ------------------------------------------------------------------


class TestSendReaction:
    """Test Gmail emoji reaction MIME construction."""

    @patch("meeseeks_api.channels.email_adapter.smtplib.SMTP")
    def test_send_reaction_builds_gmail_mime(self, mock_smtp_cls: MagicMock) -> None:
        mock_smtp = MagicMock()
        mock_smtp_cls.return_value = mock_smtp

        adapter = _make_adapter()
        msg = _make_email(subject="Hello")
        adapter.parse_email(msg)

        result = adapter.send_reaction(
            channel_id="alice@example.com",
            emoji="\N{EYES}",
            reply_to="<msg-001@example.com>",
            thread_id="<msg-001@example.com>",
        )

        assert result is not None
        mock_smtp.sendmail.assert_called_once()

        # Parse the sent MIME message
        raw = mock_smtp.sendmail.call_args[0][2]
        parsed = email_stdlib.message_from_string(raw)

        assert parsed["In-Reply-To"] == "<msg-001@example.com>"

        # Must have the Gmail reaction MIME part
        content_types = [p.get_content_type() for p in parsed.walk()]
        assert "text/vnd.google.email-reaction+json" in content_types
        assert "text/plain" in content_types
        assert "text/html" in content_types

        # Verify reaction JSON
        reaction_parts = [
            p
            for p in parsed.walk()
            if p.get_content_type() == "text/vnd.google.email-reaction+json"
        ]
        import json

        payload = json.loads(reaction_parts[0].get_payload(decode=True).decode())
        assert payload == {"emoji": "\N{EYES}", "version": 1}

    @patch("meeseeks_api.channels.email_adapter.smtplib.SMTP")
    def test_send_reaction_smtp_failure_returns_none(
        self,
        mock_smtp_cls: MagicMock,
    ) -> None:
        mock_smtp_cls.side_effect = ConnectionRefusedError("no SMTP")
        adapter = _make_adapter()
        result = adapter.send_reaction(
            channel_id="alice@example.com",
            emoji="\N{EYES}",
            reply_to="<msg-001@example.com>",
        )
        assert result is None

    @patch("meeseeks_api.channels.email_adapter.smtplib.SMTP")
    def test_send_reaction_fallback_text(self, mock_smtp_cls: MagicMock) -> None:
        mock_smtp = MagicMock()
        mock_smtp_cls.return_value = mock_smtp

        adapter = _make_adapter()
        adapter.send_reaction(
            channel_id="alice@example.com",
            emoji="\N{THUMBS UP SIGN}",
            reply_to="<msg-001@example.com>",
        )

        raw = mock_smtp.sendmail.call_args[0][2]
        parsed = email_stdlib.message_from_string(raw)
        plain_parts = [p for p in parsed.walk() if p.get_content_type() == "text/plain"]
        plain_body = plain_parts[0].get_payload(decode=True).decode()
        assert "Reacted with" in plain_body
        assert "\N{THUMBS UP SIGN}" in plain_body


# ------------------------------------------------------------------
# Markdown rendering
# ------------------------------------------------------------------


class TestMarkdownRendering:
    """Test markdown → styled HTML conversion."""

    def test_bold_renders(self) -> None:
        html = render_markdown_html("**bold text**")
        assert "<strong>bold text</strong>" in html

    def test_code_block_styled(self) -> None:
        html = render_markdown_html("```\nprint(42)\n```")
        assert "<pre" in html
        assert "print(42)" in html
        assert "font-family" in html  # Inline styles applied

    def test_table_renders(self) -> None:
        md = "| A | B |\n|---|---|\n| 1 | 2 |"
        html = render_markdown_html(md)
        assert "<table" in html
        assert "<td" in html
        assert "border" in html  # Inline styles

    def test_email_template_wraps(self) -> None:
        html = render_markdown_html("Hello")
        assert "Meeseeks" in html  # Header branding
        assert "<!DOCTYPE html>" in html
        assert "agent hypervisor" in html  # Footer


# ------------------------------------------------------------------
# EmailPoller lifecycle
# ------------------------------------------------------------------


class TestEmailPoller:
    """Test poller start/stop lifecycle (no real IMAP)."""

    def test_start_creates_daemon_thread(self) -> None:
        adapter = _make_adapter()
        process_fn = MagicMock()

        poller = EmailPoller(
            adapter=adapter,
            imap_host="imap.example.com",
            username="meeseeks@example.com",
            password="test",
            process_fn=process_fn,
        )

        with patch.object(poller, "_poll_loop"):
            poller.start()
            assert poller._thread is not None
            assert poller._thread.daemon is True
            poller.stop()

    def test_stop_sets_event(self) -> None:
        adapter = _make_adapter()
        poller = EmailPoller(
            adapter=adapter,
            imap_host="imap.example.com",
            username="meeseeks@example.com",
            password="test",
            process_fn=MagicMock(),
        )
        poller.stop()
        assert poller._stop_event.is_set()

    def test_poll_interval_has_floor(self) -> None:
        adapter = _make_adapter()
        poller = EmailPoller(
            adapter=adapter,
            imap_host="imap.example.com",
            username="meeseeks@example.com",
            password="test",
            poll_interval=1,  # Too low
            process_fn=MagicMock(),
        )
        assert poller._poll_interval >= 5

    @patch("meeseeks_api.channels.email_adapter.imapclient.IMAPClient")
    def test_poll_loop_processes_unseen(
        self,
        mock_imap_cls: MagicMock,
    ) -> None:
        """Verify the poller fetches UNSEEN, parses, and marks SEEN."""
        adapter = _make_adapter()
        process_fn = MagicMock()

        # Build a raw email for the mock to return
        test_email = _make_email(body="@Meeseeks do something")
        raw_bytes = test_email.as_bytes()

        mock_client = MagicMock()
        mock_imap_cls.return_value = mock_client
        mock_client.search.return_value = [101]
        mock_client.fetch.return_value = {
            101: {b"RFC822": raw_bytes},
        }

        poller = EmailPoller(
            adapter=adapter,
            imap_host="imap.example.com",
            username="meeseeks@example.com",
            password="test",
            process_fn=process_fn,
        )

        # Run a single poll cycle manually
        client = poller._connect()
        uids = client.search(["UNSEEN"])
        fetched = client.fetch(uids, ["RFC822"])
        for uid, data in fetched.items():
            poller._handle_email(uid, data, client)

        # Verify process_fn was called with adapter and an InboundMessage
        process_fn.assert_called_once()
        call_args = process_fn.call_args[0]
        assert call_args[0] is adapter
        assert isinstance(call_args[1], InboundMessage)
        assert call_args[1].text == "@Meeseeks do something"

        # Verify email marked as SEEN
        mock_client.add_flags.assert_called_once()


# ------------------------------------------------------------------
# Protocol compliance
# ------------------------------------------------------------------


class TestEmailProtocolCompliance:
    """Verify EmailAdapter satisfies the ChannelAdapter protocol."""

    def test_is_channel_adapter(self) -> None:
        adapter = _make_adapter()
        assert isinstance(adapter, ChannelAdapter)

    def test_verify_request_always_true(self) -> None:
        adapter = _make_adapter()
        assert adapter.verify_request({}, b"") is True

    def test_parse_inbound_returns_none(self) -> None:
        adapter = _make_adapter()
        assert adapter.parse_inbound({}, b"") is None

    def test_system_context_mentions_email(self) -> None:
        adapter = _make_adapter()
        ctx = adapter.system_context
        assert "email" in ctx.lower()
        assert "markdown" in ctx.lower()
