> ↑ [packages/mewbo_core/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md)

# `tooling/` — everything that produces or admits a callable surface

Scope: `tool_registry.py` · `session_tools.py` · `client_tools.py` · `skills.py` ·
`plugins.py` · `update_todos.py` · `exit_plan_mode.py` · `ask_user.py` ·
`model_control.py`. Two populations live here and behave differently: stateless
`ToolSpec`-described registry tools, and per-session `SessionTool` instances the
registry never sees.

## THE SESSIONTOOL DECLARATION LAW

**A `SessionTool` has no `ToolSpec`, so every per-tool knob the loop resolves from
one must be DECLARED on the tool's own class. Silence is not a default — it is
whatever the registry chose for shell and MCP output.**

| Knob | Resolved by | Undeclared fallback |
|---|---|---|
| `max_result_chars` | `_result_char_cap(tool_id)` | the 2000-char registry default |
| `execution_timeout(tool_input)` | `_declared_execution_timeout` | a flat 120s `asyncio.wait_for` ceiling |
| `terminal_reason()` | polled at the `should_terminate_run()` break | `awaiting_approval` — or an `AttributeError` |
| `poll_class` / `poll_when_args` | `DoomLoopGuard.poll_rules` | the call counts as non-progress |
| `required_terminal` / `terminal_satisfied()` | `_unmet_required_terminals()` at the natural-completion seam | the gate is inert — every tool is optional |

Each row is a live trap, not a hypothetical:

- **Result cap.** An undeclared session tool is truncated at 2000 chars on every
  call, and the model then spends whole steps repairing the contract instead of
  trusting the read. Session tools read `max_result_chars` via `getattr`, falling
  back to `DEFAULT_SESSION_TOOL_MAX_RESULT_CHARS` (200,000) only when absent.
  **Size a new cap from a realistic payload, not from taste.**
- **Result cap, the REGISTRY half.** A `ToolSpec` that declares no
  `max_result_chars` falls to the same 2000-char default. Shell output is the
  archetypal unbounded payload: a test run, a build log or a paged API response
  reaches the model as its first 2000 characters, head-first — the end a traceback
  is NOT at. **That cap is why so much tooling pipes through `head`/`tail`/`grep`:
  the pre-filtering is a workaround for the cap, not a preference.** The shell
  spec's cap is 30_000, and `_windowed` in the loop keeps BOTH ends with a sized
  omission marker. **Read the default as a decision you make by silence.**
- **Execution ceiling.** A tool that advertises "no timeout" and a dispatcher that
  honours it are both still killed by `_safe_execute` at 120s unless the class
  declares `execution_timeout`. Both halves being locally correct is exactly why it
  reads as the tool lying about itself.
- **Terminal reason.** `SessionTool` is a structural `Protocol`, so its default
  method bodies are NOT inherited by standalone implementers — only real subclasses
  get them. A tool that CAN terminate defines `terminal_reason()` on its own class;
  a tool whose effect is an event should not terminate at all. Omitting it raises
  `AttributeError` *after* the tool's work is already done.
- **Required terminal.** Declaring `should_terminate_run()` says "a call to me ends
  the run"; nothing says the inverse until `required_terminal` is set. Without it a
  model can compose a complete answer, narrate the call as plain text, and stamp a
  clean `done_reason="completed"` with the answer discarded and its backing record
  stranded at `status: "running"` forever.

**Two of the five are read PER CALL, against that call's arguments.**
`execution_timeout(tool_input)` and `poll_when_args` both take the arguments, which
is how `agentic_search(run_id=…)` (polling an existing run) is exempt from
doom-loop counting while `agentic_search(query=…)` still counts.
`_declared_execution_timeout` returns `(did_declare, seconds)` rather than a bare
value, because a bare `None` cannot distinguish "declared unbounded" from "declared
nothing". Both hooks are read defensively via `getattr` + `callable` and are
deliberately NOT `Protocol` members: a raising or non-numeric hook falls back to
the flat ceiling rather than failing the call, since the hook runs BEFORE `handle()`
validates and must never be the thing that reports a bad argument. `check_agents`
keeps a hardcoded arm because it is loop-injected and owns no class to declare on —
a third such tool declares rather than growing a third arm.

