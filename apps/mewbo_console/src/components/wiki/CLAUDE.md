> ↑ [apps/mewbo_console/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md)

# MewboWiki — Console Subsystem Guidance

Scope: `apps/mewbo_console/src/components/wiki/`. The sibling `README.md` (marked
`<!-- mewbo:noload -->`) is the depth reference — full design, endpoint table, file map, Mermaid
invariants, Q&A streaming contract. Read it before any non-trivial change.

## Atomic class paradigm

Every standalone piece of behaviour here is an atomic class: state on the instance, behaviour as
methods, helpers as statics. Examples to copy from:

| Class | File | Pattern |
|---|---|---|
| `IndexingProgress` | `progress.ts` | static factories `fromJob` / `fromStream`, private `_compute` |
| `CollapseModel` | `collapseModel.ts` | pure visible-set + edge re-point/aggregate over an `expanded` set; static `initialExpanded`/`visibleGraph` |
| `Graph3DView` | `Graph3DView.tsx` | thin domain-agnostic 3D view over `CollapseModel`; injected `Graph3DTheme` + `renderInspector` render-prop |
| `RelativeTime` | `relativeTime.ts` | static-only, `Intl.RelativeTimeFormat` |

A free function that grows past two args and carries state across calls becomes an atomic class.

## Progress is computed in ONE place

`progress.ts:IndexingProgress` is the single source of truth for indexing progress UI. Both the
landing in-flight card (`LandingScreen.tsx`) and the indexing page (`IndexingScreen.tsx`) call
`fromJob(job)` / `fromStream(state)`.

**Never compute `pct` locally from `scannedCount/totalCount`.** A local computation pegs at 96% the
moment the run leaves the scan phase, because it never sees `phase`. The class reads phase,
sub-progress and `phaseStartedAt` from one typed input and produces
`{pct, phase, label, statusLine, etaSeconds}`.

**Sub-progress has a generic, phase-agnostic fallback beyond scan/pages.** `scan`
(`scannedCount`/`totalCount`) and `pages` (`pagesSubmitted`/`totalPages`) keep dedicated field
pairs; every OTHER phase reads the generic
`IndexingJob.phaseProgressCurrent`/`phaseProgressTotal`/`phaseProgressUnit` triple instead of
pinning at its floor. **One mechanism for every phase, not one field pair per phase** — a
`graphNodesBuilt` + `enrichEntitiesDone` + … scheme would re-create the staleness bug this removes
(a value belonging to a phase that already ended, read as if it described the current one). The BE
resets all three fields to `null` on every `emit_phase` transition, which is the invariant
`_compute` leans on: a non-null `phaseProgressCurrent` always belongs to the CURRENT `phase`, no
phase-name cross-check needed. Today only `graph` (nodes/edges) and `enrich` (entities) populate
it; `clone`/`plan`/`finalize` leave it unset and render at their floor pct with an empty status
line. A `phaseProgressCurrent` with no `phaseProgressTotal` yet still paints a status line (`"N
<unit> processed"`) even though there is no honest fraction.

**Within one phase the sub-progress denominator can change, and the bar can legitimately retreat —
that is not a regression to chase.** `build_graph.py` binds two trackers in sequence for the `graph`
phase alone: one counting files while parsing (`unit="files"`), then one counting nodes while
embedding (`unit="nodes"`), both writing the same `phase_progress_current`/`_total` pair. Finishing
parsing at 30 of 30 files and starting embedding at 0 of 500 nodes is a real drop back to 0%
*inside* the `graph` phase's `[20,32]` band. The `phaseProgressUnit` swap is what keeps the status
line honest across that switch — render it every time; dropping the unit turns an expected dip into
what reads as the run losing ground.

Phase weight table in `progress.ts`:

```
clone    [ 0,  5]
scan     [ 5, 20]
graph    [20, 32]
enrich   [32, 40]
plan     [40, 45]
pages    [45, 95]
finalize [95,100]
```

Calibrated to real run shape — clone/scan are fast, page generation is the LLM-bound long tail.
Don't widen `pages` past 95; the 5% headroom is what stops the bar looking stuck at "100% but not
done yet".

**Adding a phase is a lock-step edit.** A new phase lands in `IndexingPhase` (`api/types.ts`) AND
all four `progress.ts` lookup tables (`PHASE_RANGE`/`PHASE_LABEL`/`PHASE_SHORT_LABEL`/`PHASE_ORDER`)
in the same change — each is a `Record<IndexingPhase, …>`, so `tsc` enforces exhaustiveness.
`PHASE_SHORT_LABEL` is the one-word name the phase bar and the plan outline spend, where the
sentence-length `PHASE_LABEL` would wrap seven times across a strip. `enrich` sits
post-AST to honour the GraphRAG ordering law: the entity KG is built before pages are written.

## ETA is measured-rate-or-nothing, scoped to the current phase

The BE writes `IndexingJob.phase_started_at` on every `emit_phase`. The FE computes remaining time
**for the current phase only**:

- `pages` with ≥1 page committed: `(elapsed / pagesSubmitted) * remainingPages`.
- `scan` with ≥1 file scanned: same on files.
- Any other phase with a known `phaseProgressTotal` (`graph`/`enrich` today): the same generic
  `elapsed / sub` extrapolation.
- Otherwise: **no ETA** — `etaSeconds: null`.

