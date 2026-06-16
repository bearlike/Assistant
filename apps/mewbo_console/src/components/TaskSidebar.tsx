import { useMemo } from 'react';
import { useLocation, useRoute } from 'wouter';
import { Plus } from 'lucide-react';
import { cn } from '../utils/cn';
import { useSessions } from '../hooks/useSessions';
import { useProjects } from '../hooks/useProjects';
import { ProjectLabel } from '../utils/projectLabel';
import { SessionOriginBadge } from './SessionOriginBadge';
import { Button } from './ui/button';
import { formatSessionTime } from '../utils/time';
import { isDefaultVisibleOrigin } from '../utils/sessionOrigins';
import { SessionSummary } from '../types';

// How many recent tasks the sidebar keeps in view. The full, filterable list
// still lives on the landing page (`/`); the footer links there.
const RECENT_LIMIT = 15;

interface TaskSidebarProps {
  /** Extra classes for the outer column — the parent owns width/border so the
   *  same content fills both the inline aside and the mobile Sheet. */
  className?: string;
  /** Called after a navigation so the parent can close the mobile drawer. */
  onAfterNavigate?: () => void;
}

/**
 * Persistent left task panel (LibreChat / ChatGPT / Claude-mobile style).
 * Self-contained: it shares the landing page's session/project queries via the
 * TanStack cache (no duplicate fetch), reuses the origin badge + project label,
 * honours the same default origin filter, and navigates with wouter. The
 * parent (AppLayout) decides whether to render it inline or inside a Sheet.
 */
export function TaskSidebar({ className, onAfterNavigate }: TaskSidebarProps) {
  const [, navigate] = useLocation();
  const [isSession, params] = useRoute<{ id: string }>('/s/:id');
  const activeId = isSession && params ? decodeURIComponent(params.id) : null;

  const { sessions, loading } = useSessions();
  const { projects } = useProjects();
  const projectLabel = useMemo(() => new ProjectLabel(projects), [projects]);

  // Same user-facing set as the landing page: default origin filter, newest
  // first (the backend already sorts `created_at` desc), capped for calm.
  const recents = useMemo(
    () => sessions.filter((s) => isDefaultVisibleOrigin(s.origin)),
    [sessions],
  );
  const visible = recents.slice(0, RECENT_LIMIT);

  const go = (path: string) => {
    navigate(path);
    onAfterNavigate?.();
  };

  return (
    <nav
      aria-label="Recent tasks"
      className={cn('flex h-full flex-col bg-[hsl(var(--card))]', className)}>
      <div className="px-3 py-3">
        <Button
          variant="primary"
          size="md"
          className="w-full justify-start"
          leadingIcon={<Plus className="w-4 h-4" />}
          onClick={() => go('/')}>
          New task
        </Button>
      </div>

      <div className="px-3 pb-1 text-[11px] font-medium uppercase tracking-wider text-[hsl(var(--muted-foreground))]">
        Recents
      </div>

      <div className="flex-1 overflow-y-auto px-1.5 pb-2">
        {loading && visible.length === 0 && (
          <div className="px-2 py-2 text-sm text-[hsl(var(--muted-foreground))]">
            Loading tasks…
          </div>
        )}
        {!loading && visible.length === 0 && (
          <div className="px-2 py-2 text-sm text-[hsl(var(--muted-foreground))]">
            No tasks yet.
          </div>
        )}
        <div className="space-y-0.5">
          {visible.map((session) => (
            <SidebarRow
              key={session.session_id}
              session={session}
              active={session.session_id === activeId}
              projectLabel={projectLabel}
              onSelect={(id) => go(`/s/${encodeURIComponent(id)}`)}
            />
          ))}
        </div>
      </div>

      {recents.length > RECENT_LIMIT && (
        <button
          type="button"
          onClick={() => go('/')}
          className="border-t border-[hsl(var(--border))] px-3 py-2.5 text-left text-xs font-medium text-[hsl(var(--muted-foreground))] transition-colors hover:text-[hsl(var(--foreground))]">
          View all tasks
        </button>
      )}
    </nav>
  );
}

/** Compact recent-task row. Narrower than the landing page's `SessionItem`
 *  (no branch/workspace/capability chips), but reuses the same origin badge,
 *  project label, and time format so provenance reads consistently. */
function SidebarRow({
  session,
  active,
  projectLabel,
  onSelect,
}: {
  session: SessionSummary;
  active: boolean;
  projectLabel: ProjectLabel;
  onSelect: (sessionId: string) => void;
}) {
  const { label: project } = projectLabel.resolve(session.context);
  return (
    <button
      type="button"
      onClick={() => onSelect(session.session_id)}
      aria-current={active ? 'page' : undefined}
      className={cn(
        'w-full rounded-md px-2.5 py-2 text-left text-[hsl(var(--foreground))] transition-colors',
        active
          ? 'bg-[hsl(var(--accent))]'
          : 'hover:bg-[hsl(var(--accent))]',
      )}>
      <div className="truncate text-sm font-medium">
        {session.title || 'Untitled'}
      </div>
      <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-[hsl(var(--muted-foreground))]">
        <span className="truncate whitespace-nowrap">
          {formatSessionTime(session.created_at)}
        </span>
        <SessionOriginBadge session={session} />
        {project && (
          <>
            <span aria-hidden>·</span>
            <span className="truncate">{project}</span>
          </>
        )}
      </div>
    </button>
  );
}
