/**
 * IndexingScreen — the structural contract of the loader.
 *
 * The rebuild exists because the old screen appended each region beneath the
 * last and let the page grow: the panel outgrew the viewport, the log rendered
 * every line it had ever received (5,468 rows and 22,205 DOM nodes measured on
 * a live index), and the whole declared plan sat flat above it.
 *
 * These tests pin that shape, not its pixels. jsdom performs no layout, so the
 * assertions are about what is RENDERED and where the scroll containers are —
 * which is exactly what regressed.
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { IndexingScreen } from "@/components/wiki/IndexingScreen";
import { RENDER_LIMIT } from "@/components/wiki/indexing/activityModel";
import * as client from "@/components/wiki/api/client";
import type { IndexingEvent, IndexingJob, StepRecord } from "@/components/wiki/api/types";

vi.mock("@/components/wiki/api/client", () => ({
  subscribeToIndexing: vi.fn(),
  getIndexingJob: vi.fn(),
  cancelIndexingJob: vi.fn(),
  resumeIndexingJob: vi.fn(),
}));

const subscribeToIndexing = vi.mocked(client.subscribeToIndexing);
const getIndexingJob = vi.mocked(client.getIndexingJob);

const SLUG = "git.example.com/acme/widgets";

function step(
  key: string,
  group: string,
  state: StepRecord["state"],
  extra: Partial<StepRecord> = {},
): StepRecord {
  return { key, group, label: `Step ${key}`, weight: 1, state, ...extra };
}

const LEDGER_STEPS: StepRecord[] = [
  step("clone.git", "clone", "done"),
  step("clone.count", "clone", "done"),
  step("scan.discover", "scan", "done"),
  step("graph.parse", "graph", "done"),
  step("graph.embed", "graph", "running", {
    current: 900,
    total: 13_965,
    unit: "nodes",
    startedAt: "2020-01-01T00:00:00Z",
  }),
  step("graph.persist", "graph", "pending"),
];

const RUNNING_JOB: IndexingJob = {
  jobId: "j1",
  slug: SLUG,
  status: "scanning",
  isActive: true,
  scannedCount: 235,
  totalCount: 235,
  currentFile: null,
  phase: "graph",
  progress: { version: 1, steps: LEDGER_STEPS },
};

/** A stream that yields `events` once and then stays open, as a live job's
 *  does — a generator that RETURNS would let the hook's retry loop reopen it
 *  and replay the same events forever. */
function streamOf(events: IndexingEvent[]) {
  const never = new Promise<never>(() => {
    /* deliberately never settles */
  });
  return async function* () {
    for (const event of events) yield event;
    await never;
  };
}

function renderScreen() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const loc = memoryLocation({ path: "/wiki/indexing?jobId=j1", record: true });
  return render(
    <QueryClientProvider client={qc}>
      <Router hook={loc.hook}>
        <IndexingScreen jobId="j1" slug={SLUG} />
      </Router>
    </QueryClientProvider>,
  );
}

afterEach(cleanup);
beforeEach(() => {
  getIndexingJob.mockResolvedValue(RUNNING_JOB);
  subscribeToIndexing.mockImplementation(streamOf([]) as never);
});

