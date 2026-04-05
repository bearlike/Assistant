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
  listModels: vi.fn(),
  listProjects: vi.fn(),
  invalidateCache: vi.fn(),
  peekCache: vi.fn().mockReturnValue(undefined),
  archiveSession: vi.fn(),
  unarchiveSession: vi.fn(),
  updateSessionTitle: vi.fn(),
  regenerateTitle: vi.fn(),
  listNotifications: vi.fn(),
  dismissNotification: vi.fn(),
  clearNotifications: vi.fn(),
  uploadAttachments: vi.fn(),
  createShare: vi.fn(),
  exportSession: vi.fn(),
  resolveShare: vi.fn(),
  listAgents: vi.fn(),
  getConfigSchema: vi.fn(),
  getConfig: vi.fn(),
  patchConfig: vi.fn(),
  approvePlan: vi.fn(),
  recoverSession: vi.fn(),
  fetchPlanMarkdown: vi.fn()
}));
const listSessions = vi.mocked(client.listSessions);
const createSession = vi.mocked(client.createSession);
const postQuery = vi.mocked(client.postQuery);
const fetchEvents = vi.mocked(client.fetchEvents);
const listTools = vi.mocked(client.listTools);
const listSkills = vi.mocked(client.listSkills);
const listModels = vi.mocked(client.listModels);
const listProjects = vi.mocked(client.listProjects);
const archiveSession = vi.mocked(client.archiveSession);
const unarchiveSession = vi.mocked(client.unarchiveSession);
const updateSessionTitle = vi.mocked(client.updateSessionTitle);
const regenerateTitle = vi.mocked(client.regenerateTitle);
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
  listModels.mockResolvedValue({ models: [], default: "" });
  listProjects.mockResolvedValue([]);
  fetchEvents.mockResolvedValue({
    events: [],
    running: false
  });
  archiveSession.mockResolvedValue();
  unarchiveSession.mockResolvedValue();
  updateSessionTitle.mockResolvedValue({ session_id: "sess-1", title: "t" });
  regenerateTitle.mockResolvedValue({ session_id: "sess-1", title: "AI Title" });
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
  (client as Record<string, unknown>).getConfigSchema = vi.fn().mockResolvedValue({ type: "object", properties: {} });
  (client as Record<string, unknown>).getConfig = vi.fn().mockResolvedValue({});
  (client as Record<string, unknown>).patchConfig = vi.fn().mockResolvedValue({});
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
    expect(within(homeBar).getByLabelText("Configure session")).toHaveTextContent("1");
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
  await user.click(within(detailBar).getByLabelText("Configure session"));
  await user.click(await within(detailBar).findByText("Integrations"));
  expect(await within(detailBar).findByText("MCP Beta")).toBeInTheDocument();
  const stopButton = within(detailBar).getByLabelText("Stop run");
  await user.click(stopButton);
  await waitFor(() => {
    expect(postQuery).toHaveBeenCalledWith("sess-3", "/terminate");
  });
});
test("rehydrates plan mode from session context on mount", async () => {
  // When a session was last submitted with mode="plan", re-opening it
  // must restore the plan/act toggle to "plan" — otherwise the UI lies
  // about the session's trailing state. The Configure badge's total
  // counter does not include queryMode, so we verify rehydration by
  // checking that the next postQuery call carries mode="plan" without
  // the user toggling anything.
  listSessions.mockResolvedValue([{
    session_id: "sess-plan",
    title: "Planning session",
    created_at: "2026-02-08T12:00:00Z",
    status: "completed",
    done_reason: "completed",
    running: false,
    context: { mcp_tools: [], mode: "plan" }
  }]);
  fetchEvents.mockResolvedValue({ events: [], running: false });
  const user = userEvent.setup();
  render(<App />);
  await user.click(await screen.findByText("Planning session"));
  const detailBars = await screen.findAllByTestId("inputbar-detail");
  const detailBar = detailBars[detailBars.length - 1];
  const input = within(detailBar).getByLabelText("Session query");
  await user.type(input, "continue");
  await user.click(within(detailBar).getByLabelText("Send query"));
  await waitFor(() => {
    expect(postQuery).toHaveBeenCalledWith(
      "sess-plan",
      "continue",
      expect.any(Object),
      "plan",
      undefined,
    );
  });
});
test("rehydrates MCP selections from session context on mount", async () => {
  // Regression guard for the sessionContext → pendingMcpToolsRef → mcps
  // rehydration path. A session stored with mcp_tools=["tool-2"] must
  // surface as 1 active integration in the Configure badge when the
  // user opens that session. Also serves as a live check that useEffect 2
  // fires when sessionContext.mcp_tools changes identity (it must, because
  // the cached useMcpTools list would otherwise leave mcps empty).
  listSessions.mockResolvedValue([{
    session_id: "sess-rehydrate",
    title: "Rehydrate session",
    created_at: "2026-02-08T10:00:00Z",
    status: "completed",
    done_reason: "completed",
    running: false,
    context: { mcp_tools: ["tool-2"] }
  }]);
  listTools.mockResolvedValue([
    { tool_id: "tool-1", name: "MCP Alpha", kind: "mcp", enabled: true },
    { tool_id: "tool-2", name: "MCP Beta", kind: "mcp", enabled: true }
  ]);
  const user = userEvent.setup();
  render(<App />);
  await user.click(await screen.findByText("Rehydrate session"));
  const detailBars = await screen.findAllByTestId("inputbar-detail");
  const detailBar = detailBars[detailBars.length - 1];
  // Configure badge should show 1 (one active MCP matching session context).
  await waitFor(() => {
    expect(within(detailBar).getByLabelText("Configure session"))
      .toHaveTextContent("1");
  });
});
