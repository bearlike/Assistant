<!-- mewbo:noload -->
> ↑ [root /CLAUDE.md](../../CLAUDE.md) · children: [wiki](src/components/wiki/CLAUDE.md) · [agentic_search](src/components/agentic_search/CLAUDE.md)

# Mewbo Console — Frontend Engineering Guide

**Subsystem docs (read the deepest one that applies):**
- `src/components/wiki/CLAUDE.md` — MewboWiki FE: atomic `IndexingProgress` class, KnowledgeGraphRenderer, full-log timeline pinning, SSE consumer pattern. The colocated `README.md` has the depth reference (file map, endpoint table, Mermaid invariants, Q&A streaming contract).

## What this is
A lean React + Vite + Tailwind web console that wraps the Mewbo API. Not a general-purpose SPA — it's an instrument for watching an agent work. Two principles run in parallel: **library-first** for engineering and **minimal-and-purposeful** for design (sections below). Neither overrides the other.

## Design Philosophy — minimal & purposeful

The console is two surfaces with two jobs: **left = calm reading**, **right = instrument panel**. Each side has its own shape vocabulary; mixing them flattens the hierarchy. Patterns come from tools the user already knows (Cursor's composer-owned Stop, Claude Code's token-based status and steering-during-runs, macOS peek-on-proximity scrollbars).

### Shape vocabulary

| Side | Radii | Surfaces | Signature |
|---|---|---|---|
| Left — conversation | `8` cards, `6` micro, `8/8/0/8` user bubble (`.bubble-notch`) | `--background` / `--card` / `--user-message-bg` | Bubble notch is the **only** silhouette signature |
| Right — workspace | `6` cards, `4` count pips, `0` sliding tab indicator, `10` terminal chrome | `--code-chrome` / `--code-body` family | `2.5 px` brand left rail on **log entries only** |

`rounded-full` (`9999px`) is reserved exclusively for **state containers**: status badges, agent-id pills, scrollbars, brand mark, run-pulse dot, `<ScrollToBottom>`, the frosted `.session-ts-rail` backplate. Never on buttons, cards, or chrome — those use `rounded-md`/`rounded-lg` via `Button` (`components/ui/button.tsx`).

### Operating principles

- **Status in exactly one place.** Global state → navbar `<StatusBadge>`. Live telemetry → `<RunTelemetry>` rendered as `variant="compact"` in the composer strip and `variant="full"` in the workspace sticky spinner — same `RunStatus` data, two windows. Stop → composer only. Don't start a parallel readout; extend one of these.
- **Phase-driven visibility.** Spinners, run-strips, telemetry mount only when `isRunning` (idle / awaiting / completed / failed → nothing). `runStatus` is computed once in `SessionDetailView` and threaded down; nothing polls.
- **Steering-aware composer.** During a run the composer stays alive: `.composer-shell` tints blue via `[data-running="true"]`, placeholder shifts to "Steer the run…", Send becomes **Queue** (`ArrowUpRight`), Stop appears alongside (not replacing it), and the toolbar narrows by hiding plus-menu / plan-mode pill.
- **Stop is destructive — two-step confirm always.** `InputComposerBody.StopWithConfirm`: ghost+red-hover button → popover above with danger primary + ghost cancel. Esc in the textarea opens the same popover; it never directly cancels.
- **Progressive disclosure.** Latest assistant turn fully expanded; older turns auto-collapse to **140 px** with a visible mask-fade (`SmartCollapse` in `ConversationTimeline`). Footer meta idles at `opacity-0`, reveals on hover/focus/`isLatest`. One primary action per group (e.g. footer = `Trace` + `⋯` overflow).
- **Whitespace over lines.** Padding rhythm carries turn boundaries; the composer dissolves UP into the conversation via `.composer-band-glow` — no top hairline. Add a hairline only when whitespace is ambiguous (workspace tab strip, run-indicator strip).
- **Animation is information.** Only motion: state transitions, entrance stagger (`.session-row-in`), and run-state aliveness (`.session-cmp-pulse` is the sole infinite loop while idle; `.flower-mark` only spins while mounted, i.e., while running). `prefers-reduced-motion` kills every keyframe.
- **Accessibility.** Hit targets ≥ 24×24 (AA), primary controls 32–40 px via `Button` `sm`/`md`/`lg`. Hover-revealed elements must also reveal on `group-focus-within`. Icon-only buttons need both `aria-label` and `title`. Don't strip `Button`'s built-in focus-visible ring.
- **Novelty earns its place.** The codebase ships two non-standard widgets, each with a job: `<TurnScroller>` (token-weighted cost-map dots pinned to the viewport margin via `fixed left-2`, segments capped at 22 px so they stay a margin glyph) and `<FlowerMark>` (branded conic ring local to `LogsView`). Add a third only if it answers a question the existing vocabulary can't.

