/**
 * Shared agent status palette — used by any card that renders an agent
 * lifecycle state as a chip, dot, or tree row. Tailwind palette classes
 * (no custom CSS vars) so light/dark theming comes for free.
 *
 * Keep the five keys aligned with `AgentStatus` in hypervisor.py.
 * Non-matching strings fold to `submitted`.
 */

export type StatusKey =
  | 'submitted'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled';

export interface StatusStyle {
  text: string;
  bg: string;
  border: string;
  dot: string;
  ring: string;
}

export const STATUS_STYLES: Record<StatusKey, StatusStyle> = {
  submitted: {
    text: 'text-[hsl(var(--muted-foreground))]',
    bg: 'bg-[hsl(var(--muted))]',
    border: 'border-[hsl(var(--border))]',
    dot: 'bg-[hsl(var(--muted-foreground))]',
    ring: 'ring-[hsl(var(--muted-foreground))]/20',
  },
  running: {
    text: 'text-cyan-600',
    bg: 'bg-cyan-500/10',
    border: 'border-cyan-500/30',
    dot: 'bg-cyan-500',
    ring: 'ring-cyan-500/30',
  },
  completed: {
    text: 'text-emerald-600',
    bg: 'bg-emerald-500/10',
    border: 'border-emerald-500/30',
    dot: 'bg-emerald-500',
    ring: 'ring-emerald-500/30',
  },
  failed: {
    text: 'text-red-600',
    bg: 'bg-red-500/10',
    border: 'border-red-500/30',
    dot: 'bg-red-500',
    ring: 'ring-red-500/30',
  },
  cancelled: {
    text: 'text-amber-600',
    bg: 'bg-amber-500/10',
    border: 'border-amber-500/30',
    dot: 'bg-amber-500',
    ring: 'ring-amber-500/30',
  },
};

export const STATUS_ORDER: readonly StatusKey[] = [
  'running',
  'submitted',
  'completed',
  'failed',
  'cancelled',
];

const KEY_SET = new Set<string>(STATUS_ORDER);

export function statusKey(s: string): StatusKey {
  return KEY_SET.has(s) ? (s as StatusKey) : 'submitted';
}

const TERMINAL = new Set<StatusKey>(['completed', 'failed', 'cancelled']);

export function isTerminal(s: string): boolean {
  return TERMINAL.has(statusKey(s));
}
