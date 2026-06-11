> ↑ [apps/mewbo_console/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md)

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

## Stream state machine · dedup · composer · autocomplete (#82)

- **`stream.attached`, not `runId`, gates the live view.** `reduceRun` flips
  `attached` on the FIRST frame of ANY type (incl. `agent_start`); `useRunStream`
  dispatches an `attach` action seeding the known id on subscribe (a new id wipes
  the fold). Keying off `runId` renders an empty payload early; keying off
  `run_started` wedged the live leg on "Starting search…" if that opener dropped.
- **Dedup strictly by result id** — in the reducer AND `ResultsPanel.visibleResults`
  (snapshot↔SSE / echo defense). Shared id ⇒ same React key AND `result-<id>` DOM
  id ⇒ the "hover one, highlight its twin" symptom. First occurrence wins.
- **Composer seam = `ui/composer-shell.tsx`** (`ComposerShell` + `ComposerIconButton`
  / `ComposerSendButton` + `composerSurface()`); `SearchBar`'s 3 bars compose it.
  Tasks' `InputComposerBody` deliberately stays off it (JS-glow surface, per-variant
  toolbar, running Queue/Stop) — forcing it would add knobs (YAGNI) / regress.
- **Focus language is SHARED with the Tasks composer, via CSS not JS**:
  `composerSurface()` emits `.composer-surface` + a `data-halo` attr
  (`composerSurfaceData()`), and `index.css` owns the primary-tinted 4px bloom
  + border tint + 200ms ease-out (sibling of `.composer-shell`, reduced-motion
  guarded). Don't reintroduce a `--ring`-tinted `focus-within:` Tailwind halo
  on a composer — the two composers must bloom identically.
- **Suggestions dropdown pans out from the composer**: `.composer-suggest`
  (origin-top scaleY+fade, 160ms, one-shot on `acOpen`, reduced-motion safe),
  `mt-1` tight anchor, same border-strong/rounded-xl/elev-3 family as the bar.
  `font-mono tabular-nums` ONLY on the data right-column (counts/times) — never
  on prose. The no-match state is a selectable "Search …" `CommandItem`, not a
  bare `CommandEmpty`.
- **No dead controls**: the hero's decorative Expand/Attach/Voice icon buttons
  were REMOVED (none had an onClick). A control that does nothing is worse than
  none — re-add only alongside a real implementation (user rule, emphatic).
- **Autocomplete opens on gesture only**: `SearchBar` suppresses the mount-time
  `autoFocus` open (`suppressFocusOpenRef`) — never `combobox [expanded]` at rest.

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
- **`run_id` rehydrates a run.** `/search?run=<id>` (wouter v3.9
  `useSearchParams`) loads the durable snapshot via `GET /runs/{id}` rather
  than re-executing — the param wins over the persisted localStorage
  `agentic-search:run-id` in the `runId` initializer, plus a follow-up
  effect keyed on the param string.

## Wire migration — compute time labels FE-side

The BE emits both ISO timestamps (`created_at` / `ran_at` / `updated_at`)
AND legacy human labels (`created` / `when`) for back-compat during the
migration. **Compute relative labels on the FE from the ISO fields** using
the existing `RelativeTime` util (`src/components/wiki/relativeTime.ts` —
a class with static `format(iso)` / `tooltip(iso)`; no instances, no dep)
— never render the server-formatted `created` / `when` strings.
They exist only so an un-migrated console keeps rendering; migrated code
prefers ISO and formats locally.

`types/agenticSearch.ts` mirrors the Python `schemas.py` (the source of
truth) — extend it from there. `SourceCatalogEntry.source_type` is optional
on the wire, but `POST /sources/<id>/map` requires one (SCG provider
dispatch keys: `mcp_tool_list | openapi | text`) — consumers default it via
`source.source_type ?? 'mcp_tool_list'` (the catalog is
MCP-integration-sourced).

## Mapping / SCG surface (`SourcesDialog`)

- **Two complementary transports per source row** make mapping progress
  reload-safe: `useMapJobs` polls `GET /sources/<id>/map/jobs` with
  function-form `refetchInterval` (2s while `jobs[0]` is queued/running,
  off otherwise) for the durable snapshot; `useMapJobStream` tails the SSE
  for instant phase. The stream's finally-block invalidates the map-jobs +
  `SCG_KEY` queries so polling flips off and mapped badges refresh on
  terminal.
- **The map-stream fold needs an explicit `reset` when `jobId` changes.**
  The map-job event log has NO `run_started`-style opener — events are
  `{type:'phase', name}` plus the shared terminal `run_done`/`error`/
  `cancelled` (the server reuses `RunSseGenerator`); without the reset a
  second job inherits the previous fold.
- **`GET /api/agentic_search/scg` returns 503 while `scg.enabled=false`** —
  the client maps it to `{enabled:false, counts:null, sources:[]}` so the
  UI renders a calm Settings hint, never an error state. Don't treat that
  503 as a failure.
- Hover hints use the native `title` attribute — no Tooltip primitive is
  vendored and `@radix-ui/react-tooltip` isn't installed;
  `unavailable_reason` follows the idiom.

## Tier picker — one budget knob, threaded top-down

