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
import * as sessionStreamApi from "../../api/sessionStream";

vi.mock("../../api/client", () => ({
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
  listProjects: vi.fn().mockResolvedValue([]),
}));
// `streamSession` is the only export replaced — `isSessionStateFrame` /
// `isStreamEndFrame` stay real so `useSessionEvents`'s own frame-folding logic
// (which imports them directly) keeps working against the scripted frames
// below.
vi.mock("../../api/sessionStream", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/sessionStream")>();
  return {
    ...actual,
    streamSession: vi.fn(),
  };
});
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
      <button onClick={() => props.onAnswerQuestion("call-1", "tok-1", [], "a note")}>
        Answer with notes
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
      <button onClick={() => props.onSubmit("follow up", { model: "fresh-model" })}>
        Submit follow-up
      </button>
    </div>
  ),
}));

const streamSession = vi.mocked(sessionStreamApi.streamSession);
const approvePlan = vi.mocked(client.approvePlan);
const answerQuestion = vi.mocked(client.answerQuestion);
const postQuery = vi.mocked(client.postQuery);

/**
 * Scripts the SSE transport for one `streamSession` call: replays `frames`
 * in order, then either ends (mirroring the server's `stream_end`) or hangs
 * forever (mirroring a still-open, still-running connection) depending on
 * `pending`.
 */
function frameStream(
  frames: sessionStreamApi.SessionStreamFrame[],
  pending = false,
): AsyncGenerator<sessionStreamApi.SessionStreamFrame> {
  async function* generator() {
    for (const frame of frames) {
      yield frame;
    }
    if (pending) {
      // A still-running stream never returns: the server holds the connection
      // open for the life of the run, so the generator must not complete or
      // the hook would treat it as a healthy close and re-subscribe.
      await new Promise<never>(() => undefined);
    }
  }
  return generator();
}

function sessionStateFrame(
  overrides: Partial<Omit<sessionStreamApi.SessionStateFrame, "type">> = {},
): sessionStreamApi.SessionStateFrame {
  return {
    type: "session_state",
    running: false,
    status: "",
    done_reason: "",
    title: "",
    recoverable: false,
    terminated: false,
    terminated_at: null,
    ...overrides,
  };
}

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
  streamSession.mockImplementation(() =>
    frameStream([sessionStateFrame({ running: true, status: "running" })], true),
  );
});

describe("SessionDetailView — follow-up context", () => {
  it("does not replay an app-bound project's persisted context", async () => {
    const user = userEvent.setup();
    streamSession.mockImplementation(() =>
      frameStream([sessionStateFrame({ running: false, status: "completed" })]),
    );
    renderDetail({
      ...baseSession,
      status: "completed",
      running: false,
      context: {
        app_id: "llm-model-compare-upgraded",
        project: "app-89f9c9479143",
        model: "stored-model",
      },
    });

    await user.click(screen.getByRole("button", { name: "Submit follow-up" }));

    expect(postQuery).toHaveBeenCalledWith(
      "s1",
      "follow up",
      { model: "fresh-model" },
      undefined,
      undefined,
    );
  });
});

