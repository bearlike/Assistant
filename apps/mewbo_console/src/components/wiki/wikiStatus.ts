/**
 * wikiStatus — the pure, React-free verdict a reader needs from the wiki
 * index-status card: "is this wiki current, and do I need to re-index?"
 *
 * The card used to present raw drift numbers and leave the judgment to the
 * reader. `deriveWikiStatus` makes the judgment ONCE, from the existing data
 * contracts, and hands back a single verdict the card renders verdict-first.
 *
 * Five decision-relevant verdicts (the states the spec names), plus two honest
 * indeterminate ones that keep the card from ever showing a FALSE green:
 *
 *   indexing          — an index job is running for this slug right now
 *   failed            — the last index attempt stopped before it finished
 *   behind            — N commits behind the repo's remote HEAD
 *   update-available  — the remote moved but the commit count is unknown
 *   up-to-date        — the indexed snapshot matches the remote
 *   checking          — the freshness probe is still in flight
 *   unknown           — the remote couldn't be read; we decline to guess
 *
 * Precedence is operational, not alphabetical: a live job dominates a stale
 * count (you can't act on drift mid-index), a failed attempt dominates a clean
 * freshness read (the snapshot you're reading is the older good one), and only
 * then does the freshness comparison speak.
 *
 * `classifyFreshness` is the shared freshness→class kernel — it mirrors the
 * `FreshnessBadge` state table exactly so the badge and this card never drift
 * apart on what "behind" / "update" / "fresh" mean. The card layers the job
 * states and the honest-indeterminate refinements on top; the badge keeps its
 * own "stay silent until there's drift" stance and simply renders nothing for
 * the classes it doesn't show.
 *
 * Models never import I/O: freshness, the running job and the recoverable job
 * all arrive as plain data, so a test injects fixtures instead of mocking a
 * clock or a query.
 */

import type { IndexingJob, ProjectFreshness, RecoverableJob } from "./api/types";

export type WikiStatusKind =
  | "indexing"
  | "failed"
  | "behind"
  | "update-available"
  | "up-to-date"
  | "checking"
  | "unknown";

/** Status-token family the verdict paints with (never colour alone — the
 *  headline always carries the word too). */
export type WikiStatusTone = "success" | "warning" | "info" | "destructive" | "neutral";

export interface WikiStatus {
  kind: WikiStatusKind;
  tone: WikiStatusTone;
  /** The verdict phrase — the WORD that answers "is this wiki current?". */
  headline: string;
  /** A supporting sentence (why / what to do); null when the word stands alone. */
  detail: string | null;
  /** True when the verdict wants the reader to act — drives card prominence. */
  attention: boolean;
  /** True while an index job is running — the action area shows progress, not
   *  a re-index (you can't re-index a wiki that's mid-index). */
  busy: boolean;
}

export interface WikiStatusInput {
  /** `GET …/freshness`; null/undefined while the query has no data yet. */
  freshness: ProjectFreshness | null | undefined;
  /** Freshness query still in flight (no data, no error) — drives "checking". */
  freshnessPending?: boolean;
  /** The running (non-terminal) index job for this slug, if any. */
  activeJob?: IndexingJob | null;
  /** The terminal-but-incomplete index job for this slug, if any. */
  recoverableJob?: RecoverableJob | null;
}

export type FreshnessClass = "fresh" | "behind" | "update" | "unknown";

/**
 * The freshness→class kernel, byte-for-byte the `FreshnessBadge` table:
 *   behindBy === 0                                   → fresh
 *   behindBy  >  0                                   → behind (carries the count)
 *   behindBy == null && remoteSha && remoteSha≠sha   → update
 *   anything else                                    → unknown
 *
 * `behindBy: null` is "couldn't count", NEVER "zero" (the wire contract says
 * so) — it falls through to the sha comparison, and if that can't decide
 * either, to `unknown`. The card refines `unknown` further (a matched sha is a
 * safe green); the badge treats `unknown` as "nothing to say".
 */
