<!-- mewbo:noload -->
# /wiki section — design + integration notes

Colocated reference for the `/wiki/*` namespace of the console. Read this
before touching anything under `src/components/wiki/`. The Gitea handoff
issue is at https://git.hurricane.home/bearlike/Assistant/issues/5 — it
documents the API contract verbatim and is the source of truth for the
*backend* side of the swap. This file documents the *frontend* side.

## What it is

A DeepWiki-style auto-generated docs surface for code repositories,
implemented entirely against a mock API today. Six screens cover the full
"index a repo → browse the wiki → ask questions about it" loop, all
behind `/wiki/*`. Production wiring is a one-file swap in `api/client.ts`.

## Routes

| Path | Screen | Purpose |
|---|---|---|
| `/wiki` | `LandingScreen` | Project gallery + URL bar + delete dialog |
| `/wiki/configure?url=…` | `ConfigureWizard` | 3-step onboarding (source / generation / scope) |
| `/wiki/repo?slug=…` | `WelcomeScreen` | Email-gated "not indexed" page |
| `/wiki/indexing?jobId=…&slug=…` | `IndexingScreen` | Live SSE-driven loader with cancel |
| `/wiki/p/<pageId>?slug=…` | `WikiScreen` | 3-col sidebar / markdown / TOC + Q&A dock |
| `/wiki/qa?q=…&page=…&model=…&slug=…` | `QAScreen` | 2-col streaming Q&A view |

Routing helper is `router.ts` — use `buildHref({ kind, ... })` to build
URLs and `useWikiRoute()` to parse the current one. The wouter glue at the
App-level uses `<Route path="/wiki/*?">` so all wiki paths funnel through
`WikiApp.tsx`.

## File map

```
src/components/wiki/
├── WikiApp.tsx                  # route resolver
├── LandingScreen.tsx
├── ConfigureWizard.tsx
├── WelcomeScreen.tsx
├── IndexingScreen.tsx
├── WikiScreen.tsx
├── QAScreen.tsx
│
├── WikiTopBar.tsx               # MewboWiki brand + Edit Wiki + Copy badge popovers + Copy link
├── ModelPicker.tsx              # reuses ModelBrandIcon + formatModelName
├── MarkdownBlock.tsx            # react-markdown + remark-gfm + rehype-slug
├── MermaidBlock.tsx             # lazy mermaid, memoised, cached
├── DiagramZoom.tsx              # fit-to-stage zoom modal
├── LiveBlocks.tsx               # paint blocks as a stream grows them
├── TypewriterBlocks.tsx         # legacy char-by-char typewriter (Block model)
├── QADock.tsx                   # floating composer
├── RefreshThisWiki.tsx          # right-rail two-step confirm + queue
├── IndexedSnapshot.ts           # atomic class: indexedAt/branch/commit + URL composers
├── IndexedSnapshotCaption.tsx   # sidebar caption + landing-card footer renderer
├── BrandMark.tsx                # clay flower SVG
├── mermaid-renderer.ts          # shared lazy-load + (theme,source)→svg cache
├── router.ts                    # WikiRoute parser/buildHref
├── badge.ts                     # atomic class: WikiBadge (README "Copy badge" snippet)
├── useStoredModel.ts            # localStorage `wiki:qa-model`
│
├── configure-wizard/
│   ├── Field.tsx, Stepper.tsx
│   ├── PlatformTile.tsx
│   └── PlatformIcon.tsx         # simple-icons + lobehub Azure
│
└── api/
    ├── types.ts                 # ★ wire-shape source of truth
    ├── client.ts                # real HTTP/SSE transport to /v1/wiki/*
    ├── hooks.ts                 # TanStack Query + stream consumers
    └── markdown.ts              # frontmatter splitter + TOC derivation
```

## Wire-shape source of truth

