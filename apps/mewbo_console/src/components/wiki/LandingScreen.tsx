/**
 * Wiki landing screen. Top card has the brand + URL input + Generate button;
 * below is a hero, a search/list-grid toolbar, the project grid, and a
 * footer.
 *
 * Submitting the URL routes to /wiki/configure with the URL pre-loaded.
 * Clicking a project card either opens its wiki (if indexed) or routes to
 * the "Not indexed" welcome page.
 *
 * Card/row rendering and the search/filter derivations live in `landing/` —
 * this file is the shell: hero, toolbar, grid, footer, and the two dialogs.
 */

import { useState } from "react";
import { useLocation } from "wouter";
import {
  BookOpen,
  ChevronDown,
  FileText,
  GitBranch,
  Github,
  Globe,
  LayoutGrid,
  List,
  Loader2,
  Search,
  Sparkles,
  TriangleAlert,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { cardSurface } from "@/components/ui/card-surface";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

import { BrandMark } from "../BrandMark";
import { ProductHero } from "../ProductHero";
import { ActiveJobCard } from "./landing/ActiveJobCard";
import { DeleteWikiDialog } from "./landing/DeleteWikiDialog";
import { ProjectCard } from "./landing/ProjectCard";
import { RecoverableJobRow } from "./landing/RecoverableJobRow";
import { useLandingProjects } from "./landing/useLandingProjects";
import { ProjectSettingsDialog } from "./ProjectSettingsDialog";
import {
  useActiveIndexingJobs,
  useDeleteProject,
  useRecoverableJobs,
  useResumeIndexing,
  useWikiProjects,
} from "./api/hooks";
import type { IndexingJob, Project, RecoverableJob } from "./api/types";
import { buildHref } from "./router";
import { slugFromRepoUrl } from "./slug";

export function LandingScreen() {
  const [, navigate] = useLocation();
  const projectsQuery = useWikiProjects();
  const activeJobsQuery = useActiveIndexingJobs();
  const recoverableJobsQuery = useRecoverableJobs();
  const deleteProjectMutation = useDeleteProject();
  const resumeIndexingMutation = useResumeIndexing();
  const [url, setUrl] = useState("");
  const [search, setSearch] = useState("");
  const [view, setView] = useState<"grid" | "list">("grid");
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const [settingsSlug, setSettingsSlug] = useState<string | null>(null);
  const [incompleteOpen, setIncompleteOpen] = useState(false);

  const { visible, visibleActive, visibleRecoverable } = useLandingProjects(
    search,
    projectsQuery.data,
    activeJobsQuery.data,
    recoverableJobsQuery.data,
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

  // Resume a recoverable job, then drop onto the indexing screen so the user
  // watches the resumed run finish from where it stopped.
  const resumeJob = (job: RecoverableJob) => {
    if (resumeIndexingMutation.isPending) return;
    resumeIndexingMutation.mutate(job.jobId, {
      onSuccess: (res) =>
        navigate(buildHref({ kind: "indexing", jobId: res.jobId, slug: job.slug })),
    });
  };

  const slug = slugFromRepoUrl(url);
  const canGenerate = Boolean(slug);

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
      {/* Top padding is the hero's to own (`ProductHero` sets the offset every
          landing shares), so the wrapper pads the bottom only. */}
      <div className="max-w-[1200px] mx-auto px-4 sm:px-6 pb-6 sm:pb-10 min-h-full flex flex-col">
        <ProductHero
          title="Agentic Wiki"
          subtitle="Auto-generated documentation for code repositories. Paste a repository URL below to generate a new wiki."
        >
          <form
            onSubmit={onGenerate}
            aria-label="Generate wiki for a new repository"
            className="w-full flex items-center justify-center gap-2 flex-wrap"
          >
            <div className="flex items-center gap-2 h-10 px-3 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--muted))] flex-1 min-w-[260px] focus-within:border-[hsl(var(--border-strong))] focus-within:ring-2 focus-within:ring-[hsl(var(--primary))]">
              <GitBranch className="h-4 w-4 text-[hsl(var(--muted-foreground))] shrink-0" />
              <Input
                type="text"
                placeholder="https://github.com/owner/repo"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                spellCheck={false}
                className="flex-1 h-auto border-0 px-0 py-0 shadow-none bg-transparent font-mono placeholder:text-[hsl(var(--muted-foreground))] focus-visible:ring-0"
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
        </ProductHero>

        {/* ── Indexing now (only when there are in-flight jobs) ────────── */}
        {visibleActive.length > 0 && (
          <section className="mt-8 sm:mt-10">
            <div className="flex items-center gap-2 mb-3">
              <Loader2 className="h-3.5 w-3.5 text-[hsl(var(--primary))] animate-spin" />
              <h2 className="text-sm font-medium tracking-tight">Indexing now</h2>
              <span className="text-2xs text-[hsl(var(--muted-foreground))]">
                {visibleActive.length} in progress
              </span>
            </div>
            <div
              className="gap-3.5 grid"
              style={{
                gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))",
              }}
            >
              {visibleActive.map((job) => (
                <ActiveJobCard key={job.jobId} job={job} onOpen={() => openIndexing(job)} />
              ))}
            </div>
          </section>
        )}

        {/* ── Incomplete indexes (resumable failed/interrupted jobs) ────── */}
        {visibleRecoverable.length > 0 && (
          <section className="mt-8 sm:mt-10">
            <button
              type="button"
              onClick={() => setIncompleteOpen((o) => !o)}
              aria-expanded={incompleteOpen}
              className="flex items-center gap-2 mb-3 text-left group/inc"
            >
              <ChevronDown
                className={cn(
                  "h-3.5 w-3.5 text-[hsl(var(--muted-foreground))] transition-transform",
                  incompleteOpen ? "" : "-rotate-90"
                )}
              />
              <TriangleAlert className="h-3.5 w-3.5 text-[hsl(var(--warning))]" />
              <h2 className="text-sm font-medium tracking-tight">Incomplete indexes</h2>
              <span className="text-2xs text-[hsl(var(--muted-foreground))]">
                {visibleRecoverable.length} resumable
              </span>
            </button>
            {incompleteOpen && (
              <div className="flex flex-col gap-2">
                {visibleRecoverable.map((job) => (
                  <RecoverableJobRow
                    key={job.jobId}
                    job={job}
                    resuming={
                      resumeIndexingMutation.isPending &&
                      resumeIndexingMutation.variables === job.jobId
                    }
                    onResume={() => resumeJob(job)}
                  />
                ))}
              </div>
            )}
          </section>
        )}

        {/* ── Toolbar ───────────────────────────────────────────────────── */}
        <div className="mt-10 sm:mt-14 flex items-center gap-3 flex-wrap">
          <div className={cn(cardSurface({ radius: "left" }), "flex items-center gap-2 h-9 px-3 flex-1 min-w-[240px]")}>
            <Search className="h-3.5 w-3.5 text-[hsl(var(--muted-foreground))]" />
            <Input
              type="text"
              placeholder="Search projects by name, owner, or repository…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="h-7 border-0 px-0 bg-transparent placeholder:text-[hsl(var(--muted-foreground))] focus-visible:ring-0"
            />
          </div>
          <div
            className={cn(cardSurface({ radius: "right" }), "inline-flex items-center gap-0.5 p-0.5")}
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
            <ProjectCard
              key={p.slug}
              project={p}
              onOpen={() => openProject(p)}
              onSettings={() => setSettingsSlug(p.slug)}
              onDelete={() => setPendingDelete(p.slug)}
            />
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
                  <FileText className="size-5 text-[hsl(var(--muted-foreground))]" />
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

      <DeleteWikiDialog
        slug={pendingDelete}
        pending={deleteProjectMutation.isPending}
        onOpenChange={(open) => {
          if (!open) setPendingDelete(null);
        }}
        onConfirm={() => {
          if (!pendingDelete) return;
          deleteProjectMutation.mutate(pendingDelete, {
            onSettled: () => setPendingDelete(null),
          });
        }}
      />

      {/* Same dialog the in-project WikiTopBar gear opens — mounted only while
          a card's settings are actually being edited. */}
      {settingsSlug && (
        <ProjectSettingsDialog
          slug={settingsSlug}
          open
          onOpenChange={(open) => {
            if (!open) setSettingsSlug(null);
          }}
        />
      )}
    </div>
  );
}
