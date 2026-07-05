/**
 * Wiki page — three-column grid: sidebar nav, content, right rail.
 *
 *   Sidebar    : "Last indexed" caption + tree of pages from the nav.
 *   Content    : block renderer for the active page; click diagrams to zoom.
 *   TOC rail   : "On this page" with scroll-spy; Featured + Refresh cards.
 *   Q&A dock   : floating, viewport-centered.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocation } from "wouter";
import { Loader2, Network, Sparkles, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { DiagramZoom } from "./DiagramZoom";
import { IndexedSnapshotCaption } from "./IndexedSnapshotCaption";
import { MarkdownBlock } from "./MarkdownBlock";
import { SourceHrefProvider } from "./markdownComponents";
import { QADock } from "./QADock";
import { RefreshThisWiki } from "./RefreshThisWiki";
import { WikiTopBar } from "./WikiTopBar";
import { IndexedSnapshot } from "./indexedSnapshot";
import type { Citation } from "./citations";
import { useWikiPage, useWikiProjectBySlug } from "./api/hooks";
import { buildHref, type PlatformId } from "./router";
import { useStoredModel } from "./useStoredModel";

interface WikiScreenProps {
  pageId: string;
  /** Canonical fully-qualified slug ``host/owner/repo``. */
  slug?: string;
  /** Platform of record — carried into the Graph button URL. */
  platform?: PlatformId;
}

