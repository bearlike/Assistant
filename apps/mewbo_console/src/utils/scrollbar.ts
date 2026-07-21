/**
 * Themed, always-visible scrollbar for HEIGHT-BOUNDED panes.
 *
 * Pair with `overflow-y-scroll` (never `overflow-y-auto`): macOS, iOS and
 * overlay-configured Windows auto-hide the scrollbar on `auto`, so a user
 * who doesn't know a clamped box scrolls never finds the rest of the
 * content. Forcing the track to render is a discoverability affordance —
 * see the `.stAppViewContainer` block in `src/index.css`, which solves the
 * same problem for stlite widgets and is deliberately scoped to them.
 *
 * Applies ONLY to boxes with their own max-height. A pane that scrolls with
 * the page, or a full-height panel scroller, keeps the console's default
 * overlay scrollbar — a permanent track there is page chrome, not an
 * affordance for hidden content.
 */
export const SCROLLBAR_CLASS =
  '[scrollbar-width:thin] [scrollbar-color:hsl(var(--scrollbar-thumb))_hsl(var(--muted)/0.4)] ' +
  '[&::-webkit-scrollbar]:w-2 [&::-webkit-scrollbar-track]:bg-[hsl(var(--muted)/0.4)] ' +
  '[&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-[hsl(var(--scrollbar-thumb))] ' +
  '[&::-webkit-scrollbar-thumb:hover]:bg-[hsl(var(--scrollbar-thumb-hover))]';
