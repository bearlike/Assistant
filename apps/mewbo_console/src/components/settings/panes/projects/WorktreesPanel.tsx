/**
 * WorktreesPanel — branch + worktree management for ONE managed project.
 *
 * Data comes from `useProjectGit` (TanStack Query), the same hook the composer's
 * ConfigMenu already uses: one `['project-git', <id>]` cache entry, so a
 * worktree created here shows up in the composer's picker without a refetch.
 * The panel previously raw-fetched branches/worktrees with `useState` +
 * `useEffect` and confirmed deletes with `window.confirm()` — both are gone.
 * What remains local is genuine UI state (the create-form mode + fields).
 */
import { useEffect, useMemo, useState } from 'react';
import { GitBranch, Loader2, Plus, RefreshCw, Trash2 } from 'lucide-react';

import { useProjectGit } from '../../../../hooks/useProjectGit';
import { WorktreeSummary } from '../../../../types';
import { getErrorMessage } from '../../../../utils/errors';
import {
  MEWBO_BRANCH_PREFIX,
  defaultMewboBranchName,
} from '../../../../utils/branchName';
import { cn } from '../../../../lib/utils';
import { Button } from '../../../ui/button';
import { cardSurface } from '../../../ui/card-surface';
import { Input } from '../../../ui/input';
import { ConfirmDialog } from '../../../ui/confirm-dialog';
import { ErrorAlert } from '../../../ErrorAlert';
import { inputBase } from '../../styles';

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------

interface WorktreesPanelProps {
  projectId: string;
}

