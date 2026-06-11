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
(where offered) wins over the tier map. They are NOT
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
