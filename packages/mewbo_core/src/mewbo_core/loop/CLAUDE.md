> ↑ [packages/mewbo_core/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md)

# `loop/` — the drivers

Scope: `tool_use_loop.py` · `orchestrator.py` · `session_runtime.py` ·
`task_master.py` · `planning.py` · `structured_response.py` ·
`structured_synthesis.py` · `cancellation.py`. Everything that RUNS a session, as
opposed to storing one.

`session_runtime.py` lives here rather than in `session/` because it is the top of
the dependency graph and reaches the store through `task_master → orchestrator`;
co-locating the two "session" modules by name re-creates a cycle.

This package owns **automaton B** (the turn) and **automaton C** (session status);
see the parent file's "Three automata, not one".
`tool_use_loop.py:ToolUseLoop.run()` is the only execution engine — no separate
planner/executor/synthesizer.

## The turn automaton

`while not state.done:` in `ToolUseLoop.run()` IS the automaton. **One iteration is
one state transition of the conversation:** the message list is the tape, a model
generation is the transition function, and tool results are what get written back.
`OrchestrationState` lives in `classes.py` at the package root, because four
subpackages read it.

**`done_reason` is an untyped `str | None` — the only status vocabulary in the
engine that is not a `Literal`, and therefore the one place a new value can appear
without a single consumer noticing.** Two shared readers, both to be considered
when you add a reason:

- `UNACHIEVED_DONE_REASONS` (`classes.py`) — "stopped without reaching the goal".
  ONE frozenset, not two copies: the orchestrator's completion path and the
  child-status projection both read it, so a reason added for one consumer must
  reach the other.
- `_STATUS_BY_DONE_REASON` (`session_runtime.py`) — reason → `SessionStatus`. A
  TABLE, not a dispatch chain; absent means "trust `done`".

### A cooperative stop's latency is bounded by the AWAIT, never by the poll

Polling `should_cancel` only at the top of `while not state.done:` is a correct
implementation of cooperative cancellation and a wrong implementation of Stop —
**the observed stop latency is not the poll interval, it is the duration of
whatever the loop happens to be awaiting when the flag flips.** Both things it
awaits are unbounded from its own point of view: a model generation (the whole
resilience ladder included) and a step's tool calls (a shell command, a clone, a
sub-agent fan-out).

`cancellation.py:CancellationSignal` is the fix and the shape to reuse: it wraps
the SAME injected predicate and adds `guard(awaitable)`, which races the await
against the flag, cancels it, and raises `RunCancelled`. Three load-bearing
properties, each a way the obvious version would be wrong:

- **It is INERT with no predicate.** Every caller without a run registry (the CLI's
  direct `Orchestrator` use, most tests) passes `None`, and an inert signal awaits
  directly — no extra task, no poll. That is what lets guarded call sites carry no
  None-check of their own.
- **`guard` pre-checks before starting.** A flag set while the PREVIOUS await was
  unwinding must not buy the run one more unbounded call.
- **A raising predicate reads as "not requested".** A fault READING the flag must
  never become a spurious cancellation, which is strictly worse than a stop the
  operator can re-click.

Any new unbounded await inside a turn is a new place a stop can go to sleep. Wrap
it in the signal, or state in a comment why waiting it out is acceptable. Measured
on the deployed stack: a stop issued into a 90s shell tool settles
`done_reason: "canceled"` 47ms after the request, with no `tool_result` for the
abandoned call.

**Cancelling mid-step is deliberate and leaves a partial step in the transcript** —
the tools that ran have their `tool_result` events, the rest have none. That is the
honest record, and it is safe only because the run terminates without another model
call: nothing re-drives the message list, so the dangling `tool_use` that would
otherwise need `repair_tool_pairing` never reaches a provider. **A future caller
that RESUMES from a mid-step cancel does not inherit that safety** and owes the
pairing repair.

**KNOWN RESIDUAL — an already-dispatched SYNC tool's OS work is not killed.** A
tool with no `arun` runs as `asyncio.to_thread(tool.run, …)`, and `to_thread` is
NOT cancellable: cancelling the awaiting task abandons the future while the worker
thread runs to completion, and `asyncio.run` then JOINS its default executor during
teardown. So `RunRegistry` keeps the handle for the abandoned tool's remaining
duration — `is_running` stays true, `SessionStatus` reads `running` (it overrides
the reason table) and a follow-up `POST .../query` 409s until the thread drains.
**The loop is genuinely stopped throughout.** Closing it belongs to the TOOL: the
shell tool would have to spawn a killable process group and expose a cancel hook.
Do not fix it by deregistering the handle at loop exit — a session whose handle is
released while a thread still holds its workspace admits a second concurrent run.

