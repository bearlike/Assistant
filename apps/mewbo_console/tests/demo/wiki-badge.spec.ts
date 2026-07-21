import { fileURLToPath } from "node:url";

import { test, expect } from "./fixtures";
import { GROVE_LANDING_PAGE_ID, GROVE_SLUG, stubFreshness, wikiPageHref } from "./wiki-helpers";

/**
 * Shot wikiBadge (07) — the "Copy badge" popover opened from Grove's
 * overview page (`WikiTopBar`'s README-badge affordance, `badge.ts:WikiBadge`).
 *
 * Existing placeholder raster ~1076x1042 (near-square) — the popover is
 * anchored top-right under the topbar, so a smaller viewport keeps it from
 * being swallowed by a mostly-empty page, roughly matching that framing.
 */
test.use({ viewport: { width: 1300, height: 1000 } });

test("wikiBadge — copy-badge popover", async ({ page, demo }) => {
  await stubFreshness(page);
  // The popover previews static badge artwork from an external CDN
  // (`badge.ts` → cdn.thekrishna.in). On the isolated demo network its load
  // races the capture (sometimes unpainted) → intermittent byte-diff. Route it
  // to a committed copy of the real SVG so the preview is always the same pixels.
  const badgeSvg = fileURLToPath(
    new URL("./fixtures/badge-ask-mewbo-wiki.svg", import.meta.url),
  );
  await page.route("**/Badge-Ask-Mewbo-Wiki.svg", (route) =>
    route.fulfill({ path: badgeSvg, contentType: "image/svg+xml" }),
  );

  await page.goto(wikiPageHref(GROVE_LANDING_PAGE_ID, GROVE_SLUG));
  // Match the page title <h1> by id — the body also starts with a
  // `# Grove Overview` markdown heading, so a name match collides.
  await expect(page.locator("#page-top")).toHaveText("Grove Overview");

  // aria-label wins over the (responsive, `hidden sm:inline`) visible text
  // for the accessible name, so this is stable regardless of viewport width.
  await page.getByRole("button", { name: "Copy README badge" }).click();

  await expect(page.getByText("Add the wiki badge")).toBeVisible();
  await expect(page.getByText("Markdown", { exact: true })).toBeVisible();

  await demo.capturePage("wikiBadge");
});
