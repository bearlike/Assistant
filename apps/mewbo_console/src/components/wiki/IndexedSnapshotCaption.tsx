/**
 * IndexedSnapshotCaption — render an ``IndexedSnapshot`` as either the
 * sidebar caption or the landing-card footer line.
 *
 * Both surfaces share the same atomic-class shape; this component picks
 * the variant and lays out the pills, with external SHA / branch links
 * opening in a new tab when the snapshot's platform supports a canonical
 * URL shape. The sidebar variant is a SINGLE visual line by contract:
 * ``formatSidebar()`` folds branch+commit into one ``branch@shortsha``
 * pill and this component pins ``flex-nowrap`` + truncation, so a long
 * branch name ellipsizes instead of wrapping the 260px rail.
 */

import { ExternalLink, GitBranch } from "lucide-react";

import { cn } from "@/lib/utils";

import type { IndexedSnapshot, SnapshotPill, SnapshotRender } from "./indexedSnapshot";
import { RelativeTime } from "../../utils/relativeTime";

interface CaptionProps {
  snapshot: IndexedSnapshot | null;
  /** Sidebar (one line, truncating) or landing (wrapping). Font family no
   *  longer varies by variant — only `PillContent` (branch/commit, real
   *  ids) is mono; the date label is prose and stays sans in both. */
  variant?: "sidebar" | "landing";
  className?: string;
}

export function IndexedSnapshotCaption({
  snapshot,
  variant = "sidebar",
  className,
}: CaptionProps) {
  if (!snapshot) {
    // Placeholder until project loads — keeps layout from jumping.
    return (
      <div
        className={cn(
          "text-2xs text-[hsl(var(--muted-foreground))]",
          className
        )}
      >
        Indexed
      </div>
    );
  }

  const render: SnapshotRender = snapshot.formatLandingCard();

  return (
    <div
      className={cn(
        "inline-flex items-center gap-1.5 text-2xs text-[hsl(var(--muted-foreground))]",
        variant === "sidebar" && "flex-nowrap max-w-full overflow-hidden whitespace-nowrap",
        variant === "landing" && "flex-wrap",
        className
      )}
    >
      <span className="shrink-0" title={render.date.title ?? RelativeTime.tooltip(snapshot.indexedAt)}>
        {render.date.label}
      </span>
      {render.extras.map((pill, i) => (
        <span key={i} className="inline-flex items-center gap-1 min-w-0">
          <span aria-hidden className="opacity-50 shrink-0">·</span>
          <PillContent pill={pill} />
        </span>
      ))}
    </div>
  );
}

function PillContent({ pill }: { pill: SnapshotPill }) {
  const glyph =
    pill.icon === "branch" ? (
      <GitBranch aria-hidden className="size-3 shrink-0 opacity-70" />
    ) : null;
  if (pill.href) {
    return (
      <a
        href={pill.href}
        target="_blank"
        rel="noreferrer"
        title={pill.title}
        className="inline-flex items-center gap-0.5 min-w-0 font-mono hover:text-[hsl(var(--foreground))] transition-colors"
      >
        {glyph}
        <span className="min-w-0 truncate">{pill.label}</span>
        <ExternalLink className="size-3 shrink-0 opacity-70" />
      </a>
    );
  }
  return (
    <span title={pill.title} className="inline-flex items-center gap-0.5 min-w-0 font-mono">
      {glyph}
      <span className="min-w-0 truncate">{pill.label}</span>
    </span>
  );
}
