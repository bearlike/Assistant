/**
 * Slim in-pane contextual header for the wiki sub-product, rendered INSIDE
 * the content pane below the left nav rail. Persistent across Configure /
 * Welcome / Indexing / Wiki / QA screens. (Landing has its own card-shaped
 * header instead.)
 *
 * Left: back-to-all-wikis + repo slug + optional "Maintainer Edited" pill.
 * Right: secondary actions (Edit Wiki / Graph / Copy badge / Settings) shown
 * inline ≥ md and collapsed into a "⋯" menu below md (the `AppDetail`
 * overflow convention); the Copy-link CTA stays inline at every width with a
 * shorter label on narrow screens. The rich Edit-Wiki / Copy-badge surfaces
 * render as Popovers from the inline desktop buttons only — `EditWikiBody`
 * and `CopyBadgeBody` are factored out as standalone functions so a second
 * mount (e.g. a menu Dialog) could reuse them, but nothing needs one today:
 * the mobile overflow menu deliberately excludes both (see the
 * "maintainer chores done at a desk" note below).
 *
 * No product wordmark and no theme toggle render here — the left nav rail
 * owns both (active product via its own row, theme via its avatar menu).
 * This header carries only wiki-local context: which repo, what state it's
 * in, and what a maintainer can do to it.
 */

