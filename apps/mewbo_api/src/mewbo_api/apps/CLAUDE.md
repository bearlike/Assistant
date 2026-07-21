> ↑ [apps/mewbo_api/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md)

# Mewbo Apps — API Subsystem Guidance

Scope: `apps/mewbo_api/src/mewbo_api/apps/` — the backend of the Mewbo Apps
sub-product (LLM-built, trigger-maintained mini apps: a versioned stlite
frontend + agent-authored data pipelines + a per-app data namespace, bound to a
workspace). Peer of Wiki/Search; the `agentic_search` package is the shape
precedent (store app-side, routes as an atomic controller, capability-gated
agent plugin). Read the API root `apps/mewbo_api/CLAUDE.md` first — the DI'd
controller paradigm and the `ApiResponseKit` rules apply here unchanged.
Everything readable straight from the code is left out.

## What this is (the two entities)

- **`AppSpec`** (`models.py`, keyed `app_id`) — the durable, append-only-versioned
  manifest: `frontend` (multi-file stlite bundle), `collections` (schema'd data
  namespaces), `pipelines`, `policies`, `workspace_ref`, `owner_session_id` +
  `maintainer_session_id`, `status` (draft→building→live↔paused / broken / archived).
- **`PipelineRun`** (`models.py`) — the provenance ledger, opened at a trigger fire
  and closed at the maintainer run's end. It is the freshness signal, the
  `/system` backing, and the repair-loop input all at once. `docs_written` is
  incremented by `app_data` writes; strategy (freshness, run classification) lives
  ON the model — NOW is always a method arg, never a wall clock.

Strategy-on-model rule (like `TriggerSpec`): `AppSpec.transition()`,
`PipelineRun` freshness classmethods, `AppReadToken.is_valid(now)` live on the
models; stores are DI'd; models never import I/O. Extend `_ALLOWED_TRANSITIONS`,
never bypass `transition()` with a bare `self.status =`.

## Stores — process-wide factories + a single test seam

Three storage families in `store.py`, each `Base ABC → Json + Mongo + create_*()`
+ a process singleton (`get_app_store` / `get_pipeline_run_store` /
`get_app_data_store`). The driver is read from CONFIG (`storage.driver`), NEVER
the `MEWBO_MONGODB` env — the cross-cutting trap the whole monorepo shares.

- The routes controller AND the agent plugin's `app_data` tool BOTH resolve stores
  through these factories — that shared singleton is why `app_data` needs no wiring
  push (see the submitter seam below). `set_stores_for_tests` is the ONE swap seam;
  tests must reset through it, never construct a parallel store the route can't see.
- `AppDataStore.upsert(collection_spec=)` and `query(sort=)` are ADDITIVE optional
  kwargs over the frozen positional signatures — the plugin's local Protocols
  declare the same shape, so keep them additive.

## Token model — `token_id` IS the credential