describe("SessionDetailView — resume() dead-ends", () => {
  // The session is IDLE (`running: false`) here on purpose: `useSessionEvents`
  // only reconnects after the multi-second `RESUBSCRIBE_MS` delay once a
  // stream closes, so with fake timers left un-advanced the only way a second
  // `streamSession` call can happen in these tests is `resume()` bumping the
  // epoch synchronously off the click — never the reconnect delay expiring.
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    streamSession.mockImplementation(() =>
      frameStream([
        sessionStateFrame({ status: "completed", done_reason: "completed" }),
        { type: "stream_end" },
      ]),
    );
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("wakes the stream after a plan-approval decision", async () => {
    renderDetail();
    await waitFor(() => expect(streamSession).toHaveBeenCalledTimes(1));

    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.click(screen.getByRole("button", { name: "Approve plan" }));

    await waitFor(() => expect(approvePlan).toHaveBeenCalledWith("s1", true));
    await waitFor(() => expect(streamSession.mock.calls.length).toBeGreaterThan(1));
  });

  it("wakes the stream after a question is answered", async () => {
    renderDetail();
    await waitFor(() => expect(streamSession).toHaveBeenCalledTimes(1));

    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.click(screen.getByRole("button", { name: "Answer question" }));

    await waitFor(() =>
      expect(answerQuestion).toHaveBeenCalledWith("s1", "call-1", {
        call_token: "tok-1",
        answers: [],
      }),
    );
    await waitFor(() => expect(streamSession.mock.calls.length).toBeGreaterThan(1));
  });

  it("passes the optional notes through, and omits the key when there are none", async () => {
    renderDetail();
    await waitFor(() => expect(streamSession).toHaveBeenCalledTimes(1));

    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.click(screen.getByRole("button", { name: "Answer question" }));
    await user.click(screen.getByRole("button", { name: "Answer with notes" }));

    await waitFor(() => expect(answerQuestion).toHaveBeenCalledTimes(2));
    // Blank notes send no `notes` key at all — an empty string is not a note,
    // and the server contract forbids one.
    expect(answerQuestion.mock.calls[0][2]).not.toHaveProperty("notes");
    expect(answerQuestion.mock.calls[1][2]).toEqual({
      call_token: "tok-1",
      answers: [],
      notes: "a note",
    });
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
    streamSession.mockImplementation(() =>
      frameStream([...events, sessionStateFrame({ running: true, status: "running" })], true),
    );
    renderDetail();

    const agentsBadge = await screen.findByTestId("run-status-agents");
    // Without the per-turn reset this would read "1" (the stale agent that
    // started in turn 1 and never got a matching stop).
    await waitFor(() => expect(agentsBadge).toHaveTextContent("0"));
  });
});

/**
 * The title sync is a WRITE performed from an effect, and a write from an
 * effect must be bounded by what it already did — never by state another
 * layer is expected to echo back.
 *
 * This is the regression guard for a crash that took the whole console down
 * ("Maximum update depth exceeded") on every direct visit to a session page:
 * `onTitleUpdate` is an inline arrow, so it changes identity every render and
 * re-runs the effect; the old guard compared against `session.title`, which on
 * a direct visit is the placeholder row the route mounts while the listing
 * loads — so it never went false and the write repeated until React gave up.
 *
 * The placeholder is reproduced exactly here: `title: ""`, never updated.
 */
describe("SessionDetailView title sync", () => {
  it("pushes a title_update once, even as onTitleUpdate changes identity every render", async () => {
    streamSession.mockImplementation(() =>
      frameStream(
        [
          {
            type: "title_update",
            ts: "2026-01-01T00:00:00Z",
            payload: { title: "Derived title" },
          } as unknown as sessionStreamApi.SessionStreamFrame,
          sessionStateFrame(),
        ],
        true,
      ),
    );

    const spy = vi.fn();
    const placeholder: SessionSummary = { session_id: "s1", title: "" };
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const { hook } = memoryLocation({ path: "/s/s1", record: true });
    // A NEW arrow every render — the identity churn that re-ran the effect.
    const ui = (): ReactElement => (
      <QueryClientProvider client={qc}>
        <Router hook={hook}>
          <SessionDetailView
            session={placeholder}
            onBack={vi.fn()}
            onTitleUpdate={(id, title) => spy(id, title)}
          />
        </Router>
      </QueryClientProvider>
    );

    const { rerender } = render(ui());
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    expect(spy).toHaveBeenCalledWith("s1", "Derived title");

    // `session.title` deliberately stays "" — the placeholder never resolves,
    // which is precisely the state the old guard could not escape.
    rerender(ui());
    rerender(ui());
    rerender(ui());
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
  });
});
