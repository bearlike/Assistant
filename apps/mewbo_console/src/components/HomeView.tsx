import { useState, useMemo, useCallback, useRef, useEffect, type CSSProperties } from 'react';
import { AlertCircle, Search, Archive, RotateCcw, Loader2, ListFilter } from 'lucide-react';
import { SessionItem } from './SessionItem';
import { SessionOriginBadge } from './SessionOriginBadge';
import { InputBar } from './InputBar';
import { ProductHero } from './ProductHero';
import { TypewriterGreeting } from './TypewriterGreeting';
import { QueryMode, SessionContext, SessionOrigin, SessionSummary } from '../types';
import { Alert, AlertDescription, AlertTitle } from './ui/alert';
import { Button } from './ui/button';
import { FOCUS_RING } from './ui/focus-ring';
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from './ui/command';
import { DialogTitle } from './ui/dialog';
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuCheckboxItem,
  DropdownMenuLabel,
} from './ui/dropdown-menu';
import { useProjects } from '../hooks/useProjects';
import { ProjectLabel } from '../utils/projectLabel';
import { formatSessionTime } from '../utils/time';
import { ORIGIN_FILTERS, DEFAULT_VISIBLE_ORIGINS } from '../utils/sessionOrigins';

interface HomeViewProps {
  sessions: SessionSummary[];
  archivedSessions: SessionSummary[];
  loading: boolean;
  archivedLoading: boolean;
  error?: string | null;
  archivedError?: string | null;
  actionError?: string | null;
  onSelectSession: (sessionId: string) => void;
  onCreateAndRun: (
    query: string,
    context?: SessionContext,
    mode?: QueryMode,
    attachments?: File[]
  ) => void;
  onLoadArchived: () => void;
  onArchive: (sessionId: string) => void;
  onUnarchive: (sessionId: string) => void;
  isCreating?: boolean;
  onRetry?: () => void;
}
export function HomeView({
  sessions,
  archivedSessions,
  loading,
  archivedLoading,
  error,
  archivedError,
  actionError,
  onSelectSession,
  onCreateAndRun,
  onLoadArchived,
  onArchive,
  onUnarchive,
  isCreating = false,
  onRetry
}: HomeViewProps) {
  const [isSearchOpen, setIsSearchOpen] = useState(false);
  const [activeTab, setActiveTab] = useState<'sessions' | 'archive'>('sessions');
  const [visibleOrigins, setVisibleOrigins] = useState<Set<SessionOrigin>>(
    () => new Set(DEFAULT_VISIBLE_ORIGINS)
  );
  const { projects } = useProjects();
  const projectLabel = useMemo(() => new ProjectLabel(projects), [projects]);
  const toggleOrigin = useCallback((origin: SessionOrigin) => {
    setVisibleOrigins((prev) => {
      const next = new Set(prev);
      if (next.has(origin)) next.delete(origin);
      else next.add(origin);
      return next;
    });
  }, []);
  const isDefaultOriginFilter =
    visibleOrigins.size === DEFAULT_VISIBLE_ORIGINS.length &&
    DEFAULT_VISIBLE_ORIGINS.every((origin) => visibleOrigins.has(origin));
  const sessionsRef = useRef<HTMLDivElement | null>(null);

  // Tab switch resets the scroll to the top of the sessions area so the two
  // lists always open with the same orientation — otherwise the user can land
  // in the middle of a shorter list after switching from a longer one.
  const switchTab = useCallback((tab: 'sessions' | 'archive') => {
    setActiveTab(tab);
    if (tab === 'archive') onLoadArchived();
    sessionsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, [onLoadArchived]);
  const listError = activeTab === 'archive' ? archivedError : error;
  const listLoading = activeTab === 'archive' ? archivedLoading : loading;
  const scopedSessions = activeTab === 'archive' ? archivedSessions : sessions;
  const apiUnavailable = !listLoading && !!listError && scopedSessions.length === 0;
  // Single source of truth for the visible list: scope (tab) → origin filter.
  // Everything below (recent/older split, search) derives from this so the
  // filter applies uniformly.
  const displayedSessions = scopedSessions.filter((session) =>
    visibleOrigins.has(session.origin ?? 'user')
  );

  // Single page-level IntersectionObserver for .fade-in-row reveal. Observes
  // anything not yet visible whenever the row list itself could have changed
  // (tab switch, origin filter, or the underlying session data), rather than
  // on every render — a bare no-deps effect recreated the observer on every
  // keystroke elsewhere on the page (e.g. the search dialog) with no rows to
  // pick up. CSS owns the transition + stagger via --row-index.
  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            entry.target.classList.add('is-visible');
            observer.unobserve(entry.target);
          }
        }
      },
      { threshold: 0.05 }
    );
    const rows = document.querySelectorAll('.fade-in-row:not(.is-visible)');
    rows.forEach((row) => observer.observe(row));
    return () => observer.disconnect();
  }, [activeTab, displayedSessions]);

  const now = useMemo(() => new Date(), []);
  const recentSessions = displayedSessions.filter((session) => {
    if (!session.created_at) return true;
    const created = new Date(session.created_at);
    return (now.getTime() - created.getTime()) / (1000 * 60 * 60 * 24) <= 7;
  });
  const olderSessions = displayedSessions.filter(
    (session) => !recentSessions.includes(session)
  );
  return (
    <div className="h-full w-full relative overflow-y-auto">
      {/* The composer is the focal element; the hero above it is the shared
          `ProductHero` so Tasks, Wiki and Search open on the same mark, the
          same type and the same offset. The rotating greeting is the subtitle
          TEXT — its typography belongs to the hero. */}
      <ProductHero title="Agentic Tasks" subtitle={<TypewriterGreeting />}>
        <div className="w-full max-w-[var(--thread-max-width)]">
          {(actionError || (listError && !apiUnavailable)) &&
          <div className="mb-4 text-left">
              <Alert variant="destructive">
                <AlertTitle>Session error</AlertTitle>
                <AlertDescription>{actionError || listError}</AlertDescription>
              </Alert>
            </div>
          }
          <InputBar
            mode="home"
            onSubmit={onCreateAndRun}
            isSubmitting={isCreating} />
          {isCreating &&
          <div className="mt-2 flex items-center gap-2 text-xs text-[hsl(var(--muted-foreground))]">
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
              <span>Creating session...</span>
            </div>
          }
        </div>
      </ProductHero>

      {/* Sessions — sit just below the hero, scrolled as part of the same page. */}
      <div ref={sessionsRef} className="w-full">
        <div className="max-w-4xl mx-auto px-4 pb-20">
          {apiUnavailable ?
          <div className="flex flex-col items-center justify-center py-24 text-center">
              <AlertCircle className="w-10 h-10 text-[hsl(var(--destructive))] mb-4" />
              <h2 className="text-lg font-semibold text-[hsl(var(--foreground))] mb-2">
                Unable to connect to API
              </h2>
              <p className="text-sm font-normal text-[hsl(var(--muted-foreground))] mb-6 max-w-md">
                {listError}
              </p>
              {onRetry &&
              <Button variant="neutral" size="md" onClick={onRetry}>
                  Try Again
                </Button>
              }
            </div> :
          <>
          <div className="sticky top-0 z-10 pt-2 mb-4 flex items-center justify-between border-b border-[hsl(var(--border))] bg-[hsl(var(--background))]/85 backdrop-blur-sm">
            <div className="flex gap-6">
              {/* Active vs inactive reads off WEIGHT + colour + the underline,
                  never off a size step — both tabs stay `text-sm`. */}
              <button
                onClick={() => switchTab('sessions')}
                className={`pb-3 text-sm transition-colors ${activeTab === 'sessions' ? 'font-medium text-[hsl(var(--foreground))] border-b-2 border-[hsl(var(--foreground))]' : 'font-normal text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]'}`}>

                Sessions
              </button>
              <button
                onClick={() => switchTab('archive')}
                className={`pb-3 text-sm transition-colors ${activeTab === 'archive' ? 'font-medium text-[hsl(var(--foreground))] border-b-2 border-[hsl(var(--foreground))]' : 'font-normal text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]'}`}>

                Archive
              </button>
            </div>
            {/* Both controls are icon-only, so each carries `aria-label` AND
                `title`, and each pads its 16px glyph out to a 28x28 box — a bare
                icon is a 16x16 hit target, under the 24x24 AA floor. */}
            <div className="flex items-center gap-1 pb-3">
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button
                    aria-label="Filter sessions by origin"
                    title="Filter sessions by origin"
                    className={`relative p-1.5 rounded-md text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors ${FOCUS_RING}`}>
                    <ListFilter className="w-4 h-4" />
                    {!isDefaultOriginFilter && (
                      <span className="absolute top-0.5 right-0.5 w-1.5 h-1.5 rounded-full bg-[hsl(var(--primary))]" />
                    )}
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-44">
                  {/* A group label should be quieter than the rows it labels,
                      so it drops a step and goes muted; the primitive's
                      `font-medium` still separates it from the items. */}
                  <DropdownMenuLabel className="text-xs text-[hsl(var(--muted-foreground))]">
                    Show
                  </DropdownMenuLabel>
                  {ORIGIN_FILTERS.map(({ origin, label, icon: OriginIcon }) => {
                    const isOn = visibleOrigins.has(origin);
                    return (
                      <DropdownMenuCheckboxItem
                        key={origin}
                        checked={isOn}
                        onSelect={(e) => e.preventDefault()}
                        onCheckedChange={() => toggleOrigin(origin)}
                        // Checked vs unchecked reads off weight + colour on top
                        // of the check glyph — every row stays `text-sm`, the
                        // same rule the tab strip above follows.
                        className={
                          isOn
                            ? 'font-medium text-[hsl(var(--foreground))]'
                            : 'font-normal text-[hsl(var(--muted-foreground))]'
                        }>
                        {/* Same glyph the row's chip carries, so the filter
                            teaches the mark the session list then uses. Sized
                            explicitly: unlike `DropdownMenuItem`, the checkbox
                            variant carries no `[&>svg]` rule, so an unsized
                            lucide icon would render at its 24px default. 3.5
                            matches the check indicator's own box. */}
                        <span className="inline-flex items-center gap-2">
                          <OriginIcon
                            className="size-3.5 shrink-0 text-[hsl(var(--muted-foreground))]"
                            aria-hidden
                          />
                          {label}
                        </span>
                      </DropdownMenuCheckboxItem>
                    );
                  })}
                </DropdownMenuContent>
              </DropdownMenu>
              <button
                onClick={() => setIsSearchOpen(true)}
                aria-label="Search sessions"
                title="Search sessions"
                className={`p-1.5 rounded-md text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors ${FOCUS_RING}`}>

                <Search className="w-4 h-4" />
              </button>
            </div>
          </div>

          {activeTab === 'archive' ?
          <div className="space-y-6 mt-6">
              <SessionSection
              title="Archived"
              loading={listLoading}
              sessions={displayedSessions}
              projectLabel={projectLabel}
              onSelectSession={onSelectSession}
              onArchive={onArchive}
              onUnarchive={onUnarchive} />

            </div> :

          <div className="space-y-6 mt-6">
              <SessionSection
              title="Last 7 Days"
              loading={listLoading}
              sessions={recentSessions}
              projectLabel={projectLabel}
              onSelectSession={onSelectSession}
              onArchive={onArchive}
              onUnarchive={onUnarchive} />

              <SessionSection
              title="Older"
              loading={listLoading && recentSessions.length === 0}
              sessions={olderSessions}
              projectLabel={projectLabel}
              onSelectSession={onSelectSession}
              onArchive={onArchive}
              onUnarchive={onUnarchive} />

            </div>
          }
          </>
          }
        </div>
      </div>

      {/* Search dialog. Filtering is cmdk's own — each CommandItem's `value`
          carries the title + resolved project label, matching how
          ConfigMenu's project rows already drive cmdk's filter. */}
      <CommandDialog open={isSearchOpen} onOpenChange={setIsSearchOpen}>
        <DialogTitle className="sr-only">
          {activeTab === 'archive' ? 'Search archived sessions' : 'Search sessions'}
        </DialogTitle>
        <CommandInput placeholder="Search sessions..." />
        <CommandList>
          {/* `CommandEmpty` spreads props AFTER its own `className`, so passing
              one REPLACES the primitive's layout rather than merging — the
              padding and centring have to be restated here. */}
          <CommandEmpty className="py-6 text-center text-sm font-normal text-[hsl(var(--muted-foreground))]">
            No sessions found.
          </CommandEmpty>
          <CommandGroup>
            {displayedSessions.map((session) => {
              const project = projectLabel.resolve(session.context).label;
              return (
                <CommandItem
                  key={session.session_id}
                  value={`${session.title ?? ''} ${project ?? ''}`}
                  onSelect={() => {
                    onSelectSession(session.session_id);
                    setIsSearchOpen(false);
                  }}
                  className="group flex items-start justify-between gap-3 py-2.5"
                >
                  <div className="flex flex-col gap-1 min-w-0 flex-1">
                    {/* The title is the row's own emphasis; everything under it
                        is metadata and stays at the base weight. */}
                    <h3 className="text-sm font-medium text-[hsl(var(--foreground))] line-clamp-2">
                      {session.title}
                    </h3>
                    <div className="flex items-center gap-1.5 text-xs font-normal text-[hsl(var(--muted-foreground))]">
                      <span className="whitespace-nowrap">{formatSessionTime(session.created_at)}</span>
                      <SessionOriginBadge session={session} />
                      {project && (
                        <>
                          <span>·</span>
                          <span className="truncate">{project}</span>
                        </>
                      )}
                    </div>
                  </div>
                  {(onArchive != null || onUnarchive != null) && (
                    <button
                      type="button"
                      onClick={(event) => {
                        event.stopPropagation();
                        if (session.archived) {
                          onUnarchive?.(session.session_id);
                        } else {
                          onArchive?.(session.session_id);
                        }
                      }}
                      aria-label={session.archived ? 'Unarchive session' : 'Archive session'}
                      title={session.archived ? 'Unarchive session' : 'Archive session'}
                      // Hover-revealed, so it must also reveal on keyboard
                      // focus — otherwise the control is unreachable by tab.
                      className={`shrink-0 p-1 rounded text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100 ${FOCUS_RING} transition-all`}
                    >
                      {session.archived ? (
                        <RotateCcw className="w-4 h-4" />
                      ) : (
                        <Archive className="w-4 h-4" />
                      )}
                    </button>
                  )}
                </CommandItem>
              );
            })}
          </CommandGroup>
        </CommandList>
      </CommandDialog>
    </div>);

}
function SessionSection({
  title,
  loading,
  sessions,
  projectLabel,
  onSelectSession,
  onArchive,
  onUnarchive







}: {title: string;loading: boolean;sessions: SessionSummary[];projectLabel: ProjectLabel;onSelectSession: (sessionId: string) => void;onArchive?: (sessionId: string) => void;onUnarchive?: (sessionId: string) => void;}) {
  return (
    <div>
      <h3 className="text-2xs font-medium text-[hsl(var(--muted-foreground))] mb-3 uppercase tracking-wider pl-2">
        {title}
      </h3>
      <div className="divide-y divide-[hsl(var(--border))]">
        {loading &&
        <div className="text-sm font-normal text-[hsl(var(--muted-foreground))] pl-2">
            Loading sessions...
          </div>
        }
        {!loading && sessions.length === 0 &&
        <div className="text-sm font-normal text-[hsl(var(--muted-foreground))] pl-2">
            No sessions yet.
          </div>
        }
        {sessions.map((session, i) =>
        <div
          key={session.session_id}
          className="fade-in-row"
          style={{ '--row-index': i } as CSSProperties}>
            <SessionItem
              session={session}
              projectLabel={projectLabel}
              onClick={onSelectSession}
              onArchive={onArchive}
              onUnarchive={onUnarchive} />
          </div>
        )}
      </div>
    </div>);

}
