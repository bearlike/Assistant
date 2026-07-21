/**
 * ProjectSettingsDialog — editable wiki project settings.
 *
 * The load-bearing behaviours, in the order they can bite:
 *   - the settings DTO seeds every field (a mis-seeded form silently PATCHes
 *     the wrong values back);
 *   - only the DIRTY subset is sent (an untouched field must not be echoed);
 *   - a changed `ref` invalidates freshness (else the badge keeps reporting
 *     drift against the OLD branch), and an unchanged one does not;
 *   - `graphOnly` is offered only when the SERVER flags it editable — this is
 *     the developer-mode gate, so it fails closed;
 *   - a 403 pins to the graph-only switch and a 409 fills the dialog banner —
 *     neither may escape as a raw error card / toast.
 */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectSettingsDialog } from "@/components/wiki/ProjectSettingsDialog";
import * as client from "@/components/wiki/api/client";
import type {
  GitProjectSettings,
  CatalogProjectSettings,
  Project,
} from "@/components/wiki/api/types";

vi.mock("@/components/wiki/api/client", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/components/wiki/api/client")>();
  return {
    ...actual, // keeps the real `isWikiError` type guard on the error paths
    getProjectSettings: vi.fn(),
    updateProject: vi.fn(),
    listProjects: vi.fn(),
    listLanguages: vi.fn(),
    listBranches: vi.fn(),
  };
});

// The model picker would otherwise fetch /api/models.
vi.mock("@/hooks/useModels", () => ({
  useModels: () => ({
    models: ["anthropic/claude-sonnet-5"],
    defaultModel: "anthropic/claude-sonnet-5",
    capabilities: {},
    loading: false,
    error: null,
    refresh: vi.fn(),
  }),
}));

// Credential coverage falls back to this list only when the DTO omits
// `credential`; keep it inert so no /v1/git/credentials fetch happens.
vi.mock("@/hooks/useGitCredentials", () => ({
  useGitCredentials: () => ({
    credentials: [],
    loading: false,
    error: null,
    refresh: vi.fn(),
  }),
}));

const getProjectSettings = vi.mocked(client.getProjectSettings);
const updateProject = vi.mocked(client.updateProject);
const listProjects = vi.mocked(client.listProjects);
const listLanguages = vi.mocked(client.listLanguages);
const listBranches = vi.mocked(client.listBranches);

const SLUG = "git.example.com/acme/widgets";
const REPO_URL = "https://git.example.com/acme/widgets";

const PROJECT: Project = {
  slug: SLUG,
  source: "gitea",
  lang: "TypeScript",
  indexedAt: "2026-07-01T00:00:00Z",
  pages: 12,
  desc: "Widgets",
  repoUrl: REPO_URL,
};

/** Every field editable — the ordinary git project case. */
const ALL_EDITABLE = {
  model: true,
  ref: true,
  depth: true,
  language: true,
  filterMode: true,
  dirs: true,
  files: true,
  graphOnly: true,
  desc: true,
};

const SETTINGS: GitProjectSettings = {
  slug: SLUG,
  model: "anthropic/claude-sonnet-5",
  ref: "develop",
  depth: "concise",
  language: "en",
  filterMode: "include",
  dirs: ["src", "apps"],
  files: ["**/*.ts"],
  graphOnly: false,
  desc: "Widget factory",
  credential: { present: true, scope: "git.example.com", scopeType: "host" },
  editable: ALL_EDITABLE,
};

function renderDialog(qc = makeClient()) {
  render(
    <QueryClientProvider client={qc}>
      <ProjectSettingsDialog slug={SLUG} open onOpenChange={vi.fn()} />
    </QueryClientProvider>,
  );
  return qc;
}

function makeClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

/**
 * Wait for the form to be SEEDED from the DTO — not merely mounted. `form.reset`
 * runs in an effect after the first render that shows the form, so asserting (or
 * typing) before it lands races the seed and gets clobbered. Waiting on a field
 * whose DTO value differs from the inert default is the signal that it landed.
 */
async function awaitSeeded(label = "Depth", value = "concise") {
  await screen.findByRole("button", { name: /save changes/i });
  await waitFor(() => expect(screen.getByLabelText(label)).toHaveValue(value));
}

/**
 * Wait until the remote branch list has landed. The Branch select is disabled
 * while it's in flight (the wizard does the same), so interacting before this
 * silently no-ops — the pinned ref alone is selectable from the seed.
 */