export function classifyFreshness(
  f: ProjectFreshness | null | undefined,
): { cls: FreshnessClass; behindBy: number } {
  if (!f) return { cls: "unknown", behindBy: 0 };
  const { behindBy, remoteSha, indexedSha } = f;
  if (behindBy === 0) return { cls: "fresh", behindBy: 0 };
  if (typeof behindBy === "number" && behindBy > 0) return { cls: "behind", behindBy };
  if (behindBy == null && remoteSha && remoteSha !== indexedSha) {
    return { cls: "update", behindBy: 0 };
  }
  return { cls: "unknown", behindBy: 0 };
}

const UP_TO_DATE: WikiStatus = {
  kind: "up-to-date",
  tone: "success",
  headline: "Up to date",
  detail: null,
  attention: false,
  busy: false,
};

const CHECKING: WikiStatus = {
  kind: "checking",
  tone: "neutral",
  headline: "Checking for updates",
  detail: null,
  attention: false,
  busy: false,
};

const UNKNOWN: WikiStatus = {
  kind: "unknown",
  tone: "neutral",
  headline: "Freshness unknown",
  detail: "The repository's latest commit couldn't be read to compare.",
  attention: false,
  busy: false,
};

/** The remote sha and the indexed sha are both known and equal — a safe green
 *  even when the platform couldn't count commits (`behindBy: null`). */
function shaMatched(f: ProjectFreshness): boolean {
  return (
    f.upToDate === true ||
    Boolean(f.remoteSha && f.indexedSha && f.remoteSha === f.indexedSha)
  );
}

function indexingStatus(): WikiStatus {
  return {
    kind: "indexing",
    tone: "info",
    headline: "Indexing now",
    detail: "Building an updated snapshot from the latest commits.",
    attention: true,
    busy: true,
  };
}

function failedStatus(job: RecoverableJob): WikiStatus {
  // A cancelled run was a user choice, not a fault — draw attention (the wiki is
  // still the older snapshot) but at the calmer warning tone, not destructive.
  const cancelled = job.status === "cancelled";
  const headline =
    job.status === "failed"
      ? "Last index failed"
      : job.status === "cancelled"
        ? "Last index was cancelled"
        : "Last index was interrupted";
  return {
    kind: "failed",
    tone: cancelled ? "warning" : "destructive",
    headline,
    detail: "A newer index attempt didn't finish; the snapshot below is the last good one.",
    attention: true,
    busy: false,
  };
}

function behindStatus(behindBy: number): WikiStatus {
  return {
    kind: "behind",
    tone: "warning",
    headline: `${behindBy} commit${behindBy === 1 ? "" : "s"} behind`,
    detail: "Re-index to pull the latest commits into this wiki.",
    attention: true,
    busy: false,
  };
}

const UPDATE_AVAILABLE: WikiStatus = {
  kind: "update-available",
  tone: "info",
  headline: "Update available",
  detail: "The repository moved since this wiki was indexed.",
  attention: true,
  busy: false,
};

/**
 * Reduce every input signal to the ONE verdict the card renders. Pure: same
 * inputs, same verdict, no clock and no query.
 */
export function deriveWikiStatus(input: WikiStatusInput): WikiStatus {
  const { freshness, freshnessPending, activeJob, recoverableJob } = input;

  // A live job dominates: re-index drift is meaningless mid-index.
  if (activeJob) return indexingStatus();
  // A terminal-but-incomplete attempt: what you're reading is the older good
  // snapshot, and the remedy is a fresh re-index.
  if (recoverableJob) return failedStatus(recoverableJob);

  if (freshness) {
    const { cls, behindBy } = classifyFreshness(freshness);
    if (cls === "behind") return behindStatus(behindBy);
    if (cls === "update") return UPDATE_AVAILABLE;
    if (cls === "fresh") return UP_TO_DATE;
    // cls === "unknown": a matched sha (or an authoritative upToDate) is still a
    // safe green; otherwise the remote genuinely couldn't be compared.
    return shaMatched(freshness) ? UP_TO_DATE : UNKNOWN;
  }

  // No freshness yet: distinguish "still probing" from "gave up / no data".
  return freshnessPending ? CHECKING : UNKNOWN;
}
