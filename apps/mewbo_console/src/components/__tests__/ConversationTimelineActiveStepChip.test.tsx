/**
 * ConversationTimeline — the active row's live per-step chip.
 *
 * Elapsed since the current step's root `llm_call_start` (threaded down as
 * `activeStepStartTs`), plus an honest `≈ N tok/s` reading gated to the
 * Streaming phase with `tokPerSec > 0` — both sourced from the SAME
 * `useThroughput` estimator `RunTelemetry` already renders (`SessionDetailView`
 * threads it here too, never a second estimator).
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ConversationTimeline } from "../ConversationTimeline";
import type { TimelineEntry } from "../../types";

// jsdom lacks IntersectionObserver — TurnScroller observes rows on mount.
class IntersectionObserverStub {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
  takeRecords = vi.fn(() => []);
}
globalThis.IntersectionObserver =
  globalThis.IntersectionObserver ??
  (IntersectionObserverStub as unknown as typeof IntersectionObserver);

const T0 = "2026-01-01T00:00:00.000Z";
// 5s before "now" so `useElapsed`'s initial synchronous render reads "5s".
const STEP_START = new Date(Date.parse(T0) - 5000).toISOString();

const userTurn: TimelineEntry[] = [
  {
    id: "user-1",
    role: "user",
    content: "do the thing",
    turnId: "turn-1",
    ts: "2026-01-01T00:00:00Z",
  },
];

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(T0));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function renderActiveRow(opts: {
  streamingText?: string;
  activePhase?: string;
  activeTokPerSec?: number;
  activeStepStartTs?: string;
}) {
  render(
    <ConversationTimeline
      timeline={userTurn}
      onShowTrace={vi.fn()}
      onOpenFiles={vi.fn()}
      activeTurnId="turn-1"
      isRunning
      streamingText={opts.streamingText ?? ""}
      activePhase={opts.activePhase}
      activeTokPerSec={opts.activeTokPerSec}
      activeStepStartTs={opts.activeStepStartTs}
    />,
  );
}

describe("ConversationTimeline — active row per-step chip", () => {
  it("Streaming + tokPerSec > 0 shows elapsed AND the ≈ rate (streaming row)", () => {
    renderActiveRow({
      streamingText: "hi",
      activePhase: "Streaming",
      activeTokPerSec: 42,
      activeStepStartTs: STEP_START,
    });
    expect(screen.getByText("5s · ≈ 42 tok/s")).toBeInTheDocument();
  });

  it("Reasoning shows elapsed only, even with a nonzero tokPerSec (pending row)", () => {
    renderActiveRow({
      activePhase: "Reasoning",
      activeTokPerSec: 42,
      activeStepStartTs: STEP_START,
    });
    expect(screen.getByText("5s")).toBeInTheDocument();
    expect(screen.queryByText(/tok\/s/)).not.toBeInTheDocument();
  });

  it("Running tool shows elapsed only", () => {
    renderActiveRow({
      activePhase: "Running tool",
      activeTokPerSec: 42,
      activeStepStartTs: STEP_START,
    });
    expect(screen.getByText("5s")).toBeInTheDocument();
    expect(screen.queryByText(/tok\/s/)).not.toBeInTheDocument();
  });

  it("Streaming with tokPerSec of 0 hides the rate", () => {
    renderActiveRow({
      streamingText: "hi",
      activePhase: "Streaming",
      activeTokPerSec: 0,
      activeStepStartTs: STEP_START,
    });
    expect(screen.getByText("5s")).toBeInTheDocument();
    expect(screen.queryByText(/tok\/s/)).not.toBeInTheDocument();
  });

  it("renders no chip at all with no step start ts — the 'Working' beat still shows", () => {
    renderActiveRow({ activePhase: "Reasoning", activeTokPerSec: 42 });
    expect(screen.getByText("Working")).toBeInTheDocument();
    expect(screen.queryByText(/tok\/s/)).not.toBeInTheDocument();
    expect(screen.queryByText("5s")).not.toBeInTheDocument();
  });
});
