> ↑ [root /CLAUDE.md](../../CLAUDE.md)

# Mewbo MCP — Project Guidance

Scope: `apps/mewbo_mcp/`. Captures non-obvious decisions so changes stay safe and predictable.

## Why a separate process

FastMCP is ASGI; `mewbo_api` is WSGI (Flask-RESTX / Gunicorn). They cannot share a process. The MCP server runs as a standalone Streamable-HTTP service on `/mcp`, calls the REST API over HTTP, and uses the official `mcp` SDK (`FastMCP`).

## Auth: token pass-through

The MCP facade is *curation*, not a security boundary. Issued keys are full-power and go only to trusted agents. Auth flow per tool call:

1. `auth.extract_bearer_token(ctx)` — pulls `Authorization: Bearer <token>` from the incoming HTTP request context.
2. `auth.validate_token(token)` — validates locally via the shared `mewbo_core` `KeyStore`'s expiry-aware `resolve_key` (or master-token equality). Rejects a bad OR expired token before any downstream request is issued — mirrors the REST API's own `resolve_key`-based key auth (`AuthKit.require_api_key`), so a credential the API would reject on expiry is never even forwarded. `verify_key` (expiry-blind) is deliberately NOT used here.
3. The validated token is forwarded verbatim to REST as `X-API-Key` via `RestClient`. No privileged service identity; the master token is never placed on the wire by the MCP service.

**Scopes are deliberately NOT checked here.** `resolve_key`'s returned record may carry a `KeyScopes`-matchable `scopes` field, but this shim does not consult it. Scope enforcement is the REST API's job, same as the "curation, not access control" law under "Tool exposure" below: the API is where `X-API-Key` gets re-validated per route, and route-level scope requirements belong next to the routes they protect, not duplicated into a facade that has no per-tool scope taxonomy of its own. Gating tool exposure by scope here would need a second tool→scope mapping to stay in sync with whatever the API enforces — exactly the kind of drift-prone parallel decomposition the group/tier exposure gate later in this file was designed to avoid.

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

## Gold-standard contract (the 5 invariants)

The audit's north star is `search`/`get_search_run`; every tool converges to it.
Five cross-cutting invariants, each with ONE shared seam — keep new tools on them:

1. **Structured error envelope.** Every `@tool(ToolGroup.X)` is wrapped by the
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
   session id** (a parent-session:sub-run id convention): the API mints `"<session_id>:r<seq>"`; MCP only
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
enumerate/compare answers returned `complete` but non-responsive.

**Worktree lifecycle is system-owned, not MCP-provisioned.** `create_session`
returns `{session_id, status}` only; no worktree handle. The worktree is
provisioned/reaped by the API's `on_session_end` hook, never by the caller.
`cleanup_worktree` was removed. Governing principle: minimize session
knobs exposed to callers — fewer knobs = better fast-model agentic accessibility.

## Tool exposure — gate AT REGISTRATION, on TWO orthogonal axes

`config.py`'s `ToolGroup` + `EffectTier` + `McpToolPolicy` are the exposure
gate, and `build_server`'s local `tool(group, tier)` registrar is the ONE seam
that applies it. Every registration reads `@tool(ToolGroup.X, EffectTier.Y)` —
there is **no bare `@mcp.tool()`** in this app, deliberately.

**Why two axes.** The subsystem cut alone made "expose wiki READS but not wiki
ASKS" *inexpressible*, and that was the defect: reading a generated page and
asking the wiki a question are the same PRODUCT but not the same COST — one
returns stored bytes, the other starts a model run. So each tool declares both
which subsystem it belongs to and what it does to the deployment:

| Tier | Means | Examples |
|---|---|---|
| `read` | returns stored data; no model call, no run | `list_wiki_pages`, `read_wiki_page`, `get_wiki_answer`, `list_sessions` |
| `navigate` | graph traversal over stored edges; no model call | `graph_neighbors` |
| `ask` | starts a model run | `ask_wiki`, `search`, `structured_query`, `submit_insight` |
| `drive` | creates, steers, or kills a session | `create_session`, `terminate_session`, `cancel_trigger` |

