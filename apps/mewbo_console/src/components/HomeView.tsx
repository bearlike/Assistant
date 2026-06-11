import { useState, useMemo, useCallback, useRef, useEffect, type CSSProperties } from 'react';
import { AlertCircle, Search, X, Archive, RotateCcw, Loader2, ChevronDown, ListFilter } from 'lucide-react';
import { cn } from '../utils/cn';
import { SessionItem } from './SessionItem';
import { SessionOriginBadge } from './SessionOriginBadge';
import { InputBar } from './InputBar';
import { TypewriterGreeting } from './TypewriterGreeting';
import { QueryMode, SessionContext, SessionOrigin, SessionSummary } from '../types';
import { Alert, AlertDescription, AlertTitle } from './ui/alert';
import { Button } from './ui/button';
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

// Per-origin filter. Default reveals what the user authored — sessions they
// started in the console ("user") plus channel chats — and hides the
// internally-spawned wiki/search sessions until scoped into.
const ORIGIN_FILTERS: { origin: SessionOrigin; label: string }[] = [
  { origin: 'user', label: 'My tasks' },
  { origin: 'channel', label: 'Channels' },
  { origin: 'wiki', label: 'Wiki' },
  { origin: 'search', label: 'Search' },
  { origin: 'structured', label: 'Structured' },
  { origin: 'draft', label: 'Draft' },
];
const DEFAULT_VISIBLE_ORIGINS: SessionOrigin[] = ['user', 'channel'];
interface HomeViewProps {
  sessions: SessionSummary[];
  archivedSessions: SessionSummary[];
  loading: boolean;
  archivedLoading: boolean;
  error?: string | null;
  archivedError?: string | null;
  actionError?: string | null;
  onSessionSelect: (sessionId: string) => void;
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
  onSessionSelect,
  onCreateAndRun,
  onLoadArchived,
  onArchive,
  onUnarchive,
  isCreating = false,
  onRetry
}: HomeViewProps) {
  const [isSearchOpen, setIsSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [activeTab, setActiveTab] = useState<'sessions' | 'archive'>('sessions');
  const [visibleOrigins, setVisibleOrigins] = useState<Set<SessionOrigin>>(
    () => new Set(DEFAULT_VISIBLE_ORIGINS)
  );
  const [chevronHidden, setChevronHidden] = useState(false);
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
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const sessionsRef = useRef<HTMLDivElement | null>(null);

  // Chevron is a scroll affordance — show near the top of the hero, hide
  // once the user has nudged past it, re-show when they scroll back up.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onScroll = () => {
      setChevronHidden(el.scrollTop > 80);
    };
    el.addEventListener('scroll', onScroll, { passive: true });
    return () => el.removeEventListener('scroll', onScroll);
  }, []);

  // Single page-level IntersectionObserver for .fade-in-row reveal. Observes
  // anything not yet visible on every render so freshly-rendered session rows
  // get picked up. CSS owns the transition + stagger via --row-index.
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
  });

  const handleChevronClick = useCallback(() => {
    sessionsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, []);

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
  const now = useMemo(() => new Date(), []);
  const recentSessions = displayedSessions.filter((session) => {
    if (!session.created_at) return true;
    const created = new Date(session.created_at);
    return (now.getTime() - created.getTime()) / (1000 * 60 * 60 * 24) <= 7;
  });
  const olderSessions = displayedSessions.filter(
    (session) => !recentSessions.includes(session)
  );
  const filteredSessions = displayedSessions.filter((session) => {
    const title = session.title?.toLowerCase() || '';
    const project = (projectLabel.resolve(session.context).label || '').toLowerCase();
    return (
      title.includes(searchQuery.toLowerCase()) ||
      project.includes(searchQuery.toLowerCase()));

  });
  return (
    <div ref={scrollRef} className="h-full w-full relative overflow-y-auto">
      {/* Hero — occupies the full viewport so sessions sit naturally below the fold. */}
      <section className="min-h-[85dvh] flex flex-col items-center justify-center px-4 pt-16 pb-16 relative">
        <img
          src="/logo-transparent.svg"
          alt="Mewbo"
          className="w-16 h-16 mb-6 drop-shadow-[0_0_40px_hsl(var(--primary)/.25)]" />
        <h1 className="text-4xl sm:text-5xl font-semibold text-[hsl(var(--foreground))] tracking-tight mb-3">
          Mewbo
        </h1>
        <TypewriterGreeting />

        <div className="w-full max-w-4xl">
          {(actionError || (listError && !apiUnavailable)) &&
          <div className="mb-4">
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

        {/* Scroll affordance — fades out once scrolled past the hero, returns
            when the user comes back up. Outer div centers via flex so the
            bounce keyframe's `transform` can't fight a translate-x centering. */}
        <div
          aria-hidden={chevronHidden}
          className={cn(
            "absolute bottom-8 inset-x-0 flex justify-center transition-opacity duration-300",
            chevronHidden && "opacity-0 pointer-events-none"
          )}>
          <button
            type="button"
            onClick={handleChevronClick}
            aria-label="Scroll to recent sessions"
            className="flex flex-col items-center gap-1 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] animate-scroll-bounce">
            <span>Recent sessions</span>
            <ChevronDown className="w-4 h-4" />
          </button>
        </div>
      </section>

      {/* Sessions — naturally below the fold, scrolled as part of the same page.
          sessions-peek-fade is on the list container (below the tabs), not this
          wrapper, so the tab labels render at full opacity. */}
      <div ref={sessionsRef} className="w-full">
        <div className="max-w-4xl mx-auto px-4 pb-20">
          {apiUnavailable ?
          <div className="flex flex-col items-center justify-center py-24 text-center">
              <AlertCircle className="w-10 h-10 text-red-500 mb-4" />
              <h2 className="text-lg font-semibold text-[hsl(var(--foreground))] mb-2">
                Unable to connect to API
              </h2>
              <p className="text-sm text-[hsl(var(--muted-foreground))] mb-6 max-w-md">
                {listError}
              </p>
              {onRetry &&
              <Button variant="neutral" size="md" onClick={onRetry}>
                  Try Again
                </Button>
              }
            </div> :
          <>
          <div className="sticky top-0 z-10 pt-2 mb-4 flex items-center justify-between border-b border-[hsl(var(--border-strong))] bg-[hsl(var(--background))]/85 backdrop-blur-sm shadow-[0_4px_12px_hsl(var(--background))]">
            <div className="flex gap-8">
              <button
                onClick={() => switchTab('sessions')}
                className={`pb-3 text-sm font-medium transition-colors ${activeTab === 'sessions' ? 'text-[hsl(var(--foreground))] border-b-2 border-[hsl(var(--foreground))]' : 'text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]'}`}>

                Sessions
              </button>
              <button
                onClick={() => switchTab('archive')}
                className={`pb-3 text-sm font-medium transition-colors ${activeTab === 'archive' ? 'text-[hsl(var(--foreground))] border-b-2 border-[hsl(var(--foreground))]' : 'text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]'}`}>

                Archive
              </button>
            </div>
            <div className="flex items-center gap-1 pb-3">
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button
                    aria-label="Filter sessions by origin"
                    title="Filter sessions by origin"
                    className="relative text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors">
                    <ListFilter className="w-4 h-4" />
                    {!isDefaultOriginFilter && (
                      <span className="absolute -top-0.5 -right-0.5 w-1.5 h-1.5 rounded-full bg-[hsl(var(--primary))]" />
                    )}
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-44">
                  <DropdownMenuLabel>Show</DropdownMenuLabel>
                  {ORIGIN_FILTERS.map(({ origin, label }) => (
                    <DropdownMenuCheckboxItem
                      key={origin}
                      checked={visibleOrigins.has(origin)}
                      onSelect={(e) => e.preventDefault()}
                      onCheckedChange={() => toggleOrigin(origin)}>
                      {label}
                    </DropdownMenuCheckboxItem>
                  ))}
                </DropdownMenuContent>
              </DropdownMenu>
              <button
                onClick={() => setIsSearchOpen(true)}
                aria-label="Search sessions"
                title="Search sessions"
                className="text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors">

                <Search className="w-4 h-4" />
              </button>
            </div>
          </div>

          <div className="sessions-peek-fade">
          {activeTab === 'archive' ?
          <div className="space-y-8 mt-6">
              <SessionSection
              title="Archived"
              loading={listLoading}
              sessions={displayedSessions}
              projectLabel={projectLabel}
              onSessionSelect={onSessionSelect}
              onArchive={onArchive}
              onUnarchive={onUnarchive} />

            </div> :

          <div className="space-y-8 mt-6">
              <SessionSection
              title="Last 7 Days"
              loading={listLoading}
              sessions={recentSessions}
              projectLabel={projectLabel}
              onSessionSelect={onSessionSelect}
              onArchive={onArchive}
              onUnarchive={onUnarchive} />

              <SessionSection
              title="Older"
              loading={listLoading && recentSessions.length === 0}
              sessions={olderSessions}
              projectLabel={projectLabel}
              onSessionSelect={onSessionSelect}
              onArchive={onArchive}
              onUnarchive={onUnarchive} />

            </div>
          }
          </div>
          </>
          }
        </div>
      </div>

      {/* Search Dialog Overlay */}
      {isSearchOpen &&
      <div className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50 flex items-start justify-center pt-32">
          <div className="w-full max-w-2xl bg-[hsl(var(--popover))] border border-[hsl(var(--border))] rounded-xl shadow-2xl overflow-hidden">
            <div className="flex items-center justify-between px-4 py-3 border-b border-[hsl(var(--border))]">
              <h2 className="text-sm font-medium text-[hsl(var(--foreground))]">
                {activeTab === 'archive' ?
              'Search archived sessions' :
              'Search sessions'}
              </h2>
              <Button
              variant="ghost"
              size="sm"
              iconOnly
              onClick={() => setIsSearchOpen(false)}
              aria-label="Close search">
                <X className="w-4 h-4" />
              </Button>
            </div>

            <div className="p-2">
              <div className="relative mb-2">
                <Search className="absolute left-3 top-2.5 w-4 h-4 text-[hsl(var(--muted-foreground))]" />
                <input
                type="text"
                autoFocus
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="w-full bg-[hsl(var(--muted))] border border-[hsl(var(--border))] rounded-lg py-2 pl-9 pr-8 text-sm text-[hsl(var(--foreground))] placeholder:text-[hsl(var(--muted-foreground))] focus:outline-none focus:ring-1 focus:ring-[hsl(var(--ring))]/30"
                placeholder="Search..." />

                {searchQuery &&
              <Button
                variant="ghost"
                size="sm"
                iconOnly
                onClick={() => setSearchQuery('')}
                aria-label="Clear search"
                className="absolute right-1 top-1/2 -translate-y-1/2">
                    <X className="w-3 h-3" />
                  </Button>
              }
              </div>

              <div className="max-h-[400px] overflow-y-auto">
                {filteredSessions.map((session) =>
              <div
                key={session.session_id}
                onClick={() => {
                  onSessionSelect(session.session_id);
                  setIsSearchOpen(false);
                }}
                className="flex items-start gap-4 p-3 hover:bg-[hsl(var(--accent))] rounded-lg cursor-pointer group">

                    <div className="flex flex-col gap-1 min-w-0 flex-1">
                      <h3 className="text-sm font-medium text-[hsl(var(--foreground))] line-clamp-2">
                        {session.title}
                      </h3>
                      <div className="flex items-center gap-1.5 text-xs text-[hsl(var(--muted-foreground))]">
                        <span className="whitespace-nowrap">{formatSessionTime(session.created_at)}</span>
                        <SessionOriginBadge session={session} />
                        {projectLabel.resolve(session.context).label && (
                          <>
                            <span>·</span>
                            <span className="truncate">{projectLabel.resolve(session.context).label}</span>
                          </>
                        )}
                      </div>
                    </div>
                    <div className="flex items-center gap-3 shrink-0">
                      {(onArchive != null || onUnarchive != null) &&
                      <button
                        onClick={(event) => {
                          event.stopPropagation();
                          if (session.archived) {
                            onUnarchive?.(session.session_id);
                          } else {
                            onArchive?.(session.session_id);
                          }
                        }}
                        aria-label={session.archived ? 'Unarchive session' : 'Archive session'}
                        className="p-1 rounded text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] opacity-0 group-hover:opacity-100 transition-all">

                        {session.archived ?
                        <RotateCcw className="w-4 h-4" /> :

                        <Archive className="w-4 h-4" />
                        }
                      </button>
                      }
                    </div>
                  </div>
              )}
              </div>
            </div>
          </div>
        </div>
      }
    </div>);

}
function SessionSection({
  title,
  loading,
  sessions,
  projectLabel,
  onSessionSelect,
  onArchive,
  onUnarchive







}: {title: string;loading: boolean;sessions: SessionSummary[];projectLabel: ProjectLabel;onSessionSelect: (sessionId: string) => void;onArchive?: (sessionId: string) => void;onUnarchive?: (sessionId: string) => void;}) {
  return (
    <div>
      <h3 className="text-xs font-medium text-[hsl(var(--muted-foreground))] mb-3 uppercase tracking-wider pl-2">
        {title}
      </h3>
      <div className="divide-y divide-[hsl(var(--border))]">
        {loading &&
        <div className="text-sm text-[hsl(var(--muted-foreground))] pl-2">
            Loading sessions...
          </div>
        }
        {!loading && sessions.length === 0 &&
        <div className="text-sm text-[hsl(var(--muted-foreground))] pl-2">
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
              onClick={onSessionSelect}
              onArchive={onArchive}
              onUnarchive={onUnarchive} />
          </div>
        )}
      </div>
    </div>);

}
