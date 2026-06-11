import React from 'react';
import { Archive, GitFork, Play, RotateCcw } from 'lucide-react';
import { SessionSummary } from '../types';
import { StatusBadge } from './StatusBadge';
import { SessionOriginBadge } from './SessionOriginBadge';
import { Button } from './ui/button';
import { useRecoverSession } from '../hooks/useRecoverSession';
import { ProjectLabel } from '../utils/projectLabel';
import { formatSessionTime } from '../utils/time';
interface SessionItemProps {
  session: SessionSummary;
  projectLabel: ProjectLabel;
  onClick: (sessionId: string) => void;
  onArchive?: (sessionId: string) => void;
  onUnarchive?: (sessionId: string) => void;
}
export function SessionItem({
  session,
  projectLabel,
  onClick,
  onArchive,
  onUnarchive
}: SessionItemProps) {
  const isArchived = Boolean(session.archived);
  const { label: project, branch } = projectLabel.resolve(session.context);
  const capabilities = session.capabilities ?? [];
  const workspace = session.workspace ?? null;
  const recover = useRecoverSession();
  const showRecover = Boolean(session.recoverable) && !session.running;
  const handleRecover = (
    event: React.MouseEvent<HTMLButtonElement>,
    action: 'retry' | 'continue',
  ) => {
    event.stopPropagation();
    if (recover.isPending) return;
    recover.mutate({ sessionId: session.session_id, action });
  };
  const handleArchive = (event: React.MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    if (isArchived) {
      onUnarchive?.(session.session_id);
    } else {
      onArchive?.(session.session_id);
    }
  };
  return (
    <div
      onClick={() => onClick(session.session_id)}
      className="group flex items-start gap-4 py-3.5 px-3 hover:bg-[hsl(var(--accent))] cursor-pointer transition-colors">

      <div className="flex flex-col gap-1.5 min-w-0 flex-1">
        <h3 className="text-sm font-medium text-[hsl(var(--foreground))] group-hover:opacity-90 transition-colors line-clamp-2">
          {session.title}
        </h3>
        <div className="flex items-center gap-1.5 text-xs text-[hsl(var(--muted-foreground))]">
          <span className="whitespace-nowrap">{formatSessionTime(session.created_at)}</span>
          <SessionOriginBadge session={session} />
          {project && (
            <>
              <span>·</span>
              <GitFork className="w-3 h-3 shrink-0" />
              <span className="truncate">{project}</span>
            </>
          )}
          {branch && (
            <span className="font-mono truncate bg-[hsl(var(--muted))] px-1.5 py-0.5 rounded text-[11px]">{branch}</span>
          )}
          {workspace && (
            <span
              className="font-mono truncate bg-[hsl(var(--muted))] px-1.5 py-0.5 rounded text-[11px]"
              title={`Workspace ${workspace}`}
            >
              {workspace}
            </span>
          )}
          {capabilities.map((cap) => (
            <span
              key={cap}
              className="uppercase tracking-wide bg-[hsl(var(--muted))] px-1.5 py-0.5 rounded text-[10px]"
              title={`Scoped to the "${cap}" capability`}
            >
              {cap}
            </span>
          ))}
        </div>
      </div>

      <div className="flex items-center gap-2 shrink-0 pt-0.5">
        {showRecover && (
          <div className="flex items-center gap-1.5 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
            <Button
              variant="neutral"
              size="sm"
              tone="warn"
              disabled={recover.isPending}
              leadingIcon={<Play className="w-3 h-3" />}
              onClick={(e) => handleRecover(e, 'continue')}
              title="Resume this session with its context intact"
            >
              Continue
            </Button>
            <Button
              variant="neutral"
              size="sm"
              tone="info"
              disabled={recover.isPending}
              leadingIcon={<RotateCcw className="w-3 h-3" />}
              onClick={(e) => handleRecover(e, 'retry')}
              title="Restart the last turn from scratch"
            >
              Restart
            </Button>
          </div>
        )}
        <StatusBadge
          status={session.status || 'idle'}
          doneReason={session.done_reason} />

        {(onArchive || onUnarchive) &&
        <button
          onClick={handleArchive}
          aria-label={isArchived ? 'Unarchive session' : 'Archive session'}
          className="text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] opacity-0 group-hover:opacity-100 transition-opacity">

            {isArchived ?
          <RotateCcw className="w-4 h-4" /> :

          <Archive className="w-4 h-4" />
          }
          </button>
        }
      </div>
    </div>);

}
