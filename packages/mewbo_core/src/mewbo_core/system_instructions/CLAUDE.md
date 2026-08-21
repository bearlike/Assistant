> ↑ [packages/mewbo_core/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md)

# `system_instructions/` — operator-authored prompt text

Scope: `spec.py` (the document model + the render sandbox) · `store.py` /
`store_mongo.py` · `values.py` (the candidate-value reference the console renders).

An operator authors ONE Jinja template in the console (Settings → Agent), stored as
a singleton document, that branches per client (`{% if surface == "android" %}`)
and is appended to every session's system prompt.

**The store lives in core, not the app.** The CLI drives `ToolUseLoop` in-process
without `mewbo_api`, so an app-side store means CLI sessions silently get no
operator instructions at all.

## The injection seam

`ToolUseLoop._render_system_prompt` gets ONE section,
`loop.section.user_instructions`, rendered right after `project_instructions`. The
pre-rendered text lives on the loop INSTANCE (`self._user_instructions`, set once
at construction from `Orchestrator._resolve_user_instructions`), which buys three
things for free: it survives the model-escalation re-render (`_apply_model_escalation`
re-renders against the SAME instance state); it is inherited by every child, because
`_build_child_loop` forwards `user_instructions=` verbatim; and it survives
compaction, because `_compact_messages` slices `messages[1:-recent_keep]` and never
touches `messages[0]`.

**THE TRAP that shaped the design: `skill_instructions` is an overloaded,
last-write-wins slot with FIVE writers** — user `/skill` invocation
(`Orchestrator._try_skill_invocation`), `ChannelAdapter.system_context`
(`channels/routes.py`), the wiki indexer/page-writer/QA jobs (`wiki/jobs.py`), the
scg `orchestrated_runner`/`map_job` playbooks, and `StructuredResponder`. It LOOKS
like the obvious home for new system-prompt text and it is NOT: a channel or wiki
drive silently clobbers the operator's instructions the moment it runs. **Any new
system-prompt contributor gets its OWN `loop.section.*` slot** — never overload
`skill_instructions` with a second concern.

## The SSTI boundary and the undefined policy

**The SSTI boundary is load-bearing.** `PromptRegistry._env` (every engine prompt)
is non-sandboxed, `autoescape=False`, `StrictUndefined`. Operator text never
touches it: it is rendered ONCE, upstream, in a SEPARATE `INSTRUCTION_SANDBOX`
(`spec.py` — `SandboxedEnvironment`, `loader=None`, `ChainableUndefined`), and only
its OUTPUT reaches the registry, as a plain Jinja VARIABLE (`{{ user_instructions }}`).
Jinja substitutes a variable and never re-renders its contents, so operator text
containing `{{ ... }}` cannot execute a second time against the non-sandboxed env.
**Never concatenate operator/user text into a template SOURCE** — the moment it is
spliced into a string that gets `from_string()`'d, this boundary is gone.
`loader=None` is arguably more load-bearing than the sandbox itself (which only
restricts attribute access): it makes `{% include %}`/`{% import %}`/`{% extends %}`
fail closed with no filesystem to pull from, instead of merely
restricted-but-reachable.

**The undefined policy is INVERTED vs the registry, deliberately.** The registry is
`StrictUndefined` because a typo'd engine prompt must fail CI, not ship.
`INSTRUCTION_SANDBOX` is `ChainableUndefined` because an operator's template
referencing a renamed or removed field must render BLANK, not raise:
`SystemInstructionsDoc.render()`'s call site does not wrap it in try/except, and
this render sits upstream of the first LLM call, so a raise kills the turn before
the model ever runs. **Fail-soft is the whole design:** any failure ⇒ no injection +
a persisted `last_error` (`store.record_error`), never an exception into the run.

**Validation-at-definition has a deliberate EXCEPTION here.** Template SIZE
validates at definition (`_template_within_cap`, a `field_validator`) — an immutable
fact about the data. Template COMPILATION does not: it stays `validate_template()`,
an explicit method the WRITE path calls. Promoting it to a `field_validator` makes
an already-stored template that no longer parses UNLOADABLE, so the `GET` whose
entire job is showing an operator their broken template would 500 instead.

