> ↑ [root /CLAUDE.md](../../CLAUDE.md)

# Mewbo Core — Engine Guidance

Scope: `packages/mewbo_core/src/mewbo_core/` — the async tool-use engine,
hypervisor, session runtime, hooks, plugins, and the built-in plugin suite.
This file captures the engineering decisions + orchestration invariants that
aren't obvious from the code. For cross-cutting layering rules see root CLAUDE.md.

**Layering (see root CLAUDE.md → "Monorepo layering"):** core is the lean
base of the dependency DAG — it imports DOWN from nothing and must never
import an app or a capability library; keep heavy/optional deps behind a
`mewbo-core[...]` extra. (An earlier relocation moved the `wiki`/`scg` plugin suites +
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
- **Delegation governance — the laws, not the links.**
  (a) *Contracts must be cashable*: `DelegationContract` (hypervisor.py, beside
  `AgentResult`) only carries fields with a live in-process signal — steps + wall
  enforced, `max_tokens` advisory (usage_metadata unreliable), cost deliberately
  absent. Never add a contract field whose enforcement signal doesn't exist yet.
  (b) *No agent halts into a stump*: every budget/contract exhaustion routes
  through `_budget_wrapup_turn(reason)` — one text-only unbound turn whose output
  lands in `AgentResult.summary` (`done_reason` `budget_exhausted` /
  `halted_agent_budget` → `status="incomplete"`, recoverable). The one contract
  kill is wall-deadline 100% (two-strike: NL warn at 80% precedes it).
  (c) *WriteProgressSignal* (llm_resilience.py, sibling to DoomLoopGuard):
  OBSERVE-ONLY telemetry, never a verdict fed back into the flagged agent —
  progress iff a WRITE-tier tool executed; two-gate arming (capability_mode ∈
  {execute,all} AND a write tool bound) so read-only researchers are never
  flagged. Crossing the threshold ALWAYS emits a `write_progress_signal` event
  (agent_id/depth/step/steps_since_write/threshold); the optional reminder
  (`reminder_enabled`, default OFF) injects ONLY a criterion-blind objective
  restatement (the task goal, nothing about writes/steps/the signal itself) —
  feeding a detector's verdict back into the same agent's context teaches it
  the tell, so that path is closed by construction. Session tools miss
  `specs_map` → count as non-progress by construction.
  (d) *Attestation rides the event pipeline, or nowhere*: `type:"attestation"`
  events through the terminated-gated `append_event` — never a new writer or
  uploader (grok-build exfil lesson). It is tamper-EVIDENCE (in-place edits of
  retained records), not tamper-proof: suffix deletion undetectable, no signing.
  (e) *Workspace containment crosses the package DAG via a contextvar*:
  `WorkspaceContainment` lives in core (`workspace.py`), tools enforces it in
  `resolve_safe_path`; the live object reaches tools through
  `active_containment()` set around registry-tool execution (core cannot import
  tools; JSON args cannot carry objects; propagates into `asyncio.to_thread`).
  Both narrowable axes (`capability_mode`, `workspace_mode`) share ONE
  `_narrow(parent, requested, rank)` on `AgentContext`; `atomic` is a plain OR.
  STAGED: `agent.workspace_enforcement=False` keeps every path byte-identical
  until flipped — the flip is the step that closes the 07-18 escape class.
  (f) *Verifier-gated completion* (`verification.py`, `CommandVerification`):
  a ground-truth command check runs at the NATURAL-COMPLETION seam ONLY (the
  `if not response.tool_calls:` branch, `tool_use_loop.py`) — never on a
  budget-wrapup/halt/session-terminal path, because a halt is not a claimed
  completion. Data-owned like `TriggerSpec` (discriminated union, one total
  `from_value` → `None` = inert, no `if kind ==`; the model picks argv, never
  authors verification LOGIC). The runner subprocess is behind a
  `VerifierRunner` Protocol (`interpret` does NO I/O; argv list, `shell=True`
  never, `_scrubbed_env`); the ONE feedback class injected back is the grounded
  verifier output (a `SystemMessage`, thin factual frame). A failed check
  `continue`s through the top-of-loop budget checks FIRST, so retries are
  bounded by BOTH `verification_max_retries` AND the step/wall budget. Two-gate
  arming (`CommandVerification.gate_active`, the SAME predicate the loop and
  spawn_agent's note share): master switch `verification_enabled` AND
  capability_mode ∈ {execute,all}. `done_reason:"verification_failed"` is
  HONEST — the text is still returned, never masked as `completed`
  (`state.verified` False vs None-never-ran vs True). **That honesty now rides
  up to the session STATUS layer too, not just `done_reason`:** `SessionStatus`
  gains `unmet_goal`/`blocked`, and `_STATUS_BY_DONE_REASON` maps both
  `verification_failed` and `halted_no_progress` → `unmet_goal`, while a
  `blocked_code` on the completion payload (`repo_access`/`network`/`forbidden`/
  `quota_exceeded`) maps straight to `blocked` and outranks any reason-derived
  status. A budget-halted CHILD's parent now sees the same honesty:
  `spawn_agent.py`'s `_UNACHIEVED_CHILD_REASONS` (`budget_exhausted`,
  `halted_agent_budget`, …) makes `_project_terminal_status` report `failed` —
  never `completed` — up the hypervisor's closed 6-state vocabulary.
  A spec present-but-INACTIVE
  for a child (switch off / below-execute) MUST surface
  `"specified_but_inactive (…)"` in the spawn response + event — never the
  invisible-null trap. STAGED OFF by default (mirrors `workspace_enforcement`);
  the bounded `verification` event carries scalars only (NO stdout/stderr on the
  wire — grounded tails go only into model context).
  (g) Config-derived artifacts (`configs/app.schema.json`,
  `docs/configuration.md`) must be regenerated IN-COMMIT on direct pushes —
  docs CI is pull_request-only and will not save you.
  (h) *Promise-as-completion gate (root only)*: the natural-completion seam
  (`if not response.tool_calls:`, `tool_use_loop.py`) now refuses a clean
  terminal while the run still owns LIVE background work —
  `self._ctx.registry.collect_running(agent_id)` (`hypervisor.py`) checks for
  the agent's OWN still-running children; if any are live it injects a
  `loop.agents_still_running` system message and `continue`s instead of
  accepting `done_reason="completed"`. Root-only (`depth == 0`) and bounded
  (3 nudges) so a model that genuinely won't wait for its own spawned work is
  eventually let through rather than nudged forever. Distinct from
  verifier-gated completion (f): this catches an agent PROMISING delegated
  work is done while its own children are still running, not a failed
  ground-truth command.
- **Deferred tool-schema loading (`tool_search`).** MCP / `metadata.deferred`
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
  is read-only so it binds in plan mode, and a discovered MCP tool is admitted
  unconditionally once bound — the plan-mode filter never mode-filters `kind == "mcp"`
  specs at all, since Mewbo cannot classify a third-party MCP tool's effect (MCP visible
  after the first search) — never special-case plan mode in the deferral path.
  - **`tool_search` must search what the BINDER binds, not just the registry.**
    `ToolSearchRunner` filtered only `ToolRegistry.list_specs()`, but `_bind_model`
    binds FOUR populations: registry specs + spawn family + `activate_skill` +
    per-agent SESSION TOOLS. The last three never reach the registry, so they were
    permanently unsearchable ("No deferred tools are registered." — 33 prod
    failures). Fix is at the CALL SITE, never the cached runner instance
    (baking per-agent state in a `ToolRegistryCache`-shared runner leaks scope):
    `_execute_tool_call` intercepts `tool_search` and passes the runner a
    `supplement=` built from `_directly_bound_tool_schemas(plan_mode=…)` — the ONE
    helper `_bind_model` now also uses, so searchable ≡ bound and a strictly-scoped
    agent can never widen via search. `select:` on a supplemented name is a
    harmless no-op (already bound). Supplemented names are NOT in `_deferred_ids`,
    so the per-turn re-bind discovery scanner ignores them (correct — already
    bound).
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
- **SessionTools bypass the stateless `ToolRegistry`, so their result cap must
  be DECLARED, not inherited.** `ToolUseLoop._result_char_cap(tool_id)` resolves
  a per-tool character cap; `ToolSpec.max_result_chars` (default 2000) sizes
  registry tools wrapping unbounded shell/MCP output, but a SessionTool has no
  `ToolSpec` at all, so it used to fall through to that SAME 2000-char registry
  default — starving a real repo manifest (`wiki_read_page` truncated on
  effectively every call; the indexer's `find`/`ls` "distraction" turned out to
  be the model REPAIRING this contract turn after turn). SessionTools now
  declare `max_result_chars` as a class attribute read via `getattr`, falling
  back to `DEFAULT_SESSION_TOOL_MAX_RESULT_CHARS` (200,000) only when absent.
  Size a new SessionTool's cap from a realistic payload, not taste — a
  truncated result reads to the model as a broken tool, and it will spend
  whole steps working around the truncation rather than trusting the read.
