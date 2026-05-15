/**
 * Wiki landing screen. Top card has the brand + URL input + Generate button;
 * below is a hero, a search/list-grid toolbar, the project grid, and a
 * footer.
 *
 * Submitting the URL routes to /wiki/configure with the URL pre-loaded.
 * Clicking a project card either opens its wiki (if indexed) or routes to
 * the "Not indexed" welcome page.
 */

import { useMemo, useState } from "react";
import { useLocation } from "wouter";
import {
  BookOpen,
  ChevronRight,
  FileText,
  GitBranch,
  Github,
  Globe,
  LayoutGrid,
  List,
  Loader2,
  Search,
  Sparkles,
  Trash2,
  TriangleAlert,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

import { BrandMark } from "./BrandMark";
import { IndexingProgress } from "./progress";
import { IndexedSnapshot } from "./indexedSnapshot";
import { IndexedSnapshotCaption } from "./IndexedSnapshotCaption";
import { RepoLink } from "./RepoLink";
import { useActiveIndexingJobs, useDeleteProject, useWikiProjects } from "./api/hooks";
import type { IndexingJob, Project } from "./api/types";
import { PlatformIcon } from "./configure-wizard/PlatformIcon";
import { buildHref } from "./router";
import { parseSlug, slugFromRepoUrl } from "./slug";

// Slug parsing is canonical: ``host/owner/repo``. Delegated to the
// shared helper so the wizard, the landing page, and the FE in general
// all agree on identity composition.

export function LandingScreen() {
  const [, navigate] = useLocation();
  const projectsQuery = useWikiProjects();
  const activeJobsQuery = useActiveIndexingJobs();
  const deleteProjectMutation = useDeleteProject();
  const [url, setUrl] = useState("");
  const [search, setSearch] = useState("");
  const [view, setView] = useState<"grid" | "list">("grid");
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);

  // Slugs currently indexing — used to suppress the project tile if a
  // legacy Project row exists alongside a fresh re-index for the same
  // slug. The in-flight tile takes precedence; once the job finalises,
  // the project tile reappears with the new Project record.
  const activeSlugs = useMemo(
    () => new Set((activeJobsQuery.data ?? []).map((j) => j.slug)),
    [activeJobsQuery.data]
  );

  const openIndexing = (job: IndexingJob) => {
    navigate(
      buildHref({
        kind: "indexing",
        jobId: job.jobId,
        slug: job.slug,
        platform: job.platform,
      })
    );
  };

  const slug = slugFromRepoUrl(url);
  const canGenerate = Boolean(slug);

  // Search matches against the full canonical slug (host/owner/repo) so
  // users can find their repo by org, repo, or host fragment — naturally
  // covering enterprise instances like "hurricane" or "git.example.io".
  const visible = useMemo(() => {
    const all = projectsQuery.data ?? [];
    const filteredByActive = all.filter((p) => !activeSlugs.has(p.slug));
    const q = search.trim().toLowerCase();
    if (!q) return filteredByActive;
    return filteredByActive.filter((p) => p.slug.toLowerCase().includes(q));
  }, [projectsQuery.data, search, activeSlugs]);

  const visibleActive = useMemo(() => {
    const all = activeJobsQuery.data ?? [];
    const q = search.trim().toLowerCase();
    if (!q) return all;
    return all.filter((j) => j.slug.toLowerCase().includes(q));
  }, [activeJobsQuery.data, search]);

  const onGenerate = (e?: React.FormEvent) => {
    e?.preventDefault();
    if (!url.trim()) return;
    navigate(buildHref({ kind: "configure", url: url.trim() }));
  };

  // Anything listed in /v1/wiki/projects is — by definition — indexed.
  // Route to its landing page; fall back to the welcome ("not indexed")
  // screen only for legacy records that pre-date landingPageId.
  const openProject = (p: Project) => {
    if (p.landingPageId) {
      navigate(
        buildHref({
          kind: "page",
          pageId: p.landingPageId,
          slug: p.slug,
          platform: p.source,
        })
      );
    } else {
      navigate(
        buildHref({ kind: "welcome", slug: p.slug, platform: p.source })
      );
    }
  };

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-[1200px] mx-auto px-4 sm:px-6 py-6 sm:py-10 min-h-full flex flex-col">
        {/* ── Hero column ─ matches HomeView / Agentic Search rhythm so the
            three landings read as one product family: logo stacked above
            the title, balanced subheading, centered form. ─────────────── */}
        <section className="mx-auto max-w-[720px] w-full flex flex-col items-center text-center pt-4 sm:pt-8 pb-2">
          <BrandMark
            size={56}
            className="mb-5 text-[hsl(var(--primary))] drop-shadow-[0_0_40px_hsl(var(--primary)/0.30)]"
          />
          <h1 className="text-4xl sm:text-5xl font-semibold tracking-tight mb-2.5 [text-wrap:balance]">
            Agentic Wiki
          </h1>
          <p className="max-w-[480px] mb-6 text-[15px] leading-[1.5] text-[hsl(var(--muted-foreground))] [text-wrap:balance]">
            Auto-generated documentation for code repositories. Paste a
            repository URL below to generate a new wiki.
          </p>

          <form
            onSubmit={onGenerate}
            aria-label="Generate wiki for a new repository"
            className="w-full flex items-center justify-center gap-2 flex-wrap"
          >
            <div className="flex items-center gap-2 h-10 px-3 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--input))] flex-1 min-w-[260px] focus-within:border-[hsl(var(--border-strong))] focus-within:ring-2 focus-within:ring-[hsl(var(--primary))]/30">
              <GitBranch className="h-4 w-4 text-[hsl(var(--muted-foreground))] shrink-0" />
              <input
                type="text"
                placeholder="https://github.com/owner/repo"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                spellCheck={false}
                className="flex-1 bg-transparent text-sm font-mono outline-none placeholder:text-[hsl(var(--muted-foreground))]"
              />
            </div>
            <Button
              type="submit"
              variant="primary"
              size="md"
              disabled={!canGenerate}
              leadingIcon={<Sparkles className="h-4 w-4" />}
              className="h-10 rounded-lg"
            >
              Generate Wiki
            </Button>
          </form>
        </section>

        {/* ── Indexing now (only when there are in-flight jobs) ────────── */}
        {visibleActive.length > 0 && (
          <section className="mt-8 sm:mt-10">
            <div className="flex items-center gap-2 mb-3">
              <Loader2 className="h-3.5 w-3.5 text-[hsl(var(--primary))] animate-spin" />
              <h2 className="text-sm font-semibold tracking-tight">Indexing now</h2>
              <span className="text-[11px] text-[hsl(var(--muted-foreground))]">
                {visibleActive.length} in progress
              </span>
            </div>
            <div
              className="gap-3.5 grid"
              style={{
                gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))",
              }}
            >
              {visibleActive.map((job) => {
                // Single source of truth: the same atomic class the
                // indexing page uses, so the bar/label/ETA never disagree.
                const progress = IndexingProgress.fromJob(job);
                const pct = progress.pct;
                const etaLabel = IndexingProgress.formatEta(progress.etaSeconds);
                return (
                  <article
                    key={job.jobId}
                    tabIndex={0}
                    role="button"
                    onClick={() => openIndexing(job)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") openIndexing(job);
                    }}
                    className={cn(
                      "group relative rounded-xl border p-4 cursor-pointer transition-all",
                      "hover:-translate-y-px shadow-[var(--elev-1)] hover:shadow-[var(--elev-2)]",
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
                        <h3 className="text-sm font-semibold text-[hsl(var(--foreground))] truncate">
                          <RepoLink slug={job.slug} display="short" />
                        </h3>
                        {parseSlug(job.slug)?.host && (
                          <div className="text-[10px] font-mono text-[hsl(var(--muted-foreground))] truncate">
                            {parseSlug(job.slug)?.host}
                          </div>
                        )}
                      </div>
                      <span className="text-[10px] uppercase tracking-wide text-[hsl(var(--primary))] font-mono">
                        {progress.phase}
                      </span>
                    </div>
                    <div className="mt-3 h-1 rounded-full bg-[hsl(var(--muted))]/60 overflow-hidden">
                      <div
                        className="h-full bg-gradient-to-r from-[hsl(var(--primary))] to-[hsl(var(--primary))]/70 transition-[width] duration-300"
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                    <div className="mt-2 flex items-center gap-1.5 text-[11px] text-[hsl(var(--muted-foreground))]">
                      {job.platform && (
                        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-[hsl(var(--muted))]/60">
                          <PlatformIcon platformId={job.platform} className="h-2.5 w-2.5" />
                          {job.platform}
                        </span>
                      )}
                      <span className="inline-flex items-center gap-1 ml-auto font-mono tabular-nums">
                        {`${pct}% · ${progress.label}`}
                        {etaLabel && (
                          <span className="opacity-70 ml-1.5">· {etaLabel}</span>
                        )}
                      </span>
                    </div>
                  </article>
                );
              })}
            </div>
          </section>
        )}

        {/* ── Toolbar ───────────────────────────────────────────────────── */}
        <div className="mt-10 sm:mt-14 flex items-center gap-3 flex-wrap">
          <div className="flex items-center gap-2 h-9 px-3 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] flex-1 min-w-[240px]">
            <Search className="h-3.5 w-3.5 text-[hsl(var(--muted-foreground))]" />
            <Input
              type="text"
              placeholder="Search projects by name, owner, or repository…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="h-7 border-0 px-0 bg-transparent text-sm placeholder:text-[hsl(var(--muted-foreground))] focus-visible:ring-0"
            />
          </div>
          <div
            className="inline-flex items-center gap-0.5 p-0.5 rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))]"
            role="tablist"
            aria-label="View mode"
          >
            <button
              type="button"
              role="tab"
              aria-pressed={view === "grid"}
              onClick={() => setView("grid")}
              className={cn(
                "h-7 w-7 inline-flex items-center justify-center rounded text-xs transition-colors",
                view === "grid"
                  ? "bg-[hsl(var(--muted))]/80 text-[hsl(var(--foreground))]"
                  : "text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]"
              )}
              title="Grid"
            >
              <LayoutGrid className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              role="tab"
              aria-pressed={view === "list"}
              onClick={() => setView("list")}
              className={cn(
                "h-7 w-7 inline-flex items-center justify-center rounded text-xs transition-colors",
                view === "list"
                  ? "bg-[hsl(var(--muted))]/80 text-[hsl(var(--foreground))]"
                  : "text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]"
              )}
              title="List"
            >
              <List className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>

        {/* ── Project grid ──────────────────────────────────────────────── */}
        <div
          className={cn(
            "mt-4 gap-3.5",
            view === "grid" ? "grid" : "flex flex-col"
          )}
          style={
            view === "grid"
              ? { gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))" }
              : undefined
          }
        >
          {visible.map((p) => (
            <article
              key={p.slug}
              tabIndex={0}
              role="button"
              onClick={() => openProject(p)}
              onKeyDown={(e) => {
                if (e.key === "Enter") openProject(p);
              }}
              className={cn(
                "group relative rounded-xl border p-4 cursor-pointer transition-all",
                "hover:-translate-y-px shadow-[var(--elev-1)] hover:shadow-[var(--elev-2)]",
                p.primary
                  ? "border-[hsl(var(--primary))]/40 bg-[hsl(var(--primary))]/[0.04] hover:border-[hsl(var(--primary))]/60"
                  : "border-[hsl(var(--border))] bg-[hsl(var(--card))] hover:border-[hsl(var(--border-strong))] hover:bg-[hsl(var(--accent))]/40"
              )}
            >
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  setPendingDelete(p.slug);
                }}
                aria-label="Delete this wiki"
                title="Delete this wiki"
                className="absolute top-2 right-2 inline-flex items-center justify-center w-6 h-6 rounded-md text-[hsl(var(--muted-foreground))] hover:text-red-500 hover:bg-red-500/10 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 transition-all"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>

              <div className="flex items-start gap-2.5">
                <PlatformIcon
                  platformId={p.source}
                  className="h-4 w-4 mt-0.5 text-[hsl(var(--muted-foreground))] shrink-0"
                />
                <div className="flex-1 min-w-0">
                  <h3 className="text-sm font-semibold text-[hsl(var(--foreground))] truncate">
                    <RepoLink
                      slug={p.slug}
                      repoUrl={p.repoUrl}
                      display="short"
                    />
                  </h3>
                  {parseSlug(p.slug)?.host && (
                    <div className="text-[10px] font-mono text-[hsl(var(--muted-foreground))] truncate">
                      {parseSlug(p.slug)?.host}
                    </div>
                  )}
                </div>
              </div>

              <p className="mt-2 text-xs text-[hsl(var(--muted-foreground))] [text-wrap:pretty] line-clamp-2">
                {p.desc || (
                  <span className="italic opacity-70">No description available.</span>
                )}
              </p>

              <div className="mt-3 flex items-center gap-1.5 text-[11px] text-[hsl(var(--muted-foreground))]">
                <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-[hsl(var(--muted))]/60">
                  <PlatformIcon platformId={p.source} className="h-2.5 w-2.5" />
                  {p.source}
                </span>
                <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-[hsl(var(--muted))]/60">
                  <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-amber-400" />
                  {p.lang}
                </span>
                <span className="inline-flex items-center gap-1 ml-auto">
                  <FileText className="h-2.5 w-2.5" />
                  {p.pages} pages
                </span>
              </div>

              <div className="mt-2.5 pt-2.5 border-t border-[hsl(var(--border))]/70 flex items-center justify-between text-[11px] text-[hsl(var(--muted-foreground))]">
                <IndexedSnapshotCaption
                  snapshot={IndexedSnapshot.fromProject(p)}
                  variant="landing"
                  className="min-w-0 truncate"
                />
                <span className="inline-flex items-center gap-1 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 transition-opacity text-[hsl(var(--primary))]">
                  Open
                  <ChevronRight className="h-3 w-3" />
                </span>
              </div>
            </article>
          ))}

          {projectsQuery.data &&
            visible.length === 0 &&
            (search.trim().length > 0 ? (
              <div className="col-span-full inline-flex items-center justify-center gap-2 py-12 text-sm text-[hsl(var(--muted-foreground))]">
                <Search className="h-4 w-4" />
                No projects match "<strong>{search}</strong>"
              </div>
            ) : (
              // No search filter, no completed projects → show the "no
              // wikis yet" hint only when there's also no in-flight job.
              // Otherwise the "Indexing now" section above is informative
              // on its own and the extra message would just be noise.
              (projectsQuery.data ?? []).length === 0 &&
              visibleActive.length === 0 && (
                <div className="col-span-full flex flex-col items-center justify-center gap-2 py-16 rounded-xl border border-dashed border-[hsl(var(--border))] bg-[hsl(var(--card))]/40 text-center px-6">
                  <FileText className="h-6 w-6 text-[hsl(var(--muted-foreground))]" />
                  <div className="text-sm font-medium">No wikis indexed yet</div>
                  <div className="text-xs text-[hsl(var(--muted-foreground))]">
                    Paste a repository URL above and click{" "}
                    <span className="font-medium text-[hsl(var(--foreground))]">
                      Generate Wiki
                    </span>{" "}
                    to create your first one.
                  </div>
                </div>
              )
            ))}
        </div>

        {/* ── Footer ─ pinned to viewport bottom when content is short ─── */}
        <footer className="mt-auto pt-10 pb-2 flex items-center justify-between gap-4 flex-wrap text-xs text-[hsl(var(--muted-foreground))]">
          <div className="inline-flex items-center gap-2">
            <span className="text-[hsl(var(--primary))]">
              <BrandMark size={14} />
            </span>
            <span>Agentic Wiki — auto-generated documentation for code repositories</span>
          </div>
          <div className="inline-flex items-center gap-1">
            <a
              href="https://github.com/bearlike/Assistant"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center justify-center w-7 h-7 rounded-md hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))]"
              title="GitHub"
            >
              <Github className="h-3.5 w-3.5" />
            </a>
            <a
              href="https://docs.mewbo.com"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center justify-center w-7 h-7 rounded-md hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))]"
              title="Docs"
            >
              <BookOpen className="h-3.5 w-3.5" />
            </a>
            <a
              href="#"
              onClick={(e) => e.preventDefault()}
              className="inline-flex items-center justify-center w-7 h-7 rounded-md hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))]"
              title="Status"
            >
              <Globe className="h-3.5 w-3.5" />
            </a>
          </div>
        </footer>
      </div>

      <Dialog
        open={pendingDelete !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDelete(null);
        }}
      >
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <TriangleAlert className="h-4 w-4 text-red-500" />
              Delete this wiki?
            </DialogTitle>
            <DialogDescription>
              Would you like to permanently delete this indexed repository?
              {pendingDelete && (
                <span className="block mt-2 font-mono text-xs text-[hsl(var(--foreground))] bg-[hsl(var(--muted))]/60 rounded px-1.5 py-0.5 w-fit">
                  {pendingDelete}
                </span>
              )}
              <span className="block mt-3 text-xs">
                The wiki pages, diagrams, and Q&amp;A history for this
                repository will be removed. This cannot be undone.
              </span>
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="gap-2">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setPendingDelete(null)}
              disabled={deleteProjectMutation.isPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="primary"
              size="sm"
              disabled={deleteProjectMutation.isPending}
              onClick={() => {
                if (!pendingDelete) return;
                deleteProjectMutation.mutate(pendingDelete, {
                  onSettled: () => setPendingDelete(null),
                });
              }}
              className="!bg-red-500 hover:!bg-red-500/90 !text-white"
              leadingIcon={
                deleteProjectMutation.isPending ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Trash2 className="h-3.5 w-3.5" />
                )
              }
            >
              {deleteProjectMutation.isPending ? "Deleting…" : "Delete wiki"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