**`RenderedInstructions` is a frozen dataclass, not Pydantic** — it never crosses a
trust boundary, the documented carve-out from "the Pydantic rule stops at the
process boundary" (root `CLAUDE.md`). Returning the error ALONGSIDE the text keeps
`render()` free of hidden mutation: the caller that owns the store persists
`last_error`, not the model rendering itself.

## `InstructionContext` — a published wire contract

**`InstructionContext` is a WIRE CONTRACT, not an internal DTO.**
`describe(catalog)` walks `model_json_schema()` to GENERATE the variable reference
the console renders at `GET /api/system-instructions/variables`, so every
`Field(description=...)` is user-facing documentation. Adding a field is additive
(an unrecognized name renders blank under `ChainableUndefined`); removing or
renaming one silently breaks every operator's template.

`describe()` lives on the MODEL, not on the Flask controller: schema-walking helpers
in an HTTP adapter re-derive CORE's own schema semantics and drift the moment a
field changes shape. The `$ref`/`allOf`/`anyOf` resolution is load-bearing — an enum
field does not serialize inline the way a `str`/`bool` field does, so a naive
`prop["type"]` read of `origin` yields nothing at all. `origin` is typed as the real
`SessionOrigin` enum, not a bare `str` with a hand-written prose list, so the origin
taxonomy keeps its ONE home. Because Jinja needs primitive data, `to_render_vars()`
goes through `model_dump(mode="json")` — a bare `model_dump()` prints
`SessionOrigin.WIKI` into every operator's prompt.

**The reference documents candidate VALUES, and `closed` vs `known` is the
load-bearing distinction** (`values.py`). A bare type word ("string", "array") tells
an operator nothing they can branch on — they must guess whether the surface is
`android` or `aura-android`, whether the capability is `wiki` or `wiki_search`, and
a wrong guess fails SILENTLY (`ChainableUndefined` renders a bad comparison blank
rather than raising). So each variable carries `CandidateValues`:

