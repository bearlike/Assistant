/**
 * Cross-language parity gate for the transcript turn assembler.
 *
 * `buildTimeline` (this file's target) and the canonical Python assembler
 * (`packages/mewbo_core/src/mewbo_core/transcript_timeline.py`) both
 * reconstruct conversation turns from the same event log. Until now each
 * side was only ever checked against its own unit tests, so a rule added to
 * one and missed by the other shipped silently.
 *
 * `tests/fixtures/transcript_timeline_corpus.json` is the shared contract:
 * event logs plus the rows BOTH implementations must produce, written from
 * the documented rules rather than dumped from either implementation. This
 * suite replays it through `buildTimeline` and projects the output down to
 * the corpus row shape. Loaded off disk from the repo root — same precedent
 * as `SettingsView.integration.test.tsx` reading the real
 * `configs/app.schema.json` — so a COPIED corpus can't drift from the one
 * the Python side is gated against.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, test } from "vitest";

import { EventRecord, TimelineEntry } from "../types";
import { buildTimeline } from "../utils/timeline";

const CORPUS_PATH = resolve(
  process.cwd(),
  "../../tests/fixtures/transcript_timeline_corpus.json",
);

type CorpusRow = {
  id: string;
  role: string;
  turnId: string;
  content: string;
  ts: string | null;
};

type CorpusCase = {
  name: string;
  why: string;
  events: EventRecord[];
  expected: CorpusRow[];
};

const corpus = JSON.parse(readFileSync(CORPUS_PATH, "utf-8")) as {
  cases: CorpusCase[];
};

/** Project a `TimelineEntry` down to the corpus row shape. `ts` is `null` in
 * the corpus wherever the assembler deliberately carries none (a `plan` row,
 * an assistant closure) — `undefined` on the TS side means the same thing. */
function projectRow(entry: TimelineEntry): CorpusRow {
  return {
    id: entry.id,
    role: entry.role,
    turnId: entry.turnId,
    content: entry.content,
    ts: entry.ts ?? null,
  };
}

describe("buildTimeline — cross-language parity corpus", () => {
  test("the corpus loaded and is non-empty", () => {
    expect(corpus.cases.length).toBeGreaterThan(0);
  });

  for (const c of corpus.cases) {
    test(`${c.name} — ${c.why}`, () => {
      const projected = buildTimeline(c.events).map(projectRow);
      expect(projected).toEqual(c.expected);
    });
  }

  // Every case in the corpus applies directly to `buildTimeline` with no
  // adaptation — none skipped. If a future case is TS-inapplicable, assert
  // that explicitly here (with a `why`) rather than omitting it from the
  // loop above, per the contract this file exists to enforce.
});
