> ↑ [apps/mewbo_api/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md) · children: [scg/](scg/CLAUDE.md)

# Agentic Search ("Mewbo Search") — API Subsystem Guidance

Scope: this file applies to `apps/mewbo_api/src/mewbo_api/agentic_search/`.
It captures the non-obvious engineering decisions in the multi-source
workspace search backend — the integration seam two teams build against.
Everything readable straight from the code is left out.

## What this is

A multi-source search surface: a *workspace* selects connectors
("sources"), a *run* fans an agent out across them, and the console
renders streamed results + a cited synthesis. The package owns the wire
**contracts** and the **persistence**; the orchestration team owns the
real fan-out behind a single seam (`runner.py`). The default runner is a
fixtures replay so the whole console↔API loop works with zero LLM.

## Run lifecycle — the event log IS the search-event stream

`POST /runs` keeps the **back-compat envelope**: it creates the run and
returns `{run: RunPayload}` plus top-level `run_id` / `session_id` /
`status`. Don't "fix" the duplicated fields — the console reads either
shape. The echo runner completes inline (`status="completed"`); the
orchestrated runner launches the session on the runtime's managed worker
(`runtime.start_command`) and returns promptly with `status="running"` —
the event log / snapshot carry the run to terminal.

The run's append-only, idx-keyed event log (in the store) IS the
normalized search-event stream. Three transports project the same write:

| Surface | Endpoint | Role |
|---|---|---|
| Durable snapshot | `GET /runs/{id}` | reload / share / deep-link; returns the `RunRecord` + accumulated `payload` |
| Live projection / replay | `GET /runs/{id}/events` (SSE) | replays the log from idx 0, then tails until a terminal event |
| History | `GET /workspaces/{id}/runs` | newest-first run records for a workspace |

A synchronous runner (echo) appends a terminal event (`run_done` /
`error`) before returning. The async orchestrated runner returns a
`running` snapshot and its worker keeps appending as the backing session
progresses, settling the terminal event + snapshot when it ends — the
SSE generator tails either case identically. **Do not** add a second
status channel; keep the event log authoritative.

## The shareable deep-link contract — `GET /runs/{id}` is self-sufficient

`/search?ws=<workspace_id>&run=<run_id>` is a deterministic, multi-user
shareable URL: a cold browser opens it with **one `GET /runs/{id}`
(snapshot) + an SSE attach — never a POST**. Three guarantees make that
work; they are load-bearing, locked by `test_agentic_search_runs_routes.py`,
and may only be extended **additively**:

