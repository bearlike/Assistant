import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { FOCUS_RING } from "@/components/ui/focus-ring";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

/**
 * THE rail vocabulary — section chrome, rows, and the metrics both are built
 * from. Every rail-like surface in the console renders through this module.
 *
 * **A surface that hand-rolls rail chrome is a review reject.** Not for
 * tidiness: five surfaces each spelling their own row is how one list ended up
 * at two different heights, how one focus ring got diluted in three places, and
 * why the same missing border kept being reported on a new screen each time. If
 * the kit cannot express something a surface needs, **the kit gains a prop** —
 * it does not get forked.
 *
 * Anatomy, defined here and nowhere else:
 *
 * | Element        | Height | Type       | Glyph      |
 * |----------------|--------|------------|------------|
 * | Action row     | `h-8`  | `text-sm`  | `size-4`   |
 * | Row            | `h-7`  | `text-sm`  | `size-4`   |
 * | Section header | —      | `text-sm`  | `size-3.5` |
 * | Date / meta    | —      | `text-sm`  | `size-3.5` |
 *
 * **One size for every word.** Every piece of reading text in a rail renders at
 * `text-sm` — rows, action rows, section headers, date buckets and metadata
 * alike. Hierarchy is carried by COLOUR (a receded heading tone, muted
 * metadata, foreground identity/current), by INDENTATION (`depth`), and by a
 * WEIGHT step on a strict three-level ramp — `font-light` (300) for a
 * non-interactive LABEL (a section header or date bucket, the lightest tier so
 * it recedes below the rows it introduces rather than reading as another
 * clickable one), `font-normal` (400) for a resting row, `font-medium` (500)
 * for the current destination or an identity; `font-semibold` never appears in
 * a rail. Hierarchy is NEVER carried by a size step, which at 1px reads as
 * noise rather than rank and bottoms out at an unreadable leaf.
 * The ONE sanctioned exception is a numeric STATE-CONTAINER pill (a count pip,
 * an unread badge, the version chip): it may stay `text-2xs tabular-nums`,
 * because a 13px numeral balloons an overlay pill, and every such site is
 * marked as the state-pill exception. A word is never a pill.
 *
 * Both row heights clear the 24px AA hit-target floor, which is what licenses
 * the tighter of the two; do not shrink either without re-checking it. Every
 * boundary is `--rail-border`.
 *
 * ## What the RIGHT-hand rails share, and what they must not
 *
 * `agentic_search/RightRail` takes the metrics AND the section chrome from
 * here — `FOCUS_RING`, `RAIL_PAD_X`, `RailSection`, `RailRow` — so borders,
 * spacing, type and focus behave identically on both sides of the app. The
 * apps instrument rail (`apps/railSections/`) shares the VOCABULARY — the
 * type ramp, the glyph sizes, `RailEmpty` — but owns its section chrome: its
 * sections are persisted Collapsibles with a collapsed icon-strip mode, axes
 * `RailSection` deliberately does not model, and its interactive chrome is
 * shadcn `Button`/`Collapsible` triggers that carry their own focus ring.
 *
 * The instrument ROW also stays its own component, and this is a considered
 * boundary rather than drift:
 *
 * 1. **An instrument row contains controls.** It carries an `actions` slot, so
 *    making the whole row a `<button>` like `RailRow` would nest interactive
 *    elements — invalid HTML, and broken for keyboard and screen readers. It is
 *    an `<li>` in a `<ul>`; `RailRow` is a navigation control.
 * 2. **Its anatomy is two-line.** A note or summary sits under the identity;
 *    `RailRow` is fixed-height single-line by design, because a nav list that
 *    reflows as titles change stops being scannable.
 * 3. **The silhouette differs by the console's documented shape law** — left
 *    surfaces are `rounded-lg` (8px), right-hand instrument surfaces
 *    `rounded-md` (6px). Collapsing that would flatten a deliberate hierarchy.
 *
 * Merging those two would need `as`, `actions`, multi-line, tone and radius
 * axes on one component — the variant explosion this kit exists to avoid. Share
 * the tokens, not the silhouette.
 */

// The focus ring is shared by every interactive surface in the console, not
// just the rail — its home (and the load-bearing docstring on why the colour
// must stay undiluted) is `@/components/ui/focus-ring`. Re-exported here so
// every existing kit consumer keeps importing it from `rows.tsx` unchanged.
export { FOCUS_RING };

/**
 * The nav row shell: geometry, spacing, resting type.
 *
 * Colour is deliberately absent. A row's idle/hover/active colours differ by
 * surface — the assistant-ui thread-list primitive splits them across Root and
 * Trigger — so baking colour in would force that consumer to OVERRIDE rather
 * than compose, reopening the drift this constant exists to close.
 */
