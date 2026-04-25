export const AGENT_ID_TAG_CLASS =
  'text-[10px] font-mono px-1.5 py-0.5 rounded bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))]';

export const MODEL_TAG_CLASS =
  'text-[10px] font-mono text-[hsl(var(--muted-foreground))] bg-[hsl(var(--muted))] px-1.5 py-0.5 rounded whitespace-nowrap';

/** Hash an agent id to one of 8 cycling color slots (Claude Code palette). */
export function agentColorIndex(agentId: string): number {
  let hash = 0;
  for (let i = 0; i < agentId.length; i++) {
    hash = ((hash << 5) - hash + agentId.charCodeAt(i)) | 0;
  }
  return Math.abs(hash) % 8;
}

export const AGENT_COLOR_CLASSES = [
  'text-agent-0', 'text-agent-1', 'text-agent-2', 'text-agent-3',
  'text-agent-4', 'text-agent-5', 'text-agent-6', 'text-agent-7',
] as const;

export const BADGE_COLOR_MAP: Record<string, string> = {
  emerald: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600',
  red: 'border-red-500/30 bg-red-500/10 text-red-600',
  amber: 'border-amber-500/30 bg-amber-500/10 text-amber-600',
  blue: 'border-blue-500/30 bg-blue-500/10 text-blue-600',
  cyan: 'border-cyan-500/30 bg-cyan-500/10 text-cyan-600',
  muted: 'border-[hsl(var(--border))] bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))]',
};
