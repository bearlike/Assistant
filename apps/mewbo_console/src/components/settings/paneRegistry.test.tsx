/**
 * FACET_PANES registry — every registered pane actually mounts ITS OWN card,
 * not just some card somewhere in the facet.
 *
 * `SettingsView` renders whatever `FACET_PANES[activeFacet]` finds, each
 * pane behind its own `<Suspense>`. This test iterates `Object.entries(
 * FACET_PANES)` itself — not a hand-written list of facets — so a new pane
 * (or facet) is covered by construction, with zero new test-loop code.
 *
 * A prior version of this test asserted only
 * `getAllByRole("region").length >= panes.length`. That is satisfied by the
 * facet's SCHEMA-DRIVEN sections alone wherever any exist (`plugins`: 1 pane
 * + 1 section, `automation`: 1+1, `workspace`: 1+3) — every one of those
 * panes could `return null` and the count check would stay green. Only
 * `security` (3 panes, 0 schema sections) constrained anything.
 *
 * `PANE_MARKERS` below pins ONE structural marker per registered pane so a
 * `null` render fails for every facet, not just `security`: prefer a pane's
 * stable `SettingsCard id=` prop (it becomes the `<h2 id>` an
 * `aria-labelledby` region is named by — see `settings-plugins-installed` /
 * `settings-triggers` / `settings-managed-projects` in source). `ApiKeysView`
 * / `GitCredentialsView` (Security facet, owned by a concurrent workstream —
 * not touched here) set no such id, so their markers fall back to a
 * SettingsCard `title` that's static under this test's mocks (zero keys,
 * zero credentials). A facet present in `FACET_PANES` but missing from
 * `PANE_MARKERS` throws inside its own test — nobody can register a pane
 * without pinning what proves it rendered.
 *
 * Follows `SettingsView.integration.test.tsx`'s pattern: mount against the
 * REAL `configs/app.schema.json` (never a fixture snapshot) so a schema drift
 * can't slip past. Also inherits its documented jsdom trap: `window.location`
 * is shared across this file, so once a render writes `?facet=`, the NEXT
 * test would inherit it without the `beforeEach` reset below.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { cleanup, render as rtlRender, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { afterEach, beforeAll, beforeEach, describe, expect, test, vi } from "vitest";

import { SettingsView } from "../SettingsView";
import { FACET_PANES } from "./panes";
import { FACETS, type FacetId } from "./facets";
import * as client from "../../api/client";
import * as triggersApi from "../../api/triggers";

/** One structural proof that a specific registered pane rendered its own
 * `<SettingsCard>`: either its stable `id` (preferred) or its `title` text
 * (fallback for a pane this file's owner can't add an id to). */
type PaneMarker = { id: string } | { name: string };

/**
 * facetId -> ordered marker(s), one per pane registered in `FACET_PANES` for
 * that facet (a multi-card pane only needs ONE marker to prove it rendered
 * at all — `PluginsPane`'s second card, the marketplace, isn't separately
 * pinned). Keyed by `FacetId` so a facet id typo is a compile error; a
 * *missing* entry for a facet `FACET_PANES` actually registers is instead a
 * runtime test failure (see the lookup in the loop below), which is the
 * enforcement the "zero new test code" contract relies on.
 */
const PANE_MARKERS: Partial<Record<FacetId, PaneMarker[]>> = {
  agent: [{ id: "settings-system-instructions" }],
  plugins: [{ id: "settings-plugins-installed" }],
  automation: [{ id: "settings-triggers" }],
  apps: [{ id: "settings-apps" }],
  security: [
    { id: "settings-secrets-summary" }, // SecretsSummary
    { name: "Create a new key" }, // ApiKeysView
    { name: "Git credentials" }, // GitCredentialsView
  ],
  // IdentityAccessPane, whose `fetchMe` mock below grants nothing, so it
  // settles into its no-permission branch — exactly the card this id names.
  access: [{ id: "settings-identity-access" }],
  workspace: [{ id: "settings-managed-projects" }],
};

/** Resolve a marker to the element it claims exists, failing the test with a
 * pointed message if the pane didn't render it (e.g. returned `null`). */
function expectPaneMarkerRendered(marker: PaneMarker, facetId: string) {
  if ("id" in marker) {
    const heading = document.getElementById(marker.id);
    expect(
      heading,
      `facet "${facetId}": expected a SettingsCard heading with id="${marker.id}" — ` +
        `the pane that owns it did not render (or the id drifted; check source)`
    ).toBeTruthy();
    expect(heading?.tagName).toBe("H2");
    expect(heading?.closest("section")).not.toBeNull();
  } else {
    expect(
      screen.getByRole("region", { name: marker.name }),
      `facet "${facetId}": expected a SettingsCard titled "${marker.name}"`
    ).toBeInTheDocument();
  }
}

