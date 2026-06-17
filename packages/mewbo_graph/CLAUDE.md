> ↑ [root /CLAUDE.md](../../CLAUDE.md) · children: [plugins/scg/](src/mewbo_graph/plugins/scg/CLAUDE.md)

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
| `wiki/` | tree-sitter code graph (`graph.py` + `graph_queries/*.scm`), multiplex atomic-note memory (`memory.py`, `memory_types.py`, `structure_provider.py`, `retriever.py`), the dual JSON/Mongo `WikiStoreBase` (`store.py`), the wiki domain/wire models (`types.py`), the litellm `Embedder`, the ephemeral `CloneTokenCache` (`tokens.py`), and `QaFinalizer` (`qa.py` — reconcile a Q&A answer snapshot from its event log + close it; lives here, not the API, because it mutates `QaAnswer`+store, so the terminal `wiki_emit_block` calls it down-layer alongside the API's session-end net). `resolve_qa_ctx` falls back to the latest `structured_workspace` context event when a session is not a registered QA answer, so a `StructuredResponder`-grounded run resolves a slug-only ctx and wiki grounding tools work instead of returning "wiki QA ctx not found" (#51 — `structured_workspace` was written but read by nothing until this fix). `QaMemoryDepositor` (also `qa.py`) closes the QA→memory flywheel: post-answer it distills the cited answer into atomic notes via `InsightIngestor.ingest(condense=True)` anchored to the cited code entities — best-effort, idempotent, fired from the API session-end net off the latency path (reuses the ingestor; no second fan-out). Two more `qa.py` statics own QA *citation/provenance* shape (#70): `QaFinalizer.tag_page_citations` re-schemes a bare wiki-page ref in a `sources` block to `wiki:<page-id>` (membership in the real page set is the authority — called from `wiki_emit_block` so live stream + snapshot agree), and `AccessedSourceResolver.resolve_refs` maps `graph:<node_id>` provenance refs to readable labels at READ time (AST→`file#Symbol` via `entity_key_of`, entity→`name (type)` via `get_entity`, miss→`unknown(<hash[:8]>)`) NON-destructively — the snapshot keeps raw ids because `QaMemoryDepositor` anchors off them. `catalog.py:CatalogIngestor` — programmatic non-git ingestion (pages + nodes + embeddings, deterministic upsert). |
| `entities/` | abstract-entity layer — `types` (Entity/EntityRelation/EntityMention/EntityRecommendation; deterministic id = `sha1(normalized_name\|type)`), the generalized `ResolutionLadder` + `EntityResolver`, `EntityMinter`, `EntityAnchorResolver`. Lives in the SAME multiplex store (`WikiStoreBase`) as code symbols + connector schemas + notes — NO parallel store/ER. |
| `scg/` | the Source Capability Graph reachability engine — `types`, `store`, `providers/*`, `parser`, `router`, `entity_resolution`, `memory_bridge`, `scope` (#75), plus the `MapPhaseSink` DI seam (`map_phase.py`). The store stays GLOBAL per-source — flat `{source_id}#` keys, content-addressed node ids — **deliberately NOT partitioned by workspace** (`docs/features-search.md`: the SCG is one tenant of the shared multiplex graph; the wiki/search memory layers cross-pollinate). #75 makes a workspace a **scoped VIEW**, not a store copy: `ScgScope` (`scope.py`) carries a source-id allowlist on a `ContextVar` and `ScgRouter.route` drops any candidate recipe whose steps reach an out-of-scope source — so per-source mappings are shared (a re-map benefits every workspace) while routing/insights stay workspace-isolated. The ambient ContextVar is the seam BECAUSE the `scg` plugin tools (un-owned, call `ScgCore.store()`/`ScgCore.router()` with no scope arg) must stay untouched; the search drive binds the scope for its worker thread. |
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
- **`CredentialStore`** (`wiki/credentials.py`): the DURABLE, per-slug repo
  credential (token or SSH key) — the persisted counterpart to the ephemeral
  `CloneTokenCache`. Plaintext-at-rest behind an identity `_encode`/`_decode`
  seam (the one place encryption lands), stored in an isolated backend surface
  (`credentials/<slug>.json` mode 0600 / `wiki_credentials` collection) via the
  new `WikiStoreBase.{save,get,delete}_credentials`. The API onboard writes it;
  the clone tool reads it **down** through this class. Always redacted in-flight.
- **`MapPhaseSink`** (`scg/map_phase.py`): map-job phase progress is persisted
  in the API *run* store (so it rides the SSE plumbing) — a transport concern.
  The plugin can't write it without importing up, so the API **registers a
  writer** here at startup and the plugin emits through it. No writer (a
  graph-only install, or the API never initialised) → a no-op: the SCG
  structure write already happened, the phase is purely cosmetic. This DI is
  the asymmetry vs the wiki `emit_phase`, which writes its *own* (relocated)
  store directly.
- **`SearchLauncher`** (`scg/search_launcher.py`): the SAME inversion for the
  self-facing `agentic_search` SessionTool. A task-spawned engine agent that
  RUNS a search needs the full run lifecycle (its own `scg-search` session, the
  run store) — all up-layer in the API. So the API registers a concrete
  launcher (`RunStoreSearchLauncher`, bound to the run store + session runtime,
  reusing `SearchRun.start`) and the tool drives through it. Async-by-handle:
  `start()` returns an idempotent `run_id` immediately (a search runs for
  minutes); `fetch(run_id)` reads the cited-answer snapshot + `computed_at`. No
  launcher registered → the tool degrades to a structured "unavailable" error.
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

`ScgMemoryBridge` deposits through the shared `InsightIngestor`
(`corpus="connector"`, anchored to capability `source_key`s). Its
`ScgAnchorResolver` resolves a `source_key` KIND-AGNOSTICALLY over
`_ANCHORABLE_KINDS=(capability, entity_type)` because `node_id=sha1(source_key|kind)`
— an MCP-tool-list source is `capability`-only, so a fixed-`entity_type` probe
dropped every anchor (#81-A: notes written, ANCHORS edge never created, silently
dropped on read). `ScgParser.parse_source` stamps `ManifestHash` (`manifest.py`)
on `schema_version` so a workspace-save drift check can re-map (#81-C). #76 (LANDED) makes
routing memory-aware: `ScgRouter` takes an OPTIONAL `memory_bridge` + blends a
`ScgMemoryBias` term into `cosine+edge` — a vector read + polarity-weighted sum,
NO LLM (zero-LLM core preserved, best-effort/empty without embeddings,
`ScgScope`-respecting). Polarity rides existing `MemoryNode.labels` as `scg:<pol>`
(positive `+0.6` / dead_end `-0.8`, asymmetric; NO new field); `boost_for_steps`
= MAX over steps; `route_with_memory→(recipes,bias)` lets `scg_route` project
capped anchored HINTS. `ScgGraphView` (`graph_view.py`) = the SCG multiplex
assembler mirroring `KnowledgeGraphView` (schema+memory for a source-id scope,
cross-ANCHORS via `ScgAnchorResolver`, self-contained `to_wire` — no Flask,
`auth_scope` redacted — for the #79 `…/workspaces/<id>/graph` route). Workspace
is a `ws:<id>` attribution label (ambient `ScgScope.workspace()`), NEVER a
partition.

## Abstract entities — durable decisions (Gitea #35)

- **One substrate, one ladder.** Entities are new families on the existing
  `WikiStoreBase` (JSON + Mongo), embedded via the same `Embedder`, anchored via
  the same `AnchorResolver` protocol. `entities/resolver.py:ResolutionLadder`
  (generic block→score→decide with injected strategies + recommendation priors)
  backs BOTH `EntityResolver` AND `InsightDeduper` — don't fork a second ladder.
- **Deterministic id ⇒ idempotent upsert.** `Entity.id = sha1(normalized_name|type)`
  (the `MemoryNode.compute_node_id` idiom); every write is an UPSERT, so re-index
  converges and never duplicates.
- **Soft type + per-mention provenance.** `type: str` (seed vocab + open
  extension), stored as a property — never an enum/label/partition.
  `Entity.mentions` makes a merge auditable + reversible.
- **Recommendations are priors.** `EntityRecommendation` records bias the next
  resolution pass; they never hard-mutate the graph. `wiki_submit_insight`
  carries them (page-writer surfaces a prose-only entity → next re-index mints it).
- **GraphRAG ordering law.** KG is built BEFORE generation. The entity stage is
  the `enrich` phase, POST-AST: `clone → scan → graph → enrich → plan → pages →
  finalize`. A `wiki-enricher` leaf (mirrors `wiki-page-writer` fan-out) grounds
  each LLM entity against AST symbols + SOURCE prose (docstrings/comments/READMEs)
  — never generated page prose; ungrounded ⇒ dropped. SKIP Leiden/Louvain — plan
  by the free AST/module/package/directory hierarchy + entity co-occurrence.
- **`rapidfuzz` is optional** — behind the `retrieval` extra + an import-guard
  (`fuzzy_ratio` falls back to exact match → cosine-only when absent).
- **A persisted field the model can't SEE is dead.** `Entity.labels` (open-vocab
  UML-ish tags) round-tripped through the store but stayed `[]` because
  `MintEntityArgs` never exposed it (`wiki_submit_insight` did; `mint_entity`
  didn't — that asymmetry WAS the bug). Surface every model-writable field on the
  tool schema, and UNION list-valued fields on BOTH resolution paths
  (`_apply_merge` AND the `_apply_new` idempotent re-mint fold): a deterministic id
  means re-index ALWAYS re-resolves, so a non-unioned `labels`/`mentions` silently
  regresses on the second pass.
- **The entity↔code bridge is a NOTE, not a new edge family.** The enricher
  `wiki_submit_insight`s anchored to BOTH `entity:<id>` AND `file#Symbol` — the
  memory `ANCHORS` path already ties all corpora into one graph; don't add an
  entity→AST edge type. (`mint_entity.anchors` also writes entity→node `ANCHORS`,
  resolving via `CodeStructureProvider`/`EntityAnchorResolver` with `entity:` keys
  pre-split.) The enricher reads the most prose, so it — not just the indexer —
  MUST own `wiki_submit_insight`, or the memory layer stays empty. User-story /
  actor entities are purely prompt-elicited (`type=role`→`type=user-story`), no
  schema change — soft `type` + open `labels` already carry it.
- **`KnowledgeGraphView` (api side) is the ONLY multiplex reader.** The store
  serves each layer separately; the view unifies AST+entity+memory + reconciles
  cross-layer `ANCHORS` via the existing `resolve_many` resolvers (classify a
  target by set-membership, NEVER id length). Full contract:
  `apps/mewbo_api/src/mewbo_api/wiki/CLAUDE.md` → "KG endpoint".

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
