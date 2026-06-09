/**
 * Tests for the Q&A citation grammar — the single parser/normalizer that
 * powers both the inline citation chips and the right-panel source cards.
 *
 * The load-bearing invariant: an inline chip (built via ``fromSrc`` from a
 * ``src:`` link / atom) and the source card (built via ``parse`` from a
 * ``sources``-block string) for the SAME path+range resolve to the SAME DOM
 * id — that identity is what makes chip→card scroll work without prop
 * threading. These tests pin it, plus the card-set dedup + scheme dropping.
 */
import { describe, expect, it } from "vitest";

import { CitationRef, fileCitations } from "@/components/wiki/citations";

describe("CitationRef.parse", () => {
  it("parses a path#L<a>-<b> range into 1-based start/end", () => {
    const c = CitationRef.parse("README.md#L68-71");
    expect(c.path).toBe("README.md");
    expect(c.startLine).toBe(68);
    expect(c.endLine).toBe(71);
    expect(c.isFileSource).toBe(true);
    expect(c.scheme).toBeNull();
  });

  it("parses a single-line path#L<n>", () => {
    const c = CitationRef.parse("src/app.ts#L42");
    expect(c.startLine).toBe(42);
    expect(c.endLine).toBe(42);
  });

  it("parses the terse colon form path:line", () => {
    const c = CitationRef.parse("src/app.ts:42");
    expect(c.path).toBe("src/app.ts");
    expect(c.startLine).toBe(42);
    expect(c.endLine).toBe(42);
  });

  it("treats a bare path as a whole-file source (no range)", () => {
    const c = CitationRef.parse("CLAUDE.md");
    expect(c.path).toBe("CLAUDE.md");
    expect(c.startLine).toBeNull();
    expect(c.isFileSource).toBe(true);
  });

  it("classifies graph:/wiki: schemes as non-file provenance refs", () => {
    const g = CitationRef.parse("graph:hypervisor.py::AgentHypervisor");
    expect(g.scheme).toBe("graph");
    expect(g.isFileSource).toBe(false);
    const w = CitationRef.parse("wiki:core-orchestration");
    expect(w.scheme).toBe("wiki");
    expect(w.isFileSource).toBe(false);
  });
});

describe("CitationRef.domId identity (chip ↔ card)", () => {
  it("agrees for the URL-fragment form and the src-atom form", () => {
    const fromString = CitationRef.parse("README.md#L68-71");
    const fromAtom = CitationRef.fromSrc("README.md", "L68-71");
    expect(CitationRef.domId(fromAtom)).toBe(CitationRef.domId(fromString));
  });

  it("produces a DOM-safe token", () => {
    const id = CitationRef.domId(CitationRef.parse("a/b/c.py#L1-9"));
    expect(id).toMatch(/^src-[a-zA-Z0-9_-]+$/);
  });

  it("collapses a single-line range with equal start/end", () => {
    const a = CitationRef.domId(CitationRef.parse("x.ts#L5"));
    const b = CitationRef.domId(CitationRef.parse("x.ts#L5-5"));
    expect(a).toBe(b);
  });
});

describe("CitationRef.label", () => {
  it("renders path:a–b for a range and bare path otherwise", () => {
    expect(CitationRef.label(CitationRef.parse("README.md#L68-71"))).toBe("README.md:68–71");
    expect(CitationRef.label(CitationRef.parse("README.md#L7"))).toBe("README.md:7");
    expect(CitationRef.label(CitationRef.parse("CLAUDE.md"))).toBe("CLAUDE.md");
  });
});

describe("fileCitations (card set)", () => {
  it("drops graph:/wiki: refs, dedups by path+range, keeps first-seen order", () => {
    const cards = fileCitations([
      "graph:foo::Bar",
      "README.md#L1-9",
      "wiki:page",
      "src/app.ts:42",
      "README.md#L1-9", // dup → dropped
      "",                // empty → dropped
    ]);
    expect(cards.map((c) => c.path)).toEqual(["README.md", "src/app.ts"]);
  });
});
