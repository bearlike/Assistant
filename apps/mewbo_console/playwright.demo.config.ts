import { defineConfig } from "@playwright/test";

/**
 * Deterministic demo-capture harness.
 *
 * This is NOT the e2e config. `playwright.config.ts` boots a Vite dev server and
 * drives the app against page.route mocks; THIS config has no `webServer` at
 * all — it targets the console *container* from the demo compose stack, which is
 * pointed at a freshly seeded API. The specs navigate real routes and shoot real
 * rendered data.
 *
 * Every knob below exists to make the captured PNG/JPEG byte-identical run over
 * run, so `git diff --exit-code -- docs/assets/img/` is the pass/fail gate for
 * the capture step. See `tests/demo/fixtures.ts` for the frozen clock + settle
 * logic and `tests/demo/shots.ts` for where images land.
 */
const BASE_URL = process.env.DEMO_CONSOLE_URL ?? "http://127.0.0.1:3210";

export default defineConfig({
  testDir: "./tests/demo",
  // Serial + single worker + no retries: captures mutate shared files on disk
  // (the docs images) and share one seeded backend; parallelism would race them.
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  // Gitignored (apps/mewbo_console/.gitignore ships `test-results`). Only the
  // docs/assets/img/ outputs written by the specs are meant to persist.
  outputDir: "./test-results/demo",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: BASE_URL,
    // deviceScaleFactor 2: output pixels = logical viewport x 2. Each spec sets
    // its logical viewport (test.use) so 01/02 land exactly on the reference
    // rasters — front 1347x1191 -> 2694x2382, tasks 1650x1191 -> 3300x2382.
    deviceScaleFactor: 2,
    viewport: { width: 1650, height: 1191 },
    // All reference shots are the console's default (dark) theme.
    colorScheme: "dark",
    // Kill the two remaining non-determinism sources beyond the clock: locale
    // formatting and timezone-shifted absolute timestamps.
    timezoneId: "UTC",
    locale: "en-US",
    // Freeze CSS animations/transitions; the console honours it (index.css
    // "kills every keyframe" and forces .fade-in-row rows opaque immediately).
    // reducedMotion is a context option (not a top-level use key) in this
    // Playwright version, so it goes through contextOptions.
    contextOptions: { reducedMotion: "reduce" },
    // The specs own capture; suppress Playwright's own artifact noise.
    trace: "off",
    video: "off",
    screenshot: "off",
  },
});