async function awaitBranches() {
  await screen.findByRole("option", { name: "main" });
}

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  getProjectSettings.mockResolvedValue(SETTINGS);
  updateProject.mockResolvedValue(SETTINGS);
  listProjects.mockResolvedValue([PROJECT]);
  listLanguages.mockResolvedValue([
    { id: "en", label: "English" },
    { id: "fr", label: "French" },
  ]);
  listBranches.mockResolvedValue({
    branches: ["main", "develop"],
    defaultBranch: "main",
  });
});

describe("ProjectSettingsDialog — DTO → form", () => {
  it("seeds every field from the settings payload", async () => {
    renderDialog();
    await awaitSeeded();

    await waitFor(() =>
      expect(screen.getByLabelText("Branch")).toHaveValue("develop"),
    );
    expect(screen.getByLabelText("Depth")).toHaveValue("concise");
    expect(screen.getByLabelText("Language")).toHaveValue("en");
    expect(screen.getByLabelText("Filter mode")).toHaveValue("include");
    expect(screen.getByLabelText(/directories to include/i)).toHaveValue(
      "src\napps",
    );
    expect(screen.getByLabelText(/files to include/i)).toHaveValue("**/*.ts");
    expect(screen.getByLabelText("Description")).toHaveValue("Widget factory");
  });

  it("renders the repo as read-only identity and never an inline token field", async () => {
    renderDialog();
    await awaitSeeded();

    expect(screen.getByText(REPO_URL)).toBeInTheDocument();
    expect(screen.getByText(/delete this wiki and create a new one/i)).toBeInTheDocument();
    // The ONE credential chain owns tokens — the dialog only reports coverage.
    expect(screen.queryByLabelText(/token/i)).toBeNull();
    expect(screen.queryByPlaceholderText(/token/i)).toBeNull();
  });

  it("says an index-time change applies at the next index, not now", async () => {
    renderDialog();
    await awaitSeeded();
    expect(screen.getByText(/applies at the next index/i)).toBeInTheDocument();
  });

  it("says a description-only edit lands right away (it writes through)", async () => {
    const user = userEvent.setup();
    renderDialog();
    await awaitSeeded();

    const desc = screen.getByLabelText("Description");
    await user.clear(desc);
    await user.type(desc, "New blurb");

    expect(screen.getByText(/updates right away/i)).toBeInTheDocument();
    expect(screen.queryByText(/applies at the next index/i)).toBeNull();
  });
});

describe("ProjectSettingsDialog — dirty-subset PATCH", () => {
  it("sends only the fields the user touched", async () => {
    const user = userEvent.setup();
    renderDialog();
    await awaitSeeded();

    await user.selectOptions(screen.getByLabelText("Depth"), "comprehensive");
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => expect(updateProject).toHaveBeenCalledTimes(1));
    expect(updateProject).toHaveBeenCalledWith(SLUG, { depth: "comprehensive" });
  });

  it("splits the dirs/files textareas into arrays", async () => {
    const user = userEvent.setup();
    renderDialog();
    await awaitSeeded();

    const dirs = screen.getByLabelText(/directories to include/i);
    await user.clear(dirs);
    await user.type(dirs, "src\npackages");
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => expect(updateProject).toHaveBeenCalledTimes(1));
    expect(updateProject).toHaveBeenCalledWith(SLUG, {
      dirs: ["src", "packages"],
    });
  });

  it("clears a pinned branch back to the default as an explicit null", async () => {
    const user = userEvent.setup();
    renderDialog();
    await awaitSeeded();
    await awaitBranches();

    expect(screen.getByLabelText("Branch")).toHaveValue("develop");
    await user.selectOptions(screen.getByLabelText("Branch"), "");
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => expect(updateProject).toHaveBeenCalledTimes(1));
    expect(updateProject).toHaveBeenCalledWith(SLUG, { ref: null });
  });

  it("keeps Save disabled until something actually changes", async () => {
    renderDialog();
    await awaitSeeded();
    expect(screen.getByRole("button", { name: /save changes/i })).toBeDisabled();
  });
});

