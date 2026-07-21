/**
 * PluginsPane — the Agent & Tools facet's plugin management pane.
 *
 * Covers the migration off the old `PluginsView`'s hand-rolled
 * useState/useEffect data layer onto `usePlugins()` (TanStack Query):
 *   1. Installed plugins render from `listPlugins()`.
 *   2. Marketplace search filters the list client-side.
 *   3. Install/uninstall fire the right API calls AND invalidate the
 *      query cache — proven by asserting the UI reflects a refetch, not
 *      just that the mutation function was called.
 */
import { cleanup, render as rtlRender, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { PluginsPane } from "../components/settings/panes/PluginsPane";
import * as client from "../api/client";
import type { MarketplacePlugin, PluginSummary } from "../api/contracts";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
import { toast } from "sonner";

vi.mock("../api/client", () => ({
  listPlugins: vi.fn(),
  listMarketplacePlugins: vi.fn(),
  installPlugin: vi.fn(),
  uninstallPlugin: vi.fn(),
}));

const listPlugins = vi.mocked(client.listPlugins);
const listMarketplacePlugins = vi.mocked(client.listMarketplacePlugins);
const installPlugin = vi.mocked(client.installPlugin);
const uninstallPlugin = vi.mocked(client.uninstallPlugin);

const INSTALLED: PluginSummary[] = [
  {
    name: "core-plugin",
    description: "Ships with the deployment.",
    version: "1.0.0",
    marketplace: "anthropics/claude-plugins-official",
    scope: "user",
    skills: 2,
    agents: 1,
    commands: 0,
    mcp_servers: 0,
    has_hooks: true,
  },
];

// Deliberately does NOT include "core-plugin" — the hook recomputes each
// marketplace entry's `installed` flag from the installed list by NAME, and
// giving it a distinct name here keeps text queries unambiguous (the
// installed card and the marketplace card would otherwise both render a
// "core-plugin" node).
const MARKETPLACE: MarketplacePlugin[] = [
  {
    name: "widget-plugin",
    description: "Adds widget-building skills.",
    category: "productivity",
    marketplace: "anthropics/claude-plugins-official",
    installed: false,
  },
  {
    name: "reporting-plugin",
    description: "Adds reporting dashboards.",
    category: "analytics",
    marketplace: "anthropics/claude-plugins-official",
    installed: false,
  },
];

function render(ui: ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  vi.resetAllMocks();
  listPlugins.mockResolvedValue(INSTALLED);
  listMarketplacePlugins.mockResolvedValue(MARKETPLACE);
});

afterEach(() => {
  cleanup();
});

test("renders installed plugins from listPlugins()", async () => {
  render(<PluginsPane />);

  expect(await screen.findByText("core-plugin")).toBeInTheDocument();
  expect(screen.getByText("hooks")).toBeInTheDocument();
  // Count badges pluralize: "2 skills" but "1 agent" (it used to say "1 agents").
  expect(screen.getByText("2 skills")).toBeInTheDocument();
  expect(screen.getByText("1 agent")).toBeInTheDocument();

  // Marketplace section renders both listMarketplacePlugins() entries with
  // Install affordances (neither is in the installed list).
  expect(await screen.findByText("widget-plugin")).toBeInTheDocument();
  expect(screen.getByLabelText("Install widget-plugin")).toBeInTheDocument();
  expect(screen.getByLabelText("Install reporting-plugin")).toBeInTheDocument();
});

test("marketplace search filters the list", async () => {
  const user = userEvent.setup();
  render(<PluginsPane />);

  await screen.findByText("widget-plugin");
  expect(screen.getByText("reporting-plugin")).toBeInTheDocument();

  const search = screen.getByPlaceholderText("Search plugins…");
  await user.type(search, "widget");

  expect(screen.getByText("widget-plugin")).toBeInTheDocument();
  expect(screen.queryByText("reporting-plugin")).not.toBeInTheDocument();
});

test("install fires installPlugin and invalidates so the pane reflects the refetch", async () => {
  const user = userEvent.setup();
  installPlugin.mockResolvedValue(undefined);
  // First call = initial mount; second call = the post-install invalidated refetch.
  listPlugins
    .mockResolvedValueOnce(INSTALLED)
    .mockResolvedValueOnce([
      ...INSTALLED,
      {
        name: "widget-plugin",
        description: "Adds widget-building skills.",
        version: "0.2.0",
        marketplace: "anthropics/claude-plugins-official",
        scope: "user",
        skills: 1,
        agents: 0,
        commands: 0,
        mcp_servers: 0,
        has_hooks: false,
      },
    ]);

  render(<PluginsPane />);
  await screen.findByText("widget-plugin");

  await user.click(screen.getByLabelText("Install widget-plugin"));

  expect(installPlugin).toHaveBeenCalledWith(
    "widget-plugin",
    "anthropics/claude-plugins-official",
  );
  await waitFor(() => expect(listPlugins).toHaveBeenCalledTimes(2));
  await waitFor(() =>
    expect(screen.queryByLabelText("Install widget-plugin")).not.toBeInTheDocument(),
  );
  await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Installed "widget-plugin".'));
});

test("uninstall fires uninstallPlugin and invalidates so the pane reflects the refetch", async () => {
  const user = userEvent.setup();
  uninstallPlugin.mockResolvedValue(undefined);
  listPlugins.mockResolvedValueOnce(INSTALLED).mockResolvedValueOnce([]);

  render(<PluginsPane />);
  await screen.findByLabelText("Uninstall core-plugin");

  await user.click(screen.getByLabelText("Uninstall core-plugin"));

  expect(uninstallPlugin).toHaveBeenCalledWith("core-plugin");
  await waitFor(() => expect(listPlugins).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.getByText("No plugins installed.")).toBeInTheDocument());
  await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Uninstalled "core-plugin".'));
});