`tokens.py:AppReadTokenSigner` mints stateless HMAC-SHA256 render tokens keyed by
a server secret: the dedicated `api.apps_token_secret` config field (`x-secret`)
when set, else the API master token with ONE startup WARNING
(`backend.py:_build_apps_token_signer` — set the field to rotate app-token signing
independently of the master token; rotating it invalidates outstanding tokens,
which is fine — they're 30-min short-lived). The whole signed blob
`<app_id>:<exp>:<token_id>:<sig>` is returned AS `token_id` — there is no separate
secret. The client presents it
verbatim in the **`X-Mewbo-App-Token`** header (also accepted as `Authorization:
Bearer` / `?token=`); `verify(token_id, now)` re-derives app_id + expiry.
Read-auth on the data/system GETs: a master/issued key OR a token whose `app_id`
matches the path — a valid token for ANOTHER app is 403 (no cross-app read),
forged/expired is 401. The master key never enters the browser/WebView.

## Pipeline arming — the pipeline DECLARES, the platform arms (Phase 1)

A pipeline declares HOW it is woken; `AppLifecycle.submit` arms it on the
maintainer. `PipelineSpec` carries a discriminated `schedule: PipelineSchedule |
None` (`CronSchedule {kind:"time.cron", cron}` / `AtSchedule {kind:"time.at",
at}`, each owning its own validator + a `to_trigger_spec(wake_prompt, session_id,
now)` strategy that builds the core `TriggerSpec`), an `on_demand: bool`, and
`trigger_ref: str | None` which is now **PLATFORM-OWNED OUTPUT** (the builder
never sets it). A model-level `@model_validator` makes the silent-unscheduled
state (no schedule, not on-demand, no legacy ref) **unrepresentable** — submit
refuses a pipeline that would never run, at the trust boundary.

1. **PRIMARY — declared schedule.** `submit` calls
   `schedule.to_trigger_spec(...)` to mint a fresh trigger on the **maintainer**
   session (`created_by="user"`, `created_at=now`), stamps a **long-lived
   `expires_at`** (`_SCHEDULE_TRIGGER_TTL`, 10y) BEFORE `TriggerPolicy.admit` so
   the policy's 7-day `default_expiry` (stamped ONLY when `expires_at is None`)
   can't silently kill the app's heartbeat — the live-verified
   `app-a2a5f299e0ad` trap (refreshed once, then died). Then it stamps the armed
   id into `trigger_ref`. This retired builder self-arming for apps.
2. **LEGACY — builder-armed `trigger_ref`, no schedule.** The legacy flow (the
   builder armed a trigger on its own ephemeral session via `schedule_trigger`
   and recorded the id) is still honoured: `_rehome_pipeline_trigger` re-homes it
   onto the maintainer + cancels the builder's original. Deprecated; kept working
   for a chat-builder. A builder-supplied `trigger_ref` satisfies the model floor
   because it IS a scheduling declaration.
3. **on-demand / policy-capped.** `on_demand=True` ⇒ no armed wake. A policy-cap
   rejection (over `max_armed_per_session` / cron too tight) SKIPS that one
   trigger, never sinks the submit — the app keeps the pipelines that fit.

`pause`/`resume`/`archive` are "app == its triggers" (spec §2.10): they transition
every maintainer-owned trigger in lockstep — a platform-armed trigger is on the
maintainer, so this works for it identically to a re-homed one (verified in
`test_apps_lifecycle.py`).

## The run lifecycle — kickoff, the ledger, and the failure policy

Three seams turn an app from a static manifest into a live, self-maintaining one.
All three ride EXISTING backend seams (`_trigger_deliver`, `start_async`, the
`on_session_end` hook) — no new engine, no headless executor (spec §2.1).

- **Builder kickoff (`create_draft`).** After it persists the `building` draft +
  mints the builder session, `create_draft` KICKS OFF the build via an injected
  `AppRunStarter` (a Protocol field; `_RuntimeAppRunStarter` in `backend.py` wraps
  the `_trigger_deliver` idle-start idiom, shared through `_reengage_idle_session`).
  The kickoff message hands the root the intent + workspace + **the assigned
  `app_id`** so `submit_app` reuses it and the submit reconciles onto the SAME
  draft row. Unwired run-starter ⇒ a logged no-op (the draft still persists), never
  a crash. The chat-builder path has no `create_draft` and no row — `submit` handles
  both (below).
- **`submit` reconciles the draft row (identity vs content).** When a row already
  exists for `app_id`, it is the authority for IDENTITY (`owner_session_id` /
  `created_at` / `workspace_ref` preserved) and `submit` replaces only the
  builder-authored CONTENT. A submit whose existing row is NOT `building`/`draft`
  is REFUSED (`ValueError` → the builder gets a reask) — this is the
  double-submit + live-overwrite guard, a real data-loss vector (a chat builder
  reusing a live `app_id`).
- **`PipelineRun` ledger (`AppPipelineRunTracker`, `pipeline_tracker.py`).** Opens
  a run when a fired trigger re-engages a maintainer (resolved by matching the
  firing `trigger_id` against `PipelineSpec.trigger_ref`) and closes it at the
  maintainer run's END. `trigger_id` reaches the app side because the `deliver`
  callback (`TriggerService` → `_trigger_deliver`) carries it on the structured
  `TriggerFireContext` payload (phase 0 — was a 4th positional arg). **Open
  and close are two DIFFERENT seams for a reason:** `start_async` returns a
  `run_id` immediately and never observes completion, so opening rides
  `_trigger_deliver` while closing rides `HookManager.on_session_end` (which fires
  with the run's `error` — the outcome we classify `succeeded`/`failed` on). Only
  after this landed do `docs_written`/freshness/`/system` runs become live.
- **`tools_allowlist` least privilege (I3 — default FLIPPED in Phase 1).**
  `pipeline_scope` is now AUTHORITATIVE least privilege in BOTH branches: a
  NON-EMPTY `PipelineSpec.tools_allowlist` runs under that list + `app_data`;
  an EMPTY allowlist now runs under **just `["app_data"]`** (was permissive
  `None`). Both under **`strict_tool_scope=True`** so built-ins are capped too,
  threaded into `_trigger_deliver`'s `allowed_tools`. So an undeclared unattended
  fire can write its own data and nothing else — a pipeline that needs a
  connector / `web_search` / a file read must DECLARE it. `pipeline_scope`
  returns `None` ONLY when the fire isn't an app pipeline at all. See core
  CLAUDE.md → "Capability gating" for why strict vs permissive matters here.
- **`on_pipeline_failure` policy (I4).** The tracker's close hands the app + a
  `PipelineIssue` to `AppLifecycle.handle_pipeline_failure`: `repair` starts a
  repair run on the maintainer via the same `AppRunStarter` (prompt delegates to
  the `app-repair` AgentDef); `pause` calls `lifecycle.pause`; `notify` emits an
  `app_issue` event and nothing else. TWO reasons reach it — a FAILED close, and
  a SUCCEEDED close that regressed a collection (see "Auto-repair on an integrity
  violation" below for why the second one must not be recorded as a failure).

## Code pipelines — the execution engine (Phase 2)

A `PipelineSpec` now carries `mode: "agentic" | "code"`. **`agentic` (default) is
unchanged** — a fired trigger re-engages the maintainer LLM session. **`code` runs
a deterministic `entrypoint` file (`def run(params, ctx) -> Any`) with NO LLM call**
via `AppPipelineRunner` (`pipeline_runner.py`) — the ONE new primitive this phase
adds; everything else rides existing seams.

- **Placement:** pipeline files live in the SAME `frontend.files` map under
  `pipelines/…` keys (`entrypoint` names one) — smallest diff (`submit_app` reads
  every bundle file already). `PIPELINE_ALLOWED_MODULES` + `lint_pipeline` live in
  `pipeline_runner.py` (canonical); `submit_app` routes `mode="code"` entrypoints
  to `lint_pipeline` (frontend files still get the widget/app lint), and the runner
  re-runs it at execution — SAME allowlist both boundaries, no drift. Pipeline
  source is NOT served to the browser (wave 5, closing the old v1 gap):
  the strip is **strategy-on-model** — `AppSpec.served_frontend_files()` excludes
  every `mode="code"` entrypoint + anything under `pipelines/`, and `_render_spec`
  / `_render_version` DELEGATE to it (the controller never re-derives the
  predicate). It applies to the TOP-LEVEL rendered spec AND every `versions[]`
  snapshot ON THE WIRE — history must not leak the exact source the live strip
  withholds — while the STORED spec + its version history keep every file (the
  runner's input; the STORE retains, the WIRE does not). See "SDK delivery" below.
- **Substrate = in-process `exec` in a curated namespace** (NOT a subprocess in v1).
  Justified: pipeline code is agent-authored under the SAME trust envelope as an
  agentic run, which already shells out via `aider_shell_tool` — so this is no
  escalation. Gated three ways: the pipeline lint (import allowlist + the app
  linter's dynamic-exec ban), a curated `__builtins__` (no `open`/`eval`/`exec`/
  `compile`; a **guarded `__import__`** admitting only the allowlist), and a
  **thread wall-clock watchdog + output size cap**. The watchdog bound is
  DECLARED per-pipeline (`PipelineSpec.timeout_seconds`, default 10, ge=1/le=600) —
  the runner joins the worker on the pipeline's value (a code transform finishes
  well under 10; an llm pipeline should declare 120–300); the runner's constructor
  `timeout_seconds` is an OPTIONAL global override (None ⇒ use the pipeline value;
  set ⇒ a deployment cap / how a test drives a sub-second wall under the 1s field
  floor). **Watchdog honesty:** Python threads aren't preemptible, so an
  uncooperative loop LINGERS as a daemon thread — the watchdog returns control with
  a `timeout` error but can't kill it. Subprocess isolation with a hard kill is the
  phase-2.5 upgrade; documented in the runner docstring, not pretended otherwise.
- **`ctx` surface:** `ctx.params`/`ctx.now` + `ctx.glob`/`ctx.read_file`
  (workspace-scoped, traversal-guarded — resolved UNDER the workspace root only) +
  `ctx.collection(name).upsert|query|delete` + `ctx.llm(prompt, output_schema, *,
  max_tokens=1024)`. Writes ride the REAL `AppDataStore` with `collection_spec=` +
  `max_docs=` so schema validation + the cap are the SAME enforcement seam
  `app_data` uses (never duplicated). `dry_run` exercises the identical path but
  performs NO durable write and COUNTS what would write.
- **`ctx.llm` — the bounded LLM step (Wave 5).** ONE schema-shaped model
  round-trip: returns a dict validated against `output_schema` (jsonschema, the same
  lib the runner already uses; ONE retry with the validation error appended, then
  raises `PipelineExecutionError("llm", …)`). **`output_schema` MUST have root
  `type:"object"`** — validated at ctx.llm entry BEFORE any model call (the
  synthesis seam returns an object; a non-object root otherwise wastes a round-trip
  that only fails re-validation). The runner gains a DI'd
  `llm_invoke: Callable[[str, dict, int], dict] | None`; `backend.py:_apps_llm_invoke`
  wires a THIN in-process adapter over the DEDICATED structured-output surface
  `/v1/structured` 'synthesis' mode rides — `mewbo_api.structured.synthesis.SynthesisRunner`
  (one no-loop `StructuredSynthesizer` round-trip wrapped in the write-behind
  `RealtimeSessionRecorder`), NOT a fresh `build_chat_model` client and NEVER
  HTTP-to-self. The payoff: every `ctx.llm` call becomes a session-backed,
  Langfuse-traced structured run, so a code pipeline's LLM spend gets the SAME
  observability + provenance an interactive synthesis run has, for free. It stamps
  `surface="apps-pipeline"` (→ a distinct `source_platform`) so pipeline spend is
  filterable WITHOUT surgery — a custom `session_type` tag would mean bypassing
  `SynthesisRunner`'s hardcoded `structured:fast` base tag AND teaching core's
  provenance classifier a new prefix (else it reclassifies to the `user` origin), so
  `structured:fast` is reused as-is. Unwired ⇒ a clean "not configured" error. Budget is
  DECLARED per-pipeline (`PipelineSpec.llm_budget_tokens`, `0` = llm FORBIDDEN —
  declared data over ambient permission); the **`PipelineContext.llm_tokens_spent`**
  attribute (the ctx owns the state, atomic-class rule — not a boxed closure) tracks
  cumulative `max_tokens` per RUN and the runner refuses a call past the cap (a cache
  HIT costs no budget). Results are cached content-addressed by
  `sha256(prompt+schema[+source-salt])` in the runner's existing `_cache` mapping (slot
  sentinel `__llm__`, collision-free by the slug rule), so a repeated fire with
  unchanged inputs skips the model. In `cache_mode="source"` the runner SALTS the llm
  key with the run's source fingerprint (`execute()` computes it once — same value it
  hit-checks on), so a source change busts a stale llm answer even when the prompt is
  byte-identical; ttl/no-cache modes pass `salt=None`. `ctx.llm` IS allowed under
  `dry_run` (read-only w.r.t. collections; the builder needs to test it — budget still
  charged and a cache HIT still served), **but a dry run never WRITES the llm cache**
  (a preview must not mint an entry a later real, charged run would then serve
  un-provenanced). HONEST v1 BUDGET LIMITATION (documented, not pretended): the budget
  bounds REQUESTED tokens + the CALL COUNT, not metered spend — a schema-reask retries
  the model (a second round-trip the budget never charged) AND the synthesis seam
  accepts no per-call token cap, so `max_tokens` drives the accounting but is NOT
  threaded into the round-trip. The real v1 spend bound is `timeout_seconds` + the
  call-count cap, never a token meter (a metered budget is phase-2).
- **`ctx.exec(argv, *, timeout_seconds=None)` — controlled CLI/network egress.**
  The sandbox's default posture is NO subprocess, NO network — `ctx.exec` is the
  narrow, DECLARED opt-in that lets a `mode="code"` pipeline shell out to a vetted
  binary instead of being forced into `mode="agentic"` just to reach
  `aider_shell_tool`. Two `PipelineSpec` fields, both empty by default (closed):
  `allow_exec` (a subset of `models.PIPELINE_ALLOWED_EXEC = {git, tea, gh}`) and
  `allow_egress` (bare hostnames, validated against `_HOSTNAME_RE`). A third
  `model_validator` rejects either on a `mode="agentic"` pipeline — an unreachable
  grant that reads as capability is a quiet lie an audit has to re-derive.
  **The split that matters: the AUTHORIZATION rule is a method on the model
  (`PipelineSpec.check_exec_allowed(argv)`, argv as a method ARG — the `TriggerSpec`
  discipline), and the I/O edge is one atomic class (`PipelineExecutor`) with the
  pipeline/workspace/redactor injected as fields.** Neither is a free function; the
  earlier cut had five module-level helpers and a bare `dict` subclass, which is
  what this section exists to keep from coming back.
  - **`git` is SHAPE-GATED, and the reason is a reachability asymmetry — not
    paranoia.** Declaring `git` used to be equivalent to granting a shell:
    `git -c alias.x='!sh -c …' x` executes, as do `core.pager`, `protocol.ext`/
    `ext::`, `--upload-pack`, `--exec-path`. The "same trust envelope as the
    pipeline's own code" argument that justifies the in-process `exec` substrate
    does NOT cover it, because the **GET pipeline-invoke route is reachable by a
    browser holding a short-lived app READ token**, while `aider_shell_tool` needs
    an agent session behind the master key. So `PipelineSpec._check_git_shape`
    permits only read-shaped subcommands (`_GIT_SUBCOMMANDS`) and rejects
    `_GIT_BANNED_FLAGS` plus any `::` token. **Both halves are load-bearing** —
    banning the flags alone still leaves `git config alias.x '!sh'` then `git x`,
    two perfectly ordinary calls. A pipeline needing an unlisted subcommand is
    refused until the list grows; that cost is the point.
  - **Even so, `allow_exec` is a DECLARATION, not a sandbox** — `tea`/`gh` are not
    shape-gated, and no argv inspection can bound what a binary does once it runs.
    The value is that the declaration is explicit, auditable on the manifest, and
    closed by default. Documenting it as a boundary against a hostile pipeline
    would be the lie that gets relied on.
  - **`allow_egress` guards argv-shaped remotes ONLY.** It cannot constrain a bare
    remote name (`git fetch origin` — the host lives in the workspace's
    `.git/config`), a submodule URL, or `gh`/`tea`, whose host comes from auth
    state rather than argv. A real egress boundary needs a network namespace or a
    proxy; this is a declaration check. Say that rather than implying otherwise.
  - **Two near-misses in host extraction, both worth remembering because each
    looked correct.** (1) git's scp remote is `[user@]host:path` and the user part
    is OPTIONAL — the first cut matched only tokens containing `@`, so `git
    ls-remote example.com:repo.git` reached ANY host with the allowlist none the
    wiser. (2) The URL branch used `urlparse`, which returns hostname `None` for
    `-c remote.x.url=https://evil.example.com/r.git` (the scheme candidate
    `remote.x.url=https` contains `=`, so there is no scheme) — the token was
    skipped and the call ALLOWED. **A skipped token fails OPEN; that is the
    asymmetry to watch for in any future extractor.** Hence `_URL_HOST_RE` with
    `finditer` (one token can carry two hosts, as `url.<a>.insteadOf=<b>` does)
    rather than a parse. `_SCP_REMOTE_RE` requires a DOT in the host: without it
    every ordinary refspec (`HEAD:refs/heads/x`, `HEAD:README.md`) reads as a host
    and gets refused, whose only workaround is declaring `head` in `allow_egress`
    — training authors to list junk hosts is worse than the narrow gap it closes.
  - **The env is INHERITED IN FULL, deliberately** (`dict(os.environ)` plus a
    prompt/pager overlay; `git` routes through the shared `hardened_git_env`). An
    earlier draft advertised a "minimal, scrubbed env" that the code never
    implemented — and could not, because the documented credential posture depends
    on inheritance (an SSH agent needs `SSH_AUTH_SOCK`; `tea`/`gh` need `HOME`). The
    accepted consequence, stated rather than hidden: an allowlisted binary sees the
    API process's environment.
  - **git argv carries `-c credential.helper=`**, matching `build_clone_command` /
    `build_ls_remote_command`. Without it a pipeline's git call is the one path that
    re-opens the EBUSY-masks-auth trap `mewbo_graph/CLAUDE.md` documents.
  - **Bounds:** cwd ALWAYS the workspace root (same `workspace` error `ctx.read_file`
    raises); per-call timeout clamped by the pipeline's `timeout_seconds`; the child
    runs in its own session and is killed as a PROCESS GROUP so a spawned helper
    dies with it; output truncated on a real BYTE budget (a `str` slice counts
    codepoints and overshoots ~4× on multi-byte output). stdout, stderr **and the
    timeout message** are redacted — the timeout text embeds argv, which can carry a
    credential-bearing URL, and it lands verbatim on the `PipelineRun.error` ledger
    row `/system` renders.
  - **REFUSED under `dry_run` (code `dry_run`), unlike `ctx.llm`.** `ctx.llm` may run
    under a preview because it cannot mutate the world; a subprocess can. Decisive
    reason: `AppLifecycle.submit` dry-runs EVERY code pipeline as its verifier, so
    admitting exec would let merely SUBMITTING an app push to a remote nobody asked
    it to touch. `_verify_pipelines` classifies `dry_run` alongside
    `params`/`workspace` as a verifier ARTIFACT, so a shelling-out pipeline still
    verifies cleanly instead of being refused.
  - Returns `{"returncode", "stdout", "stderr"}`; a non-zero exit is NOT an error
    (the pipeline decides what a failed `git` means). Only a refusal, a missing
    binary, or a timeout raises.
  - **Credential honesty:** `ctx.exec` does NOT resolve or inject a per-pipeline
    STORED credential (unlike `wiki_clone_repo`'s `resolve_chain`) — it rides AMBIENT
    state: an SSH agent, a `tea login`/`gh auth login` session. **NOT a `git
    credential.helper`** — the `-c credential.helper=` above disables it, so an HTTPS
    git remote has NO credential here and only SSH-agent auth resolves one. That
    combination is deliberate but easy to mis-document: the wiki clone tool can afford
    the same flag only because `resolve_chain` injects a token into the URL, and
    `ctx.exec` injects nothing. Reusing only
    `hardened_git_env` and NOT `run_git_with_chain` is correct, not a DRY gap: the
    chain exists to iterate a `CredentialStore` keyed by `store`/`slug`, neither of
    which a pipeline's `git log` has.
- **Failure is RAISED (`PipelineExecutionError` with a `code` bucket), never encoded
  in the result** — `PipelineResult` (`{output, evaluated_at, cache, docs_written}`,
  frozen) only ever represents success. TTL cache (`cache_ttl_seconds`, 0 = never)
  is keyed `(app_id, version, name, sha256(params))`; a hit does no writes.
- **`cache_mode: "ttl" | "source"` — read-through liveness (Wave 5).** `"ttl"`
  (default) is the time-driven tier above. `"source"` is the LIVE tier: the
  `PipelineContext` RECORDS every path it read/globbed, and the runner persists that
  manifest + a stat FINGERPRINT (`sha256` over sorted `(path, mtime_ns, size)` +
  the glob RESULT sets — stat-based, NO content hashing) in a dedicated
  `_source_cache`. A later `execute()` re-stats/re-globs the manifest and serves the
  cache IFF the fingerprint matches, else re-executes and re-records. A NEW file
  matching a recorded glob busts it (the result list is part of the fingerprint); a
  touched mtime or size busts it; the first run has no manifest and always executes.
  `"source"` IGNORES `cache_ttl_seconds` (precedence: liveness is stat-driven, not
  time-driven). `dry_run` never serves or populates the source cache. `ctx.glob` and
  the fingerprint share ONE `_glob_under_root` resolver so a recorded glob re-globs
  identically. **`ctx.glob` records `{pattern: resolved list}` at glob time**, and
  the POST-run fingerprint consumes those recorded results — NO re-walk (the runner
  used to re-glob up to 3× per invoke). The PRE-run check against a PRIOR manifest
  still re-globs via `_resolve_globs` — that walk is inherent (a NEW matching file
  can only be found against the live filesystem). `execute()` computes that prior
  fingerprint ONCE and reuses it as both the hit-check AND the `ctx.llm` cache salt
  (see the `ctx.llm` bullet — a source change busts a stale llm answer too, not just
  the whole-run source cache).
- **The runner exposes TWO methods for two landed clients:** `execute(app,
  pipeline, params, *, now, dry_run) -> PipelineResult` (object-keyed — `routes.py`
  reads the four attrs structurally + the fire seam) and `run_pipeline(app_id,
  pipeline_name, *, params, dry_run) -> dict` (id-keyed — the `run_pipeline`
  SessionTool's `PipelineRunner` Protocol; maps `cache` → `cache_hit`, uses the
  injected clock since it threads no `now`). It needs `app_store` DI (beyond the
  object-keyed field list) precisely to resolve the id-keyed seam.
- **Trigger fire on a code pipeline runs the ENGINE, not the LLM.**
  `_trigger_deliver` calls `AppPipelineRunTracker.run_code_pipeline_fire(trigger_id)`
  FIRST; a `mode="code"` fire executes the engine synchronously, writes ONE closed
  `PipelineRun {kind:"scheduled"}` with the result (`params_hash`/`cache`/
  `docs_written`), dispatches `on_pipeline_failure` on a raise, and returns `True` —
  **the maintainer session is NEVER re-engaged** (that's the whole point). An
  agentic / non-app / unwired-runner fire returns `False` → the unchanged agentic
  re-engage path. `PipelineRun` gained additive `kind: "scheduled"|"on_request"`,
  `params_hash`, `cache` (agentic runs default `kind="scheduled"`, the rest `None`).
  `AppPipelineRunTracker.record_code_run` writes the closed row, returning
  `(run, result)`. The scheduled fire uses the defaults (`dispatch_failure=True`,
  `require_effect=False` ⇒ always ledger + dispatch the failure policy); a
  REST/`run_pipeline` invoke passes `dispatch_failure=False, require_effect=True` ⇒
  it ledgers only an EFFECTIVE invoke and never auto-repairs on a manual failure —
  see "Constraints + accepted v1 gaps" below for the freshness/anti-spam reasoning.
- **Submit validates `mode="code"` entrypoint is IN the bundle**
  (`AppLifecycle._validate_code_pipelines`) — the model enforces "entrypoint present
  IFF mode='code'" but can't see the bundle, so this cross-field check is at submit
  (actionable reask). Wiring: `init_apps` constructs the runner, injects the
  `_resolve_app_workspace_cwd` resolver (reuses the maintainer session's own cwd
  resolution), `register_pipeline_runner(runner)` (the plugin seam), and hands it to
  the tracker.

## On-demand fire, go-live seed, re-arm (follow-on: the freshness-recovery plane)

The answer to "live app, freshness = never, no way to refresh": ONE fire seam,
`AppPipelineRunTracker.fire_pipeline(app, pipeline, *, now)`, with three callers —
`POST /apps/<id>/pipelines/<name>/fire`, the go-live seed in `AppLifecycle.submit`,
and `rearm`'s optional seed. Non-obvious laws, each review-earned:

- **Fire is read-auth ON PURPOSE; rearm is key-only.** A served app refreshing
  itself via the SDK's `pipelines.refresh(name)` is deliberate (same ruling as the
  GET invoke), bounded by the agentic cooldown; `/rearm` is an operator repair and
  rejects app tokens outright.
- **`mode="code"` fire = `record_code_run(kind="on_request", dispatch_failure=False,
  require_effect=False)` — an explicit fire ALWAYS ledgers**, deliberately unlike
  the REST invoke's `require_effect=True` anti-spam. Two callers, one primitive,
  two effect policies: an invoke is a render (spam-prone), a fire is a refresh
  (provenance-worthy).
- **`mode="agentic"` fire: guards in order (live → maintainer present → open-run
  409 → 300s cooldown 429), then OPEN the ledger row, THEN wake.** The prompt is
  `pipeline.wake_prompt` read off the SPEC — never dereference `trigger_ref` (an
  on-demand or never-armed pipeline has no trigger; that independence is what lets
  fire rescue stuck apps). Cooldown is strategy-on-model
  (`PipelineRun.cooldown_remaining`, latest `started_at` any kind/status) and
  agentic-only — code fires are TTL-cache-protected already.
- **The check-then-open is serialized by the tracker's `_open_lock` across BOTH
  open seams** (`open_run` scheduled + `_fire_agentic` manual). Unserialized, two
  gthread workers double-open and `close_runs` settles only the newer row — the
  older stays `running` forever and 409s every later fire until the restart sweep.
  In-process locking suffices because prod is gunicorn `--workers 1 --threads 8`;
  a second worker process would need the store-level unique-open primitive instead.
- **A refused wake is a 409, never a 202.** `start_app_run` (widened to return
  `"started"|"steered"|"refused"`) can refuse (run ends between `is_running` and
  the steer; concurrent-start rejection); `_fire_agentic` then closes the
  just-opened row `failed` and returns the `wake_refused` refusal. The 202 wire
  carries ONLY `started|steered` — the console's union relies on it.
- **Go-live seed: `submit` fires EVERY pipeline once, best-effort async** (code on
  a daemon thread, agentic inherently fire-and-forget), guarded on
  `lifecycle.tracker` being wired — so freshness is no longer born "never". An
  agentic seed leaves an OPEN on_request row, so an immediate manual fire of the
  same pipeline correctly 409s/429s until `close_runs` settles it. Seed failures
  log and never raise into submit; seed store-writes touch only `run_store`, never
  racing submit's `app_store` version persist.
- **`rearm` re-reads the manifest UNDER its per-app lock — the re-read is the
  cure, not the lock alone** (a racing rearm that already committed must be seen,
  else the lost-update leaves an orphaned armed trigger double-firing with no
  sweep to catch it). A non-None, non-armed, non-terminal `trigger_ref` (the
  paused-then-resumed case) is CANCELLED before its replacement is minted —
  `submit`'s cancel-before-rearm discipline, scoped to the one stale trigger.
  Response `{armed, unchanged, seeded}`; `seeded` = pipelines the seed was
  ATTEMPTED for (a refusal still lists — accepted, logged honestly).

## The submitter seam — one push, stores self-wire

The agent plugin's tools are built through the ordinary `SessionToolRegistry`
manifest path, which feeds a constructor only `session_id` + `event_logger`. Two
resolution paths, mirroring how each collaborator is exposed:

- **`app_data`** resolves its three stores directly from the process-wide factories
  above — NO wiring needed.
- **`submit_app`** has no store factory (the `AppLifecycle` is constructed once in
  `backend.py`), so it resolves its submitter through the down-only
  `register_app_submitter(lifecycle)` push in `init_apps()` (mirrors
  `register_builtin_root`). This is the ONE line of runtime wiring the plugin needs.
  Miss it and `submit_app` returns a clean "apps runtime not configured" error, NOT
  a crash. Do NOT also inject `submit_app`/`app_data` via `extra_session_tools` — a
  second instance shadows the wired one (dispatch is first-by-id).

## Plugin suite layout + the discovery warning

`plugin/` IS the suite: its manifest sits at `plugin/.claude-plugin/plugin.json`
(the `builtin_plugins/widget_builder/` layout), NOT in a sub-suite of it. So
`backend.py:init_apps` registers `PLUGIN_ROOT.parent` (the `mewbo_api.apps` package
dir) with `register_builtin_root` — because `discover_builtin_plugins(root)` scans
a root's IMMEDIATE subdirectories for `<suite>/.claude-plugin/plugin.json`, and
`plugin/` is the one suite it finds inside. `init_apps` fires a LOUD startup warning
if discovery finds nothing (the suite dir churned during the parallel build, so a
future layout drift fails visibly instead of shipping an apps deployment with no
app-builder AgentDef). `plugin/__init__.py` holds ONLY `PLUGIN_ROOT`; the docstring
there is authoritative on this parent-vs-suite distinction.

## Capability flow — `apps` is a string three sides agree on

There is no central enum of capability ids (see core CLAUDE.md → "Capability
gating"). `apps` is real only because three sides name it: the console/Aura clients
ADVERTISE it (`X-Mewbo-Capabilities`), the plugin manifest GATES on it
(`requires-capabilities: ["apps"]`), and the lifecycle STAMPS it on every app agent
session (`client_capabilities: ["apps"]` on both the builder and the maintainer).
That stamp is load-bearing: a trigger re-woken maintainer only sees the
`app-repair` AgentDef + the `app_data` tool because its session carries `apps` and
`SessionToolRegistry.build_for` surfaces a capability-gated tool to a session that
holds the capability with NO explicit allowlist (the maintainer's shape). The
integration test asserts exactly that build.

## workspace_ref → agent session scope (v1 semantics)

`AppSpec.workspace_ref` points at the SAME project primitive ordinary sessions
anchor to (the convention) — NEVER a new workspace entity. `AppLifecycle`
maps it onto the builder + maintainer context (spec §2.3, agent-side only — the
frontend never inherits workspace capability):

- **`kind="shared"`** → the `key` IS an existing project key (config name or
  `managed:<id>`), emitted as the session `project` context field. A trigger fire
  then resolves the maintainer's cwd/MCP scope through `_resolve_session_cwd`
  exactly like a console-created session.
- **`kind="own"`** → v1 isolated default scope: NO `project` key (the session runs
  in its default temp cwd), tagged to the app by `app_id` alone. This is the
  SMALLEST honest mapping — there is no "own-workspace" primitive to compose in v1,
  so none is invented. A first-class private-workspace entity (its own
  CLAUDE.md/.mcp.json/memory) is the phase-2 gap; do not fake it by minting a
  managed project here.

## SDK delivery — server-side, at the render seam only

The served stlite frontend `import mewbo_app` (the sanctioned network path, spec
§2.5); the backend injects that SDK source rather than the console/Aura bundling
it. `_load_app_sdk_files()` reads `plugin/sdk/mewbo_app.py` ONCE at startup and
caches it on the controller (`sdk_files`); `AppsRoutesController._render_spec`
does TWO edits to the RENDERED `spec.frontend.files` at the `_detail` seam (GET /
PATCH / rollback all return `AppDetail` through it), in order: (1) **STRIP
pipeline source** — delegating to `AppSpec.served_frontend_files()` (the model
owns the predicate: every `mode="code"` `entrypoint` + anything under `pipelines/`
is the engine's server-side input and must not reach the browser's file map); (2)
**INJECT the SDK** (wins a name collision so the sanctioned SDK always runs). The
STRIP also applies to every `versions[]` snapshot ON THE WIRE via `_render_version`
(same `served_frontend_files()` projection, no SDK — history is rendered read-only,
not run) so a version echo can't leak the exact source the live strip withholds;
the SDK injection does NOT touch history (opt-in, upgradable without a version
bump). The STORED `AppSpec` and its version history keep every file — the STORE
retains the pipeline source (the runner reads it), the WIRE does not. Strip is
unconditional (a security concern); injection is opt-in on `sdk_files`. A missing
SDK file logs loudly and degrades to no injection (served apps fail their `import
mewbo_app` visibly, never a server crash).

## Write-back — `user_writable` pipelines + the write-scoped token (wave 5)

The one write path a SERVED frontend has: `app.pipelines.submit(name, params)`
(the SDK) → `POST /api/apps/<id>/pipelines/<name>` (params in the JSON body, so
object/array values are fully supported, unlike the scalar-only GET `run`). It is
gated so least privilege stays STRUCTURAL:

- **`PipelineSpec.user_writable` (model field, w5) declares a `mode="code"`
  pipeline's params are user form input.** `submit_app`'s `SubmitPipelineArgs`
  carries it as a pass-through (validator: `user_writable` requires `mode="code"`).
  The SAME floor is a `PipelineSpec` `model_validator` now (w5 fix — HISTORY-SAFE:
  the field is new with default `False`, so no stored snapshot holds
  `user_writable=True`, unlike `ensure_wakeable` which had to stay off the model),
  so no path — chat-builder, rollback replay, a hand-built spec — can persist a
  `user_writable` agentic pipeline that would mint an unusable write token.
- **A WRITE token is mintable ONLY for an app that declares one.** `mint_token`
  gates `scope="write"` two ways: the literal master token (`require_master_token`,
  unchanged) AND `app.has_user_writable_pipeline` (a strategy-on-model property;
  else 403). So a served app with no form pipeline can never obtain a write
  credential it couldn't use.
- **The POST/form invoke additionally requires the TARGET pipeline be
  `user_writable` (w5 fix — per-pipeline least privilege).** The write token is
  APP-scoped, so `invoke_pipeline` gates the POST path (`json_body`) on
  `pipeline.user_writable` (else 403) — a write credential invokes a form pipeline,
  never a sibling effectful one on the same app. This also collapses the
  stale-token-after-despec risk with no extra bookkeeping: a pipeline flipped to
  `user_writable:false` now 403s there (a removed one 404s). The GET path stays
  open to ANY code pipeline including effectful ones (a served app refreshing
  itself is deliberate). So `user_writable` gates the POST/form path only, never
  GET-reachability.
- **The console mints write IFF the spec declares a `user_writable` pipeline** and
  injects the token's `scope` into `_app_context.json`; the SDK reads it (absent ⇒
  `read`) and raises a clear client-side error on `submit` from a read-token page,
  never a silent 403. Aura mints read-only + injects no `scope`, so an Aura-rendered
  form fails the same client-side check — write-back from Aura is a phase-2 gap
  (Aura would need to detect the user_writable app, mint `{scope:"write"}`, and add
  `scope` to its `mewbo-app-payload` `app_context`).

## Routes — traps that already bit

- **The dual-registration 500 (regression guard).** A Resource carrying BOTH an
  `@apps_ns.route(...)` decorator AND `add_resource()` registers the decorator
  copy WITHOUT `resource_class_kwargs`, so the controller is never injected and
  EVERY request 500s. `add_resource` in `init_apps_routes` is the SOLE registration
  (the `triggers/routes.py` idiom). Controller-direct tests structurally cannot see
  this — `tests/apps/test_apps_routes_flask.py` (Flask test client) is the guard.
  Never re-add a route decorator.
- **`/system` is the ONE introspection surface.** The granular `/system/triggers` +
  `/system/runs` were REMOVED — the console, Aura, AND the injected SDK all read the
  single consolidated `GET /api/apps/<id>/system` (`{app_id, status, freshness,
  triggers, runs, maintainer, pipelines, unscheduled_pipelines}`, the same endpoint
  the platform health pane uses, DRY). Don't split it back out. The **`pipelines`**
  array (Phase 1, additive) carries the declared per-pipeline tier —
  `{name, schedule: <union|null>, on_demand, trigger_ref, armed}` — so a client
  renders "refreshes hourly" vs "on-demand" vs the unscheduled warning. The
  wave-1 top-level **`unscheduled_pipelines`** field is LOCKED with the console
  (unchanged derivation: `trigger_ref` not armed) — an on-demand pipeline still
  appears there, and the console suppresses the false warning via the new
  `pipelines[].on_demand`. Don't "fix" `unscheduled_pipelines` to exclude
  on-demand; that's the console's job with the new field.
- **Error envelope = a top-level `message`.** `_error()` returns `{"message": ...}`
  (the `ApiResponseKit` `shape="message"` decorators document it) so the console's
  `readJson` reads `data.message` — matching `agentic_search`. `ApiResponseKit` has
  no generic runtime error builder (only the 410 `terminated_response()`), so the
  shape is produced directly; don't invent a nested `{error:{...}}` shape here.
- **The data plane is READ-ONLY at REST.** `AppData` / `AppSystem` implement only
  `get`; any write verb 405s. All writes happen agent-side through the `app_data`
  SessionTool (schema-validated), never over HTTP.

## Health honesty — "succeeded" is not "did its job"

`_freshness` (`routes.py`) derived health from ONE predicate: `stale =
last_run.status != "succeeded"`. That misses the failure a user actually notices.
Live-verified: a run wrote 1 doc to `today_digest`, 0 to `records` — the
collection every frontend page reads — closed `succeeded`, and every signal
stayed green while the dashboard rendered empty. Even `PipelineRun.wrote_nothing`
was `False`, because it asks "did this run write ANY doc anywhere", and it had.

`PipelineRun.unwritten_collections(declared_collections)` is the missing
predicate: succeeded-only, declared names minus the keys in `docs_written`. It
takes the declared names as a METHOD ARG — the run row doesn't know the app's
collections, and models never reach for I/O (the `TriggerSpec`-takes-the-clock
rule). Surfaced on both the run payload and `/system` freshness as a NEW sibling
field; **`stale` keeps its exact prior meaning** — an added signal, never a
redefinition, because silently repurposing a shipped field breaks clients
invisibly.

Two honest bounds, documented rather than discovered: it is **latest-run-only**
(a pipeline that stops targeting a collection it once wrote fires this on every
poll — "current behavior misses a declared collection", not "this collection has
ever been empty"), and `wrote_nothing` still isn't mirrored into the console
types or rendered anywhere, so it remains backend-log-only.

## Auto-repair on an integrity violation — the detect→repair loop

`unwritten_collections` made the violation VISIBLE; `PipelineRun.new_integrity_violations`
makes it ACTIONABLE, through the SAME `on_pipeline_failure` dispatch a raising run
already uses. No second dispatcher, no second policy enum, no second repair path —
the tracker just feeds a second KIND of reason into `handle_pipeline_failure`.

- **The run stays `succeeded`. The violation is a SEPARATE axis from run status.**
  Marking it `failed` would be a lie that corrupts `stale`/`last_success_at`/
  freshness — the signals this same wave just made honest. So the reason travels
  beside the status as a **`PipelineIssue`** (`models.py`), which replaced the
  dispatcher's old bare `error: str | None`. Two shapes (`run_failed`,
  `unwritten_collections`), each owning its own `describe()`/`repair_brief()`
  prose, because a repair agent told only "something went wrong" hunts for an
  exception that never happened. `_repair_prompt` composes only the PROCEDURE and
  delegates the DIAGNOSIS to the issue.
- **Anti-spam is TWO rules, both on the model, and both are load-bearing.**
  (1) *Regressed, not never-populated*: a collection counts only once some earlier
  succeeded run of THIS pipeline actually wrote it. A first-ever run and an
  app whose upstream is legitimately empty therefore dispatch NOTHING, forever.
  (2) *Edge-triggered, not level-triggered*: a violation the immediately-preceding
  succeeded run already reported is subtracted, so one break dispatches ONCE —
  including when the repair it spawned did not fix it. Suppression is
  per-collection (a NEW regression alongside an ongoing one still gets through)
  and the edge RE-ARMS after a recovery. Visibility stays level-triggered on
  `/system`; only the ACTION is edged.
- **The baseline is the LEDGER, deliberately NOT a live `AppDataStore` read** —
  which looks like the more direct question ("does this collection hold docs?")
  and is the trap. Collections are declared APP-wide while runs are per-PIPELINE,
  so a store read cannot attribute a collection to the pipeline that feeds it: in
  a two-pipeline app every run of A would report B's collection as regressed,
  forever. The ledger baseline is pipeline-attributed by construction and needs no
  I/O. `INTEGRITY_HISTORY_LIMIT` (20) bounds the scan; it is a bound, not a tuning
  knob, and deliberately not a time window (the question is ordinal, so a quiet
  week must not empty a slow pipeline's baseline).
- **`dispatch_failure` is reused verbatim, so the manual-fire law holds for free.**
  A scheduled fire dispatches; a REST invoke and a manual `/fire` do not — a user
  hammering a broken pipeline must never auto-repair or auto-pause. The agentic
  close seam gates on the same asymmetry via `kind == "scheduled"`.
- **An integrity issue ALSO emits `app_issue` under EVERY policy** (`needs_own_event`),
  unlike a failure. A failed run is already user-visible as a failed ledger row;
  an integrity violation's row is a green `succeeded`, so without the event
  `repair`/`pause` would act on something the user was never shown. The payload
  keeps its shipped `{app_id, error}` shape and gains `kind`/`pipeline`/
  `collections` additively. The policy itself means the SAME thing for both kinds —
  an operator who declared `pause` gets a pause either way; second-guessing that
  would make one declaration mean two things.

**Console gap (not yet closed):** `app_issue` is still emitted-but-unconsumed (see
the v1 gaps below), so today an integrity violation reaches a user through
`/system`'s `unwritten_collections` and the backend log, not through a
notification. Rendering the new structured payload is the console-side follow-on.

### The repair wake — three concerns, three homes

A repair fires into a FRESH maintainer context that holds no memory of the app and
no staged files, so a prompt naming only the problem burns its first turns
rediscovering its own toolbox. The cure is split so nothing is duplicated:

| Home | Owns | Why there |
|---|---|---|
| `PipelineIssue.repair_brief()` (`models.py`) | The FACTS — which pipeline, which collections, whether anything raised | Varies per dispatch; belongs to the data |
| `AppLifecycle._repair_prompt` (`lifecycle.py`) | The SURFACES — `get_app` get/**stage**, `run_pipeline(dry_run)`, `app_data`, `submit_app`, and "delegate via `spawn_agent`" | Names the toolbox so the wake is productive; stays a POINTER |
| `plugin/agents/app-repair.md` (AgentDef) | The durable HOW — hypothesis set, ordering, verification bar | Reached identically when a USER reports "my dashboard is empty" and no dispatched prompt exists |

The hypothesis list lived in `repair_brief()` in an earlier cut and was MOVED DOWN
into the AgentDef — it is the same advice for both entry paths, so a copy in a
prompt string was a second copy to drift. `get_app`/`stage` leads the surface list
deliberately: staging is ephemeral and normally already gone when a repair fires,
and it is the ONLY route back to a `mode="code"` pipeline's SOURCE — without it the
agent cannot edit the thing that broke.

**The AgentDef gained the no-exception case**, which its prior framing actively
mis-taught: every diagnosis instruction said "read the ledger error / which error",
and for a green run there ISN'T one. It now states that plainly (a run that raised
would have closed `failed`), points at the ABSENCE of writes as the evidence
(`get_app` doc_counts + `app_data` query), and carries the four-cause hypothesis
set — source resolving to nothing, **a swallowed error** (the highest-frequency
cause, and the reason the run reports success at all), a filter excluding every
row, a collection renamed on one side only. It also raises the verification bar for
this case specifically: a clean `dry_run` does NOT prove the fix, because a clean
dry run is exactly what the broken pipeline already produced — the count must be
non-zero.

**No skill is referred, and that is a finding rather than an omission.** The only
skill in the suite is `plugin/skills/app-builder/`, whose entire content is "spawn
the app-builder sub-agent" — there is no repair skill. More decisively, the
`app-repair` AgentDef declares `disallowedTools: [spawn_agent, exit_plan_mode,
activate_skill]`, so it **structurally cannot activate a skill at all**. Telling it
to would instruct it toward a tool it does not hold. A repair skill would also have
no caller: the builder skill exists so a root can learn to delegate when a user
asks out of nowhere, whereas a repair is platform-dispatched with a prompt that
already says to delegate.

## Policy enforcement — `AppPolicies` (spec §2.11)

- **`max_docs_per_collection` is enforced at the STORE.** `AppDataStore.upsert`
  takes an additive `max_docs` kwarg; a NEW key that would breach the cap raises
  `CollectionCapExceeded` (an UPDATE to an existing key is always allowed — the
  store alone knows new-vs-existing + the live count, so it owns the cap). The
  `app_data` tool passes `app.policies.max_docs_per_collection` and maps the
  exception to a clean agent-visible `code:"cap"` error the agent self-corrects on.
- **`on_pipeline_failure` is enforced at the run-tracker close seam** — see "The
  run lifecycle" above, and "Auto-repair on an integrity violation" for the
  second, non-failure reason that reaches the same dispatch.

## Constraints + accepted v1 gaps (document, don't "fix")

- **Upstream-API only.** This package is a thin product surface; the CLI never
  imports it (the `agentic_search` precedent — store lives app-side). No domain
  engine grows here.
- **`retention_days` is UNENFORCED in v1 (honest gap, not silent).** `AppPolicies`
  carries it and the manifest stores it, but nothing sweeps expired docs — that
  needs a background reaper (a trigger-driven maintenance pipeline or a store TTL
  index), out of scope for v1. There is NO trivial enforcement point (an upsert
  can't reason about age-based deletion of OTHER docs), so it is documented here
  rather than faked. Phase-2 candidate: a per-app retention trigger.
- **`app_updated` is emitted but not consumed by clients (v1).** The lifecycle
  emits `app_updated` on repair/rollback; neither the console nor Aura live-refreshes
  an open detail screen on it — the user refreshes manually. Auto-refresh on repair
  is phase 2.
- **`own`-kind workspace = isolated default scope (v1)** — see the workspace section
  above. Not a bug; the honest smallest mapping until a private-workspace primitive
  exists.
- **REST pipeline invoke (`GET`/`POST .../pipelines/<name>`) ledgers a `PipelineRun`
  ONLY when the invoke had a real effect (Phase 2 — revised ruling, not the
  original no-ledger call).** `AppsRoutesController.invoke_pipeline` routes execution
  through `AppPipelineRunTracker.record_code_run(kind="on_request",
  dispatch_failure=False, require_effect=True)` when a `tracker` is wired (`None`
  falls back to calling `runner.execute()` directly — no ledger, unwired-tolerant).
  `require_effect=True` persists a SUCCESSFUL run only when `cache=="miss"` AND
  `docs_written` is non-empty — a cache hit or a read-only render mints NO row (the
  anti-spam line: a polling dashboard on a `cache_ttl_seconds=0` pipeline must not
  mint one per render). A FAILURE is ALWAYS ledgered regardless of `require_effect` —
  a genuine break is real provenance no matter how it was invoked — but
  `dispatch_failure=False` means it never triggers `on_pipeline_failure`: a user
  manually invoking a pipeline that errors must not auto-repair/pause the app: only
  an autonomous SCHEDULED fire does that (`dispatch_failure=True`, the fire seam's
  default, unchanged). `record_code_run` returns `(PipelineRun, PipelineResult |
  None)` — `result is None` ⇒ the run failed, read `run.error`; the controller keeps
  BOTH the ledger write (via the tracker) and the response payload (via `result`)
  from ONE execution — no forked bookkeeping, no double-execution. A WRITING invoke
  legitimately moves `/system` freshness/"last success"; `kind` lets a client
  distinguish an on-request run from a scheduled one; schedule-health stays the
  separate armed/unscheduled signal. Phase-2.5 candidate: a per-pipeline cooldown on
  repeated on-request FAILURES specifically (today every failed invoke ledgers +
  skips repair-dispatch, which is safe but could accrete rows under a persistently
  broken pipeline hammered by a client).

## The read surface — `get_app` (agent-facing introspection + recovery)

The lifecycle before this tool: an app was **write-only + execute-only** from the
maintainer's own point of view — `submit_app` writes, `run_pipeline` executes,
`app_data` reads/writes app data, but nothing let a re-woken maintainer read back
what it had already shipped. Since the only update path is "resubmit the WHOLE
app" (`submit_app` reads the entire bundle off disk), a fresh context that no
longer holds the staged frontend files literally could not update the app.
`plugin/get_app.py` closes both gaps: `get` (the live manifest, no file bodies)
and `stage` (re-materialize the full stored bundle to disk).

- **Resolution mirrors `run_pipeline` exactly: no `app_id` argument.** The app is
  whichever one this session owns (`owner_session_id`) or maintains
  (`maintainer_session_id`) — structural cross-app protection, not a runtime
  check. A session bound to no app reads a uniform `not_found` (no existence
  leak, same as `app_data`/`schedule_trigger`).
- **`stage` is the canonical recovery path**, not just a convenience: it
  re-materializes the RAW `spec.frontend.files` (every frontend file AND every
  `mode="code"` pipeline SOURCE — never the browser-served
  `served_frontend_files()` projection, since a maintainer editing a pipeline
  needs its source) into `<apps_root>/<session_id>/<app_id>/`. The staging
  directory is EPHEMERAL and must never be assumed present across turns or a
  restart — `stage` is what a maintainer calls to get it back. Confined twice:
  the app dir must resolve under this session's dir (defense against a hostile
  stored `app_id` relocating within the apps root — the model's validator only
  bans `:`, not `/`/`..`), and every file path must resolve under the app dir.
- **`trigger_declared` is a deliberate honesty split, not a naming accident.**
  It reports whether the manifest carries a platform-stamped `trigger_ref`
  (non-`None` ⇒ the platform armed a schedule for it at submit) — DECLARED
  state, resolved only from the app/data/run stores this tool holds. It is
  named apart from `/system`'s `armed`, which checks the LIVE trigger store, on
  purpose: the two DIVERGE for a paused/broken app (the ref persists on the
  manifest while its trigger is paused/cancelled), which is exactly the state a
  repair agent needs to see honestly rather than conflated with "armed".
  `get_app` reports the declared signal; `/system` owns live arming.
  `doc_count` carries the same honesty discipline (no store `count()` exists,
  so it's `len(query(..., limit=1000))` with a `count_capped` flag rather than a
  silently-wrong exact number).
- **Terminal-free** (the `app_data`/`run_pipeline` shape): reading or staging is
  normal iterative work, never a run exit — `should_terminate_run`/
  `terminal_reason` are defined explicitly (structural `SessionTool` Protocol,
  no inherited bodies; see the `submit_widget` post-mortem in core's CLAUDE.md).

## Submit-time verifier — `AppLifecycle.submit` dry-runs code pipelines

Before this landed, a `mode="code"` pipeline could go live with a lint pass but
still break on its first real fire (an import error, a bad `ctx.read_file` call,
anything lint can't catch). `_verify_pipelines` closes that gap by dry-running
every code pipeline through the SAME `AppPipelineRunner` the fire seam uses
(reached via the already-wired `tracker.pipeline_runner` — no new DI), before
anything else in `submit` mutates.

- **Ordering is the whole point: verify runs BEFORE any mutation** — before the
  maintainer session is minted, before a resubmit's prior triggers are
  cancelled, before pipelines arm, before the manifest/version persist, before
  the go-live seed fires. A refusal therefore leaves NO state: no orphaned
  session, no cancelled-then-nothing trigger, no half-armed pipeline. It runs
  AFTER the live-overwrite guard, so a double-submit collision is reported as
  that, never masked by a verification error.
- **`dry_run=True` + calling `execute()` directly (never the tracker's
  `record_code_run`) guarantees no durable write and no ledger row** — the
  verify must leave no trace, including under `ctx.llm` (a dry run still makes
  a real, budget-bounded model call if the pipeline declares one; only agentic
  pipelines are genuinely model-call-free, and only because they're skipped).
- **The verdict map is `{pass, fail, skipped}`, persisted on `AppVersion.verification`
  keyed by pipeline name — but `fail` never actually reaches storage.** A
  genuine failure (lint/import/runtime/syntax/traversal) raises a `ValueError`
  that refuses the submit as an actionable reask before any version row is
  written; the `fail` member exists for honesty should that refusal ever
  soften. `skipped` covers four cases, all verifier ARTIFACTS at submit time,
  never pipeline defects: `mode="agentic"` (a wake is judgment, not
  smoke-testable); the runner unwired (logged once, never a crash); a dry run
  that fails on `params` (a `params_schema` requiring input a `params={}` smoke
  can't supply, e.g. a `user_writable` form); and a dry run that fails on
  `workspace` (at submit time NO app has a bound workspace yet — the
  maintainer session isn't minted until after verification runs, so an
  unconditional `ctx.read_file()` trips this every time, always an artifact of
  verifying early). Only `params`/`workspace` `PipelineExecutionError.code`s get
  this treatment; every other code is a genuine failure.
- **The go-live seed is unchanged** — it remains the post-live real fire that
  gives freshness its first data point; the verifier is a pre-live smoke test,
  not a replacement for it.
- **THE DISARM TRAP — a pipeline that swallows `PipelineExecutionError` makes
  this entire gate unreachable.** The dry run can only classify what actually
  PROPAGATES out of `execute()`. A pipeline wrapping `ctx.read_file`/`ctx.glob`
  in a broad `except` that returns a default therefore verifies `"pass"` no
  matter what broke — and it defeats the `params`/`workspace` skip-classification
  too, since those are also just codes on a raised error. Live-verified failure
  mode: an absolute `ctx.read_file` path (a `traversal` code, which this gate
  treats as a GENUINE failure and would have refused the submit for) was eaten
  by an `except Exception: return []`; the app went live, every scheduled run
  reported `succeeded`, and it wrote zero documents to the collection its whole
  frontend read. `check_pipeline_error_swallow` in the pipeline rule table is
  the structural fix — see the lint law below.

## Pipeline lint rules are a LIVE-FLEET MIGRATION, not a submit gate

`lint_pipeline` (`pipeline_runner.py`) is re-run by the runner on EVERY
execution, not only at submit — that is the "SAME allowlist gates both
boundaries, no drift" law, and its consequence is easy to miss: **adding a rule
to `_PIPELINE_RULES` can retroactively fail an already-live pipeline at its next
scheduled fire.** An outage vector with no relation to anyone submitting
anything.

So, before landing any new pipeline rule: **lint every STORED app version against
it** (live specs and `app_versions` snapshots alike), and either repair the apps
it breaks or consciously accept stranding them. Rollback is a shipped feature, so
a version that now fails lint is a version the user can no longer roll back to.
Accepting that is fine; discovering it in production is not.

`check_pipeline_error_swallow` itself is deliberately permissive about
re-raising: ANY `raise` in the handler body counts, including one under an `if`.
Requiring every path to re-raise would need real reachability analysis and would
false-positive the legitimate narrow-tolerance shape (`if getattr(exc, "code",
None) == "read": return []` then `raise`). The accepted gap — a contrived `if
False: raise` — needs deliberate evasion, not the sloppiness the rule targets.

## Version transparency — `AppVersionSummary` + persisted `verification`

`AppVersion` gained two additive fields (`summary: AppVersionSummary | None`,
`verification: dict[str, PipelineVerdict] | None`) so a submit's actual effect
is a stored, re-readable fact instead of something an agent (or a user) has to
reconstruct from a diff of two spec blobs.

- **`AppVersionSummary` is a pure, strategy-on-model diff** (`models.py`):
  `compute(prev, new)` takes both specs as method args (no store, no I/O —
  `prev=None` for v1 means everything reads as "added"), `describe()` renders
  one compact line. Files diff by path; pipelines and collections diff by NAME.
  **`trigger_ref` is deliberately excluded from "changed"** — it's
  platform-owned output the lifecycle re-mints on every arming pass, so
  including it would false-positive a "pipeline changed" on every single
  resubmit even when the builder touched nothing.
  - **Stamped at both submit AND rollback**, with different baselines: submit
    diffs the incoming draft against the PRIOR LATEST version's spec (read
    before persisting the new snapshot); rollback diffs the current LIVE app
    against the snapshot being rolled back TO (the reverse direction — what
    reverting changes). Rollback's `AppVersion.verification` stays `None`: the
    target snapshot's pipelines were verified at their ORIGINAL submit, and
    rollback re-arms without re-executing them.
- **Wire delivery needed no route change.** `_render_version`/`model_dump`
  already surfaced additive fields structurally — old rows simply carry
  `null`/absent for both, and every consumer must tolerate that (no migration).
- **`submit_app`'s success echo reads the version back**, rather than carrying
  the summary through in-memory: it calls `get_app_store().get_version(app_id,
  version)` after `AppLifecycle.submit` returns, and appends
  `summary.describe()` plus any non-`pass` verification verdicts to the
  builder's tool result — so the agent sees exactly what a console user would
  see on the Versions rail, in one round trip.

## Pipeline-name uniqueness — a second submit-boundary method, not a validator

`AppSpec.ensure_unique_pipeline_names()` refuses duplicate pipeline names, but
deliberately as a plain method `AppLifecycle.submit` calls on every incoming
draft — NOT a `model_validator`. This is the same discipline
`PipelineSpec.ensure_wakeable()` already established: the app store is
append-only, so a legacy `AppVersion` snapshot that already holds a duplicate
(pipeline resolution has always been first-match, so a dup silently shadowed
one pipeline rather than erroring) must keep PARSING for a detail read. A
parse-time floor here would 500 every detail read of any pre-existing app that
happens to hold one — the exact live-verified trap `ensure_wakeable`'s docstring
already documents for the wakeability floor. Validation of new data belongs at
the trust boundary it crosses (submit); stored history re-crosses the parse
seam under the contract it was written with.

## Testing

Contract tests from the caller site, stub only I/O (JSON stores real, sessions
faked, injected NOW). `test_apps_lifecycle.py` (submit/re-home/pause/rollback +
workspace scope + verifier verdicts + version summary), `test_apps_routes.py`
(controller domain logic incl. SDK-free default, mint_token scope gating,
read/write authorization), `test_apps_routes_flask.py` (Flask client — the
dual-registration guard + the real startup wiring: submitter registered, SDK
loaded, pipeline runner wired), `test_apps_integration.py` (the two cross-stream
seams: capability build surfaces `app_data`, SDK injection at the detail seam vs
the clean stored spec), `test_apps_tokens.py` (signer mint/verify round-trip +
write-scope + legacy 4-part blob backward compat), `test_apps_pipeline_endpoints.py`
(controller-direct + Flask-client: pipeline list surface, GET/POST invoke incl. params
coercion/validation, agentic 409, unwired 503, runner-exception 502, the full
write-scope token matrix, and the ledger-effect matrix: writing invoke ledgers +
moves freshness, read-only invoke doesn't, cache hit doesn't, failed invoke ledgers
without dispatching repair), `test_apps_get_app.py` (get/stage, session scoping,
hostile-app_id staging guard, trigger_declared honesty). The integrity
auto-repair loop is covered at all three altitudes: the two anti-spam rules as
pure model tests (`test_apps_models.py::TestNewIntegrityViolations` — never-populated, cache-hit, edge suppression, edge re-arm after recovery), the
agentic close seam end-to-end (`test_apps_pipeline_tracker.py::TestIntegrityDispatch`
— a regression dispatches while the run stays `succeeded`, repeated fires dispatch
ONCE, a manual fire never dispatches, a sibling pipeline's collection is never
blamed), the code tier (`test_apps_pipeline_runner.py::TestCodeFireIntegrityDispatch`),
and every policy value under an integrity issue (`test_apps_lifecycle.py::TestIntegrityIssuePolicy`). Never run a real LLM or
hit a real proxy.