- `hypervisor.py:AgentHypervisor` is the only place sub-agents register,
  cancel, get budget warnings, and resolve to their `AgentResult`.
- `spawn_agent.py` is the bridge between the LLM-facing tool schema
  and the hypervisor. Schema fields stay backwards-compatible —
  removing a field breaks every running agent that already learned
  it. **`spawn_agent`/`spawn_agents` are injected via the loop's own
  `_spawn_agent_tool`, NOT through `filter_specs`, so they must re-check
  scope themselves** — the loop gates creation on `can_spawn` (depth) AND
  the agent's allowlist (`tool_use_loop.py:264`): an explicit `tools:` list
  that omits spawn_agent disables delegation, an ABSENT one (`None`) stays
  unrestricted. **`allowed_tools` is THREE-STATE and every test on it is
  `is None`, never truthiness** — `None` unrestricted, `[]` grants NOTHING,
  non-empty grants exactly those. Truthiness collapses `[]` into `None`, i.e.
  "this principal gets no tools" into "it gets every tool", which is fail-open
  on the gate that binds an agent's whole tool surface. It bites hardest here:
  a role-bounded viewer's composed allowlist omits the spawn family precisely
  to disable delegation and can compose down to empty, so reading empty as
  unrestricted hands delegation back to the principal the ceiling exists to
  deny. The law spans the parse sites (an AgentDef `tools: []` / skill
  `allowed-tools: []` must persist as `[]`), the spawn args (model-supplied —
  and the tool schema tells the model "omit for all tools; an empty list grants
  none", so code and schema move together), and `filter_specs` itself.
  `denied_tools` is deliberately NOT three-state: deny is purely subtractive,
  so an empty denylist and no denylist are the same set.
  Without this a leaf agent (e.g. `st-widget-builder`, allowlist = build+submit
  only) could still delegate and recurse into copies of itself. RULE: any tool
  injected outside `filter_specs` escapes the allowlist/denylist and must
  enforce scope at its own injection seam. The `sub_agent` lifecycle event's `detail` is only the
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
- **Batch fan-out (`spawn_agents`).** `spawn_agent` admits one task
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
  AND identical result across N turns, fed by `record_result`). **Exemption is
  DECLARATION-DRIVEN and PER-CALL, not a hardcoded name set.** `DoomLoopGuard.
  poll_rules` merges a small built-in seed (`DOOM_LOOP_EXEMPT_TOOLS`, today just
  `check_agents`) with tool-declared rules read via `getattr`: a registry
  `ToolSpec.poll`/`poll_when_args`, or a SessionTool's `poll_class`/
  `poll_when_args` convention (deliberately not a Protocol member — read
  defensively). `ToolUseLoop._poll_class_rules` collects these off every bound
  registry spec and session tool each run and feeds them to `DoomLoopGuard.
  from_config(extra_poll_rules=...)`, so the guard evaluates PER CALL against
  that call's actual arguments — e.g. `mewbo_graph`'s `agentic_search` tool
  declares `poll_when_args=("run_id",)`, so `agentic_search(run_id=…)` (polling
  an existing run) is exempt while `agentic_search(query=…)` (a fresh search)
  still counts toward progress. A third poll-class tool costs a declaration on
  its own spec, never a core edit. `tool_use_loop` is
  a thin driver — don't reinline retries. See the root CLAUDE.md "LLM call
  resilience" invariant for the decision model + append-after-success idempotency.
  **Policy:** a model gets **2 attempts** (`agent.llm_call_retries` =
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
- **Delegation-layer bounded retry complements the model-fallback ladder:** the
  ladder heals *within* one `ToolUseLoop` call; bounded retry re-delegates a
  whole child whose loop *died*. Opt-in `retry: {max, on, backoff}` on the spawn
  schema, **DEFAULT OFF** (`max == 0` ⇒ byte-identical single-attempt path).
  `spawn_agent.py:RetryPolicy` (atomic, validated via `from_value`, never raises)
  + `SpawnAgentTool._drive_with_retry` wrap the existing admit→run→resolve, shared
  by BOTH spawn paths (blocking non-root *and* the non-blocking root lifecycle
  manager, which now creates the child task internally rather than receiving it).
  Key invariants: (1) **reuse, don't reinvent escalation** — each attempt builds a
  FRESH `ToolUseLoop` (`_build_child_loop`) so model-level recovery rides the
  fresh fallback ladder; `RetryPolicy.classify_cause` reuses `RetryStrategy.classify`'s
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
  `agentic_search:` → search, channel `:room:`/`:thread:` → channel,
  `mobile:` → mobile, `app:` → apps) and, as fallback, the first context event's
  `client_capabilities`/`source_platform`/`client` (a mobile surface —
  `android`/`ios`/`aura-*`, via `is_mobile_surface` — → `MOBILE`, checked
  BEFORE the generic `source_platform` → CHANNEL fallback so a mobile client
  never reads as a channel; this retroactively reclassifies existing Aura
  sessions, which already persist `context.client == "aura-android"`, without a
  migration). Tags win (they survive even when a job stored empty context).
  `summarize_session` attaches it as `origin`; the console badges + filters the
  landing page on it. `SessionStore.tags_for_session` is the concrete reverse of
  `resolve_tag` (one impl over `list_tags`, shared by every backend).
