/**
 * `IndexingPlan` — the declared plan reshaped for reading, nothing else.
 *
 * The ledger arrives as a flat, ordered `StepRecord[]` whose `group` is the
 * indexer's phase name. Rendering that list verbatim is what made the loader
 * unreadable: twenty-odd rows of `DONE` with no sense of where the run is, and
 * a page that grew a little taller with every phase. This class folds the flat
 * list into the seven phases the run actually has, so the screen can show the
 * coarse shape always and the fine detail only where a reader is looking.
 *
 * Pure and React-free on purpose — the grouping and the state derivation are
 * the part worth unit-testing, and a component that owns them cannot be tested
 * without mounting it.
 *
 * Cost class: `O(one record)` — one pass over one job's declared steps.
 */
import type { IndexingPhase, ProgressLedger, StepRecord } from "../api/types";
import { PHASE_ORDER, PHASE_SHORT_LABEL, IndexingProgress } from "../progress";

/**
 * A phase's state, derived from its steps. The vocabulary deliberately mirrors
 * `StepRecord["state"]` so a phase and a step read the same way in one list.
 */
export type PhaseState = StepRecord["state"];

export interface PhaseEntry {
  /** Phase name — the `group` its steps carry. */
  key: string;
  /** Short human label for the rail and the outline header. */
  label: string;
  state: PhaseState;
  /** Declared steps, in order. Empty while a phase is still undeclared. */
  steps: StepRecord[];
  /** Steps that reached a terminal state. */
  settled: number;
  /** Seconds from this phase's first start to its last end (or now). */
  elapsedSeconds: number | null;
}

const TERMINAL: ReadonlySet<PhaseState> = new Set<PhaseState>(["done", "skipped", "failed"]);

export class IndexingPlan {
  private constructor(
    /** Every phase in run order, declared or not. */
    readonly phases: PhaseEntry[],
    /** The open step, when one is running. */
    readonly activeStep: StepRecord | null,
  ) {}

  /** The phase currently doing work, or the furthest one that has started. */
  get activePhaseKey(): string | null {
    const running = this.phases.find((phase) => phase.state === "running");
    if (running) return running.key;
    const settled = [...this.phases].reverse().find((phase) => phase.state !== "pending");
    return settled?.key ?? null;
  }

  /** Phases carrying at least one declared step — what the outline can show. */
  get declared(): PhaseEntry[] {
    return this.phases.filter((phase) => phase.steps.length > 0);
  }

  /** Every declared step keyed for log attribution lookups. */
  get labelsByKey(): Map<string, string> {
    const labels = new Map<string, string>();
    for (const phase of this.phases) {
      for (const step of phase.steps) labels.set(step.key, step.label);
    }
    return labels;
  }

  /**
   * Build from a ledger. `fallbackPhase` covers a job with no ledger at all
   * (an older snapshot, or the window before the first declaration lands): the
   * rail still renders the seven phases so the screen never starts empty.
   *
   * A phase the backend has not declared yet is NOT invented — it renders as
   * `pending` with no steps, which is exactly what it is.
   */
  static from(
    ledger: ProgressLedger | null | undefined,
    fallbackPhase: IndexingPhase | null,
    now: number,
  ): IndexingPlan {
    const steps = ledger?.steps ?? [];
    const byGroup = new Map<string, StepRecord[]>();
    for (const step of steps) {
      const bucket = byGroup.get(step.group);
      if (bucket) bucket.push(step);
      else byGroup.set(step.group, [step]);
    }

    // Run order first, then any group the backend added that this client's
    // phase vocabulary does not know — an unknown phase renders as itself
    // rather than vanishing, which is what makes the outline safe to extend
    // from the backend alone.
    const ordered: string[] = [...PHASE_ORDER];
    for (const key of byGroup.keys()) {
      if (!ordered.includes(key)) ordered.push(key);
    }

    const fallbackIndex = fallbackPhase ? ordered.indexOf(fallbackPhase) : -1;
    const phases = ordered.map((key, index) =>
      IndexingPlan._phase(key, byGroup.get(key) ?? [], index, fallbackIndex, now),
    );
    return new IndexingPlan(phases, steps.find((step) => step.state === "running") ?? null);
  }

