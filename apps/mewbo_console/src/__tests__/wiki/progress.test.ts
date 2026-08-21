/**
 * Tests for IndexingProgress — the atomic class that drives both the
 * landing-card progress and the indexing-page progress bar.
 *
 * The whole point of this class is that ``fromJob`` and ``fromStream``
 * produce identical pct/label/phase output for equivalent inputs — these
 * tests pin that property.
 */
import { describe, expect, it } from "vitest";

import { IndexingProgress, PHASE_ORDER } from "@/components/wiki/progress";
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

describe("enrich phase", () => {
  it("is ordered between graph and plan", () => {
    const gi = PHASE_ORDER.indexOf("graph");
    const ei = PHASE_ORDER.indexOf("enrich");
    const pi = PHASE_ORDER.indexOf("plan");
    expect(gi).toBeLessThan(ei);
    expect(ei).toBeLessThan(pi);
  });

  it("renders a label and a monotonic pct for enrich", () => {
    const v = IndexingProgress.fromJob({
      ...baseJob,
      status: "scanning",
      phase: "enrich",
    });
    expect(v.label.length).toBeGreaterThan(0);
    expect(v.pct).toBeGreaterThan(0);
    expect(v.pct).toBeLessThan(100);
  });

  it("places enrich pct inside the [32,40] bracket", () => {
    const v = IndexingProgress.fromJob({ ...baseJob, status: "scanning", phase: "enrich" });
    expect(v.pct).toBeGreaterThanOrEqual(32);
    expect(v.pct).toBeLessThan(40);
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

describe("IndexingProgress — progress ledger", () => {
  const ledgerJob = (steps: IndexingJob["progress"] extends infer Ledger ? Ledger : never): IndexingJob => ({
    ...baseJob,
    status: "scanning",
    progress: steps,
  });

  it("does not read a completed first step as 100% or ETA zero while later steps are pending", () => {
    const v = IndexingProgress.fromJob(ledgerJob({
      version: 1,
      steps: [
        { key: "scan.files", label: "Scanning files", group: "scan", unit: "files", weight: 1, state: "done", current: 10, total: 10, startedAt: new Date(Date.now() - 120_000).toISOString() },
        { key: "graph.resolve", label: "Resolving Python symbols", group: "graph", weight: 3, state: "pending" },
      ],
    }));
    expect(v.pct).toBe(25);
    expect(v.etaSeconds).toBeGreaterThan(0);
  });

  it("renders an uncountable active step by its own label and elapsed time", () => {
    const v = IndexingProgress.fromJob(ledgerJob({
      version: 1,
      steps: [
        { key: "scan.files", label: "Scanning files", group: "scan", unit: "files", weight: 1, state: "done", current: 30, total: 30 },
        { key: "graph.resolve", label: "Resolving Python symbols", group: "graph", weight: 3, state: "running", startedAt: new Date(Date.now() - 47 * 60_000).toISOString() },
      ],
    }));
    expect(v.label).toBe("Resolving Python symbols");
    expect(v.statusLine).toMatch(/^Resolving Python symbols · 47m$/);
    expect(v.statusLine).not.toContain("30 of 30");
  });

  it("does not retreat across a counted step boundary and keeps ETA continuous", () => {
    const startedAt = new Date(Date.now() - 120_000).toISOString();
    const activeA = IndexingProgress.fromJob(ledgerJob({
      version: 1,
      steps: [
        { key: "scan.files", label: "Scanning files", group: "scan", unit: "files", weight: 1, state: "running", current: 10, total: 10, startedAt },
        { key: "graph.nodes", label: "Building nodes", group: "graph", unit: "nodes", weight: 3, state: "pending" },
      ],
    }));
    const activeB = IndexingProgress.fromJob(ledgerJob({
      version: 1,
      steps: [
        { key: "scan.files", label: "Scanning files", group: "scan", unit: "files", weight: 1, state: "done", current: 10, total: 10, startedAt },
        { key: "graph.nodes", label: "Building nodes", group: "graph", unit: "nodes", weight: 3, state: "running", current: 1, total: 100, startedAt: new Date(Date.now() - 1_000).toISOString() },
      ],
    }));
    expect(activeB.pct).toBeGreaterThanOrEqual(activeA.pct);
    expect(activeA.etaSeconds).not.toBeNull();
    expect(activeB.etaSeconds).not.toBeNull();
    expect((activeB.etaSeconds ?? 0) / (activeA.etaSeconds ?? 1)).toBeLessThan(10);
  });

  it("uses the phase fallback unchanged when a job has no ledger", () => {
    const v = IndexingProgress.fromJob({ ...baseJob, status: "scanning", phase: "graph", phaseProgressCurrent: 30, phaseProgressTotal: 60, phaseProgressUnit: "nodes" });
    expect(v.pct).toBe(26);
    expect(v.statusLine).toBe("30 of 60 nodes");
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
  it("extrapolates per-page from elapsed time + pagesSubmitted, scoped to the pages phase alone", () => {
    // 60s elapsed in pages phase, 3/10 pages done → 20s per page, 7 remaining
    // → ~140s. No trailing-phase (finalize) budget is added anymore — see
    // "no guessed ETA" below.
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
    expect(eta).toBeLessThan(160);
  });
});

// ── Defect: pct pins + ETA lies for clone/graph/enrich/plan/finalize ──────
// These phases used to fall back to a fixed PHASE_BUDGET_S guess (graph=90s,
// enrich=90s — "sized for ~30 files, 25 pages") that never scaled with repo
// size and never decreased. The fix: no fixed-budget fallback, no
// trailing-phase summation, and a generic phaseProgressCurrent/
// phaseProgressTotal/phaseProgressUnit sub-progress ladder for phases beyond
// scan/pages — ONE mechanism, not a field pair per phase.

describe("IndexingProgress — no fabricated ETA without a measured rate", () => {
  it("reports no ETA for graph/enrich/clone/plan/finalize when phaseProgressCurrent is absent (old job, or before the BE emits it)", () => {
    for (const phase of ["clone", "graph", "enrich", "plan", "finalize"] as const) {
      const v = IndexingProgress.fromJob({
        ...baseJob,
        status: "scanning",
        phase,
        phaseStartedAt: new Date(Date.now() - 5 * 60_000).toISOString(),
      });
      expect(v.etaSeconds, `phase=${phase}`).toBeNull();
    }
  });

  it("a huge elapsed time in an unmeasurable phase still reports no ETA — never a stale, non-decreasing number", () => {
    // This is the reported symptom: a real index sat at a frozen bar with a
    // stale ETA for 25 minutes. No measured rate ⇒ no ETA, regardless of
    // how long the phase has been running.
    const v = IndexingProgress.fromJob({
      ...baseJob,
      status: "scanning",
      phase: "graph",
      phaseStartedAt: new Date(Date.now() - 25 * 60_000).toISOString(),
    });
    expect(v.etaSeconds).toBeNull();
  });

  it("does not add a trailing-phase guess on top of an otherwise-measured pages ETA", () => {
    // Historically this summed a ~20s finalize budget on top of the real
    // pages-phase estimate. The ETA is now scoped to the current phase only.
    const startedAt = new Date(Date.now() - 100_000).toISOString();
    const v = IndexingProgress.fromJob({
      ...baseJob,
      status: "finalizing",
      phase: "pages",
      totalPages: 10,
      pagesSubmitted: 5,
      phaseStartedAt: startedAt,
    });
    // rate = 100s/5 = 20s/page, 5 remaining → exactly 100s, no trailing add-on.
    expect(v.etaSeconds).toBeCloseTo(100, 0);
  });
});

describe("IndexingProgress — generic phaseProgressCurrent/phaseProgressTotal/phaseProgressUnit sub-progress", () => {
  it("fills sub-progress + a unit-labeled status line for graph once phaseProgressCurrent/Total/Unit are present", () => {
    const v = IndexingProgress.fromJob({
      ...baseJob,
      status: "scanning",
      phase: "graph",
      phaseProgressCurrent: 30,
      phaseProgressTotal: 60,
      phaseProgressUnit: "nodes",
    });
    expect(v.statusLine).toBe("30 of 60 nodes");
    // 50% of [20,32] = 26.
    expect(v.pct).toBe(26);
  });

  it("falls back to a generic unit label when phaseProgressUnit is absent", () => {
    const v = IndexingProgress.fromJob({
      ...baseJob,
      status: "scanning",
      phase: "graph",
      phaseProgressCurrent: 30,
      phaseProgressTotal: 60,
    });
    expect(v.statusLine).toBe("30 of 60 units");
  });

  it("shows a real status line even with no known total yet, without inventing a pct fraction", () => {
    const v = IndexingProgress.fromJob({
      ...baseJob,
      status: "scanning",
      phase: "enrich",
      phaseProgressCurrent: 12,
      phaseProgressTotal: null,
      phaseProgressUnit: "entities",
    });
    expect(v.statusLine).toBe("12 entities processed");
    // No total ⇒ sub stays 0 ⇒ pinned at the phase floor, same as before —
    // but at least the line is honest instead of empty.
    expect(v.pct).toBe(32);
  });

  it("extrapolates a real, measured ETA for enrich once phaseProgressCurrent/Total + phaseStartedAt are all present", () => {
    // 40s elapsed, 4/20 entities done → 10s/entity, 16 remaining → 160s.
    const startedAt = new Date(Date.now() - 40_000).toISOString();
    const v = IndexingProgress.fromJob({
      ...baseJob,
      status: "scanning",
      phase: "enrich",
      phaseProgressCurrent: 4,
      phaseProgressTotal: 20,
      phaseStartedAt: startedAt,
    });
    expect(v.etaSeconds).not.toBeNull();
    expect(v.etaSeconds ?? 0).toBeGreaterThan(140);
    expect(v.etaSeconds ?? 0).toBeLessThan(170);
  });

  it("a phase that never populates phaseProgressCurrent (clone/plan/finalize) keeps the historical empty status line + floor pct", () => {
    for (const phase of ["clone", "plan"] as const) {
      const v = IndexingProgress.fromJob({ ...baseJob, status: "scanning", phase });
      expect(v.statusLine, `phase=${phase}`).toBe("");
    }
  });

  it("fromJob and fromStream agree on the generic sub-progress ladder", () => {
    const job: IndexingJob = {
      ...baseJob,
      status: "scanning",
      phase: "graph",
      phaseProgressCurrent: 9,
      phaseProgressTotal: 12,
    };
    const fromJob = IndexingProgress.fromJob(job);
    const fromStream = IndexingProgress.fromStream({
      job,
      phase: "graph",
      pagesSubmitted: 0,
      totalPages: null,
    });
    expect(fromStream.pct).toBe(fromJob.pct);
    expect(fromStream.statusLine).toBe(fromJob.statusLine);
  });
});