- **Universal recovery.** Any non-complete session is recoverable —
  `summarize_session.recoverable` is true when a run ended not-successfully (incl.
  killed mid-call with NO completion event), **with ONE carve-out: a permanently
  terminated session forces `recoverable=False` and beats even a live run
  — see "Reverse-invocation triggers + hard termination" below.** `resolve_recovery_query` yields the
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
    `completion`.** Its only job is to drop a STALE PRIOR continue attempt
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

## Bounded failure emission (`run_error.py`)

`RunError` is THE seam for classifying and bounding a run failure. One atomic
Pydantic model, `extra="forbid"`, with the classification table, the markup guard
and every length clamp as members validated AT DEFINITION — so no call site can
construct an unbounded or markup-bearing value, not even a direct `RunError(...)`
or a `model_validate` of a stored payload. It imports no I/O: the exception and
the model name arrive as ARGS, which is what lets a test drive every path with no
provider, clock or network.

The problem it closes: a run that died inside the LLM call chain surfaced as one
flat, UNCAPPED string. A live session emitted 5,887 characters there because
LiteLLM embedded an entire upstream HTML error page into the exception message,
which then rode the payload into every client and into the next run's
`recent_events`.

- **THE LAW: `kind` and `title` derive from the exception TYPE and the
  error-class NAME, NEVER from the response body.** Every message read funnels
  through `_body_free_head`, which cuts at the first `<` — that is what makes the
  rule STRUCTURAL rather than conventional, since an error-class name can never
  contain `<`. An HTML body additionally forces a SYNTHESIZED title, with the
  status taken from the derived `kind`. The reason is not tidiness: an upstream
  error page's own `<title>` routinely names the operator's internal
  infrastructure, and these payloads are persisted and replayed to every client
  on every history read, so lifting it would republish that indefinitely.
- **An empty `str(exc)` is a distinct edge case this seam does NOT cover.**
  Several exception classes stringify to `""` (`TimeoutError` is the one that
  costs the most forensic effort — a bare `str(exc)` here is invisible in a log
  line). `RunError.from_exception` has no guard for it: `message=str(exc)` with
  nothing to catch the empty case, so `title` falls to the generic
  `_FALLBACK_TITLE` literal rather than naming the exception type. `kind`
  classification survives regardless — `_chain_class_names` walks
  `type(exc).__name__` directly, never the message — so misclassification isn't
  the risk; an uninformatively generic title is. The type-name-on-empty fix DOES
  exist, just one layer up: `llm_resilience.py:LlmResilienceExhausted.
  describe_error` (and its Langfuse twin, `components.py:_span_status_message`)
  substitute the exception's TYPE NAME when the message is empty, before the
  string ever reaches `RunError`. Copy that pattern for a new error surface
  rather than assuming `RunError` already covers it.
- **Four emission paths must ALL stay bounded — a payload-only cap is not
  enough.** (1) `completion.error`/`last_error`. (2) `error_msg` forwarded to
  `on_session_end` hooks, previously captured as `str(exc)` BEFORE
  classification: its consumers post it as a forge PR comment, as a chat message,
  and persist it on a pipeline run, so it is OUTWARD-facing and must never carry
  provider markup. (3) `task_queue.last_error` on the STICKY branch
  (`halted_no_progress`/`max_steps_reached`/`verification_failed`), not only the
  raising one — the CLI and the scg map-job read that ATTRIBUTE directly rather
  than the wire payload, so capping the payload alone left both unbounded.
  (4) Sub-agent failures, bounded at the child in BOTH `_spawn_one` and
  `_run_child_lifecycle`, so a raw provider exception cannot ride into the
  parent's context.
- **Three projections, three jobs — pick by consumer.** `title` is a one-line,
  markup-free label for a card header, and is also the attestation `done_reason`
  (a short-label cap that a raw `str(exc)[:200]` slice could have filled with the
  head of an HTML page). `brief()` is the bounded blurb written to the flat
  `error`/`last_error` keys that the non-console clients render. `detail` is the
  full 10K-capped diagnostic behind an expandable view. `brief()` degrades to
  `title` ONLY for an HTML body — that carve-out is what keeps an informative
  failure informative: a rate limit still reads as one, while the 5,887-character
  page becomes 42 characters.
- **`error_detail` is ADDITIVE.** It rides alongside the flat keys rather than
  replacing them, so a consumer that only knows `error`/`last_error` is
  unaffected. Aura's decoder is `ignoreUnknownKeys = true` at every decode site
  (`di/DataModule.provideJson`), so a new payload key is safe there — verified,
  recorded here so nobody re-audits it.

## Reverse-invocation triggers + hard termination

Two absorbing-state subsystems land together: durable **triggers** (`mewbo_core/
triggers/` — wake a session later) and **hard termination** (kill a session for
good, in the session runtime + both stores). Both mirror the hypervisor's
absorbing-state ethos.

- **Triggers are the hypervisor's DURABLE peer.** The hypervisor is the ephemeral
  intra-session governor (one run, resolves children to an absorbing
  `AgentStatus`); a `TriggerSpec` is the cross-restart wake contract (survives
  process death in a store, re-engages a session later) — it is to
  reverse-invocations what `RunRegistry` is to runs. **Trigger behaviour is
  DECLARED via typed kind classes, NEVER an LLM-authored script.** The agent
  picks a `kind` + fills its fields; it never authors scheduling/matching logic.
  That guardrail *is* the subsystem — agent self-scheduling via model-written code
  is precisely the thing we refuse to ship — so never add a "custom"/eval kind.
- **Data-owned kind classes, ZERO `if kind ==` dispatch (`spec.py`).** Each
  subclass (`TimeAtTrigger`/`CronTrigger`/`CiWorkflowTrigger`/`ForgePrTrigger`/
  `WebhookTrigger`) owns its own validators + due-ness/match method
  (`next_fire_at`/`matches`/`verify`); the base `next_fire_at` returns `None`
  (event kinds never poll the clock). A `Field(discriminator="kind")` union +
  `parse_trigger()` is the ONE parse seam — any service-side `if kind ==` would
  drift out of sync the moment a kind gains a field, so there is none, anywhere.
  **Models never import I/O:** the current time, a webhook's headers/body, a
  normalized CI/PR payload all arrive as method ARGS (also why tests inject a
  fixed `NOW` instead of patching a clock). **`fired` is a transition, not a
  status:** two resting statuses (`armed`⇄`paused`, freely interchangeable) and
  four absorbing terminals (`completed`/`failed`/`cancelled`/`expired`);
  `transition()` guards illegal moves and terminals absorb; `is_due` polls
  positive only for `armed`.
- **`record_fire` never auto-fails.** An errored fire bumps `fires` + records
  `last_error` but leaves the trigger `armed` — retry/backoff is the SERVICE
  layer's call (the app-side watcher reads `last_error` to re-arm/cancel/leave),
  by design, never the model's. It auto-completes only when `fires` reaches
  `max_fires`. **Cron missed-fire is anchored, not herd-prone:**
  `CronTrigger.next_fire_at` anchors on `last_fired_at`/`created_at` (NOT `now`),
  so after downtime a due trigger fires exactly ONCE and re-anchors on that fire
  — no thundering-herd catch-up of every missed slot.
