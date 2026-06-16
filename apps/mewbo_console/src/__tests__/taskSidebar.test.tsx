import { cleanup, render as rtlRender, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import { TaskSidebar } from "../components/TaskSidebar";
import * as client from "../api/client";
import { SessionSummary } from "../types";

// useSessions / useProjects reach the API; mock the client and let the
// TanStack cache feed the sidebar exactly as it does on the landing page.
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

const SESSIONS: SessionSummary[] = [
  {
    session_id: "sess-user",
    title: "My console task",
    created_at: "2026-06-10T10:00:00Z",
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

// Render under an isolated in-memory router so navigation is deterministic and
// can't leak between tests (wouter's default browser location is global).
function render(ui: ReactElement, path = "/") {
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

// This suite doesn't enable Vitest globals, so RTL's auto-cleanup never runs —
// unmount between tests so accumulated renders don't collide in queries.
afterEach(() => cleanup());

test("lists user + channel tasks and hides internal origins by default", async () => {
  render(<TaskSidebar />);
  expect(await screen.findByText("My console task")).toBeInTheDocument();
  expect(screen.getByText("Channel chat")).toBeInTheDocument();
  // wiki origin is hidden by the shared default filter (same as the landing page)
  expect(screen.queryByText("Wiki indexing run")).not.toBeInTheDocument();
});

test("navigates to the session route when a task is clicked", async () => {
  const { history } = render(<TaskSidebar />);
  await userEvent.click(await screen.findByText("My console task"));
  expect(history.at(-1)).toBe("/s/sess-user");
});

test("the New task button returns to the landing page", async () => {
  const { history } = render(<TaskSidebar />, "/s/sess-user");
  await screen.findByText("My console task");
  await userEvent.click(screen.getByRole("button", { name: /new task/i }));
  expect(history.at(-1)).toBe("/");
});

test("shows the empty state when there are no visible tasks", async () => {
  listSessions.mockResolvedValue([]);
  render(<TaskSidebar />);
  expect(await screen.findByText("No tasks yet.")).toBeInTheDocument();
});
