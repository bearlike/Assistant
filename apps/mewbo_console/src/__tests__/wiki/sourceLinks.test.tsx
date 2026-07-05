/**
 * Citation source-navigation — regression cover for "clicking a cited source
 * link goes nowhere". Two load-bearing guarantees:
 *
 *   1. `IndexedSnapshot.sourceUrl` builds a host-aware repo blob URL (at the
 *      cited line range) for the platforms we support, and refuses to guess
 *      (returns null) for hosts with no portable blob shape / legacy records.
 *   2. An inline `SrcChip`, wrapped in a `SourceHrefProvider` that resolves a
 *      URL, renders a real `target="_blank"` anchor to that URL — not the
 *      old dead scroll-to-card button.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { IndexedSnapshot } from "@/components/wiki/indexedSnapshot";
import { CitationRef } from "@/components/wiki/citations";
import { SrcChip, SourceHrefProvider } from "@/components/wiki/markdownComponents";
import type { Project } from "@/components/wiki/api/types";

afterEach(cleanup);

function project(over: Partial<Project>): Project {
  return {
    slug: "git.example.com/o/r",
    source: "gitea",
    lang: "Python",
    indexedAt: "2026-01-01T00:00:00Z",
    pages: 1,
    desc: "",
    repoUrl: "https://git.example.com/o/r",
    branch: "main",
    ...over,
  };
}

describe("IndexedSnapshot.sourceUrl", () => {
  it("builds a Gitea blob URL with an L-range anchor", () => {
    const snap = IndexedSnapshot.fromProject(project({ source: "gitea" }));
    expect(snap.sourceUrl("src/app.py", 10, 20)).toBe(
      "https://git.example.com/o/r/src/branch/main/src/app.py#L10-L20",
    );
  });

  it("builds a GitHub blob URL", () => {
    const snap = IndexedSnapshot.fromProject(
      project({ source: "github", repoUrl: "https://github.com/o/r" }),
    );
    expect(snap.sourceUrl("a/b.ts", 5, 5)).toBe(
      "https://github.com/o/r/blob/main/a/b.ts#L5",
    );
  });

  it("builds a GitLab blob URL (single-L range form, -/blob path)", () => {
    const snap = IndexedSnapshot.fromProject(
      project({ source: "gitlab", repoUrl: "https://gitlab.com/o/r" }),
    );
    expect(snap.sourceUrl("a.py", 3, 9)).toBe(
      "https://gitlab.com/o/r/-/blob/main/a.py#L3-9",
    );
  });

  it("builds a Bitbucket blob URL (lines- anchor)", () => {
    const snap = IndexedSnapshot.fromProject(
      project({ source: "bitbucket", repoUrl: "https://bitbucket.org/o/r" }),
    );
    expect(snap.sourceUrl("a.py", 3, 9)).toBe(
      "https://bitbucket.org/o/r/src/main/a.py#lines-3:9",
    );
  });

  it("omits the anchor for a whole-file citation", () => {
    const snap = IndexedSnapshot.fromProject(project({ source: "github", repoUrl: "https://github.com/o/r" }));
    expect(snap.sourceUrl("README.md", null, null)).toBe(
      "https://github.com/o/r/blob/main/README.md",
    );
  });

  it("returns null for azure/generic-git hosts (no portable blob shape)", () => {
    expect(IndexedSnapshot.fromProject(project({ source: "azure" })).sourceUrl("a.py", 1, 2)).toBeNull();
    expect(IndexedSnapshot.fromProject(project({ source: "git" })).sourceUrl("a.py", 1, 2)).toBeNull();
  });

  it("returns null when repoUrl or branch is missing", () => {
    expect(IndexedSnapshot.fromProject(project({ repoUrl: undefined })).sourceUrl("a.py", 1)).toBeNull();
    expect(IndexedSnapshot.fromProject(project({ branch: null })).sourceUrl("a.py", 1)).toBeNull();
  });
});

describe("SrcChip navigation", () => {
  const citation = CitationRef.fromSrc("src/app.py", "L10-20");

  it("renders a new-tab anchor to the resolved repo URL", () => {
    render(
      <SourceHrefProvider resolve={() => "https://git.example.com/o/r/src/branch/main/src/app.py#L10-L20"}>
        <SrcChip citation={citation} />
      </SourceHrefProvider>,
    );
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute(
      "href",
      "https://git.example.com/o/r/src/branch/main/src/app.py#L10-L20",
    );
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("falls back to a (non-navigating) button when no repo URL resolves", () => {
    render(
      <SourceHrefProvider resolve={() => null}>
        <SrcChip citation={citation} />
      </SourceHrefProvider>,
    );
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByRole("button")).toBeInTheDocument();
  });
});
