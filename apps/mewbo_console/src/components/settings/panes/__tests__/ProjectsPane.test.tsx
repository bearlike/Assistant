/**
 * ProjectsPane — render test for the Workspace facet's managed-projects pane.
 *
 * Follows the `app.test.tsx` / `SettingsView.integration.test.tsx` pattern:
 * `vi.mock` the api/client surface the pane touches + a fresh
 * `QueryClientProvider` per render with retries off.
 *
 * Proves the four things the migration promised: the pane mounts with zero
 * props, the managed project list (and its worktrees) renders, the create form
 * validates before it calls the API, and deletion is gated by a shadcn
 * `<Dialog>` — NOT `window.confirm()`, which the old ProjectCard used.
 */
import {
  cleanup,
  render as rtlRender,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ProjectsPane } from "../ProjectsPane";
import * as client from "../../../../api/client";

vi.mock("../../../../api/client", () => ({
  listProjects: vi.fn(),
  createVirtualProject: vi.fn(),
  updateVirtualProject: vi.fn(),
  deleteVirtualProject: vi.fn(),
  listProjectBranches: vi.fn(),
  listWorktrees: vi.fn(),
  createWorktree: vi.fn(),
  deleteWorktree: vi.fn(),
}));

const listProjects = vi.mocked(client.listProjects);
const createVirtualProject = vi.mocked(client.createVirtualProject);
const deleteVirtualProject = vi.mocked(client.deleteVirtualProject);
const listProjectBranches = vi.mocked(client.listProjectBranches);
const listWorktrees = vi.mocked(client.listWorktrees);

function render(ui: ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  vi.clearAllMocks();
  listProjects.mockResolvedValue([
    // config-defined entry — must be filtered OUT of the managed pane.
    { name: "checkout", path: "/srv/checkout", source: "config" },
    {
      name: "Alpha",
      path: "/srv/alpha",
      description: "The alpha workspace",
      source: "managed",
      project_id: "p-alpha",
      is_worktree: false,
    },
  ]);
  createVirtualProject.mockResolvedValue({
    project_id: "p-beta",
    name: "Beta",
    description: "",
    path: "/srv/beta",
    path_source: "auto",
    folder_created: true,
    created_at: "",
    updated_at: "",
  });
  deleteVirtualProject.mockResolvedValue(undefined);
  listProjectBranches.mockResolvedValue({
    git_repo: true,
    branches: ["main"],
    current_branch: "main",
    branches_in_use: [],
  });
  listWorktrees.mockResolvedValue([
    {
      project_id: "wt-1",
      name: "Alpha (mewbo/feature)",
      branch: "mewbo/feature",
      path: "/srv/alpha/.mewbo/worktrees/feature",
      managed: true,
      is_worktree: true,
      clean: true,
    },
  ]);
});

afterEach(cleanup);

test("mounts and lists managed projects (config projects filtered out)", async () => {
  render(<ProjectsPane />);

  expect(
    screen.getByRole("heading", { name: /managed projects/i }),
  ).toBeInTheDocument();

  expect(await screen.findByText("Alpha")).toBeInTheDocument();
  expect(screen.getByText("/srv/alpha")).toBeInTheDocument();
  // config-source project belongs to the schema-driven section, not this pane
  expect(screen.queryByText("checkout")).not.toBeInTheDocument();

  // Worktrees for the project render through the shared project-git query.
  expect(await screen.findByText("mewbo/feature")).toBeInTheDocument();
});

test("create form validates before hitting the API", async () => {
  const user = userEvent.setup();
  render(<ProjectsPane />);
  await screen.findByText("Alpha");

  await user.click(screen.getByRole("button", { name: /new project/i }));
  await user.click(screen.getByRole("button", { name: /create project/i }));

  expect(await screen.findByText(/name is required/i)).toBeInTheDocument();
  expect(createVirtualProject).not.toHaveBeenCalled();

  // `/name/i` alone would also match the worktree panel's "New branch name".
  await user.type(screen.getByLabelText(/^name \*$/i), "Beta");
  await user.click(screen.getByRole("button", { name: /create project/i }));

  await waitFor(() => {
    expect(createVirtualProject).toHaveBeenCalledWith("Beta", "", undefined);
  });
});

test("deleting a project is gated by a Dialog, not window.confirm", async () => {
  const user = userEvent.setup();
  const confirmSpy = vi
    .spyOn(window, "confirm")
    .mockImplementation(() => true);
  render(<ProjectsPane />);
  await screen.findByText("Alpha");

  await user.click(screen.getByRole("button", { name: /delete project alpha/i }));

  // The click alone must not delete anything — the dialog gates it.
  expect(deleteVirtualProject).not.toHaveBeenCalled();
  const dialog = await screen.findByRole("dialog");
  expect(dialog).toHaveTextContent(/delete project\?/i);
  expect(dialog).toHaveTextContent(/cannot be undone/i);

  await user.click(within(dialog).getByRole("button", { name: /^delete project$/i }));

  await waitFor(() => {
    expect(deleteVirtualProject).toHaveBeenCalledWith("p-alpha");
  });
  expect(confirmSpy).not.toHaveBeenCalled();
});