export const railRowCls =
  "flex h-7 w-full items-center gap-2 rounded-lg px-2 text-left text-sm font-normal transition-colors";

/** The action/product row shell — one step taller and always icon-led. */
export const railActionRowCls =
  "flex h-8 w-full items-center gap-3 rounded-lg text-sm transition-colors";

/** A glyph sitting INSIDE a row, matched to the row's type. */
export const railRowIconCls = "size-4 shrink-0";

/** A glyph beside a label or eyebrow — a step down, because a label is not a
 *  target and its icon should recede with its text rather than compete with the
 *  row glyphs beneath it. */
export const railLabelIconCls = "size-3.5 shrink-0";

/** Horizontal padding shared by every rail element — the base inset that puts
 *  every heading, row and empty state on ONE left column. Section CONTENT then
 *  steps in from that column by `RAIL_INDENT` (below), so the base column is the
 *  headings' column and the rows sit one minor step inside it. */
export const RAIL_PAD_X = "px-2";

/** The ONE section-content indent step, defined here and applied in exactly ONE
 *  place per rail: `RailSection`'s children wrapper (and the instrument rail's
 *  section body). Everything under a section HEADING — rows, date groups and
 *  their rows, empty states — steps in by this single minor amount relative to
 *  the flush heading, which is the visual cue that they BELONG to it
 *  (hierarchical readability). It is deliberately small (`pl-1.5` = 6px, below
 *  the 8px base inset) so it reads as "visible but minor". Two rules keep it DRY:
 *  (1) it goes on the WRAPPER, never per-child — `RailRow`'s `depth` prop
 *  composes its `pl-4`/`pl-6` ON TOP for genuinely nested trees; (2) it applies
 *  only when a section has a heading — an unlabelled section (a lone action row,
 *  the product switcher) has nothing to indent under and stays flush. The
 *  heading itself is NOT in this wrapper, so its full-bleed `border-t` is
 *  untouched. */
export const RAIL_INDENT = "pl-1.5";

/**
 * The gutter that floats a row's FILL off the rail's edges. This is the
 * rules-vs-fills distinction, and it is load-bearing: RULES (zone borders,
 * `RailSectionHeader`'s `border-t`) are FULL-BLEED `[0 → rail width]` because
 * they sit on full-width elements; a row's FILL (the rounded hover/active pill)
 * must NEVER touch the rail's edges, because a rounded pill kissing the viewport
 * reads broken.
 *
 * So the gutter lives on each zone's CONTENT wrapper, never on a bordered
 * element. `px-1` (4px) is not arbitrary: combined with the row's own
 * `RAIL_PAD_X` (8px) it lands the row's icon on the **12px column where the
 * brand mark begins** (measured: header `px-2` 8px + brand-button `px-1` 4px),
 * so every icon in the rail starts where the Mewbo logo starts. The fill floats
 * 4px in from each edge; the border above it still spans edge to edge.
 */
export const RAIL_GUTTER = "px-1";

/**
 * The universal content-start column — the ONE horizontal position every icon
 * and label in the rail begins at: **12px**, where the brand mark begins. Its
 * job is the user's alignment law: nothing starts left of here, and `RAIL_INDENT`
 * only steps section CHILDREN in AFTER it.
 *
 * A ROW reaches 12px as `RAIL_GUTTER` (4px, from its zone's gutter wrapper) +
 * `RAIL_PAD_X` (8px, its own padding). The HEADER/LABEL tiers can't: they sit
 * OUTSIDE the gutter wrapper — full-width, so their `border-t` stays full-bleed —
 * so they'd otherwise begin at just `RAIL_PAD_X` (8px), LEFT of the row column
 * (the "header icon closer to the edge than the rows" defect). This constant is
 * the fix: they state the 12px column directly. Keep it equal to
 * `RAIL_GUTTER` + `RAIL_PAD_X` (4 + 8 = 12) — it is a derived value, not a taste.
 */
export const RAIL_CONTENT_START = "pl-3";

/** The rule that separates one zone from the next. */
const RAIL_DIVIDER = "border-t border-[hsl(var(--rail-border))]";

// ---------------------------------------------------------------------------
// Rows
// ---------------------------------------------------------------------------

interface RailActionRowProps {
  icon: LucideIcon;
  label: string;
  onClick: () => void;
  /** Marks the row as the current destination — the product-switcher highlight
   *  (persistent fill + `aria-current="page"`). Action rows leave it false. */
  current?: boolean;
  /** Icon-only rail: hide the label, center the glyph, show a right-side
   *  tooltip. Requires a `TooltipProvider` ancestor (NavRail supplies one). */
  collapsed?: boolean;
}

