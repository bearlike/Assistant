/**
 * IndexingScreen — terminal-but-incomplete recovery panel (Part C).
 *
 * When the snapshot poll reports a failed / interrupted / cancelled job that
 * never completed, the screen swaps the live progress UI for a recovery panel
 * (the reached percent + the error + a "Resume indexing" button) instead of a
 * stuck progress bar. Resume POSTs the resume and re-subscribes the stream in
 * place.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { IndexingScreen } from "@/components/wiki/IndexingScreen";
import * as client from "@/components/wiki/api/client";
import type { IndexingJob } from "@/components/wiki/api/types";

vi.mock("@/components/wiki/api/client", () => ({
  // SSE stream: an async generator that yields nothing (the snapshot poll is
  // the authoritative terminal-status source for failed/interrupted jobs).
  // eslint-disable-next-line require-yield
  subscribeToIndexing: vi.fn(async function* () {
    return;
  }),
  getIndexingJob: vi.fn(),
  cancelIndexingJob: vi.fn(),
  resumeIndexingJob: vi.fn(),
}));

const getIndexingJob = vi.mocked(client.getIndexingJob);
const resumeIndexingJob = vi.mocked(client.resumeIndexingJob);

const FAILED_JOB: IndexingJob = {
  jobId: "j1",
  slug: "git.example.com/acme/widgets",
  status: "failed",
  scannedCount: 30,
  totalCount: 30,
  currentFile: null,
  phase: "pages",
  pagesSubmitted: 4,
  totalPages: 10,
  error: { code: "internal", message: "page writer crashed" },
};

function renderScreen() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const loc = memoryLocation({ path: "/wiki/indexing?jobId=j1", record: true });
  render(
    <QueryClientProvider client={qc}>
      <Router hook={loc.hook}>
        <IndexingScreen jobId="j1" slug={FAILED_JOB.slug} />
      </Router>
    </QueryClientProvider>,
  );
  return loc;
}

afterEach(cleanup);
beforeEach(() => {
  getIndexingJob.mockResolvedValue(FAILED_JOB);
  resumeIndexingJob.mockReset();
});

describe("IndexingScreen — recovery panel", () => {
  it("shows the failure + a Resume button for a terminal-incomplete job", async () => {
    renderScreen();
    expect(await screen.findByText("Indexing failed")).toBeInTheDocument();
    expect(screen.getByText("page writer crashed")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /resume indexing/i }),
    ).toBeInTheDocument();
    // The dead-end "Cancel indexing" control is gone for a stopped run.
    expect(screen.queryByText(/cancel indexing/i)).toBeNull();
  });

  it("Resume POSTs the resume for the job", async () => {
    const user = userEvent.setup();
    resumeIndexingJob.mockResolvedValue({ jobId: "j1", sessionId: "s1", status: "queued" });
    renderScreen();

    await user.click(await screen.findByRole("button", { name: /resume indexing/i }));
    await waitFor(() => expect(resumeIndexingJob).toHaveBeenCalledWith("j1"));
  });
});