### A lifecycle event emitted only at the END makes its surface retroactive

`tool_call` is the INITIATION half of a tool call; `tool_result` is the outcome
half. With only the outcome, a call taking a minute is indistinguishable from no
call at all for that minute, and a call killed with the process leaves the
transcript claiming the step never happened. (The MODEL half was always correct:
`llm_call_start` precedes the generation, `llm_call_end` follows it.)

`tool_call` is emitted in `_safe_execute` — the ONE seam every tool call passes
through, chosen because it is already where `mark_tool_start` stamps the in-flight
tool BEFORE the await, and because every exit of that method already emits a
`tool_result` (including the timeout and crash paths, whose emits live in the
wrapper precisely because the inner one dies mid-flight). Emit anywhere else and
one of those paths silently loses its half of the pair.

- **`tool_call_id` is the correlation key, and `""` means NOT CORRELATABLE — never
  "shares a key".** A provider need not supply an id, and no transcript written
  before this event existed carries one, so a consumer treating empty as a key will
  fold unrelated calls onto one row. `tool_result.tool_call_id` is `NotRequired`
  for the same reason: the session page replays FULL history, so every consumer
  must still render a `tool_result` arriving with no pending row to settle.
- **The pending row and its settle land in ONE change** — `../../../CLAUDE.md` → "A
  lifecycle marker is only real when something consumes it".
- **Ordering is the feature, so a test must assert more than ordering.** Emitting
  both events at the tail satisfies "start comes before result" while leaving the
  defect intact. `tests/test_run_stop_and_tool_call_events.py` observes the event
  from INSIDE the running tool — the only assertion that can tell "before the
  await" from "before the result".

### A tool result may carry IMAGES, and they bypass the text machinery entirely

`ToolCallResult.content` stays `str`. Images ride on an additive `images` field (mirrored on
`MockSpeaker`), so every existing reader, the cap, the ANSI strip and the event snapshot are
unchanged, and a result with no image serialises byte-identically to before — which is what keeps an
ordinary conversation's cache prefix intact.

**They bypass truncation rather than teaching truncation about them.** A base64 data URI has no
meaningful character count, `_windowed` on it yields a corrupt image, and reaching `event_str` would
persist every screenshot into the store and replay it on each transcript read. `multimodal.py` owns
the split; the only place images rejoin the text is `_tool_message_content`, building the
`ToolMessage` content list that LiteLLM turns into a native `tool_result` image block.

Three traps, each already closed:

- **Images ship on the SUCCESS path only.** The provider rejects a `tool_result` carrying a non-text
  block while flagged as an error, which fails the whole request rather than the one call. A failed
  capture must degrade to text.
- **`_compact_messages` renders message bodies into a PROMPT.** A bare `str(m.content)` on a
  multipart body inlines the whole data URI into the summarizer's input, then cuts it at 2000 chars.
  `_text_of_parts` keeps the text and drops the image.
- **Stripping images is COMPACTION's job, not a turn interval's.** An interval strip mutates the
  list on an otherwise-stable turn and pays a cache invalidation to save context; compaction has
  already voided that prefix, so the same saving costs nothing. `ImageHistoryStrip` keeps the newest
  (a run driving a screen that loses sight of it must spend a turn re-observing) and replaces the
  rest with a re-request hint — for a screenshot, asking again is the only honest recovery once the
  screen has moved on. Converged with `opencode`, `kilocode` and `codex`, all of which strip at
  compaction/normalization and REPLACE rather than delete so turn structure survives.

## The safe turn boundary

**Every context mutation that re-renders `messages[0]` happens at the TOP of the
next iteration, never mid-turn.** Three do: a `model_control` model switch, a
mid-turn `switch_project`, and a resilience note that changed since it was last
baked. The reason is provider-side — a rewritten system prompt landing between a
tool call and its result produces a request the provider rejects, which is also why
`_has_dangling_tool_use` blocks a `model_control` switch while an earlier batch
sits unpaired.

