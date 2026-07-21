import {
  test as base,
  expect,
  type Locator,
  type Page,
} from "@playwright/test";
import { JPEG_QUALITY, SHOTS, shotPath, type ShotName } from "./shots";

/**
 * Frozen wall-clock for the whole capture run. The seeder writes every seeded
 * timestamp as an offset back from THIS instant, so relative times ("45 minutes
 * ago") render byte-identically forever — RelativeTime is the #1 pixel-diff
 * hazard for these shots. DEMO_T0 MUST stay in lockstep with the seeder's T0.
 */
const DEMO_T0 = process.env.DEMO_T0 ?? "2026-07-14T09:30:00Z";

/**
 * Determinism fixture. Extends the base test with:
 *  - a `page` that has the frozen clock (and optional API key) installed BEFORE
 *    the first navigation, and
 *  - a `demo` helper that owns the shared capture / navigation / locator logic.
 */
export const test = base.extend<{ demo: DemoHelper }>({
  page: async ({ page }, use) => {
    // The console container bakes VITE_API_KEY into runtime-config.js, so auth
    // normally needs nothing here. DEMO_API_KEY is an escape hatch for a stack
    // that does NOT bake one: seed window.__MEWBO_CONFIG__ before the bundle
    // reads it at module load (src/api/client.ts reads the key eagerly).
    const apiKey = process.env.DEMO_API_KEY;
    if (apiKey) {
      await page.addInitScript((key) => {
        const w = window as unknown as Record<string, unknown>;
        w.__MEWBO_CONFIG__ = {
          ...(w.__MEWBO_CONFIG__ as Record<string, unknown> | undefined),
          VITE_API_KEY: key,
        };
      }, apiKey);
    }
    // install (NOT setFixedTime): Date starts at T0 and the fake clock keeps
    // ticking, so data loading works exactly as in production (a FULLY paused
    // clock starves something in the load path — the session list sat on
    // "Loading sessions..." forever). The capture-time determinism comes from
    // settle(), which jumps the clock to a FIXED instant before shooting.
    await page.clock.install({ time: new Date(DEMO_T0) });
    await use(page);
  },
  demo: async ({ page }, use) => {
    await use(new DemoHelper(page));
  },
});

export { expect };

/**
 * One atomic helper owning the flows' shared steps: settle-before-capture, the
 * two capture modes, navigation into a seeded session, and the (aria-less) log
 * card locators. State (the page) + behaviour together; DI'd via the fixture so
 * each spec reads as a short script.
 */
export class DemoHelper {
  /** Fixed capture instant: T0 + 60s (see settle()). */
  private static readonly CAPTURE_AT = new Date(
    new Date(DEMO_T0).getTime() + 60_000,
  );

  private clockPaused = false;

  constructor(private readonly page: Page) {}

  /** Block until webfonts are laid out so glyph metrics don't shift the raster. */
  async settle(): Promise<void> {
    // Jump the fake clock to EXACTLY T0+60s and pause. Every pending timer
    // (relative-time re-renders, entrance staggers, anything scheduled during
    // load) fires up to that instant and then time stops — so the raster
    // reflects one frozen, run-independent moment no matter how long the page
    // actually took to load. 60s of headroom >> any real load time.
    if (!this.clockPaused) {
      await this.page.clock.pauseAt(DemoHelper.CAPTURE_AT);
      // The TurnScroller margin rail maps live scroll position — meaningless
      // in a still, and its measured dot geometry jitters by a few AA pixels
      // between runs (the ONLY nondeterministic pixels in an otherwise
      // byte-identical capture). Hide it rather than mask it: a mask paints a
      // colored box, this just yields the empty gutter.
      // Compositor paints run on WALL time — the faked clock governs none of
      // them, so each is a race against the capture instant. Kill every one:
      //  - scrollbar-thumb fade after a programmatic scroll
      //  - CSS transitions (expand-chevron rotation, hover/state tweens) —
      //    `transition:none` snaps to final values, it cannot strand a state
      //  - text caret blink
      // reducedMotion already forces keyframe end-states, so `animation:none`
      // on top of it is safe. No doc still loses information here.
      await this.page.addStyleTag({
        content: [
          ".session-ts-rail{display:none !important}",
          "*::-webkit-scrollbar{display:none !important}",
          "*{scrollbar-width:none !important}",
          "*,*::before,*::after{transition:none !important;animation:none !important}",
          "*{caret-color:transparent !important}",
        ].join("\n"),
      });
      this.clockPaused = true;
    }
    await this.page.evaluate(() => document.fonts.ready);
    // Notification toasts must never be in a shot. The seeder settles them
    // server-side (materialize + dismiss); if one shows anyway, fail loudly —
    // a dirty capture is worse than a red run (staleness-signal principle).
    await expect(this.page.locator("[data-sonner-toast]")).toHaveCount(0);
  }