/** The h-8 icon+label row — a product switcher entry, or a section's own action
 *  ("New task", "All wikis"). */
export function RailActionRow({
  icon: Icon,
  label,
  onClick,
  current = false,
  collapsed = false,
}: RailActionRowProps) {
  const button = (
    <button
      type="button"
      onClick={onClick}
      aria-current={current ? "page" : undefined}
      aria-label={collapsed ? label : undefined}
      title={collapsed ? undefined : label}
      className={cn(
        railActionRowCls,
        collapsed ? "justify-center px-0" : RAIL_PAD_X,
        current
          ? "bg-[hsl(var(--rail-selected))] font-medium text-[hsl(var(--foreground))]"
          : "font-normal text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--rail-selected))]/60 hover:text-[hsl(var(--foreground))]",
        FOCUS_RING,
      )}
    >
      <Icon className={railRowIconCls} aria-hidden />
      {!collapsed && <span className="truncate">{label}</span>}
    </button>
  );

  if (!collapsed) return button;
  return (
    <Tooltip>
      <TooltipTrigger asChild>{button}</TooltipTrigger>
      <TooltipContent side="right">{label}</TooltipContent>
    </Tooltip>
  );
}

interface RailRowProps {
  label: string;
  onClick: () => void;
  active?: boolean;
  /** Optional glyph before the label. Rows carry NO glyph by default — the slot
   *  indents titles past the section header and breaks the one-column read, so
   *  a homogeneous list of recents stays flush-left. A heterogeneous list (a
   *  page tree, where section-vs-page is a real distinction) opts in. */
  leading?: ReactNode;
  /** Optional trailing content — a relative time, a count, a running dot. */
  trailing?: ReactNode;
  title?: string;
  /**
   * Nesting depth for a hierarchical list. Depth reads as INDENTATION plus
   * weight and nothing else: shrinking the type per level bottoms out at an
   * unreadable leaf after two steps, and a second typeface would import a
   * vocabulary the rail does not otherwise speak.
   */
  depth?: 1 | 2 | 3;
}

/** THE navigation row — recents, page trees, and any list whose rows ARE
 *  destinations. A row that has to contain its own controls is an instrument
 *  row instead; see the module docstring. */
export function RailRow({
  label,
  onClick,
  active = false,
  leading,
  trailing,
  title,
  depth,
}: RailRowProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? "page" : undefined}
      title={title ?? label}
      className={cn(
        railRowCls,
        // Depth 1 keeps the flush-left read every other rail list has; deeper
        // levels step in, and only the top level carries weight.
        depth === 1 && "font-medium",
        depth === 2 && "pl-4",
        depth === 3 && "pl-6",
        active
          ? "bg-[hsl(var(--rail-selected))] text-[hsl(var(--foreground))]"
          : "text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--rail-selected))]/60 hover:text-[hsl(var(--foreground))]",
        FOCUS_RING,
      )}
    >
      {leading && <span className="shrink-0">{leading}</span>}
      <span className="flex-1 truncate">{label}</span>
      {trailing && <span className="shrink-0">{trailing}</span>}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Section chrome
// ---------------------------------------------------------------------------

interface RailSectionHeaderProps {
  children: ReactNode;
  icon?: LucideIcon;
  /** A count, or a small action belonging to this section. */
  trailing?: ReactNode;
  /**
   * The rule above the header. ON by default, because a header almost always
   * follows an action row and the rule is what separates the action from the
   * list it introduces. Turn it OFF only where the header OPENS its zone — a
   * rule there would double the zone divider already above it.
   */
  divider?: boolean;
}

/** The eyebrow above a list. Usually reached through `RailSection`. */
export function RailSectionHeader({
  children,
  icon: Icon,
  trailing,
  divider = true,
}: RailSectionHeaderProps) {
  return (
    <div
      className={cn(
        // `pb-2`, not `pb-1`: at the tighter value the header collided with the
        // first row and read as part of the list instead of introducing it.
        // `text-sm font-light`: a non-interactive label is the LIGHTEST tier of
        // the rail's 300/400/500 weight ramp, so it recedes below the rows it
        // introduces instead of reading as another clickable one — the fix for
        // "headings look like the buttons beneath them". Colour recedes too: it
        // rides `--rail-heading` (calibrated JUST above the 4.5:1 AA floor so it
        // sits below `--muted-foreground` without dropping under AA — the leading
        // glyph inherits it). Rank is weight + colour, never a smaller size; and
        // it carries NO hover/active styling, because a header is a label.
        "flex items-center gap-2 pb-2 pt-3 text-sm font-light text-[hsl(var(--rail-heading))]",
        // The header is full-width (so its `border-t` stays full-bleed), so it
        // states the 12px content column directly — otherwise its icon would begin
        // at 8px, LEFT of the rows it heads (which reach 12px via the gutter).
        RAIL_CONTENT_START,
        "pr-2",
        divider && cn("mt-1", RAIL_DIVIDER),
      )}
    >
      {Icon && <Icon className={railLabelIconCls} aria-hidden />}
      <span className="flex-1 truncate">{children}</span>
      {trailing && <span className="shrink-0">{trailing}</span>}
    </div>
  );
}