**`extra_session_tools` is gated on `capability_mode`, and that append is the ONLY one downstream of
`build_for`'s gates.** It sits after them, so before this it was a hole in every privilege ceiling
for the one tool population a CLIENT controls. Tolerable while that meant `setAlarm`; not once it
includes a shell at shell UID, which a `read_only` sub-agent would have been handed. It routes
through the same `capability_mode_admits` predicate, and a tool declaring no `capability` is read as
`execute` — session tools are actions, so an undeclared tier fails closed.

`ClientDeclaredTool` declares two of the five knobs: `max_result_chars = 30_000` (matching the
registry's shell spec rather than a fresh number, because device output is shell-class and each
truncation costs a network round trip to the device, not a local pipe read) and
`capability = "execute"` so the gate above has a tier to read.

## `allowed_tools` is THREE-STATE

**Every test on it is `is None`, never truthiness.** `None` is unrestricted, `[]`
grants NOTHING, non-empty grants exactly those. Truthiness collapses `[]` into
`None` — "this principal gets no tools" into "it gets every tool" — which is
fail-open on the gate that binds an agent's whole tool surface. It bites hardest on
delegation: a role-bounded viewer's composed allowlist omits the spawn family
precisely to disable delegation and can compose down to empty, so reading empty as
unrestricted hands delegation back to the principal the ceiling exists to deny.

Three kinds of site move together: the parse sites (an AgentDef `tools: []` /
skill `allowed-tools: []` must PERSIST as `[]`), the spawn args (model-supplied —
the tool schema tells the model "omit for all tools; an empty list grants none", so
code and schema move together), and `filter_specs` itself. `denied_tools` is
deliberately NOT three-state: deny is purely subtractive, so an empty denylist and
no denylist are the same set.

## Deferred tool-schema loading (`tool_search`)

MCP / `metadata.deferred` schemas are stripped from the initial `bind_tools` and
surfaced by name only (`<available-mcp-servers>` / `<available-deferred-tools>`);
the model fetches what it needs through the client-side `tool_search` tool
(`ToolSearchRunner`, in `mewbo_tools`), and the per-turn re-bind grows the bound
set. Discovery is replayed from message history each turn, so it is
compaction-resilient.

`_is_tool_search_enabled(tool_specs)` is the SINGLE read-point: `off`/`on`/`auto`.
**`on` is the default — a user's MCP tools never occupy the context window merely
by being configured.** `auto` (defer only above `agent.tool_search.auto_threshold`,
25) is an opt-in, not the default: `_select_active_specs` already requires a
NON-EMPTY deferred set, so a zero-MCP session binds an identical list under `on` and
`off` alike. The only range where the modes differ is 1..`auto_threshold`, where
`auto` binds up to 25 schemas verbatim EVERY turn (~240 tokens each) — a cliff a
deployment crosses invisibly whenever an MCP server is added or removed.

**Trap:** `_is_tool_search_enabled`'s own `get_config_value(..., default=…)`
literal is UNREACHABLE — `get_config()` returns the validated `AppConfig`, so the
`getattr` walk always yields the model default. Keep that literal in lockstep with
`ToolSearchConfig.mode` anyway; it is what a reader believes.

Two load-bearing invariants:

1. `tool_search` is `always_load`, and `filter_specs` EXEMPTS `always_load` specs
   from the `allowed_tools` allowlist gate (an explicit deny still wins) — else a
   scoped sub-agent gets its MCP tools deferred AND loses the means to fetch them.
2. Deferral is ORTHOGONAL to the plan-mode filter: `tool_search` is read-only so it
   binds in plan mode, and a discovered MCP tool is admitted unconditionally once
   bound — the plan-mode filter never mode-filters `kind == "mcp"` specs at all,
   since Mewbo cannot classify a third-party MCP tool's effect. Never special-case
   plan mode in the deferral path.

**`tool_search` must search what the BINDER binds, not just the registry.**
Filtering only `ToolRegistry.list_specs()` leaves the three directly-bound
populations (spawn family, `activate_skill`, per-agent SESSION TOOLS) permanently
unsearchable, answering "No deferred tools are registered." The fix belongs at the
CALL SITE, never on the cached runner instance (per-agent state baked into a
`ToolRegistryCache`-shared runner leaks scope): `_execute_tool_call` intercepts
`tool_search` and passes a `supplement=` built from
`_directly_bound_tool_schemas(plan_mode=…)` — the ONE helper `_bind_model` also
uses, so searchable ≡ bound and a strictly-scoped agent can never widen via search.
`select:` on a supplemented name is a no-op, and supplemented names are NOT in
`_deferred_ids`, so the re-bind discovery scanner ignores them.

