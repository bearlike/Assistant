> ↑ [apps/mewbo_console/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md)

# MewboWiki — Console Subsystem Guidance

Scope: this file applies to `apps/mewbo_console/src/components/wiki/`.
Captures the non-obvious engineering decisions made while building the
auto-generated-wiki FE — the parts you'd miss from reading the code alone.

## Read both files

- This `CLAUDE.md` — non-obvious decisions, recent changes, gotchas.
- `README.md` (sibling) — full design + endpoint table + file map +
  Mermaid invariants + Q&A streaming contract. Marked `<!-- mewbo:noload -->`
  so it's not auto-injected, but it's the depth reference. Read it
  before any non-trivial change.

## Atomic class paradigm

Every standalone piece of behaviour in this folder is structured as an
atomic class: frozen-ish state attributes on the instance, behaviour as
class methods, helpers as static methods. The pattern is repeated
deliberately — picked it up from how the Python side models
`KnowledgeGraphView` — and we hold it for any new feature here.

Existing examples to copy from:

| Class                      | File                              | Pattern         |
|----------------------------|-----------------------------------|-----------------|
| `IndexingProgress`         | `progress.ts`                     | static factories `fromJob` / `fromStream`, private `_compute` |
| `CollapseModel`            | `collapseModel.ts`                | pure visible-set + edge re-point/aggregate over an `expanded` set; static `initialExpanded`/`visibleGraph` |
| `Graph3DView`              | `Graph3DView.tsx`                 | thin domain-agnostic 3D view over `CollapseModel`; injected `Graph3DTheme` + `renderInspector` render-prop |
| `RelativeTime`             | `relativeTime.ts`                 | static-only, `Intl.RelativeTimeFormat` |

If you find yourself writing a free function that grows to need more
than two args + carries state across calls, refactor it into an atomic
class instead.

## Progress is computed in ONE place

`progress.ts:IndexingProgress` is the single source of truth for
indexing progress UI. Both the landing-page in-flight card
(`LandingScreen.tsx`) and the indexing page (`IndexingScreen.tsx`)
import it and call `fromJob(job)` / `fromStream(state)`.

Never compute `pct` locally from `scannedCount/totalCount`. The old
landing card did exactly that and pegged at 96% the moment the run
left the scan phase, because it never saw `phase`. The atomic class
reads phase, sub-progress, and `phaseStartedAt` from a single typed
input and produces `{pct, phase, label, statusLine, etaSeconds}`.

The phase weight table inside `progress.ts`:

```
clone   [ 0,  5]
scan    [ 5, 20]
graph   [20, 32]
enrich  [32, 40]
plan    [40, 45]
pages   [45, 95]
finalize [95,100]
```

Calibrated to real run shape — clone/scan are fast, page generation is
the LLM-bound long tail. Don't widen `pages` past 95 — the 5% headroom
at the end is what stops the bar from looking stuck at "100% but not
done yet".

