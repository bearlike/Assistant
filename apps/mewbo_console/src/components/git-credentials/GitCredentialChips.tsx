/**
 * GitCredentialChips — the two `rounded-full` identity chips shared by
 * `GitCredentialRow` (the list) and `GitCredentialDialog` (the add/edit
 * form's live scope-type preview).
 *
 * Not migrated onto the shared `Badge` (`components/agents.tsx`): both chips
 * pair an icon with a label, and `Badge`'s own class string has no
 * `inline-flex`/`gap`, so an icon dropped into its children would sit on the
 * text baseline unaligned rather than centered. `ScopeTypeChip`'s host
 * variant also has no home in `BADGE_COLOR_MAP` (`primary`-tinted; the map
 * only has `emerald`/`red`/`amber`/`blue`/`cyan`/`violet`/`teal`/`muted`).
 * Both are DRY-report items, not fixed here — see the burn-down report.
 */
import { FolderGit2, KeyRound, Server, Terminal } from 'lucide-react';

import { cn } from '../../lib/utils';
import type { GitCredentialKind, GitCredentialSummary } from '../../api/git';

const KIND_LABEL: Record<GitCredentialKind, string> = {
  token: 'Token',
  ssh_key: 'SSH key',
};

export function ScopeTypeChip({ scopeType }: { scopeType: GitCredentialSummary['scopeType'] }) {
  const host = scopeType === 'host';
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-2xs font-medium leading-none border',
        host
          ? 'bg-[hsl(var(--primary))]/10 text-[hsl(var(--primary-text))] border-[hsl(var(--primary))]/20'
          : 'bg-[hsl(var(--muted))]/50 text-[hsl(var(--muted-foreground))] border-[hsl(var(--border))]'
      )}
    >
      {host ? <Server className="w-3 h-3" /> : <FolderGit2 className="w-3 h-3" />}
      {host ? 'Shared host' : 'Repository'}
    </span>
  );
}

export function KindChip({ kind }: { kind: GitCredentialKind }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-2xs font-medium leading-none bg-[hsl(var(--muted))]/40 text-[hsl(var(--muted-foreground))] border border-[hsl(var(--border))]">
      {kind === 'ssh_key' ? <Terminal className="w-3 h-3" /> : <KeyRound className="w-3 h-3" />}
      {KIND_LABEL[kind]}
    </span>
  );
}
