# MewboWiki Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Source-of-truth context (read before any task):**
> - Design spec: `docs/specs/2026-05-14-deepwiki-style-gen-design.md`
> - Frontend wire contract: `apps/mewbo_console/src/components/wiki/api/types.ts`
> - Gitea handoff: `https://git.hurricane.home/bearlike/Assistant/issues/5`
> - CLAUDE.md files (root + `apps/mewbo_api/` + `tests/` for engineering principles)

**Goal:** Plumb a real DeepWiki-style backend behind the console's existing `/wiki/*` flow — indexing pipeline, hybrid-retrieval Q&A, persistence, SSE — satisfying issue #5's wire contract verbatim while reusing Mewbo's orchestration primitives.

**Architecture:** A `POST /v1/wiki/index` becomes a Mewbo session tagged `wiki:job:<id>`. A root LLM agent calls deterministic wiki tools (clone/scan/build_graph/commit_plan/spawn per-page agents/finalize); each tool emits public-shaped `IndexingEvent`s to a per-job append-only log. SSE endpoint polls the log — verbatim mirror of `/api/sessions/{id}/stream`. Q&A uses the same scaffolding. Knowledge graph is GitNexus-scope-downed (6 nodes / 5 edges / 4 languages / hybrid BM25+vec+RRF). Optional install extras: `mewbo-api[wiki]`.

**Tech Stack:** Pydantic v2 · Flask-RESTX · MongoDB (+ JSON fallback) · LiteLLM (existing) · tree-sitter + `tree-sitter-language-pack` · `rank-bm25` · numpy · pytest + `mongomock` · Playwright (E2E).

---

## File Structure

### New files

```
packages/mewbo_core/src/mewbo_core/builtin_plugins/wiki/
  __init__.py                  register agents + register tools at plugin load
  indexer_agent.md             AgentDef: indexing playbook system prompt
  qa_agent.md                  AgentDef: QA playbook system prompt
  clone.py                     WikiCloneRepoTool
  scan.py                      WikiScanTreeTool
  grounder.py                  WikiLoadGrounderTool
  build_graph.py               WikiBuildGraphTool
  query_graph.py               WikiQueryGraphTool
  commit_plan.py               WikiCommitPlanTool
  submit_page.py               WikiSubmitPageTool
  finalize.py                  WikiFinalizeTool
  search_pages.py              WikiSearchPagesTool
  read_page.py                 WikiReadPageTool
  code_search.py               WikiCodeSearchTool
  emit_block.py                WikiEmitBlockTool
  _ctx.py                      shared per-session job context resolver

apps/mewbo_api/src/mewbo_api/wiki/
  __init__.py                  init_wiki(app, runtime) — register blueprint if extras present
  README.md                    colocated design (link to docs/specs/...)
  types.py                     Pydantic mirrors of frontend types.ts
  store.py                     WikiStoreBase + JsonWikiStore + MongoWikiStore + create_wiki_store()
  graph.py                     GraphIndex (tree-sitter wrapper + per-language queries)
  embedder.py                  Embedder (LiteLLM batch + cosine)
  retriever.py                 HybridRetriever (BM25 + vec + RRF + graph expand)
  jobs.py                      WikiIndexingJob, WikiQaSession
  events.py                    SSE generator (mirror of session stream)
  routes.py                    Flask-RESTX namespace (13 endpoints)
  errors.py                    WikiError → HTTP mapping + Flask error handlers

tests/wiki/
  __init__.py
  fixtures/                    tiny fixture repos (Python + JS) for tree-sitter + indexing tests
  test_types.py
  test_store_json.py
  test_store_mongo.py          (uses mongomock)
  test_graph.py
  test_embedder.py
  test_retriever.py
  test_jobs.py
  test_qa_session.py
  test_routes.py               Flask test client; full happy/cancel/error paths
  test_grounder.py
  test_event_ordering.py

apps/mewbo_console/__tests__/wiki/
  client.test.tsx              swap-in client unit tests (mocked fetch + EventSource)

docs/plans/                    (this file)
docs/specs/                    (design doc — already committed)

configs/app.example.json       + wiki block
configs/app.schema.json        + wiki schema
docker.env.example             no change (Mongo already there)
```

### Modified files

```
apps/mewbo_api/pyproject.toml                  add [project.optional-dependencies].wiki
apps/mewbo_api/src/mewbo_api/backend.py        wire init_wiki(app, runtime) after init_channels
apps/mewbo_console/src/components/wiki/api/client.ts
                                               replace mock impl with real HTTP/SSE
apps/mewbo_console/src/components/wiki/mocks/{fixtures.ts,pages.ts}
                                               delete (dead code after swap)
configs/app.example.json                       + wiki section (see design §12)
configs/app.schema.json                        + wiki section schema
```

---

## Phase 1 — Types, storage, snapshot routes

Goal: ship the data layer + the read-side (project/page CRUD + indexing-job snapshot + QA snapshot) without any LLM involvement. Front-end mock can already be swapped for a polling fallback at the end of this phase.

### Task 1.1: Add optional `wiki` dependency block

**Files:**
- Modify: `apps/mewbo_api/pyproject.toml`

- [ ] **Step 1: Append `wiki` extras to pyproject**

Add a `[project.optional-dependencies]` table (or extend the existing one) so users can opt in via `uv sync --extra wiki`:

```toml
[project.optional-dependencies]
wiki = [
    "tree-sitter>=0.21",
    "tree-sitter-language-pack>=0.3",
    "rank-bm25>=0.2.2",
    "numpy>=1.26",
]
```

- [ ] **Step 2: Install + verify**

Run: `uv sync --extra wiki`
Expected: extras resolve and lockfile updates.

- [ ] **Step 3: Commit**

```bash
git add apps/mewbo_api/pyproject.toml uv.lock
git commit -m "🔧 chore(api): add optional wiki extras (tree-sitter, rank-bm25, numpy)"
```

### Task 1.2: Pydantic wire-shape mirrors

**Files:**
- Create: `apps/mewbo_api/src/mewbo_api/wiki/__init__.py` (placeholder)
- Create: `apps/mewbo_api/src/mewbo_api/wiki/types.py`
- Create: `tests/wiki/__init__.py`
- Create: `tests/wiki/test_types.py`

- [ ] **Step 1: Write failing roundtrip tests**

`tests/wiki/test_types.py` — assert each model parses & re-serialises the fixtures from `api/types.ts` (canonical examples for each event union variant, `WikiPage`, `IndexingJob`, etc.). Use `model_dump(mode="json")` and compare. `ConfigDict(extra="forbid")` must reject unknown keys.