// facetId -> display title, off the OTHER canonical registry (`facets.ts`) —
// needed only to find the right nav button; no per-facet text is hand-typed.
const FACET_TITLE: Record<string, string> = Object.fromEntries(
  FACETS.map((f) => [f.id, f.title])
);

const SCHEMA_PATH = resolve(process.cwd(), "../../configs/app.schema.json");
const realSchema = JSON.parse(readFileSync(SCHEMA_PATH, "utf-8")) as Record<
  string,
  unknown
>;

/**
 * Stub `/api/auth/me` — the ONE I/O boundary `IdentityAccessPane` reaches.
 *
 * Without this the pane fires a REAL `fetch` from jsdom on every render of the
 * Access facet. That is slow and nondeterministic under full-suite parallel
 * load (the connection has to be refused before the query settles, and the
 * pane holds a spinner until it does), and it was enough to push sibling tests
 * in this file past their timeouts — a failure that looked like flake in
 * whichever test happened to lose the race, not in the one that caused it.
 *
 * The profile grants no permissions, which pins the pane's no-permission
 * branch and keeps `PANE_MARKERS`'s marker for this facet stable.
 */
vi.mock("../../api/auth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/auth")>()),
  fetchMe: vi.fn().mockResolvedValue({
    authenticated: true,
    auth_enabled: true,
    subject: "user:test",
    kind: "user",
    display: "Test User",
    email: null,
    email_verified: false,
    roles: ["viewer"],
    permissions: [],
    teams: [],
    avatar: { picture_url: null, gravatar_url: null },
    auth_method: { kind: "oidc", issuer: null },
    scopes: null,
  }),
}));

vi.mock("../../api/client", () => ({
  getConfigSchema: vi.fn(),
  getConfig: vi.fn(),
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
  // Automation facet -> TriggersPane's useSessions() dependency.
  listSessions: vi.fn(),
  archiveSession: vi.fn(),
  unarchiveSession: vi.fn(),
  updateSessionTitle: vi.fn(),
  regenerateTitle: vi.fn(),
  createSession: vi.fn(),
  // Security facet -> ApiKeysView.
  listApiKeys: vi.fn(),
  createApiKey: vi.fn(),
  revokeApiKey: vi.fn(),
}));

