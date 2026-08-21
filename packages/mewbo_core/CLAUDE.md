> ↑ [root /CLAUDE.md](../../CLAUDE.md) · children:
> [contracts/](src/mewbo_core/contracts/CLAUDE.md) ·
> [llm/](src/mewbo_core/llm/CLAUDE.md) ·
> [agents/](src/mewbo_core/agents/CLAUDE.md) ·
> [tooling/](src/mewbo_core/tooling/CLAUDE.md) ·
> [session/](src/mewbo_core/session/CLAUDE.md) ·
> [loop/](src/mewbo_core/loop/CLAUDE.md) ·
> [workspaces/](src/mewbo_core/workspaces/CLAUDE.md) ·
> [secrets/](src/mewbo_core/secrets/CLAUDE.md) ·
> [triggers/](src/mewbo_core/triggers/CLAUDE.md) ·
> [system_instructions/](src/mewbo_core/system_instructions/CLAUDE.md) ·
> [builtin_plugins/](src/mewbo_core/builtin_plugins/CLAUDE.md)

# Mewbo Core — Engine Guidance

Scope: `packages/mewbo_core/src/mewbo_core/` — the async tool-use engine,
hypervisor, session runtime, hooks, plugins, and the built-in plugin suite. This
file holds only what spans packages; everything area-specific lives one level
down, and **a statement that exists here AND in a child is a bug, not
redundancy.**

