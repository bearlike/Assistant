> ↑ [agentic_search/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_api/CLAUDE.md](../../../../CLAUDE.md) · [root](../../../../../../CLAUDE.md)

# Source Capability Graph (SCG) — API Subsystem Guidance

Scope: `apps/mewbo_api/src/mewbo_api/agentic_search/scg/` — the run/map-job
**lifecycle glue** (`config`, `map_job`, `orchestrated_runner`, `map_progress`,
`descriptors`, `playbooks`).
The deterministic SCG **engine** (`types`, `store`, `providers`, `parser`,
`router`, `entity_resolution`, `memory_bridge`) moved **down** to
`mewbo_graph.scg` and the SessionTools to `mewbo_graph.plugins.scg` (Gitea #25);
see `packages/mewbo_graph/CLAUDE.md`. This file stays the canonical home for the
SCG architecture decisions below — they hold wherever the code sits. The full
spec + phased build + ~40-citation research grounding live in **Gitea #19**
(`bearlike/Assistant#19`, "Implementation Plan v2 — lean & converged"); only the
non-obvious decisions a future implementer must not relitigate are here.

## Architecture — graph routes, agents search, memory compounds

The SCG is a **cheap routing controller, not a proof-search engine.** It indexes
*reachability* (schemas + qualified pathways), **NEVER the data behind them.**

**Golden invariant — no parallel control loop.** The one and only engine is the
existing `ToolUseLoop` the `scg-search` AgentDef already runs. The deterministic
graph ops (router, parser, entity resolver) are **tools** that agent drives —
they are NOT a second control loop. `OrchestratedSearchRunner` adds no loop; it
projects a finished session transcript onto the run event log.

**Tiers (Fast / Auto / Deep) are ONE budget knob over the single loop** —
decomposition depth + probe-count fan-out (see `scg-search.md`) **and, since
2026-06, the MODEL**: `ScgConfig.model_for_tier(run.tier)` reads
`scg.traversal.tier_models` (defaults fast→`openai/gpt-5.4-nano`,
auto→`openai/claude-sonnet-4-6`, deep→`openai/gpt-5.5`) into the drive's
`model_name`; probes inherit the session model, so the one knob moves the
whole run. Blank/unknown → `llm.default_model`; an explicit request `model`
(`POST /runs` body, riding `RunRecord.model`) wins over the tier map at the
drive: `run.model or ScgConfig.model_for_tier(run.tier)`. They are NOT
verification rounds; there are no verification rounds. **The connector's real
return is the only verifier.** Cut as over-engineering — do NOT reintroduce:
explicit A\* `f=g+h` frontier, multi-path self-consistency / majority-vote
verification, process-reward heuristic, MCTS, trained PRM / value-fn, RL search.

**The search-run terminal is NL** — `AnswerSynthesis`. The **structured
graph-first** terminal (#77, LANDED) reuses `EmitStructuredResponseTool`
semantics incl. the `should_terminate_run()` override (WikiFinalize/#58: a
terminal emit tool ends the loop itself).

## #77 seams (LANDED) — binding · streaming · graph-first structured

- **`WorkspaceGraphBinding`** (`workspace_binding.py`) = THE one seam any
  workspace-bound run crosses → {capability+quarantined-instruction context
  events, connector grant ∪ `TRAVERSAL_TOOLS` (incl. `scg_observe`),
  `ScgScope.use(sources, workspace=id)` cm}. Search runner AND structured
  graph-first consume it; never re-assemble inline.
- **Live streaming** (`run_streamer.py:RunEventStreamer`): subscribe the backing
  session's core `SessionEventBus` (SideStage seam) BEFORE the drive; a daemon
  consumer projects `sub_agent`→`agent_*` AS published. `_settle` is RECONCILE-
  only (`reconcile_missing`, no double-emit). `ProbeTrace` = ONE projection (live
  + settle agree); the `start` brief's first line is the lane label.
- **Graph-first structured** (`graph_structured_runner.py`): `/v1/structured`
  with a mapped search `workspace` drives `StructuredResponder` (graph-free core
  + injected `capabilities`/`context_events`/`extra_instructions`/`scope_factory`)
  + the `scg-search-structured` playbook → schema-validated `emit_result`; streams
  via the bus natively. GET carries additive `RunProvenance` (recipes/probes).
- **Provenance facets**: search RUN `agentic_search:run:`→`search_run`; MAP job
  `scg:map:`→`scg_map`; structured `structured:run`→`structured_run`. Surface
  threads `SearchRun.start(source_platform=…)`→`_seed_session` (route passes
  `request_surface()`).

## Probe evidence → trace response + synthesis metrics (#86)

The `sub_agent` STOP event's `detail` is only the `done_reason` ("completed") —
the probe's real `EVIDENCE (pathway: …)` / `NO DATA …` block is `tq.task_result`,
now echoed as an additive `summary` on that event (core `spawn_agent`). So:

- **`ProbeTrace.result_text` / `is_dead_end`** (`run_streamer.py`) are the ONE
  reader + the ONE classifier: the `NO DATA` prefix (the scg-path-probe return
  contract) is the deterministic dead-end marker. The live `_project` and
  settle's `_build_trace` both capture it → `agent_done.result` (the console's
  per-lane response panel) + `empty`=dead-end (the lane reads visibly distinct).
- **`OrchestratedSearchRunner._synthesis_metrics`** derives
  `AnswerSynthesis.confidence` (= data-bearing probes / probes run) +
  `sources_count` (= data-bearing probe count) from that same trace — NEVER the
  echo fixture (the old code left both at the schema default → `0%` / `0 sources`
  beside a real answer). An empty trace ⇒ `(0.0, 0)` ⇒ the console suppresses the
  chip rather than render an unearned `0%`.
- **Fake-runtime transcripts must carry a realistic `summary` on the `stop`
  event** (an EVIDENCE or NO-DATA block) — else the metrics read 0 and the
  evidence panel is empty (the same "mirror the real engine shape" rule as the
  `completion` payload above).

## Coordinator lane + result cards + honest settle (#95)

A fast-tier run can legitimately spawn ZERO probes (the root inlines every
tool call — verified live, run `run-02ad562781`), and the original projection
keyed everything off `sub_agent` events → blank trace, `results=[]`,
`total_ms=0` beside a correct synthesis. The fixes, all additive on the wire
(no `OUTPUT_CONTRACT_VERSION` bump):

- **Coordinator lane** — root `tool_result` events project into ONE synthetic
  lane (id `coordinator`, name `scg-search`, `source_id ""`).
  `CoordinatorTrace` (run_streamer.py) is the pure shared projection (the
  `ProbeTrace` stance: live `_project` and settle render byte-identically).
  SECURITY: a line is a digest only — tool_id + scalar-input hint capped at
  120 chars; the tool's RESULT payload is never read (connector returns can
  carry secrets/PII). The `scg_results` call renders as `emitted N results`,
  never its entries. The lane has no `stop` lifecycle — its `agent_done`
  fires at settle, `empty = no data-bearing probe AND no results`. Slots come
  from merged first-seen order (`_assign_lane_slots`) so settle reproduces
  the live interleaving.