describe("ProjectSettingsDialog — cache invalidation", () => {
  const FRESHNESS_KEY = ["wiki", "freshness", SLUG];

  it("invalidates freshness when the ref changed", async () => {
    const user = userEvent.setup();
    const qc = makeClient();
    qc.setQueryData(FRESHNESS_KEY, { behindBy: 0, upToDate: true });
    renderDialog(qc);
    await awaitSeeded();
    await awaitBranches();

    await user.selectOptions(screen.getByLabelText("Branch"), "main");
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => expect(updateProject).toHaveBeenCalled());
    await waitFor(() =>
      expect(qc.getQueryState(FRESHNESS_KEY)?.isInvalidated).toBe(true),
    );
  });

  it("leaves freshness alone when the ref did not change", async () => {
    const user = userEvent.setup();
    const qc = makeClient();
    qc.setQueryData(FRESHNESS_KEY, { behindBy: 0, upToDate: true });
    renderDialog(qc);
    await awaitSeeded();

    await user.selectOptions(screen.getByLabelText("Language"), "fr");
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() =>
      expect(updateProject).toHaveBeenCalledWith(SLUG, { language: "fr" }),
    );
    expect(qc.getQueryState(FRESHNESS_KEY)?.isInvalidated).toBe(false);
  });
});

describe("ProjectSettingsDialog — re-index hand-off", () => {
  /**
   * A PATCH re-runs nothing. Every field except `desc` only takes effect at the
   * next index, so an index-time save hands straight off to the screen's
   * EXISTING re-index CTA — one refresh path, not a second one.
   */
  function renderWithRefresh(onRefresh: () => void) {
    render(
      <QueryClientProvider client={makeClient()}>
        <ProjectSettingsDialog
          slug={SLUG}
          open
          onOpenChange={vi.fn()}
          onRefresh={onRefresh}
        />
      </QueryClientProvider>,
    );
  }

  it("opens the re-index CTA after saving an index-time field", async () => {
    const user = userEvent.setup();
    const onRefresh = vi.fn();
    renderWithRefresh(onRefresh);
    await awaitSeeded();

    await user.selectOptions(screen.getByLabelText("Depth"), "comprehensive");
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => expect(updateProject).toHaveBeenCalled());
    await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  });

  it("does NOT nag for a re-index after a description-only save", async () => {
    const user = userEvent.setup();
    const onRefresh = vi.fn();
    renderWithRefresh(onRefresh);
    await awaitSeeded();

    const desc = screen.getByLabelText("Description");
    await user.clear(desc);
    await user.type(desc, "New blurb");
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() =>
      expect(updateProject).toHaveBeenCalledWith(SLUG, { desc: "New blurb" }),
    );
    expect(onRefresh).not.toHaveBeenCalled();
  });
});

describe("ProjectSettingsDialog — server-gated fields", () => {
  it("hides graph-only when the server does not flag it editable", async () => {
    getProjectSettings.mockResolvedValue({
      ...SETTINGS,
      editable: { ...ALL_EDITABLE, graphOnly: false },
    });
    renderDialog();
    await awaitSeeded();

    expect(screen.queryByLabelText(/graph only/i)).toBeNull();
  });

  it("shows graph-only when the server flags it editable", async () => {
    renderDialog();
    await awaitSeeded();
    expect(screen.getByLabelText(/graph only/i)).toBeInTheDocument();
  });

  it("offers nothing the server did not flag (fail closed)", async () => {
    getProjectSettings.mockResolvedValue({ ...SETTINGS, editable: {} });
    renderDialog();
    await screen.findByText(/no editable settings/i);

    expect(screen.queryByLabelText("Branch")).toBeNull();
    expect(screen.queryByLabelText("Depth")).toBeNull();
  });

  it("never offers repo identity, even though the server flags it editable", async () => {
    getProjectSettings.mockResolvedValue({
      ...SETTINGS,
      // The server DOES accept these (cosmetic re-normalisation only; a real
      // repo change is a 409) — the UI still refuses to offer them.
      editable: { ...ALL_EDITABLE, repoUrl: true, platform: true },
    });
    renderDialog();
    await awaitSeeded();

    expect(screen.queryByLabelText(/repository url/i)).toBeNull();
    expect(screen.queryByLabelText(/platform/i)).toBeNull();
    // …and it stays visible as read-only identity.
    expect(screen.getByText(REPO_URL)).toBeInTheDocument();
  });

  it("reduces to the display fields for a catalog project", async () => {
    // The server's real catalog shape: no index-time fields at the top level,
    // and every index-time flag explicitly false — only `desc` is editable.
    const catalog: CatalogProjectSettings = {
      kind: "catalog",
      slug: "my-notes",
      desc: "Notes",
      editable: {
        model: false,
        ref: false,
        depth: false,
        language: false,
        filterMode: false,
        dirs: false,
        files: false,
        graphOnly: false,
        desc: true,
      },
    };
    getProjectSettings.mockResolvedValue(catalog);
    renderDialog();
    await awaitSeeded("Description", "Notes");

    expect(screen.getByText(/this is a document catalog/i)).toBeInTheDocument();
    expect(screen.queryByLabelText("Branch")).toBeNull();
    expect(screen.queryByLabelText("Filter mode")).toBeNull();
    expect(screen.queryByLabelText(/graph only/i)).toBeNull();
    expect(screen.queryByLabelText("Depth")).toBeNull();
  });
});

