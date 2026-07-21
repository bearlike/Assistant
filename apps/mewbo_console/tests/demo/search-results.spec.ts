import { test, expect } from "./fixtures";
import { OSS_REPO_SCOUT_WS, RUN_OSS_AGENTS, searchRunHref } from "./search-helpers";

/**
 * Shot searchResults (02) — a completed run's results page: the Synthesis
 * card (with its cited RANK/REPO/STARS/LANGUAGE table), the ranked result
 * cards (e.g. `anthropics/claude-code`), and the right rail (per-lane trace
 * summary + related questions).
 *
 * Deep-link contract (verified in recon): `/search?ws=...&run=...` on a
 * COMPLETED run fires exactly one `GET /api/agentic_search/runs/<id>`
 * snapshot + an SSE attach that replays the stored event log and closes —
 * never a `POST /runs`, never a live LLM call. No `page.route` stub is
 * needed for this reason; we instead wait for the terminal state (RunStats'
 * honest one-line status reads "... complete"), never a spinner.
 */
test.use({ viewport: { width: 1900, height: 1400 } });

test("searchResults — synthesis + result cards + right rail", async ({ page, demo }) => {
  await page.goto(searchRunHref(OSS_REPO_SCOUT_WS, RUN_OSS_AGENTS));

  // Terminal state, not a spinner — RunStats renders the result-count part
  // ONLY for a finished run (while streaming it shows "streaming · Ns"), so
  // this both proves the completed snapshot rendered AND is a single, stable
  // match. (A bare /complete/ was a strict-mode violation: "complete" also
  // appears as a substring in a result-card paragraph.)
  await expect(page.getByText(/\d+ results/)).toBeVisible();

  // Synthesis card — the header label plus the seeded market-overview prose.
  // The answer.tldr renders as prose (not a markdown heading), so anchor on its
  // distinctive opening line, which is above the "Show more" fold.
  await expect(page.getByText("Synthesis", { exact: true })).toBeVisible();
  await expect(
    page.getByText(/open-source AI coding-agent space/),
  ).toBeVisible();

  // A named result from the seed contract. "anthropics/claude-code" appears in
  // several places (result card title + url, and the synthesis prose cites it),
  // so first() keeps it a single stable match.
  await expect(page.getByText("anthropics/claude-code").first()).toBeVisible();

  // Right rail (renders only >=1100px, satisfied by this viewport) — the
  // Agent trace entry point that anchors the rail's presence in the shot.
  await expect(
    page.locator("aside").getByRole("button", { name: "Agent trace" }),
  ).toBeVisible();

  await demo.capturePage("searchResults");
});
