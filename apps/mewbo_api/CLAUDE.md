> ↑ [root /CLAUDE.md](../../CLAUDE.md) · children: [wiki](src/mewbo_api/wiki/CLAUDE.md) · [agentic_search](src/mewbo_api/agentic_search/CLAUDE.md) · [apps](src/mewbo_api/apps/CLAUDE.md)

# Mewbo API — Project Guidance

Scope: `apps/mewbo_api/`. A **thin product surface** — HTTP routes, wire contracts,
transport, persistence, channel/MCP glue. Reusable engines belong in a capability library
(`mewbo_graph`), composed via an extra; don't grow domain logic inside `apps/`.

Read the deepest child that applies before editing: `wiki/CLAUDE.md` (MewboWiki BE glue),
`agentic_search/CLAUDE.md` (search run lifecycle + the runner seam), `apps/CLAUDE.md`
(Mewbo Apps), and `packages/mewbo_graph/CLAUDE.md` for the substrate this app composes —
in particular its "Git auth" section before ANY change that shells out to git.

## Route paradigm — a DI'd controller, not module globals

A new route module gets an **atomic controller class**: collaborators (service, store,
policy, runtime, auth guard) injected as FIELDS, every serialization / existence / domain
helper a METHOD, and the Flask surface a thin HTTP adapter receiving that one controller by
dependency injection. **The request path reads ZERO module state.**

- **Flask-RESTX → `resource_class_kwargs`.** `triggers/routes.py` is the reference:
  `init_trigger_routes` builds one `TriggerRoutesController` and injects it into every
  `Resource` (a `_ControllerResource` base captures it alongside the `Api` positional). The
  surviving module-level `_controller` is a **composition-root handle only** — production
  never reads it (Flask bakes the kwargs into the view closure at registration); it exists so
  a route test can point the registered controller at fresh stores by reassigning fields.
- **Blueprint → construct per request.** `wiki/settings.py` is the reference: the thin
  handler builds a `WikiProjectSettings(store, developer_mode=…)`, whose `WikiHTTPError`
  raises map to the wire through the already-registered errorhandler.
- **The module-global wiring is pre-existing DEBT — new code must not imitate it.**
  `channels/routes.py` (`_runtime`/`_hook_manager`/`_registry`/`_dedup`) and the globals in
  `vcs_pickup.py`, `ide_routes.py`, `agentic_search/routes.py`, `realtime/routes.py`.
  Migration is **mechanical** and is the expected cleanup when you next touch one: several
  already hold a perfectly good atomic class behind the global (`VcsPickupService`,
  `IdeManager`) — fold the module helpers onto it and inject it.
- **Wire models are Pydantic with `ConfigDict(extra="forbid")`** (`ProjectSettingsPatch`,
  `CredentialUpsert`, `VcsPickupBody`) — a client smuggling a server-owned field gets a 400
  at the boundary, not a silent no-op. Flask-RESTX `fields` models stay **doc-only** (their
  `example=` drives the Scalar sample bodies); they are never the validator.
- **The terminated-session 410 envelope has ONE home:** `ApiResponseKit.TERMINATED_ERROR_BODY`
  / `.terminated_response()` (`responses.py`). `backend.py`, `triggers/routes.py` and
  `errors.py` all read it and the console's `session_terminated` sentinel matches it
  byte-for-byte, so it must never drift. It lives on the response kit — which those surfaces
  already import — rather than a bare constant module, which would re-open a `backend.py` ↔
  routes import cycle.

## Access control — declared ABOVE the handler

Every route carries a `PermissionGuard` binding (`auth/permission_guard.py`); the requirement
is part of the signature, not the first two statements of the body. `requires(...)` /
`requires_master(...)` validate their ids against the closed `mewbo_iam.PermissionCatalog` AT
DECORATION — module scope, which in this app is boot — so a typo'd `"session.read"` fails the
server before it accepts traffic instead of surfacing as a 403 on a route nobody exercised.

- **`public()` and `dual_channel()` DECLARE; they do not enforce.** Both add ZERO
  request-time behavior — the handler's own first statement remains the enforcement. Nothing
  catches a handler that forgets its check, so `enforced_by` names the symbol where the
  fall-through actually lives: an auditor gets something to READ, which is a pointer, never a
  proof. What the declaration buys is that `audit()` can distinguish "open by design" from
  "someone forgot" — an UNBOUND route is the finding, and a route that is
  authenticated-but-not-by-a-permission must not be mislabelled `public` to silence it.
- **A route whose permission failure FALLS THROUGH to a second credential cannot be expressed
  as one permission.** That is why `dual_channel` exists rather than a second `requires` mode:
  under `requires`, a key that authenticates but lacks the permission is REFUSED, while on
  these routes it must degrade to the alternate channel exactly as an anonymous caller does
  (else every served app breaks and the self-service key tier disappears). Absorbing that
  channel into the decorator would mean re-deriving state the handler already holds — the
  served app's `app_id` out of the URL, the caller's own subject on a self-service mint —
  i.e. a second copy of enforcement that drifts from the first.
- **Auth OFF must be byte-identical to before IAM existed.** With `api.auth.enabled` false the
  kit resolves every request to the legacy full-power principal: no IAM store file written, no
  IAM/SCIM routes mounted, no IAM module imported on the request path, and 401/403 bodies
  matching the pre-IAM strings exactly. Corollary that is easy to get backwards: an INVALID
  `api.auth` block is a hard boot failure when auth is on (as is a configured authenticator
  whose optional extra is missing) but is logged-and-ignored when it is off — a deployment
  that never turned auth on must not fail to boot over config it does not use.
- **A refusal is a typed exception, never a loose dict.** Handlers raise from `errors.py`,
  where each class owns its status and renders a validated payload; the registered
  errorhandler does the rendering, so a refusal can travel up out of a controller method or a
  store wrapper without every caller threading it back as a sentinel. The API genuinely has
  TWO wire shapes — the `{"error": {code, reason, retryable}}` envelope and the legacy
  `{"message": ...}` — and the shape belongs to the SURFACE, not to the failure kind. Match
  what the handler you are migrating already returns; read its `return` statements rather than
  assuming, because picking the other one is a wire regression.

## Performance — budgets, the thread budget, how to measure

Root CLAUDE.md → "Performance is a contract at every boundary" carries the law and the
cost-class vocabulary. This section is the API-specific layer.

**Every handler's docstring states what its cost scales with.** On this surface the docstring
is not a note to the next maintainer — RESTX publishes it into `docs/openapi.json` and the
Scalar reference, so the bound lands where the CALLER reads it. A handler that declares
nothing is the defect one step before the handler that declares a bound it does not hold.

### Per-endpoint budgets

| Handler kind | Budget | Class it may declare |
|---|---|---|
| Create / enqueue — `POST /api/sessions`, `POST .../query` | ≤ 50 ms | `O(1)` |
| Poll — `GET .../events?after=` | ≤ 50 ms when nothing matched | `O(new events)` |
| Detail read — one session / job / repo | ≤ 300 ms, bounded payload | `O(one record)` |
| Listing — `GET /api/sessions` | ≤ 500 ms | `O(collection)`, **with a bound** |
| Long-lived / streaming — `/stream`, SSE, blocking waits | not a time budget | declare the CONCURRENCY bound |

Known defects against those budgets, each a shape to recognise elsewhere:

- **`SessionRuntime.list_sessions` (`mewbo_core/loop/session_runtime.py`) calls `load_transcript`
  per session id** — a listing that reads its children, `O(all history)` wearing a listing's
  clothes. Its `owner` narrowing is pushed into the store, which bounds WHICH transcripts are
  read, never what each one costs. A summary field a listing needs is derived in the store with a
  projection or maintained on the parent.
- **A cursor that narrows only the response is not a cursor.** `?after=<future ts>` on `/events`
  matches zero events and returns a few hundred bytes while the work stays proportional to the
  record's whole history — so **measure the time, never the payload.** Both halves are store
  primitives (`load_events_after`, an indexed `(session_id, ts)` range; and `session_digest`).
  **The spliced `status` still describes the WHOLE session**, never the window: narrowing it to
  the cursor makes the number look better and the answer wrong.
- **An unparseable `after` must 400, never widen to everything.** The footgun is specific: this
  API's own `ts` values contain `+`, so a client echoing back a timestamp without
  percent-encoding sends `+` as a space, the parse fails, and a fall-open filter silently ships
  the entire transcript at HTTP 200. **An empty `after` is NO cursor, not a bad one** (`?after=`
  means everything, which is what a first poll sends), so the refusal is gated on a non-empty
  value; and **the parse lives in ONE place** (`mewbo_core.session.event_cursor`), which the
  route calls to validate — re-deriving `fromisoformat` here lets the wire contract and the
  filter drift.
