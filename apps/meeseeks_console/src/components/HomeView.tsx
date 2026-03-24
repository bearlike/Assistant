import { useState, useMemo } from 'react';
import { Search, X, Archive, RotateCcw, Loader2 } from 'lucide-react';
import { SessionItem } from './SessionItem';
import { InputBar } from './InputBar';
import { QueryMode, SessionContext, SessionSummary } from '../types';
import { Alert, AlertDescription, AlertTitle } from './ui/alert';
import { formatSessionTime } from '../utils/time';
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
  isCreating = false
}: HomeViewProps) {
  const [isSearchOpen, setIsSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [activeTab, setActiveTab] = useState<'sessions' | 'archive'>('sessions');
  const listError = activeTab === 'archive' ? archivedError : error;
  const listLoading = activeTab === 'archive' ? archivedLoading : loading;
  const scopedSessions = activeTab === 'archive' ? archivedSessions : sessions;
  const now = useMemo(() => new Date(), []);
  const recentSessions = scopedSessions.filter((session) => {
    if (!session.created_at) return true;
    const created = new Date(session.created_at);
    return (now.getTime() - created.getTime()) / (1000 * 60 * 60 * 24) <= 7;
  });
  const olderSessions = scopedSessions.filter(
    (session) => !recentSessions.includes(session)
  );
  const filteredSessions = scopedSessions.filter((session) => {
    const title = session.title?.toLowerCase() || '';
    const project = (session.context?.project || session.context?.repo || '').toLowerCase();
    return (
      title.includes(searchQuery.toLowerCase()) ||
      project.includes(searchQuery.toLowerCase()));

  });
  return (
    <div className="flex flex-col h-full w-full relative overflow-hidden">
      {/* Fixed Top Section */}
      <div className="flex-none flex flex-col items-center pt-16 pb-6 px-4 w-full z-20 bg-[hsl(var(--background))]">
        <h1 className="text-3xl font-medium text-[hsl(var(--foreground))] mb-8">
          What should we do next?
        </h1>

        <div className="w-full max-w-3xl">
          {(listError || actionError) &&
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
      </div>

      {/* Scrollable Bottom Section */}
      <div className="flex-1 overflow-y-auto w-full">
        <div className="max-w-3xl mx-auto px-4 pb-20">
          <div className="sticky top-0 z-10 pt-2 pb-4 mb-2 flex items-center justify-between border-b border-[hsl(var(--border))] bg-[hsl(var(--background))] shadow-[0_4px_12px_hsl(var(--background))]">
            <div className="flex gap-8">
              <button
                onClick={() => setActiveTab('sessions')}
                className={`pb-3 text-sm font-medium transition-colors ${activeTab === 'sessions' ? 'text-[hsl(var(--foreground))] border-b-2 border-[hsl(var(--foreground))]' : 'text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]'}`}>

                Sessions
              </button>
              <button
                onClick={() => {
                  setActiveTab('archive');
                  onLoadArchived();
                }}
                className={`pb-3 text-sm font-medium transition-colors ${activeTab === 'archive' ? 'text-[hsl(var(--foreground))] border-b-2 border-[hsl(var(--foreground))]' : 'text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]'}`}>

                Archive
              </button>
            </div>
            <button
              onClick={() => setIsSearchOpen(true)}
              className="pb-3 text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors">

              <Search className="w-4 h-4" />
            </button>
          </div>

          {activeTab === 'archive' ?
          <div className="space-y-8 mt-6">
              <SessionSection
              title="Archived"
              loading={listLoading}
              sessions={scopedSessions}
              onSessionSelect={onSessionSelect}
              onArchive={onArchive}
              onUnarchive={onUnarchive} />

            </div> :

          <div className="space-y-8 mt-6">
              <SessionSection
              title="Last 7 Days"
              loading={listLoading}
              sessions={recentSessions}
              onSessionSelect={onSessionSelect}
              onArchive={onArchive}
              onUnarchive={onUnarchive} />

              <SessionSection
              title="Older"
              loading={listLoading && recentSessions.length === 0}
              sessions={olderSessions}
              onSessionSelect={onSessionSelect}
              onArchive={onArchive}
              onUnarchive={onUnarchive} />

            </div>
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
              <button
              onClick={() => setIsSearchOpen(false)}
              className="text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]">

                <X className="w-4 h-4" />
              </button>
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
              <button
                onClick={() => setSearchQuery('')}
                className="absolute right-3 top-2.5 text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]">

                    <X className="w-3 h-3" />
                  </button>
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
                className="flex items-center justify-between p-3 hover:bg-[hsl(var(--accent))] rounded-lg cursor-pointer group">

                    <div className="flex flex-col gap-1">
                      <h3 className="text-sm font-medium text-[hsl(var(--foreground))]">
                        {session.title}
                      </h3>
                      <div className="flex items-center gap-1.5 text-xs text-[hsl(var(--muted-foreground))]">
                        <span>{formatSessionTime(session.created_at)}</span>
                        {(session.context?.project || session.context?.repo) && (
                          <>
                            <span>·</span>
                            <span>{session.context?.project || session.context?.repo}</span>
                          </>
                        )}
                      </div>
                    </div>
                    <div className="flex items-center gap-3">
                      <div className="text-xs text-[hsl(var(--muted-foreground))]">
                        {session.status}
                      </div>
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
  onSessionSelect,
  onArchive,
  onUnarchive







}: {title: string;loading: boolean;sessions: SessionSummary[];onSessionSelect: (sessionId: string) => void;onArchive?: (sessionId: string) => void;onUnarchive?: (sessionId: string) => void;}) {
  return (
    <div>
      <h3 className="text-xs font-medium text-[hsl(var(--muted-foreground))] mb-3 uppercase tracking-wider pl-2">
        {title}
      </h3>
      <div className="space-y-1">
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
        {sessions.map((session) =>
        <SessionItem
          key={session.session_id}
          session={session}
          onClick={onSessionSelect}
          onArchive={onArchive}
          onUnarchive={onUnarchive} />

        )}
      </div>
    </div>);

}
