# NavBar Responsive Redesign

## Problem

The session detail NavBar has no responsive strategy. On mobile/portrait viewports, the title is unreadable, action labels waste space, metadata badges compete for room, and there's no overflow pattern for secondary actions. All 10 reported issues stem from the same root cause: the bar lets content squeeze instead of collapsing at defined breakpoints.

## Design

### Two-breakpoint strategy

Uses standard Tailwind breakpoints `md` (768px) and `lg` (1024px).

| Element | < 768px (mobile) | 768-1023px (tablet) | >= 1024px (desktop) |
|---------|-----------------|---------------------|---------------------|
| Title | flex-1, no max-w | flex-1, no max-w | flex-1, no max-w |
| Timestamp | hidden | visible | visible |
| StatusBadge | icon-only | icon + text | icon + text |
| Model badge | hidden | hidden | visible |
| Archive btn | in overflow | icon-only | icon + text |
| Share btn | in overflow | icon-only | icon + text |
| Langfuse | in overflow | visible | visible |
| GitHub | in overflow | visible | visible |
| Theme toggle | in overflow | visible | visible |
| Notifications | visible | visible | visible |
| Overflow menu | visible | hidden | hidden |

### Mobile layout (< 768px)

```
[<-] [Title................] [Status-icon] [Bell] [...]
```

The kebab menu contains: Archive/Restore, Share (opens sub-actions), Langfuse, GitHub, Theme toggle.

### Tablet layout (768-1023px)

```
[<-] [Title..........] [3h ago] [Status] [Archive] [Share] [Langfuse] | [GH] [Theme] [Bell]
```

Archive and Share show icon-only (no text labels).

### Desktop layout (>= 1024px)

```
[<-] [Title..........] [3h ago] [Status] [model-badge] [Archive label] [Share label] [Langfuse] | [GH] [Theme] [Bell]
```

Full current layout with text labels.

## Files to modify

### 1. `NavBar.tsx` (~50 lines changed)

**Title area (left group):**
- Remove `max-w-[320px] md:max-w-[420px]` from the title container. Replace with `flex-1 min-w-0` so the title naturally fills available space and truncates.
- Timestamp `<span>`: add `hidden md:inline` to hide below 768px.
- Model badge `<span>`: add `hidden lg:inline-flex` to hide below 1024px.
- StatusBadge: pass `compact={isMobile}` using the existing `useIsMobile` hook.

**Right group (actions):**
- Archive and Share button text `<span>` elements: add `hidden lg:inline` to collapse to icon-only below 1024px.
- Wrap the "desktop-only" actions (Archive, Share, Langfuse, divider, GitHub, Theme) in a container with `hidden md:flex items-center gap-1.5` — visible only at 768px+.
- Add an overflow menu button (`EllipsisVertical` icon from lucide-react) with `md:hidden` — visible only below 768px.
- Add `OverflowMenu` component following the existing `ShareMenu` pattern (absolute-positioned dropdown, same popover styles). Contains: Archive/Restore, Share (copy link + export), Langfuse link, GitHub link, Theme toggle — all as menu items.

**Spacing fix:**
- Use consistent `gap-1.5` across all icon button groups (issue #8).

### 2. `StatusBadge.tsx` (~10 lines changed)

Add optional `compact?: boolean` prop. When `true`, hide the text `<span>` inside each status variant — show only the icon. This is backward-compatible: SessionItem doesn't pass `compact`, so it renders as before.

```tsx
interface StatusBadgeProps {
  status: string;
  doneReason?: string | null;
  compact?: boolean;
}
```

Each status block changes from:
```tsx
<Icon className="w-3.5 h-3.5" />
<span>Completed</span>
```
to:
```tsx
<Icon className="w-3.5 h-3.5" />
{!compact && <span>Completed</span>}
```

When compact, also reduce horizontal padding from `px-2.5` to `px-1.5` and remove the `gap-1.5` (no text to space against).

### 3. `useIsMobile.ts` — no changes (already exists)

Reused in NavBar with `const isMobile = useIsMobile()` to pass `compact` to StatusBadge and control overflow menu state.

## Issues addressed

1. **Title truncation** — removed hard max-width; title gets all remaining space
2. **Text labels don't collapse** — Archive/Share labels hidden below lg via `hidden lg:inline`
3. **StatusBadge always full-width** — compact (icon-only) prop on mobile
4. **Model badge too wide** — hidden below lg
5. **Timestamp wraps awkwardly** — hidden below md
6. **No overflow menu** — kebab menu below md with all secondary actions
7. **Notification bell cramped** — always visible, adequate tap target (p-1.5 = 28px)
8. **Inconsistent spacing** — consistent `gap-1.5` across action groups
9. **No breakpoint strategy** — two clear breakpoints at md and lg
10. **No title/actions width relationship** — flex-1 title with shrink-0 actions

## What this does NOT do

- No animation on overflow menu open/close (KISS)
- No changes to HomeView NavBar (home mode is simpler, fewer items)
- No changes to NotificationPanel positioning
- No new dependencies (EllipsisVertical already in lucide-react)
- No changes to the Popover component

## Verification

1. `npm run build` — no type errors
2. Browser devtools responsive mode:
   - 390px (iPhone): Title readable, only Bell + kebab visible on right, kebab opens menu with all actions
   - 768px (iPad portrait): Labels collapse to icons, timestamp visible, no overflow menu
   - 1024px+ (desktop): Full layout with text labels
3. Test overflow menu: Archive, Share, Langfuse, GitHub, Theme all functional from menu
4. Test StatusBadge compact in NavBar vs normal in SessionItem (home view)