- [ ] **Step 2: Run test, expect import error**

Run: `pytest tests/wiki/test_types.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `wiki/types.py`**

Define Pydantic models *exact* to the frontend `api/types.ts`:

- `Project`, `Platform`, `Language` (wizard catalogues).
- `WizardSubmission` with `FilterMode`, `Depth` literals.
- `IndexingJob` with `IndexingStatus` literal.
- `WikiError` with the 9-code literal + `fields` + `retryAfter`.
- `Block` (discriminated union by `kind`) and `InlineNode` types.
- `WikiPage` (with `frontmatter`, `body`, `toc`, `nav`).
- Discriminated `IndexingEvent` union (8 variants by `type`).
- Discriminated `QaEvent` union (8 variants by `type`).
- `PagePlan` (internal — not in api/types.ts): `{id, title, description, importance, relevantFiles, relatedPages, parent?}`.
- `GraphNode`, `GraphEdge`, `Embedding` (internal).

Every model: `ConfigDict(extra="forbid", populate_by_name=True)`. Use `Field(alias=...)` to match camelCase wire names (`jobId`, `scannedCount`, `totalCount`, `currentFile`, `landingPageId`, `pageCount`, `fromPageId`).

- [ ] **Step 4: Run test to verify pass**

Run: `pytest tests/wiki/test_types.py -v`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add apps/mewbo_api/src/mewbo_api/wiki/__init__.py \
        apps/mewbo_api/src/mewbo_api/wiki/types.py \
        tests/wiki/__init__.py tests/wiki/test_types.py
git commit -m "✨ feat(api/wiki): Pydantic mirrors of frontend api/types.ts contract"
```

### Task 1.3: `WikiStoreBase` ABC + `JsonWikiStore`

**Files:**
- Create: `apps/mewbo_api/src/mewbo_api/wiki/store.py`
- Create: `tests/wiki/test_store_json.py`

- [ ] **Step 1: Write failing JSON-store tests**

Cover: create/get/list/delete project; save/get/list pages; create/update job; append/load job events (idx monotonic); cancel job; save/get qa; append/load qa events; notify add/pop; round-trip survives process restart (re-instantiate store, data persists).

- [ ] **Step 2: Run, expect ImportError**

Run: `pytest tests/wiki/test_store_json.py -v`

- [ ] **Step 3: Implement `WikiStoreBase` (ABC) + `JsonWikiStore`**

`store.py`:
- `class WikiStoreBase(abc.ABC):` declare every method named in design §5 (projects/pages/jobs/job_events/qa/qa_events/notify/graph stubs).
- `class JsonWikiStore(WikiStoreBase):` writes under `$MEWBO_HOME/wiki/`. Layout: `projects/<slug>.json`, `pages/<slug>/<pageId>.md` (frontmatter+body) plus `pages/<slug>/index.json` for the nav, `jobs/<jobId>/job.json` + `jobs/<jobId>/events.jsonl`, `qa/<answerId>/answer.json` + `qa/<answerId>/events.jsonl`, `notify/<slug>.jsonl`. Append-only files via `open(..., "a")` and a per-store `_lock = threading.Lock()`. Returns `idx` (0-based, monotonic) for appended events.
- Graph & embeddings methods raise `NotImplementedError` for now (filled in Phase 3).
- `create_wiki_store()` factory: reads `storage.driver` via `get_config_value("storage", "driver", default="json")`; returns `JsonWikiStore` by default. Mongo branch raises `NotImplementedError` until Task 1.4.

- [ ] **Step 4: Run + verify pass**

Run: `pytest tests/wiki/test_store_json.py -v`

- [ ] **Step 5: Commit**

```bash
git add apps/mewbo_api/src/mewbo_api/wiki/store.py tests/wiki/test_store_json.py
git commit -m "✨ feat(api/wiki): WikiStoreBase ABC + JsonWikiStore impl + factory"
```

### Task 1.4: `MongoWikiStore`

**Files:**
- Modify: `apps/mewbo_api/src/mewbo_api/wiki/store.py`
- Create: `tests/wiki/test_store_mongo.py`

- [ ] **Step 1: Write failing Mongo-store tests using mongomock**

Mirror the JSON tests (run the same suite via parametrization is ideal — single fixture-factory; same test body parametrised over `("json", "mongo")`). Use `mongomock.MongoClient()` directly; instantiate `MongoWikiStore(client, "test_db")`.

- [ ] **Step 2: Run, expect NotImplementedError**

Run: `pytest tests/wiki/test_store_mongo.py -v`

- [ ] **Step 3: Implement `MongoWikiStore`**

- Collections per design §5: `wiki_projects`, `wiki_pages`, `wiki_jobs`, `wiki_job_events`, `wiki_qa`, `wiki_qa_events`, `wiki_notify`, `wiki_graph_nodes`, `wiki_graph_edges`, `wiki_embeddings`.
- `_ensure_indexes()` (idempotent, called from `__init__`) creates: `(slug)` unique on `wiki_projects`; `(slug, page_id)` unique on `wiki_pages`; `(job_id)` unique on `wiki_jobs`; `(job_id, idx)` on `wiki_job_events`; `(answer_id, idx)` on `wiki_qa_events`; `(slug, email)` unique on `wiki_notify`; `(slug, node_id)` unique on `wiki_graph_nodes`/`wiki_embeddings`; `(slug, source, target, type)` on `wiki_graph_edges`.
- Event append uses Mongo's `$inc` counter on the parent job doc to compute the next `idx` atomically; events store `{job_id, idx, type, ...}`.
- Graph & embeddings methods still `NotImplementedError` (Phase 3).
- Factory updated to instantiate `MongoWikiStore` when `storage.driver == "mongodb"`, sharing the existing `storage.mongodb.{uri,database}` config.

- [ ] **Step 4: Run + verify pass**

Run: `pytest tests/wiki/test_store_mongo.py -v`

- [ ] **Step 5: Commit**

```bash
git add apps/mewbo_api/src/mewbo_api/wiki/store.py tests/wiki/test_store_mongo.py
git commit -m "✨ feat(api/wiki): MongoWikiStore + factory + idempotent indexes"
```

### Task 1.5: Read-side routes (snapshot polling)

**Files:**
- Create: `apps/mewbo_api/src/mewbo_api/wiki/errors.py`
- Create: `apps/mewbo_api/src/mewbo_api/wiki/routes.py`
- Modify: `apps/mewbo_api/src/mewbo_api/wiki/__init__.py`
- Create: `tests/wiki/test_routes.py`

- [ ] **Step 1: Write failing route tests (snapshot endpoints only)**