describe("ProjectSettingsDialog — errors stay inside the dialog", () => {
  /** Build the typed error the wiki client throws for a given HTTP shape. */
  function wikiError(code: string, message: string) {
    const err = new Error(message) as Error & { code: string };
    err.code = code;
    return err;
  }

  it("pins a 403 developer-mode rejection to the graph-only switch", async () => {
    const user = userEvent.setup();
    updateProject.mockRejectedValue(
      wikiError("forbidden", "Developer mode is disabled"),
    );
    renderDialog();
    await awaitSeeded();

    await user.click(screen.getByLabelText(/graph only/i));
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() =>
      expect(updateProject).toHaveBeenCalledWith(SLUG, { graphOnly: true }),
    );
    expect(
      await screen.findByText("Developer mode is disabled"),
    ).toBeInTheDocument();
  });

  it("shows a 409 identity conflict as a dialog banner, not a raw card", async () => {
    const user = userEvent.setup();
    updateProject.mockRejectedValue(
      wikiError("validation", "Repository identity cannot be changed"),
    );
    renderDialog();
    await awaitSeeded();

    await user.selectOptions(screen.getByLabelText("Depth"), "comprehensive");
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    const banner = await screen.findByText("Repository identity cannot be changed");
    expect(banner).toBeInTheDocument();
    // The dialog stays open so the edit isn't lost.
    expect(screen.getByRole("button", { name: /save changes/i })).toBeInTheDocument();
  });

  it("pins per-field 400 validation errors to their inputs", async () => {
    const user = userEvent.setup();
    const err = wikiError("validation", "Invalid settings") as Error & {
      code: string;
      fields?: Record<string, string>;
    };
    err.fields = { language: "Unsupported language" };
    updateProject.mockRejectedValue(err);
    renderDialog();
    await awaitSeeded();

    await user.selectOptions(screen.getByLabelText("Language"), "fr");
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    expect(await screen.findByText("Unsupported language")).toBeInTheDocument();
  });

  it("renders a load failure instead of an empty form", async () => {
    getProjectSettings.mockRejectedValue(wikiError("not_found", "No such project"));
    renderDialog();

    expect(await screen.findByText("No such project")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /save changes/i })).toBeNull();
  });
});

describe("ProjectSettingsDialog — credential coverage", () => {
  it("reports the server-resolved credential and links to Security", async () => {
    renderDialog();
    await awaitSeeded();

    expect(screen.getByText(/shared across this host/i)).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /security & access/i });
    expect(link).toHaveAttribute("href", "/settings");
  });

  it("says so when no stored credential covers the repo", async () => {
    getProjectSettings.mockResolvedValue({
      ...SETTINGS,
      credential: { present: false, scope: null, scopeType: null },
    });
    renderDialog();
    await awaitSeeded();

    expect(
      screen.getByText(/no stored credential covers this repo/i),
    ).toBeInTheDocument();
  });
});

describe("WikiTopBar → settings", () => {
  it("only mounts the dialog once the gear is clicked", async () => {
    const user = userEvent.setup();
    const { WikiTopBar } = await import("@/components/wiki/WikiTopBar");
    const qc = makeClient();
    render(
      <QueryClientProvider client={qc}>
        <WikiTopBar repo={SLUG} showSettings />
      </QueryClientProvider>,
    );

    expect(getProjectSettings).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: /wiki settings/i }));

    await waitFor(() => expect(getProjectSettings).toHaveBeenCalledWith(SLUG));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/wiki settings/i)).toBeInTheDocument();
  });
});
