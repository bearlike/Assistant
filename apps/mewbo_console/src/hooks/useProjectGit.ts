import { useCallback } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createWorktree,
  deleteWorktree,
  listProjectBranches,
  listWorktrees,
} from "../api/client";
import { ProjectBranches, WorktreeSummary } from "../types";
import { logApiError } from "../utils/errors";

/**
 * Strip the ``managed:`` prefix that the composer's project picker uses
 * when referring to managed projects. The backend's worktree routes accept
 * either the bare name (config) or the bare UUID (managed); the prefix is a
 * UI-only disambiguator.
 */
function projectKeyForApi(activeProject: string | null | undefined): string | null {
  if (!activeProject) return null;
  return activeProject.startsWith("managed:") ? activeProject.slice("managed:".length) : activeProject;
}

export type ProjectGitState = {
  /** API key used for the current project (resolved from ``activeProject``). */
  projectKey: string | null;
  gitRepo: boolean;
  branches: string[];
  currentBranch: string | null;
  worktrees: WorktreeSummary[];
  loading: boolean;
  error: string | null;
  reason?: string;
  refresh: () => void;
  createWorktreeFor: (branch: string) => Promise<WorktreeSummary>;
  deleteWorktreeFor: (worktreeId: string, force?: boolean) => Promise<void>;
  /** ``true`` while a write (create / delete) is in-flight. */
  mutating: boolean;
};

/**
 * Atomic git read for a project: branches + current HEAD + worktree
 * inventory, with mutation helpers that invalidate the same query keys.
 *
 * The hook stays idle when ``activeProject`` is null (Temporary directory),
 * so the composer can mount this unconditionally.
 */
export function useProjectGit(activeProject: string | null | undefined): ProjectGitState {
  const qc = useQueryClient();
  const projectKey = projectKeyForApi(activeProject);

  const branchesQuery = useQuery<ProjectBranches>({
    queryKey: ["project-git", projectKey, "branches"],
    queryFn: () => listProjectBranches(projectKey as string),
    enabled: !!projectKey,
    staleTime: 30_000,
  });

  const worktreesQuery = useQuery<WorktreeSummary[]>({
    queryKey: ["project-git", projectKey, "worktrees"],
    // Skip the worktree call if we already know the project isn't a git
    // repo — it's just an empty list and we save a round-trip per project
    // switch in the picker.
    queryFn: () => listWorktrees(projectKey as string),
    enabled: !!projectKey && (branchesQuery.data?.git_repo ?? true),
    staleTime: 30_000,
  });

  const invalidate = useCallback(() => {
    void qc.invalidateQueries({ queryKey: ["project-git", projectKey] });
  }, [qc, projectKey]);

  const createMutation = useMutation({
    mutationFn: ({ branch }: { branch: string }) =>
      createWorktree(projectKey as string, branch),
    onSuccess: () => invalidate(),
  });

  const deleteMutation = useMutation({
    mutationFn: ({ worktreeId, force }: { worktreeId: string; force?: boolean }) =>
      deleteWorktree(projectKey as string, worktreeId, force ?? false),
    onSuccess: () => invalidate(),
  });

  const createWorktreeFor = useCallback(
    async (branch: string) => {
      if (!projectKey) {
        throw new Error("No project selected");
      }
      return createMutation.mutateAsync({ branch });
    },
    [projectKey, createMutation],
  );

  const deleteWorktreeFor = useCallback(
    async (worktreeId: string, force?: boolean) => {
      if (!projectKey) {
        throw new Error("No project selected");
      }
      await deleteMutation.mutateAsync({ worktreeId, force });
    },
    [projectKey, deleteMutation],
  );

  const error =
    branchesQuery.error
      ? logApiError("listProjectBranches", branchesQuery.error)
      : worktreesQuery.error
        ? logApiError("listWorktrees", worktreesQuery.error)
        : null;

  return {
    projectKey,
    gitRepo: branchesQuery.data?.git_repo ?? false,
    branches: branchesQuery.data?.branches ?? [],
    currentBranch: branchesQuery.data?.current_branch ?? null,
    worktrees: worktreesQuery.data ?? [],
    loading: branchesQuery.isPending || worktreesQuery.isPending,
    error,
    reason: branchesQuery.data?.reason,
    refresh: invalidate,
    createWorktreeFor,
    deleteWorktreeFor,
    mutating: createMutation.isPending || deleteMutation.isPending,
  };
}