### Session Detail anatomy — discoverability map

| Surface | Where it lives |
|---|---|
| Composer band glow | `InputBar.tsx` + `.composer-band-glow` |
| Composer shell (running tint, focus halo) | `InputBar.tsx` + `.composer-shell[data-running][data-focused]` |
| Run-indicator strip + Stop confirm + Send→Queue | `InputComposerBody.tsx` (`RunIndicator`, `StopWithConfirm`, Send branch) |
| Conversation lane (24 px gutter) | `ConversationTimeline.tsx` + `.conv-scroll` |
| User bubble notch | `MessageBubble.tsx` + `.bubble-notch` |
| Smart-collapse + turn footer + edit-state card | `ConversationTimeline.tsx` (`SmartCollapse`, `AssistantTurnFooter`, inline-edit `.edit-kbd`) |
| TurnScroller cost map | `TurnScroller.tsx` + `.session-ts-rail` / `.session-ts-seg` |
| Workspace tabs (icons + pips + sliding indicator) | `WorkspacePanel.tsx` + `.tab-ind` |
| Log / Diff / FileRead cards | `LogEventCard.tsx`, `DiffCard.tsx`, `FileReadCard.tsx` (`rounded-md` + `border-l-[2.5px]`) |
| Sticky FlowerSpinner | `LogsView.tsx` `FlowerMark` + `<RunTelemetry variant="full">` + `.spinner-sticky` |
| Pane separator grip | `SessionDetailView.tsx` `<PanelResizeHandle className="pane-rail">` |
| Shared run telemetry + elapsed | `components/RunTelemetry.tsx` + `hooks/useElapsed.ts` |

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
The cva variants are `primary | neutral | ghost` — there is no stock-shadcn `outline`/`default`; don't pass those names.

### ReviewPane (`components/ReviewPane.tsx`)
Accordion-style file review that replaces the old single-file diff tab. Shows all edited files in a collapsible list with unified diffs.

### PluginsView (`components/PluginsView.tsx`)
Plugin discovery and management page. Displays installed plugins and marketplace browsing. Uses `/api/plugins` and `/api/plugins/marketplace` endpoints.

