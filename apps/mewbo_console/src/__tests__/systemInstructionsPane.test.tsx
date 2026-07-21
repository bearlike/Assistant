/**
 * SystemInstructionsPane — the variable reference table's value affordances.
 *
 * The pane's job here is length-adaptive: a short `values` list is a fact you
 * read (chips), a long one is a thing you search (a count that opens a
 * filterable `<Command>` popover), and a variable with no enumerable source
 * (`values: null`) must show NO affordance at all rather than an empty state.
 * Clicking any value drops it at the editor's cursor, which is what makes the
 * table useful next to an editor instead of merely informative.
 *
 * The pane is a zero-prop facet pane, so it is rendered directly rather than
 * through the Settings shell: no `?facet=` URL to seed, and no `React.lazy`
 * boundary to pre-warm (that hazard belongs to the shell's render site).
 * `../api/systemInstructions` is mocked at the transport seam so the real
 * hooks, the real TanStack cache and the real CodeMirror editor all stay live.
 */
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { SystemInstructionsPane } from "../components/settings/panes/SystemInstructionsPane";
import * as api from "../api/systemInstructions";
import type { SystemInstructionsVariable } from "../api/systemInstructions";

vi.mock("../api/systemInstructions", () => ({
  getSystemInstructions: vi.fn(),
  putSystemInstructions: vi.fn(),
  previewSystemInstructions: vi.fn(),
  listSystemInstructionsVariables: vi.fn(),
}));

const getDoc = vi.mocked(api.getSystemInstructions);
const listVariables = vi.mocked(api.listSystemInstructionsVariables);

/** A `closed` enum: short enough to render inline as chips. */
const ORIGIN: SystemInstructionsVariable = {
  name: "origin",
  type: "string",
  description: "Coarse provenance of the session.",
  values: ["user", "wiki", "search", "channel", "mobile", "structured", "draft", "apps"],
  valuesKind: "closed",
  valuesNote: "Every origin core can stamp. A session always carries exactly one of them.",
};

/** `known` and long: must collapse to a count + popover. */
const TOOLS: SystemInstructionsVariable = {
  name: "tools",
  type: "array",
  description: "Tools bound for this session.",
  values: Array.from({ length: 41 }, (_, i) => (i === 0 ? "bash" : `tool_${i}`)),
  valuesKind: "known",
  valuesNote:
    "Every tool this deployment can bind. A session's actual list is narrower, so test for a " +
    "tool rather than assuming this whole list is present, and remember a new plugin can add to it.",
};

/** No enumerable source: must render no value affordance whatsoever. */
const SESSION_ID: SystemInstructionsVariable = {
  name: "session_id",
  type: "string",
  description: "The id of the current session.",
  values: null,
  valuesKind: null,
  valuesNote: null,
};

const SURFACE: SystemInstructionsVariable = {
  name: "surface",
  type: "string",
  description: "The client surface that started this session.",
  values: ["cli", "console", "nextcloud-talk"],
  valuesKind: "known",
  valuesNote: "Every surface that stamps a session today.",
};

const VARIABLES = [SURFACE, ORIGIN, TOOLS, SESSION_ID];

function renderPane() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SystemInstructionsPane />
    </QueryClientProvider>
  );
}

/** Expand the collapsed `<details>` that wraps the variable reference table. */
async function openVariableReference(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByText("Available template variables"));
}

/** The row of the reference table for one variable. */
function rowFor(name: string): HTMLElement {
  const cell = screen.getByText(name, { selector: "td" });
  const row = cell.closest("tr");
  if (!row) throw new Error(`no table row for variable ${name}`);
  return row;
}

/** The editor's current text, read off CodeMirror's rendered content. */
function editorText(container: HTMLElement): string {
  return container.querySelector(".cm-content")?.textContent ?? "";
}

beforeEach(() => {
  getDoc.mockResolvedValue({
    template: "",
    enabled: true,
    updatedAt: "2026-07-14T00:00:00Z",
    lastError: null,
  });
  listVariables.mockResolvedValue(VARIABLES);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("SystemInstructionsPane variable values", () => {
  test("a short list renders every value as a chip, labelled by its kind", async () => {
    const user = userEvent.setup();
    renderPane();
    await openVariableReference(user);

    const row = rowFor("origin");
    // `closed` promises a session's value is always in the list.
    expect(within(row).getByText("Always one of:")).toBeInTheDocument();
    for (const value of ORIGIN.values ?? []) {
      expect(within(row).getByRole("button", { name: value })).toBeInTheDocument();
    }
  });

  test("a long list collapses to a count that opens a filterable popover", async () => {
    const user = userEvent.setup();
    renderPane();
    await openVariableReference(user);

    const row = rowFor("tools");
    // `known` is the weaker promise: what this deployment has, not a closed set.
    expect(within(row).getByText("Available here:")).toBeInTheDocument();

    // The 41 names are NOT inline — only their count is.
    expect(within(row).queryByRole("button", { name: "tool_7" })).not.toBeInTheDocument();
    const trigger = within(row).getByRole("button", { name: /browse the 41 values of tools/i });
    expect(trigger).toHaveTextContent("41 values");

    await user.click(trigger);
    const filter = await screen.findByPlaceholderText(/filter tools/i);
    expect(await screen.findByText("tool_7")).toBeInTheDocument();

    // The filter narrows the list rather than merely highlighting a match.
    await user.type(filter, "tool_31");
    expect(await screen.findByText("tool_31")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("tool_7")).not.toBeInTheDocument());
  });

  test("a variable with no enumerable values renders no value affordance", async () => {
    const user = userEvent.setup();
    renderPane();
    await openVariableReference(user);

    const row = rowFor("session_id");
    expect(within(row).getByText(SESSION_ID.description)).toBeInTheDocument();
    // No chips, no count trigger, and no "(none)" empty state either.
    expect(within(row).queryByRole("button")).not.toBeInTheDocument();
    expect(within(row).queryByText(/values/i)).not.toBeInTheDocument();
    expect(within(row).queryByText(/one of|available here/i)).not.toBeInTheDocument();
  });

  test("clicking a chip inserts the bare value at the editor's cursor", async () => {
    const user = userEvent.setup();
    const { container } = renderPane();
    await openVariableReference(user);

    expect(editorText(container)).not.toContain("channel");

    await user.click(within(rowFor("origin")).getByRole("button", { name: "channel" }));

    // The bare string, with no quoting guessed around it.
    await waitFor(() => expect(editorText(container)).toContain("channel"));
    expect(editorText(container)).not.toContain('"channel"');
  });

  test("the preview surface tabs come from the surface variable, not a hardcoded list", async () => {
    renderPane();

    // Including the one whose casing has to be prettified from the raw id.
    expect(await screen.findByRole("tab", { name: "Nextcloud Talk" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "CLI" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Console" })).toBeInTheDocument();

    // "Android" was in the old hardcoded PREVIEW_SURFACES but is absent from
    // this deployment's payload, so it must not render.
    expect(screen.queryByRole("tab", { name: "Android" })).not.toBeInTheDocument();
  });
});