  private static _phase(
    key: string,
    steps: StepRecord[],
    index: number,
    fallbackIndex: number,
    now: number,
  ): PhaseEntry {
    return {
      key,
      label: PHASE_SHORT_LABEL[key as IndexingPhase] ?? key,
      state: IndexingPlan._state(steps, index, fallbackIndex),
      steps,
      settled: steps.filter((step) => TERMINAL.has(step.state)).length,
      elapsedSeconds: IndexingPlan._elapsed(steps, now),
    };
  }

  /**
   * A phase is `failed` if any step failed, `running` while any step is open OR
   * some but not all have settled (the gap between two steps is still work in
   * that phase), and terminal only once every step has settled. `skipped` needs
   * every step skipped — one real step done makes the phase done.
   *
   * With no declared steps there is nothing to derive from, so the phase falls
   * back to its position against the coarse `phase` field. That fallback is the
   * ONLY thing keeping the rail meaningful for a job whose ledger declares one
   * phase at a time; it is deliberately never used to invent step rows.
   */
  private static _state(steps: StepRecord[], index: number, fallbackIndex: number): PhaseState {
    if (steps.length === 0) {
      if (fallbackIndex < 0) return "pending";
      if (index < fallbackIndex) return "done";
      return index === fallbackIndex ? "running" : "pending";
    }
    if (steps.some((step) => step.state === "failed")) return "failed";
    if (steps.some((step) => step.state === "running")) return "running";
    const settled = steps.filter((step) => TERMINAL.has(step.state));
    if (settled.length === 0) return "pending";
    if (settled.length < steps.length) return "running";
    return settled.every((step) => step.state === "skipped") ? "skipped" : "done";
  }

  private static _elapsed(steps: StepRecord[], now: number): number | null {
    let first: number | null = null;
    let last: number | null = null;
    let open = false;
    for (const step of steps) {
      const started = IndexingPlan._stamp(step.startedAt);
      if (started == null) continue;
      first = first == null ? started : Math.min(first, started);
      const ended = IndexingPlan._stamp(step.endedAt);
      if (ended == null) open = true;
      else last = last == null ? ended : Math.max(last, ended);
    }
    if (first == null) return null;
    const end = open ? now : (last ?? now);
    return Math.max(0, (end - first) / 1000);
  }

  private static _stamp(value: string | null | undefined): number | null {
    if (!value) return null;
    const stamp = new Date(value).getTime();
    return Number.isFinite(stamp) ? stamp : null;
  }

  /**
   * Counter for a step, or its elapsed time when it counts nothing.
   *
   * A finished step that reached its total says "5,359 files", not "5,359 of
   * 5,359 files": the fraction only carries information while it is moving,
   * and the redundant half is what pushed step labels into an ellipsis.
   */
  static stepDetail(step: StepRecord, now: number): string {
    if (step.current != null && (step.total ?? 0) > 0) {
      const unit = step.unit ?? "units";
      const total = step.total ?? 0;
      if (TERMINAL.has(step.state) && step.current >= total) {
        return `${total.toLocaleString()} ${unit}`;
      }
      return `${step.current.toLocaleString()} of ${total.toLocaleString()} ${unit}`;
    }
    if (step.state === "running") {
      const started = IndexingPlan._stamp(step.startedAt);
      return IndexingProgress.formatElapsed(started == null ? null : (now - started) / 1000);
    }
    if (step.note) return step.note;
    if (step.current != null) {
      return `${step.current.toLocaleString()} ${step.unit ?? "units"}`;
    }
    return "";
  }
}
