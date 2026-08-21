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
  // progress; "Finish" is the user-facing label for the reached final phase.
  await expect(page.getByText("Finish", { exact: true }).first()).toBeVisible();

  // Pin the exact percent (finalize's floor, per progress.ts' PHASE_RANGE
  // [95,100] — this job carries no finalize sub-progress) rather than
  // leaving it unasserted: a snapshot bound at the wrong phase (e.g. still
  // mid-"pages") would otherwise satisfy every OTHER assertion here.
  await expect(page.getByText("95%", { exact: true })).toBeVisible();

  // The log timeline replays the job's full stored event history (7 lines,
  // `DEMO_JOB`'s bundle entry) — asserting only the mid-replay "Built graph"
  // line (as this spec used to) passes on a TRUNCATED replay that stopped
  // right there. Assert the LAST line too, so a replay that stalls before
  // reaching the end of the log still fails.
  await expect(page.getByText("Built graph: 715 nodes, 3918 edges")).toBeVisible();
  await expect(page.getByText("Embedded 715 nodes (dim=3072)")).toBeVisible();
  await expect(
    page.getByRole("log", { name: "Indexer activity" }).getByRole("listitem"),
  ).toHaveCount(7);

  await demo.capturePage("wikiIndexingProgress");
});