- **`TriggerPolicy` (`policy.py`) is the ONE home of admission ceilings** —
  `max_armed_per_session`, `max_fires_cap`, `default_expiry`,
  `cron_min_interval_seconds`, `webhook_payload_max_bytes`, every one
  config-tunable per deployment, NEVER hardcoded in behaviour code. `admit()`
  validates AND stamps defaults (currently `expires_at`) in one step, and **never
  mutates** the spec — a stamped default returns a `model_copy`. Wave-2 builds the
  instance from `AppConfig` and DI-hands it to the tool; this module owns no
  config I/O and no store access.
- **`schedule_trigger` SessionTool (`session_tool.py`)** is the agent's
  arm/list/cancel surface — **terminal-free like `update_todos`**
  (`should_terminate_run()` always `False`; arming is a normal step, not a
  plan-mode exit). Built through the ordinary `SessionToolRegistry` plugin path
  (constructor gets `session_id` + `event_logger`), so `agent_id` is an OPTIONAL
  kwarg (parity with `UpdateTodosTool`) — inline-attach to get live agent
  attribution on the `trigger_armed` event. Per-kind validation is DELIBERATELY
  not duplicated in `ScheduleTriggerArgs` (it holds only the per-operation "arm
  needs kind+wake_prompt" guard) — kind requirements belong to `parse_trigger`/
  the concrete subclass alone. Errors return the shared `{"error":{code,message}}`
  envelope (codes `validation`/`policy`/`not_found`) that
  `_session_tool_error_envelope` reclassifies as a failed step; a foreign-session
  `trigger_id` reads `not_found` uniformly (no existence leak). `provenance.step`
  is ALWAYS `None` today — no per-step turn counter exists anywhere in the engine
  (`AgentContext` carries none); recorded here so nobody hunts for one.
  - **Delivery rides the ordinary `SessionToolRegistry`, NOT `extra_session_tools`.**
    The old root-only `extra_session_tools` injection was structurally
    unreachable by a SPAWNED sub-agent (`SpawnAgentTool._build_child_loop` forwards
    only `session_tool_registry=`), so the app-builder AgentDef listed
    `schedule_trigger` in its `tools:` yet never got it. Now the app pushes its
    store+policy ONCE via `session_tool.py:register_schedule_trigger_provider`
    (down-only, mirrors `register_app_submitter`/`register_builtin_root`; gated on
    `triggers.enabled`), and every `Orchestrator` registers the resulting
    `SessionToolFactory(unconditional=True)`. **`unconditional` is a THIRD gate on
    `SessionToolFactory`** beside `requires_capabilities`, and its ceiling is
    **`strict_tool_scope`, NOT bare `has_explicit_scope` — this is the df875 law
    (0bc72cef → df175870), and getting it wrong reships that exact regression.**
    A `SessionToolRegistry.build_for`/`ids_for` call now takes `strict_tool_scope`;
    an unconditional tool is capped by a non-empty allowlist ONLY when the scope is
    STRICT (an authoritative AgentDef `tools:`). Under a PERMISSIVE scope — the FE
    default, where `context.mcp_tools` (139 entries on a real console/Aura session)
    is a ceiling over MCP tools ONLY and never lists built-ins — the unconditional
    tool STILL surfaces. Missing this silently broke every mobile alarm/reminder
    flow (Aura "set an alarm in 10 min" arms a time trigger through
    `schedule_trigger` on a permissive root). So: app-builder AgentDef (strict,
    names it) ✓, differently-scoped strict child ✗, permissive FE root (mcp_tools,
    no schedule_trigger) ✓, plain root ✓. The capability gate (2) keeps the plain
    plain `has_explicit_scope` ceiling — the df875 relaxation is unconditional-only.
    **Both `build_for` (the loop) AND `ids_for` (the operator's `{{ tools }}`
    catalog via `_resolve_instruction_tools`) must be passed the SAME
    `strict_tool_scope`, or the catalog drifts from what the agent holds.**
    ASK-USER stays on `extra_session_tools` (root-only is its documented intent).
    **Test trap:** `configs/app.json` enables triggers, so importing `backend.py`
    (any apps test, any random order) sets that process global permanently →
    `schedule_trigger` then leaks into EVERY permissive/un-scoped core
    `Orchestrator` session's tools. An autouse fixture in `tests/conftest.py`
    (`_reset_schedule_trigger_provider`) resets `_TRIGGER_TOOL_PROVIDER` per test
    so exact-tool-set assertions stay hermetic; a test that WANTS it registers it
    in its own body.
- **Hard termination is a KILL SWITCH, not another recoverable state.**
  `terminate_session` stamps `terminated_at` set-once — the `terminated_at: None`
  filter clause on Mongo, `setdefault` on the json index — exactly mirroring
  `archived_at`, and there is deliberately **NO un-terminate primitive**.
  `summarize_session` derives `status="terminated"` which beats EVERYTHING,
  including a still-unwinding live run (checked AFTER the `running` override,
  since cancellation is cooperative and `is_running` briefly lags a terminate),
  and forces `recoverable=False`. `SessionTerminatedError` (HTTP surfaces map it
  to 410 Gone) is raised at the `resolve_session` core seam so a terminated
  transcript can't be FORKED — copying it into a fresh session would let an agent
  launder its way around the kill. The `_on_terminate` callback list is the
  cascade seam (best-effort, summed int returns): the API registers the trigger
  store's `cancel_for_session` on it so a dead session's armed triggers can't fire
  into it. **Terminated ≠ deleted:** reads stay open; only run/steer/recover/fork
  are blocked.

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
  (`user`/`wiki`/`search`/`channel`/`apps`/`structured`/`draft`/`mobile`);
  `product` refines it (adds `vcs`, which has no coarse origin). **Adding an
  origin is a three-site lockstep:** the `SessionOrigin` member + its `classify`
  tag-prefix, the `_ORIGIN_PRODUCT` map, and (if the tag carries sub-kind/ids) a
  `_facets_from_tags` arm — plus the console mirror (`types.ts` union +
  `utils/sessionOrigins.ts` `ORIGIN_META`, which is exhaustive and from which the
  filter list and the badge both derive). **A tag PREFIX that an app-side seam also
  stamps belongs to core, not the app** — `MOBILE_TAG_PREFIX` and `APPS_TAG_PREFIX`
  are declared here and imported by their stamp sites precisely so the classifier
  and the writer can never drift; a duplicated literal is how `app:<app_id>` came
  to be stamped for the whole life of the Apps sub-product while `classify` had no
  arm for it, silently filing every builder and maintainer session under `user`.
  Tag a session at creation (the robust signal) — context fallback only catches
  un-tagged paths. Because origin is DERIVED at `summarize_session` time and never
  stored, teaching the classifier a prefix reclassifies existing sessions with no
  migration. The
  structured-family surfaces use `structured:run` (agentic `/v1/structured`) /
  `structured:fast` (its `mode:"synthesis"` no-loop lane — formerly the
  separate `/v1/structured/fast`) / `draft:stream` (`/v1/draft/stream`): the
  tag's 2nd segment becomes `session_type` so the three structured-family
  variants stay individually filterable under one `structured`/`draft` product.
  Search has three distinct `session_type`s under product `search`: a RUN
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
- **`transcript_sink` is the CLI local-vs-synced facet.** A `transcript_sink`
  context key (`synced` when the local-first CLI mirrors to a remote API, else
  absent ⇒ local) is promoted by `_facets_from_context` to a low-card
  `transcript_sink:` chip — SEPARATE from `surface` (which stays `cli`). The CLI
  writes the context event; do NOT confuse it with a `source_platform` event
  (`SessionOrigin.classify` reads `source_platform` as CHANNEL).
- **Runtime-granted capabilities must be OVERLAID into `derive`'s context.**
  `derive` reads `client_capabilities` from the merged context — i.e. only the
  CLIENT-ADVERTISED set. A capability granted at runtime by a provider is live in
  the run yet absent from context, so it would vanish from the trace's
  `capabilities` facet. `Orchestrator.run` therefore overlays the AUGMENTED set
  (`_session_capabilities`, advertised ∪ provider grants) into the context dict it
  hands `derive` — keeping `derive` a pure `(tags, context, surface)` transform
  while making any such grant filterable. (This overlay is a no-op passthrough
  today: the only provider was an earlier `scg` grant, since removed, so no
  capability is currently runtime-granted — the seam stays for a future one. It
  also matters because of that removal: the overlay is exactly how the blanket `scg` grant
  leaked into the trace and mis-tagged bare sessions `origin:search`.) The corollary on the
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
import the engine down instead of up into an app. A library
registers its plugin root with `plugins.register_builtin_root`;
`load_all_plugin_components()` discovers core's own root plus every registered
one. Their subsystem docs live with the engine — see
`packages/mewbo_graph/CLAUDE.md` and the wiki/scg docs it points to.

**`submit_widget` is TERMINAL-FREE (like `update_todos`) — keep it that way.**
The widget renders off the `widget_ready` event, not off run termination;
forcing termination crashed every submit (`terminal_reason` AttributeError
*after* the widget was built, Aura trace `e58f7cf7…` → 4 identical rebuilds,
86% wasted calls) and robbed the agent of its closing message. **The trap that
caused it:** `SessionTool` is a structural `Protocol` — its default method
bodies are NOT inherited by standalone implementers (only real subclasses,
e.g. `WikiSessionTool`'s children, get them). RULE: a session tool that CAN
terminate must define `terminal_reason()` on its own class; a tool whose
side-effect is an event should not terminate at all.

`widget_builder`'s `widget_ready` event payload is typed by `WidgetReadyPayload`
(`submit_widget.py`, `extra="forbid"`) **in the plugin module itself**, not as a
new arm on `mewbo_core.types.EventPayload` — that union stays generic and the
event still rides its `dict[str, JsonValue]` catch-all on the wire; the plugin
just stops hand-assembling that dict unchecked. The snake_case wire shape is
FROZEN because a console TS type and an Android Kotlin type both mirror it.

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

**Token usage is a NORMALIZATION concern too.** litellm can strand the
real token counts in `response._hidden_params['usage']` instead of
`response.usage` — streaming chunks have usage stripped and re-attached only to
`_hidden_params`, and some proxy/non-streaming shapes never set the field — so
`langchain-litellm` builds an empty `usage_metadata` and every `llm_call_end`
(hence `/usage`) reads zero. `build_chat_model` wraps the litellm client in
`_UsageNormalizingLiteLLM`, which surfaces `_hidden_params['usage']` back onto
`.usage` for both paths. Feature-detecting (acts only when usage is
absent/all-zero), so it self-disables once upstream (litellm#12233 / #17476)
lands — remove it then. (At the pinned stack, langchain-core also disables
streaming when `streaming` is in `model_fields_set`, so `bound.astream()`
actually falls back to `ainvoke`; the wrapper covers both regardless.)

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

Every engine prompt has ONE schema'd home. `PromptRegistry` (atomic
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
  prompt:** the loop's per-step sites PLUS compact (`get_compact_prompt(model=)`),
  title, and the structured runners all pass `model=`.
- **Sticky model escalation re-variants the prompt + tool.** `tool_use_loop` tracks
  `self._active_model` (configured primary, or the sticky-pinned escalated model
  after a fallback). Every per-step render + `_configured_edit_tool_id` reads it;
  `_apply_model_escalation` (called each turn) re-renders `messages[0]` and
  re-derives the edit-tool variant against the escalated model when it changes —
  so the heal is behavioural, not just a model swap. `_bind_model` + the resilience
  reuse-guard key off `_active_model` too.
- **Per-model TOOL variants are controllable DATA.** `prompts/model_variants.yaml`
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

## Custom system instructions (operator-authored, `system_instructions/`)

An operator authors ONE Jinja template in the console (Settings → Agent),
stored as a singleton document (`system_instructions/spec.py`/`store.py`/
`store_mongo.py`), that branches per client (`{% if surface == "android" %}`)
and is appended to every session's system prompt.

- **The seam.** `ToolUseLoop._render_system_prompt` (`tool_use_loop.py:1087`)
  gets ONE new section, `loop.section.user_instructions`, rendered right after
  `project_instructions`. The pre-rendered text lives on the loop INSTANCE
  (`self._user_instructions`, set once at construction from
  `Orchestrator._resolve_user_instructions`), so it: (a) survives the model
  escalation re-render for free — `_apply_model_escalation` calls
  `_render_system_prompt` again against the SAME instance state, no
  re-resolution needed; (b) is inherited by every child exactly like
  `project_instructions` — `spawn_agent.SpawnAgentTool._build_child_loop`
  forwards `user_instructions=self._user_instructions` verbatim when it builds
  a fresh child `ToolUseLoop`; and (c) survives compaction for free —
  `_compact_messages` slices `to_summarize = messages[1:-recent_keep]`, never
  touching `messages[0]` (the system prompt), so this feature needed zero
  compaction-path changes.

- **THE TRAP that shaped the design: `skill_instructions` is an overloaded,
  last-write-wins slot with FIVE writers** — user `/skill` invocation
  (`Orchestrator._try_skill_invocation`), `ChannelAdapter.system_context`
  (`channels/routes.py`), the wiki indexer/page-writer/QA jobs (`wiki/jobs.py`),
  the scg `orchestrated_runner`/`map_job` playbooks, and `StructuredResponder`.
  It LOOKS like the obvious home for any new system-prompt text, and it is NOT:
  a channel or wiki drive would silently clobber the operator's instructions
  the moment it ran. **Rule, not just a fact about this feature: any new
  system-prompt contributor gets its OWN `loop.section.*` slot** — never
  overload `skill_instructions` with a second concern.

- **The SSTI boundary, and why it's load-bearing.** `PromptRegistry._env`
  (every engine prompt) is non-sandboxed, `autoescape=False`,
  `StrictUndefined`. Operator text never touches it: it is rendered ONCE,
  upstream, in a SEPARATE `INSTRUCTION_SANDBOX` (`system_instructions/spec.py`
  — `SandboxedEnvironment`, `loader=None`, `ChainableUndefined`), and only its
  OUTPUT reaches the registry, as a plain Jinja VARIABLE
  (`loop.section.user_instructions`: `{{ user_instructions }}`). Jinja
  substitutes a variable, it never re-renders its contents, so operator text
  containing `{{ ... }}` cannot execute a second time against the
  non-sandboxed registry env. **Never concatenate operator/user text into a
  template SOURCE** — the moment it's spliced into a string that gets
  `from_string()`'d, this boundary is gone. `loader=None` is arguably more
  load-bearing than the sandbox itself (which only restricts attribute
  access): it makes `{% include %}`/`{% import %}`/`{% extends %}` fail closed
  with no filesystem to pull from, instead of merely restricted-but-reachable.

- **Why the undefined policy is INVERTED vs the registry.** The registry is
  `StrictUndefined` on purpose — a typo'd engine prompt must fail CI, not
  ship. `INSTRUCTION_SANDBOX` is `ChainableUndefined` on purpose — an
  operator's template referencing a field that got renamed or removed must
  render BLANK, not raise, because `SystemInstructionsDoc.render()`'s own call
  site (`Orchestrator._resolve_user_instructions`) does not wrap it in
  try/except, and this render sits upstream of the first LLM call: a raise
  here would kill the turn before the model ever ran. Fail-soft is the whole
  design: any failure ⇒ no injection + a persisted `last_error`
  (`store.record_error`), never an exception into the run.

- **Validation-at-definition has a deliberate EXCEPTION here.** Template SIZE
  validates at definition (`SystemInstructionsDoc._template_within_cap`, a
  `field_validator`) — an immutable fact about the data, consistent with the
  house rule. Template COMPILATION does NOT get the same treatment: it stays
  `validate_template()`, an explicit method the WRITE path calls. Promoting it
  to a `field_validator` would make an already-stored template that no longer
  parses UNLOADABLE — the `GET` whose entire job is showing an operator their
  broken template so they can fix it would 500 instead. Read this as a
  genuine, intentional exception to "validate at definition," not an
  oversight.

- **`RenderedInstructions` is a frozen dataclass, not Pydantic** — it never
  crosses a trust boundary (in-process return value only), the documented
  carve-out from "the Pydantic rule stops at the process boundary" above.
  Returning the error ALONGSIDE the text is what keeps
  `SystemInstructionsDoc.render()` free of hidden mutation: the caller that
  owns the store (`Orchestrator._resolve_user_instructions`) persists
  `last_error`, not the model rendering itself.

- **Store lives in `mewbo_core`, not the app** (mirrors triggers):
  `system_instructions/{spec,store,store_mongo}.py` are all core. Reason: the
  CLI runs `ToolUseLoop` in-process without `mewbo_api` — a store in the app
  would mean CLI sessions silently got no operator instructions at all.

- **`InstructionContext` is a WIRE CONTRACT, not an internal DTO.**
  `InstructionContext.describe(catalog)` walks `model_json_schema()` to GENERATE
  the variable reference the console renders at
  `GET /api/system-instructions/variables`, so every `Field(description=...)`
  is user-facing documentation, not an internal comment. Adding a field is
  additive (an unrecognized name renders blank under `ChainableUndefined`);
  removing or renaming one silently breaks every operator's template.
  `describe()` lives on the MODEL, not on the Flask controller that used to host
  it: the schema-walking helpers there were an HTTP adapter re-deriving CORE's
  own schema semantics, which drifts the moment a field changes shape. The
  `$ref`/`allOf`/`anyOf` resolution they carried is still load-bearing and moved
  WITH them — an enum field does not serialize inline the way a `str`/`bool`
  field does, so a naive `prop["type"]` read of `origin` yields nothing at all.
  `origin` is typed as the real `SessionOrigin` enum, not a bare `str` with a
  hand-written prose list of values, so the origin taxonomy keeps its ONE
  home — see "Trace provenance" above, "adding an origin is a three-site
  lockstep": a prose list here would have been a silent fourth site. Because
  Jinja needs primitive data, not an enum member, `to_render_vars()` goes
  through `model_dump(mode="json")` rather than a bare `model_dump()` — json
  mode is what turns `SessionOrigin.WIKI` into the string `"wiki"` before it
  reaches the sandbox; a bare `model_dump()` would hand the enum member itself
  to `{{ origin }}` and print `SessionOrigin.WIKI` into every operator's
  prompt.

- **The reference documents candidate VALUES, and `closed` vs `known` is the
  load-bearing distinction** (`system_instructions/values.py`). A bare type word
  ("string", "array") tells an operator nothing they can branch on — they have to
  guess whether the surface is `android` or `aura-android`, whether the capability
  is `wiki` or `wiki_search`, and a wrong guess fails SILENTLY (`ChainableUndefined`
  renders a bad comparison blank rather than raising). So each variable carries
  `CandidateValues`. **`CLOSED` is earned ONLY by a real enum** (`origin`, whose
  members `describe()` reads out of the schema's `$defs`): a session's value is
  ALWAYS one of them, so an operator's `else` branch is genuinely unreachable.
  Everything else (`tools`/`capabilities`/`projects`/`models`/`surfaces`/
  `platforms`) is `KNOWN` — a superset claim about THIS DEPLOYMENT, never an
  exhaustive claim about a SESSION, whose set is narrower (scoped by project /
  plugins / allowlist; capabilities are only what its client advertised) and whose
  surface a brand-new client can invent. Conflating the two is what would make the
  reference LIE, so an operator must TEST for a value rather than assume the list
  is what they get. Corollary and central invariant: **the catalog must be a
  SUPERSET of what any session reports** — under-report and the documented list
  lies about the `{{ tools }}` membership test an operator writes against it,
  which is why the tools probe deliberately includes DISABLED registry specs (a
  tool a deployment *could* turn on). An empty source yields `None`, never `()`:
  "known here: (nothing)" reads as *this variable is always empty* when it means
  *this deployment did not tell us*.

- **`InstructionValueCatalog` is PLAIN DATA with ZERO I/O — the app INJECTS it.**
  Core never reads a registry, a config file or the LLM proxy to describe itself;
  the same rule that keeps `TriggerSpec` taking the clock as a method ARG (a model
  that reaches out for its own inputs is untestable and drags I/O into the wrong
  layer). It defaults only the two vocabularies core genuinely owns
  (`KNOWN_SURFACES`, `KNOWN_PLATFORMS`); the deployment-specific sources are
  resolved at the app edge by `InstructionValueSources` (see
  `apps/mewbo_api/CLAUDE.md`). **Each field DECLARES ITS OWN SOURCE inline** via
  `Field(json_schema_extra={"x-values": "tools"})` — there is deliberately NO
  second name→source lookup table, because a table drifts the moment a field is
  added or renamed. An unknown/typo'd `x-values` degrades to a plain row rather
  than raising into the operator's settings page.

- **`InstructionContext.tools` WAS LYING; the cure is `SessionToolRegistry.ids_for`.**
  It carried `ToolRegistry` specs ONLY, so every session tool the agent genuinely
  held (`wiki_*`, `scg_*`, `submit_widget`, `schedule_trigger`) was absent and
  `'wiki_search' in tools` silently rendered False — a template branching on a
  product tool could never fire. It is now registry specs ∪ session tools ∪
  `extra_session_tools`, and the session half resolves through the NEW `ids_for()`,
  which is the SAME selection `build_for()` makes (both gates now live in `ids_for`;
  `build_for` calls it and then instantiates). That shared algorithm is the point:
  a re-implemented copy of the selection would inevitably drift from what the agent
  actually gets. **Still deliberately omitted:** the internals the loop injects for
  ITSELF (`spawn_agent`/`spawn_agents`, `update_todos`, `exit_plan_mode`,
  `activate_skill`) — they are decided INSIDE `ToolUseLoop`, downstream of this
  resolution, and several are mode-dependent (`update_todos` is act-mode,
  `exit_plan_mode` is plan-mode), so naming them would trade one lie for another.
  `tool_search` is NOT one of them and IS included: despite being loop machinery it
  is a genuine `always_load` registry spec, so it arrives through `tool_specs` like
  any other tool, and listing it as omitted would have been the exact failure this
  fix exists to remove, one level down. The field's `description` states the
  omission outright — **honesty over completeness**; if the contents ever change,
  that line changes in the same commit.

- **Three traps the value catalog walked into. Do NOT "clean them up".**
  (1) **`home-assistant` IS a live surface.** A grep sweep missed it and nearly got
  it deleted from the vocabulary as "a lie in a docstring" —
  `apps/mewbo_ha_conversation/api.py:146` really does stamp
  `X-Mewbo-Surface: home-assistant`. `KNOWN_SURFACES` now sits beside
  `MOBILE_SURFACES` in `session_provenance.py`: ONE home for the surface
  vocabulary, the same argument as the origin taxonomy's one home, and every entry
  traces to a real stamp site. It is a VOCABULARY, not a validator — `surface` stays
  a plain `str` so a new client that stamps its own string keeps working.
  (2) **There is NO enum of capability ids anywhere.** A capability is only ever a
  string two sides agree on, so the only truth is the union of
  `requires-capabilities` across plugin manifests and AgentDefs — which is why the
  app COMPUTES it instead of hardcoding it, and why a plugin shipping a new
  capability appears in the operator's list the moment it is installed. It resolves
  to `scg`/`stlite`/`wiki` — but ONLY once `mewbo_graph` has been imported, since
  the wiki/scg plugin roots arrive via the down-only `register_builtin_root` push.
  A lean install legitimately reports just `stlite` (core's own `widget_builder`).
  (3) **`project` is `None` for EVERY managed-worktree session**, not only for an
  unscoped one: `TraceProvenance._facets_from_context` routes a `managed:<uuid>`
  value to the `worktree` facet and never into `metadata["project"]`. The field's
  description now says so.

- **Prompt-cache interaction.** `llm.py` sets ONE explicit `cache_control`
  breakpoint at the system message (`cache_control_injection_points`, role
  `system`, `type: ephemeral`) whenever the model supports prompt caching — so
  the WHOLE rendered system prompt is a single cached block, not a chain of
  smaller ones. `user_instructions` sits inside that block at a fixed
  position. A template that renders differently per session — branching on
  volatile facts like `session_id`/`agent_id` rather than session-invariant
  ones like `surface`/`capabilities`/`platform` — means no two sessions ever
  share that cached prefix, forfeiting the cross-session cache hit a provider
  would otherwise serve on a brand-new session's first call whenever the
  surrounding system-prompt text already matches. Keep operator templates
  branching on session-invariant facts.
  **`{{ tools }}` is the cache-hostile one the value catalog now actively
  INVITES** (the console ships tool-id chips + autocompletion over it), so say
  the rule out loud: **TEST MEMBERSHIP, NEVER RENDER THE LIST.**
  `{% if 'wiki_search' in tools %}` collapses a volatile list to a stable
  boolean and caches fine; `{{ tools | join(', ') }}` bakes the list's exact
  contents and ORDER into the single cached system block, and that string moves
  whenever an MCP server flaps or a caller varies `context.mcp_tools` between
  turns of the SAME session — busting the breakpoint for a session against
  itself, not merely across sessions. `tools` is the least session-invariant
  thing an operator can reach, which is exactly why it is the one worth warning
  about.

- **SECURITY: operator-only, never agent-writable.** No SessionTool or MCP
  tool reads or writes this document — an agent able to rewrite its own
  system prompt is a privilege-escalation path. Every route requires
  `X-API-KEY` (`apps/mewbo_api/src/mewbo_api/system_instructions/routes.py`).
  Same line the codebase already drew refusing LLM-authored trigger scripts
  ("Trigger behaviour is DECLARED via typed kind classes, NEVER an
  LLM-authored script") — don't reopen it here or anywhere else the
  operator/agent boundary matters.

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

**Force the emit, prompt-side not loop-side.** Nothing was *blocking*
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

**Graph-first structured seam — additive, keeps core graph-free.**
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
looked like a parked approval gate, so `GET /v1/structured` never settled.
Don't add a hardcoded literal at the `should_terminate_run()` break —
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
write-behind persistence via `RealtimeSessionRecorder` (see
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

**Runtime grants — the seam, no provider today.** A capability
*can* be granted by a RUNTIME predicate instead of a client advertisement:
`register_session_capability_provider` is a down-only push seam (mirrors
`plugins.register_builtin_root`) that an optional library above core uses to grant
a capability when a live condition holds. `Orchestrator._session_capabilities` is
the single read-point — it unions `augment_session_capabilities(...)` over the
advertised set, so a grant lands in ONE seam and flows through the unchanged
`requires-capabilities` gate (no per-tool filtering). Providers are best-effort (a
raising predicate is logged + skipped). **The generic seam remains, but NO library
registers a provider into it now.** `mewbo_graph` used to register an `scg`
provider (granted once `scg.enabled` + a source is mapped) so the scg reasoning
tools reached ordinary sessions — **since removed**: the provider sees only the
advertised cap tuple, so it could not distinguish a search surface from a bare
`POST /api/sessions` session and granted `scg` to the latter, binding 12 unused
tool schemas per LLM call and mis-tagging its provenance `origin:search`. `scg` is
advertisement-only again; keep the seam for a future capability that genuinely
needs a runtime predicate, but a blanket "grant it whenever the feature is
enabled" predicate is the anti-pattern that removal caught.

**Capability gating has TWO enforcement surfaces — gate BOTH.** A
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

**`capability_mode` is a third visibility axis — one shared predicate on BOTH
surfaces.** `SpawnAgentTask.capability_mode` (`read_only`/`execute`/`all`,
default `all`) is a coarse privilege ceiling layered UNDER `allowed_tools`/
`denied_tools`. Both surfaces consult the ONE `capability_mode_admits()` predicate
(`tool_registry.py`): `filter_specs` for registry tools, `ids_for`/`build_for` for
session tools — and the session gate runs over the FINAL selected id set, so an
`allowed_tools` entry cannot resurrect a write-tier tool under `read_only`. Tier
resolution: `ToolSpec.capability` (undeclared is safe-denied from `read_only`;
`read_only=True` specs auto-classify `read`) and `SessionToolFactory.capability`
(defaults `execute` — session tools are actions — so `read_only` admits zero
session tools unless a factory declares `read`, and `execute`/`all` stay
byte-identical to the historical behavior). The effective mode narrows monotonically at
`AgentContext.child()`; a descendant can never re-widen an ancestor's ceiling. The
first implementation gated only `filter_specs` and review caught the session-tool
hole pre-commit — the same class of miss documented above. **Rule:** a new tool-visibility
axis ships on BOTH surfaces through one shared predicate, or it doesn't ship.

## Hooks

`hooks.py:HookManager` runs lifecycle hooks. Three lifecycle points:
`on_session_start`, `on_session_end`, `on_compact`. Two hook types:
`"command"` (shell subprocess) and `"http"` (fire-and-forget POST).
All invocations are try/excepted — a failing hook logs a warning and
never blocks the session.

**A session-end hook may return a typed `OutcomeAssertion`** (`reason` +
bounded `detail` + a manager-stamped `source`) to assert an unmet purpose the
loop itself cannot see — the ONE sanctioned place a hook feeds a factual note
back into a session's outcome. `HookManager.run_on_session_end` collects every
hook's return value; a hook returning `None` asserts nothing (absence stays
absence — never coerced into a claim), and a non-`OutcomeAssertion` return is
logged and ignored. `Orchestrator._record_outcome_assertions` writes it onto
the transcript as an `outcome_assertion` event; `summarize_session` reads it
back and may only PROMOTE an already-`completed` status to `unmet_goal` — it
never overwrites a `failed`/`cancelled`/`blocked` status.

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

## Authoritative todos (`update_todos` → `todos` event)

`update_todos.py` is the ONE authoritative live-progress surface — replaces the
CLI-only tool-call heuristic (a `fleet_bridge.TodoTracker`, retired). It mirrors
`exit_plan_mode` (internal schema injected into `bind_tools`, dispatched in
`ToolUseLoop._execute_tool_call`, attached inline to root depth-0 so it carries
`agent_context.agent_id`) but is **terminal-free**: `should_terminate_run()` is
always `False` — the agent keeps working. `modes = {"act"}` (plan mode drafts a
plan via `exit_plan_mode` instead).

- **Re-emit the FULL list each call ⇒ compaction-resilient.** The tool publishes
  ONE `todos` event through the standard `append_event`→`SessionEventBus`
  choke-point; the latest event is the whole truth, so a rebuilt message list
  never desyncs the displayed progress. The schema *description* teaches the
  frequent-full-re-emit + exactly-one-`in_progress` idiom (no loop/prompt-registry
  change).
- **Shared wire contract (a console workstream consumes it verbatim):**
  `{ type: "todos", payload: { items: [{label, status}], source: "plan"|"agent",
  agent_id } }` where `status ∈ {pending, in_progress, completed}`. `TodosPayload`/
  `TodoItemPayload` are typed members of the `types.py` `EventPayload` union.
- **`source` discriminates ONE schema, not two systems:** `agent` = the live
  working set (`update_todos` emits this); `plan` = an approved plan's steps given
  optional status. `build_todos_event(items, *, source, agent_id)` is the single
  contract choke-point (normalizes: drops junk/empty labels, coerces unknown
  status → `pending`, enforces exactly-one `in_progress`); a plan-approval seam
  emits the `plan` variant through it.

## Ask-user questions (`ask_user_question` → blocking human answer)

`ask_user.py` is the native analog of the ask-user tools interactive harnesses
ship (CC's AskUserQuestion / Codex's request_user_input): the root agent asks
1-4 structured questions, the run BLOCKS, the human's choice returns as the
tool result. Decisions that are NOT obvious from the code:

- **Block-until-answered, NO timeout, NO default-answer concept** (explicit
  product decision — block-until-answered semantics, not Codex's auto-resolution). The escape
  hatches are the run's own steering signals, not a clock: a queued steer
  message supersedes the question (`declined` — the message still rides the
  normal queue into the next turn), interrupt → `interrupted`, cancel →
  `cancelled`. All of that lives in the API dispatcher's ONE wait loop
  (`mewbo_api/ask_user.py`), which polls `SessionRuntime.active_run_handle`'s
  signals read-only — the `/message` and `/interrupt` routes are untouched.
- **Conditional binding, not a plugin factory.** The tool exists only when the
  querying client advertised the `ask_user` capability
  (`context.client_capabilities` → `_derive_tool_grants` →
  `extra_session_tools`). Headless drives (triggers/wiki/search/channels)
  never advertise it, so nothing can ever block waiting for a user who is not
  there — the capability IS the timeout story. `extra_session_tools` are not
  forwarded by `_build_child_loop`, so the tool is structurally ROOT-ONLY (a
  sub-agent reports open questions through its result — CoA discipline).
- **Down-only seam parity:** `QuestionDispatcher` mirrors
  `DeviceToolDispatcher` exactly (register/reset/available/None-degrade); the
  CLI registers a blocking-modal dispatcher, the api an SSE+HTTP one. Answer
  IR is `QuestionAnswerItem` (`selected_indexes` XOR `text`, free text always
  legal — the ever-present "Other"); kinds are DERIVED from shape
  (`options==[]` ⇒ free_text), never stored.
- `modes = {"plan", "act"}` — clarifying questions matter MOST in plan mode.
  Non-terminal; `terminal_reason()` is defined explicitly on the class (the
  structural-Protocol default trap `submit_widget` hit).
- Events `user_question`/`user_question_answered` are typed union members in
  `types.py` (the device_tool_call precedent — core infra, unlike a plugin's
  self-owned payload) and intentionally ephemeral under compaction: the tool
  result carries the durable outcome into message history.

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
not a flat field). The same trap bites at LEAF level: four call sites consumed
`get_config_value("agent", "session_step_budget")` for months while `AgentConfig`
lacked the field — every deployment silently read `default=0` and the
hypervisor's budget enforcement was unreachable. A `get_config_value` key
is only real once its typed field exists; a docstring mentioning it is not.

Two corollaries that bit during a later settings-UI refinement:
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

**⚠️ An `x-group` and its console facet must land in LOCKSTEP — there is no
error if they don't.** The console's `SettingsModel` validates a section's
`x-group` against its `FACETS` list and **silently buckets an unknown group into
the `other` fallback facet**. So shipping a new `x-group` here without adding the
matching facet id to `apps/mewbo_console/src/components/settings/facets.ts`
(`FacetId` union + `FACETS`) does not fail a build or log a warning — the section
just *vanishes* into "Other", and nobody notices until a user goes looking for a
knob. Both sides move in one change, always.

The current facet ids are `models · agent · plugins · automation · integrations ·
interface · server · security · workspace` (+ the `other` fallback). The
authoritative list is the `x-group` comment block above `AppConfig` in
`config.py` — keep it and `facets.ts` in lockstep; it used to name only five of
them, which is exactly the kind of drift that makes the silent-`other` trap bite.

Two facets exist BECAUSE of the adjacency rule (a console surface must sit with
the settings that govern it — see `apps/mewbo_console/CLAUDE.md` → "Settings —
faceted shell over RJSF" before inventing a group):

- **`automation`** — `TriggersConfig` (reverse-invocation wakes) was mis-filed
  under `agent`, where the trigger policy ceilings sat nowhere near the trigger
  dashboard they govern. Its `x-group` flipped `agent`→`automation` together
  with the facet's introduction.
- **`plugins`** — Plugins is a top-level section of its own. `PluginsConfig`'s
  `x-group` flipped `agent`→`plugins` (`x-order: 1`) in the SAME change that
  moved `PluginsPane` onto the new facet in `panes.ts`. A pane and its config
  section always move together; moving one alone either strands the pane away
  from its knobs or drops the section into "Other".

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
