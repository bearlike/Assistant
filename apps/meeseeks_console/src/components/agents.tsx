import { ReactNode } from 'react';
import { cn } from '../utils/cn';
import { AGENT_ID_TAG_CLASS, BADGE_COLOR_MAP } from '../utils/agents';
import { STATUS_STYLES, statusKey } from '../utils/agentStatus';

export function AgentIdChip({ agentId, className }: { agentId?: string; className?: string }) {
  if (!agentId) return null;
  return <span className={cn(AGENT_ID_TAG_CLASS, className)}>{agentId.slice(0, 8)}</span>;
}

export function Badge({ children, color }: { children: ReactNode; color: string }) {
  return (
    <span className={cn(
      'text-[10px] font-medium px-1.5 py-0.5 rounded-full border whitespace-nowrap',
      BADGE_COLOR_MAP[color] || BADGE_COLOR_MAP.muted,
    )}>
      {children}
    </span>
  );
}

interface StatusDotProps {
  status: string;
  size?: 'sm' | 'md';
  pulse?: boolean;
  className?: string;
}

/** Colored status dot. `pulse` adds a pulse ring — use for running agents. */
export function StatusDot({ status, size = 'md', pulse, className }: StatusDotProps) {
  const s = STATUS_STYLES[statusKey(status)];
  return (
    <span
      aria-hidden
      className={cn(
        'inline-block rounded-full shrink-0',
        size === 'sm' ? 'w-1 h-1' : 'w-2 h-2',
        s.dot,
        pulse && cn('ring-2 animate-pulse', s.ring),
        className,
      )}
    />
  );
}

interface StatusPillProps {
  status: string;
  label?: string;
  count?: number;
  className?: string;
}

/**
 * Rounded status chip: dot + label (or count). Used in tool-card headers
 * (single status) and in CheckAgentsCard count chips (status + number).
 */
export function StatusPill({ status, label, count, className }: StatusPillProps) {
  const key = statusKey(status);
  const s = STATUS_STYLES[key];
  return (
    <span className={cn(
      'inline-flex items-center gap-1 rounded-full border',
      'text-[10px] font-mono font-medium px-1.5 py-px whitespace-nowrap',
      s.bg, s.border, s.text,
      className,
    )}>
      <StatusDot status={status} size="sm" />
      {count != null ? count : (label ?? key)}
    </span>
  );
}
