/**
 * IndexedSnapshotCaption — render an ``IndexedSnapshot`` as either the
 * uppercase sidebar caption or the landing-card footer line.
 *
 * Both surfaces share the same atomic-class shape; this component picks
 * the variant and lays out the pills (date · branch · commit-SHA), with
 * external SHA / branch links opening in a new tab when the snapshot's
 * platform supports a canonical URL shape.
 */

import { ExternalLink } from "lucide-react";

import { cn } from "@/lib/utils";

import type { IndexedSnapshot, SnapshotPill, SnapshotRender } from "./indexedSnapshot";
import { RelativeTime } from "./relativeTime";

interface CaptionProps {
  snapshot: IndexedSnapshot | null;
  /** Sidebar (uppercase, absolute date) or landing (relative). */
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
          "text-[10px] font-mono uppercase tracking-wider text-[hsl(var(--muted-foreground))]",
          variant === "landing" && "normal-case tracking-normal text-[11px]",
          className
        )}
      >
        {variant === "sidebar" ? "INDEXED" : "Indexed"}
      </div>
    );
  }

  const render: SnapshotRender =
    variant === "sidebar" ? snapshot.formatSidebar() : snapshot.formatLandingCard();

  return (
    <div
      className={cn(
        "inline-flex items-center gap-1.5 flex-wrap text-[10px] font-mono uppercase tracking-wider text-[hsl(var(--muted-foreground))]",
        variant === "landing" && "normal-case tracking-normal text-[11px] font-sans",
        className
      )}
    >
      <span title={render.date.title ?? RelativeTime.tooltip(snapshot.indexedAt)}>
        {render.date.label}
      </span>
      {render.extras.map((pill, i) => (
        <span key={i} className="inline-flex items-center gap-1">
          <span aria-hidden className="opacity-50">·</span>
          <PillContent pill={pill} />
        </span>
      ))}
    </div>
  );
}

function PillContent({ pill }: { pill: SnapshotPill }) {
  if (pill.href) {
    return (
      <a
        href={pill.href}
        target="_blank"
        rel="noreferrer"
        title={pill.title}
        className="inline-flex items-center gap-0.5 hover:text-[hsl(var(--foreground))] transition-colors"
      >
        {pill.label}
        <ExternalLink className="h-2.5 w-2.5 opacity-70" />
      </a>
    );
  }
  return <span title={pill.title}>{pill.label}</span>;
}