- **`/events` has no window, cap or page**, and grows with the record. `SessionTimeline`
  (`/sessions/<id>/timeline`) is the shape to copy for the RESPONSE half — it excludes each
  turn's events (`exclude={"turn": {"events"}}`) so the log is not repeated once per turn,
  returning tens of KB where `/events` returns tens of MB. It does not fix the server-side read;
  bounding both is the job.

### The concurrency budget — the thread pool IS the API's total concurrency

`docker/Dockerfile.api`'s `CMD` runs
`gunicorn --workers 1 --worker-class gthread --threads ${API_THREADS:-48} --timeout 300`.

**`--workers 1` is a correctness constraint, not a tuning choice.** `RunRegistry`,
`SessionEventBus` and the app-lifecycle locks are all in-process, and several places already
depend on there being exactly one (`apps/CLAUDE.md` on in-process locking, `apps/lifecycle.py`
and `apps/pipeline_tracker.py` on single-firing, `run_sweep.py` on a once-per-restart sweep);
a second worker would split run state and leave half the SSE subscribers unreachable. So there
is exactly ONE process, and its thread count is the whole deployment's request concurrency.

- **A long-lived handler holds its slot for its whole LIFE, not for the work it does.** On an
  8-thread configuration, 8 held-open SSE streams take `GET /api/models` from 0.040 s to 13.0 s.
  Recognise the SHAPE: the symptom is not "streaming is slow", it is "an unrelated trivial
  endpoint is dead". A bigger pool moves that cliff; nothing removes it.
- **Adding any long-lived path — SSE, long poll, blocking wait, synchronous clone — requires
  stating two things in the docstring: how many may exist at once, and what the (N+1)th caller
  gets.** Refusing with a diagnosable status is correct; queueing silently behind the pool is
  what wedges the process. `RepositoryCheckout.CLONE_TIMEOUT_SECONDS` (240 s, sized inside
  gunicorn's 300 s worker timeout) is the existing example of the bounded half.
- **Count threads held, not requests served.** A handler that fans out to its own threads
  spends from the same process budget — which is why `SourceProbe`
  (`system_instructions/value_sources.py`) is one long-lived daemon thread per source with
  second callers JOINING the in-flight probe, rather than a pool per request. **A bounded
  deadline bounds the WAIT, never the WORKER.**
- Check the live setting rather than trusting the Dockerfile — an override or env var may
  differ from what is in the tree:
  ```bash
  docker inspect assistant-api-1 --format '{{json .Config.Cmd}}'
  ```

**⚠️ The source tree is BIND-MOUNTED, so the container's files are current while its loaded
modules are not.** Gunicorn imports at boot, so a fix that landed after the container started is
on disk and absent from the serving worker. Every way of checking the code *inside* the container
confirms the wrong thing — including `docker exec … python -c "import m; print(m.X)"`, because
that spawns a fresh interpreter which re-imports from the current file. Only the serving worker
holds the stale module.

Two checks that are honest, and they are the only two:

```bash
docker inspect assistant-api-1 --format '{{.State.StartedAt}}'   # vs the fix's commit time
```

or measure the artifact's BEHAVIOUR through the API. **Recognisable signature: a stored value
equal to a DEFAULT the current source no longer uses** — a default the code cannot produce is a
stale import, not a logic bug. Measured instance: a device screenshot reached the model as base64
text under a 200,000-char cap while the source declared 30,000 and carried the lifting seam; the
worker predated that commit by about five hours, and the "defect" needed a restart, not a patch.

### Profiling recipes

Run against the running container. The API key is `MEWBO_MASTER_API_TOKEN`, sent as the
`X-API-KEY` header (SSE endpoints take it as `?api_key=` instead, since EventSource cannot set
headers). It lives in whichever file the deployment's `env_file:` resolves to — the gitignored
repo-root `.env` by default, or wherever `MEWBO_ENV_FILE` points.

```bash
export MEWBO_KEY="$(grep '^MEWBO_MASTER_API_TOKEN=' "${MEWBO_ENV_FILE:-.env}" | cut -d= -f2-)"
export MEWBO_API=http://localhost:5125
```

**1 — Time one endpoint, in time AND bytes.** Both, always: a shrinking response hides
constant work, and a fast response can still be megabytes.

```bash
curl -s -o /dev/null -H "X-API-KEY: $MEWBO_KEY" \
  -w 'ttfb=%{time_starttransfer}s total=%{time_total}s bytes=%{size_download}\n' \
  "$MEWBO_API/api/sessions"
```

**Run it against a SMALL record and a LARGE one and compare the ratio** — one absolute number
cannot tell you what a cost scales with. "397 B in 0.32 s" only becomes a diagnosis next to
the same call on a 15-event session at 0.010 s.

**2 — Find the concurrency cliff.** Hold N streams open, then time something trivial and
UNRELATED. Timing the streams themselves shows nothing: the starvation is global.

```bash
N=8
for i in $(seq "$N"); do
  curl -sN "$MEWBO_API/api/sessions/$SID/stream?api_key=$MEWBO_KEY" >/dev/null &
done
sleep 2
curl -s -o /dev/null -H "X-API-KEY: $MEWBO_KEY" \
  -w "held=$N models=%{time_total}s\n" "$MEWBO_API/api/models"
kill $(jobs -p)
```

Vary N around the configured `--threads` value. The cliff is the N where the trivial call
jumps; that N is the deployment's real concurrency ceiling, whatever the flag says.

**3 — Ask Mongo what the query actually did.** Credentials come from the mongo container's
environment (`MONGO_INITDB_ROOT_USERNAME` / `MONGO_INITDB_ROOT_PASSWORD` in
`docker-compose.yml`, values in the gitignored `.env`) — read them from there, never write
them into a tracked file.

```bash
docker exec -it assistant-mongo-1 mongosh -u "$MONGO_USER" -p "$MONGO_PASS" \
  --authenticationDatabase admin mewbo --eval '
    db.events.find({session_id: "<id>"}).sort({ts: 1})
      .explain("executionStats").executionStats'
```

| Read this | It tells you |
|---|---|
| `executionStages.stage` | `IXSCAN` = an index served it; `COLLSCAN` = it read the collection |
| `totalDocsExamined` vs `nReturned` | the amplification. Equal is ideal; a large ratio means the filter is not in the index |
| a `SORT` stage anywhere | Mongo sorted IN MEMORY — the index did not supply the order, and it aborts past its memory cap |

`MongoSessionStore._ensure_indexes` (`mewbo_core/session/session_store_mongo.py`) is the ONE
home for what is indexed and why — read its docstring rather than guessing. A read that
filters or orders on anything it does not cover is a scan; fix it by adding the index in the
store, not by sorting in the handler.

**4 — Time an internal phase without a profiler: the store IS the timeline.** The gap between
two events for one session measures the phase between them.

```bash
docker exec -it assistant-mongo-1 mongosh -u "$MONGO_USER" -p "$MONGO_PASS" \
  --authenticationDatabase admin mewbo --eval '
    db.events.find({session_id: "<id>"}, {ts: 1, type: 1}).sort({ts: 1}).toArray()'
```

Subtract adjacent `ts` values across the pair that brackets the phase — `run_accepted` → first
`llm_call_start` is start-up cost this app owns; `llm_call_start` → `llm_call_end` is model
time it does not. That split separates "the server is slow" from "the client is not rendering
what already arrived"; measure both ends before blaming either.

### Two rules the root's five don't cover

The root CLAUDE.md's five rules (no listing reads its children; no detail gated on a collection
query; a cursor narrows the WORK; a filter refuses rather than falls open; unbounded is a defect)
apply here verbatim — don't restate them. Two are specific to this surface:

- **Never compute whole-collection or whole-record status on a per-poll path.** A poll runs once
  per second per open client, so anything `O(one record)` inside it is `O(record × poll rate)`.
- **A handler that fans out spends from the process thread budget**, not from its own. See the
  concurrency budget above.

### Adding an endpoint — checklist

- [ ] Docstring names the cost class and, for anything long-lived, the concurrency bound plus
      what the (N+1)th caller gets.
- [ ] Every store call it makes is bounded — no `load_transcript`/`list_*` in a loop, no
      whole-transcript read on a polled path.
