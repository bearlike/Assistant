import { test, expect } from "./fixtures";
import { WIZARD_REPO_URL, stubBranches, wikiConfigureHref } from "./wiki-helpers";

/**
 * Shots wikiIndexSource / wikiIndexGeneration / wikiIndexScope (04/05/06) —
 * the three-step ConfigureWizard, prefilled with Grove's URL. No seed data
 * is involved (this is a NEW-wiki flow); the only non-determinism is the
 * wizard's own outbound `ls-remote` branch probe, stubbed below. All three
 * shots share ONE wizard session (state lives in component memory, so a
 * fresh navigation per shot would lose step 2/3 progress) — capture in
 * sequence, never click "Generate Wiki".
 *
 * Existing placeholder raster ~1920x1386 for all three; see
 * wiki-landing.spec.ts for the viewport-sizing rationale.
 */
test.use({ viewport: { width: 1900, height: 1400 } });

test("wikiIndexWizard — source, generation, scope steps", async ({ page, demo }) => {
  // The Source step mounts with `state.url` already prefilled from the query
  // param, so the branch picker's POST fires immediately on mount — stub it
  // BEFORE navigating so no shot depends on a real LAN `ls-remote`.
  await stubBranches(page);

  await page.goto(wikiConfigureHref(WIZARD_REPO_URL));

  // ── Step 1: Source ──────────────────────────────────────────────────
  await expect(
    page.getByRole("heading", { name: "What are you indexing?" }),
  ).toBeVisible();

  const urlInput = page.getByPlaceholder("https://github.com/owner/repo");
  await expect(urlInput).toHaveValue(WIZARD_REPO_URL);

  // Explicit click per the brief: the tile click also sets `platformLocked`,
  // which short-circuits the URL auto-detect effect — so the selection state
  // is deterministic regardless of that effect's timing.
  // Anchor on `^Gitea`: the loose `/Gitea/` also matches the "Git
  // repository" source-type tile ("…GitHub, GitLab, Gitea, or any hosted
  // repo") and the "How do I create a Gitea token?" help link; only the
  // platform tile's accessible name STARTS with "Gitea".
  const giteaTile = page.getByRole("button", { name: /^Gitea/ });
  await giteaTile.click();
  await expect(giteaTile).toHaveAttribute("aria-pressed", "true");

  await demo.capturePage("wikiIndexSource");

  // ── Step 2: Generation ──────────────────────────────────────────────
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(
    page.getByRole("heading", { name: "How should the wiki read?" }),
  ).toBeVisible();
  // Deterministic branch-picker fallback from the stub above (empty
  // branches + null defaultBranch → the "" / "Default branch" option, no
  // toast, no retry). Assert the <select>'s VALUE, not an <option>'s text
  // visibility — a closed native <select>'s <option> children aren't
  // independently laid out, so `getByText(...).toBeVisible()` on one is
  // unreliable across engines. Branch is the second <select> on this step
  // (language, then branch — the Model field is a Command popover, not a
  // native select).
  const branchSelect = page.locator("select").nth(1);
  await expect(branchSelect).toHaveValue("");

  await demo.capturePage("wikiIndexGeneration");

  // ── Step 3: Scope ───────────────────────────────────────────────────
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { name: "Anything to skip?" })).toBeVisible();

  await demo.capturePage("wikiIndexScope");
});
