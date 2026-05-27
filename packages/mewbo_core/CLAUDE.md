# Mewbo Core — Engine Guidance

Scope: `packages/mewbo_core/src/mewbo_core/` — the async tool-use engine,
hypervisor, session runtime, hooks, plugins, and the built-in plugin
suite (including the wiki tools). The root `CLAUDE.md` covers the
architectural invariants; this file captures the engineering decisions
that aren't obvious from the code in this package.

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

## Orchestration invariants — read root CLAUDE.md first

The full list lives in the root `CLAUDE.md` under "Orchestration
invariants". Don't restate it here. Highlights specific to this
package:

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
  (per-run; holds the retry budget + circuit breaker + knobs, `from_config()`,
  injected `invoke`/`emit`/`compact`; its methods incl. `classify`/`backoff`
  describe the behaviour over that state) and `DoomLoopGuard` (no-progress
  detection). `tool_use_loop` is a thin driver — don't reinline retries into
  the loop. See the root CLAUDE.md "LLM call resilience" invariant for the
  decision model and the append-after-success idempotency rule. A cross-model
  fallback emits a separate `llm_fallback` event carrying `to_model`;
  `token_budget` reads the successful model from THAT event (not `llm_retry`), so
  `models_used` captures fallbacks — rename the event/field in lockstep with that
  reader or fallback models silently vanish from usage.
- `session_runtime.py:SessionRuntime` is the only place a session is
  created, resumed, forked, or archived. Channels, the API, and the
  CLI all go through it — no parallel session implementations.

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

The wiki finalize tool uses an `on_session_end` hook indirectly: the
completion callback in `channels/routes.py` reads `source_platform`
from a transcript context event and dispatches a reply via the channel
adapter. Don't add session-end behavior inline in tools — put it in a
hook so it survives unrelated tool refactors.

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