- [ ] Every query param that narrows is validated at the wire and refuses on bad input.
- [ ] The response has a ceiling that does not grow with a record's history.
- [ ] If it touches Mongo: `explain("executionStats")` shows `IXSCAN`, no in-memory `SORT`, and
      `totalDocsExamined` close to `nReturned`.
- [ ] Measured on the running container with recipe 1, against a large record as well as a
      fresh one — **a green suite is not a performance measurement.** Write the numbers down.

## Runtime flow

Entry point: `backend.py`. The full route inventory is the Scalar reference
(`docs/openapi.json`, regenerated by `make openapi`); only the non-obvious behaviours are here.

- **`POST /api/sessions` / `POST .../query` accept an optional external `cwd`** (top-level or
  `context.cwd`). `ExternalCwdPolicy` (`backend.py`) is the one seam, and it asks TWO questions,
  not one: is `api.allow_external_cwd` on (default OFF), **and is this a path the server already
  owns**. Gating on the mere PRESENCE of the field was the defect — a directory the server itself
  minted was refused exactly like a host path a stranger named, so a follow-up re-sending its own
  session's `cwd` 403'd while `/message` re-engagement resolved the same directory happily.
  Server-known means either the session's persisted `cwd` (an echo of a value the server issued)
  or `ProjectCatalog.owns_path` — a configured project, a managed project or worktree, or a
  registered repository's checkout. Either way the path must still be an existing directory
  (else 400); a managed project can be reaped, so a listed path is not a live one.
  **Order is load-bearing:** binding first (`O(1)`, no store), catalog second
  (`O(collection)`), and the catalog is reached ONLY when the flag is off and the path is not
  the session's own — so a currently-working request pays nothing on a handler budgeted `O(1)`.
  A dead project store makes `_server_knows` answer False, never True: a filter that cannot be
  applied refuses rather than failing open. **On `/query` the gate runs AFTER
  `_session_specs.load`**, because the session's bound directory is what tells an echo from a
  claim — and because running it first hard-refused a field `merge_request_overrides` was about
  to log-and-ignore anyway (`cwd` is `OVERRIDABLE_WHEN_UNBOUND`). This grants no reach a caller
  lacks: the same directory was always reachable by naming its project. An explicit cwd WINS
  over the project-derived one and is persisted as `context_payload["cwd"]` so `/message`
  re-engagement and `_resolve_session_cwd` keep resolving it. Registering provided-path
  v_projects is NOT a substitute: the worktree reaper permanently deletes childless
  provided-path parents. Docker rule applies — the path must be visible in the api container at
  the identical path.
- **`GET /api/sessions`** — each summary carries `origin` (`user|wiki|search|channel`, computed
  in core `summarize_session` and forwarded verbatim; the console badges/filters on it). Filter
  params `include_archived`, `pinned` (tri-state — omit to list both) and `project`
  (repeatable; matches a session that has worked in EITHER — the accumulated set, never just
  the CURRENT binding) compile to a core `SessionQuery` and are decided at the STORE, before
  any transcript is read. Pinned sort first, then newest-first.
- **Inline `@<ref>` context expansion runs at the submit seam** — in `POST .../query` after
  cwd-resolution and before `start_async`, and at the sync `POST /api/query`. The reusable
  `ReferenceExpander` lives in **`mewbo_tools`** so the in-process CLI shares it; an app can't
  import another app, so the engine sits one layer down and the API just builds the per-call
  inputs. It resolves `@file`/`@dir/`/`@diff`/`@url` through EXISTING renderers
  (`attachments.parse_to_markdown` for docs+URLs, `git diff HEAD`, `FileCatalog`/`os.scandir`) —
  no per-type parser. **Scoping:** `@file`/`@dir` resolve ONLY to files in the project's git index
  (`FileCatalog` = `git ls-files --cached --others --exclude-standard`, so `.gitignore`d secrets
  are out of scope) or to session attachments; non-git dirs fall back to cwd-confined existing
  files. Guardrails: per-ref + aggregate char caps with **truncate-not-reject**, dedupe, no
  recursion, and any unresolved/out-of-scope ref (missing path, non-repo `@diff`, dead URL,
  `email@host`) passes through literally. `expand_references()` never raises into the request
  path. `GET /api/files?project=&session=&q=&limit=` serves the same catalog to the composer's
  `@`-autocomplete.
- **`GET /api/sessions/{id}/stream`** is **event-pushed** via core `SessionEventBus` — no poll, no
  per-event transcript re-read. Optional `?after=<ts>` trims the once-only backlog replay to that
  timestamp or later (INCLUSIVE, since several events can share a ts — a reconnecting client
  de-dupes by content rather than skipping whatever shares its cursor). Generator: subscribe →
  backlog replay → a `session_state` frame (`SessionStateFrame`, the SAME projection `/events`
  splices in flat) → queue-fed tail, content-key dedup of the subscribe↔backlog race,
  drain-before-`stream_end` (else the terminal `completion` event is dropped) → a second
  `session_state` frame, since the terminal values only settle then. Liveness
  (`session_runtime.is_running`) is read BEFORE each loop iteration picks its blocking timeout, so
  an idle or finished session's queue read is non-blocking and the stream closes in milliseconds
  rather than waiting out the 15 s heartbeat — what makes holding it open for the life of every
  console session view affordable.
- **`POST /api/sessions/{id}/pin` / `DELETE ...`** shares the archive Resource shape
  deliberately (one route, POST-then-DELETE, not a second `/unpin` path). Pinning is an
  ORDERING signal only: a pinned session stays subject to every other active filter, so a
  mobile-origin session pinned on Aura never leaks into the console's default-visible list.
- **Realtime endpoints** (`init_realtime`) register `/v1/draft/stream` only — token SSE, with
  `DraftStreamer.astream()` bridged to the sync Flask generator via ONE per-request event loop,
  single-shot. `/v1/structured` is registered separately and carries two modes:
  `mode:"synthesis"` is the no-loop retrieval-only single round-trip
  (`structured.synthesis.SynthesisRunner`, reusing `RealtimeSessionRecorder.for_fast` — tag
  `structured:fast` → session type `structured_fast` — plus `WikiGroundingProvider`), returning
  inline as `{run_id, status:"completed", output, citations, workspace}`; the agentic mode
  stamps at `StructuredResponder._prepare` (tag `structured:run`). The `run_id` handle
  (`<session_id>:r1`) resolves via `GET /v1/structured/{run_id}` either way.
- **Realtime is session-full with write-behind.** Both realtime paths mint a session, trace, and
  persist a single-turn transcript via `RealtimeSessionRecorder` (`realtime/recorder.py`,
  app-side: it needs the session store). The seam splits "session-full" into two halves that must
  NOT be conflated: (1) `recorder.trace()` opens `langfuse_session_context` on a PRE-MINTED
  `session_id` (a bare `uuid4().hex` — no store I/O) with provenance derived from the tags and
  context it is *about* to write, and the LLM call runs inside it; (2) `recorder.persist()` does
  every durable write AFTER the response/last token, on a daemon thread via `persist_async` — so
  draft TTFT never pays for a store write. Wire contract is additive-only: synthesis gains
  `run_id`/`session_id` inline; draft gains `session_id` on the terminal `done` frame plus an
  `X-Mewbo-Session` header (token frames untouched). `_runtime is None` degrades to trace-only.
- **Optional `model` override on both realtime-family endpoints** (a LiteLLM name; non-string →
  ignored → configured default). Threading: synthesis → `SynthesisRunner` →
  `StructuredSynthesizer(model_name=...)`; draft → `DraftStreamer(model_name=...)`; agentic mode
  → the ONE route seam `StructuredResource._build_responder` (default path passes `model_name=`;
  the graph-first path takes it via `dataclasses.replace` after `_graph_first_responder` returns
  — never edit `agentic_search/**` for this). API-level only: no MCP knob, no config setting.
- **Channel webhooks** (`POST /api/webhooks/<platform>`) use platform-specific HMAC, not the API
  key. Adapters are instantiated from `config.channels` by `init_channels`; a completion callback
  is appended to `hook_manager.on_session_end`. Channel sessions are standard sessions
  (Mongo-backed, visible in the console) tagged `nextcloud-talk:room:<token>` or
  `email:thread:<channel_id>:<root-msg-id>`. A shared `_process_inbound()` pipeline (dedup,
  mention gating, session resolution) serves both the webhook endpoint and the email IMAP poller.
  Email access control: an `allowed_senders` allowlist plus an `@Mewbo` mention required in
  multi-party threads, none for 1-to-1. Slash commands are a decorator-based `@command` registry
  in `channels/routes.py` (`/help` auto-generates from it) and run without an LLM. Each
  `ChannelAdapter` provides a `system_context` string injected via `skill_instructions`.
