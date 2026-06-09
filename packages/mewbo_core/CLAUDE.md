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
- `hypervisor.py:AgentHypervisor` is the only place sub-agents register,
  cancel, get budget warnings, and resolve to their `AgentResult`.
- `spawn_agent.py` is the bridge between the LLM-facing tool schema
  and the hypervisor. Schema fields stay backwards-compatible —
  removing a field breaks every running agent that already learned
  it.
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

## Skills, plugins, and the agent registry

Three discovery surfaces, all driven from this package:

- `skills.py` — Agent Skills standard discovery (`~/.claude/skills/`
  and `.claude/skills/`). Catalog injected into the system prompt for
  auto-invocation via `activate_skill`. User `/skill-name` is detected
  in `Orchestrator` and rendered via `skill_instructions`.
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
and `.start_async` share one `_prepare()` builder (DRY). `start_async` exists so
`/v1/structured` is async-recoverable: `SessionRuntime.start_async` now MINTS and
RETURNS a storeless per-run `run_id = "<session_id>:r<seq>"` (was `bool`; `""`
still means "refused, busy" so `if not started:` callers are unbroken). The
session transcript IS the run record — no parallel run store.

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
requires a `str`).

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
