/**
 * App — routing regression tests for the nav + Settings consolidation.
 *
 * Two contracts land here, both requiring the REAL `<App/>` (not NavBar in
 * isolation) because the bug lived in the App.tsx <-> NavBar wiring, not in
 * either component alone:
 *
 * 1. **Retired routes redirect into Settings facets.** `/projects`,
 *    `/plugins`, `/keys` are static `<Redirect>`s; `/triggers` is dynamic
 *    (`TriggersRedirect`, unexported — only reachable by rendering `<App/>`)
 *    because it must forward a `?session=` deep link. Verified via wouter's
 *    `memoryLocation({record: true})` and its recorded `history`, per the
 *    project's test conventions — never the global browser location.
 * 2. **The NavRail product switcher renders on every route.**
 *    The unified left rail owns primary navigation now (the old top strip is
 *    gone). `App.tsx` computes `activeProduct` at the same altitude the old
 *    `landingNav` lived; the rail renders the four product rows on every route
 *    and marks the active one `aria-current="page"` (none on `/settings`). This
 *    drives that through the real route computation in `App.tsx`, not a prop.
 *
 * Scope note: only `/` and `/settings` are exercised here for the switcher
 * (the `/settings` no-current case and the `/` Tasks-current case). `/wiki`
 * and `/search` mount self-contained, heavily-networked subsystems (WikiApp's
 * own mock-client namespace, AgenticSearchView's `useAgenticSearch` query
 * fan-out) whose `activeProduct` mapping is UNCHANGED by this refactor and
 * isn't worth the extra mocking surface here.
 *
 * Rendering `<App/>` pulls in every facet's pane (a redirect can land on
 * any of them), so the mock surface below merges the fixtures already
 * established by `app.test.tsx`, `ProjectsPane.test.tsx`, `pluginsPane.test.tsx`,
 * `TriggersPane.test.tsx`, and `GitCredentialsView.test.tsx` — nothing new,
 * just assembled in one place. This suite doesn't enable Vitest globals, so
 * RTL's auto-cleanup never runs — `afterEach(cleanup)` below is load-bearing.
 */
