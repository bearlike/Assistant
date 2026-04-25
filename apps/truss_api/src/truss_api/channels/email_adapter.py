"""Email channel adapter — IMAP polling + SMTP replies.

Implements the :class:`ChannelAdapter` protocol so that an email
mailbox becomes a Truss chat channel.  New emails start sessions;
replies in the same thread continue the session.  Agent markdown
responses are rendered as styled HTML emails.

Access control:

* **allowed_senders** gates who may interact (mandatory).
* **1-to-1** emails (no CC) are always processed — no mention needed.
* **Multi-party** threads require ``@Truss`` in the body.
"""

from __future__ import annotations

import email as email_stdlib
import email.utils
import json
import smtplib
import threading
from dataclasses import dataclass, field
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import TYPE_CHECKING, Any

import imapclient
import mistune
from jinja2 import Template
from truss_core.common import get_logger

from truss_api.channels.base import InboundMessage

if TYPE_CHECKING:
    from collections.abc import Callable
    from email.message import Message

logger = get_logger(name="channels.email")


# ------------------------------------------------------------------
# Thread metadata (for constructing reply headers)
# ------------------------------------------------------------------


@dataclass
class _ThreadMeta:
    """Minimal metadata needed to construct a proper email reply."""

    subject: str
    references: list[str] = field(default_factory=list)
    latest_message_id: str = ""


# ------------------------------------------------------------------
# Markdown → HTML rendering
# ------------------------------------------------------------------

_TEMPLATE_PATH = Path(__file__).with_name("email_template.html.j2")
_template_cache: Template | None = None

# Inline styles injected into mistune output by wrapping common tags.
_INLINE_STYLES: dict[str, str] = {
    "pre": (
        "background-color:#f5f5f5;padding:12px 16px;border-radius:6px;"
        "overflow-x:auto;font-family:'SFMono-Regular',Consolas,"
        "'Liberation Mono',Menlo,monospace;font-size:13px;line-height:1.45;"
    ),
    "code": (
        "font-family:'SFMono-Regular',Consolas,'Liberation Mono',Menlo,monospace;font-size:13px;"
    ),
    "blockquote": ("border-left:4px solid #ddd;margin:0;padding:0 16px;color:#666;"),
    "table": ("border-collapse:collapse;width:100%;margin:16px 0;"),
    "th": (
        "border:1px solid #ddd;padding:8px 12px;text-align:left;"
        "background-color:#f9f9f9;font-weight:600;"
    ),
    "td": ("border:1px solid #ddd;padding:8px 12px;"),
    "a": "color:#0366d6;text-decoration:none;",
    "h1": "font-size:24px;margin:24px 0 8px;font-weight:600;",
    "h2": "font-size:20px;margin:20px 0 8px;font-weight:600;",
    "h3": "font-size:16px;margin:16px 0 8px;font-weight:600;",
    "hr": "border:none;border-top:1px solid #eee;margin:24px 0;",
    "img": "max-width:100%;height:auto;",
}


def _load_email_template() -> Template:
    """Load and cache the Jinja2 email wrapper template."""
    global _template_cache  # noqa: PLW0603
    if _template_cache is None:
        _template_cache = Template(_TEMPLATE_PATH.read_text())
    return _template_cache


def _inline_styles(html: str) -> str:
    """Inject inline styles into common HTML tags for email clients."""
    for tag, style in _INLINE_STYLES.items():
        # Handle both <tag> and <tag attr="..."> forms
        html = html.replace(f"<{tag}>", f'<{tag} style="{style}">')
        html = html.replace(f"<{tag} ", f'<{tag} style="{style}" ')
    return html


def render_markdown_html(text: str) -> str:
    """Convert markdown to styled HTML suitable for email clients."""
    md = mistune.create_markdown(
        plugins=["table", "task_lists", "strikethrough"],
    )
    body_html = _inline_styles(str(md(text)))
    template = _load_email_template()
    return template.render(body=body_html)


# ------------------------------------------------------------------
# Email parsing helpers
# ------------------------------------------------------------------