- **`CLOSED` is earned ONLY by a real enum** (`origin`, whose members `describe()`
  reads out of the schema's `$defs`): a session's value is ALWAYS one of them, so an
  operator's `else` branch is genuinely unreachable.
- Everything else (`tools`/`capabilities`/`projects`/`models`/`surfaces`/
  `platforms`) is `KNOWN` — a superset claim about THIS DEPLOYMENT, never an
  exhaustive claim about a SESSION, whose set is narrower (scoped by project /
  plugins / allowlist; capabilities are only what its client advertised) and whose
  surface a brand-new client can invent. Conflating the two makes the reference LIE,
  so an operator must TEST for a value rather than assume the list is what they get.
- **Central invariant: the catalog must be a SUPERSET of what any session reports.**
  Under-report and the documented list lies about the `{{ tools }}` membership test
  an operator writes against it, which is why the tools probe deliberately includes
  DISABLED registry specs (a tool a deployment *could* turn on). An empty source
  yields `None`, never `()`: "known here: (nothing)" reads as *this variable is
  always empty* when it means *this deployment did not tell us*.

**`InstructionValueCatalog` is PLAIN DATA with ZERO I/O — the app INJECTS it.** Core
never reads a registry, a config file or the LLM proxy to describe itself; the same
rule that keeps `TriggerSpec` taking the clock as a method ARG. It defaults only the
two vocabularies core owns (`KNOWN_SURFACES`, `KNOWN_PLATFORMS`); deployment-specific
sources are resolved at the app edge by `InstructionValueSources`. **Each field
DECLARES ITS OWN SOURCE inline** via `Field(json_schema_extra={"x-values": "tools"})`
— there is deliberately NO second name→source lookup table, because a table drifts
the moment a field is added or renamed. An unknown/typo'd `x-values` degrades to a
plain row rather than raising into the operator's settings page.

**`InstructionContext.tools` must include SESSION tools, via
`SessionToolRegistry.ids_for`.** Carrying `ToolRegistry` specs only leaves every
session tool the agent genuinely holds (`wiki_*`, `scg_*`, `submit_widget`,
`schedule_trigger`) absent, so `'wiki_search' in tools` silently renders False and a
template branching on a product tool can never fire. It is registry specs ∪ session
tools ∪ `extra_session_tools`, and the session half resolves through `ids_for()` —
the SAME selection `build_for()` makes (both gates live in `ids_for`; `build_for`
calls it and then instantiates). **A re-implemented copy of the selection would
inevitably drift from what the agent actually gets.**

**Deliberately omitted:** the internals the loop injects for ITSELF
(`spawn_agent`/`spawn_agents`, `update_todos`, `exit_plan_mode`, `activate_skill`).
They are decided INSIDE `ToolUseLoop`, downstream of this resolution, and several
are mode-dependent (`update_todos` is act-mode, `exit_plan_mode` is plan-mode), so
naming them trades one lie for another. `tool_search` is NOT one of them and IS
included: despite being loop machinery it is a genuine `always_load` registry spec
arriving through `tool_specs` like any other tool. The field's `description` states
the omission outright — **honesty over completeness**; if the contents change, that
line changes in the same commit.

**Three traps in the value catalog. Do NOT "clean them up".**

1. **`home-assistant` IS a live surface** — `apps/mewbo_ha_conversation/api.py`
   stamps `X-Mewbo-Surface: home-assistant`, and a grep sweep that misses it reads
   the entry as a lie in a docstring. `KNOWN_SURFACES` sits beside `MOBILE_SURFACES`
   in `session_provenance.py` so the surface vocabulary has one home and every entry
   traces to a real stamp site. It is a VOCABULARY, not a validator — `surface` stays
   a plain `str` so a new client that stamps its own string keeps working.
2. **The capability row is COMPUTED, and `capabilities.py`'s registry is NOT the
   thing to compute it from.** `capabilities.py` does now hold a closed `Capability`
   `Literal` + `ALL_CAPABILITIES`, but that is the FIRST-PARTY set — the one the
   first-party clients advertise and the first-party plugins require, closed so a
   tripwire can pin the TypeScript and Kotlin mirrors to it. It is deliberately not
   the answer here: a third-party plugin ships capability ids nothing in core has
   ever heard of, so the only honest truth for an OPERATOR is still the union of
   `requires-capabilities` across installed plugin manifests and AgentDefs. That is
   why the app computes it, and why a plugin shipping a new capability appears in
   the operator's list the moment it is installed. Swapping this for
   `ALL_CAPABILITIES` would silently drop every third-party id from the row.
   It resolves to `scg`/`stlite`/`wiki` — but ONLY once `mewbo_graph` has been
   imported, since the wiki/scg plugin roots arrive via the down-only
   `register_builtin_root` push. A lean install legitimately reports just `stlite`.
3. **`project` is `None` for EVERY managed-worktree session**, not only an unscoped
   one: `TraceProvenance._facets_from_context` routes a `managed:<uuid>` value to
   the `worktree` facet and never into `metadata["project"]`.

## Prompt cache, and the one security line

`llm.py` sets ONE explicit `cache_control` breakpoint at the system message
(`cache_control_injection_points`, role `system`, `type: ephemeral`) whenever the
model supports prompt caching, so the WHOLE rendered system prompt is a single
cached block and `user_instructions` sits inside it at a fixed position. A template
that renders differently per session — branching on volatile facts like
`session_id`/`agent_id` rather than session-invariant ones like
`surface`/`capabilities`/`platform` — means no two sessions ever share that cached
prefix. **Keep operator templates branching on session-invariant facts.**

**`{{ tools }}` is the cache-hostile one the value catalog actively INVITES** (the
console ships tool-id chips + autocompletion over it): **TEST MEMBERSHIP, NEVER
RENDER THE LIST.** `{% if 'wiki_search' in tools %}` collapses a volatile list to a
stable boolean and caches fine; `{{ tools | join(', ') }}` bakes the list's exact
contents and ORDER into the single cached system block, and that string moves
whenever an MCP server flaps or a caller varies `context.mcp_tools` between turns of
the SAME session — busting the breakpoint for a session against itself, not merely
across sessions.

**SECURITY: operator-only, never agent-writable.** No SessionTool or MCP tool reads
or writes this document — an agent able to rewrite its own system prompt is a
privilege-escalation path. Every route requires `X-API-KEY`
(`apps/mewbo_api/src/mewbo_api/system_instructions/routes.py`). Same line the
codebase draws refusing LLM-authored trigger scripts (`triggers/CLAUDE.md`).