1. **Snapshot self-sufficiency.** `GET /runs/{id}` returns `{run:
   RunRecord}` with everything needed to render with no other context:
   top-level `run_id`, `session_id`, `workspace_id`, `query`, `tier`,
   `status`, `created_at`, and the `payload` (`RunPayload` — the
   result/answer block, itself carrying `workspace_id`/`query`/`tier`/
   `session_id`). The console reads these **top-level** — never move them
   under `payload`. `session_id` links the URL-addressed run to its
   auditable session (#74).
2. **Cold-store durability.** The snapshot is persisted through the run
   store (`create_run` + the terminal `update_run(..., payload=…)` BOTH
   runners write), file/Mongo-backed — it survives an api restart / a
   second worker. A shared URL must never 404 after a deploy. The read
   (`SearchRun.get` → `store.get_run`) has **no per-session/per-user
   scoping**: any valid API-key holder resolves the same run by id.
3. **Clean 404 envelope.** An unknown run id is `{"message": "run not
   found"}, 404` (a structured JSON body, never a raw 500 / Werkzeug HTML
   page); the SSE + cancel routes 404 the same way before opening a stream.

**The standalone MCP server (`apps/mewbo_mcp`) is a second consumer of
these endpoints.** Its `search` / `get_search_run` / `list_search_workspaces`
tools wrap `POST /runs` + the snapshot **poll** path (`GET /runs/{id}`),
NOT the SSE stream — the bounded poll is the canonical await for a
non-streaming caller, and it's already forward-compatible with the async
runner. The SSE stream stays the console's live-reveal transport. So the
`POST /runs` back-compat envelope (`{run, run_id, session_id, status}`) and
the `RunRecord.status` on the snapshot are load-bearing for the MCP await
loop — don't drop the top-level `status` fields.

## Storage is SEPARATE from session transcripts (hard requirement)

The run store (`agentic_search_runs` + `_run_events` + `_workspaces`) is
its own namespace, deliberately decoupled from session transcripts. A run
is *backed by* a session but its normalized projection lives here so
snapshot reads stay fast and survive session GC. JSON layout under
`<cache_dir>/agentic_search/` (`workspaces/<id>.json`,
`runs/<id>/run.json`, `runs/<id>/events.jsonl`); Mongo mirrors it in the
`agentic_search_*` collections. Never route run state through the
transcript event log.

## The seam — `SearchRunner` (runner.py)

`SearchRunner` (a `Protocol`) is the swap point. Two implementations:

- **`EchoSearchRunner`** (default, dev) — replays the prototype fixtures
  over the REAL event log + store with NO LLM. It filters canned
  results/trace/answer to the workspace's enabled sources, emits the full
  normalized event sequence (incl. the `answer_delta*` typewriter), and
  persists the terminal snapshot. This is what makes console↔API
  integration work end-to-end *before* the real fan-out exists.
- **`OrchestratedSearchRunner`** — starts a tool-scoped `SessionRuntime`
  session and translates session-transcript events into the event
  protocol using the `events.py` builders.

The active runner is resolved **per run** by `get_search_runner()`
(orchestrated iff `scg.enabled` AND ≥1 mapped source in the SCG store, else
echo) — never frozen at startup, so mapping the first source flips a live
process out of echo mode with no restart. `set_search_runner()` remains the
explicit-override seam (tests / manual swap; `None` restores resolution) and
always wins.

We **deliberately did NOT ship a speculative `SearchEventAdapter` ABC**
(upholds "no speculative abstractions"). Transcript→event normalization
is the real runner's internal concern; the `events.py` builders are the
only required shared surface. The routes call `get_search_runner()` and
stay agnostic to which strategy is wired.

## `schemas.py` is the single source of truth for the wire

Every other module references it; the orchestration team implements
*against* it. It holds the entity/wire models, `RunRecord` (durable), and
`SEARCH_EVENT_TYPES` (the canonical event vocabulary the console's
reducer switches on). Rules:

- `OUTPUT_CONTRACT_VERSION` is stamped on every `RunRecord` and emitted in
  `run_started`. Bump it only on an **incompatible** wire change so the
  console can guard.
- Wire models are `extra="forbid"`; `clean_for_model()` whitelists
  Mongo/event-log bookkeeping keys (`_id`, `idx`, `event_count`) on load
  so loads stay lenient while models stay strict (mirrors the wiki store).
- Synthesis streams as `answer_delta*` then a final `answer_ready` — the
  typewriter protocol. `agent_*` events drive the per-source trace;
  `result` events drive arrival order. The prototype's `finish_delay_ms`
  / `t_ms` are deprecated decorative fields — real ordering comes from
  event arrival, never a client timer.
- Orchestrated runs now populate ALL of it (#95): `agent_*` includes a
  synthetic **coordinator lane** for root-inlined tool activity, `result`
  events come from the agent's `scg_results` emission, and
  `total_ms`/`confidence`/`sources_count` are computed, never defaulted —
  see `scg/CLAUDE.md` → "Coordinator lane + result cards + honest settle".
- **Instrument-fidelity additions (run-797097e4b1, all additive — NO
  `OUTPUT_CONTRACT_VERSION` bump):**
  - `SearchResult.meta: dict[str, scalar] | None` — agent-emitted structured
    facts (stars/language/version/state/size…) ride verbatim, **SCALARS only** (the
    projection drops a non-scalar value silently so a connector blob can't
    leak). This is the SINGLE card-metadata channel — the "card_meta footer"
    (#111) is `meta` rendered as a structured footer by the console's open-vocab
    `resultMeta` classifier, NOT a second wire field (DRY); the LLM proposes the
    keys, the console renders any of them. `TraceAgent` gains `kind`/`model`/`steps`/`duration_ms`/
    `input_tokens`/`output_tokens`/`results_count` (+ `returned_count`, the raw
    pre-dedup emit; `returned − results_count` = the lane's "N filtered", #111);
    `RunPayload.stats:
    RunStatsWire | None` (probes·tool_calls·tokens·setup_ms·search_ms — populated
    at settle, **None when underivable; NEVER a fabricated 0**);
    `RunPayload.related_questions` is populated by a PARALLEL structured call at
    settle (`RelatedQuestionsRunner`, + a dedicated `related_questions` SSE event),
    NOT the `scg_results` emit (now the fallback) — see scg/CLAUDE.md.
  - **Result dedup is cross-emitter + semantic** (`ResultsProjection.dedup_keys`):
    a card registers BOTH a normalized-url key (scheme/trailing-slash stripped,
    host lowercased) AND a `(title, source)` key, so the root's url-less re-emit
    of a probe's hit collapses into it — first emission wins (5-cards-for-3 fix).
    Holds live (`_emit_results`) AND at settle (`_build_results`); seen-state is
    per-run on the streamer/runner.
  - **Lane labels are the agent KIND, not the model.** `TraceAgent.name`/`kind`
    read `sub_agent.agent_type` (Lane A), falling back to the literal
    `scg-path-probe` — NEVER the model (which is its own `model` field). The
    `start`-brief opener prefers a `SUB-QUERY:`/`PATHWAY:` line, skipping the
    leaf-executor system-prompt boilerplate.
  - **Probe lanes get tool-call digests.** A probe's non-`scg_results` tool call
    projects ONE secret-free digest line (tool_id + capped input hint, never the
    result payload) onto its lane; its `scg_results` emit projects its cards AND
    credits `results_count`. The coordinator lane is ALSO appended to
    `payload.trace` at settle (kind `coordinator`, blank `result` — the synthesis
    is the answer, never duplicated) so a zero-probe run stops showing `trace:[]`.

## `store.py` is the substitution boundary

Dual-backend JSON/Mongo, mirroring `project_store` / `session_store`.

- `save_workspace` is the verbatim write primitive that both `create` and
  `seed` share — one persist path, so seed ids stay stable.
- Demo-workspace seeding is gated by `MEWBO_AGENTIC_SEARCH_SEED` (default
  on) and only fires when the store is empty. Set it to `0` for a
  production start-empty.
- `reset_for_tests()` swaps a fresh seeded JSON store under a tempdir for
  isolation while still exercising the JSON backend through the routes.
- Mongo idx is atomic (`$inc` on `event_count` via `find_one_and_update`);
  the JSON backend counts non-blank lines under a lock. Keep idx
  monotonic per run — the SSE `id:` line and replay-from-idx depend on it.
- `past_queries` is bounded at `PAST_QUERY_CAP`; a `running` entry is
  written up-front and patched in place on completion.
- `GET /workspaces?q=` is `search_workspaces` — ONE concrete method on the
  base class (load-and-filter over `list_workspaces`, case-insensitive
  substring across name/description/past-query text), inherited by both
  backends like `cancel_run`. Don't add per-backend overrides.

## `SourceCatalog` (catalog.py) — source→`allowed_tools` scoping

**`entries()` is live-first.** The catalog lists the **configured MCP
servers** (id = server name, `source_type="mcp_tool_list"`) read from the
merged `configs/mcp.json` chain + the tool registry; the demo fixtures merge
*after* them **only while demo seeding is on** (`store.seeding_enabled()`,
the one gate shared with demo-workspace seeding — a live server id wins a
fixture-id collision). A production install (`MEWBO_AGENTIC_SEARCH_SEED=0`)
lists exactly what is configured. A configured server whose discovery failed
stays listed `available=False` with the manifest's `disabled_reason` as
`unavailable_reason` — greyed out, never omitted.

`SourceCatalog.tools_for(source_ids, project)` is the rule a run applies
to scope `allowed_tools` (selected sources → de-duplicated union of tool
ids). Resolution order per source: **live SCG capability nodes**
(`kind == "capability"`) → the live server's registry `mcp_<server>_*` ids →
the illustrative `tools` declared *beside the source* in
`fixtures.SOURCE_CATALOG` (seeding on only — never a `TOOL_MAP` constant in the
resolver). The union is then intersected with the live registry via
`filter_specs()`. The wire shape (`SourceCatalogEntry`) and the `tools_for`
contract are fixed; only the resolution body changes.

**GRANT INVERSION fix (run-797097e4b1) — capability nodes carry the RAW MCP tool
name, not the registry id.** `_scg_tool_ids` mints `mcp_tool_id(source_id,
node.name)` (the core id convention, the SAME translation `plugins/scg/route.py`
ships for probe spawns), preferring the raw name only when IT is the live id (a
node already holding a built-in / full id — the fixture shape). Returning the
raw name made the `filter_specs` ∩ DELETE every successfully-mapped source's
tools while a failed-map source fell through to the live branch and bound ALL
~51 raw registry tools (incl. `create_repo` / `delete_branch` / `wiki_write`).
**SEARCH grants are READ-ONLY:** `tools_for` filters the union through
`_is_write_tool` (a pure name-verb check — leading token vs the broad write set,
trailing token vs a STRICT suffix set so `get_latest_release` survives), dropping
the obvious mutators. The traversal verbs a graph drive also needs are unioned
later by `WorkspaceGraphBinding`, never here. **`RunRecord.allowed_tools` records
the BINDING's actual grant** (`orchestrated_runner` persists
`binding.allowed_tools()` at seed) — the audit field no longer diverges from what
`run_sync` drove with.

**Map descriptors auto-build at the route.** `POST /sources/<id>/map` without
a `descriptor` for an `mcp_tool_list` source builds one via
`scg/descriptors.py:SourceDescriptorBuilder` — the connector's live MCP tool
list (name/description/inputSchema) through the `mewbo_tools` pool, composed
**in the app** because `mewbo_graph` may never import `mewbo_tools`. Schema
only, never credentials. No configured connector + no descriptor → 422; other
source types keep the mapper's fetch-natively contract (descriptor stays
`None`).

**Virtual MCP config + workspace scope (#75, shipped).** A workspace = name +
instructions + a selection of MCP servers. That selection persists as a DB-backed
*virtual MCP config* — `WorkspaceMcpConfig` (`mcp_config.py`), an exact
`CredentialStore` sibling: one `_encode`/`_decode` seam, stored in the
agentic_search store namespace (`save/get/delete_workspace_mcp_config`, JSON
mode-0600 file / `agentic_search_workspace_mcp_configs` Mongo collection). It is
**the source of truth for what a run may reach** — `McpServerDef.headers`/`env`
are the only secret-bearing fields and are ALWAYS redacted outward
(`redacted()` masks values, keeps key shape; `auth_scope()` names which auth a
server carries — the `ScgNode.auth_scope` stance). `SearchRun.start` resolves the
run grant from `WorkspaceMcpConfig.attached_server_names` first, falling back to
the workspace's raw `sources` when no config is persisted (current global
behavior). `WorkspaceSourceSync.on_workspace_saved` (`source_sync.py`) is the
POST/PATCH hook: it refreshes the virtual config, then auto-maps newly-enabled
**live** sources (idempotent — skips already-mapped/in-flight; a terminal/failed
job does NOT block a re-map, so a previously-unreachable source re-maps once its
URL is fixed). **The hook is register-and-return (#97):** only step 1 (virtual
config refresh + NL fingerprint) runs on the request thread; the whole auto-map
fan-out (mappable/drifted/re-enrich resolution + live descriptor builds +
`MapSourceJob.start`) runs on one named daemon thread
(`workspace-automap-<id>`) — the descriptor build is a live MCP handshake per
source, and doing it in-request blocked "Create Workspace" for N handshakes
(the user read it as browser-dependent indexing). The method returns the
`Thread | None` so tests join deterministically; routes ignore it. Never move
the fan-out back in-request, and never let the thread body raise. It ALSO re-maps already-mapped enabled sources whose live tool
list drifted from the stamped `ManifestHash` (#81-C), and carries the workspace
`instructions`/`desc` as untrusted `nl_context` to seed the map-time enrich step
(#81-B — see scg/CLAUDE.md). **Workspace editing IS a graph-lifecycle event
(#83):** an instructions/desc edit moves no source + drifts no tool list, so the
old gates missed it — `NlContextFingerprint` (a `ManifestHash` sibling over the
prose) stamped on `WorkspaceMcpConfigRecord.nl_fingerprint` (the honest internal
home, NOT the wire `Workspace`) gates an idempotent re-enrich of enabled+mapped
sources via `_start_map`; the PATCH route fires the hook on a sources OR prose
change (an instructions-only body has no `sources` key). The per-workspace graph
is a **scoped VIEW** — removing a source narrows it without a delete (`ScgScope`
derives from `workspace.sources` per run); see scg/CLAUDE.md ("Workspace scope").

## Security invariants (real runner must uphold)

- Workspace `instructions` are **UNTRUSTED prompt input**. The real runner
  must keep them separate from system/developer instructions — never
  concatenate them into the system prompt.
- `allowed_tools` = selected sources ∩ project policy. The catalog union
  is the upper bound, not the final grant.
- Never persist secrets (tokens, credentials) in workspace or run records;
  both are JSON/Mongo-visible like the wiki submission.

## SSE plumbing (events.py)

Inherited verbatim from `wiki/events.py`: a 2 KB primer frame at byte 0 +
padded heartbeats defeat proxy buffering (OpenResty/NPM buffer ~4 KB).
**Don't shrink them** — that reintroduces the buffer bug. Frame format is
`id: <idx>\nevent: <type>\ndata: <json>\n\n`; the `id:` line carries the
event idx so a flaky proxy can resume via `Last-Event-ID` / `?after_idx=`.
The generator polls `load_run_events(after_idx=...)` until a terminal
event or the idle threshold (env-tunable: `MEWBO_AGENTIC_SSE_MAX_IDLE`,
`MEWBO_AGENTIC_SSE_SLEEP`).

SSE auth uses the `?api_key=` query param — `EventSource`/`fetch` can't set
headers everywhere, and `_require_api_key` already honours the param.

## The real runner is the SCG — a cheap router (now built — see scg/CLAUDE.md)

`OrchestratedSearchRunner` (in `scg/orchestrated_runner.py`) is the real
`SearchRunner`: **not** "spawn one agent per enabled source." It drives a
`scg-search` session over the **Source Capability Graph (SCG)** — a cheap
routing controller that indexes *reachability* (schemas + pathways, **never
data**) and lets the agent fan probes out along *qualified pathways*. The graph
ops (route / parse / ER) are tools the agent drives, never a parallel control
loop; tiers are one decomposition+probe budget knob over the single
`ToolUseLoop`; the connector's real return is the only verifier.

It is chosen **per run** (`scg.enabled` AND ≥1 mapped source — see the seam
section above); the tier rides `RunRecord.tier` (`POST /runs` body `tier`:
`fast|auto|deep`, default `scg` config `default_tier`, echoed on `RunPayload`),
never the runner instance. `RunRecord.model` rides the same way: the optional
`POST /runs` body `model` (a LiteLLM name; non-string/blank → ignored, never a
400 — the `/v1/structured` stance) wins over the tier's configured model at the
drive (`run.model or ScgConfig.model_for_tier(run.tier)`) and is echoed on
every `RunPayload` so the deep-link snapshot stays self-sufficient. Per-tier
defaults are config (`scg.traversal.tier_models`); the override is per-run
only — no config write, no restart. `GET /tiers` exposes the resolved per-tier
preset (tier map → `llm.default_model`, exactly the drive's fallback; pure
config read, NOT gated on `scg.enabled`) so the console's composer can show
which model a tier runs before submit. The durable decisions + the two silent correctness
traps live in **`scg/CLAUDE.md`**; the full spec + research grounding is
**Gitea #19**.

The SCG *engine* itself (router / parser / entity-resolution / store / memory
bridge) lives **down** in the optional `mewbo_graph.scg` library (Gitea #25);
this app holds only the runner seam + the map-job lifecycle glue and composes
the engine via the `wiki` extra. See `packages/mewbo_graph/CLAUDE.md`.

**Workspace binding ⇒ graph access (#77, LANDED):** `WorkspaceGraphBinding`
(`scg/workspace_binding.py`) is the ONE seam — any workspace-bound run gets the
`scg` capability + graph tools (`scg_route`/`scg_observe`/`scg_memory` + fan-out)
+ the `ScgScope` source scope. A `/v1/structured` run on a mapped workspace goes
graph-first (`scg/graph_structured_runner.py` → `StructuredResponder` +
`scg-search-structured` playbook → schema-validated emit). Search runs stream
LIVE via `scg/run_streamer.py` (core `SessionEventBus`). See scg/CLAUDE.md "#77 seams".

## Testing notes

- Use `store.reset_for_tests()` for isolation; mock at the runner seam, not
  inside it. The default `EchoSearchRunner` already exercises the full
  event-log → SSE replay path without an LLM.
- Tune the SSE idle/sleep env vars down in tests so the generator closes
  promptly after the terminal event.
- Agentic-search tests should NEVER spawn a real LLM or hit a real proxy.
