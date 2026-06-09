> ↑ [root /CLAUDE.md](../../CLAUDE.md) · children: [wiki](src/mewbo_api/wiki/CLAUDE.md) · [agentic_search](src/mewbo_api/agentic_search/CLAUDE.md)

# Mewbo API - Project Guidance

Scope: this file applies to the `apps/mewbo_api/` package. It captures runtime behavior, hidden dependencies, and testing notes so changes stay safe and predictable.

**Layering (see root CLAUDE.md → "Monorepo layering"):** this is a *thin product surface* — HTTP routes, wire contracts, transport, persistence, and channel/MCP glue. Reusable engines (graph, memory, embedding, search) belong in a capability library (`mewbo_graph`), not here; the API composes them via an extra. Don't grow domain logic inside `apps/`.

**Subsystem docs (read the deepest one that applies):**
- `packages/mewbo_graph/CLAUDE.md` — the wiki/search substrate engine this app composes via the `wiki` extra (code graph, memory, embedder, retriever, SCG) + the down-only seams (store singleton, `CloneTokenCache`, `MapPhaseSink`). Read it before touching anything the api glue delegates to.
- `apps/mewbo_api/src/mewbo_api/wiki/CLAUDE.md` — MewboWiki BE glue: phase model, snapshot-vs-stream parity, capability gating, embedder→litellm decision, SSE proxy primer, clone-token cache, prune_pages, KG endpoint.
- `apps/mewbo_api/src/mewbo_api/agentic_search/CLAUDE.md` — Agentic Search BE: run lifecycle, event-log-as-stream, `SearchRunner` swap-seam (echo vs orchestrated), separate run store, source→`allowed_tools` scoping, SSE proxy primer.

## Runtime flow (what actually happens)
- Entry point: `apps/mewbo_api/src/mewbo_api/backend.py` (HTTP API framework).
- Session endpoints:
  - `POST /api/sessions` create session
  - `GET /api/sessions` list sessions (each summary carries `origin` — `user|wiki|search|channel` provenance computed in core `summarize_session`, forwarded verbatim; the console badges/filters on it)
  - `POST /api/sessions/{session_id}/query` enqueue run or core command
  - `GET /api/sessions/{session_id}/events?after=...` poll events
  - `POST /api/sessions/{session_id}/message` enqueue a user steering message into a running session
  - `POST /api/sessions/{session_id}/interrupt` interrupt the current tool execution step
  - `GET /api/sessions/{session_id}/agents` return sub-agent tree with lifecycle state (status, steps_completed) and total_steps
  - `GET /api/sessions/{session_id}/stream` SSE stream for real-time session events (sub_agent, permission, tool_result, etc.) — now **event-pushed** via core `SessionEventBus` (no 0.5s poll, no per-event transcript re-read; wire format unchanged). Generator: subscribe → backlog-once → queue-fed tail, content-key dedup of the subscribe↔backlog race, drain-before-`stream_end` (else the terminal `completion` event is dropped).
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
- Realtime endpoints (`init_realtime`; low-latency SideStage surface — siblings to `/v1/structured`, NOT modes on it):
  - `POST /v1/structured/fast` retrieval-only, sessionless, single round-trip via `StructuredSynthesizer` + `WikiGroundingProvider` (`HybridRetriever` via `Embedder()`)
  - `POST /v1/draft/stream` token SSE; `DraftStreamer.astream()` bridged to the sync Flask generator via ONE per-request event loop, single-shot
  - `POST /v1/wiki/projects/{slug}/documents` non-git catalog ingestion via `CatalogIngestor` (direct write, no agent)
- Agentic Search endpoints (`init_agentic_search`; run store is separate from session transcripts):
  - `GET /api/agentic_search/sources?project=` list the source catalog (unconfigured sources returned with `available=false`, not omitted)
  - `GET/POST /api/agentic_search/workspaces`, `PATCH/DELETE /api/agentic_search/workspaces/<id>` workspace CRUD
  - `GET /api/agentic_search/workspaces/<id>/runs` recent run records for a workspace
  - `POST /api/agentic_search/runs` create + drive a run (synchronous, back-compat: returns `{run: RunPayload}` + `run_id`/`session_id`/`status`)
  - `GET /api/agentic_search/runs/<run_id>` durable run snapshot (reload / share / deep-link)
  - `GET /api/agentic_search/runs/<run_id>/events` SSE — the run's append-only idx-keyed event log replayed + tailed (the normalized search-event stream)
  - `POST /api/agentic_search/runs/<run_id>/cancel` cancel a run (best-effort cancels the backing session when real)
  - `POST /api/agentic_search/sources/<id>/map` start a map-source (SCG indexing) job for one connector (gated on `scg.enabled`, 503 when off; `descriptor` is an UNTRUSTED schema carried in the user query, never the system prompt)
  - `GET /api/agentic_search/sources/<id>/map/events` SSE over the map-job event log (reuses `RunSseGenerator`; `?job_id=` selects a job, else newest for the source)
  - `GET /api/agentic_search/scg` introspection — SCG node/edge/recipe/source counts + mapped source list (gated on `scg.enabled`; reads the deterministic core, never an LLM)
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

## MCP-facing contracts (#40–#45, non-obvious only)

The `apps/mewbo_mcp` facade depends on these REST decisions (see its CLAUDE.md
"Gold-standard contract"):
- **One JSON 404 handler.** `@app.errorhandler(NotFound)` (registered once near
  the `Api(app, …)` setup) returns `{"error": {code, reason}}` for EVERY route —
  the single fix for the raw-Werkzeug-HTML-404 leak (a `project` with a `/` no
  longer matches `<string:project_id>` and used to fall through to the HTML page).