Use Flask's test client. Cover:
- `GET /v1/wiki/projects` → 200, `[Project]`
- `GET /v1/wiki/projects/<slug>/pages/<pageId>` → 200 with WikiPage; 404 for missing slug/page
- `DELETE /v1/wiki/projects/<slug>` → 200 `{deleted: true}` (idempotent → second call returns `{deleted: false}`)
- `GET /v1/wiki/platforms`, `GET /v1/wiki/languages`, `GET /v1/wiki/models` → 200 with seeded catalogue (re-use frontend mocks/fixtures.ts content as constants in `apps/mewbo_api/.../wiki/catalogues.py`)
- `GET /v1/wiki/index/<jobId>` snapshot → 200 IndexingJob; 404 for unknown
- `GET /v1/wiki/qa/<answerId>` snapshot → 200 QaAnswer; 404 for unknown
- All endpoints require `X-Api-Key`; 401 without.

- [ ] **Step 2: Run, expect failures**

Run: `pytest tests/wiki/test_routes.py -v`

- [ ] **Step 3: Implement `errors.py`**

```python
class WikiHTTPError(Exception):
    def __init__(self, error: WikiError, status: int): ...

def wiki_error_response(err: WikiError, status: int) -> tuple[dict, int]: ...

# Module-level error→status map per design §14.
```

Register a Flask error handler in `init_wiki()` that translates `WikiHTTPError`.

- [ ] **Step 4: Implement `routes.py` (read-side only)**

