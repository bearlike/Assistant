/**
 * Tests for IndexingProgress — the atomic class that drives both the
 * landing-card progress and the indexing-page progress bar.
 *
 * The whole point of this class is that ``fromJob`` and ``fromStream``
 * produce identical pct/label/phase output for equivalent inputs — these
 * tests pin that property.
 */
import { describe, expect, it } from "vitest";

import { IndexingProgress } from "@/components/wiki/progress";
import type { IndexingJob } from "@/components/wiki/api/types";

const baseJob: IndexingJob = {
  jobId: "j1",
  slug: "g/o/r",
  status: "scanning",
  scannedCount: 0,
  totalCount: 0,
  currentFile: null,
};

describe("IndexingProgress.fromJob", () => {
  it("renders 0% for a brand-new queued job", () => {
    const v = IndexingProgress.fromJob({ ...baseJob, status: "queued", phase: "clone" });
    expect(v.pct).toBe(0);
    expect(v.label).toBe("Cloning repository");
  });

  it("scales scan progress into the 5..20 bracket", () => {
    const v = IndexingProgress.fromJob({
      ...baseJob,
      status: "scanning",
      phase: "scan",
      scannedCount: 15,
      totalCount: 30,
    });
    // 50% of [5..20] ≈ 12.5 → 13
    expect(v.pct).toBeGreaterThanOrEqual(12);
    expect(v.pct).toBeLessThanOrEqual(13);
    expect(v.statusLine).toBe("15 of 30 files");
  });

  it("uses snapshot phase even when legacy status is 'finalizing'", () => {
    // Legacy status==='finalizing' used to peg at 96% — phase now wins.
    const v = IndexingProgress.fromJob({
      ...baseJob,
      status: "finalizing",
      phase: "graph",
    });
    expect(v.phase).toBe("graph");
    expect(v.label).toBe("Building knowledge graph");
    // Inside [20..35] with sub=0.
    expect(v.pct).toBeGreaterThanOrEqual(20);
    expect(v.pct).toBeLessThan(35);
  });

  it("returns 100% on complete", () => {
    const v = IndexingProgress.fromJob({ ...baseJob, status: "complete" });
    expect(v.pct).toBe(100);
    expect(v.statusLine).toBe("Done");
    expect(v.etaSeconds).toBe(0);
  });

  it("computes a page-bar inside the pages phase", () => {
    const v = IndexingProgress.fromJob({
      ...baseJob,
      status: "finalizing",
      phase: "pages",
      totalPages: 20,
      pagesSubmitted: 5,
    });
    expect(v.statusLine).toBe("Page 5 of 20");
    // 5/20 = 25% of [45..95] = 12.5 above 45 → ~57
    expect(v.pct).toBeGreaterThanOrEqual(57);
    expect(v.pct).toBeLessThanOrEqual(58);
  });
});

describe("IndexingProgress.fromJob vs fromStream — agreement", () => {
  it("produces the same pct/label for equivalent inputs", () => {
    const job: IndexingJob = {
      ...baseJob,
      status: "finalizing",
      phase: "pages",
      totalPages: 10,
      pagesSubmitted: 4,
    };
    const fromJob = IndexingProgress.fromJob(job);
    const fromStream = IndexingProgress.fromStream({
      job,
      phase: "pages",
      pagesSubmitted: 4,
      totalPages: 10,
    });
    expect(fromStream.pct).toBe(fromJob.pct);
    expect(fromStream.label).toBe(fromJob.label);
    expect(fromStream.phase).toBe(fromJob.phase);
    expect(fromStream.statusLine).toBe(fromJob.statusLine);
  });
});

describe("IndexingProgress.formatEta", () => {
  it("returns '' for null / 0 / NaN / Infinity", () => {
    expect(IndexingProgress.formatEta(null)).toBe("");
    expect(IndexingProgress.formatEta(0)).toBe("");
    expect(IndexingProgress.formatEta(NaN)).toBe("");
    expect(IndexingProgress.formatEta(Infinity)).toBe("");
  });

  it("formats sub-minute seconds", () => {
    expect(IndexingProgress.formatEta(42)).toBe("~42s left");
  });

  it("formats minutes", () => {
    expect(IndexingProgress.formatEta(180)).toBe("~3 min left");
  });

  it("formats hours + minutes", () => {
    expect(IndexingProgress.formatEta(3600 + 1500)).toMatch(/^~1h \d+m left$/);
  });
});

describe("IndexingProgress.fromJob — ETA via phaseStartedAt", () => {
  it("extrapolates per-page from elapsed time + pagesSubmitted", () => {
    // 60s elapsed in pages phase, 3/10 pages done → 20s per page,
    // 7 remaining → ~140s inPhase + finalize budget (~20s) = ~160s.
    const startedAt = new Date(Date.now() - 60_000).toISOString();
    const v = IndexingProgress.fromJob({
      ...baseJob,
      status: "finalizing",
      phase: "pages",
      totalPages: 10,
      pagesSubmitted: 3,
      phaseStartedAt: startedAt,
    });
    expect(v.etaSeconds).not.toBeNull();
    const eta = v.etaSeconds ?? 0;
    expect(eta).toBeGreaterThan(120);
    expect(eta).toBeLessThan(200);
  });
});
