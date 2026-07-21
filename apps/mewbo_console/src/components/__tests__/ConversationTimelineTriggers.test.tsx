/**
 * ConversationTimeline — coalesced trigger rows.
 *
 * A repeating trigger (e.g. `time.cron`) fires once per tick, and each fire
 * used to render its own "Cron schedule fired" row — a scroll-heavy wall for
 * a trigger that fires often. `coalesceAdjacentTriggers` (colocated in
 * `components/triggers/coalesceTriggers.ts`) folds adjacent same-identity
 * fires into one row before the timeline is mapped to JSX; these tests pin
 * the rendered copy for both the grouped and ungrouped cases.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

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

afterEach(cleanup);

function renderTimeline(timeline: TimelineEntry[]) {
  render(<ConversationTimeline timeline={timeline} onShowTrace={vi.fn()} onOpenFiles={vi.fn()} />);
}

describe("ConversationTimeline — coalesced trigger rows", () => {
  it('renders three adjacent cron fires as one "×3 times" row', () => {
    const timeline: TimelineEntry[] = [
      {
        id: "trigger-fired-1",
        role: "trigger",
        content: "",
        turnId: "triggers",
        ts: "2026-07-19T08:00:00Z",
        trigger: { triggerId: "cron-1", kind: "time.cron", action: "fired" },
      },
      {
        id: "trigger-fired-2",
        role: "trigger",
        content: "",
        turnId: "triggers",
        ts: "2026-07-19T08:01:00Z",
        trigger: { triggerId: "cron-1", kind: "time.cron", action: "fired" },
      },
      {
        id: "trigger-fired-3",
        role: "trigger",
        content: "",
        turnId: "triggers",
        ts: "2026-07-19T08:02:00Z",
        trigger: { triggerId: "cron-1", kind: "time.cron", action: "fired" },
      },
    ];
    renderTimeline(timeline);
    expect(screen.getByText("Cron schedule fired ×3 times")).toBeInTheDocument();
    expect(screen.queryByText("Cron schedule fired")).not.toBeInTheDocument();
  });

  it("renders a lone fire with no ×N suffix (N=1 passthrough)", () => {
    const timeline: TimelineEntry[] = [
      {
        id: "trigger-fired-1",
        role: "trigger",
        content: "",
        turnId: "triggers",
        ts: "2026-07-19T08:00:00Z",
        trigger: { triggerId: "cron-1", kind: "time.cron", action: "fired" },
      },
    ];
    renderTimeline(timeline);
    expect(screen.getByText("Cron schedule fired")).toBeInTheDocument();
  });

  it("keeps two fires separated by a user turn as two distinct rows", () => {
    const timeline: TimelineEntry[] = [
      {
        id: "trigger-fired-1",
        role: "trigger",
        content: "",
        turnId: "triggers",
        ts: "2026-07-19T08:00:00Z",
        trigger: { triggerId: "cron-1", kind: "time.cron", action: "fired" },
      },
      {
        id: "user-1",
        role: "user",
        content: "check on it",
        turnId: "turn-1",
        ts: "2026-07-19T08:00:30Z",
      },
      {
        id: "trigger-fired-2",
        role: "trigger",
        content: "",
        turnId: "triggers",
        ts: "2026-07-19T08:01:00Z",
        trigger: { triggerId: "cron-1", kind: "time.cron", action: "fired" },
      },
    ];
    renderTimeline(timeline);
    expect(screen.getAllByText("Cron schedule fired")).toHaveLength(2);
  });
});
