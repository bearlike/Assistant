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
graph   [20, 35]
plan    [35, 45]
pages   [45, 95]
finalize [95,100]
```

Calibrated to real run shape — clone/scan are fast, page generation is
the LLM-bound long tail. Don't widen `pages` past 95 — the 5% headroom
at the end is what stops the bar from looking stuck at "100% but not
done yet".

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

## Q&A vs Indexing — distinct streams

`useIndexingStream` and `useQaStream` look similar but have distinct
event unions and reducers. Don't try to merge them — the indexing
stream is one-shot per job, the Q&A stream is one-shot per question.

`AbortSignal` semantics also differ. Indexing abort = stop subscribing
(the run keeps going server-side). Cancel = `DELETE /v1/wiki/index/<id>`.
Q&A doesn't have an explicit cancel endpoint — unmount = subscriber
gone, server stops streaming when the connection closes.