import { useEffect, useState } from "react";
import { useLocation } from "wouter";
import {
  ArrowLeft,
  BadgeCheck,
  BadgePlus,
  Check,
  Copy,
  FileText,
  Info,
  MoreHorizontal,
  Network,
  Pencil,
  Settings,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { CopyButton } from "@/components/CopyButton";
import { copyText } from "@/utils/clipboard";
import { cn } from "@/lib/utils";

import { PlatformIcon } from "./configure-wizard/PlatformIcon";
import { ProjectSettingsDialog } from "./ProjectSettingsDialog";
import { RepoLink } from "./RepoLink";
import { WikiBadge } from "./badge";
import { buildHref, type PlatformId } from "./router";
import { canonicalRepoUrl, shortSlug } from "./slug";

interface WikiTopBarProps {
  /** Canonical slug (``host/owner/repo`` or legacy ``owner/repo``). */
  repo?: string;
  /** Persisted repo URL — preferred over slug-derived ``https://host/...``. */
  repoUrl?: string;
  /** Carried into the Graph button's URL — same params as the rest of /wiki/*. */
  platform?: PlatformId;
  maintainerEdited?: boolean;
  showEditWiki?: boolean;
  showBackToAll?: boolean;
  /** Existing re-index CTA opener — a drift badge reveals it rather than
   *  re-indexing directly (keeps the two-step confirm the CTA owns). */
  onRefresh?: () => void;
  /** Page the README badge should link to — usually the repo's
   *  ``landingPageId`` (falls back to the current page upstream). Absent
   *  → the Copy-badge affordance is hidden (no resolvable wiki to badge). */
  badgePageId?: string;
  /** Show the gear that opens ``ProjectSettingsDialog``. On for
   *  every settled in-project screen; off where there is no project to edit yet
   *  (Configure) or one is mid-flight (Indexing). */
  showSettings?: boolean;
}

export function WikiTopBar({
  repo,
  repoUrl,
  platform,
  maintainerEdited,
  showEditWiki,
  showBackToAll,
  badgePageId,
  showSettings,
  onRefresh,
}: WikiTopBarProps) {
  const [, navigate] = useLocation();
  const [copied, setCopied] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const t = window.setTimeout(() => setCopied(false), 1600);
    return () => window.clearTimeout(t);
  }, [copied]);

  const onCopyLink = async () => {
    await copyText(window.location.href);
    setCopied(true);
  };

  const badge = WikiBadge.forPage({ slug: repo, pageId: badgePageId, platform });
  const graphHref = repo ? buildHref({ kind: "graph", slug: repo, platform }) : null;
  const repoHref = repo ? canonicalRepoUrl(repo, repoUrl) : null;
  // Edit-Wiki and Copy-badge are maintainer chores done at a desk, so they
  // stay off the phone entirely rather than crowding the overflow menu; the
  // menu carries only what a reader plausibly wants on mobile.
  const hasMobileActions = Boolean(graphHref || (showSettings && repo));

  return (
    <div className="border-b border-[hsl(var(--border))] bg-[hsl(var(--card))]/60 backdrop-blur-sm">
      {/* `overflow-hidden` + a min-w-0 slug is the belt-and-braces guarantee:
          whatever the width, this row clips rather than bleeding sideways.
          `h-10` converges this header's row height with the rail's own
          40px action rows — one row height vocabulary across rail and
          content-pane chrome. */}
      <div className="max-w-[1400px] mx-auto px-3 sm:px-6 h-10 flex items-center gap-1.5 sm:gap-3 overflow-hidden">
        {showBackToAll && (
          <button
            type="button"
            onClick={() => navigate("/wiki")}
            className="inline-flex shrink-0 items-center gap-1.5 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors -ml-1 px-1 py-1"
            aria-label="Back to all wikis"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">All wikis</span>
          </button>
        )}

        {repo && (
          repoHref ? (
            // A button, not bare text: it reads as chrome next to its
            // siblings, and the provider glyph says where it leads. The label
            // sheds the host segment below `md` — that prefix is the single
            // widest thing in the row and the icon already carries the host.
            <Button
              variant="ghost"
              size="sm"
              asChild
              className="min-w-0 font-mono text-xs text-[hsl(var(--muted-foreground))]"
              title={`Open ${repo} on its host`}
            >
              <a href={repoHref} target="_blank" rel="noopener noreferrer">
                {platform && <PlatformIcon platformId={platform} className="h-3.5 w-3.5 shrink-0" />}
                {/* `min-w-0` on the text itself — a truncating flex child
                    won't shrink below its content width without it. */}
                <span className="hidden md:inline min-w-0 truncate">{repo}</span>
                <span className="md:hidden min-w-0 truncate">{shortSlug(repo)}</span>
              </a>
            </Button>
          ) : (
            <span className="font-mono text-xs text-[hsl(var(--muted-foreground))] truncate min-w-0">
              <RepoLink slug={repo} repoUrl={repoUrl} />
            </span>
          )
        )}

        {maintainerEdited && (
          <span className="inline-flex shrink-0 items-center gap-1 px-2 h-6 rounded-full border border-[hsl(var(--primary))]/30 bg-[hsl(var(--primary))]/10 text-2xs text-[hsl(var(--primary-text))]">
            <BadgeCheck className="h-3 w-3" />
            <span className="hidden md:inline">Maintainer Edited</span>
          </span>
        )}

        <div className="flex-1" />

        <div className="flex shrink-0 items-center gap-1.5">
          {/* Inline secondary actions — ≥ md only; overflow-x as a last-resort
              guard so the row can never bleed. */}
          <div className="hidden md:flex items-center gap-1.5 overflow-x-auto">
            {showEditWiki && (
              <Popover>
                <PopoverTrigger asChild>
                  <Button
                    variant="ghost"
                    size="sm"
                    leadingIcon={<Pencil className="h-3.5 w-3.5" />}
                    aria-label="Edit wiki"
                  >
                    Edit Wiki
                  </Button>
                </PopoverTrigger>
                <PopoverContent
                  align="end"
                  sideOffset={6}
                  className="w-80 p-0 rounded-lg border-[hsl(var(--border-strong))] bg-[hsl(var(--card))]"
                >
                  <EditWikiBody />
                </PopoverContent>
              </Popover>
            )}

            {graphHref && (
              <Button
                variant="ghost"
                size="sm"
                asChild
                aria-label="Open knowledge graph in new tab"
                title="Knowledge graph"
              >
                <a
                  href={graphHref}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1.5"
                >
                  <Network className="h-3.5 w-3.5" />
                  Graph
                </a>
              </Button>
            )}

            {badge && (
              <Popover>
                <PopoverTrigger asChild>
                  <Button
                    variant="ghost"
                    size="sm"
                    leadingIcon={<BadgePlus className="h-3.5 w-3.5" />}
                    aria-label="Copy README badge"
                    title="Copy README badge"
                  >
                    Copy badge
                  </Button>
                </PopoverTrigger>
                <PopoverContent
                  align="end"
                  sideOffset={6}
                  className="w-80 p-0 rounded-lg border-[hsl(var(--border-strong))] bg-[hsl(var(--card))]"
                >
                  <CopyBadgeBody badge={badge} />
                </PopoverContent>
              </Popover>
            )}

            {showSettings && repo && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setSettingsOpen(true)}
                aria-label="Wiki settings"
                title="Wiki settings"
                leadingIcon={<Settings className="h-3.5 w-3.5" />}
              >
                Settings
              </Button>
            )}
          </div>

          {/* Below md: only the reader-facing actions, in one overflow menu. */}
          {hasMobileActions && (
            <div className="md:hidden">
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="ghost"
                    size="sm"
                    aria-label="More wiki actions"
                    title="More wiki actions"
                    leadingIcon={<MoreHorizontal className="h-3.5 w-3.5" />}
                  />
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-52">
                  {graphHref && (
                    <DropdownMenuItem asChild>
                      <a href={graphHref} target="_blank" rel="noreferrer">
                        <Network className="h-3.5 w-3.5 mr-2" />
                        Open graph
                      </a>
                    </DropdownMenuItem>
                  )}
                  {showSettings && repo && (
                    <DropdownMenuItem onSelect={() => setSettingsOpen(true)}>
                      <Settings className="h-3.5 w-3.5 mr-2" />
                      Wiki settings
                    </DropdownMenuItem>
                  )}
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          )}

          <Button
            variant="primary"
            size="sm"
            onClick={onCopyLink}
            aria-live="polite"
            leadingIcon={
              copied ? (
                <Check className="h-3.5 w-3.5" />
              ) : (
                <Copy className="h-3.5 w-3.5" />
              )
            }
            className={cn(
              "shrink-0",
              copied &&
                "bg-[hsl(var(--success))] hover:bg-[hsl(var(--success))]/90 text-[hsl(var(--success-foreground))]"
            )}
          >
            {/* CTA label shortens instead of dropping to a bare icon — text
                is the affordance here (the IdeCapsule label-swap idiom). */}
            <span className="hidden md:inline">{copied ? "Copied" : "Copy link"}</span>
            <span className="md:hidden">{copied ? "Copied" : "Copy"}</span>
          </Button>
        </div>
      </div>


      {/* Mounted only while open: the dialog reads server state, and the bar
          itself must stay renderable on screens (and in tests) that never open
          it. */}
      {settingsOpen && repo && (
        <ProjectSettingsDialog
          slug={repo}
          open={settingsOpen}
          onOpenChange={setSettingsOpen}
          // Same re-index CTA the freshness badge opens — a settings save that
          // only takes effect next index hands off to it rather than starting a
          // second refresh path.
          onRefresh={onRefresh}
        />
      )}
    </div>
  );
}

