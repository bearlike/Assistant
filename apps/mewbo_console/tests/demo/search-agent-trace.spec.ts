import { test, expect } from "./fixtures";
import { OSS_REPO_SCOUT_WS, RUN_OSS_AGENTS, searchRunHref } from "./search-helpers";

/**
 * Shot searchAgentTrace (04) — the Agent trace drawer (`TraceDrawer`, a
 * shadcn `Sheet` / Radix Dialog) opened over the run-oss-agents results
 * page, showing its 3 per-probe lanes (including the `scg-search`
 * coordinator lane — the root agent's own tool activity, `source_id === ""`,
 * per `components/agentic_search/CLAUDE.md` "Results-page top band").
 *
 * Same GET-snapshot-only deep-link contract as `search-results.spec.ts` — no
 * `page.route` stub needed; we wait for the terminal state before opening
 * the drawer so the captured trace is the finished run, not a still-
 * streaming one.
 */
test.use({ viewport: { width: 1900, height: 1400 } });

test("searchAgentTrace — trace drawer with per-probe lanes", async ({ page, demo }) => {
  await page.goto(searchRunHref(OSS_REPO_SCOUT_WS, RUN_OSS_AGENTS));

  // Terminal state, not a spinner, before opening the drawer — RunStats
  // renders the result-count part only for a finished run, and (unlike a bare
  // /complete/, which also matched a result-card paragraph) it's a single,
  // stable match. The drawer's own per-lane "N results" strips don't exist
  // yet — the drawer is still closed at this point.
  await expect(page.getByText(/\d+ results/)).toBeVisible();

  // The right rail's own "Agent trace" trigger — scoped to <aside> so this
  // doesn't collide with the run-meta row's duplicate trigger button (both
  // read "Agent trace" and open the same drawer; ResultsPanel renders only
  // one <aside>, the RightRail).
  await page.locator("aside").getByRole("button", { name: "Agent trace" }).click();

  const drawer = page.getByRole("dialog");
  await expect(drawer.getByText("Agent trace")).toBeVisible();
  // Seed contract: run-oss-agents has 3 trace lanes. `exact` disambiguates the
  // "3 lanes" count line from a probe log line reading "spawned 3 lanes".
  await expect(drawer.getByText("3 lanes", { exact: true })).toBeVisible();

  // A named probe lane from the seed contract. run-oss-agents' three lanes are
  // "coordinator" + two "scg-path-probe" probes; the lane name is embedded in a
  // longer status blob and the probe name recurs, so scope to the drawer + first().
  await expect(drawer.getByText("scg-path-probe").first()).toBeVisible();

  await demo.capturePage("searchAgentTrace");
});
