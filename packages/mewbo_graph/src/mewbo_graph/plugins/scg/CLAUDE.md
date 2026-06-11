> ↑ [packages/mewbo_graph/CLAUDE.md](../../../../CLAUDE.md) · [root](../../../../../../CLAUDE.md)

# scg built-in plugin — SCG map + search tools

Scope: `packages/mewbo_graph/src/mewbo_graph/plugins/scg/`. The
deterministic SCG logic does **not** live here — these seven SessionTools
(`scg_introspect_source`, `scg_build_structure`, `scg_link_entities`,
`scg_finalize_map`, `scg_route`, `scg_observe`, `scg_memory`) are **thin
wrappers** over the SCG core, which now lives **down** in the same library at
`mewbo_graph.scg` (same package, imported DOWN — no longer a one-way boundary UP
into an app). Read the api-side subsystem `CLAUDE.md`
(`apps/mewbo_api/.../agentic_search/scg/`) and Gitea #19 for the durable
architecture decisions.

## The tool ↔ substrate import boundary (`_core.py`)

`_core.ScgCore` is the one atomic resolver every tool crosses to reach the SCG
core: it resolves the deterministic core from `mewbo_graph.scg` (down) and the
wiki memory substrate from `mewbo_graph.wiki` (down) — both DOWN into the same
library, no longer up into an app. Each accessor **late-imports inside the
call**, so a CORE-ONLY install — the optional `mewbo-graph` library present but
its `treesitter` / `retrieval` extras (or the API run store) absent — never
fails at plugin load: the tool catches `ImportError` and degrades to a
structured error (`err_result` / `ok_result`, the wiki `_err_result` shape).
Tests monkeypatch `ScgCore` classmethods to inject fakes — no network, no real
embedder. Don't re-spread late imports across the tools; add the seam to
`ScgCore`.

## Routing invariants (each shipped a live no-answer bug — 2026-06)

- **`ScgRouter.route` ranks ONLY persisted recipes.** Neither default provider
  emits them, so `ScgParser.parse_source` backfills a single-step recipe per
  recipe-less capability — zero recipes = a mapped source that routes nothing.
- **The probe's tool scope is copied, never inferred.** `scg_route` enriches
  each recipe with `source_ids` + `source_capabilities` (every capability of
  the pathway's sources); the scg-search playbook copies that into the spawn's
  `allowed_tools`. Left to inference, the parent scoped probes to the step
  tools only, and a one-tool probe cannot chain a follow-up lookup.
- **A pathway is the probe's ENTRY, not its ceiling** (scg-path-probe.md): the
  probe chases the sub-query to ground with any granted tool of the same
  source and declares NO DATA only when the SOURCE can't supply it.
- **`scg_route` is memory-aware (#76).** `ScgCore.router` DI's the memory bridge
  so routing biases toward learned-productive pathways (best-effort: a failed
  bridge → empty bias, never a route failure). The tool calls `route_with_memory`
  and projects capped `memory_hints` (anchored "how to call this right" notes)
  per recipe — compact, so the probe needs no second `scg_memory` read.
  `scg_memory` write gains a `polarity` (positive/dead_end) arg + stamps the
  ambient `ScgScope.workspace()` as a `ws:<id>` attribution label (not a partition).
- **`scg_observe` is the Search-on-Graph read (arXiv 2510.08825).** The discipline
  it encodes: the ENGINE ranks entries (`scg_route`), the AGENT reads the hops and
  navigates — the typed edges carry the routing meaning
  (SUPPORTS_QUERY/PRODUCES/CONSUMES/RESOLVES_TO), not a second engine score. Given
  node refs (`source_key`s or node ids) it projects each node's directed, typed
  neighborhood (edges + 1-hop neighbor cards + recipes + anchored memory notes) — a
  thin read over the store + `ScgGraphView` memory assembly, NO new traversal
  engine. Two-stage (SoG Algorithm 1, minimal): a node over `_SURVEY_THRESHOLD`
  in-scope edges with no filter returns a `kinds_only` rollup, then the agent
  re-calls with `edge_kinds`/`direction` for the instances. Read-only, `ScgScope`-
  filtered (out-of-scope hops dropped), `auth_scope` redacted; wire is the
  established `ok_result(dict)` shape (typed `ObservedNode.to_wire()`).

## Capability gating (data-driven, no hardcoded literal)

The plugin manifest (`.claude-plugin/plugin.json`) declares
`requires-capabilities: ["scg"]`, and the AgentDefs (`agents/*.md`) repeat it in
frontmatter. A map/search session advertises `client_capabilities: ["scg"]` (via
`runtime.append_context_event`) so the AgentDefs surface in `spawn_agent` lookups
and the `scg_*` tools scope in — the generalized form of the wiki gate (no `scg`
string is hardcoded in `agent_registry.py` / `capabilities.py`; gating flows
entirely through `requires-capabilities`). The deterministic core is *also*
opt-in behind the `scg.enabled` config flag. #77 widened the GRANT seam to any
workspace-bound session; **#83-B makes it GENERAL**: this package registers a
runtime capability provider (`_scg_runtime_capability`, wired via
`mewbo_graph.register_runtime_capabilities` → core `register_session_capability_provider`)
that grants `scg` to ORDINARY sessions (CLI/console/channel) whenever
`scg.enabled` AND the store has ≥1 mapped source — so the gating mechanism itself
still never changes; only WHO advertises `scg` widened. **#84 closed the half that
#83-B left open:** the grant unioned `scg` into `session_caps`, but core's
`SessionToolRegistry.build_for` selected session tools by `allowed_tools` ALONE —
so these tools surfaced to a ROOT agent only when its AgentDef/allowlist named
them, NOT from the capability. A plain re-engaged session (root depositing
directly, no `scg-mapper` spawn) therefore saw no `scg_*` and answered
`TOOLS-MISSING`. `build_for` now ALSO builds any factory whose
`requires-capabilities` ⊆ `session_caps` (the manifest gate finally reaches the
session-tool build), so these three tools reach the root of every ordinary session
the predicate grants — verified live: 16 root-issued `scg_memory` deposits on
re-engagement. An unscoped session binds
no `ScgScope` ⇒ `scg_observe`/`scg_route` read the WHOLE graph (the scope default);
`scg_memory write` attributes to `session:<id>` (a `labels` fallback for `ws:<id>`,
no new field) when no workspace is bound. The three reasoning tools are
default-allowed: their ids are `get`-classified in core `_infer_operation` so the
default permission policy ALLOWs reads + the additive deposit (no new config knob;
`auth_scope` stays redacted).
