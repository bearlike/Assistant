/**
 * IndexingProgress — atomic class describing one job's progress.
 *
 * The landing page card polls the snapshot endpoint (``IndexingJob``)
 * and the indexing page consumes the SSE stream (folded
 * ``IndexingStreamState``). Both surfaces used to compute progress
 * independently and disagreed:
 *
 *   - Landing card: ``(scanned/total)*96`` only — ignored ``phase``,
 *     pegged at 96 for the entire graph/plan/pages window.
 *   - Indexing page: phase-weighted ranges with sub-progress per phase.
 *
 * This class is the single source of truth. ``fromJob`` and ``fromStream``
 * both feed the same private ``_compute`` core, so the two views can
 * never drift apart again. ETA is extrapolated from the elapsed time in
 * the current phase plus a fixed budget for what comes after.
 *
 * Convention: an atomic class — frozen state in the instance, behaviour
 * on the prototype, static helpers off the class.
 */
import type { IndexingJob, IndexingPhase } from "./api/types";

/** Indexing-screen reducer state (a subset — only the fields we read here). */
export interface IndexingStreamSnapshot {
  job: IndexingJob | null;
  phase: IndexingPhase | null;
  totalPages: number | null;
  pagesSubmitted: number;
}

// Phase weights — calibrated to real run shape.
// Each phase has a (start, end) percent range; sub-progress fills the
// range linearly. The bar never finishes a phase at 100% until the NEXT
// phase event arrives (sub is clamped to 0.98 inside ``_compute``).
const PHASE_RANGE: Record<IndexingPhase, [number, number]> = {
  clone: [0, 5],
  scan: [5, 20],
  graph: [20, 32],
  enrich: [32, 40],
  plan: [40, 45],
  pages: [45, 95],
  finalize: [95, 100],
};

const PHASE_LABEL: Record<IndexingPhase, string> = {
  clone: "Cloning repository",
  scan: "Scanning files",
  graph: "Building knowledge graph",
  enrich: "Extracting entities",
  plan: "Planning wiki structure",
  pages: "Writing wiki pages",
  finalize: "Finalizing",
};

export const PHASE_ORDER: readonly IndexingPhase[] = [
  "clone",
  "scan",
  "graph",
  "enrich",
  "plan",
  "pages",
  "finalize",
];

// Per-phase budget in seconds — used for ETA extrapolation when we
// don't have a measured rate yet. These are rough averages from real
// Grove-scale runs (~30 files, 25 pages) — the indexing page surfaces
// them as a hint, not a promise. KISS: a tiny lookup table is much
// simpler than tracking historical rates per slug.
const PHASE_BUDGET_S: Record<IndexingPhase, number> = {
  clone: 30,
  scan: 60,
  graph: 90,
  enrich: 90,
  plan: 60,
  pages: 60, // per-page; multiplied by remaining pages
  finalize: 20,
};

export interface ProgressView {
  /** Whole-number percent for the bar (0-100). */
  pct: number;
  /** Phase used for rendering. Falls back to ``"clone"`` on unknown. */
  phase: IndexingPhase;
  /** Heading line — "Cloning repository", "Writing wiki pages", … */
  label: string;
  /** Sub-line — "12 of 30 files", "Page 4 of 25", or empty. */
  statusLine: string;
  /**
   * Seconds remaining estimate. ``null`` when we lack enough signal:
   * either the run hasn't entered a measurable phase yet or it's already
   * complete.
   */
  etaSeconds: number | null;
}

interface ComputeInput {
  phase: IndexingPhase | null;
  status: IndexingJob["status"] | undefined;
  scannedCount: number;
  totalCount: number;
  pagesSubmitted: number;
  totalPages: number | null;
  phaseStartedAt: string | null | undefined;
}

export class IndexingProgress {
  /** Compute from a snapshot ``IndexingJob`` (landing card path). */
  static fromJob(job: IndexingJob | null | undefined): ProgressView {
    if (!job) {
      return { pct: 0, phase: "clone", label: PHASE_LABEL.clone, statusLine: "", etaSeconds: null };
    }
    return IndexingProgress._compute({
      phase: (job.phase ?? null) as IndexingPhase | null,
      status: job.status,
      scannedCount: job.scannedCount ?? 0,
      totalCount: job.totalCount ?? 0,
      pagesSubmitted: job.pagesSubmitted ?? 0,
      totalPages: job.totalPages ?? null,
      phaseStartedAt: job.phaseStartedAt ?? null,
    });
  }

  /** Compute from a folded SSE state (indexing page path). */
  static fromStream(state: IndexingStreamSnapshot): ProgressView {
    return IndexingProgress._compute({
      phase: state.phase,
      status: state.job?.status,
      scannedCount: state.job?.scannedCount ?? 0,
      totalCount: state.job?.totalCount ?? 0,
      pagesSubmitted: state.pagesSubmitted,
      totalPages: state.totalPages,
      // SSE state doesn't carry phaseStartedAt; the snapshot path does.
      // ETA on the indexing page falls back to the snapshot through
      // ``fromJob`` when the caller has it (most pages render both).
      phaseStartedAt: state.job?.phaseStartedAt ?? null,
    });
  }

