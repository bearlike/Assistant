/**
 * Wiki page — two-column grid: content, right rail.
 *
 *   Content    : block renderer for the active page; click diagrams to zoom.
 *   Right rail : Featured card, provenance + re-index card
 *                (`RefreshThisWiki`), then "On this page" with scroll-spy
 *                (`WikiToc`) — intra-document navigation belongs beside the
 *                document.
 *   Q&A dock   : floating, viewport-centered.
 *
 * Page-to-page (site) navigation — the wiki's page tree — no longer lives on
 * this screen. It moved onto the app's own NavRail (`WikiPagesSection` in
 * `nav-rail/sections.tsx`, keyed off the same cached `useWikiPage` payload
 * this screen fetches), which is what freed the left column this grid used
 * to reserve for it.
 *
 * Below the grid's breakpoint nothing is lost: a sticky trigger strip opens
 * a Sheet drawer rendering the SAME `RefreshThisWiki` / `WikiToc` components
 * the desktop right rail mounts (the `AppLayout` mobile-drawer convention).
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocation } from "wouter";
import { ListTree, Loader2, Network, Sparkles, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { cardSurface } from "@/components/ui/card-surface";
import { cn } from "@/lib/utils";

import { DiagramZoom } from "./DiagramZoom";
import { MarkdownBlock } from "./MarkdownBlock";
import { SourceHrefProvider } from "./markdownComponents";
import { QADock } from "./QADock";
import { RefreshThisWiki } from "./RefreshThisWiki";
import { WikiToc } from "./WikiToc";
import { WikiTopBar } from "./WikiTopBar";
import { IndexedSnapshot } from "./indexedSnapshot";
import type { Citation } from "./citations";
import { useWikiPage, useWikiProjectBySlug } from "./api/hooks";
import { buildHref, type PlatformId } from "./router";
import { DEFAULT_WIKI_SLUG } from "./slug";
import { useStoredModel } from "./useStoredModel";
import { useStoredQaMode } from "./useStoredQaMode";

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
  // Bumped by surfaces that hand off to the re-index CTA (settings dialog);
  // the snapshot card opens its confirm step rather than a second path.
  const [refreshSignal, setRefreshSignal] = useState(0);
  const [tocOpen, setTocOpen] = useState(false);
  const [model, setModel] = useStoredModel();
  const [mode, setMode] = useStoredQaMode();

  const page = pageQuery.data;
  const repoSlug = slug ?? DEFAULT_WIKI_SLUG;
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

  const onNavigate = (id: string) => {
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
        showSettings
        onRefresh={() => setRefreshSignal((n) => n + 1)}
        body={page?.body}
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
          <>
            {/* Mobile/tablet drawer trigger — sticky so it stays reachable
                mid-article. Covers the < xl ToC rail; ≥ xl the strip is
                gone because the rail itself is visible. Page-to-page nav has
                no trigger here any more — the app's own NavRail already
                covers every viewport. */}
            <div className="xl:hidden sticky top-0 z-10 border-b border-[hsl(var(--border))] bg-[hsl(var(--background))]/95 backdrop-blur-sm">
              <div className="max-w-[1400px] mx-auto px-2 sm:px-4 h-10 flex items-center justify-end gap-2">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setTocOpen(true)}
                  leadingIcon={<ListTree className="h-3.5 w-3.5" />}
                  aria-label="Open on-this-page navigation"
                >
                  On this page
                </Button>
              </div>
            </div>

            <div className="max-w-[1400px] mx-auto grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_240px] gap-0">
            {/* Content — a `<div>`, not `<main>`: `AppLayout.tsx` already
                renders the app shell's `<main>` on every route, and a
                nested `<main>` here is invalid landmark structure that
                leaves "skip to main content" pointed at an ambiguous
                target. */}
            <div className="px-6 sm:px-14 py-8 max-w-[880px] w-full mx-auto">
              <h1
                id="page-top"
                className="text-2xl font-semibold tracking-[-0.02em] mb-6 [text-wrap:balance]"
              >
                {page.title}
              </h1>
              <SourceHrefProvider resolve={resolveSourceHref} platform={snapshot?.source ?? null}>
                <MarkdownBlock
                  body={page.body}
                  frontmatter={page.frontmatter}
                  onNavigatePage={goPage}
                  onZoomDiagram={setZoomDiagId}
                />
              </SourceHrefProvider>
            </div>

            {/* Right rail — maintainer-edited badge lives in WikiTopBar
                (single source of truth; DRY). Labelled as its own
                complementary region ("Page details": provenance + re-index +
                on-this-page nav) — distinct from `WikiToc`'s own inner
                `<nav aria-label="On this page">`, which names the list, not
                the region around it. */}
            <aside
              aria-label="Page details"
              className="hidden xl:block px-3 py-6 sticky top-0 self-start max-h-screen overflow-y-auto"
            >
              {/* Featured card: gated on the persisted grounder-presence flag
                  (.mewbo/wiki.json or .devin/wiki.json detected at index
                  time) — a wiki without a maintainer grounder is not
                  "featured in the repository". */}
              {showFeatured && snapshot?.maintainerEdited && (
                <div className="relative rounded-lg border border-[hsl(var(--primary))]/30 bg-[hsl(var(--primary))]/[0.05] p-3 text-xs text-[hsl(var(--muted-foreground))] mb-3">
                  <button
                    type="button"
                    onClick={() => setShowFeatured(false)}
                    aria-label="Dismiss"
                    className="absolute top-1.5 right-1.5 inline-flex items-center justify-center w-5 h-5 rounded-md hover:bg-[hsl(var(--accent))]"
                  >
                    <X className="h-3 w-3" />
                  </button>
                  <div className="inline-flex items-center gap-1.5 mb-1 text-[hsl(var(--primary-text))]">
                    <Sparkles className="h-3 w-3" />
                    <span className="font-medium">Featured</span>
                  </div>
                  <div>
                    This wiki is featured in the repository.
                  </div>
                </div>
              )}

              {/* Provenance + drift + re-index in one card, above the
                  in-page ToC — the snapshot facts and the remedy stay
                  adjacent, and both are "about this page". */}
              <RefreshThisWiki
                slug={repoSlug}
                snapshot={snapshot}
                openSignal={refreshSignal}
                className="mb-3"
              />

              <WikiToc items={tocItems} activeId={activeToc} onNavigate={onNavigate} />
            </aside>
            </div>
          </>
        )}
      </div>

      {/* Drawer mount for the sticky trigger strip above — the same
          components the desktop right rail renders, per the AppLayout Sheet
          convention. Selecting an entry closes the drawer first so the
          scroll target isn't behind the overlay. */}
      <Sheet open={tocOpen} onOpenChange={setTocOpen}>
        <SheetContent side="right" className="w-[86vw] max-w-xs p-0">
          <SheetTitle className="sr-only">On this page</SheetTitle>
          <div className="h-full overflow-y-auto px-3 py-6">
            {/* Mirrors the desktop right rail — provenance + re-index above
                the in-page ToC. */}
            <RefreshThisWiki
              slug={repoSlug}
              snapshot={snapshot}
              openSignal={refreshSignal}
              className="mb-3"
            />
            <WikiToc
              items={tocItems}
              activeId={activeToc}
              onNavigate={(id) => {
                setTocOpen(false);
                onNavigate(id);
              }}
            />
          </div>
        </SheetContent>
      </Sheet>

      {!graphOnly && (
        <QADock
          placeholder={`Ask MewboWiki about ${repoSlug}`}
          model={model}
          onModelChange={setModel}
          mode={mode}
          onModeChange={setMode}
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
      <div className={cn(cardSurface({ radius: "modal", elevation: "elev-3" }), "max-w-[520px] w-full p-8 text-center")}>
        <span
          aria-hidden
          className="inline-flex items-center justify-center w-12 h-12 rounded-full bg-[hsl(var(--primary))]/10 text-[hsl(var(--primary))]"
        >
          {/* Hero mark inside the 48px badge — sits above the icon ramp by design. */}
          <Network className="h-6 w-6" />
        </span>
        <h2 className="mt-4 text-base font-medium tracking-[-0.01em]">
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
