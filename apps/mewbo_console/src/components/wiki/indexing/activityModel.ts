/**
 * `ActivityFeed` — what the indexer log becomes before it is rendered.
 *
 * Two facts drive every rule here, both measured on a live index rather than
 * assumed. A real run emits thousands of lines (5,468 on a mid-size repository
 * while still in the graph phase), and every line the backend emits inside a
 * declared step now carries that step's key. So the raw list is at once too
 * long to render whole and rich enough to read as a narrative.
 *
 * The feed therefore does two things and nothing else: it bounds what is
 * rendered to a tail, and it collapses consecutive lines sharing a step into
 * one run so the reader sees "Embedding graph nodes" once above its output
 * instead of once per line.
 *
 * The full list stays in the reducer. Only the RENDERED window is bounded, and
 * the view states the window's size — a silently trimmed log is what makes two
 * refreshes look like two different runs.
 *
 * Cost class: `O(tail)` — bounded by `RENDER_LIMIT` regardless of history.
 */
import type { IndexingLogEntry } from "../api/types";

/**
 * How many lines the activity pane renders. A scroll container costs one DOM
 * node per line whether or not it is on screen, and the unbounded version put
 * 5,468 rows and 22,205 nodes on a loading screen. Deep enough to scroll back
 * through the current step's output, shallow enough to stay cheap.
 */
export const RENDER_LIMIT = 250;

export interface ActivityRun {
  /** Step key these lines were emitted inside, or null for unattributed work. */
  step: string | null;
  /** Resolved step label, when the ledger declares one. */
  label: string | null;
  entries: IndexingLogEntry[];
}

export class ActivityFeed {
  private constructor(
    /** Consecutive lines folded into per-step runs, oldest first. */
    readonly runs: ActivityRun[],
    /** Lines rendered. */
    readonly shown: number,
    /** Lines received. */
    readonly total: number,
    /**
     * Whether ANY rendered line names a step. It decides how an unattributed
     * run is drawn, and the two cases are genuinely different: a job older than
     * step attribution has no step on any line, so headers would be noise —
     * while a line with no step sitting among attributed ones is work that
     * escaped its declared scope, which is worth showing rather than hiding.
     */
    readonly attributed: boolean,
  ) {}

  /** True when history is longer than the window, so the view can say so. */
  get truncated(): boolean {
    return this.total > this.shown;
  }

  /** The most severe level present in the rendered window. */
  get worstLevel(): IndexingLogEntry["level"] | null {
    let worst: IndexingLogEntry["level"] | null = null;
    for (const run of this.runs) {
      for (const entry of run.entries) {
        if (entry.level === "error") return "error";
        if (entry.level === "warn") worst = "warn";
        else if (worst == null) worst = "info";
      }
    }
    return worst;
  }

  /**
   * Fold `entries` into the rendered feed. `labels` maps a step key to its
   * declared label; a key with no entry (an older job, or a step this client's
   * ledger has not seen) keeps the raw key rather than dropping the attribution
   * — an unresolved label is still a grouping a reader can use.
   */
  static from(entries: IndexingLogEntry[], labels: Map<string, string>): ActivityFeed {
    const window = entries.length > RENDER_LIMIT ? entries.slice(-RENDER_LIMIT) : entries;
    const runs: ActivityRun[] = [];
    for (const entry of window) {
      const step = entry.step ?? null;
      const tail = runs[runs.length - 1];
      if (tail && tail.step === step) tail.entries.push(entry);
      else runs.push({ step, label: step ? (labels.get(step) ?? step) : null, entries: [entry] });
    }
    return new ActivityFeed(
      runs,
      window.length,
      entries.length,
      runs.some((run) => run.step != null),
    );
  }
}
