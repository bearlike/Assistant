/**
 * One row in the landing page's collapsible "Incomplete indexes" section —
 * a failed/interrupted/cancelled-but-incomplete job with reusable work,
 * offering Resume.
 */
import { Loader2, RotateCcw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cardSurface } from "@/components/ui/card-surface";
import { cn } from "@/lib/utils";

import { RelativeTime } from "../../../utils/relativeTime";
import { RepoLink } from "../RepoLink";
import type { RecoverableJob } from "../api/types";

export function RecoverableJobRow({
  job,
  resuming,
  onResume,
}: {
  job: RecoverableJob;
  /** True while THIS row's resume mutation is in flight. */
  resuming: boolean;
  onResume: () => void;
}) {
  const total = job.totalPages ?? 0;
  const done = job.recoverable.pagesDone;

  return (
    <div className={cn(cardSurface({ radius: "left" }), "flex items-center gap-3 px-3.5 py-2.5")}>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 text-sm font-medium text-[hsl(var(--foreground))]">
          <span className="truncate">
            <RepoLink slug={job.slug} display="short" />
          </span>
          {job.phase && (
            <span className="text-2xs uppercase tracking-wide text-[hsl(var(--muted-foreground))] shrink-0">
              stopped at {job.phase}
            </span>
          )}
        </div>
        <div className="mt-0.5 flex items-center gap-1.5 text-2xs text-[hsl(var(--muted-foreground))] flex-wrap">
          <span className="tabular-nums">
            {done}/{total || "?"} pages
          </span>
          {job.updatedAt && (
            <>
              <span className="opacity-50">·</span>
              <span title={RelativeTime.tooltip(job.updatedAt)}>
                {RelativeTime.format(job.updatedAt)}
              </span>
            </>
          )}
          {job.error && (
            <>
              <span className="opacity-50">·</span>
              <span className="text-[hsl(var(--warning))] truncate">{job.error.message}</span>
            </>
          )}
        </div>
      </div>
      <Button
        type="button"
        variant="neutral"
        size="sm"
        tone="info"
        disabled={resuming}
        onClick={onResume}
        leadingIcon={
          resuming ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <RotateCcw className="h-3 w-3" />
          )
        }
        className="shrink-0"
      >
        Resume
      </Button>
    </div>
  );
}
