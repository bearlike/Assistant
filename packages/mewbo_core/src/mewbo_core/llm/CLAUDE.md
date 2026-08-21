> ↑ [packages/mewbo_core/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md)

# `llm/` — provider calls, the resilience ladder, prompt assembly

Scope: `llm.py` (client construction) · `llm_resilience.py` (retry, fallback,
doom-loop detection, the write-progress signal) · `model_variants.py` ·
`prompt_registry.py`.

**This package is legal only because two edges stay severed.** `config.py` must not
import its retry defaults from `llm_resilience`, and `classes.py` must not import
`build_chat_model` at module top — with `llm.py` importing `config`, either edge
puts the client and the ladder on opposite sides of the package root and makes this
package a cycle. The defaults live in `contracts/defaults.py`; `classes.py` imports
the builder at its single call site.

## LLM client — LiteLLM is canonical

`llm.py:build_chat_model()` constructs a LangChain `ChatLiteLLM`. LiteLLM is the
only LLM client in the project — chat, embeddings, reranking. No
`langchain-openai`, `langchain-anthropic`, or any provider-specific client, for
three reasons: one OpenAI-compatible surface routes by model id; the proxy gives
operators a single auth point + per-key model allowlists + cost accounting; and
provider SDK drift breaks transitive deps every few weeks. **Before adding a
`langchain-<provider>` dependency, check whether LiteLLM already does the job.**

`LLMConfig.proxy_model_prefix` (default `"openai"`) is prepended to model names so
LiteLLM routes through the proxy at `llm.api_base` instead of dispatching to a
provider SDK. Same rule for embedding model names
(`mewbo_graph/wiki/embedder.py`).

**Token usage is a NORMALIZATION concern.** litellm can strand the real counts in
`response._hidden_params['usage']` instead of `response.usage` — streaming chunks
have usage stripped and re-attached only there, and some proxy/non-streaming shapes
never set the field — so `langchain-litellm` builds an empty `usage_metadata` and
every `llm_call_end` (hence `/usage`) reads zero. `_UsageNormalizingLiteLLM` wraps
the client and surfaces `_hidden_params['usage']` back onto `.usage` for both
paths. It is feature-detecting (acts only when usage is absent/all-zero), so it
self-disables once litellm#12233 / litellm#17476 land — remove it then. At the
pinned stack langchain-core also disables streaming when `streaming` is in
`model_fields_set`, so `bound.astream()` falls back to `ainvoke`; the wrapper covers
both regardless.

**Cross-model tool-calling is a REQUEST/RESPONSE NORMALIZATION concern — never
patch it in the loop.** If a provider's tool call doesn't appear in
`response.tool_calls`, the fix lives at the LiteLLM / response-parse seam or the
request config, NOT in detecting a model's text format (e.g. Gemini's
`default_api:<tool>{...}`) in `tool_use_loop` and re-prompting. LiteLLM already maps
provider-native calls → OpenAI `tool_calls` (Gemini `functionCall` via
`VertexGeminiConfig._transform_parts`), and routing `openai/<model>` to the proxy
does **not** bypass that transform. When a call looks "missing":

1. Check the other structured slots on the `AIMessage` —
   `additional_kwargs.tool_calls` / `additional_kwargs.function_call` — and
   normalize them into `.tool_calls`.
2. Ensure the *request* forces structured calls (tools declared + `tool_choice` /
   `functionCallingConfig`) so the provider returns a `functionCall` part instead
   of narrating it as text.

