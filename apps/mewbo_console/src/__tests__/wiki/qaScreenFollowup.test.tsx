/**
 * QAScreen multi-turn follow-up.
 *
 * A follow-up must NOT navigate to a fresh route (the earlier behaviour that
 * discarded context). Instead it reuses the SAME ``answerId`` (backend
 * continues the session, appending a turn) and stacks both turns in the one
 * screen. This test drives a cold ask, submits a follow-up via the dock, and
 * asserts: the second POST carries the first turn's ``answerId``; the URL never
 * changes to the follow-up question; and both turns render.
 */
import type { ReactNode } from "react";

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";

import { QAScreen } from "@/components/wiki/QAScreen";
import * as wikiClient from "@/components/wiki/api/client";
import type { QaAnswer, QaEvent } from "@/components/wiki/api/types";

vi.mock("@/components/wiki/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/wiki/api/client")>();
  return {
    ...actual,
    getAnswer: vi.fn(),
    streamAnswer: vi.fn(),
    getPage: vi.fn(),
    getSourceExcerpt: vi.fn(),
  };
});
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

function makeAnswer(answerId: string): QaAnswer {
  return {
    answerId,
    fromPageId: "core",
    summarySources: [],
    model: "anthropic/claude-sonnet-4-5",
    blocks: [],
    accessedSources: [],
    modelsUsed: [],
  };
}

/** A turn's SSE stream: assign the (shared) id, paint one paragraph, complete. */
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
  getAnswer.mockResolvedValue(makeAnswer("ans1"));
  // The follow-up's POST carries answerId; the cold ask does not.
  streamAnswer.mockImplementation((input) =>
    input.answerId
      ? streamTurn("ans1", "SECOND ANSWER")
      : streamTurn("ans1", "FIRST ANSWER"),
  );
});

describe("QAScreen — multi-turn follow-up", () => {
  it("reuses the same answerId, stays on the route, and stacks both turns", async () => {
    const { wrapper, loc } = makeWrapper();

    render(<QAScreen question="first question" pageId="core" slug="o/r" />, { wrapper });

    // Turn 1 streams (cold ask — no answerId in the POST body).
    await screen.findByText("FIRST ANSWER");
    expect(streamAnswer).toHaveBeenCalledTimes(1);
    expect(streamAnswer.mock.calls[0][0].answerId).toBeUndefined();

    // The cold id folds into the URL; the snapshot GET fires once the turn is
    // done — waiting on it guarantees the turn has settled before we follow up.
    await waitFor(() => expect(getAnswer).toHaveBeenCalledWith("ans1"));

    // Ask a follow-up via the dock.
    const box = screen.getByPlaceholderText("Ask a follow-up question");
    fireEvent.change(box, { target: { value: "second question" } });
    fireEvent.click(screen.getByLabelText("Ask question"));

    // Turn 2 streams on the SAME answerId — a continuation, not a new answer.
    await screen.findByText("SECOND ANSWER");
    expect(streamAnswer).toHaveBeenCalledTimes(2);
    expect(streamAnswer.mock.calls[1][0]).toMatchObject({
      question: "second question",
      answerId: "ans1",
    });

    // Both turns are stacked in the one screen.
    expect(screen.getByText("first question")).toBeInTheDocument();
    expect(screen.getByText("second question")).toBeInTheDocument();
    expect(screen.getByText("FIRST ANSWER")).toBeInTheDocument();

    // The URL never navigated away to the follow-up question — the same
    // answerId still addresses the whole growing conversation.
    const url = loc.history[loc.history.length - 1];
    expect(url).toContain("answer=ans1");
    expect(url).not.toContain("second");
  });

  it("ignores a follow-up submitted before the active turn has settled", async () => {
    // A stream that assigns an id but never completes — the turn stays in-flight.
    let release: (() => void) | undefined;
    const gate = new Promise<void>((r) => (release = r));
    streamAnswer.mockImplementationOnce(async function* () {
      yield { type: "meta", answerId: "ans1", model: "m", fromPageId: "core" };
      yield { type: "block_open", index: 0, block: { kind: "p", text: "" } };
      yield { type: "block_delta", index: 0, textAppend: "STREAMING" };
      await gate; // hold the stream open (never completes)
    });
    const { wrapper } = makeWrapper();

    render(<QAScreen question="q1" pageId="core" slug="o/r" />, { wrapper });
    await screen.findByText("STREAMING");
    expect(streamAnswer).toHaveBeenCalledTimes(1);

    // Submitting mid-stream must be a no-op (the active turn hasn't settled).
    const box = screen.getByPlaceholderText("Ask a follow-up question");
    fireEvent.change(box, { target: { value: "too soon" } });
    fireEvent.click(screen.getByLabelText("Ask question"));
    expect(streamAnswer).toHaveBeenCalledTimes(1);

    release?.();
  });
});
