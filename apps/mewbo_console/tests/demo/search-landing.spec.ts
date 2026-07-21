import { test, expect } from "./fixtures";
import { OSS_REPO_SCOUT_WS, searchLandingHref } from "./search-helpers";

/**
 * Shot searchLanding (01) — the Agentic Search workspace gallery: the hero
 * composer, the active workspace's health-stats band ("sources mapped",
 * "graph nodes·edges", "memory notes"), its past-query replay chips, and the
 * workspace grid (OSS Repo Scout / Knowledge graph / Beacon Ops / … — 8
 * seeded workspaces total).
 *
 * `?ws=ws-oss-repo-scout` makes OSS Repo Scout the active workspace
 * deterministically — the URL `ws` param always wins over localStorage (see
 * `AgenticSearchView`'s "URL is the single source of truth" contract) —
 * rather than relying on a workspace-picker click or incidental seed
 * ordering. The active workspace's card renders the selected/highlighted
 * treatment and feeds the hero's stats band + past-query chips.
 *
 * Existing placeholder raster framing mirrors `wiki-landing.spec.ts`'s
 * ~1900x1400 choice — the two landing galleries share the same overall shape
 * (hero + card grid).
 */
test.use({ viewport: { width: 1900, height: 1400 } });

test("searchLanding — workspace gallery + stats band", async ({ page, demo }) => {
  await page.goto(searchLandingHref(OSS_REPO_SCOUT_WS));

  await expect(page.getByRole("heading", { level: 1, name: "Agentic Search" })).toBeVisible();

  // Workspace grid — the seeded gallery (8 workspaces; these three anchor it
  // per the seed contract).
  await expect(page.getByRole("heading", { name: "OSS Repo Scout" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Knowledge graph" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Beacon Ops" })).toBeVisible();

  // WorkspaceHealthBand — wait for the POPULATED stat numbers, not just the
  // labels (which render immediately over a pulsing number skeleton). All
  // three stats resolve together from the one `graph/summary` query, so
  // gating on the distinctive graph-size VALUE proves the band is fully
  // populated at capture time — pinning it against a blank↔number byte-diff
  // across runs. OSS Repo Scout: 6/7 sources mapped · 86·95 nodes·edges · 32
  // memory notes (verified summary payload).
  await expect(page.getByText(/sources? mapped/)).toBeVisible();
  await expect(page.getByText("graph nodes·edges")).toBeVisible();
  await expect(page.getByText("86·95")).toBeVisible();

  // Past-query replay chips (History icon = "Replay this search" — a run_id
  // is present, so it's a replay, not a fresh-run prefill). OSS Repo Scout's
  // history deep-links to run-cc-plugins / run-oss-agents / run-bearlike.
  await expect(page.getByTitle("Replay this search").first()).toBeVisible();

  await demo.capturePage("searchLanding");
});
