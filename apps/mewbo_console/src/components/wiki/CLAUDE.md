> ↑ [apps/mewbo_console/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md)

# MewboWiki — Console Subsystem Guidance

Scope: this file applies to `apps/mewbo_console/src/components/wiki/`.
Captures the non-obvious engineering decisions made while building the
DeepWiki-style FE — the parts you'd miss from reading the code alone.

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
| `KnowledgeGraphRenderer`   | `KnowledgeGraphRenderer.ts`       | constructor + lifecycle methods, static `toElements/buildStylesheet/layoutOptions` |
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

## KnowledgeGraphRenderer

`KnowledgeGraphRenderer.ts` wraps `cytoscape` + `cytoscape-fcose`.
Mounted via ref by `KnowledgeGraphScreen.tsx`. No `react-cytoscapejs`
wrapper — that library re-mounts the canvas on every prop change and
loses layout state.

Pattern:
- Constructor takes the container element + theme tokens.
- `render(nodes, edges)` is the entry point.
- `fit()`, `relayout()`, `focusNode(id)`, `applyFilter(term)`,
  `applyTheme(theme)`, `onNodeClick(cb)`, `dispose()` — methods on the
  instance.
- `static toElements(view)`, `static buildStylesheet(theme)`,
  `static layoutOptions()` — pure helpers.

Theme is snapshotted from CSS variables at construction; a
`MutationObserver` on `<html>` swaps stylesheets when the theme class
flips, so the graph re-themes without re-mounting.

Cytoscape types miss `cytoscape-fcose` — handled by a one-line
ambient declaration in `vite-env.d.ts`. Do not import internals from
fcose; just call `layout({ name: "fcose", ... })`.

**Multiplex layers (not AST-only).** Each node carries `data.layer ∈
ast|entity|memory` with `kind` widened by `External|Entity|Memory`; edges add
`ANCHORS|RELATES` + `layer …|cross`. The `kind` unions stay CLOSED so every
`Record<Kind,…>` map (`ICON_PATHS`/`KIND_VAR`/`EDGE_VAR`/`KIND_DOT`/`EDGE_DOT`/
`KIND_LAYER`) is exhaustive and `tsc` flags a missing row — never loosen to
`string` (the open-vocab entity verb rides the edge `label`, not `kind`). Layers
differ by SHAPE+colour (entity=rect, memory=hexagon, external=diamond vs AST
discs); cross-layer `ANCHORS` are dashed. The per-layer toggle is a thin wrapper
over the existing `hiddenKinds` chip machinery via `static kindsForLayer` —
hiding a layer's kinds cascades to its edges through the renderer's
endpoint-hidden rule (no separate edge enumeration). New layer tokens go in BOTH
`:root` and `.light`.

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

## Q&A vs Indexing — distinct streams

`useIndexingStream` and `useQaStream` look similar but have distinct
event unions and reducers. Don't try to merge them — the indexing
stream is one-shot per job, the Q&A stream is one-shot per question.

`AbortSignal` semantics also differ. Indexing abort = stop subscribing
(the run keeps going server-side). Cancel = `DELETE /v1/wiki/index/<id>`.
Q&A doesn't have an explicit cancel endpoint — unmount = subscriber
gone, server stops streaming when the connection closes.

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
  prop threading. `fileCitations()` builds the card set (file refs only, deduped,
  `graph:`/`wiki:` dropped).
- **Inline citations are chips via the `src:` href scheme.** The generation
  prompt emits `[path:line](src:path#L<a>-<b>)`; the shared link renderer detects
  `href^="src:"` → accent chip (`bg-[hsl(var(--primary))]/10`, monospace). This
  is a BE↔FE contract — keep both ends in sync.
- **Right panel = `SourceCard` per cited file** (`SourceCard.tsx`), native
  `<details open>` (KISS — there is no vendored Collapsible; don't add one),
  lazily fetching the line-numbered excerpt via `useSourceExcerpt` →
  `GET /v1/wiki/projects/<slug>/source` and highlighting the cited range.
  Accessed-files + model are demoted to a small secondary footer (kept — they're
  appreciated provenance — just not the primary content).

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
