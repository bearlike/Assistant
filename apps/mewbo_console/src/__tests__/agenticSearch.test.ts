/**
 * Unit tests for the Agentic Search API client + the run-stream projection.
 *
 * Strategy mirrors the wiki client test: intercept `fetch` via `vi.spyOn`.
 * SSE tests build a `ReadableStream` from pre-built frame strings so the
 * shared `sseStream` parser is exercised end-to-end without a network.
 */

import { beforeEach, describe, expect, it, vi } from "vitest"
import type { MockInstance } from "vitest"

import {
  getRun,
  getScgStatus,
  listMapJobs,
  listWorkspaceRuns,
  startMapJob,
  startRun,
  streamMapJob,
  streamRun,
} from "../api/agenticSearch"
import {
  initialRunStreamState,
  reduceRun,
  toRunPayload,
  type RunStreamState,
} from "../hooks/useAgenticSearch"
import type { MapJobEvent, SearchEvent } from "../types/agenticSearch"

// ── Helpers ──────────────────────────────────────────────────────────────────

function jsonResp(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  })
}

function sseFrame(type: string, data: Record<string, unknown>): string {
  return `event: ${type}\ndata: ${JSON.stringify(data)}\n\n`
}

function sseResp(frames: string): Response {
  return new Response(new TextEncoder().encode(frames), {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  })
}

async function collect<T>(iter: AsyncIterable<T>): Promise<T[]> {
  const items: T[] = []
  for await (const item of iter) items.push(item)
  return items
}

const runPayload = {
  run_id: "r1",
  session_id: "s1",
  query: "where is auth",
  workspace_id: "w1",
  status: "completed",
  total_ms: 4200,
  answer: { tldr: "Auth lives in core.", bullets: [], confidence: 0.8, sources_count: 2 },
  results: [
    { id: "res-1", source: "github", kind: "code", relevance: 0.9, title: "auth.py", url: "", snippet: "", author: "", timestamp: "" },
  ],
  trace: [],
  related_questions: [],
  related_people: [],
}

let fetchSpy: MockInstance<Parameters<typeof fetch>, ReturnType<typeof fetch>>

beforeEach(() => {
  fetchSpy = vi.spyOn(global, "fetch")
})

// ── startRun ─────────────────────────────────────────────────────────────────

describe("startRun", () => {
  it("returns the full {run, run_id, session_id, status} envelope", async () => {
    fetchSpy.mockResolvedValueOnce(
      jsonResp({ run: runPayload, run_id: "r1", session_id: "s1", status: "completed" }),
    )
    const result = await startRun({ workspace_id: "w1", query: "q" })
    expect(result.run_id).toBe("r1")
    expect(result.session_id).toBe("s1")
    expect(result.status).toBe("completed")
    expect(result.run.results).toHaveLength(1)
  })

  it("sends the tier on the POST body", async () => {
    fetchSpy.mockResolvedValueOnce(
      jsonResp({ run: runPayload, run_id: "r1", session_id: "s1", status: "completed" }),
    )
    await startRun({ workspace_id: "w1", query: "q", tier: "deep" })
    const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(JSON.parse(init.body as string)).toMatchObject({ tier: "deep" })
  })

  it("POSTs only the live RunInput fields — the dead `model` param is gone", async () => {
    fetchSpy.mockResolvedValueOnce(
      jsonResp({ run: runPayload, run_id: "r1", session_id: "s1", status: "completed" }),
    )
    await startRun({ workspace_id: "w1", query: "q" })
    const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    const body = JSON.parse(init.body as string)
    expect(body).toEqual({ workspace_id: "w1", query: "q" })
    expect("model" in body).toBe(false)
  })
})

// ── listWorkspaceRuns ────────────────────────────────────────────────────────

describe("listWorkspaceRuns", () => {
  it("GETs /workspaces/<id>/runs and unwraps the {runs} envelope", async () => {
    const record = {
      run_id: "r1",
      session_id: "s1",
      workspace_id: "w1",
      query: "q",
      status: "completed",
      created_at: "2026-06-05T00:00:00Z",
      total_ms: 4200,
      source_ids: ["github"],
      allowed_tools: [],
      output_contract_version: "1.0",
      payload: null,
    }
    fetchSpy.mockResolvedValueOnce(jsonResp({ runs: [record] }))
    const runs = await listWorkspaceRuns("w1")
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toMatch(/\/api\/agentic_search\/workspaces\/w1\/runs$/)
    expect(init.method ?? "GET").toBe("GET")
    expect(runs).toHaveLength(1)
    expect(runs[0].run_id).toBe("r1")
    expect(runs[0].status).toBe("completed")
  })
})

// ── getRun ───────────────────────────────────────────────────────────────────

