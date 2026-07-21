import { test, expect } from "./fixtures";
import { SEED } from "./shots";

/**
 * Shot 04-file-edit — an element (locator) screenshot of the single expanded
 * file-edit tool card (DiffCard) inside the auth-refactor session: the edit to
 * src/middleware/auth.ts with its unified diff shown. Docs alt: "a file edit
 * shown as a unified diff". The reference raster is an EXPANDED card (it ends in
 * a "Collapse" footer), so expand a large diff before capturing.
 *
 * SEED.readFile ('auth.ts') is the same basename the read card uses; the DiffCard
 * renders it in the file header, and its success accent (border-l-agent-4)
 * distinguishes it from the read card (agent-1) and the shell card (border-l-[3px]).
 */
test.use({ viewport: { width: 1650, height: 1191 } });

test("file-edit — expanded diff card", async ({ demo }) => {
  await demo.openSession(SEED.session.authRefactorTitle);
  await demo.ensureLogsTab();

  const card = demo.card("border-l-agent-4", SEED.readFile);
  await expect(card).toBeVisible();

  await demo.expandDiff(card);

  await demo.captureElement("fileEdit", card);
});