This is the single source of LLM-driven orchestration for every Mewbo interface.
CLI, REST API, web console, Home Assistant and Nextcloud Talk all import from
here. If a behavior belongs in "the assistant itself" (as opposed to "the API
server" or "the CLI display"), it lives in this package.

**Layering (see root CLAUDE.md → "Monorepo layering"):** core is the lean base of
the dependency DAG — it imports DOWN from nothing and must never import an app or
a capability library; heavy/optional deps go behind a `mewbo-core[...]` extra. A
library above core contributes its plugins via `plugins.register_builtin_root` —
a push, so core never imports up to find them.

## Where doctrine lives

**Read the child `CLAUDE.md` for the package you are editing before you edit it.**

| Package | Owns |
|---|---|
| `contracts/` | the zero-core-import property config depends on; bounded failure emission; diff arithmetic |
| `llm/` | LiteLLM as the one client; the resilience ladder and THE BUDGET LAW; the prompt registry |
| `agents/` | admission; the six-member lifecycle; delegation contracts; Communication Units; privilege attenuation |
| `tooling/` | the SessionTool declaration law; three-state `allowed_tools`; deferred tool schemas; todos; ask-user |
| `session/` | the durable record and its projections; compaction; origin and trace provenance; hard termination |
| `loop/` | the turn automaton; the safe turn boundary; the completion seam; binding; session runtime and status |
| `workspaces/` | name → directory resolution; the repository registry; workspace containment |
| `secrets/` | the store-composition pattern; key handling |
| `triggers/` | the durable wake contract; data-owned kind classes; admission ceilings |
| `system_instructions/` | operator-authored prompt text and the SSTI boundary |
| `builtin_plugins/` | the zero-app-import first-party suites |

Seven modules stay at this root, each for a structural reason rather than
tidiness. `common` and `config` are the spine every package imports — a bucket
holding them would be imported by all the others and would mean nothing beyond
"the root, renamed". `classes` is read by four packages plus two root modules, so
filing it under any one of them would create a root → subpackage edge. `hooks` is
a cross-cutting extension seam with five consumers, and `permissions` is pinned
to it. `components` and `capabilities` are small shared primitives with dependents
spread across four or more packages.

## Three automata, not one

The engine is routinely described as "a conversation state machine". **There are
three, plus several result vocabularies, and the projections between them are
where the interesting failures live.**

| | Automaton | Declared in | Owner |
|---|---|---|---|
| **A** | agent lifecycle — `AgentStatus`, 6 members, closed, absorbing terminals | `agents/hypervisor.py` | `agents/` |
| **B** | the turn — `OrchestrationState` + `done_reason`, an untyped `str` | `classes.py`, driven by `loop/tool_use_loop.py` | `loop/` |
| **C** | session status — `SessionStatus`, 10 members, derived and never stored | `loop/session_runtime.py` | `loop/` |

They differ in the one way that matters when you add a state: **what it costs to
add one.** A costs five surfaces (it is closed, and hand-mirrored into three
clients behind a tripwire test). C costs a table row and silently reclassifies
history. **B costs nothing at all** — which is why it is the drift surface, and
why a new `done_reason` is the change most likely to go unnoticed by a consumer
that should have cared.

The PROJECTIONS between them are the seams to be careful at, and each is
documented where it lives: A ← B is `OrchestrationState.terminal_status()`
(`loop/`), C ← B is `_STATUS_BY_DONE_REASON` (`loop/`), and the parent-facing
result vocabularies are `agents/`.

**There is no addressable state INDEX.** The turn counter is a local variable in
`run()`; `AgentContext` carries none. That is why `provenance.step` is always
`None` and why "which transition are we on" can only be answered by replaying the
event log. Recorded so nobody hunts for a counter that was never built.

## Package boundaries

Three laws hold the package graph acyclic. All three are invisible in a diff,
which is why they are written down rather than left to review.

- **Every subpackage `__init__.py` stays EMPTY of code — a docstring and nothing
  else.** A convenience re-export makes importing ONE module in a package execute
  ALL of them, which changes what module-level side effects fire and in what
  order. The measured instance: `llm.py` imports `litellm.utils`, whose package
  body calls `load_dotenv()` — reading a file off disk and mutating `os.environ`
  at import time. `model_variants` and `prompt_registry` do not pull litellm
  today; under a re-exporting `llm/__init__.py` they both would. The question to
  ask at the next judgement call is not "does this re-enter config" but **"what do
  this module's transitive module BODIES do"** — sockets, dotenv, subprocesses,
  singletons.
- **Nothing in `contracts/` may gain a module-top core import.** It is the
  property `config.py` depends on; see `contracts/CLAUDE.md`.
- **`tests/test_core_import_graph.py` is the tripwire, and it is meant to fail.**
  It DERIVES the load-bearing guard set rather than hand-maintaining one: promote
  each `TYPE_CHECKING`/function-local edge to a runtime edge, one at a time, and
  keep the ones that make the graph cyclic. `DECLARED_CYCLE_BREAKERS` and
  `KNOWN_PACKAGE_CYCLES` there are the authority — **read them rather than any
  prose summary, including this one.** Two things about that test:
  - `KNOWN_PACKAGE_CYCLES` is an EXACT set, currently empty, so both directions
    are loud: a new package cycle fails, and so does a listed one silently
    disappearing. A guard that stops being load-bearing does NOT get removed from
    the source — removing it is a behaviour change, not a cleanup — it just stops
    being listed.
  - It asserts separately that every `mewbo_core.*` import names a real module. An
    edge to a nonexistent module is dropped before the cycle search runs, and a
    missing edge can only make a graph MORE acyclic — so a package can be
    unimportable while the structural test reports the structure is fine. **A
    reference to nothing and a reference in a circle are different failures, and
    only one shows up in the shape of the graph.**

A function-local import is NOT a general-purpose escape hatch. It is safe only
when the imported module's import-time side effects cannot re-enter the caller,
and that has to be argued per site — the comment at the `build_chat_model` call
site in `classes.py` is what one of those arguments looks like. The lazy-Mongo
store imports are the pattern worth recognising: each Mongo driver subclasses its
base, so the base can only reach it at call time, AND the same deferral is what
keeps a `storage.driver=json` deployment from importing `pymongo` at all. Two
reasons, one import.

## Capability gating

`capabilities.py` defines the capability surface. A session declares its
capabilities via a `client_capabilities` context event written at session-creation
time. Tools and agent definitions can be gated on specific capabilities.

**The registry is a closed `Capability` `Literal` + `ALL_CAPABILITIES`, mirroring
`AgentStatus`'s shape for the same reason** — it is a contract shared across three
languages, hand-mirrored into each client behind a tripwire. A `Literal` rather
than an enum because every consumer wants the bare string (a comma-joined header, a
JSON manifest, a set membership test), so mypy gets a closed set at annotated
boundaries while all of those call sites keep working verbatim. `StrEnum` needs
3.11 and this package declares `>=3.10`; the 3.10-safe `(str, Enum)` renders as
`Capability.ASK_USER` inside an f-string, which would corrupt a header silently.

Adding one: (1) add it to `Capability` AND `ALL_CAPABILITIES` in `capabilities.py`,
with a docstring saying what the client is CLAIMING; (2) add it to the client
mirrors that can service it — `apps/mewbo_console/src/api/capabilities.ts` and
Aura's `AuthInterceptor` companion object; (3) have the consumer (the agent
definition or tool) filter on it. `tests/test_capability_registry.py` fails if a
mirror names an id the registry does not, if a plugin manifest requires an unknown
one, or if Aura declares a constant it never puts on the wire.