describe("getRun", () => {
  it("GETs /runs/<id> and unwraps {run} to a RunRecord", async () => {
    const record = {
      run_id: "r1",
      session_id: "s1",
      workspace_id: "w1",
      query: "q",
      status: "completed",
      created_at: "2026-06-05T00:00:00Z",
      total_ms: 4200,
      source_ids: ["github"],
      allowed_tools: [],
      output_contract_version: "1.0",
      payload: runPayload,
    }
    fetchSpy.mockResolvedValueOnce(jsonResp({ run: record }))
    const result = await getRun("r1")
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toMatch(/\/api\/agentic_search\/runs\/r1$/)
    expect(init.method ?? "GET").toBe("GET")
    expect(result.payload?.run_id).toBe("r1")
    expect(result.status).toBe("completed")
  })
})

// ── streamRun (SSE) ──────────────────────────────────────────────────────────

describe("streamRun", () => {
  it("parses the normalized run event stream into a SearchEvent[]", async () => {
    const frames =
      sseFrame("run_started", { run_id: "r1", session_id: "s1", workspace_id: "w1", query: "q", sources: ["github"] }) +
      sseFrame("agent_start", { agent_id: "a1", source_id: "github", name: "GitHub", slot: 0 }) +
      sseFrame("agent_line", { agent_id: "a1", line: { t_ms: 10, glyph: "·", text: "searching", done: false, empty: false } }) +
      sseFrame("result", { result: runPayload.results[0] }) +
      sseFrame("agent_done", { agent_id: "a1", results_count: 1, empty: false }) +
      sseFrame("answer_delta", { text: "Auth " }) +
      sseFrame("answer_delta", { text: "lives in core." }) +
      sseFrame("answer_ready", { answer: runPayload.answer }) +
      sseFrame("run_done", { status: "completed", total_ms: 4200 })

    fetchSpy.mockResolvedValueOnce(sseResp(frames))
    const events = await collect<SearchEvent>(streamRun("r1"))

    expect(events.map((e) => e.type)).toEqual([
      "run_started",
      "agent_start",
      "agent_line",
      "result",
      "agent_done",
      "answer_delta",
      "answer_delta",
      "answer_ready",
      "run_done",
    ])
    const url = (fetchSpy.mock.calls[0] as [string])[0]
    expect(url).toMatch(/\/api\/agentic_search\/runs\/r1\/events/)
  })

  it("skips heartbeat frames", async () => {
    const frames =
      sseFrame("heartbeat", {}) +
      sseFrame("run_started", { run_id: "r1", session_id: "s1", workspace_id: "w1", query: "q", sources: [] }) +
      sseFrame("heartbeat", {}) +
      sseFrame("run_done", { status: "completed", total_ms: 0 })
    fetchSpy.mockResolvedValueOnce(sseResp(frames))
    const events = await collect<SearchEvent>(streamRun("r1"))
    expect(events.map((e) => e.type)).toEqual(["run_started", "run_done"])
  })

  it("resolves when the AbortSignal fires before any frame", async () => {
    let close: (() => void) | undefined
    const stream = new ReadableStream({
      start(ctrl) {
        close = () => ctrl.close()
      },
    })
    fetchSpy.mockResolvedValueOnce(
      new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
    )
    const ctrl = new AbortController()
    const promise = collect(streamRun("r1", { signal: ctrl.signal }))
    await new Promise((r) => setTimeout(r, 0))
    ctrl.abort()
    if (close) close()
    const events = await promise
    expect(Array.isArray(events)).toBe(true)
  })
})

// ── SCG introspection + map jobs ─────────────────────────────────────────────

describe("getScgStatus", () => {
  it("maps the 503 'SCG disabled' response to {enabled: false}", async () => {
    fetchSpy.mockResolvedValueOnce(
      jsonResp({ message: "SCG is disabled (set scg.enabled=true)" }, 503),
    )
    const status = await getScgStatus()
    expect(status.enabled).toBe(false)
    expect(status.sources).toEqual([])
  })

  it("passes the enabled introspection payload through", async () => {
    fetchSpy.mockResolvedValueOnce(
      jsonResp({
        enabled: true,
        counts: { sources: 1, nodes: 12, edges: 20, recipes: 3 },
        sources: [{ source_id: "github", source_type: "mcp_tool_list" }],
      }),
    )
    const status = await getScgStatus()
    expect(status.enabled).toBe(true)
    expect(status.counts?.nodes).toBe(12)
    expect(status.sources[0].source_id).toBe("github")
  })
})