`api/types.ts` is the canonical schema for every shape that crosses
`api/client.ts`. **Never** invent new shapes elsewhere — extend `types.ts`
and let the change ripple. Backend implementers (issue #5) treat this
file as the spec to satisfy.

Key unions:

- `IndexingEvent` — SSE event union for live indexing progress. Includes
  `heartbeat` for transport-level keep-alive; consumers ignore it.
- `QaEvent` — SSE event union for live Q&A. `meta` → `summary_ready` →
  (`block_open` → `block_delta`* → `block_close`)+ → terminal.
- `WikiError` — typed error model. Every `code` maps to an HTTP status
  (documented in the type). Use `isWikiError()` (from `api/client.ts`) to
  narrow.

## Wired to real backend

`api/client.ts` calls `/v1/wiki/*` directly (relative URLs — the Vite
dev proxy and production same-origin handle routing). Auth via
`X-API-Key` header for REST calls; `?api_key=` query param for SSE
streams (EventSource / fetch streaming doesn't support custom headers).

Endpoint mapping:

| Function | Endpoint | Method |
|---|---|---|
| `listProjects` | `/v1/wiki/projects` | GET |
| `deleteProject(slug)` | `/v1/wiki/projects/<slug>` | DELETE |
| `listPlatforms` | `/v1/wiki/platforms` | GET |
| `listLanguages` | `/v1/wiki/languages` | GET |
| (model picker) | `/api/models` via `useModels()` | GET |
| `getPage(slug, pageId)` | `/v1/wiki/projects/<slug>/pages/<pageId>` | GET |
| `createIndexingJob(submission)` | `/v1/wiki/index` | POST |
| `getIndexingJob(jobId)` | `/v1/wiki/index/<jobId>` | GET |
| `cancelIndexingJob(jobId)` | `/v1/wiki/index/<jobId>` | DELETE |
| `subscribeToIndexing(jobId)` | `/v1/wiki/index/<jobId>/stream` | GET SSE |
| `getAnswer(answerId)` | `/v1/wiki/qa/<answerId>` | GET |
| `streamAnswer(input)` | `/v1/wiki/qa` | POST SSE |
| `startAnswer(input)` | `/v1/wiki/qa` (awaits `meta` event) | POST SSE |
| `askQuestion(q, ctx)` | via `startAnswer` → `getAnswer` | — |
| `submitWizard(input)` | via `createIndexingJob` | — |
| `requestWikiRefresh(slug)` | `/v1/wiki/projects/<slug>/refresh` | POST |
| `getDefaultExclusions` | (local constant — no round-trip) | — |
| `isWikiError` | (type guard — no round-trip) | — |

SSE implementation uses `fetch` + `ReadableStream` reader (not
`EventSource`) so POST payloads and the `?api_key=` auth param both
work. Heartbeat frames are silently skipped inside the parser.
`AbortSignal` cancels the underlying `fetch` on unmount.

Backend contract and design:
`docs/specs/2026-05-14-deepwiki-style-gen-design.md`

## Markdown rendering pipeline

Wiki pages are markdown + YAML frontmatter, NOT the old block DSL. The
flow:

```
fetch raw .md  →  parsePageSource()    →  WikiPage { frontmatter, body, toc, nav }
                  (api/markdown.ts)
                                       →  <MarkdownBlock>           (renders to React)
                                          via react-markdown
                                          + remark-gfm
                                          + rehype-slug              (heading IDs)
                                          + custom component map     (code/a/h2/h3/...)
```

Custom component handlers route:

- ` ```mermaid` fenced code → `<MermaidBlock>` (with a `key` derived
  from a stable `djb2(source)` hash so re-renders preserve the SVG).
- Links with `src:` scheme → `<SrcChip>` (mono pill with optional line
  range).
- Relative links (no scheme/host/`/`/`#`) → `useLocation` navigation
  via `onNavigatePage`.

`gray-matter` was tried but pulls Node `Buffer` — replaced with a
browser-safe split using `js-yaml` directly. See `api/markdown.ts`.

## Mermaid — gotchas

The renderer is non-trivial because mermaid is global, ~700 KB, and
re-renders nuke the SVG. See `MermaidBlock.tsx` + `mermaid-renderer.ts`
for the full implementation. Invariants:

1. **Diagram ids are stable hashes of the source.** Set by the caller in
   `MarkdownBlock` (`stableId(text, "wiki-d")`). Without this, scroll-spy
   triggers re-renders that hand mermaid fresh ids → SVG replaced on
   every scroll tick → flicker + layout jump.
2. **`mermaid.initialize()` runs once per theme.** Tracked in
   `mermaid-renderer.ts`. Calling it from each block's effect races on
   shared state.
3. **SVG output is cached by `(theme, source)`.** Same diagram + same
   theme = same SVG handed back without calling mermaid again.
4. **`React.memo` on MermaidBlock.** Parents re-render on scroll-spy;
   memo bails on `diagramId + inlineSource` so the SVG subtree isn't
   touched.
5. **Zoom modal reuses the cache.** `DiagramZoom` reads from the same
   registry; on open it measures the SVG vs the stage and picks a
   fit-to-stage scale (Reset returns to fit, not 1×). Don't add a second
   close button — `<DialogContent>` already ships one.

## Q&A streaming

`useQaStream(input)` returns folded state from the `QaEvent` stream:

```ts
{ answerId, model, fromPageId, summarySources, blocks: Block[], done, cancelled, error }
```

The blocks array grows over time (`block_delta` appends to the matching
index). `<LiveBlocks>` just paints whatever state it sees — the stream
IS the typewriter. Do not stack `<TypewriterBlocks>` on top of it.

The legacy non-streaming path (`useAskWiki()` → full `QaAnswer` snapshot)
is kept because shareable QA URLs use it. `GET /v1/wiki/qa/<answerId>`
serves the snapshot.

## Indexing streaming

`useIndexingStream(jobId)` folds `IndexingEvent`s into:

```ts
{ job: IndexingJob, history: { name, done }[], error }
```

History is trimmed to the rolling 9-line window the loader UI shows.
`useIndexingJob(jobId)` is the polling fallback for environments where
SSE isn't available — it consumes the same `IndexingJob` snapshot shape.

Cancellation has two distinct semantics:

- **Abort (unmount).** Internal `AbortController` aborts when the
  component unmounts → stream emits `cancelled` and exits. **Server-side
  job continues** — the abort just stops the subscription.
- **Explicit cancel.** User clicks "Cancel indexing" → mutation calls
  `DELETE /v1/wiki/index/<jobId>` → server marks job cancelled → active
  stream emits `cancelled` and closes.

This distinction is encoded in the mock and documented in
`api/types.ts` — backend must honour both.

## Model picker

Use `ModelPicker` (`variant="full"` for input-shaped, `variant="compact"`
for the dock pill). It MUST match the main chat composer's pattern:

- `string[]` API of provider-prefixed IDs (`anthropic/claude-sonnet-4-5`).
- `ModelBrandIcon` for the brand glyph (resolves via `getProviderIcon`).
- `formatModelName` for the short display label.
- Single `<CommandInput>` filter — no extra Search icon header.
- Unsupported models (whisper/embedding) sink to the bottom via
  `isUnsupportedModel`.
- Compact pill caps width with `max-w-[220px]` + `whitespace-nowrap` to
  prevent line wrap when a long id like `anthropic/claude-sonnet-4-5` is
  selected.

If you find yourself building a bespoke dropdown for models in the wiki,
stop — reuse `ModelPicker` (or extend it). Do not fork the chat
composer's model row.

## Platform tiles

Brand glyphs come from the `simple-icons` package (CC0). The `PlatformIcon`
component switches on `Platform["id"]` and renders the official path
data with `currentColor`, so the parent (a colored tile) drives the fill.
Azure DevOps is the one exception — `simple-icons` dropped it under
Microsoft trademark policy, so it falls back to `@lobehub/icons/Azure`.

Tile backgrounds use each platform's official brand hex (`#181717`
GitHub black, `#FC6D26` GitLab orange, `#0052CC` Bitbucket Atlassian
blue, `#609926` Gitea green, `#0078D7` Azure blue, `#F05032` Git red)
so a white glyph reads at proper contrast.

## Theme integration

The wiki listens for `wiki:theme-change` events (dispatched from
`App.tsx`'s theme toggle) so `MermaidBlock` re-renders with the right
palette. Don't add bespoke theme handling inside the wiki — extend the
single dispatcher in `App.tsx` if you need a new signal.

## Pre-edit checklist

Before changing anything in `src/components/wiki/`:

- [ ] Did I check `api/types.ts` to see if there's already a shape for
      what I'm adding? (Usually yes.)
- [ ] If I'm adding a new endpoint, is it in `api/client.ts` AND reflected
      in the endpoint table in this README? (Both must move together.)
- [ ] If I'm rendering a brand icon, am I reusing `ModelBrandIcon` /
      `PlatformIcon` / `BrandMark`?
- [ ] If I'm adding a stream, does it produce an `AsyncIterable<Event>`
      that mirrors what SSE would emit? Does it honour `AbortSignal`?
- [ ] Mermaid: am I using `stableId(source, prefix)` for the diagram id?
- [ ] Markdown: any new "special atom" goes through a `react-markdown`
      component override, not a parallel parser pass.
- [ ] `api/client.ts` is the only file that calls fetch/SSE. Screens and hooks
      must not bypass it with direct fetch calls.
