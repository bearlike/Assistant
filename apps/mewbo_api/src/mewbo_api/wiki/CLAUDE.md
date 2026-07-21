> ↑ [apps/mewbo_api/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md)

# MewboWiki — API Subsystem Guidance

Scope: this file applies to `apps/mewbo_api/src/mewbo_api/wiki/` (the thin
HTTP/SSE + job-lifecycle glue) and the wiki SessionTools under
`packages/mewbo_graph/src/mewbo_graph/plugins/wiki/`. The reusable substrate
they drive — tree-sitter code graph, multiplex memory engine, embedder,
retriever, store, and the wiki domain/wire models — was extracted to
`mewbo_graph.wiki`; see `packages/mewbo_graph/CLAUDE.md` for the
library-level + layering decisions. This file captures the non-obvious
engineering decisions behind the auto-generated wiki indexing + Q&A pipeline.
Everything that can be read straight from the code is left out.

## What MewboWiki is

An auto-generated wiki for code repositories. Pipeline is
a fixed seven-phase state machine running inside a normal Mewbo session
(not a separate service): an agent owns the run, the wiki built-in
tools persist state, and SSE streams progress to the FE.

Phases, in order, are the source of truth for progress everywhere
(`enrich` is inserted post-AST):

```
clone → scan → graph → enrich → plan → pages → finalize
```