def _decode_header_value(value: str | None) -> str:
    """Decode an RFC 2047 encoded header value."""
    if not value:
        return ""
    parts: list[str] = []
    for fragment, charset in decode_header(value):
        if isinstance(fragment, bytes):
            parts.append(fragment.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(fragment)
    return " ".join(parts)


def _extract_email_address(header: str) -> str:
    """Extract the bare email address from a From/To header value."""
    _, addr = email.utils.parseaddr(header)
    return addr.lower()


def _extract_all_recipients(msg: Message) -> list[str]:
    """Collect all recipient addresses from To/Cc headers."""
    recipients: list[str] = []
    for hdr in ("To", "Cc"):
        raw = msg.get(hdr, "")
        if raw:
            for _, addr in email.utils.getaddresses([raw]):
                if addr:
                    recipients.append(addr.lower())
    return recipients


def _extract_text_body(msg: Message) -> str:
    """Extract plain-text body from a MIME message."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and part.get_content_disposition() != "attachment":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if isinstance(payload, bytes):
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


def _parse_references(msg: Message) -> list[str]:
    """Parse the References header into a list of Message-IDs."""
    raw = msg.get("References", "")
    if not raw:
        return []
    # References header: space-separated list of <msg-id> values
    return [mid.strip() for mid in raw.split() if mid.strip()]


def _derive_thread_id(msg: Message) -> str:
    """Derive the thread root Message-ID for session tagging.

    Priority: References[0] > In-Reply-To > own Message-ID.
    """
    refs = _parse_references(msg)
    if refs:
        return refs[0]
    reply_to = msg.get("In-Reply-To", "").strip()
    if reply_to:
        return reply_to
    return msg.get("Message-ID", "").strip()


# ------------------------------------------------------------------
# EmailAdapter
# ------------------------------------------------------------------


class EmailAdapter:
    """Channel adapter that bridges an email mailbox to Truss sessions.

    Implements the ``ChannelAdapter`` protocol.  Outbound responses are
    sent as styled HTML emails via SMTP.
    """

    platform: str = "email"
    trigger_keyword: str = "@Truss"

    def __init__(  # noqa: D107
        self,
        *,
        smtp_host: str,
        smtp_port: int = 587,
        smtp_ssl: bool = False,
        smtp_starttls: bool = True,
        username: str,
        password: str,
        from_address: str | None = None,
        allowed_senders: list[str] | None = None,
        allowed_recipients: list[str] | None = None,
    ) -> None:
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._smtp_ssl = smtp_ssl
        self._smtp_starttls = smtp_starttls
        self._username = username
        self._password = password
        self._from_address = from_address or username
        self._allowed: set[str] = {s.lower() for s in (allowed_senders or [])}
        self._allowed_recipients: set[str] = {r.lower() for r in (allowed_recipients or [])}
        # Thread metadata for constructing reply headers
        self._thread_meta: dict[str, _ThreadMeta] = {}

    # -- ChannelAdapter protocol stubs (not used for polled channels) --

    def verify_request(
        self,
        headers: dict[str, str],
        body: bytes,
    ) -> bool:
        """Always True — email is polled, not pushed via webhook."""
        return True

    def parse_inbound(
        self,
        headers: dict[str, str],
        body: bytes,
    ) -> InboundMessage | None:
        """Not used by the IMAP poller.  Returns None."""
        return None

    # -- Email-specific parsing --

    def parse_email(self, msg: Message) -> InboundMessage | None:
        """Parse a fetched IMAP email into an InboundMessage.

        Returns ``None`` for emails from disallowed senders.
        """
        from_header = msg.get("From", "")
        sender_email = _extract_email_address(from_header)
        sender_name = _decode_header_value(from_header).split("<")[0].strip()
        if not sender_name:
            sender_name = sender_email

        # Access control: reject unlisted senders
        if self._allowed and sender_email not in self._allowed:
            logger.debug("Email from %s rejected (not in allowed_senders)", sender_email)
            return None

        # Access control: reject if not addressed to an allowed recipient
        recipients = _extract_all_recipients(msg)
        if self._allowed_recipients and not (
            self._allowed_recipients & {r.lower() for r in recipients}
        ):
            logger.debug(
                "Email to %s rejected (no allowed_recipients match)",
                recipients,
            )
            return None

        subject = _decode_header_value(msg.get("Subject", ""))
        message_id = msg.get("Message-ID", "").strip()
        thread_id = _derive_thread_id(msg)
        date_str = msg.get("Date", "")
        text = _extract_text_body(msg)
        references = _parse_references(msg)

        # Store thread metadata for reply construction
        meta = self._thread_meta.get(thread_id)
        if meta is None:
            meta = _ThreadMeta(subject=subject, references=references)
            self._thread_meta[thread_id] = meta
        meta.latest_message_id = message_id
        if message_id and message_id not in meta.references:
            meta.references.append(message_id)

        return InboundMessage(
            platform="email",
            channel_id=sender_email,
            thread_id=thread_id,
            message_id=message_id,
            sender_id=sender_email,
            sender_name=sender_name or sender_email,
            text=text.strip(),
            timestamp=date_str,
            room_name=subject,
            raw={"recipients": recipients, "subject": subject},
        )

    # -- Mention gating (consulted by _is_mentioned in routes.py) --

    def requires_mention(self, message: InboundMessage) -> bool:
        """Return True when @Truss must appear in the message.

        Direct 1-to-1 emails (single recipient) skip mention gating.
        Multi-party threads (multiple To/Cc recipients) require the
        trigger keyword so the bot doesn't reply to every message.
        """
        recipients = list(message.raw.get("recipients", []))
        # Single recipient (the bot's mailbox) → no mention needed
        return len(recipients) > 1

    # -- Shared SMTP send --

    def _smtp_send(self, mime_msg: MIMEMultipart, recipient: str) -> str | None:
        """Send a MIME message via SMTP.  Returns Message-ID or None."""
        try:
            conn: smtplib.SMTP
            if self._smtp_ssl:
                conn = smtplib.SMTP_SSL(self._smtp_host, self._smtp_port, timeout=30)
            else:
                conn = smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=30)
                if self._smtp_starttls:
                    conn.starttls()
            conn.login(self._username, self._password)
            conn.sendmail(self._from_address, [recipient], mime_msg.as_string())
            conn.quit()
            return mime_msg["Message-ID"]
        except Exception:
            logger.warning(
                "Failed to send email to %s",
                recipient,
                exc_info=True,
            )
            return None

    def _build_thread_headers(
        self,
        msg: MIMEMultipart,
        channel_id: str,
        thread_id: str | None,
        reply_to: str | None,
    ) -> None:
        """Set From, To, Subject, In-Reply-To, References, Message-ID."""
        msg["From"] = self._from_address
        msg["To"] = channel_id

        meta = self._thread_meta.get(thread_id or "") if thread_id else None
        subject = meta.subject if meta else "Truss"
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        msg["Subject"] = subject

        if reply_to:
            msg["In-Reply-To"] = reply_to
        if meta and meta.references:
            msg["References"] = " ".join(meta.references)

        msg["Message-ID"] = email.utils.make_msgid(
            domain=self._from_address.split("@")[-1],
        )

    # -- Send response as HTML email --

    def send_response(
        self,
        channel_id: str,
        text: str,
        thread_id: str | None = None,
        reply_to: str | None = None,
    ) -> str | None:
        """Send a styled HTML email reply."""
        html = render_markdown_html(text)

        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
        self._build_thread_headers(msg, channel_id, thread_id, reply_to)

        result = self._smtp_send(msg, channel_id)
        if result:
            logger.info("Sent email reply to %s (subject: %s)", channel_id, msg["Subject"])
        return result

    # -- Send Gmail emoji reaction --

    def send_reaction(
        self,
        channel_id: str,
        emoji: str,
        reply_to: str,
        thread_id: str | None = None,
    ) -> str | None:
        """Send a Gmail emoji reaction to an email.

        Uses the proprietary ``text/vnd.google.email-reaction+json``
        MIME type.  Gmail renders this as a reaction badge on the
        original message.  Non-Gmail clients see a fallback text reply.
        """
        msg = MIMEMultipart("alternative")
        # Order: text/plain, reaction JSON, text/html (Gmail recommendation)
        msg.attach(MIMEText(f"Reacted with {emoji}", "plain", "utf-8"))
        msg.attach(
            MIMEText(
                json.dumps({"emoji": emoji, "version": 1}),
                "vnd.google.email-reaction+json",
                "utf-8",
            )
        )
        msg.attach(MIMEText(f"<p>Reacted with {emoji}</p>", "html", "utf-8"))
        self._build_thread_headers(msg, channel_id, thread_id, reply_to)

        result = self._smtp_send(msg, channel_id)
        if result:
            logger.info("Sent %s reaction to %s", emoji, channel_id)
        return result

    @property
    def system_context(self) -> str:
        """Brief system prompt note for LLM awareness."""
        from truss_api.channels.routes import _COMMANDS

        cmds = ", ".join(f"/{c}" for c in _COMMANDS)
        return (
            "This conversation is via email. "
            f"The user writes to {self._from_address}. "
            f"Quick commands: {cmds}. "
            "Respond in well-formatted markdown — it will be "
            "rendered as HTML in the email."
        )


# ------------------------------------------------------------------
# EmailPoller — background IMAP polling
# ------------------------------------------------------------------


class EmailPoller:
    """Daemon thread that polls an IMAP mailbox for new messages.

    Each UNSEEN email is parsed via :meth:`EmailAdapter.parse_email`
    and fed through the shared :func:`_process_inbound` pipeline.
    """

    def __init__(  # noqa: D107
        self,
        *,
        adapter: EmailAdapter,
        imap_host: str,
        imap_port: int = 993,
        imap_ssl: bool = True,
        username: str,
        password: str,
        mailbox: str = "INBOX",
        poll_interval: int = 30,
        process_fn: Callable[..., Any],
    ) -> None:
        self._adapter = adapter
        self._imap_host = imap_host
        self._imap_port = imap_port
        self._imap_ssl = imap_ssl
        self._username = username
        self._password = password
        self._mailbox = mailbox
        self._poll_interval = max(poll_interval, 5)  # Floor at 5s
        self._process_fn = process_fn
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Launch the polling daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="email-poller",
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the poller to stop and wait for it."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    # -- Internal --

    def _connect(self) -> imapclient.IMAPClient:
        """Create and authenticate an IMAP connection."""
        client = imapclient.IMAPClient(
            self._imap_host,
            port=self._imap_port,
            ssl=self._imap_ssl,
        )
        client.login(self._username, self._password)
        client.select_folder(self._mailbox)
        return client

    def _poll_loop(self) -> None:
        """Main polling loop — runs in a daemon thread."""
        backoff = 5
        client: imapclient.IMAPClient | None = None

        while not self._stop_event.is_set():
            try:
                if client is None:
                    client = self._connect()
                    backoff = 5  # Reset backoff on successful connect
                    logger.info(
                        "Email poller connected to %s:%d/%s",
                        self._imap_host,
                        self._imap_port,
                        self._mailbox,
                    )

                uids = client.search(["UNSEEN"])
                if uids:
                    logger.debug("Found %d unseen emails", len(uids))
                    fetched = client.fetch(uids, ["RFC822"])
                    for uid, data in fetched.items():
                        self._handle_email(uid, data, client)

            except Exception:
                logger.warning(
                    "Email poller error (reconnecting in %ds)",
                    backoff,
                    exc_info=True,
                )
                client = None
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue

            self._stop_event.wait(self._poll_interval)

        # Graceful shutdown
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass

    def _handle_email(
        self,
        uid: int,
        data: dict[bytes, Any],
        client: imapclient.IMAPClient,
    ) -> None:
        """Parse and process a single email, then mark as seen."""
        raw_bytes = data.get(b"RFC822")
        if not raw_bytes:
            return
        try:
            msg = email_stdlib.message_from_bytes(raw_bytes)
            inbound = self._adapter.parse_email(msg)
            if inbound is not None:
                self._process_fn(self._adapter, inbound)
            # Mark processed regardless (don't re-process rejected emails)
            client.add_flags([uid], [imapclient.SEEN])
        except Exception:
            logger.warning(
                "Failed to process email UID %s",
                uid,
                exc_info=True,
            )
