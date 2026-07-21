> ↑ [root /CLAUDE.md](../../CLAUDE.md) · children: [wiki](src/mewbo_api/wiki/CLAUDE.md) · [agentic_search](src/mewbo_api/agentic_search/CLAUDE.md)

# Mewbo API - Project Guidance

Scope: this file applies to the `apps/mewbo_api/` package. It captures runtime behavior, hidden dependencies, and testing notes so changes stay safe and predictable.

**Layering (see root CLAUDE.md → "Monorepo layering"):** this is a *thin product surface* — HTTP routes, wire contracts, transport, persistence, and channel/MCP glue. Reusable engines (graph, memory, embedding, search) belong in a capability library (`mewbo_graph`), not here; the API composes them via an extra. Don't grow domain logic inside `apps/`.

**Subsystem docs (read the deepest one that applies):**
- `packages/mewbo_graph/CLAUDE.md` — the wiki/search substrate engine this app composes via the `wiki` extra (code graph, memory, embedder, retriever, SCG) + the down-only seams (store singleton, the git-auth chain in `wiki/credentials.py`, `MapPhaseSink`). Read it before touching anything the api glue delegates to — in particular "Git auth" before ANY change that shells out to git.
- `apps/mewbo_api/src/mewbo_api/wiki/CLAUDE.md` — MewboWiki BE glue: phase model, snapshot-vs-stream parity, capability gating, embedder→litellm decision, SSE proxy primer, the git credential registry + freshness routes, prune_pages, KG endpoint.
- `apps/mewbo_api/src/mewbo_api/agentic_search/CLAUDE.md` — Agentic Search BE: run lifecycle, event-log-as-stream, `SearchRunner` swap-seam (echo vs orchestrated), separate run store, source→`allowed_tools` scoping, SSE proxy primer.

## Route paradigm — a DI'd controller, not module globals (read before adding a route)

A new route module gets an **atomic controller class**: collaborators (service, store, policy, runtime, the auth guard) injected as FIELDS, every serialization / existence / domain helper a METHOD, and the Flask surface a thin HTTP adapter that receives the one controller by dependency injection. **The request path reads ZERO module state.**

- **Flask-RESTX → `resource_class_kwargs`.** `triggers/routes.py` is the reference: `init_trigger_routes` builds one `TriggerRoutesController` and `add_resource(..., resource_class_kwargs={"controller": …})` injects it into every `Resource` (a `_ControllerResource` base captures it alongside the `Api` positional). The surviving module-level `_controller` is a **composition-root handle only** — production never reads it (Flask bakes the kwargs into the view closure at registration); it exists so a route test can point the one registered controller at fresh stores by reassigning its fields.
- **Blueprint → construct per request.** `wiki/settings.py` is the reference: the thin handler builds a `WikiProjectSettings(store, developer_mode=…)`, whose `WikiHTTPError` raises map to the wire through the already-registered errorhandler. Same idiom, no `add_resource` seam needed.
- **The module-global wiring is pre-existing DEBT — new code must not imitate it.** `channels/routes.py` (`_runtime`/`_hook_manager`/`_registry`/`_dedup`, set by `init_channels`) and the `_service`/`_manager`/`_runtime`/`_require_api_key` globals in `vcs_pickup.py`, `ide_routes.py`, `agentic_search/routes.py`, `realtime/routes.py`. Migration is **mechanical**, and is the expected cleanup when you next touch one: several already hold a perfectly good atomic class behind the global (`VcsPickupService`, `IdeManager`) — fold the module helpers onto it and inject it, exactly as `TriggerRoutesController` does.
- **Wire models are Pydantic with `ConfigDict(extra="forbid")`** (`ProjectSettingsPatch`, `CredentialUpsert`, `VcsPickupBody`) — a client smuggling a server-owned field gets a 400 at the boundary, not a silent no-op. Flask-RESTX `fields` models stay **doc-only** (their `example=` drives the Scalar sample bodies); they are never the validator.
- **The terminated-session 410 envelope has ONE home:** `ApiResponseKit.TERMINATED_ERROR_BODY` / `.terminated_response()` (`responses.py`). Both `backend.py` and `triggers/routes.py` import it and the console's `session_terminated` sentinel matches it byte-for-byte, so it must never drift. It lives on the response kit — which both surfaces already import — rather than a bare constant module, which would have re-opened a `backend.py` ↔ routes import cycle.

## Access control — declared ABOVE the handler (read before adding a route)

Every route carries a `PermissionGuard` binding (`auth/permission_guard.py`); the requirement is part of the signature, not the first two statements of the body. `requires(...)`/`requires_master(...)` validate their ids against the closed `mewbo_iam.PermissionCatalog` AT DECORATION — module scope, which in this app is boot — so a typo'd `"session.read"` fails the server before it accepts traffic instead of surfacing as a 403 on a route nobody exercised.

