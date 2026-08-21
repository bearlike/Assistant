<!-- mewbo:noload -->
> ↑ [root /CLAUDE.md](../../CLAUDE.md) · children: [nav-rail](src/components/nav-rail/CLAUDE.md) · [wiki](src/components/wiki/CLAUDE.md) · [agentic_search](src/components/agentic_search/CLAUDE.md) · [apps](src/components/apps/CLAUDE.md)

# Mewbo Console — Frontend Engineering Guide

A lean React + Vite + Tailwind console over the Mewbo API. Not a general-purpose SPA — an
instrument for watching an agent work. Two principles run in parallel and neither overrides the
other: **library-first** for engineering, **minimal-and-purposeful** for design.

Read the deepest subsystem file that applies: `src/components/nav-rail/` (the single navigation
surface), `src/components/wiki/`, `src/components/agentic_search/`, `src/components/apps/`. The
wiki folder also carries a `README.md` depth reference (file map, endpoint table, Mermaid
invariants, Q&A streaming contract).

## Design philosophy — minimal & purposeful

Two surfaces, two jobs: **left = calm reading**, **right = instrument panel**. Each side has its
own shape vocabulary; mixing them flattens the hierarchy.

| Side | Radii | Surfaces | Signature |
|---|---|---|---|
| Left — conversation | `8` cards, `6` micro, `8/8/0/8` user bubble (`.bubble-notch`) | `--background` / `--card` / `--user-message-bg` | Bubble notch is the **only** silhouette signature |
| Right — workspace | `6` cards, `4` count pips, `0` sliding tab indicator, `10` terminal chrome | `--code-chrome` / `--code-body` family | `2.5 px` brand left rail on **log entries only** |

`rounded-full` (`9999px`) is reserved exclusively for **state containers**: status badges,
agent-id pills, scrollbars, brand mark, run-pulse dot, `<ScrollToBottom>`, the frosted
`.session-ts-rail` backplate. Never on buttons, cards, or chrome — those use `rounded-md`/
`rounded-lg` via `Button` (`components/ui/button.tsx`).

### Typography — scale, weight, and the mono law

Inter (variable weight) + JetBrains Mono. The variable weight axis is the point: it carries a
surface's hierarchy on weight alone rather than a second typeface or a 1px size step. The one
exception is the embedded Streamlit sandbox, which keeps Streamlit's own bundled fonts by design
(see "Stlite widget rendering" → Fonts).

`tailwind.config.js` `theme.extend.fontSize` is the source of truth; every size must resolve to
one of these steps:

| Class | Size | Use |
|---|---|---|
| `text-2xs` | 11px | metadata, chips, badges, timestamps |
| `text-xs` | 12px | secondary / supporting |
| `text-sm` | 13px | dense body: rail rows, card bodies, list rows |
| `text-base` | 14px | primary body |
| `text-lg` | 16px | section titles |
| `text-xl` | 20px | pane titles |
| `text-2xl` | 28px | landing headline — the largest type in the console |
| `text-field` | 16px | text inputs only, never body text |

`sm` and `base` are deliberately redefined off Tailwind's stock 14px/16px down to 13px/14px — an
instrument panel is one step denser than a content site.

`text-field` is not a scale step, it is a floor: iOS Safari zooms the viewport when a focused text
input computes below 16px, and `base` is 14px here, so every `<input>`/`<textarea>` carries
`text-field` instead of following body type down. It is named `field`, not `input` — see the
`text-input` collision trap under "CSS / component pitfalls".

**Adjacency rule.** A single surface uses at most three adjacent scale steps. Hierarchy inside a
surface comes from weight and colour, never from a 1px size step. The NavRail spends only ONE
step (see `nav-rail/CLAUDE.md`); that is not licence to loosen the three-step ceiling elsewhere.

**`em` is allowed, `px`/`rem` absolute sizes are not.** `src/__tests__/typographyScale.test.ts`
enforces this: it bans `text-[<n>px]`/`text-[<n>rem]` but permits `text-[<n>em]`, because an em
size stays proportionate to whatever step its parent chose (inline `code` at `0.9em` works in a
heading and in body prose). If a surface needs a size the scale lacks, add a step in
`tailwind.config.js`; never inline a literal.

**Weight ramp.** `font-normal` (400) is the default. `font-medium` (500) is emphasis only — a
title, an active-state tag, badge text, a value distinguished from its label. `font-semibold`
(600) is headlines only. Applied almost everywhere, a weight step stops signalling anything;
strip `font-medium` on sight from anything that isn't emphasized.

**The mono law.** Monospace is for machine text you might copy, diff, or align: code, terminal
output, diffs, file paths, ids/SHAs, cron expressions, raw JSON, column-aligned numerals. Strip it
from labels, buttons, badges, chips, headings, prose, empty states, status text, app names,
version strings, and non-aligned counts. A version string like `v3` is a label — strip it. A
trigger kind like `time.cron` is a dotted machine identifier — keep it. When ambiguous, ask
whether a user would ever select and paste it elsewhere.

### Composer chrome — one family

Every composer — Tasks (`InputBar`), Search (`SearchBar`), Wiki (`QADock`), Apps (`AppsLanding`
`HeroComposer`) — shares ONE chrome system. A composer that hand-rolls its surface is a review
reject.

- **The knobs**: `--composer-bg` (= `hsl(var(--card))`) and `--composer-radius` (= `1rem`) in
  `index.css`. Change a knob, every composer follows. `composerSurface()` takes
  `{elevation: "elev-1"|"elev-2", halo: "soft"|"strong"}` — there is deliberately NO radius option.
- **The CSS family owns ALL states**: `.composer-surface` (border-strong hairline, 200ms
  transitions) + `:focus-within` (primary/0.55 border + 4px primary halo sized by `data-halo`) +
  `data-running` (permission tint) + `data-command` (primary tint, suppressed while running).
  Never express a composer border/halo/tint as per-component Tailwind classes — add a `data-*`
  state to the family instead.
- **`ComposerShell`** (`ui/composer-shell.tsx`) is what to reach for: slots
  `top`/`toolbarLeft`/`toolbarRight`/`popover`, `bodyClassName` = the surface-override seam
  (tailwind-merge, later wins — QADock's floating `bg-card/95 + backdrop-blur` is the canonical
  positional exception), `className` = outer-wrapper layout passthrough (the `relative w-full` ↔
  `fixed …` merge is twMerge-load-bearing). `ComposerSendButton` is the shared send affordance.
- **Tasks is chrome-shared, behavior-bespoke**: `composerCard()` only adds elevation
  (elev-1→elev-2 on expand) + `--composer-padding`; its running Queue/Stop, RunIndicator,
  plan-mode and slash-command logic stay in `InputComposerBody` and signal the family via data
  attrs.
- **`CreateAppDialog`'s textarea is NOT a composer** — it is a form field in a dialog and stays a
  shadcn `<Textarea>`; forms look like forms.
- **Typography**: every composer's `<textarea>`/`<input>` uses `composerInputCls()` — the
  parameterless helper in `ui/composer-shell.tsx` returning `text-field` (16px) plus its
  placeholder tier. There is no smaller sanctioned typed-text size: both `text-base` (14px) and
  `text-sm` (13px) sit under the iOS floor. "Hero" vs "compact" sizing lives in padding and
  chrome, never in the input's font size. **The placeholder is a deliberately smaller, separate
  tier** — `placeholder:text-base placeholder:font-normal` — safe against iOS zoom because the
  zoom keys off the focused input's computed size, never the inert `::placeholder`. Never spell
  `placeholder:text-*`/`placeholder:font-*` locally. Placeholder COLOUR stays local at each call
  site: bare `placeholder:text-[hsl(var(--muted-foreground))]`, no opacity modifier.

### Operating principles

- **Status in exactly one place.** Global state → navbar `<StatusBadge>`. Live telemetry →
  `<RunTelemetry>` as `variant="compact"` in the composer strip and `variant="full"` in the
  workspace sticky spinner — same `RunStatus` data, two windows. **Failure → `<RunFailedCard>`**,
  the one run-failure readout, rendered by both the conversation timeline and the trace panel.
  Stop → composer only. Don't start a parallel readout; extend one of these.
- **Phase-driven visibility.** Spinners, run-strips, telemetry mount only when `isRunning`.
  `runStatus` is computed once in `SessionDetailView` and threaded down; nothing polls.
- **Steering-aware composer.** During a run the composer stays alive: `composerCard({running})`
  tints the border via the permission token, placeholder shifts to "Steer the run…", Send becomes
  **Queue** (`ArrowUpRight`), Stop appears alongside (not replacing it), and the toolbar locks
  session-config/model (visible but disabled — state stays transparent).
- **Stop is destructive — two-step confirm always.** `InputComposerBody.StopWithConfirm`:
  ghost+red-hover button → popover above with danger primary + ghost cancel. Esc in the textarea
  opens the same popover; it never directly cancels.
- **Progressive disclosure.** Latest assistant turn fully expanded; older turns auto-collapse to
  **140 px** with a mask-fade (`SmartCollapse` in `ConversationTimeline`). The assistant turn has
  exactly ONE footer strip (`AssistantTurnFooter`) and nothing else beneath the message. The whole
  strip idles at `opacity-0` and reveals only on `group-hover/turn` or `group-focus-within/turn` —
  there is **no** always-visible `isLatest` case. Strict two-sided layout: read-only info LEFT
  (model id · generation timestamp · token usage · context bar), interactive controls RIGHT
  (`CopyButton`, `Trace`, `⋯`). Copy lives ONLY in that right cluster (`MessageBubble
  showCopy={false}`).
- **Whitespace over lines.** Padding rhythm carries turn boundaries; the composer dissolves UP
  into the conversation via `.composer-band-glow` — no top hairline. Add a hairline only when
  whitespace is ambiguous (workspace tab strip, run-indicator strip).
- **Animation is information.** Only motion: state transitions, entrance stagger
  (`.session-row-in`), and run-state aliveness (`.session-cmp-pulse` is the sole infinite loop
  while idle; `.flower-mark` spins only while mounted). `prefers-reduced-motion` kills every
  keyframe.
- **Accessibility.** Hit targets ≥ 24×24 (AA), primary controls 32–40 px via `Button`
  `sm`/`md`/`lg`. Hover-revealed elements must also reveal on `group-focus-within`. Icon-only
  buttons need both `aria-label` and `title`. Don't strip `Button`'s focus-visible ring.
- **Novelty earns its place.** Two non-standard widgets ship, each with a job: `<TurnScroller>`
  (token-weighted cost-map dots pinned via `absolute left-2` inside `.conv-scroll` — deliberately
  NOT viewport-`fixed`, so it stays bounded below the SessionHeader and above the composer;
  segments capped at 22 px) and `<FlowerMark>` (branded conic ring local to `LogsView`). Add a
  third only if it answers a question the existing vocabulary can't.

### Session detail anatomy — discoverability map

| Surface | Where it lives |
|---|---|
| Composer band glow | `InputBar.tsx` + `.composer-band-glow` |
| Composer shell (running tint, focus halo) | `composerCard({expanded})` in `InputBar.tsx` layers elevation/padding over `.composer-surface`; running/command tints ride `data-running`/`data-command` |
| Top-level model + fallback-chain picker | `ModelSelector.tsx` (footer pill) renders the shared `ModelPickerTabs.tsx` — a Model \| Fallback tab strip over ONE `Command` (one filter + refresh row serves both tabs), with a red/green state dot on the Fallback tab. `ModelPickerTabs` is the ONE popover body reused by every run-starting surface: the Tasks composer (`ModelSelector`), the wiki configure wizard + project settings (`wiki/ModelPicker.tsx`, opts in via its `fallbackModels`/`onFallbackModelsChange` props), and the agentic-search scope menu (`SearchScopeControl.tsx`, mounted inside a `DropdownMenuSub`). `FallbackChainRow` (`ModelFallbackChain.tsx`) is the shared ordinal+icon+name+remove row both the non-tabbed `ModelFallbackChain` (kept for a fallback-only surface with no paired model picker) and `ModelPickerTabs` render. The ladder is a flat `string[]`; there is NO separate "enabled" wire field — the Switch maps to null/empty-vs-list. Stacking the model list and the fallback editor in one scrolling pane (the pre-tab shape) stranded the filter input's border short of the panel edge and turned an armed chain into a linear mess — tabs fixed both. |
| Session-config drill-in (project/branch/worktree/skills/integrations) | `ConfigMenu.tsx` (root list → panels; model/fallback deliberately absent) |
| Session header (back, editable title, status, IDE capsule, overflow, app/wiki jump) | `SessionHeader.tsx` — in-pane sticky z-20; module-scope subcomponents preserve Radix state. Its desktop subtitle is a `·`-separated segment chain (timestamp · model · `ContextWindowBar` · `RepoLink` · `DiffStats`), each `shrink-0` |
| ThreadList rail | `nav-rail/` Tasks section + `assistant-ui/thread-list.tsx` (`RailThreadList`, rows off `threadIds`) |
| Run-indicator strip + Stop confirm + Send→Queue | `InputComposerBody.tsx` |
| Conversation lane (24 px gutter) | `ConversationTimeline.tsx` + `.conv-scroll` |
| User bubble notch | `MessageBubble.tsx` + `.bubble-notch` |
| Smart-collapse + turn footer + edit-state card | `ConversationTimeline.tsx` (`SmartCollapse`, `AssistantTurnFooter`, `.edit-kbd`) |
| Mid-run Trace pill (BOTH in-flight rows) | `ConversationTimeline.tsx` `TracePill` — shared by `PendingAssistantRow` **and** `StreamingAssistantRow` |
| Live todo/plan checklist | `TodoCard.tsx` — renders the `todos` event (`{items:[{label,status}],source,agent_id}`), parsed/upserted per turn in `utils/timeline.ts` |
| Model-authored generative-UI card | `GenerativeUICard.tsx` + the allowlist in `components/generative-ui/` |
| Agent-driven project switch | `ProjectSwitchCard.tsx` (composes `LogEventCard`; rendered by BOTH `ConversationTimeline` and `LogsView`) |
| TurnScroller cost map | `TurnScroller.tsx` + `.session-ts-rail` / `.session-ts-seg` |
| Workspace tabs | `WorkspacePanel.tsx` + `.tab-ind` |
| Log / Diff / FileRead cards | `LogEventCard.tsx`, `DiffCard.tsx`, `FileReadCard.tsx` |
| Run-failure readout + inline recovery | `RunFailedCard.tsx` (rendered by BOTH `ConversationTimeline` and `LogsView`) |
| Sticky FlowerSpinner | `LogsView.tsx` `FlowerMark` + `<RunTelemetry variant="full">` + `.spinner-sticky` |
| Pane separator grip | `SessionDetailView.tsx` `<PanelResizeHandle className="pane-rail">` |
| Shared run telemetry + elapsed + live tok/s | `components/RunTelemetry.tsx` fed by `hooks/useThroughput.ts` + `hooks/useElapsed.ts` |

## Library-first principle — read this before writing any UI code

**Default position: someone else has already built it.** Every new component, hook, or behavior
walks this tree before custom code is written:

1. **A shadcn/ui block** (https://ui.shadcn.com/blocks) for the screen-level problem? Sidebars,
   dashboards, login forms, chat layouts, settings panes, data tables come pre-composed. Copy via
   `npx shadcn@latest add <block-id>`.
2. **A shadcn/ui primitive** (https://ui.shadcn.com/docs/components)? Popover, Dialog,
   DropdownMenu, Command, Tabs, Tooltip, Sheet, Combobox, Sonner, Calendar. Install via
   `npx shadcn@latest add <name> -y` — do NOT hand-write it, even if it looks small.
3. **A Radix primitive** (`@radix-ui/react-*`)? Radix already solves click-outside, Escape, focus
   trap, ARIA, scroll-lock, portaling, keyboard nav. Add a thin wrapper in `src/components/ui/`.
4. **A battle-tested library** for the concern? See "Architecture constraints" — that table is
   the canonical list.
5. **Only after 1–4 are exhausted** may you write custom code, and the burden of proof is yours in
   review.

| You're tempted to write… | Use instead |
|---|---|
| `useEffect` + `document.addEventListener('mousedown', …)` for click-outside | shadcn `<Popover>` / `<DropdownMenu>` / `<Dialog>` (Radix handles it) |
| `useEffect` for Escape-key dismissal, focus trap, portal, scroll-lock | Same |
| `useState` + `useEffect` to fetch and cache JSON | TanStack Query `useQuery` |
| `setInterval` polling for live data | `refetchInterval` (function form) |
| `setTimeout` debouncing inside an input | shadcn `<Command>` (cmdk debounce) |
| `useState<view>` + `pushState` + `popstate` listener | wouter `<Route>` + `useLocation` |
| Per-field `useState` + manual validation | react-hook-form + zod + shadcn `<Form>` |
| Toast/notification scaffolding | shadcn `<Sonner>` |
| Tooltip with manual hover state | shadcn `<Tooltip>` |
| Bespoke split-pane / resizable layout | `react-resizable-panels` v4 |
| Custom modal scaffolding with `position: fixed` + manual `z-index` | shadcn `<Dialog>` / `<Sheet>` |
| Custom theming toggle | shadcn theme convention (CSS variables) |

Also forbidden: "just a small wrapper" components that re-implement a shadcn primitive with
slightly different styling — import the primitive and pass `className`.

## CSS / layout rules

### Z-index stacking contexts — the #1 source of popup regressions

Before changing popup direction or adding absolutely-positioned overlays, trace the full stacking
context chain from root to popup. A `z-50` inside a `z-10` parent loses to a sibling at `z-20`.

Walk the DOM from the popup to the root, noting every element that creates a stacking context:
`position: relative/absolute/fixed/sticky` with an **explicit** `z-index`; `opacity < 1`;
`transform`; `filter`; `will-change`. `overflow: hidden/auto/scroll` does NOT create one (but
clips visually).

Current layout stacking (HomeView): outer container `relative overflow-hidden` (no stacking
context) → fixed top section holding InputBar at `z-20` (popups live here) → scrollable bottom
section (no z-index) → sticky session header at `z-10`. Popups open downward in home mode and
overlap the session list, so the fixed top section's z-index must stay above the sticky header's.
Change any link in that chain and re-verify popups render above session content.

### Popup direction depends on InputBar mode

`mode="home"` (InputBar near top) → popups open **down**; `mode="detail"` (InputBar at bottom) →
**up**. Controlled by `popupDirection` in InputBar and the `direction` prop on `<Popover>`
(`components/ui/popover.tsx`), which maps to Radix `<PopoverContent side="bottom"|"top">`. Never
hardcode direction — derive it from context. All dropdown popups use that one `<Popover>` wrapper;
do not duplicate popup styling inline.

## Data fetching patterns

### TanStack Query owns server state

All server-data hooks are thin wrappers around `useQuery`/`useMutation` (v5). The
`QueryClientProvider` mounts at the app root with a 60s default `staleTime`. There is **no manual
TTL cache** in `api/client.ts` — the query cache is the single source of truth.

```ts
useQuery({ queryKey: ['mcp-tools', project ?? null], queryFn: () => listMcpTools(project), staleTime: 60_000 })

const qc = useQueryClient()
useMutation({ mutationFn: createProject, onSuccess: () => qc.invalidateQueries({ queryKey: ['projects'] }) })
```

Always invalidate by `queryKey` after a write; `invalidateQueries` matches by prefix, so
`['sessions']` invalidates every per-session query too. Use `select` to project/filter without
re-running `queryFn` — reach for it instead of copying a filtered list into component state, which
is a second cache wearing a `useState`.

**Conditional polling** uses the function form of `refetchInterval` so the interval reacts to the
data just fetched — tighten while transitional, relax once settled. This is the pattern for any
surface with no push channel (`useIdeStatus`, `useNotifications`, `useApps`, wiki indexing-job
polling):

```ts
refetchInterval: (query) => (query.state.data?.status !== 'ready' ? 2_000 : 30_000)
```

**Session state and the transcript are the one exception, and they don't poll at all.**
`useSessionEvents` holds open ONE SSE connection to `GET /api/sessions/{id}/stream`
(`api/sessionStream.ts`) for the life of a session view — the transcript,
`running`/`status`/`done_reason` and every downstream consumer update off pushed frames. Don't
reintroduce a poll for a surface the server already pushes to.

### `api/httpBase.ts` is the ONE plain-`fetch` base

"No custom fetch wrappers" does not mean zero transport code — something must build the URL and
headers. `src/api/httpBase.ts` is that, and it is the ONLY copy: `withBase` / `authHeaders` /
`jsonHeaders` / `readJson` / `readError`. `api/realClient.ts` and `api/git.ts` both consume it.
`authHeaders`/`jsonHeaders` take an OPTIONAL `apiKey` defaulting to the `client.ts` singleton,
which is what lets both call styles share one implementation.

Two seams stay deliberately separate — do not fold them in: `api/sse.ts` (streaming) and the
wiki's `components/wiki/api/client.ts:http<T>` (its own mock/backend-swap contract for the
`<path:slug>` routes). A THIRD fetch quartet is what to reject in review.

### Hook conventions

`useMcpTools(project?)` / `useSkills(project?)` re-fetch when project changes (project is part of
the queryKey); `useProjects()` is global. All hooks expose a `refresh()` shim delegating to
`queryClient.invalidateQueries` so consumers don't need to learn the query API.

⚠️ **`isPending` is not a loading flag for a query that can be disabled.** A DISABLED TanStack
query stays `pending` forever, pinning a skipped query's UI in permanent loading. Use `isLoading`
(= pending AND fetching) — see `useProjectGit`, where a project with no git repo skips the
worktrees query.

### Token usage — three semantics, one source of truth

`build_usage_numbers` (and `/api/sessions/{id}/usage` feeding `useSessionUsage`) returns three
distinct semantics:

- **Context fill (right now):** `root_last_input_tokens` — the size of the most recent root
  prompt. Drives the `<ContextWindowBar>` fill, "X% used", and "tokens until auto-compact".
- **Context pressure (worst case):** `root_peak_input_tokens` / `sub_peak_input_tokens`. Show as a
  secondary "peak this session" stat. **Never sum input across calls in a turn** — the prompt
  grows as tool results stack onto the same context, so summing double-counts the baseline.
- **Billable (cost):** `*_input_tokens_billed` / `*_output_tokens`. Pair with `*_cache_read_tokens`
  and `*_cache_creation_tokens` to apply the discount client-side: Anthropic cache reads bill at
  **0.1× input**, OpenAI cached at **0.5×**, Anthropic 5-min cache writes at **1.25×**.
  `*_reasoning_tokens` is hidden thinking from extended-thinking models — billed as output,
  surfaced separately.

`<ContextWindowBar>` displays all three (fill = now, popover header = peak, popover body =
billable + cache + reasoning). Reuse it; do not hand-roll a token display.

## Performance — the console's half of the boundary contract

Root `CLAUDE.md` → "Performance is a contract at every boundary" owns the cost classes, the
process-wide concurrency budget and the measurement law. This section is what those mean on a
React client. **The console is where a server's cost class becomes a blank screen.**

### Render gating — mount from the route param, let listings enrich

**A detail surface never waits on a collection query.** `GET /api/sessions` is `O(collection)`:
2.0 MB and several seconds on a deployment holding ~900 sessions (measured between 4 s and 8 s
across runs — re-measure rather than trusting either number). Resolving a session with
`sessions.find(...)` over that listing made opening ONE session wait on a summary of ALL of them,
rendering "Loading session…" then "Session not found." — which reads as broken, not slow.

Two halves, both load-bearing:

1. **Mount from the id alone.** `App.tsx`'s `SessionDetailRoute` builds a `{session_id: id,
   title: ""}` fallback `SessionSummary` and renders immediately; the listing row supersedes it
   when it arrives. **Memoise the fallback** — an unrelated list refresh otherwise hands the view
   a fresh object identity every render.
2. **Build the subject field-by-field from whichever source has it.** `SessionDetailView`'s
   `subject` memo spreads the listing row, then fills each field from this page's OWN per-session
   reads: `title` from the `/events` payload's projection, `created_at` from `events[0].ts`,
   `recoverable` from the live flag, `context` from `getLastContext`.

⚠️ **Precedence is decided per FIELD and is deliberately NOT uniform — making it uniform is what
flickers.** `title` prefers the LISTING snapshot (`session.title || liveTitle`) because a rename
patches that listing optimistically through `applyTitle`; letting the server projection win would
flicker the old title back on every poll tick while the projection lags. `recoverable` and
`status` prefer the LIVE value, because there the server holds the newer fact. Write the direction
AND its reason at each field — "take the freshest" is wrong half the time, and an optimistic local
write is precisely the half it is wrong for.

### Navigation must not await work it does not need

**Routing needs an id, not an accepted mutation.** `App.handleCreateAndRun` navigates the instant
`create` resolves and posts the query afterwards. `create` is awaited because its response IS the
id; nothing else on that path is.

**Moving a navigation earlier moves its error handling too — this is the part a review misses.**
An inline `<Alert>` lives on the page you just left, so a post-navigation failure written into
`actionError` is swallowed in silence. Split by which page is mounted when the call can fail:

| Failure | Page on screen | Surface |
|---|---|---|
| `create` rejects | landing page (no session yet) | inline `actionError` Alert |
| `uploadAttachments` / `postQuery` reject | the session page, post-hop | `toast.error` |

Busy flags move with it: `setCreating(false)` fires at the hop, not after the query, or navigating
back strands the landing composer as permanently busy.

### Payload and poll budgets

| Fetch | Cost class | Notes |
|---|---|---|
| `GET /events`, first poll (no `after`) | unbounded — **the known defect** | 14.8 MB measured for a 10,266-event session, then JSON-parsed and folded through `buildTimeline` |
| `GET /events`, subsequent polls | `O(new events)` via the `lastTsRef` cursor | the cursor is the only thing making a 1 Hz tick affordable |
| `GET /api/sessions` | `O(collection)` | enrichment only — never gate a render on it |
| `GET /timeline` | `O(one record)`, bodies excluded | **not a substitute for `/events`** |

| Cadence | What earns it | Where |
|---|---|---|
| 1 s | an in-flight run the operator is watching | `useSessionEvents` `POLL_INTERVAL_MS` |
| 2–4 s | a bounded resource actively changing | `useSessionUsage`, `useIdeStatus` while a container starts |
| 15–30 s | keepalive for a state change originating off-tab (CLI, Aura, a trigger) | `useSessionEvents` `KEEPALIVE_INTERVAL_MS`, `useIdeStatus` idle |
| stop (`false`) | an absorbing state, strictly checked | `useSessionEvents` on `terminated === true` |

**A keepalive tick is not free.** The API runs a single gunicorn worker whose `--threads` value is
its TOTAL process-wide concurrency (`docker/Dockerfile.api`), so every open session tab holding a
15 s tick is a recurring claim on one of those slots. Idle polling scales with tabs left open, not
with work being done.

⚠️ **Response SIZE and server TIME are independent.** A cursor can shrink a payload to a few
hundred bytes while the server still re-reads the whole record. Measure `time_total`, not only
`size_download`, and against a session with real history.

⚠️ **A 235× size gap between two endpoints over the same resource almost always means they return
DIFFERENT THINGS.** `/timeline` is 63 KB where `/events` is 14.8 MB for the same session, because
it serialises with `exclude={"turn": {"events"}}` — it is the assembled conversation, not a
cheaper event log. Swapping the transcript onto it would render messages with no bodies.

### Component complexity — bound the fetch, not just the list

Virtualising a list that already pulled 14.8 MB saves paint and nothing else; transfer, parse and
fold have all happened. Bound the fetch first.

- **No `O(all sessions)` work in a detail view.** Scanning or `.find()`-ing the sessions listing to
  render ONE session re-introduces the gate the route fallback removed.
- **Don't re-fold the whole transcript on every tick.** `buildTimeline(events)` is memoised on
  `events`, and a poll appending a single event mints a new array — so a 10k-event session re-folds
  10k events once per second, and every other `useMemo` over `events` (`getActiveTurn`,
  `getActiveStreamText`, `isRunAccepted`, `mergeDiffFiles`) is a further full pass on the same
  tick. Prefer an incremental append where available; where the fold must stay whole, that is a
  cost to STATE, not one to discover later in a flame chart.

### Profiling recipes — run these, don't reason about them

```bash
curl -sk -o /dev/null \
  -w 'time_total=%{time_total}s size_download=%{size_download}B http=%{http_code}\n' \
  "http://localhost:5125/api/sessions" -H "X-Api-Key: $KEY"