**Never fall back to a fixed per-phase budget.** On a multi-thousand-file repo a hand-sized budget
is off by 15-20×, and because a constant never changes the bar freezes with a stale, non-decreasing
ETA for the phase's whole duration, which reads as "stalled". Guessing for a phase that hasn't
started is the same dishonesty one phase early. **An absent ETA is honest; a stale one is not.**

`IndexingProgress.formatEta(seconds)` returns `""` when ETA is null, 0, NaN, or Infinity — render it
inline, the empty string is safely no-op'd in JSX.

`IndexingJob.lastProgressAt` (ISO, any-phase, unlike the per-phase `phaseStartedAt`) rides the same
wire shape but is NOT consumed by `progress.ts` — reserved for a future staleness indicator that
needs a product-specified threshold this class doesn't own an opinion on.

## Log timeline — full history in STATE, a bounded tail on SCREEN

The indexing-page reducer keeps EVERY log event in state. The view renders a bounded tail of them
and **says so** ("last 250 of 5,468 lines"). Those are two different rules and both are load-bearing.

**Never trim the log in the hook** (`api/streamHooks.ts`, where `useIndexingStream` lives;
`hooks.ts` stays the plain TanStack Query surface). A `slice(-20)` there makes every refresh appear
to show *different* logs, because the visible window shifts forward as the total grows — and
replaying SSE from idx 0 is free, so the trim buys nothing and costs determinism.

**Do bound what is RENDERED** (`indexing/activityModel.ts` `RENDER_LIMIT`). A scroll container costs
one DOM node per line whether or not it is on screen. Once the backend gained per-step granularity a
single mid-size repository emitted 5,468 lines while still in the graph phase — measured at 22,205
DOM nodes on a *loading screen*, one spinning glyph per row. The rendered window is now capped and
the count is PRINTED, which is what separates a bounded view from a silent trim: the numbers say the
history is longer, so two visits can't look like two different runs. Same page after the rebuild:
1,164 DOM nodes. History (per-file scan rows, distinct from `logs`) stays trimmed to 9 — a
recent-activity blip, not an audit log.

**Log lines carry `step`, and dropping it is a silent feature loss.** The backend stamps the open
declared step onto every line it writes (`LogJobEvent.step`); `reduceIndexing` folds it and
`ActivityFeed` groups consecutive lines under that step's declared label. Without it a reader gets
thousands of undifferentiated rows — a producer whose field no consumer reads is dead on arrival.
A line with NO step among attributed ones is work that escaped its declared scope, and the pane
labels it as such rather than rendering an anonymous block: measured on a live index, 102 of 5,470
lines were unattributed while the coverage gate was green, because that gate only ever replayed the
graph phase. **Make the gap visible; an invisible gap is how it survives a green suite.**

## The indexing screen is four fixed regions, and the root never scrolls

`IndexingScreen.tsx` composes; `indexing/` holds the parts. The shape is the contract:

| Region | Owner | Rule |
|---|---|---|
| Header | `IndexingScreen` + `indexing/PhaseBar` | headline = the OPEN STEP's label, not the phase; ONE positional readout |
| Recovery / scope bands | `IndexingScreen` | `shrink-0`, and any error text is height-bounded |
| Body | `indexing/PlanOutline` + `indexing/ActivityLog` | two panes, each owning its OWN `overflow-y-auto` |
| Footer | `IndexingScreen` | always reachable — never pushed below a fold |

The previous version appended every region beneath the last, so the panel grew as the backend
declared more work and the PAGE scrolled with a second scrollbar nested inside it. A loading screen
that grows a scrollbar as it works pushes its own Cancel control off-screen. So: the screen root is
`overflow-hidden`, exactly two `data-scroll="pane"` containers exist, and
`__tests__/wiki/indexingLayout.test.tsx` pins both — jsdom performs no layout, so it asserts on what
is RENDERED and where the scroll containers are, which is what actually regressed.

**Never render the declared plan flat.** `indexing/planModel.ts` (`IndexingPlan`, pure and
React-free) folds the flat `StepRecord[]` into phases; the outline discloses the running phase and
collapses the rest, following the run as it advances. Twenty-odd rows each shouting `DONE` in a
caps-locked column is the wall this replaced — state is a glyph plus screen-reader text
(`indexing/StepGlyph.tsx` + `stepState.ts`, split because a `.tsx` exporting both components and
functions trips `react-refresh/only-export-components` under `--max-warnings=0`).

## SSE consumer uses `fetch`, not `EventSource`

`api/client.ts:sseStream` is a thin wiki-scoped wrapper (base path + auth header) around the
**shared** `src/api/sse.ts:sseStream` — the console's ONE generic SSE parser, also consumed by
Agentic Search. ⚠️ It reads like it belongs to a single feature and does not: deleting it with a
feature would silently break wiki indexing, wiki Q&A **and** search.

It opens the connection via `fetch(..., { headers: { Accept: "text/event-stream" } })` and reads the
`ReadableStream` body line-by-line, because `EventSource` (1) doesn't allow custom request headers,
so the API key would need smuggling through a cookie or query param and the Q&A endpoint is POST
anyway, (2) doesn't integrate with `AbortSignal` for clean unmount cancellation, (3) can't POST
bodies (`streamAnswer` needs one).

`parseSseStream` walks lines per frame looking for `event:` and `data:`, ignoring `id:` — auto-resume
isn't a goal, since the BE replays from idx 0 when no header is sent.

## Graph3DView — the one shared graph engine

