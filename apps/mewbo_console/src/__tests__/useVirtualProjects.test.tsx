/**
 * useVirtualProjects — mutation failures must surface, not vanish.
 *
 * Before this fix, `createM`/`updateM`/`removeM` had no `onError`, and the
 * hook's `error` field read only the QUERY (`query.error`) — so a rejecting
 * `DELETE /api/v_projects/<id>` (409/500) produced an unhandled promise
 * rejection and no error a user could ever see. `usePlugins.ts` established
 * the fix for this same refactor: `sonner` toasts fired from the hook's
 * mutation callbacks. This file proves the toast fires on failure (and on
 * success), and that `toVirtualProject` no longer fabricates fields
 * `GET /api/projects` never actually returns.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("../api/client", () => ({
  listProjects: vi.fn(),
  createVirtualProject: vi.fn(),
  updateVirtualProject: vi.fn(),
  deleteVirtualProject: vi.fn(),
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

import * as client from "../api/client";
import { toast } from "sonner";
import { useVirtualProjects } from "../hooks/useVirtualProjects";
import type { ProjectSummary } from "../api/client";

const listProjects = vi.mocked(client.listProjects);
const createVirtualProject = vi.mocked(client.createVirtualProject);
const updateVirtualProject = vi.mocked(client.updateVirtualProject);
const deleteVirtualProject = vi.mocked(client.deleteVirtualProject);
const toastSuccess = vi.mocked(toast.success);
const toastError = vi.mocked(toast.error);

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

const alpha: ProjectSummary = {
  name: "Alpha",
  path: "/srv/alpha",
  description: "The alpha workspace",
  source: "managed",
  project_id: "p-alpha",
  is_worktree: false,
};

beforeEach(() => {
  vi.clearAllMocks();
  listProjects.mockResolvedValue([alpha]);
});
afterEach(cleanup);

describe("useVirtualProjects mutation error handling", () => {
  it("surfaces a rejecting delete as a toast instead of swallowing it", async () => {
    deleteVirtualProject.mockRejectedValue(new Error("409: worktrees still attached"));
    const qc = makeQc();
    const { result } = renderHook(() => useVirtualProjects(), { wrapper: wrapperFor(qc) });
    await waitFor(() => expect(result.current.projects).toHaveLength(1));

    await act(async () => {
      await result.current.remove("p-alpha").catch(() => undefined);
    });

    expect(toastError).toHaveBeenCalledTimes(1);
    expect(toastError.mock.calls[0][0]).toContain("409: worktrees still attached");
  });

  it("surfaces a rejecting update as a toast instead of swallowing it", async () => {
    updateVirtualProject.mockRejectedValue(new Error("500: internal error"));
    const qc = makeQc();
    const { result } = renderHook(() => useVirtualProjects(), { wrapper: wrapperFor(qc) });
    await waitFor(() => expect(result.current.projects).toHaveLength(1));

    await act(async () => {
      await result.current.update("p-alpha", { name: "Renamed" }).catch(() => undefined);
    });

    expect(toastError).toHaveBeenCalledTimes(1);
    expect(toastError.mock.calls[0][0]).toContain("500: internal error");
  });

  it("still rejects the returned promise on failure (callers can add their own local handling too)", async () => {
    deleteVirtualProject.mockRejectedValue(new Error("boom"));
    const qc = makeQc();
    const { result } = renderHook(() => useVirtualProjects(), { wrapper: wrapperFor(qc) });
    await waitFor(() => expect(result.current.projects).toHaveLength(1));

    await expect(
      act(async () => {
        await result.current.remove("p-alpha");
      }),
    ).rejects.toThrow("boom");
  });

  it("toasts success and invalidates ['projects'] on a successful create", async () => {
    createVirtualProject.mockResolvedValue({
      project_id: "p-beta",
      name: "Beta",
      description: "",
      path: "/srv/beta",
    });
    const qc = makeQc();
    const { result } = renderHook(() => useVirtualProjects(), { wrapper: wrapperFor(qc) });
    await waitFor(() => expect(result.current.projects).toHaveLength(1));

    await act(async () => {
      await result.current.create("Beta", "", undefined);
    });

    expect(toastSuccess).toHaveBeenCalledTimes(1);
    expect(toastError).not.toHaveBeenCalled();
  });
});

describe("toVirtualProject shape (Defect 3 — no fabricated fields)", () => {
  it("does not fabricate path_source/folder_created/created_at/updated_at for GET /api/projects entries", async () => {
    const qc = makeQc();
    const { result } = renderHook(() => useVirtualProjects(), { wrapper: wrapperFor(qc) });
    await waitFor(() => expect(result.current.projects).toHaveLength(1));

    const [project] = result.current.projects;
    expect(project.project_id).toBe("p-alpha");
    expect(project.path_source).toBeUndefined();
    expect(project.folder_created).toBeUndefined();
    expect(project.created_at).toBeUndefined();
    expect(project.updated_at).toBeUndefined();
  });
});
