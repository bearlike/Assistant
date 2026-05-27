/**
 * Unit tests for the Agentic Search API client + the run-stream projection.
 *
 * Strategy mirrors the wiki client test: intercept `fetch` via `vi.spyOn`.
 * SSE tests build a `ReadableStream` from pre-built frame strings so the
 * shared `sseStream` parser is exercised end-to-end without a network.
 */

import { beforeEach, describe, expect, it, vi } from "vitest"
import type { MockInstance } from "vitest"

import { getRun, startRun, streamRun } from "../api/agenticSearch"
import type { SearchEvent } from "../types/agenticSearch"

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