// Automation facet -> TriggersPane. Passthrough keeps real helpers
// (isActiveTrigger) live, per TriggersPane.test.tsx.
vi.mock("../../api/triggers", async (orig) => {
  const actual = await orig<typeof import("../../api/triggers")>();
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
// hint query; the panel's own network calls only fire on user interaction,
// which this mount-only test never triggers.
vi.mock("../../hooks/useGitCredentials", () => ({
  useGitCredentials: () => ({
    credentials: [],
    loading: false,
    error: null,
    refresh: vi.fn(),
  }),
}));
vi.mock("../wiki/api/hooks", () => ({
  useWikiProjects: () => ({ data: [] }),
}));

// Agent facet -> SystemInstructionsPane. Stub its data hooks; save/preview
// only fire on user interaction, which this mount-only test never triggers.
vi.mock("../../hooks/useSystemInstructions", () => ({
  useSystemInstructions: () => ({
    doc: { template: "", enabled: true, updatedAt: "", lastError: null },
    loading: false,
    error: null,
  }),
  useSystemInstructionsVariables: () => ({ variables: [], loading: false, error: null }),
  useSaveSystemInstructions: () => ({ mutateAsync: vi.fn(), isPending: false }),
  usePreviewSystemInstructions: () => ({ mutate: vi.fn(), isPending: false, data: undefined }),
}));

const getConfigSchema = vi.mocked(client.getConfigSchema);
const getConfig = vi.mocked(client.getConfig);
const listProjects = vi.mocked(client.listProjects);
const listProjectBranches = vi.mocked(client.listProjectBranches);
const listWorktrees = vi.mocked(client.listWorktrees);
const listPlugins = vi.mocked(client.listPlugins);
const listMarketplacePlugins = vi.mocked(client.listMarketplacePlugins);
const listSessions = vi.mocked(client.listSessions);
const listApiKeys = vi.mocked(client.listApiKeys);
const listTriggers = vi.mocked(triggersApi.listTriggers);

function render(ui: ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

afterEach(cleanup);

/**
 * Resolve every registered pane's chunk ONCE, before any timed assertion.
 *
 * Each pane is a `React.lazy`, so the first render that reaches it pays Vite's
 * COLD module transform inside the 5s spinner-clearing `waitFor`. Under
 * full-suite parallel load that transform — `SystemInstructionsPane` drags in
 * CodeMirror — can outlast the timeout on its own, and the test fails having
 * measured the bundler rather than the render. It passes in isolation, which
 * reads exactly like flake.
 *
 * Warming here, not raising the timeout: the timeout is the assertion's budget
 * and should stay tight. This list is a performance hint only. If it drifts
 * from `FACET_PANES`, the missed pane simply goes back to being transformed
 * cold — slower, never wrong — so it needs no lockstep guard of its own.
 */
beforeAll(async () => {
  await Promise.all([
    import("./panes/SecretsSummary"),
    import("../ApiKeysView"),
    import("../GitCredentialsView"),
    import("./panes/ProjectsPane"),
    import("./panes/PluginsPane"),
    import("./panes/TriggersPane"),
    import("./panes/SystemInstructionsPane"),
    import("./panes/IdentityAccessPane"),
    import("../apps/AppsPane"),
  ]);
});

beforeEach(() => {
  vi.clearAllMocks();
  getConfigSchema.mockResolvedValue(realSchema);
  getConfig.mockResolvedValue({ config: {}, secrets: {} });
  listProjects.mockResolvedValue([]);
  listProjectBranches.mockResolvedValue({
    git_repo: true,
    branches: ["main"],
    current_branch: "main",
    branches_in_use: [],
  });
  listWorktrees.mockResolvedValue([]);
  listPlugins.mockResolvedValue([]);
  listMarketplacePlugins.mockResolvedValue([]);
  listSessions.mockResolvedValue([]);
  listApiKeys.mockResolvedValue([]);
  listTriggers.mockResolvedValue([]);
  // SettingsView mirrors the active facet into `?facet=` on the (shared,
  // browser-location) router. Reset it so a prior test's click doesn't
  // deep-link this one into the wrong facet.
  window.history.replaceState({}, "", "/settings");
});

describe("FACET_PANES registry — every entry mounts its pane(s)", () => {
  for (const [facetId, panes] of Object.entries(FACET_PANES)) {
    test(`facet "${facetId}" mounts all ${panes.length} registered pane(s)`, async () => {
      window.history.replaceState({}, "", `/settings?facet=${facetId}`);
      render(<SettingsView />);

      // The `?facet=` deep link only takes effect once the config query
      // resolves — the FIRST commit renders the shell's
      // fallback-to-first-visible-facet (whichever that is), which can ALSO
      // show zero spinners transiently. Wait for the TARGET facet's own pane
      // heading to actually appear before reading its panes, or this races
      // that transient render and inspects the wrong facet. `getAllByRole`
      // rather than `getBy*`: the Plugins facet's pane heading and its
      // schema-driven `PluginsConfig` section card share the "Plugins"
      // accessible name, so a single-match query throws there.
      await waitFor(() => {
        expect(
          screen.getAllByRole("heading", { name: FACET_TITLE[facetId] }).length
        ).toBeGreaterThan(0);
      });

      // Every Suspense fallback (the shell's own loading spinner AND each
      // pane's `PaneFallback`) uses the same `animate-spin` marker. Once none
      // remain, every lazy chunk has resolved and every pane's own
      // loading-state has settled.
      await waitFor(
        () => {
          expect(document.querySelectorAll("svg.animate-spin").length).toBe(0);
        },
        { timeout: 5000 }
      );

      // Weak historical floor kept as a cheap sanity check: every pane
      // contributes at least one region. The markers below are what
      // actually pins EACH pane, not just the facet total.
      const regions = screen.getAllByRole("region");
      expect(regions.length).toBeGreaterThanOrEqual(panes.length);

      const markers = PANE_MARKERS[facetId as FacetId];
      if (!markers) {
        throw new Error(
          `PANE_MARKERS (paneRegistry.test.tsx) has no entry for facet "${facetId}", which ` +
            `FACET_PANES registers with ${panes.length} pane(s). Add a marker for each pane ` +
            `before registering it, or this test can't tell a real render from a null one.`
        );
      }
      for (const marker of markers) {
        expectPaneMarkerRendered(marker, facetId);
      }
    });
  }
});
