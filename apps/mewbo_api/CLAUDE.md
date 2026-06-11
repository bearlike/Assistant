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
  - `POST /api/automation/vcs-pickup` agent-pickup target for GitHub/Gitea Actions (`agent-pickup.yml`) — starts/continues a session by deterministic tag `vcs:<owner/repo>:<kind>:<number>` (steering message if a run is active); PR pickups bind to a managed worktree on the fetched/ff'd head branch (`vcs_pickup.py`)
  - `GET /api/notifications` list notifications
  - `POST /api/notifications/dismiss` dismiss notifications
  - `POST /api/notifications/clear` clear notifications
- Realtime endpoints (`init_realtime`; low-latency SideStage surface — siblings to `/v1/structured`, NOT modes on it):
  - `POST /v1/structured/fast` retrieval-only, single round-trip via `StructuredSynthesizer` + `WikiGroundingProvider` (`HybridRetriever` via `Embedder()`)
  - `POST /v1/draft/stream` token SSE; `DraftStreamer.astream()` bridged to the sync Flask generator via ONE per-request event loop, single-shot
  - `POST /v1/wiki/projects/{slug}/documents` non-git catalog ingestion via `CatalogIngestor` (direct write, no agent)
  - **Session-full realtime with write-behind (#78, landed).** Both realtime paths were sessionless-by-design — reclassified as a defect. They now mint a session, trace, and persist a single-turn transcript via the **`RealtimeSessionRecorder`** atomic class (`realtime/recorder.py`, app-side: needs the session store). The seam splits "session-full" into two halves that must NOT be conflated: (1) `recorder.trace()` opens `langfuse_session_context` on a PRE-MINTED `session_id` (a bare `uuid4().hex` — no store I/O) with provenance derived from the tags+context it is *about* to write (the store has nothing to read yet, and that data == what `Orchestrator.run` would read post-persist); the LLM call runs inside it (in-process, fine). (2) `recorder.persist()` does every durable write AFTER the response/last token, fired on a daemon thread via `persist_async` — so draft TTFT p95 < 1.5s never pays for a store write. Wire contract is additive-only: fast gains `session_id` in the body; draft gains `session_id` on the terminal `done` frame + an `X-Mewbo-Session` header (token frames are untouched — SideStage-safe). `_runtime is None` degrades to trace-only. The agentic `/v1/structured` stamp seam is `StructuredResponder._prepare` (tag `structured:run` + `source_platform` from `X-Mewbo-Surface`), which also covers MCP `structured_query` (it posts here).
  - **Optional `model` override (additive, all three structured-family endpoints).** `/v1/structured`, `/v1/structured/fast`, and `/v1/draft/stream` each accept an optional `model` body field (a LiteLLM name like `openai/gpt-5.4-nano`; non-string → ignored → configured default) so an external caller (SideStage) controls the model per request. Threading: fast → `StructuredSynthesizer(model_name=...)`; draft → `DraftStreamer(model_name=...)`; agentic → applied at the ONE route seam in `StructuredResource._build_responder` (default path passes `model_name=` into `StructuredResponder(...)`, graph-first path takes it via `dataclasses.replace` after `_graph_first_responder` returns — never edit `agentic_search/**`). `StructuredResponder.model_name` reaches the LLM via `_drive → runtime.run_sync(model_name=…) → Orchestrator._model_name → build_chat_model` (it was already wired, not dead). API-level only — no MCP knob, no config setting.
- Agentic Search endpoints (`init_agentic_search`; run store is separate from session transcripts):
  - `GET /api/agentic_search/sources?project=` list the source catalog (live-first: configured servers whose discovery failed stay listed `available=false`, not omitted)
  - `GET/POST /api/agentic_search/workspaces`, `PATCH/DELETE /api/agentic_search/workspaces/<id>` workspace CRUD
  - `GET /api/agentic_search/workspaces/<id>/runs` recent run records for a workspace
  - `POST /api/agentic_search/runs` create + drive a run; returns `{run: RunPayload}` + `run_id`/`session_id`/`status` — echo runner settles synchronously (`completed`), orchestrated returns `running` promptly and settles via a RunRegistry worker (terminal state arrives on the SSE/snapshot surfaces)
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

## Agent pickup — CI → session bridge (#72, non-obvious only)

`vcs_pickup.py` (one atomic `VcsPickupService`, DI'd like `ide_routes.py`) is the **CI sibling of the channel adapters**: platform event → tag-keyed session (`vcs:<owner/repo>:<kind>:<number>`, cf. `nextcloud-talk:room:<token>`). It deliberately does NOT implement `ChannelAdapter` (auth is the API key; no HMAC handshake exists), but the reply leg mirrors the channels exactly: `completion_hook` on `on_session_end` (cf. `_channel_completion_hook`, sharing `channels.routes.extract_final_answer`) posts the final answer back to the issue/PR as a comment by the bot account. User docs: `docs/ci-agent-pickup.md`.

- **Gitea Actions ≠ GitHub Actions payloads (verified live, 2026-06):** Gitea has no top-level `event.assignee` on assignment events — guard via `contains(github.event.<issue|pull_request>.assignees.*.login, …)` fallback (side effect: re-assignment while the bot is already assigned re-triggers; harmless, the tag reuses the session). `issue.pull_request` marker IS present on comment payloads; `github.api_url` IS populated (`<server>/api/v1`); `Authorization: token $GITHUB_TOKEN` works on both platforms; the act_runner image ships jq but does NOT trust internal CAs (→ `AGENT_TLS_NO_VERIFY` repo var adds `curl -k`).
- **`_resolve_repo_or_404`'s identity scan covers managed projects only.** A config project that was never promoted does not resolve by `owner/repo` — that's why `VcsPickupService._config_project_for_repo` scans config project paths with `RepoIdentity.aliases_for_path` as a fallback. Don't "fix" this by registering pickup targets via `POST /v_projects` with an explicit path: the worktree reaper deletes childless `path_source == "provided"` parents **permanently**, while config projects self-heal through promote-on-demand.
- **Deployment needs git credentials in the api container.** The pickup fetches PR branches and agent sessions push to them; the image sets `credential.helper=store` but ships no credentials — mount the host's `~/.git-credentials` to the container user's HOME (see `docker-compose.override.yml`, untracked). Without it: 422 `could not read Username`.
- Endpoint auth accepts KeyStore-minted keys (`POST /api/keys`), not just the master token — CI secrets should hold a labeled revocable key.
- **Reply tokens live server-side, keyed by forge host** (`channels.vcs.tokens` config) — the workflow's `GITHUB_TOKEN` dies with the job, long before the agent run ends, so it can't deliver the reply. `/repos/{owner}/{repo}/issues/{n}/comments` + `Authorization: token` are identical on GitHub and Gitea (one client, both forges). Gitea gotcha: minting a PAT for another user (`POST /api/v1/users/<bot>/tokens`, admin-only) rejects token auth with `auth required` — use **basic** auth (`-u admin:$TOKEN`). Unlike the act_runner, the api container's system CA store trusts the internal CA (git and Python `ssl` share it), so `tls_verify` stays default there.

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
