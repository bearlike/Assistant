/**
 * Inertness + replay-vs-rerun tests for the Agentic Search surface (#80).
 *
 * The landing page MUST be inert: mounting / revisiting `/search` never issues a
 * `POST /runs`. Past-query suggestions REPLAY a stored run (GET snapshot), and
 * re-running is a separate explicit affordance. These drive the real
 * `AgenticSearchView` with the API client mocked at the module seam so we can
 * assert exactly which network verbs fire.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"

afterEach(cleanup)

import AgenticSearchView from "../components/agentic_search/AgenticSearchView"
import * as api from "../api/agenticSearch"
import type { RunRecord, Workspace } from "../types/agenticSearch"

vi.mock("../api/agenticSearch", async (orig) => {
  const actual = await orig<typeof import("../api/agenticSearch")>()
  return {
    ...actual,
    listSources: vi.fn(),
    listWorkspaces: vi.fn(),
    listWorkspaceRuns: vi.fn(),
    startRun: vi.fn(),
    getRun: vi.fn(),
    getWorkspaceGraph: vi.fn(),
    getWorkspaceGraphSummary: vi.fn(),
    // streamRun must yield NOTHING so the SSE reducer stays idle (no fetch).
    streamRun: vi.fn(async function* () {
      /* no events */
    }),
  }
})

const workspace: Workspace = {
  id: "w1",
  name: "Eng",
  desc: "Engineering docs",
  sources: ["github"],
  instructions: "",
  created: "May 2026",
  created_at: "2026-05-01T00:00:00Z",
  past_queries: [
    {
      q: "where is auth",
      when: "yesterday",
      results: 3,
      ran_at: "2026-05-02T00:00:00Z",
      run_id: "run-123",
      status: "completed",
    },
  ],
}

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <AgenticSearchView />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.mocked(api.listSources).mockResolvedValue([
    { id: "github", name: "GitHub", color: "#fff", bg: "#000", glyph: "G", desc: "Code" },
  ])
  vi.mocked(api.listWorkspaces).mockResolvedValue([workspace])
  vi.mocked(api.listWorkspaceRuns).mockResolvedValue([])
  vi.mocked(api.startRun).mockResolvedValue({
    run: {
      run_id: "new-run",
      query: "x",
      workspace_id: "w1",
      status: "completed",
      total_ms: 1,
      answer: { tldr: "", bullets: [], confidence: 0, sources_count: 0 },
      results: [],
      trace: [],
      related_questions: [],
      related_people: [],
    },
    run_id: "new-run",
    session_id: "s",
    status: "completed",
  })
  const record: RunRecord = {
    run_id: "run-123",
    session_id: "s",
    workspace_id: "w1",
    query: "where is auth",
    status: "completed",
    created_at: "2026-05-02T00:00:00Z",
    total_ms: 4200,
    source_ids: ["github"],
    allowed_tools: [],
    output_contract_version: "1.0",
    payload: {
      run_id: "run-123",
      query: "where is auth",
      workspace_id: "w1",
      status: "completed",
      total_ms: 4200,
      answer: { tldr: "found in auth.py", bullets: [], confidence: 0.8, sources_count: 1 },
      results: [],
      trace: [],
      related_questions: [],
      related_people: [],
    },
  }
  vi.mocked(api.getRun).mockResolvedValue(record)
  // The landing health band lazily reads the workspace graph — return an
  // empty-but-valid graph so the query never resolves to `undefined`.
  vi.mocked(api.getWorkspaceGraph).mockResolvedValue({
    scope: ["github"],
    nodes: [],
    edges: [],
    stats: {
      totalNodes: 0,
      totalEdges: 0,
      kinds: {},
      perLayer: { schema: 0, memory: 0, entity: 0 },
      unmapped: ["github"],
    },
  })
})

afterEach(() => {
  vi.clearAllMocks()
})

describe("AgenticSearch landing inertness (#80)", () => {
  it("never fires POST /runs on mount", async () => {
    renderView()
    // Wait for the landing page to settle (workspace hero renders).
    await screen.findByRole("heading", { name: /agentic search/i })
    expect(api.startRun).not.toHaveBeenCalled()
  })

  it("replays a past-query suggestion via GET snapshot, NOT a new run", async () => {
    renderView()
    // The example chip on the hero carries the past query text + a replay title.
    const chip = await screen.findByTitle("Replay this search")
    fireEvent.click(chip)

    // Replay opens the stored run via getRun; it must NOT POST a fresh run.
    await waitFor(() => expect(api.getRun).toHaveBeenCalledWith("run-123"))
    expect(api.startRun).not.toHaveBeenCalled()
  })
})
