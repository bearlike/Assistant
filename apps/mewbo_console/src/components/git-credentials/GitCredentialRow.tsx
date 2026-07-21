/**
 * GitCredentialRow — one stored credential in `GitCredentialsView`'s list.
 * Owns its own validate state: repo scopes validate directly, host scopes
 * collect a repo URL in a small popover first (a host credential isn't tied
 * to one repo, so there's nothing to validate against without asking).
 */
import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import {
  CheckCircle2,
  Loader2,
  Pencil,
  ShieldCheck,
  Trash2,
  XCircle,
} from 'lucide-react';

import {
  validateGitCredential,
  type GitCredentialSummary,
  type GitCredentialValidateResult,
} from '../../api/git';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { cn } from '../../lib/utils';
import { formatDateTime } from '../../utils/time';
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover';
import { KindChip, ScopeTypeChip } from './GitCredentialChips';

interface GitCredentialRowProps {
  cred: GitCredentialSummary;
  /** For host scopes only: count of wiki projects sharing it (null = hide). */
  sharedCount: number | null;
  onEdit: () => void;
  onDelete: () => void;
}

export function GitCredentialRow({ cred, sharedCount, onEdit, onDelete }: GitCredentialRowProps) {
  const isHost = cred.scopeType === 'host';
  const [popoverOpen, setPopoverOpen] = useState(false);
  const [repoUrl, setRepoUrl] = useState('');
  const [result, setResult] = useState<GitCredentialValidateResult | null>(null);

  const validateM = useMutation({
    mutationFn: (url?: string) => validateGitCredential(cred.scope, url),
    onSuccess: (r) => {
      setResult(r);
      setPopoverOpen(false);
    },
    onError: (e) => {
      setResult({ ok: false, detail: e instanceof Error ? e.message : 'Validation failed' });
      setPopoverOpen(false);
    },
  });

  const runIcon = validateM.isPending ? (
    <Loader2 className="w-3.5 h-3.5 animate-spin" />
  ) : (
    <ShieldCheck className="w-3.5 h-3.5" />
  );

  const validateButton = isHost ? (
    <Popover open={popoverOpen} onOpenChange={setPopoverOpen}>
      <PopoverTrigger asChild>
        <Button variant="ghost" size="sm" leadingIcon={runIcon} disabled={validateM.isPending}>
          Validate
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-72 space-y-2">
        <p className="text-xs text-[hsl(var(--muted-foreground))]">
          Host credentials aren&apos;t tied to one repo, so enter a repository URL on{' '}
          <code className="font-mono text-[hsl(var(--foreground))]">{cred.scope}</code> to test against.
        </p>
        <Input
          type="text"
          value={repoUrl}
          onChange={(e) => setRepoUrl(e.target.value)}
          placeholder={`https://${cred.scope}/owner/repo`}
          aria-label="Repository URL to validate against"
          className="text-field md:text-sm"
        />
        <div className="flex justify-end">
          <Button
            variant="primary"
            size="sm"
            disabled={!repoUrl.trim() || validateM.isPending}
            leadingIcon={runIcon}
            onClick={() => validateM.mutate(repoUrl.trim())}
          >
            Check
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  ) : (
    <Button
      variant="ghost"
      size="sm"
      leadingIcon={runIcon}
      disabled={validateM.isPending}
      onClick={() => validateM.mutate(undefined)}
    >
      Validate
    </Button>
  );

  return (
    <div className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-4 py-3 space-y-2">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1 space-y-1.5">
          <div className="flex items-center gap-2 flex-wrap">
            <code className="text-sm font-mono font-medium text-[hsl(var(--foreground))] break-all">
              {cred.scope}
            </code>
            <ScopeTypeChip scopeType={cred.scopeType} />
            <KindChip kind={cred.kind} />
          </div>
          <div className="flex items-center gap-3 flex-wrap text-xs text-[hsl(var(--muted-foreground))]">
            {cred.username && (
              <span>
                user <span className="font-mono text-[hsl(var(--foreground))]">{cred.username}</span>
              </span>
            )}
            <span className="font-mono">{cred.valueHint}</span>
            {cred.updatedAt && <span>Updated {formatDateTime(cred.updatedAt)}</span>}
            {cred.scopeType === 'host' && sharedCount != null && (
              <span>
                shared across {sharedCount} project{sharedCount === 1 ? '' : 's'}
              </span>
            )}
          </div>
        </div>

        <div className="flex items-center gap-1 shrink-0">
          {validateButton}
          <Button
            variant="ghost"
            size="sm"
            iconOnly
            onClick={onEdit}
            aria-label={`Edit credential for ${cred.scope}`}
            title="Replace the stored value"
            leadingIcon={<Pencil className="w-3.5 h-3.5" />}
          />
          <Button
            variant="ghost"
            size="sm"
            tone="danger"
            iconOnly
            onClick={onDelete}
            aria-label={`Delete credential for ${cred.scope}`}
            title="Delete credential"
            leadingIcon={<Trash2 className="w-3.5 h-3.5" />}
          />
        </div>
      </div>

      {result && (
        <div
          className={cn(
            'flex items-start gap-2 rounded-md border px-2.5 py-1.5 text-xs',
            result.ok
              ? 'border-[hsl(var(--success))]/30 bg-[hsl(var(--success))]/10 text-[hsl(var(--success))]'
              : 'border-[hsl(var(--destructive))]/30 bg-[hsl(var(--destructive))]/10 text-[hsl(var(--destructive-text))]'
          )}
        >
          {result.ok ? (
            <CheckCircle2 className="w-3.5 h-3.5 shrink-0 mt-0.5" />
          ) : (
            <XCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
          )}
          <span className="break-words">{result.detail}</span>
        </div>
      )}
    </div>
  );
}
