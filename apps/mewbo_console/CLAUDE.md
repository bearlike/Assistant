<!-- mewbo:noload -->
> ↑ [root /CLAUDE.md](../../CLAUDE.md) · children: [wiki](src/components/wiki/CLAUDE.md) · [agentic_search](src/components/agentic_search/CLAUDE.md)

# Mewbo Console — Frontend Engineering Guide

**Subsystem docs (read the deepest one that applies):**
- `src/components/nav-rail/CLAUDE.md` — the NavRail: the console's SINGLE navigation surface (anatomy, per-product data contracts, Aura convergence decisions, the `--rail-w` offset contract, change discipline). Read it before touching anything nav-shaped.
- `src/components/wiki/CLAUDE.md` — MewboWiki FE: atomic `IndexingProgress` class, the shared 3D `Graph3DView` graph engine, full-log timeline pinning, SSE consumer pattern. The colocated `README.md` has the depth reference (file map, endpoint table, Mermaid invariants, Q&A streaming contract).

## What this is
A lean React + Vite + Tailwind web console that wraps the Mewbo API. Not a general-purpose SPA — it's an instrument for watching an agent work. Two principles run in parallel: **library-first** for engineering and **minimal-and-purposeful** for design (sections below). Neither overrides the other.

## Design Philosophy — minimal & purposeful

The console is two surfaces with two jobs: **left = calm reading**, **right = instrument panel**. Each side has its own shape vocabulary; mixing them flattens the hierarchy. Patterns come from familiar tools (composer-owned Stop, token-based status with steering-during-runs, peek-on-proximity scrollbars).

### Shape vocabulary

| Side | Radii | Surfaces | Signature |
|---|---|---|---|
| Left — conversation | `8` cards, `6` micro, `8/8/0/8` user bubble (`.bubble-notch`) | `--background` / `--card` / `--user-message-bg` | Bubble notch is the **only** silhouette signature |
| Right — workspace | `6` cards, `4` count pips, `0` sliding tab indicator, `10` terminal chrome | `--code-chrome` / `--code-body` family | `2.5 px` brand left rail on **log entries only** |

`rounded-full` (`9999px`) is reserved exclusively for **state containers**: status badges, agent-id pills, scrollbars, brand mark, run-pulse dot, `<ScrollToBottom>`, the frosted `.session-ts-rail` backplate. Never on buttons, cards, or chrome — those use `rounded-md`/`rounded-lg` via `Button` (`components/ui/button.tsx`).

### Typography — scale, weight, and the mono law

The console runs **Inter** (variable weight) paired with **JetBrains Mono**, a battle-tested pairing chosen specifically for Inter's variable weight axis: it is what makes it possible to carry a surface's hierarchy on font-weight alone, rather than reaching for a second typeface or a 1px size step. The console ran Geist for a stretch; its two families didn't clear the same battle-tested bar, so the pairing reverted. The one deliberate exception is the embedded Streamlit widget sandbox, which keeps Streamlit's own bundled fonts by design — see "Stlite widget rendering" → "Fonts" below; the console-wide family does not reach into it.

**The type scale** — `tailwind.config.js` `theme.extend.fontSize` is the source of truth; every size in the app must resolve to one of these steps:

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

`sm` and `base` are deliberately redefined off Tailwind's stock 14px/16px down to 13px/14px — the console is an instrument panel, one step denser than a content site, and renaming the classes instead would have meant rewriting hundreds of already-correct call sites to say the same thing under a different name.

`text-field` is not a scale step, it's a floor: iOS Safari zooms the viewport when a focused text input computes below 16px, and `base` is 14px here, so every `<input>`/`<textarea>` carries `text-field` instead of following body type down. It is named `field`, not `input` — `colors.input` (shadcn's border token) already existed, and a `fontSize.input` key would have generated the same `.text-input` utility twice; see the `text-input` collision trap under "CSS / Component Pitfalls" for what that does.

**Adjacency rule.** A single surface uses at most three adjacent scale steps. Hierarchy inside a surface comes from weight and colour, never from a 1px size step — a difference that small reads as noise, not structure. The NavRail deliberately spends only ONE of those steps — every reading row is `text-sm`, hierarchy carried entirely by colour, weight and indentation (see `nav-rail/CLAUDE.md` → "Type & density ramp"). A single-size surface trivially satisfies this rule; it is not a licence to loosen the three-step ceiling elsewhere.

**`em` is allowed, `px`/`rem` absolute sizes are not.** `src/__tests__/typographyScale.test.ts` is the enforcement seam: it bans `text-[<n>px]`/`text-[<n>rem]` (an inlined absolute size that escapes the scale) but permits `text-[<n>em]`, because an em size is relative to whatever step its parent already chose — inline `code` at `0.9em` stays correctly proportioned whether it lands in a heading or in body prose, which an absolute value can't do. If a surface genuinely needs a size the scale doesn't have, the fix is to argue for a new step in `tailwind.config.js`, not to inline a literal.

**Weight ramp.** `font-normal` (400) is the default. `font-medium` (500) is emphasis only — a title, an active-state tag, badge text, a value distinguished from its own label. `font-semibold` (600) is headlines only. The console had drifted far enough that `font-medium` became the de-facto default across the tree — 272 call sites against 8 of `font-normal` — and at that ratio a weight step stops signalling anything, because it's applied almost everywhere regardless of whether the content is actually emphasized. Strip `font-medium` on sight from anything that isn't.

**The mono law.** Monospace is for machine text you might copy, diff, or align: code, terminal output, diffs, file paths, ids/SHAs, cron expressions, raw JSON, column-aligned numerals. Strip it from labels, buttons, badges, chips, headings, prose, empty states, status text, app names, version strings, and non-aligned counts — those carry hierarchy through weight and colour, never a second typeface. The tree had accumulated 257 `font-mono` call sites and most were wrong, added for a "technical" look rather than because the content was actually machine-readable. A version string like `v3` is a label — strip it. A trigger kind like `time.cron` is a dotted machine identifier — keep it. When a string is ambiguous, ask whether a user would ever select and paste it elsewhere; if yes, it's probably machine text.

### Composer chrome — one family

Every composer in the console — Tasks (`InputBar`), Search (`SearchBar`), Wiki (`QADock`), Apps (`AppsLanding` `HeroComposer`) — shares ONE chrome system. A new composer that hand-rolls its surface is a review reject.

