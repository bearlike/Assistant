/**
 * One indexed-project tile in the landing page's gallery grid: platform +
 * slug + description, hover-revealed settings/delete affordances, the
 * language + page-count chips, and the indexed-snapshot caption row
 * (freshness badge included).
 */
import { ChevronRight, FileText, Settings, Trash2 } from "lucide-react";

import { cn } from "@/lib/utils";

import { FreshnessBadge } from "../FreshnessBadge";
import { IndexedSnapshot } from "../indexedSnapshot";
import { IndexedSnapshotCaption } from "../IndexedSnapshotCaption";
import { RepoLink } from "../RepoLink";
import type { Project } from "../api/types";
import { PlatformIcon } from "../configure-wizard/PlatformIcon";
import { parseSlug } from "../slug";

export function ProjectCard({
  project: p,
  onOpen,
  onSettings,
  onDelete,
}: {
  project: Project;
  onOpen: () => void;
  onSettings: () => void;
  onDelete: () => void;
}) {
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
        p.primary
          ? "border-[hsl(var(--primary))]/40 bg-[hsl(var(--primary))]/[0.04] hover:border-[hsl(var(--primary))]/60"
          : "border-[hsl(var(--border))] bg-[hsl(var(--card))] hover:border-[hsl(var(--border-strong))] hover:bg-[hsl(var(--accent))]/40"
      )}
    >
      <div className="absolute top-2 right-2 flex items-center gap-0.5 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 transition-all">
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onSettings();
          }}
          aria-label="Wiki settings"
          title="Wiki settings"
          className="inline-flex items-center justify-center w-6 h-6 rounded-md text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] transition-colors"
        >
          <Settings className="h-3.5 w-3.5" />
        </button>
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onDelete();
          }}
          aria-label="Delete this wiki"
          title="Delete this wiki"
          className="inline-flex items-center justify-center w-6 h-6 rounded-md text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--destructive-text))] hover:bg-[hsl(var(--destructive))]/10 transition-colors"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      </div>

      <div className="flex items-start gap-2.5">
        <PlatformIcon
          platformId={p.source}
          className="h-4 w-4 mt-0.5 text-[hsl(var(--muted-foreground))] shrink-0"
        />
        <div className="flex-1 min-w-0">
          <h3 className="text-sm font-medium text-[hsl(var(--foreground))] truncate">
            <RepoLink slug={p.slug} repoUrl={p.repoUrl} display="short" />
          </h3>
          {parseSlug(p.slug)?.host && (
            <div className="text-2xs font-mono text-[hsl(var(--muted-foreground))] truncate">
              {parseSlug(p.slug)?.host}
            </div>
          )}
        </div>
      </div>

      <p className="mt-2 text-xs text-[hsl(var(--muted-foreground))] [text-wrap:pretty] line-clamp-2">
        {p.desc || <span className="italic opacity-70">No description available.</span>}
      </p>

      <div className="mt-3 flex items-center gap-1.5 text-2xs text-[hsl(var(--muted-foreground))]">
        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-[hsl(var(--muted))]/60">
          <PlatformIcon platformId={p.source} className="h-2.5 w-2.5" />
          {p.source}
        </span>
        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-[hsl(var(--muted))]/60">
          {/* Identity hue, not state — see wiki CLAUDE.md QW1 note; do not
              migrate onto a semantic status token. */}
          <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-amber-400" />
          {p.lang}
        </span>
        <span className="inline-flex items-center gap-1 ml-auto">
          <FileText className="size-3" />
          {p.pages} pages
        </span>
      </div>

      <div className="mt-2.5 pt-2.5 border-t border-[hsl(var(--border))] flex items-center justify-between gap-2 text-2xs text-[hsl(var(--muted-foreground))]">
        <div className="flex items-center gap-2 min-w-0">
          <IndexedSnapshotCaption
            snapshot={IndexedSnapshot.fromProject(p)}
            variant="landing"
            className="min-w-0 truncate"
          />
          {/* Every visible card fires its own freshness probe on mount
              (no `enabled` gate here) — deliberate: the server-side
              freshness cache (TTL) + gthread workers + lazy credential
              chain already bound the resulting burst. If galleries grow
              enough to matter, `enabled` is the seam for visibility
              gating (e.g. an IntersectionObserver), not a client cache. */}
          <FreshnessBadge slug={p.slug} className="shrink-0" />
        </div>
        <span className="inline-flex items-center gap-1 shrink-0 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 transition-opacity text-[hsl(var(--primary-text))]">
          Open
          <ChevronRight className="h-3 w-3" />
        </span>
      </div>
    </article>
  );
}
