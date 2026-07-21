import { test, expect } from "./fixtures";
import { SEED } from "./shots";

/**
 * Shot 02 — "inside a task, step by step" (docs caption): the session-detail
 * view for the auth-refactor session, workspace open on the Logs tab with its
 * tool cards. NOTE: the file is named "...-02-tasks" but the reference image and
 * the docs alt text ("a running task, broken into steps with tool calls and
 * results") are a SESSION DETAIL, not a sessions list — the landing/list is
 * shot 01. This spec matches the actual image.
 *
 * Reference raster is 3300x2382 -> viewport 1650x1191 at deviceScaleFactor 2.
 */
test.use({ viewport: { width: 1650, height: 1191 } });

test("tasks — session detail with logs", async ({ demo }) => {
  await demo.openSession(SEED.session.authRefactorTitle);

  // The workspace auto-opens on the Logs tab on desktop; make it explicit so the
  // shot is stable even if that heuristic changes.
  await demo.ensureLogsTab();

  // Both hero tool steps must be present before the capture.
  await expect(demo.card("border-l-[3px]", SEED.shellCommand)).toBeVisible();
  await expect(demo.card("border-l-agent-1", SEED.readFile)).toBeVisible();

  await demo.capturePage("tasks");
});