**GraphRAG ordering law.** The knowledge graph is built BEFORE
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
which has no tool of its own (it's a `wiki-enricher` fan-out). **A phase is
stamped when its work STARTS, never at a predecessor's return.** `enrich` used
to be emitted at the tail of `wiki_build_graph`, which stamps it started the
moment its PREDECESSOR ended rather than when enrichment does anything (a
stale rule this file used to state) — `emit_phase_once` (`_ctx.py`) now fires
from inside the first real enrich write (`mint_entity`, guarded on `job_id` so
a resume that skips enrich never re-stamps it) instead, closing the same
~2-minute "graph plateau" that used to read as a stall. If you add another
tool-less logical phase, emit it from the first tool call that actually DOES
that phase's work — never from the tail of the tool that precedes it.

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

**The indexer session itself now runs under a real ceiling.**
`_start_indexer_session` (`jobs.py` — the shared seam both `start()` and
`resume()` call, see "Restart durability" below) passes
`allowed_tools=INDEXER_TOOLS, strict_tool_scope=True, enable_skills=False`.
This used to be inert: the permissive branch unions every non-MCP builtin back
in regardless of an allowlist, and the AgentDef's own `disallowedTools`
frontmatter was already silently discarded by the (body-only) playbook loader
— so NEITHER layer was actually enforcing a ceiling. `mint_entity`/
`relate_entities` stay deliberately OUT of `INDEXER_TOOLS`: those are writes
the `wiki-enricher` CHILD performs, and a parent doesn't need a tool in its
OWN allowlist to grant it to a child — a spawned child's specs are filtered
against its own allowlist, never its parent's.

## Developer mode — graph-only onboarding (zero-LLM, sessionless)

`runtime.developer_mode` unlocks AST-only onboarding (no docs, no LLM) — the
engine is `GraphOnlyIndexer` down in `mewbo_graph` (see its CLAUDE.md). The
API/glue decisions, all non-obvious:

- **The dev-mode gate is at the `post_index` ROUTE only.** It forces
  `submission.graph_only=False` unless the flag is on. Mode is STICKY thereafter:
  the submission sidecar round-trips `graph_only` (`model_dump(by_alias=True,
  exclude_none=True)` keeps booleans), so `refresh()`/recovery PRESERVE graph-only
  — do NOT re-gate at `start()` (that flips a project's mode mid-life).
- **Graph-only jobs are SESSIONLESS.** `WikiIndexingJob._start_graph_only_index`
  drives `GraphOnlyIndexer.run` on a daemon thread with `session_id=""` (no agent,
  no `wiki` capability advertisement). Three consequences fall out of that:
  - **QA must short-circuit BEFORE the session.** `post_qa` returns
    `documentation_unavailable_response(slug)` when `project.graph_only` — because
    the store's raise fires INSIDE a probe's `wiki_read_page` SessionTool, where
    the route-level `@app.errorhandler(DocumentationUnavailableError)` 409 net
    never runs.
  - **Recovery must never reach the agent path.** `WikiIndexingSessionEndHook`
    can't match a sessionless job, so `WikiResume.resume` branches on the sticky
    `submission.graph_only` and re-drives `_start_graph_only_index` (idempotent
    from-scratch — graph-only builds no `ResumePlan`). BOTH auto-`JobRecovery` and
    manual `/resume` funnel through `WikiResume.resume`, so one branch covers both.
  - **Cancel is cooperative.** `GraphOnlyIndexer` re-reads job status at each phase
    boundary and bails before `_finalize` (the ONLY `complete`/Project writer), so
    a cancel is never clobbered by a late terminal write.
- **409 mapping:** `DocumentationUnavailableError` → `documentation_unavailable`
  (HTTP 409, `retryable:false`) via `WIKI_CODE_STATUS` + the errorhandler. MCP
  maps the 4xx to a non-retryable structured envelope for free; graph-exploration
  tools (`read_wiki_structure` → `/graph`) are unaffected.

## Editable project settings — `Project` is a snapshot, `ProjectSettings` is the record

**The fact everything here follows from: `Project` is a DISPLAY snapshot, rebuilt
WHOLESALE on every successful (re)index** (`finalize.py` / `graph_only.py` both
construct a full `Project(...)` and upsert it). Anything PATCHed directly onto it
is silently wiped by the next reindex. So `Project` is NOT the edit target.

The edit target is **`ProjectSettings`** (`mewbo_graph.wiki.types`) — ONE
slug-keyed record holding the `WizardSubmission` contract minus the token, plus a
`desc` override. It is what `WikiIndexingJob.refresh` consults FIRST, which is the
only reason an edit takes effect at all. Don't confuse it with the **job-keyed
submission sidecar**, which stays what it always was: immutable history of what
ONE job ran with. Same separation (and same reason) as the recovery counter.

- **Resolution ladder — ONE definition, walked by both `refresh` and the façade**
  so the UI can't show settings a refresh wouldn't use: settings record → newest
  job sidecar (`jobs._latest_job_submission`) → `Project` fields
  (`jobs.submission_from_project`). A legacy project has no record; the first
  PATCH materialises one from that ladder.
- **The legacy sidecar scan sorts by `phase_started_at`, NEVER by `job_id`.**
  `job_id` is a `uuid4` hex — it sorts RANDOMLY, so the old `key=lambda j: j.job_id`
  picked an arbitrary job's submission as "latest" (measured: correct only ~1/N of
  the time). Same trap the freshness baseline already documents; `IndexingJob` has
  no `created_at`, and `phase_started_at` is the one ordering signal it carries.
- **`start()` re-seeds the record on every onboard AND every refresh** (refresh
  re-enters `start`), so it MERGES: an existing `desc` override is carried forward
  rather than clobbered by a submission that never had one.
- **finalize read-preserve.** `finalize._resolve_project_desc` is THE seam (shared
  by `wiki_finalize` and `GraphOnlyIndexer` — it replaced a copy-pasted fallback in
  both): user override → platform-API fetch → previous record's `desc`. Without it
  a rebuilt `Project` overwrites an edited description on the next index. `model`
  needs no such seam — it isn't a `Project` field at all, so only the settings
  record holds it.
- **`update_project(slug, fields)`** is a partial upsert whitelisted to
  `PROJECT_UPDATABLE = {"desc"}`. That set is deliberately tiny: every other
  `Project` field is either rebuilt by the next index or is identity. `desc` is
  written to BOTH surfaces — the snapshot (so the console updates now) and the
  settings override (so the next index re-applies it).

**Routes** (`settings.py` — `WikiProjectSettings`, the DI'd atomic class; the
Blueprint handlers are thin adapters that let its `WikiHTTPError` raises map
through the already-registered handler):

- `GET /v1/wiki/projects/<slug>/settings` → the effective settings + `credential`
  presence (**scope + scopeType only, never a value** — via the ONE durable-tier
  walk `jobs._durable_credential_scope`, which also backs the indexer's auth note)
  + a camelCase `editable` map. Catalog projects get a reduced `kind:"catalog"`
  shape.
- `PATCH /v1/wiki/projects/<slug>` → 200 / 400 / 403 / 404 / 409 / 410.
  - **`extra="forbid"` is load-bearing**: a `token`, a `slug` rename, or any
    system-owned field is a 400, not a silent no-op. Credentials go through the ONE
    registry at `/v1/git/credentials/<scope>`.
  - **Dev-mode RE-GATE on `graph_only` (403)** — the real privilege fix. The gate
    exists only at `POST /index`, and the mode is deliberately STICKY (refresh
    replays it unchecked, by design — see "Developer mode" above), so a PATCH that
    wrote it blind would hand an unprivileged caller the zero-LLM/no-docs path the
    index route refuses them. Turning it OFF is not privileged.
  - **Repo-identity guard (409).** The slug keys pages, graph, jobs, credentials and
    freshness — a bare URL swap would re-point the next clone while leaving all of
    them pinned to the old repo. Identity is compared through
    `CredentialScope.from_repo_url`, which normalises (host lowercased, `.git` and
    trailing slash stripped), so a same-repo re-normalisation passes for free and
    only a real (host, owner, repo) change is refused. `platform` alone is not
    identity.
  - **Omitted ≠ null.** `model_fields_set` (not an `is not None` filter) decides
    what was sent, so `{"ref": null}` unpins a branch while an absent `ref` persists.
  - **A PATCH never starts a re-index** — everything but `desc` takes effect at the
    next one (the user drives that with Refresh), which is also why it can't be used
    to bypass the per-IP indexing rate limiter.
- **Flipping `graph_only` ON drops the project's existing pages.** `GraphOnlyIndexer`
  now prunes them at finalize: the Project is stamped `graph_only=True`, so every
  surviving page is unreachable behind the doc-read guard, and it would collide with
  freshly generated ones if the project were ever flipped back. No-op for a project
  onboarded graph-only from the start.

**Not on the RESTX spec.** `/v1/wiki/*` is a plain Flask Blueprint, so it
contributes ZERO paths to `docs/openapi.json` (verified) — the Flask-RESTX
`example=`/Scalar convention does not reach this surface. A wiki route documents
itself in its docstring; adding wiki to the OpenAPI spec is a separate migration.

## Non-git catalog ingestion

`CatalogIngestor` (`mewbo_graph.wiki.catalog`) — direct write (no agent/tree-sitter):
each doc → `WikiPage` (BM25) + a content-addressed graph node (embeddings, guarded →
BM25-only) → honest `complete` Project (non-empty graph). Catalog nodes reuse
`type=File` with `file="catalog/"` prefix (`doc_total` counts by that prefix; a
dedicated `"Document"` node type + FE Record-map update is a deferred follow-up).
Refresh rejects catalog projects (`repo_url is None` AND no git submission).

## Q&A model default + snapshot terminal status

`post_qa` makes `model` genuinely optional via `_resolve_qa_model()` (the one
helper for the `wiki.default_qa_model → wiki.default_model → llm.default_model`
chain, reused by `get_meta`/`get_wiki_defaults`/`_build_condense_model` — don't
re-inline it). The route accepts `project` OR `slug` in the body but reports
validation against the PUBLIC `project` name.

`QaAnswer.status` (`mewbo_graph.wiki.types`, default `"running"`,
`QA_TERMINAL_STATUSES = {complete, cancelled, error}`) is the terminal flag a
NON-streaming consumer needs (the MCP `ask_wiki` poll over `GET /v1/wiki/qa/<id>`
— the SSE stream already had its `complete` event). It is set at each accept
state: the terminal `wiki_emit_answer` call (`QaFinalizer.close` →
`status="complete"`, or `"error"` on a zero-block close) and
`WikiQaSession.cancel` (`status="cancelled"`). Set it
through `store.save_qa(answer)` so both store backends round-trip it onto the
snapshot. Any NEW QA terminal path MUST set the status too, or a snapshot poller
waits out its timeout.

That same snapshot is the **idempotent-replay source for the console `?answer=<id>`
URL**: a completed answer is fully reconstructable from `GET /v1/wiki/qa/<id>`
(blocks + cited + accessed + models), so a refresh/share replays it with zero LLM —
the FE deep-links the id instead of re-POSTing `/v1/wiki/qa`. No BE change was needed;
the persistence already existed (see console `CLAUDE.md` → "Idempotent Q&A URL").

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

## Git credential resolution — durable store + ambient fallback (security-sensitive)

Private repos need auth to `git clone`/`ls-remote`. The wizard/API submits a
token or SSH key; it MUST NOT land in the persisted submission, the session
transcript, or any event log — Mewbo sessions are visible in Langfuse/Mongo
and we treat the transcript as semi-public.

There is deliberately **no in-process token cache** anymore — `CloneTokenCache`
(`mewbo_graph.wiki.tokens`) is gone. It was a THIRD source of truth that could
drift from the durable store: a revoked stored token permanently shadowed a
still-valid ambient credential with no fallback, which is exactly what caused
cascading re-index failures for real. The database and the ambient (built-in)
git credential are now the ONLY two sources of truth.

- `RepoCredential` (`mewbo_graph.wiki.types`) — `{kind: token|ssh_key, value,
  username?, updatedAt?}`. Supports git tokens AND SSH/deploy private keys. It
  carries NO scope field: the scope is the store KEY (stamped into the blob at
  save), so there is one binding, not two that can disagree. `value` is stripped
  at definition — a PAT pasted with a trailing newline silently 401s.
- `CredentialStore` (`mewbo_graph.wiki.credentials`) — the single read/write
  chokepoint, keyed by a validated **`CredentialScope`** (never a bare `str`):
  a full slug (`host/owner/repo`, repo-specific) OR a bare host
  (`git.example.home`, shared by every repo on that host). The host-covers-repo
  sharing rule is `CredentialScope.covers()`, and `.kind` (`host|repo`) is what
  the `scopeType` wire field mirrors — the BE has ONE definition of the rule and
  the FE (`api/git.ts`) mirrors it. Zero migration; existing rows are unchanged.
  Plaintext-at-rest behind an identity `_encode`/`_decode` seam —
  encryption-at-rest is a one-line swap there, nothing else changes.
- Store: `save_credentials`/`get_credentials`/`delete_credentials`/
  `list_credentials` on `WikiStoreBase`; JSON driver writes
  `credentials/<scope>.json` at mode `0600`, Mongo uses the `wiki_credentials`
  collection.
- **Plaintext-at-rest ≠ plaintext-in-flight**: NEVER log `RepoCredential.value`,
  echo it into an SSE event, a transcript, or a tool result. The clone error
  scrubber + the credential store are the only places it appears.

**`resolve_chain(store, slug, *, arg_token=None)` (`mewbo_graph.wiki.credentials`)
is the ONE canonical resolution order**, used identically by clone, branch
listing, freshness, and the description fetch:

1. `arg` — an explicit override (e.g. the wizard testing a not-yet-saved token).
2. `store:repo` — the repo-scoped durable credential.
3. `store:host` — the host-scoped durable credential (shared across every repo
   on that host).
4. `ambient` — the built-in git credential via `git credential fill`
   (read-only, `GIT_TERMINAL_PROMPT=0`, 10s timeout) — the fallback tier that
   fixes a revoked DB token: it no longer permanently shadows a still-valid
   ambient credential.
5. `anonymous` — always last (public repos).

Consumers iterate the ordered candidates and advance on an **auth-class**
failure only (`is_auth_failure(stderr)` — the ONE auth classifier; its markers
are ANCHORED to real git/HTTP auth text, e.g. `error: 403` / `http 401`, never a
bare `401`/`403` substring, which used to misread a `port 8403: Connection
refused` as a rejection). A non-auth failure (network, timeout) propagates
immediately; it won't succeed on retry with a different credential.

**Every git subprocess in the product runs through the hardened executor in
`mewbo_graph.plugins.wiki.clone`** (`run_git_with_chain` + the shared
`build_clone_command` / `build_ls_remote_command` / `hardened_git_env` builders):
clone, `ls-remote`/branches, freshness, and the credential-validate route below.
That is what disables git's OWN credential helper (`-c credential.helper=`) and
prompting (`GIT_TERMINAL_PROMPT=0`) everywhere. The reason is a live incident, not
hygiene: the api container mounts `~/.git-credentials` READ-ONLY, so when git
tried to erase a rejected entry it failed with `Device or resource busy` — and
that EBUSY MASKED the real auth error, killing an otherwise-fine clone. We read
the ambient credential ourselves (read-only `git credential fill`) and inject it
into the URL, so git never touches the mounted file. A new git call site that
hand-assembles its own argv reintroduces the bug — see `mewbo_graph/CLAUDE.md`
→ "Git auth" for the full trap, including why a git success does NOT prove a
credential is valid.

Token → URL injection (`x-access-token:<token>@host`, or a stored `username`).
SSH key → temp file (`0600`) + `GIT_SSH_COMMAND="ssh -i <tmp> -o
StrictHostKeyChecking=accept-new"`, deleted in a `finally`.

Onboard (`jobs.start`) saves the credential durably BEFORE stripping the token
from the persisted submission. Refresh (`jobs.refresh`) does **no restore step
of its own** — the clone tool's own `resolve_chain` reads the durable
credential directly at clone time for the new job, so there is nothing to warm.
`_render_user_query`'s auth note is therefore derived from
`_durable_credential_present(store, slug)` (the store), NEVER from
`submission.token` (which refresh no longer carries — reading it made every
refresh of a private repo render "public repo assumed"). Finalize's description
fetch and the freshness compare both re-walk the chain through the shared
`_platform_api.api_get_json_with_chain` — a git success does NOT prove a
credential authenticates the platform's REST API (a public repo clones fine with
a revoked token), so no REST caller may trust the clone's winner. See the second
trap in `mewbo_graph/CLAUDE.md` → "Git auth". Project-delete removes ONLY the
exact repo-scoped credential
(`CredentialStore.delete(store, slug)`) — a host-scoped credential is shared
across every repo on that host and must never cascade.

## Git credential management — `/v1/git/credentials*` (product-wide)

`git_credentials_routes.py` mounts a PRODUCT-WIDE registry at
`/v1/git/credentials*` — NOT under `/v1/wiki/*`, even though it is registered
alongside the wiki routes from the same `init_wiki` and reads/writes the SAME
`WikiStoreBase` credential surface the resolution chain above reads. Wiki is
the first consumer; task/vcs-pickup flows are expected to read/write the same
registry next.

- **Every route validates its `<path:scope>` through `CredentialScope` first** —
  a malformed scope (a URL, an empty segment, whitespace) is a clean 400
  `validation` AT THE BOUNDARY. It used to be accepted, written under a key the
  resolution chain could never look up, and only surfaced later as an opaque
  "the clone fell back to anonymous". `scopeType` reads the model's own `.kind`;
  the old local `_scope_type` (`"/" in scope`) is GONE.
- `GET /v1/git/credentials` → `{"credentials": [{scope, scopeType, kind,
  username, valueHint, updatedAt}]}` — `valueHint` is `"…" + value[-4:]` for a
  token, `"ssh key"` for an SSH key. The raw `value` NEVER appears in this or
  any other response.
- `PUT /v1/git/credentials/<path:scope>` `{kind, value, username?}` — the body is
  the `CredentialUpsert` wire model (`extra="forbid"`, so a client that tries to
  smuggle a `scope`/`updatedAt` in the body gets a 400 rather than having it
  silently ignored), which validates into `RepoCredential` (empty value / bad
  kind → 400 `validation`) before `CredentialStore.save`.
- `DELETE /v1/git/credentials/<path:scope>` → 200 / 404 when absent.
- `POST /v1/git/credentials/<path:scope>/validate` `{repoUrl?}` → `{ok, detail}`
  — runs ONE `git ls-remote` with the stored credential injected, built from the
  SAME `build_ls_remote_command` + `hardened_git_env` the clone uses (20s cap).
  It threads the credential's own `username` through `_inject_token` exactly as
  the clone chain does — a GitLab `oauth2`/deploy-token credential that validated
  under a hardcoded `x-access-token` would have authenticated differently here
  than in the clone that follows, which is worse than not validating at all.
  `repoUrl` defaults to `https://<scope>` for a repo scope and is REQUIRED (400)
  for a host scope (no single repo to probe). **An `ssh_key` credential requires
  an SSH-form `repoUrl`** (`ssh://…` or `git@host:owner/repo`) and returns
  `ok:false` with an explanatory `detail` otherwise: an `https://` URL ignores
  `GIT_SSH_COMMAND` entirely, so probing one would run an ANONYMOUS HTTPS
  ls-remote and hand back a meaningless verdict — `ok` on any public repo, `fail`
  on any private one, in both cases saying nothing about the key. `detail` is
  scrubbed through the shared `clone._redact` before it reaches the response.
- **Values are never returned by any of these routes** — only `valueHint`. The
  write path is the only direction a secret travels.

## Repository freshness — `GET /v1/wiki/projects/<slug>/freshness`

Compares the indexed commit against the remote HEAD via `RepoFreshness.check`
(`mewbo_graph.plugins.wiki.freshness`) — a `git ls-remote` plus a per-platform
compare-API call, using the SAME credential chain above. Response:
`{indexedSha, remoteSha, behindBy, upToDate, checkedAt}` — `behindBy`/`upToDate`
are `None` when the platform compare couldn't run (an honest "unknown", never a
false "up to date"; the FE renders "Update available" with no count).