`AgenticSearchView` owns the persisted tier (localStorage
`agentic-search:tier`, validated against the literal list on read) and
passes `tier`/`onTierChange` to both panels' `SearchBar`; the pill renders
only when both props are present so legacy call sites stay valid. Sent as
`tier` on the `POST /runs` body — never a verification knob.

## Snippet rendering is injection-safe by construction

`ResultCard.renderSnippet` regex-parses ONLY `<mark>`/`<code>` tokens into
React elements and emits everything else as text nodes — no
`dangerouslySetInnerHTML` anywhere in these components. Snippets are
connector-derived text; keep it that way.

## What stays

- Server state flows through `useAgenticSearch.ts` hooks only; the view
  never calls `fetch` directly. `agenticSearch.ts` reuses `API_BASE` /
  `API_KEY` from `api/client.ts` — don't duplicate auth/base-URL logic.
- The catalog query is live (60s `staleTime`); `useMapJobStream` invalidates
  `SOURCES_KEY` (with `SCG_KEY`) on stream end so SCG-mapped tool ids and
  availability refetch after a map job.
- Shape vocabulary, theming tokens (`hsl(var(--…))`), and the
  library-first checklist from the console root apply to every card here
  (`ResultCard`, `AnswerCard`, `TraceDrawer`, `SrcAvatar`, …). The
  per-source `slot` maps to `--agent-N` tokens — reuse them, don't
  hand-pick agent colors.

## Landing inertness + URL-as-source-of-truth (#80)

**The URL is the single source of truth for `{workspace, active run}`.** Canonical
shape: `/search?ws=<workspace_id>&run=<run_id>`. `AgenticSearchView` DERIVES both
facets from `useSearchParams` — there is NO separate `runId`/`workspaceId`
`useState` (deleted). This makes URLs deterministic, shareable across browsers,
and Back/Forward correct (param removed ⇒ that view closes).

Transition contract (push = new history entry so Back works; replace = a
selection/derived correction, not navigation):

| Transition | `run` | `ws` | push/replace |
|---|---|---|---|
| Submit success (`handleSubmit`) | set new id (async from POST) | set submitting ws | **push** |
| Open/replay a run (`handleOpenRun`, all `onOpenRun` sites + chips) | set id | unchanged | **push** |
| Pick workspace on landing (`handlePickWorkspace`, create/edit success) | unchanged | set id | **replace** |
| Clear run ("Back to search", `clearRun`) | delete | keep | **push** |
| Run-only deep-link reconcile | unchanged | set from snapshot | **replace** |

`workspaceId = ws-param ?? localStorage("agentic-search:workspace-id")` — the
param ALWAYS wins; localStorage is the bare-`/search` fallback only (and is
mirrored from the resolved workspace so a later bare visit restores it).

INERT INVARIANT: a fresh `/search` visit (no `run`) lands on the inert landing
page and NEVER `POST /runs`. The active run seeds from `?run=` ONLY — opening any
such URL performs GETs only (snapshot via `GET /runs/<id>` + stream attach). Past-
query chips + autocomplete REPLAY via `onReplay(run_id)` → `handleOpenRun` → push
`run`; re-running is the explicit "Run again" affordance. **Sharability core:** a
`?run=` URL WITHOUT `ws` (or a mismatched one) reconciles `ws` from the snapshot's
`workspace_id` (`useRun` / live `stream.workspaceId`) once it resolves, so the
shared link renders the same run + workspace on ANY browser regardless of
localStorage. `done`/`answerReady` pair with the AUTHORITATIVE status (live stream,
else snapshot `status`) — a `running` snapshot never renders terminally.

## Workspace editing is a graph-lifecycle event (#83)

`WorkspaceModal` (edit mode) is reachable from EVERY workspace card — a `Pencil`
button beside the graph button in `LandingPanel` (`onOpenConfig(w)`) — plus the
hero search-bar Configure chip. The instructions textarea is framed as the
graph's purpose ("Purpose & instructions — codifies what this workspace's graph
is for; editing re-indexes the graph"). On a successful edit, `AgenticSearchView`
compares the prior workspace's instructions/desc/sources to the submitted values
and fires a `sonner` re-index toast ONLY when one of those moved (a name-only
edit stays quiet) — the smallest honest signal that the BE re-drove the map.

## Workspace graph view (#79)

`graph/` reuses the wiki `KnowledgeGraphRenderer` ENGINE via an injected
`GraphRenderConfig` (honest extraction — kind/edge/colour maps only; no fork).
`graph/types.ts` mirrors the API wire 1:1 (closed unions, exhaustive Record
maps); `scgGraphConfig.ts` owns the SCG palette/glyphs/layer grouping;
`useWorkspaceGraph` → `GET /workspaces/<id>/graph`. Schema edges address nodes by
`node_id` (the API remaps from `source_key`); unmapped sources render as ghost
nodes linking to the Sources map flow. Entry: workspace-card + results-rail.

## Testing

- vitest runs WITHOUT `globals: true`, so RTL auto-cleanup does not fire —
  every `.test.tsx` must call `afterEach(cleanup)` explicitly (established
  convention; see EditableTitle/ModelSummary/SecretField tests).
- jsdom lacks `ResizeObserver` and cmdk requires it; the stub lives in
  `src/setupTests.ts` next to the matchMedia stub, so tests mounting
  `SearchBar`/Command surfaces work out of the box.
