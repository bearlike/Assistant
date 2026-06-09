> ↑ [root /CLAUDE.md](../../CLAUDE.md)

# Mewbo MCP — Project Guidance

Scope: `apps/mewbo_mcp/`. Captures non-obvious decisions so changes stay safe and predictable.

## Why a separate process

FastMCP is ASGI; `mewbo_api` is WSGI (Flask-RESTX / Gunicorn). They cannot share a process. The MCP server runs as a standalone Streamable-HTTP service on `/mcp`, calls the REST API over HTTP, and uses the official `mcp` SDK (`FastMCP`).

## Auth: token pass-through

The MCP facade is *curation*, not a security boundary. Issued keys are full-power and go only to trusted agents. Auth flow per tool call:

1. `auth.extract_bearer_token(ctx)` — pulls `Authorization: Bearer <token>` from the incoming HTTP request context.
2. `auth.validate_token(token)` — validates locally via the shared `mewbo_core` `KeyStore` (or master-token equality). Rejects bad tokens before any downstream request is issued.
3. The validated token is forwarded verbatim to REST as `X-API-Key` via `RestClient`. No privileged service identity; the master token is never placed on the wire by the MCP service.

## Shared KeyStore dependency (critical for deployment)

`auth.validate_token` calls `mewbo_core.key_store.create_key_store()`, which selects either the file driver (`$MEWBO_HOME/api_keys.json`) or the Mongo driver (`api_keys` collection) depending on config. The MCP service **must** share the same storage as the API — same `MEWBO_HOME` volume mount or same `MEWBO_MONGODB_URI`. Without this, the MCP server can only accept the master token; no issued key will validate.

In Docker Compose the `api-data` named volume is mounted at `/app/data` in both services. If you use the Mongo driver, pass the same `MEWBO_MONGODB_URI` in `docker.env`.

## Code shape — every tool group is an atomic class

`tools.py` models each tool group as one frozen dataclass — `SessionTools`,
`WikiTools`, `IntegrationTools`, `SearchTools`. The class holds the injected
`RestClient` (plus any per-feature config: `timeout_s`/`poll_interval_s` for the
bounded-poll groups, a `TURN_TEXT_TRUNC` / `TERMINAL_STATUSES` `ClassVar` for the
rest) **as state**, and exposes the feature's behaviors as methods over it; pure
helpers are `@staticmethod` / `@classmethod`. `server.py` is the only FastMCP
touch point and constructs one per call — `await SessionTools(client).history(…)`
— so the client is **dependency-injected** and tests stub only the HTTP boundary.

The three module-level `_as_dict` / `_dict_list` / `_as_list` are cross-cutting
coercion primitives shared by every group; everything feature-specific lives on
its class. **Adding a group = adding a class, not a pile of module functions.**
This is the house style (atomic class holds state; class/static methods describe
behavior; DI over globals) — keep new code in it.

## Gold-standard contract (the 5 invariants — issues #40–#45)

The audit's north star is `search`/`get_search_run`; every tool converges to it.
Five cross-cutting invariants, each with ONE shared seam — keep new tools on them:

1. **Structured error envelope.** Every `@mcp.tool()` is wrapped by the
   `_enveloped` decorator in `server.py` — a `RestError`/`ValueError` becomes
   `{"error": {code, reason, retryable}}` (retryable = transport/5xx), never a
   raised exception or leaked transport text. The partner seam is
   `rest._error_detail`: it reads the API's `{"error": {"reason"}}` envelope and
   **drops a raw HTML body** (Werkzeug's 404 page) instead of dumping it — the
   fix for the "raw HTML 404 in the tool result" report. Don't add per-tool
   try/except; rely on the decorator.
2. **Async run-handle for every long await.** The bounded await is one helper,
   `tools.bounded_poll(fetch, is_terminal, *, timeout_s, interval_s)` (shared by
   `search`, `ask`, `structured_query`). Every long tool returns a handle + has a
   `get_*_run` companion: `search`→`get_search_run`, `ask_wiki`→`get_wiki_answer`,
   `structured_query`→`get_structured_run`. A timeout returns `status:"running"`
   + the handle, never a terminal failure. **`run_id` is per-run, not the
   session id** (Devin-modeled): the API mints `"<session_id>:r<seq>"`; MCP only
   passes it through.
3. **Smallest-useful-payload default.** Projection lives MCP-side. `list_sessions`
   caps to 20 newest + compact rows; `read_wiki_structure` defaults to `detail=
   "stats"` (never the full graph dump); `list_integrations` drops the boilerplate
   `"MCP tool X from Y"` descriptions; `get_session_history full` references the
   agent tree instead of inlining it.
4. **Schema matches behavior.** `create_session` takes a single `tag` (the API
   stores one `session_tag`); `ask_wiki`'s `model` is genuinely optional (server
   defaults it); `list_sessions` returns the `project` it filters on; overview
   reads the API's authoritative `status`/`title` from `/events` (the source of
   the old `status:null` / ignored-title bugs) — not a timeline reconstruction.
5. **Discovery + actionable ids.** `list_projects` wraps `GET /api/projects`
   (registered name + git `repo`/`aliases` identity); `create_session` returns
   the `worktree_project_id`+`parent_project_id` a later `cleanup_worktree` needs.

Two display traps worth remembering: the **QA terminal signal** is the snapshot's
`status` (authoritative when present — a `running` snapshot is never terminal even
with a premature `sources` block; the sources-block check is only the fallback for
status-less snapshots) — that's what killed the "complete-but-truncated" answers,
NOT a poll-stability heuristic. And a **tool-call-only turn's `(no content)…`
summary** is rendered from the turn's steps (`→ called <tool>`) keyed on OUR
`NO_CONTENT_SENTINEL`; the trailing `call:default_api:<tool>{}` is a Gemini
text-leak (a response-normalization concern fixed at the LiteLLM/adapter seam,
never string-matched here). See `[[feedback_tool_call_normalization_not_loop]]`.

