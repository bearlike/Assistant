> ↑ [root /CLAUDE.md](../../CLAUDE.md)

# Mewbo Core — Engine Guidance

Scope: `packages/mewbo_core/src/mewbo_core/` — the async tool-use engine,
hypervisor, session runtime, hooks, plugins, and the built-in plugin suite.
This file captures the engineering decisions + orchestration invariants that
aren't obvious from the code. For cross-cutting layering rules see root CLAUDE.md.

**Layering (see root CLAUDE.md → "Monorepo layering"):** core is the lean
base of the dependency DAG — it imports DOWN from nothing and must never
import an app or a capability library; keep heavy/optional deps behind a
`mewbo-core[...]` extra. (Gitea #25 relocated the `wiki`/`scg` plugin suites +
their substrate to `mewbo_graph`; core's `builtin_plugins/` now holds only
zero-app-import suites like `widget_builder`. A library above core contributes
its plugins via `plugins.register_builtin_root` — a push, so core never imports
up to find them.)

## What this package is

The single source of LLM-driven orchestration for every Mewbo
interface. CLI, REST API, web console, Home Assistant, and Nextcloud
Talk all import from here. If a behavior belongs in "the assistant
itself" (as opposed to "the API server" or "the CLI display"), it
lives in this package.

## Orchestration invariants (key decisions)

- `tool_use_loop.py:ToolUseLoop.run()` is the only execution engine.
  No separate planner/executor/synthesizer.
