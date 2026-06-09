/**
 * Recovery-UI code-review fixes (Gitea #54).
 *
 * Three regressions, asserted at the hook seam so each test fails if the fix
 * is reverted:
 *   1. ``useIndexingJob`` must STOP polling on a terminal ``interrupted``
 *      status (it previously polled at 500ms forever).
 *   2. ``useRecoverSession`` must thread ``slug`` into the indexing-screen nav
 *      on a wiki-dispatch ``/recover`` response (it previously dropped it).
 *   3. A failed recover / resume POST must surface a sonner toast (both
 *      mutations previously swallowed the error silently).
 */
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useIndexingJob, useResumeIndexing } from "@/components/wiki/api/hooks";
import { useRecoverSession } from "@/hooks/useRecoverSession";
import type { IndexingJob } from "@/components/wiki/api/types";

vi.mock("sonner", () => ({ toast: { error: vi.fn() } }));
import { toast } from "sonner";
const toastError = vi.mocked(toast.error);

vi.mock("@/components/wiki/api/client", () => ({
  getIndexingJob: vi.fn(),
  resumeIndexingJob: vi.fn(),
}));
import * as wikiClient from "@/components/wiki/api/client";
const getIndexingJob = vi.mocked(wikiClient.getIndexingJob);
const resumeIndexingJob = vi.mocked(wikiClient.resumeIndexingJob);

vi.mock("@/api/client", () => ({ recoverSession: vi.fn() }));
import * as apiClient from "@/api/client";
const recoverSession = vi.mocked(apiClient.recoverSession);

function makeJob(status: IndexingJob["status"]): IndexingJob {
  return {
    jobId: "j1",
    slug: "git.example.com/acme/widgets",
    status,
    scannedCount: 30,
    totalCount: 30,
    currentFile: null,
    phase: "pages",
    pagesSubmitted: 4,
    totalPages: 10,
  };
}

/** Wraps hooks in a fresh QueryClient + a recording wouter Router. */
function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const loc = memoryLocation({ path: "/", record: true });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>
      <Router hook={loc.hook}>{children}</Router>
    </QueryClientProvider>
  );
  return { wrapper, loc };
}

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
});

describe("useIndexingJob — terminal-status poll halt", () => {
  it("STOPS polling once the snapshot reports 'interrupted'", async () => {
    getIndexingJob.mockResolvedValue(makeJob("interrupted"));
    const { wrapper } = makeWrapper();
    renderHook(() => useIndexingJob("j1"), { wrapper });

    // First fetch resolves the terminal snapshot.
    await waitFor(() => expect(getIndexingJob).toHaveBeenCalledTimes(1));
    // Well past two 500ms intervals — a still-polling hook would re-fetch.
    await new Promise((r) => setTimeout(r, 1300));
    expect(getIndexingJob).toHaveBeenCalledTimes(1);
  });

  it("KEEPS polling on a non-terminal status (regression guard)", async () => {
    getIndexingJob.mockResolvedValue(makeJob("scanning"));
    const { wrapper } = makeWrapper();
    renderHook(() => useIndexingJob("j1"), { wrapper });

    // The 500ms refetchInterval should fire at least once more.
    await waitFor(() => expect(getIndexingJob.mock.calls.length).toBeGreaterThan(1), {
      timeout: 2000,
    });
  });
});

describe("useRecoverSession — wiki-dispatch nav threads slug", () => {
  it("navigates to the indexing screen WITH the slug query param", async () => {
    recoverSession.mockResolvedValue({
      session_id: "s1",
      action: "continue",
      accepted: true,
      job_id: "j9",
      slug: "git.example.com/acme/widgets",
      status: "queued",
    });
    const { wrapper, loc } = makeWrapper();
    const { result } = renderHook(() => useRecoverSession(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ sessionId: "s1", action: "continue" });
    });

    await waitFor(() => {
      const href = loc.history.at(-1) ?? "";
      expect(href).toMatch(/\/wiki\/indexing\?/);
      expect(href).toMatch(/jobId=j9/);
      expect(href).toContain("slug=git.example.com%2Facme%2Fwidgets");
    });
  });

  it("surfaces a toast when the recover POST fails", async () => {
    recoverSession.mockRejectedValue(new Error("session already running"));
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useRecoverSession(), { wrapper });

    await act(async () => {
      await result.current
        .mutateAsync({ sessionId: "s1", action: "continue" })
        .catch(() => undefined);
    });

    await waitFor(() => expect(toastError).toHaveBeenCalledTimes(1));
    expect(toastError.mock.calls[0][0]).toMatch(/session already running/);
  });
});

describe("useResumeIndexing — failed resume surfaces a toast", () => {
  it("toasts the error when the resume POST is rejected", async () => {
    resumeIndexingJob.mockRejectedValue(new Error("job is not resumable"));
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useResumeIndexing(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync("j1").catch(() => undefined);
    });

    await waitFor(() => expect(toastError).toHaveBeenCalledTimes(1));
    expect(toastError.mock.calls[0][0]).toMatch(/job is not resumable/);
  });
});