**`ask_wiki` answer serializes ALL block kinds.** `_block_text` must enumerate
every `WikiEmitBlock` variant — `table`→markdown table, `diagram`→mermaid
placeholder, `hr`→`---`. The old allowlist silently dropped these, so
enumerate/compare answers returned `complete` but non-responsive (#52).

**Worktree lifecycle is system-owned, not MCP-provisioned.** `create_session`
returns `{session_id, status}` only; no worktree handle. The worktree is
provisioned/reaped by the API's `on_session_end` hook, never by the caller.
`cleanup_worktree` was removed (#53). Governing principle: minimize session
knobs exposed to callers — fewer knobs = better fast-model agentic accessibility.

## Tool groups (~15 tools)

**A. Sessions — create & control**
- `create_session` — auto-provisions a fresh worktree+branch off the base (or targets `branch`/`worktree`). `project`/`repo` resolves by registered name OR git identity `host/owner/repo` (see `list_projects`). Single `tag`; `idempotency_key` tags for retry-identity. Returns `{session_id, status, worktree_project_id?, parent_project_id?}`. Wires `POST /api/v_projects/<id>/worktrees` → `POST /api/sessions` → `POST /api/sessions/<id>/query`.
- `send_followup` — `POST /api/sessions/<id>/message`: steers a running session, or RE-ENGAGES an idle/finished one (returns the new `run_id`); only a terminated session rejects.
- `interrupt_session` — `POST /api/sessions/<id>/interrupt`; idle = graceful no-op (`status:"no_active_run"`).
- `cleanup_worktree(parent_project_id, worktree_project_id, force?)` — `DELETE /api/v_projects/<parent>/worktrees/<id>`: reaps the worktree, keeps the transcript resumable.

**B. Sessions — discover & read (tiered)**
- `list_sessions` — `GET /api/sessions` with optional project/status/since filters.
- `get_session_history(session_id, level, turn?)` — four tiers over `GET /api/sessions/<id>/events`:
  - `overview` — title, summary, status, turn/step counts, token totals (cheapest).
  - `turns` — one row per turn: truncated user/assistant text, done_reason, step count, per-turn tokens.
  - `steps` (needs `turn=N`, 1-based) — per-step `tool_id → summary` previews, no full results.
  - `full` (needs `turn=N`) — full step logs (tool_input, result, error) plus sub-agent tree.
- `get_agent_tree` — `GET /api/sessions/<id>/agents`.

**C. Wiki — query & ask**
- `list_wiki_projects`, `read_wiki_structure`, `read_wiki_page` — thin wrappers over `GET /v1/wiki/*`.
- `submit_insight` — `POST /v1/wiki/projects/<slug>/insights`. Suggest a memory note for the multiplex code–memory–docs graph: the server condenses raw text into atomic claims, auto-anchors each to the tree-sitter graph, dedups, and safely merges (the human/external-agent write surface; the indexer/QA agents use the in-session `wiki_submit_insight` tool). `condense=True` (default) → `raw` body (decompose); `condense=False` → `content` body (verbatim ≤200-char claim). Returns the per-claim `IngestResult`. The endpoint returns 201 on store, **200 with `ok:false`** when every claim is rejected (a normal advisory outcome — NOT an error, so it doesn't raise `RestError`).
- `ask_wiki` — `POST /v1/wiki/qa` (SSE-only): stream the start frames to read the `meta` `answerId`, then `bounded_poll` `GET /v1/wiki/qa/<id>` until the snapshot `status` is terminal (`complete`/`cancelled`/`error`; sources-block fallback only when status-less). Returns `{answer_id, answer, citations, status}`; `status:"running"` means the await budget elapsed — resume via `get_wiki_answer(answer_id)`. `model` is optional (server defaults it).
- `get_wiki_answer(answer_id, detail?)` — `GET /v1/wiki/qa/<id>`; the companion that consumes the `answer_id` (mirrors `get_search_run`).

**D. Integrations / capability discovery**
- `list_integrations` — `GET /api/tools` + `GET /api/plugins`, projected to `{tool_id, name, kind?, enabled?}` (boilerplate descriptions dropped), so a caller knows what tool ids to pass to `create_session`'s `integrations` argument.
- `list_projects` — `GET /api/projects`: registered config + managed projects with `name`/`project_id` + canonical git `repo`/`aliases`. The discovery surface for `create_session`'s `project`.

**E. Mewbo Search — multi-source workspace search**
- `list_search_workspaces` — `GET /api/agentic_search/workspaces`, projected compact (`id/name/desc/sources/recent_query_count`). **Drops `instructions` and the full `past_queries`** — `instructions` is untrusted prompt input and must not leak to a consuming agent; the history is console state.
- `search(query, workspace, project?, detail?)` — resolves `workspace` (id OR case-insensitive name) → `POST /api/agentic_search/runs` → awaits a terminal run, then projects the `RunPayload`.
- `get_search_run(run_id, detail?)` — `GET /api/agentic_search/runs/<id>`; same projection (replay / deep-link, or re-read an async run that returned `running`).

Non-obvious decisions for this group:

- **Await mirrors `WikiTools.ask`, forward-compatible with the async runner.** The default `EchoSearchRunner` finishes synchronously so `POST /runs` is already terminal and `search` returns instantly with zero polling. The real `OrchestratedSearchRunner` returns a `running` snapshot immediately; `search` then polls `GET /runs/<id>` until a terminal status (`completed`/`failed`/`cancelled`) or `SearchTools.timeout_s`, returning the partial with `status:"running"` rather than hanging. **Don't add an SSE consumer here** — the snapshot poll is the canonical bounded await; the SSE stream is the console's live-reveal transport.
- **Two tiers, not four.** `detail="answer"` (default, cheapest) returns the cited synthesis + a compact result index (`id/source/kind/title/url/relevance`) so citations resolve; `detail="full"` adds `snippet/insight/refs`. Runs are shallow (unlike sessions, which justify `get_session_history`'s four tiers), so two suffice.
- **The projection always drops the per-source trace + decorative fields** (`related_people`, `image`, `embed`). Those are console-render signal, not search signal — an external agent never needs them. Read the run's SSE stream directly if you ever do.
- **`SearchTools.TERMINAL_STATUSES` is duplicated, not imported.** Same HTTP-boundary rule as everything else: this process talks to the API over REST only and never imports `mewbo_api`. The `ClassVar` is a small mirror of `agentic_search.schemas.TERMINAL_RUN_STATUSES`; if the contract's terminal set changes, update the mirror.
- **Workspace resolution is by id-or-name** (agents think in names, as with `create_session`'s repo resolution). An ambiguous name or no-match raises `ValueError` with the candidates rather than silently searching the wrong workspace. Workspace authoring (create/edit) is deliberately NOT exposed — that's the console's surface; the MCP group is read+search only.

**F. Structured query — schema-constrained synthesis**
- `structured_query(query, schema, workspace?, tool_ids?)` — `POST /v1/structured`
  (async run-handle). The server runs an agentic session (model may call grounding
  tools) and emits a JSON-Schema-validated object. Returns `{run_id, status,
  output?}`; `bounded_poll`s `GET /v1/structured/<run_id>` for a slow run, else
  resume via `get_structured_run(run_id)`. Atomic class
  `StructuredQueryTools(RestClient)`; the MCP-tool param is `tool_ids` (not
  `tools`) to avoid shadowing the `tools` module — forwarded as the body's `tools`.
- `get_structured_run(run_id)` — `GET /v1/structured/<run_id>`; resume/replay a run.

## `timeline.py` — DRY pairing with the console

`timeline.py` is a Python port of `buildTimeline` / `computeTurnTokenUsage` from `apps/mewbo_console/src/utils/timeline.ts`. The two files are intentionally kept in sync — the console renders the same turns visually that MCP exposes to callers. Parity is enforced by `apps/mewbo_mcp/tests/test_timeline.py` against shared fixtures. **When you change turn-boundary or token-usage logic in either file, update both and the parity test.**

Turn boundary rule (matches TS): a `user` event opens a turn; the next `assistant` event closes it. A `completion` event defensively closes an open turn if no `assistant` event precedes it. `tool_result` events inside a turn are steps. Token totals: PEAK root input (context pressure), SUM output (additive), SUM sub-agent peaks.

## Env vars

| Variable | Default | Notes |
|---|---|---|
| `MEWBO_API_URL` | `http://localhost:5124` | REST API base URL. Default targets local dev (`mewbo-api` binds `:5124`). In Docker (compose uses `network_mode: host`): `http://localhost:5125` (gunicorn port) via the compose override. |
| `MEWBO_MCP_HOST` | `127.0.0.1` | Bind host. Set to `0.0.0.0` in Docker. |
| `MEWBO_MCP_PORT` | `5127` | Bind port. Deliberately not `5125` — that is the API's gunicorn port in Docker (`API_PORT=5125`), so sharing it would clash. |
| `MASTER_API_TOKEN` | `msk-strong-password` | Break-glass token; must match the API's value. Set in `docker.env`. |
| `MEWBO_HOME` | `~/.mewbo` | Data dir for file-driver KeyStore (`api_keys.json`). Must match the API. |
| `MEWBO_MONGODB_URI` | *(unset)* | When set, Mongo KeyStore driver is selected — must point to the same DB as the API. |

## Run

```bash
uv run mewbo-mcp
```

Entry point: `apps/mewbo_mcp/src/mewbo_mcp/server.py:main`. Calls `build_server()` → `server.run(transport="streamable-http")`. MCP endpoint: `http://<host>:<port>/mcp`.
