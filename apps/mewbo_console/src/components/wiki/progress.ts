/**
 * IndexingProgress — atomic class describing one job's progress.
 *
 * The landing page card polls the snapshot endpoint and the indexing page
 * consumes folded SSE state. Both enter through this class so the two surfaces
 * cannot disagree about a job's current work or its remaining estimate.
 */
import type { IndexingJob, IndexingPhase, ProgressLedger, StepRecord } from "./api/types";

/** Indexing-screen reducer state (a subset — only the fields we read here). */
export interface IndexingStreamSnapshot {
  job: IndexingJob | null;
  phase: IndexingPhase | null;
  totalPages: number | null;
  pagesSubmitted: number;
}

// The weights now live with the work on the backend. This table is strictly the
// no-ledger fallback for an in-flight un-migrated job or an older snapshot.
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

/**
 * One-word phase names for the phase rail and the plan outline, where the
 * sentence-length `PHASE_LABEL` would wrap seven times across a strip. The
 * long label still carries the header, so a reader gets the sentence where
 * there is room for one and the noun where there is not.
 */
export const PHASE_SHORT_LABEL: Record<IndexingPhase, string> = {
  clone: "Clone",
  scan: "Scan",
  graph: "Graph",
  enrich: "Entities",
  plan: "Plan",
  pages: "Pages",
  finalize: "Finish",
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

const DEFAULT_PROGRESS_UNIT = "units";
const TERMINAL_STATES = new Set<StepRecord["state"]>(["done", "skipped", "failed"]);

export interface ProgressView {
  /** Whole-number percent for the bar (0-100). */
  pct: number;
  /** Phase used for the legacy phase strip. Falls back to ``clone``. */
  phase: IndexingPhase;
  /** The active ledger step's label, or the fallback phase label. */
  label: string;
  /** Active counter, or an uncountable step's label plus its elapsed time. */
  statusLine: string;
  /** Whole-operation estimate for a ledger, current-phase estimate otherwise. */
  etaSeconds: number | null;
  /** Elapsed from the first declared step that opened, when a ledger exists. */
  elapsedSeconds: number | null;
}

interface ComputeInput {
  phase: IndexingPhase | null;
  status: IndexingJob["status"] | undefined;
  scannedCount: number;
  totalCount: number;
  pagesSubmitted: number;
  totalPages: number | null;
  phaseStartedAt: string | null | undefined;
  phaseProgressCurrent: number | null;
  phaseProgressTotal: number | null;
  phaseProgressUnit: string | null;
  progress: ProgressLedger | undefined;
}

export class IndexingProgress {
  /** Compute from a snapshot ``IndexingJob`` (landing card path). */
  static fromJob(job: IndexingJob | null | undefined): ProgressView {
    if (!job) return IndexingProgress._empty();
    return IndexingProgress._compute({
      phase: (job.phase ?? null) as IndexingPhase | null,
      status: job.status,
      scannedCount: job.scannedCount ?? 0,
      totalCount: job.totalCount ?? 0,
      pagesSubmitted: job.pagesSubmitted ?? 0,
      totalPages: job.totalPages ?? null,
      phaseStartedAt: job.phaseStartedAt ?? null,
      phaseProgressCurrent: job.phaseProgressCurrent ?? null,
      phaseProgressTotal: job.phaseProgressTotal ?? null,
      phaseProgressUnit: job.phaseProgressUnit ?? null,
      progress: job.progress,
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
      phaseStartedAt: state.job?.phaseStartedAt ?? null,
      phaseProgressCurrent: state.job?.phaseProgressCurrent ?? null,
      phaseProgressTotal: state.job?.phaseProgressTotal ?? null,
      phaseProgressUnit: state.job?.phaseProgressUnit ?? null,
      progress: state.job?.progress,
    });
  }

  /** Human label for *phase*, exposed for the phase strip. */
  static label(phase: IndexingPhase): string {
    return PHASE_LABEL[phase];
  }

  /** Format *seconds* as "~3 min left" / "~45 s left"; ``""`` if unusable. */
  static formatEta(seconds: number | null): string {
    if (seconds == null || seconds <= 0 || !Number.isFinite(seconds)) return "";
    if (seconds < 60) return `~${Math.round(seconds)}s left`;
    const m = Math.round(seconds / 60);
    if (m < 60) return `~${m} min left`;
    const h = Math.floor(m / 60);
    const rem = m % 60;
    return rem ? `~${h}h ${rem}m left` : `~${h}h left`;
  }

  /** Compact elapsed duration for an open, uncountable step. */
  static formatElapsed(seconds: number | null): string {
    if (seconds == null || seconds < 0 || !Number.isFinite(seconds)) return "running";
    const whole = Math.floor(seconds);
    if (whole < 60) return `${whole}s`;
    const minutes = Math.floor(whole / 60);
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    const remainder = minutes % 60;
    return remainder ? `${hours}h ${remainder}m` : `${hours}h`;
  }

  // ── Ledger path ─────────────────────────────────────────────────────

  private static _ledger(input: ComputeInput, ledger: ProgressLedger): ProgressView {
    const now = Date.now();
    const totalWeight = ledger.steps.reduce((total, step) => total + step.weight, 0) || 1;
    const spentWeight = ledger.steps.reduce(
      (spent, step) => spent + IndexingProgress._progressedWeight(step),
      0,
    );
    const fraction = Math.max(0, Math.min(1, spentWeight / totalWeight));
    const startedAt = ledger.steps
      .map((step) => IndexingProgress._stamp(step.startedAt))
      .filter((stamp): stamp is number => stamp != null)
      .sort((a, b) => a - b)[0];
    const elapsedSeconds = startedAt == null ? null : Math.max(0, (now - startedAt) / 1000);
    const remainingWeight = totalWeight - spentWeight;
    const etaSeconds =
      elapsedSeconds == null || elapsedSeconds <= 0 || spentWeight <= 0 || remainingWeight <= 0
        ? null
        : remainingWeight * (elapsedSeconds / spentWeight);
    const active = ledger.steps.find((step) => step.state === "running");
    const phase = IndexingProgress._phase(active?.group ?? input.phase, input.status);

    if (!active) {
      return {
        pct: Math.round(fraction * 100),
        phase,
        label: input.status === "complete" ? PHASE_LABEL.finalize : PHASE_LABEL[phase],
        statusLine: input.status === "complete" ? "Done" : "",
        etaSeconds,
        elapsedSeconds,
      };
    }

    const counted = IndexingProgress._counted(active);
    const activeElapsed = IndexingProgress._elapsed(active, now);
    return {
      pct: Math.round(fraction * 100),
      phase,
      label: active.label,
      statusLine: counted
        ? `${active.current ?? 0} of ${active.total} ${active.unit ?? DEFAULT_PROGRESS_UNIT}`
        : `${active.label} · ${IndexingProgress.formatElapsed(activeElapsed)}`,
      etaSeconds,
      elapsedSeconds,
    };
  }

  private static _progressedWeight(step: StepRecord): number {
    if (TERMINAL_STATES.has(step.state)) return step.weight;
    if (!IndexingProgress._counted(step)) return 0;
    return step.weight * Math.max(0, Math.min(1, (step.current ?? 0) / (step.total ?? 1)));
  }

  private static _counted(step: StepRecord): boolean {
    return step.current != null && (step.total ?? 0) > 0;
  }

  private static _stamp(value: string | null | undefined): number | null {
    if (!value) return null;
    const stamp = new Date(value).getTime();
    return Number.isFinite(stamp) ? stamp : null;
  }

  private static _elapsed(step: StepRecord, now: number): number | null {
    const started = IndexingProgress._stamp(step.startedAt);
    if (started == null) return null;
    const ended = IndexingProgress._stamp(step.endedAt);
    return Math.max(0, ((ended ?? now) - started) / 1000);
  }

  // ── Legacy no-ledger fallback ────────────────────────────────────────

  private static _compute(input: ComputeInput): ProgressView {
    if (input.progress) return IndexingProgress._ledger(input, input.progress);

    const phase = IndexingProgress._phase(input.phase, input.status);
    if (input.status === "complete") {
      return {
        pct: 100,
        phase: "finalize",
        label: PHASE_LABEL.finalize,
        statusLine: "Done",
        etaSeconds: 0,
        elapsedSeconds: null,
      };
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
    } else if (input.phaseProgressCurrent != null) {
      const current = input.phaseProgressCurrent;
      const total = input.phaseProgressTotal;
      const unit = input.phaseProgressUnit ?? DEFAULT_PROGRESS_UNIT;
      if (total != null && total > 0) {
        sub = current / Math.max(1, total);
        line = `${current} of ${total} ${unit}`;
      } else {
        line = `${current} ${unit} processed`;
      }
    }
    sub = Math.max(0, Math.min(1, sub));
    const pct = Math.round(lo + (hi - lo) * Math.min(sub, 0.98));
    return {
      pct,
      phase,
      label: PHASE_LABEL[phase],
      statusLine: line,
      etaSeconds: IndexingProgress._legacyEta({ ...input, phase, sub }),
      elapsedSeconds: null,
    };
  }

  private static _phase(
    candidate: string | null | undefined,
    status: IndexingJob["status"] | undefined,
  ): IndexingPhase {
    if (candidate && PHASE_ORDER.includes(candidate as IndexingPhase)) return candidate as IndexingPhase;
    if (status === "scanning") return "scan";
    if (status === "finalizing") return "pages";
    if (status === "complete") return "finalize";
    return "clone";
  }

  private static _empty(): ProgressView {
    return {
      pct: 0,
      phase: "clone",
      label: PHASE_LABEL.clone,
      statusLine: "",
      etaSeconds: null,
      elapsedSeconds: null,
    };
  }

  /** The legacy phase estimate remains measured-rate-or-nothing. */
  private static _legacyEta(
    input: ComputeInput & { phase: IndexingPhase; sub: number },
  ): number | null {
    const startedAt = IndexingProgress._stamp(input.phaseStartedAt);
    const elapsed = startedAt == null ? null : Math.max(0, (Date.now() - startedAt) / 1000);
    let inPhase: number | null;
    if (input.phase === "pages" && (input.totalPages ?? 0) > 0) {
      const remaining = Math.max(0, (input.totalPages ?? 0) - input.pagesSubmitted);
      inPhase = elapsed != null && input.pagesSubmitted > 0
        ? (elapsed / input.pagesSubmitted) * remaining
        : null;
    } else if (input.phase === "scan" && input.totalCount > 0) {
      const remaining = Math.max(0, input.totalCount - input.scannedCount);
      inPhase = elapsed != null && input.scannedCount > 0
        ? (elapsed / input.scannedCount) * remaining
        : null;
    } else if (elapsed != null && input.sub > 0) {
      inPhase = elapsed / Math.max(0.01, input.sub) - elapsed;
    } else {
      inPhase = null;
    }
    return inPhase == null || !Number.isFinite(inPhase) || inPhase < 0 ? null : inPhase;
  }
}