- **Web IDE**: opt-in per-session code-server containers via `agent.web_ide`. `IdeManager` +
  `IdeStore` in `ide.py`, routes in `ide_routes.py`, Mongo required. Privileged work is confined
  to ONE injected `IdeContainerBackend` — `DockerContainerBackend` (local socket) or
  `BrokerContainerBackend` (HTTP to `apps/mewbo_ide`, which holds the socket instead). Two rules
  keep the implementations honest against each other:
  - **The protocol is keyed by SESSION ID, never by container name.** The name is derived from
    the session id (`IdeInstance.name_for`, the one home for that rule), so a name-keyed
    protocol would make the broker backend un-derive it on the way back out. The route layer
    still reads `IdeInstance.container_name`; that is a display concern, not a protocol one.
  - **An operation the broker performs atomically is ONE protocol method.** `launch` merges the
    deadline write with the container start, and `teardown` merges the container removal with the
    deadline unlink, because the broker's `POST`/`DELETE` each do both in one request; split, a
    stop costs two round trips whose second `removed` is always `false`. `IdeManager._forget` is
    the single implementation of "drop every trace of a session" — `stop()`, the failed-launch
    rollback and both lazy drift reconciliations all call it.
  - **Which directory a session mounts is an ORDERED WALK of tiers, first match wins**
    (`ide_routes.py`: `IdeWorkspaceResolver` over `CatalogProjectMount` → `WikiCheckoutMount` →
    `AppStagingMount`, assembled by `IdeWorkspaceResolver.over_catalog`). Each tier owns its own
    binding rule and its own refusal, so a fourth surface is a new tier class, never an arm in
    the walk. A tier answers `None` for "not my kind of session" and raises
    `IdeWorkspaceUnavailable` for "I recognise this session and still cannot give it a current
    directory" — the split matters because the generic "session has no project in context" 409
    tells a console user nothing about a wiki project whose checkout was reaped. A tier's
    INCIDENTAL failure (dead store, missing optional extra) is logged and falls through to the
    next; only the deliberate refusal stops the walk, so a wiki outage cannot cost a configured
    project its IDE.
  - **The first tier resolves the context `project` key through the ONE `ProjectCatalog`, and
    that is the whole point of it.** It reads the key with the bounded
    `latest_event_of_type(..., payload_key="project")` store call, then hands it to the catalog
    — which is injected as a `Callable[[], ProjectCatalog]` through `init_ide` from
    `backend.py`'s `_catalog()` accessor, a callable because that accessor re-points the catalog
    at the live config and project store per call. Reading `get_config().projects` here instead
    recognised a CONFIGURED project and nothing else, so every session anchored to a managed
    project (`managed:<id>`), a worktree or a repository slug — which is what the console's own
    picker writes — was refused an IDE with a message saying it had no project at all. The
    catalog's `ProjectResolutionError.code` decides which answer this is: `not_found` / `empty` /
    `auto_sentinel` mean "not this tier's kind" and fall through, anything else (`unavailable`,
    `no_checkout`) is the tier's own refusal and travels out carrying the catalog's message
    verbatim — it already names the path and the Docker mount rule, so restating it here would
    be a second copy that drifts. The mount is stamped with the resolved `ProjectEntry.name`,
    never the raw key: that string is persisted on `IdeInstance` and shown in the console
    capsule, where `managed:<uuid>` names nothing a user recognises.
  - **The wiki tier reads the server-stamped `wiki:maintain:<slug>` TAG, never the `slug`
    context key — and this is the one a future reader will try to "simplify".** A request's
    `context` is merged VERBATIM into the session (`backend.py:_build_context_payload` refuses
    no unknown key), so the key ADDRESSES a project but authorizes nothing: reading it would
    let any caller name any indexed project and be handed a container over its checkout. The
    tag is stamped only by the maintainer route, only after validating the slug, and cannot be
    re-pointed by a later turn. It resolves through `WikiJobCtx.for_maintainer`, which is also
    where the checkout path comes from — never re-derive the clone layout here.
  - **The apps tier MATERIALIZES the staging directory on demand.** An app's source lives in
    its manifest and reaches disk only when something writes it, so ABSENCE is the normal case
    and a tier that merely mounted an existing directory would work almost never. The write is
    `AppStagingArea.materialize` (`apps/staging.py`), the same one `get_app`'s `stage`
    operation calls — one implementation of where an app's files land, and of the two
    containment rules that keep a hostile stored `app_id` inside its own session's directory.
  - **⚠️ An existence check inside the api process proves NOTHING about what the daemon sees
    for the same string.** `is_dir()` resolves in the API's own mount namespace, so it
    establishes that a checkout exists in the mounted VOLUME — not that the identical path
    string names the identical directory to the docker daemon, which resolves a bind source
    against the HOST. The two genuinely diverge: `/tmp/mewbo/wiki/clones` is a named volume in
    the api container while a stale, unrelated directory of the same name exists on the host,
    and the apps root has no volume at all (it is container-ephemeral). No resolver running
    inside the api container can detect that mismatch — it is closed on the MOUNT side.
  - **A workspace under a configured volume root is mounted as a VOLUME with a subpath, never
    as a bind.** A bind would hand the daemon a path it resolves on the host, so the container
    would open the stale host tree — successfully, with nothing failing and no error to read.
    Silently mounting the wrong files is strictly worse than refusing, which is also why a tier
    must refuse rather than return a directory it is not confident is current.
- Auth: `X-API-KEY` header (webhooks excepted), defaulting to `api.master_token`. `api_key` is
  accepted as a query param on SSE endpoints, since EventSource cannot set headers.
- CORS: an `after_request` hook sets `Access-Control-Allow-Origin: *`.
- Hooks: `HookManager.load_from_config(_config.hooks)` at startup; `hook_manager` is passed to
  every `start_async()` call site. Supports `type: "command"` and `type: "http"`.
- Plugins: `mewbo_core.plugins` drives discovery/install/uninstall; plugin components (skills,
  hooks, AgentDefs, MCP tools) load during session init via `load_all_plugin_components()`.
- Event payloads: `action_plan` steps are `{title, description}`; tool events use `tool_id`,
  `operation`, `tool_input`.

## SessionSpec — durable purpose binding

`SessionSpec` (`session_spec.py`) is the durable purpose-binding of a session — origin,
project/cwd, model + fallback ladder, tools + strict-scope, capabilities, and whether
skill-instructions are present. `merge_request_overrides` is the ONE seam deciding which fields
a request may override, tiered `ALWAYS_OVERRIDABLE` (model, fallback_models, mode) /
`OVERRIDABLE_WHEN_UNBOUND` (only while the session has no bound purpose) / `NEVER_OVERRIDABLE`
(origin, surface, capabilities); a refused override is logged, never silently applied. It ships
a GET `projection()` (`skill_instructions` reduced to a boolean `skill_instructions_present`,
never the raw playbook text) plus a server-declared `editable_fields()` map at
`GET /api/sessions/<id>/spec` (`SessionSpecView`) — the wiki-settings fail-closed pattern (a
field absent from the map is not editable, never editable-by-default): front ends hydrate from
it rather than inferring editability from absence.

⚠️ **ORIGIN MUST BE CLASSIFIED FROM TAGS, never from capabilities.** `SessionSpecStore.load`
falls back to rebuilding from the newest context event for any session with no stored mirror, and
`SessionSpec.from_context` classifies from a payload alone. Route both legs through
`SessionSpecStore.origin_for`, which reads TAGS — the same signal the creation path uses; it
lives on the store because it reads state the store owns. Classifying off `SessionOrigin.classify`'s
*capability* arms instead binds a session's purpose to what a CLIENT says it can render, so
removing a capability arm silently drops every affected session to `USER`, flipping
`purpose_bound` false and re-opening the whole `OVERRIDABLE_WHEN_UNBOUND` tier including
`project`. It does not stay a read-time slip: the unattended-wake path writes the loaded spec
straight back, so the first fire bakes the wrong origin in.

⚠️ **THE STORED MIRROR'S `origin` IS RE-DERIVED ON EVERY LOAD — never trusted verbatim.**
`SessionOrigin` is documented as derived at read time and never stored, but the typed mirror
persists it anyway, so a blob keeps whatever classifier wrote it. `load` passes
`origin=self.origin_for(session_id, payload)` into `from_blob`, overwriting the field BEFORE
validation, so a classifier fix heals every affected row on its next read. Every OTHER field on
the mirror stays authoritative. Consequence: a bad stored `origin` alone cannot trigger the
corrupt-blob recovery path — it is healed inline.

