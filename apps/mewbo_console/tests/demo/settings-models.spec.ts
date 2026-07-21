import { test, expect } from "./fixtures";
import { SEED } from "./shots";

/**
 * Shot settingsModels (mewbo-settings-01-models.jpg) — Settings › Models &
 * Inference.
 *
 * This shot replaces a legacy hand-capture taken against a live internal
 * instance, which printed that deployment's real LLM proxy host. Everything
 * visible here now comes from `demo/configs/app.json` (mounted into the api),
 * so the rendered "Api Base" is the fictional `llm.example.com` and the API-key
 * field renders only its is-set state — the backend strips secret VALUES, so a
 * secret can never reach this page even when configured.
 *
 * The facet is schema-driven RJSF: no seeded Mongo state is involved, and
 * nothing on this pane fetches a model list, so it cannot call out.
 */
test.use({ viewport: { width: 1400, height: 1000 } });

test("settingsModels — models & inference facet", async ({ page, demo }) => {
  await page.goto("/settings?facet=models");

  // The facet rail resolves `?facet=` before the schema arrives, so anchor on a
  // rendered FIELD rather than the heading — the heading is present while the
  // section list is still empty, which would capture a half-built page.
  const apiBase = page.getByLabel("Api Base");
  await expect(apiBase).toHaveValue(SEED.llmApiBase);

  // The provider key must render as configured-and-masked. Asserting the
  // "Replace" affordance (SecretField's 3rd state) is the load-bearing check:
  // it proves the secret is set WITHOUT a value ever being rendered, which is
  // exactly the property that made the legacy capture safe to replace.
  await expect(page.getByRole("button", { name: "Replace" }).first()).toBeVisible();

  await demo.capturePage("settingsModels");
});