```

Point it at `/api/sessions/<id>/events`, then again with `?after=<ts>`, to see whether a cursor
narrowed the WORK or only the bytes. Cheapest measurement in the stack, and it routinely refutes
the obvious diagnosis.

| Question | Devtools panel | What to read |
|---|---|---|
| "why is this blank" | Network waterfall | which request the first paint is WAITING on — that request is the gate |
| "why is it janky once loaded" | Performance | long tasks on the poll tick: JSON parse plus the `buildTimeline` fold |
| "is it fetching this twice" | Network, filtered | duplicate keys mean a second cache, which library-first already forbids |

**⚠️ Verify the BUILT artifact, not the source.** nginx serves the console from
`/usr/share/nginx/html/assets/` inside the console container. Grep the served bundle for the symbol
you expect — **and always run a control grep for a string you KNOW is bundled**:

```bash
docker exec assistant-console-1 grep -rlF "run_accepted"   /usr/share/nginx/html/assets/
docker exec assistant-console-1 grep -rlF "session-events" /usr/share/nginx/html/assets/  # control: MUST hit
```

Without the control an empty result is unreadable — "not deployed", "wrong path", "empty assets
dir" and "this `grep` mangled the pattern" demand opposite next steps. **A suite cannot fail for an
event no client subscribes to**, so a client half that was never built passes every test; the
bundle grep is the only thing that catches it. A stale container also serves a stale bundle, so a
landed fix looks absent until the image is rebuilt AND the container recreated. The converse guard
is "Bundle presence ≠ visual render" below.

### Before you add a fetch or a poll

- [ ] What does its cost scale with? `O(all history)` does not belong on a request path.
- [ ] Does anything RENDER-GATE on it? A detail surface may gate only on a fetch for the one record it shows.
- [ ] Is it bounded, and does the cursor narrow the server's WORK rather than just the bytes?
- [ ] What cadence, and what earns it? A new interval constant needs its reason in the same commit.
- [ ] Is it a SECOND source for a fact something else already returns? One fact, one source.
- [ ] Does its failure surface on the page that will actually be mounted when it fails?
- [ ] Measured on the deployed artifact: `curl -w` for the endpoint half, a bundle grep WITH its control for the client half. A green suite closes nothing here.

## Architecture constraints

These libraries are not suggestions — they are the *only* approved choice for each concern.
Adding a competing library requires explicit design discussion, not a drive-by PR.

- **Server state:** TanStack Query v5. No custom caches, TTLs, or fetch wrappers.
- **UI state:** native React (`useState`, `useReducer`, Context). No Redux/Zustand/Jotai.
- **UI primitives:** shadcn/ui (Radix + cva, vendored into `src/components/ui/`). Never hand-roll
  click-outside, focus traps, Escape handlers, or ARIA.
- **Routing:** wouter v3.
- **Forms:** react-hook-form + zod for static schemas. RJSF stays only in `SettingsView`, whose
  schema is supplied dynamically by the backend `AppConfig` endpoint.
- **Styling helper:** `cn()` from `src/lib/utils.ts` — `clsx` + `tailwind-merge`, so later Tailwind
  classes override earlier ones.

### assistant-ui — the chat-surface vendor

**assistant-ui (`@assistant-ui/react`) OWNS the chat-surface concern**: composer chrome,
thread-list rail, and eventually the transcript. Bespoke chat-surface code is to be replaced by its
primitives, not extended. This is the design discussion the one-library-per-concern rule demands —
don't relitigate it per PR.

- **Two install surfaces.** `@assistant-ui/react` (+ `-markdown`) are npm imports; the styled
  components are **source-copied** shadcn-registry files under `src/components/assistant-ui/`
  (`npx shadcn@latest add "https://r.assistant-ui.com/base/<name>.json"`). `zustand` is in the tree
  ONLY as their internal dep; the "no Zustand for app state" rule stands.
- **⚠️ The registry CLI clobbers.** Re-running it rewrote our custom `button.tsx` to stock variants
  and injected Tailwind-v4 directives (`@import "tw-shimmer"`, `@custom-variant`, `@theme inline`)
  into `index.css`. Always diff-review its output; expect to revert those two files.
- **⚠️ npm lockfile churn.** Bare `npm install <pkg>` here rewrites the whole lockfile and DROPS
  hoisted transitives (`@testing-library/dom`, top-level `esbuild`) → broken tests/build. Fix:
  `git checkout package-lock.json && npm install`.
- **⚠️ Vendored files are written for Tailwind v4 + Base UI; we run v3 + Radix.** v4 syntax
  SILENTLY emits no CSS on v3 — no error, just unstyled. Translate AT MOUNT TIME, and only then
  (unmounted reference files stay vendor-faithful so upstream diffs stay clean): `bg-(--x)` →
  `bg-[var(--x)]` · fractional `size-4.5` → arbitrary values · `wrap-break-word` → `break-words` ·
  `@container`/`@md:` → unsupported, restructure · Base-UI `data-open:` → Radix
  `data-[state=open]:` · their primitives keep `render` props, but OUR Radix wrappers need
  `asChild`. `data-slot` attrs don't forward through assistant-ui primitives — style via
  `className` only.
- **`dark:` variants never fire here.** We toggle `.light` (dark is the `:root` default; Tailwind's
  selector strategy expects `.dark`, which never exists). Fold any vendored `dark:` style into
  token values.
- **Runtime seam = `useExternalStoreRuntime`** (`assistant-ui/MewboRuntimeProvider.tsx`, mounted in
  `AppLayout`). assistant-ui does ZERO networking. Thread-list fields nest under
  `adapters.threadList` with archived sessions as a SEPARATE `archivedThreads` array; active-row
  highlight comes free from mapping `activeSessionId → threadId`. **Render rows off
  `s.threads.threadIds`, NOT `threadItems`** (not 1:1 — indexing it throws `useClientLookup: Key
  undefined`).
- **`messages: []` is deliberate scaffolding.** `ConversationTimeline` still owns the transcript.
  `App.handleComposerNew` (the `onNew` seam) MUST keep the `isRunning` steer fork (`sendMessage` vs
  `postQuery`) in sync with `useSessionQuery.send` — a mid-run submit without it double-fires
  instead of steering.
- **Composer = vendor look, app logic.** `InputBar` renders the vendor card language but submit
  stays on `props.onSubmit` with its context assembly
  (`mcp_tools/skill/project/branch/model/fallback_models`). Never wire `ComposerPrimitive.Send` /
  runtime `send()` while `onNew` also posts — that's the double-fire. Control grouping is
  load-bearing: **model is a top-level footer control** (`ModelSelector.tsx`), `ConfigMenu` is
  session-config only (a root list that drills into panels), per-chat controls
  (attach/plan/voice/stop/send) sit directly in the footer.
- **`ConfigMenu` carries exactly ONE tab strip, and what it separates is a SCOPE from a TARGET.**
  The `'project'` drill-in is `Projects` | `Wiki & Apps`. A wiki project or a Mewbo App is not a
  fourth project source: it does not answer "where does this session run", it answers "what is
  this session about", and it is spent by **routing session creation to that product's own
  get-or-create endpoint** (`utils/sessionTarget.ts` → `openTargetSession`) instead of `POST
  /api/sessions`. **That is why it cannot be a `CommandGroup` beside Configured / Repositories /
  Managed** — sitting in that flat list it would read as another way to set `cwd`, and the house
  rule one line down (a repository is never a new session-context field) would be read as covering
  it. It does not: a target adds no key to `SessionContext` at all. The tab strip is the only
  affordance that says so before the click. Everything else stays a root list; a second tab strip
  anywhere in this menu is a review reject.
  - **A target is HOME-mode only** (`targetsEnabled={mode === 'home'}`). An existing session's
    purpose binding is durable and the server refuses to retarget it, so a detail-mode control
    could only offer a pick that fails. It follows that a target is never read BACK off the wire —
    no event or spec field carries one — so `InputBar`'s single writer can only ever RESET it on a
    session switch, never restore it. "Reset to defaults" clears it in home mode and is a no-op in
    detail mode, which is the same rule as `activeProject`'s, not an exception to it.
  - **A targeted send omits every workspace key** (`project`/`branch`/`skill`/`mcp_tools`) — the
    session arrives purpose-bound and would refuse them per turn, exactly as `toolsLocked` and
    `projectBindingLocked` already omit theirs. The model choice travels unchanged.
  - **The failure split is the one in "Navigation must not await work it does not need."** The
    target call replaces `create` and sits BEFORE the hop, because it is the failure that leaves
    nothing to navigate to: it surfaces on the landing page's inline `actionError` Alert. Anything
    after the hop is a `toast.error`.
  - Product marks come from `nav-rail/products.ts` — `BookOpen` wiki, `AppWindow` apps, one mark
    per product, the same rule the session-origin glyphs follow.

### Generative UI — model-authored cards through an allowlist

A `present_ui` call emits a `generative_ui` event carrying a JSON node tree; `GenerativeUICard.tsx`
walks it with assistant-ui's `GenerativeUIRender` against the eleven-name registry in
`components/generative-ui/`. Parsed in `utils/timeline.ts` **below** the open-turn gate (mid-run by
nature, like `widget_ready`) and upserted on `ui_id`, so a run refining a card it already showed
replaces it instead of stacking a near-copy.

- **`GenerativeUIRender` is STANDALONE** — `spec`/`components`/`Fallback` are plain props and it
  calls ZERO context hooks. That is why generative UI needed no transcript migration onto
  assistant-ui `Thread`, and why this call site keeps working after that migration happens.
- **The allowlist IS the security boundary, and props spread DIRECTLY onto the resolved
  component.** So no adapter may spread its props onto a DOM element — each destructures the names
  it knows and drops the rest, which is what keeps a model-authored `onClick` / `style` /
  `dangerouslySetInnerHTML` off the DOM. `Link` re-parses its `href` here as well as server-side
  (`http`/`https`/`mailto` only), because a href is the one prop that becomes executable when it is
  wrong and a replayed transcript reaches this renderer having bypassed emit-time validation.
  **`Fallback` is mandatory**: without one the renderer THROWS `GenerativeUIRenderError` on an
  unknown name, and a throw at transcript depth blanks the whole conversation. A model typo must
  cost one placeholder row.
- **The eleven-name vocabulary is a contract, not a menu.** Adding a name later is cheap; removing
  one breaks every transcript containing it. Each maps to a primitive already vendored in `ui/`,
  which is what makes the token, shape and type laws hold automatically. **A proposed node needing
  a NEW primitive is evidence it doesn't belong.** The registry lives in its own `.ts` beside the
  `.tsx` adapters: `react-refresh/only-export-components` warns when a `.tsx` exports both
  components and an object, `allowConstantExport` covers literals but not a `Record`, and
  `lint:ci` runs at `--max-warnings=0`.
- **Advertise and answer land together.** The console names `generative_ui` in
  `CLIENT_CAPABILITIES` (`api/realClient.ts`) AND renders the result — the capability is what binds
  the tool at all. Advertising without rendering is worse than neither.

### shadcn convention

- Components live under `src/components/ui/` as lower-case file names. They are *vendored* — patch
  the vendored file inline rather than wrapping it in another component.
- App-level wrappers belong in `src/components/` and **compose** the `ui/` primitive — never fork
  it. Pass `className` via `cn()` for visual variants. If a wrapper grows beyond ~30 lines of
  layout JSX, ask whether a shadcn block already covers it.
- New iconography uses `lucide-react`. Do not introduce a third icon library.
- `<Button>` (`components/ui/button.tsx`) cva variants are `primary | neutral | ghost` — there is
  no stock-shadcn `outline`/`default`; don't pass those names.
- `ui/card-surface.ts` (`cardSurface({radius, elevation})`) is the shared card geometry helper —
  the apps gallery and its skeleton both route through it.

## Navigation & information architecture

**There is exactly one primary nav: the left NavRail** (`src/components/nav-rail/` — its own
CLAUDE.md is the authority). There is NO top navigation bar anywhere, on any route. Everything
else is a facet of Settings, a slim in-pane contextual header (`SessionHeader`, `WikiTopBar`,
`AppDetailHeader`), or a link. Four standalone pages (`/projects`, `/plugins`, `/keys`,
`/triggers`) and `/draft` stay deleted.

- **The avatar menu is NOT a page index.** It lives in the rail footer and is exactly four rows:
  Settings · Documentation · GitHub · theme toggle. A menu that duplicates the primary nav teaches
  users the menu *is* the nav; adding a destination here is the smell.
- **`activeProduct` says which rail row is CURRENT, not whether the rail renders** (`App.tsx` →
  `'tasks'|'wiki'|'search'|'apps'|null`). The rail renders on **every** route; a route mapping to
  no product (`/settings`) marks nothing `aria-current` — never strand a route without primary nav.
- **Retired routes redirect, they don't 404.** `/projects`→`?facet=workspace`,
  `/plugins`→`?facet=plugins`, `/keys`→`?facet=security`, `/triggers`→`?facet=automation`.
  `/triggers` can't be a static `<Redirect to>` — `SessionTriggersSection` deep-links
  `/triggers?session=<id>`, so `TriggersRedirect` (`App.tsx`) carries the query across the hop.
- **There is no `/draft` page, but the `draft` BACKEND is live.** `POST /v1/draft/stream`,
  `mewbo_core.draft_stream.DraftStreamer`, and the `draft` **`SessionOrigin`** all exist — the origin
  is session provenance for sessions minted by external API callers. ⚠️ **`src/api/sse.ts` reads
  draft-owned and is not**: it is the shared generic SSE parser consumed by Agentic Search **and**
  the wiki (Q&A + indexing). A grep-and-delete purge of "draft" silently breaks both.

### Settings — faceted shell over RJSF (`components/settings/`)

The Settings page is a **faceted shell**, not a flat form, and it is the console's cabinet — four
surfaces that would otherwise be standalone pages live here as *panes*. RJSF is the per-section
field engine only (`RjsfTheme.tsx`); the grouping/search/save shell is ours.

- **`settings/panes.ts` is the facet→panes REGISTRY, and the shell knows NO facet by name.** A
  facet may carry schema-driven sections, custom panes, or both; panes render above sections.
  **Registering a pane is ONE line in `panes.ts` and ZERO in the shell.** Even the search-dimming
  rule is a registry lookup ("has panes" ⇒ never dim, because search only indexes schema sections),
  not a name test.
- **The pane contract — conform or you can't be registered.** (1) **Zero props.** A pane is a
  `ComponentType` with no props, which is what lets the registry be a plain data map instead of a
  switch. A pane fetches its own data via TanStack Query, and re-calling a hook the shell already
  called is **not** a second fetch (shared cache by queryKey) — so there is never a reason to
  prop-drill into a pane. (2) **`React.lazy` + its own `<Suspense>`** at the render site, so one
  slow chunk never blocks a sibling's fallback and heavy deps stay out of the Settings chunk until
  the facet opens. (3) **Chrome-agnostic** — bare `<SettingsCard>`s, no page width/padding; the
  shell owns `max-w-3xl mx-auto px-6 py-6`.
- **A pane sits in the facet that owns its settings. That adjacency IS the reason for the
  consolidation.** The plugin marketplace next to `plugins.marketplaces`; the trigger dashboard
  next to the trigger policy ceilings; managed projects next to the `projects` map. Registered:
  `agent`→SystemInstructionsPane, `plugins`→Plugins, `automation`→Triggers,
  `security`→SecretsSummary + ApiKeysView, `workspace`→Projects, `repositories`→RepositoriesPane +
  GitCredentialsView, `apps`→AppsPane.
- **A pane and its config section MOVE TOGETHER, in one commit.** Promoting Plugins to its own
  facet touched `facets.ts` (new id + `FACETS` entry + renumbered neighbours), `panes.ts`,
  `SettingsView.FACET_ICONS` (an unmapped lucide name falls back to `Settings2`), core
  `PluginsConfig.x-group`, the regenerated `configs/app.schema.json`, AND the `/plugins` redirect
  target.
- ⚠️ **`FacetId` and core's `x-group` must land in LOCKSTEP.** `SettingsModel` validates a section's
  `x-group` against `FACETS` and **silently buckets an unknown group into the `other` fallback
  facet** — no error, no warning. Ship an `x-group` in `config.py` without adding its id to the
  `FacetId` union and the section simply *vanishes* into "Other". `panes.ts` is keyed by `FacetId`,
  so a pane on an undeclared facet is at least a compile error; the schema side has no such guard.
  The live guard is `SettingsView.integration.test.tsx`, which mounts the REAL
  `configs/app.schema.json` and asserts the Plugins section renders in the Plugins facet — so
  regenerate the schema after any `x-group` edit.
- **`SettingsCard.tsx` is THE one SECTION-level card primitive.** Every top-level card renders
  through it — the schema-driven `SettingsSection` and every pane's own top-level cards. It owns
  the card class (`sectionCardCls` in `styles.ts`), the title style, and the description slot
  (routed to `FieldHelp`). A new top-level SECTION card that isn't a `<SettingsCard>` is a review
  reject. This does NOT extend to smaller non-section chrome nested inside a section
  (`SettingsView`'s "View as JSON" `<details>`, `RjsfTheme`'s object-group `<details>`,
  `PluginsPane`'s per-plugin rows) — an intentional lighter primitive. Keep `SettingsCard` a dumb
  shell; Save/Reset, dirty state and fetching belong to the caller.
- **`?facet=` deep-linking.** The **NavRail's Settings zone is the ONLY facet navigation** — the
  shell only READS `?facet=` and derives `activeFacet` from it directly (no local state, no seed
  effect; an unknown facet falls back to the first visible one). The facet WRITER (the rail's
  `settingsSection`) **rebuilds the query from `prev` and only `.set()`s the `facet` key** with
  `{replace: true}`, so unrelated params survive a switch (which is what keeps a `?session=`
  deep-link alive) and Back leaves Settings rather than walking facets.
- **`SettingsModel.ts`** (pure, React-free, unit-tested) turns the backend `AppConfig` schema +
  `{config, secrets}` into facets → sections → fields and owns slicing/search/diff. Facets and
  their order/icons/blurbs live in `facets.ts` (React-free — icons by lucide *name*); facet
  membership comes from each section's `x-group` metadata. **`SettingsModel.uiSchemaFor(id,
  secrets, savedAt)` is the single archetype-routing home** — schema shape → widget
  (`list[BaseModel]`→`recordList`, `dict[str,X]`→`keyedCollection`, secrets→`secret`,
  marketplaces→injected `itemValidator`, `plan_mode_shell_allowlist`→`restoreDefault`). Never
  recompute routing in components.
- **Copy standard: no em dashes in any user-visible string.** Not in a facet `blurb`, a
  `<SettingsCard description>`, an empty state, or a `config.py` field description (those render
  here as field help). Help text must READ as connected prose that teaches the concept, not
  telegraphic clauses bolted together with dashes. Code comments may keep their em dashes.
  **Respect the `FieldHelp` contract:** a facet `blurb` is a bare line, so keep it under 140 chars
  or it collapses into a popover-only `?`; a card/field description gets a summary line, then
  `\n\n`, then the narrative that lands in the popover.
- **`SettingsView.tsx`** owns edit state (a `sectionId→formData` map holding ONLY genuine edits).
  A section's current value is resolved **INLINE at render time** — `formState[id] ??
  normalizeSection(config?.[id])` — never seeded into `formState` by an effect. ⚠️ **This is the fix
  for an RJSF seed-race and it is a general rule.** RJSF's `Form` fires `onChange` with its OWN
  computed schema defaults *during its mount* (a child's effects run before the parent's), so an
  eager parent seed effect loses the race: the `onChange` writes the empty computed shape (an array
  field defaulting to `[]`) into `formState`, permanently shadowing the real config. Saves are
  per-section: `model.patchFor(id, sectionValue(id))` → `useConfig.savePatch`; re-seed only the
  just-saved section so concurrent sibling edits aren't clobbered.
- **`SecretField`** = write-only 3-state widget (unconfigured / configured-masked+Replace /
  editing) for `x-secret`/`writeOnly` fields; "configured" comes from the `secrets` map, never a
  value (the backend strips secret values, so the field is always absent from `config`).
- **Field kit (`settings/fields/`)** — atomic widgets *around* RJSF that NEVER fall through to its
  defaults. Load-bearing: RJSF's default array/object/dict toolbars render as invisible `0×0px`
  buttons under our theme, so collections become silently un-editable. One registered
  **`ArrayFieldTemplate`** owns every array; **`RecordListField`** (`ui:field:"recordList"`) owns
  `list[BaseModel]`; **`KeyedCollectionField`** (`ui:field:"keyedCollection"`) owns `dict[str,X]`
  with an injected value-renderer + `JsonValueEditor`. **`FieldHelp` is the ONLY description
  renderer** — render help nowhere else. **Its collapse rule keys off an author-supplied `\n`,
  never length alone**: no break + short ⇒ all inline; **has a `\n` ⇒ that first line is a real
  author-written summary, shown inline with the full text behind a `?` popover**; no break + long ⇒
  popover-only. It deliberately does NOT fabricate a summary by truncation (a character cut can
  slice a markdown token in half). **So the way to get a summary line is to write one.**
  `marketplaceValidation.ts` MIRRORS backend `plugins.py::_resolve_git_url`, injected as the
  marketplaces `itemValidator`. **Every settings `<input>`/`<textarea>` renders `inputTextCls`**
  (`settings/styles.ts` = `inputBase` + `text-field md:text-sm`) — the settings-side seam for the
  same iOS-zoom floor the composers carry via `composerInputCls()`.
- **`GitCredentialsView`** is the CRUD surface over the product-wide `/v1/git/credentials` registry
  (`api/git.ts` + `hooks/useGitCredentials.ts`) — NOT a wiki feature, even though the wiki is its
  first consumer. Two invariants: a credential's `value` is **write-only** (the API returns only a
  `valueHint`, so clear it from form state right after submit), and `scopeType` (`host` = shared by
  every repo on that host, `repo` = pinned to one slug) is **served by the API**, not re-derived in
  a component. The onboarding wizard consumes the same hook read-only to hint "a saved credential
  already covers this repo" (repo-scope match wins over host-scope).
- **The Repositories facet: a repository is a PRODUCT object, and registering one is a no-op.**
  `RepositoriesPane` reads `useRepositories()` (`/v1/git/repositories`), never `useWikiProjects()`.
  That distinction is the whole facet: wiki `Project` rows are written only at index finalize, so a
  list built from them can only show repositories that already survived a paid multi-minute run —
  it can never answer "what does Mewbo know about?". The one wiki read that remains is enrichment
  (a landing page id to link to), never truth. **`AddRepositoryDialog` submits and returns you to
  the list**: it normalizes a URL, validates it, persists a record, and clones and indexes nothing.
  A settings-level "add" must never leave Settings. **"Generate wiki"** is the ONE row action
  allowed to navigate, carrying `?repo=<slug>` so the wizard seeds url, platform and default branch
  from the record (hand it `?url=` instead and the wizard's seed yields to the explicit url,
  filling one field and silently dropping the other two). **"Remove from Mewbo" and "Delete wiki
  index" are permanently separate actions with separate confirm copy** — deregistering forgets a
  bookmark, deleting an index destroys a paid run.
- **The composer's project picker lists registered repositories, and the selection contract does
  NOT grow to hold them.** `ConfigMenu`'s `'project'` drill-in renders a **Repositories** group
  beside Configured and Managed. A repository with a checkout selects through the EXISTING
  `managed:<project_id>` mechanism — a repository is never a new session-context field. One with no
  checkout renders as the labelled **"Set up"** row: clicking calls `POST
  /v1/git/repositories/<slug>/checkout` (`useCheckoutRepository`), spins that row, then selects the
  project id read **off the response**. Three rules hold it together. (1) **Nothing clones
  implicitly** — the clone is a labelled control or it does not happen. (2) **The row IS the
  control**, not a nested `<button>`: a button inside a `role="option"` has no keyboard path in and
  cmdk would fire both handlers on one click. (3) **A checked-out repository is filtered OUT of the
  Managed group** — listing it in both would show one thing twice. A failure to LIST degrades to no
  group; a failure to CHECK OUT banners, because the user asked for it.
- **The Workspace facet holds two things users conflate — keep saying so.** `ProjectsPane`
  (`/api/projects`) = workspaces **Mewbo** creates and owns, including git worktrees; server-owned
  means server-deleted. The schema `projects` dict below it = directories **you** already have,
  registered by name in `app.json`; Mewbo only points sessions at them. The pane's card description
  carries the distinction — don't drop it.
- **`SystemInstructionsPane`'s variable reference is LENGTH-ADAPTIVE, and the popover is an a11y
  decision.** `GET /api/system-instructions/variables` returns each variable's candidate `values`
  plus a `valuesKind` saying how far to trust them — `closed` (a session's value is ALWAYS one of
  these, e.g. `origin`, so a template may branch exhaustively) vs `known` (what this deployment
  happens to have installed/observed: tools, models, projects, capabilities — real, but NOT
  exhaustive). **Render the two differently or the reference lies.** A short list renders as inline
  chips; past `INLINE_VALUE_LIMIT` (8) it collapses to a count opening a `<Popover>` over cmdk's
  `<Command>`. **A hover `<Tooltip>` was rejected on accessibility:** a scrollable, selectable list
  of hundreds of tool ids has no keyboard or touch path in through a tooltip. Every value is a
  `<button>` dropping the bare string at the CodeMirror cursor — deliberately dumb, with no attempt
  to guess quoting. ⚠️ **The preview tabs ARE the `surface` variable's `values`** — never hardcode a
  copy of one of these lists in a component; title-case unknown ids rather than dropping them. The
  same payload builds the editor's autocomplete source.
- `getConfig`/`patchConfig` return `{config, secrets}` — every consumer (incl. `useWebIdeEnabled`)
  must read `.config`, not the top level.

## Auto-select projects — a session that moves under you

A session can run in **auto-select**: it starts in a temporary directory and the AGENT chooses the
project, switching again later if the task spans several. Everything below follows from one fact —
the project is no longer a fixed property of a session, so any surface that read it once has to
keep reading it.

- **`"auto"` is a project KEY, a reserved sentinel sibling to `managed:<uuid>` — not an absence.**
  Spelled ONCE as `AUTO_PROJECT` in `utils/projectLabel.ts` (mirroring core's
  `project_catalog.AUTO_PROJECT`). Selecting it sends `{project: "auto"}`; **"Temporary directory"
  OMITS the key entirely, and the two are different requests** — a plain temp dir versus a temp dir
  the agent is expected to move out of. `ProjectLabel.resolve` names it (`AUTO_PROJECT_LABEL`) so
  no surface prints the bare token.
- **The sentinel resolves to no directory, so every project-SCOPED lookup must treat it as
  unscoped.** `InputBar`'s `scopedProject` is that ONE guard, feeding
  `useMcpTools`/`useSkills`/`useProjectFiles`/`useProjectGit`. Backend `ProjectCatalog.resolve`
  refuses the sentinel by design, and the failure of passing it through would be QUIET — an empty
  tool list reads as "this project has no MCP tools", not as an error.
- **⚠️ The Auto row survives `projectLocked`; "Temporary directory" does not — and the asymmetry is
  deliberate.** What disqualifies Temporary is a WIRE SHAPE, not a policy: its pick is `null` and
  the rebind route (`PUT .../project`) has no representation for that. `auto` is non-empty and the
  same route takes it. Hiding it anyway would leave a session created in auto mode, whose agent has
  since switched into a concrete project, with every project pickable EXCEPT the one that restores
  the behaviour it was created for. Whether a bound session may go back to auto is the SERVER's
  call; it refuses inline via `projectRebindError`.
- **`SessionHeader` takes the LIVE context, not `session.context`.** The summary's context names
  where the session STARTED; `SessionDetailView` threads `effectiveContext` (`getLastContext`) so
  the header's project segment, repo link and IDE capsule name where it IS.
- **⚠️ A switch is durable as an ordinary `context` event, and `getLastContext` returns the most
  recent payload VERBATIM, never merged.** So the switch must be written as the MERGED context, and
  `tool_use_loop.rebind_workspace` does exactly that. A two-key `{project, cwd}` patch would not
  merely mislabel the verbatim readers, it would BLANK them: `model`, `fallback_models`,
  `mcp_tools`, `skill` and `app_id` all vanish, taking the model pill and the recovery model default
  with them. `timeline.test.ts` pins both halves from this side.
- **`role: "project_switch"` is a below-the-gate timeline role**, parsed from a `tool_result` whose
  `tool_id` is `switch_project`. `parseProjectSwitch` (`utils/logs.ts`) is the SINGLE parse seam,
  consumed by `buildTimeline` AND `buildLogs`, so the conversation card and the trace row cannot
  disagree. It does NOT divert the event. A REFUSED switch parses to null and falls through to the
  generic tool row, because nothing moved. **How that refusal is caught is worth knowing, because
  the obvious way does not work:** the tool's error envelope is a Python `str(dict)` repr, NOT
  `json.dumps` (load-bearing — the loop's recognizer is `ast.literal_eval`), so it does not
  `JSON.parse` and no `error` key can be read out of it. What catches it is requiring `project` from
  the RESULT and deliberately NOT falling back to `tool_input.project`: the input carries the
  requested key whether or not the switch succeeded. Not upserted per turn — a task spanning
  projects must show every move.
  - **⚠️ The parity gate is ONE-DIRECTIONAL, and this is the role that proved it.** The Python guard
    is `declared - covered == set()` — it catches a role the PYTHON union declares with no corpus
    case. A role added on the TS side alone is in NEITHER set, so it is structurally invisible: no
    assertion on either side can fail. Adding `project_switch` required teaching
    `mewbo_core.transcript_timeline.TranscriptTimeline` and adding a corpus case as a deliberate
    act. **The corpus compares only `id`/`role`/`turnId`/`content`/`ts`** — parsed meta is held by
    review, not by the gate. On this side, green is not evidence.

## Retry / branch / fork — the turn recovery actions

Three actions live in the assistant-turn `⋯` menu and look interchangeable — they are not. The
label and the wire body must stay in lockstep, and the vocabulary is shared **verbatim** with the
Aura Android client's long-press sheet, so renaming any of these three strings is a cross-surface
break.

- **"Retry from here"** → `POST /api/sessions/<id>/recover {action:"retry", from_ts}`. SAME session;
  the server DESTRUCTIVELY TRUNCATES the transcript at `from_ts` before re-running. A rewind, not a
  redo.
- **"Branch in new chat"** → `POST /api/sessions/<id>/fork {from_ts}`. NEW session, transcript
  copied up to that message; the source is untouched.
- **"Fork session"** → `POST /api/sessions/<id>/fork {}` (no `from_ts`). NEW session, WHOLE
  transcript copied.

- **`ts` IS the message id.** There is no event-id field anywhere in this wire contract — the event
  timestamp (`turn.events[0].ts`) is the only addressable anchor for both `from_ts` params.
- **A 201 that never navigates is a silent no-op.** Both fork paths
  (`SessionDetailView.handleFork(fromTs?)` → `useForkSession`) MUST call
  `onSelectSession(result.session_id)` on success — navigating to the new session is the feature.
  `useForkSession` `toast.error(...)`s on failure and is the ONE fork call path for both menu items.

### Session-level recovery — `RunFailedCard` and the `run_failed` role

The turn-level actions above are per-message rewinds. Session-level Retry/Continue (`POST /recover
{action}`, no `from_ts`) is a different affordance and lives on the failure card.

- **Recovery lives in the COLLAPSED header; the body is bounded and collapsed by default.**
  Retrying must never require expanding a wall of provider markup first. The body is
  `max-h-[300px]` and scrolls on the code-surface tokens. Every control in the header must
  `stopPropagation()`, because `LogEventCard`'s root toggles expansion on click.
- **`run_failed` is a real `TimelineEntry` role, not an assistant bubble.** A real role renders
  chronologically, which matters because one session can hold several failures.
- **Recovery is offered on the LATEST failure only, and "latest" is resolved by scanning back to
  the first CLOSING entry — not by taking the last failure in the list.** A failure followed by a
  successful turn must offer nothing, because the session already recovered. `SessionDetailView`
  scans for the newest `run_failed` and bails on an `assistant` entry; `LogsView` does the
  equivalent over `logs`, stopping at the newest `completion`.
- **The header recovery strip narrows to the one case no card can cover**: `session.recoverable`
  with NO completion event at all — the process-killed case. When a live failure card is on screen
  it defers.
- **Recovery carries a deliberately chosen model, and every recover call passes one.** Omitting it
  lets the server fall back to config policy, so a recovery would silently run on a DIFFERENT model
  than the turn it recovers. Header + row affordances pass the session's own model. The failure
  card mounts `ModelPicker` (compact) and DEFAULTS it: to the next untried rung of `[primary,
  ...ladder]` when `failure.failureReason` is model-attributable
  (`timeout`/`rate_limit`/`server_error`/`bad_gateway`/`connection`/`quota_exhausted`/`auth`/
  `no_deployments`/`invalid_model`), and to the SAME model otherwise — a deterministic failure
  (`bad_request`/`content_policy`/`permission_denied`) reproduces everywhere, so switching there
  hides a real defect. Always a DEFAULT the user overrides. `models_tried` renders as an
  always-visible `belowHeader` strip so the choice is informed.
- **`parseRunFailure` (`utils/logs.ts`) is the single parse seam**, consumed by both `buildLogs` and
  `buildTimeline`. It treats the payload as untrusted: an unrecognised `kind` coerces to
  `"unknown"`, and a `detail_chars` smaller than the string it describes is rejected so the
  truncation footer can never state a bogus size. `error_detail` is absent on every event stored
  before it shipped and the session page replays FULL history, so the parser falls back to
  `error`/`last_error` and omits the title/provider/truncation chips — never gate the card on the
  new key.
- **What counts as a failure is broader than `done_reason ∈ {error, max_steps_reached}`; getting it
  wrong launders a failure into a green success card.** `unmet_goal`,
  `verification_failed` and `halted_no_progress` all mean "ended without achieving the goal" and map
  to the `unmet_goal` reason. Worse, a **blocked** run — one that hit a wall it can't clear
  (repository access, a network path, a permission, quota) — deliberately keeps `done_reason:
  "completed"` and carries the fact out ONLY as a top-level `blocked_code`. `parseRunFailure`
  therefore consults `blocked_code` independently of `done_reason` and lets it win when both are
  present. Recognition here is what makes the whole failure path reachable at all; `StatusBadge` is
  the only other place that must know this vocabulary (`types.ts` keeps `status`/`done_reason` open
  strings on purpose so a new backend status renders a badge rather than failing the build).
- **The THIRD unmet path lives in a SEPARATE event, and `parseRunFailure` structurally cannot see
  it.** A session-end hook leaves the completion's `done_reason` at `completed` and appends a
  distinct `outcome_assertion` event (`{reason, detail, source}`) right after it. So the association
  is the BUILDER's job, not the parser's: `parseOutcomeAssertion` (`utils/logs.ts`) turns the event
  into an `unmet_goal` `RunFailureMeta`, and BOTH builders attach it to the preceding completion —
  `buildLogs` walks back to the most recent completion row, `buildTimeline` keeps a one-shot
  `lastCompletion {index, synthetic}` pointer set in both its completion seams. **`synthetic` is
  captured at the source, never re-derived, because the two seams mint different placeholder texts**
  (`(Run …)` capital-R via the orchestrator's closure vs the builder's own lowercase `(run ended)`)
  and only the first matches `SYNTHETIC_CLOSURE`. A placeholder is replaced in place; a REAL answer
  that missed its goal is preserved with the verdict pushed BESIDE it. **Known residual:** the
  Python `TranscriptTimeline` does not consume `outcome_assertion`, so an MCP consumer still sees a
  clean completion for this case.

## Triggers + terminated sessions — `components/triggers/`

Reverse-invocation wakes (`time.at`/`time.cron`/`ci.workflow`/`forge.pr`/`webhook`) and permanent
session termination. `api/triggers.ts` mirrors the `api/git.ts` seam — typed DTOs over the shared
`httpBase` quartet, no third fetch wrapper — and every closed union
(`TriggerKind`/`TriggerStatus`/`TriggerAction`) is exhaustively mapped in
`components/triggers/triggerFormat.ts` so `tsc` flags drift the moment the contract grows.

- **Naming collision — never conflate these two "stop"s.** The composer's **Stop** button
  (`InputComposerBody.StopWithConfirm`) sends the `/terminate` *slash command* through
  `useSessionQuery.stop()` — it stops the **RUN**, the session lives on. The session-menu
  **"Terminate session"** item calls `POST /api/sessions/{id}/terminate` — it permanently kills the
  **SESSION**. A reviewer seeing "terminate" in a diff must check which one.
- **`isTerminated` is a three-signal OR, and all three must stay in lockstep with the wire
  strings.** `SessionDetailView.tsx` computes it from `session.status === "terminated"` OR a
  `session_terminated` transcript event OR `queryTerminated` (a 410 caught by `useSessionQuery`'s
  send/stop mutations). Any surface reading only one signal is deliberately narrower — don't hoist
  it without checking why it was narrow.
- **410 handling has one shape.** `api/triggers.ts:readMutation` maps a 410 body
  `{error:{code:"session_terminated"}}` to the typed `SessionTerminatedError`;
  `isSessionTerminatedError()` also parses the stringified-JSON message that `httpBase`-routed
  mutations produce, so both fetch paths trip the same guard. Callers catch it and flip to the calm
  terminated state — never surface it via the red `error` string. `InputBar`'s `terminated` prop
  replaces the *entire* composer with a static "permanently terminated" row (no Stop, no input).
- **The open-turn gate is the timeline's real seam, and the Python port trails it.**
  `utils/timeline.ts:buildTimeline` walks events carrying a `currentTurnId`; every role folded after
  its `if (!currentTurnId) continue` gate is silently DROPPED when no turn is open. Roles that
  legitimately arrive BETWEEN turns are therefore parsed ABOVE the gate:
  `trigger_armed`/`trigger_fired`, `session_terminated`, `recovery`, and the failure `completion`
  that upgrades an already-closed turn. Roles handled BELOW it — `plan`, `todos`, `widget`,
  `question` — are mid-run by nature and are *meant* to be dropped when no turn is open. A turn
  still open when the next `user` event arrives is flushed as a `run_failed` entry with reason
  `"interrupted"` rather than discarded with its body; the trailing open turn is deliberately exempt
  so the live `PendingAssistantRow`/`StreamingAssistantRow` keep rendering.
  - **`mewbo_core.transcript_timeline.TranscriptTimeline` is the canonical Python assembler**, and
    `apps/mewbo_mcp/src/mewbo_mcp/timeline.py` is a thin delegate over it. **`buildTimeline` here is
    still a separate implementation, and that is the drift risk.**
    `tests/fixtures/transcript_timeline_corpus.json` is the shared contract both sides read off
    disk — replayed by `tests/test_transcript_timeline_parity.py` AND
    `src/__tests__/timelineParity.test.ts`. Never copy it (two fixtures drift as easily as two
    implementations), and it is written from the documented rules rather than dumped from either
    side, because an expectation generated from the code under test only proves the code equals
    itself. Adding a role here means adding a corpus case and teaching the canonical assembler in
    the same change.
- **A failure completion may only blank a closure that matches the synthetic shape.** `done_reason`
  decides *whether* a run failed, but whether the already-closed turn's bubble is disposable is
  decided by matching its text against the orchestrator's `(Run …)` closure shape. The API's boot
  sweep appends its own terminal completion to an orphaned run, so a session that died between a
  REAL assistant event and its completion gets a failure landing on genuine prose; blanking it
  destroys the only copy of the answer.
- **The dashboard is a Settings pane, not a page** (`settings/panes/TriggersPane.tsx`, Automation
  facet), directly above the schema-driven "Triggers" section holding the backend policy ceilings.
  Its help copy is the reference implementation of the copy standard: the visible line says what a
  trigger *is*, and the popover teaches reverse invocation, the five kinds in plain language, that
  the AGENT arms them from inside a session, and where the ceilings live.
- **Filtering is client-side over one `limit=200` fetch** — a trigger dashboard is small enough that
  local filtering beats a refetch per toggle, and it lets the dropdowns offer only values actually
  present. The server's `?kind`/`status`/`session_id` params are the documented escalation seam once
  volume outgrows one page.
- **Optimistic mutations patch every cached `['triggers']` list, not just the active query.**
  `patchTriggerStatus` walks `qc.getQueriesData({queryKey: TRIGGERS_ROOT})` and rewrites the
  matching row in each, with a snapshot for rollback and an `invalidateQueries` on settle. A new
  trigger-reading query inherits this for free.

## Stlite widget rendering — Streamlit inside the React shell

`StliteWidgetPanel` embeds a full Streamlit app via `@stlite/react` + Pyodide. It shares nothing
with Streamlit's HTML shell, so several Streamlit/stlite defaults leak through and fight the chat
layout.

- **`.stApp` is `position: absolute; inset: 0`** — it fills its NEAREST positioned ancestor. Putting
  `position: relative` on the outer wrapper (the one holding both the chrome title bar AND the
  widget area) makes `.stApp` fill the whole card and paint **over** the chrome. **Invariant:**
  `relative` goes only on the inner widget-area div BELOW the chrome. The failure is invisible to
  every other check — JSX, colours and CSS selectors all compile and grep correctly while the chrome
  is simply painted over. Verify via Playwright: `.stApp` rect vs title bar rect.
- **Two nested scrollbars by default.** Streamlit's `<section data-testid="stMain">` ships
  `overflow: auto` on both axes, AND `.stAppViewContainer` also scrolls; users scroll the inner one,
  hit its end, and never realize the outer has more. Kill stMain's overflow with
  `[&_[data-testid='stMain']]:!overflow-visible`.
- **`document.title` hijack during Pyodide boot.** When a widget's `app.py` doesn't call
  `st.set_page_config(page_title=...)`, Streamlit forces `document.title = "Streamlit"`. The
  `useTitleGuard()` MutationObserver reverts ONLY that literal string; legitimate title updates flow
  through. Don't blanket-freeze the title.
- **`.stMainBlockContainer` has a ~736px max-width cap** (`layout="centered"`). Override
  `!max-w-none` + `!w-full` + `!px-4` + `!pt-4` + `!pb-4` at the panel level — widget authors should
  not have to know about the console's layout.
- **Kernel options are init-only.** `@stlite/react` reads `kernelOptions` once on mount and exposes
  no `setConfig`/`setTheme`. Runtime theme or config changes require a full remount via
  `key={theme}`.
- **Natural scale — `zoom` and `transform: scale` on the stlite subtree are banned.** Fractional CSS
  zoom lays glyphs out at fractional pixel positions — an effective 1.7× raster at devicePixelRatio 2 — producing irregular
  intra-word letter spacing and sub-pixel border-AA jitter in screenshot captures. Size containment
  is `WidgetCard`'s job alone.
- **Fonts — stlite ships Source Sans Pro, not Inter, and that's the point.** `theme.font: "sans
  serif"` maps to Streamlit's bundled Source Sans Pro. An embedded Streamlit app is SUPPOSED to look
  like Streamlit; a same-family-but-mis-metric font reads as "something is off". **Don't try to
  CSS-override the font.** An ancestor rule marked `!important` does NOT beat a descendant's own
  explicit `font-family`, so a `<style>` forcing Inter onto `.stApp` is mostly inert (only `code`
  chips flip, which is an infidelity, not a fix), and the font files live inside the Pyodide wheel
  where no runtime hook reaches them.
