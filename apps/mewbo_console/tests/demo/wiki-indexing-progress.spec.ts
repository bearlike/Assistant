import { test, expect } from "./fixtures";
import { DEMO_JOB, wikiIndexingHref } from "./wiki-helpers";

/**
 * Shot wikiIndexingProgress (09) — the live progress screen for the seeded
 * in-flight job (`job-demo-project-64-0001`, status `finalizing`, phase
 * `finalize`, 18/18 pages, model `claude-sonnet-4-6`). No `platform` query
 * param is passed — `IndexingScreen` falls back to the job snapshot's own
 * `platform` field, which is more accurate than guessing here.
 *
 * IndexingScreen makes no outbound/LAN calls (only `/v1/wiki/index/<id>` +
 * its SSE stream, both internal), so no `page.route` stub is needed for
 * this shot.
 *
 * Registered PNG in shots.ts (existing placeholder ~1141x709, wide/short —
 * matches the centered, `max-w-[720px]` progress card).
 */
test.use({ viewport: { width: 1100, height: 800 } });

test("wikiIndexingProgress — finalizing job", async ({ page, demo }) => {
  await page.goto(wikiIndexingHref(DEMO_JOB.jobId, DEMO_JOB.slug));

  // Header label for phase "finalize" (IndexingProgress.PHASE_LABEL) —
  // distinct from the amber "Indexing stopped" recovery panel, which only
  // shows for a terminal failed/interrupted/cancelled status.
  await expect(page.getByText("Finalizing", { exact: true })).toBeVisible();
  // The slug renders twice — a RepoLink <a> and a font-mono <span>; either
  // presence proves the screen bound the right job.
  await expect(page.getByText(DEMO_JOB.slug).first()).toBeVisible();

  // Phase rail — all seven phases render as dots + labels regardless of
  // progress; "finalize" is the last, reached one.
  await expect(page.getByText("finalize", { exact: true })).toBeVisible();

  // Seeded event-log line, replayed from the job's stored SSE history —
  // anchors on real seeded content rather than a guessed log format.
  await expect(page.getByText("Built graph: 715 nodes, 3918 edges")).toBeVisible();

  await demo.capturePage("wikiIndexingProgress");
});