`messages[0]` is never touched by compaction (`_compact_messages` slices
`messages[1:-recent_keep]`), which is why operator system-instructions needed zero
compaction work. Anything added to the system prompt inherits that for free;
anything added to the message list does not.

### `state.done` means "the loop stopped", never "the task succeeded"

**The loop PROJECTS its terminal; it does not re-derive one.**
`OrchestrationState.terminal_status()` (`classes.py`) lives on the model precisely
so a second copy cannot drift: `failed` for a not-`done` state, for
`verified is False`, and for any `UNACHIEVED_DONE_REASONS` value; `cancelled` for a
`CANCELLED_DONE_REASONS` value; `completed` only otherwise. Its return type IS
`SettledStatus` — keep the two in lockstep.

`state.done` is `True` on EVERY exit path — `canceled`, `halted_no_progress`,
`budget_exhausted`, `halted_agent_budget`, `verification_failed` included — so a
status derived from it directly says `completed` for all of them. The loop's
`finally` therefore calls `terminal_status()`. Two traps before you widen it:

- **Adding a `done_reason` whose terminal is not `failed` means teaching the
  projection the new arm FIRST.** `canceled` is deliberately NOT in
  `UNACHIEVED_DONE_REASONS` — a cancelled run did not fall short of its goal, it was
  never allowed to pursue it. The call site is not where a missing vocabulary gets
  patched.
- **Widening the return type is a change to every consumer that compares against
  it.** `_drive_with_retry` keys on `== "failed"`, not `!= "completed"`, because the
  latter silently began matching `cancelled` the moment the third member existed —
  re-delegating work somebody had deliberately stopped.

### A sub-agent reaching a terminal does not mean its subtree is settled

The loop's shutdown sweep cancels children whose status is `running`, but the
non-terminal set is `ACTIVE_STATUSES` = `{submitted, running}`. A capacity-deferred
child sitting at `submitted` is outside the sweep, while the session-wide
`AgentQueue`'s `release()` pump fires on any sibling's completion anywhere in the
session. **Only session-level cleanup (`registry.cleanup(timeout=…)`, at session
end) guarantees settlement.** Do not write a caller that infers subtree quiescence
from a parent's terminal.

## The completion seam

Three gates stand between "the model returned no tool calls" and
`done_reason="completed"`, in this order: the **verifier**, the **promise gate**,
then the **required-terminal gate**. They answer different questions — "is the
claim contradicted by ground truth", "is delegated work still live", "did the run
call a tool it was required to call" — and a new gate joins this list rather than
becoming a fourth branch somewhere else.

**A gate is IN-BAND; the automatic re-drive is not one and must not become a
fourth.** The three run inside the turn, with the context warm and no second run to
pay for, so they are the cheaper correction and stay the first line; `GoalRetryGate`
is the escalation that begins where they stop. The required-terminal gate is the
clearest instance of the pairing: when its nudges are exhausted it stamps the
honest `done_reason="unmet_goal"`, exactly the input the re-drive keys on.

`blocked_code` is NOT a gate. It is stamped from the last unrecovered blocked-class
tool error AFTER `done_reason` is set, at this seam and on every other terminal
path alike, because a blocked run is blocked regardless of which exit it took. It
annotates a terminal; it never prevents one.

