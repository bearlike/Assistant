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

// Pre-warm the lazily-imported ApiKeysView module (same path SettingsView's
// React.lazy uses) so the Security facet's Suspense boundary resolves from the
// module cache instead of racing a dynamic chunk transform. That race is what
// made the default 1 s findBy time out on slow CI runners — the component
// itself renders its "API Keys" heading unconditionally.
beforeAll(async () => {
  await import("../ApiKeysView");
});

beforeEach(() => {
  vi.clearAllMocks();
  getConfigSchema.mockResolvedValue(realSchema);
  getConfig.mockResolvedValue({ config, secrets });
  patchConfig.mockResolvedValue({ config, secrets });
  listApiKeys.mockResolvedValue([]);
});

/** Wait for the shell to finish loading (first facet heading rendered). */
async function renderSettings() {
  render(<SettingsView />);
  // "Models & Inference" is the default facet; its nav button proves the model
  // built from the real schema and the shell mounted.
  await screen.findByRole("button", { name: "Models & Inference" });
}

describe("SettingsView against the real backend schema", () => {
  // 1 — the facet nav renders every facet that has sections (by accessible name)
  test("renders the expected facet navigation from the real schema", async () => {
    await renderSettings();
    const nav = screen.getByRole("navigation", { name: "Settings" });
    for (const name of [
      "Models & Inference",
      "Agent & Tools",
      "Integrations",
      "Interface",
      "Server & Storage",
      "Security & Access",
      // Workspace facet (projects + wiki) — sections come from the schema, so it
      // renders even though the fixture config seeds no projects/wiki values.
      "Workspace",
    ]) {
      expect(
        within(nav).getByRole("button", { name })
      ).toBeInTheDocument();
    }
  });

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

  // 3 — Agent & Tools renders the plugins section + the generic array editor:
  // the marketplaces `list[str]` now routes through the console-themed
  // ArrayFieldTemplate (RepositoriesField is deleted). The seeded marketplace
  // entry appears in an editable input and a working "Add" control is present.
  test("Agent & Tools facet renders the plugins marketplaces array editor", async () => {
    await renderSettings();
    await userEvent.click(
      screen.getByRole("button", { name: "Agent & Tools" })
    );

    // The plugins section card — humanized schema title is "Plugins".
    expect(
      await screen.findByRole("heading", { name: "Plugins" })
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
    await screen.findByRole("heading", { name: "Plugins" });
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
    await renderSettings();
    await userEvent.click(
      screen.getByRole("button", { name: "Security & Access" })
    );

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
});
