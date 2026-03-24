import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { App } from "../App";
import * as client from "../api/client";
vi.mock("../api/client", () => ({
  listSessions: vi.fn(),
  createSession: vi.fn(),
  postQuery: vi.fn(),
  fetchEvents: vi.fn(),
  listTools: vi.fn(),
  listSkills: vi.fn(),
  listProjects: vi.fn(),
  invalidateCache: vi.fn(),
  archiveSession: vi.fn(),
  unarchiveSession: vi.fn(),
  listNotifications: vi.fn(),
  dismissNotification: vi.fn(),
  clearNotifications: vi.fn(),
  uploadAttachments: vi.fn(),
  createShare: vi.fn(),
  exportSession: vi.fn(),
  resolveShare: vi.fn()
}));
const listSessions = vi.mocked(client.listSessions);
const createSession = vi.mocked(client.createSession);
const postQuery = vi.mocked(client.postQuery);
const fetchEvents = vi.mocked(client.fetchEvents);
const listTools = vi.mocked(client.listTools);
const listSkills = vi.mocked(client.listSkills);
const listProjects = vi.mocked(client.listProjects);
const archiveSession = vi.mocked(client.archiveSession);
const unarchiveSession = vi.mocked(client.unarchiveSession);
const listNotifications = vi.mocked(client.listNotifications);
const dismissNotification = vi.mocked(client.dismissNotification);
const clearNotifications = vi.mocked(client.clearNotifications);
const uploadAttachments = vi.mocked(client.uploadAttachments);
const createShare = vi.mocked(client.createShare);
const exportSession = vi.mocked(client.exportSession);
const resolveShare = vi.mocked(client.resolveShare);
beforeEach(() => {
  vi.resetAllMocks();
  listTools.mockResolvedValue([]);
  listSkills.mockResolvedValue([]);
  listProjects.mockResolvedValue([]);
  fetchEvents.mockResolvedValue({
    events: [],
    running: false
  });
  archiveSession.mockResolvedValue();
  unarchiveSession.mockResolvedValue();
  listNotifications.mockResolvedValue([]);
  dismissNotification.mockResolvedValue();
  clearNotifications.mockResolvedValue();
  uploadAttachments.mockResolvedValue([]);
  createShare.mockResolvedValue({ token: "share-1", session_id: "sess-1" });
  exportSession.mockResolvedValue({
    session_id: "sess-1",
    events: [],
    summary: null
  });
  resolveShare.mockResolvedValue({
    token: "share-1",
    session_id: "sess-1",
    events: [],
    summary: null
  });
  window.history.pushState({}, "", "/");
});
test("loads sessions from the API", async () => {
  listSessions.mockResolvedValue([{
    session_id: "sess-1",
    title: "First session",
    created_at: "2026-02-08T10:00:00Z",
    status: "completed",
    done_reason: "completed",
    running: false,
    context: {
      repo: "acme/web",
      branch: "main",
      mcp_tools: []
    }
  }]);
  render(<App />);
  expect(await screen.findByText("First session")).toBeInTheDocument();
});
test("submits a query and includes MCP tool ids", async () => {
  listSessions.mockResolvedValue([]);
  createSession.mockResolvedValue("sess-2");
  listTools.mockResolvedValue([{
    tool_id: "tool-1",
    name: "MCP Alpha",
    kind: "mcp",
    enabled: true,
    description: "alpha"
  }]);
  const user = userEvent.setup();
  render(<App />);
  const homeBars = screen.getAllByTestId("inputbar-home");
  const homeBar = homeBars[homeBars.length - 1];
  const homeInput = within(homeBar).getByLabelText("Task description");
  await waitFor(() => {
    expect(within(homeBar).getByLabelText("Select MCP tools")).toHaveTextContent("1 MCPs");
  });
  await user.type(homeInput, "Do the thing");
  await user.click(within(homeBar).getByLabelText("Send query"));
  await waitFor(() => {
    expect(createSession).toHaveBeenCalled();
  });
  expect(postQuery).toHaveBeenCalledWith(
    "sess-2",
    "Do the thing",
    {
      mcp_tools: ["tool-1"]
    },
    "act",
    undefined
  );
});
test("renders MCP list and sends stop command", async () => {
  listSessions.mockResolvedValue([{
    session_id: "sess-3",
    title: "Running session",
    created_at: "2026-02-08T09:00:00Z",
    status: "running",
    done_reason: null,
    running: true,
    context: {
      mcp_tools: []
    }
  }]);
  listTools.mockResolvedValue([{
    tool_id: "tool-2",
    name: "MCP Beta",
    kind: "mcp",
    enabled: true
  }]);
  fetchEvents.mockResolvedValue({
    events: [],
    running: true
  });
  const user = userEvent.setup();
  render(<App />);
  await user.click(await screen.findByText("Running session"));
  const detailBar = await screen.findByTestId("inputbar-detail");
  await user.click(within(detailBar).getByLabelText("Select MCP tools"));
  expect(await screen.findByText("MCP Beta")).toBeInTheDocument();
  const stopButton = within(detailBar).getByLabelText("Stop run");
  await user.click(stopButton);
  await waitFor(() => {
    expect(postQuery).toHaveBeenCalledWith("sess-3", "/terminate");
  });
});
