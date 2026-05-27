# MewboWiki — API Subsystem Guidance

Scope: this file applies to `apps/mewbo_api/src/mewbo_api/wiki/` (the thin
HTTP/SSE + job-lifecycle glue) and the wiki SessionTools under
`packages/mewbo_graph/src/mewbo_graph/plugins/wiki/`. The reusable substrate
they drive — tree-sitter code graph, multiplex memory engine, embedder,
retriever, store, and the wiki domain/wire models — was extracted to
`mewbo_graph.wiki` (Gitea #25); see `packages/mewbo_graph/CLAUDE.md` for the
library-level + layering decisions. This file captures the non-obvious
engineering decisions behind the DeepWiki-style indexing + Q&A pipeline.
Everything that can be read straight from the code is left out.

## What MewboWiki is

A DeepWiki-style auto-generated wiki for code repositories. Pipeline is
a fixed six-phase state machine running inside a normal Mewbo session
(not a separate service): an agent owns the run, the wiki built-in
tools persist state, and SSE streams progress to the FE.

Phases, in order, are the source of truth for progress everywhere:

```
clone → scan → graph → plan → pages → finalize
```

`emit_phase(ctx, name)` is the one and only writer of the current
phase. It writes the SSE event AND updates the persisted job snapshot
in the same call — that's why the landing-page card and the indexing
page can never drift apart (they read the same write through two
different transports).

## Single source of truth for progress

| Field                       | Written by                | Read by                                  |
|-----------------------------|---------------------------|------------------------------------------|
| `IndexingJob.phase`         | `emit_phase` (_ctx.py)    | `/v1/wiki/projects` snapshot + SSE `phase` |
| `IndexingJob.phase_started_at` | `emit_phase`           | FE `IndexingProgress` ETA extrapolation  |
| `IndexingJob.total_pages`   | `commit_plan`             | Landing card page-bar denominator        |
| `IndexingJob.pages_submitted` | `submit_page` (per page) | Landing card page-bar numerator          |
| `IndexingJob.scanned_count` / `total_count` | `scan` per file | Scan-phase sub-progress in both views   |

If you add a new progress signal, write it here too — never in only one
transport. The FE atomic class
(`apps/mewbo_console/src/components/wiki/progress.ts`) reads every one
of these and feeds both the landing card and the indexing page.

Legacy `IndexingStatus` (`queued|scanning|finalizing|complete|cancelled|failed`)
is the coarse 6-state lifecycle — keep it for backwards compatibility
but never use it as fine-grained phase. The FE `IndexingProgress`
class will infer phase from status for ancient jobs that never emitted
a `phase` event, but new code MUST emit `phase`.

## Wiki capability gating

Wiki agent definitions (`wiki-indexer`, `wiki-page-writer`, `wiki-qa`)
are loaded by `agent_registry.py` only when the session advertises the
`"wiki"` capability. `WikiIndexingJob.start` and `WikiQaSession.start`
append `{"client_capabilities": ["wiki"]}` as a context event right
after creating the session. Without that line, `spawn_agent` cannot
look up the wiki-* AgentDefs and the run will appear "stuck after
scan" — the parent agent finishes scan but has no child it can hand
the rest off to.

If you ever rename a wiki capability or add a new one, update both
`jobs.py` (capability advertisement) and `agent_registry.py` (gate).

## SSE plumbing — proxy buffer + resume

`events.py:_SSE_PRIMER` is a 2KB padded comment frame emitted once at
stream start. The reason: OpenResty/NPM and similar HTTP/2 proxies
buffer responses up to ~4KB by default, so the first few real SSE
events never reach the browser until either the buffer fills or the
connection closes. Yielding a 2KB comment frame at byte 0 forces the
proxy to flush and switch into streaming mode. Same trick is used for
heartbeats — `_heartbeat_frame()` is also 2KB padded. Don't reduce
these sizes "to save bytes"; that just reintroduces the buffer bug.

`_to_sse` emits `id: <idx>\nevent: <type>\ndata: <json>\n\n`. The `id:`
line is mandatory: browsers using a native `EventSource` will send the
last received `id` back as `Last-Event-ID` on auto-reconnect, and the
route honours that header so a flaky proxy that drops mid-stream can
resume without replaying the entire transcript. The current FE doesn't
use a native `EventSource` (it uses `fetch` so it can send the
`X-Api-Key` header), so the `id` line is only used by the server-side
resume path — but keep emitting it for future native-EventSource
consumers.

## Clone-token cache (security-sensitive)

Private repos need an access token to `git clone`. The wizard submits
it; the token MUST NOT land in the persisted submission, the session
transcript, or any event log — Mewbo sessions are visible in
Langfuse/Mongo and we treat the transcript as semi-public.

The cache is `CloneTokenCache` in `mewbo_graph.wiki.tokens` — a zero-dependency
atomic class both the API (writer) and the relocated clone/finalize tools
(readers) import **down** (Gitea #25 moved it out of `jobs.py` so the relocated
tools no longer reach up). The flow:

1. The wizard submission arrives as `{ ..., token: "..." }`.
2. `jobs.py` strips `token` before persisting the submission — the stored
   object never contains it.
3. `WikiIndexingJob.start` stashes the plaintext via `CloneTokenCache.store`
   (a class-level `dict[job_id → token]`) — in-process only, never serialised.
4. `clone.py` reads it (`CloneTokenCache.peek`) for the `git clone`.
5. `finalize.py` reads it again to authenticate the repo-description API fetch
   (private Gitea/GHE instances reject anon API calls).
6. `CloneTokenCache.forget(job_id)` clears the entry at end of finalize.

`peek` is **non-evicting** (`.get`, not `.pop`) so multiple consumers (clone,
finalize, refresh) each read it; `forget` is the only delete. A new consumer
goes BEFORE the `forget`.

Never log the token. Never echo it into a tool result. Never include it
in `submission.model_dump()` output.

## Embeddings — LiteLLM, not LangChain

`embedder.py` wraps `litellm.embedding(...)`. We do **not** depend on
`langchain-openai`. The reason:

- `langchain-openai 1.x` requires `openai>=2.26.0`.
- `litellm` requires `openai==2.24.0` exactly.
- The version pins are mutually incompatible, so installing both leaves
  one of them broken at import time.

LiteLLM is already the project's canonical LLM client (chat completions
ride it through `build_chat_model`), and it supports embeddings via
`litellm.embedding(model=..., input=..., api_base=..., api_key=...)`.
This drops a 30-package transitive dep and removes the conflict.