  /**
   * Full-viewport capture — overwrites the docs image in place, honoring the
   * shot's DECLARED encoding in `shots.ts` (console 01/02 + the wiki
   * indexing-progress shot are PNG; every other full-page shot keeps the JPEG
   * the hand-authored original used). JPEG-at-90 of a viewport raster is
   * byte-deterministic given the pinned Playwright encoder, same as the element
   * closeups. (`captureElement` already branched on type; this now matches it.)
   */
  async capturePage(name: ShotName): Promise<void> {
    await this.settle();
    await this.page.screenshot(
      SHOTS[name].type === "jpeg"
        ? { path: shotPath(name), type: "jpeg", quality: JPEG_QUALITY }
        : { path: shotPath(name), type: "png" },
    );
  }

  /** Tight element JPEG (log closeups) — the single card, nothing masked. */
  async captureElement(name: ShotName, target: Locator): Promise<void> {
    await this.settle();
    // Pin the scroll alignment before shooting. locator.screenshot() only
    // scrolls when the element is NOT already visible, and whether the pane's
    // own scroll settled first is a race — two attractor scroll states means
    // two sub-pixel croppings and a flaky byte-diff. An explicit center
    // alignment is one deterministic geometry regardless of prior state.
    await target.evaluate((el) =>
      el.scrollIntoView({ block: "center", inline: "nearest" }),
    );
    // Shoot via an integer clip of the PAGE raster, not locator.screenshot():
    // the full-viewport captures are proven byte-stable run over run, so a
    // fixed-rect crop of that same raster inherits the stability — while the
    // element-screenshot path re-scrolls and re-rasterizes on its own terms.
    const box = await target.boundingBox();
    if (!box) throw new Error(`captureElement(${name}): target has no box`);
    await this.page.screenshot({
      path: shotPath(name),
      type: "jpeg",
      quality: JPEG_QUALITY,
      clip: {
        x: Math.floor(box.x),
        y: Math.floor(box.y),
        width: Math.ceil(box.width),
        height: Math.ceil(box.height),
      },
    });
  }

  /** Open the landing, wait for the seeded list, click a session by its title. */
  async openSession(title: string): Promise<void> {
    await this.page.goto("/");
    // Scope to <main>: a completion toast (rendered in the sonner region
    // OUTSIDE main) carries the same session title, and an unscoped
    // getByText(...).first() clicks the toast — which navigates nowhere.
    const row = this.page
      .locator("main")
      .getByText(title, { exact: false })
      .first();
    await expect(row).toBeVisible();
    await row.click();
    await expect(this.page).toHaveURL(/\/s\//);
  }

  /**
   * The workspace pane body (WorkspacePanel renders LogsView on --surface-deep).
   * Scopes card lookups to the right-hand instrument panel.
   */
  logsPane(): Locator {
    return this.page.locator('div[class*="surface-deep"]');
  }

  /** Make the Logs tab active if the workspace auto-opened on Diff instead. */
  async ensureLogsTab(): Promise<void> {
    const logsTab = this.page.getByRole("button", { name: /^Logs/ });
    await expect(logsTab).toBeVisible();
    if ((await logsTab.getAttribute("aria-current")) !== "page") {
      await logsTab.click();
    }
  }

  /**
   * A tool-log card in the Logs pane, found by the text it renders plus its
   * card-shape Tailwind fragment. These cards carry no role/aria/testid (the
   * console has 4 data-testids total, none here), so text + shape is the
   * sturdiest anchor available — and a copy change in the seeded data is meant
   * to break it (staleness signal). Shape fragments:
   *   - TerminalCard (shell)     -> "border-l-[3px]"
   *   - FileReadCard (file read) -> "border-l-agent-1"  (LogEventCard accent)
   */
  card(shapeClassFragment: string, hasText: string): Locator {
    return this.logsPane()
      .locator(`div[class*="${shapeClassFragment}"]`)
      .filter({ hasText })
      .first();
  }

  /** Expand a click-to-toggle card unless its reveal target already shows. */
  async expand(card: Locator, reveal: Locator): Promise<void> {
    if (!(await reveal.isVisible())) {
      await card.click();
    }
    await expect(reveal).toBeVisible();
  }

  /**
   * Fully expand a DiffCard. Unlike TerminalCard/FileReadCard (body hidden until
   * expanded), a DiffCard always renders its diff but TRUNCATES past 8 lines with
   * a "Click to expand" footer; a short diff shows whole with no affordance. So:
   * click only when that footer is present, then wait for it to go away. Both
   * cases end with the full diff shown (matching the reference's "Collapse" state).
   */
  async expandDiff(card: Locator): Promise<void> {
    const moreHint = card.getByText(/Click to expand/);
    if (await moreHint.isVisible()) {
      await card.click();
      await expect(moreHint).toBeHidden();
    }
  }
}
