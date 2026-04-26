# Email

<div style="display: flex; justify-content: center;">
  <img src="../truss-email-01.jpg" alt="Truss email thread in Gmail" style="width: 100%; max-width: 520px; height: auto;" />
</div>

Truss can be reached via email. Send a message to the configured mailbox and the agent replies with a styled HTML email. Thread replies continue the same session.

## How it works

Truss checks your inbox for new messages on an interval. Each new email goes through the same pipeline as any other channel. Session resolution, slash command handling, and LLM invocation all behave identically to the web console or Nextcloud Talk. Replies are sent back over SMTP as multipart emails that include both a styled HTML body and a plain-text fallback.

## Access control

- **`allowed_senders`**: Only emails from listed addresses are processed. All others are silently ignored and marked as read. This is the primary gate.
- **1-to-1 emails** (single recipient): Always processed. No mention keyword needed.
- **Multi-party threads** (multiple To/Cc recipients): The agent only responds when `@Truss` appears in the message body.

## Session model

Email threads map to Truss sessions via the `References` and `In-Reply-To` headers:

- **New email** (no `In-Reply-To`): Creates a new session. Tag: `email:thread:<sender>:<Message-ID>`.
- **Reply** (has `In-Reply-To` / `References`): Resolves the existing session via the thread root's `Message-ID`.

Replies from the agent include proper `In-Reply-To` and `References` headers so email clients group them in the same thread.

## Slash commands

All channel commands work in email. Send the command as the email body:

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
  "username": "truss@example.com",
  "password": "app-password-here",
  "from_address": "Truss <truss@example.com>",
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

## Limitations

- **Attachments**: Inbound email attachments are not yet processed (file metadata is ignored).
- **Rate limiting**: There is no built-in rate limit on the poller. The `allowed_senders` list is the primary access control.
- **Polling latency**: The poller checks the mailbox on the interval you configure. Latency is bounded by `poll_interval_seconds`. IMAP IDLE push is not yet supported.

> [!NOTE] How it works internally
> See [Architecture Overview → Channel adapters](core-orchestration.md#channel-adapters).