  /** Human label for *phase*, exposed for the phase strip. */
  static label(phase: IndexingPhase): string {
    return PHASE_LABEL[phase];
  }

  /** Format *seconds* as "~3 min left" / "~45 s left"; ``""`` if null. */
  static formatEta(seconds: number | null): string {
    if (seconds == null || seconds <= 0 || !Number.isFinite(seconds)) return "";
    if (seconds < 60) return `~${Math.round(seconds)}s left`;
    const m = Math.round(seconds / 60);
    if (m < 60) return `~${m} min left`;
    const h = Math.floor(m / 60);
    const rem = m % 60;
    return rem ? `~${h}h ${rem}m left` : `~${h}h left`;
  }

  // ── Internal ────────────────────────────────────────────────────────

  private static _compute(input: ComputeInput): ProgressView {
    // Pick a phase — explicit if known, else infer from the legacy
    // status field so old runs and the snapshot endpoint (which only
    // recently learned about ``phase``) still render meaningfully.
    let phase: IndexingPhase = input.phase ?? "clone";
    if (!input.phase) {
      if (input.status === "scanning") phase = "scan";
      else if (input.status === "finalizing") phase = "pages";
      else if (input.status === "complete") phase = "finalize";
    }
    if (input.status === "complete") {
      return { pct: 100, phase: "finalize", label: PHASE_LABEL.finalize, statusLine: "Done", etaSeconds: 0 };
    }

    const [lo, hi] = PHASE_RANGE[phase];
    let sub = 0;
    let line = "";
    if (phase === "scan" && input.totalCount > 0) {
      sub = input.scannedCount / Math.max(1, input.totalCount);
      line = `${input.scannedCount} of ${input.totalCount} files`;
    } else if (phase === "pages" && (input.totalPages ?? 0) > 0) {
      sub = input.pagesSubmitted / Math.max(1, input.totalPages ?? 1);
      line = `Page ${input.pagesSubmitted} of ${input.totalPages}`;
    }
    sub = Math.max(0, Math.min(1, sub));

    // Headroom: don't paint 100% of the phase until the next phase event arrives.
    const reach = lo + (hi - lo) * Math.min(sub, 0.98);
    const pct = Math.round(reach);

    return {
      pct,
      phase,
      label: PHASE_LABEL[phase] ?? "Indexing repository",
      statusLine: line,
      etaSeconds: IndexingProgress._eta({ ...input, phase, sub }),
    };
  }

  private static _eta(
    input: ComputeInput & { phase: IndexingPhase; sub: number },
  ): number | null {
    // No timestamp → can't extrapolate measured rate. Fall back to the
    // raw remaining-budget estimate so the user gets *something* useful.
    const elapsed = input.phaseStartedAt
      ? Math.max(0, Date.now() / 1000 - new Date(input.phaseStartedAt).getTime() / 1000)
      : null;

    // Phase-local ETA: how long is left inside the current phase?
    let inPhase: number;
    if (input.phase === "pages" && (input.totalPages ?? 0) > 0) {
      const remaining = Math.max(0, (input.totalPages ?? 0) - input.pagesSubmitted);
      // Prefer measured per-page rate when we have ≥1 page committed:
      // (elapsed / pagesSubmitted) extrapolated to remaining pages.
      if (elapsed != null && input.pagesSubmitted > 0) {
        inPhase = (elapsed / input.pagesSubmitted) * remaining;
      } else {
        inPhase = PHASE_BUDGET_S.pages * remaining;
      }
    } else if (input.phase === "scan" && input.totalCount > 0) {
      const remaining = Math.max(0, input.totalCount - input.scannedCount);
      if (elapsed != null && input.scannedCount > 0) {
        inPhase = (elapsed / input.scannedCount) * remaining;
      } else {
        inPhase = (PHASE_BUDGET_S.scan / Math.max(1, input.totalCount)) * remaining;
      }
    } else if (elapsed != null && input.sub > 0) {
      // Generic linear extrapolation: ``elapsed / sub`` is total-phase
      // estimate; subtract elapsed for remaining.
      inPhase = elapsed / Math.max(0.01, input.sub) - elapsed;
    } else {
      inPhase = PHASE_BUDGET_S[input.phase];
    }

    // Trailing phases: sum their fixed budgets.
    const idx = PHASE_ORDER.indexOf(input.phase);
    let trailing = 0;
    for (let i = idx + 1; i < PHASE_ORDER.length; i++) {
      const p = PHASE_ORDER[i];
      if (p === "pages" && (input.totalPages ?? 0) > 0) {
        // For pages phase not yet entered, scale per-page budget by plan size.
        trailing += PHASE_BUDGET_S.pages * (input.totalPages ?? 0);
      } else {
        trailing += PHASE_BUDGET_S[p];
      }
    }
    const total = inPhase + trailing;
    if (!Number.isFinite(total) || total <= 0) return null;
    return total;
  }
}