/**
 * A rail section: an optional heading plus its rows, with the boundary and the
 * spacing that separate it from its neighbours.
 *
 * **A rail zone is a STACK of these.** Composing header-plus-rows by hand is
 * what produced sections with a border on one screen and none on the next; here
 * the boundary and the gap come from one place, so every surface gets them
 * whether or not its author remembered.
 *
 * A section with no `label` is still a section — it is how a lone action row
 * gets the same spacing as a labelled list without growing a redundant eyebrow.
 */
export function RailSection({
  label,
  icon,
  trailing,
  divider,
  children,
}: {
  label?: ReactNode;
  icon?: LucideIcon;
  trailing?: ReactNode;
  divider?: boolean;
  children: ReactNode;
}) {
  return (
    <div className="pb-1">
      {/* The header stays OUTSIDE the gutter wrapper below, so its `border-t`
          spans the rail edge to edge (rules are full-bleed). */}
      {label !== undefined && (
        <RailSectionHeader icon={icon} trailing={trailing} divider={divider}>
          {label}
        </RailSectionHeader>
      )}
      {/* `RAIL_GUTTER` floats the row FILLS a few px off the rail's edges (a
          rounded pill must never kiss the viewport) and lands their icons on the
          brand-mark column. `RAIL_INDENT` then steps a LABELLED section's content
          in from its heading — on an inner wrapper so the two stack (margin-free
          padding on one element would fight over `padding-left`). An unlabelled
          section (a lone action row) has no heading to indent under, so it gets
          the gutter only and stays flush. `RailRow`'s `depth` composes on top. */}
      <div className={cn("flex flex-col", RAIL_GUTTER)}>
        {label !== undefined ? (
          <div className={cn("flex flex-col", RAIL_INDENT)}>{children}</div>
        ) : (
          children
        )}
      </div>
    </div>
  );
}

/** A date bucket (Today / Previous 7 days / Older) — a label tier alongside
 *  `RailSectionHeader`, sharing its glyph size AND its colour so the two read as
 *  one family. Reads at the rail's single `text-sm` size like every other word;
 *  its lower rank is carried by `font-light` (the label tier of the weight ramp)
 *  and the receded `--rail-heading` colour, never a smaller font. That token is
 *  calibrated to sit just above the AA floor so it recedes below
 *  `--muted-foreground` without ever dropping under it. */
export function RailDateGroup({
  label,
  icon: Icon,
  children,
}: {
  label: string;
  icon?: LucideIcon;
  children: ReactNode;
}) {
  return (
    <div>
      <div
        className={cn(
          "flex items-center gap-2 pb-1.5 pt-2 text-sm font-light text-[hsl(var(--rail-heading))]",
          RAIL_PAD_X,
          // A date bucket is a header-tier label, so its icon sits on the SAME
          // 12px column as the section header above it — NOT indented with its
          // own rows. But it renders INSIDE the section's `RAIL_INDENT` wrapper
          // (its rows want that indent), so the LABEL alone counteracts that one
          // step with `-ml-1.5` to land back on the base column. This mirrors
          // `RAIL_INDENT` (`pl-1.5`); the two must change together.
          "-ml-1.5",
        )}
      >
        {Icon && <Icon className={railLabelIconCls} aria-hidden />}
        <span className="truncate">{label}</span>
      </div>
      {/* Rows keep the section's indent (18px), so they sit one step in from
          this bucket label — the label heads them, it doesn't join them. */}
      <div className="flex flex-col">{children}</div>
    </div>
  );
}

/** Calm one-line empty state for a section with nothing in it yet. A rail shows
 *  no error residue — a failed query renders this, never a red banner. */
export function RailEmpty({ children }: { children: ReactNode }) {
  return (
    <p
      className={cn(
        "py-2 text-sm font-normal text-[hsl(var(--muted-foreground))]",
        RAIL_PAD_X,
      )}
    >
      {children}
    </p>
  );
}