/**
 * Shared body of the Edit-Wiki surface — rendered by the desktop Popover and
 * the overflow-menu Dialog alike, so the two mounts can never drift.
 */
function EditWikiBody() {
  return (
    <>
      <div className="flex items-center gap-2 px-3.5 py-2.5 border-b border-[hsl(var(--border))]">
        <Info className="h-3.5 w-3.5 text-[hsl(var(--primary))]" />
        <span className="text-sm font-medium">Steer wiki generation</span>
      </div>
      <div className="px-3.5 py-3 space-y-2.5">
        <p className="text-xs text-[hsl(var(--muted-foreground))] leading-relaxed">
          To customise this wiki, add or edit one of these files in
          the repository root and re-index:
        </p>
        <ul className="space-y-1.5">
          {[".mewbo/wiki.json", ".devin/wiki.json"].map((p) => (
            <li
              key={p}
              className="inline-flex items-center gap-2 px-2 py-1 rounded-md bg-[hsl(var(--muted))]/50 w-full"
            >
              <FileText className="h-3 w-3 text-[hsl(var(--muted-foreground))]" />
              <code className="font-mono text-xs text-[hsl(var(--foreground))]">{p}</code>
            </li>
          ))}
        </ul>
        <p className="text-2xs text-[hsl(var(--muted-foreground))] pt-1">
          Either file is recognised — pick whichever fits your tooling.
        </p>
      </div>
    </>
  );
}

/**
 * Shared body of the Copy-badge surface — one markup for the desktop Popover
 * and the overflow-menu Dialog.
 */
function CopyBadgeBody({
  badge,
}: {
  badge: NonNullable<ReturnType<typeof WikiBadge.forPage>>;
}) {
  return (
    <>
      <div className="flex items-center gap-2 px-3.5 py-2.5 border-b border-[hsl(var(--border))]">
        <BadgePlus className="h-3.5 w-3.5 text-[hsl(var(--primary))]" />
        <span className="text-sm font-medium">Add the wiki badge</span>
      </div>
      <div className="px-3.5 py-3 space-y-3">
        <p className="text-xs text-[hsl(var(--muted-foreground))] leading-relaxed">
          Drop this into your README so visitors can browse — and ask
          questions about — your codebase.
        </p>
        {/* Live preview of the actual artwork; click-testable. */}
        <a
          href={badge.linkUrl}
          target="_blank"
          rel="noreferrer"
          title="Open the wiki page in a new tab"
          className="flex items-center justify-center rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/40 py-4 transition-colors hover:bg-[hsl(var(--muted))]/70"
        >
          <img src={WikiBadge.IMAGE_URL} alt={WikiBadge.ALT} className="h-6" />
        </a>
        {/* Copyable README snippet. */}
        <div className="rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/40">
          <div className="flex items-center justify-between pl-2.5 pr-1 py-1 border-b border-[hsl(var(--border))]">
            <span className="text-2xs uppercase tracking-wider text-[hsl(var(--muted-foreground))]">
              Markdown
            </span>
            <CopyButton text={badge.markdown} className="h-6 px-2 text-2xs">
              Copy
            </CopyButton>
          </div>
          <code className="block px-2.5 py-2 font-mono text-2xs leading-relaxed text-[hsl(var(--foreground))] break-all select-all">
            {badge.markdown}
          </code>
        </div>
      </div>
    </>
  );
}