import { cleanup, render as rtlRender, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { App } from "../App";
import * as client from "../api/client";
import * as triggersApi from "../api/triggers";

vi.mock("../api/client", () => ({
  // App-level: session list, notifications, langfuse config lookup, compose actions.
  listSessions: vi.fn(),
  createSession: vi.fn(),
  archiveSession: vi.fn(),
  unarchiveSession: vi.fn(),
  updateSessionTitle: vi.fn(),
  regenerateTitle: vi.fn(),
  listNotifications: vi.fn(),
  dismissNotification: vi.fn(),
  clearNotifications: vi.fn(),
  postQuery: vi.fn(),
  sendMessage: vi.fn(),
  uploadAttachments: vi.fn(),
  createShare: vi.fn(),
  exportSession: vi.fn(),
  fetchEvents: vi.fn(),
  // HomeView -> ProductHero -> InputBar (mounted at "/", the catch-all route).
  listModels: vi.fn(),
  listTools: vi.fn(),
  listSkills: vi.fn(),
  fetchCommands: vi.fn(),
  fetchProjectFiles: vi.fn(),
  executeCommand: vi.fn(),
  // Settings shell.
  getConfig: vi.fn(),
  getConfigSchema: vi.fn(),
  patchConfig: vi.fn(),
  // Workspace facet -> ProjectsPane.
  listProjects: vi.fn(),
  createVirtualProject: vi.fn(),
  updateVirtualProject: vi.fn(),
  deleteVirtualProject: vi.fn(),
  listProjectBranches: vi.fn(),
  listWorktrees: vi.fn(),
  createWorktree: vi.fn(),
  deleteWorktree: vi.fn(),
  // Plugins facet -> PluginsPane.
  listPlugins: vi.fn(),
  listMarketplacePlugins: vi.fn(),
  installPlugin: vi.fn(),
  uninstallPlugin: vi.fn(),
  // Security facet -> ApiKeysView.
  listApiKeys: vi.fn(),
  createApiKey: vi.fn(),
  revokeApiKey: vi.fn(),
}));

// Automation facet -> TriggersPane. Passthrough keeps real helpers
// (isActiveTrigger, isSessionTerminatedError) live, per TriggersPane.test.tsx.
vi.mock("../api/triggers", async (orig) => {
  const actual = await orig<typeof import("../api/triggers")>();
  return {
    ...actual,
    listTriggers: vi.fn(),
    listSessionTriggers: vi.fn(),
    updateTriggerStatus: vi.fn(),
    cancelTrigger: vi.fn(),
    terminateSession: vi.fn(),
  };
});

// Security facet -> GitCredentialsView. Stub its data hook + the wiki-projects
// hint query it reads at mount; the panel's own network calls (put/delete/
// validate) only fire on user interaction, which these tests never trigger.
vi.mock("../hooks/useGitCredentials", () => ({
  useGitCredentials: () => ({
    credentials: [],
    loading: false,
    error: null,
    refresh: vi.fn(),
  }),
}));
vi.mock("../components/wiki/api/hooks", () => ({
  useWikiProjects: () => ({ data: [] }),
}));

const listSessions = vi.mocked(client.listSessions);
const listNotifications = vi.mocked(client.listNotifications);
const getConfig = vi.mocked(client.getConfig);
const getConfigSchema = vi.mocked(client.getConfigSchema);
const listProjects = vi.mocked(client.listProjects);
const listProjectBranches = vi.mocked(client.listProjectBranches);
const listWorktrees = vi.mocked(client.listWorktrees);
const listPlugins = vi.mocked(client.listPlugins);
const listMarketplacePlugins = vi.mocked(client.listMarketplacePlugins);
const listApiKeys = vi.mocked(client.listApiKeys);
const listTriggers = vi.mocked(triggersApi.listTriggers);
const listModels = vi.mocked(client.listModels);
const listTools = vi.mocked(client.listTools);
const listSkills = vi.mocked(client.listSkills);
const fetchCommands = vi.mocked(client.fetchCommands);

function renderAt(path: string) {
  const { hook, history } = memoryLocation({ path, record: true });
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const result = rtlRender(
    <QueryClientProvider client={qc}>
      <Router hook={hook}>
        <App />
      </Router>
    </QueryClientProvider>
  );
  return { ...result, history };
}

afterEach(cleanup);

beforeEach(() => {
  vi.clearAllMocks();
  listSessions.mockResolvedValue([]);
  listNotifications.mockResolvedValue([]);
  getConfig.mockResolvedValue({ config: {}, secrets: {} });
  getConfigSchema.mockResolvedValue({ type: "object", properties: {} });
  listProjects.mockResolvedValue([]);
  listProjectBranches.mockResolvedValue({
    git_repo: true,
    branches: ["main"],
    current_branch: "main",
    branches_in_use: [],
  });
  listWorktrees.mockResolvedValue([]);
  listModels.mockResolvedValue({ models: [], default: "" });
  listTools.mockResolvedValue([]);
  listSkills.mockResolvedValue([]);
  fetchCommands.mockResolvedValue([]);
  listPlugins.mockResolvedValue([]);
  listMarketplacePlugins.mockResolvedValue([]);
  listApiKeys.mockResolvedValue([]);
  listTriggers.mockResolvedValue([]);
});

describe("retired routes redirect into Settings facets, preserving other params", () => {
  test("/projects -> /settings?facet=workspace", async () => {
    const { history } = renderAt("/projects");
    await waitFor(() => expect(history.at(-1)).toBe("/settings?facet=workspace"));
  });

  // Plugins is its OWN top-level facet (it was folded into `agent` when the
  // page was first absorbed). The pane and `PluginsConfig`'s `x-group` moved
  // together, so the redirect target moved with them.
  test("/plugins -> /settings?facet=plugins", async () => {
    const { history } = renderAt("/plugins");
    await waitFor(() => expect(history.at(-1)).toBe("/settings?facet=plugins"));
  });

  test("/keys -> /settings?facet=security", async () => {
    const { history } = renderAt("/keys");
    await waitFor(() => expect(history.at(-1)).toBe("/settings?facet=security"));
  });

  test("/triggers -> /settings?facet=automation", async () => {
    const { history } = renderAt("/triggers");
    await waitFor(() => expect(history.at(-1)).toBe("/settings?facet=automation"));
  });

  test("/triggers?session=abc -> /settings?facet=automation&session=abc (session survives the hop)", async () => {
    const { history } = renderAt("/triggers?session=abc");
    await waitFor(() =>
      expect(history.at(-1)).toBe("/settings?facet=automation&session=abc")
    );
  });
});

describe("NavRail product switcher renders on every route", () => {
  test("/settings renders the switcher with no product current", async () => {
    renderAt("/settings");

    const nav = await screen.findByRole("navigation", { name: "Products" });
    for (const product of ["Tasks", "Wiki", "Search", "Apps"]) {
      expect(
        within(nav).getByRole("button", { name: product }),
      ).not.toHaveAttribute("aria-current");
    }
  });

  test("/ marks Tasks current via aria-current=page", async () => {
    renderAt("/");

    const nav = await screen.findByRole("navigation", { name: "Products" });
    expect(within(nav).getByRole("button", { name: "Tasks" })).toHaveAttribute(
      "aria-current",
      "page"
    );
    expect(within(nav).getByRole("button", { name: "Wiki" })).not.toHaveAttribute(
      "aria-current"
    );
  });
});