- **`public()` and `dual_channel()` DECLARE; they do not enforce.** Both add ZERO request-time behavior — the handler's own first statement remains the enforcement. Nothing catches a handler that forgets to call its check, so `enforced_by` names the symbol where the fall-through actually lives: an auditor gets something to READ, which is a pointer, never a proof. What the declaration buys is that `audit()` can distinguish "open by design" from "someone forgot" — an UNBOUND route is the finding, and a route that is authenticated-but-not-by-a-permission must not be mislabelled `public` to silence it.
- **A route whose permission failure FALLS THROUGH to a second credential cannot be expressed as one permission.** That is the whole reason `dual_channel` exists rather than a second `requires` mode: under `requires`, a key that authenticates but lacks the permission is REFUSED, while on these routes it must degrade to the alternate channel exactly as an anonymous caller does (else every served app breaks and the self-service key tier disappears). Absorbing that channel into the decorator would mean re-deriving state the handler already holds — the served app's `app_id` out of the URL, the caller's own subject on a self-service mint — i.e. a second copy of enforcement that drifts from the first.
- **Auth OFF must be byte-identical to before IAM existed.** With `api.auth.enabled` false the kit resolves every request to the legacy full-power principal: no IAM store file is written, no IAM/SCIM routes mount, no IAM module is imported on the request path, and 401/403 bodies match the pre-IAM strings exactly. A corollary that is easy to get backwards: an INVALID `api.auth` block is a hard boot failure when auth is on (as is a configured authenticator whose optional extra is missing) but is logged-and-ignored when it is off — a deployment that never turned auth on must not fail to boot over config it does not use.
- **A refusal is a typed exception, never a loose dict.** Handlers raise from `errors.py`, where each class owns its status and renders a validated payload; the registered errorhandler does the rendering, so a refusal can travel up out of a controller method or a store wrapper without every caller threading it back as a sentinel. The API genuinely has TWO wire shapes — the `{"error": {code, reason, retryable}}` envelope and the legacy `{"message": ...}` — and the shape belongs to the SURFACE, not to the failure kind. Match what the handler you are migrating already returns; read its `return` statements rather than assuming, because picking the other one is a wire regression.

## Runtime flow (what actually happens)
- Entry point: `apps/mewbo_api/src/mewbo_api/backend.py` (HTTP API framework).
- Session endpoints:
  - `POST /api/sessions` create session — accepts an optional external `cwd`
    (top-level or `context.cwd`; also on `POST .../query`) gated behind
    `api.allow_external_cwd` (default OFF). `ExternalCwdPolicy` (backend.py) is
    the one seam: flag off + cwd present → structured 403; flag on → must be an
    existing directory (else 400); explicit cwd WINS over project-derived and is
    persisted as `context_payload["cwd"]` so `/message` re-engagement and
    `_resolve_session_cwd` (diff endpoints) keep resolving it — external
    workspace managers anchor sessions in their own worktrees;
    registering provided-path v_projects is NOT a substitute: the reaper
    permanently deletes childless provided-path parents). Docker rule applies:
    the path must be visible in the api container at the identical path.
  - `GET /api/sessions` list sessions (each summary carries `origin` — `user|wiki|search|channel` provenance computed in core `summarize_session`, forwarded verbatim; the console badges/filters on it)
  - `POST /api/sessions/{session_id}/query` enqueue run or core command. Inline `@<ref>` context expansion runs HERE, after cwd-resolution and before `start_async` (and at the sync `POST /api/query`): the app calls `mewbo_tools.integration.reference_expansion.expand_references(user_query, cwd, attachments=…)`. The reusable `ReferenceExpander` lives in **`mewbo_tools`** (not the app) so the in-process CLI shares it — an app can't import another app, so the engine sits one layer down; the API just builds the per-call inputs (`_session_attachment_map`) and invokes it at the submit seam. It resolves `@file`/`@dir/`/`@diff`/`@url` through EXISTING renderers (`attachments.parse_to_markdown` for docs+URLs via markitdown's `convert_uri`, `git diff HEAD`, `FileCatalog`/`os.scandir`) — no per-type parser. **Scoping:** `@file`/`@dir` resolve ONLY to files in the project's git index (`FileCatalog` = `git ls-files --cached --others --exclude-standard`, so `.gitignore`d secrets/artifacts are out of scope) or to session attachments; non-git dirs fall back to cwd-confined existing files. Guardrails: per-ref + aggregate char caps with **truncate-not-reject**, dedupe, no recursion, and any unresolved/out-of-scope ref (missing path / non-repo `@diff` / dead URL / `email@host`) passes through literally. `expand_references()` never raises into the request path.
  - `GET /api/files?project=&session=&q=&limit=` list referenceable files for the composer's `@`-autocomplete — `FileCatalog.list_files()` (git index ∪ session attachments), scoped by `project` (home composer) or `session` (in-session). Backs the console file picker; the CLI builds the same list in-process via `FileCatalog`.
  - `GET /api/sessions/{session_id}/events?after=...` poll events
  - `POST /api/sessions/{session_id}/message` enqueue a user steering message into a running session
  - `POST /api/sessions/{session_id}/interrupt` interrupt the current tool execution step
  - `POST /api/sessions/{session_id}/questions/{call_id}/answer` answer a pending `user_question` event (the blocked `ask_user_question` tool call resolves with it) — see "Ask-user questions" below
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
  - `POST /api/automation/vcs-pickup` agent-pickup target for GitHub/Gitea Actions (`agent-pickup.yml`) — starts/continues a session by deterministic tag `vcs:<owner/repo>:<kind>:<number>` (steering message if a run is active); PR pickups bind to a managed worktree on the fetched/ff'd head branch, issue pickups to an isolated `mewbo/issue-<n>` worktree cut from HEAD (graceful main-checkout fallback) (`vcs_pickup.py`)
  - `GET /api/notifications` list notifications
  - `POST /api/notifications/dismiss` dismiss notifications
  - `POST /api/notifications/clear` clear notifications