- **Baseline sha** is `Project.commit_sha`, falling back for older projects to
  the latest `complete` job's commit — ordered by `phase_started_at` (ISO-8601,
  so lexicographic == chronological), **never by `job_id`**, which is a `uuid4`
  hex and therefore sorts randomly. `IndexingJob` has no `created_at`; sorting
  by the id picked an arbitrary job's commit as the baseline.
- **Cache**: in-module TTL dict, 5 min, keyed by slug (cheap, but not free to
  poll on every card render). `?force=1` bypasses a cached READ and recomputes
  (still refreshing the entry for the next caller). It is EVICTED on
  `refresh_project` (we just started re-indexing at HEAD — a "behind by N" badge
  against the commit being rebuilt is a lie) and on `delete_project` (so a
  re-created slug can't inherit the dead one's badge). Negative results are
  cached like any other body, so an unreachable remote can't re-block every request.
- **The API serves this synchronously, so the worker class matters.**
  `docker/Dockerfile.api`'s `CMD` runs gunicorn with `--workers 1 --threads 8`, but
  `--threads` is INERT on the default *sync* worker (one request per process) —
  so a cold freshness check (ls-remote + compare, seconds on a slow remote) blocked
  the WHOLE API. The worker class is now `gthread`, which is what actually serves
  those threads. Don't drop `-k gthread` "because threads are already set".