### Settings — faceted shell over RJSF (`components/settings/`)
The Settings page is a **faceted shell**, not a flat form. RJSF is kept ONLY as the per-section field engine (`RjsfTheme.tsx` templates/widgets); the grouping/search/save shell around it is ours. Don't swap RJSF out, and don't recompute its job in components — call the model.
- **`SettingsModel.ts`** (pure, React-free, unit-tested) is the single heart: it turns the backend `AppConfig` schema + `{config, secrets}` into facets → sections → fields and owns slicing/search/diff (its `recursiveDiff` is kept byte-identical to `useConfig.shallowDiff`). Facets + their order/icons live in `facets.ts`; facet membership comes from each section's `x-group` metadata (on the section **class**, OR on a collection field directly — a `dict`/`list` field isn't a bare `$ref`, so its `json_schema_extra` survives; see core `config.py` contract). The **Workspace** facet holds projects+wiki; "Other" is now only a hidden fallback (rendered when non-empty). **`SettingsModel.uiSchemaFor(id, secrets, savedAt)` is the single archetype-routing home** — schema shape → widget (`list[BaseModel]`→`recordList`, `dict[str,X]`→`keyedCollection` incl. root-dict sections, secrets→`secret`, marketplaces→injected `itemValidator`, `plan_mode_shell_allowlist`→`restoreDefault`). Never recompute routing in components.
- **`SettingsView.tsx`** owns edit state (a `sectionId→formData` map) so the @modified filter + per-section Save/Reset work; sections are controlled. Saves are per-section: `model.patchFor(id, data)` → `useConfig.savePatch` (returns the new `{config,secrets}`; re-seed only the saved section so concurrent edits to sibling sections aren't clobbered — the seed effect seeds only *missing* sections).
- **`SecretField`** = write-only 3-state widget (unconfigured / configured-masked+Replace / editing) for `x-secret`/`writeOnly` fields; "configured" comes from the `secrets` map, never a value (the backend strips secret values, so the field is always absent from `config`).
- **Field kit (`settings/fields/`)** — atomic widgets *around* RJSF that NEVER fall through to its defaults. This is load-bearing: RJSF's default array/object/dict toolbars render as invisible `0×0px` buttons under our theme (no Bootstrap/icon CSS), so collections become silently un-editable. So a single registered **`ArrayFieldTemplate`** owns *every* array (`list[str]` rows + Add/Remove/Move via the shared `<Button>`; per-item `ui:options.itemValidator` + `restoreDefault`); **`RecordListField`** (`ui:field:"recordList"`) owns `list[BaseModel]` (the HooksConfig command/http type-switch); **`KeyedCollectionField`** (`ui:field:"keyedCollection"`) owns `dict[str,X]` (projects/channels/model_context_windows/lsp.servers) with an injected value-renderer + `JsonValueEditor` for freeform maps. **`FieldHelp` is the ONLY description renderer** (one 12px-muted style, compact react-markdown, long help → first-line + `?` Popover) — render help nowhere else or the old 12px-vs-16px page-bloat bimodality returns. `marketplaceValidation.ts` still MIRRORS backend `plugins.py::_resolve_git_url`, now injected as the marketplaces `itemValidator` (the old `RepositoriesField` was folded into `ArrayField` and deleted).
- The **Security facet reuses `ApiKeysView`** (do not rebuild the token UI) plus a read-only "configured secrets" summary. `ApiKeysView` is **chrome-agnostic** — it renders bare section cards matching schema-driven sections and owns no page width/padding, so BOTH consumers supply the shell: the Security pane and the standalone `/keys` route in `App.tsx`.
- `getConfig`/`patchConfig` return `{config, secrets}` — every consumer (incl. `useWebIdeEnabled`) must read `.config`, not the top level.

### Web IDE (`components/IdeLoader.tsx`, `hooks/useWebIdeEnabled.ts`)
"Open in Web IDE" launches per-session code-server containers via the API. `useWebIdeEnabled()` checks whether the feature is available. IDE loader shows a floral background animation during container startup.

### MewboWiki — `/wiki/*` section (`components/wiki/`)
Self-contained DeepWiki-style namespace: project gallery, configure wizard, indexing loader, wiki page, Q&A streaming view. All screens are wired against a mock client at `components/wiki/api/client.ts` — backend swap is one file. **Full design + integration notes live in [`components/wiki/README.md`](src/components/wiki/README.md)** — read it before editing anything under `components/wiki/`. The Gitea handoff issue describing the API contract verbatim is `bearlike/Assistant#5`. Key invariants colocated there: stable diagram ids for mermaid (avoid scroll-spy flicker), `mermaid-renderer.ts` singleton cache, `useIndexingStream`/`useQaStream` AsyncIterable contract honouring `AbortSignal`, mock data isolated to `components/wiki/mocks/`, model picker reusing `ModelBrandIcon` + `formatModelName` (no bespoke brand glyphs), platform tiles via `simple-icons` (CC0).

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

### `zoom` vs `transform: scale`
`StliteWidgetPanel` scales the stlite subtree via `zoom: 0.85`. This matters because `zoom` is part of the CSS layout tree — `scrollHeight` on descendants comes back ALREADY scaled, so `WidgetCard`'s ResizeObserver measures what the user sees. `transform: scale(0.85)` would paint smaller but keep the natural layout, leaving the card oversized by ~15%. If you ever "optimize" this to a transform, you'll break the content-height sizing.

### Fonts — stlite ships Source Sans Pro, not Inter
`theme.font: "sans serif"` maps to Streamlit's bundled Source Sans Pro. There is no runtime hook to inject Inter into the stlite worker. Accept the typography mismatch; don't spend time trying to CSS-override it — the font files are inside the Pyodide wheel.

### `streamlitConfig` dotted keys that matter
Passed through `useKernel` → spread into Streamlit's Python `load_config_options` in the worker:
- `client.toolbarMode: "viewer"` — hides Deploy / hamburger / Rerun band
- `theme.base: "dark" | "light"` — Streamlit's bundled palette, flipped via `key={theme}` remount
- `theme.{background,secondaryBackground,text}Color` — override to match `--widget-panel-bg` / `--muted` / `--foreground`
- `theme.primaryColor: "#D97757"` — brand clay
- `theme.font: "sans serif"` — see above

Belt-and-braces chrome hiding on top of `toolbarMode`: `[&_[data-testid='stHeader']]:!hidden`, `[&_[data-testid='stToolbar']]:!hidden`, `[&_[data-testid='stDecoration']]:!hidden`. Streamlit sometimes still ships the Running indicator / top gradient bar; kill them explicitly.

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

### Scrollbar affordance — `overflow-y: auto` is invisible to many users
macOS, iOS, and overlay-configured Windows hide scrollbars by default on `overflow-y: auto`. Users don't know the content is scrollable, so they miss clamped/truncated content entirely. When height is bounded and content may overflow, use `overflow-y: scroll` (track always rendered) paired with themed `::-webkit-scrollbar*` and `scrollbar-color` — see the `.stAppViewContainer` block in `src/index.css` for the canonical pattern. Give the track a faint background (`hsl(var(--muted) / 0.4)`) so the "rail" itself is visible, not just the thumb. This is a discoverability / accessibility concern; don't skip it to make the UI "cleaner".

### Bundle presence ≠ visual render
When a user says "I can't see my change", grepping the minified JS/CSS bundle only proves the code COMPILED. It doesn't prove the element mounts, isn't covered by an absolutely-positioned sibling, isn't off-screen, isn't `display: none`-d by a CSS override. For any "it's not showing" bug, **query the actual DOM and take a screenshot before concluding** — `mcp__plugin_playwright_playwright__browser_evaluate` + `browser_take_screenshot` cover both. The Library-First Principle's debugging equivalent: DOM inspection is cheaper than yet another bundle grep.

Concrete example: the macOS chrome title bar on WidgetCard shipped with correct JSX, correct hex colors in the bundle, correct CSS selectors — but was completely invisible across three rebuilds because `.stApp { position: absolute; inset: 0 }` anchored to a `relative` ancestor that contained the chrome, so stlite painted over it. One DOM inspection would have caught it; three bundle-greps missed it.

### InputBar session context hydration
When rendering InputBar in detail mode, pass `sessionContext={session.context}` so project/skill/MCP tool selections reflect the session's stored context. Without this, the toolbar defaults to null/global state regardless of what the session was created with.

### Data ordering must be explicit
Never rely on storage enumeration order for user-facing lists. UUIDs sort randomly; filesystem `readdir`/`os.listdir` order is undefined. Sort by `created_at` (or the appropriate field) at the data source — not in the UI layer — so all consumers get correct order (DRY). The backend `session_runtime.list_sessions()` sorts descending by `created_at`.

### Session list — provenance filter & the `managed:<uuid>` label trap
The landing page hides internally-spawned sessions by default. Each `SessionSummary` carries an `origin` (`user|wiki|search|channel|structured|draft`) computed in **core** (`session_provenance`, not the FE). `HomeView` filters client-side over the already-fetched list via a per-origin `DropdownMenu` (default visible = `user`+`channel`; **wiki/search/structured/draft sit behind the filter** — wiki-indexing / ask-question / realtime-API clutter); `SessionOriginBadge` (composes the shared `Badge`, no new primitive) chips every row beside the timestamp. The origin union is a **closed mirror of core**: a new origin touches `types.ts` (union), `ORIGIN_FILTERS`, and the exhaustive `SessionOriginBadge` `Record<SessionOrigin,…>` together — a missing arm is a TS error, which is the point. Badge colors come from `BADGE_COLOR_MAP` (`utils/agents.ts`); add the key there before referencing it. Separately: the console persists `context.project` as `managed:<uuid>`, and `SessionItem` used to render `context.project || context.repo` — so managed *and worktree* sessions showed a raw UUID (worktree `repo`/`branch` were set but lost to the `project` precedence). Resolve display names through `ProjectLabel` (atomic class, fed by `useProjects()`, built once and threaded down): `managed:<id>` → project name; worktree → **parent repo name + branch**. No backend change — `/api/projects` already returns `is_worktree`/`parent_project_id`/`branch`. `SessionItem` also chips the session's `capabilities` (e.g. `scg`) + `workspace` id beside project/branch (transparency: WHAT the session was scoped to) — both ride `SessionSummary` from core `summarize_session`, additive metadata chips (`rounded`, not `rounded-full` — they're not state containers). These reflect the session's ADVERTISED capabilities only; a runtime-granted capability (#83-B `scg` on a plain session) is durable in the trace, not the session row (core does not probe the live predicate per list row).

