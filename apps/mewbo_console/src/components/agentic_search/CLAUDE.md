> ↑ [apps/mewbo_console/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md)

# Agentic Search ("Mewbo Search") — Console Subsystem Guidance

Scope: this file applies to `apps/mewbo_console/src/components/agentic_search/`
plus the colocated `src/hooks/useAgenticSearch.ts`,
`src/api/agenticSearch.ts`, and `src/types/agenticSearch.ts`. Captures the
non-obvious FE decisions for the multi-source search surface. Read the
console root `apps/mewbo_console/CLAUDE.md` first — the library-first,
theming, and shape-vocabulary rules all apply here unchanged.

## Landing first paint — lazy the run/graph children, not just the route

`AgenticSearchView` is `React.lazy()` at the `/search` route, but that only
defers the route — it does NOT shrink the chunk. The view statically imported
its heavy children, so the inert landing page (the common first paint) was
forced to download all of them: `ResultsPanel` → `AnswerCard` →
react-markdown/remark-gfm/rehype-highlight, and `graph/WorkspaceGraphDialog` →
the shared `Graph3DView` (`react-force-graph-3d` + three.js). Rollup pooled them
into one heavy chunk the landing chunk referenced statically.

Fix: `React.lazy` the run-only / dialog-only children (`ResultsPanel`,
`WorkspaceGraphDialog`) and wrap their render sites in `Suspense`. They already
render conditionally (`run ? …`, `graphWorkspace && …`), so the import fires
only on an active run / graph open. `LandingPanel`, `SourcesDialog`,
`WorkspaceModal` stay eager — they're light and `LandingPanel` IS the landing.

**The trap (verify, don't assume): React.lazy ≠ a build-time chunk-size win.**
A lazy boundary only helps if it removes the *static* import edge — confirm the
emitted landing chunk no longer statically references the heavy chunk
(`grep` the built `AgenticSearchView-*.js` for `markdownComponents`/`react-force-graph`;
dynamic `import()` refs are fine, top-level `import … from` are not). Here the
landing chunk dropped 104KB→32KB and the markdown + 3D-graph engine
(`react-force-graph-3d` + three.js — heavier than the old cytoscape build, so the
split matters more) now loads only on run/graph. Keep `cssMinify: "esbuild"`
(lightningcss chokes on the
composer-shell template literal).

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

## Synthesis card + trace fidelity

- **The synthesis is markdown — reuse the ONE renderer.** `AnswerCard` renders
  `answer.tldr` through the wiki's `buildMarkdownComponents` + react-markdown
  (`SYNTHESIS_MD`, built once at module scope; `onNavigatePage` a no-op — a
  search synthesis has no wiki-page links). Do NOT hand-roll a second renderer
  (the cross-subsystem single-renderer rule). The orchestrated runner's answer
  IS a markdown blob; `bullets` stay empty (echo-fixture-only), so only `tldr`
  needs rendering. Snippet injection-safety (`ResultCard.renderSnippet`) stays
  separate — results are connector text, the synthesis is LLM text.
- **Provenance chips are honest or absent.** `confidence` / `sources_count` are
  settle-derived BE-side from data-bearing probes — never invented FE-side; the
  `ConfidenceBar` is suppressed when `confidence <= 0` (unknown ≠ "0% faith").
- **Related questions stream on their OWN event.** The rail's "Related
  questions" come from a dedicated `related_questions` SSE event (the BE's parallel
  structured call), folded by `reduceRun` onto `stream.related_questions` — NOT
  `answer_ready`, and NOT the snapshot alone. This matters because the live view
  renders `toRunPayload(stream)` even after `done`; without the event the rail
  would stay empty until a reload re-hydrated the snapshot. The snapshot's
  `RunPayload.related_questions` carries the same list for replay/share.
- **The per-lane response panel is `agent_done.result`.** Each probe's terminal
  evidence (`EVIDENCE (pathway: …)` / `NO DATA …`) rides `agent_done.result`
  onto `TraceAgent.result`; `AgentBlock` renders it as a collapsed native
  `<details>` (KISS — no Collapsible), dead-ends (`empty`) railed in primary.
  The lifecycle line `text` is only the done-status — the evidence is the field.

## Results-page top band · honest stats · coordinator lane