## Branch picker — `POST /v1/wiki/branches` + ref threading

The wizard's generation step lets the user pick a branch. `post_branches`
resolves the remote's heads via the down-layer `RemoteBranchLister`
(`git ls-remote --symref`, host-agnostic — see `mewbo_graph/CLAUDE.md`) and
returns `{branches, defaultBranch}`. Credential resolution is the SAME
`resolve_chain` every other consumer uses, but **jobless**: there is no
job_id/slug yet at onboarding, so the route falls back to the URL's bare HOST
scope (`CredentialScope.from_repo_url(repo_url).host_scope()`) as the resolution
scope when no slug was chosen yet —
repo-scoped store → host-scoped store → ambient → anonymous, each candidate
tried in order; an auth-class failure advances to the next, so a revoked/wrong
stored credential doesn't shadow a valid fallback. A non-auth failure (network,
timeout, ...) propagates immediately as the standard `repo_access` envelope.

**An explicit body `token` is EXCLUSIVE — it is NEVER part of that chain.** The
wizard sends one only when the user is testing a specific, not-yet-saved
credential, so the route tries THAT token and nothing else: an auth-class
rejection returns `400 validation` (`fields: {token: "rejected"}`) instead of
falling through. This is a correctness rule, not a UX preference — if a stored /
ambient / anonymous candidate were allowed to succeed behind a rejected typed
token, the wizard would report success and onboarding would then durably PERSIST
the bad token, which is precisely how an invalid credential gets silently saved.
Fail fast on the credential the user actually typed. `BranchListRequest`/the
`{branches, defaultBranch}` reply are api-side transport models (never persisted),
not `mewbo_graph` domain types. `ls-remote` failure → the standard `repo_access`
envelope (no new error code).

