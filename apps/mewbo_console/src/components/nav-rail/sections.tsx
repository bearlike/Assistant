import { Suspense, lazy, useMemo } from "react";
import { useLocation, useSearchParams } from "wouter";
import {
  AppWindow,
  BookOpen,
  Clock,
  FileText,
  Folder,
  FolderGit2,
  History,
  Plus,
  Search,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { useSessions } from "@/hooks/useSessions";
import { useApps } from "@/hooks/useApps";
import { useRecentSearchRuns } from "@/hooks/useAgenticSearch";
import { isDefaultVisibleOrigin } from "@/utils/sessionOrigins";
import { groupByDateBucket } from "@/utils/dateBuckets";
import { formatSessionTime } from "@/utils/time";
import {
  useWikiPage,
  useWikiProjectBySlug,
  useWikiProjects,
} from "@/components/wiki/api/hooks";
import {
  buildHref as buildWikiHref,
  useWikiRoute,
  type PlatformId,
} from "@/components/wiki/router";
import { buildHref as buildAppsHref, useAppsRoute } from "@/components/apps/router";
import {
  RailMetaProvider,
  RailThreadList,
  type RailMeta,
} from "@/components/assistant-ui/thread-list";
import {
  FOCUS_RING,
  RailActionRow,
  RailDateGroup,
  RailEmpty,
  RailRow,
  RailSection,
  railRowCls,
} from "./rows";
import type { ActiveProduct } from "./products";

// The rail is mounted on every route, but the settings section reaches into
// `SettingsModel` and the pane registry — machinery that otherwise lives behind
// the lazily-loaded Settings shell. Loading it eagerly here would drag all of
// it into the bundle every route pays for, so this arm is split the same way
// the shell is.
const SettingsFacetsSection = lazy(() =>
  import("./settingsSection").then((m) => ({ default: m.SettingsFacetsSection })),
);

// How many recent tasks the rail keeps in view; the full filterable list lives
// on the landing page (`/`), where the footer link points.
const RECENT_LIMIT = 15;

/** owner/repo from a `host/owner/repo` slug — the host is noise in a narrow rail
 *  (the full slug stays on the row's title attribute). */
function shortSlug(slug: string): string {
  return slug.split("/").slice(-2).join("/");
}

/**
 * Tasks. "New task" → the landing composer, then the assistant-ui recents
 * (shared runtime + `RailThreadList`). On `/` the recents COLLAPSE to just the
 * action row — the landing page IS the full task list, so showing it twice both
 * clutters the rail and doubles every title in the App smoke test.
 */
function TasksSection() {
  const [location, navigate] = useLocation();
  const { sessions, loading } = useSessions();

  const railMeta = useMemo<RailMeta>(
    () => ({ metaById: new Map(sessions.map((s) => [s.session_id, s])) }),
    [sessions],
  );
  const recents = useMemo(
    () => sessions.filter((s) => isDefaultVisibleOrigin(s.origin)),
    [sessions],
  );
  const isLanding = location === "/";

  return (
    <div>
      <RailSection>
        <RailActionRow icon={Plus} label="New task" onClick={() => navigate("/")} />
      </RailSection>
      {!isLanding && (
        <RailSection label="Recents" icon={History}>
          {loading && recents.length === 0 && <RailEmpty>Loading tasks…</RailEmpty>}
          {!loading && recents.length === 0 && <RailEmpty>No tasks yet.</RailEmpty>}
          <RailMetaProvider value={railMeta}>
            <RailThreadList limit={RECENT_LIMIT} />
          </RailMetaProvider>
          {recents.length > RECENT_LIMIT && (
            <button
              type="button"
              onClick={() => navigate("/")}
              // Consume the kit's row geometry/spacing/resting type (`railRowCls`)
              // instead of hand-spelling it; the "see more" affordance keeps its
              // lighter treatment (text-brighten on hover, no fill) so it doesn't
              // compete with the actual recents above it.
              className={cn(
                railRowCls,
                "mt-1 text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]",
                FOCUS_RING,
              )}
            >
              View all tasks
            </button>
          )}
        </RailSection>
      )}
    </div>
  );
}

/**
 * Search. "New search" → the search landing, then recent runs across every
 * workspace (`useRecentSearchRuns`, date-bucketed). A row opens the run's
 * snapshot at the canonical `/search?ws=&run=` URL the search view reads.
 */
function SearchSection() {
  const [, navigate] = useLocation();
  const [params] = useSearchParams();
  const activeRun = params.get("run");
  const { data: runs = [] } = useRecentSearchRuns();

  const groups = useMemo(
    () => groupByDateBucket(runs, (r) => r.created_at),
    [runs],
  );

  return (
    <div>
      <RailSection>
        <RailActionRow
          icon={Search}
          label="New search"
          onClick={() => navigate("/search")}
        />
      </RailSection>
      <RailSection label="Recent searches" icon={History}>
      {runs.length === 0 && <RailEmpty>Searches you run appear here.</RailEmpty>}
      {groups.map((group) => (
        <RailDateGroup key={group.bucket} label={group.bucket} icon={Clock}>
          {group.items.map((run) => (
            <RailRow
              key={run.run_id}
              label={run.query || "Untitled search"}
              title={run.query}
              active={activeRun === run.run_id}
              trailing={
                <span className="text-sm text-[hsl(var(--muted-foreground))]">
                  {formatSessionTime(run.created_at)}
                </span>
              }
              onClick={() =>
                navigate(
                  `/search?ws=${encodeURIComponent(run.workspace_id)}&run=${encodeURIComponent(run.run_id)}`,
                )
              }
            />
          ))}
        </RailDateGroup>
      ))}
      </RailSection>
    </div>
  );
}

/**
 * The wiki's page tree — zone 3 while a page is open.
 *
 * The rail is SITE navigation, so what belongs here is "move to another page",
 * not "move within this page": a table of contents is intra-document navigation
 * and lives beside the document, on the page itself. An earlier revision had
 * these two swapped.
 *
 * `nav` ships ON the page payload (it is the same tree for every page in a
 * wiki), so this reads the exact `useWikiPage` query the page already
 * populated — cache-deduped by queryKey, no second request, no props threaded
 * from the page, no context.
 *
 * ⚠️ **`nav` is only ever returned ATTACHED to a page fetch** — there is no
 * project-level page-tree endpoint. So a route that sits inside a wiki without
 * naming a page (the graph view) still needs some page to key the fetch off,
 * and that is what `landingPageId` is for: it is validated to exist at finalize
 * and is already what `WikiScreen` uses for repo-level links. The fallback
 * costs nothing — `useWikiProjectBySlug` is a `select` over the SAME
 * `["wiki","projects"]` query this rail section already reads.
 *
 * `activePageId` is deliberately separate from the fetch key. On the graph
 * route the tree is fetched via the landing page, but the reader is not ON that
 * page, so nothing is marked current rather than highlighting a row they are
 * not looking at.
 */
function WikiPagesSection({
  activePageId,
  slug,
  platform,
}: {
  activePageId?: string;
  slug?: string;
  platform?: PlatformId;
}) {
  const [, navigate] = useLocation();
  const project = useWikiProjectBySlug(slug);
  const fetchPageId = activePageId ?? project.data?.landingPageId ?? null;
  const { data: page, isLoading } = useWikiPage(fetchPageId, slug);
  // Memoised because `?? []` mints a fresh array every render, which would make
  // the `parentIds` memo below recompute on each one despite nothing changing.
  const nav = useMemo(() => page?.nav ?? [], [page]);
  // Still resolving which page to key off — without this the section would
  // flash its empty state while the project lookup is in flight.
  const loading = isLoading || (!fetchPageId && project.isLoading);

  // A node is a SECTION when something else declares it as a parent. That is a
  // structural fact about the tree, unlike "is it level 1", which only says
  // where a node sits — a top-level page with no children is still a page.
  const parentIds = useMemo(
    () => new Set(nav.map((n) => n.parent).filter(Boolean)),
    [nav],
  );

  return (
    <div>
      <RailSection label="Pages" icon={FileText}>
      {loading && nav.length === 0 && <RailEmpty>Loading pages…</RailEmpty>}
      {!loading && nav.length === 0 && <RailEmpty>No pages in this wiki.</RailEmpty>}
      {nav.map((entry) => {
        const Icon = parentIds.has(entry.id) ? Folder : FileText;
        return (
          <RailRow
            key={entry.id}
            label={entry.label}
            title={entry.label}
            depth={entry.lvl}
            active={entry.id === activePageId}
            // Recents carry no leading glyph by default (the slot indents titles
            // past the section header and breaks the one-column read). A page
            // tree opts IN because it is heterogeneous: the glyph is what
            // separates a section from a page at a glance, which is a
            // distinction a list of same-shaped recents does not have to make.
            leading={<Icon className="size-4" aria-hidden />}
            onClick={() =>
              navigate(
                buildWikiHref({ kind: "page", pageId: entry.id, slug, platform }),
              )
            }
          />
        );
      })}
      </RailSection>
    </div>
  );
}

/**
 * Wiki. "All wikis" → the gallery, then whatever is inside the current wiki
 * context: the page tree while reading a page, the indexed projects everywhere
 * else. Rows route through the wiki `buildHref` helper (never a hand-built
 * path); a project with no finalized landing page degrades to the gallery
 * rather than guessing a page id.
 *
 * "All wikis" stays above BOTH branches on purpose — a reader deep in a page
 * must never need the browser's Back button to reach the rest of the product.
 * The rule that separates it from the list below comes from `RailSection`, so
 * every product gets that boundary from one place.
 */
function WikiSection() {
  const [, navigate] = useLocation();
  const route = useWikiRoute();
  const wikiSlug = useWikiSlug();
  const { data: projects = [] } = useWikiProjects();

  const allWikis = (
    <RailSection>
      <RailActionRow
        icon={BookOpen}
        label="All wikis"
        onClick={() => navigate("/wiki")}
      />
    </RailSection>
  );

  // Zone 3 shows the page tree on every route that is INSIDE a wiki, not only a
  // page route. `page` and `qa` both name a page directly; `graph` names only a
  // project, so the section keys off its landing page and marks nothing current.
  // The pre-index flows (`welcome`, `indexing`) and the gallery deliberately
  // keep the project list — there is no page tree to show yet, and on the
  // gallery the cross-project list IS the contents of the current context.
  if (route.kind === "page" || route.kind === "qa") {
    return (
      <div>
        {allWikis}
        <WikiPagesSection
          activePageId={route.pageId}
          slug={route.slug}
          platform={route.platform}
        />
      </div>
    );
  }

  if (route.kind === "graph") {
    return (
      <div>
        {allWikis}
        <WikiPagesSection slug={route.slug} platform={route.platform} />
      </div>
    );
  }

  return (
    <div>
      {allWikis}
      <RailSection label="Indexed projects" icon={FolderGit2}>
      {projects.length === 0 && <RailEmpty>Generate your first wiki.</RailEmpty>}
      {projects.map((project) => (
        <RailRow
          key={project.slug}
          label={shortSlug(project.slug)}
          title={project.slug}
          active={wikiSlug === project.slug}
          onClick={() =>
            navigate(
              project.landingPageId
                ? buildWikiHref({
                    kind: "page",
                    pageId: project.landingPageId,
                    slug: project.slug,
                    platform: project.source,
                  })
                : buildWikiHref({ kind: "landing" }),
            )
          }
        />
      ))}
      </RailSection>
    </div>
  );
}

/**
 * Apps. "Create app" → the gallery/composer, then installed (non-archived) apps
 * newest-first, each row carrying the app's own emoji glyph.
 */
function AppsSection() {
  const [, navigate] = useLocation();
  const route = useAppsRoute();
  const activeAppId = route.kind === "detail" ? route.appId : null;
  const { data: apps = [] } = useApps();

  const installed = useMemo(
    () =>
      apps
        .filter((a) => a.status !== "archived")
        .sort((a, b) => (b.updated_at ?? "").localeCompare(a.updated_at ?? "")),
    [apps],
  );

  return (
    <div>
      <RailSection>
        <RailActionRow
          icon={Plus}
          label="Create app"
          onClick={() => navigate("/apps")}
        />
      </RailSection>
      <RailSection label="Installed" icon={AppWindow}>
      {installed.length === 0 && <RailEmpty>Apps you build appear here.</RailEmpty>}
      {installed.map((app) => (
        <RailRow
          key={app.app_id}
          label={app.title}
          leading={
            <span className="text-sm leading-none" aria-hidden>
              {app.icon || "✨"}
            </span>
          }
          active={activeAppId === app.app_id}
          onClick={() => navigate(buildAppsHref({ kind: "detail", appId: app.app_id }))}
        />
      ))}
      </RailSection>
    </div>
  );
}

/** The wiki route's slug, read the same way `useWikiRoute` does — pulled into a
 *  helper so `WikiSection` stays a thin section. */
function useWikiSlug(): string | undefined {
  const [params] = useSearchParams();
  return params.get("slug") ?? undefined;
}

/** The rail's zone 3 — what is inside the current scope. For a product that is
 *  its recents; for Settings it is the facet list. `null` (an unscoped route)
 *  falls back to Tasks, the home product. */
export function ProductSection({ product }: { product: ActiveProduct }) {
  switch (product) {
    case "wiki":
      return <WikiSection />;
    case "search":
      return <SearchSection />;
    case "apps":
      return <AppsSection />;
    case "settings":
      return (
        <Suspense fallback={<RailEmpty>Loading settings…</RailEmpty>}>
          <SettingsFacetsSection />
        </Suspense>
      );
    case "tasks":
    default:
      return <TasksSection />;
  }
}
