/**
 * NavRail — the Settings facet section (rail zone 3 on `/settings`).
 *
 * Zone 3 shows what is inside the current scope: recents for a product, the
 * facet list for Settings. These tests hold the two contracts that make that
 * safe against the Settings shell, which owns the same `?facet=` key:
 *
 *   1. The rail offers exactly the facets the shell considers VISIBLE, read
 *      from the same model rather than a hand-kept list, so a row can never
 *      point at a facet the shell would refuse to open.
 *   2. A facet click sets only `facet` and preserves every other param — the
 *      `?session=` deep-link into the Automation pane is the case that breaks
 *      if the rail rebuilds the query from scratch.
 *
 * Run against the REAL `configs/app.schema.json` for the same reason
 * `SettingsView.integration.test.tsx` is: facet visibility is decided by the
 * schema's `x-group` metadata, so a fixture would prove only that the rail
 * agrees with the fixture.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { cleanup, render as rtlRender, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { afterEach, beforeAll, beforeEach, describe, expect, test, vi } from "vitest";

import { SettingsFacetsSection } from "../components/nav-rail/settingsSection";
import * as client from "../api/client";

const SCHEMA_PATH = resolve(process.cwd(), "../../configs/app.schema.json");
const realSchema = JSON.parse(readFileSync(SCHEMA_PATH, "utf-8")) as Record<
  string,
  unknown
>;

vi.mock("../api/client", () => ({
  getConfigSchema: vi.fn(),
  getConfig: vi.fn(),
  patchConfig: vi.fn(),
}));

const getConfigSchema = vi.mocked(client.getConfigSchema);
const getConfig = vi.mocked(client.getConfig);

const config: Record<string, unknown> = {
  llm: { default_model: "claude-opus-4-8" },
  agent: {},
  permissions: {},
  plugins: { enabled: true, marketplaces: [] },
  langfuse: {},
  home_assistant: {},
  cli: {},
  chat: {},
  runtime: {},
  storage: {},
  api: {},
};

function render(ui: ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

afterEach(cleanup);

// The section is reached through a `React.lazy` arm in `ProductSection`; warm
// the module so a Suspense boundary can never race a cold transform against the
// default 1 s findBy budget (the documented lazy-in-waitFor hazard).
beforeAll(async () => {
  await import("../components/nav-rail/settingsSection");
});

beforeEach(() => {
  vi.clearAllMocks();
  getConfigSchema.mockResolvedValue(realSchema);
  getConfig.mockResolvedValue({ config, secrets: {} });
  // jsdom shares one window.location per file: without this reset a test that
  // writes `?facet=` deep-links the NEXT test into that facet.
  window.history.replaceState({}, "", "/settings");
});

/** The rail rows, in render order. */
async function renderRail() {
  render(<SettingsFacetsSection />);
  // "Models & Inference" is order 1 and always has schema sections.
  await screen.findByRole("button", { name: "Models & Inference" });
  return screen.getAllByRole("button");
}

describe("NavRail settings facet section", () => {
  test("renders a row per visible facet, titled from the facet model", async () => {
    await renderRail();

    for (const title of [
      "Models & Inference",
      "Agent & Tools",
      "Plugins",
      "Security & Access",
      "Workspace",
    ]) {
      expect(screen.getByRole("button", { name: title })).toBeTruthy();
    }
  });

  test("marks the first visible facet current when no ?facet= is set", async () => {
    await renderRail();
    // The shell lands on the first visible facet when the param is absent, so
    // the rail must agree on the very first paint.
    expect(
      screen.getByRole("button", { name: "Models & Inference" }).getAttribute("aria-current"),
    ).toBe("page");
  });

  test("marks the deep-linked facet current, not the first one", async () => {
    window.history.replaceState({}, "", "/settings?facet=workspace");
    await renderRail();

    expect(
      screen.getByRole("button", { name: "Workspace" }).getAttribute("aria-current"),
    ).toBe("page");
    expect(
      screen.getByRole("button", { name: "Models & Inference" }).getAttribute("aria-current"),
    ).toBeNull();
  });

  test("a facet click sets ?facet= and PRESERVES every other param", async () => {
    // The Automation pane is deep-linked as `/settings?facet=automation&session=<id>`;
    // dropping `session` on a facet switch is the regression this guards.
    window.history.replaceState({}, "", "/settings?facet=automation&session=abc123");
    await renderRail();

    await userEvent.click(screen.getByRole("button", { name: "Workspace" }));

    await waitFor(() => {
      const params = new URLSearchParams(window.location.search);
      expect(params.get("facet")).toBe("workspace");
      expect(params.get("session")).toBe("abc123");
    });
  });

  test("offers no facet the shell would refuse to open", async () => {
    const rows = await renderRail();
    const labels = rows.map((b) => b.textContent?.trim());

    // `other` is the fallback bucket for sections with an unknown `x-group`.
    // A clean schema puts nothing there, so offering the row would strand the
    // user on an empty facet the shell drops them out of.
    expect(labels).not.toContain("Other");
  });
});
