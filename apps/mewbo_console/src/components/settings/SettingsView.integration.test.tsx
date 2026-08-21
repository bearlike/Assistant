/**
 * SettingsView — integration / render test against the REAL backend schema.
 *
 * The other settings tests (`SettingsModel.test.ts`, `SecretField.test.tsx`)
 * exercise the model and a single widget against HAND-BUILT fixtures. That
 * leaves the seam that actually ships untested:
 *
 *     real configs/app.schema.json  →  SettingsModel  →  sliced RJSF schema
 *       →  RjsfTheme widgets/fields  →  rendered controls
 *
 * A drift in the generated schema (a renamed `x-group`, a section whose def
 * RJSF can't render, a secret losing its `x-secret` flag, the `marketplaces`
 * array changing shape) would slip past the fixture tests but break the live
 * Settings page. This test wires `<SettingsView/>` to the REAL schema file
 * (imported from the repo root, never a fixture snapshot) and asserts the
 * faceted shell renders every facet that has sections, slices each section
 * through RJSF without throwing, and renders the bespoke widgets (SecretField,
 * the generic ArrayFieldTemplate, the secrets summary, the lazy ApiKeysView).
 *
 * Pattern follows `src/__tests__/app.test.tsx`: `vi.mock('../../api/client')`
 * for the whole client surface + a fresh `QueryClientProvider` per render with
 * retries off.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  cleanup,
  render as rtlRender,
  screen,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { afterEach, beforeAll, beforeEach, describe, expect, test, vi } from "vitest";

import { SettingsView } from "../SettingsView";
import { PANE_COUNTS } from "./panes";
import * as client from "../../api/client";

// --- the REAL backend-generated schema -------------------------------------
// Loaded from repo-root `configs/app.schema.json` (generated from `AppConfig`,
// carrying x-group / x-order / x-advanced / x-secret / writeOnly). We read it
// off disk rather than importing it, because the file lives OUTSIDE the package
// root and we want the test to fail loudly (not silently snapshot-drift) if it
// ever moves. Vitest runs with cwd at the package root (`apps/mewbo_console`),
// so the repo-root schema is two levels up.
const SCHEMA_PATH = resolve(process.cwd(), "../../configs/app.schema.json");
const realSchema = JSON.parse(readFileSync(SCHEMA_PATH, "utf-8")) as Record<
  string,
  unknown
>;

// --- mocked client surface (mirror of app.test.tsx) ------------------------
vi.mock("../../api/client", () => ({
  getConfigSchema: vi.fn(),
  getConfig: vi.fn(),
  patchConfig: vi.fn(),
  // ApiKeysView (lazy, Security facet) calls these — must resolve so it mounts.
  listApiKeys: vi.fn().mockResolvedValue([]),
  createApiKey: vi.fn(),
  revokeApiKey: vi.fn(),
  // PluginsPane (lazy, Plugins facet) calls these. Opening a facet mounts its
  // registered panes as well as its schema sections, so a pane's client calls
  // must resolve even when the test only asserts on the schema side — an
  // unmocked export throws out of the lazy chunk and takes the whole tree with
  // it (empty <body>), which reads as a confusing "element not found".
  listPlugins: vi.fn().mockResolvedValue([]),
  listMarketplacePlugins: vi.fn().mockResolvedValue([]),
  installPlugin: vi.fn(),
  uninstallPlugin: vi.fn(),
}));

const getConfigSchema = vi.mocked(client.getConfigSchema);
const getConfig = vi.mocked(client.getConfig);
const patchConfig = vi.mocked(client.patchConfig);
const listApiKeys = vi.mocked(client.listApiKeys);

// A representative config. Section values just need to be present so the model
// seeds form state and RJSF has data to render; the marketplaces entry is the
// load-bearing `list[str]` value asserted by the Agent & Tools facet test (it
// must render through the generic ArrayFieldTemplate).
const config: Record<string, unknown> = {
  llm: {
    default_model: "claude-opus-4-8",
    api_base: "",
    // api_key intentionally ABSENT — the backend strips secret values; the
    // field is driven entirely by the `secrets` is-set map below.
  },
  agent: { edit_tool: "" },
  permissions: {},
  plugins: {
    enabled: true,
    enabled_plugins: [],
    marketplaces: ["anthropics/claude-plugins-official"],
    marketplace_default_host: "github.com",
    install_path: "",
  },
  langfuse: { host: "https://cloud.langfuse.com" },
  home_assistant: { url: "" },
  cli: {},
  chat: {},
  runtime: {},
  storage: {},
  api: {},
};

// is-set map: secret_key is configured (masked/Replace), the rest are not.
const secrets: Record<string, boolean> = {
  "llm.api_key": false,
  "langfuse.secret_key": true,
  "langfuse.public_key": false,
  "home_assistant.token": false,
};

function render(ui: ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

// This project does not enable Vitest `globals`, so RTL's auto-cleanup is not
// registered — unmount each render explicitly between tests (the shared jsdom
// document would otherwise stack multiple SettingsView shells).
afterEach(cleanup);

// Pre-warm every lazily-imported pane this file's tests actually mount (same
// paths SettingsView's React.lazy uses) so each Suspense boundary resolves
// from the module cache instead of racing a dynamic chunk transform. That
// race is what made the default 1 s findBy time out on slow CI runners — the
// components themselves render their headings unconditionally. `ApiKeysView`
// was the original single entry here; `SecretsSummary` (Security facet),
// `PluginsPane` (Plugins facet) and `ProjectsPane` (Workspace facet) never
// were, which stayed marginal until the Repositories facet's own, heavier
// pane landed elsewhere in `panes.ts` and tipped the settings folder's
// aggregate transform load over the timeout under full-suite parallel load.
// EVERY pane in `FACET_PANES`, not a hand-picked subset. The first test below
// walks every facet, so it mounts every registered pane; warming four of ten
// left the rest racing their own cold transform, which is why this file passed
// alone and failed under full-suite load. Adding an unrelated test file
// anywhere in the repo was enough to tip it, so the symptom pointed at whatever
// landed last rather than at the gap here.
const WARMED_PANES = [
  import("./panes/SystemInstructionsPane"),
  import("./panes/PluginsPane"),
  import("./panes/TriggersPane"),
  import("../apps/AppsPane"),
  import("./panes/SecretsSummary"),
  import("../ApiKeysView"),
  import("./panes/IdentityAccessPane"),
  import("./panes/RepositoriesPane"),
  import("../GitCredentialsView"),
  import("./panes/ProjectsPane"),
];

beforeAll(async () => {
  // A list mirroring a registry goes stale silently, and the failure it causes
  // is a timeout in a DIFFERENT test — so tie the two together and let a newly
  // registered pane fail HERE, naming the real problem, instead of re-opening
  // the flake somewhere a reader will blame on load.
  const registered = Object.values(PANE_COUNTS).reduce<number>((n, c) => n + (c ?? 0), 0);
  expect(WARMED_PANES).toHaveLength(registered);
  await Promise.all(WARMED_PANES);
});

beforeEach(() => {
  vi.clearAllMocks();
  getConfigSchema.mockResolvedValue(realSchema);
  getConfig.mockResolvedValue({ config, secrets });
  patchConfig.mockResolvedValue({ config, secrets });
  listApiKeys.mockResolvedValue([]);
  // The active facet is `?facet=` (wouter's default browser-location router),
  // read but no longer WRITTEN by this shell — the NavRail's own Settings
  // zone (`nav-rail/settingsSection.tsx`) owns facet selection now. jsdom
  // shares ONE window.location across the whole file, so without this reset a
  // test that lands on a specific facet via `window.history.replaceState`
  // would deep-link the NEXT test into that same facet — the tests would
  // only pass in order.
  window.history.replaceState({}, "", "/settings");
});

/** Render on the default (first-visible) facet and wait for it to mount. */
async function renderSettings() {
  render(<SettingsView />);
  // "Language Model" is the llm section's humanized title, the first section
  // on the default "models" facet; its presence proves the model built from
  // the real schema and RJSF sliced the section without throwing.
  await screen.findByRole("heading", { name: "Language Model" });
}

