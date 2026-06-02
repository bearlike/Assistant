# MewboWiki Backend — Design Spec

**Date:** 2026-05-14
**Worktree branch:** `grove/deepwiki-style-gen-20260514-055235`
**Tracking:** [Gitea issue #5](https://git.hurricane.home/bearlike/Assistant/issues/5) · [PR #6](https://git.hurricane.home/bearlike/Assistant/pulls/6)

## 1 Background

The console ships a complete DeepWiki-style `/wiki/*` surface — landing, configure wizard, indexing loader, wiki page, Q&A streaming view — wired against a mock client at `apps/mewbo_console/src/components/wiki/api/client.ts`. The wire contract is fixed by `api/types.ts` and the SSE event unions therein (`IndexingEvent`, `QaEvent`).

Nothing on the backend is implemented. This spec plumbs:

- a real indexing pipeline (clone → scan → AST graph + embeddings → page generation),
- a hybrid retrieval Q&A loop,
- persistence for projects/pages/jobs/QA/notify queue/code graph,
- two SSE endpoints whose shapes mirror the existing `/api/sessions/{id}/stream` pattern.

We do **not** invent new wire shapes. The single source of truth remains `api/types.ts`.

## 2 Goals & non-goals

**Goals**

- Faithful satisfaction of every endpoint + event shape in issue #5.
- Maximum reuse of Mewbo orchestration primitives — the indexing pipeline IS a Mewbo session.
- Multi-turn, tool-call-driven generation visible in the existing agent tree (showcase LLM use).
- Optional dependency footprint — users who don't want the wiki should pay nothing.
- Class-state + functional-methods structure throughout (atomic Pydantic atoms, behavior on focused classes).

**Non-goals**

- Replacing `ToolUseLoop` / `SessionRuntime`.
- Building a generic vector DB or graph DB. We persist in MongoDB / JSON; vector ops happen in Python.
- Real-time incremental indexing on push. Refresh = full re-index.
- Multi-language exhaustiveness in v1. v1 ships Python + JS/TS/Go/Rust tree-sitter queries; rest follows the same pattern.
- Server-side mermaid rendering. The indexer emits ` ```mermaid` fences verbatim.

## 3 Architecture overview

`POST /v1/wiki/index` becomes a Mewbo session tagged `wiki:job:<jobId>`. A root LLM agent runs the **indexing playbook** using a small set of deterministic wiki tools. Each tool writes public-shaped `IndexingEvent` records directly to a per-job append-only log; the SSE endpoint polls that log — verbatim mirror of `/api/sessions/{id}/stream`.

```
POST /v1/wiki/index
  └─► WikiIndexingJob.start(submission)
        ├─ create job (status=queued)
        ├─ session_id = SessionRuntime.resolve_session(session_tag=f"wiki:job:{jobId}")
        └─ SessionRuntime.start_async(
               user_query="Index <slug> per playbook",
               allowed_tools=INDEXER_TOOLS,
               skill_instructions=<indexer_agent.md>,
               cwd=<temp clone dir>,
           )

Root LLM (multi-turn):
  1  wiki_clone_repo(url, ref, token)        → queued{totalCount}
  2  wiki_scan_tree(filter_mode, dirs, files)→ scanning|scanned per file
  3  wiki_load_grounder()                    → .mewbo/wiki.json or .devin/wiki.json (or null)
  4  wiki_build_graph()                      → tree-sitter parse, persist graph + embeddings
  5  wiki_query_graph(...)                   → optional: clusters / top modules
  6  wiki_commit_plan(pages)                 → finalizing{}
  7  spawn_agent × N (parallel, non-blocking root spawn)
  8  check_agents(wait=true)                 → await all
  9  wiki_finalize(landingPageId)            → complete{landingPageId, pageCount}

Per-page sub-agent (constrained allowed_tools):
   read_file · glob · grep · code_search · wiki_query_graph · wiki_submit_page
   Multi-turn: gather → synthesize → submit_page → terminate.
```

Q&A uses the same scaffolding: `POST /v1/wiki/qa` opens a session tagged `wiki:qa:<answerId>` with a different tool subset.

## 4 Module layout

```
packages/mewbo_core/src/mewbo_core/builtin_plugins/wiki/    # LLM-facing
  ├ indexer_agent.md / qa_agent.md          AgentDefs (system prompts)
  ├ clone.py · scan.py · grounder.py        one Tool class per file
  ├ build_graph.py · query_graph.py
  ├ commit_plan.py · submit_page.py · finalize.py
  ├ search_pages.py · read_page.py · code_search.py · emit_block.py
  └ __init__.py                              register agents + tools

apps/mewbo_api/src/mewbo_api/wiki/           # storage + routes
  ├ types.py                                 Pydantic mirrors of api/types.ts
  ├ store.py                                 WikiStoreBase + MongoWikiStore + JsonWikiStore + factory
  ├ graph.py                                 class GraphIndex  (tree-sitter wrapper)
  ├ embedder.py                              class Embedder    (LiteLLM batch + cosine)
  ├ retriever.py                             class HybridRetriever (BM25 + vec + graph + RRF)
  ├ jobs.py                                  class WikiIndexingJob, class WikiQaSession
  ├ events.py                                SSE generator (verbatim pattern from /sessions stream)
  └ routes.py                                Flask-RESTX namespace, 13 endpoints

apps/mewbo_console/src/components/wiki/api/
  └ client.ts                                swap mock for real HTTP/SSE (one-file change)
```

Atomic Pydantic atoms (state-only): `Project`, `WizardSubmission`, `IndexingJob`, `IndexingEvent`, `QaEvent`, `QaAnswer`, `WikiPage`, `PagePlan`, `GraphNode`, `GraphEdge`, `Embedding`, `WikiError`.

Behavior classes (state + focused methods): `WikiStoreBase` (+ Mongo/JSON impls), `WikiIndexingJob`, `WikiQaSession`, `GraphIndex`, `Embedder`, `HybridRetriever`, and one tool class per file.

## 5 Storage model

Mirrors `MongoSessionStore` + `SessionStore` factory:

```
wiki_projects          (slug PK)
wiki_pages             ((slug, page_id) compound PK)
wiki_jobs              (job_id PK; status, scanned/total, current_file, landing_page_id, error)
wiki_job_events        ((job_id, idx) compound; append-only)
wiki_qa                (answer_id PK)
wiki_qa_events         ((answer_id, idx) compound)
wiki_notify            ((slug, email) compound PK)
wiki_graph_nodes       ((slug, node_id) compound PK; type, name, file, range)
wiki_graph_edges       ((slug, source, target, type) compound)
wiki_embeddings        ((slug, node_id) compound PK; vector: float[])
```

JSON fallback under `$MEWBO_HOME/wiki/`: per-slug directories (`pages/*.md`, `graph/nodes.jsonl`, `graph/edges.jsonl`, `embeddings.jsonl`), per-job `events.jsonl`, per-answer `events.jsonl`. `create_wiki_store()` reads `storage.driver` from existing config — no new knob.

**Token handling:** `WizardSubmission.token` lives only in the in-memory clone call. Never persisted.

## 6 Knowledge graph + embeddings (GitNexus-inspired, KISS-scoped)

The user asked for GitNexus-shape semantics. We implement a scope-down: same idea, ~5% of the surface area.

| GitNexus | Mewbo wiki v1 | Why |
|---|---|---|
| 44 node types | **6** (File, Module, Class, Function, Method, Interface) | Cover top 80% of code structure. |
| 21 relationship types | **5** (CONTAINS, IMPORTS, CALLS, EXTENDS, REFERENCES) | Same. |
| LadybugDB | MongoDB (graph in `wiki_graph_*` collections) | Reuse existing infra. |
| Snowflake arctic-embed-xs (local 384D) | LiteLLM batch embeddings (configurable; default `openai/text-embedding-3-small`, 1536D) | Reuses the project's existing LLM abstraction; no new model wheel. |
| 14 languages | **4 first** (Python, JS/TS, Go, Rust); pattern open for more | Tree-sitter queries are additive. |
| BM25 + semantic + RRF | Same | Standard hybrid IR pattern; `rank-bm25` is ~80 LOC dep. |
| Leiden community detection | Skipped in v1 (LLM does cluster reasoning over the manifest) | Add in v2 if quality demands it. |
| Worker pool, CSV streaming | Sequential async via `asyncio.gather` with semaphore = 8 | Repos we target (≤5K files) don't need a worker pool. |

**Performance budget:** 1K-file repo → ~30s graph build + embedding, plus LLM page generation (≈2-15 min depending on depth). Acceptable.

**Pipeline (inside `wiki_build_graph` tool):**

```
for each scanned file:
    detect language → parse with tree-sitter → extract nodes + edges via per-language query
batch nodes by chunk_count, embed via LiteLLM (configurable model, default text-embedding-3-small)
persist all to wiki_graph_*, wiki_embeddings
```

**Hybrid retrieval (`HybridRetriever.search(slug, query, k)`):**

```
1. embed(query)                                       → qvec
2. cosine top-K vec_hits over wiki_embeddings
3. BM25 top-K bm25_hits over (page bodies | graph node docstrings)
4. RRF fuse(vec_hits, bm25_hits, k=k)                 → ranked candidates
5. optional graph expand: include 1-hop CALLS/IMPORTS neighbors
6. return top-k with snippets
```

Used by both `wiki_search_pages` (for page-level retrieval) and `code_search` (for symbol-level).

## 7 Indexing flow — public event ordering

Tool implementations enforce the contract by construction:

| Tool | Public events emitted |
|---|---|
| `wiki_clone_repo` | `queued{totalCount}` |
| `wiki_scan_tree` | `scanning`+`scanned` per file (batched ~10/s; `heartbeat` every 20s) |
| `wiki_load_grounder` | — |
| `wiki_build_graph` | — (internal progress only) |
| `wiki_commit_plan` | `finalizing{scannedCount, totalCount}` |
| `wiki_submit_page` | — (counter only) |
| `wiki_finalize` | `complete{landingPageId, pageCount}` |
| job cancellation path | `cancelled` |
| any tool failure | `error{WikiError}` |

The LLM owns *what* to do; tools own *what events to emit*. Contract violations are impossible.

## 8 Grounder contract

The indexer agent always calls `wiki_load_grounder()` first. Lookup order in the cloned working tree:

1. `.mewbo/wiki.json` (canonical, MewboWiki-native)
2. `.devin/wiki.json` (compat — works with existing repos)

Shape (compatible with the existing `.devin/wiki.json` in this repo):

```json
{
  "repo_notes": [{ "content": "Free-form guidance the indexer should honor" }],
  "pages": [
    {
      "title": "Overview & Repo Map",
      "purpose": "What the page should cover",
      "parent": "Optional parent page title"
    }
  ]
}
```

When present, `repo_notes[].content` is injected into the indexer's system prompt and `pages[]` is adopted verbatim (titles slugified to `pageId`). When absent, the indexer plans its own pages by reasoning over the scan manifest + graph clusters.

## 9 Q&A flow

```
POST /v1/wiki/qa { question, fromPageId, model }
  └─► WikiQaSession.start(...)
        ├─ create answer record (answerId)
        └─ start_async(user_query=question, allowed_tools=QA_TOOLS, ...)

Root LLM:
  1. wiki_search_pages(query)            → top page IDs + sources
  2. wiki_read_page(pageId) × few        → gather context
  3. code_search(query) (optional)       → symbol-level evidence
  4. emit meta + summary_ready events    (auto-fired by first emit_block / by SSE bootstrap)
  5. wiki_emit_block × N                 → block_open / block_delta / block_close
  6. completion                          → complete event
```

`meta` and `summary_ready` events are emitted automatically when the QA session bootstraps — before the LLM's first tool call — so the UI flips out of skeleton state immediately.

## 10 Streaming, cancellation, resume

SSE endpoint generator (`apps/mewbo_api/.../wiki/events.py`) mirrors `backend.py:1219`:

```python
def generate():
    last_idx = 0
    while True:
        events = store.load_job_events(job_id, after_idx=last_idx)
        for ev in events:
            yield f"event: {ev['type']}\ndata: {json.dumps({k:v for k,v in ev.items() if k!='type'})}\n\n"
            last_idx = ev["idx"]
        if terminal(events):
            break
        if idle_since > 20s: yield heartbeat
        time.sleep(0.5)
```

**Cancellation:** `DELETE /v1/wiki/index/<jobId>` → calls `SessionRuntime.cancel_run(session_id)` (existing API) + appends `cancelled` event. Client AbortSignal alone just closes the SSE (server-side job continues per contract).

**Resume:** Reconnect re-emits `queued` (from event log idx=0), replays `scanned` events for already-completed files, resumes live.

## 11 Optional dependency footprint

Wiki feature is **opt-in** at install time.

`apps/mewbo_api/pyproject.toml`:

```toml
[project.optional-dependencies]
wiki = [
    "tree-sitter>=0.21",
    "tree-sitter-language-pack>=0.3",
    "rank-bm25>=0.2.2",
    "numpy>=1.26",
]
```

Install: `uv sync --extra wiki`. Without it, `routes.py` returns `503 Service Unavailable` with a `WikiError{code:"internal", hint:"install with `pip install mewbo-api[wiki]`"}` shape. The detection lives in one place (`_check_wiki_extras()` at module import) and gates the entire `/v1/wiki/*` namespace.

The wiki backend itself imports defensively (`try: import tree_sitter` at the top of the relevant modules); the API blueprint registers the routes only when extras are present.

## 12 Config instrumentation

New `wiki` section in `configs/app.example.json` + `app.schema.json`:

```json
"wiki": {
  "enabled": false,
  "clone_dir": "",
  "indexing": {
    "default_depth": "comprehensive",
    "default_language": "en",
    "max_files": 10000,
    "max_file_size_kb": 512,
    "planner_model": "",
    "page_model": "",
    "max_parallel_pages": 5
  },
  "embedding": {
    "enabled": true,
    "model": "openai/text-embedding-3-small",
    "batch_size": 64,
    "dimensions": 1536
  },
  "qa": {
    "default_model": "",
    "retrieval_k": 6,
    "answer_ttl_days": 30
  },
  "qa_grounder_paths": [".mewbo/wiki.json", ".devin/wiki.json"]
}
```

All numeric/string values have sane defaults read by `get_config_value("wiki", ...)` (existing helper).

## 13 Frontend swap

`apps/mewbo_console/src/components/wiki/api/client.ts` — replace each mock function with real HTTP/SSE calls. Hooks (`api/hooks.ts`), screens, components, types: **unchanged**. Two mock files (`mocks/fixtures.ts`, `mocks/pages.ts`) get deleted.

The SSE consumer becomes an `EventSource` (or `fetch` reader for `POST + stream`) that yields the same `AsyncIterable<IndexingEvent>` / `AsyncIterable<QaEvent>` the mock currently yields. AbortSignal honored throughout.

## 14 Error handling

`WikiError` (Pydantic) mirrors the frontend type. HTTP status mapping:

| code | status |
|---|---|
| `not_found` | 404 |
| `forbidden` | 403 |
| `repo_access` | 502 |
| `quota_exceeded` / `rate_limited` | 429 (+ `Retry-After`) |
| `validation` | 400 (+ `fields`) |
| `cancelled` | 499 |
| `internal` | 500 |

Errors raised from inside a job are translated to a terminal `{type:"error", error:<WikiError>}` event and the job record updates to `status="failed"`.

## 15 Testing strategy

| Layer | Strategy |
|---|---|
| Pydantic types | Round-trip tests against fixtures matching `api/types.ts`. |
| Stores | Mongo via `mongomock`, JSON via `tmp_path`. Test ordering, idempotency, indexes. |
| Graph + Embedder | Real tree-sitter on small fixture repos; mock LiteLLM. Assert nodes/edges count + embedding dims. |
| HybridRetriever | Fixed embeddings + bodies; assert RRF rank ordering. |
| Indexing job | Stub LLM via `AsyncMock`; assert exact `IndexingEvent` sequence on the event log. |
| QA session | Same pattern — assert `QaEvent` ordering + block shape conformance. |
| Routes | Flask test client; full happy path + cancel + 404 + validation errors. |
| E2E | Playwright against `docker compose up` (Mongo + API + console); index a small fixture repo, browse, ask. |

## 16 Phasing

1. **Storage + types + routes (snapshot fallback).** Project/page CRUD + indexing job snapshot polling + QA snapshot. SSE stubbed.
2. **Indexing tools + agent + SSE.** Clone/scan/grounder/commit_plan/submit_page/finalize. Event log + SSE generator. No graph yet — page plan from scan manifest only.
3. **Graph + embeddings + hybrid retrieval.** Tree-sitter Python first; extend to JS/TS/Go/Rust. `code_search` and `wiki_search_pages` use hybrid retrieval.
4. **QA agent + streaming.** QA tools + SSE QA stream. Snapshot endpoint for shareable URLs.
5. **Console wiring.** Swap `client.ts` for real HTTP/SSE. Delete mocks.
6. **Polish.** Refresh/notify/delete endpoints. Heartbeat + resume. Cancellation propagation tests.
7. **E2E.** Docker compose smoke test via Playwright against a small fixture repo.

## 17 Open follow-ups (post-merge)

- Leiden / Louvain community detection (replace LLM cluster reasoning).
- Incremental re-index on push.
- Auth scoping when user identity ships.
- Add more languages to tree-sitter pipeline (Ruby, Java, C/C++, Swift, Kotlin, Dart).