export function WikiScreen({ pageId, slug, platform }: WikiScreenProps) {
  const [, navigate] = useLocation();
  const pageQuery = useWikiPage(pageId, slug);
  const [activeToc, setActiveToc] = useState("page-top");
  const [zoomDiagId, setZoomDiagId] = useState<string | null>(null);
  const [showFeatured, setShowFeatured] = useState(true);
  const [showRefresh, setShowRefresh] = useState(true);
  const [model, setModel] = useStoredModel();

  const page = pageQuery.data;
  const nav = page?.nav ?? [];
  const repoSlug = slug ?? "bearlike/Assistant";
  const projectQuery = useWikiProjectBySlug(repoSlug);
  const snapshot = useMemo(
    () => (projectQuery.data ? IndexedSnapshot.fromProject(projectQuery.data) : null),
    [projectQuery.data]
  );
  // Graph-only (developer-mode) projects carry no documentation pages — show a
  // dedicated empty state whose primary action opens the graph viewer.
  const graphOnly = projectQuery.data?.graphOnly === true;

  // Scroll-spy: pick the heading whose top is just above the offset.
  // rAF-coalesced so multi-event scroll bursts don't queue duplicate work;
  // setState with the same value is bailed by React (Object.is) so the
  // upstream MarkdownBlock subtree doesn't get re-triggered while the
  // active heading is unchanged.
  useEffect(() => {
    if (!page) return;
    const ids = ["page-top", ...page.toc.map((t) => t.id).filter(Boolean)];
    let ticking = false;
    const measure = () => {
      ticking = false;
      const offset = 120;
      let current = ids[0];
      for (const id of ids) {
        const el = document.getElementById(id);
        if (!el) continue;
        const top = el.getBoundingClientRect().top;
        if (top < offset) current = id;
      }
      setActiveToc(current);
    };
    const onScroll = () => {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(measure);
    };
    measure();
    const scroller = document.getElementById("wiki-scroller") ?? window;
    scroller.addEventListener("scroll", onScroll, { passive: true });
    return () => scroller.removeEventListener("scroll", onScroll);
  }, [page]);

  // Stable callbacks so MarkdownBlock's component map memo bails on
  // scroll-spy ticks (otherwise every scroll event creates fresh closures
  // and re-runs react-markdown's tree walk).
  const goPage = useCallback(
    (id: string) => {
      navigate(buildHref({ kind: "page", pageId: id, slug }));
      const root = document.getElementById("wiki-scroller");
      if (root) root.scrollTo({ top: 0, behavior: "auto" });
    },
    [navigate, slug]
  );

  // Resolve a cited source to its repo blob URL so citation chips open the
  // file (at the cited lines) in a new tab. Null → chip keeps its scroll-to
  // fallback. Keyed on the snapshot so the memo is stable across scroll ticks.
  const resolveSourceHref = useCallback(
    (c: Citation) => snapshot?.sourceUrl(c.path, c.startLine, c.endLine) ?? null,
    [snapshot]
  );

  const onJump = (id: string) => {
    const el = document.getElementById(id);
    const scroller = document.getElementById("wiki-scroller");
    if (!el || !scroller) return;
    const top =
      el.getBoundingClientRect().top -
      scroller.getBoundingClientRect().top +
      scroller.scrollTop -
      72;
    scroller.scrollTo({ top, behavior: "smooth" });
  };

  const onAsk = (question: string) => {
    navigate(
      buildHref({ kind: "qa", question, pageId, slug, model })
    );
  };

  const tocItems = useMemo(() => page?.toc ?? [], [page]);

  return (
    <div className="flex flex-col flex-1 overflow-hidden">
      <WikiTopBar
        repo={repoSlug}
        platform={platform}
        maintainerEdited={snapshot?.maintainerEdited ?? false}
        badgePageId={projectQuery.data?.landingPageId ?? pageId}
        showEditWiki
        showBackToAll
      />
      <div id="wiki-scroller" className="flex-1 overflow-y-auto pb-32">
        {graphOnly ? (
          <GraphOnlyEmptyState
            href={buildHref({ kind: "graph", slug: repoSlug, platform })}
          />
        ) : pageQuery.isLoading || !page ? (
          <div className="flex items-center justify-center py-20 text-sm text-[hsl(var(--muted-foreground))]">
            <Loader2 className="h-4 w-4 mr-2 animate-spin" />
            Loading page…
          </div>
        ) : (
          <div className="max-w-[1400px] mx-auto grid grid-cols-1 lg:grid-cols-[260px_minmax(0,1fr)_240px] gap-0">
            {/* Sidebar */}
            <aside className="hidden lg:block border-r border-[hsl(var(--border))] px-3 py-6 sticky top-0 self-start max-h-screen overflow-y-auto">
              <IndexedSnapshotCaption snapshot={snapshot} className="px-2 mb-2.5" />
              <nav className="space-y-px">
                {nav.map((n) => (
                  <button
                    key={n.id}
                    type="button"
                    onClick={() => goPage(n.id)}
                    className={cn(
                      "w-full text-left text-[13px] px-2 py-1 rounded transition-colors block",
                      n.lvl === 1 && "pl-2 font-medium",
                      n.lvl === 2 && "pl-4",
                      n.lvl === 3 && "pl-6 text-[12px]",
                      n.id === pageId
                        ? "bg-[hsl(var(--muted))]/70 text-[hsl(var(--foreground))]"
                        : "text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))]/30"
                    )}
                  >
                    <span className="truncate block [text-wrap:pretty]">{n.label}</span>
                  </button>
                ))}
              </nav>
            </aside>

            {/* Content */}
            <main className="px-5 sm:px-14 py-8 max-w-[880px] w-full mx-auto">
              <h1
                id="page-top"
                className="text-[clamp(24px,3vw,30px)] font-semibold tracking-[-0.02em] mb-6 [text-wrap:balance]"
              >
                {page.title}
              </h1>
              <SourceHrefProvider resolve={resolveSourceHref}>
                <MarkdownBlock
                  body={page.body}
                  frontmatter={page.frontmatter}
                  onNavigatePage={goPage}
                  onZoomDiagram={setZoomDiagId}
                />
              </SourceHrefProvider>
            </main>

            {/* Right rail — maintainer-edited badge lives in WikiTopBar
                (single source of truth; DRY). */}
            <aside className="hidden xl:block px-3 py-6 sticky top-0 self-start max-h-screen overflow-y-auto">
              {showFeatured && (
                <div className="relative rounded-lg border border-[hsl(var(--primary))]/30 bg-[hsl(var(--primary))]/[0.05] p-3 text-xs text-[hsl(var(--muted-foreground))] mb-3">
                  <button
                    type="button"
                    onClick={() => setShowFeatured(false)}
                    aria-label="Dismiss"
                    className="absolute top-1.5 right-1.5 inline-flex items-center justify-center w-5 h-5 rounded-md hover:bg-[hsl(var(--accent))]"
                  >
                    <X className="h-3 w-3" />
                  </button>
                  <div className="inline-flex items-center gap-1.5 mb-1 text-[hsl(var(--primary))]">
                    <Sparkles className="h-3 w-3" />
                    <span className="font-medium">Featured</span>
                  </div>
                  <div>
                    This wiki is featured in the repository.
                  </div>
                </div>
              )}

              {showRefresh && (
                <RefreshThisWiki slug={repoSlug} onDismiss={() => setShowRefresh(false)} />
              )}

              <div className="mt-5 text-[10px] uppercase tracking-wider font-medium text-[hsl(var(--muted-foreground))] mb-1.5 px-1">
                On this page
              </div>
              <nav className="space-y-px text-[12px]">
                {tocItems.map((t) => (
                  <button
                    key={t.id}
                    type="button"
                    onClick={() => onJump(t.id)}
                    className={cn(
                      "block w-full text-left px-2 py-1 border-l-2 transition-colors",
                      t.lvl === 1 && "pl-2",
                      t.lvl === 2 && "pl-3",
                      t.lvl === 3 && "pl-5 text-[11px]",
                      activeToc === t.id
                        ? "border-[hsl(var(--primary))] text-[hsl(var(--primary))]"
                        : "border-transparent text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]"
                    )}
                  >
                    {t.label}
                  </button>
                ))}
              </nav>
            </aside>
          </div>
        )}
      </div>

      {!graphOnly && (
        <QADock
          placeholder={`Ask MewboWiki about ${repoSlug}`}
          model={model}
          onModelChange={setModel}
          onAsk={onAsk}
        />
      )}

      <DiagramZoom diagramId={zoomDiagId} onClose={() => setZoomDiagId(null)} />
    </div>
  );
}

