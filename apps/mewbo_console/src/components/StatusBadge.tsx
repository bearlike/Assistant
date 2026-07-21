import {
  CheckCircle2,
  Circle,
  Archive,
  AlertCircle,
  PlayCircle,
  XCircle,
  Target,
  Ban } from
'lucide-react';
import { cn } from '../utils/cn';

interface StatusBadgeProps {
  status: string;
  doneReason?: string | null;
  compact?: boolean;
}

type StatusConfig = {
  icon: React.ElementType;
  label: string;
  style: string;
  padding: string;
};

const PILL = "rounded-full border shadow-sm text-xs font-medium";
const PILL_MUTED = "rounded-full border border-[hsl(var(--border))] bg-[hsl(var(--muted))] text-xs font-medium";
const PAD_STD = "px-2.5 py-1";

function resolveStatus(status: string, doneReason?: string | null): StatusConfig {
  const reason = String(doneReason || '').toLowerCase();

  // Running takes absolute precedence — a live run overrides any prior done_reason.
  if (status === 'running')
    return { icon: PlayCircle, label: 'Running', style: `${PILL} border-[hsl(var(--info)/0.3)] bg-[hsl(var(--info)/0.1)] text-[hsl(var(--info))]`, padding: PAD_STD };

  // Terminated is a permanent end-state — render it calm-but-final (a terminated
  // session is never running, so this can sit right after the running check).
  if (status === 'terminated')
    return { icon: Ban, label: 'Terminated', style: `${PILL_MUTED} text-[hsl(var(--muted-foreground))]`, padding: PAD_STD };

  // `blocked` and `unmet_goal` are STATUSES the runtime derives, not
  // done_reasons — this arm read only `done_reason` and was therefore
  // unreachable. Both fields are consulted because a deployed backend that
  // lags HEAD stamps the same words on `done_reason`.
  if (status === 'blocked' || reason === 'blocked')
    return { icon: AlertCircle, label: 'Blocked', style: `${PILL} border-[hsl(var(--warning)/0.3)] bg-[hsl(var(--warning)/0.1)] text-[hsl(var(--warning))]`, padding: PAD_STD };
  // Ended without an exception but without achieving what it was asked to do.
  // `verification_failed` and `halted_no_progress` are the pre-existing
  // done_reasons this status absorbs; before it existed they fell all the way
  // through to the Archived catch-all and read as a filed-away success.
  if (
    status === 'unmet_goal' ||
    reason === 'unmet_goal' ||
    reason === 'verification_failed' ||
    reason === 'halted_no_progress'
  )
    return { icon: Target, label: 'Goal not met', style: `${PILL} border-[hsl(var(--warning)/0.3)] bg-[hsl(var(--warning)/0.1)] text-[hsl(var(--warning))]`, padding: PAD_STD };
  if (reason === 'canceled')
    return { icon: XCircle, label: 'Canceled', style: "text-[hsl(var(--muted-foreground))] text-xs font-medium", padding: "px-2 py-0.5" };
  if (reason === 'error')
    return { icon: AlertCircle, label: 'Failed', style: `${PILL} border-[hsl(var(--destructive)/0.3)] bg-[hsl(var(--destructive)/0.1)] text-[hsl(var(--destructive-text))]`, padding: PAD_STD };
  if (reason === 'incomplete' || reason === 'max_iterations_reached' || reason === 'max_steps_reached')
    return { icon: AlertCircle, label: 'Incomplete', style: `${PILL} border-[hsl(var(--warning)/0.3)] bg-[hsl(var(--warning)/0.1)] text-[hsl(var(--warning))]`, padding: PAD_STD };

  if (status === 'completed' || status === 'merged')
    return { icon: CheckCircle2, label: 'Completed', style: `${PILL} border-[hsl(var(--success)/0.3)] bg-[hsl(var(--success)/0.1)] text-[hsl(var(--success))]`, padding: PAD_STD };
  if (status === 'incomplete')
    return { icon: AlertCircle, label: 'Incomplete', style: `${PILL} border-[hsl(var(--warning)/0.3)] bg-[hsl(var(--warning)/0.1)] text-[hsl(var(--warning))]`, padding: PAD_STD };
  // stone-400 intentionally NOT migrated to --muted: 'idle' must stay visually
  // distinct from 'terminated'/'archived' (both already on the --muted token
  // via PILL_MUTED just above/below) when shown side by side in a status list.
  if (status === 'idle')
    return { icon: Circle, label: 'Idle', style: `${PILL} border-stone-400/30 bg-stone-400/10 text-stone-500`, padding: PAD_STD };
  // lime-600 intentionally NOT migrated to --success: 'open' must stay visually
  // distinct from 'completed'/'merged' (--success above) when an Open PR-like
  // state is listed alongside a Merged/Completed one.
  if (status === 'open')
    return { icon: Circle, label: 'Open', style: `${PILL} border-lime-600/30 bg-lime-600/10 text-lime-700`, padding: PAD_STD };
  if (status === 'failed')
    return { icon: AlertCircle, label: 'Failed', style: `${PILL} border-[hsl(var(--destructive)/0.3)] bg-[hsl(var(--destructive)/0.1)] text-[hsl(var(--destructive-text))]`, padding: PAD_STD };
  // A recoverable paused state (a plan-mode proposal or wiki-QA awaiting the
  // operator, core `session_runtime` status `awaiting_approval`). Without this
  // arm it fell through to the Archived catch-all below and mislabelled a live,
  // non-archived session as archived. Warning tone: it wants attention, not a
  // "done" or "failed" read.
  if (status === 'awaiting_approval')
    return { icon: AlertCircle, label: 'Awaiting', style: `${PILL} border-[hsl(var(--warning)/0.3)] bg-[hsl(var(--warning)/0.1)] text-[hsl(var(--warning))]`, padding: PAD_STD };

  return { icon: Archive, label: 'Archived', style: `${PILL_MUTED} text-[hsl(var(--muted-foreground))]`, padding: PAD_STD };
}

export function StatusBadge({ status, doneReason, compact }: StatusBadgeProps) {
  const { icon: Icon, label, style, padding } = resolveStatus(status, doneReason);

  return (
    <div className={cn(
      "flex items-center shrink-0",
      compact ? "gap-0 px-1.5 py-0.5" : cn("gap-1.5", padding),
      style
    )}>
      <Icon className="w-3.5 h-3.5" />
      {!compact && <span>{label}</span>}
    </div>
  );
}
