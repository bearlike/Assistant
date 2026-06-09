> ↑ [packages/mewbo_graph/CLAUDE.md](../../../../CLAUDE.md) · [root](../../../../../../CLAUDE.md)

# scg built-in plugin — SCG map + search tools

Scope: `packages/mewbo_graph/src/mewbo_graph/plugins/scg/`. The
deterministic SCG logic does **not** live here — these six SessionTools
(`scg_introspect_source`, `scg_build_structure`, `scg_link_entities`,
`scg_finalize_map`, `scg_route`, `scg_memory`) are **thin wrappers** over the
SCG core, which now lives **down** in the same library at `mewbo_graph.scg`
(same package, imported DOWN — no longer a one-way boundary UP into an app).
Read the api-side subsystem `CLAUDE.md` (`apps/mewbo_api/.../agentic_search/scg/`)
and Gitea #19 for the durable architecture decisions.

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

## Capability gating (data-driven, no hardcoded literal)

The plugin manifest (`.claude-plugin/plugin.json`) declares
`requires-capabilities: ["scg"]`, and the AgentDefs (`agents/*.md`) repeat it in
frontmatter. A map/search session advertises `client_capabilities: ["scg"]` (via
`runtime.append_context_event`) so the AgentDefs surface in `spawn_agent` lookups
and the `scg_*` tools scope in — the generalized form of the wiki gate (no `scg`
string is hardcoded in `agent_registry.py` / `capabilities.py`; gating flows
entirely through `requires-capabilities`). The deterministic core is *also*
opt-in behind the `scg.enabled` config flag.
