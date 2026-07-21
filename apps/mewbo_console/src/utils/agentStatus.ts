/**
 * Shared agent status palette — used by any card that renders an agent
 * lifecycle state as a chip, dot, or tree row. Bound to the semantic status
 * tokens in `index.css` (`--success`/`--warning`/`--info`/`--destructive`/
 * `--muted`) so light/dark theming comes for free.
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
    text: 'text-[hsl(var(--info))]',
    bg: 'bg-[hsl(var(--info)/0.1)]',
    border: 'border-[hsl(var(--info)/0.3)]',
    dot: 'bg-[hsl(var(--info))]',
    ring: 'ring-[hsl(var(--info)/0.3)]',
  },
  completed: {
    text: 'text-[hsl(var(--success))]',
    bg: 'bg-[hsl(var(--success)/0.1)]',
    border: 'border-[hsl(var(--success)/0.3)]',
    dot: 'bg-[hsl(var(--success))]',
    ring: 'ring-[hsl(var(--success)/0.3)]',
  },
  failed: {
    text: 'text-[hsl(var(--destructive-text))]',
    bg: 'bg-[hsl(var(--destructive)/0.1)]',
    border: 'border-[hsl(var(--destructive)/0.3)]',
    dot: 'bg-[hsl(var(--destructive))]',
    ring: 'ring-[hsl(var(--destructive)/0.3)]',
  },
  cancelled: {
    text: 'text-[hsl(var(--warning))]',
    bg: 'bg-[hsl(var(--warning)/0.1)]',
    border: 'border-[hsl(var(--warning)/0.3)]',
    dot: 'bg-[hsl(var(--warning))]',
    ring: 'ring-[hsl(var(--warning)/0.3)]',
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
