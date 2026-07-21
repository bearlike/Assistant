import { test, expect } from "./fixtures";
import { SEED } from "./shots";

/**
 * Shot file-read-log — an element (locator) screenshot of the single expanded
 * file-read tool card (FileReadCard) inside the auth-refactor session: the
 * src/middleware/auth.ts read with its line-numbered, syntax-highlighted body
 * visible. Docs alt: "a read_file tool card ... showing lines of a file".
 */
test.use({ viewport: { width: 1650, height: 1191 } });

test("file-read-log — expanded file-read tool card", async ({ demo }) => {
  await demo.openSession(SEED.session.authRefactorTitle);
  await demo.ensureLogsTab();

  const card = demo.card("border-l-agent-1", SEED.readFile);
  await expect(card).toBeVisible();

  // The reference closeup is the EXPANDED card (code viewer shown). The viewer
  // rows render as <code class="hljs"> only once expanded; expand until shown.
  await demo.expand(card, card.locator("code.hljs").first());

  await demo.captureElement("fileReadLog", card);
});
