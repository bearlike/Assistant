import { test, expect } from "./fixtures";
import { SEED } from "./shots";

/**
 * Shot 01 — the console home / landing: the flower brand-mark hero, the
 * "Describe a task..." composer, and the recent-sessions list below it.
 * Docs alt text: "landing page with the composer and a list of recent sessions".
 *
 * Reference raster is 2694x2382 -> viewport 1347x1191 at deviceScaleFactor 2.
 */
test.use({ viewport: { width: 1347, height: 1191 } });

test("front — console home landing", async ({ page, demo }) => {
  await page.goto("/");

  // The composer is the focal element and renders with no data at all.
  await expect(page.getByLabel("Task description")).toBeVisible();

  // Wait for the seeded list so the shot always has session rows. Under
  // reducedMotion the .fade-in-row rows are forced opaque, so a visible row is a
  // settled row (no half-faded capture).
  await expect(
    page.getByText(SEED.session.authRefactorTitle).first(),
  ).toBeVisible();

  await demo.capturePage("front");
});
