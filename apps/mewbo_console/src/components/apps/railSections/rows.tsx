/* eslint-disable react-refresh/only-export-components --
 * This module is a KIT: it deliberately exports components alongside the tone
 * map and the shared metric re-exports, so a section body has ONE import
 * surface (see the module doc below). The rule is a hot-reload granularity
 * hint, not a correctness one — the cost of obeying it is fragmenting that
 * surface across two modules, which is the exact drift this kit exists to
 * prevent. The trade is a full module reload for this file during dev.
 */
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";
import { RAIL_PAD_X } from "@/components/nav-rail/rows";

/**
 * The instrument rail's ROW vocabulary — the right-hand counterpart to the
 * NavRail kit's navigation `RailRow`. Health, Recent runs, Pipelines, Schedule
 * and Versions all render through these, so no section re-styles a row, a
 * label, a timestamp, a stat or an empty state.
 *
 * ## The boundary with `nav-rail/rows.tsx` (documented in that kit too)
 * This module does NOT re-implement the kit; it SHARES the kit's metrics and
 * defers to it for anything not instrument-specific:
 *   - `RAIL_PAD_X` — the shared base inset. The section body then adds one
 *     `RAIL_INDENT` step (applied by `InstrumentRail`), so a row's text column
 *     steps IN from its section heading by the same minor amount the NavRail kit
 *     indents a labelled section's content — the header stays flush, its rows sit
 *     one step inside it.
 *   - `railRowIconCls` (size-4) / `railLabelIconCls` (size-3.5) — the shared
 *     two-tier glyph scale, re-exported so section bodies size lucide icons
 *     from the metric rather than a hardcoded `h-x w-x`.
 *   - `RailEmpty` — re-exported unchanged, so both rails show one empty state.
 *
 * What stays here is ONLY what the kit's navigation row cannot be: an instrument
 * row is an `<li>` (it contains its own controls, so it can't be a `<button>`
 * like `RailRow` without nesting interactive elements), it is two-line (a
 * note/summary sits under the identity), and it wears the `rounded-md`
 * right-hand instrument shape, not the nav side's `rounded-lg`. That boundary is
 * the kit author's documented decision — share the tokens, not the silhouette.
 *
 * ## Type ramp — one size, and nothing else
 * Every word in an instrument row renders at `text-sm` (13px) — the identity,
 * the supporting note, and the trailing metadata alike. Rank comes from WEIGHT
 * and COLOUR, never a size step: identity is `font-medium` on `--foreground`,
 * everything qualifying it is `font-normal` on `--muted-foreground`. The sole
 * exception is a numeric STATE-CONTAINER pill (a count pip), which stays
 * `text-2xs tabular-nums` because a 13px numeral balloons an overlay pill; a
 * word is never a pill.
 *
 * ## Row anatomy — the same four tiers everywhere
 * leading glyph (state) · identity (takes the slack, truncates) · metadata
 * (shrinks and truncates, capped at 60%) · actions (never shrink). A row that
 * needs a second line passes it as `children`, which lands under the identity
 * line at the same indent.
 */

/** One instrument row's geometry + resting type. `rounded-md` is the right-hand
 *  shape law; `min-h-7` (not the kit row's fixed `h-7`) lets a row grow for a
 *  second line; `RAIL_PAD_X` is the kit's inset so text columns align. Colour is
 *  deliberately excluded: a row's tone is per-slot, so folding one colour in
 *  would force every slot to override it. 28px clears the 24px hit-target floor. */
export const RAIL_ROW_CLS = cn(
  "flex min-h-7 w-full items-center gap-2 rounded-md py-1 text-sm",
  RAIL_PAD_X,
);

/**
 * Status tones, all from the semantic token family. Colour NEVER travels
 * alone in this rail — every caller pairs a tone with a glyph or a word, so a
 * state stays readable to anyone who cannot separate the hues.
 */
export type RailTone = "success" | "warning" | "danger" | "active" | "muted" | "default";

export const RAIL_TONE_CLS: Record<RailTone, string> = {
  success: "text-[hsl(var(--success))]",
  warning: "text-[hsl(var(--warning))]",
  danger: "text-[hsl(var(--destructive-text))]",
  active: "text-[hsl(var(--primary-text))]",
  muted: "text-[hsl(var(--muted-foreground))]",
  default: "text-[hsl(var(--foreground))]",
};

/** The list wrapper for a section body. Rows carry their own padding, so the
 *  gap between them is the only spacing this owns. */
export function RailList({ children }: { children: ReactNode }) {
  return <ul className="space-y-0.5">{children}</ul>;
}

interface RailRowProps {
  /** Identity — what this row IS. Truncates; never wraps. */
  label: ReactNode;
  /** Tooltip for a truncated identity. */
  labelTitle?: string;
  /** State glyph before the identity (a run's outcome, a schedule warning). */
  leading?: ReactNode;
  /** Metadata after the identity — a timestamp, a count, a state word. */
  trailing?: ReactNode;
  /** Controls. Kept out of `trailing` so every row's buttons align. */
  actions?: ReactNode;
  /** Marks the row as the current one (the active app version). */
  active?: boolean;
  /** A secondary block under the identity line: notes, summaries, badges. */
  children?: ReactNode;
}