- **The query lives ONLY in the SearchBar input.** The old band echoed it a
  second time in an italic-mono strip — deleted. Band structure is three calm
  rows: the centered compact composer (570px cap, same `ComposerShell`
  language as the landing hero) → the load-bearing no-sources warning →
  a `RunStats` meta row with the Copy-link + "N sources" Configure affordances
  right-aligned (`ml-auto`, real gaps). Status reads in exactly ONE place
  (`RunStats`).
- **`RunStats` never fabricates.** Streaming → `streaming · Ns` (live tick
  from `elapsedMs`). Done → joins only the EARNED parts: result count only
  when > 0 (the results column owns the empty state), seconds only when
  elapsed > 0. A finished run never renders `0.0s` — unknown duration is
  silence. Snapshot elapsed: `AgenticSearchView.snapshotDuration` prefers BE
  `total_ms`, else derives `created_at→completed_at` off the RunRecord —
  both already on the wire; nothing invented.
- **Coordinator lane renders honestly.** `utils.laneSource(agent, sources)`
  flags `source_id === ""` / catalog-unmatched lanes `isCoordinator` (the
  `scg-search` root lane emits). ProgressStrip / RightRail / TraceDrawer
  show a `Workflow` glyph instead of a blank `SrcAvatar` and HIDE the
  per-source count chip (no misleading 0). Keep the honest lane name.
- **cmdk identity trap: `CommandItem value` IS the hover/selection
  identity.** Identical values ⇒ all twins highlight together. Past-query
  items use `pastQueryKey(p, i)` (`run_id`, fallback `q-index`) — never the
  bare query text. And the rendered recents are `dedupePastQueries(...)`
  (normalized text, first occurrence wins = most recent since the BE
  prepends) so duplicates aren't selectable at all. Both consumers
  (SearchBar Recent group + LandingPanel example chips) share the two
  `utils.ts` helpers — don't fork the rule.
- **Workspace card meta shelf is single-line by contract**: footer is
  `mt-auto flex items-center justify-between gap-2 flex-nowrap`; avatar rail
  `min-w-0 overflow-hidden` (avatars `flex-none` clip, never squish); the
  action cluster + "N past" pill are `flex-none whitespace-nowrap`. `mt-auto`
  pins the row so description length never shifts it; the grid's stretch
  alignment keeps rows reading as one shelf.

## Stream state machine · dedup · composer · autocomplete

- **`stream.attached`, not `runId`, gates the live view.** `reduceRun` flips
  `attached` on the FIRST frame of ANY type (incl. `agent_start`); `useRunStream`
  dispatches an `attach` action seeding the known id on subscribe (a new id wipes
  the fold). Keying off `runId` renders an empty payload early; keying off
  `run_started` wedged the live leg on "Starting search…" if that opener dropped.
- **Dedup strictly by result id** — in the reducer AND `ResultsPanel.visibleResults`
  (snapshot↔SSE / echo defense). Shared id ⇒ same React key AND `result-<id>` DOM
  id ⇒ the "hover one, highlight its twin" symptom. First occurrence wins.
- **Composer seam = `ui/composer-shell.tsx`** (`ComposerShell` +
  `ComposerSendButton` + `composerSurface()`). `SearchBar` has ONE render path:
  both `variant="hero"` (tall landing) and `variant="compact"` (results topbar)
  render the SAME `ComposerShell`, differing only by surface/padding size tokens
  — so the landing and in-run composers are literally one component, not two
  look-alikes (the old `compact` branch hand-rolled a divergent flat pill row;
  deleted). Since the composer unification the shell is console-wide: Wiki's
  `QADock` and Apps' `HeroComposer` render it too, and Tasks' `composerCard()`
  shares the same `.composer-surface` chrome family (only its BODY/behavior —
  per-variant toolbar, running Queue/Stop — stays bespoke). `composerSurface()`
  takes `{elevation, halo}` only; radius is always `--composer-radius`.
- **Focus language is SHARED across every composer, via CSS not JS**:
  `composerSurface()` emits `.composer-surface` + a `data-halo` attr
  (`composerSurfaceData()`), and `index.css` owns the primary-tinted 4px bloom
  + border tint + 200ms ease-out (reduced-motion guarded), plus the
  `data-running`/`data-command` behavioural tints Tasks sets. Don't reintroduce
  a `--ring`-tinted `focus-within:` Tailwind halo on a composer — all composers
  must bloom identically.
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