### Task sidebar — scope to detail, share the origin filter, one open-state for both surfaces
`TaskSidebar` is the persistent left task panel (`/s/:id` only — the landing page **is** the full task list, so a sidebar there just duplicates it and breaks the App smoke test by rendering each title twice). It is **self-contained**: it re-calls `useSessions()` + `useProjects()` (sharing the TanStack cache by queryKey — calling the hook again is *not* a second fetch) and navigates with wouter directly, so `AppLayout` stays dumb and threads no session props. It honours the **same default origin filter** as the landing page — the `user`+`channel` default lives in the shared `utils/sessionOrigins.ts` (`DEFAULT_VISIBLE_ORIGINS` / `isDefaultVisibleOrigin`), imported by both `HomeView` and `TaskSidebar` so they never drift. Rows are a **compact** variant (title + time + `SessionOriginBadge` + project label, capped at 15 with a "View all tasks" → `/` footer) — reuse the badge/label/time utils, but don't reuse the wide `SessionItem` (its branch/workspace/capability chips overflow a 288px rail). Responsiveness is **one persisted open-state** (`mewbo:sidebar-open`) driving two renderers: an inline `<aside>` on desktop and the existing `Sheet side="left"` drawer on mobile (`useIsMobile`) — default closed on mobile so the drawer never covers content on load; the NavBar `PanelLeft` toggle flips it on both. Chose composition over the shadcn `sidebar` block deliberately: that block ships a parallel `--sidebar-*` token palette + SidebarProvider/cookie/rail/keyboard-shortcut apparatus that fights this repo's bespoke shape-vocabulary and token discipline — and the only non-trivial a11y concern (the off-canvas drawer) is already solved by `Sheet` (Radix Dialog). Test gotcha: this suite doesn't enable Vitest globals, so RTL auto-cleanup never runs — add `afterEach(cleanup)` and wrap navigation tests in a `wouter` `memoryLocation` `Router` (assert on its recorded `history`) instead of the global browser location.

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

