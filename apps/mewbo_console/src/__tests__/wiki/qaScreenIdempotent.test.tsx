/**
 * QAScreen snapshot-vs-stream branch — the idempotent ``?answer=<id>`` URL.
 *
 * Headline guarantee: refreshing a completed answer (an ``answerId`` already in
 * the URL) reads the persisted snapshot with exactly ONE GET and NEVER POSTs a
 * fresh Q&A run. Conversely, a cold ask streams (POST) and then folds the
 * freshly-assigned id into the URL so the NEXT refresh is idempotent.
 */
import type { ReactNode } from "react";

import { cleanup, render, waitFor } from "@testing-library/react";
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
// Keep the model picker inert (no /api/models fetch during the screen render).
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

/** An SSE generator that assigns an id (on `meta`) then completes. */
async function* metaThenComplete(): AsyncGenerator<QaEvent> {
  yield { type: "meta", answerId: "ansX", model: "m", fromPageId: "core" };
  yield { type: "complete", totalBlocks: 0 };
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

describe("QAScreen — idempotent ?answer= refresh", () => {
  it("reads the snapshot with ONE GET and zero POST when answerId is present", async () => {
    getAnswer.mockResolvedValue(makeAnswer("ans1"));
    const { wrapper } = makeWrapper();

    render(
      <QAScreen question="how does it work" pageId="core" slug="o/r" answerId="ans1" />,
      { wrapper },
    );

    await waitFor(() => expect(getAnswer).toHaveBeenCalledWith("ans1"));
    // The whole point: no live Q&A run is started on a refresh.
    expect(streamAnswer).not.toHaveBeenCalled();
    expect(getAnswer).toHaveBeenCalledTimes(1);
  });

  it("streams (POST) when no answerId, then folds the new id into the URL", async () => {
    streamAnswer.mockImplementation(() => metaThenComplete());
    getAnswer.mockResolvedValue(makeAnswer("ansX"));
    const { wrapper, loc } = makeWrapper();

    render(<QAScreen question="what is the engine" pageId="core" slug="o/r" />, {
      wrapper,
    });

    // A run is opened, and once `meta` lands the id is replace-navigated in.
    await waitFor(() =>
      expect(loc.history[loc.history.length - 1]).toContain("answer=ansX"),
    );
    expect(streamAnswer).toHaveBeenCalledTimes(1);
  });
});