**Exposure is the CONJUNCTION.** `policy.allows(group, tier)` is two set
memberships and nothing else — no dispatch, no `if kind ==`. Allowing the `ask`
tier does not expose a withheld group's ask-tier tools, and allowing a group
does not expose the tiers the operator turned off. Two notes worth keeping:
`get_wiki_answer` is `read`, not `ask` (it re-fetches a stored snapshot rather
than starting a second run), and `submit_insight` IS `ask` (the server condenses
the raw text with a model call before storing it).

**Filter at construction; never build-then-prune.** A withheld tool's function
is returned undecorated, so FastMCP never sees it: `Tool.from_function` never
runs, no arg-model/JSON schema is built, and the tool exists in neither
`list_tools` nor dispatch. (Verified: a default `build_server()` constructs 15
tool schemas out of a possible 24, not 24-then-delete.) An earlier iteration
registered everything and then called `remove_tool` — same observable surface,
wrong paradigm: absence became a side effect rather than a decision, and the
gate sat 400 lines away from the `@mcp.tool()` a new author actually reads.

**The group IS the subsystem taxonomy — don't invent a third one.** `ToolGroup`
mirrors the atomic tool-group classes in `tools.py`. A flat list of bare tool
NAMES would be a parallel, drift-prone copy of a decomposition that already
exists. The effect axis is not a second taxonomy of the same thing; it is a
different question about the same tool.

**Fail-closed, by construction.** Four properties, each load-bearing:

- **Every registration must name a group AND a tier**, so a new tool cannot be
  added without both decisions being made at the line its author is editing.
- **`_DEFAULT_EXPOSURE` must decide every `ToolGroup` and `_DEFAULT_TIER_EXPOSURE`
  every `EffectTier`** — one import-time `RuntimeError` loop covers both axes.
  A new member can never silently auto-expose.
- **`MEWBO_MCP_EXPOSED_GROUPS` / `MEWBO_MCP_EXPOSED_TIERS` are ALLOWLISTS**
  (naming a member is the only way to expose it), resolved independently, and an
  unknown name is a hard startup `ValueError` — never a silent no-op. Same rule
  as `extra="forbid"` on a Pydantic wire model.

**Default GROUP exposure: `sessions` + `wiki` only.** `search` and `structured`
stay withheld for the reasons below; `integrations` and `triggers` joined them —
capability discovery describes this deployment's installed tools/plugins/projects
and trigger management is an operator surface, so neither is part of the minimum
agent-facing surface. Agentic Search is a *product* surface — the console and the
REST API own it — and `structured_query` routes GRAPH-FIRST through a mapped
Search workspace, so exposing it re-opens the same surface by another door.
Neither touches the engine: `scg.enabled` stays on and `/api/agentic_search/*`
keeps serving the console, because MCP reaches search only over REST. The tools
are **gated, not deleted** —
`MEWBO_MCP_EXPOSED_GROUPS=sessions,wiki,integrations,triggers,search,structured`
restores the full surface.

**Default TIER exposure: all four open.** The effect axis ships open on purpose.
It exists so an operator CAN narrow the facade — `MEWBO_MCP_EXPOSED_TIERS=read,navigate`
yields a wiki that lists pages and walks the graph but answers no questions and
starts no runs — not to narrow it by default. Widening a *group* is still the
explicit act.

**This is curation, not access control.** Per the auth section above, issued keys
are full-power: a key-holding caller can still hit `/api/agentic_search/*` on the
REST API directly. Withholding a group or tier here removes it from *this
facade's* surface; it is not a security boundary. Enforcing that would be an API
auth-scope change, not an MCP one.

The instructions blurb is DERIVED from the policy (`_instructions`) so the server
can never advertise something it withholds — gold-standard invariant 4 (schema
matches behavior) applied to the server's own description. It splits on the
effect axis too: withhold `ask` and the blurb stops promising answers and stops
listing long-running tools, while still advertising the reads.

## Response bounds — bound the QUERY, don't truncate the reply

Mewbo's internal `ToolSpec.max_result_chars` cap CANNOT reach an external MCP
client (different process), so each external tool carries its own ceiling. The
rule for choosing one: **truncating a large answer to N characters is
context-safe but useless** — the caller gets a mangled prefix of a JSON blob.
Bounding or paginating at the QUERY returns a COMPLETE, SMALL, CORRECT answer.

