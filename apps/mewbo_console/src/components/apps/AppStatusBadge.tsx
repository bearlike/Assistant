import { cn } from "@/lib/utils";
import type { AppStatus } from "../../types/apps";

/**
 * The one status pill for an app, shared by the gallery card, the detail
 * header, and the settings pane so status reads identically everywhere. It is a
 * state container, so it takes the `rounded-full` silhouette reserved for state
 * (per the console shape vocabulary). Honest by construction: `broken` is a
 * visible destructive tone, never hidden or softened into a neutral grey.
 */

interface AppStatusMeta {
  label: string;
  /** Tailwind classes bound to a semantic token pair (bg tint + text). */
  tone: string;
  /** Live-work statuses pulse; settled ones are static. */
  pulse?: boolean;
}

const STATUS_META: Record<AppStatus, AppStatusMeta> = {
  draft: {
    label: "Draft",
    tone: "bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))]",
  },
  building: {
    label: "Building",
    tone: "bg-[hsl(var(--primary)/0.15)] text-[hsl(var(--primary-text))]",
    pulse: true,
  },
  live: {
    label: "Live",
    tone: "bg-[hsl(var(--success)/0.15)] text-[hsl(var(--success))]",
  },
  paused: {
    label: "Paused",
    tone: "bg-[hsl(var(--warning)/0.15)] text-[hsl(var(--warning))]",
  },
  broken: {
    label: "Broken",
    tone: "bg-[hsl(var(--destructive)/0.15)] text-[hsl(var(--destructive-text))]",
  },
  archived: {
    label: "Archived",
    tone: "bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))]",
  },
};

export function AppStatusBadge({
  status,
  className,
}: {
  status: AppStatus;
  className?: string;
}) {
  const meta = STATUS_META[status];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-2xs font-medium leading-none",
        meta.tone,
        className,
      )}
    >
      <span
        aria-hidden
        className={cn(
          "h-1.5 w-1.5 rounded-full bg-current",
          meta.pulse && "motion-safe:animate-pulse",
        )}
      />
      {meta.label}
    </span>
  );
}