The production console runs at `https://mewbo.hurricane.home` (self-signed cert).

- **API credentials live in `/home/kk/Projects/Personal-Assistant/docker.env`** — NOT in `app.json` or `~/.mewbo/app.json`.
- **Auth header:** `X-Api-Key: <MASTER_API_TOKEN>` (not `Authorization: Bearer`).
- **Ports:** API on `API_PORT` (default 5125), console on `CONSOLE_PORT` (default 3001).
- **Curl pattern:** Always use `-sk` (silent + skip cert verification) for the self-signed cert:
  ```bash
  curl -sk "https://mewbo.hurricane.home/api/sessions/<ID>/events" \
    -H "X-Api-Key: $(grep MASTER_API_TOKEN /home/kk/Projects/Personal-Assistant/docker.env | cut -d= -f2)"
  ```
- **Playwright (via the MCP plugin) CAN reach this site.** The browser context accepts the self-signed cert. Use `mcp__plugin_playwright_playwright__browser_navigate` + `browser_evaluate` + `browser_take_screenshot` to inspect rendered DOM, verify styling, and diagnose "it's not showing" bugs. (An earlier version of this doc said the opposite; that note was stale — it was written before the plugin was wired up.) Use `curl -sk` for plain API JSON endpoints where Playwright is overkill.

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

**Design checks** (see "Design Philosophy" for context):

- [ ] Shape vocabulary respected: left side `8/6/notch`, right side `6/4/0`, `rounded-full` only on state containers.
- [ ] Any new status / phase / progress display extends `<RunTelemetry>` or `<StatusBadge>` rather than starting a parallel readout. Stop & steering stay in the composer.
- [ ] Loaders / strips mount on `isRunning` (no always-on indicators). Hover-revealed elements also reveal on `group-focus-within`.
- [ ] No new infinite keyframes beyond `.session-cmp-pulse` and the mounted-only `.flower-mark`; no new top hairlines on the composer.
