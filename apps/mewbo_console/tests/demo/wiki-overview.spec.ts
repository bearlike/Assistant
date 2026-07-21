import { test, expect } from "./fixtures";
import { GROVE_LANDING_PAGE_ID, GROVE_SLUG, stubFreshness, wikiPageHref } from "./wiki-helpers";

/**
 * Shot wikiOverview (02) — Grove's landing page (`grove-overview`, titled
 * "Grove Overview"): sidebar page-tree, markdown body (incl. a mermaid
 * flowchart), and the right-rail "On this page" TOC.
 *
 * Existing placeholder raster ~1920x1386; see wiki-landing.spec.ts for the
 * viewport-sizing rationale (deviceScaleFactor stays at the config default).
 */
test.use({ viewport: { width: 1900, height: 1400 } });

test("wikiOverview — Grove overview page", async ({ page, demo }) => {
  // WikiTopBar renders a FreshnessBadge on every settled in-project screen —
  // stub the outbound ls-remote probe so it never depends on real network.
  await stubFreshness(page);

  await page.goto(wikiPageHref(GROVE_LANDING_PAGE_ID, GROVE_SLUG));

  // The page title <h1> is `#page-top`; the Grove overview body ALSO starts
  // with a `# Grove Overview` markdown heading, so match the title by id.
  await expect(page.locator("#page-top")).toHaveText("Grove Overview");

  // Left sidebar page tree + right-rail "On this page" TOC — both columns
  // only render once `lg`/`xl` breakpoints are satisfied by the viewport.
  await expect(page.locator("aside nav").first()).toBeVisible();
  // Match the TOC by its landmark role, not by text: the narrow-viewport
  // trigger button carries the same "On this page" label, so a text match
  // resolves to two elements and trips strict mode. The role query names
  // `WikiToc`'s own `<nav aria-label="On this page">` and nothing else.
  await expect(page.getByRole("navigation", { name: "On this page" })).toBeVisible();

  // The overview page body has a ```mermaid flowchart per the seed
  // contract — wait for it to finish rendering (not the "Rendering
  // diagram…" placeholder) so the shot never races the async mermaid load.
  await expect(page.getByText("Rendering diagram…")).toHaveCount(0);

  await demo.capturePage("wikiOverview");
});