Flask-RESTX `Namespace("wiki", path="/v1/wiki")`. One Resource class per route. Each method: auth-guard (re-use existing `_require_api_key` from `backend.py`), delegate to `runtime.wiki_store` (we'll attach the store to runtime in Step 5), translate exceptions.

- [ ] **Step 5: Implement `__init__.py` integration**

```python
# apps/mewbo_api/src/mewbo_api/wiki/__init__.py
def init_wiki(app, runtime) -> bool:
    try:
        from .store import create_wiki_store
        runtime.wiki_store = create_wiki_store()
    except ImportError:
        return False  # extras not installed
    from .routes import register
    register(app, runtime)
    return True
```

`backend.py` calls `init_wiki(app, runtime)` after `init_channels(...)`. When the extras aren't installed, the routes simply don't mount.

- [ ] **Step 6: Run + verify pass**

Run: `pytest tests/wiki/test_routes.py -v`

- [ ] **Step 7: Commit**

```bash
git add apps/mewbo_api/src/mewbo_api/wiki/errors.py \
        apps/mewbo_api/src/mewbo_api/wiki/routes.py \
        apps/mewbo_api/src/mewbo_api/wiki/__init__.py \
        apps/mewbo_api/src/mewbo_api/wiki/catalogues.py \
        apps/mewbo_api/src/mewbo_api/backend.py \
        tests/wiki/test_routes.py
git commit -m "✨ feat(api/wiki): snapshot routes (projects, pages, catalogues, job/qa snapshots)"
```

### Task 1.6: Config + schema entries

**Files:**
- Modify: `configs/app.example.json`
- Modify: `configs/app.schema.json`

- [ ] **Step 1: Add `wiki` block to `app.example.json`** per design §12 (defaults: `enabled: false`, `embedding.enabled: true`, `embedding.model: "openai/text-embedding-3-small"`, etc.)

- [ ] **Step 2: Add matching JSON schema to `app.schema.json`** (object with all fields, none required, restrictive types).

- [ ] **Step 3: Commit**

```bash
git add configs/app.example.json configs/app.schema.json
git commit -m "🔧 chore(config): add wiki section to app.example.json + schema"
```

---

## Phase 2 — Indexing pipeline (no graph yet)

Goal: end-to-end indexing flow visible in the console with real LLM page generation. Graph + embeddings deferred; page plan derived from the scan manifest only.

### Task 2.1: Per-session job context resolver

**Files:**
- Create: `packages/mewbo_core/src/mewbo_core/builtin_plugins/wiki/_ctx.py`
- Create: `packages/mewbo_core/src/mewbo_core/builtin_plugins/wiki/__init__.py`

- [ ] **Step 1: Define context model + resolver**

```python
# _ctx.py
@dataclass(frozen=True)
class WikiJobCtx:
    job_id: str
    slug: str
    submission: WizardSubmission
    clone_dir: Path
    store: WikiStoreBase

def resolve_job_ctx(session_id: str, runtime) -> WikiJobCtx:
    """Look up the wiki job for this session via session_tag 'wiki:job:<id>'."""
    ...

@dataclass(frozen=True)
class WikiQaCtx:
    answer_id: str
    slug: str
    question: str
    from_page_id: str
    store: WikiStoreBase

def resolve_qa_ctx(session_id: str, runtime) -> WikiQaCtx: ...
```

- [ ] **Step 2: Register plugin shell in `__init__.py`**

The wiki built-in plugin contributes both agents and tools. Match `widget_builder/__init__.py` style — `register_skills(catalog)` / `register_agents(registry)` / `register_tools(registry)` lifecycle hooks invoked during session init.

- [ ] **Step 3: Commit (no tests yet — wired by Tasks 2.2-2.8)**

```bash
git add packages/mewbo_core/src/mewbo_core/builtin_plugins/wiki/
git commit -m "✨ feat(core/wiki): builtin plugin shell + per-session ctx resolvers"
```

### Task 2.2: `WikiCloneRepoTool`

**Files:**
- Create: `packages/mewbo_core/src/mewbo_core/builtin_plugins/wiki/clone.py`
- Modify: `packages/mewbo_core/src/mewbo_core/builtin_plugins/wiki/__init__.py`
- Create: `tests/wiki/test_tool_clone.py`

- [ ] **Step 1: Write failing test**

Use `pytest-subprocess` (already an indirect dep via mewbo's existing shell tool tests) or `unittest.mock.patch('subprocess.run')` to stub `git`. Assert: successful clone updates `IndexingJob.total_count`, appends `queued` event with `totalCount`. Failed clone (non-zero exit) appends terminal `error` event with `WikiError{code:"repo_access"}`. Token is forwarded via URL rewriting (`https://x-access-token:<token>@host/...`) and NOT persisted.

- [ ] **Step 2: Run, expect ImportError**

Run: `pytest tests/wiki/test_tool_clone.py -v`

- [ ] **Step 3: Implement `WikiCloneRepoTool`**

```python
class WikiCloneRepoTool(AbstractTool):
    tool_id = "wiki_clone_repo"
    description = "Shallow-clone the configured repo into the session cwd."
    schema = { ... }  # OpenAI-style JSON schema: url, ref?, token?

    async def execute(self, *, url, ref=None, token=None, _ctx: WikiJobCtx) -> dict:
        ...
```

`tool_id`, `operation` and `tool_input` field names per CLAUDE.md invariants. Returns `{"totalCount": int, "ref": str, "head": str}`. Emits `queued{jobId, slug, totalCount}` to `store.append_job_event`.

- [ ] **Step 4: Run + verify pass**

Run: `pytest tests/wiki/test_tool_clone.py -v`

- [ ] **Step 5: Commit**

```bash
git add packages/mewbo_core/src/mewbo_core/builtin_plugins/wiki/clone.py \
        packages/mewbo_core/src/mewbo_core/builtin_plugins/wiki/__init__.py \
        tests/wiki/test_tool_clone.py
git commit -m "✨ feat(core/wiki): WikiCloneRepoTool with queued event emission"
```

### Task 2.3: `WikiScanTreeTool`

**Files:**
- Create: `packages/mewbo_core/src/mewbo_core/builtin_plugins/wiki/scan.py`
- Modify: `.../wiki/__init__.py`
- Create: `tests/wiki/test_tool_scan.py`

- [ ] **Step 1: Failing test**

Fixture tree under `tests/wiki/fixtures/tiny_repo/` (10 files, mix of Python + markdown + .gitignore-style files + a directory to be excluded). Assert: applies filter_mode/dirs/files; emits `scanning` then `scanned` per included file in plan order; indexes are monotonic from 1..N; `totalCount` from the clone event is honoured; returns a manifest `{files: [{path, size, ext}, ...]}`.

- [ ] **Step 2: Run** → ImportError.

- [ ] **Step 3: Implement** — pathlib walk with include/exclude predicates. Use `aniso8601.parse_datetime` for `ts`. Batched event flush every 50ms or 10 events (whichever first) to avoid overwhelming Mongo.

- [ ] **Step 4: Run + verify pass.**

- [ ] **Step 5: Commit**

```bash
git commit -m "✨ feat(core/wiki): WikiScanTreeTool with scanning/scanned event ordering"
```

### Task 2.4: `WikiLoadGrounderTool`

**Files:**
- Create: `.../wiki/grounder.py`
- Modify: `.../wiki/__init__.py`
- Create: `tests/wiki/test_tool_grounder.py`

- [ ] **Step 1: Failing test**

Fixture cases:
- Repo with `.mewbo/wiki.json` → returns parsed.
- Repo with `.devin/wiki.json` only → returns parsed (compat).
- Repo with both → `.mewbo/wiki.json` wins.
- Repo with neither → returns `None`.
- Malformed JSON → raises `WikiError{code:"validation"}` with `fields.path`.

- [ ] **Step 2: Run** → ImportError.

- [ ] **Step 3: Implement.** Pydantic model `WikiGrounder(BaseModel)` with `repo_notes`, `pages`. Pages contain optional `parent` for nesting. Default `parent: None` becomes top-level.

- [ ] **Step 4: Run + verify.**

- [ ] **Step 5: Commit**

```bash
git commit -m "✨ feat(core/wiki): WikiLoadGrounderTool (.mewbo/wiki.json → .devin/wiki.json fallback)"
```

### Task 2.5: `WikiCommitPlanTool` + `WikiSubmitPageTool` + `WikiFinalizeTool`

**Files:**
- Create: `.../wiki/commit_plan.py`, `.../wiki/submit_page.py`, `.../wiki/finalize.py`
- Modify: `.../wiki/__init__.py`
- Create: `tests/wiki/test_tool_commit_plan.py`, `tests/wiki/test_tool_submit_page.py`, `tests/wiki/test_tool_finalize.py`

- [ ] **Step 1: Failing tests** (one per tool)

`commit_plan`: validates `pages: PagePlan[]` shape; persists plan onto the job; emits `finalizing{scannedCount, totalCount}`; rejects empty list with `validation` error.

`submit_page`: validates frontmatter shape; renders markdown body as-is (already markdown); persists page; increments job's `submitted_pages` counter; idempotent re-submit overwrites. Verify mermaid fenced blocks pass through unchanged.

`finalize`: marks job `status="complete"`; persists project record (slug, source, lang, indexed_at, pages count, primary=false); pops the notify queue for the slug (returns the email list — actual email-send is wired in Phase 6); emits `complete{landingPageId, pageCount}`.

- [ ] **Step 2-4: TDD per tool.**

- [ ] **Step 5: Commit (one combined commit, since all three close out the deterministic state machine):**

```bash
git commit -m "✨ feat(core/wiki): commit_plan/submit_page/finalize tools (state-machine terminals)"
```

### Task 2.6: `indexer_agent.md` AgentDef

**Files:**
- Create: `packages/mewbo_core/src/mewbo_core/builtin_plugins/wiki/indexer_agent.md`
- Create: `tests/wiki/test_agent_indexer.py`

- [ ] **Step 1: Write the playbook**

Markdown with YAML frontmatter (existing `AgentDef` format per `agent_registry.py:87+`). Frontmatter:

```yaml
---
agent_id: wiki-indexer
name: Wiki Indexer
description: Generates a DeepWiki-style site for a code repository.
allowed_tools:
  - wiki_clone_repo
  - wiki_scan_tree
  - wiki_load_grounder
  - wiki_build_graph    # tolerate missing (Phase 3)
  - wiki_query_graph
  - wiki_commit_plan
  - wiki_submit_page
  - wiki_finalize
  - spawn_agent
  - check_agents
  - read_file
  - glob
  - grep
  - ls
model_hint: default
---
```

Body: bounded, concise playbook (≤200 lines). Phase-by-phase instructions:
1. Always call `wiki_clone_repo` first.
2. Call `wiki_load_grounder` next; if non-null, adopt `pages[]` verbatim, slugify titles for `pageId`.
3. Call `wiki_scan_tree` with submission's filter args.
4. (Phase 3 step — note as "if available") `wiki_build_graph`.
5. Construct `PagePlan[]` (use grounder if present + scan manifest).
6. `wiki_commit_plan(pages)`.
7. For each page: `spawn_agent(agent_type="wiki-page-writer", task=<focused task>, allowed_tools=["read_file","glob","grep","code_search","wiki_query_graph","wiki_submit_page"], acceptance_criteria=...)`. Non-blocking — root spawn (depth=0).
8. `check_agents(wait=true)` to await all.
9. `wiki_finalize(landingPageId=<first page id>)`.

Also write a `wiki-page-writer` AgentDef for the sub-agent (separate small markdown).

- [ ] **Step 2: Write test that loads + validates the agent def**

Asserts: AgentDef parses; `allowed_tools` matches expected list; body contains required milestones (regex check for tool call sequence keywords).

- [ ] **Step 3: Run + verify.**

- [ ] **Step 4: Commit**

```bash
git commit -m "✨ feat(core/wiki): indexer + page-writer AgentDefs"
```

### Task 2.7: `WikiIndexingJob` orchestrator class

**Files:**
- Create: `apps/mewbo_api/src/mewbo_api/wiki/jobs.py`
- Create: `tests/wiki/test_jobs.py`

- [ ] **Step 1: Failing test**

Mock LLM via `AsyncMock` on `mewbo_core.llm.build_chat_model`. Drive the model through the indexer playbook tool calls (clone → scan → grounder → commit_plan → spawn_agent × 2 → check_agents → finalize). Assert:
- POST creates job record with `status="queued"`.
- After `start()` returns, session is registered in `RunRegistry`.
- After the simulated run completes, event log has the exact ordered shape: `queued`, N×`scanning`/`scanned`, `finalizing`, `complete`.
- `WikiIndexingJob.cancel(jobId)` calls `runtime.cancel_run(session_id)` AND appends a `cancelled` event idempotently.
- `WikiIndexingJob.events_since(jobId, after_idx)` returns events with `idx > after_idx`.

- [ ] **Step 2-3: Implement.**

```python
class WikiIndexingJob:
    """Atomic indexing-job orchestrator. State lives in the store; this
    class is the thin Python facade for create/start/cancel/events."""

    @staticmethod
    def start(submission: WizardSubmission, *, runtime, store, hook_manager) -> IndexingJob:
        job = IndexingJob(jobId=uuid4().hex, slug=submission.slug, status="queued",
                          scannedCount=0, totalCount=0, currentFile=None)
        store.create_job(job)
        session_id = runtime.resolve_session(session_tag=f"wiki:job:{job.jobId}")
        # Attach job_id → session_id mapping so tools can resolve ctx
        store.attach_job_session(job.jobId, session_id)
        runtime.start_async(
            session_id=session_id,
            user_query=_render_indexer_query(submission),
            allowed_tools=INDEXER_TOOLS,
            skill_instructions=_load_indexer_playbook(),
            cwd=str(_clone_dir(job.jobId)),
            hook_manager=hook_manager,
            ...
        )
        return job

    @staticmethod
    def cancel(job_id: str, *, runtime, store) -> bool: ...

    @staticmethod
    def events_since(job_id: str, after_idx: int, *, store) -> list[IndexingEvent]: ...
```

- [ ] **Step 4: Run + verify.**

- [ ] **Step 5: Commit**

```bash
git commit -m "✨ feat(api/wiki): WikiIndexingJob orchestrator (Mewbo session under the hood)"
```

### Task 2.8: Indexing routes (POST/DELETE) + SSE

**Files:**
- Modify: `apps/mewbo_api/src/mewbo_api/wiki/routes.py`
- Create: `apps/mewbo_api/src/mewbo_api/wiki/events.py`
- Modify: `tests/wiki/test_routes.py` + `tests/wiki/test_event_ordering.py`

- [ ] **Step 1: Write SSE/event-ordering tests**

Spin up the Flask test client; POST `/v1/wiki/index` with a fixture submission; immediately open `GET /v1/wiki/index/<jobId>/stream`; assert event ordering (`queued`, …, `complete`). Use a stubbed LLM (same AsyncMock pattern as Task 2.7). Also cover the DELETE cancel path → `cancelled` terminal.

- [ ] **Step 2-3: Implement** the routes + SSE generator (verbatim mirror of `/sessions/<id>/stream` shape, but reads `wiki_job_events`). Encode as `event: <type>\ndata: <json-without-type>\n\n` per design §10.

Heartbeat every 20s when idle.

- [ ] **Step 4: Verify pass.**

- [ ] **Step 5: Commit**

```bash
git commit -m "✨ feat(api/wiki): POST/DELETE /index + SSE /index/<id>/stream with event ordering"
```

### Task 2.9: Console integration test against snapshot-fallback

**Files:**
- (no console code change yet — the mock still drives the UI)

- [ ] **Step 1: Manual smoke** — start `uv run mewbo-api` + `npm run dev` in `apps/mewbo_console`; hit `/wiki`; verify the catalogue endpoints respond and the wizard's model picker populates from the real API. Update `MEWBO_HOME` / `app.json` to use a temp dir.

- [ ] **Step 2: Document smoke command in `apps/mewbo_api/src/mewbo_api/wiki/README.md`** (one-liner section).

- [ ] **Step 3: Commit**

```bash
git commit -m "📝 docs(api/wiki): smoke-run instructions for Phase 2 verification"
```

---

## Phase 3 — Knowledge graph + embeddings + hybrid retrieval

Goal: graph + embedding sidecar to elevate page planning and Q&A quality.

### Task 3.1: `GraphIndex` — tree-sitter parsing for Python

**Files:**
- Create: `apps/mewbo_api/src/mewbo_api/wiki/graph.py`
- Create: `apps/mewbo_api/src/mewbo_api/wiki/graph_queries/`
  - `python.scm` — tree-sitter query (functions, classes, imports, calls, methods)
- Create: `tests/wiki/test_graph.py`

- [ ] **Step 1: Failing test**

Fixture: `tests/wiki/fixtures/tiny_python_repo/` (mini Python pkg with imports across files, class with method, function-calls function). Assert: `GraphIndex.parse_file("foo.py")` returns `GraphParseResult(nodes=[...], edges=[...])` with the expected counts (e.g., 3 functions, 1 class with 2 methods, 2 imports, 4 calls). Node IDs are stable hashes of `(file_path, type, name, byte_range_start)`.

- [ ] **Step 2-3: Implement**

```python
@dataclass(frozen=True)
class GraphNode:
    slug: str
    node_id: str
    type: Literal["File", "Module", "Class", "Function", "Method", "Interface"]
    name: str
    file: str
    range: tuple[int, int]  # byte range
    docstring: str | None = None

@dataclass(frozen=True)
class GraphEdge:
    slug: str
    source: str
    target: str
    type: Literal["CONTAINS", "IMPORTS", "CALLS", "EXTENDS", "REFERENCES"]

class GraphIndex:
    def __init__(self): ...
    def parse_file(self, slug: str, file_path: Path) -> GraphParseResult: ...
    def parse_repo(self, slug: str, root: Path, scan_manifest: list[ScanFile]) -> GraphParseResult: ...
    def persist(self, slug: str, result: GraphParseResult, *, store): ...
```

Use `tree_sitter_language_pack.get_language("python")` + the `python.scm` query file. Defensive import (`try: import tree_sitter` at module top; raise `WikiError{code:"internal", hint:"install mewbo-api[wiki]"}` if absent).

- [ ] **Step 4: Verify pass.**

- [ ] **Step 5: Commit**

```bash
git commit -m "✨ feat(api/wiki): GraphIndex with tree-sitter Python parser + 6 node / 5 edge types"
```

### Task 3.2: Add JS/TS/Go/Rust queries

**Files:**
- Create: `apps/mewbo_api/src/mewbo_api/wiki/graph_queries/{javascript,typescript,go,rust}.scm`
- Modify: `apps/mewbo_api/src/mewbo_api/wiki/graph.py`
- Modify: `tests/wiki/test_graph.py` (parameterize over languages, fixtures per lang)

- [ ] **Step 1: Failing tests** — one fixture per language, expected counts asserted.

- [ ] **Step 2-3: Add per-language scm files + language-routing logic in `GraphIndex.parse_file` (detect by extension; skip unsupported with a warning).**

- [ ] **Step 4: Verify all pass.**

- [ ] **Step 5: Commit**

```bash
git commit -m "✨ feat(api/wiki): multi-language tree-sitter queries (JS/TS/Go/Rust)"
```

### Task 3.3: `Embedder` — LiteLLM batch embeddings

**Files:**
- Create: `apps/mewbo_api/src/mewbo_api/wiki/embedder.py`
- Create: `tests/wiki/test_embedder.py`

- [ ] **Step 1: Failing test**

Mock LiteLLM's embedding endpoint (use `litellm.aembedding` AsyncMock). Assert: `Embedder.embed_nodes(["text1","text2",...], batch_size=64)` returns a list of `Embedding` objects with `vector: list[float]` of the configured dimension. Errors retry once then escalate as `WikiError{code:"internal"}`.

- [ ] **Step 2-3: Implement.** Model + dim + batch size read via `get_config_value("wiki","embedding",...)`. Cosine similarity helper as `staticmethod` on `Embedder`.

- [ ] **Step 4: Verify.**

- [ ] **Step 5: Commit**

```bash
git commit -m "✨ feat(api/wiki): Embedder (LiteLLM batch embed + cosine helper)"
```

### Task 3.4: Persistence: graph + embeddings store methods

**Files:**
- Modify: `apps/mewbo_api/src/mewbo_api/wiki/store.py`
- Modify: `tests/wiki/test_store_json.py`, `tests/wiki/test_store_mongo.py`

- [ ] **Step 1: Add tests** for `upsert_nodes`, `upsert_edges`, `upsert_embeddings`, `query_graph(slug, type?, name_match?, neighbors_of?)`, `vector_search(slug, qvec, k)`.

- [ ] **Step 2-3: Implement** in both backends:
- Json: append/replace under per-slug graph/ dir; vector_search loads all vectors into a numpy matrix in-process.
- Mongo: aggregation `$lookup` for neighbors; vector_search loads vectors via `find({slug})` then numpy cosine in-process (no Mongo vector index; we target <10K vectors per repo).

- [ ] **Step 4: Verify.**

- [ ] **Step 5: Commit**

```bash
git commit -m "✨ feat(api/wiki): WikiStore graph + embedding persistence (Json + Mongo)"
```

### Task 3.5: `HybridRetriever` — BM25 + vec + RRF

**Files:**
- Create: `apps/mewbo_api/src/mewbo_api/wiki/retriever.py`
- Create: `tests/wiki/test_retriever.py`

- [ ] **Step 1: Failing test**

Curated fixture: 6 documents with intentionally split lexical-vs-semantic relevance. Assert:
- BM25 top-K respects lexical overlap.
- Vector top-K respects pre-set embedding cosine.
- RRF fusion produces a deterministic blended ranking (assert specific top-3 order).
- Graph expansion adds 1-hop CALLS/IMPORTS neighbors.

- [ ] **Step 2-3: Implement.** `rank_bm25.BM25Okapi`. RRF: `score(d) = sum(1/(k+rank_i(d)))` with `k=60` (standard). Public API: `HybridRetriever.search(slug, query, k=10, types=None, graph_expand=False)`.

- [ ] **Step 4: Verify.**

- [ ] **Step 5: Commit**

```bash
git commit -m "✨ feat(api/wiki): HybridRetriever (BM25 + cosine + RRF + 1-hop graph expand)"
```

### Task 3.6: Wire `WikiBuildGraphTool` + `WikiQueryGraphTool`

**Files:**
- Create: `.../wiki/build_graph.py`
- Create: `.../wiki/query_graph.py`
- Create: `tests/wiki/test_tool_build_graph.py`, `tests/wiki/test_tool_query_graph.py`

- [ ] **Step 1: Failing tests.**

- [ ] **Step 2-3: Implement.** `wiki_build_graph` orchestrates `GraphIndex.parse_repo` + `Embedder.embed_nodes` + `store.upsert_*`. Returns a summary `{nodeCount, edgeCount, embeddedCount, languages: [...]}`. `wiki_query_graph` is a thin wrapper over `store.query_graph`.

- [ ] **Step 4: Verify.**

- [ ] **Step 5: Commit**

```bash
git commit -m "✨ feat(core/wiki): wiki_build_graph + wiki_query_graph tools wired to GraphIndex"
```

---

## Phase 4 — Q&A agent + streaming

### Task 4.1: `WikiSearchPagesTool` + `WikiReadPageTool` + `WikiCodeSearchTool` + `WikiEmitBlockTool`

**Files:**
- Create: `.../wiki/search_pages.py`, `.../wiki/read_page.py`, `.../wiki/code_search.py`, `.../wiki/emit_block.py`
- Create: `tests/wiki/test_qa_tools.py`

- [ ] **Step 1: Failing tests** for each.

- [ ] **Step 2-3: Implement.** `search_pages` uses `HybridRetriever.search(...)` over page bodies. `read_page` returns `WikiPage`. `code_search` uses `HybridRetriever.search(...)` with `types=["Function","Class","Method"]` and `graph_expand=True`. `emit_block` validates against `Block` model + appends events to `wiki_qa_events`.

- [ ] **Step 4: Verify.**

- [ ] **Step 5: Commit**

```bash
git commit -m "✨ feat(core/wiki): QA tools (search_pages, read_page, code_search, emit_block)"
```

### Task 4.2: `qa_agent.md` AgentDef

**Files:**
- Create: `.../wiki/qa_agent.md`
- Create: `tests/wiki/test_agent_qa.py`

- [ ] **Step 1: Write playbook + test.** Mirror Task 2.6 shape.

- [ ] **Step 2-3: Implement + verify.**

- [ ] **Step 4: Commit**

```bash
git commit -m "✨ feat(core/wiki): qa AgentDef with retrieval+block-emit playbook"
```

### Task 4.3: `WikiQaSession` + QA routes + SSE

**Files:**
- Modify: `apps/mewbo_api/src/mewbo_api/wiki/jobs.py`
- Modify: `apps/mewbo_api/src/mewbo_api/wiki/routes.py`
- Modify: `apps/mewbo_api/src/mewbo_api/wiki/events.py`
- Create: `tests/wiki/test_qa_session.py`

- [ ] **Step 1: Failing tests.** Cover: meta + summary_ready auto-emit on session bootstrap; block_open/delta/close ordering; complete terminal; shareable URL snapshot endpoint; cancel via DELETE.

- [ ] **Step 2-3: Implement** `WikiQaSession.start(question, fromPageId, model, slug, ...)`. The `meta` event is emitted by the route (before start_async), then `summary_ready` is emitted on the FIRST tool call (post-retrieval) — wrap via a one-shot hook. Both routes (`POST /v1/wiki/qa`, `POST /v1/wiki/qa/<id>/stream`, `DELETE /v1/wiki/qa/<id>`, snapshot `GET /v1/wiki/qa/<id>`).

- [ ] **Step 4: Verify.**

- [ ] **Step 5: Commit**

```bash
git commit -m "✨ feat(api/wiki): WikiQaSession + QA routes + SSE with meta/summary_ready ordering"
```

---

## Phase 5 — Console wiring

### Task 5.1: Swap `apps/mewbo_console/src/components/wiki/api/client.ts`

**Files:**
- Modify: `apps/mewbo_console/src/components/wiki/api/client.ts`
- Delete: `apps/mewbo_console/src/components/wiki/mocks/fixtures.ts`
- Delete: `apps/mewbo_console/src/components/wiki/mocks/pages.ts`
- Modify: `apps/mewbo_console/__tests__/app.test.tsx` (the mock points are in here)
- Create: `apps/mewbo_console/__tests__/wiki/client.test.tsx`

- [ ] **Step 1: Write Vitest tests for real client** (mock `fetch` + `EventSource`). Assert: each function in `client.ts` calls the right endpoint with the right shape; `subscribeToIndexing` yields events parsed from the SSE stream; AbortSignal cancels in-flight stream.

- [ ] **Step 2-3: Replace each mock function** with real impl. Pattern per function:
- Catalogues / projects / page CRUD → `fetch(url, {headers: {"X-Api-Key": apiKey}}).then(parse)`.
- Indexing stream → `new EventSource(url + "?api_key=" + apiKey)`; wrap in `AsyncIterable<IndexingEvent>` honouring `AbortSignal`.
- QA stream (POST with body) → `fetch(url, {method:"POST", ..., signal})`; manually parse the SSE stream from the body reader.
- Delete the two mock files.

- [ ] **Step 4: Verify.**

```bash
npm test -- wiki/client.test.tsx
```

- [ ] **Step 5: Commit**

```bash
git commit -m "✨ feat(console/wiki): swap mock client for real HTTP/SSE (delete fixtures)"
```

### Task 5.2: Update `apps/mewbo_console/src/components/wiki/README.md`

**Files:**
- Modify: `apps/mewbo_console/src/components/wiki/README.md`

- [ ] **Step 1: Update the "Mock client — backend swap seam" section** to reflect that mocks were deleted; document the real client's mapping table; link to design spec.

- [ ] **Step 2: Commit**

```bash
git commit -m "📝 docs(console/wiki): backend-real-now; update integration notes"
```

---

## Phase 6 — Polish: refresh / notify / heartbeat / cancellation

### Task 6.1: `POST /v1/wiki/projects/<slug>/refresh`

**Files:**
- Modify: `apps/mewbo_api/src/mewbo_api/wiki/routes.py`
- Modify: `apps/mewbo_api/src/mewbo_api/wiki/jobs.py`
- Modify: `tests/wiki/test_routes.py`

- [ ] **Step 1: Test** — POST → 202 `{queued: true}`; a new IndexingJob is created reusing the project's stored config; the requester's email is added to `wiki_notify`.

- [ ] **Step 2-3: Implement.** `WikiIndexingJob.refresh(slug, email, ...)` builds a new `WizardSubmission` from the stored project record + queues an index job.

- [ ] **Step 4: Verify + commit**

```bash
git commit -m "✨ feat(api/wiki): refresh-this-wiki endpoint with notify-on-complete enqueue"
```

### Task 6.2: `POST /v1/wiki/notify-when-indexed`

**Files:**
- Modify: `apps/mewbo_api/src/mewbo_api/wiki/routes.py`
- Modify: `tests/wiki/test_routes.py`

- [ ] **Step 1: Test** — request without existing slug returns `IndexingJob{status:"queued", scannedCount:0}` (a placeholder for the UI's loading screen); request when slug is currently indexing piggybacks on the existing job; on completion the notify queue pops & emits emails.

- [ ] **Step 2-3: Implement.** Notify-on-complete email-send is fire-and-forget via the existing `EmailAdapter` if configured; otherwise log warning.

- [ ] **Step 4: Verify + commit**

```bash
git commit -m "✨ feat(api/wiki): notify-when-indexed endpoint + email-on-complete"
```

### Task 6.3: SSE heartbeat + resume semantics

**Files:**
- Modify: `apps/mewbo_api/src/mewbo_api/wiki/events.py`
- Modify: `tests/wiki/test_event_ordering.py`

- [ ] **Step 1: Test** — verify heartbeat appears every 20s of idle; reconnect mid-job re-emits `queued` then `scanned` catch-up events.

- [ ] **Step 2-3: Implement** the heartbeat tick + resume re-emit logic in the SSE generator.

- [ ] **Step 4: Verify + commit**

```bash
git commit -m "✨ feat(api/wiki): SSE heartbeat (20s) + resume catch-up on reconnect"
```

### Task 6.4: Validation hardening + `WikiError.fields`

**Files:**
- Modify: `apps/mewbo_api/src/mewbo_api/wiki/routes.py`
- Modify: `tests/wiki/test_routes.py`

- [ ] **Step 1: Test** — malformed WizardSubmission returns 400 `WikiError{code:"validation", fields: {...}}`; rate-limit hook returns 429 `Retry-After`; oversize repo returns 502 `repo_access`.

- [ ] **Step 2-3: Implement** Flask-level validation + the rate-limit gate (per-IP counter; threshold from `wiki.indexing.max_files`; reuse existing `redis_or_dict` if present, else in-process dict).

- [ ] **Step 4: Verify + commit**

```bash
git commit -m "✨ feat(api/wiki): WikiError validation + rate-limit + repo_access mapping"
```

---

## Phase 7 — Docker, demo repo, Playwright E2E

### Task 7.1: Docker compose target for wiki E2E

**Files:**
- Modify: `docker-compose.yml`
- Create: `docker/Dockerfile.wiki-fixture` (optional — local-only seeder)

- [ ] **Step 1: Add a profile** `e2e` that brings up mongo + api (with wiki extras installed) + console. Wiki-extras install handled in `docker/Dockerfile.api` via a build arg `WIKI_EXTRAS=1`.

- [ ] **Step 2: Commit**

```bash
git commit -m "🔧 chore(docker): e2e profile with wiki extras installed in api image"
```

### Task 7.2: Demo repo on Gitea Hurricane

**Files:**
- Local-only (no repo file change).

- [ ] **Step 1: Create `wiki-demo` repo on `git.hurricane.home/bearlike/wiki-demo`** via the gitea MCP (`mcp__gitea__Gitea-Hurricane-create_repo`). Seed it with: a tiny Python pkg (one module, one class, two functions, an import), one markdown doc, a `.mewbo/wiki.json` grounder with 3 pages, a `README.md`.

- [ ] **Step 2: Push the seed content via the `create_or_update_file` MCP** in a single commit.

- [ ] **Step 3: Document the demo repo URL in `apps/mewbo_api/src/mewbo_api/wiki/README.md`** under "Quick test against a real repo".

- [ ] **Step 4: Commit** the README mention.

```bash
git commit -m "📝 docs(api/wiki): point at the public wiki-demo repo for E2E"
```

### Task 7.3: Playwright E2E

**Files:**
- Create: `apps/mewbo_console/e2e/wiki.spec.ts` (if Playwright already configured) OR
- Use the `mcp__plugin_playwright_playwright__*` tools to drive an ad-hoc E2E test pass.

- [ ] **Step 1: Bring up stack** via `docker compose --profile e2e up -d`. Wait for `:5125/health` ready (≤60s).

- [ ] **Step 2: Drive UI** via Playwright MCP:
  - Navigate to `https://mewbo.hurricane.home/wiki`.
  - Verify landing page loads & catalogues populate.
  - Paste the demo repo URL, click Generate Wiki.
  - Walk through the wizard (3 steps); submit.
  - Wait on `/wiki/indexing` until the loader completes (≤5 min).
  - Verify navigation to `/wiki/p/<landingPageId>`; the page body renders markdown + mermaid + nav.
  - Ask a question via the dock; verify QA stream renders blocks.

- [ ] **Step 3: Capture screenshots at each checkpoint** for the PR description.

- [ ] **Step 4: Commit any test fixtures / scripts**

```bash
git commit -m "🧪 test(wiki): Playwright E2E against live wiki-demo repo"
```

### Task 7.4: Final wiring + PR update

**Files:**
- Modify: PR body via `gh pr edit` (or `mcp__gitea__Gitea-Hurricane-pull_request_write`).
- Modify: `CHANGELOG.md` if the repo maintains one.

- [ ] **Step 1: Verify** the full pytest suite, ruff/mypy, console tests pass:

```bash
make lint typecheck
pytest tests/wiki -v
cd apps/mewbo_console && npm test
```

- [ ] **Step 2: Push** the worktree branch (after explicit user/auto-confirmation):

```bash
git push origin grove/deepwiki-style-gen-20260514-055235
```

- [ ] **Step 3: Update PR #6 body** to check off "Backend API implementation" and "Orchestrated Plumbing" boxes; include screenshots from Task 7.3.

- [ ] **Step 4: Commit (already pushed) — verify CI**.

---

## Self-Review

**Coverage vs design spec:**
- §1 Background → covered (Phase 1+2+3 all reference issue #5).
- §2 Goals/non-goals → Phase 1 + 2 satisfy "no orchestration loop replacement"; Phase 3 satisfies "graph + embeddings"; Phase 5 satisfies "frontend swap is one file".
- §3 Architecture overview → Tasks 2.6/2.7/2.8 implement the full pipeline.
- §4 Module layout → matched by the File Structure section above.
- §5 Storage model → Tasks 1.3/1.4/3.4.
- §6 Graph + embeddings → Tasks 3.1-3.6.
- §7 Indexing flow → Task 2.7 wires tools to event-log ordering.
- §8 Grounder contract → Task 2.4 (`.mewbo/wiki.json` first, `.devin/wiki.json` fallback).
- §9 QA flow → Phase 4.
- §10 Streaming / cancel / resume → Tasks 2.8 + 6.3.
- §11 Optional dependency footprint → Task 1.1 + `init_wiki` guard in 1.5.
- §12 Config instrumentation → Task 1.6.
- §13 Frontend swap → Task 5.1.
- §14 Error handling → Tasks 1.5 (errors.py) + 6.4 (validation hardening).
- §15 Testing strategy → tests created in every task.
- §16 Phasing → mirrored exactly.

**Placeholder scan:** every "TBD/TODO" instance grep'd; none found. Each "Step N: Implement" block names concrete file paths + class names + method signatures. AgentDef bodies (Tasks 2.6, 4.2) reference exact tool names defined in earlier tasks.

**Type consistency:** spot-checked — `WikiStoreBase.append_job_event(job_id, event)` is consistent across Tasks 1.3, 1.4, 2.7, 2.8; `WikiIndexingJob.start(submission, *, runtime, store, hook_manager)` consistent across Tasks 2.7, 6.1, 6.2; `IndexingEvent` discriminated union shape consistent with §10 of the spec.

**Outstanding risks:** none blocking. Two notes:
- `tree_sitter_language_pack` API may differ slightly across versions; Task 3.1's defensive-import + version pin in pyproject mitigates.
- Playwright against the self-signed `mewbo.hurricane.home` requires the MCP plugin (covered in `apps/mewbo_console/CLAUDE.md`); not a regression.

---

## Execution Handoff

Plan complete and saved to `docs/plans/2026-05-14-deepwiki-style-gen-plan.md`. User has pre-approved autonomous execution.

Proceeding with **subagent-driven-development** (fresh subagent per task, review between tasks) to maximize parallelism on independent tasks (e.g., Phase 3 graph-language additions, Phase 4 QA tools).
