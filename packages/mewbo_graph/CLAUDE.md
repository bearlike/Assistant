# Mewbo Graph — Capability-Library Guidance

Scope: `packages/mewbo_graph/src/mewbo_graph/` — the optional knowledge-graph
substrate shared by **MewboWiki** and **Mewbo Search**. Root `CLAUDE.md`
covers the monorepo layering rule; this file captures the engineering
decisions specific to this library that aren't obvious from the code.

**Layering (see root CLAUDE.md → "Monorepo layering"):** this is a *capability
library*. It imports **down** into `mewbo_core` + `pydantic` and **nothing
else** — never `mewbo_tools`, never an app. It was extracted from inside the
API (Gitea #25) precisely to make that DAG acyclic: the wiki/scg substrate and
their plugin suites used to be reached by *upward* imports from `mewbo_core`
and `mewbo_api`. If you find yourself adding `import mewbo_api` (or
`mewbo_tools`) here, stop — that is the inversion this package exists to kill.

## What lives here

| Submodule | Owns |
|---|---|
| `wiki/` | tree-sitter code graph (`graph.py` + `graph_queries/*.scm`), multiplex atomic-note memory (`memory.py`, `memory_types.py`, `structure_provider.py`, `retriever.py`), the dual JSON/Mongo `WikiStoreBase` (`store.py`), the wiki domain/wire models (`types.py`), the litellm `Embedder`, and the ephemeral `CloneTokenCache` (`tokens.py`). |
| `scg/` | the Source Capability Graph reachability engine — `types`, `store`, `providers/*`, `parser`, `router`, `entity_resolution`, `memory_bridge`, plus the `MapPhaseSink` DI seam (`map_phase.py`). |
| `plugins/{wiki,scg}/` | the capability-gated SessionTools + AgentDefs that drive the above. They ship **with the substrate they wrap** (not in the core wheel) so they import it **down**. |

Product-level decisions stay in the subsystem docs — read them before editing
the substrate they describe:
- Wiki (memory identity, dedup ladder, refresh, embedder→litellm): `apps/mewbo_api/src/mewbo_api/wiki/CLAUDE.md`.
- SCG (cheap-router architecture, the two silent correctness traps, ER): `apps/mewbo_api/src/mewbo_api/agentic_search/scg/CLAUDE.md` + `plugins/scg/CLAUDE.md`.

## The API/library boundary — engine here, transport there

The api (`mewbo_api.wiki`, `mewbo_api.agentic_search`) is a **thin shell**:
HTTP routes, SSE framing, run/job lifecycle, and persistence *glue*. The
reusable engine + domain models live here. The api composes this library via
the `wiki` extra; it never re-hosts an engine. Two consequences worth
remembering:

- **Domain models travel with the store, not the transport.** `Project`,
  `WikiPage`, `IndexingJob`, etc. live in `wiki/types.py` (here) because the
  store persists them; the api serialises them over the wire. SSE *event*
  models stay in the api (they're transport).
- **`ScgConfig` stays in the api.** It reads `scg.enabled` and is consumed
  only by api glue (routes/map_job/orchestrated_runner) — it is config, not
  engine. Keep it out of this library so the library never reaches for app
  config wiring.

## Down-only seams (how the relocated plugins reach shared state)

Moving the plugins out of an app meant replacing four reach-ups with seams the
plugins import **down**. Don't reintroduce the reach-ups:

- **Wiki store singleton** (`wiki/store.py`): `get_wiki_store()` /
  `set_wiki_store()` / `reset_for_tests()`, mirroring the SCG + run-store
  pattern. The API pins the instance at startup (`init_wiki`); the wiki tools
  resolve it via `_ctx.resolve_runtime()` (a `SimpleNamespace(wiki_store=…)`)
  instead of importing the API's `_runtime`. Tests still patch each tool's
  module-level `_resolve_runtime` alias.
- **`CloneTokenCache`** (`wiki/tokens.py`): the ephemeral, never-persisted
  clone token, shared between the API wizard (writes) and the clone/finalize
  tools (read/forget). It carries zero deps so both layers import it down.
- **`MapPhaseSink`** (`scg/map_phase.py`): map-job phase progress is persisted
  in the API *run* store (so it rides the SSE plumbing) — a transport concern.
  The plugin can't write it without importing up, so the API **registers a
  writer** here at startup and the plugin emits through it. No writer (a
  graph-only install, or the API never initialised) → a no-op: the SCG
  structure write already happened, the phase is purely cosmetic. This DI is
  the asymmetry vs the wiki `emit_phase`, which writes its *own* (relocated)
  store directly.
- **`register_builtin_root`** (in `mewbo_core.plugins`): importing this package
  **pushes** its `plugins/` root to the core loader
  (`mewbo_graph.register_builtin_plugins`, fired on import). Core never imports
  up to discover the plugins; the library registers itself. A lean install
  without `mewbo-graph` simply never registers them — the feature is absent,
  not broken.

## "Optional" means BOTH layers

Heavy deps live behind the `treesitter` and `retrieval` extras (PEP 621), and
**every import site is guarded** so the feature is absent — never a crash —
when an extra is uninstalled. `mewbo-api[wiki]` forwards to
`mewbo-graph[treesitter,retrieval]` (preserving the public extra name + the
Docker `WIKI_EXTRAS` toggle), and the api's graph imports are lazy/guarded so a
base `mewbo-api` install boots graph-less (`init_wiki` returns `False`;
`SourceCatalog` falls back to demo/empty). Adding a heavy dep? It goes behind
an extra + an import-guard, both, or it doesn't go in.

## Two protocols named `StructureProvider` — never merge them

`wiki/structure_provider.py`'s `StructureProvider` (`resolve` / `resolve_many`
/ `entity_key_of`) resolves an `entity_key`↔node for the memory layer.
`scg/providers/base.py`'s `SourceStructureProvider` (`build_structure`) builds
a connector subgraph from a raw descriptor. They share a name root and nothing
else. The SCG memory bridge MUST hand `InsightIngestor` a `ScgAnchorResolver`
(which implements the *former* over `source_key`) or connector insights are
written then silently dropped on read — see the scg subsystem doc.

## Testing

Substrate + plugin tests live under the root suite at `tests/wiki/` and
`tests/agentic_search/scg/` (historical locations) and import `mewbo_graph.*`.
Inject fakes at the seams (`reset_for_tests` for the store, a fake embedder /
fake LLM, `MapPhaseSink.reset()` for the sink); never spawn a real LLM or hit a
real proxy. The library is installed editable in the dev workspace, so
`mewbo_graph` import side-effects (plugin self-registration) are live in tests.

## Pre-edit checklist

- [ ] Adding code here? Does it import only `mewbo_core` + `pydantic` (down)?
      Any `mewbo_api`/`mewbo_tools` import is a layering bug.
- [ ] New heavy dependency? Behind an extra AND import-guarded at the call site?
- [ ] New **wiki** plugin tool? Subclass `plugins/wiki/_base.py:WikiSessionTool`
      (it owns the ctor, runtime/ctx resolution, `should_terminate_run`, and the
      canonical `_err_result`) and implement only `handle()` + the args schema.
      Don't re-inline that boilerplate — collapsing 18 copies of it onto this base
      is why it exists.
- [ ] New plugin tool/AgentDef? Dropped under `plugins/<suite>/` with its
      `plugin.json` entry — discovered via the pushed root, no core change. An
      AgentDef's `tools`/spawn `allowed_tools` MUST use the REAL registered id
      (e.g. `wiki_code_search`, not `code_search`): `filter_specs` silently drops
      unknown ids, so a typo becomes a tool the agent can never call.
- [ ] Shared state the API also touches? Add a down-only seam (singleton /
      cache / DI sink) here; don't make the api reach up or the plugin reach up.