## File map — `hooks/useAgenticSearch.ts` split by streaming concern

`hooks/useAgenticSearch.ts` is the TanStack Query surface ONLY (every
`use*` hook that reads/writes through `useQuery`/`useMutation`, plus the
query-key consts it exports for its siblings). The two SSE-streaming
reducers live as SEPARATE sibling files, not re-exported through the query
file — import them directly:

- `hooks/runStream.ts` — self-contained (`RunStreamState`, `reduceRun`,
  `useRunStream`, `toRunPayload`; no dependency on the query file).
- `hooks/mapJobStream.ts` — imports the query file's `mapJobsKey`/`SCG_KEY`/
  `SOURCES_KEY` for its terminal-invalidation (`MapJobStreamState`,
  `useMapJobStream`).

This mirrors the wiki's `api/hooks.ts` → `api/streamHooks.ts` split. Don't
grow `useAgenticSearch.ts` back into a streaming-reducer host — a new
event-log fold gets its own sibling file.

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

## Run config — ONE scope control, not competing pills

Tier + model + sources are ONE backend resolution (`run.model or the tier's
preset`, fanned across the workspace's sources), so they live behind ONE
progressively-disclosed control — `SearchScopeControl` — not three toolbar
pills fighting for attention. The composer toolbar is exactly two controls:
the workspace context pill and the scope pill. This is the design fix for
"everything visible, nothing important": the composer is the focal point;
power-user knobs disclose on click.

- **The run-config quartet is ONE `SearchScope` object prop** (`{tier,
  onTierChange, model, onModelChange}`, exported from `SearchScopeControl.tsx`),
  threaded `AgenticSearchView` → `ResultsPanel`/`LandingPanel` → `SearchBar` →
  `SearchScopeControl` as `scope`/`scope?`, not four individually-drilled props
  per layer. `AgenticSearchView` builds the one `scope` object (`useMemo`) and
  passes it down; a new run-config knob is one field here, not a new prop at
  every hop.
- **`SearchScopeControl` is its OWN atomic file** (`SearchScopeControl.tsx`),
  not inlined in `SearchBar` — one feature, one component. `SearchBar`'s toolbar
  is just `WorkspacePill` + `SearchScopeControl`; the control renders only when
  its `scope` prop is present so
  legacy call sites stay valid.
- **The pill names the resolved config at rest** — tier name + override-else-preset
  (`run.model or models?.[tier]`, via `formatModelName`) → `"Auto · claude-sonnet-4-6"`;
  falls back to the tier name alone while `useTiers()` is unresolved (never
  fabricate a model name). The info scent stays visible without opening anything.
- **ONE `DropdownMenu` hosts all three concerns — not a Popover.** Budget = the
  `DropdownMenuRadioGroup` (Fast/Auto/Deep two-line rows: name + depth/fan-out
  hint in prose, tier preset muted below it — a model name is a name, not
  machine text, so it stays out of the mono family). Model = a `DropdownMenuSub` whose
  `DropdownMenuSubContent` embeds the shared `ModelMenu` as a flyout. Sources =
  a `DropdownMenuItem` (avatars + count → `onOpenConfig`). A `DropdownMenuSub` is
  why this is a DropdownMenu and not a Popover: nesting the cmdk `ModelMenu`
  flyout inside a Popover would fight Radix outside-dismiss, and a sub-flyout
  needs no height/scroll hack (an inline-stacked model list pushes the budget
  rows off-viewport). The sub-trigger is controlled-open (`open`/`setOpen`) so a
  model pick inside the cmdk list — which is NOT a `DropdownMenuItem`, so Radix
  won't auto-close — can dismiss the whole menu.
- **Picking a tier CLEARS the override.** `AgenticSearchView.handleTierChange`
  wraps `setTier` + `setModel("")`; without the reset a stale override from a
  previous tier silently wins. The user re-deviates via the Model sub-menu.
- **`ModelMenu` is the extracted menu body of the wiki `ModelPicker`** (filter +
  Default row + ordered list), exported so the scope control embeds the SAME list
  in its sub-flyout. `ModelPicker` itself now renders exactly `ModelMenu` inside
  its Popover — honest extraction, no behavioural change to the wiki picker.
- **`TIERS` lives in `tiers.ts`** (the budget table — id/name/depth·fan-out hint),
  a non-component module so the control and any other consumer share one
  definition (DRY; it can't drift).

### Mobile viewport — the sub-flyout can't sit beside the parent

A nested side-flyout (`DropdownMenuSub`) geometrically cannot coexist with a
full-width parent menu on a phone-width viewport: parent (≈288px) + flyout
(340px) far exceeds ~375px, so Radix flips the flyout to the side with more
space and — because submenu collision avoidance does NOT shift on the main
(horizontal) axis — leaves it hanging off the edge. The durable fix, staying
within the DropdownMenu architecture (do NOT rewrite to a Popover):

- **Bind the flyout width to Radix's measured available width**:
  `w-[min(340px,var(--radix-dropdown-menu-content-available-width))]`. It keeps
  the full 340px on desktop and shrinks to fit the side it flips to on mobile.
- **`min-w-0` is load-bearing.** The shadcn `DropdownMenuSubContent` primitive
  carries `min-w-[8rem]` (128px), which FLOORS the width above and silently
  re-overflows — the width clamp looks applied but `min-width` wins. Always pair
  the clamp with `min-w-0`.
- **Narrow the PARENT menu on mobile** (`w-[min(18rem,calc(100vw-8rem))]`) so
  the flyout has a side to flip onto that stays on-screen; full 18rem on desktop.
- **Add `collisionPadding`** to both content + sub-content (16px gutter).
- **Shrink the toolbar pill labels on mobile** (`max-w-[78px] sm:max-w-[180px]`
  on the scope label, `max-w-[72px] sm:max-w-[140px]` on WorkspacePill) — the two
  inline pills otherwise push the composer toolbar past the viewport edge.

Verified concretely (headless Chromium, 320–1280px): trigger within viewport and
both poppers on-screen at every width; desktop keeps the 340px flyout. A
className-only assertion would be worthless here — drive a real narrow viewport.

The model override is **DELIBERATELY session-instance-only** (plain `useState("")`
in `AgenticSearchView`, no localStorage): trialling a custom model for one search
without a config edit; a reload restores the tier→model mapping. Sent as `tier` /
non-empty `model` on `POST /runs`; the BE echoes `RunPayload.model`.
`SearchBar.test.tsx` locks the resting-label + budget-row contract — the scope
pill is a Radix **DropdownMenu**, so tests open it via `keyDown(Enter)` (NOT
`click`, which is the Popover idiom); any test mounting `SearchBar` needs a
`QueryClientProvider` (useTiers). The Model sub-flyout stays unmounted in those
tests, so no `scrollIntoView` stub is needed.

## Rich result cards · the title-is-a-link contract

The card is a **reading surface, not a button**. The old whole-card `onClick`
toggle was a UI anti-pattern (a user expects a title click to NAVIGATE; expanding
showed only dead fixture data). The rebuilt contract:

- **Title = `<a href target=_blank rel=noopener>`** to `result.url` (schemed
  `https://` if bare) when a url exists; url-less cards render the title as plain
  text — NO dead `https://`+empty "Open" link, NO copy action.
- **No whole-card click-to-expand.** An explicit **More/Less** affordance renders
  ONLY when there is expandable content (`overflow meta` / `insight` / `refs` /
  snippet > 240ch). Nothing expandable ⇒ no toggle button at all.
- **Anatomy** (classic search-result): identity row (rank · `SrcAvatar` brand
  mark · source name · kind badge · relevance/confidence) → title link → url
  breadcrumb → snippet (`line-clamp-3` until expanded) → **meta chip row** →
  footer (author/timestamp **only when non-empty** — no dangling `·` — + actions).
- **`meta` IS the "card_meta" footer — there is NO second field.** The agent
  proposes an open-vocab scalar dict on each `scg_results` entry; the card
  renders it as a structured footer. A parallel `card_meta` would duplicate it
  verbatim (DRY) — `resultMeta.ts` is the generic *mechanism* that renders ANY
  key richly, so the wire stays one field. `metaChips` classifies each pair:
  byte-size keys (`size`/`bytes`/`filesize`/`disk`) → `formatBytes` (`24 KB`);
  count-ish keys → lucide icon + `compactNumber` (`46.2k`); date-ish keys/ISO
  values → `RelativeTime`; **`state`/`status` → a colour-coded STATUS badge**
  (`statusTone`: open=`--success`, merged/closed=`--permission`, failed=
  `--destructive`, draft=`--warning`, unknown=`--muted-foreground` — never
  dropped); language/license/version → tag chip; unknown → `label: value`. The
  classifier is the ONLY place keys gain meaning — add a key family here, never
  branch in `ResultCard`. ~6 chips at rest, rest fold behind `+N` into expanded.
- **Relevance/confidence are accessible**, not title-only tooltips: an
  `aria-label`led chip (the sweep fixed the old `title=`-only dot).
- **Per-card follow-up is wired**: the Sparkles button → `onAskFollowUp(result)`
  → `ResultsPanel.handleCardFollowUp` prefills the composer with
  `Regarding "<title>" (<url>): ` and focuses it (reuses the refine prefill+focus
  path — runs are independent, no session continuation). No handler ⇒ no button
  (no dead control). The fixture-only image/embed traffic-light chrome was DELETED
  (insight/refs stay — they're legitimately data-gated).

## Source brand marks (`sourceBrand.ts` + `SrcAvatar`)

Known servers (github/gitea/searxng·internet-search/huggingface/deepwiki/context7/
gitmcp) render their official **CC0 `simple-icons`** glyph over a brand-tinted
tile; unknown ids fall back to the catalog letter glyph. `sourceBrand(id)` matches
by NORMALIZED substring (so `mcp_github` resolves). Same stance as the wiki
`PlatformIcon` — never hand-roll provider SVGs, never add a third icon library.

## Results layout — content-sized centered grid, trace reachable at all widths

The grid is `minmax(0,900px) | 340px`, `justify-center`, `gap-x-8`, capped at
1320. The OLD grid (`minmax(0,1fr) | 270` + 760px main cap) pooled all leftover
width into ONE dead gutter (measured ~54% content). Content-sized columns +
`justify-center` distribute slack as balanced margins instead. Below 1100px the
rail hides and the grid collapses to one centered column — so the **Agent-trace
trigger also lives in the run-meta row** (`Layers` button), reachable at every
width (the rail held the only at-rest trigger).

**Density is VERTICAL rhythm, not the horizontal gutter.** The grid
fixed horizontal dead space; the remaining "too much empty space" was vertical
slack. The tightening pass lives in the inter-section margins (grid `py-4`,
`gap-y-4`; `mb-3/mb-4` between progress/answer/filter/results) and intra-card
padding (`ResultCard` `px-3.5 py-2.5` + `leading-normal` snippet; `AnswerCard`
`p-4`; rail `gap-3` + `p-3` panels). Do NOT recover density by shrinking
`gap-x-8` or the 900/340 columns — horizontal breathing is load-bearing for the
two-surface hierarchy; squeeze the vertical rhythm instead.

## Trace instrument fidelity (RightRail + TraceDrawer)

The trace surfaces read per-lane + run-level instrument data, all
**present-only / never-fabricated**:

- `TraceAgent` gains `kind`/`model`/`steps`/`duration_ms`/`input_tokens`/
  `output_tokens`/`results_count` (folded in `reduceRun`: kind+model on
  `agent_start`, the rest on `agent_done`). The lane **`name` IS the kind**; the
  model is a separate field.
- **RightRail `LaneRow`** leads with the kind, a prominent **result-count pip**
  (`ResultCountPip` — the headline "how much this tool contributed"; the
  count-pip vocabulary, `rounded`), then a metric strip
  (`model · N steps · duration · in→out tok · N filtered`) rendering only present
  parts. The count moved OUT of the strip into the pip because "must be
  clearer" — buried in a `·`-joined mono strip it read as noise. **`returned_count`
  vs `results_count`**: the pip shows KEPT (post-dedup); `returned − kept` is the
  "N filtered" in the strip (cards that collapsed into another lane's via the
  cross-emitter dedup), with the kept/filtered split in the pip's `title`. The
  coordinator lane never shows a per-source pip (its results carry connector
  source ids, never ""). **`RunStatsBlock`** reads `payload.stats` (probes · tool
  calls · tokens · `setup Xs · search Ys` split) — each field only when
  present/non-null; absent stats ⇒ no block.
- **TraceDrawer** header shows kind + model (was raw `agent.name`+`agent_id`);
  the hardcoded **"budget 8"** is GONE (it now reads `N lanes`). `humanizeMs` /
  `compactTokens` (in `utils.ts`) are the shared formatters.
- **Run-meta row affordances**: Agent-trace trigger, **Open agent session**
  (`/s/<session_id>` when stamped), **Cancel** (mid-run → `useCancelRun` →
  `POST /runs/<id>/cancel`; fire-and-forget, the stream's `cancelled` frame flips
  the view — mirrors the composer Stop), **Go deeper** (footer; re-runs one tier
  up `fast→auto→deep` via `onDeeper`, hidden at deep — clears the model override).
- **Filter chips**: zero-count kinds are HIDDEN (not greyed-out — a disabled chip
  looked identical to a populated one); the rail suppresses entirely with ≤1 real
  kind.

## Per-card confidence is honest-or-absent

`SearchResult.confidence` (optional) is the EMITTING AGENT's per-card
certainty from an `scg_results` entry — probe-emitted cards carry it,
connector-era cards don't. `ResultCard` renders the mono `%` chip only when
`confidence > 0`; absence renders nothing (never an invented 0% — the
`ConfidenceBar` suppression stance, per-card). It complements `relevance`
(the rank dot), it does not replace it.

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

## Landing inertness + URL-as-source-of-truth

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

## Workspace editing is a graph-lifecycle event

`WorkspaceModal` (edit mode) is reachable from EVERY workspace card — a `Pencil`
button beside the graph button in `LandingPanel` (`onOpenConfig(w)`) — plus the
hero search-bar Configure chip. The instructions textarea is framed as the
graph's purpose ("Purpose & instructions — codifies what this workspace's graph
is for; editing re-indexes the graph"). On a successful edit, `AgenticSearchView`
compares the prior workspace's instructions/desc/sources to the submitted values
and fires a `sonner` re-index toast ONLY when one of those moved (a name-only
edit stays quiet) — the smallest honest signal that the BE re-drove the map.

## Workspace graph view

`graph/` reuses the shared 3D `Graph3DView` engine (the SAME WebGL galaxy the
wiki Knowledge Graph renders) via an injected `SCG_GRAPH_THEME` — no Cytoscape,
no per-domain renderer. `WorkspaceGraphDialog` owns only the React lifecycle, the
SCG `NodeInspector` (capability schema / recipe / anchored notes), and the
unmapped-ghost hint; an SCG graph has no folder hierarchy, so it passes through
the engine's `CollapseModel` unchanged. `graph/types.ts` mirrors the API wire 1:1
(closed unions, exhaustive Record maps); `scgGraphConfig.ts` owns the SCG
palette/labels/layer grouping and builds `SCG_GRAPH_THEME` (+ `SCG_KIND_DOT` /
`SCG_KIND_LABEL` for the inspector header) — all from the existing `--graph-*`
token family, no icon SVGs or node shapes (the 3D engine has neither).
`useWorkspaceGraph` → `GET /workspaces/<id>/graph`. Schema edges address nodes by
`node_id` (the API remaps from `source_key`); unmapped sources render as ghost
nodes linking to the Sources map flow. Entry: workspace-card + results-rail.

**Landing health band reads the SUMMARY, never the full graph.** The
`WorkspaceHealthBand` (LandingPanel) shows four numbers off `stats`
(mapped-source coverage, node·edge size, memory notes), so it fetches the
light `GET /workspaces/<id>/graph/summary` (`useWorkspaceGraphSummary` →
`WorkspaceGraphSummary` = `Pick<WorkspaceGraph,"scope"|"stats">`) — NOT the
full node/edge graph. The full `useWorkspaceGraph` stays lazy behind the
`React.lazy` `WorkspaceGraphDialog`; the two share the BE's warm per-source
`query_nodes` cache. Don't point the band back at `useWorkspaceGraph` — that
re-downloads the whole graph on every landing just to render the stat strip.

## Testing

- vitest runs WITHOUT `globals: true`, so RTL auto-cleanup does not fire —
  every `.test.tsx` must call `afterEach(cleanup)` explicitly (established
  convention; see EditableTitle/ModelSummary/SecretField tests).
- jsdom lacks `ResizeObserver` and cmdk requires it; the stub lives in
  `src/setupTests.ts` next to the matchMedia stub, so tests mounting
  `SearchBar`/Command surfaces work out of the box.
