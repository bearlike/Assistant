> ↑ [apps/mewbo_api/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md)

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
a fixed seven-phase state machine running inside a normal Mewbo session
(not a separate service): an agent owns the run, the wiki built-in
tools persist state, and SSE streams progress to the FE.

Phases, in order, are the source of truth for progress everywhere
(`enrich` is inserted post-AST):

```
clone → scan → graph → enrich → plan → pages → finalize
```

**GraphRAG ordering law (Gitea #35).** The knowledge graph is built BEFORE
generation and generation CONSUMES it. The `enrich` phase (a `wiki-enricher`
fan-out, mirroring `wiki-page-writer`) mints abstract entities from AST symbols +
SOURCE prose (docstrings/comments/READMEs) — never from generated page prose —
grounding each LLM-proposed entity against the high-confidence AST symbols
(precision; anything that can't attach to a symbol/span is dropped). `plan` is
entity-aware; `pages` query the entity graph via `resolve_entity` instead of
re-extracting. We SKIP Leiden/Louvain community detection (over-engineering,
non-reproducible on low-degree code graphs) — pages are planned by the free
AST/module/package/directory hierarchy + entity co-occurrence. The entity
substrate itself lives down in `mewbo_graph.entities` (one multiplex store,
deterministic-id upsert, one `ResolutionLadder` shared with insight dedup).

`emit_phase(ctx, name)` is the one and only writer of the current
phase. It writes the SSE event AND updates the persisted job snapshot
in the same call — that's why the landing-page card and the indexing
page can never drift apart (they read the same write through two
different transports).

Each phase has exactly one emitter at its boundary tool — EXCEPT `enrich`,
which has no tool of its own (it's a `wiki-enricher` fan-out). So `enrich` is
emitted at the **tail of `wiki_build_graph`** (`build_graph.py`, right after the
graph is built): the snapshot advances into the enrichment window the moment the
graph is done, instead of sitting at `graph` until `plan` lands (the old
~2-minute "graph plateau" that read as a stall). If you add another tool-less
logical phase, emit it from the tail of the tool that precedes it.

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

Wiki agent definitions (`wiki-indexer`, `wiki-enricher`, `wiki-page-writer`,
`wiki-qa`, `wiki-qa-probe`) are loaded by `agent_registry.py` only when the
session advertises the `"wiki"` capability. `WikiIndexingJob.start` and
`WikiQaSession.start` append `{"client_capabilities": ["wiki"]}` as a context
event right after creating the session. Without that line, `spawn_agent` cannot
look up the wiki-* AgentDefs and the run will appear "stuck after scan" — the
parent agent finishes scan but has no child it can hand the rest off to.

If you ever rename a wiki capability or add a new one, update both
`jobs.py` (capability advertisement) and `agent_registry.py` (gate).

## Non-git catalog ingestion

`CatalogIngestor` (`mewbo_graph.wiki.catalog`) — direct write (no agent/tree-sitter):
each doc → `WikiPage` (BM25) + a content-addressed graph node (embeddings, guarded →
BM25-only) → honest `complete` Project (non-empty graph). Catalog nodes reuse
`type=File` with `file="catalog/"` prefix (`doc_total` counts by that prefix; a
dedicated `"Document"` node type + FE Record-map update is a deferred follow-up).
Refresh rejects catalog projects (`repo_url is None` AND no git submission).

## Q&A model default + snapshot terminal status (#41)

`post_qa` makes `model` genuinely optional via `_resolve_qa_model()` (the one
helper for the `wiki.default_qa_model → wiki.default_model → llm.default_model`
chain, reused by `get_meta`/`get_wiki_defaults`/`_build_condense_model` — don't
re-inline it). The route accepts `project` OR `slug` in the body but reports
validation against the PUBLIC `project` name.

`QaAnswer.status` (`mewbo_graph.wiki.types`, default `"running"`,
`QA_TERMINAL_STATUSES = {complete, cancelled, error}`) is the terminal flag a
NON-streaming consumer needs (the MCP `ask_wiki` poll over `GET /v1/wiki/qa/<id>`
— the SSE stream already had its `complete` event). It is set at each accept
state: the terminal `sources` block (`emit_block._finalize_snapshot` →
`status="complete"`) and `WikiQaSession.cancel` (`status="cancelled"`). Set it
through `store.save_qa(answer)` so both store backends round-trip it onto the
snapshot. Any NEW QA terminal path MUST set the status too, or a snapshot poller
waits out its timeout.

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

## Repository credential persistence (durable, redacted in-flight)

The ephemeral `CloneTokenCache` dies with the process — so re-index used to
reconstruct a `token=None` submission and the clone failed with
`fatal: could not read Username for '<host>'`. Credentials are now **persisted
per-slug** in an isolated store, separate from job submissions:

- `RepoCredential` (`mewbo_graph.wiki.types`) — `{kind: token|ssh_key, value,
  username?}`. Supports git tokens AND SSH/deploy private keys.
- `CredentialStore` (`mewbo_graph.wiki.credentials`) — the single read/write
  chokepoint, keyed by **slug** (durable identity, not job_id). Plaintext-now
  behind an identity `_encode`/`_decode` seam — encryption-at-rest is a one-line
  swap there, nothing else changes.
- Store: `save_credentials`/`get_credentials`/`delete_credentials` on
  `WikiStoreBase`; JSON driver writes `credentials/<slug>.json` at mode `0600`,
  Mongo uses the `wiki_credentials` collection.
- **Plaintext-at-rest ≠ plaintext-in-flight**: NEVER log `RepoCredential.value`,
  echo it into an SSE event, a transcript, or a tool result. The clone error
  scrubber + the credential store are the only places it appears.

Clone credential resolution order: **LLM arg → `CloneTokenCache` (warm) →
`CredentialStore.load(slug)` (durable source of truth)**. Token → URL injection
(`x-access-token`). SSH key → temp file (`0600`) + `GIT_SSH_COMMAND="ssh -i
<tmp> -o StrictHostKeyChecking=accept-new"`, deleted in a `finally`.

Onboard (`jobs.start`) saves the credential BEFORE stripping the token; refresh
(`jobs.refresh`) restores it onto the reconstructed submission (THE line that
fixes token-less re-index); finalize keeps `CloneTokenCache.forget` but NEVER
deletes the persisted credential (re-index needs it); project-delete drops it.

## Restart durability is checkpoint-aware resume (Gitea #54, Part B)

`init_wiki` no longer marks interrupted jobs failed. `JobRecovery`
(`recovery.py`) finds recoverable jobs on startup (`_RECOVERABLE` =
`queued|scanning|finalizing|interrupted`), marks the non-`interrupted` ones
`interrupted`, and re-drives **`WikiResume.resume`** ONCE per distinct slug —
the restored credential authenticates the re-clone. `interrupted` is itself in
the recoverable set: if the API died after marking a job `interrupted` but
before recovery re-drove it, the next restart must still retry it. A SLUG-KEYED
retry cap (`JobRecovery.MAX_RETRIES`, on its own persistent surface via
`store.{get,bump,reset}_recovery_attempts` — `recovery/<slug>.json` /
`wiki_recovery` collection, NOT the submission sidecar) bounds the AUTOMATIC
re-drives across recovery generations / new job_ids and stops a job that keeps
dying from looping the API. `interrupted` is a NON-terminal status (it shows in
the "Indexing now" active-jobs surface).

**Checkpoint-aware resume, not full refresh (the #54 reversal of the old
"recovery == refresh" rule).** `WikiResume` (`resume.py`) reuses the SAME job_id
(continuous event log), re-clones at the recorded `commit_sha` (NOT latest HEAD,
so the reused graph stays consistent — re-indexing at HEAD is the distinct
`/refresh` path), and SKIPS the expensive idempotent phases whose store artifacts
already exist. The "what's done" decision is the atomic `ResumePlan`
(`mewbo_graph.wiki.resume`): `build(store, job)` computes `skip ⊆ {graph, enrich,
plan}` (graph non-empty → skip graph; entities exist → skip enrich; committed
plan → skip plan) + `pages_done`/`pages_remaining` (plan minus persisted pages).
`clone`/`scan` ALWAYS run (cheap; the page-writers need the source on disk);
`finalize` always runs (idempotent). It is computed ONCE at resume time and
persisted via `store.save_resume_plan(job_id, …)`; `resolve_job_ctx` rebuilds it
cheaply per tool call (`ResumePlan.from_persisted` — a tiny dict, no graph
re-query) onto `WikiJobCtx.resume_plan`. The phase tools (`build_graph`,
`commit_plan`) consult it with a ONE-LINE `ctx.resume_plan.should_skip(...)` guard
and short-circuit (still `emit_phase`, return a cached-summary result); done-
detection lives ONLY in `ResumePlan` (DRY). The enrich fan-out has no tool, so the
agent skips it via `ResumePlan.summary()` injected into the indexer instruction.
The shared "create wiki session + advertise the `wiki` capability + start the
indexer with INDEXER_TOOLS" sequence is the `_start_indexer_session` seam in
`jobs.py` — used by BOTH `start()` and `resume()` so the capability advertisement
and tool allowlist can never drift. User-initiated resume
(`POST /v1/wiki/index/<job_id>/resume`) is exempt from / resets the per-slug cap
(`reset_recovery_attempts`); the AUTOMATIC `JobRecovery` path keeps it
(`user_initiated=False`). `GET /v1/wiki/jobs/recoverable` lists non-complete jobs
whose `ResumePlan` has reusable work. (`submit_page` is already idempotent, so a
re-submitted done page is harmless.)

When a slug **exhausts** `JobRecovery.MAX_RETRIES`, recovery now moves its job to
terminal **`failed`** (`_mark_failed`) instead of leaving it `interrupted`
forever — a job that keeps dying must stop being a perpetual zombie in the
active-jobs surface (that ghost is what made a completed project keep showing
"Indexing now" — the FE suppresses a completed tile while its slug has any active
job, `LandingScreen` `activeSlugs`).

## Terminal accept-state for indexing (Gitea #58)

`wiki_finalize` is a **terminal `SessionTool`** (overrides `should_terminate_run()` /
`terminal_reason()`). On a successful `handle()` it sets `_terminate_run_pending = True`;
the loop polls this at `tool_use_loop.py:689-696` and breaks immediately —
`done_reason = "completed"` — with **no extra post-finalize LLM turn**. This is
the primary fix for the hanging session: before this, the loop always took one
more turn after finalize (the model had to produce a text response to exit
naturally), which consumed ~100K tokens per successful index AND created a window
where a stuck child could wedge `asyncio.run(loop.run(...))` before terminal events
were ever written.

**Mirror this pattern** for any new wiki tool that signals end-of-index (if you
add a `wiki_publish` or similar). The base `WikiSessionTool.should_terminate_run()`
always returns `False` — override it only where the tool IS the terminal accept state.

## Infra-failure recovery net (Gitea #56)

`WikiIndexingSessionEndHook` (registered in `routes.register()` alongside
`QaSessionEndHook`) fires on every session end. If the backing wiki job is
non-terminal (status not in `{complete, failed, cancelled}`) when the session ends,
it marks the job `interrupted` — handing off to `JobRecovery` on next restart via
the existing checkpoint-aware `WikiResume` path.

This catches tool-internal infra failures (network/IO/timeout inside a phase tool)
where the LLM catches the error, reports it, and exits cleanly (`done_reason =
"completed"` with an error field). Without this hook the job would stay in its
last phase status forever, invisible to `JobRecovery` and un-resumable without
a manual re-submit.

Happy-path invariant: `wiki_finalize` sets job status to `complete` before the
session ends, so the hook always sees a terminal status and no-ops. The hook only
fires for error/interrupted paths.

## Honest terminal job state (no zombie "still indexing")

The wiki job status is only advanced by the tools the indexer calls (`clone`→
`scanning`, `commit_plan`→`finalizing`, `finalize`→`complete`); a session that
ends WITHOUT reaching `wiki_finalize` (e.g. `halted_no_progress`) leaves the job
non-terminal. Two guards keep state honest:

- **Supersede at finalize** (`finalize.py:_supersede_stale_jobs`): a `complete`
  index marks every *other* non-terminal job for the same slug `failed`
  (superseded) — so older stuck attempts drop out of `/jobs/active` and stop
  hiding the finished wiki.
- **Completion correctness** (`finalize.py:_graph_is_populated`): finalize
  REFUSES to mark `complete` when the knowledge graph is empty ("completed
  without creating the graph" ⇒ `failed`, code `validation`) — soft-gated so a
  graph-less install isn't blocked. "Error AFTER the graph was built" stays a
  distinct `failed`-with-populated-graph state.

The `on_session_end` seam is now WIRED (the real `hook_manager` threads
`backend.py` → `init_wiki` → `register` → `WikiQaSession.start` → `start_async`).
Q&A uses it as its terminal net (see below); indexing still advances status via its
own tools (`wiki_finalize`) + the supersede/recovery guards above, so a halted index
is covered without an indexing-side session-end reconciler.

## Grounded-structured slug resolution (#51)

`resolve_qa_ctx` falls back to the `structured_workspace` context event → a slug-only
`WikiQaCtx` (`answer_id` Optional — retrieval tools need only `slug`; emit/QA tools
guard on `answer_id`). The session store is reached via a **process-singleton in
`_ctx`**, NEVER `create_session_store()` per tool call (that leaked a Mongo connection
pool and added per-call latency against the sub-1.5s budget).

## Q&A — agentic probe fan-out + terminal submission

`wiki-qa` is a **hypervisor, not a flat retrieval agent**. The old design was one
capped agent that read a couple of pages and stopped — it never touched the graph or
embeddings the index built (a `halted_no_progress`/page-only-citation smell). Now the
root decomposes the question and fans out `wiki-qa-probe` sub-agents (the existing
`spawn_agent`/`check_agents` hypervisor — NO new control loop), fuses their grounded
findings, and emits one cited answer. The ANN multi-probe intuition (diverse seeds →
best-first beam over typed edges → consensus → early-stop) is instrumented **in the
probe prompt**, not a deterministic engine — the orchestrator IS the prober. Durable
decisions:

- **Root has NO retrieval tools by design** (`QA_TOOLS` = list_pages/emit/insight +
  spawn/check). That's what FORCES delegation; giving the root the retrieval surface is
  exactly how it regressed to read-one-page-and-stop. The probe leaf
  (`wiki-qa-probe.md`) owns the read-only retrieval surface.
- **`approval_callback` is inherited parent→child**, so `_approve_qa_tool` must admit
  `QA_APPROVED_TOOLS` = root tools ∪ the probe's retrieval tools ∪ `steer_agent`. Admit
  only the root set and every probe call falls ASK→DENY — the fan-out silently does
  nothing. This is the non-obvious gotcha.
- **The terminal `sources` block IS the accept state** (the `EmitStructuredResponseTool`
  pattern): `wiki_emit_block` of a `sources` block drives `QaFinalizer.close` (reconcile
  snapshot + `complete`) and sets `should_terminate_run()`. So completion is a clean
  LLM-native submission, not a hook reconstruction. `QaSessionEndHook` (on_session_end)
  is the NET for a run that halts before the sources block (+ stamps `models_used`).
- **`QaFinalizer` lives DOWN in `mewbo_graph.wiki.qa`** (with `QaAnswer` + the store),
  so both the terminal emit (same layer) and the API net (imports down) call it. It
  rebuilds `blocks` + `summary_sources` from the append-only log — previously NOTHING
  did, so a reloaded/shared answer came back empty (`blocks=[]`) and the SSE stream only
  ended by idle-timeout.
- **Two kinds of citation, captured deterministically.** `summary_sources` = the LLM's
  curated sources block. `accessed_sources` = the full trail of every graph node / file
  / page a probe TOUCHED — each retrieval tool records a compact `access` event
  (`WikiSessionTool._record_qa_access`); the finalizer folds + de-dups them. `models_used`
  is the only provenance needing the transport layer (the transcript's `llm_call` model),
  so `QaSessionEndHook` reads it and `QaFinalizer.enrich` stamps it post-close.

## Q&A answer depth + cited-sources viewer

Answer depth is **prompt-controlled, not code-controlled** — `QaFinalizer` never
truncates; it passes blocks straight from the event log. Two recurring-regression
guards live in the prompts (`mewbo_graph/.../agents/wiki-qa.md`,
`wiki-qa-probe.md`):

- **Structured-output floor.** `wiki-qa.md` MANDATES minimum structure (a lead
  `p` direct answer + ≥1 `h2` facet section + ≥2 supporting `p`, then the
  `sources` block) for non-trivial questions; only a yes/no or single-value
  lookup may be one paragraph. Without an explicit floor the model reads "quick +
  authoritative" as "be brief" and emits one paragraph + `sources` — the
  DeepWiki-parity regression. "Quick" means LATENCY (don't spawn marginal extra
  probes), never answer brevity. The probe contract was un-capped (the old
  "2–5 terse claims" starved the fused answer). Only emit kinds in the
  `types.py` block union (`p/h2/h3/hr/ul/accordion/sources/table/diagram` — NO
  `ol`; express ordered lists as markdown prefixes inside a `ul`/`p`).
- **Inline citations = `src:` links.** The prompt emits
  `[path:line](src:path#L<a>-<b>)`; probes MUST pass `start_line`/`end_line` to
  `wiki_read_file` so the citation carries a precise range (a bare path can't open
  the right lines). The console renders these as chips + a source card.

**Two #70 citation/provenance fixes — both at a single deterministic seam:**

- **Page citations are re-schemed at the EMIT seam.** The QA agent sometimes
  cites a wiki PAGE as a bare path; the console's `fileCitations` then treats it
  as a source FILE and the `SourceCard` 404s `/source` (pages live in the page
  store, not the clone). `wiki_emit_block` runs the `sources` block through
  `QaFinalizer.tag_page_citations` (`mewbo_graph.wiki.qa`) BEFORE it lands on the
  log — a bare ref whose id ∈ the real page set becomes `wiki:<page-id>`. One seam
  fixes the LIVE stream AND the reconciled snapshot together; the FE already drops
  `wiki:` from the file cards. File / `path#L…` / `graph:` refs pass through.
- **Retrieve-details hashes are resolved at READ.** `accessed_sources` records
  graph nodes as `graph:<node_id>` (content-addressed sha1 — opaque in the panel).
  `GET /v1/wiki/qa/<id>` humanises them via `AccessedSourceResolver.resolve_refs`
  (`qa.py`): AST node → `file#Symbol` (one `query_graph` pass), entity →
  `name (type)` (`get_entity`), miss → `unknown (<hash[:8]>)`. **NON-destructive**
  — the stored snapshot keeps raw ids because `QaMemoryDepositor` anchors off them
  (`graph:<id>` → `entity_key`); resolving at read also stays current across a
  re-index. The FE wire stays `string[]` (no FE change).

**Source-blob endpoint** `GET /v1/wiki/projects/<slug>/source?path=&start=&end=`
→ `{path,startLine,endLine,totalLines,content}` (`routes.py:get_project_source`).
Backs the FE cited-sources viewer: the console parses each `path#L<a>-<b>`
citation and fetches its excerpt LAZILY per card — excerpts are deliberately NOT
carried on the SSE wire (keeps stream payloads small + the `sources` block shape
unchanged). Reuses `WikiSourceAccess._safe_path` (the load-bearing traversal
guard — absolute/`..`/symlink escapes 403) + `resolve_qa_clone_dir`; whole-file
reads cap at `_SOURCE_MAX_LINES` while `totalLines` still reports the true count.

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

**The view is the multiplex ASSEMBLER (not AST-only).** `for_slug` reads all
three store families — AST (`query_graph`/`list_edges`), entities
(`query_entities`/`list_entity_edges`), memory (`query_memory`/`list_memory_edges`)
— and tags every node/edge `layer ∈ ast|entity|memory|cross`. Cross-layer
`ANCHORS` edges are reconciled IN-VIEW via the EXISTING resolvers
(`CodeStructureProvider.resolve_many` for `path#Name` keys,
`EntityAnchorResolver.resolve_many` for `entity:<id>`): pre-split the key kinds
(mixing them defeats `resolve_many`'s early-exit), classify an edge target by
SET-MEMBERSHIP against the loaded id-sets (NEVER id hex-length), drop the
unresolvable (no dangling edges). Cross-file AST edges re-point to real in-repo
nodes or converge on ONE shared NAMED `External` view-node (synthesized, never
persisted) — that is what fixed the "disconnected File-stars": the old view
dropped cross-file edges whose synthetic `<external>` target wasn't a real node.
`node_limit` degree-prunes the AST layer ONLY; `truncated`/`totalEdges` count
post-resolution reals. Open-vocab entity-relation verbs ride the edge `label`;
`kind` stays a closed union (`RELATES`/`ANCHORS`) so the FE `Record` maps stay
exhaustive.

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
  (A-MEM notes-at-ingest), and QA deposits one per answer — so memory is useful
  from day one. The QA half is `QaMemoryDepositor` (`mewbo_graph.wiki.qa`, beside
  `QaFinalizer`), fired from `QaSessionEndHook` (post-delivery → OFF the user's
  latency path), best-effort, idempotent (content-addressed node id), skips an
  empty slug. It reuses `InsightIngestor.ingest(condense=True)` — no second
  fan-out, no new memory writer — anchoring the distilled answer to the cited
  code entities from `accessed_sources`/`summary_sources`.
- **REST insights returns 200 (`ok:false`), NOT 422**, on a fully-rejected
  well-formed request — so the MCP `RestClient` facade doesn't raise.
- **Config**: `wiki.memory.*` / `wiki.refresh.*` (typed `WikiMemoryConfig` /
  `WikiRefreshConfig` in `config.py`); layer gated on `wiki.memory.enabled`.
  `vector_search` / `memory_vector_search` is the documented scale seam (IVF /
  Matryoshka / quantization land behind it) — keep the signature stable.
