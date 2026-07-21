import { test, expect } from "./fixtures";
import { SEED } from "./shots";

/**
 * Shot shell-log — an element (locator) screenshot of the single expanded shell
 * tool card (TerminalCard) inside the auth-refactor session: the `npm test`
 * command with its output visible. Docs alt: "a shell tool card ... showing a
 * command with its response and a duration".
 */
test.use({ viewport: { width: 1650, height: 1191 } });

test("shell-log — expanded shell tool card", async ({ demo }) => {
  await demo.openSession(SEED.session.authRefactorTitle);
  await demo.ensureLogsTab();

  const card = demo.card("border-l-[3px]", SEED.shellCommand);
  await expect(card).toBeVisible();

  // The reference closeup is the EXPANDED card (stdout shown). TerminalCard
  // renders output in a <pre> only once expanded; expand until that shows.
  await demo.expand(card, card.locator("pre").first());

  await demo.captureElement("shellLog", card);
});