- Realtime endpoints (`init_realtime`; registers `/v1/draft/stream` only — the former `/v1/structured/fast` sibling was folded into `/v1/structured` as `mode:"synthesis"`):
  - `POST /v1/draft/stream` token SSE; `DraftStreamer.astream()` bridged to the sync Flask generator via ONE per-request event loop, single-shot
  - `POST /v1/wiki/projects/{slug}/documents` non-git catalog ingestion via `CatalogIngestor` (direct write, no agent)
  - **`POST /v1/structured` with `mode:"synthesis"`.** The no-loop, retrieval-only single round-trip (formerly `/v1/structured/fast`, removed) is now a mode on the main structured endpoint. Implemented by `mewbo_api.structured.synthesis.SynthesisRunner`, which reuses `RealtimeSessionRecorder.for_fast` (tag `structured:fast` → `session_type structured_fast`) + `WikiGroundingProvider`. Response is returned inline as `{run_id, status:"completed", output, citations, workspace}`. The `run_id` handle (`<session_id>:r1`) still resolves via `GET /v1/structured/{run_id}`. The agentic-mode stamp seam (`StructuredResponder._prepare`, tag `structured:run`) is unchanged.
  - **Session-full realtime with write-behind (landed).** Both realtime paths were sessionless-by-design — reclassified as a defect. They now mint a session, trace, and persist a single-turn transcript via the **`RealtimeSessionRecorder`** atomic class (`realtime/recorder.py`, app-side: needs the session store). The seam splits "session-full" into two halves that must NOT be conflated: (1) `recorder.trace()` opens `langfuse_session_context` on a PRE-MINTED `session_id` (a bare `uuid4().hex` — no store I/O) with provenance derived from the tags+context it is *about* to write (the store has nothing to read yet, and that data == what `Orchestrator.run` would read post-persist); the LLM call runs inside it (in-process, fine). (2) `recorder.persist()` does every durable write AFTER the response/last token, fired on a daemon thread via `persist_async` — so draft TTFT p95 < 1.5s never pays for a store write. Wire contract is additive-only: synthesis mode gains `run_id`/`session_id` inline; draft gains `session_id` on the terminal `done` frame + an `X-Mewbo-Session` header (token frames are untouched). `_runtime is None` degrades to trace-only.
  - **Optional `model` override (additive, both realtime-family endpoints).** `/v1/structured` (both modes) and `/v1/draft/stream` each accept an optional `model` body field (a LiteLLM name like `openai/gpt-5.4-nano`; non-string → ignored → configured default) so an external caller controls the model per request. Threading: synthesis mode → `SynthesisRunner` → `StructuredSynthesizer(model_name=...)`; draft → `DraftStreamer(model_name=...)`; agentic mode → applied at the ONE route seam in `StructuredResource._build_responder` (default path passes `model_name=` into `StructuredResponder(...)`, graph-first path takes it via `dataclasses.replace` after `_graph_first_responder` returns — never edit `agentic_search/**`). `StructuredResponder.model_name` reaches the LLM via `_drive → runtime.run_sync(model_name=…) → Orchestrator._model_name → build_chat_model` (it was already wired, not dead). API-level only — no MCP knob, no config setting.
