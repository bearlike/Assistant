> ↑ [apps/mewbo_api/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md) · children: [scg/](scg/CLAUDE.md)

# Agentic Search ("Mewbo Search") — API Subsystem Guidance

Scope: this file applies to `apps/mewbo_api/src/mewbo_api/agentic_search/`.
It captures the non-obvious engineering decisions in the multi-source
workspace search backend — the integration seam two teams build against.
Everything readable straight from the code is left out.

## What this is

A multi-source search surface: a *workspace* selects connectors
("sources"), a *run* fans an agent out across them, and the console
renders streamed results + a cited synthesis. The package owns the wire
**contracts** and the **persistence**; the orchestration team owns the
real fan-out behind a single seam (`runner.py`). The default runner is a
fixtures replay so the whole console↔API loop works with zero LLM.

## Run lifecycle — the event log IS the search-event stream

`POST /runs` is **synchronous and back-compat today**: it creates the
run, drives it to completion via the active runner, and returns
`{run: RunPayload}` plus top-level `run_id` / `session_id` / `status`.
Don't "fix" the duplicated fields — the console reads either shape.

The run's append-only, idx-keyed event log (in the store) IS the
normalized search-event stream. Three transports project the same write:

| Surface | Endpoint | Role |
|---|---|---|
| Durable snapshot | `GET /runs/{id}` | reload / share / deep-link; returns the `RunRecord` + accumulated `payload` |
| Live projection / replay | `GET /runs/{id}/events` (SSE) | replays the log from idx 0, then tails until a terminal event |
| History | `GET /workspaces/{id}/runs` | newest-first run records for a workspace |

A synchronous runner appends a terminal event (`run_done` / `error`)
before returning. An async runner returns a `running` snapshot and keeps
appending as its backing session progresses — the SSE generator tails
either case identically. When you wire async runs, **do not** add a
second status channel; keep the event log authoritative.

**The standalone MCP server (`apps/mewbo_mcp`) is a second consumer of
these endpoints.** Its `search` / `get_search_run` / `list_search_workspaces`
tools wrap `POST /runs` + the snapshot **poll** path (`GET /runs/{id}`),
NOT the SSE stream — the bounded poll is the canonical await for a
non-streaming caller, and it's already forward-compatible with the async
runner. The SSE stream stays the console's live-reveal transport. So the
`POST /runs` back-compat envelope (`{run, run_id, session_id, status}`) and
the `RunRecord.status` on the snapshot are load-bearing for the MCP await
loop — don't drop the top-level `status` fields.

## Storage is SEPARATE from session transcripts (hard requirement)

The run store (`agentic_search_runs` + `_run_events` + `_workspaces`) is
its own namespace, deliberately decoupled from session transcripts. A run
is *backed by* a session but its normalized projection lives here so
snapshot reads stay fast and survive session GC. JSON layout under
`<cache_dir>/agentic_search/` (`workspaces/<id>.json`,
`runs/<id>/run.json`, `runs/<id>/events.jsonl`); Mongo mirrors it in the
`agentic_search_*` collections. Never route run state through the
transcript event log.

## The seam — `SearchRunner` (runner.py)

`SearchRunner` (a `Protocol`) is the swap point. Two implementations:

- **`EchoSearchRunner`** (default, dev) — replays the prototype fixtures
  over the REAL event log + store with NO LLM. It filters canned
  results/trace/answer to the workspace's enabled sources, emits the full
  normalized event sequence (incl. the `answer_delta*` typewriter), and
  persists the terminal snapshot. This is what makes console↔API
  integration work end-to-end *before* the real fan-out exists.
- **`OrchestratedSearchRunner`** (orchestration team, registered via
  `runner.set_search_runner()`) — starts a tool-scoped `SessionRuntime`
  session and translates session-transcript events into the event
  protocol using the `events.py` builders.

We **deliberately did NOT ship a speculative `SearchEventAdapter` ABC**
(upholds "no speculative abstractions"). Transcript→event normalization
is the real runner's internal concern; the `events.py` builders are the
only required shared surface. The routes call `get_search_runner()` and
stay agnostic to which strategy is wired.

## `schemas.py` is the single source of truth for the wire

Every other module references it; the orchestration team implements
*against* it. It holds the entity/wire models, `RunRecord` (durable), and
`SEARCH_EVENT_TYPES` (the canonical event vocabulary the console's
reducer switches on). Rules:

- `OUTPUT_CONTRACT_VERSION` is stamped on every `RunRecord` and emitted in
  `run_started`. Bump it only on an **incompatible** wire change so the
  console can guard.
- Wire models are `extra="forbid"`; `clean_for_model()` whitelists
  Mongo/event-log bookkeeping keys (`_id`, `idx`, `event_count`) on load
  so loads stay lenient while models stay strict (mirrors the wiki store).
- Synthesis streams as `answer_delta*` then a final `answer_ready` — the
  typewriter protocol. `agent_*` events drive the per-source trace;
  `result` events drive arrival order. The prototype's `finish_delay_ms`
  / `t_ms` are deprecated decorative fields — real ordering comes from
  event arrival, never a client timer.

