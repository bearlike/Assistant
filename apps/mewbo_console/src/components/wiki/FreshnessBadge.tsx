/**
 * FreshnessBadge — how far a project's indexed wiki has drifted from its
 * repo's remote HEAD. Lazy per-project: it owns a `useProjectFreshness` query
 * (staleTime ≥5 min, `retry: false`) so a gallery of cards doesn't re-probe on
 * every scroll, and it renders NOTHING on error / absent / indeterminate — the
 * badge only appears when there's something worth saying.
 *
 * State table (mirrors the API contract exactly):
 *   behindBy === 0                                   → "Up to date" (subtle)
 *   behindBy  >  0                                   → "N commits behind" (attention)
 *   behindBy === null && remoteSha && remoteSha≠sha  → "Update available"
 *   anything else                                    → nothing
 *
 * When drift is detected and `onRefresh` is provided, the badge becomes the
 * click target for the project's EXISTING re-index CTA — it doesn't own a
 * refresh of its own.
 */
import { Check, RefreshCw } from "lucide-react";
import { cn } from "@/lib/utils";
import { FOCUS_RING } from "@/components/ui/focus-ring";
import { useProjectFreshness } from "./api/hooks";
import { classifyFreshness } from "./wikiStatus";

type FreshnessState = "fresh" | "behind" | "update";

interface FreshnessBadgeProps {
  slug: string;
  /** Gate the query — pass false to defer probing (e.g. off-screen). */
  enabled?: boolean;
  /** The project's existing refresh / re-index action. Wired to drift states. */
  onRefresh?: () => void;
  className?: string;
}

const STATE_STYLES: Record<FreshnessState, string> = {
  fresh:
    "bg-[hsl(var(--muted))]/40 text-[hsl(var(--muted-foreground))] border-[hsl(var(--border))]",
  behind:
    "bg-[hsl(var(--warning))]/15 text-[hsl(var(--warning-text))] border-[hsl(var(--warning))]/25",
  update:
    "bg-[hsl(var(--primary))]/10 text-[hsl(var(--primary-text))] border-[hsl(var(--primary))]/25",
};

export function FreshnessBadge({ slug, enabled = true, onRefresh, className }: FreshnessBadgeProps) {
  const { data } = useProjectFreshness(slug, enabled);
  if (!data) return null;

  const { remoteSha } = data;

  // The shared freshness kernel (also drives the wiki page's index-status card)
  // is the single source of truth for what counts as behind / update / fresh.
  // The badge keeps its own "stay silent unless there's something to say"
  // stance: `unknown` renders nothing.
  const { cls, behindBy } = classifyFreshness(data);

  let state: FreshnessState | null = null;
  let label = "";
  // Narrow-width form: the same fact in the fewest glyphs, so the badge can
  // ride a phone-width toolbar without pushing its neighbours off the row.
  let shortLabel = "";
  if (cls === "fresh") {
    state = "fresh";
    label = "Up to date";
    shortLabel = "Current";
  } else if (cls === "behind") {
    state = "behind";
    label = `${behindBy} commit${behindBy === 1 ? "" : "s"} behind`;
    shortLabel = `${behindBy} behind`;
  } else if (cls === "update") {
    state = "update";
    label = "Update available";
    shortLabel = "Update";
  }
  if (!state) return null;

  const isDrift = state !== "fresh";
  const Icon = isDrift ? RefreshCw : Check;
  const base = cn(
    "inline-flex shrink-0 items-center gap-1 rounded-full border px-1.5 py-0.5 text-2xs font-medium leading-none whitespace-nowrap",
    STATE_STYLES[state],
    className
  );
  const text = (
    <>
      <span className="hidden sm:inline">{label}</span>
      <span className="sm:hidden">{shortLabel}</span>
    </>
  );

  // Drift states double as the entry point to the existing re-index CTA.
  if (isDrift && onRefresh) {
    return (
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          e.preventDefault();
          onRefresh();
        }}
        className={cn(base, "transition-colors hover:brightness-110", FOCUS_RING)}
        title={`${label} — re-index to pull the latest commits`}
      >
        <Icon className="w-3 h-3 shrink-0" />
        {text}
      </button>
    );
  }

  return (
    <span className={base} title={remoteSha ? `${label} (remote HEAD ${remoteSha.slice(0, 7)})` : label}>
      <Icon className="w-3 h-3 shrink-0" />
      {text}
    </span>
  );
}
