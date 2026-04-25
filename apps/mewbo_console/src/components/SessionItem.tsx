import React from 'react';
import { Archive, GitFork, RotateCcw } from 'lucide-react';
import { SessionSummary } from '../types';
import { StatusBadge } from './StatusBadge';
import { formatSessionTime } from '../utils/time';
interface SessionItemProps {
  session: SessionSummary;
  onClick: (sessionId: string) => void;
  onArchive?: (sessionId: string) => void;
  onUnarchive?: (sessionId: string) => void;
}
export function SessionItem({
  session,
  onClick,
  onArchive,
  onUnarchive
}: SessionItemProps) {
  const isArchived = Boolean(session.archived);
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
          {(session.context?.project || session.context?.repo) && (
            <>
              <span>·</span>
              <GitFork className="w-3 h-3 shrink-0" />
              <span className="truncate">{session.context?.project || session.context?.repo}</span>
            </>
          )}
          {session.context?.branch && (
            <span className="font-mono truncate bg-[hsl(var(--muted))] px-1.5 py-0.5 rounded text-[11px]">{session.context.branch}</span>
          )}
        </div>
      </div>

      <div className="flex items-center gap-3 shrink-0 pt-0.5">
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