## A model-facing schema is READ, not RESOLVED

**Do not assume a model will dereference `$ref`.** Two unrelated small models (a
26B MoE and a 9B) were each handed `present_ui`'s FULL untruncated schema —
11,082 characters, delivered complete through `tool_search`, no truncation
anywhere — and neither could call the tool. One ran
`tool_search select:GenerativeUISpec` and
`select:AlertNode,BadgeNode,CardNode,…`, trying to resolve `$ref` TARGETS as if
they were tools. Both then used `$defs` KEYS as object keys. Seven of twelve
calls were rejected.

**That is observed behaviour, not a measured cause**, and the honesty matters
because the schema was also 3,144 tokens with an eleven-way recursive union —
three confounded variables, no ablation. A literature check found **no** public
benchmark isolating flattened-vs-`$ref` accuracy (BFCL, ToolBench and API-Bank
all vary function *count*, not schema indirection). What corroborates the
mechanism is at the ENGINE layer rather than the model layer: llama.cpp's own
GBNF README says its JSON-schema converter handles a subset, that **unsupported
features are skipped silently**, and that **nested `$ref`s are broken** — and a
reported 8B case had an enum behind a `$def` accept arbitrary objects while the
same enum inlined worked. So a schema can be grammatically enforced, weakened,
or unenforced with nothing logged either way.

**The corollary is a debugging rule: never infer that guided decoding is engaged
because a schema was submitted.** vLLM has a confirmed case where a reasoning
parser routed generation into an unconstrained channel and the grammar never
bound, on the same server and schema where the other API path was constrained.
Check the serving backend and the compiled grammar, not the request.

Three rules follow, and all three are cheap:

- **State the vocabulary flat, in the DESCRIPTION.** A closed set of names and
  their required fields belongs in prose the model reads before it decides
  anything — `present_ui`'s whole eleven-component vocabulary is 93 tokens that
  way against 3,144 tokens of schema. DERIVE it from the model
  (`GenerativeUISpec.component_guide()` reads `model_fields`), never hand-write
  it: a hand-written list is a mirror, and mirrors drift.
- **Never let a Python class name reach the model.** Pydantic keys a `$def` by
  CLASS, so a discriminated union offers `#/$defs/AlertNode` for a variant whose
  only legal tag is `Alert`. Fix it at pydantic's OWN seam — a
  `GenerateJsonSchema` subclass overriding `normalize_name`, passed through
  `pydantic_to_openai_tool(schema_generator=…)`; `ComponentTagSchema` is the
  worked example. `ConfigDict(title=…)` does NOT do it (verified: it sets
  `title` and leaves the key). **Do not post-process the emitted dict** — the
  name appears as the `$defs` key, in every `$ref` STRING, and in the
  discriminator mapping, and a pass that finds only some of them leaves the
  schema self-inconsistent, which is worse than not renaming. Overriding
  `normalize_name` makes pydantic repoint all three itself.
- **Delete wrapper levels that carry no information.** A field whose only job is
  to hold one other field is a level that can only be got wrong: five of those
  seven rejections were `present_ui`'s old `spec` wrapper — sent as a bare list,
  as an object, with node fields spread onto it, with `$defs` names as its keys.
  It is gone; `PresentUiArgs` subclasses `GenerativeUISpec` so `root` is
  top-level, and the EMITTED event keeps its frozen `spec: {"root": […]}` shape.

**A rejection is model-facing text too, so it must teach.** A bare pydantic error
names the failing path and stops, leaving the caller to INFER the contract from a
sequence of refusals — which both traced models did out loud, and one of them
inferred wrongly, announced the wrong shape as a key insight, and sent it. Append
the canonical call and the derived vocabulary to the error. This is ADDITIVE:
validation stays exactly as strict, nothing is coerced, and no evidence is
normalized away. Size `max_result_chars` for it.

`pydantic_to_openai_tool` strips `title` at every depth (it long claimed to and
only did the top level). `properties` and `$defs` are NAME MAPS — recursion
enters them through VALUES only, or a field genuinely called `title` (`Card`,
`Alert`) vanishes from the model's view while validation still requires it. Flat
schemas save nothing; nested ones save 6-10% (`submit_app`: 3,096 → 2,893).

