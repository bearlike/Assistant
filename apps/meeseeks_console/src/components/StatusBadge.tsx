import {
  CheckCircle2,
  Circle,
  Archive,
  AlertCircle,
  PlayCircle,
  XCircle } from
'lucide-react';
interface StatusBadgeProps {
  status: string;
  doneReason?: string | null;
}
export function StatusBadge({ status, doneReason }: StatusBadgeProps) {
  const normalizedReason = String(doneReason || '').toLowerCase();
  if (normalizedReason === 'blocked') {
    return (
      <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-amber-500/30 bg-amber-500/10 text-amber-600 text-xs font-semibold shadow-sm">
        <AlertCircle className="w-3.5 h-3.5" />
        <span>Blocked</span>
      </div>);

  }
  if (normalizedReason === 'canceled') {
    return (
      <div className="flex items-center gap-1.5 px-2 py-0.5 text-[hsl(var(--muted-foreground))] text-xs font-medium">
        <XCircle className="w-3.5 h-3.5" />
        <span>Canceled</span>
      </div>);

  }
  if (normalizedReason === 'error') {
    return (
      <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-red-500/30 bg-red-500/10 text-red-600 text-xs font-semibold shadow-sm">
        <AlertCircle className="w-3.5 h-3.5" />
        <span>Failed</span>
      </div>);

  }
  if (normalizedReason === 'incomplete' || normalizedReason === 'max_iterations_reached' || normalizedReason === 'max_steps_reached') {
    return (
      <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-amber-500/30 bg-amber-500/10 text-amber-600 text-xs font-semibold shadow-sm">
        <AlertCircle className="w-3.5 h-3.5" />
        <span>Incomplete</span>
      </div>);

  }
  if (status === 'running') {
    return (
      <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-blue-500/30 bg-blue-500/10 text-blue-600 text-xs font-semibold shadow-sm">
        <PlayCircle className="w-3.5 h-3.5" />
        <span>Running</span>
      </div>);

  }
  if (status === 'completed' || status === 'merged') {
    return (
      <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-emerald-500/30 bg-emerald-500/10 text-emerald-600 text-xs font-semibold shadow-sm">
        <CheckCircle2 className="w-3.5 h-3.5" />
        <span>Completed</span>
      </div>);

  }
  if (status === 'incomplete') {
    return (
      <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-amber-500/30 bg-amber-500/10 text-amber-600 text-xs font-semibold shadow-sm">
        <AlertCircle className="w-3.5 h-3.5" />
        <span>Incomplete</span>
      </div>);

  }
  if (status === 'idle') {
    return (
      <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-[hsl(var(--border))] bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))] text-xs font-medium">
        <Circle className="w-3.5 h-3.5" />
        <span>Idle</span>
      </div>);

  }
  if (status === 'open') {
    return (
      <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-[hsl(var(--border))] bg-[hsl(var(--muted))] text-[hsl(var(--foreground))] text-xs font-medium">
        <Circle className="w-3.5 h-3.5" />
        <span>Open</span>
      </div>);

  }
  if (status === 'failed') {
    return (
      <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-red-500/30 bg-red-500/10 text-red-600 text-xs font-semibold shadow-sm">
        <AlertCircle className="w-3.5 h-3.5" />
        <span>Failed</span>
      </div>);

  }
  return (
    <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-[hsl(var(--border))] bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))] text-xs font-medium">
      <Archive className="w-3.5 h-3.5" />
      <span>Archived</span>
    </div>);

}