- Agentic Search endpoints (`init_agentic_search`; run store is separate from session transcripts):
  - `GET /api/agentic_search/sources?project=` list the source catalog (live-first: configured servers whose discovery failed stay listed `available=false`, not omitted)
  - `GET /api/agentic_search/tiers` search-budget tiers + the resolved model preset each runs on (`tier_models` → `llm.default_model` fallback, mirroring the drive); feeds the console's coupled tier/model composer pills
  - `GET/POST /api/agentic_search/workspaces`, `PATCH/DELETE /api/agentic_search/workspaces/<id>` workspace CRUD
  - `GET /api/agentic_search/workspaces/<id>/runs` recent run records for a workspace
  - `GET /api/agentic_search/runs?limit=` recent runs ACROSS all workspaces, newest-first (default 30, clamped 100) — feeds the console nav-rail's "recent searches" section. Lives as the `get` on the SAME `RunsResource` class as the `post` below: flask-restx silently drops verbs from the swagger spec if two Resource classes share one route path
  - `POST /api/agentic_search/runs` create + drive a run (optional `tier` budget knob + per-run `model` override — see agentic_search/CLAUDE.md); returns `{run: RunPayload}` + `run_id`/`session_id`/`status` — echo runner settles synchronously (`completed`), orchestrated returns `running` promptly and settles via a RunRegistry worker (terminal state arrives on the SSE/snapshot surfaces)
  - `GET /api/agentic_search/runs/<run_id>` durable run snapshot (reload / share / deep-link)
  - `GET /api/agentic_search/runs/<run_id>/events` SSE — the run's append-only idx-keyed event log replayed + tailed (the normalized search-event stream)
  - `POST /api/agentic_search/runs/<run_id>/cancel` cancel a run (best-effort cancels the backing session when real)
  - `POST /api/agentic_search/sources/<id>/map` start a map-source (SCG indexing) job for one connector (gated on `scg.enabled`, 503 when off; `descriptor` is an UNTRUSTED schema carried in the user query, never the system prompt)
  - `GET /api/agentic_search/sources/<id>/map/events` SSE over the map-job event log (reuses `RunSseGenerator`; `?job_id=` selects a job, else newest for the source)
  - `GET /api/agentic_search/scg` introspection — SCG node/edge/recipe/source counts + mapped source list (gated on `scg.enabled`; reads the deterministic core, never an LLM)
- Channel webhook endpoints (HMAC auth, not API key):
  - `POST /api/webhooks/<platform>` receive inbound message from a chat platform (e.g. `nextcloud-talk`). Delegates to the appropriate `ChannelAdapter` for verification and parsing. Creates/continues sessions using existing session tags.
- Auth: requires `X-API-KEY` header (except webhook endpoints which use platform-specific HMAC verification). Token defaults to `api.master_token` from `configs/app.json` (default: `msk-strong-password`). Also accepts `api_key` query parameter for SSE endpoints (EventSource does not support custom headers). Per-route requirements and the identity plane on top of this are covered in "Access control" above.
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

## SessionSpec — durable purpose binding (read before touching `/query`, `/message`, `/recover`, or trigger fires)

`SessionSpec` (`session_spec.py`) is the durable purpose-binding of a session —
origin, project/cwd, model + fallback ladder, tools + strict-scope, capabilities,
and whether skill-instructions are present. `merge_request_overrides` is the ONE
seam deciding which fields a request may override, tiered `ALWAYS_OVERRIDABLE`
(model, fallback_models, mode) / `OVERRIDABLE_WHEN_UNBOUND` (only once the
session has no bound purpose yet) / `NEVER_OVERRIDABLE` (origin, surface,
capabilities) — a refused override is logged, never silently applied. It ships
a GET `projection()` (`skill_instructions` reduced to a boolean
`skill_instructions_present`, never the raw playbook text) + a server-declared
`editable_fields()` map at `GET /api/sessions/<id>/spec` (`SessionSpecView`) —
the wiki-settings fail-closed pattern (a field absent from the map is not
editable, never editable-by-default): front ends hydrate from it rather than
inferring editability from absence.