- **Card height = content + chrome — measure both.** `StliteWidgetPanel`'s chrome title bar (~30px,
  tagged `data-widget-chrome`) sits INSIDE the card, above the widget viewport, so sizing to
  `.stMainBlockContainer` `scrollHeight` alone leaves the viewport ~24px short of its own content —
  permanently clipping the bottom padding. Card height = content `scrollHeight` + chrome
  `offsetHeight` + buffer, clamped `clamp(120px, content+chrome+8px, min(600px, 70vh))`. The
  portalled wrapper mirrors the placeholder's rect via ResizeObserver → rAF → state, so anything
  freezing rAF (Playwright's paused clock) freezes the mirror mid-flight — a capture flow must let
  widget geometry quiesce BEFORE pausing the clock (`demo/CLAUDE.md`).

### `streamlitConfig` dotted keys that matter

Passed through `useKernel` into Streamlit's `load_config_options`:

- `client.toolbarMode: "viewer"` — hides Deploy / hamburger / Rerun band
- `ui.hideTopBar: true` — removes the "Running…" status widget + toolbar actions (it does NOT hide
  the hamburger; `toolbarMode` covers that half)
- `theme.base: "dark"|"light"` — flipped via `key={theme}` remount
- `theme.{background,secondaryBackground,text}Color` — matched to `--widget-panel-bg` / `--muted` /
  `--foreground`; `theme.primaryColor: "#D97757"` brand clay
- `theme.baseRadius: "6px"` + `theme.baseFontSize: 14` — the compact-density levers. Radius is
  deliberately a **px string, not rem**: rem radii compute against the PAGE root font, not
  Streamlit's, so they drift the moment either side changes its base size. `theme.buttonRadius` was
  removed on purpose — `"full"` made every button a pill. Streamlit applies `baseFontSize` at
  `.stApp` (not the document root), so probing `document.documentElement` font-size to verify it is
  a false negative.

Belt-and-braces chrome hiding on top of `toolbarMode`:
`[&_[data-testid='stHeader']]:!hidden`, `[&_[data-testid='stToolbar']]:!hidden`,
`[&_[data-testid='stDecoration']]:!hidden`.

### Compact density — shrink the `rem` BASIS

Streamlit's **entire** design system — heading sizes, control heights, paddings, gaps, icon boxes,
metric values — is authored in `rem`, and `rem` resolves against the PAGE's `<html>`, which
`baseFontSize` never touches. So an embedded app renders at full-desktop-Streamlit scale
regardless: 16px root ⇒ `st.title()` at `2.75rem` = 44px, 40px selectboxes, 32px metric values,
inside console chrome built on a 13–14px scale.

`COMPACT_DENSITY_CSS` in `stliteBoot.ts` is injected as a plain
`st.markdown(unsafe_allow_html=True)` `<style>` by the wrapper entrypoint, in **two layers, both
load-bearing**:

1. **`html { font-size: 12.5px }`** — shrinks the basis so the whole rem-derived system comes down
   together. 78% of the 16px default, chosen as the largest reduction keeping touch targets usable:
   it lands controls at 31px against the console's own 32px `Button`.
2. **Absolute px for READING text** (body prose, widget labels, inputs, tab labels → 13px) — layer 1
   would otherwise drag prose to ~11px. **Chrome scales; text a human reads does not.** Headings are
   named explicitly (20/17/15px): even at a 12.5px basis Streamlit's `2.75rem` default is still
   34px, and an embedded app's title must not outrank a console pane title.

⚠️ **Two rem values that multiply are the trap the basis shrink creates, and the sidebar is where it
bites.** facade zeroes every margin and padding it can reach and re-expresses ALL separation as one
flex `gap` — `0.125rem` on `[data-testid="stSidebar"] div[data-testid="stVerticalBlock"]`. That is
2px against the 16px root it was authored for; at our 12.5px basis it computes to **1.56px**, and
because facade removed the margins there is nothing else holding the panel apart. Measured on a real
app's filter sidebar: a section label renders as a 5px-tall box 1.56px above the 55px control it
names, so the label reads as colliding with its widget and the whole sidebar reads as broken rather
than dense. `APP_SIDEBAR_GAP_PX` (8px, Streamlit's own 8.8px rhythm on the console's 4px grid)
restores it in absolute px, which is the only spelling that survives a basis change — raising the
basis instead would give the space back by undoing the density win. **When a vendor's rem spacing
must survive the basis shrink, pin it in px**, the same rule "layer 2" already applies to reading
text.

⚠️ **The wrapper's CSS reaches the ENTRYPOINT PAGE ONLY, so a `pages/` multipage app is themed on
exactly one of its pages.** Streamlit's classic MPA runs `pages/<name>.py` on navigation and does
NOT re-run the entrypoint, so facade's stylesheet, `COMPACT_DENSITY_CSS` and `SIDEBAR_NAV_CSS` all
vanish the moment the user leaves the root page. Measured live on the same app: root reports
`html` font-size 12.5px and a sidebar gap of 1.56px, a subpage reports 16px and 8.8px, with zero
injected styles present. This is why a spacing defect caused by the injected theme presents as "the
root page looks wrong and every other page looks fine" — the pages that look right are the ones the
theme never reached. **Read a per-page difference as an injection-SCOPE question first**, and never
conclude from "it self-heals on navigation" that the first paint lost a race. Making the theme
consistent across pages is unsolved: wrapping each page file the way the entrypoint is wrapped would
also expose the relocated sibling to Streamlit's page scanner, whose `PAGE_FILENAME_REGEX`
(`([0-9]*)[_ -]*(.*)\.py`) STRIPS a leading underscore rather than skipping the file, so every page
would appear twice in the nav.

⚠️ **A per-element override list is NOT a substitute for the basis change.** An `h1`-`h6` +
container-padding list measures a 41.8% smaller `h1` and still reads as *completely unchanged* on a
running deployment, because every control, gap and metric it does not name stays full-size. **When a
framework derives its whole scale from one root value, fix the root; naming elements only ever fixes
the ones you thought to name.** Verify against a full inventory (controls, metrics, gaps, total page
height) at a REAL desktop width. Target, measured at 1920px on the deployed artifact: content height
9371→7431px (-21%), h1 44→20px, metric 32→25px, selectbox/tabs 40→31px, body held at 13px.

Never `zoom`/`transform: scale`. Changing the `rem` basis is a genuine layout recalculation — values
re-derive at fresh px sizes and render crisply, which is exactly what `zoom` cannot do.

### Platform theming — streamlit-facade injected at the boot seam

Every widget/app gets the console's shadcn look via
[streamlit-facade](https://github.com/itsdaniyalm/streamlit-facade) (pure-Python token system;
version PINNED in `stliteBoot.ts` and vendored in `fetch-pyodide.mjs` — bump both together).
`kernelOptionsFor` appends the pinned requirement, injects a wrapper entrypoint (facade
`theme.apply(...)` with the hand-synced `FACADE_THEME` tokens, then `runpy.run_path(<real
entrypoint>)` so authored files stay byte-pristine and tracebacks honest), and pre-seeds
`.streamlit/config.toml`. **The wrapper takes over the AUTHORED entrypoint's path** and the authored
source is relocated verbatim to a private `_mewbo_app_`-prefixed sibling — Streamlit derives the
multipage sidebar's main-page label from the entrypoint FILENAME, so a fixed `_mewbo_main.py`
surfaced to users as "Mewbo Main". Sibling, not a root-level name, because sibling-module imports
resolve relative to the file's directory.

- **`theme.apply` must run in the entrypoint script, never an import-once module.** Streamlit
  re-executes the entrypoint each rerun but caches imports; facade's `apply` calls `st.rerun()`
  whenever `.streamlit/config.toml` content differs, and inside a cached import that aborts the
  first run and then never injects CSS again.
- **The seeded `.streamlit/config.toml` is a byte-lock against facade's `_write_config` output.**
  Byte-identical content → no `st.rerun()` at boot. If a facade upgrade changes its emitted format,
  the failure mode is one extra boot rerun; regenerate the seed when bumping the pin.
- **facade's font tokens are pointed at `"Source Sans Pro"`/`"Source Code Pro"` deliberately.**
  Unlike an ancestor-rule override, facade applies `font-family: var(--font-sans) !important` on
  DESCENDANT selectors, which genuinely beat Streamlit's stylesheet — left at its `system-ui`
  default it WOULD flip the fonts. Never pass `font_link` (external Google Fonts fetch = offline
  break).
- **`[data-testid="stSidebarNav"]` (the auto-generated multipage page-link list) is the one sidebar
  element facade's own CSS never targets, and it doesn't get its color from an injected `<style>`
  at all — it paints from Streamlit's OWN theme state at first mount, ahead of `.streamlit/
  config.toml` being honored on a cold kernel boot.** Verified live: landing on an app's root page
  shows the page-link list in Streamlit's stock light theme (white box, black text) above a
  correctly dark-themed filter panel; navigating to any other page force-remounts the chrome
  against the by-then-correct theme and the mismatch silently self-heals, masking the defect on
  every page except the first one a session ever lands on. `SIDEBAR_NAV_CSS` in `stliteBoot.ts`
  sidesteps the race the same way `COMPACT_DENSITY_CSS` does — a plain `st.markdown
  (unsafe_allow_html=True)` rule edits the DOM directly instead of waiting on Streamlit's theme
  engine, reusing facade's own `--chrome-*` custom properties and its `rgba(255,255,255,0.08/0.12)`
  hover/active idiom so the nav list reads as one system with the rest of the sidebar.
- **Styles born inside a widget stay inside the widget.** stlite mounts Streamlit into the host
  document — no iframe — so a `<style>` from `st.markdown(unsafe_allow_html=True)` applies
  DOCUMENT-WIDE. facade's CSS uses `body { … !important }` / `:root` globals that repainted the
  entire console. `stlitePanel.ts`'s `useStliteStyleScope` watches the panel subtree and rewrites
  every injected style via CSSOM: `body`/`html`/`:root` selector heads become the
  `.mewbo-stlite-scope` container, everything else is prefixed. The standalone `widget-host.html`
  page is exempt BY DESIGN — there the page IS the widget.

### Self-hosted Pyodide runtime — `scripts/fetch-pyodide.mjs` + `public/pyodide/`

stlite's worker hard-defaults `pyodideUrl` to a jsDelivr CDN URL, and the npm `@stlite/*` packages
bundle ONLY the streamlit + stlite-lib wheels — every *other* boot wheel (micropip, numpy, pandas,
pillow, altair's whole subtree, starlette, …) is fetched from that CDN at runtime, so widgets
silently fail to boot on any offline / LAN-only deploy. `fetch-pyodide.mjs` materializes
`public/pyodide/` (Vite copies it to `dist/pyodide/`) and both surfaces pass `pyodideUrl` → the
directory of that URL becomes Pyodide's `indexURL`, so the vendored `pyodide-lock.json` + wheels
resolve locally. Verified: a widget boots from a deep foreign subpath with **zero** requests off
`127.0.0.1`.

- **Boot wheel set is computed, not guessed.** micropip installs streamlit's mandatory
  `Requires-Dist` (read from the bundled wheel's METADATA — the `BOOT_ROOTS` list) + `stlite_lib`'s
  dep + a worker-forced `protobuf>=7.34.1,<8`, resolved against the lockfile, then the transitive
  closure over the lockfile's `depends` edges. `altair` is a MANDATORY streamlit dep, so its whole
  subtree is fetched at *install* time even though Python imports it lazily. `pyarrow` is
  `micropip.add_mock_package`'d by the worker — do NOT vendor it.
- **Five packages can't come from the lockfile → vendored from PyPI with INJECTED lockfile entries**
  (`PYPI_WHEELS`): `blinker`/`itsdangerous`/`python-multipart`/`tenacity` aren't in the pyodide
  distribution at all, and the lockfile's `protobuf` is older than the forced pin. All five are
  pure-python `-none-any` leaves; the script pins them + sha256 and writes synthetic lockfile
  entries.
- **Trap: key the trimmed lockfile by the ORIGINAL lockfile key, never `entry.name`.** pyodide keys
  packages by PEP 503 normalized name (`pillow`, `markupsafe`); the `name` field is display-cased
  (`Pillow`, `MarkupSafe`). micropip looks up the normalized key, so keying by `name` makes it fall
  through to PyPI for exactly the capitalized-display packages. numpy/pandas hide the bug (name ==
  key).
- **Trap: `micropip` itself must be in the trimmed lockfile.** The worker loads it via
  `loadPackage("micropip")` BEFORE the streamlit install, and nothing lists it as a dependency, so
  it isn't in the computed closure — add it to `BOOT_ROOTS` explicitly.
- **Known limitation (documented, not a bug):** a package a widget names in `requirements` that we
  didn't vendor falls through to micropip/PyPI — works online, fails offline. This is why the
  lockfile is TRIMMED: an un-vendored name is simply absent, routing cleanly to PyPI rather than
  404-ing against a local `indexURL`. We do NOT run a PyPI mirror.
- **Wiring:** `build` runs the fetch first (idempotent via a version+pins fingerprint stamp);
  `postinstall` runs it lenient (`|| true`) for dev; Docker's `npm ci --ignore-scripts` skips
  postinstall so `build` covers it. `public/pyodide/` is gitignored (~26 MB). nginx caches
  `^(/widget-host)?/pyodide/` for a day; the SW glob excludes `.whl/.wasm/.zip/.mjs` and
  `vite.config.ts` `globIgnores`s `**/pyodide/**` wholesale, since `pyodide.asm.js` is a `.js` file
  that would otherwise get precached.
- **`CORE_SHA256` pins the core-runtime + lockfile fetch itself.** Every wheel verifies against
  `p.sha256`/`w.sha256`, but those come FROM the lockfile — an unverified lockfile fetch was the one
  unpinned link. **Trap: `pyodide-lock.json`'s pin is NOT `sha256sum public/pyodide/
  pyodide-lock.json`** — that file on disk is already trimmed/rewritten, so its bytes never match
  the raw CDN response. Pin it from the RAW fetch. `CORE_SHA256` is folded into `FINGERPRINT` so a
  pin bump forces re-materialization.
- **`public` is in `.eslintrc.cjs`'s `ignorePatterns`** (alongside `dist`, `dist-widget-host`,
  `coverage`, `test-results`): it is Vite's verbatim-copy directory, so nothing in it is authored by
  us. Without that, a materialized `public/pyodide/` reported ~330 errors and ~320 warnings from a
  vendored file that is in no diff, making `--max-warnings=0` permanently unsatisfiable.
  `npm run lint:ci` is expected to exit **0 with no output** — treat any output as caused by your
  diff, and do not scope eslint at individual files to dodge noise.

### `widget-host.html` — standalone host for non-React embedders

A framework-free page (`src/widget/host.ts` + `@stlite/browser`'s `mount()`) for embedders that only
have a browser context and a `postMessage` channel — chiefly the Aura Android WebView. Theme +
kernel-option logic is shared with `StliteWidgetPanel` via `src/widget/stliteBoot.ts`
(`buildKernelOptions`, React-free); the only per-surface inputs are `wheelUrls` and `pyodideUrl`.

**postMessage contract:** on boot the page posts `{type:"mewbo-widget-host-ready"}`; the embedder
posts a `message` whose `data` is `{type:"mewbo-widget-payload", payload: WidgetReadyPayload,
theme?:"light"|"dark"}`. `host.ts` validates the shape defensively (a WebView shares one `message`
bus) and ignores anything else silently. Re-posting a payload unmounts the running kernel and
remounts (kernel options are init-only).

**Relocatable base — the load-bearing constraint.** The Android WebView serves this page from a
synthetic origin at a mount path we don't control, so EVERY asset must resolve relative to the page.
It is therefore a **separate Vite build** (`vite.widget-host.config.ts`, `base: './'`, output
`dist/widget-host/`) — NOT a second `rollupOptions.input`. The console's build MUST stay `base: '/'`
(its PWA precache + SPA sub-route hard-loads depend on absolute asset URLs), so flipping the global
base for one entry would endanger that machinery. Two physical pyodide copies in the web dist is the
tradeoff for Android self-containment.

Two build traps specific to the relative-base build:

- **Wheels must keep their exact PEP 427 filename.** Vite's default `-[hash]` suffix turns
  `streamlit-1.57.0-cp313-none-any.whl` into `…-none-any-CQR0LZsx.whl`, which micropip can't parse
  ("Invalid build number: cp313"). `assetFileNames` keeps `.whl` names unhashed (already
  version-stamped, so still cache-safe).
- **One `@stlite/browser` wasm needs a manual copy.** Its kernel references `new
  URL("./assets/<hash>.wasm", "" + import.meta.url)`; the `"" +` defeats Vite's static asset-URL
  detection under relative base, so the file isn't emitted and the literal resolves to
  `assets/assets/<hash>.wasm`. The `copyStliteWasmAssets` plugin copies them there. Fonts in the
  same upstream dir come through `stlite.css` `url(...)` (which Vite DOES rewrite), so only wasm
  needs copying.

## PWA service worker — deploy resilience

Config lives in `vite.config.ts` under `VitePWA({ workbox: { ... } })`; the update UX in
`components/UpdatePrompt.tsx`.

- **`skipWaiting: true` + `clientsClaim: true`.** The opposite config leaves a newly-installed SW
  waiting until every open tab closes, pinning users to the previous deploy's precached
  `index.html` — which references hashed bundle names the server has since deleted, so every lazy
  chunk 404s and widgets silently fail to mount.
- **Large lazy chunks are in `globIgnores`** (`stlite-*.js` — every chunk containing @stlite
  prebuilt modules gets that deterministic prefix via `build.rollupOptions.output.chunkFileNames`)
  because they exceed Workbox's 2 MiB precache limit; they are served network-only. Never
  globIgnore by upstream chunk name (`PlotlyChart-*`, `DeckGlJsonChart-*`) — those names and hashes
  drift with every stlite upgrade and bundler chunking change. This HAS to pair with
  `skipWaiting: true`.
- **`sw.js` must be `Cache-Control: no-cache, no-store, must-revalidate`** at nginx
  (`docker/nginx-console.conf`), or the browser never sees new versions.
- **`UpdatePrompt.handleReload` is deliberately nuclear**: unregisters every SW via
  `navigator.serviceWorker.getRegistrations()` + wipes all `caches.keys()` entries, THEN
  `location.reload()`. Do not trust workbox's `updateServiceWorker(true)` handshake alone.
- **`registerType: "prompt"` + a 15-min `registration.update()` tick**, re-ticking on
  `window.focus`.

| Symptom | Likely cause |
|---|---|
| Widget lazy chunk 404s after deploy | Stale SW pinned to old `index.html`; the Reload prompt was dismissed or never seen |
| Users refresh, still see old UI | Old SW never unregistered — force via Reload (nuclear path) or DevTools → Application → Unregister |
| `curl -sk /index.html` returns fresh, browser runs stale | SW precache serves stale `index.html` before the network layer sees the request |

## Dependency selection

Past the library-first tree above, before adding any dependency:

1. **Read the actual installed version's API** — not blog posts. `node -e "console.log(Object.keys(require('pkg')))"`
   or read `node_modules/pkg/dist/*.d.ts`. Major versions often rename everything.
2. **Check units and defaults** — a numeric prop may mean pixels in one version and percentages in
   another.
3. **Prefer rehype/remark plugins over component-level libraries** — AST-level plugins integrate
   with one prop addition and don't require component overrides.
4. **Verify bundle size claims yourself** — bundlephobia numbers and blog claims are often stale.
5. **Prefer the library the rest of the stack already endorses** (`cmdk`, `sonner`).
6. **One library per concern.** Mixed stacks double the surface area for bugs and onboarding.

| Package | Purpose | Why this one |
|---|---|---|
| `@tanstack/react-query` v5 | Server-state cache, polling, invalidation | Covers caching, TTL and invalidation with no bespoke hooks. Function-form `refetchInterval` handles conditional polling cleanly. |
| `wouter` v3 | Client routing | ~1.5 KB; matches our handful of routes. Rejected react-router for size + complexity. |
| `react-hook-form` + `zod` | Static forms with validation | Tiny runtime; integrates with shadcn `Form`. RJSF stays only for `SettingsView`'s dynamic backend schema. |
| `class-variance-authority` + `tailwind-merge` | Variant components + class dedup | Required by shadcn primitives; `cn()` combines `clsx` + `tailwind-merge`. |
| `react-markdown` + `remark-gfm` | Markdown rendering | Renders to React elements (no `dangerouslySetInnerHTML` / XSS risk). |
| `rehype-highlight` | Syntax highlighting | One-line rehype plugin (~50 KB gz, 37 languages). Rejected: `react-syntax-highlighter` (75-495 KB, not tree-shakeable), Shiki (async init), `prism-react-renderer` (low maintenance). |
| `react-resizable-panels` v4 | Draggable split pane | Zero deps, ~7.7 KB gz, built-in ARIA/keyboard a11y. **v4 API:** exports are `Group`, `Panel`, `Separator` (not `PanelGroup`/`PanelResizeHandle`); prop `orientation` (not `direction`); numeric sizes = **pixels**, use strings for percentages. |
| `react-force-graph-3d` (+ `three`) | 3D WebGL graph (`Graph3DView`) | The one graph engine — wiki Knowledge Graph AND Agentic-Search SCG share it. Replaced a Cytoscape 2D engine whose main-thread force-sim choked past a few thousand nodes (real graphs hit ~17K/68K). The GPU/WebGL build is what clears the 2D-canvas wall. Lazy-loaded at the render site. |
| `@assistant-ui/react` (+ `-markdown`) | Chat-surface primitives | Production-grade vendor for the chat surface. `useExternalStoreRuntime` binds our existing SSE/TanStack state with zero networking opinions. `zustand` rides along as ITS internal dep only. |
| `@uiw/react-codemirror` (+ `@codemirror/lang-markdown`, `-legacy-modes`, `-language`, `-state`, `-view`, `@lezer/highlight`) | Code editor for `SystemInstructionsPane` | CodeMirror 6 is the standard React-ecosystem choice; `@uiw/react-codemirror` owns mount/unmount/state-sync so we don't hand-roll an `EditorView` lifecycle effect. There is no dedicated CM6 Jinja package — `@codemirror/legacy-modes/mode/jinja2` (a standalone `StreamParser`, no backdrop language) is hand-driven via the public `StringStream` API inside a `ViewPlugin` decoration layer on top of `lang-markdown`'s prose grammar. Colors reuse the `--hl-*`/`--code-*` family, so no new theme tokens. |
| `@codemirror/autocomplete` | Variable/value completion in the Jinja editor | It was already a transitive of `basicSetup`; importing it directly makes it a DECLARED dep (an undeclared transitive is a break waiting for a hoist change). The pane turns `basicSetup`'s own `autocompletion` **OFF** and owns the extension, so there is exactly ONE autocompletion config. The source is built from the SAME `/variables` payload that feeds the reference table, so **nothing about the variable set is written down in the console**. It deliberately does NOT parse Jinja: it scans raw text around the cursor and degrades to offering EVERY candidate tagged with its owning variable, which is still useful and can never be wrong. ⚠️ Feed it a **referentially stable** variables array (`useSystemInstructionsVariables` returns one shared `NO_VARIABLES` while in flight) or the extension memo reconfigures the editor every render. ⚠️ CM6 measures character size via `Range.getClientRects()` from a `requestAnimationFrame`, which **jsdom does not implement**, so the throw lands as an UNHANDLED error *after* the test passed (`setupTests.ts` stubs it to empty rects). |

## Tool card components

```
LogEventCard (base: rounded-md + border-l-[2.5px], expand/collapse, icon/title/badge header)
├── Used by: renderPermission, renderAgent, renderAgentResult, renderCompletion, renderShell fallback, renderReflection, SpawnAgentBatchCard
└── Compose this for new tool types unless the visual structure is fundamentally different

TerminalCard (specialized: always-dark bg, window chrome, $ prompt, stdout/stderr)
└── Used by: renderShell when structured shell data is present

CopyButton (shared: copy-to-clipboard with icon-swap feedback)
└── Used by: TerminalCard, MessageBubble, ConversationTimeline
```

Log cards use the right-side instrument-panel language — `rounded-md` bodies with a `2.5 px`
brand-tinted left rail (`border-l-[2.5px]`) and `pl-[14px] pr-3 py-[9px]` head padding. The rail is
the sole signature on the right surface; **don't** add it to non-log entries (turn footers,
spinners, separators). `TerminalCard`'s `10 px` outer radius is a deliberate macOS-window-chrome
exception; keep it.

### A card dispatched on RESULT data cannot render a RUNNING call

The engine emits `tool_call` before dispatching a tool and `tool_result` after it returns (paired by
`tool_call_id`), so `buildLogs` renders a pending row the result settles in place. That collides
with how `renderShell` chooses a component: **it dispatches on `log.shellCommand`, which the settled
path parses out of the RESULT payload.** A pending row therefore had no `shellCommand`, fell through
to the generic `LogEventCard`, and printed the tool arguments through `prettyJsonIfValid` — so a
running `sleep 90` showed a JSON blob for ninety seconds.

The fix is `shellIdentityFromInput(toolId, toolInput)` in `utils/logs.ts`: the ONE derivation of a
shell call's identity from its input, shared by the pending path and by the settled path's
timeout/internal-error fallback. Rules that follow:

- **Ask what a card DISPATCHES on before adding a pending variant.** Shell works because the command
  is an ARGUMENT. `DiffCard` needs the diff text and `FileReadCard` needs the file body — both exist
  only in a result — so neither has an honest pending form. Those keep the generic row, which is
  correct, not a gap.
- **A pending card asserts nothing it cannot know.** `TerminalCard` takes `pending` and gates every
  settled readout on `!== undefined` (exit code, duration, output). A known exit code overrides a
  stale `pending`, so the settle cannot be blocked by a leftover flag.
- **Reuse `.session-cmp-pulse` for the running indicator** — the one sanctioned infinite keyframe.
- **Assert the RENDER, not the parse.** `utils/logs.ts` unit tests can confirm the fields are
  populated while the dispatch still picks the wrong component, and a bundle grep proves only that
  the code compiled. `components/__tests__/LogsViewRunningTool.test.tsx` mounts `LogsView` and
  asserts a terminal card exists AND that no `"command":` JSON is present — the second half is what
  fails when the dispatch regresses.

**Rules when adding new tool card types:**

1. **Compose `LogEventCard` first.** Only specialize when the visual structure is genuinely
   different (TerminalCard's dark background and window chrome).
2. **Never duplicate behavioral components inline** — import `CopyButton`, use the `Badge` function
   in `LogsView`. The raw clipboard helper behind `CopyButton` is `copyText`
   (`src/utils/clipboard.ts`); reach for it only when `CopyButton` genuinely can't render.
3. **Styling goes in `className`, behavior goes in the component.**
4. **Keep tool-specific helpers colocated** (`formatDuration()`, `shortenCwd()` in `TerminalCard`).
   Extract to `utils/` only when a second consumer appears.
5. **Parse structured tool data in `buildLogs()`, not in components.** Components receive clean
   props, never raw JSON.
6. **A payload `kind` with no arm is dead on arrival, and the check runs both directions.** A
   `spawn_agents` fan-out result (`{kind: "agent_batch", ...}`) had no branch in `buildLogs()`, so a
   payload honestly reporting which units were permanently refused fell through to the generic shell
   card and was never shown. Before shipping a new tool-result `kind`, grep `buildLogs()`/
   `LogsView`'s type switch for an arm that handles it; before adding an arm, grep the emitting tool
   for a payload that actually mints that `kind` — a renderer with no producer is exactly as dead as
   a producer with no renderer.

**A status vocabulary mirrored from a Python `Literal` needs a test that pins it, not a comment.**
`utils/agentStatus.ts`'s `StatusKey` union carried five members while `hypervisor.py`'s
`AgentStatus` had six — `rejected` (a permanent admission refusal) was missing, and `statusKey()`
folds anything outside its known set to `submitted`, so a permanently-refused agent rendered as a
muted, non-terminal pill with a live-progress affordance. `agentStatusAlignment.test.ts` hardcodes
the six-member list and fails the moment `agentStatus.ts` drifts — apply this to any TS union
mirroring a Python enum. Relatedly, `renderAgentResult`'s badge is humanized directly from the
status string (`status.replace(...)`) rather than an enumerated ternary with an "else", so a status
this file doesn't special-case reads as itself instead of a fabricated word.

## CSS / component pitfalls

### `font-sans` beats `body { font-family }` — brand stacks live in tailwind.config

Tailwind's default `font-sans` stack is `ui-sans-serif, system-ui, …` — NOT Inter. The app-root
layout div carries `font-sans`, so without `theme.extend.fontFamily` (sans = Inter, mono = JetBrains
Mono) the whole console silently renders in the system font while `index.css`'s `body {
font-family: 'Inter' … }` sits defeated underneath. If a surface renders the wrong font, check for a
`font-sans`/`font-mono` utility resolving to Tailwind defaults before touching CSS.

### Never nest duplicate width constraints

A `max-w-[70%]` inside another `max-w-[70%]` compounds to ~49%. The parent container owns the
constraint; child components fill their parent.

### Markdown `components` map belongs outside the render function

Define `const markdownComponents = { ... }` at module level, not inside a component body, to avoid
recreating the object on every render.

### rehype-highlight + custom `code` component coexistence

`rehype-highlight` adds `hljs`/`language-*` classes to `<code>` inside `<pre>`. The custom `code`
component must detect these and pass through rather than restyling as inline code:

```tsx
code: ({ className, ...props }) => {
  if (className?.startsWith('hljs') || className?.startsWith('language-')) {
    return <code className={className} {...props} />;
  }
  return <code className="inline-code-styles" {...props} />;
}
```

The `pre` component uses `[&_code.hljs]:bg-transparent [&_code.hljs]:p-0` to prevent double
backgrounds.

### `cn()` is tailwind-merge-aware — later classes win

`cn("p-2 text-sm", "p-4")` resolves to `"text-sm p-4"`. If you rely on order to keep an early class,
move the precedence-bearing class to the end.

### ⚠️ `shadow-[var(--elev-N)]` silently renders NO shadow — use `[box-shadow:var(--elev-N)]`

`shadow-` is the prefix for **two** Tailwind utilities (`boxShadow` and `boxShadowColor`), so a bare
`var()` arbitrary value is ambiguous and Tailwind v3 resolves it to the **color** one. Verified
against the installed compiler (3.4.17): `shadow-[var(--elev-1)]` emits `--tw-shadow-color:
var(--elev-1); --tw-shadow: var(--tw-shadow-colored)` and **no `box-shadow` property at all** — and
since preflight defaults `--tw-shadow-colored` to `0 0 #0000`, the result is a fully transparent
non-shadow. No error, no warning, nothing painted.

Two forms work: `[box-shadow:var(--elev-2)]` (arbitrary property, what `composerCard()`,
`composerSurface()` and `ui/card-surface.ts` all use) or the type-hinted
`shadow-[shadow:var(--elev-3)]`. Prefer the first — it reads as what it is.

The tree is swept clean; a bare `shadow-[var(--elev-N)]` reintroduced anywhere is a review reject.
**Don't copy an existing `shadow-[...]` call site as a template without checking which form it
uses.** **Grep trap:** a template literal like `` `shadow-[var(--${tier})]` `` never contains the
literal substring `--elev`, so a grep anchored on `shadow-[var(--elev` misses it — grep the broader
`shadow-[var(` prefix when auditing.

### ⚠️ A key shared by `fontSize` and `colors` collides — `text-input` silently sets colour, not size

Tailwind derives a `text-*` utility from BOTH `theme.extend.fontSize` and `theme.extend.colors` —
one class prefix serving two theme sections. A key present in both compiles to the same utility
twice, with whichever rule is emitted later winning. `colors.input` already existed (shadcn's border
token), so a same-named `fontSize.input` for the 16px iOS floor would have generated `.text-input`
twice; the colour rule wins and `.text-input` sets `color: hsl(var(--input))` — the dim border
colour. Typed text in every composer and every shadcn `<Input>` would have rendered nearly
invisible, with no build error. `cn()` doesn't rescue it: tailwind-merge classifies `text-input` as
a colour utility, so it drops the foreground class, not the size one. The floor class is named
`text-field` instead. **Before adding a key to `fontSize` or any theme section, check whether the
same key already exists under a different section.**

### Theming — never hardcode colors that should adapt

Light and dark mode ride CSS variables in `src/index.css` (`:root` defines dark, `.light` defines
light; the `<html>` element gets the `light` class toggled). Every color in a component must come
from a variable.

**Palette identity: "Anthropic cream (light) / warm carbon (dark) with clay primary", shared with
the docs site** — warm hue-40 carbon, bg `40 6% 8%`, card `40 5% 12%`, whisper-subtle borders, one
clay accent. Three things the docs site does that the console deliberately does not: a cool
`#0d1117` code-block background (our `--code-*` family stays warm carbon), Geist fonts, and a 10px
radius (our shape vocabulary is 8px). `--border` is translucent here (0.15α of `55 9% 74%`) and
resolves to the intended hairline on the carbon bg — don't "fix" it to an opaque value.

**Theme persistence:** the choice persists in localStorage key `mewbo:theme` (dark default; only an
exact `'light'` restores light). Ordering is load-bearing: `toggleTheme` mutates the `<html>` class
**synchronously before** dispatching `wiki:theme-change`, and Mermaid/graph consumers live-read CSS
vars at render time — reorder that and diagrams repaint with the outgoing theme's colors. The
`[theme]`-dep effect in `App.tsx` is an idempotent re-assert for the persisted initial value, not
the primary sync.

**Status semantics have tokens — use them.** `--success` / `--warning` / `--info` / `--destructive`
are GitHub-semantic hues, per-theme tuned. Three carry a `-text` variant (`--destructive-text`,
`--warning-text`, `--info-text`) — the text-on-tint reading for a status WORD sitting on its own
tint fill, tuned to clear AA on that tint. Use the plain token for a dot or fill, the `-text`
variant for a label against the tint. Every run-state/status color MUST come from these four — raw
`emerald-500`/`amber-500`/`red-500` status classes are a review reject now that `STATUS_STYLES`
(`utils/agentStatus.ts`), `BADGE_COLOR_MAP` (`utils/agents.ts`) and `StatusBadge` are token-based.
The sanctioned raw exceptions are **identity hues only**: origin/kind chips that need side-by-side
distinctness (`cyan` search/webhook vs `--info` blue, `violet` structured, `teal` draft, `lime`
PR-open, `stone` idle), the `--agent-N` cycling slots, and platform brand icons.

**The distinguishing test: is the colour gated on run state? Then it is state, not identity.**
`TerminalCard`'s macOS traffic-light dots are gated on `isError`, which makes them a run-state
readout wearing a familiar shape — real traffic lights are a fixed red/amber/green triple regardless
of window state. **The identity signature is the SHAPE** (three `rounded-full` dots at a fixed size
and gap), which is untouched; only the state-driven colour moved onto `--destructive`/`--success`.

| Anti-pattern | Replace with |
|---|---|
| `bg-[hsl(220_5%_12%)]` (literal HSL) | `bg-[hsl(var(--surface))]` or another semantic token |
| `text-white`, `text-white/60` | `text-[hsl(var(--foreground))]` / `text-[hsl(var(--muted-foreground))]` |
| `text-black`, `text-zinc-900` | `text-[hsl(var(--foreground))]` |
| `bg-white`, `bg-gray-100` | `bg-[hsl(var(--background))]` / `bg-[hsl(var(--card))]` |
| `border-white/10`, `border-zinc-800` | `border-[hsl(var(--border))]` or `border-[hsl(var(--code-border))]` |
| `text-emerald-300` for diff additions | `text-[hsl(var(--diff-add-text))]` |
| `bg-red-500/10` for diff deletions | `bg-[hsl(var(--diff-del-bg))]` |
| `border-[hsl(var(--border)/0.5)]` — appending alpha to a token that already has it | plain `border-[hsl(var(--border))]`, or a token with no alpha |

**⚠️ Some tokens already embed their alpha — appending `/NN` produces invalid CSS.** `--border`,
`--border-strong`, `--code-border`, `--rail-border`, `--scrollbar-thumb` and
`--scrollbar-thumb-hover` are declared as `H S% L% / A`, so `hsl(var(--border) / 0.5)` expands to a
four-value `hsl()` with two slashes: the browser drops the declaration and the element falls back to
`currentColor`, which reads as a near-white hairline in dark mode. Worked example:
`.session-ts-rail`'s backplate wrote `box-shadow` off `hsl(var(--border-strong) / 0.55)` — the
double-slash made the whole declaration invalid, so the TurnScroller backplate rendered with no ring
and no shadow in either theme, with nothing in the console pointing at why. Alphaless tokens
(`--destructive`, `--success`, `--primary`, the `--code-fg-*` family) legitimately take a slash.
Check the token's declaration in `index.css` before reaching for one.

**Adding a new theme token (the only correct way to introduce a new color):**

1. Add it under `:root` (dark) AND `.light` — both definitions, even if values are similar. A token
   defined in only one block silently inherits the other from `:root`, producing broken contrast.
2. Use a semantic name (`--code-prompt`, `--card-elevated`), not a visual one (`--green-bright`).
3. Reference it as `bg-[hsl(var(--token))]` / `text-[hsl(var(--token))]`.
4. Where a family already exists (the `--code-*` group, `--diff-add-*` / `--diff-del-*`), add to it
   — don't start a parallel set.

**Code-surface tokens specifically** (`--code-chrome`, `--code-body`, `--code-fg`,
`--code-fg-muted`, `--code-fg-subtle`, `--code-border`, `--code-prompt`, `--code-stderr`, plus the
`--hl-*` syntax set) power TerminalCard, DiffCard, ReviewPane and the `.hljs` block. New
code-display components MUST use them — never hand-pick colors for "the dark code box". TerminalCard
and DiffCard originally shipped with literal `bg-[hsl(220_5%_12%)]` and `text-white/40` so they
would "always look like a terminal"; they stayed dark in light mode and looked broken next to themed
siblings. A literal `hsl(...)` or `text-white` in a PR diff is a reject.

### Scrollbar affordance — `overflow-y: auto` is invisible to many users

macOS, iOS and overlay-configured Windows hide scrollbars by default on `overflow-y: auto`, so users
miss clamped content entirely. When height is bounded and content may overflow, use `overflow-y:
scroll` (track always rendered) paired with themed `::-webkit-scrollbar*` and `scrollbar-color` —
see the `.stAppViewContainer` block in `src/index.css`. Give the track a faint background
(`hsl(var(--muted) / 0.4)`) so the rail itself is visible, not just the thumb.

### Bundle presence ≠ visual render

Grepping the minified bundle proves the code COMPILED. It does not prove the element mounts, isn't
covered by an absolutely-positioned sibling, isn't off-screen, isn't `display: none`-d. For any
"it's not showing" bug, **query the actual DOM and take a screenshot before concluding** —
`mcp__plugin_playwright_playwright__browser_evaluate` + `browser_take_screenshot`. A bundle grep
cannot distinguish "not built" from "built and painted over"; the `.stApp` positioning trap above is
the worked example.

### InputBar session context hydration

In detail mode pass `sessionContext={effectiveContext}` (`SessionDetailView`, via
`utils/timeline.ts:getLastContext`) so project/skill/model/MCP tool selections reflect the session's
stored context. Without it the toolbar defaults to null/global state.

`getLastContext` returns the **single most-recent `context`-type event's payload, verbatim — never
merged across events.** This mirrors the backend's `_load_last_context`, the function `/message`
re-engage and `/recover` actually read to resume a session. Folding payloads forward made a cleared
field "stick": InputBar's per-turn context payload *omits* a falsy field (e.g. `project` cleared back
to Temporary) rather than sending `null`, so a fold-merge kept resurrecting the last truthy value.
Corollary: `runtime.reinject_recovery_context` only re-emits `client_capabilities`/
`structured_workspace` after `/recover`, so a capability-gated session's LATEST context event
post-recovery can be that narrow gating-only payload — `effectiveContext` will faithfully show that
narrowed shape, because that is genuinely what the backend will read next. Don't reintroduce a
merge.

**`activeProject` has exactly ONE writer.** The durable spec (`GET /spec`) is authoritative;
`sessionContext` is the fallback for a session with no typed spec. A second writer in the
session-switch reset effect makes correctness depend on the two effects' DECLARATION ORDER, which
only holds while both dep arrays move together — and they don't: the reset watches
`project·skill·model·mode·mcp_tools·branch·fallback_models`, the spec effect watches
`[sessionId, sessionSpec, sessionContext?.project]`. A `context` event carrying a new `model` while
`project` stays ABSENT moves only the reset's deps, runs it alone, and blanks a spec-bound project
**permanently** — nothing re-fires to heal it. One writer deletes the ordering question rather than
re-tuning it. `sessionId` stays in the deps even though the body never reads it: switching between
two sessions that both resolve to `null` must still clear a local pick carried over.

**"Reset to defaults" must restore, never null, in detail mode.** There is no wire representation of
"clear the project": omitting `project` from the turn payload means INHERIT, not clear
(`session_spec.py:SessionSpecOverrides.from_request_context` — *presence* is what marks a field
declared), and when the binding is locked `handleSubmit` omits it anyway. Only home mode
legitimately resets to none.

### Data ordering must be explicit

Never rely on storage enumeration order for user-facing lists. UUIDs sort randomly; filesystem
`readdir` order is undefined. Sort by `created_at` at the data source — not in the UI layer — so all
consumers get correct order. The backend `session_runtime.list_sessions()` sorts descending by
`created_at`.

## Session list — origins, labels, pinning, filters

The landing page hides internally-spawned sessions by default. Each `SessionSummary` carries an
`origin` (`user|wiki|search|channel|apps|structured|draft|mobile`) computed in **core**
(`session_provenance`, not the FE). `HomeView` filters client-side over the already-fetched list via
a per-origin `DropdownMenu` (default visible = `user`+`channel`); `SessionOriginBadge` (composes the
shared `Badge`) chips every row beside the timestamp.

**`utils/sessionOrigins.ts` is the ONE origin registry — label, colour AND glyph.** `ORIGIN_META` is
an exhaustive `Record<SessionOrigin, {label, filterLabel?, color, icon}>`; `ORIGIN_FILTERS` is
DERIVED from it over an explicit `ORIGIN_ORDER`, and `SessionOriginBadge` reads the same record.
Spell the labels twice and one origin reads "My tasks" in the menu and "Manual" on the row;
`filterLabel` states that difference deliberately, because a filter row names a SET and a chip names
one session. The union stays a **closed mirror of core**: a new origin touches `types.ts` and `ORIGIN_META` — a
missing arm is a TS error. Badge colors come from `BADGE_COLOR_MAP` (`utils/agents.ts`).

**Glyphs: one mark per class, and the four product origins reuse the rail's.**
`user`/`wiki`/`search`/`apps` carry the same lucide icon their NavRail row does
(`nav-rail/products.ts`: `ListChecks`/`BookOpen`/`Search`/`AppWindow`) — a Wiki session and the Wiki
rail row must not teach two different marks. The remaining four are their own (`MessagesSquare`
channel · `Braces` structured · `PenLine` draft · `Smartphone` mobile). The glyph is the fast read
and the word is what disambiguates, so **neither is ever dropped**. Sizes: `size-3` in the chip,
`size-3.5` in the filter menu. ⚠️ **`DropdownMenuCheckboxItem` carries no `[&>svg]` sizing rule** —
only `DropdownMenuItem` does — so an icon there MUST be sized explicitly or lucide renders it at its
24px default.

**Resolve display names through `ProjectLabel`** (atomic class, fed by `useProjects()`, built once
and threaded down): `managed:<id>` → project name; worktree → **parent repo name + branch**. The
console persists `context.project` as `managed:<uuid>`, and rendering `context.project ||
context.repo` showed a raw UUID for managed *and* worktree sessions (worktree `repo`/`branch` were
set but lost to the `project` precedence). No backend change — `/api/projects` already returns
`is_worktree`/`parent_project_id`/`branch`.

**`ProjectLabel` owns repo identity too, not just the label.** `repoSlug(context)` returns the
canonical `host/owner/repo` (or null) by walking the SAME chain `resolve()` walks — managed id, then
worktree-defers-to-parent, since a worktree has no remote of its own. It lives on the class rather
than beside its caller because two copies of that walk drift the moment one gains a case;
`SessionHeader` consumes it to render `RepoLink` (the wiki's dotted-underline affordance —
domain-agnostic despite its folder, reuse it). A project with no remote has neither `repo` nor
`aliases` (the backend leaves the keys ABSENT, not null), so nothing renders.

⚠️ **`capabilities` means EXERCISED, not advertised. A class label must be earned by an INVOCATION,
never by an advertisement.** The console's `headers()` builder stamps a FIXED
`stlite,apps,ask_user,generative_ui` on every request, so anything derived from
`context.client_capabilities` describes the console's rendering ability, not the session. Feed that
advertisement into `SessionOrigin.classify` and its `"apps" in capabilities` arm classifies ordinary
chats as `apps` — which, since the default filter shows only `user`+`channel`, does not merely
mislabel them, it **hides them**. A capability chips a row only when the transcript proves the tool
ran. The advertised set is legitimately shown by `SessionBindingPanel`, which reads
`spec.capabilities` — a different source, where "what this session was scoped to" IS the question.

**The row also carries `diff_stat` (`+N`/`-M` via the shared `DiffStats`), and the open-session
header carries the same number.** Both read the one server-side `SessionSummary.diff_stat`;
`SessionDetailView`'s timeline-derived `sessionFiles` aggregate sits right there and is deliberately
NOT used for the header, because one fact with two sources is exactly the class of bug this removed.
Absent/zero renders nothing.

### Pinning and the project filter

`HomeView` owns both: the NavRail's Tasks section stays a capped recents view with no filter UI of
its own, so the origin filter, the project filter and pinning all live on the one surface rendering
the full list.

- **Pinning is an ORDERING over the already-filtered list, never a filter of its own.**
  `displayedSessions` (origin filter → project filter) is computed first; pinned rows are then
  partitioned OUT into their own `Pinned` section ahead of `Last 7 Days`/`Older`, and removed from
  those two so a row never renders twice. A pinned session the filters would hide stays hidden.
- **The pin action lives on `SessionItem`** (`onPin`/`onUnpin`, mirroring `onArchive`/`onUnarchive`)
  and on each row of the search `CommandDialog` — two render sites, one pair of callbacks threaded
  from `App.tsx`/`useSessions()`. The pin button stays visible (not hover-gated) when a row IS
  pinned, unlike archive, because a user should see which rows are pinned without hovering every one.
- **The project filter's OPTIONS come from `SessionSummary.projects`** (the accumulated set a
  session's context has ever bound to), **never from `context.project`** (the current binding
  alone), because an auto-select session that already switched projects would otherwise vanish from
  every project's filter option except its last. `ProjectLabel.resolveIdentity(identity)` resolves
  ONE entry of that set — a DELIBERATELY separate method from `resolve(context)`, because the
  backend records `projects` entries with the `managed:` prefix already stripped, so
  `resolveIdentity` checks `byId` FIRST where `resolve` checks it only after stripping a prefix that
  isn't there.
- **`SessionListFilter`/`listSessions(includeArchived, filter)` exist on the wire client** (mirroring
  the `?project=`/`?pinned=` params `GET /api/sessions` accepts) but `HomeView` does NOT use them —
  it keeps fetch-once-filter-locally rather than mixing a server-side and a client-side filter on
  one page. The server-side capability exists for MCP and any future paginated page; if the
  console's list ever needs to stop fetching everything, this is the seam to push the filters
  through, not a new one.

### The list in chrome lives in the NavRail

`TaskSidebar` is deleted; the rail's Tasks section absorbed it (`src/components/nav-rail/CLAUDE.md`
is the authority). Two laws born here that SURVIVE in the rail: **the shared origin filter**
(`utils/sessionOrigins.ts` `DEFAULT_VISIBLE_ORIGINS`/`isDefaultVisibleOrigin`, imported by both
`HomeView` and the rail so they can never drift), and **the shadcn `sidebar`-block rejection** —
that block ships a parallel `--sidebar-*` token palette + SidebarProvider/cookie/keyboard apparatus
that fights this repo's token discipline; the rail stays hand-composed on `Sheet` (Radix), and
re-introducing the block is a review reject.

## Live deployment & API access

The console runs on the deployment's own domain over HTTPS with a self-signed cert (check the local
ops/deployment config for the host — not tracked in this repo).

- **API credentials live in the deployment's `.env`** (a separate, untracked ops directory) — NOT in
  `app.json` or `~/.mewbo/app.json`.
- **Auth header:** `X-Api-Key: <MEWBO_MASTER_API_TOKEN>` (not `Authorization: Bearer`).
- **Ports:** API on `MEWBO_API_PORT` (default 5125), console fixed at 3001.
- **Curl pattern:** always `-sk` (silent + skip cert verification) for the self-signed cert.
- **Playwright (via the MCP plugin) CAN reach this site** — the browser context accepts the
  self-signed cert. Use `browser_navigate` + `browser_evaluate` + `browser_take_screenshot` to
  inspect rendered DOM, verify styling, and diagnose "it's not showing" bugs. Use `curl -sk` for
  plain API JSON where Playwright is overkill.

## Testing

- Vitest + React Testing Library. Mock fixtures live in `__tests__/fixtures/` and are imported by
  tests only — there is no runtime mock fallback. When adding exports to `api/client.ts`, mirror
  them in the `vi.mock` block of `__tests__/app.test.tsx`.
- This suite does **not** enable Vitest globals, so RTL auto-cleanup never runs — add
  `afterEach(cleanup)` yourself.
- `SettingsView.integration.test.tsx` renders the shell against the **REAL**
  `configs/app.schema.json` (read off disk from the repo root, never a fixture snapshot), so a
  backend `x-group` rename or a section RJSF can't slice fails loudly here instead of in the
  browser. Keep it pointed at the real file.

### Three traps that make a correct test look flaky

- **jsdom shares `window.location` across a whole test file.** Once `SettingsView` writes `?facet=`,
  the NEXT test in that file inherits it and starts on the wrong facet. Any file rendering
  `SettingsView` (or anything reading `useSearchParams`) needs `window.history.replaceState({}, "",
  "/settings")` in `beforeEach`.
- **A `React.lazy` boundary crossed inside a `waitFor` races the COLD module transform against the
  default 1000 ms timeout.** It passes in isolation and fails under full-suite parallel load, which
  reads exactly like flake. **Pre-warm the chunk (`await import("…")`) — do NOT bump the timeout**;
  the timeout is measuring Vite's transform, not your assertion. Settings panes are all lazy, so any
  pane test inherits this hazard.
  - **⚠️ A pre-warm list that mirrors a registry goes stale SILENTLY, and the failure lands somewhere
    else.** `SettingsView.integration.test.tsx` warmed four panes while `panes.ts` registered ten,
    and its first case walks every facet — so six raced their own cold transform. The warmed list is
    now asserted against the registry's own `PANE_COUNTS`, so registering a pane without warming it
    fails THERE. **Generalise it: when a test enumerates something a registry also enumerates,
    couple the two.**
- Wrap navigation tests in a wouter `memoryLocation` `Router` and assert on its recorded `history`
  rather than the global browser location.

### A type mirroring a wire contract needs a test against a real payload

`tsc` only checks that a test's object literal matches the TS type — it has no way to know whether
that type still matches what the backend serializes. A unit test that hand-constructs the fixture
can stay green forever after the backend renames or adds a field, because the test and the type were
only ever checked against each other. Both suites report green while the feature reads nothing at
runtime. This is the same failure `tests/fixtures/transcript_timeline_corpus.json` closes for the
transcript assembler.

Concretely, worth watching on the batch-spawn envelope: `spawn_agent.py`'s refusal path attaches a
machine-readable `code` per refused entry, but `SpawnBatchAgentEntry` (`types.ts`) declares no `code`
field and `buildSpawnBatchLog` never reads it — a hand-built fixture typing out only `reason` would
stay green with the field silently dropped. Build the next batch-spawn fixture from a real serialized
`agent_batch` result.

## Pre-PR self-review checklist

- [ ] Nothing I wrote appears in the library-first "use instead" table. If I wrote a component from
      scratch, I can name the specific shadcn component(s) it would have replaced and why they don't
      fit.
- [ ] Any new fetch, poll or render gate has walked "Before you add a fetch or a poll", and a perf
      claim carries a measurement on the deployed artifact, not a green suite.
- [ ] A new runtime dependency serves a concern not already covered, and is in the dependency table
      with a "why this one" entry.
- [ ] Every color comes from a CSS variable (`hsl(var(--token))`). No literal `hsl(...)`, no
      `text-white`/`text-black`, no Tailwind palette names outside `index.css`. Status colors are
      NOT an exception — the only sanctioned raw colors are identity hues.
- [ ] A new theme token is in **both** `:root` (dark) AND `.light`.
- [ ] Diff is **smaller than it would have been with a custom implementation.**
- [ ] Shape vocabulary respected: left `8/6/notch`, right `6/4/0`, `rounded-full` only on state
      containers.
- [ ] Any new status / phase / progress display extends `<RunTelemetry>` or `<StatusBadge>` rather
      than starting a parallel readout. Stop & steering stay in the composer.
- [ ] Loaders / strips mount on `isRunning`. Hover-revealed elements also reveal on
      `group-focus-within`.
- [ ] No new infinite keyframes beyond `.session-cmp-pulse` and the mounted-only `.flower-mark`; no
      new top hairlines on the composer.
