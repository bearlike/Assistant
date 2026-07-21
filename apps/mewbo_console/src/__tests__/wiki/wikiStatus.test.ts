/**
 * Unit coverage for the wiki index-status verdict kernel. Pure data in, one
 * verdict out — every state the card can show, plus the operational precedence
 * between them, is pinned here without mounting a component.
 */
import { describe, expect, it } from "vitest";

import {
  classifyFreshness,
  deriveWikiStatus,
  type WikiStatusInput,
} from "@/components/wiki/wikiStatus";
import type {
  IndexingJob,
  ProjectFreshness,
  RecoverableJob,
} from "@/components/wiki/api/types";

function freshness(partial: Partial<ProjectFreshness>): ProjectFreshness {
  return {
    indexedSha: null,
    remoteSha: null,
    behindBy: null,
    upToDate: null,
    checkedAt: null,
    ...partial,
  };
}

function activeJob(partial: Partial<IndexingJob> = {}): IndexingJob {
  return {
    jobId: "job-1",
    slug: "git.example.com/acme/widgets",
    status: "scanning",
    scannedCount: 3,
    totalCount: 10,
    currentFile: null,
    ...partial,
  };
}

function recoverableJob(partial: Partial<RecoverableJob> = {}): RecoverableJob {
  return {
    jobId: "job-1",
    slug: "git.example.com/acme/widgets",
    status: "failed",
    recoverable: { skip: [], pagesDone: 0 },
    ...partial,
  } as RecoverableJob;
}

const NONE: WikiStatusInput = { freshness: undefined };

describe("classifyFreshness — the shared freshness kernel", () => {
  it("counts behind commits when the platform could compare", () => {
    expect(classifyFreshness(freshness({ behindBy: 4 }))).toEqual({
      cls: "behind",
      behindBy: 4,
    });
  });

  it("reads behindBy === 0 as fresh", () => {
    expect(classifyFreshness(freshness({ behindBy: 0 })).cls).toBe("fresh");
  });

  it("treats an uncounted-but-moved sha as an update", () => {
    expect(
      classifyFreshness(freshness({ behindBy: null, remoteSha: "bbb", indexedSha: "aaa" })).cls,
    ).toBe("update");
  });

  it("treats behindBy: null with no readable remote as unknown, never zero", () => {
    expect(classifyFreshness(freshness({ behindBy: null, remoteSha: null })).cls).toBe("unknown");
    expect(classifyFreshness(undefined).cls).toBe("unknown");
  });
});

describe("deriveWikiStatus — the five decision-relevant verdicts", () => {
  it("up to date when the count is zero (calm, success, no attention)", () => {
    const s = deriveWikiStatus({ freshness: freshness({ behindBy: 0 }) });
    expect(s.kind).toBe("up-to-date");
    expect(s.tone).toBe("success");
    expect(s.attention).toBe(false);
    expect(s.busy).toBe(false);
    expect(s.headline).toBe("Up to date");
  });

  it("up to date when the shas match even though commits went uncounted", () => {
    const s = deriveWikiStatus({
      freshness: freshness({ behindBy: null, remoteSha: "abc123", indexedSha: "abc123" }),
    });
    expect(s.kind).toBe("up-to-date");
  });

  it("up to date when the server asserts upToDate without a count", () => {
    const s = deriveWikiStatus({ freshness: freshness({ behindBy: null, upToDate: true }) });
    expect(s.kind).toBe("up-to-date");
  });

  it("behind N pluralises and asks for a re-index (warning, attention)", () => {
    const one = deriveWikiStatus({ freshness: freshness({ behindBy: 1 }) });
    expect(one.headline).toBe("1 commit behind");
    const many = deriveWikiStatus({ freshness: freshness({ behindBy: 12 }) });
    expect(many.kind).toBe("behind");
    expect(many.tone).toBe("warning");
    expect(many.attention).toBe(true);
    expect(many.headline).toBe("12 commits behind");
  });

  it("update available when the remote moved but couldn't be counted (info)", () => {
    const s = deriveWikiStatus({
      freshness: freshness({ behindBy: null, remoteSha: "new", indexedSha: "old" }),
    });
    expect(s.kind).toBe("update-available");
    expect(s.tone).toBe("info");
    expect(s.attention).toBe(true);
  });

  it("indexing when a job is running, and it is busy", () => {
    const s = deriveWikiStatus({ freshness: freshness({ behindBy: 0 }), activeJob: activeJob() });
    expect(s.kind).toBe("indexing");
    expect(s.tone).toBe("info");
    expect(s.busy).toBe(true);
    expect(s.attention).toBe(true);
  });

  it("failed with a destructive tone when the last index crashed", () => {
    const s = deriveWikiStatus({
      freshness: freshness({ behindBy: 0 }),
      recoverableJob: recoverableJob({ status: "failed" }),
    });
    expect(s.kind).toBe("failed");
    expect(s.tone).toBe("destructive");
    expect(s.headline).toBe("Last index failed");
    expect(s.busy).toBe(false);
  });

  it("failed-kind but warning tone for a user-cancelled run", () => {
    const s = deriveWikiStatus({
      freshness: freshness({ behindBy: 0 }),
      recoverableJob: recoverableJob({ status: "cancelled" }),
    });
    expect(s.kind).toBe("failed");
    expect(s.tone).toBe("warning");
    expect(s.headline).toBe("Last index was cancelled");
  });

  it("failed-kind, destructive, for an interrupted run", () => {
    const s = deriveWikiStatus({
      recoverableJob: recoverableJob({ status: "interrupted" }),
      freshness: undefined,
    });
    expect(s.kind).toBe("failed");
    expect(s.tone).toBe("destructive");
    expect(s.headline).toBe("Last index was interrupted");
  });
});

describe("deriveWikiStatus — honest indeterminate states (never a false green)", () => {
  it("checking while the freshness probe is still in flight", () => {
    const s = deriveWikiStatus({ freshness: undefined, freshnessPending: true });
    expect(s.kind).toBe("checking");
    expect(s.attention).toBe(false);
  });

  it("unknown when there is no freshness and nothing is loading", () => {
    const s = deriveWikiStatus(NONE);
    expect(s.kind).toBe("unknown");
    expect(s.tone).toBe("neutral");
    expect(s.attention).toBe(false);
  });

  it("unknown when the remote couldn't be read to compare", () => {
    const s = deriveWikiStatus({
      freshness: freshness({ behindBy: null, remoteSha: null, indexedSha: "aaa" }),
    });
    expect(s.kind).toBe("unknown");
  });
});

describe("deriveWikiStatus — operational precedence", () => {
  it("a running job dominates a stale count", () => {
    const s = deriveWikiStatus({
      freshness: freshness({ behindBy: 9 }),
      activeJob: activeJob(),
    });
    expect(s.kind).toBe("indexing");
  });

  it("a running job dominates a failed remnant", () => {
    const s = deriveWikiStatus({
      activeJob: activeJob(),
      recoverableJob: recoverableJob(),
      freshness: freshness({ behindBy: 0 }),
    });
    expect(s.kind).toBe("indexing");
  });

  it("a failed remnant dominates a clean freshness read", () => {
    const s = deriveWikiStatus({
      recoverableJob: recoverableJob(),
      freshness: freshness({ behindBy: 0 }),
    });
    expect(s.kind).toBe("failed");
  });
});