- **The `/query` trap.** `POST /api/sessions/<id>/query` (`SessionQuery.post`)
  was the only re-engage path that never loaded persisted context — it
  re-derived model/tools/cwd from the request and PERSISTED a default model,
  corrupting later `/message` and `/recover`, and fell back to an empty
  per-session temp dir whenever nothing named a cwd (the reported "temporary
  project with no awareness of previous state"). It now loads the persisted
  spec FIRST, then applies only sanctioned overrides through
  `merge_request_overrides`; non-spec request keys merge in via `setdefault`
  only, explicitly skipping `SessionSpec.SPEC_OWNED_CONTEXT_KEYS` so a refused
  override can't sneak back in the side door.
- **A readiness gate** (`RunReadinessGate`) refuses a run BEFORE it is
  persisted when `llm.default_model` hasn't finished resolving — a restarting
  worker used to accept a query seconds before its first model call died on a
  missing credential. The gate returns a retryable 503 rather than
  accept-and-die; the readiness signal latches once true and never un-latches,
  and any probe failure OTHER than a config-load failure reports ready, so the
  gate itself can never wrongly refuse live traffic.
- **Capabilities are re-derived per unattended fire, mirroring `allowed_tools`.**
  `allowed_tools` was already recomputed on every trigger fire; capabilities
  were not — so one interactive turn that widened `client_capabilities` (e.g.
  advertising `ask_user`, a tool that BLOCKS until a human answers) could leak
  onto every later scheduled fire with nobody there to answer it.
  `SessionSpec.unattended_capabilities()` strips `INTERACTIVE_ONLY_CAPABILITIES`
  (`{"ask_user"}`) fresh on every fire; the shared idle-restart path under
  trigger delivery rebuilds the fire's context from it rather than trusting
  whatever was last advertised.

## Ask-user questions — api dispatch glue (non-obvious only)

`ask_user.py` hosts the concrete `ApiQuestionDispatcher` + `QuestionPendingCalls`
(registered beside the device-tool dispatcher at startup; core contract +
rationale in `packages/mewbo_core/CLAUDE.md` → "Ask-user questions"). The three
deliberate differences from the device-tool bridge it mirrors:

- **No `has_subscribers` short-circuit.** The console POLLS `/events` (it does
  not hold the transcript SSE open), so subscriber-presence would
  false-negative and kill every console-asked question. The `ask_user`
  capability advertisement is the delivery gate instead — don't "add back" the
  device bridge's check here.
- **No expiry/reaping machinery.** The dispatcher coroutine owns the entry's
  whole lifecycle (create → wait → take/withdraw in `finally`), so the
  registry has no deadline bookkeeping at all. Duplicate-POST honesty is a
  TWO-phase story: 409 while the answered entry lingers (pre-read), 404 once
  the dispatcher took it (or after a supersede withdrew it) — the console/Aura
  cards treat both as "resolved elsewhere", never an error.
- **Answer validation is split by what each layer can know:** the route
  Pydantic-validates SHAPE (`QuestionAnswerItem`, XOR enforced at definition →
  400); the registry validates SEMANTICS against the stored questions under
  its lock (count/bounds/arity → 422 with a user-actionable message, entry NOT
  consumed — the user can fix and resubmit). `X-Mewbo-Surface` becomes
  `answered_via` on the `user_question_answered` event, so every other surface
  can render "answered on console".

Tests: `tests/test_ask_user_routes.py` (route contract + registry semantics +
steer/interrupt/cancel supersede paths, no LLM).

## Agent pickup — CI → session bridge (non-obvious only)

`vcs_pickup.py` (one atomic `VcsPickupService`, DI'd like `ide_routes.py`) is the **CI sibling of the channel adapters**: platform event → tag-keyed session (`vcs:<owner/repo>:<kind>:<number>`, cf. `nextcloud-talk:room:<token>`). It deliberately does NOT implement `ChannelAdapter` (auth is the API key; no HMAC handshake exists), but the reply leg mirrors the channels exactly: `completion_hook` on `on_session_end` (cf. `_channel_completion_hook`, sharing `channels.routes.extract_final_answer`) posts the final answer back to the issue/PR as a comment by the bot account. User docs: `docs/ci-agent-pickup.md`.

- **Gitea Actions ≠ GitHub Actions payloads (verified live):** Gitea has no top-level `event.assignee` on assignment events — guard via `contains(github.event.<issue|pull_request>.assignees.*.login, …)` fallback (side effect: re-assignment while the bot is already assigned re-triggers; harmless, the tag reuses the session). `issue.pull_request` marker IS present on comment payloads; `github.api_url` IS populated (`<server>/api/v1`); `Authorization: token $GITHUB_TOKEN` works on both platforms; the act_runner image ships jq but does NOT trust internal CAs (→ `AGENT_TLS_NO_VERIFY` repo var adds `curl -k`).
- **`_resolve_repo_or_404`'s identity scan covers managed projects only.** A config project that was never promoted does not resolve by `owner/repo` — that's why `VcsPickupService._config_project_for_repo` scans config project paths with `RepoIdentity.aliases_for_path` as a fallback. Don't "fix" this by registering pickup targets via `POST /v_projects` with an explicit path: the worktree reaper deletes childless `path_source == "provided"` parents **permanently**, while config projects self-heal through promote-on-demand.
- **Issue pickups get an isolated worktree from HEAD too (expanded intent).** Not just PRs: `ensure_issue_worktree` cuts a deterministic `mewbo/issue-<n>` branch from the default-branch HEAD (`create_worktree(..., base=origin/<default>|HEAD)`) so concurrent issue pickups never collide in the shared checkout and the agent has a clean push-ready branch. `mewbo/`-prefix ⇒ the reaper deletes the branch with the worktree; idempotent on the deterministic branch ⇒ a repeat pickup reuses it (`base=None` once it exists, so it resumes, not re-bases). Issues therefore resolve with `promote=True` now (was PR-only). KEY ASYMMETRY: it's **best-effort** — a non-git/unpromotable project degrades to the main checkout (returns `None`), never a hard 422 like a PR's *required* head branch.
- **Deployment needs git credentials in the api container.** The pickup fetches PR branches and agent sessions push to them; the image sets `credential.helper=store` but ships no credentials — mount the host's `~/.git-credentials` to the container user's HOME (see `docker-compose.override.yml`, untracked). Without it: 422 `could not read Username`.
- Endpoint auth accepts KeyStore-minted keys (`POST /api/keys`), not just the master token — CI secrets should hold a labeled revocable key.
- **Reply tokens live server-side, keyed by forge host** (`channels.vcs.tokens` config) — the workflow's `GITHUB_TOKEN` dies with the job, long before the agent run ends, so it can't deliver the reply. `/repos/{owner}/{repo}/issues/{n}/comments` + `Authorization: token` are identical on GitHub and Gitea (one client, both forges). Gitea gotcha: minting a PAT for another user (`POST /api/v1/users/<bot>/tokens`, admin-only) rejects token auth with `auth required` — use **basic** auth (`-u admin:$TOKEN`). Unlike the act_runner, the api container's system CA store trusts the internal CA (git and Python `ssl` share it), so `tls_verify` stays default there.
- **PR-creation identity comes from the forge CLI login, not the git credential store (full issue loop E2E-verified live).** `gh` only speaks GitHub, so on Gitea the agent needs `tea` — without it (in the earlier design) the agent self-served by reading the PAT out of the mounted `~/.git-credentials` and curling `POST /repos/{owner}/{repo}/pulls`, which (1) authored PRs as the mounted PAT's human owner and (2) leaked that PAT in plaintext into the session transcript (Mongo/Langfuse). Now `docker/init.d/15-tea-setup.sh` installs-if-missing + logs `tea` in per `channels.vcs.tokens` host (bot identity; falls back to `~/.git-credentials` per host), and the pickup prompt nudges "prefer a forge CLI like `tea` or `gh`". The bot PAT needs scopes `read:user` (tea login resolves the user) + `write:issue` (reply comments) + `write:repository` (PRs) — a `write:issue`-only token 403s on `/api/v1/user` and tea login fails. Git *pushes* still authenticate via `~/.git-credentials` (unchanged). Continuity/guards all live-proven together: `@mention` → `resumed:true` + steering/`r2` on the SAME tagged session and worktree; the bot's own reply comment and a non-bot assignment both skip.

## MCP-facing contracts (non-obvious only)

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
- **Idle session-control follows the common coding-agent convention.** `/interrupt` on idle → 200
  `{interrupted:false}` (no-op); `/message` on idle/finished → re-engage via the
  `start_async`/query path, returning the new `run_id`; only a terminated session
  rejects. `/agents` token rollup delegates to `build_usage_numbers` — the same
  builder `/usage` calls — so a root-only session's call/agent counts aggregate
  correctly; the `input_tokens`/`output_tokens` fields themselves currently read
  0 for every session for now (Langfuse is the only live token source
  meanwhile).
- **Worktree lifecycle is system-owned.** The `on_session_end` hook is the SOLE
  reaper; it also auto-reaps the promoted parent project when it has no worktree
  children left (kills the orphan). The DELETE route is idempotent:
  already-absent → 200 `{status:"already_absent"}`, not 404. MCP no longer
  hands out a worktree handle.
- **`/agents` `total_input_tokens` = PEAK semantics** (`root_peak_input_tokens +
  sub_peak_input_tokens`) matching the `get_session_history` overview; the
  cumulative billed sum is separately exposed as `total_input_tokens_billed`
  (the old bare sum was ~2× the peak and confused callers).
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

## Custom system instructions — REST surface (`system_instructions/routes.py`)

Three routes over the operator's singleton document (see
`packages/mewbo_core/CLAUDE.md` → "Custom system instructions" for the
render/security model): `GET`/`PUT /api/system-instructions`, `POST
/api/system-instructions/preview`, `GET /api/system-instructions/variables`.
Built on the `TriggerRoutesController` DI pattern this file already names as
the reference — `SystemInstructionsRoutesController` owns the injected store
+ auth guard as fields, the Resources are thin adapters wired via
`resource_class_kwargs`.

- **`PUT` compiles before persisting.** `SystemInstructionsDoc.validate_template()`
  runs at the write boundary; a syntax error 400s BEFORE anything reaches the
  store — an operator never silently stores an unparseable template.
- **`/variables` is GENERATED, never hand-authored** — so the variable table the
  console renders can never drift from what the renderer actually exposes. Its
  SHAPE comes from core (`InstructionContext.describe(catalog)`: the model
  documents its own schema); its VALUES come from `InstructionValueSources`. The
  controller re-derives NOTHING about `InstructionContext`'s semantics — the
  schema-walking classmethods that used to sit here (`_resolve_schema_ref` and
  friends) were an HTTP adapter re-implementing the model's meaning, and MOVED
  onto the model. The controller keeps only its wire ownership. (The `$ref`-drop
  trap the config-schema section above documents is alive in that resolution, just
  a different symptom: there a dropped `x-group`, here an enum field that would
  read as a bare `"string"` with no values for an operator to compare against.)
- **`InstructionValueSources` (`value_sources.py`) is the I/O EDGE — the ONLY
  class in this feature that touches the outside world, and its failure isolation
  is the whole design, not a nicety.** `/variables` is precisely what an operator
  opens when their template is misbehaving, which is often *because* something in
  the deployment is broken. So the four probes (tools / capabilities / projects /
  models) run CONCURRENTLY under ONE shared wall-clock deadline and each degrades
  to `()` independently — logged once, NEVER re-raised into the request. A dead
  LiteLLM proxy (`LLMConfig.list_models` RAISES `ValueError` on an unreachable
  one) or a hanging MCP server must empty ONE row, never 500 the page that exists
  to debug it. Verified live: proxy down ⇒ `models: []` while `tools` and
  `capabilities` still resolve. The deadline is what a plain `try/except` cannot
  cover — a probe that never returns at all. Collaborators are injected as FIELDS,
  so a test simulates "the proxy is down" by injecting a config whose
  `list_models` raises: no monkeypatching, no network in the suite.
- **A probe is a long-lived single-flight `SourceProbe`, NOT a per-request
  `ThreadPoolExecutor` — and that is a leak fix, not a style preference.** The
  first cut spawned a pool per request and shut it down `wait=False`. But pool
  workers are NON-DAEMON and `cancel_futures=True` only drops *queued* futures
  (all four start immediately), so a probe wedged in MCP discovery kept its thread
  for the process lifetime, accreting one MORE per `/variables` call while the
  server stayed wedged — and `_python_exit`'s atexit join meant a hung worker also
  BLOCKED interpreter shutdown (a gunicorn worker hanging until SIGKILL). Now each
  source owns one daemon thread and a second caller JOINS the in-flight probe
  rather than stacking another, so a wedged source costs exactly one thread, ever.
  Rule for any future best-effort fan-out on a request path: a bounded deadline
  does not bound the WORKER, only the WAIT.
- **THE TRAP, and it bit twice: the tools catalog and a SESSION must be resolved
  from the SAME registry inputs, or the reference lies in the one direction its
  caveat does not cover.** `load_registry` merges `<cwd>/.mcp.json` and the
  subtree's, so a catalog built at `cwd=None` does not report a merely WIDER list
  than a project-scoped session's — it reports a **DIFFERENT** one, omitting MCP
  ids that the operator's own sessions genuinely hold. The `known` note says the
  session list is *narrower*, which made the gap invisible. So `_registry_cwds()`
  probes `None` **plus every CONFIGURED project path** (existing dirs only: a stale
  `app.json` entry must not spend the deadline on a directory that is gone).
  **Both tests missed the original bug** and that is the reusable lesson: core's
  test built its catalog from the very registry the session was scoped from (a
  tautology w.r.t. cwd — it could only ever pass), and the app's fake registry
  *swallowed the `cwd` kwarg* (`lambda **_kwargs: registry`). A fake that discards
  the argument under test cannot fail. The suite now RECORDS every `cwd` and
  asserts the exact set.
- **MANAGED projects are deliberately NOT probed — a bound, not an oversight.**
  They are server-created, so the set is unbounded and churning (worktrees cut per
  pickup and reaped; a long-lived store accretes promoted parents — a dev box held
  **974**, nearly all dead `/tmp` paths from old test runs). Probing them put an
  O(store-size) fan of LIVE MCP discovery on the settings page and took the cold
  probe to **15s**, which the deadline then ate — silently emptying the very tools
  row the union existed to complete. The residue is DISCLOSED in the `tools` note
  instead of hidden. **A `known` list with an accurate caveat is honest; an
  exhaustive-looking list that times out into emptiness is not.**
- **`probe_timeout` (20s) is sized so a COLD tools probe FITS, and that is the
  point.** Cold = live MCP discovery across the configured projects (~7.5s here
  with fifteen servers); warm = ~0.2s, because the registry cache is process-wide
  and ordinary session traffic warms it too. A deadline *under* the cold cost does
  not protect anyone — at the original 6.0s it just made the page prefer an EMPTY
  tools row (a lie, the exact lie this feature exists to remove) over a slow one
  (the truth), once per restart. Make them wait once behind the pane's existing
  "Loading variables…" state; do not tell them their deployment has no tools.
  **Priming the probes at startup was tried and REVERTED — remember why:** the API
  is served by gunicorn, which IMPORTS this module rather than calling `main()`, so
  the only place a prime fires in production is module scope, which also fires it in
  every test that imports `backend.py` — real MCP network I/O inside the suite. Not
  worth saving one slow page load per restart. **Corollary for anything else you are
  tempted to warm at boot here: in this app, "at startup" and "at import" are the
  same place, and import is shared with the tests.**
- **The `/variables` wire contract CHANGED, breaking: `enum` was DROPPED** in
  favour of `values` + `valuesKind` + `valuesNote` (see core's `closed` vs `known`
  bullet — that distinction is the reason a plain value list is not enough).
  `enum` had become a redundant SECOND channel for the same fact: `values` +
  `valuesKind: "closed"` says it strictly better, and two channels carrying one
  fact is exactly the drift the house rules forbid. Breaking a shipped contract
  was safe ONLY because the feature landed the same week and the console is its
  sole consumer, updated in the same change — do not read this as licence to break
  a wire shape that has real clients. `docs/openapi.json` regenerated.
- **`/preview` never 500s on a bad template** — a render failure comes back
  200 with `{rendered: "", error: "..."}`. Its whole purpose is letting an
  author see what a given surface actually gets BEFORE it reaches a live
  session, so the debugging surface can't itself break on the exact input it
  exists to catch.
- **Store construction is deliberately unguarded here.** `backend.py`'s
  `init_system_instructions()` calls `create_system_instructions_store()` with
  no try/except, but it runs after `session_store = create_session_store()`
  already fails the app at startup when `storage.driver=mongodb` and Mongo is
  unreachable — that existing fail-fast covers store construction for this
  feature too, so re-guarding it here would be dead code.

## Session-run boot sweep (`run_sweep.py`)

A process death (deploy, restart, OOM kill) can strand an in-flight session run:
the worker dies mid-turn, the transcript ends on run activity with no terminal
`completion`, and `summarize_session` then derives `status="idle"` — the
console's recovery card never renders, leaving the user guessing whether
anything ran. `SessionRunSweeper.sweep()` (`backend.py`, called once at import,
which in this app IS startup) closes that gap: it appends a synthetic terminal
`completion` (`{done: true, done_reason: "error", error: "interrupted: process
restart", task_result: null}`) to every session whose transcript shows an open
run, so the derived status flips to `failed` and the existing recovery
affordance surfaces.

- **Orphan detection is a narrow, miss-only positive allowlist.** A run is
  "open" only if one of `run_accepted`/`llm_call_start`/`llm_call_end` appears
  with no `completion` after it — events that occur ONLY within a root run's
  active lifetime. A bare `user` turn, a background `sub_agent` stop, or a
  `user_steer` can legitimately land AFTER a completed run's `completion` (a
  queued next turn; a late-arriving sub-agent lifecycle event; a `/message`
  slipping into the handle-teardown window after the completion write), so
  including any of them would FALSE-FLIP a genuinely-completed session to
  `failed` — and `user_steer` is redundant anyway, since every real run is
  anchored by its own `run_accepted`/`llm_call_start`. Worst case of the
  narrow allowlist is a MISSED orphan (status stays `idle`, no regression),
  never a wrongly-flipped completed run — the contract is miss-only by
  construction, not merely by care.
