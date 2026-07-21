/**
 * ConversationTimeline — the Show-Traces regression guard.
 *
 * Commit 9e29fc5 flipped the in-flight row to `StreamingAssistantRow`
 * the instant the first token streamed, and that row had no Trace pill — so
 * mid-run access to the logs panel vanished for the rest of the turn. The fix
 * extracts a shared `<TracePill>` and mounts it on BOTH in-flight rows. These
 * tests assert the pill is reachable whether or not tokens have streamed.
 */
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

const userTurn: TimelineEntry[] = [
  {
    id: "user-1",
    role: "user",
    content: "do the thing",
    turnId: "turn-1",
    ts: "2026-07-01T08:00:00Z",
  },
];

function renderTimeline(streamingText: string, onShowActiveTrace = vi.fn()) {
  render(
    <ConversationTimeline
      timeline={userTurn}
      onShowTrace={vi.fn()}
      onOpenFiles={vi.fn()}
      activeTurnId="turn-1"
      isRunning
      streamingText={streamingText}
      onShowActiveTrace={onShowActiveTrace}
    />,
  );
  return onShowActiveTrace;
}

describe("ConversationTimeline — Trace pill on in-flight rows", () => {
  it("shows the Trace pill before any token streams (pending row)", () => {
    renderTimeline("");
    expect(screen.getByTitle("Open trace")).toBeInTheDocument();
  });

  it("KEEPS the Trace pill once streaming begins (the streaming regression)", async () => {
    const spy = renderTimeline("Hello, I am streaming a live answer…");
    const pill = screen.getByTitle("Open trace");
    expect(pill).toBeInTheDocument();
    // And it still routes to the live-trace handler.
    await userEvent.click(pill);
    expect(spy).toHaveBeenCalledTimes(1);
  });
});
