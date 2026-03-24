import React from 'react';
import { Archive, Layers, RotateCcw } from 'lucide-react';
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
      className="group flex items-center justify-between py-3 px-2 -mx-2 rounded-lg hover:bg-[hsl(var(--accent))] cursor-pointer transition-colors">

      <div className="flex flex-col gap-1">
        <h3 className="text-sm font-medium text-[hsl(var(--foreground))] group-hover:opacity-90 transition-colors">
          {session.title}
        </h3>
        <div className="flex items-center gap-1.5 text-xs text-[hsl(var(--muted-foreground))]">
          <span>{formatSessionTime(session.created_at)}</span>
          <span>·</span>
          <span>{session.context?.repo || 'No repo'}</span>
          <span>·</span>
          <span className="font-mono">
            {session.context?.branch || 'No branch'}
          </span>
        </div>
      </div>

      <div className="flex items-center gap-4">
        <StatusBadge
          status={session.status || 'idle'}
          doneReason={session.done_reason} />

        {session.status !== 'failed' && session.context?.mcp_tools?.length ?
        <div className="flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))] text-xs font-medium border border-[hsl(var(--border))]">
            <Layers className="w-3 h-3" />
            <span>{session.context?.mcp_tools?.length}</span>
          </div> :
        null}
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