**Adding a phase is a lock-step edit (Gitea #35 added `enrich`).** A new phase
must land in `IndexingPhase` (`api/types.ts`) AND all four `progress.ts` lookup
tables (`PHASE_RANGE`/`PHASE_LABEL`/`PHASE_ORDER`/`PHASE_BUDGET_S`) in the same
change — each is a `Record<IndexingPhase, …>`, so `tsc` enforces exhaustiveness
and fails the build if any table misses the new key. `enrich` sits post-AST
(graph narrowed to `[20,32]`, plan to `[40,45]`) to honour the GraphRAG ordering
law — the entity KG is built before pages are written.

## ETA is extrapolated from `phaseStartedAt`

The BE writes `IndexingJob.phase_started_at` on every `emit_phase`
call. The FE reads it and computes per-phase remaining time:

- For `pages` with ≥1 page committed: `(elapsed / pagesSubmitted) *
  remainingPages` — real measured rate.
- For `scan` with ≥1 file scanned: same idea on files.
- Otherwise: a fixed per-phase budget (a small lookup table in
  `progress.ts:PHASE_BUDGET_S`).

Trailing phases add their fixed budgets. The result is an honest "~3
min left" / "~45 s left" — not a fake static estimate.

`IndexingProgress.formatEta(seconds)` returns `""` when ETA is null,
0, NaN, or Infinity. Render the result inline — empty string is
safely no-op'd in JSX.

## Log timeline — full history, not a rolling window

The indexing-page reducer keeps EVERY log event in state. The view
renders all of them inside an `overflow-y-auto` container and
auto-scrolls to the bottom via a `ref + useLayoutEffect` whenever the
log count changes.

Earlier code did `state.logs.slice(-20)` in the hook (`hooks.ts`).
Symptom: every page refresh appeared to "show different logs", because
the visible last-20 shifted forward as the total grew. Replaying SSE
from idx 0 was already free, the trim was the only thing breaking
determinism. Don't reintroduce a slice here.

History (per-file scan rows, distinct from `logs`) is still trimmed
to 9 — that surface is just a recent-activity blip, not an audit log.

## SSE consumer uses `fetch`, not `EventSource`

`api/client.ts:sseStream` opens an SSE connection via `fetch(...,
{ headers: { Accept: "text/event-stream" } })` and reads the
`ReadableStream` body line-by-line. Not a native `EventSource`. Reasons:

1. `EventSource` doesn't allow custom request headers — we'd need to
   smuggle the API key through a cookie or query param. (Query param
   works for GET-only routes; the Q&A endpoint is POST, so we'd need
   both transports.)
2. `fetch` integrates with `AbortSignal` so unmount cleanly cancels.
3. POST payloads with bodies (used by `streamAnswer`) require it.

`parseSseStream` walks lines per frame looking for `event:` and
`data:`. It ignores `id:` lines — that's fine; auto-resume isn't a
goal here because the BE already replays from idx 0 if no header is
sent.

## Graph3DView — the one shared graph engine

`Graph3DView.tsx` is the SINGLE graph renderer in the console — `react-force-graph-3d`
(three.js / WebGL). It replaced the old 2D Cytoscape `KnowledgeGraphRenderer`
that the wiki and Agentic Search forked between them; both `KnowledgeGraphRenderer.ts`
and the dead `KnowledgeGraphScreen.tsx` are DELETED, and `cytoscape` /
`cytoscape-fcose` / `@types/cytoscape` (and the fcose ambient decl in
`vite-env.d.ts`) are gone. There is no second renderer to keep in sync.

It is **domain-agnostic**: it treats node `kind` / `layer` as opaque strings and
reads every colour / label / layer / size from an injected `Graph3DTheme`; the
caller supplies the side-panel via the `renderInspector` render-prop. Both the
wiki Knowledge Graph and the SCG workspace graph satisfy the structural
`{ nodes:[{data}], edges:[{data}] }` wire shape, so each is a THIN adapter:

- **Wiki** — `KnowledgeGraph3DScreen.tsx` assembles `WIKI_GRAPH_THEME` from the
  `graphTheme.ts` maps and wires the typed `inspector/GraphInspector` registry.
- **SCG** — `agentic_search/graph/WorkspaceGraphDialog.tsx` assembles
  `SCG_GRAPH_THEME` from `scgGraphConfig.ts` and renders its own `NodeInspector`.

`Graph3DView` owns the toolbar (name filter, kind chips, per-layer toggle group,
Fit/Reset), the click→pick selection, and composes the pure `CollapseModel` for
folder LOD. Don't wrap `<ForceGraph3D>` in a bespoke imperative scene class — the
library owns camera, picking, force layout, hover labels, and nav.

**Multiplex layers (not AST-only).** Each wiki node carries `data.layer ∈
ast|entity|memory` with `kind` widened by `External|Entity|Memory`; edges add
`ANCHORS|RELATES`. The `kind`/`layer` unions stay CLOSED so every `Record<Kind,…>`
map in `graphTheme.ts` (`KIND_VAR`/`EDGE_VAR`/`KIND_DOT`/`KIND_LAYER` +
`ALL_NODE_KINDS`/`LAYER_ORDER`/`LAYER_LABEL`/`LAYER_DOT`) is exhaustive and `tsc`
flags a missing row — never loosen to `string` (the open-vocab entity verb rides
the edge `label`, not `kind`). In 3D, kinds differ by COLOUR (and folders by a
size boost), not shape — the old Cytoscape per-shape/dashed-edge scheme is gone.
The per-layer toggle cascades into the engine's `hiddenKinds` set via
`kindLayer`; hiding a layer's kinds drops its edges through the `linkVisibility`
endpoint-hidden rule (no separate edge enumeration). New layer tokens go in BOTH
`:root` and `.light`.

## Code Galaxy — the 3D graph view (`/wiki/graph`)

`KnowledgeGraph3DScreen.tsx` (`react-force-graph-3d`, three.js/WebGL) is the
`/wiki/graph` route — a thin adapter over the shared `Graph3DView`. That same
`Graph3DView` is THE single graph engine for both this wiki Knowledge Graph and
the Agentic-Search SCG workspace graph (`agentic_search/graph/WorkspaceGraphDialog`);
the old 2D Cytoscape screen + renderer are deleted. `CollapseModel` and
`graphTheme` are shared; a folderless graph (the SCG case) flows through
`CollapseModel` as a pure pass-through. Non-obvious decisions (so we don't
re-derive them):

- **3D is the GPU path, not decoration.** `react-force-graph`'s *2D* build is
  plain Canvas (the same wall the old Cytoscape engine hit at ~2–5K nodes); only
  the *3D* build is WebGL. The worst real graph (a large internal Python
  monorepo) is ~17K nodes / ~68K edges — far past the 2D canvas.
- **Directory hierarchy IS the level-of-detail — that's the crash fix, not a
  fancier renderer.** The BE `?hierarchy=1` wire (see `mewbo_graph` `FolderTree`)
  adds `Folder` supernodes + `parentId`; folders start collapsed except depth-1,
  so the live force sim is a few hundred nodes, never 17K. Cosmograph/GPU-sim,
  server-side layout precompute, tile streaming, and Louvain/Leiden clustering
  were all deliberately CUT (YAGNI at this scale — the folder tree IS the
  clustering).
- **One atomic class: `CollapseModel`** (`collapseModel.ts`) — pure visible-set +
  edge re-point/aggregate over an externally-held `expanded` set. `Graph3DView` is
  thin glue: `graphData = useMemo(() => model.visibleGraph(expanded), …)` fed to
  `<ForceGraph3D>`. Do NOT wrap ForceGraph3D in a bespoke imperative scene class;
  the library owns camera, picking, layout, labels, and nav (an earlier
  `CodeGalaxyScene` wrapper was written then deleted for exactly this reason).
- **`graphTheme.ts` is the single kind→colour/layer/size home** — it also houses
  the presentation maps the toolbar reads (`ALL_NODE_KINDS`, `KIND_DOT`,
  `LAYER_ORDER`, `LAYER_LABEL`, `LAYER_DOT`). Both adapters draw from it via their
  `Graph3DTheme` (the wiki directly; the SCG theme mirrors the same token family).
  Don't re-duplicate the palette maps or the `cssVarColor` hsl-comma-normaliser.
- **Per-frame accessors must be O(1).** `linkVisibility`/`nodeColor`/`nodeVal`
  run per element every frame — look up via an id→node/kind `Map` built in the
  `graphData` memo, never `nodes.find()` (that was O(N·E) per frame).
- **Selection → the typed `inspector/` registry** — exhaustive
  `Record<GraphSelectionKind,…>` (a missing kind is a `tsc` error, no silent
  default), one atomic panel per kind, reusing the old side-panel rendering +
  `buildMarkdownComponents` + `buildHref`. Shared neighbour lookups go through a
  plain-`Map` `GraphIndex` (no graph library).
- **Escalation if heavy folder expansions ever choke:** `react-force-graph` runs
  d3-force on the MAIN thread (no worker, no GPU) → precompute layout
  server-side (Graphviz `sfdp`) and ship static `fx/fy/fz`. Not built yet.

## Route additions go through `router.ts`

`router.ts` exposes `buildHref({ kind, ... })` and `useWikiRoute()` for
URL building and parsing. When you add a route variant, extend the
`WikiRoute` union AND the parser AND the builder in lock-step — the
type system flags drift.

Current variants: `landing`, `configure`, `repo`, `indexing`, `page`,
`qa`, `graph`. Don't bypass `buildHref` to write a string URL
inline — that scatters route knowledge across the component tree.

## Brand glyphs come from libraries, not bespoke SVG

- `ModelBrandIcon` (in main components) — resolves an LLM model id to
  the right provider glyph via `getProviderIcon`. Reuse for any
  model-pickery surface in the wiki.
- `PlatformIcon` (`configure-wizard/PlatformIcon.tsx`) — uses
  `simple-icons` (CC0) for Git platforms; falls back to
  `@lobehub/icons` for Azure DevOps (dropped from simple-icons under
  Microsoft trademark policy).
- `BrandMark.tsx` — the in-house clay flower, only for the MewboWiki
  brand.

Never add a third icon library. Never hand-roll provider SVGs.

## Repository badge ("Copy badge")

`badge.ts:WikiBadge` is the atomic class behind the `WikiTopBar` "Copy
badge" popover — the snippet a maintainer pastes into their README. Two
non-obvious facts to keep:

- **Artwork is a single static external CDN SVG** (`WikiBadge.IMAGE_URL`,
  `cdn.thekrishna.in`), shared by every repo; only the *link* is per-repo.
  That's why the feature is pure-FE (no backend). This is a remote image
  asset — NOT an exception to the "no bespoke provider SVG glyphs" rule
  above (we render `<img>`, we don't hand-author a glyph).
- **The link target is the repo's `landingPageId`**, not a fixed page id.
  `landingPageId` (on `Project`, set at finalize, validated to exist) is
  the canonical "enter this wiki" target — `LandingScreen` tile-click uses
  it too. There is NO universal `landing-page` sentinel; never hardcode a
  page id for a repo-level deep link. `WikiScreen` passes
  `landingPageId ?? pageId` (current page is the always-valid fallback);
  the affordance is gated by `WikiBadge.forPage(...)` returning non-null.

`WikiTopBar`'s copy actions go through `@/utils/clipboard:copyText` and the
shared `@/components/CopyButton` — don't re-inline an execCommand fallback.

## Service worker — stale-deploy resilience

Inherited from the parent console's PWA config (see
`apps/mewbo_console/CLAUDE.md` for the full set of invariants). Big
chunks (`StliteWidgetPanel`, `PlotlyChart`, `DeckGlJsonChart`) are in
the precache `globIgnores` list. The wiki adds nothing new here.

If a deploy ever ships and users report stale chunks, the nuclear
reload in `UpdatePrompt.handleReload` is the consistent fix.

## Pre-edit checklist (wiki additions)

- [ ] Did I add a new progress signal? If so, did I extend
      `IndexingProgress` instead of computing it locally?
- [ ] Did I add a new SSE event type? Did I extend the `IndexingEvent`
      / `QaEvent` discriminated union in `api/types.ts` AND handle it
      in the reducer (`api/hooks.ts`)?
- [ ] Did I add a new route variant? Did I update `router.ts` AND
      `WikiApp.tsx`'s `<Route>`?
- [ ] Did I render any color literal (`text-white`, `hsl(220 5% 12%)`,
      `bg-zinc-800`)? If yes, swap for a `--*` token from
      `src/index.css`.
- [ ] If I added a "small" wrapper around a Radix/shadcn primitive,
      did I check the primitive itself doesn't already accept the
      `className` / `asChild` I needed?

## Catalog wizard + draft stream (SideStage FE)

`ConfigureWizard` has a `git|catalog` toggle; the catalog branch renders
`CatalogDocsForm` and submits via `POST /v1/wiki/projects/{slug}/documents`
(no git URL, no clone token). `DraftPanel` + `useDraftStream` consume
`POST /v1/draft/stream` via the shared `sseStream` — render tokens on arrival,
no synthetic timer. Route variant: `/draft` added to `WikiRoute` union +
`router.ts` + `WikiApp.tsx` in lockstep.

## Branch picker (generation step)

The generation step offers a branch dropdown so the user picks the ref to
onboard. It's a plain native `<select>` reusing the **language-selector**
pattern (bordered flex box + `GitBranch` icon + overlay `ChevronDown`) — NOT a
shadcn `Command`/combobox: the option set is tiny and static per repo, so a
combobox would be bespoke weight for nothing (KISS). Branches load via
`useBranches` (`POST /v1/wiki/branches`), keyed `["wiki","branches", repoUrl,
Boolean(token)]` — the cache key carries token-*presence* only, NEVER the raw
token — `enabled` only once the URL looks like a git URL, `retry: false` so a
bad URL/token doesn't hammer the endpoint. The first option (value `""`) is
`Default · <defaultBranch>`; on loading it's a disabled "Loading branches…", on
error/empty it falls back to just the default option (the query failure is
swallowed — no toast, no retry). `state.ref` is spread into the submission ONLY
when non-empty (mirrors how `graphOnly` is conditionally included), so an
untouched picker submits no `ref` and the BE clones the default branch.

## Developer mode — graph-only onboarding + no-docs state

When `config.runtime.developer_mode` is on (read via `useConfig`),
`ConfigureWizard`'s generation step reveals a **"Developer mode" subheading**
grouping the single "Graph only — skip documentation (no LLM)" `<Switch>` that
sets `graphOnly` on the submission; the whole section is HIDDEN (and
`graphOnly` omitted) when dev mode is off — the subheading exists so the toggle
reads as a dev-only setting, distinct from the normal generation options above
it (the branch picker is one of those — NOT under this heading). A project that
comes back
`graphOnly:true` renders `GraphOnlyEmptyState` in `WikiScreen` — a "No
documentation available" panel whose PRIMARY CTA is `buildHref({ kind:"graph" })`
(the existing graph viewer is the whole point); it REPLACES the docs grid and
suppresses the `QADock` (nothing to ask). Reuses the shadcn `Switch`/`Button`
primitives — no bespoke UI. A graph-only run's progress simply skips
enrich/plan/pages (a `graph`→`finalize` jump); don't special-case the bar.

## Q&A vs Indexing — distinct streams

`useIndexingStream` and `useQaStream` look similar but have distinct
event unions and reducers. Don't try to merge them — the indexing
stream is one-shot per job, the Q&A stream is one-shot per question.

`AbortSignal` semantics also differ. Indexing abort = stop subscribing
(the run keeps going server-side). Cancel = `DELETE /v1/wiki/index/<id>`.
Q&A doesn't have an explicit cancel endpoint — unmount = subscriber
gone, server stops streaming when the connection closes.

## Idempotent Q&A URL — `?answer=<id>` (Gitea #165)

A completed answer is addressable, so a refresh/share replays it with ZERO LLM
(was: every mount re-POSTed `/v1/wiki/qa` and re-ran the agent). The whole fix is
FE — the backend already persisted the full answer (`GET /v1/wiki/qa/<id>` →
`blocks`+`summarySources`+`accessedSources`+`modelsUsed`), it was just only read
for the footer. Wiring: `router.ts`'s `qa` variant carries an optional `answer`
id (parse + `buildHref` in lock-step); `QAScreen`, when it has an `answer` id it
did NOT publish itself, renders from `useQaAnswerSnapshot(id)` and feeds
`useQaStream(null)` (a genuine no-op — no POST). On a fresh ask, the `meta`
event's `answerId` is folded into the URL via `navigate(replace)`;
`publishedAnswerRef` keeps our OWN id from flipping the live stream into snapshot
mode, so the stream paints through without an abort/refetch flash. Known edge: a
refresh mid-generation shows the partial persisted snapshot (still no LLM
re-invoke), not a resumed stream.

## Q&A answer rendering — cited-sources viewer (one renderer, one citation grammar)

The answer area is one slot, not two parallel components. The skeleton resolves
to the answer via `hasBlocks ? answer : error ? error : stream.done ? empty :
skeleton` — the `stream.done` branch (on BOTH columns; left guards
`summarySources !== null || stream.done`) is what stops the skeleton dangling
forever on a zero-block completion. `hasBlocks` excludes `sources`/`accordion`
blocks so a sources-only stream still hits the terminal branch.

- **One renderer.** `markdownComponents.tsx:buildMarkdownComponents()` + the
  shared `SrcChip` is the SINGLE markdown component map; both `LiveBlocks`
  (streaming prose) and `MarkdownBlock` (wiki pages) consume it
  (`remarkGfm + rehypeHighlight + rehypeSlug`). The old `LiveBlocks` was a
  reduced renderer that silently dropped `ol`/`table`/`blockquote`, had no code
  highlighting, and rendered citations as plain links — never reintroduce a
  second divergent renderer. `LiveBlocks` does NOT render the `sources` block
  (it feeds the right panel).
- **One citation grammar.** `citations.ts:CitationRef` is the single parser for
  `path`, `path#L<a>-<b>`, `path:line`, `graph:<id>`, `wiki:<id>`.
  `CitationRef.domId` is the load-bearing chip↔card identity: the inline chip
  and the `SourceCard` derive the SAME id, so chip→card scroll-nav is
  `getElementById(domId).scrollIntoView()` + transient `src-card-flash` — no
  prop threading. **Chips NAVIGATE first, scroll second (the old scroll-to-card is
  now the FALLBACK):** a `SourceHrefProvider` context carries
  `IndexedSnapshot.sourceUrl(path,start,end)` — the ONE host-aware blob-URL builder
  (GitHub `/blob`, Gitea `/src/branch`, GitLab `/-/blob`, Bitbucket `/src`+`#lines-`;
  returns `null` for azure/generic/missing `repoUrl`/`branch` so a chip never
  mis-links) — so a `src:` chip renders a real `<a target="_blank">` to the
  remote-repo file at its cited line range, and `SourceCard` file→repo-blob /
  page→wiki-route open in a new tab. `parseCitations()` builds the card set as a discriminated union
  (`{kind: file|page|graph}`, deduped, first-seen order) — it KEEPS the `wiki:`
  page + `graph:` node refs that the legacy `fileCitations()` dropped (the latter
  is still exported for file-only callers). #165.
- **Inline citations are chips via the `src:` href scheme.** The generation
  prompt emits `[path:line](src:path#L<a>-<b>)`; the shared link renderer detects
  `href^="src:"` → accent chip (`bg-[hsl(var(--primary))]/10`, monospace). This
  is a BE↔FE contract — keep both ends in sync.
- **Right panel = `SourceCard` per cited source** (`SourceCard.tsx`), native
  `<details open>` (KISS — no vendored Collapsible; don't add one), branching on
  `ParsedCitation.kind`: `file` → line-numbered excerpt via `useSourceExcerpt` →
  `GET /v1/wiki/projects/<slug>/source`; `page` → the cited page's own body via
  the SINGLE-page hook `useWikiPage(pageId, slug)` (so a doc citation shows the
  prose it grounded on, not a 404); `graph` → a compact resolved label, no fetch.
  A failed excerpt reads a muted "source unavailable", never raw stderr-red.
  Accessed-files + model stay a small secondary footer.
- **Cited page/graph sources now RENDER (supersedes the #70 "FE unchanged" rule — #165).**
  A wiki PAGE cited by the model is re-schemed `wiki:<page-id>` server-side (now by
  page id OR slugified title — `tag_page_citations`); the FE no longer DROPS it —
  the `page` branch fetches that ONE page by id and shows its body. This is the
  allowed single-page-by-id read (same as `WikiScreen`), NOT the still-forbidden
  page-SET fetch or a client-side hash resolver: the retrieval-trail `graph:<hash>`
  refs in the FOOTER stay resolved server-side (`AccessedSourceResolver`, wire is
  `string[]`). Don't reintroduce a page-set fetch or a FE hash resolver.

## Recovery UI (Gitea #54)

A failed/interrupted index is **resumable from the last good checkpoint**, not
restart-only. `LandingScreen` shows a collapsible **"Incomplete indexes"** section
(the in-repo `useState`+chevron idiom — there is no vendored Collapsible
primitive; don't add one) fed by `useRecoverableJobs()` (`GET
/v1/wiki/jobs/recoverable`); each row's **Resume** → `useResumeIndexing(jobId)`
(`POST /v1/wiki/index/<id>/resume`) → navigate to the indexing screen. When a job
is **terminal-but-incomplete** (failed/interrupted/cancelled), `IndexingScreen`
swaps the frozen progress bar for a recovery panel (reached % via the existing
`IndexingProgress` class — never local fractions) + **Resume indexing**. Re-open
trick: `useIndexingStream` takes a `resubscribeKey`; bump it after resume to
re-open the SSE **in place**, and `useResumeIndexing` invalidates the per-job
snapshot query so the terminal-status poll re-fetches and clears the panel once
the resumed job is live (else the snapshot poll stays frozen on the terminal
status). Wire casing is camelCase here (`jobId`, `pagesSubmitted`) — distinct from
the snake_case generic session API.

Generic session recovery is **cross-cutting, not wiki-specific**: `SessionItem` +
`SessionDetailView` show Continue/Restart on any `recoverable` session via the
shared `useRecoverSession` hook; a wiki-indexing `/recover` response carries
`job_id` and routes back into this indexing screen (the server dispatches; the
client just follows `job_id`).
