export const AGENT_ID_TAG_CLASS =
  'text-2xs font-mono px-1.5 py-0.5 rounded bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))]';

export const MODEL_TAG_CLASS =
  'text-2xs text-[hsl(var(--muted-foreground))] bg-[hsl(var(--muted))] px-1.5 py-0.5 rounded whitespace-nowrap';

/** Hash an agent id to one of 8 cycling color slots. */
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
  emerald: 'border-[hsl(var(--success)/0.3)] bg-[hsl(var(--success)/0.1)] text-[hsl(var(--success))]',
  red: 'border-[hsl(var(--destructive)/0.3)] bg-[hsl(var(--destructive)/0.1)] text-[hsl(var(--destructive-text))]',
  amber: 'border-[hsl(var(--warning)/0.3)] bg-[hsl(var(--warning)/0.1)] text-[hsl(var(--warning))]',
  blue: 'border-[hsl(var(--info)/0.3)] bg-[hsl(var(--info)/0.1)] text-[hsl(var(--info))]',
  // primary is a STATE tone, not an identity hue: the brand clay marks an
  // active/in-progress badge (apps AppStatusBadge "Building", wiki
  // FreshnessBadge "refreshing/indexing"). By the identity-vs-state test the
  // colour is gated on processing state, so it belongs with the semantic keys
  // above rather than the identity block below.
  primary: 'border-[hsl(var(--primary)/0.3)] bg-[hsl(var(--primary)/0.1)] text-[hsl(var(--primary-text))]',
  // cyan intentionally NOT migrated to --info: SessionOriginBadge (wiki=blue vs
  // search=cyan) and triggerFormat.ts KIND_META (time.*=blue vs webhook=cyan)
  // show blue and cyan side by side as distinct identity chips — collapsing
  // both onto --info would erase that distinction.
  cyan: 'border-cyan-500/30 bg-cyan-500/10 text-cyan-600',
  violet: 'border-violet-500/30 bg-violet-500/10 text-violet-600',
  teal: 'border-teal-500/30 bg-teal-500/10 text-teal-600',
  // fuchsia is the apps origin chip. It sits beside violet (structured) in the
  // same filter menu, so the magenta cast is what keeps the two readable as
  // distinct classes rather than two shades of purple; --primary was rejected
  // because the identity-vs-state test files the brand clay under state.
  fuchsia: 'border-fuchsia-500/30 bg-fuchsia-500/10 text-fuchsia-600',
  muted: 'border-[hsl(var(--border))] bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))]',
};
