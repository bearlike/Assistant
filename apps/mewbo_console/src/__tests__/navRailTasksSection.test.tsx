import { cleanup, render as rtlRender, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { Router, useLocation } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import { ProductSection } from "../components/nav-rail/sections";
import { MewboRuntimeProvider } from "../components/assistant-ui/MewboRuntimeProvider";
import { useSessions } from "../hooks/useSessions";
import * as client from "../api/client";
import { SessionSummary } from "../types";

// The rail's Tasks section absorbed the old TaskSidebar: "New task" + the
// assistant-ui recents (default origin filter, date-bucketed). It reads the
// shared runtime for its ordered/active threads and the TanStack-cached
// sessions for per-row meta — mock the client and let the cache feed both.
vi.mock("../api/client", () => ({
  listSessions: vi.fn(),
  listProjects: vi.fn(),
  archiveSession: vi.fn(),
  unarchiveSession: vi.fn(),
  updateSessionTitle: vi.fn(),
  regenerateTitle: vi.fn(),
  createSession: vi.fn(),
}));

const listSessions = vi.mocked(client.listSessions);
const listProjects = vi.mocked(client.listProjects);

const NOW = "2026-06-10T12:00:00Z";

const SESSIONS: SessionSummary[] = [
  {
    session_id: "sess-user",
    title: "My console task",
    created_at: NOW,
    status: "completed",
    origin: "user",
    context: { mcp_tools: [] },
  },
  {
    session_id: "sess-channel",
    title: "Channel chat",
    created_at: "2026-06-09T10:00:00Z",
    status: "completed",
    origin: "channel",
    context: { mcp_tools: [] },
  },
  {
    session_id: "sess-wiki",
    title: "Wiki indexing run",
    created_at: "2026-06-08T10:00:00Z",
    status: "completed",
    origin: "wiki",
    context: { mcp_tools: [] },
  },
];

// The section reads its ordered/active threads from the shared runtime (mounted
// app-wide in AppLayout). Mirror that: feed MewboRuntimeProvider from the SAME
// TanStack-cached sessions and wire its switch callbacks to wouter navigation,
// exactly as App does — so row-click and "New task" record real navigations.
function RailHarness() {
  const [, navigate] = useLocation();
  const { sessions, archivedSessions } = useSessions();
  return (
    <MewboRuntimeProvider
      sessions={sessions}
      archivedSessions={archivedSessions}
      activeSessionId={null}
      isRunning={false}
      onNew={() => Promise.resolve()}
      onSwitchToThread={(id) => navigate(`/s/${encodeURIComponent(String(id))}`)}
      onSwitchToNewThread={() => navigate("/")}
    >
      <ProductSection product="tasks" />
    </MewboRuntimeProvider>
  );
}

// Render under an isolated in-memory router so navigation is deterministic. The
// default path is a NON-landing route: on `/` the Tasks section deliberately
// collapses its recents (the landing page IS the full list), so a recents test
// must mount somewhere else.
function render(ui: ReactElement, path = "/settings") {
  const { hook, history } = memoryLocation({ path, record: true });
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const result = rtlRender(
    <QueryClientProvider client={qc}>
      <Router hook={hook}>{ui}</Router>
    </QueryClientProvider>,
  );
  return { ...result, history };
}

beforeEach(() => {
  vi.resetAllMocks();
  listSessions.mockResolvedValue(SESSIONS);
  listProjects.mockResolvedValue([]);
});

// This suite doesn't enable Vitest globals, so RTL's auto-cleanup never runs.
afterEach(() => cleanup());

test("lists user + channel tasks and hides internal origins by default", async () => {
  render(<RailHarness />);
  expect(await screen.findByText("My console task")).toBeInTheDocument();
  expect(screen.getByText("Channel chat")).toBeInTheDocument();
  // wiki origin is hidden by the shared default filter (same as the landing page)
  expect(screen.queryByText("Wiki indexing run")).not.toBeInTheDocument();
});

test("navigates to the session route when a task is clicked", async () => {
  const { history } = render(<RailHarness />);
  await userEvent.click(await screen.findByText("My console task"));
  expect(history.at(-1)).toBe("/s/sess-user");
});

test("the New task action returns to the landing page", async () => {
  const { history } = render(<RailHarness />);
  await screen.findByText("My console task");
  await userEvent.click(screen.getByRole("button", { name: /new task/i }));
  expect(history.at(-1)).toBe("/");
});

test("shows the empty state when there are no visible tasks", async () => {
  listSessions.mockResolvedValue([]);
  render(<RailHarness />);
  expect(await screen.findByText("No tasks yet.")).toBeInTheDocument();
});

test("collapses recents to just the action row on the landing route", async () => {
  render(<RailHarness />, "/");
  expect(await screen.findByRole("button", { name: /new task/i })).toBeInTheDocument();
  // On `/` the recents list is suppressed to avoid duplicating the landing list.
  expect(screen.queryByText("My console task")).not.toBeInTheDocument();
});