/** One instrument row. */
export function RailRow({
  label,
  labelTitle,
  leading,
  trailing,
  actions,
  active = false,
  children,
}: RailRowProps) {
  return (
    <li
      className={cn(
        RAIL_ROW_CLS,
        "flex-col items-stretch gap-1",
        active && "bg-[hsl(var(--primary)/0.08)]",
      )}
    >
      <div className="flex min-w-0 items-center gap-2">
        {leading && <span className="flex flex-none items-center">{leading}</span>}
        <RailRowLabel title={labelTitle}>{label}</RailRowLabel>
        {/* Metadata may SHRINK (no flex-none) so a long liveness phrase or a
            long timestamp truncates instead of pushing the row wider than the
            rail; the 60% ceiling keeps the identity legible while it does. */}
        {trailing && (
          <span className="flex min-w-0 max-w-[60%] items-center gap-1.5">{trailing}</span>
        )}
        {actions && <span className="-mr-1 flex flex-none items-center">{actions}</span>}
      </div>
      {children}
    </li>
  );
}

/** A row's identity: the one place `font-medium` is spent inside a body. */
export function RailRowLabel({
  children,
  title,
}: {
  children: ReactNode;
  title?: string;
}) {
  return (
    <span
      title={title}
      className="min-w-0 flex-1 truncate font-medium text-[hsl(var(--foreground))]"
    >
      {children}
    </span>
  );
}

/** Trailing metadata — a timestamp, a count, a state word. `tabular-nums` so a
 *  column of times and counts stays aligned as values change. */
export function RailMeta({
  children,
  tone = "muted",
  title,
  icon,
}: {
  children: ReactNode;
  tone?: RailTone;
  title?: string;
  icon?: ReactNode;
}) {
  return (
    <span
      title={title}
      className={cn(
        // `text-sm` like every rail word; `tabular-nums` keeps a column of
        // times and counts aligned as values change. Metadata reads as lower
        // rank through its muted tone, not a smaller font.
        "flex min-w-0 items-center gap-1 text-sm tabular-nums",
        RAIL_TONE_CLS[tone],
      )}
    >
      {icon}
      {/* The ellipsis has to live on the TEXT, not on the flex row: `truncate`
          on a flex container clips its children without ever rendering an
          ellipsis, which is the silent half-working version of this. */}
      <span className="truncate">{children}</span>
    </span>
  );
}

/**
 * A row's secondary line — a note, a summary, a warning that names names.
 *
 * Truncates by default (a note is qualifying text; the row's identity must
 * stay the widest thing on the line). `wrap` is for the case where the text IS
 * the value — a list of collection names that got no documents is useless
 * clipped at the rail's edge. It is a prop and not a caller-supplied class
 * because `truncate` bundles `whitespace-nowrap`, and overriding half a bundle
 * through `cn()` is exactly the kind of quiet no-op this rail is being cleaned
 * of.
 */
export function RailNote({
  children,
  tone = "muted",
  wrap = false,
  title,
  className,
}: {
  children: ReactNode;
  tone?: RailTone;
  wrap?: boolean;
  /** Full text for a note the row had to clip. */
  title?: string;
  className?: string;
}) {
  return (
    <p
      title={title}
      className={cn(
        // `text-sm` like every rail word; the note's lower rank than the row
        // identity above it is carried by tone and position, not a smaller font.
        "text-sm",
        wrap ? "break-words" : "truncate",
        RAIL_TONE_CLS[tone],
        className,
      )}
    >
      {children}
    </p>
  );
}

/** Shared from the kit: the calm empty state (one "nothing here" size and
 *  rhythm on both rails) and the two-tier glyph scale (a row glyph at row size,
 *  a metadata glyph a step down). Re-exported so section bodies keep one import
 *  surface and size their icons from the metric, never a hardcoded `h-x w-x`. */
export { RailEmpty, railLabelIconCls, railRowIconCls } from "@/components/nav-rail/rows";

/** The one loading placeholder every section shows — same size everywhere, so
 *  a rail mid-fetch doesn't read as five differently-shaped gaps. Indented to
 *  the shared text column. */
export function RailSkeleton() {
  return (
    <div className={cn("py-1", RAIL_PAD_X)}>
      <div className="h-4 w-28 animate-pulse rounded bg-[hsl(var(--muted))]" />
    </div>
  );
}

/** Label → value stat line (Health). Its own primitive because the anatomy is
 *  genuinely different from `RailRow` — there is no identity/metadata split,
 *  just a term and its reading — but it wears the SAME geometry and type, so
 *  the two sections read as one instrument. */
export function RailStatList({ children }: { children: ReactNode }) {
  return <dl className="space-y-0.5">{children}</dl>;
}

export function RailStat({
  label,
  value,
  tone = "default",
  title,
}: {
  label: ReactNode;
  value: ReactNode;
  tone?: RailTone;
  title?: string;
}) {
  return (
    <div className={cn(RAIL_ROW_CLS, "justify-between")}>
      {/* The READING wins the width fight: a term is recoverable from a clipped
          prefix, a value is not, so the dd never shrinks and the dt truncates. */}
      <dt className="min-w-0 truncate text-[hsl(var(--muted-foreground))]">{label}</dt>
      <dd
        title={title}
        className={cn("flex-none font-medium tabular-nums", RAIL_TONE_CLS[tone])}
      >
        {value}
      </dd>
    </div>
  );
}