- **`POST /api/sessions/<id>/query` (`SessionQuery.post`) must load the persisted spec FIRST**,
  then apply only sanctioned overrides through `merge_request_overrides`; non-spec request keys
  merge via `setdefault` only, explicitly skipping `SessionSpec.SPEC_OWNED_CONTEXT_KEYS` so a
  refused override cannot re-enter by the side door. Re-deriving model/tools/cwd from the request
  instead persists a default model that corrupts later `/message` and `/recover`, and falls back
  to an empty per-session temp dir whenever nothing names a cwd.
- **`RunReadinessGate` refuses a run BEFORE it is persisted** when `llm.default_model` hasn't
  finished resolving, so a restarting worker returns a retryable 503 rather than accepting a query
  whose first model call dies on a missing credential. The readiness signal latches once true and
  never un-latches, and any probe failure OTHER than a config-load failure reports ready, so the
  gate itself can never wrongly refuse live traffic.
- **Capabilities are re-derived per unattended fire, mirroring `allowed_tools`.** One
  interactive turn that widened `client_capabilities` (e.g. advertising `ask_user`, which
  BLOCKS until a human answers) could otherwise leak onto every later scheduled fire with
  nobody there to answer. `SessionSpec.unattended_capabilities()` strips
  `INTERACTIVE_ONLY_CAPABILITIES` (`{"ask_user"}`) fresh on every fire; the shared idle-restart
  path under trigger delivery rebuilds the fire's context from it.

## Ask-user questions — api dispatch glue

`ask_user.py` hosts the concrete `ApiQuestionDispatcher` + `QuestionPendingCalls` +
`QuestionAnswerRouter` (the dispatcher registers beside the device-tool one at startup; core
contract in `packages/mewbo_core/CLAUDE.md` → "Ask-user questions"). The deliberate differences
from the device-tool bridge it mirrors:

- **No presence short-circuit**, even though the console holds ONE `/stream` connection
  open for the life of a session view. `useSessionEvents`'s reconnect model (a short delay before
  re-subscribing after a healthy close, exponential backoff after a failed one) means the bus can
  legitimately read zero subscribers for several seconds while a human is still on the page —
  exactly the false-negative shape that would kill a live question. The `ask_user` capability
  advertisement is the delivery gate instead; don't "add back" the device bridge's check.
  **The device bridge has the same false negative and answers it differently, because the two
  waits differ:** it must ask (a 30 s budget per call is the thing an absent client wastes), so
  `SessionEventBus.has_executor` admits an executor that DETACHED within `DEVICE_EXECUTOR_GRACE_S`
  — sized from the clients' own reconnect ladders, not picked. The bounds are what keep the fast
  refusal honest: a session that never had an executor gets no window, and a wrong "yes" costs the
  window plus a poll tick because presence is re-checked every tick. A question with no deadline
  has nothing to protect and so needs no window; do not copy the constant across.
- **No expiry/reaping machinery.** The dispatcher coroutine owns the entry's whole lifecycle
  (create → wait → take/withdraw in `finally`), so the registry has no deadline bookkeeping — a
  bounded call's deadline lives in the WAIT LOOP, measured on an injected `monotonic` field so a
  test drives expiry with no sleeping.
- **Answer validation is split by what each layer can know:** the route Pydantic-validates SHAPE
  (`QuestionAnswerItem`, XOR enforced at definition → 400, plus the `notes` cap); the registry
  validates SEMANTICS against the stored questions under its lock (count/bounds/arity → 422 with
  a user-actionable message, entry NOT consumed — the user can fix and resubmit).
  `X-Mewbo-Surface` becomes `answered_via` on the `user_question_answered` event.

**The registry is a rendezvous; the transcript is the record — that is the whole late-answer
design.** `QuestionAnswerRouter` owns the ONE decision of where an answer goes: a live waiter
takes it (`delivery: "run"`, the blocked tool call resolves), and when no waiter is left the
questions AND the `call_token` are recovered from the durable `user_question` event and delivered
as a new user turn (`delivery: "message"`).

- **A question is answerable until it is ANSWERED.** A `timed_out`/`declined`/`interrupted`/
  `cancelled` `user_question_answered` event records that the RUN stopped waiting, never that the
  question closed. Only a prior `answered` event (409) or a terminated session (410) refuses one;
  the answered event is durable, so a duplicate POST reads 409 on every path, including after a
  restart. **A surface must not grey out its card on a non-`answered` outcome.**
- **404 means "no such `call_id` in this session"** — a real absence, not "the waiter left". Token
  mismatch is checked BEFORE answered-state, so the same POST cannot read a different status
  depending on whether a waiter happens to be alive.
- **Late-answer semantics reuse `AskUserQuestionArgs`**, rebuilt from the event payload, rather
  than a second copy of `render_answers`' rules — which is why the payload carries
  `timeout_seconds`/`notes_placeholder` too.
- **Delivery goes through the ONE seam, `backend.py:_deliver_user_turn`** — `POST .../message`'s
  extracted body (steer a live run / re-engage an idle one with the session's persisted model,
  mode, tool scope, cwd and budget). The router takes it as an injected callable and never
  re-derives re-engagement; a second copy drifts on the next persisted-context field.
  `TurnDelivery` is declared in `ask_user.py` rather than beside the implementation because
  `backend.py` already imports that module and the reverse would cycle. A `refused` delivery (a
  run started between the two attempts) reports 409 and writes NO answered event, so the card
  stays answerable.

Tests: `tests/test_ask_user_routes.py`.

## Agent pickup — CI → session bridge

