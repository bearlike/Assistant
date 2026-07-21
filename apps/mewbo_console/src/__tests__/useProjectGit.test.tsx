/**
 * useProjectGit — worktree write mutations must invalidate the `['projects']`
 * root, not just their own `['project-git', <id>]` cache.
 *
 * A worktree IS a managed project row: `GET /api/projects` returns it as a
 * child entry with `is_worktree` set (backend.py `Projects.get`), and both
 * `useProjects()` and `useVirtualProjects()` key off `['projects']`. Before
 * this fix, `invalidate()` only touched `['project-git', projectKey]`, so a
 * worktree created here left the composer's ConfigMenu picker and the
 * Workspace pane's own project list stale for up to the default 60s
 * `staleTime` — the worktree's own card, missing from the very list rendered
 * two divs above it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("../api/client", () => ({
  listProjectBranches: vi.fn(),
  listWorktrees: vi.fn(),
  createWorktree: vi.fn(),
  deleteWorktree: vi.fn(),
}));

import * as client from "../api/client";
import { useProjectGit } from "../hooks/useProjectGit";
import type { WorktreeSummary } from "../types";

const listProjectBranches = vi.mocked(client.listProjectBranches);
const listWorktrees = vi.mocked(client.listWorktrees);
const createWorktree = vi.mocked(client.createWorktree);
const deleteWorktree = vi.mocked(client.deleteWorktree);

function makeQc() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}
function wrapperFor(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

function invalidatedKeys(spy: { mock: { calls: unknown[][] } }): unknown[][] {
  return spy.mock.calls.map((call) => (call[0] as { queryKey: unknown[] }).queryKey);
}

const newWorktree: WorktreeSummary = {
  project_id: "wt-1",
  name: "Alpha (mewbo/feature)",
  branch: "mewbo/feature",
  path: "/srv/alpha/.mewbo/worktrees/feature",
  managed: true,
  is_worktree: true,
  clean: true,
};

beforeEach(() => {
  vi.clearAllMocks();
  listProjectBranches.mockResolvedValue({
    git_repo: true,
    branches: ["main"],
    current_branch: "main",
    branches_in_use: [],
  });
  listWorktrees.mockResolvedValue([]);
});
afterEach(cleanup);

describe("useProjectGit worktree mutations", () => {
  it("invalidates the ['projects'] root (in addition to ['project-git', id]) on worktree create", async () => {
    createWorktree.mockResolvedValue(newWorktree);
    const qc = makeQc();
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");

    const { result } = renderHook(() => useProjectGit("proj-1"), { wrapper: wrapperFor(qc) });
    await waitFor(() => expect(result.current.gitRepo).toBe(true));
    invalidateSpy.mockClear(); // drop invalidations from the initial mount, if any

    await act(async () => {
      await result.current.createWorktreeFor({ branch: "mewbo/feature", base: "main" });
    });

    expect(createWorktree).toHaveBeenCalledWith("proj-1", { branch: "mewbo/feature", base: "main" });
    const keys = invalidatedKeys(invalidateSpy);
    expect(keys).toContainEqual(["projects"]);
    expect(keys).toContainEqual(["project-git", "proj-1"]);
  });

  it("invalidates the ['projects'] root on worktree delete too — deleting leaves a dead card behind otherwise", async () => {
    deleteWorktree.mockResolvedValue(undefined);
    const qc = makeQc();
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");

    const { result } = renderHook(() => useProjectGit("proj-1"), { wrapper: wrapperFor(qc) });
    await waitFor(() => expect(result.current.gitRepo).toBe(true));
    invalidateSpy.mockClear();

    await act(async () => {
      await result.current.deleteWorktreeFor("wt-1");
    });

    expect(deleteWorktree).toHaveBeenCalledWith("proj-1", "wt-1", false);
    const keys = invalidatedKeys(invalidateSpy);
    expect(keys).toContainEqual(["projects"]);
  });

  it("does NOT touch ['projects'] on a plain read refresh (only writes do)", async () => {
    const qc = makeQc();
    const { result } = renderHook(() => useProjectGit("proj-1"), { wrapper: wrapperFor(qc) });
    await waitFor(() => expect(result.current.gitRepo).toBe(true));

    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
    act(() => {
      result.current.refresh();
    });

    const keys = invalidatedKeys(invalidateSpy);
    expect(keys).toContainEqual(["project-git", "proj-1"]);
    expect(keys).not.toContainEqual(["projects"]);
  });
});
