import {
  CheckCircle2,
  Circle,
  Archive,
  AlertCircle,
  PlayCircle,
  XCircle } from
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

const PILL = "rounded-full border shadow-sm text-xs font-semibold";
const PILL_MUTED = "rounded-full border border-[hsl(var(--border))] bg-[hsl(var(--muted))] text-xs font-medium";
const PAD_STD = "px-2.5 py-1";

function resolveStatus(status: string, doneReason?: string | null): StatusConfig {
  const reason = String(doneReason || '').toLowerCase();

  // Running takes absolute precedence — a live run overrides any prior done_reason.
  if (status === 'running')
    return { icon: PlayCircle, label: 'Running', style: `${PILL} border-blue-500/30 bg-blue-500/10 text-blue-600`, padding: PAD_STD };

  if (reason === 'blocked')
    return { icon: AlertCircle, label: 'Blocked', style: `${PILL} border-amber-500/30 bg-amber-500/10 text-amber-600`, padding: PAD_STD };
  if (reason === 'canceled')
    return { icon: XCircle, label: 'Canceled', style: "text-[hsl(var(--muted-foreground))] text-xs font-medium", padding: "px-2 py-0.5" };
  if (reason === 'error')
    return { icon: AlertCircle, label: 'Failed', style: `${PILL} border-red-500/30 bg-red-500/10 text-red-600`, padding: PAD_STD };
  if (reason === 'incomplete' || reason === 'max_iterations_reached' || reason === 'max_steps_reached')
    return { icon: AlertCircle, label: 'Incomplete', style: `${PILL} border-amber-500/30 bg-amber-500/10 text-amber-600`, padding: PAD_STD };

  if (status === 'completed' || status === 'merged')
    return { icon: CheckCircle2, label: 'Completed', style: `${PILL} border-emerald-500/30 bg-emerald-500/10 text-emerald-600`, padding: PAD_STD };
  if (status === 'incomplete')
    return { icon: AlertCircle, label: 'Incomplete', style: `${PILL} border-amber-500/30 bg-amber-500/10 text-amber-600`, padding: PAD_STD };
  if (status === 'idle')
    return { icon: Circle, label: 'Idle', style: `${PILL} border-stone-400/30 bg-stone-400/10 text-stone-500`, padding: PAD_STD };
  if (status === 'open')
    return { icon: Circle, label: 'Open', style: `${PILL} border-lime-600/30 bg-lime-600/10 text-lime-700`, padding: PAD_STD };
  if (status === 'failed')
    return { icon: AlertCircle, label: 'Failed', style: `${PILL} border-red-500/30 bg-red-500/10 text-red-600`, padding: PAD_STD };

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
