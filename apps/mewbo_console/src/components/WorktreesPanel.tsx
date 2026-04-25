import { useCallback, useEffect, useState } from 'react';
import { GitBranch, Loader2, Plus, Trash2, AlertTriangle } from 'lucide-react';
import {
  createWorktree,
  deleteWorktree,
  listProjectBranches,
  listWorktrees,
} from '../api/client';
import { WorktreeSummary } from '../types';
import { Button } from './ui/button';
import { logApiError } from '../utils/errors';

interface WorktreesPanelProps {
  projectId: string;
}

/**
 * Branch + worktree management for a single managed project.
 *
 * KISS: one component, no state library. Works against the backend's
 * `/api/v_projects/<id>/{branches,worktrees}` endpoints.
 */
export function WorktreesPanel({ projectId }: WorktreesPanelProps) {
  const [worktrees, setWorktrees] = useState<WorktreeSummary[]>([]);
  const [branches, setBranches] = useState<string[]>([]);
  const [gitRepo, setGitRepo] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [selectedBranch, setSelectedBranch] = useState<string>('');
  const [busyId, setBusyId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [wt, br] = await Promise.all([
        listWorktrees(projectId),
        listProjectBranches(projectId),
      ]);
      setWorktrees((wt as WorktreeSummary[]) ?? []);
      setBranches(br.branches ?? []);
      setGitRepo(br.git_repo);
    } catch (err) {
      setError(logApiError('listWorktrees', err));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleCreate = async () => {
    if (!selectedBranch) return;
    setCreating(true);
    try {
      await createWorktree(projectId, selectedBranch);
      setSelectedBranch('');
      await refresh();
    } catch (err) {
      setError(logApiError('createWorktree', err));
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async (worktree: WorktreeSummary) => {
    if (!worktree.project_id) {
      // User-created worktrees have no managed project_id — they live
      // entirely on disk and aren't ours to remove.
      setError('User-created worktrees must be removed via `git worktree`.');
      return;
    }
    const dirty = worktree.clean === false;
    const confirmText = dirty
      ? `Worktree "${worktree.branch}" has uncommitted changes or unpushed commits.\nForce-delete anyway?`
      : `Remove worktree "${worktree.branch}"?`;
    if (!confirm(confirmText)) return;
    const wtId = worktree.project_id;
    setBusyId(wtId);
    try {
      await deleteWorktree(projectId, wtId, dirty);
      await refresh();
    } catch (err) {
      setError(logApiError('deleteWorktree', err));
    } finally {
      setBusyId(null);
    }
  };

  if (!gitRepo && !loading) {
    return (
      <div className="text-xs text-[hsl(var(--muted-foreground))] flex items-center gap-2">
        <GitBranch className="w-3.5 h-3.5" />
        Not a git repository — worktrees unavailable.
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2 mt-2">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-[hsl(var(--foreground))] flex items-center gap-1.5">
          <GitBranch className="w-3.5 h-3.5" />
          Worktrees
        </span>
        {loading && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
      </div>

      {error && (
        <div className="text-xs text-red-400 flex items-center gap-1.5">
          <AlertTriangle className="w-3.5 h-3.5" />
          {error}
        </div>
      )}

      {worktrees.length === 0 && !loading && (
        <div className="text-xs text-[hsl(var(--muted-foreground))]">
          No worktrees yet.
        </div>
      )}

      {worktrees.map((wt) => {
        const rowKey = wt.project_id ?? `unmanaged:${wt.branch}:${wt.path}`;
        const busy = busyId === wt.project_id;
        return (
          <div
            key={rowKey}
            className="flex items-center justify-between text-xs rounded border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-2 py-1.5"
          >
            <div className="flex items-center gap-2 min-w-0">
              <GitBranch className="w-3.5 h-3.5 text-[hsl(var(--muted-foreground))] shrink-0" />
              <span className="truncate font-mono">{wt.branch}</span>
              <span
                className="text-[10px] text-[hsl(var(--muted-foreground))] bg-[hsl(var(--muted))] px-1 py-0.5 rounded shrink-0"
                title={wt.managed ? 'Created by Mewbo' : 'Existing on-disk worktree'}
              >
                {wt.managed ? 'managed' : 'user'}
              </span>
              {wt.clean === false && (
                <span
                  title="Uncommitted changes or unpushed commits"
                  className="text-amber-400"
                >
                  ●
                </span>
              )}
            </div>
            {wt.managed ? (
              <Button
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={() => handleDelete(wt)}
                aria-label={`Delete worktree ${wt.branch}`}
              >
                {busy ? (
                  <Loader2 className="w-3 h-3 animate-spin" />
                ) : (
                  <Trash2 className="w-3 h-3" />
                )}
              </Button>
            ) : (
              <span
                className="text-[10px] text-[hsl(var(--muted-foreground))]"
                title="Use `git worktree remove` from the parent repo"
              >
                git CLI
              </span>
            )}
          </div>
        );
      })}

      <div className="flex items-center gap-2 mt-1">
        <select
          value={selectedBranch}
          onChange={(e) => setSelectedBranch(e.target.value)}
          disabled={branches.length === 0 || creating}
          className="flex-1 rounded border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-2 py-1 text-xs"
        >
          <option value="">— select branch —</option>
          {branches
            .filter(
              (b) => !worktrees.some((w) => w.branch === b)
            )
            .map((b) => (
              <option key={b} value={b}>
                {b}
              </option>
            ))}
        </select>
        <Button
          size="sm"
          onClick={handleCreate}
          disabled={!selectedBranch || creating}
        >
          {creating ? (
            <Loader2 className="w-3 h-3 animate-spin" />
          ) : (
            <Plus className="w-3 h-3" />
          )}
          Add
        </Button>
      </div>
    </div>
  );
}
