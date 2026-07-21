/**
 * NavRail — the wiki page tree (rail zone 3 on a page route).
 *
 * The rail is SITE navigation, so zone 3 carries "move to another page". A
 * table of contents is intra-document navigation and belongs beside the
 * document; an earlier revision had the two swapped.
 *
 * The contracts worth pinning are the ones that make it safe, not the ones that
 * make it look right:
 *
 *   1. Entries come from the page payload's `nav`, read off the SAME
 *      `useWikiPage` query the page populates — no second request, no context.
 *   2. Section-vs-page is decided STRUCTURALLY (does anything call this node its
 *      parent), not by nesting level — a top-level page with no children is
 *      still a page.
 *   3. Depth reads as indentation plus weight. No level may drop to a smaller
 *      step of the type scale.
 *
 * ⚠️ Routing setup is load-bearing here: `useWikiRoute` takes the PATHNAME from
 * wouter but the SEARCH string from `window.location` directly. So the memory
 * router gets a bare path and the slug is put on the real jsdom location — feed
 * the query string to the memory router instead and it becomes part of the
 * pathname, and the page id parses as `overview?slug=…`.
 */
import { cleanup, render as rtlRender, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { ProductSection } from "../components/nav-rail/sections";
import * as wikiClient from "../components/wiki/api/client";

vi.mock("../components/wiki/api/client", () => ({
  getPage: vi.fn(),
  listProjects: vi.fn().mockResolvedValue([]),
}));

const getPage = vi.mocked(wikiClient.getPage);

const SLUG = "github.com/acme/beacon";

// `parent` is what makes a node a section. "Guides" has children; "Overview"
// and "Changelog" are level-1 LEAVES — the case a level-based rule gets wrong.
const PAGE = {
  id: "overview",
  title: "Overview",
  blocks: [],
  toc: [],
  nav: [
    { id: "overview", label: "Overview", lvl: 1 as const },
    { id: "guides", label: "Guides", lvl: 1 as const },
    { id: "install", label: "Installing", lvl: 2 as const, parent: "guides" },
    { id: "deep", label: "Deep topic", lvl: 3 as const, parent: "install" },
    { id: "changelog", label: "Changelog", lvl: 1 as const },
  ],
};

function render(ui: ReactElement) {
  const { hook, history } = memoryLocation({ path: "/wiki/p/overview", record: true });
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const result = rtlRender(
    <QueryClientProvider client={qc}>
      <Router hook={hook}>{ui}</Router>
    </QueryClientProvider>,
  );
  return { ...result, history };
}

afterEach(cleanup);

beforeEach(() => {
  vi.clearAllMocks();
  getPage.mockResolvedValue(PAGE as never);
  // The slug reaches `useWikiRoute` through the REAL location, not the router.
  window.history.replaceState({}, "", `/wiki/p/overview?slug=${SLUG}`);
});

async function renderPages() {
  const rendered = render(<ProductSection product="wiki" />);
  await screen.findByRole("button", { name: /Guides/ });
  return rendered;
}

describe("NavRail wiki page tree", () => {
  test("reads entries from the page payload's nav, through the shared query", async () => {
    await renderPages();

    for (const label of ["Overview", "Guides", "Installing", "Changelog"]) {
      expect(screen.getByRole("button", { name: new RegExp(label) })).toBeTruthy();
    }
    // One fetch — the rail rides the page's own query rather than adding a call.
    expect(getPage).toHaveBeenCalledTimes(1);
    expect(getPage).toHaveBeenCalledWith(SLUG, "overview");
  });

  test("keeps 'All wikis' reachable from inside a page", async () => {
    await renderPages();
    expect(screen.getByRole("button", { name: /All wikis/ })).toBeTruthy();
  });

  test("marks the page being read as current", async () => {
    await renderPages();
    expect(
      screen.getByRole("button", { name: /Overview/ }).getAttribute("aria-current"),
    ).toBe("page");
    expect(
      screen.getByRole("button", { name: /Changelog/ }).getAttribute("aria-current"),
    ).toBeNull();
  });

  test("expresses depth as indentation and weight, never a smaller font", async () => {
    await renderPages();

    const l1 = screen.getByRole("button", { name: /Guides/ }).className;
    const l2 = screen.getByRole("button", { name: /Installing/ }).className;
    const l3 = screen.getByRole("button", { name: /Deep topic/ }).className;

    expect(l1).toContain("font-medium");
    expect(l2).toContain("pl-4");
    expect(l3).toContain("pl-6");

    for (const cls of [l1, l2, l3]) {
      expect(cls).toContain("text-sm");
      expect(cls).not.toContain("text-xs");
      expect(cls).not.toContain("text-2xs");
    }
  });

  test("every row carries a leading glyph", async () => {
    await renderPages();
    for (const label of ["Overview", "Guides", "Installing", "Changelog"]) {
      const row = screen.getByRole("button", { name: new RegExp(label) });
      expect(row.querySelector("svg")).toBeTruthy();
    }
  });

  test("a level-1 leaf gets the page glyph, not the section glyph", async () => {
    await renderPages();

    // Both are level 1 — only the child relationship distinguishes them, so a
    // level-based rule would give them the same glyph.
    const section = screen.getByRole("button", { name: /Guides/ }).querySelector("svg");
    const leaf = screen.getByRole("button", { name: /Changelog/ }).querySelector("svg");
    expect(section?.getAttribute("class")).not.toEqual(leaf?.getAttribute("class"));
  });

  test("a click navigates to that page through the wiki href builder", async () => {
    const { history } = await renderPages();
    await userEvent.click(screen.getByRole("button", { name: /Installing/ }));

    // `URLSearchParams` percent-encodes the slug's slashes.
    await waitFor(() =>
      expect(history[history.length - 1]).toBe(
        "/wiki/p/install?slug=github.com%2Facme%2Fbeacon",
      ),
    );
  });

  test("degrades to an empty state when the wiki has no pages", async () => {
    getPage.mockResolvedValue({ ...PAGE, nav: [] } as never);
    render(<ProductSection product="wiki" />);
    expect(await screen.findByText("No pages in this wiki.")).toBeTruthy();
  });
});
