/**
 * ConversationTimeline — turn overflow menu vocabulary + call args.
 *
 * The assistant-turn "⋯" menu offers three recovery actions: "Retry from
 * here" (same session, truncate + re-run), "Branch in new chat" (fork at
 * this point, navigate to the new session) and "Fork session" (fork the
 * whole transcript, no `fromTs`). These tests pin the exact labels and the
 * exact request args each one drives, plus the isRunning gate that hides
 * all three mid-run.
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { ConversationTimeline } from "../ConversationTimeline";
import type { TimelineEntry } from "../../types";

// The footer's read-aloud button probes the server for speech support, so the
// strip now needs a query client in scope. Answering "no speech" keeps these
// tests pinned to the overflow menu they were written against.
vi.mock("../../api/speech", () => ({
  fetchSpeechCapability: vi.fn().mockResolvedValue({ synthesis: false, transcription: false, maxAudioBytes: null, maxTextChars: null }),
  synthesizeSpeech: vi.fn(),
}));

/** A fresh client per render, so no probe result leaks between tests. */
function queryWrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

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

const FROM_TS = "2026-07-01T08:00:05Z";

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
        events: [{ ts: FROM_TS, type: "completion", payload: {} }],
        files: [],
      },
    },
  ];
}

async function openTurnMenu() {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "More turn actions" }));
  const menu = await screen.findByRole("menu");
  return { user, menu };
}

describe("ConversationTimeline — turn overflow menu (retry / branch / fork)", () => {
  it('"Retry from here" calls onRetryFrom with the turn\'s fromTs', async () => {
    const onRetryFrom = vi.fn();
    render(
      <ConversationTimeline
        timeline={completedTurn()}
        onShowTrace={vi.fn()}
        onOpenFiles={vi.fn()}
        onRetryFrom={onRetryFrom}
        onForkFrom={vi.fn()}
        onForkSession={vi.fn()}
      />,
      { wrapper: queryWrapper() },
    );
    const { user, menu } = await openTurnMenu();
    await user.click(
      within(menu).getByRole("menuitem", { name: "Retry from here" }),
    );
    expect(onRetryFrom).toHaveBeenCalledWith(FROM_TS);
  });

  it('"Branch in new chat" calls onForkFrom WITH the turn\'s fromTs', async () => {
    const onForkFrom = vi.fn();
    render(
      <ConversationTimeline
        timeline={completedTurn()}
        onShowTrace={vi.fn()}
        onOpenFiles={vi.fn()}
        onRetryFrom={vi.fn()}
        onForkFrom={onForkFrom}
        onForkSession={vi.fn()}
      />,
      { wrapper: queryWrapper() },
    );
    const { user, menu } = await openTurnMenu();
    await user.click(
      within(menu).getByRole("menuitem", { name: "Branch in new chat" }),
    );
    expect(onForkFrom).toHaveBeenCalledWith(FROM_TS);
  });

  it('"Fork session" calls onForkSession WITHOUT a fromTs (whole transcript)', async () => {
    const onForkSession = vi.fn();
    render(
      <ConversationTimeline
        timeline={completedTurn()}
        onShowTrace={vi.fn()}
        onOpenFiles={vi.fn()}
        onRetryFrom={vi.fn()}
        onForkFrom={vi.fn()}
        onForkSession={onForkSession}
      />,
      { wrapper: queryWrapper() },
    );
    const { user, menu } = await openTurnMenu();
    await user.click(
      within(menu).getByRole("menuitem", { name: "Fork session" }),
    );
    expect(onForkSession).toHaveBeenCalledWith();
  });

  it("hides all three recovery actions while the session is running", async () => {
    render(
      <ConversationTimeline
        timeline={completedTurn()}
        onShowTrace={vi.fn()}
        onOpenFiles={vi.fn()}
        isRunning
        onRetryFrom={vi.fn()}
        onForkFrom={vi.fn()}
        onForkSession={vi.fn()}
      />,
      { wrapper: queryWrapper() },
    );
    const { menu } = await openTurnMenu();
    expect(
      within(menu).queryByRole("menuitem", { name: "Retry from here" }),
    ).not.toBeInTheDocument();
    expect(
      within(menu).queryByRole("menuitem", { name: "Branch in new chat" }),
    ).not.toBeInTheDocument();
    expect(
      within(menu).queryByRole("menuitem", { name: "Fork session" }),
    ).not.toBeInTheDocument();
  });
});