/**
 * Render landed directly on one facet via `?facet=` — replaces the old
 * "render on default, then click the nav button" pattern. Clicking a facet is
 * no longer this component's job: the URL is the only interface left between
 * a facet picker (the NavRail) and this shell.
 */
async function renderOnFacet(facetId: string) {
  window.history.replaceState({}, "", `/settings?facet=${facetId}`);
  render(<SettingsView />);
}

describe("SettingsView against the real backend schema", () => {
  // 1 — every facet that has sections actually renders ITS OWN content when
  // selected via `?facet=`, rather than silently falling back to the default
  // facet. This is the guard against the silent-`other`-bucket trap (was: a
  // nav-button-existence check against this shell's own now-removed facet
  // nav; the NavRail's Settings zone has the equivalent "does every facet get
  // a row" coverage in its own test file).
  const SCHEMA_FACETS: Array<[id: string, title: string]> = [
    ["models", "Models & Inference"],
    ["agent", "Agent & Tools"],
    // Automation facet — TriggersConfig carries `x-group: automation`, so the
    // facet renders from the real schema. If `facets.ts` ever loses the
    // "automation" id, SettingsModel silently buckets Triggers into "Other"
    // and the fallback facet's heading would render instead, failing this.
    ["automation", "Automation"],
    ["integrations", "Integrations"],
    ["interface", "Interface"],
    ["server", "Server & Storage"],
    ["security", "Security & Access"],
    // Workspace facet (projects + wiki) — sections come from the schema, so it
    // renders even though the fixture config seeds no projects/wiki values.
    ["workspace", "Workspace"],
  ];

  // ONE CASE PER FACET, not one case looping over all eight — and the reason is
  // a failure mode, not tidiness. Eight full renders of this shell against the
  // REAL schema do not fit one default 5 s budget on a loaded runner, and the
  // timeout did not fail alone: the aborted loop resumed afterwards and ran its
  // in-loop `cleanup()` after the NEXT test had already rendered, unmounting
  // that test's tree so it failed against an empty `<body />` — a cascade that
  // reads as two unrelated defects. Per-case gives each facet its own budget,
  // names the offending facet when one regresses, and leaves teardown to
  // `afterEach(cleanup)`, which an aborted case cannot outlive.
  test.each(SCHEMA_FACETS)(
    "renders the %s facet's own heading when selected via ?facet=",
    async (facetId, title) => {
      await renderOnFacet(facetId);
      // `findAllByRole` rather than `findBy*`: a facet whose pane's own card
      // shares its title (Plugins does this, tested separately below) would
      // otherwise match two headings and throw. Any match proves the facet
      // resolved to itself instead of the fallback.
      expect(
        (await screen.findAllByRole("heading", { name: title })).length
      ).toBeGreaterThan(0);
    }
  );

  // 2 — the default facet renders section cards: a section heading + a real
  // form control, proving RJSF rendered the sliced section without throwing.
  test("default facet renders the llm section card with a rendered control", async () => {
    await renderSettings();
    // The llm section now carries a humanized `title=` in the backend schema
    // ("Language Model"), surfaced verbatim by SettingsModel as the card heading.
    expect(
      await screen.findByRole("heading", { name: "Language Model" })
    ).toBeInTheDocument();
    // Save/Reset footer is only emitted by SettingsSection once RJSF rendered.
    const resetButtons = await screen.findAllByRole("button", { name: "Reset" });
    expect(resetButtons.length).toBeGreaterThan(0);
    // At least one real input control rendered in the pane (RJSF produced the
    // sliced section's fields without throwing).
    expect(document.querySelectorAll("input").length).toBeGreaterThan(0);
  });

  // 3 — Plugins renders the plugins section + the generic array editor: the
  // marketplaces `list[str]` now routes through the console-themed
  // ArrayFieldTemplate (RepositoriesField is deleted). The seeded marketplace
  // entry appears in an editable input and a working "Add" control is present.
  //
  // Plugins is its own top-level facet: `PluginsPane` and `PluginsConfig`'s
  // `x-group` moved off `agent` together, per the "a pane sits in the facet
  // that owns its settings" rule. This test drives the REAL schema, so it is
  // also the guard against the silent-`other`-bucket trap — if `x-group` and
  // `FacetId` ever fall out of lockstep, this section vanishes and this fails.
  test("Plugins facet renders the plugins marketplaces array editor", async () => {
    await renderOnFacet("plugins");

    // The plugins section card — humanized schema title is "Plugins". Queried
    // as the labelled REGION, not the heading: the facet's own <h2> now carries
    // the same accessible name ("Plugins" is both the facet title and the
    // section title), so a heading query matches two elements and throws. Only
    // the SettingsCard is a region.
    expect(
      await screen.findByRole("region", { name: "Plugins" })
    ).toBeInTheDocument();

    // The seeded marketplace value is rendered in an editable input by the
    // generic ArrayFieldTemplate (item.children → RJSF string widget).
    const repoInput = await screen.findByDisplayValue(
      "anthropics/claude-plugins-official"
    );
    expect(repoInput.tagName).toBe("INPUT");

    // The generic array template exposes a visible "Add" button (it replaced
    // RJSF's invisible 0×0 default toolbar). Clicking it appends a new editable
    // row, proving the custom template — not the broken default — is wired.
    const addButtons = screen.getAllByRole("button", { name: "Add" });
    expect(addButtons.length).toBeGreaterThan(0);
    const inputsBefore = document.querySelectorAll("input").length;
    await userEvent.click(addButtons[0]);
    await screen.findByRole("region", { name: "Plugins" });
    expect(document.querySelectorAll("input").length).toBeGreaterThan(
      inputsBefore
    );
  });

  // 4 — llm.api_key renders as the write-only SecretField. Because
  // secrets['llm.api_key'] === false it is UNCONFIGURED → a password input.
  test("llm.api_key renders as an unconfigured SecretField (password input)", async () => {
    await renderSettings();
    // RJSF ids the field `root_api_key` within the llm section's form.
    const secretInput = document.querySelector<HTMLInputElement>(
      "#root_api_key"
    );
    expect(secretInput).not.toBeNull();
    expect(secretInput?.type).toBe("password");
    // Unconfigured → no masked "Configured"/Replace affordance for this field.
    // (The masked path is covered by SecretField.test.tsx; here we assert the
    // real-schema field reaches the SecretField widget at all.)
  });

  // 5 — Security & Access renders the secrets summary AND mounts the lazy
  // ApiKeysView (Suspense → use findBy*).
  test("Security & Access renders the secrets summary and ApiKeysView", async () => {
    await renderOnFacet("security");

    // Secrets summary: configured vs not-configured rendered from the is-set map.
    const summary = await screen.findByRole("region", {
      name: "Configured secrets",
    });
    const secretKeyRow = within(summary)
      .getByText("langfuse.secret_key")
      .closest("li");
    expect(secretKeyRow).not.toBeNull();
    expect(within(secretKeyRow as HTMLElement).getByText("set")).toBeInTheDocument();

    const apiKeyRow = within(summary).getByText("llm.api_key").closest("li");
    expect(apiKeyRow).not.toBeNull();
    expect(
      within(apiKeyRow as HTMLElement).getByText("not set")
    ).toBeInTheDocument();

    // The lazy ApiKeysView mounted (Suspense resolved). It no longer renders a
    // standalone "API Keys" page header — it was harmonized to section-card
    // chrome — so assert its section headings + the create-key control instead.
    // Pre-warmed in beforeAll; the explicit timeout is a safety net for an
    // unusually slow CI runner (the default 1 s was too tight for the lazy
    // chunk to transform + resolve).
    expect(
      await screen.findByRole(
        "heading",
        { name: "Create a new key" },
        { timeout: 5000 }
      )
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /Issued keys/ })
    ).toBeInTheDocument();
    expect(
      screen.getByRole("textbox", { name: "New key label" })
    ).toBeInTheDocument();
  });

  // 6 — `?facet=` deep-linking. Landing with a facet in the URL selects it
  // (instead of the default first facet) — a pane deep-link such as
  // `/settings?facet=automation&session=<id>` depends on this. This shell no
  // longer WRITES `?facet=` itself (the NavRail's Settings zone does, and
  // covers "a facet switch preserves an unrelated param" in its own test
  // file), so the only thing left to prove here is that this shell doesn't
  // clobber a param it doesn't own.
  test("?facet= selects the facet on mount and leaves an unrelated param alone", async () => {
    window.history.replaceState({}, "", "/settings?facet=security&session=abc123");
    render(<SettingsView />);

    // Landed directly on Security (a pane-only facet).
    expect(
      await screen.findByRole("region", { name: "Configured secrets" })
    ).toBeInTheDocument();

    const params = new URLSearchParams(window.location.search);
    expect(params.get("facet")).toBe("security");
    expect(params.get("session")).toBe("abc123");
  });

  // 7 — an unknown `?facet=` falls back to the first visible facet rather than
  // rendering an empty pane.
  test("an unknown ?facet= falls back to the first facet", async () => {
    window.history.replaceState({}, "", "/settings?facet=nonsense");
    render(<SettingsView />);
    expect(
      await screen.findByRole("heading", { name: "Language Model" })
    ).toBeInTheDocument();
  });
});
