/**
 * Render coverage for the wiki index-status card. One assertion runs through
 * every verdict: the card states its status with a WORD and a GLYPH (never
 * colour alone), and surfaces exactly one re-index action — primary when the
 * verdict asks for action, neutral when the wiki is current, and replaced by a
 * progress link while an index is already running.
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RefreshThisWiki } from "@/components/wiki/RefreshThisWiki";
import type { ProjectFreshness } from "@/components/wiki/api/types";

const mocks = vi.hoisted(() => ({
  freshness: vi.fn(),
  active: vi.fn(),
  recoverable: vi.fn(),
  refresh: vi.fn(),
}));

vi.mock("@/components/wiki/api/hooks", () => ({
  useProjectFreshness: () => mocks.freshness(),
  useActiveIndexingJobs: () => mocks.active(),
  useRecoverableJobs: () => mocks.recoverable(),
  useRequestWikiRefresh: () => ({ mutate: mocks.refresh, isPending: false }),
}));

const SLUG = "git.example.com/acme/widgets";

function freshness(partial: Partial<ProjectFreshness>) {
  return {
    data: {
      indexedSha: null,
      remoteSha: null,
      behindBy: null,
      upToDate: null,
      checkedAt: null,
      ...partial,
    },
    isLoading: false,
  };
}

beforeEach(() => {
  mocks.freshness.mockReturnValue({ data: undefined, isLoading: false });
  mocks.active.mockReturnValue({ data: [] });
  mocks.recoverable.mockReturnValue({ data: [] });
  mocks.refresh.mockReset();
});

afterEach(cleanup);

/** The verdict live-region — the card's status readout. */
function verdict() {
  return screen.getByRole("status");
}

describe("RefreshThisWiki — verdict is a word + a glyph", () => {
  it("up to date: the word, a glyph, and a calm (neutral) re-index button", () => {
    mocks.freshness.mockReturnValue(freshness({ behindBy: 0 }));
    render(<RefreshThisWiki slug={SLUG} snapshot={null} />);

    const v = verdict();
    expect(v).toHaveAttribute("data-kind", "up-to-date");
    expect(within(v).getByText("Up to date")).toBeInTheDocument();
    expect(v.querySelector("svg")).toBeTruthy();
    // Calm: the action is present but does not shout.
    expect(screen.getByRole("button", { name: /re-index this wiki/i })).toBeInTheDocument();
  });

  it("behind: the counted word + glyph, attention re-index still one action", () => {
    mocks.freshness.mockReturnValue(freshness({ behindBy: 3 }));
    render(<RefreshThisWiki slug={SLUG} snapshot={null} />);

    const v = verdict();
    expect(v).toHaveAttribute("data-kind", "behind");
    expect(within(v).getByText("3 commits behind")).toBeInTheDocument();
    expect(v.querySelector("svg")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: /re-index this wiki/i })).toHaveLength(1);
  });

  it("update available: word + glyph", () => {
    mocks.freshness.mockReturnValue(
      freshness({ behindBy: null, remoteSha: "new", indexedSha: "old" }),
    );
    render(<RefreshThisWiki slug={SLUG} snapshot={null} />);

    const v = verdict();
    expect(v).toHaveAttribute("data-kind", "update-available");
    expect(within(v).getByText("Update available")).toBeInTheDocument();
    expect(v.querySelector("svg")).toBeTruthy();
  });

  it("indexing: states the verdict and swaps the action for a progress link", () => {
    mocks.freshness.mockReturnValue(freshness({ behindBy: 0 }));
    mocks.active.mockReturnValue({
      data: [{ jobId: "j1", slug: SLUG, status: "scanning" }],
    });
    render(<RefreshThisWiki slug={SLUG} snapshot={null} />);

    const v = verdict();
    expect(v).toHaveAttribute("data-kind", "indexing");
    expect(within(v).getByText("Indexing now")).toBeInTheDocument();
    // No re-index while indexing — the one action becomes "view progress".
    expect(screen.queryByRole("button", { name: /re-index this wiki/i })).toBeNull();
    expect(screen.getByRole("link", { name: /view progress/i })).toHaveAttribute(
      "href",
      expect.stringContaining("jobId=j1"),
    );
  });

  it("failed: word + glyph, re-index offered as the remedy", () => {
    mocks.freshness.mockReturnValue(freshness({ behindBy: 0 }));
    mocks.recoverable.mockReturnValue({
      data: [{ jobId: "j1", slug: SLUG, status: "failed", recoverable: {} }],
    });
    render(<RefreshThisWiki slug={SLUG} snapshot={null} />);

    const v = verdict();
    expect(v).toHaveAttribute("data-kind", "failed");
    expect(within(v).getByText("Last index failed")).toBeInTheDocument();
    expect(v.querySelector("svg")).toBeTruthy();
    expect(screen.getByRole("button", { name: /re-index this wiki/i })).toBeInTheDocument();
  });

  it("unknown: says so honestly rather than showing a false green", () => {
    mocks.freshness.mockReturnValue({ data: undefined, isLoading: false });
    render(<RefreshThisWiki slug={SLUG} snapshot={null} />);

    const v = verdict();
    expect(v).toHaveAttribute("data-kind", "unknown");
    expect(within(v).getByText(/freshness unknown/i)).toBeInTheDocument();
    // Explicitly NOT the green verdict.
    expect(within(v).queryByText("Up to date")).toBeNull();
  });
});
