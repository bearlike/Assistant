import { useCallback, useEffect, useMemo, useState } from 'react';
import { GitBranch, Loader2, Plus, RefreshCw, Trash2, AlertTriangle } from 'lucide-react';
import {
  createWorktree,
  deleteWorktree,
  listProjectBranches,
  listWorktrees,
} from '../api/client';
import { WorktreeSummary } from '../types';
import { Button } from './ui/button';
import { logApiError } from '../utils/errors';

/**
 * Mirrors the Mewbo branch convention used by the API. Keeping this
 * literal in the panel as well avoids a cross-file dep just for a 6-char
 * prefix; the only place it's authoritative is the Python worktree module.
 */
const MEWBO_BRANCH_PREFIX = 'mewbo/';

function slugifyBranchClient(branch: string): string {
  return branch
    .trim()
    .replace(/[^A-Za-z0-9._-]+/g, '-')
    .replace(/^[-._]+|[-._]+$/g, '')
    || 'branch';
}

function shortId(): string {
  const c = (globalThis as unknown as { crypto?: Crypto }).crypto;
  if (c?.getRandomValues) {
    const bytes = new Uint8Array(3);
    c.getRandomValues(bytes);
    return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
  }
  return Math.floor(Math.random() * 0xffffff).toString(16).padStart(6, '0');
}

function defaultMewboBranchName(base: string): string {
  return `${MEWBO_BRANCH_PREFIX}${slugifyBranchClient(base)}-${shortId()}`;
}

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
  const [branchesInUse, setBranchesInUse] = useState<string[]>([]);
  const [currentBranch, setCurrentBranch] = useState<string | null>(null);
  const [gitRepo, setGitRepo] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

  // Form state — explicit two-mode UX matching the composer's ConfigMenu.
  const [mode, setMode] = useState<'new' | 'existing'>('new');
  const [base, setBase] = useState<string>('');
  const [newBranch, setNewBranch] = useState<string>('');

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
      setBranchesInUse(br.branches_in_use ?? []);
      setCurrentBranch(br.current_branch ?? null);
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

  const canSubmit = (() => {
    if (creating) return false;
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
  })();

  const handleCreate = async () => {
    if (!canSubmit) return;
    setCreating(true);
    try {
      if (mode === 'new') {
        await createWorktree(projectId, { branch: newBranch.trim(), base });
      } else {
        await createWorktree(projectId, { branch: base });
      }
      // Reset just the new-branch field so the user can stamp out another
      // session-branch quickly without re-picking the base.
      setNewBranch(defaultMewboBranchName(base));
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

      {/* Worktree creation form. Two explicit modes — defaults to "new
          branch from base" since that's the recommended workflow that
          guarantees session isolation. */}
      <div className="flex flex-col gap-2 mt-1 rounded border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-2 py-2">
        <div className="flex items-center gap-1 text-[10px] text-[hsl(var(--muted-foreground))]">
          <button
            type="button"
            onClick={() => setMode('new')}
            className={`px-2 py-0.5 rounded ${
              mode === 'new'
                ? 'bg-[hsl(var(--accent))] text-[hsl(var(--foreground))] font-medium'
                : 'hover:bg-[hsl(var(--accent))]'
            }`}
          >
            New branch from base
          </button>
          <button
            type="button"
            onClick={() => setMode('existing')}
            className={`px-2 py-0.5 rounded ${
              mode === 'existing'
                ? 'bg-[hsl(var(--accent))] text-[hsl(var(--foreground))] font-medium'
                : 'hover:bg-[hsl(var(--accent))]'
            }`}
          >
            Use existing branch
          </button>
        </div>

        <label className="flex flex-col gap-0.5">
          <span className="text-[10px] uppercase tracking-wide text-[hsl(var(--muted-foreground))]">
            {mode === 'new' ? 'Base branch' : 'Branch'}
          </span>
          <select
            value={base}
            onChange={(e) => setBase(e.target.value)}
            disabled={branches.length === 0 || creating}
            className="rounded border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-2 py-1 text-xs font-mono"
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
            <span className="text-[10px] uppercase tracking-wide text-[hsl(var(--muted-foreground))]">
              New branch name
            </span>
            <div className="flex items-center gap-1">
              <input
                type="text"
                value={newBranch}
                onChange={(e) => setNewBranch(e.target.value)}
                placeholder="mewbo/feature-x-ab12cd"
                disabled={creating}
                className="flex-1 rounded border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-2 py-1 text-xs font-mono"
              />
              <button
                type="button"
                onClick={() => setNewBranch(defaultMewboBranchName(base))}
                disabled={creating || !base}
                title="Generate a fresh mewbo/<base>-<id> name"
                className="shrink-0 px-1.5 py-1 rounded hover:bg-[hsl(var(--accent))] text-[hsl(var(--muted-foreground))]"
              >
                <RefreshCw className="w-3 h-3" />
              </button>
            </div>
            {newBranch && branches.includes(newBranch.trim()) && (
              <span className="text-[10px] text-amber-400">
                Branch already exists — pick a different name.
              </span>
            )}
          </label>
        )}

        <p className="text-[10px] text-[hsl(var(--muted-foreground))] leading-snug">
          {mode === 'new' && base && newBranch.trim()
            ? `Will run: git worktree add -b ${newBranch.trim()} <path> ${base}`
            : mode === 'existing' && base
              ? `Will run: git worktree add <path> ${base}`
              : 'Pick a base branch to continue.'}
        </p>

        <Button size="sm" onClick={handleCreate} disabled={!canSubmit}>
          {creating ? (
            <Loader2 className="w-3 h-3 animate-spin" />
          ) : (
            <Plus className="w-3 h-3" />
          )}
          Create worktree
        </Button>
      </div>
    </div>
  );
}