export function WorktreesPanel({ projectId }: WorktreesPanelProps) {
  const git = useProjectGit(projectId);
  const {
    branches,
    branchesInUse,
    currentBranch,
    gitRepo,
    loading,
    worktrees,
    mutating,
  } = git;

  // Form state — explicit two-mode UX matching the composer's ConfigMenu.
  const [mode, setMode] = useState<'new' | 'existing'>('new');
  const [base, setBase] = useState<string>('');
  const [newBranch, setNewBranch] = useState<string>('');
  const [actionError, setActionError] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<WorktreeSummary | null>(null);

  // Pre-fill ``base`` once the data lands; users can override either way.
  useEffect(() => {
    if (!base && branches.length > 0) {
      setBase(currentBranch ?? branches[0]);
    }
  }, [branches, currentBranch, base]);

  // Re-prefill ``newBranch`` whenever ``base`` changes — but don't stomp a
  // user-edited non-mewbo name.
  useEffect(() => {
    setNewBranch((cur) => {
      if (!base) return cur;
      if (!cur || cur.startsWith(MEWBO_BRANCH_PREFIX)) {
        return defaultMewboBranchName(base);
      }
      return cur;
    });
  }, [base]);

  const inUseSet = useMemo(() => new Set(branchesInUse), [branchesInUse]);
  const wtBranchSet = useMemo(
    () => new Set(worktrees.map((w) => w.branch)),
    [worktrees],
  );

  const canSubmit = useMemo(() => {
    if (mutating) return false;
    if (!base) return false;
    if (mode === 'new') {
      const t = newBranch.trim();
      if (!t) return false;
      if (branches.includes(t)) return false;
      return true;
    }
    if (inUseSet.has(base)) return false;
    if (wtBranchSet.has(base)) return false;
    return true;
  }, [mutating, base, mode, newBranch, branches, inUseSet, wtBranchSet]);

  const error = actionError ?? git.error;
  const deleteDirty = deleteTarget?.clean === false;

  const handleCreate = async () => {
    if (!canSubmit) return;
    setActionError(null);
    try {
      await git.createWorktreeFor(
        mode === 'new' ? { branch: newBranch.trim(), base } : { branch: base },
      );
      // Reset just the new-branch field so the user can stamp out another
      // session-branch quickly without re-picking the base.
      setNewBranch(defaultMewboBranchName(base));
    } catch (err) {
      setActionError(getErrorMessage(err, 'Failed to create worktree'));
    }
  };

  const handleDeleteConfirm = async () => {
    const target = deleteTarget;
    if (!target?.project_id) return;
    setActionError(null);
    try {
      await git.deleteWorktreeFor(target.project_id, target.clean === false);
      setDeleteTarget(null);
    } catch (err) {
      setDeleteTarget(null);
      setActionError(getErrorMessage(err, 'Failed to delete worktree'));
    }
  };

  if (!gitRepo && !loading) {
    return (
      <div className="text-xs text-[hsl(var(--muted-foreground))] flex items-center gap-2">
        <GitBranch className="w-3.5 h-3.5" />
        Not a git repository, so worktrees are unavailable.
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-[hsl(var(--foreground))] flex items-center gap-1.5">
          <GitBranch className="w-3.5 h-3.5" />
          Worktrees
        </span>
        {loading && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
      </div>

      {error && <ErrorAlert error={error} fallback="Worktree operation failed" />}

      {worktrees.length === 0 && !loading && (
        <div className="text-xs text-[hsl(var(--muted-foreground))]">
          No worktrees yet. Add one to give a session its own branch and checkout, so parallel
          runs don&apos;t edit the same files.
        </div>
      )}

      {worktrees.map((wt) => {
        const rowKey = wt.project_id ?? `unmanaged:${wt.branch}:${wt.path}`;
        return (
          <div
            key={rowKey}
            className={cn(
              cardSurface({ radius: 'right' }),
              'flex items-center justify-between text-xs px-2 py-1.5'
            )}
          >
            <div className="flex items-center gap-2 min-w-0">
              <GitBranch className="w-3.5 h-3.5 text-[hsl(var(--muted-foreground))] shrink-0" />
              <span className="truncate font-mono font-medium text-[hsl(var(--foreground))]">{wt.branch}</span>
              <span
                className="text-2xs text-[hsl(var(--muted-foreground))] bg-[hsl(var(--muted))] px-1 py-0.5 rounded shrink-0"
                title={wt.managed ? 'Created by Mewbo' : 'Existing on-disk worktree'}
              >
                {wt.managed ? 'managed' : 'user'}
              </span>
              {wt.clean === false && (
                <span
                  title="Uncommitted changes or unpushed commits"
                  className="text-2xs text-[hsl(var(--warning))] bg-[hsl(var(--warning))]/10 px-1 py-0.5 rounded shrink-0"
                >
                  Uncommitted
                </span>
              )}
            </div>
            {wt.managed && wt.project_id ? (
              <Button
                size="sm"
                variant="ghost"
                tone="danger"
                iconOnly
                disabled={mutating}
                onClick={() => setDeleteTarget(wt)}
                aria-label={`Delete worktree ${wt.branch}`}
                title="Delete worktree"
                leadingIcon={<Trash2 className="w-3 h-3" />}
              />
            ) : (
              <span
                className="text-2xs text-[hsl(var(--muted-foreground))]"
                title="User-created worktrees must be removed with `git worktree remove` from the parent repo"
              >
                git CLI
              </span>
            )}
          </div>
        );
      })}

      {/* Worktree creation form. Two explicit modes — defaults to "new
          branch from base" since that's the recommended workflow that
          guarantees session isolation. */}
      <div
        className={cn(
          cardSurface({ radius: 'right' }),
          'flex flex-col gap-2 mt-1 px-2 py-2'
        )}
      >
        <div className="flex items-center gap-1 text-2xs text-[hsl(var(--muted-foreground))]">
          <Button
            size="sm"
            variant="ghost"
            className={cn(
              'h-6 px-2 text-2xs',
              mode === 'new' && 'bg-[hsl(var(--accent))] text-[hsl(var(--foreground))]'
            )}
            aria-pressed={mode === 'new'}
            onClick={() => setMode('new')}
          >
            New branch from base
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className={cn(
              'h-6 px-2 text-2xs',
              mode === 'existing' && 'bg-[hsl(var(--accent))] text-[hsl(var(--foreground))]'
            )}
            aria-pressed={mode === 'existing'}
            onClick={() => setMode('existing')}
          >
            Use existing branch
          </Button>
        </div>

        <label className="flex flex-col gap-0.5">
          <span className="text-2xs uppercase tracking-wide text-[hsl(var(--muted-foreground))]">
            {mode === 'new' ? 'Base branch' : 'Branch'}
          </span>
          <select
            value={base}
            onChange={(e) => setBase(e.target.value)}
            disabled={branches.length === 0 || mutating}
            className={cn(inputBase, 'h-7 py-1 text-xs font-mono')}
          >
            {branches.map((b) => {
              const disabled =
                mode === 'existing' && (inUseSet.has(b) || wtBranchSet.has(b));
              const suffix = inUseSet.has(b)
                ? ' (in use)'
                : wtBranchSet.has(b)
                  ? ' (worktree exists)'
                  : b === currentBranch
                    ? ' (current)'
                    : '';
              return (
                <option key={b} value={b} disabled={disabled}>
                  {b}
                  {suffix}
                </option>
              );
            })}
          </select>
        </label>

        {mode === 'new' && (
          <label className="flex flex-col gap-0.5">
            <span className="text-2xs uppercase tracking-wide text-[hsl(var(--muted-foreground))]">
              New branch name
            </span>
            <div className="flex items-center gap-1">
              <Input
                type="text"
                value={newBranch}
                onChange={(e) => setNewBranch(e.target.value)}
                placeholder="mewbo/feature-x-ab12cd"
                disabled={mutating}
                className="h-7 flex-1 font-mono text-field md:text-sm"
              />
              <Button
                size="sm"
                variant="ghost"
                iconOnly
                onClick={() => setNewBranch(defaultMewboBranchName(base))}
                disabled={mutating || !base}
                aria-label="Generate a fresh branch name"
                title="Generate a fresh mewbo/<base>-<id> name"
                leadingIcon={<RefreshCw className="w-3 h-3" />}
              />
            </div>
            {newBranch && branches.includes(newBranch.trim()) && (
              <span className="text-2xs text-[hsl(var(--warning))]">
                Branch already exists. Pick a different name.
              </span>
            )}
          </label>
        )}

        <p className="text-2xs text-[hsl(var(--muted-foreground))] leading-snug">
          {mode === 'new' && base && newBranch.trim()
            ? `Will run: git worktree add -b ${newBranch.trim()} <path> ${base}`
            : mode === 'existing' && base
              ? `Will run: git worktree add <path> ${base}`
              : 'Pick a base branch to continue.'}
        </p>

        <Button
          size="sm"
          variant="primary"
          onClick={handleCreate}
          disabled={!canSubmit}
          leadingIcon={
            mutating ? <Loader2 className="w-3 h-3 animate-spin" /> : <Plus className="w-3 h-3" />
          }
        >
          Create worktree
        </Button>
      </div>

      {/* A dirty worktree (uncommitted changes / unpushed commits) turns the
          same confirm into an explicit FORCE delete: different title, different
          copy, different button. */}
      <ConfirmDialog
        open={deleteTarget !== null}
        title={deleteDirty ? 'Force-delete worktree?' : 'Remove worktree?'}
        description={
          <>
            This removes the worktree for{' '}
            <span className="font-mono font-medium text-[hsl(var(--foreground))]">
              {deleteTarget?.branch}
            </span>
            .{' '}
            {deleteDirty
              ? 'It has uncommitted changes or unpushed commits, and they will be lost. This cannot be undone.'
              : 'The branch itself is kept; only the checked-out worktree directory goes away.'}
          </>
        }
        confirmLabel={deleteDirty ? 'Force delete' : 'Remove worktree'}
        pendingLabel="Deleting…"
        confirmIcon={<Trash2 className="w-4 h-4" />}
        pending={mutating}
        onConfirm={() => void handleDeleteConfirm()}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}
