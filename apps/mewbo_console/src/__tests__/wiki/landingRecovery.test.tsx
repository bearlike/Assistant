/**
 * LandingScreen — "Incomplete indexes" recovery section (Part C).
 *
 * Failed / interrupted / cancelled-but-incomplete jobs with reusable work are
 * surfaced in a collapsible section. Each row carries a Resume button that
 * POSTs the resume and routes to the indexing screen for that job. An empty
 * recoverable list hides the section entirely.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LandingScreen } from "@/components/wiki/LandingScreen";
import * as client from "@/components/wiki/api/client";
import type { RecoverableJob } from "@/components/wiki/api/types";

// Mock the whole wiki client — the hooks call these directly. Only the
// functions the landing screen touches need real-ish returns.
vi.mock("@/components/wiki/api/client", () => ({
  listProjects: vi.fn().mockResolvedValue([]),
  listActiveJobs: vi.fn().mockResolvedValue([]),
  listRecoverableJobs: vi.fn().mockResolvedValue([]),
  deleteProject: vi.fn(),
  resumeIndexingJob: vi.fn(),
}));

const listRecoverableJobs = vi.mocked(client.listRecoverableJobs);
const resumeIndexingJob = vi.mocked(client.resumeIndexingJob);

const JOB: RecoverableJob = {
  jobId: "j1",
  slug: "git.example.com/acme/widgets",
  status: "failed",
  phase: "pages",
  error: { code: "internal", message: "page writer crashed" },
  pagesSubmitted: 4,
  totalPages: 10,
  updatedAt: "2026-06-07T00:00:00Z",
  recoverable: { skip: ["core"], pagesDone: 4, pagesRemaining: 6, nodeCount: 120 },
};

function renderLanding() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const loc = memoryLocation({ path: "/wiki", record: true });
  render(
    <QueryClientProvider client={qc}>
      <Router hook={loc.hook}>
        <LandingScreen />
      </Router>
    </QueryClientProvider>,
  );
  return loc;
}

afterEach(cleanup);
beforeEach(() => {
  listRecoverableJobs.mockResolvedValue([]);
  resumeIndexingJob.mockReset();
});

describe("LandingScreen — Incomplete indexes", () => {
  it("hides the section when there are no recoverable jobs", async () => {
    renderLanding();
    // Let the query settle, then confirm the header never appears.
    await waitFor(() => expect(listRecoverableJobs).toHaveBeenCalled());
    expect(screen.queryByText("Incomplete indexes")).toBeNull();
  });

  it("lists each recoverable job once expanded", async () => {
    const user = userEvent.setup();
    listRecoverableJobs.mockResolvedValue([JOB]);
    renderLanding();

    const header = await screen.findByText("Incomplete indexes");
    await user.click(header);

    expect(screen.getByText(/stopped at pages/i)).toBeInTheDocument();
    expect(screen.getByText("4/10 pages")).toBeInTheDocument();
    expect(screen.getByText("page writer crashed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /resume/i })).toBeInTheDocument();
  });

  it("Resume POSTs and routes to the indexing screen for the job", async () => {
    const user = userEvent.setup();
    listRecoverableJobs.mockResolvedValue([JOB]);
    resumeIndexingJob.mockResolvedValue({ jobId: "j1", sessionId: "s1", status: "queued" });
    const loc = renderLanding();

    await user.click(await screen.findByText("Incomplete indexes"));
    await user.click(screen.getByRole("button", { name: /resume/i }));

    await waitFor(() => expect(resumeIndexingJob).toHaveBeenCalledWith("j1"));
    await waitFor(() => expect(loc.history.at(-1)).toMatch(/\/wiki\/indexing\?jobId=j1/));
  });
});