**The registry closes the FIRST-PARTY set, and is NOT a wire validator.**
`parse_capability_header` KEEPS an unrecognised id rather than dropping it, because
a third-party plugin legitimately ships its own and the operator-facing list is
computed from installed MANIFESTS, never from `ALL_CAPABILITIES`
(`system_instructions/CLAUDE.md` trap 2). Filtering to the registry there would
silently disable every third-party gate, and a newer client talking to an older
server would lose features with nothing logged as an error.

**The header has exactly one parse and one serialize.** `parse_capability_header` /
`serialize_capabilities` own the comma format; the persisted list is sorted and
deduped, so two clients advertising the same set store the same payload and header
ORDER carries no meaning (every consumer uses set semantics).

**Capability gating has TWO enforcement surfaces — gate BOTH.** A capability gates
(a) the AgentDef/skill CATALOGS via `filter_by_capabilities`, AND (b) the per-agent
SESSION-TOOL build. These are independent gates. When only (a) consulted
capabilities and `SessionToolRegistry.build_for` selected tools **purely by
`allowed_tools`**, a capability-gated plugin SessionTool was invisible to a session
that got the capability by RUNTIME GRANT — and it bit only on re-engagement,
because the original run worked by *spawning* sub-agents whose AgentDef lists the
tools while the ROOT agent never had them. A plugin's `requires_capabilities` is
threaded onto its `SessionToolFactory` (via
`load_entry(..., requires_capabilities=)`, fed from `pc.manifest` in the
orchestrator's `fan_out.components` loop) and `build_for(session_capabilities=)`
unions an allowlist gate with a capability gate. **Rule:** any new gate deciding
"can this agent see X" must read `session_capabilities`, not just `allowed_tools` —
a runtime grant reaches the former, never the latter.

**`capability_mode` is a third visibility axis — one shared predicate on BOTH
surfaces.** `SpawnAgentTask.capability_mode` (`read_only`/`execute`/`all`, default
`all`) is a coarse privilege ceiling layered UNDER `allowed_tools`/`denied_tools`.
Both surfaces consult the ONE `capability_mode_admits()` predicate
(`tool_registry.py`): `filter_specs` for registry tools, `ids_for`/`build_for` for
session tools — and the session gate runs over the FINAL selected id set, so an
`allowed_tools` entry cannot resurrect a write-tier tool under `read_only`. Tier
resolution: `ToolSpec.capability` (undeclared is safe-denied from `read_only`;
`read_only=True` specs auto-classify `read`) and `SessionToolFactory.capability`
(defaults `execute` — session tools are actions — so `read_only` admits zero
session tools unless a factory declares `read`). The effective mode narrows
monotonically at `AgentContext.child()`; a descendant can never re-widen an
ancestor's ceiling. **Rule: a new tool-visibility axis ships on BOTH surfaces
through one shared predicate, or it doesn't ship.**

**Runtime grants — the seam exists, no provider uses it.** A capability *can* be
granted by a RUNTIME predicate instead of a client advertisement:
`register_session_capability_provider` is a down-only push seam (mirroring
`plugins.register_builtin_root`) that an optional library above core uses to grant
a capability when a live condition holds. `Orchestrator._session_capabilities` is
the single read-point — it unions `augment_session_capabilities(...)` over the
advertised set, so a grant lands in ONE seam and flows through the unchanged
`requires-capabilities` gate. Providers are best-effort (a raising predicate is
logged and skipped). **No library registers a provider today, and the reason is
the anti-pattern to avoid:** a blanket "grant it whenever the feature is enabled"
predicate sees only the advertised cap tuple, so it cannot distinguish a search
surface from a bare `POST /api/sessions` session — the removed `scg` provider
granted `scg` to the latter, binding 12 unused tool schemas per LLM call and
mis-tagging its provenance `origin:search`. Keep the seam for a capability that
genuinely needs a runtime predicate.

## Hooks

`hooks.py:HookManager` runs lifecycle hooks. Three lifecycle points:
`on_session_start`, `on_session_end`, `on_compact`. Two hook types: `"command"`
(shell subprocess) and `"http"` (fire-and-forget POST). All invocations are
try/excepted — a failing hook logs a warning and never blocks the session.

**Command hooks are deliberately NOT confined by the shell sandbox.** Every other
`subprocess` spawn we own carries a Landlock scope derived from the session's
active project (`mewbo_tools/CLAUDE.md` → "Shell sandbox"); a hook's `shell=True`
command does not. The trust relationship is what differs: a hook command is
authored by the OPERATOR in `hooks.json` or a plugin manifest, not generated by a
model mid-turn. An operator who writes a hook that copies a build artifact out of
the workspace, notifies an external system, or reads a credential file is
expressing intent, and confining it to the active project would break exactly the
integrations hooks exist to enable — while giving nothing back, since the same
operator chooses the deny list. **The line to hold: model-authored command text is
confined, operator-authored command text is not.** A hook whose command is
templated from model output belongs on the confined side.

**A session-end hook may return a typed `OutcomeAssertion`** (`reason` + bounded
`detail` + a manager-stamped `source`) to assert an unmet purpose the loop itself
cannot see — the ONE sanctioned place a hook feeds a factual note back into a
session's outcome. `HookManager.run_on_session_end` collects every hook's return
value; a hook returning `None` asserts nothing (absence stays absence — never
coerced into a claim), and a non-`OutcomeAssertion` return is logged and ignored.
`Orchestrator._record_outcome_assertions` writes it onto the transcript as an
`outcome_assertion` event; `summarize_session` reads it back and may only PROMOTE
an already-`completed` status to `unmet_goal` — it never overwrites a
`failed`/`cancelled`/`blocked` status.

**That promotion is not inert, so assert it only when it is true.** An
`unmet_goal` status triggers one automatic re-invocation of the session
(`loop/CLAUDE.md` → "Automatic one-shot re-drive"), so a hook asserting an unmet
purpose it cannot substantiate spends a real run rather than annotating a row.
Returning `None` costs nothing; a claim costs whatever acting on it costs.

**Fire-and-forget is not the same as unobservable.** Every background hook — the
http POST at any lifecycle point, and the `on_event` command subprocess — is
submitted to `HOOK_DISPATCH` (`hooks.py:HookDispatch`), which holds the daemon
threads so a caller can `wait(timeout)` on them. Hot-path callers ignore the
handle and behave exactly as before; the point is that "has it landed" now has an
answer other than sleeping and hoping the thread won, which is a RACE rather than
a slow caller — on a loaded box the observation is WRONG, not late. A new
background dispatch goes through `HOOK_DISPATCH.submit`, never a bare
`threading.Thread`.

`on_event` is a fourth slot fired from `append_event` on BOTH store backends (the
universal choke-point, a superset of the orchestrator's event_logger). Hooks
registered here are **fire-and-forget in a daemon thread even for the command
factory** — unlike `on_session_end` (sync, once) — because they run on the append
hot path. The API wires one bus observer here at startup so the `SessionEventBus`
and command/http hooks share a single publish choke-point.

Don't add session-end behavior inline in tools — put it in a hook so it survives
unrelated tool refactors. The wiki finalize tool uses an `on_session_end` hook
indirectly: the completion callback in `channels/routes.py` reads
`source_platform` from a transcript context event and dispatches a reply via the
channel adapter.

## The cost of a run

**Read root CLAUDE.md → "Performance is a contract at every boundary" FIRST.** The
cost classes, the listing/detail rules and the measurement law live there. This
section is only what is specific to *accepting and executing a run*.

### Acceptance and execution are two cost contracts, not one

| Seam | Cost class | Persists |
|---|---|---|
| **acceptance** — `SessionRuntime.start_async` mints a run_id and hands off | `O(one record)`, and that is the ceiling | what it ACCEPTED |
| **execution** — the background worker, from `Orchestrator.__init__` onward | unbounded; a model call is inside it | only what it DISCOVERED |

**The acceptor persists what it accepted; the executor persists only what it
discovered.** A fact already certain when the run was taken — the run id, the text
the operator submitted, the accepted-at timestamp — belongs in the store before
the caller gets its answer back. Anything the executor writes is invisible for the
whole cold-start window, so a certain fact deposited there is a fact no client can
render while it waits. **A client must never wait on executor setup to see
something that was true at acceptance.** The mechanism is `loop/CLAUDE.md`'s —
read it there.

Acceptance is `O(one record)` rather than `O(1)` because `_mint_run_id` derives
the run sequence by counting `user` events in the session's transcript. **One
record is the ceiling** — the acceptor may read the session it is accepting into,
never a collection, never a child fan-out.

**A session may cost TWO runs, not one, and that ceiling is deliberate.** A run
terminating with its goal unmet is re-driven exactly once from a post-release seam
(`loop/CLAUDE.md`). Nothing in this system bounds a session's total token spend —
`TokenBudget` is compaction accounting, not a wallet — so any further automatic
re-invocation you are tempted to add has no backstop underneath it and must state
its own ceiling the same way.

### The per-run critical path

`Orchestrator.__init__` runs four setup legs, in order, before the orchestration
body appends anything. All four sit between `run_accepted` and the first
executor-written event:

| Leg | Cached? | Invalidation signal |
|---|---|---|
| `discover_project_instructions(cwd)` | **no** | — walks the tree every run |
| `SkillRegistry.load` / `maybe_reload` | per-turn re-scan | file mtime; `SkillBinaryProbe` caches for ONE turn |
| `load_all_plugin_components()` | process-wide | installed-plugins registry mtime |
| `get_or_build_registry(...)` | process-wide | cwd + plugin-contributed MCP servers + MCP-config fingerprint |

- **Do not regress the registry cache.** `ToolRegistryCache` builds OUTSIDE its
  lock so one slow scope never blocks another's lookup, and `allowed_tools`
  scoping is deliberately not part of the key — it is applied per-run downstream
  via `filter_specs`. A direct `load_registry` call in `Orchestrator.__init__`
  re-opens the rebuild.
- **New synchronous per-run work that hits the network, spawns a subprocess or
  walks the filesystem needs a written justification and a cache with a NAMED
  invalidation signal** — or a lifecycle marker in front of it, so clients are not
  blind for its duration. `SkillBinaryProbe` is the shape to copy: scope the cache
  to the shortest window that stays correct (one turn, so the many per-step
  catalog renders share a probe while a binary installed into a days-old server is
  picked up next turn), and say in the docstring what clears it.

### A lifecycle marker is only real when something consumes it

Emitting a progress or acceptance event is **half a feature**, and **a test suite
cannot fail for an event nothing consumes** — an emit that is correct, tested and
subscribed to by no client passes every gate while delivering nothing.

- **The emit and its consumer land in the same change.** Otherwise the emit is
  dead weight that reads like a shipped feature.
- **Verify the CONSUMER, and verify the built artifact** (root CLAUDE.md →
  "Measurement law"). For a lifecycle event that means grepping the *built* console
  bundle and the server-side reader for the event name, not the sources you just
  edited. `run_accepted` today is read by the console timeline as a turn-opening
  event and by `apps/mewbo_api/run_sweep.py` as a run-lifetime marker; a new
  marker needs the equivalent named before it merits the emit.
- Markers ride the existing `append_event` → `SessionEventBus` choke-point. A new
  transport for a new marker is a smell — the choke-point already fans out.

### One worker is the invariant every in-process primitive rests on

The API deploys gunicorn with `--workers 1` (`docker/Dockerfile.api`) because
`RunRegistry` is in-memory. **Cite the worker count, never a thread count** —
`--threads` is a tunable knob that has already been raised; `--workers 1` is
structural. In-process locks (`ToolRegistryCache`), in-memory registries
(`RunRegistry`) and the process-local `SessionEventBus` observer wired onto the
`on_event` hook are all correct *because of it*, and for no other reason.

**Anything that would require a second worker must move its coordination out of
process FIRST** — a shared store, a real broker, a cross-process cache — and only
then the worker count. In that order, never the reverse.

### Profiling the engine

**Time an internal phase by differencing stored event timestamps.** Every phase
boundary is already an event in `db.events`; the elapsed time between two of them
is that phase's real cost on the deployed stack, with nothing to instrument.
**Profile the phase before optimising it** — orchestrator cold start is widely
assumed to cost about a minute, and measured (while the executor still wrote the
`user` event) the `run_accepted` → `user` gap was a median of **0.76s** (n=25, min
0.107s).

Run it from the repo root, so the deployed stack's compose project resolves. The
credentials come from the container's OWN environment — never inline them here:

```sh
docker compose exec -T mongo sh -c 'mongosh \
  -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" \
  --authenticationDatabase admin --quiet "$MONGO_INITDB_DATABASE"' <<'JS'
printjson(db.events.aggregate([
  { $match: { type: { $in: ["run_accepted", "user"] } } },
  { $set:   { at: { $toDate: "$ts" } } },          // ts is an ISO-8601 STRING
  { $setWindowFields: { partitionBy: "$session_id", sortBy: { at: 1 },
      output: { nxt:      { $shift: { output: "$at",   by: 1 } },
                nxt_type: { $shift: { output: "$type", by: 1 } } } } },
  { $match:   { type: "run_accepted", nxt_type: "user" } },
  { $project: { _id: 0, gap_s: { $divide: [{ $subtract: ["$nxt", "$at"] }, 1000] } } },
  { $group:   { _id: null, n: { $sum: 1 }, min: { $min: "$gap_s" },
      p50: { $percentile: { input: "$gap_s", p: [0.5], method: "approximate" } } } },
]).toArray());
JS
```

Swap the two `type` values to bracket any other phase. Three traps:

- **`ts` is an ISO-8601 STRING, not a BSON `Date`.** `$subtract` on it fails;
  `$toDate` first or the pipeline errors rather than quietly mis-measuring.
- **Pick the pair that BRACKETS the phase you mean.** Now that the acceptor writes
  `run_accepted` and `user` adjacently, that pair measures two store writes; cold
  start is bracketed by `run_accepted` → the first EXECUTOR-written event
  (`llm_call_start` in the ordinary case). Re-derive the pair whenever a write
  moves seams.
- **Credentials must come from the container, not your shell.** `docker compose
  exec` passes the *host* shell's expansion of `$MONGO_INITDB_ROOT_USERNAME`,
  normally unset — hence the `sh -c '...'` wrapper, which defers expansion to the
  container. The symptom of getting this wrong is an authentication error, not a
  wrong number, so it fails loudly.

For per-agent attribution versus where token counts actually live, root CLAUDE.md
→ "Debugging sessions" documents the split — join the two by session.

### Checklist — adding work to the run path

- [ ] Acceptance or execution? If the fact is certain at acceptance, the ACCEPTOR
      writes it — and stays within one record doing so.
- [ ] Network, subprocess, or filesystem walk, synchronously, per run? Justify it
      in a comment, give it a cache with a named invalidation signal, or put a
      lifecycle marker in front of it.
- [ ] Adding a cache? Key and invalidation signal in the class docstring; lifetime
      scoped to the shortest window that stays correct.
- [ ] Still routing through `get_or_build_registry`?
- [ ] Emitting a lifecycle event? Name its consumer and land it in the same change.
- [ ] Would it need a second gunicorn worker? Move the coordination out of process
      first.
- [ ] Claiming it got faster? Which two stored timestamps, and what was the number?

## Config curation annotations (faceted Settings UI)

`config.py` section models carry presentation/security metadata in their JSON
schema for the console's Settings UI. The non-obvious trap: a submodel field
serializes to a bare `$ref` and Pydantic **drops sibling `json_schema_extra`**, so
facet metadata (`x-group`, `x-order`, `x-advanced`) MUST sit on the section
**class** (`model_config = ConfigDict(json_schema_extra=...)`) where it lands on
`$defs/<Class>` — not on the field, where it silently vanishes. Field-level flags
(`x-secret` write-only, `x-protected` never-exposed, `x-advanced`) go on the
scalar `Field(...)` and survive. Full contract: the comment block above
`AppConfig`.

**The `$ref`-drop trap has an exception: collection fields.** A `dict[str, X]` or
`list[...]` field is NOT a bare `$ref` — it serializes inline
(`{type: object, additionalProperties: …}`), so field-level `json_schema_extra`
**survives** on it. That is how `projects`/`channels` carry their `x-group` from
the *field* with no wrapper class. Only a single-submodel field needs the
class-level workaround.

**`x-env-var` is the one annotation that CHANGES a value rather than describing
it.** A field names the environment variable that overrides it, and the shared
`EnvOverridable` base — which the field's section must inherit from, or the
declaration is inert — applies every declaration in one `model_validator`. Adding
one is a declaration, not new code. It is the OPPOSITE direction to `EnvRef`
(`${VAR}` written by the operator as a value, refused when unset); an override
that is unset or empty is simply not applied. The API's `ConfigSchemaView` reads
these; the console's `SettingsModel` mirrors them.

**`AppConfig` is `extra="ignore"`** — an unknown top-level block in `app.json` is
silently dropped at `model_validate`, and `get_config_value` walks one `getattr`
per dotted key, so a new feature section is physically un-enableable until it
becomes a typed `AppConfig` field whose nesting matches the accessor's dotted path
(e.g. `scg.traversal.default_tier` needs a `traversal` submodel, not a flat
field). The same trap bites at LEAF level: a `get_config_value("agent",
"session_step_budget")` whose typed field does not exist reads `default=0` in every
deployment with nothing raised, leaving the feature behind it unreachable. **A
`get_config_value` key is only real once its typed field exists; a docstring
mentioning it is not.**

**`title=` (in the class `json_schema_extra`) humanizes the section heading** and
flows live to the console (the FE `prettify` is only a fallback);
**`deprecated=True` on a `Field`** hides that knob in the console.

**⚠️ An `x-group` and its console facet must land in LOCKSTEP — there is no error
if they don't.** The console's `SettingsModel` validates a section's `x-group`
against its `FACETS` list and **silently buckets an unknown group into the `other`
fallback facet**. Shipping a new `x-group` without adding the matching facet id to
`apps/mewbo_console/src/components/settings/facets.ts` (`FacetId` union + `FACETS`)
fails no build and logs no warning — the section just *vanishes* into "Other". The
authoritative facet list is the `x-group` comment block above `AppConfig`; keep it
and `facets.ts` in lockstep.

**A console surface must sit with the settings that govern it** (see
`apps/mewbo_console/CLAUDE.md` → "Settings — faceted shell over RJSF" before
inventing a group), so **a pane and its config section always move together** —
moving one alone either strands the pane away from its knobs or drops the section
into "Other".

**Config-derived artifacts regenerate IN-COMMIT.** `configs/app.schema.json` and
`docs/configuration.md` are generated from the `Field(description=...)` text in
`config.py` via `scripts/ci/generate_config_schema.py` (the docs side is an MkDocs
`on_pre_build` hook in `docs/hooks/schema_to_md.py`), and docs CI is
`pull_request`-only — a direct push will not save you. Treat every field
description as published copy, because it is: hand-editing the generated file is
undone by the next build, so fix the source and regenerate.

## Testing

Tests live in `tests/`; patterns in `tests/CLAUDE.md`. This package's tests focus
on tool-use loop edges (cancellation, plan context, lifecycle-aware depth
guidance, no-step-count-in-messages), hypervisor CRUD + admission control + the
six-state lifecycle, sub-agent spawn with `filter_specs()` tool scoping,
compaction modes producing the right post-compact state, and hook lifecycle +
failure isolation.

Stub at I/O boundaries (`model.ainvoke`, tool execution) — never run a real model
or hit a real network.

## Pre-edit checklist

- [ ] **Read the child `CLAUDE.md` for the package you are touching.** The
      paragraph that governs your edit is almost certainly one level down.
- [ ] Writing a rule down? Does it already exist in another file? One law, one
      home — leave a pointer instead.
- [ ] Touching the tool-use loop? Verify all four execution paths still work
      (text response, tool call, plan context, cancellation).
- [ ] Adding a module? Does its package still import only DOWN, and does its
      `__init__.py` still contain nothing but a docstring?
- [ ] Adding a new built-in plugin? Drop it under `builtin_plugins/<suite>/` with
      a `.claude-plugin/plugin.json` — discovery is a filesystem scan, there is no
      manifest constant to register — AND add a `tests/<plugin>/` module that
      stubs I/O.
- [ ] Adding a new context event type? Update `session/compaction.py` so the event
      survives compaction (or document that it is intentionally ephemeral).
- [ ] Adding a new lifecycle state? Update the six-state enum, the hypervisor's
      terminal-state handling, AND the three client mirrors.
- [ ] Adding a new `SessionTool`? Walk the declaration law's knob table in
      `tooling/CLAUDE.md`.
- [ ] Adding a new LLM call site? Use `build_chat_model()` from `llm/llm.py`
      instead of constructing a LiteLLM client inline.
- [ ] Adding work to the run path, a cache, or a lifecycle marker? Walk "The cost
      of a run" → "Checklist" above.