The chosen branch is `WizardSubmission.ref`. `_render_user_query` emits a `ref:`
line ONLY when set — an omitted ref keeps the rendered query byte-identical to the
default-branch behaviour (so the golden render tests don't churn) — and the
`wiki-indexer` playbook passes it to `wiki_clone_repo`. The RESUME path is
unchanged: it pins the recorded `commit_sha` as the clone ref (a resume re-clones
the exact indexed commit, NOT the chosen branch's latest HEAD).

**A pinned `commit_sha` cannot ride a `git clone --branch <ref>`** — git resolves
that flag's value as a branch/tag name on the REMOTE, so handing it a raw SHA
always fails. The clone tool pins server-side instead: `git init` + a depth-1
`fetch` of the exact object + `checkout FETCH_HEAD` (`clone_at_sha`). It also
VERIFIES rather than overwrites — if the checked-out HEAD disagrees with the
recorded `commit_sha`, the tool refuses (a `repo_access` failure) instead of
silently rewriting the pin. Never have the model pass a `ref` for a pinned
resume job: the ref IS the pinned sha, and it never goes through `--branch`.

## Restart durability is checkpoint-aware resume (Part B)

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

**Checkpoint-aware resume, not full refresh (a reversal of the old
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

**`ResumePlan`'s artifact counts fail CLOSED, not open.** The shared read helper
behind its graph/entity/page counts used to swallow ANY store exception and
return 0 — one transient Mongo hiccup silently selected a full rebuild instead
of a resume. It now RAISES instead of defaulting to zero. The sharp edge: a
refusal to resume must NOT consume the retry budget, or the fail-closed fix
just trades a silent full rebuild for a silent PERMANENT failure after a few
transient glitches — `JobRecovery.recover_interrupted` catches that raise
specifically and skips the slug-keyed `_bump_attempts` for THAT cause only; any
other exception still counts against the cap.

**Per-job artifact attribution makes "the graph for THIS commit" expressible.**
Graph nodes/edges/embeddings and the entity family all carry `commit_sha`/
`job_id`; pages get the same pair as a store-side attribution sidecar (keyed
alongside the page, not a `WikiPage` model field) so the console's wire type
stays byte-identical. The commit-scoped counts `ResumePlan` actually queries,
and the supersede logic that reaps a superseded job's artifacts, both key off
exactly these fields — without them the store is the union of every index
ever run for a slug, and a resume's "is the graph already built" question has
no commit to ask it about.

When a slug **exhausts** `JobRecovery.MAX_RETRIES`, recovery now moves its job to
terminal **`failed`** (`_mark_failed`) instead of leaving it `interrupted`
forever — a job that keeps dying must stop being a perpetual zombie in the
active-jobs surface (that ghost is what made a completed project keep showing
"Indexing now" — the FE suppresses a completed tile while its slug has any active
job, `LandingScreen` `activeSlugs`).

## Terminal accept-state for indexing

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

## Infra-failure recovery net

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

## Grounded-structured slug resolution

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

- **Root has NO retrieval tools *in its allowlist* by design** (`QA_TOOLS` =
  list_pages/emit/insight + spawn/check). That's what FORCES delegation; handing the root
  the retrieval surface is exactly how it regressed to read-one-page-and-stop. The probe
  leaf (`wiki-qa-probe.md`) owns retrieval. **Structurally enforced, not just prompted
  (resolved, Phase 3):** `SessionToolRegistry.build_for` now treats a non-empty
  `allowed_tools` as a ceiling over the capability gate too (mechanism in its docstring), so
  the 5-item `QA_TOOLS` list means the root genuinely CANNOT bind `wiki_read_page`/
  `wiki_query_graph`/etc. even though the session holds the `wiki` capability. The one
  exception preserves that surface: an empty/`None` `allowed_tools` (a plain session with a
  runtime-granted capability, no AgentDef scope) still auto-surfaces the gated tool.
  `strict_tool_scope=True` narrows the *stateless* surface (shell/edit) on top of this; the
  probe's own `tools:` already lists what it uses, so it is unaffected.
- **Greedy graph-first is the intended QA behavior.** The root spawns the FEWEST
  probes that cover the question (default 1–2) and emits as soon as the findings answer —
  no confirmatory/marginal probes (that tail was the p90=54-tool blow-up). Probes go
  GRAPH + REAL SOURCE first (`wiki_query_graph`/`wiki_graph_neighbors`/`wiki_read_file`),
  demoting generated pages (`wiki_read_page`/`wiki_search_pages`) and semantic
  `wiki_code_search` (embeddings may be BM25-degraded — graph nav always works) to
  last-resort orientation. Prompt-only; the anti-under-answering guards survive —
  the structure floor is now scoped to architectural/how-does-X questions, so a narrow
  lookup answers concisely instead of padding.
- **The QA run is read-only + self-approving (`approval_callback=auto_approve`).**
  Deliberately NO hand-maintained admit-list. Even with the capability gate now honouring
  the structural ceiling (Phase 3, above), the PROBES still need a wide, evolving
  retrieval surface (`wiki-qa-probe.md`'s `tools:`) called freely turn-to-turn — a
  restrictive callback would just turn each of those legitimate read-only calls (e.g.
  `agentic_search`, which is GET-classified so it even auto-executes elsewhere) into an
  unanswerable ASK/park in the headless flow (the `awaiting_approval` stall). `auto_approve`
  is the posture every other headless drive uses; the probe prompt is what steers WHICH
  retrieval tool to call, not a permission gate.
- **ONE atomic `wiki_emit_answer` call IS the accept state** (the `EmitStructuredResponseTool`
  pattern): the model delivers the WHOLE answer as a single `{blocks: [...]}` call — every
  block schema-validated (positional errors ride the tool feedback loop), exactly one
  `sources` block required LAST — and the tool fans the array into the per-block
  `block_open`/`block_close` QA events server-side (SSE stream + console untouched), drives
  `QaFinalizer.close`, and sets `should_terminate_run()`. **Contract-shape lesson:** the old
  per-block `wiki_emit_block × N` choreography fought how models compose (whole answer in one
  pass) — a weaker model narrated the call sequence as plain TEXT at the fuse step and
  delivered nothing. Align the contract to the composition grain instead of stacking prompt
  guards. `QaSessionEndHook` (on_session_end) is the NET: a no-error run with ZERO blocks gets
  exactly ONE corrective re-drive (`nudge` marker event bounds it; the guard is domain state —
  zero `block_open` events — never text-format sniffing), and a still-empty close is an honest
  `error`, never an empty `complete` (`QaFinalizer.close`).
- **`QaFinalizer` lives DOWN in `mewbo_graph.wiki.qa`** (with `QaAnswer` + the store),
  so both the terminal emit (same layer) and the API net (imports down) call it. It
  rebuilds `blocks` + `summary_sources` from the append-only log — previously NOTHING
  did, so a reloaded/shared answer came back empty (`blocks=[]`) and the SSE stream only
  ended by idle-timeout.
- **Two kinds of citation, captured deterministically.** `summary_sources` = the LLM's
  curated sources block. `accessed_sources` = the retrieval trail — but it is **bounded +
  score-ranked**, not "every node a probe touched". Each retrieval tool records a
  structured `QaAccessRecord {ref, score, rank, tool, op, ok}` (`mewbo_graph.wiki.qa_access`)
  via `WikiSessionTool._record_qa_access`: graph-NAVIGATION tools (`wiki_query_graph` /
  `wiki_graph_neighbors`) record only their seed (navigation ≠ grounding), the ranked search
  tools record only hits clearing a score floor (`…_SCORE_RATIO`, default 0.5× the top hit),
  and `QaFinalizer._accessed_from_events` folds → dedupe-by-ref (best score) → score-desc →
  top-N cap (`MEWBO_WIKI_QA_ACCESS_TOPN`, default 12). This killed the ~200-source sprawl
  (171 raw `graph:` nodes in a real run) the old unranked dump produced; `HybridRetriever`
  stays the one ranking engine. **Cited sources now REPRESENT file/graph, not just pages:**
  files were the most-read source yet `summary_sources` was ~100% page-slugs, so
  `QaFinalizer._summary_sources` folds the non-page refs off that bounded/ranked trail into
  the cited set — curated pages first, then the files/symbols the answer grounded on
  (`wiki:` trail refs skipped; the curated half owns pages). `GET /qa/<id>` resolves
  `graph:` refs for BOTH panels via `AccessedSourceResolver`. `models_used` rides
  `QaSessionEndHook` / `QaFinalizer.enrich`.

## Q&A answer depth + cited-sources viewer

Answer depth is **prompt-controlled, not code-controlled** — `QaFinalizer` never
truncates; it passes blocks straight from the event log. Two recurring-regression
guards live in the prompts (`mewbo_graph/.../agents/wiki-qa.md`,
`wiki-qa-probe.md`):

- **Structured-output floor — scoped to architectural Qs.** `wiki-qa.md`
  MANDATES minimum structure (a lead `p` direct answer + ≥1 `h2` facet section +
  ≥2 supporting `p`, then the `sources` block) for an **architectural / "how does
  X work" / relationship** question — the ones that span components. A narrow
  question (yes/no, single-value or single-fact lookup, "where/what is X", a
  definition) answers concisely in the lead `p` + `sources` and is NOT padded into
  sections. Without the floor the model reads "quick + authoritative" as "be brief"
  and one-paragraphs an architectural answer — the reference-parity regression;
  scoping it (rather than dropping it) keeps that guard for the
  questions that need it while letting greedy narrow answers stay tight. "Quick"
  means LATENCY (fewest probes, emit as soon as covered), never answer brevity. The
  probe contract stays un-capped (the old "2–5 terse claims" starved the fused
  answer). Only emit kinds in the `types.py` block union
  (`p/h2/h3/hr/ul/accordion/sources/table/diagram` — NO `ol`; express ordered lists
  as markdown prefixes inside a `ul`/`p`).
- **Inline citations = `src:` links.** The prompt emits
  `[path:line](src:path#L<a>-<b>)`; probes MUST pass `start_line`/`end_line` to
  `wiki_read_file` so the citation carries a precise range (a bare path can't open
  the right lines). The console renders these as chips + a source card.

**Two citation/provenance fixes — both at a single deterministic seam:**

- **Page citations are re-schemed at the EMIT seam (by id OR title).** The QA
  agent cites a wiki PAGE the console would otherwise treat as a source FILE and 404 on
  `/source` (pages live in the page store, not the clone). `wiki_emit_answer` runs the
  `sources` block (and the `summary_ready` page ids) through
  `QaFinalizer.tag_page_citations` (`mewbo_graph.wiki.qa`): a bare ref becomes
  `wiki:<page-id>` when its slugified form matches a page id **OR a slugified page TITLE**
  — the model frequently emits the human title ("Agent X Search Subsystem"), not the slug
  ("agent-x-search-subsystem"), which was the residual "file not found". Match reuses the
  core `_slugify` as a symmetric normalizer; the scheme guard is anchored to the closed
  `wiki|graph|src|entity` set so a colon-bearing title still matches. One seam fixes LIVE
  stream + snapshot together. The FE now RENDERS the `wiki:` card (single-page fetch) rather
  than dropping it — see console `CLAUDE.md`. File / `path#L…` / `graph:` refs pass through.
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

## Q&A follow-up continuation

A follow-up question REUSES the same backing session instead of starting a new
one — the console already had a follow-up input (`QADock`) with no continuity
behind it before this; that gap is now closed. Durable decisions:

- **`WikiQaSession.follow_up(answer_id, question, *, runtime, hook_manager=None)`**
  is the QA-domain entry point onto the SAME engine primitive the generic
  session-continuation path (`send_followup` / `POST /api/sessions/<id>/message`)
  and `QaSessionEndHook._nudge_if_silent` already use: `runtime.start_async`
  re-engaging an EXISTING `session_id`. It is not a second, parallel resume
  mechanism — it looks up the answer's session via `store.get_qa_session`,
  snapshots the just-finished turn into `QaAnswer.turns` (oldest-first history),
  resets the top-level fields for the new turn, and re-drives the session with
  `QA_TOOLS`/`strict_tool_scope=True`/the wiki-qa playbook — the exact same
  scope `start()` uses, referenced from the SAME module constants (one source
  of truth, not re-derived). **No new `answer_id` is minted** — the SSE stream
  for a follow-up is the same `WikiQaSseGenerator(answer_id=answer_id)` a
  caller already uses for a fresh question.
- **The QA scope is now first-class persisted session state, not bare
  `start_async` kwargs — and the GENERIC re-engage path honours it too.**
  `WikiQaSession.start` writes `mcp_tools` (=`QA_TOOLS`), `strict_tool_scope`,
  `session_step_budget`, and `skill_instructions` (the playbook text) into the
  SAME context event that advertises the `wiki` capability. `backend.py`'s
  `_extract_strict_tool_scope`/`_extract_skill_instructions`/
  `_extract_session_step_budget` (mirroring the pre-existing
  `_extract_allowed_tools`/`mcp_tools` pattern) read these back generically —
  wired into BOTH re-engage sites that already derive grants from persisted
  context, `SessionMessage.post` (`/message`) and the session-recover route
  (`/recover`). So a QA session re-engaged through the *generic* continuation
  endpoint — not just `WikiQaSession.follow_up` — ALSO keeps its narrow
  `QA_TOOLS`/`strict_tool_scope`/playbook/budget instead of silently widening
  back to the unscoped default (the exact bug this issue root-caused). Every
  extractor is generic — no QA-specific branch in `backend.py` — so any future
  session type gets the same re-engage fidelity for free by persisting the same
  context keys. This completes the pre-existing context-inheritance
  contract (which already re-applied model/mode/`mcp_tools`).
- **`POST /v1/wiki/qa` accepts an optional `answerId`.** Present ⇒ continuation
  (`WikiQaSession.follow_up`, only `question` + `answerId` required — no
  `project`/`fromPageId`/`model`, those ride the existing answer). Absent ⇒
  today's behaviour unchanged (`WikiQaSession.start`, fresh `answer_id`).
  Unknown `answerId` → `404 not_found`; a session already mid-run → `400
  validation` (mirrors `SessionMessage.post`'s 409, surfaced as a QA-shaped
  error since this route has no 409 code).
- **The `meta` SSE event now carries `sessionId`** (alongside the existing
  `answerId`/`model`/`fromPageId`) — exposes the backing session so
  continuation is addressable/traceable. Purely additive; existing consumers
  that ignore unknown fields are unaffected.
- **`QaAnswer.turns: list[QaTurn]`** (each a full snapshot: `question`,
  `blocks`, `summarySources`, `accessedSources`, `modelsUsed`, `status`) is
  the additive, oldest-first history of every PRIOR completed turn. The
  top-level `QaAnswer` fields (`blocks`, `summarySources`, `status`, …) keep
  describing the LATEST turn only, byte-compatible with every consumer that
  predates `turns` — MCP `ask_wiki`/`get_wiki_answer` and a plain
  `GET /v1/wiki/qa/<id>` both keep working unchanged; `turns` is purely
  additional context for a multi-turn console view.
- **MCP stays single-shot — deliberately, not an oversight.** `ask_wiki`/
  `get_wiki_answer` (`apps/mewbo_mcp/src/mewbo_mcp/server.py`) gained NO
  `answerId`/follow-up parameter. This issue is scoped to the first-party web
  console experience; adding continuation to the external MCP tool surface
  would over-engineer the harness for a use case (external agents) that
  doesn't need it. An MCP caller can still read a continued answer's full
  `turns` history via `GET /v1/wiki/qa/<id>` — it just can't itself drive a
  new turn.
- **`QaFinalizer.current_turn_events` scopes reconciliation to ONE turn — this
  is load-bearing, not cosmetic.** A follow-up's `wiki_emit_answer` restarts
  block indices at 0, exactly like the first turn's. `QaFinalizer.close`
  (`_blocks_from_events`, `_accessed_from_events`, `_summary_sources`) and
  `QaSessionEndHook._nudge_if_silent`'s silent-check both used to scan the
  WHOLE cumulative event log — a second turn's index-0 block would silently
  collide with the first turn's in `_blocks_from_events`'s index-keyed dict,
  and the idempotency/silent checks would see a PRIOR turn's `complete`/
  `block_open` and misfire. Every `meta` event (emitted once per turn by both
  `start()` and `follow_up()`) already marks a turn boundary on the log, so
  `current_turn_events` slices to events strictly after the LAST `meta` — no
  new event type needed. `models_used` (`QaFinalizer.enrich`) is the one field
  that stays session-WIDE on purpose (rides the whole transcript, not scoped
  per turn) since it's a "which models ran at all" observability field, not
  per-turn content.
- **Organic orchestration — no hardcoded fan-out, no turn-history cap.** A
  follow-up does NOT force a fixed "re-probe vs. reuse" behavior. The prior
  turn's probe findings are already in the hypervisor's carried-over session
  context; on a continuation turn the hypervisor decides — same as it always
  has (`wiki-qa.md`: fewest probes that cover the question, no fixed cap) —
  whether that context suffices or fresh `wiki-qa-probe` leaves are worth
  spawning. No "follow-up mode" flag, no hardcoded probe count, no `session_id`
  turn-count cap: context growth relies on the session's existing compaction,
  same as every other continued session.

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
graph. Non-obvious decisions only (full spec + research refs):

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
