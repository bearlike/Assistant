/**
 * `IndexingPlan` / `ActivityFeed` — the pure half of the indexing loader.
 *
 * These pin the two things that made the old screen unreadable: a flat step
 * list with no phase shape, and a log rendered in full however long it grew.
 */
import { describe, expect, it } from "vitest";

import { ActivityFeed, RENDER_LIMIT } from "@/components/wiki/indexing/activityModel";
import { IndexingPlan } from "@/components/wiki/indexing/planModel";
import type { IndexingLogEntry, ProgressLedger, StepRecord } from "@/components/wiki/api/types";

const NOW = Date.parse("2020-01-01T00:10:00Z");

function step(partial: Partial<StepRecord> & Pick<StepRecord, "key" | "group">): StepRecord {
  return {
    label: partial.key,
    weight: 1,
    state: "pending",
    ...partial,
  } as StepRecord;
}

function ledger(steps: StepRecord[]): ProgressLedger {
  return { version: 1, steps };
}

describe("IndexingPlan", () => {
  it("renders every phase, declared or not, so the rail never starts empty", () => {
    const plan = IndexingPlan.from(ledger([step({ key: "clone.git", group: "clone" })]), null, NOW);

    expect(plan.phases.map((phase) => phase.key)).toEqual([
      "clone",
      "scan",
      "graph",
      "enrich",
      "plan",
      "pages",
      "finalize",
    ]);
    // An undeclared phase is pending with NO invented steps — the outline must
    // never show work the backend has not declared.
    expect(plan.phases.find((phase) => phase.key === "pages")?.steps).toEqual([]);
  });

  it("keeps a group the client's phase vocabulary does not know", () => {
    const plan = IndexingPlan.from(ledger([step({ key: "future.work", group: "future" })]), null, NOW);

    expect(plan.phases.map((phase) => phase.key)).toContain("future");
  });

  it("holds a phase open while some steps have settled and none are running", () => {
    const plan = IndexingPlan.from(
      ledger([
        step({ key: "graph.a", group: "graph", state: "done" }),
        step({ key: "graph.b", group: "graph", state: "pending" }),
      ]),
      null,
      NOW,
    );

    // The gap between two steps is still work in that phase, not a stall.
    expect(plan.phases.find((phase) => phase.key === "graph")?.state).toBe("running");
  });

  it("marks an all-skipped phase skipped, but one real step makes it done", () => {
    const skipped = IndexingPlan.from(
      ledger([
        step({ key: "pages.write", group: "pages", state: "skipped" }),
        step({ key: "pages.check", group: "pages", state: "skipped" }),
      ]),
      null,
      NOW,
    );
    const done = IndexingPlan.from(
      ledger([
        step({ key: "pages.write", group: "pages", state: "skipped" }),
        step({ key: "pages.check", group: "pages", state: "done" }),
      ]),
      null,
      NOW,
    );

    expect(skipped.phases.find((phase) => phase.key === "pages")?.state).toBe("skipped");
    expect(done.phases.find((phase) => phase.key === "pages")?.state).toBe("done");
  });

  it("a failed step fails its whole phase", () => {
    const plan = IndexingPlan.from(
      ledger([
        step({ key: "scan.discover", group: "scan", state: "done" }),
        step({ key: "scan.inspect", group: "scan", state: "failed" }),
      ]),
      null,
      NOW,
    );

    expect(plan.phases.find((phase) => phase.key === "scan")?.state).toBe("failed");
  });

  it("falls back to the coarse phase when no ledger has arrived yet", () => {
    const plan = IndexingPlan.from(null, "graph", NOW);

    expect(plan.phases.find((phase) => phase.key === "clone")?.state).toBe("done");
    expect(plan.phases.find((phase) => phase.key === "graph")?.state).toBe("running");
    expect(plan.phases.find((phase) => phase.key === "pages")?.state).toBe("pending");
    expect(plan.activeStep).toBeNull();
  });

  it("reports the running phase as active, and the furthest started one otherwise", () => {
    const running = IndexingPlan.from(
      ledger([
        step({ key: "clone.git", group: "clone", state: "done" }),
        step({ key: "scan.discover", group: "scan", state: "running" }),
      ]),
      null,
      NOW,
    );
    const settled = IndexingPlan.from(
      ledger([
        step({ key: "clone.git", group: "clone", state: "done" }),
        step({ key: "scan.discover", group: "scan", state: "done" }),
      ]),
      null,
      NOW,
    );

    expect(running.activePhaseKey).toBe("scan");
    expect(settled.activePhaseKey).toBe("scan");
  });

  it("measures a phase from its first start to its last end, or to now while open", () => {
    const closed = IndexingPlan.from(
      ledger([
        step({
          key: "clone.git",
          group: "clone",
          state: "done",
          startedAt: "2020-01-01T00:00:00Z",
          endedAt: "2020-01-01T00:00:30Z",
        }),
      ]),
      null,
      NOW,
    );
    const open = IndexingPlan.from(
      ledger([
        step({
          key: "graph.parse",
          group: "graph",
          state: "running",
          startedAt: "2020-01-01T00:09:00Z",
        }),
      ]),
      null,
      NOW,
    );

    expect(closed.phases.find((phase) => phase.key === "clone")?.elapsedSeconds).toBe(30);
    expect(open.phases.find((phase) => phase.key === "graph")?.elapsedSeconds).toBe(60);
  });

  it("prefers a counter over elapsed time for a step's detail", () => {
    const counted = step({
      key: "graph.parse",
      group: "graph",
      state: "running",
      current: 1200,
      total: 5359,
      unit: "files",
      startedAt: "2020-01-01T00:09:00Z",
    });
    const uncounted = step({
      key: "graph.validate",
      group: "graph",
      state: "running",
      startedAt: "2020-01-01T00:09:00Z",
    });

    expect(IndexingPlan.stepDetail(counted, NOW)).toBe("1,200 of 5,359 files");
    expect(IndexingPlan.stepDetail(uncounted, NOW)).toBe("1m");
  });

  it("drops the redundant half of a completed counter", () => {
    const finished = step({
      key: "graph.parse",
      group: "graph",
      state: "done",
      current: 5359,
      total: 5359,
      unit: "files",
    });
    const stoppedShort = step({
      key: "graph.embed",
      group: "graph",
      state: "failed",
      current: 900,
      total: 13_965,
      unit: "nodes",
    });

    // "5,359 of 5,359" carries no information the state does not, and the
    // wasted width is what forced step labels into an ellipsis.
    expect(IndexingPlan.stepDetail(finished, NOW)).toBe("5,359 files");
    // A step that stopped short keeps both numbers — there the gap IS the point.
    expect(IndexingPlan.stepDetail(stoppedShort, NOW)).toBe("900 of 13,965 nodes");
  });
});

