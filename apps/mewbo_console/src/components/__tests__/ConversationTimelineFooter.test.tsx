/**
 * ConversationTimeline — the single assistant-turn footer strip.
 *
 * The turn footer was consolidated into ONE hover/focus-revealed strip: the
 * persistent copy button that used to sit beneath the message is gone, copy now
 * lives in the footer's right-hand action cluster (`Copy response`), and the
 * left cluster carries a NEW human-readable generation timestamp sourced from
 * the turn's closing completion event (`ts` IS the message id). These tests pin
 * that contract — the relocated copy, the absent bubble copy, and the timestamp.
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

const COMPLETION_TS = "2026-07-01T08:00:05Z";

function completedTurn(): TimelineEntry[] {
  return [
    {
      id: "user-1",
      role: "user",
      content: "do the thing",
      turnId: "turn-1",
      ts: "2026-07-01T08:00:00Z",
    },
    {
      id: "assistant-1",
      role: "assistant",
      content: "done",
      turnId: "turn-1",
      turn: {
        id: "turn-1",
        events: [{ ts: COMPLETION_TS, type: "completion", payload: {} }],
        files: [],
      },
    },
  ];
}

// Assistant turn with no preceding user row — isolates the assistant bubble so
// a "Copy" assertion can't collide with the (deliberately kept) user-row copy.
function assistantOnlyTurn(): TimelineEntry[] {
  return [
    {
      id: "assistant-1",
      role: "assistant",
      content: "done",
      turnId: "turn-1",
      turn: {
        id: "turn-1",
        events: [{ ts: COMPLETION_TS, type: "completion", payload: {} }],
        files: [],
      },
    },
  ];
}

function renderTimeline(timeline: TimelineEntry[] = completedTurn()) {
  render(
    <ConversationTimeline
      timeline={timeline}
      onShowTrace={vi.fn()}
      onOpenFiles={vi.fn()}
      onRetryFrom={vi.fn()}
      onForkFrom={vi.fn()}
      onForkSession={vi.fn()}
    />,
  );
}

describe("ConversationTimeline — assistant turn footer strip", () => {
  it("relocates copy into the footer action cluster (Copy response)", () => {
    renderTimeline();
    expect(
      screen.getByRole("button", { name: "Copy response" }),
    ).toBeInTheDocument();
  });

  it("no longer renders the assistant bubble's own persistent copy beneath the message", () => {
    // Assistant-only render: the (kept) user-row copy is also named "Copy", so
    // exclude it to prove the assistant BUBBLE copy is what got relocated.
    renderTimeline(assistantOnlyTurn());
    // The old bubble copy carried the default accessible name "Copy"; the
    // relocated footer copy is named "Copy response". Nothing should be named
    // exactly "Copy" any more.
    expect(
      screen.queryByRole("button", { name: /^Copy$/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Copy response" }),
    ).toBeInTheDocument();
  });

  it("shows a generation timestamp sourced from the closing completion ts", () => {
    renderTimeline();
    // Full ISO on title + dateTime; the visible text is the human-readable form.
    const ts = screen.getByTitle(COMPLETION_TS);
    expect(ts.tagName).toBe("TIME");
    expect(ts).toHaveAttribute("dateTime", COMPLETION_TS);
    expect(ts.textContent && ts.textContent.length).toBeGreaterThan(0);
  });
});
