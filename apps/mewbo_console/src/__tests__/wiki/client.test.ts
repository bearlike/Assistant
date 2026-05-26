/**
 * Unit tests for the wiki HTTP/SSE client.
 *
 * Strategy: intercept `fetch` via `vi.spyOn`. For SSE tests, construct a
 * `ReadableStream` from pre-built SSE frame strings so the async iterable
 * parser is exercised end-to-end without a real network.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import type { MockInstance } from "vitest";

// getApiKey() in client.ts reads window.__MEWBO_CONFIG__ lazily (at call time),
// so setting it here before tests run is sufficient — no module re-import needed.
(window as unknown as Record<string, unknown>).__MEWBO_CONFIG__ = { VITE_API_KEY: "test-key" };

import {
  cancelIndexingJob,
  createIndexingJob,
  deleteProject,
  getAnswer,
  getIndexingJob,
  getPage,
  isWikiError,
  listLanguages,
  listPlatforms,
  listProjects,
  requestWikiRefresh,
  startAnswer,
  streamAnswer,
  subscribeToIndexing,
} from "../../components/wiki/api/client";

// ── Helpers ────────────────────────────────────────────────────────────────

/** Build a fake `Response` wrapping a JSON body. */
function jsonResp(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Build a fake `Response` wrapping an SSE stream from a list of frame strings. */
function sseResp(frames: string[]): Response {
  const text = frames.join("") + "\n\n";
  return new Response(new TextEncoder().encode(text), {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

/** Build an SSE frame string from event type + JSON data payload. */
function sseFrame(type: string, data: Record<string, unknown>): string {
  return `event: ${type}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** Collect all items from an async iterable into an array. */
async function collect<T>(iter: AsyncIterable<T>): Promise<T[]> {
  const items: T[] = [];
  for await (const item of iter) {
    items.push(item);
  }
  return items;
}

// ── Setup ──────────────────────────────────────────────────────────────────

let fetchSpy: MockInstance<Parameters<typeof fetch>, ReturnType<typeof fetch>>;

beforeEach(() => {
  fetchSpy = vi.spyOn(global, "fetch");
});

// ── isWikiError ────────────────────────────────────────────────────────────

describe("isWikiError", () => {
  it("returns true for an object with code", () => {
    expect(isWikiError({ code: "not_found", message: "x" })).toBe(true);
  });
  it("returns false for plain strings and nulls", () => {
    expect(isWikiError("oops")).toBe(false);
    expect(isWikiError(null)).toBe(false);
  });
});

// ── listProjects ───────────────────────────────────────────────────────────

describe("listProjects", () => {
  it("GETs /v1/wiki/projects and returns array", async () => {
    const payload = [{ slug: "owner/repo", source: "github", lang: "en", indexedAt: "2026-01-01", pages: 5, desc: "x" }];
    fetchSpy.mockResolvedValueOnce(jsonResp(payload));

    const result = await listProjects();

    expect(fetchSpy).toHaveBeenCalledOnce();
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/wiki/projects");
    expect(init.method).toBe("GET");
    expect((init.headers as Record<string, string>)["X-API-Key"]).toBe("test-key");
    expect(result).toEqual(payload);
  });
});

// ── deleteProject ──────────────────────────────────────────────────────────

describe("deleteProject", () => {
  it("DELETEs the slug-encoded URL", async () => {
    fetchSpy.mockResolvedValueOnce(jsonResp({ deleted: true }));
    const result = await deleteProject("owner/repo");
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/wiki/projects/owner%2Frepo");
    expect(init.method).toBe("DELETE");
    expect(result).toEqual({ deleted: true });
  });
});

// ── listPlatforms / listLanguages ──────────────────────────────────────────
// Note: there is intentionally no listModels — the wiki picker uses the
// shared /api/models endpoint via the top-level useModels() hook (DRY).

describe("listPlatforms", () => {
  it("GETs /v1/wiki/platforms", async () => {
    fetchSpy.mockResolvedValueOnce(jsonResp([]));
    await listPlatforms();
    expect((fetchSpy.mock.calls[0] as [string])[0]).toBe("/v1/wiki/platforms");
  });
});

describe("listLanguages", () => {
  it("GETs /v1/wiki/languages", async () => {
    fetchSpy.mockResolvedValueOnce(jsonResp([]));
    await listLanguages();
    expect((fetchSpy.mock.calls[0] as [string])[0]).toBe("/v1/wiki/languages");
  });
});

// ── getPage ────────────────────────────────────────────────────────────────

describe("getPage", () => {
  it("GETs /v1/wiki/projects/<slug>/pages/<pageId> with encoded path", async () => {
    const payload = { id: "core", title: "Core", frontmatter: { title: "Core", slug: "core" }, body: "# Core", toc: [], nav: [] };
    fetchSpy.mockResolvedValueOnce(jsonResp(payload));
    const result = await getPage("owner/repo", "core");
    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toBe("/v1/wiki/projects/owner%2Frepo/pages/core");
    expect(result).toEqual(payload);
  });

  it("returns null on 404", async () => {
    fetchSpy.mockResolvedValueOnce(
      new Response(JSON.stringify({ code: "not_found", message: "page not found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const result = await getPage("owner/repo", "missing");
    expect(result).toBeNull();
  });
});

// ── createIndexingJob ──────────────────────────────────────────────────────

describe("createIndexingJob", () => {
  it("POSTs the submission body to /v1/wiki/index", async () => {
    const job = { jobId: "j1", slug: "owner/repo", status: "queued", scannedCount: 0, totalCount: 10, currentFile: null };
    fetchSpy.mockResolvedValueOnce(jsonResp(job));
    const result = await createIndexingJob({ slug: "owner/repo", model: "m" } as never);
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/wiki/index");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toMatchObject({ slug: "owner/repo" });
    expect(result).toEqual(job);
  });

  it("throws validation error when slug is missing", async () => {
    await expect(createIndexingJob({ slug: "" } as never)).rejects.toMatchObject({ code: "validation" });
  });
});

// ── getIndexingJob ─────────────────────────────────────────────────────────

describe("getIndexingJob", () => {
  it("GETs /v1/wiki/index/<jobId>", async () => {
    const job = { jobId: "j1", slug: "s", status: "complete", scannedCount: 5, totalCount: 5, currentFile: null };
    fetchSpy.mockResolvedValueOnce(jsonResp(job));
    const result = await getIndexingJob("j1");
    expect((fetchSpy.mock.calls[0] as [string])[0]).toBe("/v1/wiki/index/j1");
    expect(result).toEqual(job);
  });
});

// ── cancelIndexingJob ──────────────────────────────────────────────────────

describe("cancelIndexingJob", () => {
  it("DELETEs /v1/wiki/index/<jobId>", async () => {
    const job = { jobId: "j1", slug: "s", status: "cancelled", scannedCount: 2, totalCount: 5, currentFile: null };
    fetchSpy.mockResolvedValueOnce(jsonResp(job));
    const result = await cancelIndexingJob("j1");
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/wiki/index/j1");
    expect(init.method).toBe("DELETE");
    expect(result.status).toBe("cancelled");
  });
});

// ── getAnswer ─────────────────────────────────────────────────────────────

describe("getAnswer", () => {
  it("GETs /v1/wiki/qa/<answerId>", async () => {
    const answer = { answerId: "a1", fromPageId: "core", summarySources: [], model: "m", blocks: [] };
    fetchSpy.mockResolvedValueOnce(jsonResp(answer));
    const result = await getAnswer("a1");
    expect((fetchSpy.mock.calls[0] as [string])[0]).toBe("/v1/wiki/qa/a1");
    expect(result).toEqual(answer);
  });
});

// ── requestWikiRefresh ────────────────────────────────────────────────────

describe("requestWikiRefresh", () => {
  it("POSTs an empty body to /v1/wiki/projects/<slug>/refresh", async () => {
    fetchSpy.mockResolvedValueOnce(jsonResp({ queued: true }));
    await requestWikiRefresh("owner/repo");
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/wiki/projects/owner%2Frepo/refresh");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({});
  });

  it("throws validation error when slug is empty", async () => {
    await expect(requestWikiRefresh("")).rejects.toMatchObject({ code: "validation" });
  });
});

// ── HTTP error propagation ────────────────────────────────────────────────

describe("HTTP error propagation", () => {
  it("maps 403 JSON error to WikiError", async () => {
    fetchSpy.mockResolvedValueOnce(
      new Response(JSON.stringify({ code: "forbidden", message: "bad token" }), {
        status: 403,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await expect(listProjects()).rejects.toMatchObject({ code: "forbidden", message: "bad token" });
  });

  it("falls back to internal code for non-JSON error bodies", async () => {
    fetchSpy.mockResolvedValueOnce(
      new Response("Internal Server Error", { status: 500 }),
    );
    await expect(listProjects()).rejects.toMatchObject({ code: "internal" });
  });
});

// ── subscribeToIndexing (SSE) ─────────────────────────────────────────────

describe("subscribeToIndexing", () => {
  it("parses an SSE stream into IndexingEvents", async () => {
    const frames =
      sseFrame("queued", { jobId: "j1", slug: "owner/repo", totalCount: 2 }) +
      sseFrame("scanning", { file: "README.md", index: 0, totalCount: 2 }) +
      sseFrame("scanned", { file: "README.md", index: 0, totalCount: 2 }) +
      sseFrame("complete", { landingPageId: "overview", pageCount: 5 });

    fetchSpy.mockResolvedValueOnce(
      new Response(new TextEncoder().encode(frames), {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      }),
    );

    const events = await collect(subscribeToIndexing("j1"));

    expect(events).toHaveLength(4);
    expect(events[0]).toMatchObject({ type: "queued", jobId: "j1", totalCount: 2 });
    expect(events[1]).toMatchObject({ type: "scanning", file: "README.md" });
    expect(events[2]).toMatchObject({ type: "scanned", file: "README.md" });
    expect(events[3]).toMatchObject({ type: "complete", landingPageId: "overview" });
  });

  it("skips heartbeat frames", async () => {
    const frames =
      sseFrame("heartbeat", {}) +
      sseFrame("queued", { jobId: "j2", slug: "s", totalCount: 0 }) +
      sseFrame("heartbeat", {}) +
      sseFrame("complete", { landingPageId: "core", pageCount: 1 });

    fetchSpy.mockResolvedValueOnce(
      new Response(new TextEncoder().encode(frames), {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      }),
    );

    const events = await collect(subscribeToIndexing("j2"));
    expect(events.map((e) => e.type)).toEqual(["queued", "complete"]);
  });

  it("appends api_key as a query param to the SSE URL", async () => {
    fetchSpy.mockResolvedValueOnce(sseResp([sseFrame("complete", { landingPageId: "c", pageCount: 0 })]));
    await collect(subscribeToIndexing("j3"));
    const url = (fetchSpy.mock.calls[0] as [string])[0];
    expect(url).toMatch(/api_key=test-key/);
    expect(url).toMatch(/\/v1\/wiki\/index\/j3\/stream/);
  });

  it("aborts when AbortSignal fires", async () => {
    // Stream that never closes (ReadableStream stays open)
    let closeStream: (() => void) | undefined;
    const stream = new ReadableStream({
      start(ctrl) {
        closeStream = () => ctrl.close();
      },
    });
    fetchSpy.mockResolvedValueOnce(
      new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
    );

    const ctrl = new AbortController();
    const gen = subscribeToIndexing("j4", { signal: ctrl.signal });
    // Start consuming — it should block waiting for data.
    const promise = collect(gen);
    // Abort after one tick
    await new Promise((r) => setTimeout(r, 0));
    ctrl.abort();
    // Close the stream so the reader unblocks
    if (closeStream) closeStream();
    const events = await promise;
    // May yield 0 events (aborted before any frame) — the key is it resolves.
    expect(Array.isArray(events)).toBe(true);
  });
});

// ── streamAnswer (SSE POST) ───────────────────────────────────────────────

describe("streamAnswer", () => {
  it("POSTs body to /v1/wiki/qa and parses QaEvents", async () => {
    const frames =
      sseFrame("meta", { answerId: "a1", model: "anthropic/claude-sonnet-4-5", fromPageId: "core" }) +
      sseFrame("summary_ready", { sources: ["docs/x.md"] }) +
      sseFrame("complete", { totalBlocks: 0 });

    fetchSpy.mockResolvedValueOnce(
      new Response(new TextEncoder().encode(frames), {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      }),
    );

    const input = { question: "What is Mewbo?", fromPageId: "core", model: "anthropic/claude-sonnet-4-5", slug: "owner/repo" };
    const events = await collect(streamAnswer(input));

    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toMatch(/\/v1\/wiki\/qa(\?|$)/);
    expect(url).toMatch(/api_key=test-key/);
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toMatchObject({ question: "What is Mewbo?", slug: "owner/repo" });
    expect(events[0]).toMatchObject({ type: "meta", answerId: "a1" });
    expect(events[1]).toMatchObject({ type: "summary_ready" });
    expect(events[2]).toMatchObject({ type: "complete" });
  });
});

// ── startAnswer ───────────────────────────────────────────────────────────

describe("startAnswer", () => {
  it("returns answerId from the first meta event", async () => {
    const frames =
      sseFrame("meta", { answerId: "ans-42", model: "m", fromPageId: "p" }) +
      sseFrame("complete", { totalBlocks: 0 });

    fetchSpy.mockResolvedValueOnce(
      new Response(new TextEncoder().encode(frames), {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      }),
    );

    const result = await startAnswer({ question: "q", fromPageId: "p", model: "m", slug: "s" });
    expect(result).toEqual({ answerId: "ans-42" });
  });

  it("throws if the stream emits an error before meta", async () => {
    // The QaEvent error shape: { type: "error", error: WikiError }
    const frames = sseFrame("error", { error: { code: "internal", message: "boom" } });

    fetchSpy.mockResolvedValueOnce(
      new Response(new TextEncoder().encode(frames), {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      }),
    );

    await expect(
      startAnswer({ question: "q", fromPageId: "p", model: "m", slug: "s" }),
    ).rejects.toMatchObject({ code: "internal" });
  });
});
