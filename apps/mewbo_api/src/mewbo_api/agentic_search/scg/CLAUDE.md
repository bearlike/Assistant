> ↑ [agentic_search/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_api/CLAUDE.md](../../../../CLAUDE.md) · [root](../../../../../../CLAUDE.md)

# Source Capability Graph (SCG) — API Subsystem Guidance

Scope: `apps/mewbo_api/src/mewbo_api/agentic_search/scg/` — the run/map-job
**lifecycle glue** (`config`, `map_job`, `orchestrated_runner`, `map_progress`).
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
decomposition depth + probe-count fan-out (see `scg-search.md`). They are NOT
verification rounds; there are no verification rounds. **The connector's real
return is the only verifier.** Cut as over-engineering — do NOT reintroduce:
explicit A\* `f=g+h` frontier, multi-path self-consistency / majority-vote
verification, process-reward heuristic, MCTS, trained PRM / value-fn, RL search.

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
2. **Read the flywheel via the store, not the expander.** Retrieve connector
   insights with `store.memory_vector_search(slug, qvec, k, filt=MemoryFilter(corpus="connector"))`,
   **NOT** `MultiplexExpander.expand` — its code-graph neighbour expansion no-ops
   for connectors (they have no tree-sitter CALLS/IMPORTS edges to walk).

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

The asymmetry vs the wiki: the mapper SessionTool now lives **down** in
`mewbo_graph.plugins.scg`, so it can't write this api run store directly. The
API registers a writer at startup (`_register_map_phase_sink` in `routes.py`)
into `mewbo_graph.scg.map_phase.MapPhaseSink`, and the plugin emits cosmetic
phases through that DI seam. No writer (a graph-only install, or the API never
initialised) → the phase is skipped: the SCG structure write already happened,
the phase is purely cosmetic. (The wiki `emit_phase` needs no such sink — it
writes its own *relocated* store, which is already down in `mewbo_graph`.)

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

## Testing notes

- `tests/agentic_search/scg/`. Use `scg.store.reset_for_tests()` + the run
  store's `reset_for_tests()` for isolation; inject a fake embedder / fake LLM /
  fake runtime at the seams. Embedding is best-effort — a missing backend leaves
  a structure-only SCG (mirrors the wiki BM25 fallback), never a hard failure.
- SCG tests must NEVER spawn a real LLM or hit a real proxy.