- **This WRITES to the configured store at import**, shared with tests (any
  test importing `backend.py` triggers it against whatever store the test
  config points at) — idempotent (a session already carrying a terminal
  completion for its last turn is skipped), but still a write. `MEWBO_BOOT_RUN_SWEEP=0`
  opts out entirely (env-driven, no config-schema knob, mirrors
  `MEWBO_APPS_ROOT`). See "Config endpoints & secret handling" above's `/variables`
  prime-at-boot note for why import == startup applies here too.
- **The Mongo session store gained a real tail-bounded `load_recent_events`
  override** (`session_store_mongo.py`) — a bounded `sort(ts DESC).limit(n)`
  range read — because the base template method materializes the WHOLE
  transcript before slicing. The JSON driver has no cheap reverse read for a
  flat JSONL file, so it inherently pays O(transcript) per candidate session;
  Mongo now pays one indexed tail query instead. Either way only each
  candidate's tail (`_TAIL_SCAN_LIMIT`, 64 events) is scanned, and a
  `_RECENT_ACTIVITY_WINDOW` (7 days) bounds which orphans get settled — an
  ancient abandoned session isn't the incident this fixes and shouldn't
  resurface as a fresh red card on every future boot.

## Hidden dependencies / assumptions
- Uses core logging (`mewbo_core.common.get_logger`); log level controlled by `runtime.log_level`.
- Relies on core LLM config (`llm.api_base`, `llm.api_key`, `llm.default_model`, `llm.action_plan_model`).
- No rate limiting.
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
