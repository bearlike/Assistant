/**
 * Unit tests for useDraftStream.
 *
 * Strategy: spy on the shared `sseStream` generator (which itself uses
 * `fetch`). We intercept `fetch` via `vi.spyOn` and construct `ReadableStream`
 * responses from pre-built SSE frame strings — the same approach used by the
 * agenticSearch and wiki client tests.
 *
 * `renderHook` from @testing-library/react drives the hook inside a minimal
 * React tree so the `useEffect` / `useReducer` lifecycle runs correctly.
 */

import { beforeEach, describe, expect, it, vi } from "vitest"
import { renderHook, act, waitFor } from "@testing-library/react"
import type { MockInstance } from "vitest"

// Ensure the mock API key is available before module imports resolve it.
;(window as unknown as Record<string, unknown>).__MEWBO_CONFIG__ = {
  VITE_API_KEY: "test-key",
}

import { useDraftStream } from "../hooks/useDraftStream"

// ── Helpers ───────────────────────────────────────────────────────────────────

function sseFrame(data: Record<string, unknown>): string {
  return `data: ${JSON.stringify(data)}\n\n`
}

function sseResp(frames: string): Response {
  return new Response(new TextEncoder().encode(frames), {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  })
}

let fetchSpy: MockInstance<Parameters<typeof fetch>, ReturnType<typeof fetch>>

beforeEach(() => {
  fetchSpy = vi.spyOn(global, "fetch")
})

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("useDraftStream", () => {
  it("accumulates token deltas and sets done on the terminal frame", async () => {
    const frames =
      sseFrame({ token: "Hello" }) +
      sseFrame({ token: ", " }) +
      sseFrame({ token: "world!" }) +
      sseFrame({ done: true })

    fetchSpy.mockResolvedValueOnce(sseResp(frames))

    const { result } = renderHook(() =>
      useDraftStream({ query: "test query" }),
    )

    // Wait for the stream to complete.
    await waitFor(() => {
      expect(result.current.done).toBe(true)
    })

    expect(result.current.text).toBe("Hello, world!")
    expect(result.current.streaming).toBe(false)
    expect(result.current.error).toBe(null)
  })

  it("sets streaming=true while frames are arriving before done", async () => {
    // Use a stream that we control manually so we can inspect mid-stream state.
    // We do NOT call close() manually after the `done` frame because the generator
    // cancels the reader internally on `break`, which already closes the controller.
    let enqueue!: (chunk: Uint8Array) => void
    const stream = new ReadableStream<Uint8Array>({
      start(ctrl) {
        enqueue = (c) => ctrl.enqueue(c)
      },
    })
    fetchSpy.mockResolvedValueOnce(
      new Response(stream, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      }),
    )

    const { result } = renderHook(() =>
      useDraftStream({ query: "streaming test" }),
    )

    // Pump one token frame.
    await act(async () => {
      enqueue(new TextEncoder().encode(sseFrame({ token: "Hi" })))
      await new Promise((r) => setTimeout(r, 0))
    })

    expect(result.current.streaming).toBe(true)
    expect(result.current.text).toBe("Hi")

    // Terminal frame — the generator breaks, cancelling the reader internally.
    await act(async () => {
      enqueue(new TextEncoder().encode(sseFrame({ done: true })))
      await new Promise((r) => setTimeout(r, 0))
    })

    await waitFor(() => expect(result.current.done).toBe(true))
    expect(result.current.streaming).toBe(false)
  })

  it("captures an error message when the fetch throws", async () => {
    fetchSpy.mockRejectedValueOnce(new Error("Network failure"))

    const { result } = renderHook(() =>
      useDraftStream({ query: "fail query" }),
    )

    await waitFor(() => {
      expect(result.current.error).toBe("Network failure")
    })

    expect(result.current.done).toBe(true)
    expect(result.current.streaming).toBe(false)
  })

  it("resets state when input becomes null", async () => {
    const frames = sseFrame({ token: "A" }) + sseFrame({ done: true })
    fetchSpy.mockResolvedValueOnce(sseResp(frames))

    const { result, rerender } = renderHook(
      ({ input }) => useDraftStream(input),
      { initialProps: { input: { query: "q1" } as { query: string } | null } },
    )

    await waitFor(() => expect(result.current.done).toBe(true))
    expect(result.current.text).toBe("A")

    // Set input to null → should reset.
    rerender({ input: null })

    await waitFor(() => {
      expect(result.current.text).toBe("")
      expect(result.current.done).toBe(false)
      expect(result.current.streaming).toBe(false)
      expect(result.current.error).toBe(null)
    })
  })

  it("restarts the stream when the query changes", async () => {
    const frames1 = sseFrame({ token: "first" }) + sseFrame({ done: true })
    const frames2 = sseFrame({ token: "second" }) + sseFrame({ done: true })
    fetchSpy
      .mockResolvedValueOnce(sseResp(frames1))
      .mockResolvedValueOnce(sseResp(frames2))

    const { result, rerender } = renderHook(
      ({ input }) => useDraftStream(input),
      { initialProps: { input: { query: "q1" } as { query: string } | null } },
    )

    await waitFor(() => expect(result.current.done).toBe(true))
    expect(result.current.text).toBe("first")

    rerender({ input: { query: "q2" } })

    // New stream: text resets then fills with "second".
    await waitFor(() => expect(result.current.text).toBe("second"))
    expect(fetchSpy).toHaveBeenCalledTimes(2)
  })

  it("sends the query body + optional workspace/model to the endpoint", async () => {
    const frames = sseFrame({ done: true })
    fetchSpy.mockResolvedValueOnce(sseResp(frames))

    const { result } = renderHook(() =>
      useDraftStream({ query: "q", workspace: "my-repo", model: "gpt-4o" }),
    )

    await waitFor(() => expect(result.current.done).toBe(true))

    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toMatch(/\/v1\/draft\/stream/)
    expect(init.method).toBe("POST")
    const body = JSON.parse(init.body as string) as Record<string, unknown>
    expect(body.query).toBe("q")
    expect(body.workspace).toBe("my-repo")
    expect(body.model).toBe("gpt-4o")
  })
})
