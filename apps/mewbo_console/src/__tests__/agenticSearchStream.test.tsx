/**
 * Live-SSE leg tests for Agentic Search (#80 follow-up / #82).
 *
 * Two layers:
 *  1. `reduceRun` unit: the fold attaches on the FIRST frame of any type (so a
 *     dropped/buffered `run_started` opener can't wedge the view), seeds the
 *     known run id via the `attach` action, and dedups `result` ids.
 *  2. View integration: a controllable SSE generator feeds frames into the real
 *     `AgenticSearchView`; the view must flip off "Starting search…" onto the
 *     trace/results view as soon as events stream — even with NO `run_started`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"

afterEach(cleanup)

import {
  initialRunStreamState,
  reduceRun,
  toRunPayload,
} from "../hooks/useAgenticSearch"
import type { SearchResult } from "../types/agenticSearch"

// ── reduceRun unit ───────────────────────────────────────────────────────────

function result(id: string): SearchResult {
  return {
    id,
    source: "github",
    kind: "code",
    relevance: 0.9,
    title: id,
    url: `github.com/${id}`,
    snippet: "x",
    author: "kk",
    timestamp: "2d",
  }
}

describe("reduceRun attach + first-frame attach", () => {
  it("seeds the run id on `attach` but stays un-attached until a real frame", () => {
    const s = reduceRun(initialRunStreamState, { type: "attach", runId: "r1" })
    expect(s.runId).toBe("r1")
    expect(s.attached).toBe(false)
  })

  it("attaches on the FIRST frame even when run_started never arrives", () => {
    let s = reduceRun(initialRunStreamState, { type: "attach", runId: "r1" })
    // Opener dropped/buffered — first real frame is agent_start.
    s = reduceRun(s, {
      type: "agent_start",
      agent_id: "a1",
      source_id: "github",
      name: "GitHub",
      slot: 0,
    })
    expect(s.attached).toBe(true)
    expect(s.startedAt).not.toBeNull()
    expect(s.status).toBe("running")
    expect(s.trace).toHaveLength(1)
    // The known run id survives so toRunPayload renders against it.
    expect(toRunPayload(s).run_id).toBe("r1")
  })

  it("dedups duplicate result ids (echo replay / snapshot merge)", () => {
    let s = reduceRun(initialRunStreamState, { type: "attach", runId: "r1" })
    s = reduceRun(s, { type: "result", result: result("dup") })
    s = reduceRun(s, { type: "result", result: result("dup") })
    s = reduceRun(s, { type: "result", result: result("other") })
    expect(s.results.map((r) => r.id)).toEqual(["dup", "other"])
  })

  it("a new `attach` run id wipes the previous fold", () => {
    let s = reduceRun(initialRunStreamState, { type: "attach", runId: "r1" })
    s = reduceRun(s, { type: "result", result: result("a") })
    s = reduceRun(s, { type: "attach", runId: "r2" })
    expect(s.runId).toBe("r2")
    expect(s.results).toHaveLength(0)
    expect(s.attached).toBe(false)
  })

  it("agent_done projects the probe's evidence onto the lane (#86)", () => {
    let s = reduceRun(initialRunStreamState, { type: "attach", runId: "r1" })
    s = reduceRun(s, {
      type: "agent_start",
      agent_id: "a1",
      source_id: "github",
      name: "scg-path-probe",
      slot: 0,
    })
    s = reduceRun(s, {
      type: "agent_done",
      agent_id: "a1",
      results_count: 0,
      empty: false,
      result: "EVIDENCE (pathway: github#search_issues): 2 issues filed last week.",
    })
    const lane = s.trace.find((a) => a.agent_id === "a1")
    expect(lane).toBeDefined()
    // The evidence rides the lane (TraceDrawer renders it as the response panel)…
    expect(lane?.result).toContain("EVIDENCE (pathway: github#search_issues)")
    // …and the terminal line marks it done + non-empty.
    expect(lane?.lines.at(-1)).toMatchObject({ done: true, empty: false })
  })

  it("agent_done flags a NO-DATA dead-end as empty (#86)", () => {
    let s = reduceRun(initialRunStreamState, { type: "attach", runId: "r1" })
    s = reduceRun(s, {
      type: "agent_start",
      agent_id: "a2",
      source_id: "linear",
      name: "scg-path-probe",
      slot: 1,
    })
    s = reduceRun(s, {
      type: "agent_done",
      agent_id: "a2",
      results_count: 0,
      empty: true,
      result: "NO DATA on pathway linear#search for: matching issues",
    })
    const lane = s.trace.find((a) => a.agent_id === "a2")
    expect(lane).toBeDefined()
    expect(lane?.result).toMatch(/^NO DATA/)
    expect(lane?.lines.at(-1)).toMatchObject({ done: true, empty: true })
  })
})

// ── View integration: stream flips off "Starting search…" ────────────────────

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
    streamRun: vi.fn(),
  }
})

import AgenticSearchView from "../components/agentic_search/AgenticSearchView"
import * as api from "../api/agenticSearch"
import type { RunRecord, SearchEvent, Workspace } from "../types/agenticSearch"

const workspace: Workspace = {
  id: "w1",
  name: "Eng",
  desc: "Engineering docs",
  sources: ["github"],
  instructions: "",
  created: "May 2026",
  created_at: "2026-05-01T00:00:00Z",
  past_queries: [],
}

/** An async generator the test pushes frames into, then closes. */
function makeControllableStream() {
  const queue: SearchEvent[] = []
  let resolveNext: (() => void) | null = null
  let closed = false
  const push = (ev: SearchEvent) => {
    queue.push(ev)
    resolveNext?.()
  }
  const close = () => {
    closed = true
    resolveNext?.()
  }
  const gen = (async function* () {
    while (true) {
      if (queue.length > 0) {
        yield queue.shift() as SearchEvent
        continue
      }
      if (closed) return
      await new Promise<void>((r) => {
        resolveNext = r
      })
    }
  })()
  return { gen, push, close }
}

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <AgenticSearchView />
    </QueryClientProvider>,
  )
}