/**
 * Empty state shown for a project indexed in graph-only (developer) mode:
 * no documentation pages exist, so the primary action is to open the graph
 * viewer. Atomic, single-purpose; composes the shared `Button` (`asChild`
 * link) over the existing `/wiki/graph` route — no bespoke nav.
 */
function GraphOnlyEmptyState({ href }: { href: string }) {
  return (
    <div className="flex items-center justify-center px-4 py-20">
      <div className="max-w-[520px] w-full rounded-2xl border border-[hsl(var(--border))] bg-[hsl(var(--card))] shadow-[0_6px_22px_rgba(0,0,0,0.12)] p-8 text-center">
        <span
          aria-hidden
          className="inline-flex items-center justify-center w-12 h-12 rounded-full bg-[hsl(var(--primary))]/10 text-[hsl(var(--primary))]"
        >
          <Network className="h-6 w-6" />
        </span>
        <h2 className="mt-4 text-lg font-semibold tracking-[-0.01em]">
          No documentation available
        </h2>
        <p className="mt-2 text-sm text-[hsl(var(--muted-foreground))] [text-wrap:pretty]">
          This project was indexed in graph-only (developer) mode, so no
          documentation pages were generated. The AST code graph is ready to
          explore.
        </p>
        <div className="mt-5 flex items-center justify-center">
          <Button variant="primary" size="md" asChild>
            <a href={href} className="inline-flex items-center gap-1.5">
              <Network className="h-4 w-4" />
              Explore the graph
            </a>
          </Button>
        </div>
      </div>
    </div>
  );
}
