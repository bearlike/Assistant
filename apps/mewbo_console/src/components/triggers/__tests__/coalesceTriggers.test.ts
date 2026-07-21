import { describe, expect, it } from "vitest";
import { coalesceAdjacentTriggers } from "../coalesceTriggers";
import type { TimelineEntry } from "../../../types";

function fired(
  id: string,
  ts: string,
  opts: { triggerId?: string; kind?: string; summary?: string } = {},
): TimelineEntry {
  return {
    id,
    role: "trigger",
    content: "",
    turnId: "triggers",
    ts,
    trigger: {
      triggerId: opts.triggerId,
      kind: opts.kind ?? "time.cron",
      action: "fired",
      summary: opts.summary,
    },
  };
}

function armed(id: string, ts: string, opts: { triggerId?: string; kind?: string } = {}): TimelineEntry {
  return {
    id,
    role: "trigger",
    content: "",
    turnId: "triggers",
    ts,
    trigger: {
      triggerId: opts.triggerId,
      kind: opts.kind ?? "time.cron",
      action: "armed",
    },
  };
}

function userTurn(id: string, ts: string): TimelineEntry {
  return { id, role: "user", content: "hi", turnId: id, ts };
}

describe("coalesceAdjacentTriggers", () => {
  it("passes a lone trigger entry through unchanged (N=1, no count)", () => {
    const entries = [fired("t1", "2026-07-19T08:00:00Z", { triggerId: "cron-1" })];
    const result = coalesceAdjacentTriggers(entries);
    expect(result).toHaveLength(1);
    expect(result[0]).toBe(entries[0]); // same reference — untouched
    expect(result[0].trigger?.count).toBeUndefined();
  });

  it("folds adjacent fires of the same trigger id into one row with count", () => {
    const entries = [
      fired("t1", "2026-07-19T08:00:00Z", { triggerId: "cron-1" }),
      fired("t2", "2026-07-19T08:01:00Z", { triggerId: "cron-1" }),
      fired("t3", "2026-07-19T08:02:00Z", { triggerId: "cron-1" }),
    ];
    const result = coalesceAdjacentTriggers(entries);
    expect(result).toHaveLength(1);
    expect(result[0].id).toBe("t1"); // stable key = first entry of the group
    expect(result[0].ts).toBe("2026-07-19T08:02:00Z"); // latest ts
    expect(result[0].trigger?.count).toBe(3);
    expect(result[0].trigger?.firstTs).toBe("2026-07-19T08:00:00Z");
  });

  it("falls back to kind identity when no trigger id is present", () => {
    const entries = [
      fired("t1", "2026-07-19T08:00:00Z"),
      fired("t2", "2026-07-19T08:01:00Z"),
    ];
    const result = coalesceAdjacentTriggers(entries);
    expect(result).toHaveLength(1);
    expect(result[0].trigger?.count).toBe(2);
  });

  it("does not merge different trigger ids even of the same kind", () => {
    const entries = [
      fired("t1", "2026-07-19T08:00:00Z", { triggerId: "cron-1" }),
      fired("t2", "2026-07-19T08:01:00Z", { triggerId: "cron-2" }),
    ];
    const result = coalesceAdjacentTriggers(entries);
    expect(result).toHaveLength(2);
    expect(result[0].trigger?.count).toBeUndefined();
    expect(result[1].trigger?.count).toBeUndefined();
  });

  it("does not merge different kinds when no trigger id is present", () => {
    const entries = [
      fired("t1", "2026-07-19T08:00:00Z", { kind: "time.cron" }),
      fired("t2", "2026-07-19T08:01:00Z", { kind: "webhook" }),
    ];
    const result = coalesceAdjacentTriggers(entries);
    expect(result).toHaveLength(2);
  });

  it("does not merge armed and fired for the same trigger id — different sentences", () => {
    const entries = [
      armed("t1", "2026-07-19T08:00:00Z", { triggerId: "cron-1" }),
      fired("t2", "2026-07-19T08:01:00Z", { triggerId: "cron-1" }),
    ];
    const result = coalesceAdjacentTriggers(entries);
    expect(result).toHaveLength(2);
    expect(result[0].trigger?.action).toBe("armed");
    expect(result[1].trigger?.action).toBe("fired");
  });

  it("splits a run when a non-trigger entry interleaves (order stays legible)", () => {
    const entries = [
      fired("t1", "2026-07-19T08:00:00Z", { triggerId: "cron-1" }),
      userTurn("u1", "2026-07-19T08:00:30Z"),
      fired("t2", "2026-07-19T08:01:00Z", { triggerId: "cron-1" }),
      fired("t3", "2026-07-19T08:02:00Z", { triggerId: "cron-1" }),
    ];
    const result = coalesceAdjacentTriggers(entries);
    expect(result).toHaveLength(3);
    expect(result[0].role).toBe("trigger");
    expect(result[0].trigger?.count).toBeUndefined();
    expect(result[1].role).toBe("user");
    expect(result[2].role).toBe("trigger");
    expect(result[2].trigger?.count).toBe(2);
  });

  it("carries the latest entry's summary into the merged row", () => {
    const entries = [
      fired("t1", "2026-07-19T08:00:00Z", { triggerId: "cron-1", summary: "first payload" }),
      fired("t2", "2026-07-19T08:01:00Z", { triggerId: "cron-1", summary: "second payload" }),
    ];
    const result = coalesceAdjacentTriggers(entries);
    expect(result[0].trigger?.summary).toBe("second payload");
  });

  it("leaves non-trigger entries untouched and in order", () => {
    const entries = [userTurn("u1", "2026-07-19T08:00:00Z"), userTurn("u2", "2026-07-19T08:00:10Z")];
    const result = coalesceAdjacentTriggers(entries);
    expect(result).toEqual(entries);
  });
});