Tool-call ids can carry an embedded `__thought__<signature>` for round-tripping
(LiteLLM's `THOUGHT_SIGNATURE_SEPARATOR`), so a multi-turn "leak" is a
serialization bug at the adapter, never a model quirk to detect-and-reprompt.

## Resilience — the decision model

`llm_resilience.py` models retry/fallback as atomic objects: `RetryStrategy`
(per-run; the retry budget + circuit breaker + `_pinned_model` + knobs,
`from_config()`, injected `invoke`/`emit`/`compact`) and `DoomLoopGuard`
(no-progress detection — **progress-aware**: halts only on identical tool input AND
identical result across N turns, fed by `record_result`). `tool_use_loop` is a thin
driver: don't reinline retries, and the append-after-success idempotency rule is
this section's, not the loop's.

**Doom-loop exemption is DECLARATION-DRIVEN and PER-CALL, not a hardcoded name
set.** `DoomLoopGuard.poll_rules` merges a small built-in seed
(`DOOM_LOOP_EXEMPT_TOOLS`, today just `check_agents`) with tool-declared rules read
via `getattr`: a registry `ToolSpec.poll`/`poll_when_args`, or a SessionTool's
`poll_class`/`poll_when_args` (deliberately not a Protocol member — read
defensively). `ToolUseLoop._poll_class_rules` collects these off every bound spec
and session tool each run and feeds `DoomLoopGuard.from_config(extra_poll_rules=…)`,
so the guard evaluates PER CALL against that call's arguments — `agentic_search`
declares `poll_when_args=("run_id",)`, so polling an existing run is exempt while a
fresh query still counts. A third poll-class tool costs a declaration on its own
spec, never a core edit.

**Policy:** a model gets **2 attempts** (`agent.llm_call_retries` = 1 try + 1
retry), then the run escalates down `[primary, *fallback_models]` — never a 3rd
attempt on a dead model. **The autoheal/rescue model is simply the last entry of
the fallback ladder** (no dedicated key). Escalation is **sticky**: a non-primary
model that wins is pinned (`_pinned_model` reorders the chain) for the rest of the
run, so a dead primary is never re-probed turn after turn. Cross-model fallback
lives HERE, **not** in LiteLLM Router (a deployment-pool LB — orthogonal, and
routing through it would lose the compact-then-retry seam). A switch emits an
`llm_fallback` event carrying `to_model` + `sticky` + `reason`; **`token_budget`
reads the successful model from THAT event, not `llm_retry`**, so keep that reader
in lockstep or fallbacks vanish from `models_used`.

- **The ladder is only a ladder when fallback is ENABLED, and it defaults to OFF.**
  `FallbackConfig.enabled` is `False` by default, so a stock deployment runs ONE
  rung and every rule below about advancing the chain is inert. The operational tell
  is a failure naming a single model — `LLM call failed on all models (<the one
  primary>)` — which reads like a provider-wide outage but is a model-entitlement or
  quota error on the only model ever tried. **`fallback.models` being populated is
  NOT sufficient; the switch gates it.**
- **THE BUDGET LAW: `turn_deadline` must exceed `llm_call_timeout ×
  llm_call_retries` plus one more attempt, or the ladder below rung 1 is
  UNREACHABLE.** The deadline bounds one logical call across every retry AND every
  fallback, so a primary rung sized to consume the whole budget means the chain
  advances, finds the clock spent, and stops. `120 × 2` against a `240` deadline is
  the shape that fails, with `_deadline_reached` a strict `>` so rung 2 dies on the
  backoff jitter alone — and raising `llm_call_timeout` without raising
  `turn_deadline` re-creates it silently. `AgentConfig` warns at config load rather
  than raising: the single-model path still serves traffic, and a hard failure would
  take down a running deployment over a knob it can keep working with.
- **The deadline is checked BEFORE the chain advances, not only inside the attempt
  loop.** Per-attempt-only checking lets a spent budget still emit an `llm_fallback`
  and append to `tried` for a model it never called, so the exhaustion error blames
  a healthy model and reads as a provider-wide outage. **`models_tried` must only
  ever name models actually invoked.** Reading a failure back: the session summary's
  `retries_exhausted` is the *advance* reason (why the previous model was left), NOT
  the cause of death, and `models_tried` is the deduped union of every
  `llm_fallback`'s `from_model` and `to_model` across the whole session. Neither is
  a ladder transcript.
- **The liveness leg is re-armed per ATTEMPT, not per turn.** It exists for the
  wedge `asyncio.wait_for` cannot cancel (a provider read that never reaches a
  cancellation point), so measuring from the turn's first attempt makes a healthy
  two-attempt retry report as one stalled call and buries the signal. Keep
  `agent.llm_call_liveness_s` above `llm_call_timeout` for the same reason.

**Exhaustion exits by RAISING, and it is the one terminal outside the turn
automaton.** Every other exit sets `state.done`/`done_reason`;
`LlmResilienceExhausted` propagates out of `ToolUseLoop.run()`, whose main `try` has
only a `finally` and no `except`. Two things depend on that:
`RetryPolicy._drive_with_retry` (`agents/spawn_agent.py`) drives the child retry off
the raise, and the loop's `finally` marks the handle `failed` because `state.done`
is still `False`. **Converting it to a return is not a refactor — it silently turns
a dead run into a completed one.**

## Progress signalling is observe-only

`WriteProgressSignal` (sibling to `DoomLoopGuard`) is OBSERVE-ONLY telemetry, never
a verdict fed back into the flagged agent — progress iff a WRITE-tier tool executed;
two-gate arming (`capability_mode ∈ {execute, all}` AND a write tool bound) so
read-only researchers are never flagged. Crossing the threshold ALWAYS emits a
`write_progress_signal` event (agent_id/depth/step/steps_since_write/threshold); the
optional reminder (`reminder_enabled`, default OFF) injects ONLY a criterion-blind
objective restatement — the task goal, nothing about writes, steps or the signal
itself. **Feeding a detector's verdict back into the same agent's context teaches it
the tell**, so that path is closed by construction. Session tools miss `specs_map`
and count as non-progress by construction.

## Tool schemas drop upper bounds (`sanitize_tool_schema`)

`maxLength`, `maxItems` and `maxProperties` are stripped from every tool schema at
the `specs_to_langchain_tools` funnel. **This looks like lost validation and is not
— do not "restore" them.** A backend that constrains decoding with a grammar
(llama.cpp/Ollama, anything compiling JSON Schema to GBNF) expands an upper bound
into that many literal repetitions and inlines every `$ref` while doing it, so in a
RECURSIVE schema the two multiply. `present_ui`, whose `Card.children` is a `oneOf`
over eleven component types including `Card`, made Ollama reject a whole 17-tool
request with `400 Failed to initialize samplers: failed to parse grammar`.

A size threshold would not fix it: the blow-up scales with nesting depth as well as
with the bound, so any limit safe at one depth is fatal one level down. Dropping is
strictly permissive — it can never truncate or reject a valid argument, only stop
advertising a ceiling — and tool arguments are still validated against the ORIGINAL
schema in `EmitStructuredResponseTool.handle`. Lower bounds stay. Where a ceiling is
genuinely load-bearing for generation, say it in the field's `description`, which is
what `ask_user` already does with "2-4 distinct choices".

## Prompt registry (`prompt_registry.py`)

Every engine prompt has ONE schema'd home. `PromptRegistry` (atomic class) loads
`prompts/registry/*.yaml` — one file per owning module (`compact`, `loop`,
`structured`, `planning`/`assembly`, `catalog`, `common`, `spawn`, `title`, plus
`files` = `template_file` pointers to standalone `*.txt`) — into `PromptEntry`s and
renders them through one Jinja2 dialect. `get_system_prompt()` is a thin shim;
`get_prompt_registry()` is the singleton.

- **templated ⟺ `variables` non-empty.** A no-variable entry is returned VERBATIM
  and NEVER Jinja-parsed, which is why a static prompt full of `{`/`{{` (the
  compaction `<summary>` block, the structured force-emit JSON) survives untouched.
  Declaring a var flips it to Jinja (`StrictUndefined` — a missing var RAISES).
  `validate_all()` (the CI gate, run in the suite) asserts declared `variables` ==
  the template's Jinja-AST vars, so a typo'd slot fails the build, not a live render.
- **`render()` never strips.** Byte-equality is controlled by the YAML block
  scalar: `|` keeps one trailing newline, `|-` strips all. Every migrated prompt has
  a golden test asserting `render(id, **vars)` byte-equals the original literal.
  Adding or editing a prompt: write that test first, pick the scalar to match.
- **`render_jinja_prompt()` stays tolerant — NOT registry-routed.** Its HA callers
  rely on a missing var rendering blank; the registry's `StrictUndefined` would
  raise. HA prompts are registry-INVENTORIED (`file.homeassistant-*`) but rendered
  on the tolerant env, and making them strict is a deliberate behaviour change.
- **Layered override = cross-model convergence.** `render(id, model=, scenario=,
  **vars)` resolves scenario (exact) > model (longest prefix) > base, with
  `mode: replace|append|prepend`. A model that diverges from the orchestration
  contract gets a small declared DELTA on the shared base, never a forked code path.
  Caveman compaction is the live `scenario`; the `gemma-` append on
  `loop.depth.root` is the live `model` one — BEHAVIOURAL only (delegate-less /
  terminate), never a tool-call-format fix, which stays a normalization concern at
  the LiteLLM seam. Per-model variants reach EVERY prompt: the loop's per-step sites
  plus compact (`get_compact_prompt(model=)`), title and the structured runners all
  pass `model=`.
- **Sticky model escalation re-variants the prompt AND the tool.** `tool_use_loop`
  tracks `self._active_model` (configured primary, or the sticky-pinned escalated
  model). Every per-step render + `_configured_edit_tool_id` reads it;
  `_apply_model_escalation` re-renders `messages[0]` and re-derives the edit-tool
  variant against the escalated model when it changes, so the heal is behavioural,
  not just a model swap. `_bind_model` and the resilience reuse-guard key off
  `_active_model` too.
- **Per-model TOOL variants are controllable DATA.** `prompts/model_variants.yaml`
  (sibling of `registry/`) maps a model-name PREFIX → edit-tool variant; loaded by
  `model_variants.py:ModelVariantRegistry` (Pydantic `extra=forbid`, longest-prefix
  wins, `validate_all` lint gate). `llm.model_prefers_structured_patch` reads it,
  with config `llm.structured_patch_models` overriding on top and `defaults.edit_tool`
  as the conservative fallback. The map PAIRS with the prompt overrides by the SHARED
  model-prefix key.
- **Two down-only seams** (mirroring `plugins.register_builtin_root`):
  `register_prompt_root(pkg, subdir)` (a library contributes its own
  `registry/*.yaml`) and `register_prompt_modifier(...)`. Core never imports up to
  find them.
- **Footguns:** `prompt_registry` imports `common.get_logger`, so `common.py`'s
  registry calls are LAZY in-function (import cycle). `ruff` cannot lint `.yaml` (it
  parses as Python) — `validate_all()` is the YAML gate, not ruff.
