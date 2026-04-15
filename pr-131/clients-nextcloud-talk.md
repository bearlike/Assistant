# Nextcloud Talk

The Nextcloud Talk integration allows users to interact with Meeseeks directly from any Nextcloud Talk conversation. Mention the bot and it responds, creating a standard Meeseeks session visible in the web console and Langfuse traces.

## How it works

1. A Nextcloud Talk bot is registered on the server pointing to the Meeseeks API webhook endpoint.
2. When a user @mentions the bot, Nextcloud POSTs an ActivityStreams 2.0 webhook to `POST /api/webhooks/nextcloud-talk`.
3. The adapter verifies the HMAC-SHA256 signature, parses the message, and creates or continues a session.
4. Non-mentioned messages are silently ignored — the bot only responds when triggered.
5. When the session completes, the final answer is sent back via the Nextcloud OCS Bot API with `replyTo` (creating a visual quote link).

### Session model

- **Room-scoped**: All @mentions in the same room share one persistent session. Tag format: `nextcloud-talk:room:<room_token>`.
- **Thread-scoped**: Messages with a `threadId` (explicit NC Talk thread) get their own session. Tag format: `nextcloud-talk:thread:<room_token>:<thread_id>`.
- Sessions are standard API sessions — stored in MongoDB/JSON, visible in the console, support forking/archiving/export.

## Slash commands

Commands are available after the @mention keyword. They run without invoking the LLM.

| Command | Description |
|---------|-------------|
| `/help` | Show available commands and usage |
| `/usage` | Show session token usage, context window utilization, and compaction status |
| `/new` | Start a fresh conversation (clears current session context) |
| `/switch-project <name>` | Switch the active project context for this session |

Examples:
```
@Meeseeks /help
@Meeseeks /usage
@Meeseeks /new
@Meeseeks /switch-project personal-assistant
```

The `/switch-project` command sets the working directory for subsequent LLM runs. If the project name is invalid or omitted, it lists available projects.

## Prerequisites

- Nextcloud 27.1+ with Talk 17.1+ (for bots-v1 capability)
- Nextcloud 32+ with Talk 22+ (for thread support via `threadId`)
- The Meeseeks API server running and reachable from the Nextcloud instance

## Setup

### 1. Register the bot in Nextcloud

On the Nextcloud server (requires admin shell access):

```bash
occ talk:bot:install "Meeseeks" \
  "<shared-secret-at-least-40-chars>" \
  "https://<meeseeks-api-host>/api/webhooks/nextcloud-talk" \
  --feature webhook --feature response \
  "AI assistant powered by Meeseeks"
```

Note the shared secret — it must match the `bot_secret` in the Meeseeks config.

### 2. Enable the bot in conversations

Either via the admin CLI:

```bash
occ talk:bot:setup <bot-id> <conversation-token>
```

Or via the Nextcloud Talk web UI (moderator role required): open the conversation settings and enable the bot under "Bots".

### 3. Configure Meeseeks

Add the channel config to `configs/app.json`:

```json
{
  "channels": {
    "nextcloud-talk": {
      "enabled": true,
      "bot_secret": "<shared-secret-from-step-1>",
      "nextcloud_url": "https://cloud.example.com",
      "allowed_backends": ["https://cloud.example.com"],
      "trigger_keyword": "@Meeseeks",
      "nextcloud_host_header": ""
    }
  }
}
```

| Field | Description |
|-------|-------------|
| `enabled` | Set to `true` to activate the adapter |
| `bot_secret` | Shared HMAC secret from `occ talk:bot:install` |
| `nextcloud_url` | Base URL of the Nextcloud instance (use internal URL if behind a CDN) |
| `allowed_backends` | Origin allowlist for the `X-Nextcloud-Talk-Backend` header (recommended) |
| `trigger_keyword` | Literal keyword that must appear in a message to trigger the bot (default: `@Meeseeks`) |
| `nextcloud_host_header` | Optional `Host` header override for outbound requests (useful when `nextcloud_url` is an internal address) |

### 4. Restart the API server

```bash
uv run meeseeks-api
# or: docker compose restart api
```

The startup logs should show:

```
Nextcloud Talk channel adapter registered
Channel webhook routes registered (platforms: ['nextcloud-talk'])
```

## Usage

In any Nextcloud Talk conversation where the bot is enabled:

```
@Meeseeks help me refactor the auth module
```

The bot responds with the orchestration result. Subsequent @mentions in the same room continue the conversation with full context. Use `/new` to reset.

## Architecture

The adapter lives in `apps/meeseeks_api/src/meeseeks_api/channels/`:

| File | Purpose |
|------|---------|
| `base.py` | `ChannelAdapter` Protocol, `InboundMessage`, `ChannelRegistry`, `DeduplicationGuard` |
| `nextcloud_talk.py` | HMAC verification, ActivityStreams parsing, OCS Bot API response, `system_context` for LLM awareness |
| `email_adapter.py` | Email adapter (IMAP polling + SMTP reply with markdown-to-HTML rendering), `EmailPoller` daemon thread |
| `routes.py` | Flask Blueprint, shared `_process_inbound()` pipeline, `@command` registry, completion callback, `init_channels()` |

Commands use a decorator-based registry — adding a new command is one `@command` decorator and one function. Help text auto-generates from the registry.

The adapter provides a `system_context` property injected into the LLM system prompt, making the model aware it's communicating through Nextcloud Talk.

Channel sessions are standard API sessions — they use existing `create_session()`, `tag_session()`, `start_async()`, and `enqueue_message()`. No custom session infrastructure.

## Limitations

- **File attachments**: the bot receives file metadata but cannot download file content (requires Nextcloud user auth, not bot auth). File sending is not supported by the NC Talk Bot API.
- **DM auto-respond**: the bot requires @mention in all rooms (no automatic DM detection yet).
- **Emoji reactions for status**: not yet implemented.

## Adding other chat platforms

The `ChannelAdapter` protocol is designed to be extended. Email is the second adapter (see [Email](clients-email.md)). To add Slack or Discord:

1. Create `channels/slack.py` implementing `verify_request`, `parse_inbound`, `send_response`, and `system_context`
2. Add the platform config to `config.channels` in `app.json`
3. Register the adapter in `init_channels()` — the webhook route `POST /api/webhooks/slack` works automatically
4. For non-webhook channels (like IMAP email), use a poller that calls `_process_inbound()` directly
5. Optionally implement `requires_mention(message)` for per-message mention gating logic
6. Add platform-specific commands if needed via the `@command` decorator in `routes.py`