The configured embedding model (`wiki.embedding.model`) MUST resolve to
a name LiteLLM routes through our OpenAI-compatible proxy. Bare names
like `gemini-embedding-001` dispatch directly to a provider SDK
(Vertex AI for that one) and bypass the proxy entirely. `Embedder`
prepends `openai/` to any model name without a `/` so the LiteLLM
router takes the OpenAI-compatible path against `llm.api_base`. This
mirrors the `LLMConfig.proxy_model_prefix` rule chat models use.

Embedding failure is non-fatal — `build_graph.py` catches it, emits a
warn-level log, and falls back to BM25-only retrieval. Don't change
this to a hard fail; some operators run against proxies that don't
expose any embedding model and the wiki still produces useful pages.

## prune_pages at finalize — slug-drift dedup

Each re-index emits a new page plan. LLM stochasticity means the same
topic may get a slightly different slug (`auth-and-pairing` vs
`authentication-and-session-security`). Without intervention, every
re-index accumulates duplicates alongside the previous run's pages.

`finalize.py` calls `ctx.store.prune_pages(slug, keep)` where `keep`
is `{plan_page_ids} | {landing_page_id}`. The Mongo backend overrides
with a single `delete_many({slug, page_id: {$nin: list(keep)}})` for
atomicity; the JSON backend uses the default per-page delete loop.

If you need to keep a page across runs that isn't in the committed
plan (e.g. a hand-pinned "intro"), add it to the `keep` set in
`finalize.py` — don't disable pruning.

## Description fetch + private TLDs

`_fetch_description` in `finalize.py` calls the platform's public API
(GitHub `/repos`, Gitea `/api/v1/repos`, GitLab `/projects`,
Bitbucket `/repositories`). On a token-less refresh against a private
host, the call returns "". To avoid blowing away a previously-saved
description with an empty string, finalize reads the existing project
record and keeps its `desc` when the fetch yields nothing.

`_is_private_host` (from `clone.py`) is reused to disable TLS
verification for `.home`, `.local`, `.internal`, `.lan`, `.intranet`,
`.corp` TLDs — same carve-out the git clone uses. Don't add other
TLDs without a strong reason; cert verification matters for the public
internet.

## KG endpoint (atomic class)

`graph.py:KnowledgeGraphView` is the read-side atomic class for the
`/v1/wiki/projects/<slug>/graph` route. Frozen dataclass with slots;
class method `for_slug(store, slug, *, node_limit=None)` reads from
the store and constructs the immutable view; instance method
`to_wire()` returns the JSON payload the FE consumes. The static
helpers `_node_to_wire` / `_edge_to_wire` are private and live on the
class so adding a new node/edge attribute only touches one file.

The FE renderer (`KnowledgeGraphRenderer` in the console) follows the
same atomic-class shape — both halves of the contract are explicit
instead of leaking through anonymous dicts.

## SessionRuntime session tags

Wiki sessions are tagged so the API can resolve them by job id without
storing extra mappings:

| Surface | Tag                              |
|---------|----------------------------------|
| Indexer | `wiki:job:<job_id>`              |
| Q&A     | `wiki:qa:<answer_id>`            |

`runtime.resolve_session(session_tag=...)` upserts the session and
attaches the tag, so a process restart can still find the running
session if the SSE stream is reopened. Use the same tag in both
producer (start) and consumer (resume) code paths — if you invent a
different tag in `routes.py`, the session won't reattach.

## Testing notes

- Tests live in `tests/wiki/`. Mock at the I/O boundary:
  `litellm.embedding` for embedder tests, a JSON store fixture for
  store/route tests.
