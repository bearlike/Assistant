<!-- meeseeks:noload -->
# Meeseeks Console — Frontend Engineering Guide

## What this is
A lean React + Vite + Tailwind web console that wraps the Meeseeks API. Not a general-purpose SPA — it exists solely to transparently show what the agent is doing and provide controls around the API.

The codebase is **library-first** and **shadcn-first**. Every screen, primitive, and behavior should map to an existing shadcn component, shadcn block, Radix primitive, or other vetted library before any custom code is written. Bespoke implementations are a last resort and require justification (see "Library-First Principle" below).

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

### Hooks accept project scope
- `useMcpTools(project?)` — re-fetches when project changes (project is part of the queryKey)
- `useSkills(project?)` — same pattern
- `useProjects()` — no project scope (global list)
- `useWebIdeEnabled()` — checks if Web IDE feature is available
- All hooks expose a `refresh()` shim that delegates to `queryClient.invalidateQueries`. New hooks should follow the same convention so consumers don't need to learn the query API.

### Agent lifecycle data
- `listAgents(sessionId)` fetches `/api/sessions/{id}/agents` returning `AgentSummary[]` with status, steps_completed, and total_steps
- Agent activity is tracked from `sub_agent` SSE events with enriched fields: `status`, `steps_completed`
- `AgentResult` JSON from `spawn_agent` tool results is parsed in `logs.ts` for structured display

### Token usage — three semantics, one source of truth
The backend's `build_usage_numbers` (and the `/api/sessions/{id}/usage` endpoint feeding `useSessionUsage`) returns three distinct semantics. Pick the right one for the surface you're rendering:

- **Context fill (right now):** `root_last_input_tokens` — the size of the *most recent* root prompt. This is what the model has in its window now. Use this to drive the `<ContextWindowBar>` fill, the "X% used" label, and any "tokens until auto-compact" display. (Equivalent to Claude Code's `currentUsage` and Codex's `last_token_usage`.)
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

### ReviewPane (`components/ReviewPane.tsx`)
Accordion-style file review that replaces the old single-file diff tab. Shows all edited files in a collapsible list with unified diffs.

### PluginsView (`components/PluginsView.tsx`)
Plugin discovery and management page. Displays installed plugins and marketplace browsing. Uses `/api/plugins` and `/api/plugins/marketplace` endpoints.

### Web IDE (`components/IdeLoader.tsx`, `hooks/useWebIdeEnabled.ts`)
"Open in Web IDE" launches per-session code-server containers via the API. `useWebIdeEnabled()` checks whether the feature is available. IDE loader shows a floral background animation during container startup.

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

## Tool Card Components — DRY/KISS Rules

Tool-call events render in `LogsView` as cards. The component hierarchy is intentional — follow it:

```
LogEventCard (base: expand/collapse, accent border, icon/title/badge header)
├── Used by: renderPermission, renderAgent, renderAgentResult, renderCompletion, renderShell fallback, renderReflection
└── Compose this for new tool types unless the visual structure is fundamentally different

TerminalCard (specialized: always-dark bg, window chrome, $ prompt, stdout/stderr)
└── Used by: renderShell when structured shell data is present

CopyButton (shared: copy-to-clipboard with icon-swap feedback)
└── Used by: TerminalCard, MessageBubble, ConversationTimeline
```

**Rules when adding new tool card types:**