describe("AgenticSearch live-SSE view transition (#80)", () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.mocked(api.listSources).mockResolvedValue([
      { id: "github", name: "GitHub", color: "#fff", bg: "#000", glyph: "G", desc: "Code" },
    ])
    vi.mocked(api.listWorkspaces).mockResolvedValue([workspace])
    vi.mocked(api.listWorkspaceRuns).mockResolvedValue([])
    // Async/orchestrated run: POST returns `running` immediately.
    vi.mocked(api.startRun).mockResolvedValue({
      run: {
        run_id: "run-live",
        query: "where is auth",
        workspace_id: "w1",
        status: "running",
        total_ms: 0,
        answer: { tldr: "", bullets: [], confidence: 0, sources_count: 0 },
        results: [],
        trace: [],
        related_questions: [],
        related_people: [],
      },
      run_id: "run-live",
      session_id: "s",
      status: "running",
    })
    // Snapshot of a still-running async run has NO materialized payload yet —
    // exactly the deployed condition that wedged the view on "Starting search…".
    const record: RunRecord = {
      run_id: "run-live",
      session_id: "s",
      workspace_id: "w1",
      query: "where is auth",
      status: "running",
      created_at: "2026-05-02T00:00:00Z",
      total_ms: 0,
      source_ids: ["github"],
      allowed_tools: [],
      output_contract_version: "1.0",
      payload: null,
    }
    vi.mocked(api.getRun).mockResolvedValue(record)
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

  it("flips from 'Starting search…' to the trace view on the first agent_start (no run_started)", async () => {
    const { gen, push, close } = makeControllableStream()
    vi.mocked(api.streamRun).mockReturnValue(gen as ReturnType<typeof api.streamRun>)

    const { container } = renderView()
    await screen.findByRole("heading", { name: /agentic search/i })

    // Submit a run via the hero search bar.
    const input = await screen.findByPlaceholderText("Ask or search the workspace…")
    fireEvent.change(input, { target: { value: "where is auth" } })
    fireEvent.keyDown(input, { key: "Enter" })

    // Before any SSE frame: the view sits on the in-flight "Starting search…".
    await waitFor(() => expect(api.startRun).toHaveBeenCalled())
    await screen.findByText(/Starting search/i)

    // First real frame is agent_start — opener intentionally omitted.
    push({
      type: "agent_start",
      agent_id: "a1",
      source_id: "github",
      name: "GitHub",
      slot: 0,
    })

    // The view must leave the "Starting search…" wedge and render the run.
    await waitFor(() => {
      expect(screen.queryByText(/Starting search/i)).not.toBeInTheDocument()
    })
    // The streaming results header ("… · streaming") proves the run view mounted.
    await waitFor(() => {
      expect(container.textContent).toContain("streaming")
    })

    close()
  })
})