- **The knobs**: `--composer-bg` (= `hsl(var(--card))`) and `--composer-radius` (= `1rem`) in `index.css`. Change a knob, every composer follows. `composerSurface()` takes `{elevation: "elev-1"|"elev-2", halo: "soft"|"strong"}` — there is deliberately NO radius option.
- **The CSS family owns ALL states**: `.composer-surface` (border-strong hairline, 200ms transitions) + `:focus-within` (primary/0.55 border + 4px primary halo sized by `data-halo`) + `data-running` (permission tint, permission-tinted ring) + `data-command` (primary tint, suppressed while running). Never express a composer border/halo/tint as per-component Tailwind classes — add a `data-*` state to the family instead.
- **`ComposerShell`** (`ui/composer-shell.tsx`) is the component to reach for: slots `top`/`toolbarLeft`/`toolbarRight`/`popover`, `bodyClassName` = the surface-override seam (tailwind-merge, later wins — QADock's floating `bg-card/95 + backdrop-blur + shadow-[var(--elev-3)]` is the canonical positional exception), `className` = outer-wrapper layout passthrough (the `relative w-full` ↔ `fixed …` merge is twMerge-load-bearing). `ComposerSendButton` is the shared send affordance.
- **Tasks is chrome-shared, behavior-bespoke**: `composerCard()` only adds elevation (elev-1→elev-2 on expand) + `--composer-padding`; its running Queue/Stop, RunIndicator, plan-mode and slash-command logic stay in `InputComposerBody` and signal the family via data attrs.
- **`CreateAppDialog`'s textarea is NOT a composer** — it's a form field in a dialog and stays a shadcn `<Textarea>`; forms look like forms.
- **Typography**: every composer's actual `<textarea>`/`<input>` uses `composerInputCls()` — the parameterless helper in `ui/composer-shell.tsx` that returns `text-field` (16px) plus its placeholder tier — iOS Safari zooms the viewport when a focused input computes below 16px, and under the current scale both `text-base` (14px) and `text-sm` (13px) sit under that floor, so there is no smaller sanctioned TYPED-text size variant. "Hero" vs "compact" composer sizing lives in padding and chrome, never in the input's own font size (a `md:text-sm` step-down once lived in this helper, had zero callers, and was deleted — it takes no arguments for exactly that reason). **The placeholder is a deliberately smaller, separate tier**: `composerInputCls()` also carries `placeholder:text-base placeholder:font-normal` (14px, explicit normal weight) — the same 16px placeholder read as oversized next to the rest of the 13–14px UI, and this is safe against the iOS zoom because the zoom keys off the *focused input's* computed size (still unconditionally `text-field`), never the inert `::placeholder` pseudo-element. Every composer gets this from the shared helper — never spell `placeholder:text-*`/`placeholder:font-*` locally. Placeholder colour is a separate, pre-existing rule and stays local at each call site: the bare `placeholder:text-[hsl(var(--muted-foreground))]` (no opacity modifier).

### Operating principles

- **Status in exactly one place.** Global state → navbar `<StatusBadge>`. Live telemetry → `<RunTelemetry>` rendered as `variant="compact"` in the composer strip and `variant="full"` in the workspace sticky spinner — same `RunStatus` data, two windows. **Failure → `<RunFailedCard>`**, the one run-failure readout, rendered by both the conversation timeline and the trace panel. Stop → composer only. Don't start a parallel readout; extend one of these. `RunFailedCard` exists because the failure case had drifted the furthest from this law: THREE surfaces each rendered the error with their own Retry/Continue pair, all calling the same `triggerRecover`.
- **Phase-driven visibility.** Spinners, run-strips, telemetry mount only when `isRunning` (idle / awaiting / completed / failed → nothing). `runStatus` is computed once in `SessionDetailView` and threaded down; nothing polls.
- **Steering-aware composer.** During a run the composer stays alive: `composerCard({running})` (`InputBar.tsx`) tints the border via the permission token, placeholder shifts to "Steer the run…", Send becomes **Queue** (`ArrowUpRight`), Stop appears alongside (not replacing it), and the toolbar locks session-config/model (visible but disabled — state stays transparent).
- **Stop is destructive — two-step confirm always.** `InputComposerBody.StopWithConfirm`: ghost+red-hover button → popover above with danger primary + ghost cancel. Esc in the textarea opens the same popover; it never directly cancels.
- **Progressive disclosure.** Latest assistant turn fully expanded; older turns auto-collapse to **140 px** with a visible mask-fade (`SmartCollapse` in `ConversationTimeline`). The assistant turn has exactly ONE footer strip (`AssistantTurnFooter`) and nothing else beneath the message — no separate always-on copy row. The whole strip (copy included) idles at `opacity-0` and reveals in full only on `group-hover/turn` or `group-focus-within/turn`; there is **no** always-visible `isLatest` case — the latest turn hides its footer too. Strict two-sided layout: read-only info hugs the LEFT (model id · human-readable generation timestamp · token usage · context bar), interactive controls hug the RIGHT (the shared `CopyButton`, then `Trace`, then the `⋯` overflow). Copy lives ONLY in that right cluster — the bubble carries no copy of its own when the footer renders (`MessageBubble showCopy={false}`).
- **Whitespace over lines.** Padding rhythm carries turn boundaries; the composer dissolves UP into the conversation via `.composer-band-glow` — no top hairline. Add a hairline only when whitespace is ambiguous (workspace tab strip, run-indicator strip).
- **Animation is information.** Only motion: state transitions, entrance stagger (`.session-row-in`), and run-state aliveness (`.session-cmp-pulse` is the sole infinite loop while idle; `.flower-mark` only spins while mounted, i.e., while running). `prefers-reduced-motion` kills every keyframe.
- **Accessibility.** Hit targets ≥ 24×24 (AA), primary controls 32–40 px via `Button` `sm`/`md`/`lg`. Hover-revealed elements must also reveal on `group-focus-within`. Icon-only buttons need both `aria-label` and `title`. Don't strip `Button`'s built-in focus-visible ring.
- **Novelty earns its place.** The codebase ships two non-standard widgets, each with a job: `<TurnScroller>` (token-weighted cost-map dots pinned to the conversation pane's left margin via `absolute left-2` inside `.conv-scroll` — deliberately NOT viewport-`fixed`, so it stays bounded below the SessionHeader and above the composer instead of centering on the full viewport; segments capped at 22 px so they stay a margin glyph) and `<FlowerMark>` (branded conic ring local to `LogsView`). Add a third only if it answers a question the existing vocabulary can't.

### Session Detail anatomy — discoverability map

| Surface | Where it lives |
|---|---|
| Composer band glow | `InputBar.tsx` + `.composer-band-glow` |
| Composer shell (running tint, focus halo) | `composerCard({expanded})` in `InputBar.tsx` layers elevation/padding over the shared `.composer-surface` chrome family (see "Composer chrome — one family"); running/command tints ride `data-running`/`data-command` attrs, not Tailwind classes |
| Top-level model + fallback-chain picker | `ModelSelector.tsx` (footer pill; reuses `ModelBrandIcon`/`formatModelName`). The fallback sub-control is `ModelFallbackChain.tsx` — a Switch + ordered add/remove ladder, extracted so every run-starting surface reuses ONE control (Tasks composer, wiki configure wizard, wiki project settings, agentic-search scope menu) instead of hand-rolling its own. The ladder is a flat `string[]`; there is NO separate "enabled" wire field — the Switch maps to null/empty-vs-list, so a caller persists "no ladder" as null/omitted. |
| Session-config drill-in (project/branch/worktree/skills/integrations) | `ConfigMenu.tsx` (root list → panels; model/fallback deliberately absent) |
| Session header (back, editable title, status, IDE capsule, overflow, app/wiki jump) | `SessionHeader.tsx` — in-pane sticky z-20; module-scope subcomponents preserve Radix state |
| ThreadList rail (active tint, origin badges) | `nav-rail/` Tasks section + `assistant-ui/thread-list.tsx` (`RailThreadList`, rows off `threadIds`) |
| Run-indicator strip + Stop confirm + Send→Queue | `InputComposerBody.tsx` (`RunIndicator`, `StopWithConfirm`, Send branch) |
| Conversation lane (24 px gutter) | `ConversationTimeline.tsx` + `.conv-scroll` |
| User bubble notch | `MessageBubble.tsx` + `.bubble-notch` |
| Smart-collapse + turn footer + edit-state card | `ConversationTimeline.tsx` (`SmartCollapse`; `AssistantTurnFooter` = ONE hover/`group-focus-within`-revealed strip, read-only info left (model · generation timestamp · tokens · context) / interactive controls right (`CopyButton` + `Trace` + `⋯`), copy relocated out of the bubble; inline-edit `.edit-kbd`) |
| Mid-run Trace pill (BOTH in-flight rows) | `ConversationTimeline.tsx` `TracePill` — shared by `PendingAssistantRow` **and** `StreamingAssistantRow` (an earlier revision had dropped it once streaming began) |
| Live todo/plan checklist | `TodoCard.tsx` — renders the authoritative `todos` event (schema `{items:[{label,status}],source,agent_id}`), reusing the `PlanCard` badge idiom; parsed/upserted per turn in `utils/timeline.ts` |
| TurnScroller cost map | `TurnScroller.tsx` + `.session-ts-rail` / `.session-ts-seg` |
| Workspace tabs (icons + pips + sliding indicator) | `WorkspacePanel.tsx` + `.tab-ind` |
| Log / Diff / FileRead cards | `LogEventCard.tsx`, `DiffCard.tsx`, `FileReadCard.tsx` (`rounded-md` + `border-l-[2.5px]`) |
| Run-failure readout + inline recovery | `RunFailedCard.tsx` (composes `LogEventCard`; rendered by BOTH `ConversationTimeline` and `LogsView`) — see "Retry / branch / fork" |
| Sticky FlowerSpinner | `LogsView.tsx` `FlowerMark` + `<RunTelemetry variant="full">` + `.spinner-sticky` |
| Pane separator grip | `SessionDetailView.tsx` `<PanelResizeHandle className="pane-rail">` |
| Shared run telemetry + elapsed + live tok/s | `components/RunTelemetry.tsx` (extended with tok/s + phase — never a parallel readout) fed by `hooks/useThroughput.ts` (differences streamed output over the poll clock) + `hooks/useElapsed.ts` |

## Library-First Principle — read this before writing any UI code

**Default position: someone else has already built it. Find that thing and use it.** Every new component, hook, or behavior must pass through this decision tree before custom code is written:

1. **Is there a shadcn/ui block** (https://ui.shadcn.com/blocks) that solves the screen-level problem? Sidebars, dashboards, login forms, chat layouts, settings panes, data tables — many entire screens come pre-composed. If yes: copy the block via `npx shadcn@latest add <block-id>` and adapt props.
2. **Is there a shadcn/ui primitive** (https://ui.shadcn.com/docs/components) for the element? Popover, Dialog, DropdownMenu, Command, Tabs, Tooltip, Sheet, Combobox, Toast, Sonner, Calendar, etc. If yes: install via `npx shadcn@latest add <name> -y` — do NOT hand-write the component, even if it looks "small".
3. **Is there a Radix primitive** (`@radix-ui/react-*`) that handles the interaction? Radix already solves click-outside, Escape, focus trap, ARIA, scroll-lock, portaling, keyboard nav. If shadcn doesn't wrap it yet, install Radix directly and add a thin wrapper in `src/components/ui/`.
4. **Is there a battle-tested library** for the concern? TanStack Query for server state, wouter for routing, react-hook-form + zod for forms, react-resizable-panels for split panes, react-markdown for markdown, cmdk for command palettes, sonner for toasts. Check the "Architecture Constraints" table below — that is the canonical list.
5. **Only after 1–4 are exhausted** may you write custom code. When you do, the burden of proof is on you in code review: justify why no library fits.

**Anti-patterns that this rule explicitly forbids:**
- Hand-rolled `useEffect` for click-outside / Escape / focus trap (Radix owns these).
- Custom modal/dialog/popover scaffolding with `position: fixed` and manual `z-index` (use shadcn `<Dialog>` / `<Popover>` / `<Sheet>`).
- Custom dropdown menu with manual keyboard nav (use shadcn `<DropdownMenu>` or `<Command>`).
- Custom form state machine with `useState` per field + ad-hoc validation (use react-hook-form + zod with shadcn `<Form>`).
- Custom data-fetching hook with `useEffect` + `useState` + manual cache (use TanStack Query `useQuery`).
- Custom router with `popstate` + path parsing (use wouter `<Route>` / `useLocation`).
- "Just a small wrapper" components that re-implement a shadcn primitive's behavior with slightly different styling — instead, import the shadcn primitive and pass `className`.

**The rule's purpose**: this codebase carried ~3,000 LOC of bespoke UI/data infrastructure that we just deleted in favor of these libraries. Every line of custom code we re-introduce is a line we will eventually delete. Stop the cycle by reaching for the library first.

## CSS / Layout Rules

### Z-index stacking contexts — the #1 source of popup regressions

**Rule:** Before changing popup direction or adding absolutely-positioned overlays, trace the full stacking context chain from root to popup. A `z-50` inside a `z-10` parent loses to a sibling at `z-20`.

**How to verify:** Walk the DOM from the popup to the root, noting every element that creates a stacking context:
- `position: relative/absolute/fixed/sticky` with an **explicit** `z-index`
- `opacity < 1`, `transform`, `filter`, `will-change`
- `overflow: hidden/auto/scroll` does NOT create a stacking context (but clips visually)

**Current layout stacking (HomeView):**
- Outer container: `relative overflow-hidden` (no z-index, no stacking context)
- Fixed top section (contains InputBar): `z-20` — popups live here
- Scrollable bottom section: no z-index
  - Sticky session header: `z-10`

Popups open **downward** in home mode and overlap the session list. They need the fixed top section's z-index to be higher than the sticky header's z-index. If you change any z-index in this chain, re-verify popups render above session content.

### Popup direction depends on InputBar mode
- `mode="home"` (InputBar near top) → popups open **down**
- `mode="detail"` (InputBar at bottom) → popups open **up**
- This is controlled by `popupDirection` in InputBar and the `direction` prop on `<Popover>` (our shadcn wrapper in `components/ui/popover.tsx`), which maps to Radix `<PopoverContent side="bottom" | "top">`.
- Never hardcode popup direction — always derive from context.

### Shared Popover component
All dropdown popups use `<Popover>` from `components/ui/popover.tsx` (shadcn wrapper around Radix `@radix-ui/react-popover`). Do not duplicate popup styling inline. If you need a new popup, compose `<Popover>` / `<PopoverTrigger>` / `<PopoverContent>` and pass `side` for direction.

## Data Fetching Patterns

### TanStack Query owns server state
All server-data hooks are thin wrappers around `useQuery` / `useMutation` from `@tanstack/react-query` (v5). The `QueryClientProvider` is mounted at the app root with a 60s default `staleTime`. There is **no manual TTL cache** in `api/client.ts` anymore — the query cache is the single source of truth.

**Read pattern:**
```ts
useQuery({
  queryKey: ['mcp-tools', project ?? null],
  queryFn: () => listMcpTools(project),
  staleTime: 60_000,
})
```

**Write pattern (mutations):**
```ts
const qc = useQueryClient()
useMutation({
  mutationFn: (input) => createProject(input),
  onSuccess: () => qc.invalidateQueries({ queryKey: ['projects'] }),
})
```

**Invalidation:** Always invalidate by `queryKey` after a write. `qc.invalidateQueries({ queryKey: [...] })` matches by prefix, so `['sessions']` invalidates every per-session query as well.

**Conditional polling:** Use the function form of `refetchInterval` so polling pauses automatically when the live state says it should. This is the pattern used by `useSessionEvents` and `useIdeStatus`:
```ts
useQuery({
  queryKey: ['session-events', sessionId],
  queryFn: () => fetchEvents(sessionId, lastTs),
  refetchInterval: (query) => (query.state.data?.running ? 1000 : false),
})
```

**Derived data:** Use `select` to project/filter without re-running `queryFn`:
```ts
useQuery({ queryKey: ['sessions'], queryFn: listSessions, select: (s) => s.filter(x => !x.archived) })
```

### `api/httpBase.ts` is the ONE plain-`fetch` base
The "no custom fetch wrappers" rule in Architecture Constraints does not mean *zero* transport code — TanStack Query owns caching/state, but something still has to build the URL and headers. `src/api/httpBase.ts` is that something, and it is the ONLY copy: `withBase` / `authHeaders` / `jsonHeaders` / `readJson` / `readError`. `api/realClient.ts` and `api/git.ts` both consume it (before it existed each carried its own near-identical quartet — exactly the pattern the rule forbids). `authHeaders`/`jsonHeaders` take an OPTIONAL `apiKey` defaulting to the `client.ts` singleton, which is what lets both call styles share one implementation: `realClient` is instantiated per `ApiConfig` and threads its own key, while `git.ts` closes over the singleton.

Two seams stay deliberately separate — do not fold them in: `api/sse.ts` (streaming) and the wiki's `components/wiki/api/client.ts:http<T>` (its own established mock/backend-swap contract for the `<path:slug>` routes). A THIRD fetch quartet is the thing to reject in review.

### Hooks accept project scope
- `useMcpTools(project?)` — re-fetches when project changes (project is part of the queryKey)
- `useSkills(project?)` — same pattern
- `useProjects()` — no project scope (global list)
- `useWebIdeEnabled()` — checks if Web IDE feature is available
- All hooks expose a `refresh()` shim that delegates to `queryClient.invalidateQueries`. New hooks should follow the same convention so consumers don't need to learn the query API.

### Agent lifecycle data
- Agent activity is tracked from `sub_agent` SSE events with enriched fields: `status`, `steps_completed`
- `AgentResult` JSON from `spawn_agent` tool results is parsed in `logs.ts` for structured display

### Token usage — three semantics, one source of truth
The backend's `build_usage_numbers` (and the `/api/sessions/{id}/usage` endpoint feeding `useSessionUsage`) returns three distinct semantics. Pick the right one for the surface you're rendering:

- **Context fill (right now):** `root_last_input_tokens` — the size of the *most recent* root prompt. This is what the model has in its window now. Use this to drive the `<ContextWindowBar>` fill, the "X% used" label, and any "tokens until auto-compact" display. (This is the live context-window usage.)
- **Context pressure (worst case so far):** `root_peak_input_tokens` and `sub_peak_input_tokens` — max input across calls. Show as a secondary stat ("peak this session") for users who care about historical worst case. **Never sum input across calls in a turn** — the prompt grows as tool results stack onto the same context, and summing double-counts the baseline (the 120K-phantom bug fixed in `f6bf745`).
- **Billable (cost):** `*_input_tokens_billed` and `*_output_tokens` — provider-charged sum across all calls. Pair with `*_cache_read_tokens` and `*_cache_creation_tokens` to apply the discount client-side: Anthropic cache reads bill at **0.1× input**, OpenAI cached at **0.5×**, Anthropic 5-min cache writes at **1.25×**. Reasoning output (`*_reasoning_tokens`) is hidden thinking tokens from extended-thinking / o1-class models — billed as output, surfaced separately so users can see its share.

`<ContextWindowBar>` (`components/ContextWindowBar.tsx`) is the single canonical widget that displays all three semantics — the fill bar shows context (now), the popover header shows peak (pressure), the popover body shows billable (cost) plus cache + reasoning. **Reuse it everywhere; do not hand-roll a token display.**

## Architecture Constraints

**Before writing any code that touches the categories below, confirm you've followed the Library-First Principle decision tree.** These libraries are not suggestions — they are the *only* approved choice for each concern. Adding a competing library (e.g., Redux alongside TanStack Query, or react-router alongside wouter) requires explicit design discussion, not a drive-by PR.

Permitted libraries (and the only libraries to use for these concerns):

- **Server state:** TanStack Query v5 (`@tanstack/react-query`). Do not write custom caches, TTLs, or fetch wrappers — `useQuery`/`useMutation` cover every case.
- **UI state:** Native React (`useState`, `useReducer`, Context). No Redux/Zustand/Jotai.
- **UI primitives:** shadcn/ui (Radix + cva, copied into `src/components/ui/`). Use `<Popover>`, `<DropdownMenu>`, `<Dialog>`, `<Command>`, `<Tabs>`, `<Alert>`, `<Button>`, `<Input>`, `<Textarea>`, `<Label>`, `<Form>`, `<ScrollArea>` from there. Never hand-roll click-outside, focus traps, Escape handlers, or ARIA — Radix handles all of them.
- **Routing:** wouter v3. `useLocation()` for the current path; `<Route>` / `<Switch>` for matching; `navigate()` for programmatic moves.
- **Forms:** react-hook-form + zod for static schemas. RJSF stays only in `SettingsView` because the schema is supplied dynamically by the backend `AppConfig` endpoint.
- **Styling helper:** `cn()` from `src/lib/utils.ts`. It is `clsx` + `tailwind-merge`, so later Tailwind classes override earlier ones (e.g., `cn("p-2", "p-4")` → `"p-4"`).

Other rules:
- Agent activity indicator in `ConversationTimeline` tracks active agents with task descriptions (not just count).
- Permission events rendered with icons: ⛔ deny, ✓ allow.
- Sub-agent logs show lifecycle status and step count (▶ start, ■ stop with status).

### assistant-ui — the chat-surface vendor (composer, thread list, thread)

**Decision: assistant-ui (`@assistant-ui/react`) OWNS the chat-surface concern.** Composer chrome, thread-list rail, and (eventually) the transcript migrate onto its primitives; bespoke chat-surface code is legacy to be burned down, not extended. This is the explicit design discussion the "one library per concern" rule demands — don't relitigate it per-PR.

- **Two install surfaces.** `@assistant-ui/react` (+ `@assistant-ui/react-markdown`) are npm imports; the styled components are **source-copied** shadcn-registry files under `src/components/assistant-ui/` (`npx shadcn@latest add "https://r.assistant-ui.com/base/<name>.json"`) — same vendored-and-patched-inline convention as `ui/`. `zustand` is in the tree ONLY as their internal dep; the "no Zustand for app state" rule stands.
- **⚠️ The registry CLI clobbers.** Re-running it rewrote our custom `button.tsx` (to stock variants) and injected Tailwind-v4 directives (`@import "tw-shimmer"`, `@custom-variant`, `@theme inline`) into `index.css`. Always diff-review its output; expect to revert those two files.
- **⚠️ npm lockfile churn.** Bare `npm install <pkg>` here rewrites the whole lockfile and DROPS hoisted transitives (`@testing-library/dom`, top-level `esbuild`) → broken tests/build. Fix pattern: `git checkout package-lock.json && npm install` (re-adds on top of HEAD's lock, keeps hoisting).
- **⚠️ Vendored files are written for Tailwind v4 + Base UI; we run v3 + Radix.** v4 syntax SILENTLY emits no CSS on v3 — no error, just unstyled. Translate AT MOUNT TIME, and only then (unmounted reference files stay vendor-faithful so future diffs against upstream stay clean): `bg-(--x)`→`bg-[var(--x)]` · fractional `size-4.5`→arbitrary values · `wrap-break-word`→`break-words` · `@container`/`@md:`→unsupported, restructure · Base-UI `data-open:`→Radix `data-[state=open]:` · their own primitives keep `render` props, but OUR Radix wrappers need `asChild`. `data-slot` attrs don't forward through assistant-ui primitives — style via `className` only.
- **`dark:` variants never fire here.** We toggle `.light` (dark is the `:root` default; Tailwind's selector strategy expects `.dark`, which never exists). Fold any vendored `dark:` style into token values — semantic tokens already adapt per theme.
- **Runtime seam = `useExternalStoreRuntime`** (`assistant-ui/MewboRuntimeProvider.tsx`, mounted in `AppLayout`). assistant-ui does ZERO networking — our SSE/TanStack/REST stack is untouched. Thread-list fields nest under `adapters.threadList` with archived sessions as a SEPARATE `archivedThreads` array; active-row highlight comes free from mapping `activeSessionId → threadId`. **Render rows off `s.threads.threadIds`, NOT `threadItems`** (not 1:1 — indexing it throws `useClientLookup: Key undefined`).
- **`messages: []` is deliberate scaffolding.** `ConversationTimeline` still owns the transcript; the provider exists for ThreadList + the future Thread migration. `App.handleComposerNew` (the `onNew` seam) MUST keep the `isRunning` steer fork (`sendMessage` vs `postQuery`) in sync with `useSessionQuery.send` — a mid-run submit without it double-fires instead of steering.
- **Composer = vendor look, app logic.** `InputBar` renders the vendor card language (`composerCard()`, `--composer-bg/-radius`, `--elev-*`) but submit stays on `props.onSubmit` with its context assembly (`mcp_tools/skill/project/branch/model/fallback_models`). Never wire `ComposerPrimitive.Send`/runtime `send()` while `onNew` also posts — that's the double-fire. Control grouping is load-bearing UX: **model is a top-level footer control** (`ModelSelector.tsx`, fallback chain lives in the same popover), ConfigMenu is session-config only (project/branch/worktree/skills/integrations, root-list drill-in — the 8-tab strip is gone and must not return), per-chat controls (attach/plan/voice/stop/send) sit directly in the footer.

### shadcn convention
- **Always check shadcn first.** Before creating any new component file, search https://ui.shadcn.com/docs/components and https://ui.shadcn.com/blocks. If shadcn ships it (or composes it as a block), install it via `npx shadcn@latest add <name> -y` and adapt — do not write a parallel implementation. This applies to "small" things too: a wrapper around `<input>` with a label is `<FormField>`, not three lines of JSX.
- Components live under `src/components/ui/` as **lower-case file names** (`button.tsx`, `popover.tsx`, …). They are *vendored* — copied into the repo, not imported from a node_module — so we own them and can patch freely. Patch the vendored file inline rather than wrapping it in another component.
- Install / regenerate via `npx shadcn@latest add <name>` from the console package. The CLI writes into `src/components/ui/` and updates `components.json`. Re-running `add` is safe — review the diff before committing.
- **Use shadcn blocks for whole-screen patterns.** Sidebar navigation, dashboards, login forms, settings shells, chat layouts, data tables, and similar composed pieces are available as blocks. Prefer copying a block and trimming it over composing primitives from scratch.
- Prefer Radix primitives over custom dropdowns/menus/dialogs. Radix already solves click-outside, Escape, focus trap, ARIA, scroll-lock, portaling — never re-implement these. If shadcn doesn't yet wrap the Radix primitive you need, add a thin shadcn-style wrapper in `src/components/ui/` and PR it the same way shadcn would.
- App-level wrappers (e.g., a project-shaped button variant) belong in `src/components/` and **compose** the `ui/` primitive — never fork it. Pass `className` via `cn()` for visual variants; pass children for content. If a wrapper grows beyond ~30 lines of layout JSX, ask whether a shadcn block already covers it.
- New iconography uses `lucide-react` (shadcn's default). Do not introduce a third icon library — see the audit in Phase 4 of the foundation reset.

### `<Button>` primitive (`components/ui/button.tsx`)
All buttons use the shared `<Button>` component. Variants and sizes are defined with `cva`. Never create ad-hoc button styles — use `<Button>` with the appropriate variant.
The cva variants are `primary | neutral | ghost` — there is no stock-shadcn `outline`/`default`; don't pass those names.

### ReviewPane (`components/ReviewPane.tsx`)
Accordion-style file review that replaces the old single-file diff tab. Shows all edited files in a collapsible list with unified diffs.

## Navigation & information architecture — where a page is allowed to live

**There is exactly one primary nav: the left NavRail** (`src/components/nav-rail/` — its own CLAUDE.md is the authority). There is NO top navigation bar anywhere, on any route: the old root `NavBar` (home strip + detail bar) and `TaskSidebar` are deleted, and nothing may reintroduce a horizontal chrome bar. Everything else is a facet of Settings, a slim in-pane contextual header (`SessionHeader` for sessions, `WikiTopBar` for wiki screens, `AppDetailHeader` for apps), or a link. The earlier consolidation laws survive the rail unchanged: four standalone pages (`/projects`, `/plugins`, `/keys`, `/triggers`) and `/draft` stay deleted.

- **The avatar menu is NOT a page index.** It lives in the rail footer now and is still exactly four rows: Settings · Documentation · GitHub · theme toggle. A menu that duplicates the primary nav teaches users the menu *is* the nav; adding a destination here is the smell — add a facet or a rail affordance instead.
- **`activeProduct` says which rail row is CURRENT, not whether the rail renders** (`App.tsx` → `'tasks'|'wiki'|'search'|'apps'|null`, computed at the same altitude the old `landingNav` was). The rail renders on **every** route; a route that maps to no product (`/settings`) marks nothing `aria-current` — never strand a route without primary nav. Session pages render the rail too, with `SessionHeader` carrying the detail obligations in-pane.
- **Retired routes redirect, they don't 404.** `/projects`→`?facet=workspace`, `/plugins`→`?facet=plugins`, `/keys`→`?facet=security`, `/triggers`→`?facet=automation`. Bookmarks and in-app deep links keep resolving. `/triggers` is the one that can't be a static `<Redirect to>` — `SessionTriggersSection` deep-links `/triggers?session=<id>` and the automation pane still reads that param, so `TriggersRedirect` (`App.tsx`) carries the query across the hop; `SessionTriggersSection` itself now links straight at `/settings?facet=automation&session=<id>`.
- **The `/draft` page is gone; the `draft` BACKEND is not.** It was a demo harness for `POST /v1/draft/stream`. The endpoint, `mewbo_core.draft_stream.DraftStreamer`, and the `draft` **`SessionOrigin`** all survive — the origin is session provenance for sessions minted by external API callers, and it stays in `types.ts`/`ORIGIN_FILTERS`/`SessionOriginBadge`. ⚠️ **`src/api/sse.ts` reads draft-owned and is not**: it is the shared generic SSE parser consumed by Agentic Search (`api/agenticSearch.ts`) **and** the wiki (`components/wiki/api/client.ts` — Q&A + indexing). A grep-and-delete purge of "draft" would have silently broken both.

### Settings — faceted shell over RJSF (`components/settings/`)
The Settings page is a **faceted shell**, not a flat form, and it is now the console's cabinet: four surfaces that used to be standalone pages live here as *panes*. RJSF is kept ONLY as the per-section field engine (`RjsfTheme.tsx` templates/widgets); the grouping/search/save shell around it is ours. Don't swap RJSF out, and don't recompute its job in components — call the model.

- **`settings/panes.ts` is the facet→panes REGISTRY, and the shell knows NO facet by name.** A facet may carry schema-driven sections, custom panes, or both; panes render above sections. This replaced a hardcoded `activeGroup?.id === "security" ? … : …` ternary in `SettingsView` — copy-pasting that branch per facet was the DRY violation the registry removes. **Registering a pane is ONE line in `panes.ts` and ZERO in the shell.** Even the search-dimming rule stopped being a name test (`!== "security"`) and became a registry lookup ("has panes" ⇒ never dim, because search only indexes schema sections).
- **The pane contract — conform or you can't be registered.** (1) **Zero props.** A pane is a `ComponentType` with no props, which is exactly what lets the registry be a plain data *map* instead of a switch. A pane fetches its own data via TanStack Query, and re-calling a hook the shell already called is **not** a second fetch (the cache is shared by queryKey) — so there is never a reason to prop-drill into a pane. `SecretsSummary` re-calling `useConfig()` is the canonical demonstration. (2) **`React.lazy` + its own `<Suspense>`** at the render site, so one slow chunk never blocks a sibling's fallback and heavy deps (rhf/zod, extra queries) stay out of the Settings chunk until the facet opens. (3) **Chrome-agnostic** — bare `<SettingsCard>`s, no page width/padding; the shell owns `max-w-3xl mx-auto px-6 py-6`.
- **A pane sits in the facet that owns its settings. That adjacency IS the reason for the consolidation.** The plugin marketplace sits next to `plugins.marketplaces`; the trigger dashboard next to the trigger policy ceilings (`TriggersConfig`); managed projects next to the `projects` map. Live surface and the knobs that govern it read as one page. Registered today: `agent`→SystemInstructionsPane, `plugins`→Plugins, `automation`→Triggers, `security`→SecretsSummary + ApiKeys + GitCredentials, `workspace`→Projects.
- **The corollary: a pane and its config section MOVE TOGETHER, in one commit.** Promoting Plugins to its own top-level facet was not a one-line registry edit — it was `facets.ts` (new `plugins` id + `FACETS` entry + renumbered neighbours), `panes.ts` (`agent`→`plugins`, and `agent` loses its key entirely rather than keeping an empty array), `SettingsView.FACET_ICONS` (the lucide name must be mapped or the facet falls back to `Settings2`), core `PluginsConfig.x-group` (`agent`→`plugins`), the regenerated `configs/app.schema.json`, AND the `/plugins` redirect target. Move the pane alone and it is stranded away from its knobs; move the `x-group` alone and the section vanishes into "Other" (see the lockstep warning below).
- **`settings/SettingsCard.tsx` is THE one SECTION-level card primitive of the surface.** Every top-level card renders through it — the schema-driven `SettingsSection` *and* every pane's own top-level card(s). It owns the card class (`sectionCardCls` in `styles.ts`), the title style, and the description slot (routed to `FieldHelp`). Before it existed, `SettingsSection`, `ApiKeysView` and `GitCredentialsView` each hand-copied the string `rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-5`, and the "chrome-agnostic pane" contract was upheld by convention alone. **The copied-class-string era is over for that section-level card**: a new top-level SECTION card that isn't a `<SettingsCard>` is a review reject. This does NOT extend to smaller, non-section chrome that deliberately skips `SettingsCard`'s title/description/footer slots: `SettingsView.tsx`'s "View as JSON" `<details>`, `RjsfTheme.tsx`'s collapsible RJSF object-group `<details>`, and `PluginsPane.tsx`'s per-plugin list rows all still hand-write the same `rounded-lg border … bg-[hsl(var(--card))]` family for a lighter-weight surface nested *inside* a section — that's an intentional smaller primitive, not a regression of this rule. Keep `SettingsCard` itself a dumb shell — Save/Reset, dirty state and fetching belong to the caller.
- **`?facet=` deep-linking** (`SettingsView`, via wouter `useSearchParams`). The **NavRail's Settings zone is now the ONLY facet navigation** — `SettingsView`'s own facet sidebar and its mobile Sheet are gone, so the shell only READS `?facet=` and derives `activeFacet` from it directly (no local state, no seed effect; an unknown/invisible facet falls back to the first visible one rather than erroring). The facet WRITER — the rail's `settingsSection` — **rebuilds the query from `prev` and only `.set()`s the `facet` key** with `{replace: true}`, so every unrelated param survives a switch (precisely what keeps a `?session=` deep-link alive when the Automation pane opens) and Back leaves Settings rather than walking facets.
- **`SettingsModel.ts`** (pure, React-free, unit-tested) is the single heart: it turns the backend `AppConfig` schema + `{config, secrets}` into facets → sections → fields and owns slicing/search/diff (its `recursiveDiff` is kept byte-identical to `useConfig.shallowDiff`). Facets + their order/icons/blurbs live in `facets.ts` (React-free — icons referenced by lucide *name*; the shell maps name→component); facet membership comes from each section's `x-group` metadata (on the section **class**, OR on a collection field directly — a `dict`/`list` field isn't a bare `$ref`, so its `json_schema_extra` survives; see core `config.py` contract). **`SettingsModel.uiSchemaFor(id, secrets, savedAt)` is the single archetype-routing home** — schema shape → widget (`list[BaseModel]`→`recordList`, `dict[str,X]`→`keyedCollection` incl. root-dict sections, secrets→`secret`, marketplaces→injected `itemValidator`, `plan_mode_shell_allowlist`→`restoreDefault`). Never recompute routing in components.
- ⚠️ **`FacetId` and core's `x-group` must land in LOCKSTEP.** `SettingsModel` validates a section's `x-group` against `FACETS` and **silently buckets an unknown group into the `other` fallback facet** — no error, no warning. Ship an `x-group` in `config.py` without adding its id to the `FacetId` union in `facets.ts` and the section simply *vanishes* into "Other". (This is why the `automation` facet and `TriggersConfig`'s `x-group` flip were one change, and later the `plugins` facet and `PluginsConfig`'s.) `panes.ts` is keyed by `FacetId`, so a pane on an undeclared facet is at least a compile error — the schema side has no such guard. The live guard against the schema half drifting is `SettingsView.integration.test.tsx`, which mounts the REAL `configs/app.schema.json` and asserts the Plugins section renders **in the Plugins facet** — so regenerate `configs/app.schema.json` after any `x-group` edit, or that test (correctly) fails. It caught the committed schema being stale on `TriggersConfig` for exactly this reason.
- **Copy standard for this surface: no em dashes, in any user-visible string.** Not in a facet `blurb`, not in a `<SettingsCard description>`, not in an empty state, not in a `config.py` field description (those render here as field help). Use full sentences, commas, semicolons, or a new sentence. Help text must READ as connected prose that teaches the concept to someone who has never heard of it, not as telegraphic clauses bolted together with dashes — the Automation copy is the worked example (what a trigger is, why you'd want one, what the five kinds do in plain language, who arms them, and where the ceilings live). Code comments may keep their em dashes; user-facing strings may not. **And respect the `FieldHelp` contract when you write one:** a facet `blurb` is a bare line, so keep it under 140 chars or it collapses into a popover-only `?` and the heading loses its blurb; a card/field description gets a real summary line, then `\n\n`, then the narrative that lands in the popover.
- **`SettingsView.tsx`** owns edit state (a `sectionId→formData` map holding ONLY genuine edits) so the @modified filter + per-section Save/Reset work; sections are controlled. A section's current value is resolved **INLINE at render time** — `formState[id] ?? normalizeSection(config?.[id])` (the edit if present, else the live config) — never seeded into `formState` by an effect. ⚠️ **This is the fix for an RJSF seed-race, and it is a general rule.** RJSF's `Form` fires `onChange` with its OWN computed schema defaults *during its mount* (a child's effects run before the parent's), so an eager parent seed effect loses the race: the `onChange` writes the empty computed shape (e.g. an array field defaulting to `[]`, `plugins.marketplaces` locked at `[]`) into `formState`, permanently shadowing the real config. **Never eager-seed RJSF form state from an effect — resolve `edit ?? live` inline.** Saves are per-section: `model.patchFor(id, sectionValue(id))` → `useConfig.savePatch` (returns the new `{config,secrets}`; re-seed only the just-saved section so concurrent edits to sibling sections aren't clobbered).
- **`SecretField`** = write-only 3-state widget (unconfigured / configured-masked+Replace / editing) for `x-secret`/`writeOnly` fields; "configured" comes from the `secrets` map, never a value (the backend strips secret values, so the field is always absent from `config`). The Security facet's `SecretsSummary` pane is the read-only roll-up of that same map.
- **Field kit (`settings/fields/`)** — atomic widgets *around* RJSF that NEVER fall through to its defaults. This is load-bearing: RJSF's default array/object/dict toolbars render as invisible `0×0px` buttons under our theme (no Bootstrap/icon CSS), so collections become silently un-editable. So a single registered **`ArrayFieldTemplate`** owns *every* array (`list[str]` rows + Add/Remove/Move via the shared `<Button>`; per-item `ui:options.itemValidator` + `restoreDefault`); **`RecordListField`** (`ui:field:"recordList"`) owns `list[BaseModel]` (the HooksConfig command/http type-switch); **`KeyedCollectionField`** (`ui:field:"keyedCollection"`) owns `dict[str,X]` (projects/channels/model_context_windows/lsp.servers) with an injected value-renderer + `JsonValueEditor` for freeform maps. **`FieldHelp` is the ONLY description renderer** (one `helpCls` style, compact react-markdown) — render help nowhere else, or the old 12px-vs-16px page-bloat bimodality returns. **Its collapse rule keys off an author-supplied `\n`, never off length alone**, and the reason is worth keeping: a length-triggered collapse whose summary was `text.split("\n")[0]` rendered a >140-char *single-line* description in full inline **and** again in the popover (the `GitCredentialsView` long-intro bug — the trigger and the summary disagreed about what "first line" meant). The rule now: no break + short ⇒ all inline; **has a `\n` ⇒ that first line is a real author-written summary, shown inline with the full text behind a `?` popover; no break + long ⇒ nothing inline, full text popover-only.** It deliberately does NOT fabricate a summary by truncation (a character cut can slice a markdown token in half, e.g. an unbalanced `**`). **So the way to get a summary line is to write one — put a `\n\n` after it in the description**; don't lean on the long-single-line fallback. `marketplaceValidation.ts` still MIRRORS backend `plugins.py::_resolve_git_url`, injected as the marketplaces `itemValidator` (the old `RepositoriesField` was folded into `ArrayField` and deleted). **Every settings `<input>`/`<textarea>` renders `inputTextCls`** (`settings/styles.ts` = `inputBase` + the `text-field` 16px floor, `text-field md:text-sm`) — the settings-side seam for the same iOS-zoom input floor the composers carry via `composerInputCls()`; don't hand-spell `text-sm` on a settings field and re-open the sub-16px zoom.
- **`ApiKeysView` / `GitCredentialsView` are Security-facet panes and nothing else** — the old dual-mount (pane *and* standalone `/keys` route) is gone with the route. They stay chrome-agnostic anyway, because that is the pane contract. `GitCredentialsView` is the CRUD surface over the product-wide `/v1/git/credentials` registry (`api/git.ts` + `hooks/useGitCredentials.ts`) — NOT a wiki feature, even though the wiki is its first consumer. Two invariants: a credential's `value` is **write-only** (the API returns only a `valueHint`, so the UI can never render or round-trip a secret — clear it from form state right after submit), and `scopeType` (`host` = shared by every repo on that host, `repo` = pinned to one slug) is **served by the API**, not re-derived in a component. The onboarding wizard consumes the same hook read-only to hint "a saved credential already covers this repo" (repo-scope match wins over host-scope; typing a token overrides the hint).
- **The Workspace facet holds two things users conflate — keep saying so.** The `ProjectsPane` (managed projects, `/api/projects`) = workspaces **Mewbo** creates and owns, including git worktrees; server-owned means server-deleted (the reaper removes a parent once its last worktree is gone). The schema `projects` dict below it = directories **you** already have, registered by name in `app.json`; Mewbo only points sessions at them. The pane's card description carries the distinction — don't drop it.
- **`SystemInstructionsPane`'s variable reference is LENGTH-ADAPTIVE, and the popover is an a11y decision, not a style one.** `GET /api/system-instructions/variables` returns each variable's candidate `values` plus a `valuesKind` that says how far to trust them — `closed` (a session's value is ALWAYS one of these, e.g. `origin`, so a template may branch exhaustively) vs `known` (what this deployment happens to have installed/observed: tools, models, projects, capabilities — real, but NOT exhaustive). **Render the two differently or the reference lies.** A short list is a fact you read at a glance, so it renders as inline chips; past `INLINE_VALUE_LIMIT` (8) it collapses to a count that opens a `<Popover>` over cmdk's `<Command>` — filter box, arrow-key nav and an empty state, all free from primitives already vendored. **A hover `<Tooltip>` was rejected on accessibility, not taste:** a scrollable, selectable list of a couple of hundred tool ids has no keyboard path in and no touch path in through a tooltip. Every value is a `<button>` that drops the bare string at the CodeMirror cursor — deliberately dumb, with no attempt to guess quoting (the operator is almost always already inside a quoted comparison, and a wrong guess is worse than no guess). ⚠️ **The hardcoded `PREVIEW_SURFACES` const is GONE: the preview tabs ARE the `surface` variable's `values`.** A hand-kept copy went stale the moment a new client started stamping sessions — so never hardcode a copy of one of these lists in a component, and title-case unknown ids rather than dropping them. Same payload, one more consumer: it also builds the editor's autocomplete source (see the `@codemirror/autocomplete` row in the dependency table).
- `getConfig`/`patchConfig` return `{config, secrets}` — every consumer (incl. `useWebIdeEnabled`) must read `.config`, not the top level.

### Panes are where the Library-First rule got enforced retroactively
The four absorbed pages were also the console's last holdouts against the "no hand-rolled caches" rule — the migration was as much a data-layer cleanup as an IA one. What the panes must never regress to:
- **`useVirtualProjects`** was a hand-rolled `useState`/`useEffect` cache that *also* invalidated `['projects']` for the rest of the app — a second cache sitting next to the real one. It now reads the SAME `['projects']` query as `useProjects()` and narrows it with `select`; writes are `useMutation`s invalidating that one key, so the composer's project picker, the session list's `ProjectLabel` and the pane can never disagree.
- **`WorktreesPanel`** raw-fetched branches/worktrees the same way; it now goes through `useProjectGit` (`['project-git', <id>, …]`), the same hook the composer's ConfigMenu already uses.
- **`PluginsView` had no TanStack at all** plus a `window.setTimeout` fake toast; `usePlugins` is the replacement (one `['plugins']` root key so install/uninstall refreshes the installed list AND the marketplace's derived `installed` flag together), with `sonner` for feedback.
- Native `window.confirm()` deletes → shadcn `<Dialog>`.
- **`useTriggers` was already idiomatic and was deliberately left alone.** Not every hook needed touching; the diff stopped where the rule was already satisfied.
- ⚠️ **`isPending` is not a loading flag for a query that can be disabled.** A DISABLED TanStack query stays `pending` **forever**, so `isPending` pins a skipped query's UI in a permanent loading state. Use `isLoading` (= pending AND fetching) — see `useProjectGit`, where a project with no git repo skips the worktrees query.

### Web IDE (`components/IdeLoader.tsx`, `hooks/useWebIdeEnabled.ts`)
"Open in Web IDE" launches per-session code-server containers via the API. `useWebIdeEnabled()` checks whether the feature is available. IDE loader shows a floral background animation during container startup.

### MewboWiki — `/wiki/*` section (`components/wiki/`)
Self-contained auto-generated-wiki namespace: project gallery, configure wizard, indexing loader, wiki page, Q&A streaming view. All screens are wired against a mock client at `components/wiki/api/client.ts` — backend swap is one file. **Full design + integration notes live in [`components/wiki/README.md`](src/components/wiki/README.md)** — read it before editing anything under `components/wiki/`. Key invariants colocated there: stable diagram ids for mermaid (avoid scroll-spy flicker), `mermaid-renderer.ts` singleton cache, `useIndexingStream`/`useQaStream` AsyncIterable contract honouring `AbortSignal`, mock data isolated to `components/wiki/mocks/`, model picker reusing `ModelBrandIcon` + `formatModelName` (no bespoke brand glyphs), platform tiles via `simple-icons` (CC0).

## Stlite widget rendering — Streamlit inside the React shell

`StliteWidgetPanel` (`components/StliteWidgetPanel.tsx`) embeds a full Streamlit app via `@stlite/react` + Pyodide. It shares nothing with Streamlit's HTML shell, so several Streamlit / stlite defaults leak through and fight with our chat layout. These are non-obvious and keep biting — read this section before touching widget rendering.

### `.stApp` is `position: absolute; inset: 0`
It fills its NEAREST positioned ancestor. If you put `position: relative` on the outer panel wrapper (the one that holds both the chrome title bar AND the widget area), `.stApp` fills the entire card and paints **over** the chrome. **Invariant:** never put `relative` on a wrapper that contains the chrome — only on the inner widget-area div BELOW the chrome. Verify via Playwright (query `.stApp` rect vs title bar rect, check `stAppCoversTitleBar`). This bit us for multiple rebuilds because the chrome JSX / hex colors / CSS all shipped correctly — `.stApp` was simply painted on top.

### Two nested scrollbars by default
Streamlit's `<section data-testid="stMain">` ships with `overflow: auto` on both axes, AND our `.stAppViewContainer` also scrolls. Tall widgets render two stacked scrollbars; users scroll the inner one, hit its end at ~900px, and never realize the outer has more content. Kill stMain's overflow with `[&_[data-testid='stMain']]:!overflow-visible` so there's exactly one scroller (the outer `.stAppViewContainer`), themed and always-visible per `index.css`.

### `document.title` hijack during Pyodide boot
When a widget's `app.py` doesn't call `st.set_page_config(page_title=...)`, Streamlit forces `document.title = "Streamlit"` during boot. The `useTitleGuard()` MutationObserver in StliteWidgetPanel reverts ONLY the literal `"Streamlit"` string; legitimate App.tsx title updates (session renames, navigation) flow through untouched. Don't blanket-freeze the title — the guard is specifically scoped to the one value stlite forces.

### `.stMainBlockContainer` has a ~736px max-width cap
Streamlit defaults to `layout="centered"` (~736px). To fill the card edge-to-edge without requiring every widget's `app.py` to call `st.set_page_config(layout="wide")`, override `!max-w-none` + `!w-full` + `!px-4` + `!pt-4` + `!pb-4` at the panel level. This is the right place — widget authors should not have to know about the console's layout.

### Kernel options are init-only
`@stlite/react` reads `kernelOptions` exactly once on mount and exposes no `setConfig` / `setTheme`. Runtime theme or config changes require a full component remount via `key={theme}` on the inner component. Don't try to mutate the kernel's config — it won't take.

### Natural scale — no zoom, ever
Widgets render at NATURAL scale, like Streamlit in a real browser tab. An earlier revision applied `zoom: 0.85` to the stlite subtree to make widgets read "smaller than the page" — it was removed and must not come back, in either `zoom` or `transform: scale` form. Fractional CSS zoom lays glyphs out at fractional pixel positions — an effective 1.7× raster under devicePixelRatio 2 — which produced subtly irregular intra-word letter spacing: widget text looked janky next to the crisp console text, and it caused a ~1-in-5 sub-pixel border-AA jitter in demo screenshot captures. Size containment is `WidgetCard`'s job alone (its height cap, below) — never a scale hack's.

### Fonts — stlite ships Source Sans Pro, not Inter, and that's the point
`theme.font: "sans serif"` maps to Streamlit's bundled Source Sans Pro. This is the deliberate product look, not a gap to close: an embedded Streamlit app is SUPPOSED to look like Streamlit. A same-family-but-mis-metric font (Inter fighting Streamlit's own CSS) reads as "something is off"; a genuinely distinct native font reads as an intentional embed.

**Cautionary tale — the removed `useFontInjection` hook.** A previous revision injected a `<style data-stlite-font>` forcing Inter/JetBrains Mono onto `.stApp` with `!important`. It was mostly INERT: Streamlit's own stylesheet sets an explicit `font-family` on `p`/headings/captions, and an ancestor rule marked `!important` does not beat a descendant's own explicit `font-family` — visible text stayed Source Sans regardless. Only `code` chips actually flipped (to JetBrains Mono), which was an infidelity, not a fix. Don't try to CSS-override the font again — the font files are inside the Pyodide wheel, no runtime hook reaches them, and `!important` on an ancestor can't beat a descendant's own rule.

### Card height = content + chrome — measure both
`WidgetCard`'s content-height measurement used to be `.stMainBlockContainer`'s `scrollHeight` plus an 8px buffer, but `StliteWidgetPanel`'s chrome title bar (~30px, tagged `data-widget-chrome`) sits INSIDE the card, above the widget viewport. Sizing to content alone left the widget viewport ~24px shorter than its own content — permanently clipping the app's bottom padding and parking the last row's border right on the clip edge, which was the actual origin of the demo shot's bottom-band AA jitter. Fix: card height = `.stMainBlockContainer` `scrollHeight` + chrome `offsetHeight` (queried via `data-widget-chrome`) + buffer. The clamp is `clamp(120px, content+chrome+8px, min(600px, 70vh))` — 120px floor, capped at the smaller of 600px or 70% viewport height, internal scroll beyond that. One more coupling to respect: the portalled wrapper mirrors the placeholder's rect via ResizeObserver → rAF → state, so anything that freezes rAF (Playwright's paused clock does) freezes the mirror mid-flight — the demo capture flow must let widget geometry quiesce BEFORE pausing the clock (see `demo/CLAUDE.md` → widget shot traps).

### `streamlitConfig` dotted keys that matter
Passed through `useKernel` → spread into Streamlit's Python `load_config_options` in the worker:
- `client.toolbarMode: "viewer"` — hides Deploy / hamburger / Rerun band
- `ui.hideTopBar: true` — config-native removal of the "Running…" status widget + toolbar actions (it does NOT hide the hamburger — `toolbarMode` covers that half)
- `theme.base: "dark" | "light"` — Streamlit's bundled palette, flipped via `key={theme}` remount
- `theme.{background,secondaryBackground,text}Color` — override to match `--widget-panel-bg` / `--muted` / `--foreground`
- `theme.primaryColor: "#D97757"` — brand clay
- `theme.font: "sans serif"` — see above
- `theme.baseRadius: "6px"` + `theme.baseFontSize: 14` — the compact-density levers (console shape vocabulary: 6px right-side cards, 14px type). Radius is deliberately a **px string, not rem**: rem radii compute against the PAGE root font, not Streamlit's, so they drift the moment either side changes its base size. `theme.buttonRadius` was removed on purpose — `"full"` made every button a pill, which violates the "rounded-full is for state containers only" rule; unset, it falls back to baseRadius. Streamlit applies `baseFontSize` at `.stApp` (not the document root), so probing `document.documentElement` font-size to verify it is a false negative.

Belt-and-braces chrome hiding on top of `toolbarMode`: `[&_[data-testid='stHeader']]:!hidden`, `[&_[data-testid='stToolbar']]:!hidden`, `[&_[data-testid='stDecoration']]:!hidden`. Streamlit sometimes still ships the Running indicator / top gradient bar; kill them explicitly.

### Platform theming — streamlit-facade injected at the boot seam

Every widget/app gets the console's shadcn look via [streamlit-facade](https://github.com/itsdaniyalm/streamlit-facade) (pure-Python token system; version PINNED in `stliteBoot.ts` and vendored in `fetch-pyodide.mjs` — bump both together). `kernelOptionsFor` appends the pinned requirement, injects a wrapper entrypoint (facade `theme.apply(...)` with the hand-synced `FACADE_THEME` tokens, then `runpy.run_path(<real entrypoint>)` so authored files stay byte-pristine and tracebacks honest), and pre-seeds `.streamlit/config.toml`. **The wrapper takes over the AUTHORED entrypoint's path** and the authored source is relocated verbatim to a private `_mewbo_app_`-prefixed sibling — Streamlit derives the multipage sidebar's main-page label from the entrypoint FILENAME, so the earlier fixed `_mewbo_main.py` name surfaced to users as "Mewbo Main". Sibling, not a root-level name, because sibling-module imports resolve relative to the file's directory. Non-obvious laws, each paid for:

- **`theme.apply` must run in the entrypoint script, never an import-once module.** Streamlit re-executes the entrypoint each rerun but caches imports; facade's `apply` calls `st.rerun()` whenever `.streamlit/config.toml` content differs, and inside a cached import that aborts the first run and then never injects CSS again.
- **The seeded `.streamlit/config.toml` is a byte-lock against facade's `_write_config` output.** Byte-identical content → no `st.rerun()` at boot. If a facade upgrade changes its emitted format, the failure mode is one extra boot rerun (graceful) — regenerate the seed from the new `_write_config` when bumping the pin.
- **facade's font tokens are pointed at `"Source Sans Pro"`/`"Source Code Pro"` deliberately.** Unlike the removed `useFontInjection` (ancestor rule, inert), facade applies `font-family: var(--font-sans) !important` on DESCENDANT selectors, which genuinely beat Streamlit's stylesheet — left at its `system-ui` default it WOULD flip the fonts. Feeding it Streamlit's own bundled fonts keeps the native-embed look law intact. Never pass `font_link` (external Google Fonts fetch = offline-deploy break).
- **Styles born inside a widget stay inside the widget.** stlite mounts Streamlit into the host document — no iframe — so a `<style>` from `st.markdown(unsafe_allow_html=True)` applies DOCUMENT-WIDE. facade's CSS (written for real Streamlit, which owns its page) uses `body { … !important }` / `:root` globals that repainted the entire console. `stlitePanel.ts`'s `useStliteStyleScope` (same defense class as the title guard) watches the panel subtree and rewrites every injected style via CSSOM — `body`/`html`/`:root` selector heads become the `.mewbo-stlite-scope` container, everything else is prefixed. The standalone `widget-host.html` page is exempt BY DESIGN: there the page is the widget, and the "bleed" is the correct full-page theme (Aura WebView relies on it).

### Self-hosted Pyodide runtime — `scripts/fetch-pyodide.mjs` + `public/pyodide/`

**The bug this fixes:** stlite's worker hard-defaults `pyodideUrl` to `https://cdn.jsdelivr.net/pyodide/v0.29.3/full/pyodide.mjs`, and the npm `@stlite/*` packages bundle ONLY the streamlit + stlite-lib wheels — every *other* boot wheel (micropip, numpy, pandas, pillow, altair's whole subtree, starlette, …) is fetched from that CDN at runtime. So widgets silently fail to boot on any offline / LAN-only deploy. We now self-host the whole core boot: `fetch-pyodide.mjs` materializes `public/pyodide/` (Vite copies it to `dist/pyodide/`) and both surfaces pass `pyodideUrl` → the directory of that URL becomes Pyodide's `indexURL`, so the vendored `pyodide-lock.json` + wheels next to it resolve locally. Verified: a widget boots from a deep foreign subpath with **zero** requests off `127.0.0.1` (Playwright network log).

The non-obvious parts — do NOT re-derive these by trial:

- **Boot wheel set is computed, not guessed.** micropip installs streamlit's mandatory `Requires-Dist` (read from the bundled wheel's METADATA — the `BOOT_ROOTS` list) + `stlite_lib`'s dep + a worker-forced `protobuf>=7.34.1,<8`, resolving each against the lockfile. The script takes the transitive closure over the lockfile's `depends` edges. `altair` is a MANDATORY streamlit dep, so its whole subtree (jsonschema→referencing→rpds-py, narwhals, jinja2→markupsafe) is fetched at *install* time even though Python imports it lazily — it must be vendored. `pyarrow` is `micropip.add_mock_package`'d by the worker (a stub, no wheel) — do NOT vendor it.
- **Five packages can't come from the lockfile → vendored from PyPI with INJECTED lockfile entries** (`PYPI_WHEELS`): `blinker`/`itsdangerous`/`python-multipart`/`tenacity` aren't in the pyodide distribution at all, and the lockfile's `protobuf` is 6.31.1 < the forced 7.34.1. All five are pure-python `-none-any` leaves; the script pins them + sha256, downloads them, and writes synthetic lockfile entries (protobuf's REPLACES the stale one) so micropip resolves them locally and still verifies checksums.
- **Trap: key the trimmed lockfile by the ORIGINAL lockfile key, never `entry.name`.** pyodide keys packages by PEP 503 normalized name (`pillow`, `markupsafe`); the `name` field is display-cased (`Pillow`, `MarkupSafe`). micropip looks up the normalized key, so keying by `name` makes it fall through to PyPI ("Can't find a pure Python 3 wheel for pillow") for exactly the capitalized-display packages. numpy/pandas hide the bug (name == key).
- **Trap: `micropip` itself must be in the trimmed lockfile.** The worker loads it via `loadPackage("micropip")` BEFORE the streamlit install, and nothing lists it as a dependency, so it isn't in the computed closure — add it to `BOOT_ROOTS` explicitly.
- **Known limitation (documented, not a bug):** a package a widget names in `requirements` that we didn't vendor falls through to micropip/PyPI — works online, fails offline. This is why the lockfile is TRIMMED (only vendored packages): an un-vendored name is simply absent, routing cleanly to PyPI, rather than 404-ing against a local `indexURL`. We do NOT run a PyPI mirror.
- **Wiring:** `build` runs the fetch first (idempotent — a version+pins fingerprint stamp skips re-download); `postinstall` runs it lenient (`|| true`) for dev; Docker's `npm ci --ignore-scripts` skips postinstall so the `build` step covers it. `public/pyodide/` is gitignored (~26 MB of blobs). nginx caches `^(/widget-host)?/pyodide/` for a day; the SW glob deliberately excludes `.whl/.wasm/.zip/.mjs` (`vite.config.ts` also `globIgnores`s `**/pyodide/**` wholesale, since `pyodide.asm.js` is a `.js` file that would otherwise match `globPatterns` and get precached).
- **`CORE_SHA256` pins the core-runtime + lockfile fetch itself** (every wheel already verifies against `p.sha256`/`w.sha256`, but those sha256s come FROM the lockfile — an unverified lockfile fetch was the one unpinned link, and it's the load-bearing one). **Trap: `pyodide-lock.json`'s pin is NOT `sha256sum public/pyodide/pyodide-lock.json`** — that file on disk is already trimmed/rewritten (see step 5 above), so its bytes never match the raw CDN response. Pin it from the RAW fetch instead (`curl … | sha256sum`), or the check can never legitimately pass. `CORE_SHA256` is folded into `FINGERPRINT` so a pin bump alone forces re-materialization.
- **This directory used to break a full-repo `npm run lint`, and no longer does.** `public/pyodide/` is gitignored, but `.eslintrc.cjs`'s `ignorePatterns` didn't exclude it, so once `postinstall`/`fetch-pyodide` had materialized it locally a lint run reported ~330 errors and ~320 warnings (`no-undef` on `process`/`require`, `no-empty`, …) inside a vendored file that is in no diff and cannot be fixed — which made `--max-warnings=0` permanently unsatisfiable and buried the handful of real findings in `src/`. **`public` is now in `ignorePatterns`** (alongside `dist`, `dist-widget-host`, `coverage`, `test-results`): it is Vite's verbatim-copy directory, so nothing in it is authored by us or processed by the bundler. `npm run lint:ci` is expected to exit **0 with no output** — treat any output at all as caused by your diff, and do not go back to scoping eslint at individual files to dodge the noise.

### `widget-host.html` — standalone host for non-React embedders

A framework-free page (`src/widget/host.ts` + `@stlite/browser`'s `mount()`) for embedders that only have a browser context and a `postMessage` channel — chiefly the Aura Android WebView. The theme + kernel-option logic is shared with `StliteWidgetPanel` via `src/widget/stliteBoot.ts` (`buildKernelOptions`, React-free); the only per-surface inputs are `wheelUrls` (React panel = `@stlite/react/vite-utils`; host = `@stlite/browser`'s bundled wheels) and `pyodideUrl`.

**postMessage contract:** on boot the page posts `{type:"mewbo-widget-host-ready"}` to its host; the embedder posts a `message` whose `data` is `{type:"mewbo-widget-payload", payload: WidgetReadyPayload, theme?:"light"|"dark"}`. `host.ts` validates the shape defensively (a WebView shares one `message` bus) and ignores anything else silently. Re-posting a payload unmounts the running kernel and remounts (kernel options are init-only — no in-place update; each mount is a fresh Pyodide worker).

**Relocatable base — the load-bearing constraint.** The Android WebView serves this page from a synthetic origin (`https://appassets.androidplatform.net/`) at a mount path we don't control, so EVERY asset must resolve relative to the page, not an absolute origin. It is therefore a **separate Vite build** (`vite.widget-host.config.ts`, `base: './'`, output `dist/widget-host/`) — NOT a second `rollupOptions.input` in the main config. Reason: the console's build MUST stay `base: '/'` (its PWA precache + SPA sub-route hard-loads depend on absolute asset URLs — see "PWA service worker"); flipping the global base to make one entry relative would endanger that documented-fragile machinery. Keeping the widget host in its own relative-base build leaves the console byte-identical. `host.ts` resolves the vendored runtime as `new URL("./pyodide/pyodide.mjs", document.baseURI)` (the runtime is copied into `dist/widget-host/pyodide/`, so the page is self-contained); the console panel resolves `/pyodide/` (base `/`). Two physical pyodide copies in the web dist — the tradeoff for Android self-containment; de-dup later if image size bites.

Two build traps this cost real debugging — both specific to the relative-base build, neither hits the console:
- **Wheels must keep their exact PEP 427 filename.** Vite's default `-[hash]` suffix turns `streamlit-1.57.0-cp313-none-any.whl` into `…-none-any-CQR0LZsx.whl`, which micropip can't parse ("Invalid build number: cp313"). `assetFileNames` keeps `.whl` names unhashed (they're already version-stamped → still cache-safe).
- **One `@stlite/browser` wasm needs a manual copy.** Its kernel references `new URL("./assets/<hash>.wasm", "" + import.meta.url)`; the `"" +` defeats Vite's static asset-URL detection under relative base, so the file isn't emitted and the literal resolves to `assets/assets/<hash>.wasm` (relative to a chunk already in `assets/`). The `copyStliteWasmAssets` plugin copies `@stlite/browser/build/assets/*.wasm` there. Fonts in the same upstream dir come through `stlite.css` `url(...)` (which Vite DOES rewrite), so only wasm needs copying. `stlite.css` itself is imported by relative filesystem path — `@stlite/browser`'s `exports` map blocks the `build/stlite.css` subpath under Vite 8's strict enforcement.

## Retry / branch / fork — the turn recovery actions (`ConversationTimeline`'s "⋯" menu)

Three actions live in the assistant-turn overflow menu and look interchangeable — they are not; the label and the wire body must stay in lockstep, and the vocabulary is shared **verbatim** with the Aura Android client's long-press sheet, so renaming any of these three strings here is a cross-surface break, not a local one.

- **"Retry from here"** → `POST /api/sessions/<id>/recover {action:"retry", from_ts}`. SAME session; the server DESTRUCTIVELY TRUNCATES the transcript at `from_ts` before re-running. This is a rewind, not a redo — every turn after the anchor is deleted, not just re-answered.
- **"Branch in new chat"** → `POST /api/sessions/<id>/fork {from_ts}`. NEW session, transcript copied up to that message; the source session is untouched.
- **"Fork session"** → `POST /api/sessions/<id>/fork {}` (no `from_ts`). NEW session, WHOLE transcript copied; source untouched.

The trap this fixed: the item labeled "Fork session" used to pass `from_ts` — the label lied, it was actually a branch. Renamed to "Branch in new chat"; "Fork session" is now the genuine whole-transcript fork (`onForkSession` prop, threaded through `AssistantTurnFooter`).

- **`ts` IS the message id.** There is no event-id field anywhere in this wire contract — the event timestamp (`turn.events[0].ts`) is the only addressable anchor for both `from_ts` params. Don't invent an id field.
- **A 201 that never navigates is a silent no-op.** Both fork paths (`SessionDetailView.handleFork(fromTs?)` → `useForkSession`) MUST call `onSelectSession(result.session_id)` on success — navigating to the new session is the feature, not a nicety. The prior `handleForkFrom` returned 201 correctly but swallowed errors behind a comment claiming "notification will surface via API" — it never did. `useForkSession` (`hooks/useForkSession.ts`, mirrors `useRecoverSession.ts`) now `toast.error(...)`s on failure and is the ONE fork call path for both menu items — don't hand-roll a second one per item.

### Session-level recovery — `RunFailedCard` and the `run_failed` role

The turn-level actions above are per-message rewinds. Session-level Retry/Continue (`POST /recover {action}`, no `from_ts`) is a different affordance and lives on the failure card.

- **Recovery lives in the COLLAPSED header; the body is bounded and collapsed by default.** This is the inversion that matters: the trace card previously FORCE-OPENED on error via `defaultExpanded`, which is exactly backwards for a wall of provider markup. Retrying must never require expanding the error first. The body is `max-h-[300px]` and scrolls on the code-surface tokens — the successful-tool-output card two functions away had been capped for ages while the error path never was. Every control in the header must `stopPropagation()`, because `LogEventCard`'s root toggles expansion on click.
- **`run_failed` is a real `TimelineEntry` role, not an assistant bubble.** A failed run used to be faked as assistant text reading "(run interrupted — see logs)", which pointed the user at another panel instead of showing the error in place. A real role renders it chronologically, which matters because one session can hold several failures.
- **Recovery is offered on the LATEST failure only, and "latest" is resolved by scanning back to the first CLOSING entry — not by taking the last failure in the list.** A failure followed by a successful turn must offer nothing, because the session already recovered; picking the last failure would resurrect a dead affordance mid-transcript. Every other failure card renders read-only. `SessionDetailView` scans the timeline for the newest `run_failed` and bails on an `assistant` entry; `LogsView` does the equivalent over `logs`, stopping at the newest `completion` whatever its outcome.
- **The header recovery strip narrows to the one case no card can cover**: `session.recoverable` with NO completion event at all — the process-killed case. When a live failure card is on screen it defers, so there is exactly one affordance.
- **Recovery carries a deliberately chosen model, and every recover call passes one.** `recoverSession`/`useRecoverSession` already accept `model`; the callers now always fill it. Header + row affordances pass the session's own model (`effectiveContext?.model` / `session.context?.model`) — omitting it lets the server fall back to config policy, so a recovery would silently run on a DIFFERENT model than the turn it recovers. The failure card mounts the extracted `ModelFallbackChain`'s sibling picker (`ModelPicker`, compact) and DEFAULTS it: to the next untried rung of `[primary, ...ladder]` when `failure.failureReason` is model-attributable (`timeout`/`rate_limit`/`server_error`/`bad_gateway`/`connection`/`quota_exhausted`/`auth`/`no_deployments`/`invalid_model`), and to the SAME model otherwise — a deterministic failure (`bad_request`/`content_policy`/`permission_denied`) reproduces everywhere, so switching there hides a real defect. It is always a DEFAULT the user overrides, never a forced switch; picking "Session default" clears the override back to the persisted model. `models_tried` renders as an always-visible `belowHeader` strip so the choice is informed. `failureReason`/`modelsTried` ride `RunFailureMeta` (mirrored in `types.ts`, parsed in `parseRunFailure`) — the classifier reason is surfaced on the record, never re-derived in the FE.
- **`parseRunFailure` (`utils/logs.ts`) is the single parse seam**, consumed by both `buildLogs` and `buildTimeline`, so the conversation card and the trace card cannot disagree about what failed. It treats the payload as untrusted: an unrecognised `kind` coerces to `"unknown"` rather than being trusted into the union, and a `detail_chars` smaller than the string it describes is rejected so the truncation footer can never state a bogus size. `error_detail` is absent on every event stored before it shipped and the session page replays FULL history, so the parser falls back to `error`/`last_error` and simply omits the title, provider and truncation chips — never gate the card on the new key.
- **What counts as a failure is broader than `done_reason ∈ {error, max_steps_reached}`, and getting this wrong laundered failures into green success cards.** `unmet_goal`, `verification_failed` and `halted_no_progress` all mean "ended without achieving the goal" and map to the `unmet_goal` reason; before they were recognised they rendered as an ordinary assistant bubble with no warning and no recovery. Worse, a **blocked** run — one that hit a wall it can't clear on its own (repository access, a network path, a permission, quota) — deliberately keeps `done_reason: "completed"` and carries the fact out ONLY as a top-level `blocked_code`, so gating on `done_reason` rendered it as a green "Run completed" card. `parseRunFailure` therefore consults `blocked_code` independently of `done_reason` and lets it win when both are present (a wall the user can act on is the more useful classification), surfacing it as `RunFailureMeta.blockedCode` so the card names what to fix. Recognition here is what makes the whole failure path — the `run_failed` role, the recovery affordance, the model picker — reachable at all; fixing it in this one function fixes both surfaces, and `StatusBadge` is the only other place that must know this vocabulary (the session pill; `types.ts` keeps `status`/`done_reason` open strings on purpose so a new backend status renders a badge rather than failing the build).
- **The THIRD unmet path lives in a SEPARATE event, and `parseRunFailure` structurally cannot see it.** A session-end hook (the wiki "job never reached its terminal tool" majority case) leaves the completion's `done_reason` at `completed` and appends a distinct `outcome_assertion` event (`{reason, detail, source}`) right after the completion it judges. Reading the completion payload alone, `parseRunFailure` returns null, so the association is the BUILDER's job, not the parser's: `parseOutcomeAssertion` (`utils/logs.ts`) turns the event into a `unmet_goal` `RunFailureMeta` (its `reason` token → `failureReason`, `detail` → body), and BOTH builders attach it to the preceding completion — `buildLogs` walks back to the most recent completion row, `buildTimeline` keeps a one-shot `lastCompletion {index, synthetic}` pointer set in both its completion seams. **`synthetic` is captured at the source, never re-derived, because the two seams mint different placeholder texts** (`(Run …)` capital-R via the orchestrator's closure vs the builder's own lowercase `(run ended)`) and only the first matches `SYNTHETIC_CLOSURE`. A placeholder is replaced in place; a REAL answer that missed its goal is preserved with the verdict pushed BESIDE it (same non-destructive rule as the failure-completion seam). A completion already classified as a failure keeps its more-specific read — the assertion only upgrades a would-be-`assistant` turn. **Known residual (not fixed here):** the Python `TranscriptTimeline` (the MCP timeline surface) does not consume `outcome_assertion`, so an MCP consumer still sees a clean completion for this case — a separate surface, owned server-side.

## Triggers + terminated sessions — `components/triggers/`

Reverse-invocation wakes (`time.at`/`time.cron`/`ci.workflow`/`forge.pr`/`webhook`) and permanent session termination, both built against a **FROZEN REST contract** (routes + `TriggerDTO` + PATCH pause/resume) before the backend `TriggerService` landed. `api/triggers.ts` mirrors the `api/git.ts` seam — typed DTOs over the shared `httpBase` quartet, no third fetch wrapper — and every closed union (`TriggerKind`/`TriggerStatus`/`TriggerAction`) is exhaustively mapped in `components/triggers/triggerFormat.ts` so `tsc` flags drift the moment the contract grows. Until the backend ships, empty lists and 404s degrade gracefully by design — don't "fix" the graceful-empty states, they're the point.

- **Naming collision — never conflate these two "stop"s.** The composer's **Stop** button (`InputComposerBody.StopWithConfirm`) sends the `/terminate` *slash command* through `useSessionQuery.stop()` → `postQuery(sessionId, "/terminate")` — it stops the **RUN**, the session lives on. The session-menu **"Terminate session"** item (`NavBar.tsx` → `TerminateSessionDialog`) calls `POST /api/sessions/{id}/terminate` — it permanently kills the **SESSION**. Same word, two different destructive actions at two different layers; a reviewer seeing "terminate" in a diff must check which one.
- **`isTerminated` is a three-signal OR, and all three must stay in lockstep with the wire strings.** `SessionDetailView.tsx` computes it from `session.status === "terminated"` OR a `session_terminated` transcript event OR `queryTerminated` (a 410 caught by `useSessionQuery`'s send/stop mutations). Any surface that reads only one signal (e.g. `NavBar`'s own `isTerminated = session?.status === 'terminated'`, used just to hide the menu item) is deliberately narrower — don't hoist it to the three-signal definition without checking why it was narrow.
- **410 handling has one shape.** `api/triggers.ts:readMutation` maps a 410 body `{error:{code:"session_terminated"}}` to the typed `SessionTerminatedError`; `isSessionTerminatedError()` also parses the stringified-JSON message that `httpBase`-routed mutations produce, so both fetch paths trip the same guard. Callers (`useSessionQuery`, `TerminateSessionDialog`) catch it and flip to the calm terminated state — never surface it via the red `error` string. `InputBar`'s `terminated` prop replaces the *entire* composer with a static "permanently terminated" row (no Stop, no input) — this is the no-error-residue rule from the compact design language, applied to a whole-composer swap instead of a toast.
- **The open-turn gate is the timeline's real seam, and the Python port trails it.** `utils/timeline.ts:buildTimeline` walks events carrying a `currentTurnId`; every role folded after its `if (!currentTurnId) continue` gate is silently DROPPED when no turn is open. Roles that legitimately arrive BETWEEN turns are therefore parsed ABOVE the gate: `trigger_armed`/`trigger_fired` (`"trigger"`), `session_terminated`, `recovery` (a user-driven retry/continue, which the runtime records *only* between turns), and the failure `completion` that upgrades an already-closed turn. Roles handled BELOW it — `plan`, `todos`, `widget`, `question` — are mid-run by nature and are *meant* to be dropped when no turn is open. **`todos` is not the precedent for hoisting anything: it sits inside the gate.** A turn still open when the next `user` event arrives is flushed as a `run_failed` entry with reason `"interrupted"` rather than discarded with its body; the trailing open turn is deliberately exempt so the live `PendingAssistantRow`/`StreamingAssistantRow` keep rendering. **The Python side is no longer a second implementation.** `mewbo_core.transcript_timeline.TranscriptTimeline` is the canonical assembler — events in, turns out, zero I/O — and `apps/mewbo_mcp/src/mewbo_mcp/timeline.py` is a thin delegate over it, so the roles an MCP consumer sees are the same set this file emits (`plan`, `todos`, `widget`, `question` and `run_failed` included; the old gaps are closed). **`buildTimeline` here is still a separate implementation, and that is the drift risk to respect.** `tests/fixtures/transcript_timeline_corpus.json` is the shared contract both sides read off disk — never copy it, since two fixtures drift as easily as two implementations — and it is written from the documented rules rather than dumped from either side, because an expectation generated from the code under test only proves the code equals itself. Adding a role here means adding a corpus case and teaching the canonical assembler, in the same change. One asymmetry to know: the Python parity test also asserts every role it can emit HAS a case, while this side cannot make that claim (the role union is compile-time only), so a new role added here alone ships green.
- **A failure completion may only blank a closure that matches the synthetic shape.** `done_reason` decides *whether* a run failed, but whether the already-closed turn's bubble is disposable is decided by matching its text against the orchestrator's `(Run …)` closure shape — the one place that text is read, and it has no substitute signal. The API's boot sweep appends its own terminal completion to an orphaned run, so a session that died between a REAL assistant event and its completion gets a failure landing on genuine prose; blanking it destroys the only copy of the answer. Don't "simplify" that check away on the grounds that `done_reason` is the authority.
- **The dashboard is a Settings pane, not a page.** It lives at `settings/panes/TriggersPane.tsx` in the **Automation** facet, directly above the schema-driven "Triggers" section that holds the backend policy ceilings (max armed per session, expiry, cron minimum interval) — the surface and the knobs that govern it on one page. `/triggers` redirects here; the `?session=` deep link survives the hop (see "Navigation & information architecture"). **Its help copy is the reference implementation of the copy standard** (see "Settings — faceted shell over RJSF"): the visible line says what a trigger *is*, and the popover teaches the concept end to end — reverse invocation, the five kinds in plain language (`time.at` once at a set moment · `time.cron` on a repeating schedule · `ci.workflow` when a CI run finishes · `forge.pr` when a PR changes · `webhook` when something outside calls in), that the AGENT arms them from inside a session so this page is only for watching/pausing/cancelling, and that the ceilings live in the Triggers section below. Don't compress it back into a dash-separated list of clauses; that is the exact regression this replaced.
- **Filtering is client-side over one `limit=200` fetch** (`TriggersPane.tsx`) — a trigger dashboard is small enough that local filtering across kind/status/session beats a refetch per toggle, and it lets the filter dropdowns only offer values actually present. The server's `?kind`/`status`/`session_id` query params (already wired in `listTriggers`) are the documented escalation seam once volume outgrows one page — reach for those before inventing a second filtering path.
- **Optimistic mutations patch every cached `['triggers']` list, not just the active query.** `patchTriggerStatus` in `hooks/useTriggers.ts` walks `qc.getQueriesData({queryKey: TRIGGERS_ROOT})` and rewrites the matching row in each — the management page's filtered list AND any per-session `SessionTriggersSection` query — with a snapshot for rollback on error and an `invalidateQueries` on settle. Adding a new trigger-reading query means it inherits this for free (it's keyed under the same `["triggers", …]` prefix); don't hand-roll a second cache-patch path for a new consumer.

## PWA service worker — deploy resilience

Config lives in `vite.config.ts` under `VitePWA({ workbox: { ... } })`; the update UX lives in `components/UpdatePrompt.tsx` (registered in `src/index.tsx`). Non-trivial rules that MUST hold:

- **`skipWaiting: true` + `clientsClaim: true`.** New SW activates immediately and claims all clients. The opposite config leaves a newly-installed SW in "waiting" state until every open tab closes, which in practice leaves users pinned to the previous deploy's precached `index.html`. That stale `index.html` references hashed bundle names that the server has since deleted → 404 cascade on every lazy chunk → widgets silently fail to mount. This has bitten us hard.
- **Large lazy chunks are in `globIgnores`** (`stlite-*.js` — every chunk containing @stlite prebuilt modules gets that deterministic prefix via `build.rollupOptions.output.chunkFileNames` in `vite.config.ts`) because the big ones exceed Workbox's 2 MiB precache limit. They're served network-only. Never globIgnore by upstream chunk name (`PlotlyChart-*`, `DeckGlJsonChart-*`) — those names and hashes drift with every stlite upgrade and bundler chunking change. This HAS to pair with `skipWaiting: true` — stale index.html referencing missing hashed chunks = 404.
- **`sw.js` must be `Cache-Control: no-cache, no-store, must-revalidate`** at nginx (`docker/nginx-console.conf`). If the browser caches `sw.js`, it never sees new versions.
- **`UpdatePrompt.handleReload` is deliberately nuclear**: unregisters every SW via `navigator.serviceWorker.getRegistrations()` + wipes all `caches.keys()` entries, THEN `location.reload()`. Do not trust workbox's `updateServiceWorker(true)` skipWaiting+controllerchange handshake alone — browsers land in weird precache states often enough that brute-force is the only consistent fix.
- **`registerType: "prompt"` + 15-min `registration.update()` tick** (was 60 min). Also re-ticks on `window.focus`. Users see the update notification within ~15 min of a deploy instead of up to an hour.

### Deploy symptom → likely cause
| Symptom | Likely cause |
|---|---|
| Widget lazy chunk 404s after deploy | Stale SW pinned to old `index.html`; user dismissed or never saw the Reload prompt |
| Users refresh, still see old UI | Old SW never unregistered — force via Reload button (nuclear path) or DevTools → Application → Unregister |
| `curl -sk /index.html` returns fresh, browser runs stale | SW's precache is serving stale `index.html` BEFORE the network layer sees the request |

## Dependency Selection — KISS/DRY Policy

The instinct in this codebase is **always reach for an external library before writing custom code**. The whole point of the foundation reset was to delete bespoke implementations of problems that were already solved upstream. Custom code is only justified when no library fits.

Before adding (or writing custom code in place of) any dependency, walk this checklist in order:

0. **Have you exhausted shadcn first?** Re-read the Library-First Principle. A new "form input" or "modal" or "menu" almost certainly already exists as a shadcn primitive or block. The answer to "should I write this?" is usually "no — install the shadcn version".
1. **Read the actual installed version's API** — not blog posts, not older docs. Run `node -e "console.log(Object.keys(require('pkg')))"` or read `node_modules/pkg/dist/*.d.ts` to confirm exports and prop names. Major versions often rename everything.
2. **Check units and defaults** — numeric props may mean pixels in one version and percentages in another. Always confirm from the type definitions what a bare number means.
3. **Prefer rehype/remark plugins over component-level libraries** — AST-level plugins (e.g., `rehype-highlight`) integrate with one prop addition and don't require component overrides. Component-level libraries (e.g., `react-syntax-highlighter`) require custom wiring, are harder to maintain, and tend to be larger.
4. **Verify bundle size claims yourself** — bundlephobia numbers and blog claims are often stale or wrong. Check the actual package after install.
5. **Prefer the library that the rest of the stack already endorses.** If shadcn picks `cmdk` for command palettes and `sonner` for toasts, use those — don't introduce a parallel choice.
6. **One library per concern.** If TanStack Query owns server state, do not add SWR. If wouter owns routing, do not add react-router. If react-hook-form owns forms, do not add Formik. Mixed stacks double the surface area for bugs and onboarding.

### Current dependency rationale

| Package | Purpose | Why this one (not alternatives) |
|---------|---------|-------------------------------|
| `@tanstack/react-query` v5 | Server-state cache, polling, invalidation | Replaces ~900 LOC of custom hooks + TTL cache. Function-form `refetchInterval` handles conditional polling cleanly. |
| `wouter` v3 | Client routing | ~1.5 KB; matches our handful of routes; no pre-built router opinions to fight. Rejected react-router for size + complexity. |
| `react-hook-form` + `zod` | Static forms with validation | Standard React form stack; tiny runtime; integrates with shadcn `Form`. RJSF stays only for `SettingsView`'s dynamic backend schema. |
| `class-variance-authority` (cva) + `tailwind-merge` | Variant components + class dedup | Required by shadcn primitives. `cn()` in `lib/utils.ts` combines `clsx` + `tailwind-merge`. |
| `react-markdown` + `remark-gfm` | Markdown rendering | Renders to React elements (no `dangerouslySetInnerHTML` / XSS risk). Standard for React chat UIs. |
| `rehype-highlight` | Syntax highlighting | One-line rehype plugin integration (~50 KB gz, 37 languages). Rejected: `react-syntax-highlighter` (75-495 KB, not tree-shakeable, 138 open issues), Shiki (async init, complex setup), `prism-react-renderer` (low maintenance). |
| `react-resizable-panels` v4 | Draggable split pane | Zero deps, ~7.7 KB gz, built-in ARIA/keyboard accessibility, by Brian Vaughn (React core team). Rejected: `allotment` (heavier, 82 issues), custom impl (loses a11y). **v4 API:** exports are `Group`, `Panel`, `Separator` (not `PanelGroup`/`PanelResizeHandle`). Prop `orientation` (not `direction`). Numeric sizes = **pixels**; use strings for percentages (`"40%"`, not `40`). |
| `react-force-graph-3d` (+ `three`) | 3D WebGL graph (`Graph3DView`) | The one graph engine — wiki Knowledge Graph AND Agentic-Search SCG share it. Replaced the Cytoscape/`cytoscape-fcose` 2D engine, whose main-thread force-sim rendered inconsistently and choked past a few thousand nodes (real graphs hit ~17K/68K). Off-the-shelf scene/camera/picking/labels — no bespoke three.js wrapper; the GPU/WebGL build is what clears the 2D-canvas wall. Lazy-loaded at the render site (heavy chunk). |
| `@assistant-ui/react` (+ `-markdown`) | Chat-surface primitives: composer, thread list, thread runtime | Production-grade vendor for the chat surface (see "assistant-ui" section) — replaces bespoke composer/rail code and offloads maintenance upstream. `useExternalStoreRuntime` binds our existing SSE/TanStack state with zero networking opinions. `zustand` rides along as ITS internal dep only — still forbidden for app state. |
| `@uiw/react-codemirror` (+ `@codemirror/lang-markdown`, `@codemirror/legacy-modes`, `@codemirror/language`, `@codemirror/state`, `@codemirror/view`, `@lezer/highlight`) | Code editor for `SystemInstructionsPane` (custom system-instructions Jinja template) | The console had no code editor (rehype-highlight is read-only rendering; the "Web IDE" is a redirect to an external container). CodeMirror 6 is the standard React-ecosystem choice — `@uiw/react-codemirror` owns mount/unmount/state-sync so we don't hand-roll an `EditorView` lifecycle effect (the exact anti-pattern this checklist forbids). There is no dedicated CM6 Jinja package; `@codemirror/legacy-modes/mode/jinja2` (confirmed via its installed `.d.ts`: a standalone `StreamParser`, no backdrop language) is hand-driven via the public `StringStream` API inside a `ViewPlugin` decoration layer to color Jinja tags on top of `@codemirror/lang-markdown`'s prose grammar — see `settings/panes/systemInstructionsCodeMirror.ts` for why that composition, not a single combined grammar. Colors reuse the existing `--hl-*`/`--code-*` token family (`.cm-jinja-*` rules in `index.css`), so no new theme tokens and free light/dark. |
| `@codemirror/autocomplete` | Variable/value completion inside the Jinja editor | **It was already in the tree** (a transitive of `basicSetup` / `lang-markdown`), and importing it directly makes it a DECLARED dep — an undeclared transitive is a break waiting for a hoist change. The pane turns `basicSetup`'s own `autocompletion` **OFF** and owns the extension itself, so there is exactly ONE autocompletion config in the editor and no dependence on how two would merge (basicSetup's copy has no sources anyway). The completion source is built from the SAME `/variables` payload that feeds the reference table — variable names, types, descriptions, and per-variable candidate values — so **nothing about the variable set is written down in the console.** It deliberately does NOT parse Jinja: it scans raw text around the cursor for `{{ }}`/`{% %}` and quoted strings, and infers the owning variable from the handful of comparison shapes an operator actually types; a cursor it cannot place degrades to offering EVERY candidate value tagged with its owning variable, which is still useful and can never be *wrong*. A grammar would be a lot of code to be occasionally more precise about a settings-pane convenience. Two traps it brings: ⚠️ feed it a **referentially stable** variables array (`useSystemInstructionsVariables` returns one shared `NO_VARIABLES` while the query is in flight) — a fresh `[]` literal per render is a new identity, so the `useMemo` that builds the extension set would reconfigure the editor on every render; and ⚠️ CM6 measures character size by calling `Range.getClientRects()` from a `requestAnimationFrame`, which **jsdom does not implement**, so the throw lands as an UNHANDLED error *after* the test has already passed (`setupTests.ts` stubs it to empty rects — the honest answer in a DOM with no layout, which CM6 reads as "not measurable yet"). |

## Tool Card Components — DRY/KISS Rules

Tool-call events render in `LogsView` as cards. The component hierarchy is intentional — follow it:

```
LogEventCard (base: rounded-md + border-l-[2.5px], expand/collapse, icon/title/badge header)
├── Used by: renderPermission, renderAgent, renderAgentResult, renderCompletion, renderShell fallback, renderReflection
└── Compose this for new tool types unless the visual structure is fundamentally different

TerminalCard (specialized: always-dark bg, window chrome, $ prompt, stdout/stderr)
└── Used by: renderShell when structured shell data is present

CopyButton (shared: copy-to-clipboard with icon-swap feedback)
└── Used by: TerminalCard, MessageBubble, ConversationTimeline
```

**Shape vocabulary**: log cards use the right-side instrument-panel language — `rounded-md` (6 px) bodies with a `2.5 px` brand-tinted left rail (`border-l-[2.5px]`) and `pl-[14px] pr-3 py-[9px]` head padding. The 2.5 px rail is the sole signature on the right surface; **don't** add it to non-log entries (turn footers, spinners, separators) — when the rail is everywhere it stops identifying anything (P11 in the design philosophy).

`TerminalCard`'s `10 px` outer radius is a deliberate macOS-window-chrome exception; keep it.

**Rules when adding new tool card types:**

1. **Compose `LogEventCard` first.** It handles expand/collapse, accent borders, and the standard header layout. Only create a specialized component when the visual structure is genuinely different (like TerminalCard's dark background and window chrome).

2. **Never duplicate behavioral components inline.** If you need a copy button, import `CopyButton` from `components/CopyButton.tsx`. If you need a badge, use the `Badge` function in `LogsView`. Do not redefine these patterns inline.
   The raw clipboard helper behind it is `copyText` (`src/utils/clipboard.ts`) — reach for it only when `CopyButton` genuinely can't render (no UI surface).

3. **Styling goes in `className`, behavior goes in the component.** Shared components like `CopyButton` accept `className` for visual customization and `children` for additional content (labels, etc.). They own the behavioral logic (clipboard API, state feedback, event handling).

4. **Keep tool-specific helpers colocated.** Functions like `formatDuration()`, `shortenCwd()` in `TerminalCard` are used only by that component — keep them in the same file. Only extract to `utils/` when a second consumer appears.

5. **Parse structured tool data in `buildLogs()`, not in components.** The `utils/logs.ts` parser extracts typed fields from event payloads. Components receive clean props, never raw JSON.

## CSS / Component Pitfalls — Lessons Learned

### `font-sans` beats `body { font-family }` — brand stacks live in tailwind.config
Tailwind's default `font-sans` stack is `ui-sans-serif, system-ui, …` — NOT Inter. The app-root layout div carries `font-sans`, so before `tailwind.config.js` gained `theme.extend.fontFamily` (sans = Inter, mono = JetBrains Mono) the entire console silently rendered in the system font while `index.css`'s `body { font-family: 'Inter' … }` rule sat defeated underneath. If a surface ever renders the wrong font, check for a `font-sans`/`font-mono` utility resolving to Tailwind defaults before touching CSS — the config stacks must stay mirrored with the `index.css` body/code rules.

### Never nest duplicate width constraints
A `max-w-[70%]` inside another `max-w-[70%]` compounds to ~49%. Width constraints must live in exactly one place (DRY). The parent container owns the constraint; child components fill their parent.

### Markdown `components` map belongs outside the render function
Define `const markdownComponents = { ... }` as a module-level constant, not inside a component body. This avoids recreating the object on every render and prevents unnecessary ReactMarkdown re-renders.

### rehype-highlight + custom `code` component coexistence
`rehype-highlight` adds `hljs` / `language-*` classes to `<code>` inside `<pre>`. The custom `code` component must detect these classes and pass through (not restyle as inline code). Pattern:
```tsx
code: ({ className, ...props }) => {
  if (className?.startsWith('hljs') || className?.startsWith('language-')) {
    return <code className={className} {...props} />;
  }
  return <code className="inline-code-styles" {...props} />;
}
```
The `pre` component uses `[&_code.hljs]:bg-transparent [&_code.hljs]:p-0` to prevent double-background on highlighted blocks.

### `cn()` is tailwind-merge-aware — later classes win
`cn()` in `src/lib/utils.ts` uses `clsx` + `tailwind-merge`. Conflicting Tailwind utilities are deduplicated **with later-wins semantics**: `cn("p-2 text-sm", "p-4")` resolves to `"text-sm p-4"`. This is what you want 99% of the time, but it changes the older "concat-only" behavior — if you previously relied on order to keep an early class, you must now move the precedence-bearing class to the end. When migrating an existing component that used the old `cn`, scan for places that pass conditional overrides and verify they still win.

### ⚠️ `shadow-[var(--elev-N)]` silently renders NO shadow — use `[box-shadow:var(--elev-N)]`

`shadow-` is the prefix for **two** Tailwind utilities (`boxShadow` and `boxShadowColor`), so a bare `var()` arbitrary value is ambiguous and Tailwind v3 resolves it to the **color** one. Verified against the installed compiler (3.4.17): `shadow-[var(--elev-1)]` emits `--tw-shadow-color: var(--elev-1); --tw-shadow: var(--tw-shadow-colored)` and **no `box-shadow` property at all** — and since preflight defaults `--tw-shadow-colored` to `0 0 #0000`, the result is a fully transparent non-shadow. No error, no warning, nothing painted.

Two forms actually work: `[box-shadow:var(--elev-2)]` (arbitrary property, what `composerCard()` in `InputBar.tsx:41` and `composerSurface()` in `ui/composer-shell.tsx` both use) or the type-hinted `shadow-[shadow:var(--elev-3)]`. Prefer the first — it reads as what it is.

**Swept clean: zero broken call sites remain.** The tree previously carried ~30 `shadow-[var(--elev-N)]` sites against 2 working ones, so most elevation in agentic_search, apps, and wiki was inert; every site was converted to the arbitrary-property form. The rule still stands going forward — **don't copy an existing `shadow-[...]` call site as a template without checking which form it uses**, and a bare `shadow-[var(--elev-N)]` reintroduced anywhere is a review reject. **Grep trap:** a template literal like `` `shadow-[var(--${tier})]` `` never contains the literal substring `--elev`, so a grep anchored on `shadow-[var(--elev` misses it — grep the broader `shadow-[var(` prefix too when auditing.

### ⚠️ A key shared by `fontSize` and `colors` collides — `text-input` silently sets colour, not size

Tailwind derives a `text-*` utility from BOTH `theme.extend.fontSize` (font size) and `theme.extend.colors` (text colour) — the same class prefix serves two different theme sections, and a key present in both compiles to the same utility twice, with whichever rule is emitted later in the stylesheet winning. `colors.input` already existed (shadcn's border token), so adding a same-named `fontSize.input` for the 16px iOS floor (see "Typography" above) would have generated `.text-input` twice; the colour rule wins, and `.text-input` sets `color: hsl(var(--input))` — the dim border colour — instead of a font size. Typed text in every composer and every shadcn `<Input>` would have rendered nearly invisible, with no build error and no signal until someone tried to read what they'd typed. `cn()` doesn't rescue it either: tailwind-merge classifies `text-input` as a colour utility, so it drops the foreground class, not the size one, when both are present. The floor class is named `text-field` instead — no collision, verified as a single font-size rule in the compiled output. Before adding a key to `fontSize` or any theme section, check whether the same key already exists under a different section first.

### Theming — never hardcode colors that should adapt

The app supports light and dark mode via CSS variables in `src/index.css` (the `:root` block defines dark, the `.light` block defines light; the `<html>` element gets the `light` class toggled). Every color in a component must come from a CSS variable so it adapts automatically.

**Palette identity: "Anthropic cream (light) / warm carbon (dark) with clay primary" — shared with docs.mewbo.com.** The dark `:root` block is a value-port of the docs theme's `.dark` tokens (the brand-forked `mkdocs-shadcn-mewbo` package's `mewbo.css`; installed copy under `.venv/.../site-packages/shadcn/css/`): warm hue-40 carbon, bg `40 6% 8%` (#161513), card `40 5% 12%`, whisper-subtle borders, one clay accent. Light was already byte-identical to docs and stays untouched. Things we deliberately did NOT port from docs: its cool `#0d1117` code-block background (docs' own documented inconsistency — our `--code-*` family stays in the warm-carbon family), its Geist fonts (the console tried Geist and reverted to Inter + JetBrains Mono — see "Typography" above for why), and its 10px radius (our 8px shape vocabulary stands). The console's translucent `--border` (0.15α of `55 9% 74%`) blends to ≈ the docs' opaque 17%-lightness border on the carbon bg — that's why it survived the port unchanged; don't "fix" it to an opaque value.

**Theme persistence:** the choice persists in localStorage key `mewbo:theme` (dark default; only an exact `'light'` restores light). Ordering is load-bearing: `toggleTheme` mutates the `<html>` class **synchronously before** dispatching `wiki:theme-change`, and Mermaid/graph consumers live-read CSS vars at render time — reorder that and diagrams repaint with the outgoing theme's colors. The `[theme]`-dep effect in `App.tsx` is an idempotent re-assert for the persisted initial value, not the primary sync.

**Status semantics have tokens — use them.** `--success` / `--warning` / `--info` / `--destructive` are GitHub-semantic hues (green/amber/blue/red), aligned with the docs site's admonition + endpoint-badge palette, per-theme tuned. Three of them also carry a `-text` variant — `--destructive-text`, `--warning-text`, `--info-text` — the text-on-tint reading for a status WORD sitting on its own tint fill, tuned to clear AA on that tint (the same `-text` convention as `--diff-add-text`): reach for the plain token for a dot or fill, the `-text` variant for a label set against the tint. Every run-state/status color (running, completed, failed, warning, pending, error) MUST come from these four — raw `emerald-500`/`cyan-600`/`amber-500`/`red-500` status classes are a review reject now that `STATUS_STYLES` (`utils/agentStatus.ts`), `BADGE_COLOR_MAP` (`utils/agents.ts`) and `StatusBadge` are token-based. The sanctioned raw exceptions are **identity hues only**: origin/kind chips that need side-by-side distinctness (`cyan` search/webhook vs `--info` blue, `violet` structured, `teal` draft, `lime` PR-open, `stone` idle — collapsing any onto a neighbouring token would erase a visible distinction), the `--agent-N` cycling slots, and platform brand icons.

**The distinguishing test: is the colour gated on run state? Then it is state, not identity.** `TerminalCard`'s macOS traffic-light dots used to be listed here as an identity hue and no longer are — they are gated on `isError`, the same boolean already driving the tokenised styling elsewhere in that component, which makes them a run-state readout wearing a familiar shape. Real traffic lights are a fixed red/amber/green triple regardless of window state; ours are not. **The identity signature is the SHAPE** — three `rounded-full` dots at a fixed size and gap — and that is untouched; only the state-driven colour moved onto `--destructive`/`--success`. Apply the same test before claiming a new exception: a hue that changes with what the run is doing is status, and status has tokens.

**Forbidden patterns** — these will look correct in one theme and broken in the other:

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

**⚠️ Some tokens already embed their alpha — appending `/NN` produces invalid CSS.** `--border`, `--border-strong`, `--code-border`, `--rail-border`, `--scrollbar-thumb` and `--scrollbar-thumb-hover` are declared as `H S% L% / A` (e.g. `55 9% 74% / 0.15`), so `hsl(var(--border) / 0.5)` expands to a four-value `hsl()` with two slashes: the browser drops the declaration and the element falls back to `currentColor`, which reads as a near-white hairline in dark mode. Worked example: `.session-ts-rail`'s backplate wrote `box-shadow` off `hsl(var(--border-strong) / 0.55)` — the double-slash value made the whole `box-shadow` declaration invalid, so the browser dropped it entirely rather than partially applying it, and the TurnScroller backplate rendered with no ring and no shadow in either theme, with nothing in the console pointing at why. Alphaless tokens (`--destructive`, `--success`, `--primary`, the `--code-fg-*` family) legitimately take a slash — `bg-[hsl(var(--destructive)/0.3)]` is correct. Check the token's declaration in `index.css` before reaching for a slash.

**Rule:** if you reach for a Tailwind color name (`emerald`, `red`, `zinc`, `slate`, `white`, `black`) or a literal `hsl(...)` outside `src/index.css`, stop and ask: *"is this an inherently brand color (a logo, a status indicator that means the same thing in both themes), or am I about to hardcode a theme-specific color?"* If the latter, use a CSS variable. If you can't find one that fits, **add a new variable to both `:root` and `.light` in `src/index.css`**, then use it. Do not bypass the variable system to "just pick a color quickly".

**Adding a new theme token (the only correct way to introduce a new color):**

1. Open `src/index.css`. Add the token under `:root` (dark) AND under `.light` (light) — both definitions, even if values are similar. Both blocks must be in sync — a token defined in only one block silently inherits the other from `:root`, which produces broken contrast.
2. Use a semantic name (`--code-prompt`, `--card-elevated`), not a visual name (`--green-bright`, `--dark-gray-3`). Visual names rot the moment a designer changes the palette.
3. Reference it as `bg-[hsl(var(--token))]` or `text-[hsl(var(--token))]` from components.
4. Where a family of related tokens already exists (e.g., the `--code-*` group for terminal/diff/review surfaces, or `--diff-add-*` / `--diff-del-*` for diff rows), add to that family — don't start a parallel set.

**Code-surface tokens specifically** (`--code-chrome`, `--code-body`, `--code-fg`, `--code-fg-muted`, `--code-fg-subtle`, `--code-border`, `--code-prompt`, `--code-stderr`, plus the `--hl-*` syntax-highlighting set): these power TerminalCard, DiffCard, ReviewPane, and the `.hljs` rule block in `index.css`. New code-display components (terminal, diff, file-edit, log output) MUST use them — never hand-pick colors for "the dark code box".

**Lesson learned**: TerminalCard and DiffCard initially shipped with `bg-[hsl(220_5%_12%)]` literal backgrounds and `text-white/40` colors because the original author wanted the cards to "always look like a terminal". Result: they stayed dark in light mode and looked broken next to themed siblings. The fix was a one-time CSS-variable refactor; we should never need to do it again. Any reviewer seeing a literal `hsl(...)` or `text-white` in a PR diff should reject it.

### Scrollbar affordance — `overflow-y: auto` is invisible to many users
macOS, iOS, and overlay-configured Windows hide scrollbars by default on `overflow-y: auto`. Users don't know the content is scrollable, so they miss clamped/truncated content entirely. When height is bounded and content may overflow, use `overflow-y: scroll` (track always rendered) paired with themed `::-webkit-scrollbar*` and `scrollbar-color` — see the `.stAppViewContainer` block in `src/index.css` for the canonical pattern. Give the track a faint background (`hsl(var(--muted) / 0.4)`) so the "rail" itself is visible, not just the thumb. This is a discoverability / accessibility concern; don't skip it to make the UI "cleaner".

### Bundle presence ≠ visual render
When a user says "I can't see my change", grepping the minified JS/CSS bundle only proves the code COMPILED. It doesn't prove the element mounts, isn't covered by an absolutely-positioned sibling, isn't off-screen, isn't `display: none`-d by a CSS override. For any "it's not showing" bug, **query the actual DOM and take a screenshot before concluding** — `mcp__plugin_playwright_playwright__browser_evaluate` + `browser_take_screenshot` cover both. The Library-First Principle's debugging equivalent: DOM inspection is cheaper than yet another bundle grep.

Concrete example: the macOS chrome title bar on WidgetCard shipped with correct JSX, correct hex colors in the bundle, correct CSS selectors — but was completely invisible across three rebuilds because `.stApp { position: absolute; inset: 0 }` anchored to a `relative` ancestor that contained the chrome, so stlite painted over it. One DOM inspection would have caught it; three bundle-greps missed it.

### InputBar session context hydration
When rendering InputBar in detail mode, pass `sessionContext={effectiveContext}` (`SessionDetailView`, via `utils/timeline.ts:getLastContext`) so project/skill/model/MCP tool selections reflect the session's stored context. Without this, the toolbar defaults to null/global state regardless of what the session was created with.

`getLastContext` returns the **single most-recent `context`-type event's payload, verbatim — never merged across events.** This mirrors the backend's `_load_last_context` (`apps/mewbo_api/.../backend.py`), the function `/message` re-engage and `/recover` actually read to resume a session. Folding payloads forward (the earlier bug) made a cleared field "stick": InputBar's per-turn context payload *omits* a falsy field (e.g. `project` cleared back to Temporary) rather than sending `null`, so a fold-merge across the whole transcript kept resurrecting the last truthy value. Non-obvious corollary: `runtime.reinject_recovery_context` (core) only re-emits `client_capabilities`/`structured_workspace` after `/recover`, so a capability-gated session's LATEST context event post-recovery can be that narrow gating-only payload — `effectiveContext` will faithfully show that narrowed shape too (project/model/mcp_tools absent) because that is genuinely what the backend will read next. Don't "fix" this by reintroducing a merge — it would silently diverge from backend truth again.

### Data ordering must be explicit
Never rely on storage enumeration order for user-facing lists. UUIDs sort randomly; filesystem `readdir`/`os.listdir` order is undefined. Sort by `created_at` (or the appropriate field) at the data source — not in the UI layer — so all consumers get correct order (DRY). The backend `session_runtime.list_sessions()` sorts descending by `created_at`.

### Session list — provenance filter & the `managed:<uuid>` label trap
The landing page hides internally-spawned sessions by default. Each `SessionSummary` carries an `origin` (`user|wiki|search|channel|apps|structured|draft|mobile`) computed in **core** (`session_provenance`, not the FE). `HomeView` filters client-side over the already-fetched list via a per-origin `DropdownMenu` (default visible = `user`+`channel`; **wiki/search/apps/structured/draft/mobile sit behind the filter** — wiki-indexing / ask-question / realtime-API clutter; `apps` is the Mewbo Apps builder and maintainer sessions, which belong to the Apps product the way a wiki-indexing session belongs to Wiki; and `mobile` is the Aura Android app's own sessions, which scope themselves to the mobile rail and shouldn't clutter the console task list by default — symmetric with the rail hiding task sessions); `SessionOriginBadge` (composes the shared `Badge`, no new primitive) chips every row beside the timestamp.

**`utils/sessionOrigins.ts` is the ONE origin registry — label, colour AND glyph.** `ORIGIN_META` is an exhaustive `Record<SessionOrigin, {label, filterLabel?, color, icon}>`; `ORIGIN_FILTERS` is DERIVED from it over an explicit `ORIGIN_ORDER`, and `SessionOriginBadge` reads the same record. Before the consolidation the labels were spelled twice — the filter menu owned one set, the badge owned another — which is how the same origin came to read "My tasks" in the menu and "Manual" on the row with no shared home; `filterLabel` now states that difference deliberately (a filter row names a SET, a chip names one session) instead of it being an accident of two files. The union stays a **closed mirror of core**: a new origin touches `types.ts` (union) and `ORIGIN_META` — a missing arm is a TS error, which is the point, and `ORIGIN_FILTERS` follows for free. Badge colors come from `BADGE_COLOR_MAP` (`utils/agents.ts`); add the key there before referencing it.

**Glyphs: one mark per class, and the four product origins reuse the rail's.** `user`/`wiki`/`search`/`apps` carry the same lucide icon their NavRail row does (`nav-rail/products.ts`: `ListChecks`/`BookOpen`/`Search`/`AppWindow`) — a Wiki session and the Wiki rail row must not teach two different marks for the same thing. The remaining four are their own (`MessagesSquare` channel · `Braces` structured · `PenLine` draft · `Smartphone` mobile). The glyph is the fast read and the word is what disambiguates, so **neither is ever dropped**: an icon-only chip would make provenance guessable rather than legible, which is the opposite of the transparency the filter exists for. Sizes: `size-3` in the chip (proportionate to its `text-2xs` type), `size-3.5` in the filter menu (matching the check indicator's own box). ⚠️ **`DropdownMenuCheckboxItem` carries no `[&>svg]` sizing rule** — only `DropdownMenuItem` does — so an icon there MUST be sized explicitly or lucide renders it at its 24px default. Separately: the console persists `context.project` as `managed:<uuid>`, and `SessionItem` used to render `context.project || context.repo` — so managed *and worktree* sessions showed a raw UUID (worktree `repo`/`branch` were set but lost to the `project` precedence). Resolve display names through `ProjectLabel` (atomic class, fed by `useProjects()`, built once and threaded down): `managed:<id>` → project name; worktree → **parent repo name + branch**. No backend change — `/api/projects` already returns `is_worktree`/`parent_project_id`/`branch`. `SessionItem` also chips the session's `capabilities` (e.g. `scg`) + `workspace` id beside project/branch (transparency: WHAT the session was scoped to) — both ride `SessionSummary` from core `summarize_session`, additive metadata chips (`rounded`, not `rounded-full` — they're not state containers). These reflect the session's ADVERTISED capabilities only; a runtime-granted capability (`scg` granted on a plain session) is durable in the trace, not the session row (core does not probe the live predicate per list row).

### Session list in chrome — it lives in the NavRail now
`TaskSidebar` is deleted; the rail's Tasks section absorbed it (`src/components/nav-rail/CLAUDE.md` is the authority — anatomy, per-product contracts, the duplicate-list trap on `/`, state persistence/migration). Two laws born here that SURVIVE in the rail and must not regress: **the shared origin filter** (`utils/sessionOrigins.ts` `DEFAULT_VISIBLE_ORIGINS`/`isDefaultVisibleOrigin`, imported by both `HomeView` and the rail so they can never drift), and **the shadcn `sidebar`-block rejection** — that block ships a parallel `--sidebar-*` token palette + SidebarProvider/cookie/keyboard apparatus that fights this repo's token discipline; the rail stays hand-composed on `Sheet` (Radix) for the one real a11y concern, and re-introducing the block is a review reject.

### Don't hand-roll what an external library already gives you
The single largest source of churn in this codebase has been hand-rolled versions of solved problems: custom popovers, custom click-outside hooks, custom routers, custom data caches, custom dropdown keyboard handlers. Every one of these has now been deleted in favor of shadcn / Radix / TanStack Query / wouter / react-hook-form. Before writing a `useEffect` for any of the following, stop and use the library:

| You're tempted to write… | Use instead |
|---|---|
| `useEffect` with `document.addEventListener('mousedown', …)` for click-outside | shadcn `<Popover>` / `<DropdownMenu>` / `<Dialog>` (Radix handles it) |
| `useEffect` for Escape-key dismissal | Same as above |
| `useState` + `useEffect` to fetch and cache JSON | TanStack Query `useQuery` |
| `setInterval` polling for live data | TanStack Query `refetchInterval` (function-form) |
| `setTimeout` debouncing inside an input | shadcn `<Command>` (uses cmdk debounce) or `useDebouncedValue` from `@tanstack/react-query` ecosystem |
| `useState<view>` + `pushState` + `popstate` listener | wouter `<Route>` + `useLocation` |
| Per-field `useState` + manual validation | react-hook-form + zod + shadcn `<Form>` |
| Toast/notification scaffolding | shadcn `<Sonner>` (or `<Toast>` if you need actions) |
| Tooltip with manual hover state | shadcn `<Tooltip>` |
| Bespoke split-pane / resizable layout | `react-resizable-panels` v4 |
| Custom focus management for a dialog | Radix (already inside shadcn `<Dialog>`) |
| Custom theming toggle | shadcn theme convention (CSS variables on `[data-theme]`) |

If your task name reads like one of these and you find yourself writing JSX or hooks instead of `npm install` / `npx shadcn add`, **stop and re-read the Library-First Principle.**

## Live Deployment & API Access

The production console runs on this deployment's own domain over HTTPS with a
self-signed cert (check the local ops/deployment config for the actual host —
not tracked in this repo).

- **API credentials live in the deployment's `docker.env`** (a separate, untracked ops directory — ask if you can't locate it) — NOT in `app.json` or `~/.mewbo/app.json`.
- **Auth header:** `X-Api-Key: <MASTER_API_TOKEN>` (not `Authorization: Bearer`).
- **Ports:** API on `API_PORT` (default 5125), console on `CONSOLE_PORT` (default 3001).
- **Curl pattern:** Always use `-sk` (silent + skip cert verification) for the self-signed cert:
  ```bash
  curl -sk "https://<this-deployment's-console-host>/api/sessions/<ID>/events" \
    -H "X-Api-Key: $(grep MASTER_API_TOKEN <path-to-deployment's-docker.env> | cut -d= -f2)"
  ```
- **Playwright (via the MCP plugin) CAN reach this site.** The browser context accepts the self-signed cert. Use `mcp__plugin_playwright_playwright__browser_navigate` + `browser_evaluate` + `browser_take_screenshot` to inspect rendered DOM, verify styling, and diagnose "it's not showing" bugs. (An earlier version of this doc said the opposite; that note was stale — it was written before the plugin was wired up.) Use `curl -sk` for plain API JSON endpoints where Playwright is overkill.

## Testing
- Tests use Vitest + React Testing Library.
- Mock fixtures live in `__tests__/fixtures/` and are imported by tests only — there is no runtime mock fallback. When adding new exports to `api/client.ts`, mirror them in the `vi.mock` block of `__tests__/app.test.tsx`.
- This suite does **not** enable Vitest globals, so RTL auto-cleanup never runs — add `afterEach(cleanup)` yourself.
- `SettingsView.integration.test.tsx` renders the shell against the **REAL** `configs/app.schema.json` (read off disk from the repo root, never a fixture snapshot), so a backend `x-group` rename or a section RJSF can't slice fails loudly here instead of in the browser. Keep it pointed at the real file.

### Three traps that make a correct test look flaky

- **jsdom shares `window.location` across a whole test file.** Once `SettingsView` writes `?facet=`, the NEXT test in that file inherits it and starts on the wrong facet. Any file rendering `SettingsView` (or anything reading `useSearchParams`) needs `window.history.replaceState({}, "", "/settings")` in `beforeEach`. This is not hypothetical — it is why the settings + triggers-pane suites both carry that line.
- **A `React.lazy` boundary crossed inside a `waitFor` races the COLD module transform against the default 1000 ms timeout.** It passes in isolation and fails under full-suite parallel load, which reads exactly like flake. **Pre-warm the chunk (`await import("…")`) in the test — do NOT bump the timeout**; the timeout is measuring Vite's transform, not your assertion. (This is what made `agenticSearchStream.test.tsx` look flaky.) Settings panes are all lazy, so any pane test inherits this hazard.
- Wrap navigation tests in a wouter `memoryLocation` `Router` and assert on its recorded `history` rather than the global browser location.

## Pre-PR self-review checklist

Before opening a PR (or asking a reviewer to look at one), confirm each of these. If any answer is "no", fix it before requesting review:

- [ ] Every new component started with a search of shadcn primitives **and** shadcn blocks. If I wrote one from scratch, I can name the specific shadcn component(s) it would have replaced and why they don't fit.
- [ ] No new `useEffect` for click-outside / Escape / focus trap / portal / scroll-lock — Radix (via shadcn) handles all of those.
- [ ] No new `useState` + `useEffect` data-fetching hook — `useQuery` instead.
- [ ] No new `setInterval` polling — `refetchInterval` instead.
- [ ] No new manual route parsing — wouter instead.
- [ ] No new per-field form `useState` — react-hook-form + zod instead.
- [ ] No new ad-hoc button/input/dialog/menu styling — shadcn primitive instead.
- [ ] If I added a runtime dependency, it serves a concern not already covered by an existing dep, and I've added it to the "Current dependency rationale" table with a "Why this one (not alternatives)" entry.
- [ ] Every color comes from a CSS variable in `src/index.css` (`hsl(var(--token))`). No literal `hsl(...)`, no `text-white`/`text-black`, no Tailwind palette color names like `emerald-500` or `zinc-800` outside `index.css`. Status colors are NOT an exception anymore — `--success`/`--warning`/`--info`/`--destructive` exist for exactly that; the only sanctioned raw colors are identity hues that need side-by-side distinctness (origin/kind chips, `--agent-N` slots, platform icons — see "Status semantics have tokens", including the test for whether a colour is identity or state).
- [ ] If I added a new theme token, I added it to **both** `:root` (dark) AND `.light` (light) blocks in `src/index.css`.
- [ ] Diff is **smaller than it would have been with a custom implementation**. If my diff grew because I added a library wrapper, I went the wrong way — re-evaluate.

**Design checks** (see "Design Philosophy" for context):

- [ ] Shape vocabulary respected: left side `8/6/notch`, right side `6/4/0`, `rounded-full` only on state containers.
- [ ] Any new status / phase / progress display extends `<RunTelemetry>` or `<StatusBadge>` rather than starting a parallel readout. Stop & steering stay in the composer.
- [ ] Loaders / strips mount on `isRunning` (no always-on indicators). Hover-revealed elements also reveal on `group-focus-within`.
- [ ] No new infinite keyframes beyond `.session-cmp-pulse` and the mounted-only `.flower-mark`; no new top hairlines on the composer.
