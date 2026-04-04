
# Meeseeks Frontend — Engineering Guide

> **Version:** 2.1.0-alpha  
> **Stack:** React 18 + TypeScript + Tailwind CSS + Vite  
> **Design system:** Shadcn UI preset (CSS variables, not component library)  
> **Fonts:** Inter (UI), JetBrains Mono (code)

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [File Structure](#2-file-structure)
3. [Design System & Theming](#3-design-system--theming)
4. [Component Catalog](#4-component-catalog)
5. [Hooks Reference](#5-hooks-reference)
6. [API Layer](#6-api-layer)
7. [Types](#7-types)
8. [Utilities](#8-utilities)
9. [Routing](#9-routing)
10. [Conventions & Rules](#10-conventions--rules)
11. [Common Patterns](#11-common-patterns)

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────┐
│  App.tsx  (root state, routing, theme)          │
│  ├── NavBar  (nav, theme toggle, notifications) │
│  ├── HomeView  (session list, input bar)        │
│  │   ├── InputBar (home mode)                   │
│  │   └── SessionItem × N                        │
│  └── SessionDetailView                          │
│      ├── ConversationTimeline                   │
│      │   ├── MessageBubble × N                  │
│      │   ├── FileList                           │
│      │   └── SummaryBlock                       │
│      ├── InputBar (detail mode)                 │
│      └── WorkspacePanel (right side)            │
│          ├── DiffView                           │
│          └── LogsView                           │
│              └── TerminalCard × N                 │
└─────────────────────────────────────────────────┘
```

**Data flow:** App.tsx owns top-level state (active view, session ID, theme, notifications). Views receive data via props. Hooks (`useSessions`, `useSessionEvents`, `useSessionQuery`, `useMcpTools`) encapsulate API calls and polling. The API layer (`api/client.ts`) tries the real backend first, then falls back to mock data automatically.

---

## 2. File Structure

```
├── api/
│   ├── client.ts          # API functions (real + mock fallback)
│   └── mockData.ts        # In-memory mock sessions, events, tools
├── components/
│   ├── ui/
│   │   └── alert.tsx      # Shadcn Alert primitive
│   ├── ConversationTimeline.tsx
│   ├── CopyButton.tsx       # Shared copy-to-clipboard button with icon feedback
│   ├── DiffStats.tsx
│   ├── DiffView.tsx
│   ├── FileList.tsx
│   ├── HomeView.tsx
│   ├── InputBar.tsx
│   ├── LogsView.tsx
│   ├── McpSelector.tsx
│   ├── MessageBubble.tsx
│   ├── NotificationPanel.tsx
│   ├── SessionDetailView.tsx
│   ├── SessionItem.tsx
│   ├── TerminalCard.tsx
│   ├── StatusBadge.tsx
│   ├── SummaryBlock.tsx
│   ├── NavBar.tsx
│   ├── AppLayout.tsx
│   └── WorkspacePanel.tsx
├── hooks/
│   ├── useMcpTools.ts
│   ├── useSessionEvents.ts
│   ├── useSessionQuery.tsx
│   └── useSessions.ts
├── utils/
│   ├── cn.ts              # className merge utility
│   ├── diff.ts            # Unified diff parser
│   ├── errors.ts          # Error logging helper
│   ├── logs.ts            # Event → log entry builder
│   ├── time.ts            # Time formatting
│   └── timeline.ts        # Events → timeline entries
├── App.tsx                # Root component
├── index.tsx              # Entry point
├── index.css              # Tailwind + CSS variables
├── types.ts               # Shared TypeScript types
└── __tests__/
    └── app.test.tsx
```

---

## 3. Design System & Theming

### 3.1 CSS Variable Tokens

All colors are defined as HSL values in `index.css`. **Never use hardcoded hex colors or raw Tailwind color classes** (e.g., `text-zinc-400`). Always reference CSS variables.

| Token | Dark Mode | Light Mode | Usage |
|-------|-----------|------------|-------|
| `--background` | 3.9% L | 96% L | Page canvas |
| `--foreground` | 91% L | 12% L | Primary text |
| `--card` | 9% L | 100% L | Raised surfaces (input bars, cards) |
| `--popover` | 13% L | 100% L | Floating elements (dropdowns, notifications) |
| `--muted` | 12% L | 90% L | Subtle backgrounds (tags, badges, code) |
| `--muted-foreground` | 50% L | 40% L | Secondary/dimmed text |
| `--accent` | 17% L | 93% L | Hover states |
| `--border` | 18% L | 84% L | All borders |
| `--primary` | Indigo 67% | Indigo 57% | CTA buttons, links, focus rings |
| `--destructive` | Red 31% | Red 45% | Error states |
| `--success` | Green 35% | Green 35% | Success states |
| `--surface` | 11% L | 93% L | General raised surface |
| `--surface-deep` | 5.5% L | 96% L | Workspace panels |

**Diff-specific tokens:**
| Token | Usage |
|-------|-------|
| `--diff-add-bg` | Green tinted background for added lines |
| `--diff-add-text` | Text color for added lines |
| `--diff-del-bg` | Red tinted background for deleted lines |
| `--diff-del-text` | Text color for deleted lines |
| `--diff-hunk-text` | Purple text for `@@` hunk headers |

### 3.2 Elevation Hierarchy (Dark Mode)

Each layer is 3–5% lighter than the one below for clear visual separation:

```
background (3.9%)  →  surface-deep (5.5%)  →  card (9%)  →  surface (11%)
    →  muted (12%)  →  popover (13%)  →  secondary (14%)  →  accent (17%)  →  border (18%)
```

### 3.3 How to Use Colors in Components

```tsx
// ✅ CORRECT — uses CSS variables, works in both themes
className="bg-[hsl(var(--card))] text-[hsl(var(--foreground))] border-[hsl(var(--border))]"

// ❌ WRONG — hardcoded, breaks in light mode
className="bg-[#1c1c21] text-zinc-200 border-zinc-800"

// ❌ WRONG — Tailwind dark: prefix doesn't work (we use .light class, not Tailwind dark mode)
className="text-green-400 dark:text-green-600"
```

### 3.4 Theme Toggle

- **Default:** Dark mode (no class on `<html>`)
- **Light mode:** `.light` class added to `<html>`
- **State:** Managed in `App.tsx` via `useState<'dark' | 'light'>('dark')`
- **Toggle:** `document.documentElement.classList.add/remove('light')`
- **Important:** We do NOT use Tailwind's `darkMode: 'class'` or browser `prefers-color-scheme`. Dark is always the default on refresh.

### 3.5 Typography

| Element | Font | Weight | Size |
|---------|------|--------|------|
| Body text | Inter | 400 | 14px (text-sm) |
| Headings | Inter | 500–600 | 14–30px |
| Code/mono | JetBrains Mono | 400 | 12px (text-xs) |
| Labels | Inter | 500 | 12px (text-xs) |
| Tiny text | Inter | 500 | 10px (text-[10px]) |

### 3.6 Shadows for Floating Elements

All dropdowns and popovers must use this shadow pattern:

```tsx
className="shadow-2xl shadow-black/40 ring-1 ring-white/[0.03]"
```

This creates depth in dark mode (deep shadow) and a subtle highlight ring for edge definition.

---

## 4. Component Catalog

### 4.1 NavBar

**File:** `components/NavBar.tsx`  
**Purpose:** Sticky navigation header (48px / h-12)

| Prop | Type | Description |
|------|------|-------------|
| `mode` | `'home' \| 'detail'` | Switches between home and session detail layouts |
| `session` | `SessionSummary?` | Current session (detail mode only) |
| `onBack` | `() => void` | Navigate back to home |
| `theme` | `'dark' \| 'light'` | Current theme |
| `onToggleTheme` | `() => void` | Toggle theme |
| `notifications` | `Notification[]` | Notification list |
| `onDismissNotification` | `(id: string) => void` | Dismiss single notification |
| `onClearNotifications` | `() => void` | Clear all notifications |

**Contains:** Logo + version badge, Settings/Docs links, GitHub link, theme toggle (Sun/Moon), notification bell with count badge, avatar. In detail mode: back arrow, session title, timestamp, status badge, archive/share actions.

---

### 4.2 HomeView

**File:** `components/HomeView.tsx`  
**Purpose:** Landing page with task input and session list

| Prop | Type | Description |
|------|------|-------------|
| `sessions` | `SessionSummary[]` | Active sessions |
| `archivedSessions` | `SessionSummary[]` | Archived sessions |
| `loading` | `boolean` | Loading state |
| `onSessionSelect` | `(id: string) => void` | Navigate to session |
| `onCreateAndRun` | `(query, context?) => void` | Create session + run query |
| `onLoadArchived` | `() => void` | Fetch archived sessions |
| `onArchive` | `(id: string) => void` | Archive a session |
| `onUnarchive` | `(id: string) => void` | Unarchive a session |
| `isCreating` | `boolean` | Submission in progress |

**Layout:** Fixed top section (heading + InputBar) → Scrollable bottom (tabbed session list with sticky tabs + search dialog overlay).

**Session grouping:** "Last 7 Days" and "Older" sections, computed from `created_at`.

---

### 4.3 SessionDetailView

**File:** `components/SessionDetailView.tsx`  
**Purpose:** Full session conversation + workspace panel

| Prop | Type | Description |
|------|------|-------------|
| `session` | `SessionSummary` | The active session |

**Layout:** Left panel (40% when workspace open, 100% otherwise) with ConversationTimeline + InputBar. Right panel (WorkspacePanel) opens when user clicks "Show Trace" or "Open Files".

**Hooks used:** `useSessionEvents` (polling), `useSessionQuery` (send/stop).

---

### 4.4 InputBar

**File:** `components/InputBar.tsx`  
**Purpose:** Text input with toolbar (Plus menu, MCP selector, mic, send/stop)

| Prop | Type | Description |
|------|------|-------------|
| `mode` | `'home' \| 'detail'` | Home = large textarea; Detail = compact inline |
| `onSubmit` | `(query, context?) => void` | Submit handler |
| `onStop` | `() => void` | Stop running session |
| `isRunning` | `boolean` | Show stop button |
| `isSubmitting` | `boolean` | Disable input during submission |
| `error` | `string?` | Error message to display |

**Features:**
- Auto-resizing textarea (both modes)
- Plus menu dropdown (Plan, Upload attachment)
- MCP tool selector dropdown
- Enter to submit, Shift+Enter for newline
- File attachment chip

---

### 4.5 MessageBubble

**File:** `components/MessageBubble.tsx`  
**Purpose:** Renders a single message (user or assistant)

| Prop | Type | Description |
|------|------|-------------|
| `role` | `'user' \| 'system' \| 'ai' \| 'assistant'` | Message sender |
| `content` | `string?` | Markdown content |
| `children` | `ReactNode?` | Extra content (action row, file list) |

**User messages:** Right-aligned card with collapsible long text (threshold: 180 chars). Shows "Show more/less" toggle.

**Assistant messages:** Full-width, left-aligned. Markdown rendered via `react-markdown` + `remark-gfm`.

**Markdown component overrides:** `p`, `a`, `ul`, `ol`, `li`, `blockquote`, `code`, `pre`, `h3`, `h4` — all use CSS variable colors.

---

### 4.6 ConversationTimeline

**File:** `components/ConversationTimeline.tsx`  
**Purpose:** Scrollable list of MessageBubbles with action rows

Each assistant message includes:
- **Action row:** Duration, "Show Trace" button, "Open Files" button (separated by vertical dividers)
- **FileList:** Collapsible list of modified files with diff stats

---

### 4.7 StatusBadge

**File:** `components/StatusBadge.tsx`  
**Purpose:** Colored pill showing session status

| Status | Color | Icon |
|--------|-------|------|
| `running` | Blue | PlayCircle |
| `completed` / `merged` | Emerald | CheckCircle2 |
| `incomplete` | Amber | AlertCircle |
| `failed` | Red | AlertCircle |
| `canceled` | Muted | XCircle |
| `idle` | Muted | Circle |
| `open` | Muted | Circle |

**Color approach:** Uses Tailwind's `500`-level colors (e.g., `text-emerald-600`, `bg-emerald-500/10`) which have acceptable contrast in both dark and light modes without needing `dark:` prefixes.

---

### 4.8 McpSelector

**File:** `components/McpSelector.tsx`  
**Purpose:** Dropdown to toggle MCP tool groups on/off

Uses `forwardRef` for click-outside detection. Options are grouped by server name. Each option shows name, tool count badge, and active indicator (green/grey circle).

---

### 4.9 NotificationPanel

**File:** `components/NotificationPanel.tsx`  
**Purpose:** Dropdown panel from bell icon showing session status notifications

| Prop | Type | Description |
|------|------|-------------|
| `notifications` | `Notification[]` | List of notifications |
| `onDismiss` | `(id: string) => void` | Remove single notification |
| `onClearAll` | `() => void` | Remove all |
| `onClose` | `() => void` | Close the panel |

**Notification type:**
```ts
type Notification = {
  id: string
  sessionTitle: string
  status: 'completed' | 'canceled' | 'stopped' | 'failed'
  timestamp: string  // ISO string
}
```

**Features:** Header with count badge, scrollable list with status icon + title + time ago + dismiss button (visible on hover), "Clear all" footer.

---

### 4.10 FileList

**File:** `components/FileList.tsx`  
**Purpose:** Collapsible list of modified files with +/- stats

Renders inside assistant message bubbles. Each file row is clickable (opens WorkspacePanel diff view). Uses `DiffStats` component for addition/deletion counts.

---

### 4.11 WorkspacePanel

**File:** `components/WorkspacePanel.tsx`  
**Purpose:** Right-side panel with Diff and Logs tabs

| Prop | Type | Description |
|------|------|-------------|
| `activeTab` | `'diff' \| 'logs'` | Current tab |
| `onTabChange` | `(tab) => void` | Switch tabs |
| `events` | `EventRecord[]` | Events for the active turn |
| `diffContent` | `string?` | Raw unified diff |
| `filename` | `string?` | File being viewed |
| `onClose` | `() => void` | Close panel |

---

### 4.12 DiffView

**File:** `components/DiffView.tsx`  
**Purpose:** Renders unified diff with syntax-colored lines

Uses `--diff-add-*`, `--diff-del-*`, `--diff-hunk-*` CSS variables for theme-aware coloring. Lines starting with `+` get green, `-` get red, `@@` get purple.

---

### 4.13 TerminalCard

**File:** `components/TerminalCard.tsx`
**Purpose:** Terminal emulator-style card for shell tool calls. Dark background with window chrome (traffic-light dots), CWD tab title, duration, exit status. Click to expand/collapse output. Used by `LogsView` when structured shell JSON is parsed from `payload.result`.

---

### 4.14 SessionItem

**File:** `components/SessionItem.tsx`  
**Purpose:** Single row in the session list

Shows: title, timestamp, repo, branch, status badge, MCP tool count, archive/unarchive button (visible on hover).

---

### 4.15 Other Components

| Component | File | Purpose |
|-----------|------|---------|
| `DiffStats` | `DiffStats.tsx` | `+N -N` addition/deletion display |
| `SummaryBlock` | `SummaryBlock.tsx` | Summary bullet list + test results |
| `LogsView` | `LogsView.tsx` | Renders shell blocks + system logs |
| `Alert` | `ui/alert.tsx` | Shadcn alert primitive (destructive variant) |

---

## 5. Hooks Reference

### `useSessions()`

**File:** `hooks/useSessions.ts`

```ts
const {
  sessions,           // SessionSummary[] — active sessions
  archivedSessions,   // SessionSummary[] — archived sessions
  loading,            // boolean
  archivedLoading,    // boolean
  error,              // string | null
  archivedError,      // string | null
  refresh,            // () => Promise<void> — reload active sessions
  refreshArchived,    // () => Promise<void> — reload archived sessions
  create,             // (context?) => Promise<string> — returns session_id
  archive,            // (sessionId) => Promise<void>
  unarchive,          // (sessionId) => Promise<void>
} = useSessions()
```

Auto-fetches on mount. `create` calls `refresh` after creating.

---

### `useSessionEvents(sessionId?)`

**File:** `hooks/useSessionEvents.ts`

```ts
const {
  events,          // EventRecord[] — all events so far
  running,         // boolean — is session still running
  error,           // string | null
  reset,           // () => void — clear all state
  resume,          // () => void — restart polling
  pollingEnabled,  // boolean
} = useSessionEvents(sessionId)
```

**Polling:** Every 1 second. Sends `after` param with last event timestamp. Stops when `running: false` or on error after initial fetch.

**Reset:** Automatically resets when `sessionId` changes.

---

### `useSessionQuery(sessionId?, context?)`

**File:** `hooks/useSessionQuery.tsx`

```ts
const {
  send,        // (query: string) => Promise<void>
  stop,        // () => Promise<void> — sends "/terminate"
  error,       // string | null
  submitting,  // boolean
  clearError,  // () => void
} = useSessionQuery(sessionId, context)
```

---

### `useMcpTools()`

**File:** `hooks/useMcpTools.ts`

```ts
const {
  tools,    // McpTool[] — MCP tools (filtered by kind === 'mcp')
  loading,  // boolean
  error,    // string | null
} = useMcpTools()
```

Fetches once on mount.

---

## 6. API Layer

**File:** `api/client.ts`

### Fallback Strategy

Every API function tries the real backend first. On **any** network failure, it flips a global `useMock = true` flag and uses mock data for all subsequent calls. This means:

- **With backend:** Real API calls to `VITE_API_BASE_URL`
- **Without backend:** Seamless mock data (no config needed)

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `VITE_API_BASE_URL` or `VITE_API_BASE` | Backend URL (empty = relative paths) |
| `VITE_API_KEY` | Sent as `X-API-KEY` header |

### Endpoints

| Function | Method | Path | Description |
|----------|--------|------|-------------|
| `listSessions(includeArchived?)` | GET | `/api/sessions` | List sessions |
| `createSession(context?)` | POST | `/api/sessions` | Create new session |
| `postQuery(sessionId, query, context?)` | POST | `/api/sessions/:id/query` | Send query or `/terminate` |
| `fetchEvents(sessionId, after?)` | GET | `/api/sessions/:id/events` | Poll for events |
| `archiveSession(sessionId)` | POST | `/api/sessions/:id/archive` | Archive session |
| `unarchiveSession(sessionId)` | DELETE | `/api/sessions/:id/archive` | Unarchive session |
| `listTools()` | GET | `/api/tools` | List MCP tools |

### Mock Data

**File:** `api/mockData.ts`

Pre-seeded with 5 active sessions + 2 archived. Each session generates a realistic event sequence: `user` → `action_plan` → `tool_result` (shell, write_file) → `step_reflection` → `assistant`.

---

## 7. Types

**File:** `types.ts`

```ts
SessionContext    // { repo?, branch?, mcp_tools? }
SessionSummary    // { session_id, title, status, created_at, done_reason, running, context, archived }
EventRecord       // { ts, type, payload }
DiffFile          // { name, path, additions, deletions, diff? }
TurnMeta          // { id, events, duration?, files }
TimelineEntry     // { id, role, content, turnId, turn? }
LogEntry          // { id, type, content, title?, timestamp? }
```

### Event Types (from backend)

| Type | Payload | Description |
|------|---------|-------------|
| `user` | `{ text }` | User message |
| `action_plan` | `{ steps }` | AI's planned steps |
| `tool_result` | `{ tool_id, operation, tool_input, summary, result }` | Tool execution result |
| `step_reflection` | `{ notes }` | AI's internal reflection |
| `assistant` | `{ text }` | AI's final response (markdown) |
| `summary` | `{ text[] }` | Session summary bullets |
| `test_result` | `{ command, passed }` | Test execution result |

---

## 8. Utilities

### `buildTimeline(events)` — `utils/timeline.ts`

Converts flat `EventRecord[]` into `TimelineEntry[]`. Groups events into "turns": each turn starts with a `user` event and ends with an `assistant` event. Intermediate `tool_result` events are collected for diff extraction.

### `extractUnifiedDiffs(text)` — `utils/diff.ts`

Parses unified diff format from tool result strings. Returns `DiffFile[]` with parsed additions/deletions counts.

### `mergeDiffFiles(files)` — `utils/diff.ts`

Deduplicates files by path, merging addition/deletion counts and concatenating diffs.

### `buildLogs(events)` — `utils/logs.ts`

Converts events into `LogEntry[]` for the LogsView panel.

### `extractSummaryTesting(events)` — `utils/logs.ts`

Extracts `summary` and `test_result` events into structured data.

### `formatSessionTime(timestamp)` — `utils/time.ts`

Formats ISO timestamps into relative time strings ("1 hr ago", "Feb 7, 2026").

### `cn(...classes)` — `utils/cn.ts`

Tailwind class merge utility (likely wraps `clsx` + `tailwind-merge`).

### `logApiError(context, error)` — `utils/errors.ts`

Logs errors to console and returns a user-friendly message string.

---

## 9. Routing

**Simple path-based routing** (no React Router). Managed in `App.tsx`.

| Path | View | Description |
|------|------|-------------|
| `/` | Home | Session list + input |
| `/s/:sessionId` | Detail | Session conversation |

**Implementation:** `window.history.pushState` for navigation, `popstate` listener for back/forward. `parseRoute()` extracts view + sessionId from pathname.

---

## 10. Conventions & Rules

### Styling

1. **ALWAYS use CSS variables** — `hsl(var(--foreground))`, never `text-zinc-400` or `bg-[#1c1c21]`
2. **No `dark:` prefix** — Our theme uses `.light` class, not Tailwind dark mode
3. **Elevation via tokens** — `--background` < `--card` < `--popover` < `--accent`
4. **Floating elements** — Always use `shadow-2xl shadow-black/40 ring-1 ring-white/[0.03]`
5. **Status colors** — Use `500`/`600`-level Tailwind colors that work in both themes (e.g., `text-emerald-600`)

### Components

1. **Named exports only** — `export function Component()`, never `export default`
2. **Props interfaces** — Define inline or above the component
3. **Click-outside pattern** — Use `useRef` + `mousedown` listener for dropdowns
4. **Icons** — Always from `lucide-react`, size `w-3.5 h-3.5` or `w-4 h-4`

### State

1. **No global state library** — Props drilling from `App.tsx`
2. **Hooks for API** — Each data concern has its own hook
3. **Optimistic updates** — Not used; always `await` then `refresh()`

### Code Style

1. **TypeScript strict** — No `any` types
2. **Functional components** — No class components
3. **KISS** — Minimal abstractions, no over-engineering
4. **DRY** — Shared tokens in CSS variables, shared types in `types.ts`

---

## 11. Common Patterns

### Adding a New Dropdown/Popover

```tsx
const [isOpen, setIsOpen] = useState(false)
const ref = useRef<HTMLDivElement>(null)

useEffect(() => {
  function handleClickOutside(e: MouseEvent) {
    if (ref.current && !ref.current.contains(e.target as Node)) {
      setIsOpen(false)
    }
  }
  document.addEventListener('mousedown', handleClickOutside)
  return () => document.removeEventListener('mousedown', handleClickOutside)
}, [])

return (
  <div className="relative" ref={ref}>
    <button onClick={() => setIsOpen(!isOpen)}>Toggle</button>
    {isOpen && (
      <div className="absolute ... bg-[hsl(var(--popover))] border border-[hsl(var(--border))] rounded-lg shadow-2xl shadow-black/40 ring-1 ring-white/[0.03] z-50">
        {/* content */}
      </div>
    )}
  </div>
)
```

### Adding a New API Endpoint

1. Add mock implementation in `api/mockData.ts`
2. Add real + fallback function in `api/client.ts` following the existing pattern
3. Create or extend a hook in `hooks/` if needed
4. Wire into the component tree via props from `App.tsx`

### Adding a New Status Badge Variant

Edit `components/StatusBadge.tsx`. Follow the existing pattern:
```tsx
if (status === 'your_status') {
  return (
    <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-COLOR-500/30 bg-COLOR-500/10 text-COLOR-600 text-xs font-semibold shadow-sm">
      <Icon className="w-3.5 h-3.5" />
      <span>Label</span>
    </div>
  )
}
```

### Adding a New Event Type

1. Add mock events in `api/mockData.ts` → `makeEvents()`
2. Handle in `utils/timeline.ts` → `buildTimeline()` if it affects the conversation
3. Handle in `utils/logs.ts` → `buildLogs()` if it should appear in the logs panel

---

## Quick Reference Card

| Need | Where |
|------|-------|
| Add a page/view | `App.tsx` routing + new component |
| Add an API call | `api/client.ts` + `api/mockData.ts` |
| Add a hook | `hooks/` directory |
| Change colors | `index.css` CSS variables (both `:root` and `.light`) |
| Add an icon | `import { IconName } from 'lucide-react'` |
| Add a UI primitive | `components/ui/` (Shadcn pattern) |
| Format time | `utils/time.ts` → `formatSessionTime()` |
| Parse diffs | `utils/diff.ts` → `extractUnifiedDiffs()` |
| Merge classnames | `utils/cn.ts` → `cn()` |