`Graph3DView.tsx` is the SINGLE graph renderer in the console (`react-force-graph-3d`,
three.js/WebGL). It is **domain-agnostic**: node `kind`/`layer` are opaque strings, every
colour/label/layer/size comes from an injected `Graph3DTheme`, and the caller supplies the side
panel via the `renderInspector` render-prop. Both the wiki Knowledge Graph and the SCG workspace
graph satisfy the structural `{nodes:[{data}], edges:[{data}]}` wire shape, so each is a THIN
adapter:

- **Wiki** — `KnowledgeGraph3DScreen.tsx` assembles `WIKI_GRAPH_THEME` from `graphTheme.ts` and
  wires the typed `inspector/GraphInspector` registry.
- **SCG** — `agentic_search/graph/WorkspaceGraphDialog.tsx` assembles `SCG_GRAPH_THEME` from
  `scgGraphConfig.ts` and renders its own `NodeInspector`.

`Graph3DView` owns the toolbar (name filter, kind chips, per-layer toggle group, Fit/Reset), the
click→pick selection, and composes the pure `CollapseModel` for folder LOD. **Don't wrap
`<ForceGraph3D>` in a bespoke imperative scene class** — the library owns camera, picking, force
layout, hover labels and nav.

**Multiplex layers (not AST-only).** Each wiki node carries `data.layer ∈ ast|entity|memory` with
`kind` widened by `External|Entity|Memory`; edges add `ANCHORS|RELATES`. The `kind`/`layer` unions
stay CLOSED so every `Record<Kind,…>` map in `graphTheme.ts`
(`KIND_VAR`/`EDGE_VAR`/`KIND_DOT`/`KIND_LAYER` + `ALL_NODE_KINDS`/`LAYER_ORDER`/`LAYER_LABEL`/
`LAYER_DOT`) is exhaustive and `tsc` flags a missing row — never loosen to `string` (the open-vocab
entity verb rides the edge `label`, not `kind`). In 3D, kinds differ by COLOUR (folders by a size
boost), not shape. The per-layer toggle cascades into the engine's `hiddenKinds` set via `kindLayer`;
hiding a layer's kinds drops its edges through the `linkVisibility` endpoint-hidden rule. New layer
tokens go in BOTH `:root` and `.light`.

### Code Galaxy — `/wiki/graph`

- **3D is the GPU path, not decoration.** `react-force-graph`'s *2D* build is plain Canvas, which
  walls at ~2–5K nodes; only the *3D* build is WebGL. The worst real graph is ~17K nodes / ~68K
  edges — far past what a 2D canvas carries.
- **Directory hierarchy IS the level-of-detail — that's the crash fix, not a fancier renderer.** The
  BE `?hierarchy=1` wire (`mewbo_graph` `FolderTree`) adds `Folder` supernodes + `parentId`; folders
  start collapsed except depth-1, so the live force sim is a few hundred nodes, never 17K.
  Cosmograph/GPU-sim, server-side layout precompute, tile streaming and Louvain/Leiden clustering
  were all deliberately CUT — the folder tree IS the clustering at this scale.
- **`External` nodes hang off a synthetic bucket, and it starts COLLAPSED.** The BE mints one
  top-level `Folder` per graph (`kind: "Folder"`, id `folder:__external__`, label `External`,
  `parentId: null`, `folderPath: ""`) and parents every `External` under it. `collapseModel.ts`
  exports that id as `EXTERNAL_BUCKET_ID` and `initialExpanded` EXCLUDES it. **Externals must have a
  parent**: `isVisible` walks a node's ANCESTORS, so a parentless node never enters that loop and
  renders unconditionally, while real symbols below depth 1 fold into collapsed ancestors and draw
  nothing. Measured on a live repo in that state, the top 20 nodes by degree were 10 language
  builtins and 10 `File` nodes, with not one real function — and a naive depth-1 expand rule
  restores exactly that. Two knock-ons the empty `folderPath` forces: the wiki theme sets `externalBucketVar` (a distinct fill so the bucket reads apart from a
  real source folder), and `nodeLabel` uses `n.folderPath || n.label` — `??` would keep the empty
  string and render a bare `" · N items"`.
- **`CollapseModel`** is pure: visible-set + edge re-point/aggregate over an externally-held
  `expanded` set. `Graph3DView` is thin glue (`graphData = useMemo(() => model.visibleGraph(expanded),
  …)`). The bucket is a genuine `Folder` structurally, so it folds and re-points like any other
  supernode — `initialExpanded` is the ONE place that knows its id.
- **`graphTheme.ts` is the single kind→colour/layer/size home**, and also houses the presentation
  maps the toolbar reads. Both adapters draw from it via their `Graph3DTheme`. Don't re-duplicate
  the palette maps or the `cssVarColor` hsl-comma-normaliser.
- **Per-frame accessors must be O(1) AND identity-stable across renders.**
  `linkVisibility`/`nodeColor`/`nodeVal`/`linkColor`/`linkWidth` run per element every frame — look
  up via an id→node/kind `Map` built in the `graphData` memo, never `nodes.find()` (that was O(N·E)
  per frame), AND wrap each in `useCallback` keyed on its actual deps so `<ForceGraph3D>` isn't
  handed a fresh closure prop every render.