describe("map jobs", () => {
  const job = {
    job_id: "j1",
    source_id: "github",
    source_type: "mcp_tool_list",
    status: "running",
    phase: "parse",
    node_count: 4,
    edge_count: 6,
    created_at: "2026-06-09T00:00:00Z",
  }

  it("startMapJob POSTs the source_type and unwraps {job, job_id}", async () => {
    fetchSpy.mockResolvedValueOnce(jsonResp({ job, job_id: "j1" }, 202))
    const result = await startMapJob("github", { source_type: "mcp_tool_list" })
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toMatch(/\/api\/agentic_search\/sources\/github\/map$/)
    expect(JSON.parse(init.body as string)).toEqual({ source_type: "mcp_tool_list" })
    expect(result.job_id).toBe("j1")
  })

  it("listMapJobs unwraps {jobs} latest-first", async () => {
    fetchSpy.mockResolvedValueOnce(jsonResp({ jobs: [job] }))
    const jobs = await listMapJobs("github")
    const [url] = fetchSpy.mock.calls[0] as [string]
    expect(url).toMatch(/\/api\/agentic_search\/sources\/github\/map\/jobs$/)
    expect(jobs[0].job_id).toBe("j1")
  })

  it("streamMapJob parses phase events and the terminal run_done", async () => {
    const frames =
      sseFrame("phase", { name: "connect" }) +
      sseFrame("phase", { name: "finalize" }) +
      sseFrame("run_done", { status: "completed", total_ms: 900 })
    fetchSpy.mockResolvedValueOnce(sseResp(frames))
    const events = await collect<MapJobEvent>(streamMapJob("github", { jobId: "j1" }))
    expect(events.map((e) => e.type)).toEqual(["phase", "phase", "run_done"])
    const url = (fetchSpy.mock.calls[0] as [string])[0]
    expect(url).toMatch(/\/sources\/github\/map\/events\?job_id=j1/)
  })
})

describe("streamRun abort", () => {
  it("resolves when the AbortSignal fires before any frame", async () => {
    let close: (() => void) | undefined
    const stream = new ReadableStream({
      start(ctrl) {
        close = () => ctrl.close()
      },
    })
    fetchSpy.mockResolvedValueOnce(
      new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
    )
    const ctrl = new AbortController()
    const promise = collect(streamRun("r1", { signal: ctrl.signal }))
    await new Promise((r) => setTimeout(r, 0))
    ctrl.abort()
    if (close) close()
    const events = await promise
    expect(Array.isArray(events)).toBe(true)
  })
})

// ── reduceRun — progressive reducer-driven state ─────────────────────────────
// The UI renders exclusively from this folded state; these tests pin the
// progressive reveal (no fixture short-circuit dumping everything at once).

describe("reduceRun", () => {
  const fold = (events: SearchEvent[], from = initialRunStreamState): RunStreamState =>
    events.reduce(reduceRun, from)

  const started: SearchEvent = {
    type: "run_started",
    run_id: "r1",
    session_id: "s1",
    workspace_id: "w1",
    query: "q",
    sources: ["github"],
  }

  it("reveals progressively: agents on agent_start, results on result, answer on deltas", () => {
    let state = fold([started])
    // Nothing has arrived yet — no fixture payload pre-populates anything.
    expect(state.status).toBe("running")
    expect(state.trace).toHaveLength(0)
    expect(state.results).toHaveLength(0)
    expect(state.answer.tldr).toBe("")
    expect(state.answerReady).toBe(false)

    state = fold(
      [{ type: "agent_start", agent_id: "a1", source_id: "github", name: "GitHub", slot: 0 }],
      state,
    )
    expect(state.trace).toHaveLength(1)
    expect(state.results).toHaveLength(0)

    const result = runPayload.results[0] as never
    state = fold([{ type: "result", result }], state)
    expect(state.results).toHaveLength(1)
    expect(state.answer.tldr).toBe("") // answer still pending

    state = fold(
      [
        { type: "answer_delta", text: "Auth " },
        { type: "answer_delta", text: "lives in core." },
      ],
      state,
    )
    expect(state.answer.tldr).toBe("Auth lives in core.")
    expect(state.answerReady).toBe(false)
    expect(state.done).toBe(false)

    state = fold(
      [
        { type: "answer_ready", answer: runPayload.answer as never },
        { type: "run_done", status: "completed", total_ms: 4200 },
      ],
      state,
    )
    expect(state.answerReady).toBe(true)
    expect(state.done).toBe(true)
    expect(state.status).toBe("completed")
    expect(toRunPayload(state).total_ms).toBe(4200)
  })

  it("folds an error event into a terminal failed state", () => {
    const state = fold([
      started,
      { type: "error", error: { code: "internal", message: "fan-out crashed" } },
    ])
    expect(state.status).toBe("failed")
    expect(state.done).toBe(true)
    expect(state.error?.message).toBe("fan-out crashed")
    expect(toRunPayload(state).error).toBe("fan-out crashed")
  })

  it("folds a cancelled event into a terminal cancelled state", () => {
    const state = fold([started, { type: "cancelled" }])
    expect(state.status).toBe("cancelled")
    expect(state.done).toBe(true)
    expect(state.error).toBeNull()
  })
})
