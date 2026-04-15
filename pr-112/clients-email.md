# Email

<div style="display: flex; justify-content: center;">
  <img src="../meeseeks-email-01.jpg" alt="Meeseeks email thread in Gmail" style="width: 100%; max-width: 520px; height: auto;" />
</div>

Meeseeks can be reached via email. Send a message to the configured mailbox and the agent replies with a styled HTML email. Thread replies continue the same session.

## How it works

An `EmailPoller` daemon thread polls an IMAP mailbox for unread messages. Each new email is parsed and fed through the shared `_process_inbound()` pipeline — the same session resolution, slash commands, and LLM invocation used by all channel adapters. Responses are sent back via SMTP as `multipart/alternative` emails (plain text + styled HTML).

## Access control

- **`allowed_senders`**: Only emails from listed addresses are processed. All others are silently ignored and marked as read. This is the primary gate.
- **1-to-1 emails** (single recipient): Always processed — no mention keyword needed.
- **Multi-party threads** (multiple To/Cc recipients): The agent only responds when `@Meeseeks` appears in the message body.

## Session model

Email threads map to Meeseeks sessions via the `References` and `In-Reply-To` headers:

- **New email** (no `In-Reply-To`): Creates a new session. Tag: `email:thread:<sender>:<Message-ID>`.
- **Reply** (has `In-Reply-To` / `References`): Resolves the existing session via the thread root's `Message-ID`.

Replies from the agent include proper `In-Reply-To` and `References` headers so email clients group them in the same thread.

## Slash commands

All channel commands work in email — send the command as the email body:

| Command | Description |
|---------|-------------|
| `/help` | Show available commands |
| `/usage` | Show session context usage and token budget |
| `/new` | Start a fresh conversation (clears context) |
| `/switch-project <name>` | Switch active project context |

Command responses are sent back as email replies.

## Configuration

Add to `configs/app.json` under `channels`:

```json
"email": {
  "enabled": true,
  "imap_host": "imap.example.com",
  "imap_port": 993,
  "imap_ssl": true,
  "smtp_host": "smtp.example.com",
  "smtp_port": 587,
  "smtp_starttls": true,
  "username": "meeseeks@example.com",
  "password": "app-password-here",
  "from_address": "Meeseeks <meeseeks@example.com>",
  "mailbox": "INBOX",
  "poll_interval_seconds": 30,
  "allowed_senders": ["alice@example.com", "bob@example.com"]
}
```

| Field | Description |
|-------|-------------|
| `imap_host` / `imap_port` / `imap_ssl` | IMAP server for polling incoming mail |
| `smtp_host` / `smtp_port` / `smtp_starttls` / `smtp_ssl` | SMTP server for sending replies |
| `username` / `password` | Credentials for both IMAP and SMTP (typically an app password) |
| `from_address` | Display name + address for outbound emails (defaults to `username`) |
| `mailbox` | IMAP folder to poll (default: `INBOX`) |
| `poll_interval_seconds` | How often to check for new emails in seconds (default: 30, minimum: 5) |
| `allowed_senders` | List of email addresses allowed to interact with the agent |

## Response rendering

Agent responses are markdown. The email adapter converts them to styled HTML:

- Headings, bold, italic, links render as expected
- Code blocks get monospace font with a light background
- Tables render with borders and padding
- A plain-text fallback (raw markdown) is included for text-only email clients

The HTML uses inline CSS for compatibility with Gmail, Outlook, and Apple Mail.

## Architecture

| File | Purpose |
|------|---------|
| `email_adapter.py` | `EmailAdapter` (parse, send, mention logic), `EmailPoller` (IMAP daemon), markdown-to-HTML rendering |
| `email_template.html.j2` | Jinja2 HTML email wrapper with inline CSS |
| `routes.py` | Shared `_process_inbound()` pipeline, adapter registration in `init_channels()` |
| `base.py` | `ChannelAdapter` Protocol (shared with all adapters) |

## Limitations

- **Attachments**: Inbound email attachments are not yet processed (file metadata is ignored).
- **Rate limiting**: No built-in rate limiting on the IMAP poller — relies on `allowed_senders` for access control.
- **IMAP IDLE**: The poller uses periodic polling, not IMAP IDLE push. This means up to `poll_interval_seconds` latency.