- `list_wiki_pages` → `limit`/`offset` on `GET /v1/wiki/projects/<slug>/pages`;
  the reply carries `truncated` + `nextOffset` so a caller pages forward.
- `graph_neighbors` → the shared `WikiGraphNeighborsArgs` bounds (`hops<=3`,
  `limit<=500`) plus a facade default of **25** nodes rather than the model's
  internal 50 (an external caller's context is not ours to spend). Its edge list
  is the one place a response-side cap survives, because the node limit does not
  bound a hub's incident edges at all — `WikiTools.NEIGHBOR_EDGE_LIMIT` caps them
  and reports `edgesTruncated` + the true `edgeCount`. Announced, never silent,
  and the node list stays complete.
- `read_wiki_structure` was already bounded by its `detail` tier (default
  `stats`, never the full dump) + `limit`.
- **Still unbounded:** `list_wiki_projects` (a whole-project list with no cap —
  small in practice, but nothing enforces that) and `read_wiki_page` (returns a
  full markdown body; a large generated page has no ceiling). Neither has a
  natural pagination key today; both are worth a bound the next time this
  surface is touched.

## Tool groups (24 tools; 15 exposed by default)

**A. Sessions — create & control**
- `create_session` — auto-provisions a fresh worktree+branch off the base (or targets `branch`/`worktree`). `project`/`repo` resolves by registered name OR git identity `host/owner/repo` (see `list_projects`). Single `tag`; `idempotency_key` tags for retry-identity. Returns `{session_id, status, worktree_project_id?, parent_project_id?}`. Wires `POST /api/v_projects/<id>/worktrees` → `POST /api/sessions` → `POST /api/sessions/<id>/query`.
- `send_followup` — `POST /api/sessions/<id>/message`: steers a running session, or RE-ENGAGES an idle/finished one (returns the new `run_id`); only a terminated session rejects.
- `interrupt_session` — `POST /api/sessions/<id>/interrupt`; idle = graceful no-op (`status:"no_active_run"`).
- `terminate_session` — `POST /api/sessions/<id>/terminate`: PERMANENT, irreversible kill switch (never reads as another recoverable state). Distinct from `interrupt_session`, which only pauses the current run — the session stays steerable. Cascade-cancels every armed trigger on the session (`cancelled_triggers` in the result); reads stay open, there is no un-terminate. Idempotent.
- `cleanup_worktree(parent_project_id, worktree_project_id, force?)` — `DELETE /api/v_projects/<parent>/worktrees/<id>`: reaps the worktree, keeps the transcript resumable.

**B. Sessions — discover & read (tiered)**
- `list_sessions` — `GET /api/sessions` with optional project/status/since filters. Forwards the honest-outcome facets `recoverable` + (when present) `blocked_code`/`failure_reason`/`models_tried` off the summary row (pure projection — the row is `summarize_session` output).
- `get_session_history(session_id, level, turn?)` — four tiers over `GET /api/sessions/<id>/events`:
  - `overview` — title, summary, status, turn/step counts, token totals (cheapest); plus `recoverable` and (when the run hit one) `blocked_code`/`failure_reason`/`models_tried`.
  - `turns` — one row per turn: truncated user/assistant text, done_reason, step count, per-turn tokens, plus `error` (truncated, omitted unless the run failed), `blocked_code` when the run hit a user-actionable wall (`done_reason` then reads `"blocked"`), and `unmet_goal_reason` when a session-end hook contradicted a clean completion (`done_reason` then reads `"unmet_goal"`).
  - `steps` (needs `turn=N`, 1-based) — per-step `tool_id → summary` previews, no full results.
  - `full` (needs `turn=N`) — full step logs (tool_input, result, error, `blocked_code`, `unmet_goal_reason`) plus sub-agent tree.
- `get_agent_tree` — `GET /api/sessions/<id>/agents`.

**C. Wiki — query & ask**
- `list_wiki_projects`, `read_wiki_structure`, `read_wiki_page` — thin wrappers over `GET /v1/wiki/*`. **`read_wiki_structure` is MISNAMED for what it returns**: it maps to `GET .../graph`, i.e. the multiplex CODE GRAPH, not a page index — the page index is `list_wiki_pages`.
- `list_wiki_pages(project, title_contains?, limit=50, offset=0)` — `GET /v1/wiki/projects/<slug>/pages`, the `{id, title}` roster `read_wiki_page` consumes. Nothing exposed a way to discover a page id before this. Paginated at the query (`truncated` + `nextOffset`), never truncated.
- `graph_neighbors(project, node_id, edge_kind?, direction?, hops?, limit=25)` — `GET /v1/wiki/projects/<slug>/graph/neighbors`; directed, kind-filtered, multi-hop BFS answering "what calls X" / "what does X contain" in ONE call. **No model call, no run** (`navigate` tier). The route reuses the down-layer `WikiGraphNeighbors` engine + its `WikiGraphNeighborsArgs` AS the query contract — the bounds and vocabularies have exactly one definition, and `extra="forbid"` reaches the HTTP boundary for free.
- `submit_insight` — `POST /v1/wiki/projects/<slug>/insights`. Suggest a memory note for the multiplex code–memory–docs graph: the server condenses raw text into atomic claims, auto-anchors each to the tree-sitter graph, dedups, and safely merges (the human/external-agent write surface; the indexer/QA agents use the in-session `wiki_submit_insight` tool). `condense=True` (default) → `raw` body (decompose); `condense=False` → `content` body (verbatim ≤200-char claim). Returns the per-claim `IngestResult`. The endpoint returns 201 on store, **200 with `ok:false`** when every claim is rejected (a normal advisory outcome — NOT an error, so it doesn't raise `RestError`).
- `ask_wiki` — `POST /v1/wiki/qa` (SSE-only): stream the start frames to read the `meta` `answerId`, then `bounded_poll` `GET /v1/wiki/qa/<id>` until the snapshot `status` is terminal (`complete`/`cancelled`/`error`; sources-block fallback only when status-less). Returns `{answer_id, answer, citations, status}`; `status:"running"` means the await budget elapsed — resume via `get_wiki_answer(answer_id)`. `model` is optional (server defaults it).
- `get_wiki_answer(answer_id, detail?)` — `GET /v1/wiki/qa/<id>`; the companion that consumes the `answer_id` (mirrors `get_search_run`).

**D. Integrations / capability discovery**
- `list_integrations` — `GET /api/tools` + `GET /api/plugins`, projected to `{tool_id, name, kind?, enabled?}` (boilerplate descriptions dropped), so a caller knows what tool ids to pass to `create_session`'s `integrations` argument.
- `list_projects` — `GET /api/projects`: registered config + managed projects with `name`/`project_id` + canonical git `repo`/`aliases`. The discovery surface for `create_session`'s `project`.

**E. Mewbo Search — multi-source workspace search** *(group `search` — WITHHELD by default; see "Tool exposure")*
- `list_search_workspaces` — `GET /api/agentic_search/workspaces`, projected compact (`id/name/desc/sources/recent_query_count`). Optional `query` forwards as `?q=` (the API's case-insensitive substring filter over name/description/past-query text). **Drops `instructions` and the full `past_queries`** — `instructions` is untrusted prompt input and must not leak to a consuming agent; the history is console state.
- `search(query, workspace, project?, detail?)` — resolves `workspace` (id OR case-insensitive name) → `POST /api/agentic_search/runs` → awaits a terminal run, then projects the `RunPayload`.
- `get_search_run(run_id, detail?)` — `GET /api/agentic_search/runs/<id>`; same projection (replay / deep-link, or re-read an async run that returned `running`).

Non-obvious decisions for this group:

- **Await mirrors `WikiTools.ask`, forward-compatible with the async runner.** The default `EchoSearchRunner` finishes synchronously so `POST /runs` is already terminal and `search` returns instantly with zero polling. The real `OrchestratedSearchRunner` returns a `running` snapshot immediately; `search` then polls `GET /runs/<id>` until a terminal status (`completed`/`failed`/`cancelled`) or `SearchTools.timeout_s`, returning the partial with `status:"running"` rather than hanging. **Don't add an SSE consumer here** — the snapshot poll is the canonical bounded await; the SSE stream is the console's live-reveal transport.
- **Two tiers, not four.** `detail="answer"` (default, cheapest) returns the cited synthesis + a compact result index (`id/source/kind/title/url/relevance`) so citations resolve; `detail="full"` adds `snippet/insight/refs`. Runs are shallow (unlike sessions, which justify `get_session_history`'s four tiers), so two suffice.
- **The projection always drops the per-source trace + decorative fields** (`related_people`, `image`, `embed`). Those are console-render signal, not search signal — an external agent never needs them. Read the run's SSE stream directly if you ever do.
- **`SearchTools.TERMINAL_STATUSES` is duplicated, not imported.** Same HTTP-boundary rule as everything else: this process talks to the API over REST only and never imports `mewbo_api`. The `ClassVar` is a small mirror of `agentic_search.schemas.TERMINAL_RUN_STATUSES`; if the contract's terminal set changes, update the mirror.
- **Workspace resolution is by id-or-name** (agents think in names, as with `create_session`'s repo resolution). An ambiguous name or no-match raises `ValueError` with the candidates rather than silently searching the wrong workspace. Workspace authoring (create/edit) is deliberately NOT exposed — that's the console's surface; the MCP group is read+search only.

**F. Structured query — schema-constrained synthesis** *(group `structured` — WITHHELD by default: it routes graph-first through a mapped Search workspace)*
- `structured_query(query, schema, workspace?, tool_ids?)` — `POST /v1/structured`
  (async run-handle). The server runs an agentic session (model may call grounding
  tools) and emits a JSON-Schema-validated object. A `workspace` that is a mapped
  Mewbo Search workspace goes GRAPH-FIRST: route→probe→aggregate→emit, and
  the result carries additive `provenance` (recipes routed, probes run). Returns
  `{run_id, status, output?, provenance?}`; `bounded_poll`s `GET /v1/structured/<run_id>`
  for a slow run, else resume via `get_structured_run(run_id)`. Atomic class
  `StructuredQueryTools(RestClient)`; the MCP-tool param is `tool_ids` (not
  `tools`) to avoid shadowing the `tools` module — forwarded as the body's `tools`.
  `_shape` passes `provenance` through verbatim alongside `output`/`workspace`/`error`.
- `get_structured_run(run_id)` — `GET /v1/structured/<run_id>`; resume/replay a run.

**H. Triggers — external manage/observe surface**
- `list_triggers(session_id?, kind?, status?, limit?)` — `GET /api/triggers`, the server's own filters forwarded verbatim; compact rows (`id/session_id/kind/status/wake_prompt/action/fires/max_fires/next_fire_at/expires_at/created_at`, plus `last_error` when set). `args`/`provenance`/`created_by` are dropped from the projection — a caller managing triggers already knows the kind and armed it (or is just watching status).
- `cancel_trigger(trigger_id)` — `DELETE /api/triggers/<id>`; idempotent, same contract as `terminate_session` (a repeat call on an already-terminal trigger reports its current status, never errors).
- **Deliberately NO create/arm tool.** Arming is an agent-facing, IN-SESSION capability — the `schedule_trigger` SessionTool (`mewbo_core/triggers/session_tool.py`) an agent calls on *itself* mid-turn to end its turn and be woken later — never a general external mutation surface. `TriggerTools` is the read/manage counterpart an external caller (or the console's trigger dashboard) uses on triggers armed elsewhere.

## `timeline.py` — DRY pairing with the console

`timeline.py` is a Python port of `buildTimeline` / `computeTurnTokenUsage` from `apps/mewbo_console/src/utils/timeline.ts`. The two files are intentionally kept in sync — the console renders the same turns visually that MCP exposes to callers. Parity is enforced by `apps/mewbo_mcp/tests/test_timeline.py` against shared fixtures. **When you change turn-boundary or token-usage logic in either file, update both and the parity test.**

Turn boundary rule (matches TS): a `user` event opens a turn; the next `assistant` event closes it. A `completion` event defensively closes an open turn if no `assistant` event precedes it. `tool_result` events inside a turn are steps. Token totals: PEAK root input (context pressure), SUM output (additive), SUM sub-agent peaks.

**`done_reason` resolves from THREE sources, and needs all three.** The canonical `TurnMeta` carries a reason only when a `completion` closed the turn ITSELF — the rare shape, since an ordinary run writes its final `assistant` event first and the completion then lands on an already-closed turn, where the assembler keeps the reason only if it classifies as a failure. Reading that one field is why this tier reported `null` for every turn of sessions the `overview` tier correctly called failed. So: the turn metadata, then the failure record (`run_failure.reason`, also the source of the row's `error`, and read BEFORE the turn-metadata gate because a failure beside real prose carries no turn metadata at all), then `_completion_outcomes` — a pass over the raw events pairing each `completion` with the most recently opened turn, which is the only source covering successful turns and the halted/verification/budget outcomes the assembler does not model as failures. This is a projection concern, not a boundary rule: attribution reuses the one-turn-per-prompt rule this module already stands on. **It is deliberately RICHER than the console's `buildTimeline`**, which still renders `done_reason` off the turn metadata alone — the divergence is in outcome attribution, not turn boundaries or token math, so the parity rule below still governs those.

**Token totals are TWO quantities, both reported.** `total_input_tokens` sums per-turn PEAKS and measures context pressure; `total_billed_input_tokens` sums EVERY call's input and is what the per-call event log adds up to. On a tool-heavy session the second is several times the first — the whole prompt is resent each step — so reporting only the peak under a "total" name reads as a metering bug against Mongo's `llm_call_end` events. Neither number is wrong; the pair is what makes them legible.

**A BLOCKED run must not read as `done_reason: "completed"`.** The loop leaves `done_reason` at `"completed"` (or `"error"`) for a run that hit a user-actionable wall and carries the real fact in the completion payload's `blocked_code` (`repo_access`/`network`/`forbidden`/`quota_exceeded`). An external agent reading the turns/full tier and trusting `done_reason` alone would treat that as a success and act on it — worse than a human misreading a badge. So `_completion_outcomes` reads `blocked_code` off each completion, validated against core's `_BLOCKED_CODES` (imported, never re-listed — a second copy is the cross-surface drift this closes), and `build_timeline` OVERRIDES the turn's `done_reason` to `"blocked"` and stamps `Turn.blocked_code`. This is the ONE derivation MCP does; the four honest-outcome facets themselves are derived once in core's `summarize_session`. `list_sessions` forwards all four off the row (pure projection). The `overview` tier forwards `recoverable` off the `/events` meta and reconstructs `blocked_code` from the transcript (the authoritative-or-reconstructed idiom this tier already uses for `status`/`terminated`); `failure_reason`/`models_tried` ride the meta once `GET /events` forwards them from the summary it already computes (append-when-present, so they light up with no MCP change). Like the token divergence, MCP is deliberately RICHER here than the console — a projection concern, not a turn-boundary one, so the parity rule still governs boundaries and token math.

**UNMET_GOAL rides a SEPARATE `outcome_assertion` event, not the completion.** The largest unmet-goal case — a wiki index that ends CLEAN (`done_reason: "completed"`, no halt, no blocked envelope) yet never completed its job — the loop cannot see, because every signal it owns says success. A session-end hook appends an `outcome_assertion` event (`{reason, detail, source}`) AFTER the completion to contradict it, and core's `summarize_session` promotes `status` `"completed"`→`"unmet_goal"` by reading it. The turns/full tiers reconstruct from the transcript, so `_completion_outcomes` ALSO folds the assertion (attached to the turn whose completion it follows — a stray one before the terminal is ignored, mirroring core resetting its pending assertion on each completion; validated through core's own `OutcomeAssertion` model, so a malformed record is dropped). `build_timeline` promotes ONLY a `"completed"` turn (a failed/halted one is already honest — core's `status == "completed"` gate) and blocked (checked first) OUTRANKS it, exactly as in core. `done_reason` becomes `"unmet_goal"`, `Turn.unmet_goal_reason` carries the product token, and the assertion detail rides `error`. The `overview` tier needs no change here — it reads the authoritative `status` (already `"unmet_goal"`) off the `/events` meta.

**`extract_trigger_events` — a SEPARATE pass, not folded into `build_timeline`.** Ports the console's `trigger`/`session_terminated` `TimelineEntry` roles: `trigger_armed`/`trigger_fired`/`session_terminated` events are recognized REGARDLESS of turn-open state (the console parses them BEFORE the open-turn gate, so a marker arriving between turns — the common case for `trigger_fired`, which wakes a session BEFORE the next `user` event opens a turn — is never silently dropped). Kept as its own function (rather than added to `build_timeline`'s loop) so turn reconstruction and its existing test coverage stay untouched; it duplicates only the minimal open/close bookkeeping needed to stamp a `TriggerMarker.turn_index`. `SessionTools.history()` wires it in two places: the `full` tier surfaces a turn's trigger markers (`out["triggers"]`, omitted when empty — same idiom as `attachments`), and the `overview` tier's `terminated`/`terminated_at` prefers the API's authoritative `/events` meta but FALLS BACK to the transcript's `session_terminated` marker — a defensive read that works even before that field is wired API-side (mirrors the existing `status`/`title` authoritative-or-reconstructed idiom in `_overview`).

**`extract_question_events` (ask-user questions) — another SEPARATE pass.** Ports the console's `question` `TimelineEntry` role: `user_question` opens a pending `QuestionMarker` keyed by `call_id` and `user_question_answered` settles it in place (status + answers + answered_via) — the same pending→settled fold the console applies via `plan_proposed → plan_approved`. Two deliberate departures from the trigger pass, both TS-faithful: (1) a `user_question` with NO open turn is DROPPED (the console folds it AFTER the open-turn gate, like plan/widget — a question is only ever asked mid-run, so `turn_index` is always a real int, never `None` like a `trigger_fired` orphan); (2) the marker OMITS the console's `call_token` — that is the single-use bearer secret the answering surface POSTs, and this read-only projection must never hand an external agent the answer credential (same "don't leak the sensitive field" rule the search group applies to workspace `instructions`). Not wired into `SessionTools.history()` today — added for TS parity + the shared fixture test; surface it there if an external reader ever needs to see a session's open questions.

## Env vars

| Variable | Default | Notes |
|---|---|---|
| `MEWBO_API_URL` | `http://localhost:5124` | REST API base URL. Default targets local dev (`mewbo-api` binds `:5124`). In Docker (compose uses `network_mode: host`): `http://localhost:5125` (gunicorn port) via the compose override. |
| `MEWBO_MCP_HOST` | `127.0.0.1` | Bind host. Set to `0.0.0.0` in Docker. |
| `MEWBO_MCP_PORT` | `5127` | Bind port. Deliberately not `5125` — that is the API's gunicorn port in Docker (`API_PORT=5125`), so sharing it would clash. |
| `MEWBO_MCP_EXPOSED_GROUPS` | *(unset ⇒ `sessions,wiki`)* | Comma-separated ALLOWLIST of `ToolGroup` names — see "Tool exposure". Fail-closed: `search`/`structured`/`integrations`/`triggers` are withheld unless named; an unknown name fails startup. |
| `MEWBO_MCP_EXPOSED_TIERS` | *(unset ⇒ `read,navigate,ask,drive`)* | Comma-separated ALLOWLIST of `EffectTier` names — the second, orthogonal exposure axis. `read,navigate` yields a zero-model-call facade. Unknown name fails startup. |
| `MASTER_API_TOKEN` | `msk-strong-password` | Break-glass token; must match the API's value. Set in `docker.env`. |
| `MEWBO_HOME` | `~/.mewbo` | Data dir for file-driver KeyStore (`api_keys.json`). Must match the API. |
| `MEWBO_MONGODB_URI` | *(unset)* | When set, Mongo KeyStore driver is selected — must point to the same DB as the API. |

## Run

```bash
uv run mewbo-mcp
```

Entry point: `apps/mewbo_mcp/src/mewbo_mcp/server.py:main`. Calls `build_server()` → `server.run(transport="streamable-http")`. MCP endpoint: `http://<host>:<port>/mcp`.