**A discriminated union is a PORTABILITY liability, and the vendor limits are
published.** Pydantic emits `oneOf` + `discriminator`; OpenAI's structured-output
docs list `anyOf` and document neither `oneOf` nor `discriminator`, and Gemini's
subset likewise. Anthropic's strict tool use caps a request at **16 union-type
parameters** (also 20 strict tools, 24 optional params) and returns a 400,
`Schema is too complex for compilation`, past its grammar budget — its own
guidance is to flatten. `present_ui`'s eleven-way recursive union is inside that
cap today and could stop being so.

**What is NOT established**: that per-variant typed alternatives are easier for a
model to FILL than a generic `{component, props}` bag. `nodes.py` used to assert
that flatly; no measurement supports it in either direction. What per-variant
contracts definitely buy is post-generation validation and renderer safety, which
is reason enough to keep them — but the call-success half is an empirical
question about a specific model and backend, and the way to settle it is an A/B
against the deployed endpoint, not an argument.

## A SessionTool that RETURNS a structured-error envelope is a FAILED step

A tool signals failure by RAISING (caught → `success=False`) OR by RETURNING the
shared `{"error": {"code", "message"}}` envelope as a *successful* `MockSpeaker`.
Recording the latter as `success=True` means the per-step failure nudge ("N/M tool
call(s) failed this step") never fires and an enveloped provider 429 renders as
"✓ ok". `_session_tool_error_envelope` — a pure structural check, since core never
imports the graph layer and the envelope shape is the only contract — reclassifies
such a return: the `tool_result` event gets `success: False` + the envelope message
as `error`, while the model STILL receives the envelope JSON as the tool output.
The seam fires ONLY on the session-tool dispatch path; normal `ok_result` returns
are untouched.

## Skills and plugins

`skills.py` — Agent Skills discovery (`~/.claude/skills/` and `.claude/skills/`).
Catalog injected into the system prompt for auto-invocation via `activate_skill`;
a user's `/skill-name` is detected in `Orchestrator` and rendered via
`skill_instructions`.

**Per-drive opt-out: `enable_skills` (default `True`).** Discovery scans the
process cwd/home, so a headless product drive (search/wiki) inherits the HOST's
`~/.claude` skills and burns its first step on `activate_skill`. Passing
`enable_skills=False` (threaded `run_sync`/`start_async` → `orchestrate_session` →
`Orchestrator.run` → `ToolUseLoop`, consumed at the `ACTIVATE_SKILL_SCHEMA`
injection site) suppresses the schema for the ROOT *and* every spawned child
(`SpawnAgentTool` carries the flag onto child loops). The deeper cure is CWD
isolation — not loading host skills at all for these drives.

`plugins.py` — plugin discovery, install, uninstall, marketplace read. Plugins
contribute agent definitions, skills, hooks and MCP tools.
`load_all_plugin_components()` runs during session init.

## Authoritative todos (`update_todos` → `todos` event)

The ONE authoritative live-progress surface. It mirrors `exit_plan_mode` (internal
schema injected into `bind_tools`, dispatched in `ToolUseLoop._execute_tool_call`,
attached inline to root depth-0 so it carries `agent_context.agent_id`) but is
**terminal-free**: `should_terminate_run()` is always `False`. `modes = {"act"}` —
plan mode drafts a plan via `exit_plan_mode` instead.

- **Re-emit the FULL list each call ⇒ compaction-resilient.** ONE `todos` event
  through the standard `append_event`→`SessionEventBus` choke-point; the latest
  event is the whole truth, so a rebuilt message list never desyncs the displayed
  progress. The schema *description* teaches the frequent-full-re-emit +
  exactly-one-`in_progress` idiom, so no loop or prompt-registry change is needed.
- **Wire contract (the console consumes it verbatim):**
  `{ type: "todos", payload: { items: [{label, status}], source: "plan"|"agent",
  agent_id } }` where `status ∈ {pending, in_progress, completed}`. `TodosPayload`/
  `TodoItemPayload` are typed members of the `types.py` `EventPayload` union.
- **`source` discriminates ONE schema, not two systems:** `agent` = the live
  working set; `plan` = an approved plan's steps given optional status.
  `build_todos_event(items, *, source, agent_id)` is the single contract
  choke-point (drops junk/empty labels, coerces unknown status → `pending`,
  enforces exactly-one `in_progress`).

## Ask-user questions (`ask_user_question`)

The root agent asks 1-4 structured questions, the run BLOCKS, the human's choice
returns as the tool result.

- **Block-until-answered is the DEFAULT; `timeout_seconds` is opt-in per call, and
  there is no default-answer concept.** Unbounded is safe because the escape
  hatches are the run's own steering signals rather than a clock: a queued steer
  message supersedes the question (`declined` — the message still rides the normal
  queue into the next turn), interrupt → `interrupted`, cancel → `cancelled`. All of
  it lives in the API dispatcher's ONE wait loop (`mewbo_api/ask_user.py`), which
  polls `SessionRuntime.active_run_handle`'s signals read-only. A bounded call adds
  `timed_out` to that set, owned by the same loop against an INJECTED clock.
- **The dispatcher must be what expires, not the loop.** `execution_ceiling()`
  returns `timeout_seconds + QUESTION_TIMEOUT_MARGIN_S` so the outer `wait_for`
  sits ABOVE the dispatcher's own deadline. Invert that and expiry becomes a failed
  tool call instead of the `timed_out` RESULT the model reads. Same reasoning as
  the `_WAIT_TOOL_RENDER_MARGIN_S` headroom on `check_agents`.
- **`timed_out` names BOTH continuations and no policy field chooses between
  them.** There is deliberately no `on_timeout: continue|stop` argument: proceeding
  on the most reasonable assumption and stopping to report the block are both
  already available to the model, and what was missing was an outcome readable as
  something other than a broken tool. Which one is right depends on the task, which
  the model holds and this seam does not.
- **A QUESTION IS ANSWERABLE UNTIL IT IS ANSWERED.** Timeout, supersede and process
  restart do not close it — only a real answer does, and only a terminated session
  refuses one. That makes "the user was away" recoverable rather than a lost
  decision, and it is a ONE-rule lifecycle on purpose: no expiry bookkeeping, no
  reaper, no extra states. **Corollary for every client: only `answered` settles a
  card.** The other four outcomes record that the RUN stopped waiting, which is not
  the same fact; greying a card out on them hides the affordance precisely when the
  user finally came back to use it.
- **`user_question` is the DURABLE record; the pending registry is only a
  rendezvous.** The registry dies with the run, but the event carries the questions
  AND the `call_token`, so a late answer is validated against the transcript and
  delivered as a new turn. Late answering therefore survives an API restart with no
  new store.
- **Conditional binding, not a plugin factory.** The tool exists only when the
  querying client advertised the `ask_user` capability
  (`context.client_capabilities` → `_derive_tool_grants` → `extra_session_tools`).
  Headless drives (triggers/wiki/search/channels) never advertise it, so nothing can
  block waiting for a user who is not there — the capability is why an unbounded
  default is SAFE and remains the primary guard even where a call names its own
  deadline. `SessionSpec.unattended_capabilities()` additionally strips `ask_user`
  fresh on every trigger fire, so one interactive turn cannot leak it onto a
  scheduled one. `extra_session_tools` are not forwarded by `_build_child_loop`, so
  the tool is structurally ROOT-ONLY: a sub-agent reports open questions through its
  result.
- **Down-only seam parity:** `QuestionDispatcher` mirrors `DeviceToolDispatcher`
  (register/reset/available/None-degrade); the CLI registers a blocking-modal
  dispatcher, the api an SSE+HTTP one. Answer IR is `QuestionAnswerItem`
  (`selected_indexes` XOR `text`, free text always legal — the ever-present
  "Other"); kinds are DERIVED from shape (`options==[]` ⇒ free_text), never stored.
- **`notes` is CALL-level, not per question.** One optional free-text box for the
  whole group, rendered only when the model supplies `notes_placeholder` — notes are
  prose *surrounding* the questions, so they are never validated against any one of
  them and `render_answers` appends them as a trailing line. Keeping it off
  `QuestionAnswerItem` preserves that model's 1:1 mapping onto external adapters'
  answer items.
- `modes = {"plan", "act"}` — clarifying questions matter MOST in plan mode.
  Non-terminal; `terminal_reason()` is defined explicitly on the class (the
  structural-Protocol trap above).
- `user_question`/`user_question_answered` are typed union members in `types.py`
  and intentionally ephemeral under compaction: the tool result carries the durable
  outcome into message history.