## `store.py` is the substitution boundary

Dual-backend JSON/Mongo, mirroring `project_store` / `session_store`.

- `save_workspace` is the verbatim write primitive that both `create` and
  `seed` share — one persist path, so seed ids stay stable.
- Demo-workspace seeding is gated by `MEWBO_AGENTIC_SEARCH_SEED` (default
  on) and only fires when the store is empty. Set it to `0` for a
  production start-empty.
- `reset_for_tests()` swaps a fresh seeded JSON store under a tempdir for
  isolation while still exercising the JSON backend through the routes.
- Mongo idx is atomic (`$inc` on `event_count` via `find_one_and_update`);
  the JSON backend counts non-blank lines under a lock. Keep idx
  monotonic per run — the SSE `id:` line and replay-from-idx depend on it.
- `past_queries` is bounded at `PAST_QUERY_CAP`; a `running` entry is
  written up-front and patched in place on completion.

## `SourceCatalog` (catalog.py) — source→`allowed_tools` scoping

`SourceCatalog.tools_for(source_ids, project)` is the rule a run applies
to scope `allowed_tools` (selected sources → de-duplicated union of tool
ids). It resolves each source's tool ids from its **live SCG capability
nodes** (`kind == "capability"`, the tool id is the node `name`), then
intersects with the live registry via `filter_specs()`. The wire shape
(`SourceCatalogEntry`) and the `tools_for` contract are fixed; only the
resolution body changes.

**No hardcoded fallback constant.** Before a source is mapped, resolution
falls back to the illustrative `tools` declared *beside the source* in
`fixtures.SOURCE_CATALOG` — and **only while demo seeding is on**
(`store.seeding_enabled()`, the one gate shared with demo-workspace seeding).
A production install (`MEWBO_AGENTIC_SEARCH_SEED=0`) therefore reports an
unmapped source as `available=False`, never a guess. Demo data lives with the
source it describes, never as a `TOOL_MAP`-style constant in the resolver.

**Unconfigured sources must be returned with `available=False` (+
`unavailable_reason`), NOT omitted** — so the console can grey out a
persisted workspace source instead of silently dropping it.

## Security invariants (real runner must uphold)

- Workspace `instructions` are **UNTRUSTED prompt input**. The real runner
  must keep them separate from system/developer instructions — never
  concatenate them into the system prompt.
- `allowed_tools` = selected sources ∩ project policy. The catalog union
  is the upper bound, not the final grant.
- Never persist secrets (tokens, credentials) in workspace or run records;
  both are JSON/Mongo-visible like the wiki submission.

## SSE plumbing (events.py)

Inherited verbatim from `wiki/events.py`: a 2 KB primer frame at byte 0 +
padded heartbeats defeat proxy buffering (OpenResty/NPM buffer ~4 KB).
**Don't shrink them** — that reintroduces the buffer bug. Frame format is
`id: <idx>\nevent: <type>\ndata: <json>\n\n`; the `id:` line carries the
event idx so a flaky proxy can resume via `Last-Event-ID` / `?after_idx=`.
The generator polls `load_run_events(after_idx=...)` until a terminal
event or the idle threshold (env-tunable: `MEWBO_AGENTIC_SSE_MAX_IDLE`,
`MEWBO_AGENTIC_SSE_SLEEP`).

SSE auth uses the `?api_key=` query param — `EventSource`/`fetch` can't set
headers everywhere, and `_require_api_key` already honours the param.

## The real runner is the SCG — a cheap router (now built — see scg/CLAUDE.md)

`OrchestratedSearchRunner` (in `scg/orchestrated_runner.py`) is the real
`SearchRunner`: **not** "spawn one agent per enabled source." It drives a
`scg-search` session over the **Source Capability Graph (SCG)** — a cheap
routing controller that indexes *reachability* (schemas + pathways, **never
data**) and lets the agent fan probes out along *qualified pathways*. The graph
ops (route / parse / ER) are tools the agent drives, never a parallel control
loop; tiers are one decomposition+probe budget knob over the single
`ToolUseLoop`; the connector's real return is the only verifier.

It is registered (replacing the echo runner) only when `scg.enabled` is on AND
at least one source has been mapped — see `_maybe_register_orchestrated_runner`
in `routes.py`. The durable decisions + the two silent correctness traps live in
**`scg/CLAUDE.md`**; the full spec + research grounding is **Gitea #19**.

The SCG *engine* itself (router / parser / entity-resolution / store / memory
bridge) lives **down** in the optional `mewbo_graph.scg` library (Gitea #25);
this app holds only the runner seam + the map-job lifecycle glue and composes
the engine via the `wiki` extra. See `packages/mewbo_graph/CLAUDE.md`.

## Testing notes

- Use `store.reset_for_tests()` for isolation; mock at the runner seam, not
  inside it. The default `EchoSearchRunner` already exercises the full
  event-log → SSE replay path without an LLM.
- Tune the SSE idle/sleep env vars down in tests so the generator closes
  promptly after the terminal event.
- Agentic-search tests should NEVER spawn a real LLM or hit a real proxy.
