/**
 * AssistantTurnFooter — average generation throughput ("N.N tok/s").
 *
 * Σ output_tokens ÷ Σ duration_ms over the turn's root (depth 0) SUCCESSFUL
 * `llm_call_end` events. `duration_ms` is additive on a successful end only
 * (older sessions and failed ends lack it), so these pin the guard: shown
 * only when at least one qualifying event carries a positive `duration_ms`,
 * and neither a failed call nor a sub-agent's own call (depth > 0) may
 * contribute to the ratio.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { AssistantTurnFooter } from "../ConversationTimeline";
import type { EventRecord, TurnMeta } from "../../types";

// The footer's read-aloud button probes the server for speech support, so the
// strip needs a query client in scope — same mock as the sibling footer
// suites, so a speaker button appearing or not can't change these results.
vi.mock("../../api/speech", () => ({
  fetchSpeechCapability: vi.fn().mockResolvedValue({ synthesis: false, transcription: false, maxAudioBytes: null, maxTextChars: null }),
  synthesizeSpeech: vi.fn(),
}));

function queryWrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

afterEach(cleanup);

function llmCallEnd(overrides: Record<string, unknown> = {}): EventRecord {
  return {
    ts: "2026-07-01T08:00:05Z",
    type: "llm_call_end",
    payload: { depth: 0, success: true, ...overrides },
  };
}

function turnWith(events: EventRecord[]): TurnMeta {
  return { id: "turn-1", events, files: [] };
}

function renderFooter(turn: TurnMeta) {
  render(
    <AssistantTurnFooter
      turn={turn}
      onShowTrace={vi.fn()}
      onOpenFiles={vi.fn()}
      responseText="done"
    />,
    { wrapper: queryWrapper() },
  );
}

describe("AssistantTurnFooter — average tok/s", () => {
  it("shows the average when a root successful call carries duration_ms", () => {
    renderFooter(turnWith([llmCallEnd({ output_tokens: 200, duration_ms: 4000 })]));
    // 200 tokens / 4000ms * 1000 = 50.0 tok/s
    expect(screen.getByText("50.0 tok/s")).toBeInTheDocument();
  });

  it("renders nothing when no event carries duration_ms (an old session)", () => {
    renderFooter(turnWith([llmCallEnd({ output_tokens: 200 })]));
    expect(screen.queryByText(/tok\/s/)).not.toBeInTheDocument();
  });

  it("mixed: an event with no duration_ms contributes to neither side of the ratio", () => {
    renderFooter(
      turnWith([
        llmCallEnd({ output_tokens: 100, duration_ms: 2000 }),
        llmCallEnd({ output_tokens: 999 }), // no duration_ms — excluded entirely
      ]),
    );
    // Only the first event counts: 100 / 2000 * 1000 = 50.0 — NOT diluted
    // toward the 999-token call whose duration is unknown.
    expect(screen.getByText("50.0 tok/s")).toBeInTheDocument();
  });

  it("averages across multiple qualifying root calls", () => {
    renderFooter(
      turnWith([
        llmCallEnd({ output_tokens: 100, duration_ms: 2000 }),
        llmCallEnd({ output_tokens: 150, duration_ms: 1000 }),
      ]),
    );
    // (100 + 150) / (2000 + 1000) * 1000 = 83.33…
    expect(screen.getByText("83.3 tok/s")).toBeInTheDocument();
  });

  it("zero-duration guard: a duration_ms of 0 never divides by zero", () => {
    renderFooter(turnWith([llmCallEnd({ output_tokens: 10, duration_ms: 0 })]));
    expect(screen.queryByText(/tok\/s/)).not.toBeInTheDocument();
  });

  it("excludes a failed call even when it carries duration_ms", () => {
    renderFooter(
      turnWith([llmCallEnd({ output_tokens: 100, duration_ms: 2000, success: false })]),
    );
    expect(screen.queryByText(/tok\/s/)).not.toBeInTheDocument();
  });

  it("excludes a sub-agent (depth > 0) call from the average", () => {
    renderFooter(
      turnWith([llmCallEnd({ output_tokens: 100, duration_ms: 2000, depth: 1 })]),
    );
    expect(screen.queryByText(/tok\/s/)).not.toBeInTheDocument();
  });
});