`vcs_pickup.py` (one atomic `VcsPickupService`, DI'd like `ide_routes.py`) is the **CI sibling
of the channel adapters**: platform event → tag-keyed session
(`vcs:<owner/repo>:<kind>:<number>`, cf. `nextcloud-talk:room:<token>`). It deliberately does
NOT implement `ChannelAdapter` (auth is the API key; no HMAC handshake exists), but the reply
leg mirrors the channels exactly: `completion_hook` on `on_session_end` posts the final answer
back to the issue/PR as a comment by the bot account, sharing
`channels.routes.extract_final_answer`.

- **Gitea Actions ≠ GitHub Actions payloads.** Gitea has no top-level `event.assignee` on
  assignment events — guard via a
  `contains(github.event.<issue|pull_request>.assignees.*.login, …)` fallback (side effect:
  re-assignment while the bot is already assigned re-triggers; harmless, the tag reuses the
  session). `issue.pull_request` IS present on comment payloads; `github.api_url` IS populated
  (`<server>/api/v1`); `Authorization: token $GITHUB_TOKEN` works on both platforms; the
  act_runner image ships jq but does NOT trust internal CAs (→ `AGENT_TLS_NO_VERIFY` adds
  `curl -k`).
- **`_resolve_repo_or_404`'s identity scan covers managed projects only.** A config project
  never promoted does not resolve by `owner/repo` — hence
  `VcsPickupService._config_project_for_repo` scanning config project paths with
  `RepoIdentity.aliases_for_path` as a fallback. Don't "fix" this by registering pickup targets
  via `POST /v_projects` with an explicit path: the worktree reaper deletes childless
  `path_source == "provided"` parents **permanently**, while config projects self-heal through
  promote-on-demand.
- **Issue pickups get an isolated worktree from HEAD too, not just PRs.**
  `ensure_issue_worktree` cuts a deterministic `mewbo/issue-<n>` branch from the default-branch
  HEAD so concurrent issue pickups never collide in the shared checkout and the agent has a
  clean push-ready branch. The `mewbo/` prefix means the reaper deletes the branch with the
  worktree; the deterministic branch makes a repeat pickup resume rather than re-base
  (`base=None` once it exists). KEY ASYMMETRY: it is **best-effort** — a non-git or
  unpromotable project degrades to the main checkout (returns `None`), never a hard 422 like a
  PR's *required* head branch.
- **Deployment needs git credentials in the api container.** The pickup fetches PR branches and
  agent sessions push to them; the image sets `credential.helper=store` but ships no
  credentials — mount the host's `~/.git-credentials` to the container user's HOME. Without it:
  422 `could not read Username`.
- **Reply tokens live server-side, keyed by forge host** (`channels.vcs.tokens` config) — the
  workflow's `GITHUB_TOKEN` dies with the job, long before the agent run ends, so it cannot
  deliver the reply. `/repos/{owner}/{repo}/issues/{n}/comments` + `Authorization: token` are
  identical on GitHub and Gitea (one client, both forges). Gitea gotcha: minting a PAT for
  another user (`POST /api/v1/users/<bot>/tokens`, admin-only) rejects token auth with
  `auth required` — use **basic** auth. Unlike the act_runner, the api container's system CA
  store trusts an internal CA (git and Python `ssl` share it), so `tls_verify` stays default.
- Endpoint auth accepts KeyStore-minted keys (`POST /api/keys`), not just the master token — CI
  secrets should hold a labeled revocable key.
- **⚠️ `extract_final_answer` reads TWO event kinds and has no `else` — a turn whose only content
  is a non-text event posts an EMPTY reply.** It walks the transcript backwards for
  `completion.task_result` then `assistant.text`; anything else is invisible. `present_ui`
  (`mewbo_core.builtin_plugins.generative_ui`) puts its content on a `generative_ui` event
  carrying `alt_text` — a prose rendering that exists precisely so a non-rendering surface can
  show it — and **only the `generative_ui` capability gate keeps that off this path today, since
  only the console advertises it.** The moment any channel, the vcs-pickup leg, or another
  reply-capable surface advertises that capability, this is silent DATA LOSS: an empty comment
  posted where the answer should be. Both reply legs share this ONE function, so the fix is one
  arm here, not two. **Whoever widens that capability owns adding it.**
- **PR-creation identity comes from the forge CLI login, not the git credential store.** `gh`
  only speaks GitHub, so on Gitea the agent needs `tea`; without a forge CLI the agent
  self-serves by reading the PAT out of the mounted `~/.git-credentials` and curling
  `POST /repos/{owner}/{repo}/pulls`, which (1) authors PRs as the mounted PAT's human owner and
  (2) leaks that PAT in plaintext into the session transcript (Mongo/Langfuse).
  `docker/init.d/15-tea-setup.sh` installs-if-missing and logs `tea` in per `channels.vcs.tokens`
  host (bot identity), and the pickup prompt nudges "prefer a forge CLI like `tea` or `gh`".
  The bot PAT needs `read:user` (tea login resolves the user) + `write:issue` (reply comments) +
  `write:repository` (PRs) — a `write:issue`-only token 403s on `/api/v1/user` and tea login
  fails. Git *pushes* still authenticate via `~/.git-credentials`.

## MCP-facing contracts

The `apps/mewbo_mcp` facade depends on these REST decisions (see its CLAUDE.md):

- **One JSON 404 handler.** `@app.errorhandler(NotFound)`, registered once near the
  `Api(app, …)` setup, returns `{"error": {code, reason}}` for EVERY route. Without it a path
  that matches no rule — a `project` containing a `/` does not match `<string:project_id>` —
  falls through to raw Werkzeug HTML, which every JSON client then fails to parse.
- **Storeless async `run_id`.** `SessionRuntime.start_async` mints `"<session_id>:r<seq>"`
  (seq = count of prior user turns) and returns it (`""` when the run registry refuses a
  concurrent start — preserves `if not started:`). No run store: recover the session by
  splitting on the FIRST `:`. `/v1/structured` is async on this handle; `GET
  /v1/structured/<run_id>` resolves the session's latest `structured_output` event.
- **`/events` carries authoritative status** (`status`/`done_reason`/`title` from
  `summarize_session`/`load_title`) so the MCP overview reads them instead of reconstructing
  from the timeline tail.
- **Idle session-control follows the common coding-agent convention.** `/interrupt` on idle →
  200 `{interrupted:false}`; `/message` on idle/finished → re-engage via the
  `start_async`/query path, returning the new `run_id`; only a terminated session rejects.
  `/agents` token rollup delegates to `build_usage_numbers` — the same builder `/usage` calls —
  so a root-only session's call/agent counts aggregate correctly. The `input_tokens`/
  `output_tokens` fields themselves read 0 for every session (Langfuse is the only live token
  source meanwhile).
- **Worktree lifecycle is system-owned.** The `on_session_end` hook is the SOLE reaper; it also
  auto-reaps the promoted parent project when it has no worktree children left. The DELETE
  route is idempotent: already-absent → 200 `{status:"already_absent"}`, not 404.
- **`/agents` `total_input_tokens` = PEAK semantics** (`root_peak_input_tokens +
  sub_peak_input_tokens`), matching the `get_session_history` overview; the cumulative billed
  sum is separately exposed as `total_input_tokens_billed` (a bare sum reads ~2× the peak and
  confused callers).
- **`GET /v1/structured/<run_id>`**: output-present always maps to `status: "completed"`
  regardless of raw `summarize_session` status — the emit tool only fires on success, so
  presence IS completion.
- **`RepoIdentity` (`repo_identity.py`)** parses a canonical `(host, owner, repo)` from a
  project's git remotes; `_resolve_repo_or_404` matches a key against every registered
  project's identity + aliases (so one repo resolves via its self-hosted host OR GitHub mirror
  OR `owner/repo` OR bare name), and `GET /api/projects` surfaces `repo`/`aliases`. Ambiguous
  bare names raise a candidates error, never a silent wrong match.

## Config endpoints & secret handling

`ConfigSchemaView` (`config_view.py`) is the single atomic class governing how `/api/config*`
treats sensitive fields — one schema traversal, DI'd with the generated schema. Two field
classes, declared via `x-*` in core `config.py`:

- **`x-protected`** — never read, never written: stripped from `GET /config/schema` and
  `GET /config`; a `PATCH` touching one is 403'd. (Host paths, `api.master_token`.)
- **`x-secret`** — write-only: kept in the schema as `writeOnly`, settable via `PATCH`, but its
  VALUE is never returned. `GET /config` returns `{config, secrets}` where
  `secrets: {dot.path: bool}` reports is-set only. (`llm.api_key`, `langfuse.*`,
  `home_assistant.token`.)

