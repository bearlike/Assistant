<!-- meeseeks:noload -->
# Meeseeks Console — Frontend Engineering Guide

## What this is
A lean React + Vite + Tailwind web console that wraps the Meeseeks API. Not a general-purpose SPA — it exists solely to transparently show what the agent is doing and provide controls around the API.

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
- `mode="home"` (InputBar near top) → popups open **down** (`top-full mt-2`)
- `mode="detail"` (InputBar at bottom) → popups open **up** (`bottom-full mb-2`)
- This is controlled by `popupDirection` in InputBar and the `direction` prop on `<Popover>`.
- Never hardcode popup direction — always derive from context.

### Shared Popover component
All dropdown popups use `<Popover>` from `components/Popover.tsx`. Do not duplicate popup styling inline. If you need a new popup, use `<Popover direction={...} width="..." maxHeight="...">`.

## Data Fetching Patterns

### Hooks accept project scope
- `useMcpTools(project?)` — re-fetches when project changes
- `useSkills(project?)` — re-fetches when project changes
- `useProjects()` — no project scope (global list)
- All hooks expose `refresh()` for manual cache invalidation.

### TTL cache in `api/client.ts`
- 60s in-memory cache keyed by `tools:<project>`, `skills:<project>`, `projects`
- `invalidateCache(prefix?)` clears matching entries
- Cache is NOT shared between tabs/windows

### API client fallback pattern
`withFallback(realFn, mockFn)` tries the real API first, falls back to mocks in `auto` mode. Keep this pattern for all new endpoints.

### Agent lifecycle data
- `listAgents(sessionId)` fetches `/api/sessions/{id}/agents` returning `AgentSummary[]` with status, steps_completed, and total_steps
- Agent activity is tracked from `sub_agent` SSE events with enriched fields: `status`, `steps_completed`
- `AgentResult` JSON from `spawn_agent` tool results is parsed in `logs.ts` for structured display

## Architecture Constraints
- No state management libraries (no Redux, Zustand, Jotai)
- No data-fetching libraries (no React Query, SWR, Apollo)
- No UI component libraries (no Radix, Headless UI, shadcn)
- Hooks for data fetching, `useState`/`useCallback`/`useMemo` for state
- The `cn()` utility joins class names — it is NOT tailwind-merge (no class deduplication)
- Agent activity indicator in `ConversationTimeline` tracks active agents with task descriptions (not just count)
- Permission events rendered with icons: ⛔ deny, ✓ allow
- Sub-agent logs show lifecycle status and step count (▶ start, ■ stop with status)

## Dependency Selection — KISS/DRY Policy

Before adding any dependency, verify:
1. **Read the actual installed version's API** — not blog posts, not older docs. Run `node -e "console.log(Object.keys(require('pkg')))"` or read `node_modules/pkg/dist/*.d.ts` to confirm exports and prop names. Major versions often rename everything.
2. **Check units and defaults** — numeric props may mean pixels in one version and percentages in another. Always confirm from the type definitions what a bare number means.
3. **Prefer rehype/remark plugins over component-level libraries** — AST-level plugins (e.g., `rehype-highlight`) integrate with one prop addition and don't require component overrides. Component-level libraries (e.g., `react-syntax-highlighter`) require custom wiring, are harder to maintain, and tend to be larger.
4. **Verify bundle size claims yourself** — bundlephobia numbers and blog claims are often stale or wrong. Check the actual package after install.

### Current dependency rationale

| Package | Purpose | Why this one (not alternatives) |
|---------|---------|-------------------------------|
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

### InputBar session context hydration
When rendering InputBar in detail mode, pass `sessionContext={session.context}` so project/skill/MCP tool selections reflect the session's stored context. Without this, the toolbar defaults to null/global state regardless of what the session was created with.

### Data ordering must be explicit
Never rely on storage enumeration order for user-facing lists. UUIDs sort randomly; filesystem `readdir`/`os.listdir` order is undefined. Sort by `created_at` (or the appropriate field) at the data source — not in the UI layer — so all consumers get correct order (DRY). The backend `session_runtime.list_sessions()` sorts descending by `created_at`.

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
- Tests use Vitest + React Testing Library
- All API functions must be mocked in `__tests__/app.test.tsx` `vi.mock` block
- When adding new exports to `api/client.ts`, add them to the mock block too