- **The toolbar strip lives in `Graph3DToolbar.tsx`**, a thin presentational component —
  `Graph3DView.tsx` computes every derivation and hands them down as props. `Graph3DView.tsx`'s
  exports are imported directly by `agentic_search/graph/WorkspaceGraphDialog.tsx` and
  `scgGraphConfig.ts` — never move or rename anything it exports.
- **Selection → the typed `inspector/` registry** — an exhaustive `Record<GraphSelectionKind,…>` (a
  missing kind is a `tsc` error, no silent default), one atomic panel per kind. Shared neighbour
  lookups go through a plain-`Map` `GraphIndex`, no graph library.
- **Escalation if heavy folder expansions ever choke:** `react-force-graph` runs d3-force on the MAIN
  thread → precompute layout server-side (Graphviz `sfdp`) and ship static `fx/fy/fz`. Not built.

## Route additions go through `router.ts`

`router.ts` exposes `buildHref({ kind, ... })` and `useWikiRoute()`. When adding a route variant,
extend the `WikiRoute` union AND the parser AND the builder in lock-step — the type system flags
drift. Current variants: `landing`, `configure`, `repo`, `indexing`, `page`, `qa`, `graph`. Don't
bypass `buildHref` to write a string URL inline.

## Brand glyphs come from libraries, not bespoke SVG

- `ModelBrandIcon` (main components) resolves an LLM model id to a provider glyph via
  `getProviderIcon`. Reuse it for any model-picker surface here.
- `PlatformIcon` (`configure-wizard/PlatformIcon.tsx`) uses `simple-icons` (CC0) for Git platforms,
  falling back to `@lobehub/icons` for Azure DevOps (dropped from simple-icons under Microsoft
  trademark policy).
- `BrandMark.tsx` — the in-house clay flower, only for the MewboWiki brand.

Never add a third icon library. Never hand-roll provider SVGs.

## Repository badge ("Copy badge")

`badge.ts:WikiBadge` is the atomic class behind the `WikiTopBar` "Copy badge" popover — the snippet a
maintainer pastes into their README.

- **Artwork is a single static external CDN SVG** (`WikiBadge.IMAGE_URL`, on `cdn.thekrishna.in`),
  identical for every repo; only the *link* is per-repo. That's why the feature is pure-FE, with no
  backend. This is a remote image asset, NOT an exception to the no-bespoke-provider-SVG rule — we
  render `<img>`, we don't hand-author a glyph.
- **The link target is the repo's `landingPageId`**, not a fixed page id. `landingPageId` (on
  `Project`, set at finalize, validated to exist) is the canonical "enter this wiki" target;
  `LandingScreen` tile-click uses it too. There is NO universal `landing-page` sentinel — never
  hardcode a page id for a repo-level deep link. `WikiScreen` passes
  `badgePageId={landingPageId ?? pageId}` (the current page is the always-valid fallback), and the
  affordance is gated on `WikiBadge.forPage(...)` returning non-null.

`WikiTopBar`'s copy actions go through `@/utils/clipboard:copyText` and the shared
`@/components/CopyButton` — don't re-inline an execCommand fallback.

## `ConfigureWizard` — non-JSX logic lives in `configure-wizard/wizardState.ts`

`useWizardMachine()` owns everything the wizard decides that isn't markup: state, model-seed effect,
platform auto-detect effect, branch query, `gitSlug`/`catalogSlug` derivation,
`validate`/`goNext`/`goBack`, and both submit paths — mirroring the `useQaConversation` pattern.
`ConfigureWizard.tsx` renders what it returns; the three steps are their own files (`StepSource`,
`StepGeneration`, `StepScope`) taking `{state, set}` as dumb consumers.

**Catalog wizard (docs-only projects).** A `git|catalog` toggle; the catalog branch renders
`CatalogDocsForm` and submits via `POST /v1/wiki/projects/{slug}/documents` (no git URL, no clone
token).

**Branch picker (generation step).** A plain native `<select>` reusing the language-selector pattern
(bordered flex box + `GitBranch` icon + overlay `ChevronDown`) — NOT a shadcn `Command`/combobox: the
option set is tiny and static per repo. Branches load via `useBranches` (`POST /v1/wiki/branches`),
keyed `["wiki","branches", repoUrl, Boolean(token)]` — **the cache key carries token-*presence*
only, NEVER the raw token** — `enabled` only once the URL looks like a git URL, `retry: false` so a
bad URL/token doesn't hammer the endpoint. First option (value `""`) is `Default · <defaultBranch>`;
loading shows a disabled "Loading branches…"; error/empty falls back to the default option alone
(the failure is swallowed — no toast, no retry). `state.ref` is spread into the submission ONLY when
non-empty, so an untouched picker submits no `ref` and the BE clones the default branch.

**Developer mode — graph-only onboarding.** When `config.runtime.developer_mode` is on (via
`useConfig`), the generation step reveals a "Developer mode" subheading grouping the single "Graph
only — skip documentation (no LLM)" `<Switch>` that sets `graphOnly`; the whole section is HIDDEN
(and `graphOnly` omitted) when dev mode is off — the subheading exists so the toggle reads as
dev-only, distinct from the normal generation options above it (the branch picker is one of those,
NOT under this heading). A project that comes back `graphOnly:true` renders `GraphOnlyEmptyState` in
`WikiScreen` — a "No documentation available" panel whose PRIMARY CTA is `buildHref({kind:"graph"})`;
it REPLACES the docs grid and suppresses the `QADock` (nothing to ask). A graph-only run's progress
simply skips enrich/plan/pages (a `graph`→`finalize` jump); don't special-case the bar.