describe("ActivityFeed", () => {
  function line(text: string, step?: string, level: IndexingLogEntry["level"] = "info"): IndexingLogEntry {
    return { text, level, step: step ?? null };
  }

  it("bounds the rendered window and reports the full total", () => {
    const entries = Array.from({ length: 5_000 }, (_, i) => line(`line ${i}`, "graph.embed"));

    const feed = ActivityFeed.from(entries, new Map());

    expect(feed.total).toBe(5_000);
    expect(feed.shown).toBe(RENDER_LIMIT);
    expect(feed.truncated).toBe(true);
    // The tail is kept, not the head — the live edge is what a reader watches.
    expect(feed.runs.at(-1)?.entries.at(-1)?.text).toBe("line 4999");
  });

  it("does not claim truncation for a log that fits", () => {
    const feed = ActivityFeed.from([line("only one")], new Map());

    expect(feed.truncated).toBe(false);
    expect(feed.shown).toBe(1);
  });

  it("folds consecutive lines sharing a step into one run and resolves its label", () => {
    const feed = ActivityFeed.from(
      [
        line("a", "graph.parse"),
        line("b", "graph.parse"),
        line("c", "graph.embed"),
        line("d", "graph.parse"),
      ],
      new Map([
        ["graph.parse", "Parsing source files"],
        ["graph.embed", "Embedding graph nodes"],
      ]),
    );

    expect(feed.runs.map((run) => [run.label, run.entries.length])).toEqual([
      ["Parsing source files", 2],
      ["Embedding graph nodes", 1],
      // A step returning later starts a NEW run — the feed is chronological,
      // never regrouped, so the order lines were emitted in survives.
      ["Parsing source files", 1],
    ]);
  });

  it("keeps an unresolved step key rather than dropping the attribution", () => {
    const feed = ActivityFeed.from([line("x", "graph.future")], new Map());

    expect(feed.runs[0].label).toBe("graph.future");
  });

  it("groups unattributed lines together with no label", () => {
    const feed = ActivityFeed.from([line("boot"), line("still booting")], new Map());

    expect(feed.runs).toHaveLength(1);
    expect(feed.runs[0].label).toBeNull();
    // Nothing in this feed names a step, so there is no gap to point at — a
    // job older than step attribution must not be papered with headings.
    expect(feed.attributed).toBe(false);
  });

  it("flags a feed where SOME lines name a step, so the gaps are visible", () => {
    const feed = ActivityFeed.from(
      [line("inside", "graph.parse"), line("escaped")],
      new Map([["graph.parse", "Parsing source files"]]),
    );

    // A line with no step among attributed ones is work that ran outside its
    // declared scope. Measured on a live index: 102 of 5,470 lines.
    expect(feed.attributed).toBe(true);
    expect(feed.runs.map((run) => run.label)).toEqual(["Parsing source files", null]);
  });

  it("surfaces the worst level present so a caller can flag the pane", () => {
    expect(ActivityFeed.from([line("a"), line("b", undefined, "warn")], new Map()).worstLevel).toBe("warn");
    expect(
      ActivityFeed.from([line("a", undefined, "warn"), line("b", undefined, "error")], new Map()).worstLevel,
    ).toBe("error");
    expect(ActivityFeed.from([], new Map()).worstLevel).toBeNull();
  });
});