describe("IndexingScreen — structure", () => {
  it("scrolls its panes, never the screen root", async () => {
    const { container } = renderScreen();
    await screen.findByRole("region", { name: /indexing progress/i });

    // The regression: the screen's own root carried `overflow-y-auto`, so the
    // page grew a scrollbar as the backend declared more work.
    const root = container.firstElementChild as HTMLElement;
    expect(root.className).not.toMatch(/overflow-y-auto/);
    expect(root.className).toMatch(/overflow-hidden/);

    // Exactly two scroll containers, one per pane.
    const panes = container.querySelectorAll('[data-scroll="pane"]');
    expect(panes).toHaveLength(2);
    expect(container.querySelector('[data-region="plan"]')).not.toBeNull();
    expect(container.querySelector('[data-region="activity"]')).not.toBeNull();
  });

  it("renders a bounded tail of the log and says the window is a tail", async () => {
    const flood: IndexingEvent[] = Array.from({ length: 1_200 }, (_, i) => ({
      type: "log",
      level: "info",
      text: `line ${i}`,
      step: "graph.embed",
    }));
    subscribeToIndexing.mockImplementation(streamOf(flood) as never);

    renderScreen();
    const activity = (await screen.findByRole("log", { name: /indexer activity/i })) as HTMLElement;

    // The oldest lines are dropped from the DOM; the newest are kept.
    expect(await within(activity).findByText("line 1199")).toBeInTheDocument();
    expect(within(activity).queryByText("line 0")).toBeNull();
    expect(within(activity).getAllByRole("listitem").length).toBeLessThanOrEqual(RENDER_LIMIT);
    // A silent trim is what makes two visits look like two different runs.
    expect(screen.getByText(/last 250 of 1,200 lines/i)).toBeInTheDocument();
  });

  it("groups log lines under the declared step that produced them", async () => {
    subscribeToIndexing.mockImplementation(
      streamOf([
        { type: "log", level: "info", text: "parsed a.py", step: "graph.parse" },
        { type: "log", level: "info", text: "embedded 1", step: "graph.embed" },
      ]) as never,
    );

    renderScreen();
    const activity = (await screen.findByRole("log", { name: /indexer activity/i })) as HTMLElement;

    // The backend stamps the open step onto every line; dropping it left
    // thousands of undifferentiated rows.
    expect(await within(activity).findByRole("heading", { name: "Step graph.parse" })).toBeInTheDocument();
    expect(within(activity).getByRole("heading", { name: "Step graph.embed" })).toBeInTheDocument();
  });

  it("discloses the running phase's steps and keeps settled phases collapsed", async () => {
    const { container } = renderScreen();
    const plan = (await screen.findByRole("region", { name: /indexing progress/i })).querySelector(
      '[data-region="plan"]',
    ) as HTMLElement;

    // The active phase is open: its steps are readable without a click.
    expect(await within(plan).findByText("Step graph.embed")).toBeInTheDocument();
    // A finished phase is one row until asked for — the flat dump is gone.
    expect(within(plan).queryByText("Step clone.git")).toBeNull();

    await userEvent.click(within(plan).getByRole("button", { name: /clone/i }));
    expect(within(plan).getByText("Step clone.git")).toBeInTheDocument();

    // Every phase of the run is named, declared or not, so the rail is never
    // a partial picture of the pipeline.
    for (const label of ["Clone", "Scan", "Graph", "Entities", "Plan", "Pages", "Finish"]) {
      expect(within(container).getAllByText(label).length).toBeGreaterThan(0);
    }
  });

  it("renders the ledger of whichever transport is ahead, not a fixed one", async () => {
    // The SSE `progress` event fires on a write cadence; the snapshot polls
    // twice a second. Preferring the stream unconditionally rendered a stale
    // outline beside a fresh percentage — seen live as a header still naming
    // an embedding step while the activity log was already minting entities.
    const staleStreamLedger: StepRecord[] = [
      step("clone.git", "clone", "done"),
      step("graph.embed", "graph", "running"),
    ];
    subscribeToIndexing.mockImplementation(
      streamOf([
        { type: "queued", jobId: "j1", slug: SLUG, totalCount: 0 },
        {
          type: "progress",
          version: 1,
          fraction: 0.3,
          etaSeconds: null,
          elapsedSeconds: null,
          activeKey: "graph.embed",
          groups: [],
          steps: staleStreamLedger,
        },
      ]) as never,
    );
    // The snapshot is further along: graph is finished and enrich is open.
    getIndexingJob.mockResolvedValue({
      ...RUNNING_JOB,
      phase: "enrich",
      progress: {
        version: 1,
        steps: [
          step("clone.git", "clone", "done"),
          step("graph.embed", "graph", "done"),
          step("enrich.mint_entities", "enrich", "running"),
        ],
      },
    });

    renderScreen();

    expect(
      await screen.findByRole("heading", { level: 1, name: "Step enrich.mint_entities" }),
    ).toBeInTheDocument();
  });

  it("headlines the open step and its counter rather than the coarse phase", async () => {
    const { container } = renderScreen();

    expect(
      await screen.findByRole("heading", { level: 1, name: "Step graph.embed" }),
    ).toBeInTheDocument();
    // Scoped to the header: the same counter also renders on the step's own
    // row in the plan, and both reading it off the SAME step is the point.
    const header = container.querySelector("header") as HTMLElement;
    expect(within(header).getByText(/900 of 13,965 nodes/)).toBeInTheDocument();
  });
});