- **Probe tool_results ARE on this transcript/bus (#102 — the #95
  "own-sessions" premise was WRONG, verified live in Mongo).** A child loop
  inherits the parent's `event_logger` (core `AgentContext.child`), and every
  `tool_result` payload carries the emitting `agent_id` — so EVERY
  `tool_result` must be classified `agent_id ∈ probe lanes` before
  projection (live `_probe_emitter` / settle `probe_ids` from the trace; a
  spawn's `sub_agent` `start` always precedes the child's first tool call, so
  the lane is known in time). Unclassified, probe tool calls mislabel into
  the coordinator lane. A probe's `scg_results` projects as ITS result
  cards; its other tool calls project NOWHERE (the probe lane stays
  lifecycle-only — the #86 evidence rides the stop summary).
- **`scg_results` = transcript-as-transport, EVERY search agent emits.** The
  tool (`mewbo_graph.plugins.scg.results`, granted via `TRAVERSAL_TOOLS` +
  bound to probes by the capability gate) validates (≤50 entries,
  `extra="forbid"`, relevance/confidence 0..1) and returns `{ok, count}` —
  it writes NOTHING (layering: the library never touches the api run store;
  no MapPhaseSink-style DI needed because the transcript already reaches the
  api). `ResultsProjection` maps entries → `SearchResult` with stable ids:
  `r-<run_id8>-<n>` (root) / `r-<run_id8>-<agent8>-<n>` (probe emit, salted
  so concurrent emitters never collide) — that id is the live↔settle dedup
  key. Entry `confidence` rides the wire verbatim (`SearchResult.confidence`,
  #102) AND folds into `relevance` only when `relevance` is absent. Playbook
  discipline: each probe emits ONCE before its evidence block; the ROOT
  emits once, before synthesis, ONLY for hits it grounded inline (never
  re-emitting a probe's — duplicate cards have distinct ids, the playbook is
  the dedup). Emitting is NOT terminal for anyone — a probe's terminal stays
  the stop-summary evidence block (#86).
- **Metrics provenance** (`_synthesis_metrics`): `sources_count` = distinct
  grounding sources = data-bearing probe lanes (keyed by `agent_id` — the
  wire `source_id` is the shared parent grouping key, useless for
  distinctness) ∪ emitted results' `source` fields. `confidence` =
  data-bearing/probes when probes ran, else mean folded entry score, else
  `(0.0, 0)` (console suppresses). The coordinator lane is NEVER a probe.
  `total_ms` = `started_at→settle` wall clock, stamped on `run_done`,
  `RunPayload`, and the `_fail` payload — never hardcoded 0 again. **It is also
  now persisted on the RECORD** (`update_run(total_ms=…)` on BOTH the settle and
  `_fail` paths) — the EVIDENCE: those two `update_run` calls OMITTED it, so
  `RunRecord.total_ms` kept its default 0 while `run_done` carried the real
  elapsed (the echo runner already passed it; the orchestrated didn't).
- **Instrument fidelity (run-797097e4b1 forensic audit) — `run_streamer` +
  `orchestrated_runner._settle`, all additive on the wire:**
  - **Cross-emitter SEMANTIC result dedup** (`ResultsProjection.dedup_keys`): a
    card registers BOTH its normalized-url key AND its `(title, source)` key, so
    the root's url-less prose re-emit of a probe's hit collapses into the probe's
    card — FIRST emission wins (5-cards-for-3 fix). Held live AND at settle
    (shared keys); seen-state is per-run instance state.
  - **Per-lane `results_count` is the TRUE count.** `_build_results` returns
    `emitter→count`; each `TraceAgent.results_count` + `agent_done` is credited
    from it (the old hardcoded 0 was blind to a probe that emitted 3 cards). The
    coordinator is credited only its OWN inline emits, never a probe's.
  - **Lane label = agent KIND, never the model.** `ProbeTrace.lane_name` reads
    `sub_agent.agent_type` (Lane A), falling back to the literal `scg-path-probe`;
    `ProbeTrace.model` is the LLM, surfaced as `TraceAgent.model` separately. The
    `start`-brief opener prefers a `SUB-QUERY:`/`PATHWAY:` line (the real task is
    ~7KB past the leaf-executor system-prompt header).
  - **Probe tool digests project.** A probe's non-`scg_results` tool call →
    ONE secret-free digest line (tool_id + capped input hint, never the payload)
    on its lane — the run's only real data fetch
    (`mcp_github_search_repositories`) was being dropped. The coordinator lane is
    ALSO appended to `payload.trace` at settle (kind `coordinator`, blank
    `result`) so a zero-probe run stops snapshotting `trace:[]`.
  - **Per-lane telemetry** (`_lane_stats`) derives `steps`/`duration_ms`/tokens
    from the lane's `llm_call_start`/`llm_call_end` events (match `agent_id`;
    duration = last-end − first-start; tokens = cumulative on the last end, else
    per-call sums) + the `sub_agent` stop aggregates (stop wins for steps/tokens).
  - **`RunStatsWire` is HONEST** (`_build_stats`): `probes` = lanes, `tool_calls`
    = tool_result count, tokens = cross-lane totals, `setup_ms` = `created_at` →
    first user/llm event (the pre-turn MCP-handshake gap the "73s total" hid),
    `search_ms` = `total − setup`. The two `_ms` fields are **None when the
    bracketing event is absent — NEVER a fabricated 0** (the RunStats discipline).
  - **`related_questions` is a PARALLEL structured call, not the agent's emit
    (#111).** `RelatedQuestionsRunner` (`related_questions.py`) reuses the core
    no-loop `StructuredSynthesizer` (one emit + reask) to generate follow-ups from
    the query + synthesized answer. The settle worker kicks it off on a daemon
    thread BEFORE the `answer_delta*`/`answer_ready` events stream (so the answer
    reveals without waiting on it), then joins it (bounded) and appends a dedicated
    `related_questions` event BETWEEN `answer_ready` and the terminal `run_done`
    (the stream closes on `run_done`, so it must land before). It is the PRIMARY
    source; the legacy transcript-read (`_build_related_questions`, last scg_results
    emit) is the fallback when the call is off/empty. The runner is INJECTED — only
    `get_search_runner()` arms it (on the cheap fast-tier model), so fake-runtime
    tests stay LLM-free (`_related_runner=None` ⇒ legacy path), upholding the
    no-real-LLM rule. The console also folds the event live (the snapshot carries
    `RunPayload.related_questions` regardless).
  - **Per-lane `returned_count` complements `results_count` (#111).** `results_count`
    is the KEPT count (post cross-emitter dedup); `returned_count` is the RAW emit.
    `_build_results` returns `(results, kept, returned)`; the live streamer tracks
    `_returned_by_emitter` (credit raw BEFORE the dedup skip in `_emit_results`).
    The `returned − kept` delta is the lane's "N filtered" — the console surfaces
    it so the trace reads how much each tool contributed vs. duplicated.
  - **`run_started.session_id`** now carries the REAL session id (resolved before
    the emit on the happy path); fail-fast paths keep the tag (no session yet).
- **Skills opt-out on BOTH drives.** The search + map drives pass
  `enable_skills=False` (Lane A) via `_skills_opt_out(runtime)` — a signature
  introspection so a pre-Lane-A `run_sync` never raises — because the scg-*
  playbook is the ONLY trusted system-prompt extension.
- **`resolve_entity` trap (open, #95-D):** it reaches search-run sessions via
  the scg plugin manifest + #84's capability-driven `build_for`, but a search
  RUN never satisfies `resolve_qa_ctx` (no QA answer, no
  `structured_workspace` event) → it always errors `wiki ctx not found`.
  Exclusion is NOT one-seam: the same registration serves
  `scg-search-structured`, where the ctx DOES resolve. The clean future fix
  is gating entity tools on wiki-ctx presence rather than the bare `scg`
  capability — don't hack the manifest.

## Two correctness traps (both silent — no exception)

1. **Two different `StructureProvider` protocols share a name root and nothing
   else.** #13's `StructureProvider` (`resolve` / `resolve_many` / `entity_key_of`
   / `exists`) resolves an `entity_key`↔code-node. SCG's `SourceStructureProvider`
   (`build_structure`, in `providers/base.py`) *builds* a connector subgraph from
   a raw descriptor. The memory bridge MUST hand `InsightIngestor` a
   `ScgAnchorResolver` (which implements **#13's** protocol over `source_key`) —
   otherwise the default `CodeStructureProvider` can't resolve a connector
   `source_key`, the live `ANCHORS` edge is never created, and the insight is
   written but silently dropped on read (`memory_vector_search` defaults to
   `exclude_invalidated=True`).
   **The resolver must be KIND-AGNOSTIC (#81-A).** `node_id =
   sha1(source_key|kind)`, so `ScgAnchorResolver.resolve` probes `_ANCHORABLE_KINDS
   = (capability, entity_type)` — an MCP-tool-list source mints `capability` nodes
   (no entity layer), so the old hard-coded `make_id(source_key, "entity_type")`
   resolved NONE for every connector and dropped all anchors. Seed `capability`
   nodes in the seam test (the legacy fixture seeded only `entity_type` — exactly
   the shape that masked the bug). One fix point; `ScgGraphView` reuses the resolver.
2. **Read the flywheel via the store, not the expander.** Retrieve connector
   insights with `store.memory_vector_search(slug, qvec, k, filt=MemoryFilter(corpus="connector"))`,
   **NOT** `MultiplexExpander.expand` — its code-graph neighbour expansion no-ops
   for connectors (they have no tree-sitter CALLS/IMPORTS edges to walk).

## Session-drive invariants (this exact drift shipped two bugs)

Every LLM session is driven through the RunRegistry seam (`runtime.start_command`
/ `start_async`) — the mapper (`map_job.py`) AND the search drive
(`orchestrated_runner.py`). Bare `run_sync` never registers a `RunHandle`, so
`runtime.cancel` is a no-op by construction and a dead worker strands a
`running` record with no terminal event. Terminal status always comes from
`runtime.summarize_session` (the engine's single status chokepoint), never
re-derived from the raw completion payload — re-derivation coerced non-success
`done_reason`s (`awaiting_approval`, `max_iterations_reached`, …) to
"completed" and guarded a "cancelled" spelling the engine never emits
(`canceled`). Wrinkle: settling from inside the worker sees summarize's
`is_running` override (`status="running"`), so `_run_status` falls back to the
summary's verbatim `done_reason`; the raw payload is read only for
`task_result` (the summary doesn't carry it). Deferred: `source_type "text"`
maps 422 at the route — the schemaless `LlmStructureProvider` is never
registered (needs an injected LLM).

## Substrate split — touches two stores by design

- **SCG *structure*** (schemas + pathways) is search-owned: its own `ScgStore`
  (dual JSON/Mongo, `agentic_search_scg_*` collections), deliberately SEPARATE
  from the run store — a re-map rewrites graph nodes without touching in-flight
  runs. Mirrors the run/wiki/project dual-backend pattern (singleton + factory +
  `reset_for_tests`). `node_id = sha1(source_key|kind)[:16]`, overwritten on
  validate (content-addressed, stable across re-maps); `parse_source` does a
  clean `delete_source` first so re-indexing replaces, never accumulates.
- **Learned layer** reuses **#13's `InsightIngestor`** with `corpus="connector"`,
  anchored by `source_key` (the shared memory substrate on `runtime.wiki_store`).
  ZERO re-implementation of atomic-note / anchor / dedup machinery.
- **Manifest hash + drift re-map (#81-C).** `parse_source` stamps
  `mewbo_graph.scg.manifest.ManifestHash.of_descriptor_raw` (order-independent,
  schema-aware sha over sorted tool names+props+required) onto
  `SourceDescriptor.schema_version`. `WorkspaceSourceSync._drifted` recomputes it
  from the LIVE tool list on workspace save and re-maps already-mapped enabled
  sources whose surface drifted (idempotent, in-flight-guarded, no new tick).
- **Map-time enrich (#81-B).** The mapper playbook mints initial memory notes from
  the connector's own tool descriptions + the workspace `instructions`/`desc`,
  which ride `SourceMapInput.nl_context` → `_render_user_query` as an UNTRUSTED
  user-turn block (NEVER `skill_instructions`); anchored to capability `source_key`s.

## Workspace scope is a VIEW, never a store partition (#75 — do NOT re-litigate)

A workspace does **not** get its own copy of the SCG. `docs/features-search.md`
is binding: the SCG is one tenant of the shared multiplex graph and the
wiki/search memory layers cross-pollinate without explicit wiring — a
per-workspace store partition would sever that. So per-source mappings stay
GLOBAL + content-addressed (a re-map is a cheap idempotent upsert *every*
workspace mapping that source benefits from), and a workspace is a **scope
filter**: the source-id allowlist its enabled sources resolve to. `ScgScope`
(`mewbo_graph.scg.scope`) holds that allowlist on a `ContextVar`; `ScgRouter.route`
drops any candidate recipe whose steps reach an out-of-scope source, so
`scg_route` only proposes pathways through the workspace's own sources.
**The scope rides an ambient ContextVar specifically because the `scg` plugin
tools call `ScgCore.store()` / `ScgCore.router()` with no scope argument** — the
search drive (`orchestrated_runner._scoped_to_workspace`) binds it for the worker
thread, so the un-owned plugin tools stay untouched. Cross-workspace insight
attribution, if ever needed, is a TAG on the note, not a partition. (An earlier
namespace-partition-the-store approach was started then reversed against this
doc — bigger diff, wrong philosophy. Don't reintroduce it.)

## source_key scheme + security

`source_key = "<source_id>#<Qualified.Name>"`; a flat MCP tool list →
`"<source_id>#<tool_name>"`. This is the stable anchor the learned layer hangs
off. **No secrets in nodes** — only a redacted `auth_scope` descriptor string
(e.g. `"oauth:repo"`); tokens/credentials stay in the connector config, never
copied into a node, transcript, or event log.

## Entity resolution — type-level offline, instance-level online

`TypeAligner` (`entity_resolution.py`) runs **at map time**: it compares
`entity_type` nodes *across* sources and deposits durable `RESOLVES_TO` edges
(`method="type_align"`) — a weighted, provenanced *hypothesis*, never an
asserted join. **Abstain by default**: emit on the field-overlap heuristic above
`confident_threshold`; a band below it emits only if an *injected* LLM affirms it
(one call per band pair); no LLM → band abstains (NONE-default). Instance-level
matching ("is Jira #42 == Linear ENG-7?") is **native to the probe agent** over
live data — there is no instance-ER module.

## Map-job progress — a DI sink, not a direct write

`MapSourceJob` (`map_job.py`) is the wiki-`WikiIndexingJob` analogue. All durable
state lives in the **agentic_search store** (`MapJobRecord` + its event log), NOT
the SCG structure store, so it rides the run-event-log + `RunSseGenerator`
plumbing verbatim. `MapJobProgress.emit_phase` (`map_progress.py`) dual-writes
the `phase` event + snapshot patch — mirroring the wiki `emit_phase` invariant
(indexing-page vs landing-card never drift) WITHOUT importing wiki `_ctx`.

**Lifecycle is settled by the drive, not the agent.** `start` drives the mapper
session via `runtime.start_command` (the same `RunRegistry` seam `start_async`
rides — serialized per session, cancellable via `should_cancel`), so the worker
that ran `run_sync` settles the job when the session ends:
`queued → running → completed|failed` plus a terminal event (`run_done` /
`error` ∈ `TERMINAL_EVENT_TYPES`) appended to the map-job event log — the SSE
stream closes on it instead of dying by idle timeout, and a crashed mapper can
never stay `queued` forever. That four-state coarse vocabulary is ALL of
`MapJobStatus`; fine-grained pipeline progress is `MapJobPhase`
(connect..finalize) via `emit_phase` — the old `mapping/linking/finalizing`
statuses were dead vocabulary nothing ever wrote. `_settle` is the ONE terminal path (event first,
snapshot second — a snapshot failure never loses the terminal event). Failure
detection reads `TaskQueue.last_error` (the orchestrator catches its own
exceptions and returns the queue; `run_sync` raising is the secondary net).

The asymmetry vs the wiki: the mapper SessionTool now lives **down** in
`mewbo_graph.plugins.scg`, so it can't write this api run store directly. The
API registers a writer at startup (`_register_map_phase_sink` in `routes.py`)
into `mewbo_graph.scg.map_phase.MapPhaseSink`, and the plugin emits cosmetic
phases through that DI seam. No writer (a graph-only install, or the API never
initialised) → the phase is skipped: the SCG structure write already happened,
the phase is purely cosmetic. (The wiki `emit_phase` needs no such sink — it
writes its own *relocated* store, which is already down in `mewbo_graph`.)

## Playbooks + descriptors — two small app-side seams

- **Playbook delivery = `skill_instructions`, BOTH sessions.**
  `playbooks.py:load_playbook` reads the bundled AgentDef body from
  `mewbo_graph.plugins.scg` (via `plugins_root()`), and both drives pass it as
  `skill_instructions` — the mapper (`map_job.py`) and the search session
  (`orchestrated_runner.py`). That is the ONLY trusted system-prompt extension;
  untrusted input (source descriptors, workspace instructions) rides the user
  turn / context events, never through it.
- **`descriptors.py:SourceDescriptorBuilder` lives HERE by layering necessity:**
  it composes `mewbo_tools` (the PUBLIC
  `mewbo_tools.integration.mcp.list_server_tool_schemas(server, cwd=…)` seam,
  which wraps the `_invoke_via_pool` pattern: config load →
  `refresh_if_config_changed` → `get_or_connect` → schema extraction — never
  import that module's underscore privates from an app) with
  `mewbo_graph.scg.types.SourceDescriptor`, and
  `mewbo_graph` may never import `mewbo_tools` (DAG). Both imports are guarded:
  `LookupError` = no configured connector (route 422), `RuntimeError` = deps
  absent / introspect failed (503). Auto-build is deliberately gated on
  `source_type == "mcp_tool_list"` (`SOURCE_TYPE`) — a descriptor-less openapi
  map keeps the mapper's fetch-natively contract. The built raw shape
  `{"tools": [{name, description?, inputSchema?}]}` is exactly what
  `McpToolListStructureProvider` parses.

## Node-query cache + landing summary (#139)

`/sources` (a per-source capability lookup per configured server) and
`/workspaces/<id>/graph` (a `query_nodes(source_id=…)` per scoped source)
re-scan the node collection once per source on every request — on JSON that
reloads + re-validates the whole `nodes.json` each time. `ScgStore` now memoizes
`query_nodes` by the `(source_id, kind, name_contains)` triple (`_NodeQueryCache`,
in `mewbo_graph.scg.store`): the base `query_nodes` is concrete (cache → delegate
to the driver's `_query_nodes_uncached`); **every node write (`upsert_nodes` /
`delete_source`) calls `_invalidate_nodes()`**, so same-process reads (incl. an
in-process map job re-driving the parser) never see a stale graph. A 30s TTL
bounds cross-worker Mongo staleness (another worker's map job can't reach our
`clear`) — the same eventual-consistency contract the console's `staleTime`
already assumes. Adding a new node-mutating write path? Invalidate there too.

The landing health band reads `GET /workspaces/<id>/graph/summary`
(`graph_routes.py`) — the SAME `_build_graph_payload` assembly projected to
`{scope, stats}` only (no node/edge arrays). It shares `_resolve_scope` +
`_safe_graph_payload` with the full graph route (identical auth/404/degradation)
and rides the warm `query_nodes` cache, so the landing never ships the full
graph just to render the four-number stat strip.

## Router — brute-force now, PPR is the documented scale seam

`ScgRouter.route` is the cheap, zero-LLM query-time job: embed → `vector_search`
(brute-force cosine) → expand one hop along capability/route edges → rank by
`cosine + edge weight`. A query-seeded Personalized PageRank with hub damping is
the documented upgrade for catalog scale — it lands **behind the same `route()`
signature** (callers never change) and is deliberately NOT built in v1.

## Plugin boundary

The SessionTool wrappers in `mewbo_graph.plugins.scg` are **thin** — the
deterministic logic lives in `mewbo_graph.scg`. They reach it through the one
`_core.ScgCore` resolver, which late-imports the engine from `mewbo_graph.scg`
(down) and the shared wiki memory substrate from `mewbo_graph.wiki` (down) —
both DOWN within the library, no longer up into an app. The late imports keep a
core-only install (the optional `mewbo-graph` extras absent) degrading to a
structured error instead of crashing at plugin load. AgentDefs (`scg-mapper`,
`scg-search`, `scg-path-probe`) gate on the `scg` capability advertised at
session start; see `mewbo_graph/src/mewbo_graph/plugins/scg/CLAUDE.md`.
Today ONLY `OrchestratedSearchRunner` advertises `scg` — that is the gating
seam #77 widens (any workspace-bound run type grants it + the graph tools).

## Testing notes

- `tests/agentic_search/scg/`. Use `scg.store.reset_for_tests()` + the run
  store's `reset_for_tests()` for isolation; inject a fake embedder / fake LLM /
  fake runtime at the seams. Embedding is best-effort — a missing backend leaves
  a structure-only SCG (mirrors the wiki BM25 fallback), never a hard failure.
- **Fake-runtime transcripts MUST mirror the REAL engine event shapes.** The
  engine completion payload is `{done, done_reason, task_result, error?,
  last_error?}` (`orchestrator.py`) — there is no `text` key. A fabricated
  `{"text": ...}` fixture once masked an always-empty-answer bug in
  `_terminal`; when in doubt, copy the shape from the orchestrator's
  `append_event` call sites, never from memory.
- SCG tests must NEVER spawn a real LLM or hit a real proxy.