- **Deferred tool-schema loading (`tool_search`, Gitea #131).** MCP / `metadata.deferred`
  schemas are stripped from the initial `bind_tools` and surfaced by name via
  `<available-mcp-servers>`; the model fetches what it needs through the client-side
  `tool_search` tool (`ToolSearchRunner`, in `mewbo_tools`), and the per-turn re-bind
  (`run()` ~:793) grows the bound set — discovery is replayed from message history each
  turn, so it is compaction-resilient. `_is_tool_search_enabled(tool_specs)` is the SINGLE
  read-point: `off`/`on`/`auto`, where **`auto` (the shipped default) defers only when the
  deferrable count exceeds `agent.tool_search.auto_threshold` (25)** — lean sessions pay
  nothing. Two load-bearing invariants: (1) `tool_search` is `always_load`, and
  `filter_specs` EXEMPTS `always_load` specs from the `allowed_tools` allowlist gate (an
  explicit deny still wins) — else a scoped sub-agent gets its MCP tools deferred AND loses
  the means to fetch them. (2) Deferral is ORTHOGONAL to the plan-mode filter: `tool_search`
  is read-only so it binds in plan mode, and a discovered MCP tool passes through the
  existing `plan_mode_allow_mcp` gate (MCP visible after the first search; graceful no-op
  when disallowed) — never special-case plan mode in the deferral path.
- **A SessionTool that RETURNS a structured-error envelope is a FAILED step.**
  A tool signals failure to the loop by RAISING (caught → `success=False`) OR by
  RETURNING the shared `{"error": {"code", "message"}}` envelope (the graph
  suites' `err_result`/`_err_result`, returned as a *successful* `MockSpeaker`).
  The latter used to record `success=True`, so the per-step failure-feedback
  nudge ("N/M tool call(s) failed this step") never fired — an enveloped
  embedding-429 rendered as "✓ ok". `_session_tool_error_envelope` (a pure
  structural check — core never imports the graph layer; the envelope shape is
  the only contract) reclassifies such a return: the `tool_result` event gets
  `success: False` + the envelope message as `error`, while the model STILL
  receives the envelope JSON as the tool output. The seam fires ONLY on the
  session-tool dispatch path; normal `ok_result` returns are untouched.
- `hypervisor.py:AgentHypervisor` is the only place sub-agents register,
  cancel, get budget warnings, and resolve to their `AgentResult`.
- `spawn_agent.py` is the bridge between the LLM-facing tool schema
  and the hypervisor. Schema fields stay backwards-compatible —
  removing a field breaks every running agent that already learned
  it. The `sub_agent` lifecycle event's `detail` is only the
  `done_reason` on `stop`; the child's real result (its compressed CU,
  `tq.task_result`) rides an **additive `summary`** key on that event
  (`_emit_event`, capped) so a consumer can project the child's actual
  response — the agentic-search trace's per-lane evidence panel reads it.
  Additive (only on `stop`): legacy consumers that read the existing keys
  are untouched. The event also carries an additive **`agent_type`** (the
  spawned AgentDef name, e.g. `scg-path-probe`) on BOTH `start` and `stop` —
  stored on the `AgentHandle` so the background `stop` from
  `_run_child_lifecycle` (which holds only the handle) can stamp it; omitted
  entirely for an ad-hoc spawn with no `agent_type`. The trace projection needs
  the LANE identity (the def), which the `model` name can never carry — without
  it the API trace shows the model name as the lane name.
- **Batch fan-out (`spawn_agents`, Gitea #117).** `spawn_agent` admits one task
  per call, so fast tiers that serialize delegation across turns degrade an N-way
  fan-out into N round-trips. `spawn_agents(tasks=[…])` is a thin wrapper — NOT a
  new engine: `run_async` and `run_batch_async` both funnel through one
  `_spawn_one(args, *, blocking_admit)`, so every entry takes the SAME per-task
  fields (validated at definition by the `SpawnAgentTask` Pydantic model,
  `extra="forbid"`) and rides the EXISTING hypervisor admission + non-blocking
  root lifecycle. The batch admits **non-blocking** (`AgentHypervisor.try_admit`)
  so an over-subscribed call marks the surplus `rejected` in its slot instead of
  stalling 30s per entry; it returns ordered `agent_id`s monitored via the
  existing `check_agents`. The loop is untouched (no `_partition_tool_calls`
  change). **Load-bearing fix it depends on:** the root non-blocking spawn used to
  release its semaphore slot in `run_async`'s `finally` AND again in the lifecycle
  manager — a double-release that inflated the semaphore and meant root children
  never actually held a slot (admission was a no-op for root fan-out). Slot
  ownership now transfers to the lifecycle manager (`slot_transferred` guards the
  `finally` release), so the semaphore is released exactly once and per-slot
  `rejected` is real. Any future non-blocking spawn path must transfer slot
  ownership the same way — never release a slot the lifecycle manager owns.
- `compact.py:run_compaction()` is the only compaction path. Both
  FULL and PARTIAL modes share the same `<analysis>`/`<summary>`
  prompt structure.
- `llm_resilience.py` models retry/fallback as atomic objects: `RetryStrategy`
  (per-run; holds the retry budget + circuit breaker + `_pinned_model` + knobs,
  `from_config()`, injected `invoke`/`emit`/`compact`; methods incl.
  `classify`/`backoff` describe behaviour over that state) and `DoomLoopGuard`
  (no-progress detection — **progress-aware**: halts only on identical tool input
  AND identical result across N turns, fed by `record_result`; wait/sync tools
  like `check_agents` are exempt via `DOOM_LOOP_EXEMPT_TOOLS`). `tool_use_loop` is
  a thin driver — don't reinline retries. See the root CLAUDE.md "LLM call
  resilience" invariant for the decision model + append-after-success idempotency.
  **Policy (Gitea #54):** a model gets **2 attempts** (`agent.llm_call_retries` =
  1 try + 1 retry) then the run escalates down `[primary, *fallback_models]` —
  never a 3rd attempt on a dead model. The **autoheal/rescue model is simply the
  last entry of the fallback ladder** (no dedicated key; opt-in by schema default,
  turned on in deployed config). Escalation is **sticky**: a non-primary model that
  wins is pinned (`_pinned_model` reorders the chain) for the rest of the run, so a
  dead primary is never re-probed turn after turn. Cross-model fallback lives HERE,
  **not** LiteLLM Router (a deployment-pool LB — orthogonal, and routing through it
  would lose the compact-then-retry seam). A switch emits a separate `llm_fallback`
  event carrying `to_model` + `sticky` + `reason` (`retries_exhausted` for a
  transient-cap escalation vs the classifier reason for a `switch_model` decision);
  `token_budget` reads the successful model from THAT event (not `llm_retry`), so
  `models_used` captures fallbacks — keep that reader in lockstep or fallbacks
  silently vanish from usage.
- **Delegation-layer bounded retry (Gitea #118)** complements #54: #54 heals
  *within* one `ToolUseLoop` call (model fallback ladder); #118 re-delegates a
  whole child whose loop *died*. Opt-in `retry: {max, on, backoff}` on the spawn
  schema, **DEFAULT OFF** (`max == 0` ⇒ byte-identical single-attempt path).
  `spawn_agent.py:RetryPolicy` (atomic, validated via `from_value`, never raises)
  + `SpawnAgentTool._drive_with_retry` wrap the existing admit→run→resolve, shared
  by BOTH spawn paths (blocking non-root *and* the non-blocking root lifecycle
  manager, which now creates the child task internally rather than receiving it).
  Key invariants: (1) **reuse, don't reinvent escalation** — each attempt builds a
  FRESH `ToolUseLoop` (`_build_child_loop`) so model-level recovery rides #54's
  fresh ladder; `RetryPolicy.classify_cause` reuses `RetryStrategy.classify`'s
  reason taxonomy to bucket a failure into `timeout`/`failed`. (2) **One slot for
  the whole sequence** — the semaphore slot acquired once in `run_async` is HELD
  across attempts (re-admission re-uses it), so concurrency stays bounded and
  there's no release/reacquire race. (3) **cancelled/rejected are structurally
  unretryable** — `rejected` returns before the loop; `CancelledError` is caught
  and re-raised *before* the generic retry `except`. (4) `attempts` is additive on
  `AgentResult`+`AgentHandle` (default 1) → surfaced in `check_agents` + the agent
  tree (`N attempts` marker only when >1). The driver keys off the fact that
  `ToolUseLoop.run` RAISES `LlmResilienceExhausted` on exhaustion (its main try has
  only a `finally`, no `except`) — relevant if that ever changes to a return.
- `session_runtime.py:SessionRuntime` is the only place a session is
  created, resumed, forked, or archived. Channels, the API, and the
  CLI all go through it — no parallel session implementations.
- `session_provenance.py:SessionOrigin` is the single classifier for *who
  spawned a session*. The session record stores **no** origin field — every
  session is created identically — so origin is reconstructed from two durable
  signals: the session's **tags** (`wiki:job:`/`wiki:qa:` → wiki,
  `agentic_search:` → search, channel `:room:`/`:thread:` → channel) and, as
  fallback, the first context event's `client_capabilities`/`source_platform`.
  Tags win (they survive even when a job stored empty context).
  `summarize_session` attaches it as `origin`; the console badges + filters the
  landing page on it. `SessionStore.tags_for_session` is the concrete reverse of
  `resolve_tag` (one impl over `list_tags`, shared by every backend).
- **Universal recovery (Gitea #54).** Any non-complete session is recoverable —
  `summarize_session.recoverable` is true when a run ended not-successfully (incl.
  killed mid-call with NO completion event). `resolve_recovery_query` yields the
  re-drive query (`continue` = resume the SAME session, memory/transcript intact;
  `retry` = restart the last turn), then `start_async`/`run_sync` re-runs the one
  loop — so recovery inherits automatic model-heal for every task type (all share
  the loop + `fallback_models`). Non-obvious trap: `reinject_recovery_context` MUST
  re-emit the session's gating context (`client_capabilities`,
  `structured_workspace`) on recovery, else a recovered capability-gated session
  (wiki/qa/structured) silently loses its AgentDefs because the most-recent context
  event no longer advertises them. The API `/recover` dispatches **origin-aware**:
  a session backing a recoverable wiki indexing job → checkpoint `WikiResume`
  (skip-if-done), everything else → generic continue/retry.
  - **`continue` truncation anchors on the last `recovery` marker, NOT the last
    `completion` (#84).** Its only job is to drop a STALE PRIOR continue attempt
    (the `recovery` audit event + the synthetic turn it drove) so the transcript
    doesn't accrete duplicate recovery prompts. Anchoring on the last *completion*
    was a latent data-loss bug: a run killed mid-flight (process restart) emits no
    completion for its turn, so the anchor fell back to the PREVIOUS completed turn
    and `truncate_after` physically deleted the entire interrupted turn — its user
    message and every tool call/result. That presented as a console "prior traces
    hidden" bug (the FE faithfully rendered a transcript the backend had erased).
    No prior `recovery` marker ⇒ nothing stale ⇒ preserve everything; the
    interrupted turn stays an open turn the continuation resumes from. Lesson: a
    recovery/stitch that DELETES transcript must key off a marker that is present
    ONLY when there is genuinely something stale to delete, never off a terminal
    event that a crash can skip.

## Trace provenance — filterable Langfuse tags (one seam, one classifier)

Every execution path funnels through ONE observability entry point:
`components.py:langfuse_session_context`, opened once in `Orchestrator.run`. Its
`langfuse_propagate` (a thin wrap of Langfuse `propagate_attributes`) attaches
tags+metadata to **every** child observation — nested LangChain CallbackHandler
generations AND `langfuse_trace_span` spans (planning/context/tools) — because
propagation is contextvar-based. **So enrich filterability HERE, never at
per-call-site handlers.** The seam stays taxonomy-free: it propagates whatever
`tags`/`metadata` it is handed and never learns the prefixes.

- `session_provenance.py:TraceProvenance` is the single classifier (pure, no I/O,
  sibling to `SessionOrigin`). `derive(tags, context, surface)` folds a session's
  three durable signals into filter chips + metadata. `tags` = low-cardinality
  `key:value` chips (`origin`/`product`/`session_type`/`surface`/`project`/`repo`/
  `branch`/`workspace`/`model`); `metadata` = the superset incl. high-card ids
  (`wiki_id`/`search_id`/`vcs_*`/`channel_id`/`thread_id`), `worktree`, and
  `capabilities`. Tags win over context on overlap (a `vcs:<owner/repo>` tag's
  repo supersedes a bare context `repo`); an unrecognised manual `/tag` label is
  skipped so it never masks the real product. `origin` is the console enum
  (`user`/`wiki`/`search`/`channel`/`structured`/`draft`); `product` refines it
  (adds `vcs`, which has no coarse origin). **Adding an origin is a three-site
  lockstep:** the `SessionOrigin` member + its `classify` tag-prefix, the
  `_ORIGIN_PRODUCT` map, and (if the tag carries sub-kind/ids) a `_facets_from_tags`
  arm — plus the console mirror (`types.ts` union, `ORIGIN_FILTERS`,
  `SessionOriginBadge` Record, all exhaustive). Tag a session at creation
  (the robust signal) — context fallback only catches un-tagged paths. The
  structured-family surfaces use `structured:run` (agentic `/v1/structured`) /
  `structured:fast` (its `mode:"synthesis"` no-loop lane, #85 — formerly the
  separate `/v1/structured/fast`) / `draft:stream` (`/v1/draft/stream`): the
  tag's 2nd segment becomes `session_type` so the three structured-family
  variants stay individually filterable under one `structured`/`draft` product.
  Search has three distinct `session_type`s under product `search` (#77): a RUN
  `agentic_search:run:`→`search_run`, a MAP-source job `scg:map:`→`scg_map`, and
  the legacy `agentic_search:scg:`→`scg_map` arm — a search RUN must never read as
  a map (the old `agentic_search:scg:` run tag was the mislabel; the runner now
  tags `agentic_search:run:`).
- **`source_platform` IS the client-surface channel** — the existing param on
  `start_async`/`run_sync` → `Orchestrator.run`. Each entry point stamps it:
  in-process callers (CLI) pass the kwarg; HTTP clients (console/mcp/HA) send an
  `X-Mewbo-Surface` header the API reads (default `api`) — mirror
  `X-Mewbo-Capabilities` and keep it in the CORS allow-headers or it's dropped
  cross-origin. Channels pass their platform; vcs-pickup derives `github`/`gitea`
  from the forge host. Surface precedence in `derive`: explicit param > context
  `source_platform` > `vcs:`-tag forge > `unknown` (kept visible, not dropped, so
  un-stamped paths are findable).
- `project = managed:<uuid>` is an ephemeral worktree → emitted as the `worktree`
  metadata facet, never a (high-card) `project` chip.
- **Runtime-granted capabilities must be OVERLAID into `derive`'s context (#84).**
  `derive` reads `client_capabilities` from the merged context — i.e. only the
  CLIENT-ADVERTISED set. A capability granted at runtime (the #83-B `scg`
  provider) is live in the run yet absent from context, so it would vanish from
  the trace's `capabilities` facet. `Orchestrator.run` therefore overlays the
  AUGMENTED set (`_session_capabilities`, advertised ∪ provider grants) into the
  context dict it hands `derive` — keeping `derive` a pure `(tags, context,
  surface)` transform while making the grant filterable. The corollary on the
  landing-page side: `summarize_session` surfaces the advertised `capabilities` +
  `workspace` as top-level keys for per-row chips, but deliberately does NOT probe
  the runtime predicate per session (a live store read in a session LIST is the
  wrong tradeoff) — the grant is durable only in the trace, ephemeral on the list.
- **DRY seam:** `SessionStoreBase.merge_context_events` is the one context reducer
  (most-recent payload wins), shared by `latest_context` (trace path) and
  `summarize_session` (origin/recovery path).
- Derivation runs at run start; apps write context/tags BEFORE invoking the
  runtime, so they're present. A brand-new session minted inside `run()` (CLI
  first turn) only has surface+origin until its first context event exists — by
  design, not a bug.

## Built-in plugins

`builtin_plugins/` ships the **zero-app-import** first-party suites that
belong in the core wheel (e.g. `widget_builder`). They follow the same
SessionTool contract as user-provided plugins and use the same
`filter_specs()` tool-scope rules. Discovery is a filesystem scan of each
suite's `.claude-plugin/plugin.json` (no hardcoded manifest), so adding a
suite = drop a directory here with a `plugin.json`.

Plugins whose tools wrap a heavier substrate ship **with that substrate**, not
here — the `wiki`/`scg` suites live in `mewbo_graph.plugins.{wiki,scg}` so they
import the engine down instead of up into an app (Gitea #25). A library
registers its plugin root with `plugins.register_builtin_root`;
`load_all_plugin_components()` discovers core's own root plus every registered
one. Their subsystem docs live with the engine — see
`packages/mewbo_graph/CLAUDE.md` and the wiki/scg docs it points to.

## LLM client — LiteLLM is canonical

`llm.py:build_chat_model()` constructs a LangChain `ChatLiteLLM`
instance. LiteLLM is the only LLM client we use across the project —
chat, embeddings (in the wiki), reranking. We do NOT use
`langchain-openai`, `langchain-anthropic`, or any provider-specific
client.

Reasons:
1. LiteLLM exposes a single OpenAI-compatible API surface and routes
   to whichever provider the model id implies.
2. The LiteLLM proxy gives operators a single auth point + per-key
   model allowlists + cost accounting.
3. Provider SDK drift breaks transitive deps every few weeks; LiteLLM
   isolates us from that churn.

`LLMConfig.proxy_model_prefix` (default `"openai"`) is prepended to
model names so LiteLLM routes through the proxy at `llm.api_base`
instead of dispatching to a provider SDK. This same rule applies to
embedding model names — see `apps/mewbo_api/src/mewbo_api/wiki/embedder.py`.

If you find yourself adding a `langchain-<provider>` dependency, stop
and check whether LiteLLM already does the job.

**Cross-model tool-calling is a REQUEST/RESPONSE NORMALIZATION concern — never
patch it in the loop.** If a provider's tool call doesn't appear in
`response.tool_calls`, the fix lives at the LiteLLM / response-parse seam or the
request config — NOT by detecting a model's text format (e.g. Gemini's
`default_api:<tool>{...}`) in `tool_use_loop` and re-prompting. That is brittle,
the wrong layer, and fights the abstraction we chose LiteLLM *for*. LiteLLM
already maps provider-native calls → OpenAI `tool_calls` (Gemini `functionCall`
via `VertexGeminiConfig._transform_parts`); routing `openai/<model>` to the proxy
does **not** bypass that transform. When a call looks "missing": (1) check the
other structured slots on the `AIMessage` — `additional_kwargs.tool_calls` /
`additional_kwargs.function_call` (legacy) — and normalize them into
`.tool_calls` (this is opencode's `from*Response` pattern); (2) ensure the
*request* forces structured calls (tools declared + `tool_choice` /
`functionCallingConfig`) so the provider returns a `functionCall` part instead of
narrating it as text (gemini-cli's approach). Mature multi-model agents
(opencode, gemini-cli) rely on structured calls + a normalization layer; **none**
string-match text-leaked calls. (Empirical: `gemini-3-flash-preview` through the
proxy returns clean structured `tool_calls` in the simple single-turn case;
tool-call ids carry an embedded `__thought__<signature>` for round-tripping —
LiteLLM's `THOUGHT_SIGNATURE_SEPARATOR`. So a multi-turn "leak" is a
serialization/normalization bug to fix at the adapter, never a model quirk to
detect-and-re-prompt.)

## Prompt registry (`prompt_registry.py`)

Every engine prompt has ONE schema'd home (Gitea #89). `PromptRegistry` (atomic
class) loads `prompts/registry/*.yaml` — one file per owning module (`compact`,
`loop`, `structured`, `planning`/`assembly`, `catalog`, `common`, `spawn`,
`title`, plus `files` = `template_file` pointers to standalone `*.txt`) — into
`PromptEntry`s and renders them through one Jinja2 dialect. `get_system_prompt()`
is a thin shim over it; `get_prompt_registry()` is the singleton. Key decisions
(don't relearn):

- **templated ⟺ `variables` non-empty.** A no-variable entry is returned VERBATIM
  and NEVER Jinja-parsed — that is why a static prompt full of `{`/`{{` (the
  compaction `<summary>` block, the structured force-emit JSON) survives
  untouched. Declaring a var flips it to Jinja (`StrictUndefined` — a missing var
  RAISES). `validate_all()` (the CI gate, run in the suite) asserts declared
  `variables` == the template's Jinja-AST vars, so a typo'd slot fails the build,
  not a live render.
- **`render()` never strips.** Byte-equality is controlled by the YAML block
  scalar: `|` keeps one trailing newline, `|-` strips all. Phase 1 was VERBATIM
  extraction — every migrated prompt has a golden test asserting `render(id,
  **vars)` byte-equals the original literal. Adding/editing a prompt: write that
  test first, pick the scalar style to match.
- **`render_jinja_prompt()` stays tolerant — NOT registry-routed.** Its HA callers
  rely on a missing var rendering blank; the registry's `StrictUndefined` would
  raise. HA prompts are registry-INVENTORIED (`file.homeassistant-*`) but rendered
  on the legacy tolerant env. Making them strict is a deliberate future behaviour
  change, not part of extraction.
- **Layered override = cross-model convergence.** `render(id, model=, scenario=,
  **vars)` resolves scenario (exact) > model (longest prefix) > base, with
  `mode: replace|append|prepend`. A model that diverges from the orchestration
  contract gets a small declared DELTA (append) on the shared base, never a forked
  code path. Caveman compaction is the live `scenario` example; the `gemma-` append
  override on `loop.depth.root` is the live `model` example — BEHAVIOURAL-only
  (delegate-less / terminate), never a tool-call-format fix (that stays a
  normalization concern at the LiteLLM seam). **Per-model variants now reach EVERY
  prompt (#113 Phase A):** the loop's per-step sites PLUS compact (`get_compact_prompt(model=)`),
  title, and the structured runners all pass `model=`.
- **#54 escalation re-variants the prompt + tool (#113).** `tool_use_loop` tracks
  `self._active_model` (configured primary, or the sticky-pinned escalated model
  after a fallback). Every per-step render + `_configured_edit_tool_id` reads it;
  `_apply_model_escalation` (called each turn) re-renders `messages[0]` and
  re-derives the edit-tool variant against the escalated model when it changes —
  so the heal is behavioural, not just a model swap. `_bind_model` + the resilience
  reuse-guard key off `_active_model` too.
- **Per-model TOOL variants are controllable DATA (#113).** `prompts/model_variants.yaml`
  (sibling of `registry/`) maps a model-name PREFIX → edit-tool variant; loaded by
  the atomic `model_variants.py:ModelVariantRegistry` (Pydantic `extra=forbid`,
  longest-prefix wins, `validate_all` lint gate). The old in-code
  `llm.model_prefers_structured_patch` defaults (gpt-5/o3/o4/codex/gpt-4 →
  structured_patch) MIGRATED onto that file — the function now reads it (config
  `llm.structured_patch_models` overrides on top; `defaults.edit_tool` is the
  conservative fallback). The map PAIRS with the prompt overrides by the SHARED
  model-prefix key (a model's edit variant + its `file.tools.*` nudge under one prefix).
- **Two down-only seams** (mirror `plugins.register_builtin_root`):
  `register_prompt_root(pkg, subdir)` (a library contributes its own
  `registry/*.yaml`) and `register_prompt_modifier(...)` (a mod transforms any
  rendered prompt). Core never imports up to find them.
- **Footguns:** `prompt_registry` imports `common.get_logger`, so `common.py`'s
  registry calls are LAZY in-function (import cycle). `ruff` can't lint `.yaml`
  (it parses as Python) — `validate_all()` is the YAML gate, not ruff.

## Skills, plugins, and the agent registry

Three discovery surfaces, all driven from this package:

- `skills.py` — Agent Skills standard discovery (`~/.claude/skills/`
  and `.claude/skills/`). Catalog injected into the system prompt for
  auto-invocation via `activate_skill`. User `/skill-name` is detected
  in `Orchestrator` and rendered via `skill_instructions`.
  - **Per-drive opt-out: `enable_skills` (default `True`).** Because discovery
    scans the process cwd/home, a headless product drive (search/wiki) inherits
    the HOST's `~/.claude` skills and burns its first step on `activate_skill`.
    Pass `enable_skills=False` (threaded `run_sync`/`start_async` → `orchestrate_session`
    → `Orchestrator.run` → `ToolUseLoop`, consumed at the `ACTIVATE_SKILL_SCHEMA`
    injection site) and the `activate_skill` schema is never injected — for the
    ROOT *and* every spawned child (`SpawnAgentTool` carries the flag onto child
    loops). Default `True` keeps CLI/channel behavior unchanged. The CWD-isolation
    fix (not loading host skills at all for these drives) is the deeper cure;
    this knob is the minimal-diff suppression at the injection seam.
- `plugins.py` — Plugin discovery, install, uninstall, marketplace
  read. Plugins contribute agent definitions, skills, hooks, and MCP
  tools. `load_all_plugin_components()` runs during session init.
- `agent_registry.py` — Agent definitions registry. Loaded from
  built-in agents + plugins. Wiki AgentDefs (`wiki-indexer`,
  `wiki-page-writer`, `wiki-qa`) are capability-gated — they only
  appear when the session advertises `client_capabilities: ["wiki"]`.

## Structured responses (`structured_response.py`)

Schema-constrained agentic output = a terminal `SessionTool` whose function
parameters ARE the caller's JSON Schema. `EmitStructuredResponseTool` reuses the
`ExitPlanModeTool` termination pattern (`should_terminate_run()` polled at
`tool_use_loop.py:676`) and validates `tool_input` with `jsonschema` inside
`handle()`. Validate-or-reask lives **in the emit tool** via the normal
tool-result feedback loop — on a `ValidationError` it returns a "fix these
fields" `MockSpeaker` (bounded by `reask_cap`); on success it emits a
`structured_output` event and terminates. So schema-constrained output needs
**zero new control loop and no `tool_choice` plumbing**. Prefer this tool-call
route over native `response_format` whenever other tools are bound (mixing a
strict `json_schema` with non-strict MCP tools throws an `additionalProperties`
400). Non-object/union schema roots are wrapped as `{"result": <schema>}` and
validated against the *wrapped* params (so what the model sees == what we
check), then unwrapped on the way out. `StructuredResponder` drives one bounded
session via `run_sync(strict_tool_scope=True, approval_callback=auto_approve,
extra_session_tools=[emit])` — the `extra_session_tools` param is the
plugin-manifest-free seam for injecting a `SessionTool` (threaded `run_sync →
orchestrate_session → Orchestrator → ToolUseLoop`). The emit tool's `operation`
infers to `"set"` → default policy `ASK`, so the auto-approve callback is
**required**, not optional.

**Force the emit (#40 fix), prompt-side not loop-side.** Nothing was *blocking*
the emit — the model just wasn't *forced* to call it and would finish in prose
→ `payload is None` → 422. Fix: inject `FORCE_EMIT_DIRECTIVE` via the existing
`skill_instructions` seam, and in `run()` do ONE bounded re-drive (a sharper
`SystemMessage`, reusing the emit tool's own reask machinery) when `payload`
is still `None` — only then raise. No `tool_choice`, no new control loop;
consistent with the structured-output invariant above. `StructuredResponder.run`
and `.start_async` share one `_prepare()` builder (DRY) — the SOLE provenance
stamp seam: it tags the session `structured:run` (`STRUCTURED_RUN_TAG`) and writes
the `source_platform` context event from the responder's `source_platform` field
(set by the route from `X-Mewbo-Surface`). Stamp once here and MCP `structured_query`
is covered for free. `start_async` exists so
`/v1/structured` is async-recoverable: `SessionRuntime.start_async` now MINTS and
RETURNS a storeless per-run `run_id = "<session_id>:r<seq>"` (was `bool`; `""`
still means "refused, busy" so `if not started:` callers are unbroken). The
session transcript IS the run record — no parallel run store. (Tag a session you
already hold via `runtime.tag_session`, the write-only sibling of
`resolve_session`'s tag *resolution* — never reuse a constant tag as a resolution
key or two runs collide onto one session.)

**Graph-first structured seam (#77) — additive, keeps core graph-free.**
`StructuredResponder` gained four optional, app-injected fields so a search
workspace can drive it graph-first without core importing the SCG engine:
`capabilities` (override the default `wiki` advertisement with `["scg"]`),
`context_events` (extra binding context, e.g. quarantined instructions),
`extra_instructions` (a trusted playbook PREPENDED to `FORCE_EMIT_DIRECTIVE` in
the one `skill_instructions` slot), and `scope_factory` (a context-manager
factory — the `ScgScope` source scope — wrapped around each `_drive`). All
default to the historical wiki behaviour. The app side
(`agentic_search/scg/graph_structured_runner.py`) supplies them via
`WorkspaceGraphBinding`; the terminal stays the schema-validated `emit_result`.

**`done_reason` on success = `"completed"`, not `"awaiting_approval"`.** The loop
reads `SessionTool.terminal_reason()` (default `"awaiting_approval"` — the
`ExitPlanModeTool` pattern) from the terminating tool's class; `EmitStructuredResponseTool`
overrides it to `"completed"`. Without that override a successful structured emit
looked like a parked approval gate, so `GET /v1/structured` never settled (#40
reopen). Don't add a hardcoded literal at the `should_terminate_run()` break —
override `terminal_reason()` on the tool class instead.

**`start_async` drives through `runtime.start_command`** (the `RunRegistry` seam)
— a managed background run, serialized per session and cancellable — NOT a raw
daemon thread. `SessionRuntime` stays the only place a session runs.

**Two no-loop synthesis primitives** reuse the emit machinery without a
`ToolUseLoop`: `StructuredSynthesizer` (one schema-constrained round-trip + one
reask, reusing `build_emit_schema` + `EmitStructuredResponseTool.handle`) and
`DraftStreamer` (one **tool-light** `.astream()` of token deltas — NO
`bind_tools`). Retrieval grounding is injected via the `GroundingProvider`
Protocol so **core stays graph-free** (the concrete `WikiGroundingProvider`
lives in the app). `model_name None → config default` (build_chat_model
requires a `str`). Both stay store-free here; the app session-backs them with
write-behind persistence via `RealtimeSessionRecorder` (#78 — see
`apps/mewbo_api/CLAUDE.md`), keeping the latency-critical synthesis in core
unburdened by a session store.

## Capability gating

`capabilities.py` defines the capability surface. A session declares
its capabilities via a `client_capabilities` context event written at
session-creation time. Tools and agent definitions can be gated on
specific capabilities — see the wiki capability for the canonical
example.

When you add a new capability:

1. Define it in `capabilities.py`.
2. Have the producer (the caller that creates the session) advertise
   it via `runtime.append_context_event(session_id, {"client_capabilities": [...]})`.
3. Have the consumer (the agent definition or tool) filter on it.

**Runtime grants (#83-B).** A capability can also be granted by a RUNTIME
predicate instead of a client advertisement: `register_session_capability_provider`
is a down-only push seam (mirrors `plugins.register_builtin_root`) that an
optional library above core uses to grant a capability when a live condition
holds. `Orchestrator._session_capabilities` is the single read-point — it unions
`augment_session_capabilities(...)` over the advertised set, so the grant lands in
ONE seam and flows through the unchanged `requires-capabilities` gate (no
per-tool filtering). `mewbo_graph` registers the `scg` provider (granted once
`scg.enabled` + a source is mapped) so the scg reasoning tools reach ordinary
sessions. Providers are best-effort (a raising predicate is logged + skipped).

**Capability gating has TWO enforcement surfaces — gate BOTH (#84).** A
capability gates (a) the AgentDef/skill CATALOGS via `filter_by_capabilities`
(catalog render + `spawn_agent`/`activate_skill` lookups read `session_caps`), AND
(b) the per-agent SESSION-TOOL build. These are independent gates. For a long
time only (a) consulted capabilities; `SessionToolRegistry.build_for` selected
tools **purely by `allowed_tools`**, so a capability-gated plugin SessionTool
(`scg_*`, manifest `requires-capabilities: ["scg"]`) was invisible to a session
that got the capability by RUNTIME GRANT rather than an explicit `allowed_tools`
entry. The bug only bit re-engagement: the original run worked because it
*spawned* `scg-mapper` sub-agents (whose AgentDef lists the tools in
`allowed_tools`), but the ROOT agent — which an interrupted-then-continued run
tries to deposit from directly — never had them and answered `TOOLS-MISSING`. The
fix threads the plugin's `requires_capabilities` onto its `SessionToolFactory`
(via `load_entry(..., requires_capabilities=)`, fed from `pc.manifest` in the
orchestrator's existing `fan_out.components` loop) and unions an allowlist gate
with a capability gate inside `build_for(session_capabilities=)`. **Rule:** any
new gate that decides "can this agent see X" must read `session_capabilities`,
not just `allowed_tools` — a runtime grant reaches the former, never the latter.

## Hooks

`hooks.py:HookManager` runs lifecycle hooks. Three lifecycle points:
`on_session_start`, `on_session_end`, `on_compact`. Two hook types:
`"command"` (shell subprocess) and `"http"` (fire-and-forget POST).
All invocations are try/excepted — a failing hook logs a warning and
never blocks the session.

`on_event` is a fourth slot fired from `append_event` on BOTH store backends
(the universal choke-point, a superset of the orchestrator's event_logger).
Hooks registered here are **fire-and-forget in a daemon thread even for the
command factory** — unlike `on_session_end` (sync, once) — because they run
on the append hot path. The API wires one bus observer here at startup so the
`SessionEventBus` and command/http hooks share a single publish choke-point.

The wiki finalize tool uses an `on_session_end` hook indirectly: the
completion callback in `channels/routes.py` reads `source_platform`
from a transcript context event and dispatches a reply via the channel
adapter. Don't add session-end behavior inline in tools — put it in a
hook so it survives unrelated tool refactors.

## Session event bus (push SSE)

`session_event_bus.py:SessionEventBus` — per-session in-process pub/sub, fired
from `append_event`. Publish is **non-blocking** off the hot path (bounded
drop-oldest queue). Single-process is correct (gunicorn `--workers 1`); a
`RedisSessionEventBus` subclass is a **documented seam, deliberately NOT built
(YAGNI)**. The API SSE generator subscribes → emits backlog once → drains the
queue, applying content-key dedup against the subscribe↔backlog race and
**draining completely before `stream_end`** so the terminal `completion` event
(published during the close-race window) is never dropped.

## Compaction

`compact.py:run_compaction()` produces a structured `<analysis>` +
`<summary>` block and rebuilds the message list around it. Auto-compact
uses PARTIAL mode by default; manual `/compact` uses FULL. Both modes
share the same prompt template.

Compaction-resilient state lives in places the LLM rebuilds each step
or that survive a message list rewrite:

- Agent tree (`hypervisor.render_agent_tree()`) — rebuilt every step.
- Child results — stored on `AgentHandle.result`, not in the message
  list.
- `AgentHandle.progress_note` — updated each step automatically.

If you add a new piece of orchestration state, ask: "does this survive
a compaction?". If not, store it on the hypervisor or in the session
runtime, not in messages.

## Sync→async bridge

`orchestrator.py:Orchestrator.run_sync` wraps the async loop for
callers that can't `await`. It owns the event loop lifecycle and
guarantees cleanup of background tasks via
`await_lifecycle_managers(timeout)`. Don't introduce a parallel
sync/async bridge — extend this one if you need new behavior.

## Config curation annotations (faceted Settings UI)

`config.py` section models carry presentation/security metadata in their JSON
schema for the console's Settings UI. The non-obvious trap: a submodel field
serializes to a bare `$ref` and Pydantic **drops sibling `json_schema_extra`**,
so facet metadata (`x-group`, `x-order`, `x-advanced`) MUST sit on the section
**class** (`model_config = ConfigDict(json_schema_extra=...)`) where it lands on
`$defs/<Class>` — not on the field, where it silently vanishes. Field-level
flags (`x-secret` write-only, `x-protected` never-exposed, `x-advanced`) go on
the scalar `Field(...)` and survive. Full contract: the comment block above
`AppConfig`. The API's `ConfigSchemaView` reads these; the console's
`SettingsModel` mirrors them.

**`AppConfig` is `extra="ignore"`** — an unknown top-level block in `app.json`
is silently dropped at `model_validate`, and `get_config_value` walks one
`getattr` per dotted key, so a new feature section is physically un-enableable
until it becomes a typed `AppConfig` field whose nesting matches the accessor's
dotted path (e.g. `scg.traversal.default_tier` needs a `traversal` submodel,
not a flat field).

Two corollaries that bit during the Settings UI refinement (#30):
- **The `$ref`-drop trap has an exception: collection fields.** A `dict[str, X]`
  or `list[...]` field is NOT a bare `$ref` — it serializes inline
  (`{type: object, additionalProperties: …}`), so field-level `json_schema_extra`
  **survives** on it. That's how `projects`/`channels` carry their `x-group` from
  the *field* with no wrapper class. Only a single-submodel field needs the
  class-level workaround.
- **`title=` (in the class `json_schema_extra`) humanizes the section/subsection
  heading** and flows live to the console (the FE `prettify` is only a fallback);
  **`deprecated=True` on a `Field`** hides that knob in the console. After
  changing any curation metadata, regenerate the committed
  `configs/app.schema.json` + `docs/configuration.md` via
  `scripts/ci/generate_config_schema.py` (the docs side is an MkDocs
  `on_pre_build` hook in `docs/hooks/schema_to_md.py`).

## Testing

Tests live in `tests/`. Patterns documented in `tests/CLAUDE.md`.
This package's tests focus on:

- Tool-use loop edges (cancellation, plan context, lifecycle-aware
  depth guidance, no-step-count-in-messages).
- Hypervisor CRUD + admission control + 6-state lifecycle.
- Sub-agent spawn with `filter_specs()` tool scoping.
- Compaction modes producing the right post-compact state.
- Hook lifecycle + failure isolation.

Stub at I/O boundaries (`model.ainvoke`, tool execution) — never run a
real model or hit a real network.

## Pre-edit checklist

- [ ] Touching the tool-use loop? Verify all four execution paths
      still work (text response, tool call, plan context, cancellation).
- [ ] Adding a new built-in plugin? Did I register it in
      `tool_registry.AUTO_MANIFEST` AND add a `tests/<plugin>/` test
      module that stubs I/O?
- [ ] Adding a new context event type? Did I update `compaction.py`
      so the event survives compaction (or document that it's
      intentionally ephemeral)?
- [ ] Adding a new lifecycle state? Did I update the 6-state lifecycle
      enum AND the hypervisor's terminal-state handling?
- [ ] Adding a new LLM call site? Did I use `build_chat_model()` from
      `llm.py` instead of constructing a LiteLLM client inline?