## Freshness badge (`FreshnessBadge` + `useProjectFreshness`)

How far a project's indexed wiki has drifted from its repo's remote HEAD. The badge owns its own
`useProjectFreshness` query (`GET /v1/wiki/projects/<slug>/freshness`, `staleTime` 5 min,
`retry: false`) rather than taking data as a prop — freshness is per-card, lazy, and never worth
blocking a gallery render on.

- **Render nothing unless there's something to say.** Error / absent / indeterminate → `null`. The
  state table mirrors the API contract EXACTLY: `behindBy === 0` → "Up to date"; `behindBy > 0` → "N
  commits behind"; `behindBy == null && remoteSha && remoteSha !== indexedSha` → "Update available"
  (the BE knows the sha moved but couldn't count commits — never collapse this into a false green);
  anything else → nothing.
- **The freshness classes are derived by the shared `classifyFreshness` kernel** (`wikiStatus.ts`),
  NOT re-implemented here — the wiki page's index-status card reads the same contract, and one
  kernel keeps the two from disagreeing on what "behind"/"update"/"fresh" mean. The badge renders
  `null` for the kernel's `unknown` class; the card, which must always speak, refines `unknown`
  further.
- **The badge does not own a refresh.** In a drift state it becomes the click target for the
  project's EXISTING re-index CTA (`onRefresh`), so there is one refresh path, not two.
- **The per-card burst on gallery mount is deliberate and bounded SERVER-side** (5-min TTL cache +
  `gthread` workers + the lazy credential chain — see the api wiki `CLAUDE.md`), so `LandingScreen`
  mounts one badge per card with no client throttle. Do NOT "fix" this with a bespoke client cache or
  request queue — that's the forbidden custom-TTL pattern. If galleries grow enough to matter, the
  `enabled` prop is the intended seam (gate on visibility).
- `getProjectFreshness(slug, force)` sends `?force=1` to bust the server TTL — reserve it for an
  explicit "check again" affordance.
- **Deliberately NOT migrated onto the shared `Badge`.** Two independent blockers: the `update`
  state's `--primary` colour has no `BADGE_COLOR_MAP` key, and `Badge`'s contract
  (`{children, color}` → a bare `<span>`) can't express an icon, a clickable-button variant, or a
  responsive short/long label swap. Revisit only if `Badge` grows those affordances generally.

## Wiki index-status card (`RefreshThisWiki` + `wikiStatus.ts`)

The card in the wiki page's right aside (and the mobile ToC Sheet — both mounts render the SAME
component) answers ONE question at a glance: *is this wiki current, and do I need to re-index?* It is
**verdict-first**: the judgment up top, raw numbers demoted below it.