**Verifier-gated completion** (`contracts/verification.py`, `CommandVerification`).
A ground-truth command check runs at the NATURAL-COMPLETION seam ONLY (the
`if not response.tool_calls:` branch) — never on a budget-wrapup/halt/session-
terminal path, because a halt is not a claimed completion. Data-owned like
`TriggerSpec` (discriminated union, one total `from_value` → `None` = inert, no
`if kind ==`; the model picks argv, never authors verification LOGIC). The runner
subprocess sits behind a `VerifierRunner` Protocol (`interpret` does NO I/O; argv
list, `shell=True` never, `_scrubbed_env`); the ONE feedback class injected back is
the grounded verifier output, a `SystemMessage` with a thin factual frame. A failed
check `continue`s through the top-of-loop budget checks FIRST, so retries are
bounded by BOTH `verification_max_retries` AND the step/wall budget. Two-gate
arming (`CommandVerification.gate_active`, the SAME predicate the loop and
`spawn_agent`'s note share): `verification_enabled` AND
`capability_mode ∈ {execute, all}`. `done_reason:"verification_failed"` is HONEST —
the text is still returned, never masked as `completed` (`state.verified` False vs
None-never-ran vs True). A spec present-but-INACTIVE for a child MUST surface
`"specified_but_inactive (…)"` in the spawn response and event — never the
invisible-null trap. `verification_enabled` defaults OFF; the bounded
`verification` event carries scalars only (NO stdout/stderr on the wire — grounded
tails go only into model context).

**Promise-as-completion gate (root only).** The seam refuses a clean terminal while
the run still owns LIVE background work: `collect_running(agent_id)` checks the
agent's OWN still-running children; if any are live it injects a
`loop.agents_still_running` system message and `continue`s. Root-only
(`depth == 0`) and bounded (3 nudges) so a model that genuinely won't wait for its
own spawned work is eventually let through rather than nudged forever.

**Required-terminal gate (root only).** A bound `SessionTool` can declare
`required_terminal = True` + `terminal_satisfied() -> bool` (the SessionTool
declaration law's fifth instance, `tooling/CLAUDE.md`) — an obligation a clean
terminal must not accept while unmet. `_unmet_required_terminals()` reads both
defensively via `getattr` + `callable` (never Protocol members). Bounded the same
way the promise gate is, but the two diverge on exhaustion: the promise gate lets a
stubborn model through as `completed`, while this gate stamps
`done_reason="unmet_goal"` — the reply text was never delivered through the required
tool, so accepting it as `completed` would repeat the defect the gate exists to
close. `wiki_emit_answer` is the canonical case.

**No agent halts into a stump.** Every budget/contract exhaustion routes through
`_budget_wrapup_turn(reason)` — one text-only unbound turn whose output lands in
`AgentResult.summary` (`done_reason` `budget_exhausted` / `halted_agent_budget` →
`status="incomplete"`, recoverable). The one contract kill is wall-deadline 100%
(two-strike: an NL warning at 80% precedes it).

## Binding

**EVERY rung binds through `_bind_model` — the same four populations, or the
escalated call is lossy.** `_bind_model` appends the directly-bound legs to its own
LOCAL parameter, so the caller keeps a REGISTRY-ONLY list and that is what reaches
`_invoke_with_resilience`. A fallback rung rebuilt with
`build_chat_model(...).bind_tools(tool_schemas)` binds the registry leg alone, so a
run that fell to a fallback model loses the spawn family, `activate_skill` and its
SessionTools for exactly that generation (observed live: 15 tools → 1 → 15). It
self-heals on the next step, which is precisely why it hides: the collapse is one
generation wide, at the moment a struggling run can least afford to lose
delegation. The binder takes an optional `model_name` and the fallback branch
routes through it. **The reuse guard keys off the model NAME, never
`is_fallback`** — a sticky pin reordered to index 0 makes `is_fallback` False while
the binding is stale. Corollary: `llm_call_start` carries `bound_tools` (a COUNT
stamped by the binder rather than recomputed at the emit site — a second derivation
of "what is bound" is free to drift from the real one), so a collapsed surface is
one query.

`_bind_model` binds FOUR populations — registry specs, the spawn family,
`activate_skill`, per-agent session tools — and three never reach the registry. Two
other packages depend on that: `tool_search` must search what the binder binds
(`tooling/CLAUDE.md`), and a child must not lose the spawn family
(`agents/CLAUDE.md`). The binder is owned here; do not restate the population list
elsewhere.

## Session runtime and status

`session_runtime.py:SessionRuntime` is the only place a session is created,
resumed, forked, archived, or pinned. Channels, the API and the CLI all go through
it — no parallel session implementations.

**`list_sessions` takes a `SessionQuery` for narrowing, plus `limit`/`offset` for
paging.** ONE way to narrow a listing, because two ways to say the same thing is
how the archived filter came to be applied in a different place from the owner
filter. The WHOLE query goes down to `list_session_digests`, which resolves it
through `query_sessions` — so `owner`/`archived`/`pinned`/`projects` are decided at
the store, in one indexed read, and a session the query rejects is never opened.
See `session/CLAUDE.md` → "Filterable facets" for which fields may be storable at
all. What remains post-summarisation are the two visibility rules that genuinely
need the transcript's events — no user-visible event, and no `created_at` — and
those are hygiene, not user filters.

**A page is cut from what the query ADMITTED — filter first, then page.** Paging
first and filtering the page would make `pinned=True` with a `limit` return only
the pinned sessions inside the newest N candidates, usually none of them. A filter
shrinking the *candidate set* is the feature; a filter shrinking the *page* is a
listing that silently lies. `tests/test_session_filterable_facets.py` pins both
directions with an adversarial seed (every matching session older than every
non-matching one, so a wrong order returns an EMPTY page, not a short one).

**Ordering is pinned-first, then newest-first, and pinning is ONLY an ordering.** A
pinned session stays subject to whatever filters are active. That makes "a mobile
surface shows only its own pinned sessions" true by construction: filtering a
sorted list preserves relative order, so a consumer's own origin filter composes
with the server's order for free. **A pin that could hoist a row past a filter
would be a leak** — it would surface an Aura session in the console task list,
which is what `DEFAULT_VISIBLE_ORIGINS` exists to prevent. Implemented as two
STABLE sorts rather than one composite key, so the second pass only moves pinned
rows to the front and stability preserves the recency order.

**The seam that ACCEPTS a turn persists it.** `start_async` returns a `run_id` the
instant it hands the run to a background worker, and that worker pays
`Orchestrator.__init__`'s heavy synchronous setup before the orchestration body
appends anything — so a `user` event written by the body is invisible for the whole
cold-start window and every client renders an empty session. `start_async` writes
the `user` event itself, adjacent to `run_accepted`, and threads
`user_turn_persisted` down (`run_sync`/`arun` → `orchestrate_session[_async]` →
`Orchestrator.run`/`arun` → the shared body) so the body skips its own append.
Three load-bearing properties:

- The flag defaults to **`False`**, so every direct-`Orchestrator` caller (the CLI
  turn engine, the structured runners) still owns the append.
- Suppression is by FLAG, never by content comparison — two identical consecutive
  queries are legitimate, and comparing text would silently drop a real turn.
- The flag is the RESULT of the `is_running` pre-check rather than a hardcoded
  `True`, since that check is not atomic with the registry's own locked accept; a
  run finishing in between would otherwise lose the turn outright.

`SessionStoreBase.append_user_turn` is the ONE builder of that payload — both seams
write it, and a second literal would drift silently.

A listing row is built from two batched store calls — `list_session_digests`
(EXACTLY once per listing, a test pins it) and `load_session_records` — never from
per-session transcript reads. The cost argument and the rule for adding a new
listed fact belong to the store: `session/CLAUDE.md` → "Shrink the INPUT, never
fork the fold".

### `SessionStatus`

**DERIVED AT READ TIME AND NEVER STORED.** Ten members: `idle · running ·
completed · incomplete · canceled · failed · awaiting_approval · terminated ·
unmet_goal · blocked`. Teaching the map a new arm reclassifies every existing
session with no migration — a feature when the classification improves, a hazard
when it is wrong, since there is no stored value to compare against.

Override precedence: `running` overrides the reason table → `terminated` overrides
everything, including a live run (cancellation is cooperative, so `is_running`
briefly lags a terminate) → a `blocked_code` on the completion payload outranks any
reason-derived status. `_STATUS_BY_DONE_REASON` maps `verification_failed` and
`halted_no_progress` → `unmet_goal`; a `blocked_code`
(`repo_access`/`network`/`forbidden`/`quota_exceeded`) maps straight to `blocked`.

**Universal recovery.** Any non-complete session is recoverable —
`summarize_session.recoverable` is true when a run ended not-successfully (incl.
killed mid-call with NO completion event), **with ONE carve-out: a permanently
terminated session forces `recoverable=False` and beats even a live run**
(`session/CLAUDE.md` → "Hard termination"). `resolve_recovery_query` yields the
re-drive query (`continue` = resume the SAME session, memory/transcript intact;
`retry` = restart the last turn), then `start_async`/`run_sync` re-runs the one
loop — so recovery inherits automatic model-heal for every task type. Neither
re-prompt is built in code: `continue`'s is `loop.recovery_continue` in the prompt
registry, and `replacement_text` substitutes whichever query the action would
otherwise build. The optional `trigger=` names a NON-HUMAN originator on the
`recovery` marker and is written ONLY when supplied, so every operator-driven
recovery event stays byte-identical.

**Non-obvious trap: `reinject_recovery_context` MUST re-emit the session's gating
context** (`client_capabilities`, `structured_workspace`) on recovery, else a
recovered capability-gated session (wiki/qa/structured) silently loses its AgentDefs
because the most-recent context event no longer advertises them. The API `/recover`
dispatches origin-aware: a session backing a recoverable wiki indexing job →
checkpoint `WikiResume` (skip-if-done), everything else → generic continue/retry.

### Automatic one-shot re-drive on an unmet goal

`GoalRetryGate` (`session_runtime.py`) re-invokes a session EXACTLY ONCE when its
run terminated with the goal unmet. Six facts, each a place the obvious
implementation is wrong:

- **The seam is post-RELEASE, and `on_session_end` is structurally incapable of
  it.** `RunRegistry.start` takes `on_release`, fired inside `_wrap_run`'s `finally`
  AFTER the handle has been popped; `_on_loop_run_done` is the loop-backed twin,
  firing after `finalize_loop_run`. A session-end hook runs inside the run's own
  `finally`, which still HOLDS the registry slot, so a same-session `start_async`
  refuses and returns `""` — and the one-shot marker is written before the launch,
  so that spends the whole budget on an attempt that never started. Being inside
  the `finally` also means a run that RAISED reaches the callback: a crash is a
  terminal too. The callback owns its own failure isolation, because a raise there
  would replace the run's exception.
- **The predicate is the DERIVED session status, not a new goal check.** It
  proceeds only when `summarize_session` reports `status == "unmet_goal"` AND
  `recoverable`. Two independent producers already converge on that status — the
  loop's `done_reason` through `_STATUS_BY_DONE_REASON`, and an `OutcomeAssertion`
  promoting an otherwise-clean `completed` — so a third predicate would be a second
  opinion, free to drift from the status every client renders. The goal is NAMED
  from the same summary, most specific first: `unmet_goal_detail` →
  `unmet_goal_reason` → the `done_reason` token.
- **The one-shot ledger is the EXISTING `recovery` event, which is what forces
  `action="continue"`.** The gate stamps `payload["trigger"] = "auto_goal_unmet"`
  (`RecoveryTrigger`) and refuses when a marker carrying that trigger is already in
  the transcript — durable across restarts, no store field, no counter. `retry`
  deletes the failed turn and everything after it, which would delete the marker
  and make the guard structurally impossible; `continue` is mandatory, not a
  preference. The scan reads the FULL transcript rather than a digest: `recovery`
  is not a projected signal type, so a digest read would report "never retried" for
  a session that had been.
- **Exclusion is a TABLE keyed on `SessionTag.parse(tag).session_type`** —
  `EXCLUDED_SESSION_TYPES`, currently `{"wiki_index"}`. An indexing job already
  fails closed on zero pages and on an empty graph and is re-driven up to its own
  cap at boot, so a generic re-invocation would stack one more attempt on the most
  expensive workload in the product AND be invisible to that cap, which counts only
  its own retries. The uncovered case the gate exists for is wiki QA, which shares
  the `wiki` ORIGIN but not the session type — hence keying on type, and a second
  self-correcting job kind is one row rather than a new branch.
- **The relaunch replays `start_async`'s own arguments.** `run_arguments =
  dict(locals())` is the FIRST statement of `start_async`, so it is exactly that
  call's parameters; `_relaunch` re-invokes with `user_query` swapped for
  `loop.goal_unmet_retry` and `attachments` dropped (already persisted on the turn
  this session holds; replaying them would attach the same files twice). Do not
  hand-list the parameters — that is a second home for one thirty-parameter
  contract, and it drifts the day a parameter is added.
- **The cost ceiling is stated because nothing else bounds it.** No token wallet
  exists (`TokenBudget` is compaction accounting), so: **one additional run per
  session, bounded only by that run's own step/wall budget.** It cannot compound —
  the retry's own terminal reaches this gate, finds its own marker, and refuses.

## Structured responses (`structured_response.py`)

Schema-constrained agentic output = a terminal `SessionTool` whose function
parameters ARE the caller's JSON Schema. `EmitStructuredResponseTool` reuses the
`ExitPlanModeTool` termination pattern (`should_terminate_run()` polled at the
per-step terminal check) and validates `tool_input` with `jsonschema` inside
`handle()`. Validate-or-reask lives **in the emit tool** via the normal tool-result
feedback loop — on a `ValidationError` it returns a "fix these fields"
`MockSpeaker`, bounded by `DEFAULT_MAX_FAILURES` (the Nth failure terminates the
run, so the model gets N-1 reasks); on success it emits a `structured_output` event
and terminates. So schema-constrained output needs **zero new control loop and no
`tool_choice` plumbing**.

Prefer this tool-call route over native `response_format` whenever other tools are
bound — mixing a strict `json_schema` with non-strict MCP tools throws an
`additionalProperties` 400. Non-object/union schema roots are wrapped as
`{"result": <schema>}` and validated against the *wrapped* params (what the model
sees == what we check), then unwrapped on the way out.

`StructuredResponder` drives one bounded session via
`run_sync(strict_tool_scope=True, approval_callback=auto_approve,
extra_session_tools=[emit])` — `extra_session_tools` is the plugin-manifest-free
seam for injecting a `SessionTool`. The emit tool's `operation` infers to `"set"` →
default policy `ASK`, so the auto-approve callback is **required**, not optional.

**Force the emit prompt-side, not loop-side.** Nothing blocks the emit; the model
just is not *forced* to call it and will finish in prose → `payload is None` → 422.
`FORCE_EMIT_DIRECTIVE` is injected via the existing `skill_instructions` seam, and
`run()` does ONE bounded re-drive (a sharper `SystemMessage`, reusing the emit
tool's own reask machinery) when `payload` is still `None` — only then raise.

`StructuredResponder.run` and `.start_async` share one `_prepare()` builder — the
SOLE provenance stamp seam: it tags the session `structured:run`
(`STRUCTURED_RUN_TAG`) and writes the `source_platform` context event from the
responder's field (set by the route from `X-Mewbo-Surface`). Stamp once here and
MCP `structured_query` is covered for free. (Tag a session you already hold via
`runtime.tag_session`, the write-only sibling of `resolve_session`'s tag
*resolution* — never reuse a constant tag as a resolution key or two runs collide
onto one session.)

`start_async` exists so `/v1/structured` is async-recoverable:
`SessionRuntime.start_async` mints and returns a storeless per-run
`run_id = "<session_id>:r<seq>"`; `""` means "refused, busy". **The session
transcript IS the run record — no parallel run store.** It drives through
`runtime.start_command` (the `RunRegistry` seam) — a managed background run,
serialized per session and cancellable — NOT a raw daemon thread.

**Graph-first structured seam — additive, keeps core graph-free.**
`StructuredResponder` has four optional, app-injected fields so a search workspace
can drive it graph-first without core importing the SCG engine: `capabilities`
(override the default `wiki` advertisement with `["scg"]`), `context_events` (extra
binding context), `extra_instructions` (a trusted playbook PREPENDED to
`FORCE_EMIT_DIRECTIVE` in the one `skill_instructions` slot), and `scope_factory`
(a context-manager factory — the `ScgScope` source scope — wrapped around each
`_drive`). All default to the historical wiki behaviour; the app side
(`agentic_search/scg/graph_structured_runner.py`) supplies them via
`WorkspaceGraphBinding`. The terminal stays the schema-validated `emit_result`.

**`done_reason` on success = `"completed"`, not `"awaiting_approval"`.** The loop
reads `SessionTool.terminal_reason()` (default `"awaiting_approval"`) from the
terminating tool's class; `EmitStructuredResponseTool` overrides it. Without that
override a successful structured emit looks like a parked approval gate and
`GET /v1/structured` never settles. Don't add a hardcoded literal at the
`should_terminate_run()` break — override `terminal_reason()` on the tool class.

**Two no-loop synthesis primitives** reuse the emit machinery without a
`ToolUseLoop`: `StructuredSynthesizer` (one schema-constrained round-trip + one
reask, reusing `build_emit_schema` + `EmitStructuredResponseTool.handle`) and
`DraftStreamer` (one **tool-light** `.astream()` of token deltas — NO
`bind_tools`). Retrieval grounding is injected via the `GroundingProvider` Protocol
so **core stays graph-free** (the concrete `WikiGroundingProvider` lives in the
app). `model_name None → config default` (`build_chat_model` requires a `str`).
Both stay store-free here; the app session-backs them with write-behind persistence
via `RealtimeSessionRecorder`, keeping the latency-critical synthesis unburdened by
a session store.

## Sync→async bridge

`orchestrator.py:Orchestrator.run_sync` wraps the async loop for callers that can't
`await`. It owns the event loop lifecycle and guarantees cleanup of background
tasks via `await_lifecycle_managers(timeout)`. Don't introduce a parallel
sync/async bridge — extend this one.