**An EMPTY secret in a PATCH means UNCHANGED, and the write boundary is where that is decided.**
A client hydrates its form from a read that strips the value, so it re-sends the section with the
field empty — taken literally that erases the credential and answers 200, which is how a settings
save took a deployment's model gateway offline. `ConfigSchemaView.resolve_secret_writes(patch,
stored)` resolves it: empty carries the stored value forward, an explicit `null` clears (the only
spelling left, since absence and empty both mean "the client had nothing to send"), anything else
is written. Two things that look optional and are not: it must **carry the value forward, not drop
the key**, because `_deep_merge` replaces a list wholesale so a patched authenticator entry would
otherwise land without its `client_secret`; and *stored* must be the operator's own document, or an
`${ENV_VAR}` reference is carried forward as the literal it resolved to. A front end may drop empty
secrets from its own diff as well, but that is defence in depth — the console is not the only client
and the rule cannot live only where it can be routed around.

The console's `SecretField` is the matching write-only 3-state widget (unconfigured / configured /
editing — note there is no Clear affordance yet, so the `null` clear is an API-client path today).
Multi-token API auth is a separate concern — the `KeyStore` + `/api/keys` routes.

## Custom system instructions — REST surface

Three routes over the operator's singleton document (render/security model in
`packages/mewbo_core/CLAUDE.md` → "Custom system instructions"): `GET`/`PUT
/api/system-instructions`, `POST /api/system-instructions/preview`, `GET
/api/system-instructions/variables`. Built on the `TriggerRoutesController` DI pattern.

- **`PUT` compiles before persisting.** `SystemInstructionsDoc.validate_template()` runs at the
  write boundary; a syntax error 400s BEFORE anything reaches the store.
- **`/variables` is GENERATED, never hand-authored** — so the variable table the console renders
  cannot drift from what the renderer exposes. Its SHAPE comes from core
  (`InstructionContext.describe(catalog)`: the model documents its own schema); its VALUES come
  from `InstructionValueSources`. **Schema-walking belongs ON the model, not in this controller**,
  which owns the wire and re-derives nothing about `InstructionContext`'s semantics.
- **`InstructionValueSources` (`value_sources.py`) is the I/O EDGE, and its failure isolation is
  the design.** `/variables` is what an operator opens when their template misbehaves, often
  *because* something in the deployment is broken. So the four probes (tools / capabilities /
  projects / models) run CONCURRENTLY under ONE shared wall-clock deadline and each degrades to
  `()` independently — logged once, NEVER re-raised into the request. A dead LiteLLM proxy
  (`LLMConfig.list_models` RAISES `ValueError` on an unreachable one) or a hanging MCP server
  must empty ONE row, never 500 the page that exists to debug it. Verified live: proxy down ⇒
  `models: []` while `tools` and `capabilities` still resolve. **The deadline covers what a plain
  `try/except` cannot — a probe that never returns at all.** Collaborators are injected as
  FIELDS, so a test simulates "the proxy is down" by injecting a config whose `list_models`
  raises: no monkeypatching, no network in the suite.
- **A probe is a long-lived single-flight `SourceProbe`, NOT a per-request
  `ThreadPoolExecutor` — a leak fix, not a style preference.** Pool workers are NON-DAEMON and
  `cancel_futures=True` only drops *queued* futures (all four start immediately), so a probe
  wedged in MCP discovery kept its thread for the process lifetime, accreting one MORE per
  `/variables` call — and `_python_exit`'s atexit join meant a hung worker also BLOCKED
  interpreter shutdown (a gunicorn worker hanging until SIGKILL). Each source now owns one daemon
  thread and a second caller JOINS the in-flight probe, so a wedged source costs one thread ever.
- **THE TRAP: the tools catalog and a SESSION must be resolved from the SAME registry inputs.**
  `load_registry` merges `<cwd>/.mcp.json` and the subtree's, so a catalog built at `cwd=None`
  does not report a merely WIDER list than a project-scoped session's — it reports a
  **DIFFERENT** one, omitting MCP ids the operator's own sessions genuinely hold. So
  `_registry_cwds()` probes `None` **plus every CONFIGURED project path** (existing dirs only: a
  stale `app.json` entry must not spend the deadline on a directory that is gone). Two test
  shapes cannot catch a regression here and both were in place: one built its catalog from the
  very registry the session was scoped from (a tautology w.r.t. cwd), the other used a fake
  registry that *swallowed the `cwd` kwarg* (`lambda **_kwargs: registry`). **A fake that
  discards the argument under test cannot fail.** Record every `cwd` and assert the exact set.
- **MANAGED projects are deliberately NOT probed — a bound, not an oversight.** They are
  server-created, so the set is unbounded and churning; a long-lived store accretes promoted
  parents in the high hundreds, nearly all dead `/tmp` paths. Probing them puts an O(store-size)
  fan of LIVE MCP discovery on the settings page, taking the cold probe past the deadline, which
  then silently empties the very tools row the union exists to complete. The residue is DISCLOSED
  in the `tools` note instead. **A `known` list with an accurate caveat is honest; an
  exhaustive-looking list that times out into emptiness is not.**
- **`probe_timeout` (20 s) is sized so a COLD tools probe FITS.** Cold = live MCP discovery
  across the configured projects (~7.5 s with fifteen servers); warm ≈ 0.2 s, since the registry
  cache is process-wide and ordinary session traffic warms it. A deadline *under* the cold cost
  protects nobody — it makes the page prefer an EMPTY tools row (the exact lie this feature
  exists to remove) over a slow one, once per restart.
- **Do not warm anything at boot here.** The API is served by gunicorn, which IMPORTS this module
  rather than calling `main()`, so **"at startup" and "at import" are the same place, and import
  is shared with the tests** — a prime fires real MCP network I/O inside the suite.
- **`/variables` exposes `values` + `valuesKind` + `valuesNote`, not `enum`** (see core's
  `closed` vs `known` distinction). `enum` would be a redundant second channel for one fact.
- **`/preview` never 500s on a bad template** — a render failure comes back 200 with
  `{rendered: "", error: "..."}`, so the surface that exists to catch bad input cannot break on it.
- **Store construction is deliberately unguarded here.** `init_system_instructions()` calls
  `create_system_instructions_store()` with no try/except, but runs after
  `create_session_store()` already fails the app at startup when `storage.driver=mongodb` and
  Mongo is unreachable — that fail-fast covers this too, so re-guarding would be dead code.

## Project resolution + the `auto` mode

Every "which directory does this project name mean" question goes through the ONE
`mewbo_core.project_catalog.ProjectCatalog`, built once at the composition root and injected.
Read `packages/mewbo_core/CLAUDE.md` → "Project catalog" for the grammar; this is the app side.

- **A resolver copied per call site does not stay in sync.** A copy that does not handle
  `managed:<id>` silently returns the UNSCOPED tool or skill list for a worktree-backed session —
  no error, no log, just the wrong answer.
- **The `checkout_locator` is injected FROM here, and that direction is the point.** The catalog
  cannot join a repository slug to its checkout without `RepoIdentity`, which lives in this app,
  above core in the DAG, so the app hands the join in. Core stays importable by the CLI, which
  gets repositories listed without a path — the honest answer there rather than a wrong one.
- **A private key on a shared concept is a divergence waiting for its first cross-surface
  reader.** `_resolve_session_cwd` reads `cwd` / `project`; anything writing a private pair
  (`active_project` / `active_project_cwd`) is invisible to it, so a channel session that
  switched project reports the wrong directory on every OTHER surface. **Write the new shape,
  read both** — the private keys are still READ so older transcripts keep resolving.
- **`GET /api/projects`' wire shape is a two-client contract** — the console AND Aura both decode
  that payload.

`project: "auto"` is a reserved value of the field that already existed, NOT a new mode enum.
(`WorkspaceMode` is the containment tier — `read_only` / `workspace_write` / `full_access` — a
different axis entirely. Do not extend it for this.)

- **The sentinel resolves to NO directory**, falling through to the session temp dir exactly as
  an absent project does. `ProjectCatalog.resolve` REFUSES it, so the fall-through is written
  here, at the callers that own that policy.
- **The spec keeps `auto` even after the model has settled somewhere, and that IS the binding** —
  the session may switch again, so the mode outlives any one choice. The directory for the next
  turn comes from the `context` event the switch wrote, which `_resolve_session_cwd` already
  walks back for. The spec holds the MODE, the transcript holds the CURRENT LOCATION; collapsing
  them would make a settled session stop being able to switch.
- Every run path (`/query`, re-engagement, unattended fires) derives `project_autoselect` from
  that persisted spec rather than from the request, so tools stay bound across turns and a client
  cannot turn the mode on for a session that was never created in it.

## Repository registry — `/v1/git/repositories*`

The product-level list of git remotes Mewbo knows about. A **repository** is not a wiki project:
registering one is inert (normalize the URL, validate, dedupe, persist — no clone, no
`ls-remote`, no index, no network on the happy path), and a wiki project is an INDEX of a
repository someone opted into separately. The domain models + store live in core (see
`packages/mewbo_core/CLAUDE.md` → "Repository registry"); this covers the HTTP side.

- **It mounts from `backend.py`, NOT from `init_wiki`.** `init_wiki` returns early on an install
  without the optional `wiki` extra, so anything registered there is invisible on a base install
  — and agentic tasks run on exactly such an install. It is registered unconditionally, with
  every reach into `mewbo_graph` confined to the usage projection.
- **`/v1/git` is a deliberate prefix reuse** (same as `wiki/git_credentials_routes.py`): the
  console's nginx and vite proxies already forward it, so no proxy change is needed.
- **`RepositoryUsageSources` is the ONE class touching an optional dependency.** Each leg (wiki
  projects, credential scopes, managed projects) is guarded independently and degrades to empty,
  so a graph-less deployment serves the whole surface with `usage.wiki`/`usage.credential` null —
  never a 500, never a route that fails to mount. The wiki store is resolved PER READ off the
  runtime rather than captured at construction, because `init_wiki` may run after this: capturing
  would pin `None` forever and disable the wiki half on a machine that HAS the extra, a failure
  indistinguishable from not having it. The surface is thus independent of init ORDER too.
- **The list endpoint ADOPTS any wiki project slug the registry is missing** (stamped
  `origin: "wiki"`), which makes the registry a superset rather than a second list that drifts.
  Reconciling on read is idempotent and self-healing — nothing to run, nothing to re-run if it
  fails. A legacy two-segment wiki slug is skipped rather than failing the list; it is not a
  repository identity. **Corollary that reads like a bug and is not:** deregistering a repository
  that still has a wiki index re-adopts it on the next list.
- **The PATCH wire model is deliberately NARROWER than the core patch model.** Core's
  `RepositoryPatch` accepts `remote_url` and `platform`; `RepositoryUpdate` exposes only the three
  descriptive fields and converts into the core model. `repoUrl` is absent by design — the slug IS
  the identity pages, credentials and task bindings resolve through, so re-pointing a registered
  slug at a different remote is a delete-and-re-register, and `extra="forbid"` makes the attempt a
  clean 400 naming the field. Note the DIFFERENCE from the wiki's `ProjectSettingsPatch`, which
  does accept a `repoUrl` and guards it with a 409 when the `(host, owner, repo)` identity would
  change: that surface must accept a cosmetic re-normalisation of a URL it already stores. Do not
  "harmonise" them — there is no identity-change 409 on this path.
- **DELETE is not a cascade, deliberately.** Deregistration destroys nothing a consumer built —
  the wiki index goes through `DELETE /v1/wiki/projects/<slug>`, the credential through
  `DELETE /v1/git/credentials/<scope>`. Cascading would mean a user tidying a list they treated as
  a bookmark silently threw away a paid indexing run, and a host-scoped credential is shared by
  every repo on that host, so cascading it would break repositories the caller never named.
- **`POST .../<slug>/checkout` is the ONE action that puts a repository on disk**, a separate verb
  precisely so registration stays free. It creates a managed project (`create_project` with NO
  path — a `path_source="provided"` parent is permanently deleted by the worktree reaper once
  childless), clones into it, and surfaces on the next read as `usage.tasks`. Four decisions worth
  not re-litigating: (1) `RepositoryCheckout` is the only other class here touching `mewbo_graph`
  — a LAZY in-method import, so a base install gets a 503 NAMING the missing extra rather than an
  ImportError 500, and the read routes sharing this module neither pay for it nor die with it.
  (2) The clone is FULL (`depth=None`): `--depth` implies `--single-branch`, and a task diffs
  against the default branch and cuts a branch from it. (3) It clones straight into the project's
  own path and lets the executor's `reset_dir` wipe the seeded `CLAUDE.md` — and does NOT write
  one back, because the repository's own arrives with the clone and a synthesized one would be an
  untracked file an agent could commit upstream. (4) Synchronous, bounded by
  `CLONE_TIMEOUT_SECONDS` (240 s, inside both gunicorn's 300 s worker timeout and the console
  proxy's — the `/v1/git/` nginx block needed an explicit `proxy_read_timeout`, since nginx
  defaults to 60 s). On any failure the just-created project is DELETED. **Idempotency reuses
  `_task_usage`** — the guard and the `usage.tasks` the UI renders must read the same fact, or a
  client sees "no checkout" and is then refused one. Corollary: the alias match is
  `RepoIdentity`'s LAST-TWO-segments rule, so a GitLab subgroup repository is not recognised as
  already-checked-out and a second call clones again; that gap belongs to the projection and is
  shared with `usage.tasks`.
- **The IAM pair rides with the credential permissions, not with `projects.read`.**
  `repositories.read`/`repositories.write` sit beside `git_credentials.*` in the `member` bundle
  because a repository DTO reports which credential SCOPE covers it — the same secrets-adjacent
  metadata a viewer is denied.
- **Not on the RESTX spec.** A plain Flask Blueprint contributes ZERO paths to `docs/openapi.json`
  (the generator exports `api.__schema__` only) — same as `/v1/wiki/*` and `/v1/git/credentials*`.
  Regenerating the spec after touching this file is a no-op by construction.

## Session-run boot sweep (`run_sweep.py`)

A process death (deploy, restart, OOM kill) can strand an in-flight session run: the worker dies
mid-turn, the transcript ends on run activity with no terminal `completion`, and
`summarize_session` then derives `status="idle"` — the console's recovery card never renders,
leaving the user guessing whether anything ran. `SessionRunSweeper.sweep()` (called from
`backend.py` at import, which in this app IS startup) appends a synthetic terminal `completion`
(`{done: true, done_reason: "error", error: "interrupted: process restart", task_result: null}`)
to every session whose transcript shows an open run, so the derived status flips to `failed` and
the existing recovery affordance surfaces.

- **Orphan detection is a narrow, miss-only positive allowlist.** A run is "open" only if one of
  `run_accepted`/`llm_call_start`/`llm_call_end` appears with no `completion` after it — events
  that occur ONLY within a root run's active lifetime. A bare `user` turn, a background
  `sub_agent` stop, or a `user_steer` can legitimately land AFTER a completed run's `completion`
  (a queued next turn; a late-arriving sub-agent lifecycle event; a `/message` slipping into the
  handle-teardown window), so including any of them would FALSE-FLIP a genuinely-completed
  session to `failed`. Worst case of the narrow allowlist is a MISSED orphan (status stays
  `idle`, no regression) — the contract is miss-only by construction, not merely by care.
- **This WRITES to the configured store at import**, shared with tests (any test importing
  `backend.py` triggers it against whatever store the test config points at). Idempotent (a
  session already carrying a terminal completion for its last turn is skipped), but still a
  write. `MEWBO_BOOT_RUN_SWEEP=0` opts out entirely (env-driven, no config-schema knob).
- **The Mongo session store carries a tail-bounded `load_recent_events` override** — a bounded
  `sort(ts DESC).limit(n)` range read — because the base template method materializes the WHOLE
  transcript before slicing. The JSON driver has no cheap reverse read for a flat JSONL file, so
  it inherently pays O(transcript) per candidate session. Either way only each candidate's tail
  (`_TAIL_SCAN_LIMIT`, 64 events) is scanned, and `_RECENT_ACTIVITY_WINDOW` (7 days) bounds which
  orphans get settled — an ancient abandoned session shouldn't resurface as a fresh red card on
  every future boot.
- **A correct diagnosis with no live consumer is not a fix.** This sweep and the wiki
  `JobRecovery.recover_interrupted` re-drive stranded work exactly once, at import, because that
  is the only place either is wired to run. Core marks a run that stopped short of its goal
  `unmet_goal` + `recoverable` the moment it ends, correctly — and then nothing acts on the flag
  while the process keeps running: it waits for a human to notice the recovery affordance, or for
  the next restart. **Ship a new honest-outcome signal only alongside a LIVE consumer for it, in
  the same change.**

## Hidden dependencies / assumptions

- Core logging (`mewbo_core.common.get_logger`); level from `runtime.log_level`.
- Core LLM config (`llm.api_base`, `llm.api_key`, `llm.default_model`, `llm.action_plan_model`).
- No rate limiting. No heartbeat or health endpoint — external deployments handle liveness.
- `api.master_token`'s default is insecure; production overrides it in `configs/app.json`.
- The API returns the whole `TaskQueue` including action steps; ensure tool results are safe to
  expose.

## Testing guidance

- `apps/mewbo_api/tests` mock `SessionRuntime.run_sync` and focus on response schema.
- Avoid mocking too much of core: keep at least one integration test exercising real
  `SessionStore` behavior.

## Debugging session errors (trace methodology)

Given a session URL (`/s/<session_id>`), work the layers in order. **Mongo is authoritative for
the event SEQUENCE; Langfuse holds the LLM conversation chain and is the only live token source.**

1. **MongoDB transcript** — `db.events.find({session_id}).sort({ts:1})` via `MEWBO_MONGODB_URI`
   (port 27018). Check `tool_result.error`, `context.mcp_tools`, `completion.done_reason`.
   Per-agent attribution lives here: `llm_call_start` and `llm_call_end` each carry
   `agent_id` / `depth` / `step` (`mewbo_core/loop/tool_use_loop.py`), plus `model` and
   `bound_tools` on the start and `success`/`model` on the end. Langfuse has no depth axis and
   its span tree orphans parents under concurrent sub-agents, so join the two by session, not by
   span.
2. **Langfuse traces** — `fetch_traces(age=N)` → `fetch_observation(id)` on a `GENERATION` to see
   the system prompt, bound tool schemas, and the returned content. Trace-to-session:
   `trace_id == session_id`.
3. **Config** — `configs/app.json` (mounted read-write at `/app/configs/`; `PATCH /api/config`
   persists back to it, so a read-only mount breaks every settings save), `.env` for secrets, MCP
   at `configs/mcp.json` (global) or `<project>/.mcp.json` (project).
4. **Docker env** — `docker-compose.yml` + override for mounts. The API runs at `/app` with
   `MEWBO_HOME=/app/data`. Project dirs need identical host and container paths.

### Common root-cause signatures

| Symptom | Cause |
|---|---|
| `result: null, success: false` on shell/file tools | CWD missing in container (volume mount), or `root` not injected |
| `"Tool not available"` | the model named a filtered-out built-in tool, or `tool_id` mismatch with the registry |
| `"MCP server 'X' not found in config"` | `MCPToolRunner` loaded config without the project CWD; project `.mcp.json` not merged |
| Langfuse `sessionId: null` | `invoke_config["metadata"]` not propagated; check the `langfuse_metadata` pattern in `tool_use_loop.py` |
