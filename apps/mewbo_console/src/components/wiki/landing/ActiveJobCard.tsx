/**
 * One in-flight indexing job tile in the landing page's "Indexing now"
 * section. Progress comes from the same `IndexingProgress` atomic class the
 * indexing page itself uses, so the bar/label/ETA never disagree between
 * the two surfaces.
 */
import { Loader2 } from "lucide-react";

import { cn } from "@/lib/utils";

import { IndexingProgress } from "../progress";
import { RepoLink } from "../RepoLink";
import type { IndexingJob } from "../api/types";
import { PlatformIcon } from "../configure-wizard/PlatformIcon";
import { parseSlug } from "../slug";

export function ActiveJobCard({ job, onOpen }: { job: IndexingJob; onOpen: () => void }) {
  const progress = IndexingProgress.fromJob(job);
  const pct = progress.pct;
  const etaLabel = IndexingProgress.formatEta(progress.etaSeconds);

  return (
    <article
      tabIndex={0}
      role="button"
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === "Enter") onOpen();
      }}
      className={cn(
        "group relative rounded-xl border p-4 cursor-pointer transition-all",
        "hover:-translate-y-px [box-shadow:var(--elev-1)] hover:[box-shadow:var(--elev-2)]",
        "border-[hsl(var(--primary))]/40 bg-[hsl(var(--primary))]/[0.04] hover:border-[hsl(var(--primary))]/60"
      )}
    >
      <div className="flex items-start gap-2.5">
        {job.platform ? (
          <PlatformIcon
            platformId={job.platform}
            className="h-4 w-4 mt-0.5 text-[hsl(var(--muted-foreground))] shrink-0"
          />
        ) : (
          <Loader2 className="h-4 w-4 mt-0.5 text-[hsl(var(--primary))] animate-spin shrink-0" />
        )}
        <div className="flex-1 min-w-0">
          <h3 className="text-sm font-medium text-[hsl(var(--foreground))] truncate">
            <RepoLink slug={job.slug} display="short" />
          </h3>
          {parseSlug(job.slug)?.host && (
            <div className="text-2xs font-mono text-[hsl(var(--muted-foreground))] truncate">
              {parseSlug(job.slug)?.host}
            </div>
          )}
        </div>
        <span className="text-2xs uppercase tracking-wide text-[hsl(var(--primary-text))]">
          {progress.phase}
        </span>
      </div>
      <div className="mt-3 h-1 rounded-full bg-[hsl(var(--muted))]/60 overflow-hidden">
        <div
          className="h-full bg-gradient-to-r from-[hsl(var(--primary))] to-[hsl(var(--primary))]/70 transition-[width] duration-300"
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="mt-2 flex items-center gap-1.5 text-2xs text-[hsl(var(--muted-foreground))]">
        {job.platform && (
          <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-[hsl(var(--muted))]/60">
            <PlatformIcon platformId={job.platform} className="h-2.5 w-2.5" />
            {job.platform}
          </span>
        )}
        <span className="inline-flex items-center gap-1 ml-auto tabular-nums">
          {`${pct}% · ${progress.label}`}
          {progress.statusLine && <span className="opacity-70 ml-1.5">· {progress.statusLine}</span>}
          {etaLabel && <span className="opacity-70 ml-1.5">· {etaLabel}</span>}
        </span>
      </div>
    </article>
  );
}
