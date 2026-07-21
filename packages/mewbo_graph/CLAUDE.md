> ↑ [root /CLAUDE.md](../../CLAUDE.md) · children: [plugins/scg/](src/mewbo_graph/plugins/scg/CLAUDE.md)

# Mewbo Graph — Capability-Library Guidance

Scope: `packages/mewbo_graph/src/mewbo_graph/` — the optional knowledge-graph
substrate shared by **MewboWiki** and **Mewbo Search**. Root `CLAUDE.md`
covers the monorepo layering rule; this file captures the engineering
decisions specific to this library that aren't obvious from the code.

**Layering (see root CLAUDE.md → "Monorepo layering"):** this is a *capability
library*. It imports **down** into `mewbo_core` + `pydantic` and **nothing
else** — never `mewbo_tools`, never an app. It was extracted from inside the
API precisely to make that DAG acyclic: the wiki/scg substrate and
their plugin suites used to be reached by *upward* imports from `mewbo_core`
and `mewbo_api`. If you find yourself adding `import mewbo_api` (or
`mewbo_tools`) here, stop — that is the inversion this package exists to kill.

## What lives here

| Submodule | Owns |
|---|---|
| `wiki/` | tree-sitter code graph (`graph.py` + `graph_queries/*.scm`), multiplex atomic-note memory (`memory.py`, `memory_types.py`, `structure_provider.py`, `retriever.py`), the dual JSON/Mongo `WikiStoreBase` (`store.py`), the wiki domain/wire models (`types.py`), the litellm `Embedder`, the durable git-auth seam (`credentials.py` — the `CredentialScope`/`CredentialSource`/`CredentialCandidate` domain models + `CredentialStore` + `resolve_chain` + `is_auth_failure`; the old ephemeral `tokens.py`/`CloneTokenCache` is **deleted**, see "Git auth" below), and `QaFinalizer` (`qa.py` — reconcile a Q&A answer snapshot from its event log + close it; lives here, not the API, because it mutates `QaAnswer`+store, so the terminal `wiki_emit_answer` calls it down-layer alongside the API's session-end net). `resolve_qa_ctx` falls back to the latest `structured_workspace` context event when a session is not a registered QA answer, so a `StructuredResponder`-grounded run resolves a slug-only ctx and wiki grounding tools work instead of returning "wiki QA ctx not found" (`structured_workspace` was written but read by nothing until this fix). `QaMemoryDepositor` (also `qa.py`) closes the QA→memory flywheel: post-answer it distills the cited answer into atomic notes via `InsightIngestor.ingest(condense=True)` anchored to the cited code entities — best-effort, idempotent, fired from the API session-end net off the latency path (reuses the ingestor; no second fan-out). Three `qa.py`/`qa_access.py` statics own QA *citation/provenance* shape: `QaFinalizer.tag_page_citations` re-schemes a bare wiki-page ref in a `sources` block to `wiki:<page-id>` (a page id OR a slugified page TITLE is the authority — `_slugify` symmetric match, so the model citing a human title instead of the slug still resolves — called from `wiki_emit_answer` so live stream + snapshot agree); `AccessedSourceResolver.resolve_refs` maps `graph:<node_id>` provenance refs to readable labels at READ time (AST→`file#Symbol` via `entity_key_of`, entity→`name (type)` via `get_entity`, miss→`unknown(<hash[:8]>)`) NON-destructively — the snapshot keeps raw ids because `QaMemoryDepositor` anchors off them; and `qa_access.py:QaAccessRecord` makes `accessed_sources` a BOUNDED, score-ranked trail — tools emit `{ref,score,rank,tool,op,ok}`, graph-NAV tools (`wiki_query_graph`/`wiki_graph_neighbors`) record only their seed, ranked search tools score-floor their hits, and `QaFinalizer._accessed_from_events` folds → dedupe-by-ref → score-desc → top-N cap (default 12), killing the unranked ~200-node sprawl (`HybridRetriever` stays the one ranker). `catalog.py:CatalogIngestor` — programmatic non-git ingestion (pages + nodes + embeddings, deterministic upsert). |
| `entities/` | abstract-entity layer — `types` (Entity/EntityRelation/EntityMention/EntityRecommendation; deterministic id = `sha1(normalized_name\|type)`), the generalized `ResolutionLadder` + `EntityResolver`, `EntityMinter`, `EntityAnchorResolver`. Lives in the SAME multiplex store (`WikiStoreBase`) as code symbols + connector schemas + notes — NO parallel store/ER. |
| `scg/` | the Source Capability Graph reachability engine — `types`, `store`, `providers/*`, `parser`, `router`, `entity_resolution`, `memory_bridge`, `scope`, plus the `MapPhaseSink` DI seam (`map_phase.py`). The store stays GLOBAL per-source — flat `{source_id}#` keys, content-addressed node ids — **deliberately NOT partitioned by workspace** (`docs/features-search.md`: the SCG is one tenant of the shared multiplex graph; the wiki/search memory layers cross-pollinate). A workspace is instead a **scoped VIEW**, not a store copy: `ScgScope` (`scope.py`) carries a source-id allowlist on a `ContextVar` and `ScgRouter.route` drops any candidate recipe whose steps reach an out-of-scope source — so per-source mappings are shared (a re-map benefits every workspace) while routing/insights stay workspace-isolated. The ambient ContextVar is the seam BECAUSE the `scg` plugin tools (un-owned, call `ScgCore.store()`/`ScgCore.router()` with no scope arg) must stay untouched; the search drive binds the scope for its worker thread. |
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
- **`CredentialStore` + `resolve_chain`** (`wiki/credentials.py`): the ONE
  git-auth seam — the durable, scope-keyed repo credential AND the resolution
  order every consumer shares. The API onboard + the `/v1/git/credentials`
  registry write it; clone / branches / freshness / the description fetch read
  it **down** through here. Full contract in "Git auth" below. (There is no
  longer a `CloneTokenCache`/`tokens.py` seam — deleted, deliberately; see the
  no-cache rule below before you reach for one.)
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

**Grammar loading is a download, not a bundle (supersedes any "1.x bundles
parsers" lore).** `tree-sitter-language-pack` (pinned `>=1.12,<2`) fetches each
grammar over the network on its FIRST `get_language()` call, caching it under
the pack's own versioned, XDG_CACHE_HOME-honouring cache dir — verified
empirically; a bare first-index in a fresh/offline container aborts mid-parse
on that download. Pre-warm offline/air-gapped deploys with `python -m
mewbo_graph.wiki.prefetch` (derives the language list from `graph.py`'s
`_LANG_BY_EXT`, the single source of truth, and calls the pack's official
`prefetch()` — never resurrect the manual `ctypes`+`cache_dir()`+`download()`
path; it's been dead since `download()` went no-op in 1.10.x). Docker's
prefetch `RUN` step needs its OWN `WIKI_EXTRAS=1` guard (the module's own
import-guard can't fire when `mewbo_graph` itself isn't installed) and pins
`XDG_CACHE_HOME=/opt/mewbo-cache` so build-time root and runtime uid-1000
share the cache. `get_tags_query()` exists at this pin (kotlin/java/python/
js/go/rust non-`None`) — relevant to the multi-language overlay.

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
dropped every anchor (a real regression: notes written, ANCHORS edge never created, silently
dropped on read). `ScgParser.parse_source` stamps `ManifestHash` (`manifest.py`)
on `schema_version` so a workspace-save drift check can re-map. Routing is also
memory-aware: `ScgRouter` takes an OPTIONAL `memory_bridge` + blends a
`ScgMemoryBias` term into `cosine+edge` — a vector read + polarity-weighted sum,
NO LLM (zero-LLM core preserved, best-effort/empty without embeddings,
`ScgScope`-respecting). Polarity rides existing `MemoryNode.labels` as `scg:<pol>`
(positive `+0.6` / dead_end `-0.8`, asymmetric; NO new field); `boost_for_steps`
= MAX over steps; `route_with_memory→(recipes,bias)` lets `scg_route` project
capped anchored HINTS. `ScgGraphView` (`graph_view.py`) = the SCG multiplex
assembler mirroring `KnowledgeGraphView` (schema+memory for a source-id scope,
cross-ANCHORS via `ScgAnchorResolver`, self-contained `to_wire` — no Flask,
`auth_scope` redacted — for the `…/workspaces/<id>/graph` route). Workspace
is a `ws:<id>` attribution label (ambient `ScgScope.workspace()`), NEVER a
partition.

## Abstract entities — durable decisions

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
- **Hierarchy wire mode (the 3D "Code Galaxy" viewer).**
  `KnowledgeGraphView.for_slug(..., hierarchy=True)` (route `?hierarchy=1`)
  merges a directory scaffold so the FE can cluster + collapse by folder.
  `FolderTree` (`folder_tree.py`, atomic frozen class, `pathlib.PurePosixPath`)
  synthesises VIEW-ONLY `Folder` nodes + folder `CONTAINS` edges + one
  deterministic `parentId`/`folderPath` per node from the kept File paths
  (folders are never persisted — same view-only status as `External`; symbol
  parents read off the inbound `CONTAINS` edge). The `to_wire` stamp helper is a
  no-op when off, so the DEFAULT payload stays byte-identical — the SCG /
  Agentic Search reuse path is untouched. The folder-collapse this enables is
  the level-of-detail that lets the FE render a ~17K-node repo without OOM (see
  `apps/mewbo_console/src/components/wiki/CLAUDE.md` → "Code Galaxy").

## Developer mode — graph-only indexing (zero-LLM)

`runtime.developer_mode` (core config) unlocks a DETERMINISTIC, SESSIONLESS,
zero-LLM onboarding for inspecting AST graph construction without paying for doc
generation. `GraphOnlyIndexer` (`plugins/wiki/graph_only.py`) is an atomic class
that runs `clone → scan → graph → finalize` and SKIPS `enrich/plan/pages`. It
REUSES the phase-tool cores — the `clone` credential/SSH helpers,
`scan._collect_files`, and `build_graph.build_graph_core` (extracted so
tree-sitter parse+persist+embed lives in ONE place shared by the
`wiki_build_graph` tool AND the indexer) + the `finalize` helpers — so there is
NO duplicated git/scan/tree-sitter logic. It emits the SAME `phase`/`log`/
`complete` events the agent path does (SSE + console progress unchanged), stamps
`Project.graph_only=True`, writes ZERO pages, and uses the synthetic landing id
`"graph"` (the graph view IS the entry point; the empty state never reads it).

**ONE doc-content read seam raises for everyone.** `WikiStoreBase.get_page` is a
template method → `_assert_docs_available(slug)` raises
`DocumentationUnavailableError` (`wiki/errors.py`) when the project is
`graph_only`. That SINGLE raise site is inherited by the API page route, MCP
`read_wiki_page`, and the QA orchestrator's probe reads — add no per-caller
checks. `list_pages` is DELIBERATELY unguarded (finalize prune / `WikiResume` /
the retriever / `QaFinalizer.tag_page_citations` walk it on deterministic
internal paths; guarding it would break indexing itself). `KnowledgeGraphView`
never calls `get_page`, so a graph-only project's graph stays fully explorable.

## Git auth — one chain, one hardened executor, NO cache

Every git-touching path in this library (clone, `ls-remote`/branches, freshness,
the platform description fetch) authenticates through the same two modules. Do
not add a third way.

**`wiki/credentials.py` — the ONE resolution chain.** The domain is Pydantic
end-to-end (`CredentialScope`, `CredentialSource`, `CredentialCandidate`): the
rules below live ON the models, so a caller never re-derives them from a string.
`resolve_chain(store, slug, *, arg_token=None)` defines git-auth precedence for
every consumer: `arg` → `store:repo` → `store:host` → `ambient` → `anonymous`,
yielding a `CredentialCandidate` per tier.

- It is a **lazy generator**, and that laziness is load-bearing, not style: the
  ambient tier forks `git credential fill` (a 10s-capped subprocess). Eager
  resolution paid two store reads + that fork on EVERY call, even when a
  repo-scoped token satisfied the request on the first candidate. A consumer
  that authenticates immediately must never pay for tiers it didn't reach.
- **`CredentialStore` is keyed by SCOPE, not by repo** — and the key is a
  validated `CredentialScope`, never a bare `str`. A bare host
  (`git.example.home`) is shared by every repo on that host; a full slug
  (`host/owner/repo`) is repo-pinned. **`CredentialScope.covers(other)` IS the
  sharing rule** (host covers every repo on its host; a repo covers only itself;
  a different host is never covered) — one predicate, not a `"/" in scope` check
  re-typed at each call site. `.kind` (`host|repo`) backs the `scopeType` wire
  field; no migration, no new stored field.
- **A malformed scope now fails FAST at the write boundaries** (`from_slug` /
  the `<path:scope>` route → clean 400), instead of being written under a key
  nothing could resolve and surfacing later as an opaque "the clone silently
  fell back to anonymous". READ paths (`CredentialScope.coerce`) stay tolerant —
  they degrade to "no scope" rather than blowing up a clone that would otherwise
  succeed anonymously.
- **Scope DEPTH is deliberately uncapped past `host/owner/repo`.** A GitLab
  subgroup (`gitlab.com/group/sub/proj`) is a legitimate 4-segment slug and a
  legacy `owner/repo` 2-segment slug is still in the wild, so `.owner`/`.repo`
  read the LAST TWO segments (the rule `finalize._split_owner_repo` — which now
  delegates here — has always used). Rejecting deeper scopes would make a
  subgroup repo's credential unresolvable and silently downgrade its clone to
  anonymous.
- **Scope is stamped INSIDE the blob at save** (`CredentialStore.save` writes
  `blob["scope"]`; `_decode` strips it back off, since `RepoCredential` is
  `extra="forbid"`). Never reverse-engineer a scope from a filename: the JSON
  driver's `credentials/<scope>.json` encodes `/` as `__`, and inverting that
  corrupts any scope containing a literal `__`. `list_credentials` reads the
  stamped scope and only falls back to the filename for legacy blobs.
- **`is_auth_failure(stderr)` is the one auth classification.** Its markers are
  ANCHORED (`error: 403`, `http 401`, `authentication failed`, …) because bare
  `401`/`403` substrings misfired on unrelated network noise — a `port 8403:
  Connection refused` read as a 403 rejection, burning the chain and emitting a
  false "credential rejected" warning. `repository not found` stays in the set
  deliberately (GitHub masks private repos as 404 to unauthorized clients).
- **`CredentialScope.from_repo_url` parses three shapes** — URL, scp-style
  `git@host:owner/repo`, and a bare slug (it replaced the old `host_of`). The scp
  branch is reachable, not defensive: the wizard accepts an scp remote and, with
  no slug chosen yet, that raw string arrives as the resolution scope (without
  the branch it yielded a garbled `git@host:owner` host). Normalisation is part
  of parsing — trailing `.git`/slashes dropped, host lowercased (owner/repo case
  PRESERVED: it is significant on some forges, and lowercasing would re-key
  existing credentials) — so a credential saved from a pasted remote URL keys the
  same row the clone chain looks up.
- **`CredentialCandidate` enforces "credential is None IFF source is
  anonymous"**, so a consumer never has to check both; `.token`/`.ssh_key`/
  `.username` project the credential (an ssh key's username never leaks into the
  token-injection path). `CredentialSource.scope_for(scope)` names the scope a
  STORED candidate came from (arg/ambient/anonymous → `None`) — that is how the
  clone warns "credential for X was rejected" without re-deriving repo-vs-host.
- Chain dedup keys on `RepoCredential.dedup_key` = `(kind, value, username)` — a
  value shared by two usernames (a GitLab `oauth2` deploy token vs a PAT) must
  try BOTH.
- **`RepoCredential.value` is stripped at definition.** A PAT pasted with a
  trailing newline silently 401s with no hint that an invisible character is the
  cause; the `mode="before"` validator kills that footgun. Safe for SSH keys too
  (internal newlines survive; `clone._ssh_env_for` re-appends the terminating
  one the key file needs).

**Why there is no token cache.** `CloneTokenCache` (`wiki/tokens.py`) was a
THIRD source of truth alongside the store and the ambient credential, and it
drifted: a revoked stored token permanently shadowed a still-valid ambient one
with no fallback, which is what produced cascading re-index failures in
production. The database and the ambient git credential are now the only two
sources. If you find yourself caching a resolved token "to save a store read",
you are rebuilding the bug — the chain is lazy precisely so you don't need to.

**`plugins/wiki/clone.py` — the ONE hardened git executor.**
`run_git_with_chain(store, slug, url, build_argv, …)` is the single
credential-iterating subprocess runner: it walks `resolve_chain`, injects a
token into the URL (or writes an SSH key to a 0600 temp file), runs the argv the
caller builds, redacts every tried secret out of stderr, advances ONLY on an
auth-class failure, and aborts the whole chain on anything else (a network error
or bad ref will not succeed with a different credential — don't burn valid
candidates on it). Its outcome carries the `winner` — which candidate actually
authenticated — so a follow-up call can prefer it instead of re-resolving blind.
`build_clone_command` / `build_ls_remote_command` / `hardened_git_env` are the
shared argv+env builders. **EVERY git subprocess goes through them** — clone,
`ls-remote` in `branches.py`, freshness, and the API's credential-validate route.

**Advance on REFUSAL, abort on FAILURE — the same law on both sides.** The git
executor advances the chain only on an auth-class stderr (`is_auth_failure`); the
REST helper advances only on an auth REFUSAL status (`_AUTH_RETRY_STATUSES` =
401/403/404 — 404 because platforms mask private repos from unauthorized
clients). Anything else — a network error, a timeout, a 5xx — returns
immediately in both: a different credential cannot fix a transport failure, and
retrying one burns valid candidates against a dead endpoint while the user waits.

**THE TRAP (verified live).** A container that inherits `credential.helper=store`
with a READ-ONLY mounted `~/.git-credentials` makes git's post-auth `store`/
`erase` fail with `fatal: unable to write credential store: Device or resource
busy` — and that EBUSY **masks the real auth error**, killing an otherwise
successful clone with a nonsense message that `is_auth_failure` doesn't even
match (so the chain aborts instead of falling through). Hence `-c
credential.helper=` on every git subprocess plus `GIT_TERMINAL_PROMPT=0`, and
hence we read the ambient credential ourselves via read-only `git credential
fill` and inject it into the URL — git is never allowed to touch that file. A new
git call site that assembles its own argv reintroduces this; the un-hardened
branch lister was exactly where the incident kept reproducing after the "fix".

**THE SECOND TRAP: winning a git operation does NOT prove a credential is valid.**
A PUBLIC repo serves `git ls-remote` and `clone` happily with a REVOKED token —
the git layer never challenges it — so the chain never advances and
`run_git_with_chain` returns a DEAD credential as its `winner`. Hand that winner
to an AUTHENTICATED REST call and it 401s, and the caller silently degrades (no
description; `behind_by=None`) even though a later chain credential would have
worked. Verified live: a revoked repo-scoped token cloned a public repo fine,
then 401'd the Gitea compare API that the ambient credential answered with
`total_commits=155`. **Treat a git success as evidence about the REPO's
visibility, not about the CREDENTIAL.**

So every authenticated REST call re-walks the chain **itself**, through the ONE
retry policy: `_platform_api.api_get_json_with_chain(...)`. It tries
`preferred_token` first (the git winner — usually right, and free when it is),
then each remaining token candidate on a refusal per the advance-vs-abort law
above, capped at `_MAX_CRED_ATTEMPTS`. Anonymous is ALWAYS the terminal attempt
and is never counted against the cap: when every stored token has been revoked, a
public repo still answers unauthenticated. Its candidate walk is lazy for the
same reason `resolve_chain` is — a winning `preferred_token` must never fork the
10s `git credential fill`. Both callers go through it: freshness's compare passes
the ls-remote winner as `preferred_token`; finalize's description fetch has no
git step to inherit a winner from, so it passes none and lets the chain walk
itself. Neither owns a retry loop of its own — if you add a third authenticated
platform call, it doesn't get one either.

**`plugins/wiki/_platform_api.py` — the ONE per-platform REST seam.** The auth
header table (github `Bearer` / gitea `token` / gitlab `PRIVATE-TOKEN`), the GHE
`api/v3` base-URL branch, the private-TLD TLS carve-out, the guarded fetch that
never raises, AND the chain-retry policy above. Each caller supplies only its
endpoint path + response key. Bitbucket is description-only on purpose (its
app-password Basic scheme isn't portable from a bare token, and it has no
portable compare shape).

**`plugins/wiki/freshness.py` — `RepoFreshness.check`.** `ls-remote` through the
chain → platform compare → `behind_by`. The three-state result is deliberate and
honest: `up_to_date=None` = could not check (remote unreachable);
`behind_by=None` with `up_to_date=False` = the sha MOVED but we can't count the
commits ("differs but uncountable" is NOT "up to date"); `behind_by=0` = fresh.
Never collapse the two `None`s into a false green.

## Remote branch listing + ref-pinned clone

The wizard lets the user pick a branch to onboard. `RemoteBranchLister`
(`plugins/wiki/branches.py`) is the atomic class behind it: `git ls-remote
--symref <url> HEAD refs/heads/*` (ONE subprocess, no clone) → `RemoteBranches
{branches, default_branch}` (the symref line names the default). **Host-agnostic
by design** — `ls-remote` works identically on all six platforms, so we
deliberately do NOT call per-platform REST branch APIs (GitHub `/branches`,
GitLab `/repository/branches`, … each a different path + auth + pagination). It
builds its argv/env with the SHARED `build_ls_remote_command` + `hardened_git_env`
(plus `_inject_token` / `_ssh_env_for` / `_is_private_host` / `_redact`), so token
injection, the SSH temp-key env, the private-TLD TLS carve-out, the helper-disable,
and the stderr scrubber are byte-for-byte the clone path — this endpoint is jobless
and was the last place the EBUSY-masks-auth trap survived. `username` threads a
stored credential's own username (GitLab `oauth2`/deploy tokens) rather than always
injecting `x-access-token`. It is DI'd (url + token/ssh_key/username in, NO store) —
iterating the durable chain is the API route's job, not the engine's. Failure raises
`BranchListError`, stderr secret-scrubbed.

The chosen branch rides `WizardSubmission.ref` (optional, null = default branch).
`build_clone_command(clone_url, dir, *, ref, private_host)` is now the SINGLE
clone-argv builder — both `WikiCloneRepoTool` AND `GraphOnlyIndexer._clone` thread
`ref` through it (the `--branch <ref> --single-branch` + private-TLS insert live
in ONE place, never duplicated across the two clone paths). `ref` round-trips the
submission sidecar like `graph_only`, so refresh/recovery preserve it.

## Code graph schema v2 (`types.py`) — validated discriminated union

Nodes are per-kind subclasses of `GraphNodeBase` (`FileNode`, `ClassNode`, …,
`ObjectNode`/`PropertyNode`) discriminated on the LITERAL `type` field (wire
compat is non-negotiable) — construct via a per-kind class or
`make_graph_node(**data)`, never a bare dict. `GraphNode` is a union type
ALIAS + `GraphNodeAdapter` (`TypeAdapter`) for rehydration, a DELIBERATE
deviation from the house `RootModel` union pattern: node fields (`.node_id`/
`.type`/…) are read pervasively, so a `.root` wrapper would churn dozens of
call sites. Nodes are `frozen=True` (hierarchy stamping happens on the wire
dict in `KnowledgeGraphView`, never the node). Nodes AND edges share two open
extension axes: `subkind: str | None` (linguistic flavor, e.g. Kotlin
`companion` — kinds stay structural + closed) and `attributes` (namespaced
`<lang|tool>.<name>` keys, validator-enforced).

`CodeGraph` validates the WHOLE graph at ingest (`build_graph_core`, before
persist): node-id uniqueness, referential integrity, and a small data-driven
per-edge-type source-kind rules table; rehydrating a trusted persisted graph
bypasses this (`model_construct`) — an ingest-time gate, not a read-time tax.

**Known trap, deliberately NOT fixed.** The tree-sitter EXTENDS block computes
its edge SOURCE id from the subclass NAME token's `start_byte`, not the class
DEF node's — it matches no persisted node, so `KnowledgeGraphView` silently
drops the edge (superseded for Python by the scip resolver below; other
languages just lose it). `CodeGraph` encodes this rather than fighting it: any
edge carrying `target_name` (a synthetic cross-file/by-name ref) is exempt from
endpoint resolution on BOTH ends, not just the target. Any NEW capture family
emitting edges MUST derive its source id from the DEF node's byte — the
extension-function block (`fun Receiver.name()`) is the reference.

**Subkind assignment picks the TIGHTEST containing def, not the first.** A
`.scm` mints a subkind purely by naming a capture `@<family>.subkind.<value>`
(e.g. `@class.subkind.data_class`); `_subkinds_for` (`graph.py`) byte-contains
it against same-family defs. Naive "first containing def" mis-assigns a
nested same-family construct's subkind to its OUTER container (data class
nested in sealed class — the outer's wider range also contains the inner's
"data" token); fixed by preferring the smallest-span match (regression:
`test_kotlin_class_subkinds`, Circle-in-Shape) — a general trap for any
nesting-capable construct, not Kotlin-specific.

**`captures()` already dedupes; `_dedupe_nodes` is a backstop, not the
mechanism.** `QueryCursor.captures()` collapses identical (capture-name,
node) pairs across overlapping patterns within ONE compiled query, so
Kotlin's shared `class_declaration` (a generic pattern layered under
data/sealed/enum subkind patterns) never actually double-emits — the fold
just guards a future combination that DOES. A def with no name at all
(anonymous `companion object`) pairs via `_pair_defs_with_names`
(byte-containment, not the strict positional zip) and defaults to the
literal "Companion".

## Faithful symbol resolution — `wiki/resolve/` (scip-python)

The tree-sitter extractor emits cross-file edges *by name*, which mislinks every
same-named method/class onto one node. `wiki/resolve/` resolves them EXACTLY
behind a `SymbolResolver` protocol (the seam per-language backends plug into).
`ScipPythonResolver` is the Python backend (scip-python / Pyright). It is
**standalone + reversible** — NOT wired into the index pipeline or
`KnowledgeGraphView._resolve_ast_edges` yet; it returns `ResolutionResult{edges,
externals, stats}` whose edge targets are REAL `node_id`s (`target_name=None`, so
the view's name-fallback is bypassed). Atomic-class throughout: `ScipSymbol` owns
descriptor parsing; `SubprocessScipProducer` the subprocess + pyrightconfig (a DI
seam, so unit tests inject a captured `scip print --json` and need no binaries);
`ScipPythonResolver` discovery + the pure two-pass mapping, with
`_SymbolIndex`/`_ExternalRegistry`/`_EdgeAccumulator` collaborators.

Non-obvious scip-python facts (verified on a large internal Python monorepo —
don't re-derive):
- **One run per `[project]` root.** The monorepo root alone yields an EMPTY
  index. Discover each `pyproject.toml` with a `[project]` table (line scan, no
  `tomllib` → 3.10-safe), run scip-python per root (parallelizable), and write a
  per-root `pyrightconfig.json` whose `extraPaths` point at sibling import roots
  (`<root>/src` or the root itself, e.g. `lib/*`) so cross-package imports resolve
  **without a virtualenv**.
- **Descriptor stitching.** A symbol is `scip-python python <pkg> <ver>
  <descriptor>`. Cross-*project* refs carry the WRONG `<pkg> <ver>` (no venv → no
  distribution map) but a byte-identical `<descriptor>` tail. Resolve order:
  exact-full-symbol → descriptor-single → **drop on descriptor-ambiguous (never
  mislink)** → External (converged by descriptor). In-repo-but-unmodelled symbols
  (attributes/params = SCIP `.`/`(name)` leaves) are DROPPED, never faked external.
- **`--project-version` is required** or scip-python crashes inferring it from git
  on a non-git checkout (pin `0`; the token is stripped during stitching anyway).
- **byte ↔ line:col.** SCIP ranges are 0-based `[line, col]` with
  `text_document_encoding=UTF-8`, so `col` IS a byte offset and a def occurrence
  points at the NAME token. Build a line→byte table per file and map by innermost
  byte-containment + name. SCIP `relative_path` is project-root-relative — join it
  with the project-root prefix to get the repo-relative path that **equals
  `GraphNode.file`** (the wiring contract).
- **Edge types are `REFERENCES` + `EXTENDS` only** (the latter from
  `SymbolInformation.relationships[].is_implementation`). NOT `IMPORTS`/`CALLS`:
  scip-python doesn't reliably set the SCIP Import role (imports surface as
  `ReadAccess`), so splitting them out would be guessing — fix at this seam later
  if ever, never by sniffing roles.
- We resolve to NODES (only File/Class/Method/Function exist), so the headline
  rate reads lower than scip's def-*site* % — but **100% of in-repo refs to
  modeled kinds resolve, with ZERO mislinks** (validated at scale: 41.5k
  exact + 1.2k EXTENDS, 0 ambiguous). No Python dep (binaries are subprocesses);
  gated on `ScipPythonResolver.is_available()` + the empty `mewbo-graph[resolve]`
  extra (the forward home for a future pure-Python SCIP parser that drops `scip`).

> **tree-sitter capture-pairing gotcha (FIXED — `_by_position`).** `QueryCursor.
> captures()` groups matches per capture NAME, but the per-name lists are NOT
> guaranteed to be mutually index-aligned — their order varies run-to-run. So
> `_extract`'s `zip(captures["class.def"], captures["class.name"])` could attach a
> node's name to ANOTHER node's byte range (reproduced: source `Base/Item/Thing` →
> a `Base` node carrying `Thing`'s range). NOT a `Query`-reuse bug (an earlier
> mis-diagnosis): the captures dict itself is per-call nondeterministic. Fix =
> `graph.py:_by_position` sorts each capture list by start byte before zipping —
> every captured def is a disjoint sibling that contains its own name, so document
> order is a sound join key. Regression: `tests/wiki/test_graph.py::
> test_extract_binds_names_to_own_def_under_misaligned_captures`. (This is why the
> resolver's `_match_node` requires `node.name == leaf_name` — a correct name is
> the join key.)

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