- **Storeless async `run_id`.** `SessionRuntime.start_async` mints
  `"<session_id>:r<seq>"` (seq = count of prior user-turns) and returns it (`""`
  when the run registry refuses a concurrent start — preserves `if not started:`).
  No run-store: recover the session by splitting on the FIRST `:`. `/v1/structured`
  is async on this handle (`POST` → `{run_id, status, output?}`, `GET
  /v1/structured/<run_id>` resolves the session's latest `structured_output`
  event); core force-emits so it stops 422-ing (see core CLAUDE.md).
- **`/events` carries authoritative status.** `GET /api/sessions/<id>/events`
  returns `status`/`done_reason`/`title` (from `summarize_session`/`load_title`)
  so the MCP overview reads them instead of reconstructing from the timeline tail
  (the old `status:null` + ignored-title source).
- **Idle session-control = Devin-modeled.** `/interrupt` on idle → 200
  `{interrupted:false}` (no-op); `/message` on idle/finished → re-engage via the
  `start_async`/query path, returning the new `run_id`; only a terminated session
  rejects. `/agents` token rollup delegates to `build_usage_numbers` so a
  root-only session reports real tokens (not 0).
- **Worktree lifecycle is system-owned.** The `on_session_end` hook is the SOLE
  reaper; it also auto-reaps the promoted parent project when it has no worktree
  children left (kills the #53 orphan). The DELETE route is idempotent:
  already-absent → 200 `{status:"already_absent"}`, not 404. MCP no longer
  hands out a worktree handle.
- **`/agents` `total_input_tokens` = PEAK semantics** (`root_peak_input_tokens +
  sub_peak_input_tokens`) matching the `get_session_history` overview; the
  cumulative billed sum is separately exposed as `total_input_tokens_billed`
  (#45 — the old bare sum was ~2× the peak and confused callers).
- **`GET /v1/structured/<run_id>`**: output-present always maps to `status:
  "completed"` regardless of raw `summarize_session` status — the emit tool
  only fires on success, so presence IS completion.
- **`RepoIdentity` (`repo_identity.py`).** Canonical `(host, owner, repo)` parsed
  from a project's git remotes; `_resolve_repo_or_404` matches a key against every
  registered project's identity + aliases (so one repo resolves via its Gitea host
  OR GitHub mirror OR `owner/repo` OR bare name), and `GET /api/projects` surfaces
  `repo`/`aliases`. Ambiguous bare names raise a candidates error, never a silent
  wrong match.

## Config endpoints & secret handling
`ConfigSchemaView` (`config_view.py`) is the single atomic class governing how `/api/config*` treats sensitive fields — it consolidates four former scattered `_*_protected_*` helpers into one schema traversal, DI'd with the generated schema. Two field classes (declared via `x-*` in core `config.py`):
- **`x-protected`** — never read, never written: stripped from `GET /config/schema` and `GET /config`; a `PATCH` touching one is 403'd. (host paths, `api.master_token`.)
- **`x-secret`** — write-only: kept in the schema as `writeOnly`, settable via `PATCH`, but its VALUE is never returned. `GET /config` returns `{config, secrets}` where `secrets: {dot.path: bool}` reports is-set only. (`llm.api_key`, `langfuse.*`, `home_assistant.token`.)
The console's `SecretField` is the matching write-only 3-state widget. Multi-token API auth is a separate concern — the `KeyStore` + `/api/keys` routes (see auth above), surfaced in the console's Security settings facet via the reused `ApiKeysView`.

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

## Debugging session errors (trace methodology)

When given a session URL (`/s/<session_id>`), work through these layers in order:

1. **MongoDB transcript** — authoritative event log. Query `db.events.find({session_id}).sort({ts:1})` via `MEWBO_MONGODB_URI` (port 27018). Check `tool_result.error`, `context.mcp_tools`, `completion.done_reason`.
2. **Langfuse traces** — LLM conversation chain. `fetch_traces(age=N)` → `fetch_observation(id)` on `GENERATION` to see system prompt, bound tool schemas, model reasoning. Trace-to-session: `trace_id == session_id`.
3. **Config** — `configs/app.json` (mounted read-only at `/app/configs/`), `docker.env` for secrets, MCP at `configs/mcp.json` (global) or `<project>/.mcp.json` (project).
4. **Docker env** — `docker-compose.yml` + override for mounts. API runs at `/app` with `MEWBO_HOME=/app/data`. Project dirs need identical host/container paths.

### Common root-cause signatures

| Symptom | Cause |
|---|---|
| `result: null, success: false` on shell/file tools | CWD missing in container (volume mount), or `root` not injected |
| `"Tool not available"` | LLM hallucinated a filtered-out built-in tool, or `tool_id` mismatch with registry |
| `"MCP server 'X' not found in config"` | `MCPToolRunner` loaded config without project CWD; project `.mcp.json` not merged |
| `done_reason: "max_steps_reached"` | Legacy only — agents now run until natural completion |
| Langfuse `sessionId: null` | `invoke_config["metadata"]` not propagated; check `langfuse_metadata` 3-line pattern in `tool_use_loop.py` |

## Cross-project insights (fast decision help)
- Explicit tool allowlists and permission gates reduce unsafe actions; keep API calls explicit and auditable.
- Clear turn boundaries help keep outputs stable; avoid mixing raw tool output with the final response.
- Keep the API surface small and obvious; avoid hidden behaviors.
