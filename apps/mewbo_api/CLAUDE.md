# Mewbo API - Project Guidance

Scope: this file applies to the `apps/mewbo_api/` package. It captures runtime behavior, hidden dependencies, and testing notes so changes stay safe and predictable.

## Runtime flow (what actually happens)
- Entry point: `apps/mewbo_api/src/mewbo_api/backend.py` (HTTP API framework).
- Session endpoints:
  - `POST /api/sessions` create session
  - `GET /api/sessions` list sessions
  - `POST /api/sessions/{session_id}/query` enqueue run or core command
  - `GET /api/sessions/{session_id}/events?after=...` poll events
  - `POST /api/sessions/{session_id}/message` enqueue a user steering message into a running session
  - `POST /api/sessions/{session_id}/interrupt` interrupt the current tool execution step
  - `GET /api/sessions/{session_id}/agents` return sub-agent tree with lifecycle state (status, steps_completed) and total_steps
  - `GET /api/sessions/{session_id}/stream` SSE stream for real-time session events (sub_agent, permission, tool_result, etc.)
  - `GET /api/projects` list configured projects for multi-project support
  - `GET /api/tools?project=name` list tools scoped to a project's CWD
  - `GET /api/skills?project=name` list skills scoped to a project's CWD
  - `POST /api/sessions/{session_id}/archive` / `DELETE ...` archive/unarchive
  - `POST /api/sessions/{session_id}/attachments` upload attachments
  - `POST /api/sessions/{session_id}/share` create share link
  - `GET /api/sessions/{session_id}/export` export session payload
  - `GET /api/share/{token}` fetch shared session data
  - `POST /api/query` synchronous endpoint (simple/CLI-compatible)
  - `GET /api/tools` list tool registry entries
  - `GET /api/skills` list available skills
  - `GET /api/plugins` list installed plugins and their components
  - `GET /api/plugins/marketplace` list available plugins from configured marketplaces
  - `POST /api/plugins/marketplace` install a plugin from a marketplace
  - `DELETE /api/plugins/<name>` uninstall a plugin
  - `POST /api/sessions/{session_id}/ide` launch a Web IDE (code-server) container
  - `DELETE /api/sessions/{session_id}/ide` stop the Web IDE container
  - `POST /api/sessions/{session_id}/ide/extend` extend Web IDE session TTL
  - `GET /api/notifications` list notifications
  - `POST /api/notifications/dismiss` dismiss notifications
  - `POST /api/notifications/clear` clear notifications
- Channel webhook endpoints (HMAC auth, not API key):
  - `POST /api/webhooks/<platform>` receive inbound message from a chat platform (e.g. `nextcloud-talk`). Delegates to the appropriate `ChannelAdapter` for verification and parsing. Creates/continues sessions using existing session tags.
- Auth: requires `X-API-KEY` header (except webhook endpoints which use platform-specific HMAC verification). Token defaults to `api.master_token` from `configs/app.json` (default: `msk-strong-password`). Also accepts `api_key` query parameter for SSE endpoints (EventSource does not support custom headers).
- CORS: `after_request` hook sets `Access-Control-Allow-Origin: *` for cross-origin console access.
- Hooks: `HookManager.load_from_config(_config.hooks)` at startup; `hook_manager` passed to all `start_async()` call sites. Supports `type: "command"` and `type: "http"` hooks.
- Channel adapters: `init_channels(app, runtime, _hook_manager, _config)` registers the webhook Blueprint and instantiates adapters from `config.channels`. Completion callback appended to `hook_manager.on_session_end`. Channel sessions are standard sessions (MongoDB-backed, visible in console). Session tags: `nextcloud-talk:room:<token>`, `email:thread:<channel_id>:<root-msg-id>`. Shared `_process_inbound()` pipeline used by both webhook endpoint and email IMAP poller. Email adapter: `EmailAdapter` (IMAP parse, SMTP send, markdown→HTML via mistune) + `EmailPoller` (daemon thread, configurable `poll_interval_seconds`). Email access control: `allowed_senders` allowlist + `@Mewbo` mention required in multi-party threads, no mention for 1-to-1.
- Channel slash commands: decorator-based `@command` registry in `channels/routes.py`. `/help`, `/usage`, `/new`, `/switch-project <name>`. Adding a command = one decorator + one function; `/help` auto-generates from the registry. Commands run without LLM invocation.
- Client-aware system prompt: each `ChannelAdapter` provides a `system_context` property (brief string) injected via `skill_instructions` parameter to `start_async`. The LLM knows which chat interface the conversation flows through.
- Plugins: `GET/POST /api/plugins`, `GET/POST /api/plugins/marketplace`, `DELETE /api/plugins/<name>`. Uses `mewbo_core.plugins` for discovery, install, uninstall. Plugin components (skills, hooks, agent definitions, MCP tools) are loaded during session init via `load_all_plugin_components()`.
- Web IDE: opt-in per-session code-server containers via `agent.web_ide` config. `IdeManager` + `IdeStore` (MongoDB-backed) in `ide.py`. Routes in `ide_routes.py`. Requires MongoDB. Console shows "Open in Web IDE" button when enabled.
- Orchestration: uses `mewbo_core.session_runtime.SessionRuntime` to run sync/async sessions. Passes `allowed_tools` from `context.mcp_tools` to scope tool binding per query.
- Core commands: `/compact`, `/status`, `/terminate` (shared runtime).
- Sessions: supports `session_id`, `session_tag`, and `fork_from` (tag or id). Tags are resolved via `SessionStore`.
- Event payloads: `action_plan` steps are `{title, description}`; tool events use `tool_id`, `operation`, `tool_input`.

## Hidden dependencies / assumptions
- Uses core logging (`mewbo_core.common.get_logger`); log level controlled by `runtime.log_level`.
- Relies on core LLM config (`llm.api_base`, `llm.api_key`, `llm.default_model`, `llm.action_plan_model`).
- No rate limiting or auth hardening beyond the header token (webhook endpoints use HMAC instead).
- Channel system module-level globals (`_runtime`, `_hook_manager`, `_registry`, `_dedup`) in `channels/routes.py` are set by `init_channels()` at startup.

## Pitfalls / gotchas
- `api.master_token` default is insecure; production should override it in `configs/app.json`.
- No heartbeat or health endpoint; external deployments must handle liveness checks.
- The API returns the whole `TaskQueue` including action steps; ensure tool results are safe to expose.
- Treat language models as black-box APIs with non-deterministic output; avoid anthropomorphic language in docs/changes.

## Testing guidance
- `apps/mewbo_api/tests` mock `SessionRuntime.run_sync` and focus on response schema.
- Avoid mocking too much of core: keep at least one integration test that exercises `SessionStore` behavior.

## Cross-project insights (fast decision help)
- Explicit tool allowlists and permission gates reduce unsafe actions; keep API calls explicit and auditable.
- Clear turn boundaries help keep outputs stable; avoid mixing raw tool output with the final response.
- Keep the API surface small and obvious; avoid hidden behaviors.
