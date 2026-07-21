import { test, expect } from "./fixtures";
import {
  ASSISTANT_OVERVIEW_PAGE_ID,
  ASSISTANT_SLUG,
  QA_ANSWER_ID,
  wikiQaSnapshotHref,
} from "./wiki-helpers";

/**
 * Shot wikiQna (08) — the stored Q&A snapshot for
 * `qa-assistant-what-is-this-for-0001` (Assistant, from `overview`,
 * "What is this project for?"). The `?answer=` deep link renders the
 * PERSISTED answer (`GET /v1/wiki/qa/<id>`) and never opens a live stream —
 * see `wiki/CLAUDE.md` "Idempotent Q&A URL" — so this shot has zero LLM
 * involvement and no stream-timing nondeterminism to fight.
 *
 * Existing placeholder raster ~1270x1261 (near-square, tall). Viewport
 * picked to roughly match; the QA screen's two-column grid needs `lg`
 * (1024px+) to render side-by-side.
 */
test.use({ viewport: { width: 1280, height: 1200 } });

test("wikiQna — stored Q&A snapshot", async ({ page, demo }) => {
  await page.goto(
    wikiQaSnapshotHref({
      answer: QA_ANSWER_ID,
      pageId: ASSISTANT_OVERVIEW_PAGE_ID,
      slug: ASSISTANT_SLUG,
    }),
  );

  await expect(
    page.getByRole("heading", { level: 1, name: "What is this project for?" }),
  ).toBeVisible();

  // Answer prose — the seeded snapshot's H2 section heading.
  await expect(page.getByRole("heading", { name: "Core Capabilities" })).toBeVisible();
  // The seeded snapshot also includes a "Repository Structure" table.
  await expect(page.getByText("Repository Structure")).toBeVisible();

  // Left-column cited-sources rail (visually uppercased via CSS; the DOM
  // text itself is mixed-case "Cited sources").
  await expect(page.getByText(/cited sources/i)).toBeVisible();

  await demo.capturePage("wikiQna");
});
