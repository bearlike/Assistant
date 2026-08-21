import { test, expect } from "./fixtures";
import { SEED } from "./shots";

/**
 * Shot settingsPlugins (mewbo-console-05-plugins.png) — Settings › Plugins.
 *
 * This replaces a hand-capture that documented UI a user cannot reach: a
 * deleted top NavBar and the retired standalone `/plugins` page. Plugins is a
 * Settings facet now (`/plugins` redirects to `?facet=plugins`), so the shot
 * has to show the NavRail as the only primary navigation.
 *
 * Nothing here is fetched off the network. Both `GET /api/plugins` and
 * `/api/plugins/marketplace` are plain file reads against the fixture install
 * cache mounted at `/app/data/plugins`, and `plugins.marketplaces` stays empty
 * in the demo config, which is what suppresses the clone-on-read a configured
 * marketplace would trigger. So there is nothing to `page.route`-stub.
 *
 * ## Framing
 * The facet is ~1830px of content, so no landscape viewport holds all of it and
 * the shot has to pick a band. It picks the two panes — `External plugins` and
 * `Marketplace` — because that pair is what the docs pages promise
 * (`features-plugins.md`, and the index carousel's "Plugins and a marketplace
 * to extend any session"), and a Marketplace with no Install button in frame
 * would not show it. `pinToPaneTop` scrolls the facet heading out; the NavRail's
 * active `Plugins` row and the two card titles carry the identity instead.
 *
 * Pinning also makes the framing independent of everything ABOVE the pane,
 * which is worth more than it sounds: the sibling Projects shot tunes a raw
 * viewport height against absolute positions, so when the demo api stopped
 * serving a read-only config mount and the "Settings are read-only" alert
 * disappeared, every element moved up by the alert's height and that shot's
 * bottom edge had to be re-measured. This one did not move at all — only its
 * scroll offset did.
 *
 * 1400x1249 is chosen so the bottom edge lands in the GAP between marketplace
 * rows 2 and 3 rather than through one. It is taller than the sibling Settings
 * shots (1400x1000), which costs some window width on the 16:9 canvas and buys
 * the whole External list plus two Install buttons. Even so the composited
 * window comes out wider than the hand-capture it replaces, which was nearly
 * square.
 */
test.use({ viewport: { width: 1400, height: 1249 } });

/**
 * Breathing room left above the pinned card. At zero the card's top border sits
 * flush against the pane edge and reads as a clipped card.
 */
const PANE_TOP_MARGIN = 16;

test("settingsPlugins — installed plugins + marketplace", async ({ page, demo }) => {
  await page.goto("/settings?facet=plugins");

  // ⚠️ TWO h2s are named exactly "Plugins" — the facet heading and the
  // schema-driven config section below the panes. Anchor on the pane titles,
  // which are unique, or strict mode resolves to 2 elements.
  //
  // `SettingsCard` renders a `<section aria-labelledby>`, so each pane is a
  // `region` named by its own title. That resolves the card ROOT rather than
  // the heading inside it, which is what the framing below has to scroll.
  const installed = page.getByRole("region", { name: "External plugins" });
  await expect(installed).toBeVisible();
  await expect(page.getByRole("region", { name: "Marketplace" })).toBeVisible();

  // Cardinality first: membership alone cannot see contamination, because an
  // extra row satisfies every toBeVisible. Each list's per-row button carries
  // the plugin name in its accessible name, which makes the count an assertion
  // over an affordance that already exists rather than a testid added for the
  // capture. `Uninstall <name>` cannot match `/^Install /`, so the two counts
  // stay independent.
  const uninstallButtons = page.getByRole("button", { name: /^Uninstall / });
  const installButtons = page.getByRole("button", { name: /^Install / });
  await expect(uninstallButtons).toHaveCount(SEED.plugins.installed.length);
  await expect(installButtons).toHaveCount(SEED.plugins.marketplace.length);

  // Then identity: the counts above hold for any six rows. Naming every one
  // proves the fixture the api read is the fixture this repo ships, and a
  // rename in `demo/plugins/` is meant to break this (staleness signal).
  for (const name of SEED.plugins.installed) {
    await expect(page.getByRole("button", { name: `Uninstall ${name}` })).toBeVisible();
  }
  for (const name of SEED.plugins.marketplace) {
    await expect(page.getByRole("button", { name: `Install ${name}` })).toBeVisible();
  }

  // A row's METADATA is the reason this pane exists — "installing a plugin is
  // not cosmetic". Pin one of each kind so a payload that renders bare names
  // (version, capability counts and the hooks flag all dropped) fails here
  // instead of shipping a screenshot that undersells the surface.
  //
  // ⚠️ Anchor only on the counts that are real ones. `agents` and `commands`
  // are `len()` over a `*.md` glob, so they count files. `skills` is NOT: the
  // api derives it from `skill_dirs`, which takes a single `append` of the
  // plugin's `skills/` PARENT directory, so it is 0-or-1 whatever the plugin
  // ships. "1 skill" on a plugin carrying a whole skills library is that flag
  // rendering as a quantity — a true string to assert and a false thing to
  // treat as evidence, which is why it is deliberately absent here.
  await expect(page.getByText("v6.2.0", { exact: true })).toBeVisible();
  await expect(page.getByText("hooks", { exact: true })).toBeVisible();
  // Unique, and the one badge in the shot proving a count can exceed one.
  await expect(page.getByText("3 agents", { exact: true })).toHaveCount(1);
  // Three plugins ship exactly one command each; a count, not a `.first()`.
  await expect(page.getByText("1 command", { exact: true })).toHaveCount(3);

  // Frame the two panes. See the Framing note above for why the facet heading
  // is deliberately outside the capture.
  await demo.pinToPaneTop(installed, PANE_TOP_MARGIN);

  // The pointer never touched a row, so no card can be stuck in :hover — but
  // the same guard the widgets flow needs costs nothing and states the intent.
  await page.mouse.move(0, 0);
  await demo.capturePage("settingsPlugins");
});