1. **Compose `LogEventCard` first.** It handles expand/collapse, accent borders, and the standard header layout. Only create a specialized component when the visual structure is genuinely different (like TerminalCard's dark background and window chrome).

2. **Never duplicate behavioral components inline.** If you need a copy button, import `CopyButton` from `components/CopyButton.tsx`. If you need a badge, use the `Badge` function in `LogsView`. Do not redefine these patterns inline.

3. **Styling goes in `className`, behavior goes in the component.** Shared components like `CopyButton` accept `className` for visual customization and `children` for additional content (labels, etc.). They own the behavioral logic (clipboard API, state feedback, event handling).

4. **Keep tool-specific helpers colocated.** Functions like `formatDuration()`, `shortenCwd()` in `TerminalCard` are used only by that component — keep them in the same file. Only extract to `utils/` when a second consumer appears.

5. **Parse structured tool data in `buildLogs()`, not in components.** The `utils/logs.ts` parser extracts typed fields from event payloads. Components receive clean props, never raw JSON.

## CSS / Component Pitfalls — Lessons Learned

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

### Theming — never hardcode colors that should adapt

The app supports light and dark mode via CSS variables in `src/index.css` (the `:root` block defines dark, the `.light` block defines light; the `<html>` element gets the `light` class toggled). Every color in a component must come from a CSS variable so it adapts automatically.

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

**Rule:** if you reach for a Tailwind color name (`emerald`, `red`, `zinc`, `slate`, `white`, `black`) or a literal `hsl(...)` outside `src/index.css`, stop and ask: *"is this an inherently brand color (a logo, a status indicator that means the same thing in both themes), or am I about to hardcode a theme-specific color?"* If the latter, use a CSS variable. If you can't find one that fits, **add a new variable to both `:root` and `.light` in `src/index.css`**, then use it. Do not bypass the variable system to "just pick a color quickly".

**Adding a new theme token (the only correct way to introduce a new color):**

1. Open `src/index.css`. Add the token under `:root` (dark) AND under `.light` (light) — both definitions, even if values are similar. Both blocks must be in sync — a token defined in only one block silently inherits the other from `:root`, which produces broken contrast.
2. Use a semantic name (`--code-prompt`, `--card-elevated`), not a visual name (`--green-bright`, `--dark-gray-3`). Visual names rot the moment a designer changes the palette.
3. Reference it as `bg-[hsl(var(--token))]` or `text-[hsl(var(--token))]` from components.
4. Where a family of related tokens already exists (e.g., the `--code-*` group for terminal/diff/review surfaces, or `--diff-add-*` / `--diff-del-*` for diff rows), add to that family — don't start a parallel set.

**Code-surface tokens specifically** (`--code-chrome`, `--code-body`, `--code-fg`, `--code-fg-muted`, `--code-fg-subtle`, `--code-border`, `--code-prompt`, `--code-stderr`, plus the `--hl-*` syntax-highlighting set): these power TerminalCard, DiffCard, ReviewPane, and the `.hljs` rule block in `index.css`. New code-display components (terminal, diff, file-edit, log output) MUST use them — never hand-pick colors for "the dark code box".

**Lesson learned**: TerminalCard and DiffCard initially shipped with `bg-[hsl(220_5%_12%)]` literal backgrounds and `text-white/40` colors because the original author wanted the cards to "always look like a terminal". Result: they stayed dark in light mode and looked broken next to themed siblings. The fix was a one-time CSS-variable refactor; we should never need to do it again. Any reviewer seeing a literal `hsl(...)` or `text-white` in a PR diff should reject it.

### InputBar session context hydration
When rendering InputBar in detail mode, pass `sessionContext={session.context}` so project/skill/MCP tool selections reflect the session's stored context. Without this, the toolbar defaults to null/global state regardless of what the session was created with.

### Data ordering must be explicit
Never rely on storage enumeration order for user-facing lists. UUIDs sort randomly; filesystem `readdir`/`os.listdir` order is undefined. Sort by `created_at` (or the appropriate field) at the data source — not in the UI layer — so all consumers get correct order (DRY). The backend `session_runtime.list_sessions()` sorts descending by `created_at`.

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

The production console runs at `https://meeseeks.hurricane.home` (self-signed cert).

- **API credentials live in `/home/kk/Projects/Personal-Assistant/docker.env`** — NOT in `app.json` or `~/.meeseeks/app.json`.
- **Auth header:** `X-Api-Key: <MASTER_API_TOKEN>` (not `Authorization: Bearer`).
- **Ports:** API on `API_PORT` (default 5125), console on `CONSOLE_PORT` (default 3001).
- **Curl pattern:** Always use `-sk` (silent + skip cert verification) for the self-signed cert:
  ```bash
  curl -sk "https://meeseeks.hurricane.home/api/sessions/<ID>/events" \
    -H "X-Api-Key: $(grep MASTER_API_TOKEN /home/kk/Projects/Personal-Assistant/docker.env | cut -d= -f2)"
  ```
- **Playwright cannot access this site** due to `ERR_CERT_AUTHORITY_INVALID` — use curl for API inspection instead.

## Testing
- Tests use Vitest + React Testing Library.
- Mock fixtures live in `__tests__/fixtures/` and are imported by tests only — there is no runtime mock fallback. When adding new exports to `api/client.ts`, mirror them in the `vi.mock` block of `__tests__/app.test.tsx`.

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
- [ ] Every color comes from a CSS variable in `src/index.css` (`hsl(var(--token))`). No literal `hsl(...)`, no `text-white`/`text-black`, no Tailwind palette color names like `emerald-500` or `zinc-800` outside `index.css` (brand/status colors that mean the same thing in both themes are the only exception, and even then prefer a token).
- [ ] If I added a new theme token, I added it to **both** `:root` (dark) AND `.light` (light) blocks in `src/index.css`.
- [ ] Diff is **smaller than it would have been with a custom implementation**. If my diff grew because I added a library wrapper, I went the wrong way — re-evaluate.