- **The verdict is derived, not presented raw, by `deriveWikiStatus`** (`wikiStatus.ts`, pure +
  React-free + unit-tested). Seven verdicts: the five a reader acts on —
  `up-to-date`/`behind`/`update-available`/`indexing`/`failed` — plus two honest indeterminate ones,
  `checking` (probe in flight) and `unknown` (remote couldn't be read). `unknown` exists so the card
  NEVER shows a false green when it doesn't know; a matched sha or an authoritative `upToDate` still
  resolves to `up-to-date` even when the platform couldn't count commits.
- **Precedence is operational, not by severity.** A running job dominates a stale count (drift is
  meaningless mid-index); a terminal-but-incomplete job dominates a clean freshness read (what
  you're reading is the older good snapshot). So the card consults THREE contracts:
  `useProjectFreshness` for drift, `useActiveIndexingJobs`/`useRecoverableJobs` (filtered to this
  slug) for the indexing/failed states — the same polled queries the landing page uses, no new
  endpoint.
- **State-aware prominence: calm when current, loud only when action is needed.** Fresh / checking /
  unknown → a neutral re-index button; behind / update / failed → a primary one; indexing → the
  button becomes a "View progress" link (you can't re-index mid-index). This is the `AppFreshness`
  "always says something" precedent, NOT the `FreshnessBadge` "silence when fine" one.
- **Status is never colour alone** — every verdict pairs a glyph with the word, and the verdict row
  is a `role="status"` live region carrying `data-kind`. Tone paints from the status token families;
  the `-text` variants are used for text-on-tint, and because the status tokens carry NO embedded
  alpha the `/12` glyph-tile tint is legitimate.
- **One refresh path, unchanged.** The re-index Button IS the project's single re-index CTA — the
  same `useRequestWikiRefresh` + confirm/queued phase machine `FreshnessBadge` and the settings
  dialog hand off to via `openSignal`. Branch + commit are demoted to quiet muted-2xs mono reference
  links (machine text keeps the mono face).

## Editable project settings — `ProjectSettingsDialog`

Post-onboarding CRUD over a project's indexing settings: `GET /v1/wiki/projects/<slug>/settings` +
`PATCH /v1/wiki/projects/<slug>`. Trigger = the `WikiTopBar` gear (`showSettings`, on every settled
in-project screen) and the gallery card's hover cluster. **react-hook-form + zod in a shadcn
`Dialog`** — NOT RJSF: that machinery is for the schema-driven global `AppConfig`; a Project is a
fixed-shape REST resource.

**The pure form model lives in `projectSettingsForm.ts`** (zod `settingsSchema`, `FORM_FIELD_BY_WIRE`,
`INDEX_TIME_FIELDS`/`needsReindex`, `EMPTY_FORM`/`seedFrom`/`splitLines`/`buildPatch`) — no JSX, no
React, unit-testable on its own. `ProjectSettingsDialog.tsx` is the rendering + rhf-wiring surface;
the four select fields (Branch/Depth/Language/Filter mode) render through one shared
`SettingsSelectField` wrapper. `splitLines` is also what `ConfigureWizard`'s scope step reuses for
its dirs/files textareas — don't re-fork it.

- **The server's `editable` map is the SOLE field gate, and it fails closed** (`canEdit(f) ===
  settings.editable?.[f] === true`). The server always emits EVERY key with an explicit bool, so
  `graphOnly: false` (developer mode off) is how the switch stays hidden — the client never provokes
  the 403. It's also why the reduced `{kind:"catalog"}` payload collapses to just its description.
- **`editable` also flags `repoUrl` + `platform` `true` — do NOT render them.** The server accepts a
  *cosmetic* re-normalisation (`.git` suffix, scheme/host case) but 409s a real
  `(host, owner, repo)` change, because the slug keys the project's pages, jobs and credentials.
  Repo URL stays READ-ONLY identity; the honest way to re-point a wiki is delete + recreate.
  `ProjectSettingsField` excludes both so the field→form map can't grow an input for them.
- **Only the DIRTY subset is PATCHed** (`buildPatch` over rhf's `dirtyFields`). `ref: null` is an
  explicit "clear to the default branch"; an ABSENT `ref` means "leave the pin alone" — not
  interchangeable.
- **Never name a form field `ref`.** It collides with the `ref` key on rhf's own `field` object: the
  control renders and even records itself in `dirtyFields`, but `formState.isDirty` never flips, so
  Save stays disabled and a real edit is silently un-saveable. The form field is `branch`; only the
  WIRE key is `ref`. (`useFormState` does NOT work around it.)
- **The settings DTO is camelCase** (`filterMode`/`graphOnly`/`scopeType`), like every other wiki
  wire shape; the server keeps snake_case internals behind Pydantic aliases. The `editable` map is
  keyed by the same camelCase names.
- **Errors stay inside the dialog, never a toast.** A 403 (dev-mode gate) pins to the graph-only
  switch, per-field 400s pin to their inputs, everything else (409 identity edit, 5xx) fills the
  dialog banner. Success = close + refresh.
- **No inline token field, ever.** Credential coverage is a read-only line from the server-resolved
  `credential` (the ONE credential chain), falling back to the shared `matchCredential()`
  (`api/git.ts`) only when the payload omits it. Changing a credential deep-links to Settings →
  Security & Access.
- **A changed `ref` invalidates `["wiki","freshness",slug]`** as well as the projects list —
  otherwise `FreshnessBadge` keeps reporting drift against the OLD branch.
- **A PATCH re-indexes NOTHING, and `desc` is the one exception.** Every other field takes effect at
  the next index; `desc` writes through to the `Project` snapshot immediately (finalize reads the
  override back, so a reindex won't clobber it). The footer copy switches between those two messages
  off the dirty set, and an index-time save hands off to the screen's EXISTING re-index CTA instead
  of minting a second refresh path. Auto-queuing a reindex from the PATCH was deliberately NOT done
  — a contract change that drags in the per-IP indexing rate limiter.
- **Test trap:** `form.reset(seedFrom(dto))` runs in an effect AFTER the render that first shows the
  form, so asserting or typing as soon as Save appears races the seed and gets clobbered. Wait on a
  field whose DTO value differs from the inert default (`awaitSeeded`).

## Q&A vs indexing — distinct streams

`useIndexingStream` and `useQaStream` look similar but have distinct event unions and reducers.
Don't merge them — indexing is one-shot per job, Q&A one-shot per question.

`AbortSignal` semantics differ too. Indexing abort = stop subscribing (the run keeps going
server-side); cancel = `DELETE /v1/wiki/index/<id>`. Q&A has no explicit cancel endpoint — unmount =
subscriber gone, server stops streaming when the connection closes.

## Idempotent Q&A URL — `?answer=<id>`

A completed answer is addressable, so a refresh/share replays it with ZERO LLM. The whole fix is FE
— the backend already persisted the full answer (`GET /v1/wiki/qa/<id>` → `blocks` +
`summarySources` + `accessedSources` + `modelsUsed`), it was just only read for the footer. Wiring:
`router.ts`'s `qa` variant carries an optional `answer` id (parse + `buildHref` in lock-step);
`QAScreen`, when it has an `answer` id it did NOT publish itself, renders from
`useQaAnswerSnapshot(id)` and feeds `useQaStream(null)` (a genuine no-op — no POST). On a fresh ask,
the `meta` event's `answerId` is folded into the URL via `navigate(replace)`; `publishedAnswerRef`
keeps our OWN id from flipping the live stream into snapshot mode. Known edge: a refresh
mid-generation shows the partial persisted snapshot (still no LLM re-invoke), not a resumed stream.

**The SAME `answer` id addresses a whole multi-turn conversation, not one turn.**
`publishedAnswerRef`'s job is exactly one thing: fold a COLD ask's freshly-minted id into the URL
once. It does NOT decide live-vs-snapshot mode (that's `liveInput === null`) — don't conflate the
two — collapsed into one boolean, follow-up-without-navigation is inexpressible.

## Multi-turn follow-up

**All the state/effects live in `useQaConversation.ts`, not `QAScreen.tsx`.** `QAScreen` calls the
hook and renders `renderedTurns` + `onAsk`. The hook is unit-tested directly
(`__tests__/wiki/useQaConversation.test.tsx`) independent of mounting the two-column layout. This
section documents the RULES it encodes.

- **Wire contract (FE side).** `POST /v1/wiki/qa` takes an optional `answerId`; `QaAnswer` gains
  additive `question`/`turns: QaTurn[]`. `useQaStream`'s input gains the matching optional
  `answerId`, threaded straight into `streamAnswer`'s POST body. The new `QaAnswer`/`QaTurn` fields
  are typed OPTIONAL specifically so an older persisted answer or test fixture still validates.
- **One instance, stacked turns, no route change.** `QAScreen` holds `sessionTurns: RenderedTurn[]`
  (frozen at each follow-up) + `liveInput: {question, answerId?} | null` (`null` ⇒ pure-snapshot
  render). `onAsk` (wired from `QADock`, whose own `onAsk(question: string)` contract is UNCHANGED)
  no longer calls `navigate` — it freezes the current turn into `sessionTurns` and re-points
  `liveInput` at the SAME `answerId`, which changes `useQaStream`'s derived `key` and reopens a
  fresh stream on the continued backend session. Every turn — live or historical — renders through
  the identical in-file `TurnView` sub-component, stacked with a `border-t` divider. Follow-up is
  gated on the active turn being `done` — a mid-stream submit is a silent no-op, so a partial turn
  can never freeze into history.
- **`isSnapshot` is decoupled from the publish-ref**: `isSnapshot = liveInput === null &&
  Boolean(answerId)`; the id-fold effect fires once per COLD ask (guarded on the `answerId` PROP
  being absent, not on publish-tracking).
- **`useQaStream`'s reducer is unchanged on purpose.** It still fully resets on every `meta` event —
  correct, because each hook invocation is legitimately ONE in-flight turn; accumulating turns is
  `QAScreen`'s job. The one addition is a `settled` flag (`folded key === live key`) so `QAScreen`
  paints a skeleton instead of flashing the PREVIOUS turn's blocks in the round-trip window after
  swapping `liveInput`.
- **Organic orchestration — no FE-side "follow-up mode" branching.** Whether the backend re-probes or
  reuses prior context on a continuation turn is the hypervisor's call, server-side and
  prompt-driven. The FE's only job: reuse the id, stack the render. No turn-count cap either —
  `sessionTurns`/`QaAnswer.turns` grow unbounded, same as the backend relies on session compaction
  rather than a hard limit.

## Q&A answer rendering — one renderer, one citation grammar

The answer area is one slot, not two parallel components. The skeleton resolves via
`hasBlocks ? answer : error ? error : stream.done ? empty : skeleton` — the `stream.done` branch (on
BOTH columns; left guards `summarySources !== null || stream.done`) is what stops the skeleton
dangling forever on a zero-block completion. `hasBlocks` excludes `sources`/`accordion` blocks so a
sources-only stream still hits the terminal branch.

- **One renderer.** `markdownComponents.tsx:buildMarkdownComponents()` + the shared `SrcChip` is the
  SINGLE markdown component map; both `LiveBlocks` (streaming prose) and `MarkdownBlock` (wiki
  pages) consume it (`remarkGfm + rehypeHighlight + rehypeSlug`). A second, reduced renderer drops
  `ol`/`table`/`blockquote`, loses code highlighting, and degrades citations to plain links — all
  silently. `LiveBlocks` does NOT render the `sources` block (that feeds the right panel).
- **One citation grammar.** `citations.ts:CitationRef` is the single parser for `path`,
  `path#L<a>-<b>`, `path:line`, `graph:<id>`, `wiki:<id>`. `CitationRef.domId` is the load-bearing
  chip↔card identity: the inline chip and the `SourceCard` derive the SAME id, so chip→card
  scroll-nav is `getElementById(domId).scrollIntoView()` + a transient `src-card-flash`, with no prop
  threading. **Chips NAVIGATE first, scroll second (the scroll-to-card is now the FALLBACK):** a
  `SourceHrefProvider` context carries `IndexedSnapshot.sourceUrl(path,start,end)` — the ONE
  host-aware blob-URL builder (GitHub `/blob`, Gitea `/src/branch`, GitLab `/-/blob`, Bitbucket
  `/src`+`#lines-`; returns `null` for azure/generic/missing `repoUrl`/`branch` so a chip never
  mis-links). `parseCitations()` builds the card set as a discriminated union (`{kind: file|page|
  graph}`, deduped, first-seen order) and KEEPS the `wiki:` page + `graph:` node refs a file-only
  parser would drop silently.
- **Inline citations are chips via the `src:` href scheme.** The generation prompt emits
  `[path:line](src:path#L<a>-<b>)`; the shared link renderer detects `href^="src:"` → accent chip.
  This is a BE↔FE contract — keep both ends in sync.
- **Right panel = `SourceCard` per cited source**, native `<details open>` (KISS — no vendored
  Collapsible), branching on `ParsedCitation.kind`: `file` → line-numbered excerpt via
  `useSourceExcerpt` → `GET /v1/wiki/projects/<slug>/source`; `page` → the cited page's own body via
  the SINGLE-page hook `useWikiPage(pageId, slug)`, so a doc citation shows the prose it grounded on
  rather than a 404; `graph` → a compact resolved label, no fetch. A failed excerpt reads a muted
  "source unavailable", never raw stderr-red.
- **Cited page/graph sources RENDER.** A wiki PAGE cited by the model is re-schemed `wiki:<page-id>`
  server-side (by page id OR slugified title — `tag_page_citations`). This is the allowed
  single-page-by-id read, NOT a page-SET fetch or a client-side hash resolver: the retrieval-trail
  `graph:<hash>` refs in the FOOTER stay resolved server-side (`AccessedSourceResolver`, wire is
  `string[]`). Don't reintroduce a page-set fetch or an FE hash resolver.

## Watching the backing session — `SessionJumpButton`

Both `IndexingScreen` ("Watch the indexing session") and `QAScreen` ("Watch the answering session")
mount one shared `SessionJumpButton.tsx`; only the `label`/`title` strings differ. It jumps into the
Mewbo session actually running the work, reusing the vocabulary the console already spends on a
backing session — a ghost `Button` + `ExternalLink`, same as `apps/BuildProgress`'s "Watch the
builder session" and agentic search's "Open agent session". Don't invent a third idiom.

- **Indexing is snapshot-only, by contract.** `sessionId` rides the `IndexingJob` wire shape
  (`useIndexingJob`), NOT the SSE union — the binding is stamped at read time (api-side `CLAUDE.md` →
  "One job→wire seam"). There is nothing to add to `reduceIndexing`; adding an event for it would be
  the duplicate-writer bug that seam avoids.
- **Renders ONLY when the field is present, never disabled-with-tooltip.** A graph-only index is
  deliberately sessionless and a just-queued job has no session for a beat, so absence means
  "nothing to watch". It stays mounted for a stopped run beside **Resume indexing** — a failed index
  is exactly when the transcript is worth reading.
- **The landing page's `ActiveJobCard` deliberately does NOT get one**, even though `/jobs/active`
  serves the field. The whole tile is already a `role="button"` into this screen, and a nested
  `<button>` inside it has no keyboard path in and would fire both handlers on one click.
- **Q&A can't be snapshot-only — it needs the binding WHILE streaming, and the `meta` SSE event
  carries it.** The wire default is `""` (so an older persisted `meta` frame replays unchanged) and is
  normalised to `null` once, in the reducer, so absence has one spelling downstream.
  `useQaConversation` resolves `sessionId = streamSessionId ?? snapshot.data?.sessionId ?? null` —
  the live event wins because it's watchable from the opening frame; the snapshot covers a replayed
  `?answer=` load where no stream ever opens, and carries the jump across a follow-up's round trip.
- **`streamSessionId` is `settled`-masked, and that mask is load-bearing.** The reducer only clears on
  the next `meta`, and navigating to a DIFFERENT answer drops the hook to a null input, so no `meta`
  ever arrives to clear it. Unmasked, the folded state keeps the PREVIOUS conversation's session id
  indefinitely and the jump points at the run that answered some other question — permanently, with
  no visible tell.
- **The jump mounts at CONVERSATION level, not per turn.** A follow-up continues the SAME backend
  session, so one binding answers every turn.

## Recovery UI

A failed/interrupted index is **resumable from the last good checkpoint**, not restart-only.
`LandingScreen` shows a collapsible "Incomplete indexes" section (the in-repo `useState`+chevron
idiom — there is no vendored Collapsible primitive; don't add one) fed by `useRecoverableJobs()`
(`GET /v1/wiki/jobs/recoverable`); each row's **Resume** → `useResumeIndexing(jobId)` (`POST
/v1/wiki/index/<id>/resume`) → navigate to the indexing screen.

When a job is **terminal-but-incomplete** (failed/interrupted/cancelled), `IndexingScreen` swaps the
frozen progress bar for a recovery panel (reached % via the existing `IndexingProgress` class —
never local fractions) + **Resume indexing**. Re-open trick: `useIndexingStream` takes a
`resubscribeKey`; bump it after resume to re-open the SSE **in place**, and `useResumeIndexing`
invalidates the per-job snapshot query so the terminal-status poll re-fetches and clears the panel
once the resumed job is live (else the poll stays frozen on the terminal status). Wire casing is
camelCase here (`jobId`, `pagesSubmitted`) — distinct from the snake_case generic session API.

Generic session recovery is **cross-cutting, not wiki-specific**: `SessionItem` +
`SessionDetailView` show Continue/Restart on any `recoverable` session via the shared
`useRecoverSession` hook; a wiki-indexing `/recover` response carries `job_id` and routes back into
this indexing screen (the server dispatches; the client just follows `job_id`).

## Pre-edit checklist

- [ ] New progress signal? Extend `IndexingProgress` rather than computing it locally.
- [ ] New SSE event type? Extend the `IndexingEvent`/`QaEvent` discriminated union in `api/types.ts`
      AND handle it in the reducer (`api/streamHooks.ts` — `reduceIndexing`/`reduceQa`).
- [ ] New route variant? Update `router.ts` AND `WikiApp.tsx`'s `<Route>`.
- [ ] Any color literal (`text-white`, `hsl(220 5% 12%)`, `bg-zinc-800`)? Swap for a `--*` token from
      `src/index.css`.
- [ ] A "small" wrapper around a Radix/shadcn primitive? Check the primitive itself doesn't already
      accept the `className`/`asChild` you needed.
