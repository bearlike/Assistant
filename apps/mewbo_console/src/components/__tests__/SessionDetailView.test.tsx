/**
 * SessionDetailView — resume() dead-ends + per-turn agent-count scoping.
 *
 * Two defects fixed here:
 *  - Plan approval and question answers unblock a stopped run, but neither
 *    call site woke the events poll back up — `useSessionEvents.resume()`
 *    is the ONLY thing that does, and it was never called from either
 *    handler, so the UI stayed on the pre-answer snapshot until a manual
 *    refresh.
 *  - `runStatus.agents` (the composer's live agent-count) accumulated
 *    `sub_agent` start/stop pairs over the WHOLE session rather than the
 *    current turn, so a `start` with no matching `stop` from a stale, long-
 *    finished turn kept inflating every later turn's count.
 *
 * `ConversationTimeline`/`InputBar`/`WorkspacePanel`/`SessionTriggersSection`
 * are stubbed to isolate SessionDetailView's own wiring (the two facts above)
 * from those components' own rendering rules, which are exercised by their
 * own test suites.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SessionDetailView } from "../SessionDetailView";
import type { EventRecord, SessionSummary } from "../../types";
import * as client from "../../api/client";

vi.mock("../../api/client", () => ({
  fetchEvents: vi.fn(),
  // eslint-disable-next-line @typescript-eslint/no-empty-function
  fetchUsage: vi.fn(() => new Promise(() => {})), // never resolves — untested facet
  postQuery: vi.fn(),
  sendMessage: vi.fn(),
  uploadAttachments: vi.fn(),
  interruptStep: vi.fn(),
  approvePlan: vi.fn().mockResolvedValue(undefined),
  answerQuestion: vi.fn().mockResolvedValue({ ok: true }),
  recoverSession: vi.fn(),
  forkSession: vi.fn(),
  getConfig: vi.fn().mockResolvedValue({ config: {}, secrets: {} }),
}));
vi.mock("../wiki/api/client", () => ({
  getWikiSessionLink: vi.fn(),
  listProjects: vi.fn().mockResolvedValue([]),
}));

vi.mock("../ConversationTimeline", () => ({
  // A mock stands in for the real component's whole prop surface; typing it buys nothing.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  ConversationTimeline: (props: any) => (
    <div data-testid="conversation-timeline">
      <button onClick={() => props.onApprovePlan(true)}>Approve plan</button>
      <button onClick={() => props.onAnswerQuestion("call-1", "tok-1", [])}>
        Answer question
      </button>
    </div>
  ),
}));
vi.mock("../WorkspacePanel", () => ({
  WorkspacePanel: () => <div data-testid="workspace-panel" />,
}));
vi.mock("../triggers/SessionTriggersSection", () => ({
  SessionTriggersSection: () => <div data-testid="triggers-section" />,
}));
vi.mock("../InputBar", () => ({
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- see above.
  InputBar: (props: any) => (
    <div data-testid="input-bar">
      <span data-testid="run-status-agents">{props.runStatus?.agents ?? "none"}</span>
    </div>
  ),
}));

const fetchEvents = vi.mocked(client.fetchEvents);
const approvePlan = vi.mocked(client.approvePlan);
const answerQuestion = vi.mocked(client.answerQuestion);

const baseSession: SessionSummary = {
  session_id: "s1",
  title: "A session",
  status: "running",
};

function renderDetail(session: SessionSummary = baseSession) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const { hook } = memoryLocation({ path: "/s/s1", record: true });
  const ui: ReactElement = (
    <QueryClientProvider client={qc}>
      <Router hook={hook}>
        <SessionDetailView session={session} onBack={vi.fn()} />
      </Router>
    </QueryClientProvider>
  );
  return render(ui);
}

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  fetchEvents.mockResolvedValue({ events: [], running: true, status: "running", done_reason: "" });
});

describe("SessionDetailView — resume() dead-ends", () => {
  // The session is IDLE (`running: false`) here on purpose: the poll's
  // `refetchInterval` sits at the slow keepalive cadence in that state, so
  // fake timers are left un-advanced — the only way a second `fetchEvents`
  // call can happen in these two tests is `resume()` firing synchronously
  // off the click, never the keepalive tick.
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fetchEvents.mockResolvedValue({
      events: [],
      running: false,
      status: "completed",
      done_reason: "completed",
    });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("wakes the poll after a plan-approval decision", async () => {
    renderDetail();
    await waitFor(() => expect(fetchEvents).toHaveBeenCalledTimes(1));

    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.click(screen.getByRole("button", { name: "Approve plan" }));

    await waitFor(() => expect(approvePlan).toHaveBeenCalledWith("s1", true));
    await waitFor(() => expect(fetchEvents.mock.calls.length).toBeGreaterThan(1));
  });

  it("wakes the poll after a question is answered", async () => {
    renderDetail();
    await waitFor(() => expect(fetchEvents).toHaveBeenCalledTimes(1));

    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.click(screen.getByRole("button", { name: "Answer question" }));

    await waitFor(() =>
      expect(answerQuestion).toHaveBeenCalledWith("s1", "call-1", {
        call_token: "tok-1",
        answers: [],
      }),
    );
    await waitFor(() => expect(fetchEvents.mock.calls.length).toBeGreaterThan(1));
  });
});

describe("SessionDetailView — per-turn agent-count scoping", () => {
  function subAgentEvent(ts: string, agentId: string, action: "start" | "stop"): EventRecord {
    return { ts, type: "sub_agent", payload: { agent_id: agentId, action } };
  }
  function userTurn(ts: string, text: string): EventRecord {
    return { ts, type: "user", payload: { text } };
  }

  it("only counts live agents from the CURRENT turn, not a stale never-stopped agent from an earlier one", async () => {
    const events: EventRecord[] = [
      userTurn("2026-01-01T00:00:00Z", "turn 1"),
      subAgentEvent("2026-01-01T00:00:01Z", "stale-agent", "start"), // never stopped
      userTurn("2026-01-01T00:01:00Z", "turn 2"), // turn boundary
      subAgentEvent("2026-01-01T00:01:01Z", "turn2-agent", "start"),
      subAgentEvent("2026-01-01T00:01:02Z", "turn2-agent", "stop"),
    ];
    fetchEvents.mockResolvedValue({
      events,
      running: true,
      status: "running",
      done_reason: "",
    });
    renderDetail();

    const agentsBadge = await screen.findByTestId("run-status-agents");
    // Without the per-turn reset this would read "1" (the stale agent that
    // started in turn 1 and never got a matching stop).
    await waitFor(() => expect(agentsBadge).toHaveTextContent("0"));
  });
});
