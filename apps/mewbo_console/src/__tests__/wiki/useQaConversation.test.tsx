/**
 * Unit tests for `useQaConversation` — the hook `QAScreen.tsx` extracted its
 * stream/snapshot join, URL-fold effect, and multi-turn stacking into (see
 * `CLAUDE.md` → "Idempotent Q&A URL" / "Multi-turn follow-up"). These drive the
 * hook directly via `renderHook` rather than mounting the two-column layout —
 * `qaScreenFollowup.test.tsx` / `qaScreenIdempotent.test.tsx` already cover the
 * same guarantees end-to-end through the rendered screen; these pin the hook's
 * OWN contract so a future screen change can't silently break it unnoticed.
 */
import type { ReactNode } from "react";

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";

import { useQaConversation } from "@/components/wiki/useQaConversation";
import * as wikiClient from "@/components/wiki/api/client";
import type { QaAnswer, QaEvent } from "@/components/wiki/api/types";

vi.mock("@/components/wiki/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/wiki/api/client")>();
  return {
    ...actual,
    getAnswer: vi.fn(),
    streamAnswer: vi.fn(),
    getPage: vi.fn(),
  };
});
// `useStoredModel` (used for the QADock model picker) calls `useModels()`,
// which otherwise fires a real `/api/models` fetch — keep it inert like the
// sibling QAScreen render tests do.
vi.mock("@/hooks/useModels", () => ({
  useModels: () => ({
    models: [],
    defaultModel: "",
    capabilities: {},
    loading: false,
    error: null,
    refresh: vi.fn(),
  }),
}));

const getAnswer = vi.mocked(wikiClient.getAnswer);
const streamAnswer = vi.mocked(wikiClient.streamAnswer);
const getPage = vi.mocked(wikiClient.getPage);

function makeAnswer(answerId: string, extra: Partial<QaAnswer> = {}): QaAnswer {
  return {
    answerId,
    fromPageId: "core",
    summarySources: [],
    model: "anthropic/claude-sonnet-4-5",
    blocks: [],
    accessedSources: [],
    modelsUsed: [],
    ...extra,
  };
}

async function* streamTurn(answerId: string, answer: string): AsyncGenerator<QaEvent> {
  yield { type: "meta", answerId, model: "m", fromPageId: "core" };
  yield { type: "block_open", index: 0, block: { kind: "p", text: "" } };
  yield { type: "block_delta", index: 0, textAppend: answer };
  yield { type: "complete", totalBlocks: 1 };
}

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const loc = memoryLocation({ path: "/wiki/qa", record: true });
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
  getPage.mockResolvedValue(null);
});

describe("useQaConversation", () => {
  it("cold ask: streams once, folds the minted id into the URL exactly once", async () => {
    streamAnswer.mockImplementation(() => streamTurn("ans1", "ANSWER"));
    getAnswer.mockResolvedValue(makeAnswer("ans1"));
    const { wrapper, loc } = makeWrapper();

    const { result } = renderHook(
      () => useQaConversation({ question: "q1", pageId: "core", slug: "o/r" }),
      { wrapper },
    );

    await waitFor(() =>
      expect(result.current.renderedTurns.at(-1)?.done).toBe(true),
    );
    expect(streamAnswer).toHaveBeenCalledTimes(1);
    expect(streamAnswer.mock.calls[0][0].answerId).toBeUndefined();

    await waitFor(() =>
      expect(loc.history[loc.history.length - 1]).toContain("answer=ans1"),
    );
    // The fold effect is gated on the `answerId` prop, not on re-renders — it
    // must not re-navigate on every subsequent state settle.
    const foldedCount = loc.history.filter((h) => h.includes("answer=ans1")).length;
    expect(foldedCount).toBe(1);
  });

  it("snapshot mode (answerId prop present): reads once, never streams", async () => {
    getAnswer.mockResolvedValue(makeAnswer("ans1", { blocks: [{ kind: "p", text: "SAVED" }] }));
    const { wrapper } = makeWrapper();

    const { result } = renderHook(
      () => useQaConversation({ question: "q1", pageId: "core", slug: "o/r", answerId: "ans1" }),
      { wrapper },
    );

    await waitFor(() =>
      expect(result.current.renderedTurns.at(-1)?.blocks).toEqual([{ kind: "p", text: "SAVED" }]),
    );
    expect(streamAnswer).not.toHaveBeenCalled();
    expect(getAnswer).toHaveBeenCalledTimes(1);
  });

  it("follow-up reuses the same answerId and stacks both turns", async () => {
    streamAnswer.mockImplementation((input) =>
      input.answerId ? streamTurn("ans1", "SECOND") : streamTurn("ans1", "FIRST"),
    );
    getAnswer.mockResolvedValue(makeAnswer("ans1"));
    const { wrapper } = makeWrapper();

    const { result } = renderHook(
      () => useQaConversation({ question: "first", pageId: "core", slug: "o/r" }),
      { wrapper },
    );

    await waitFor(() => expect(result.current.renderedTurns.at(-1)?.done).toBe(true));
    expect(result.current.renderedTurns).toHaveLength(1);

    act(() => result.current.onAsk("second"));

    await waitFor(() => expect(streamAnswer).toHaveBeenCalledTimes(2));
    expect(streamAnswer.mock.calls[1][0]).toMatchObject({
      question: "second",
      answerId: "ans1",
    });
    await waitFor(() => expect(result.current.renderedTurns).toHaveLength(2));
    expect(result.current.renderedTurns[0].question).toBe("first");
    expect(result.current.renderedTurns[1].question).toBe("second");
  });

  it("onAsk is a no-op while the active turn hasn't settled", async () => {
    let release: (() => void) | undefined;
    const gate = new Promise<void>((r) => (release = r));
    streamAnswer.mockImplementationOnce(async function* () {
      yield { type: "meta", answerId: "ans1", model: "m", fromPageId: "core" };
      await gate;
    });
    const { wrapper } = makeWrapper();

    const { result } = renderHook(
      () => useQaConversation({ question: "q1", pageId: "core", slug: "o/r" }),
      { wrapper },
    );

    await waitFor(() => expect(streamAnswer).toHaveBeenCalledTimes(1));
    expect(result.current.renderedTurns.at(-1)?.done).toBe(false);

    act(() => result.current.onAsk("too soon"));
    expect(streamAnswer).toHaveBeenCalledTimes(1);

    release?.();
  });
});
