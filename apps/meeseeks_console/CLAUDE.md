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

## Architecture Constraints
- No state management libraries (no Redux, Zustand, Jotai)
- No data-fetching libraries (no React Query, SWR, Apollo)
- No UI component libraries (no Radix, Headless UI, shadcn)
- Hooks for data fetching, `useState`/`useCallback`/`useMemo` for state
- The `cn()` utility joins class names — it is NOT tailwind-merge (no class deduplication)

## Testing
- Tests use Vitest + React Testing Library
- All API functions must be mocked in `__tests__/app.test.tsx` `vi.mock` block
- When adding new exports to `api/client.ts`, add them to the mock block too
