import { test, expect } from "./fixtures";
import { SEED } from "./shots";

/**
 * Shot settingsSecurity (mewbo-settings-02-security.jpg) — Settings › Security
 * & Access: the configured-secrets roll-up plus the issued API keys.
 *
 * This replaces a legacy hand-capture that printed a live deployment's key ids
 * and labels (internal host naming + CI topology). Both rows here are seeded
 * (`console-poc.json` -> `api_keys`) with invented ids and labels, and their
 * stored hashes are salted so a seeded key can never authenticate anything.
 *
 * Determinism note: the issued-keys list is only byte-stable because the seeder
 * writes FIXED ids and T0-relative `created_at` values — `create_key` would
 * mint a fresh uuid and wall-clock stamp per seed. See
 * `DemoSeeder._seed_api_key` for why that is a written record, not a contract
 * call.
 */
test.use({ viewport: { width: 1400, height: 1000 } });

test("settingsSecurity — secrets roll-up + issued keys", async ({ page, demo }) => {
  await page.goto("/settings?facet=security");

  // SecretsSummary and ApiKeysView are separate lazy panes that resolve
  // independently; wait for BOTH or the capture can freeze one mid-Suspense.
  await expect(page.getByText("Configured secrets")).toBeVisible();

  // A seeded key row proves the store round-trip (seed -> GET /api/keys ->
  // pane), not merely that the card shell mounted.
  for (const label of SEED.apiKeyLabels) {
    await expect(page.getByText(label, { exact: false })).toBeVisible();
  }

  // The secrets roll-up must show real is-set state, not an all-"not set"
  // skeleton — `llm.api_key` is configured in demo/configs/app.json.
  await expect(page.getByText("llm.api_key")).toBeVisible();

  await demo.capturePage("settingsSecurity");
});