- `test_tool_finalize.py` covers prune_pages-at-finalize and
  description-keeps-existing behaviour. Mock `_fetch_description` via
  `patch.object` when you don't want real HTTP.
- `test_embedder.py` mocks `mewbo_graph.wiki.embedder.litellm.embedding`
  — do NOT mock `OpenAIEmbeddings`, that import was removed.
- Wiki tests should NEVER spawn a real LLM or hit a real proxy.

## When in doubt

Read `apps/mewbo_console/src/components/wiki/README.md` (the FE-side
spec) — it documents the wire shapes verbatim and is the source of
truth for the API contract between BE and FE.

## Multiplex memory + docs overlay

An evolving memory + docs graph (`memory_types.py`, `memory.py`,
`refresh.py`, `structure_provider.py`) overlaid on the tree-sitter code
graph. Non-obvious decisions only (full spec + research refs: Gitea #13):

- **One identity for all three layers**: `entity_key = file#Qualified.Name`
  (bare `path` for a File; NO byte offsets, so anchors survive a re-index).
  `structure_provider.entity_key_for_node` is the ONLY derivation; the
  byte-offset `_stable_id` stays an internal handle. `StructureProvider` is a
  Protocol (corpus-agnostic seam: PDFs/DB schemas later); keep
  `CodeStructureProvider` stateless — a refresh mutates the graph.
- **Atomic, content-addressed notes**: `MemoryNode.content` ≤200 chars, one
  claim; `node_id = sha1(slug|content.strip().lower())[:16]` is *derived* and
  overwrites any supplied value — that IS the exact-dup dedup tier. Don't add a
  random/byte-offset id (Dense-X / Molecular-Facts: long notes decontextualize).
- **One ingestor, three surfaces**: `InsightIngestor.from_store` backs the
  SessionTool `wiki_submit_insight`, REST `POST .../insights`, and MCP
  `submit_insight`. 3-tier dedup: exact node_id → fuzzy Jaccard → LLM over
  cosine-kNN, NONE-default (uncertainty/no-LLM → NEW; Mem0). The in-session
  tool is deterministic (no LLM); raw human text condenses on the REST/MCP
  path. Merge keeps the *crisper* note (never concatenates) and retires the
  superseded node. **All dedup tiers route through the single
  `memory_vector_search` ANN seam** — upgrading that seam makes dedup sublinear.
- **Invalidate-don't-delete** (Graphiti): validity is the single nullable
  `MemoryEdge.invalid_at`; NO node-level flag. A memory is live iff it has ≥1
  live ANCHORS edge.
- **Retrieval is additive**: `MultiplexExpander` seeds notes by cosine →
  ANCHORS → code + ≤`expansion_hops` neighbours, GAAMA `0.1·ppr + 1.0·sim`,
  hub-damp deg>`hub_degree`. `memory_expand=False` is byte-identical to legacy.
- **Refresh is on-demand only** (no watcher/cron). `ChangeDetector` =
  content-hash vs `FileManifest` (mtime is unreliable). `GraphDeltaIndexer`:
  retract → reparse → Salsa early-cutoff → reverse-dependency closure. **GOTCHA**:
  cross-file CALLS/IMPORTS/EXTENDS targets are *synthetic* ids
  (`_stable_id(slug,"Function",name,"<external>",0)`), so the closure matches by
  NAME via those ids — over-approximating on purpose (false positive = safe
  wasted work; false negative = unsafe stale index). `MemoryReconciler` drift
  ladder per anchor: ≥`drift_keep` keep / <`drift_invalidate` invalidate / band
  → 1 LLM call; idempotent via `anchor_checked_at`; `override`-labelled notes
  immutable. `DocStalenessPlanner` maps each page to a `DocPageNote` and scores
  `0.5·direct + 0.3·drift + 0.2·deleted` (drift=0 in v1 — pages aren't embedded).
  `RefreshOrchestrator` is the plan-then-act conductor; `RefreshReport` is the
  committed scope. **Not yet wired**: `jobs.refresh()` currently runs a *full*
  re-index — `RefreshOrchestrator` is ready substrate (in `mewbo_graph.wiki.refresh`)
  with no production caller. Don't re-add a `refresh(mode=...)` knob until the
  orchestrator is actually wired (an earlier `mode` param was deleted because all
  branches did the same full re-index); wire it and reintroduce `mode` together.
- **Flywheel**: the indexer deposits a few atomic insights *while indexing*
  (A-MEM notes-at-ingest), QA deposits one per answer — so memory is useful
  from day one, not only after Q&A.
- **REST insights returns 200 (`ok:false`), NOT 422**, on a fully-rejected
  well-formed request — so the MCP `RestClient` facade doesn't raise.
- **Config**: `wiki.memory.*` / `wiki.refresh.*` (typed `WikiMemoryConfig` /
  `WikiRefreshConfig` in `config.py`); layer gated on `wiki.memory.enabled`.
  `vector_search` / `memory_vector_search` is the documented scale seam (IVF /
  Matryoshka / quantization land behind it) — keep the signature stable.
