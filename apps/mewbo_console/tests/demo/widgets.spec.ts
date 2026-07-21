import { test, expect } from "./fixtures";
import { SEED } from "./shots";

/**
 * Shot 07 — a session with an inline rendered widget: the trending
 * agent-harness-repositories card grid, booted from a seeded `widget_ready`
 * event (files inlined in the bundle — no LLM, no network, no live stlite
 * fetch). The widget is built from NATIVE Streamlit components
 * (st.container(border=True) cards in st.columns, native st.markdown / st.caption
 * / st.progress) rather than a hand-rolled `unsafe_allow_html` blob — that is
 * what makes the capture read like a genuine Streamlit embed (authentic
 * typography, spacing, and colors) instead of a cramped custom card. A
 * whole-card `<a>` wrapper was the old bug: Streamlit paints every bit of link
 * text in the theme primaryColor, so custom cards came out "mostly orange".
 *
 * The seed↔flow contract is the session title (`SEED.session.widgetTitle` ↔
 * `console-poc.json`) plus the widget's own `app.py`, which renders one native
 * `st.progress` bar per repository and emits exactly 6 of them for the seeded
 * `data.json` — that count is both the deterministic content anchor to wait on
 * AND the staleness signal (a repository added or removed in the bundle, or the
 * progress bar dropped, breaks this test by design).
 *
 * The grid is 6 (2 rows x 3 cols) of UNIFORM `height=200` bordered containers
 * ON PURPOSE: the widget renders at natural scale (no zoom — fractional zoom
 * made every glyph land on fractional pixels and janked the text), and the
 * console caps a widget card at min(600px, 70vh) with internal scroll beyond
 * it. Fixed-height cards keep the rows even despite per-description wrap
 * differences, and the whole app (~548px + chrome) fits the cap with slack,
 * so nothing scrolls or clips and the grid paints as one deterministic block.
 * See demo/CLAUDE.md.
 *
 * Viewport is 1280x960 (4:3), matching this shot's aspect ratio — distinct from
 * the console closeups above, which follow the reference raster's own ratio.
 */
test.use({ viewport: { width: 1280, height: 960 } });

test("widgets — inline stlite widget with repo cards", async ({
  page,
  demo,
}) => {
  await demo.openSession(SEED.session.widgetTitle);

  // Pyodide boot is slow offline (fetching + instantiating the wasm runtime
  // before the widget's own render even starts) — the generous timeout is
  // required, do NOT lower it. One native st.progress bar per card; all 6 means
  // the widget is fully rendered. `stProgress` is a Streamlit testid, unique to
  // the widget (the console itself renders no Streamlit), so it is unambiguous —
  // unlike the widget title text, which also matches the session title + the
  // sidebar row (the strict-mode multi-match trap in demo/CLAUDE.md).
  const bars = page.locator('[data-testid="stProgress"]');
  await expect(bars).toHaveCount(6, { timeout: 60_000 });

  // Park the pointer in the corner so no card is left in a :hover state
  // (openSession leaves the pointer wherever it clicked the row).
  await page.mouse.move(0, 0);

  // The count only means the 6 nodes EXIST. stlite streams its render, and
  // WidgetCard re-measures content height asynchronously (ResizeObserver →
  // rAF → state), so two things can still move after the last node appears:
  // the grid's own layout AND the card's measured height. Content is
  // top-anchored, so the bars can be rock-steady while the card BOTTOM is
  // still settling. Require BOTH stable across consecutive polls.
  //
  // ⚠️ This wait MUST run BEFORE settle(): settle pauses the fake clock, and
  // Playwright's paused clock also freezes requestAnimationFrame — which the
  // WidgetCard portal-mirror (ResizeObserver → rAF → state) depends on. Any
  // card growth landing after the pause can never reach the portalled
  // wrapper, freezing a TRUNCATED card into the shot (observed: the second
  // card row cut off mid-render). Geometry must quiesce while the clock is
  // still alive.
  await page.waitForFunction(
    () => {
      const bars = document.querySelectorAll('[data-testid="stProgress"]');
      if (bars.length !== 6) return false;
      const app = document.querySelector(".stApp");
      const card = app?.closest(".fixed");
      if (!card) return false;
      const key =
        bars[5].getBoundingClientRect().bottom.toFixed(2) +
        "|" +
        card.getBoundingClientRect().bottom.toFixed(2);
      const w = window as unknown as { __rc?: string; __rn?: number };
      w.__rn = key === w.__rc ? (w.__rn ?? 0) + 1 : 0;
      w.__rc = key;
      return (w.__rn ?? 0) >= 8;
    },
    { timeout: 30_000, polling: 250 },
  );

  // Settle AFTER the widget geometry is final: fonts finished long before the
  // bars painted, and the capture stylesheet + frozen clock freeze the frame
  // exactly as verified above.
  await demo.settle();
  await demo.capturePage("widgets");
});
