# Agentic Search ("Mewbo Search") — Console Subsystem Guidance

Scope: this file applies to `apps/mewbo_console/src/components/agentic_search/`
plus the colocated `src/hooks/useAgenticSearch.ts`,
`src/api/agenticSearch.ts`, and `src/types/agenticSearch.ts`. Captures the
non-obvious FE decisions for the multi-source search surface. Read the
console root `apps/mewbo_console/CLAUDE.md` first — the library-first,
theming, and shape-vocabulary rules all apply here unchanged.

## The streaming rule — no synthetic timers

The original mockup **FAKED streaming** with `useStaggeredReveal` (a 50 ms
`setInterval` ticking `elapsed` against fixture `finish_delay_ms` / `t_ms`,
then deriving which results/trace-lines/bullets are "visible"). That hook
is prototype scaffolding being removed.

**RULE: result / trace / answer visibility derives from RECEIVED SSE
events — NEVER reintroduce a synthetic-timer reveal.** The BE event log is
the arrival clock: `result` events gate result cards, `agent_*` events
gate trace lines, `answer_delta*` drive the typewriter, `answer_ready` /
`run_done` close it. Treat `finish_delay_ms` / `t_ms` as dead fields.

## SSE consumer — reuse the shared util, mirror `useQaStream`

Use the shared `sseStream` / `parseSseStream` util (lifted from the wiki's
`components/wiki/api/client.ts` into `src/api/sse.ts` so both subsystems
share one implementation). It is **fetch-based, not `EventSource`**,
because it must:

1. send the API key (`X-API-Key` header / `?api_key=`),
2. honor `AbortSignal` so unmount cleanly cancels the subscriber,
3. POST bodies when needed.

The run-stream reducer mirrors `useQaStream`: own an AsyncIterable, fold
typed events into accumulating state, honor the `AbortSignal`. Don't
hand-roll a parallel SSE reader — extend the shared util's event-union
handling. Indexing/Q&A and search streams stay distinct reducers (one-shot
per run); don't try to merge them.

## TanStack Query round-trip rules

- **Do NOT `invalidateQueries(WORKSPACES_KEY)` after a run.** The current
  `useRunSearch` does exactly this; it triggers a full workspace-list
  refetch just to bump one `past_query`. Replace with an optimistic
  `setQueryData` that prepends/patches the history entry. (Server state
  rule from the console root: invalidate only when you can't cheaply
  reconcile locally.)
- **Do NOT auto-run a search on workspace switch.** The current
  `handlePickWorkspace` re-runs the last query against the newly selected
  workspace — drop that. Switching a workspace selects it; the user
  submits the next run explicitly.
- **`run_id` rehydrates a run.** A `run_id` in the URL/flow loads the run
  via `GET /runs/{id}` (durable snapshot) rather than re-executing — this
  is how reload / share / deep-link work. Wire the view to read a
  snapshot, not to re-`POST`.

## Wire migration — compute time labels FE-side

The BE emits both ISO timestamps (`created_at` / `ran_at` / `updated_at`)
AND legacy human labels (`created` / `when`) for back-compat during the
migration. **Compute relative labels on the FE from the ISO fields** using
the existing `RelativeTime` util (`components/wiki/relativeTime.ts` /
shared) — never render the server-formatted `created` / `when` strings.
They exist only so an un-migrated console keeps rendering; migrated code
prefers ISO and formats locally.

The current `types/agenticSearch.ts` is the pre-migration shape (missing
`created_at` / `ran_at` / `run_id` / `status` on workspace+past-query, and
still marks `finish_delay_ms` required). Extend it toward the Python
`schemas.py` as the source of truth — add the ISO/run-link fields,
make the deprecated timing fields optional.

## What stays

- Server state flows through `useAgenticSearch.ts` hooks only; the view
  never calls `fetch` directly. `agenticSearch.ts` reuses `API_BASE` /
  `API_KEY` from `api/client.ts` — don't duplicate auth/base-URL logic.
- The catalog query keeps `staleTime: Infinity` (effectively static).
- Shape vocabulary, theming tokens (`hsl(var(--…))`), and the
  library-first checklist from the console root apply to every card here
  (`ResultCard`, `AnswerCard`, `TraceDrawer`, `SrcAvatar`, …). The
  per-source `slot` maps to `--agent-N` tokens — reuse them, don't
  hand-pick agent colors.
