import { test, expect } from "./fixtures";
import { stubFreshness } from "./wiki-helpers";

/**
 * Shot wikiLanding (01) — the Agentic Wiki gallery: 12 seeded projects (the
 * richest three — Grove, Assistant, Beacon — anchor the wait) plus the
 * collapsed "Incomplete indexes" band fed by the 16 seeded interrupted jobs.
 *
 * The existing placeholder raster is ~1920x1386; this harness captures at
 * deviceScaleFactor 2 (playwright.demo.config.ts default), so a ~1900x1400
 * CSS viewport is a reasonable framing match. Exact size is not
 * load-bearing — only content visibility is asserted before capture.
 */
test.use({ viewport: { width: 1900, height: 1400 } });

test("wikiLanding — project gallery + incomplete indexes", async ({ page, demo }) => {
  // Every visible card fires its own freshness probe on mount (LandingScreen
  // deliberately has no per-card gate) — stub it so the badge state never
  // depends on real `git ls-remote` reachability/timing.
  await stubFreshness(page);

  await page.goto("/wiki");

  await expect(page.getByRole("heading", { name: "Agentic Wiki" })).toBeVisible();

  // The three richest seeded projects — confirms the gallery loaded real
  // data, not the empty state. RepoLink's "short" display = "owner/repo".
  await expect(page.getByText("bearlike/Grove", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("bearlike/Assistant", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("acme/beacon", { exact: true }).first()).toBeVisible();

  // The resumable-jobs band — collapsed by default (matches the app's
  // default state); only the header needs to be visible for this shot.
  await expect(page.getByRole("heading", { name: "Incomplete indexes" })).toBeVisible();
  await expect(page.getByText(/\d+ resumable/)).toBeVisible();

  await demo.capturePage("wikiLanding");
});
