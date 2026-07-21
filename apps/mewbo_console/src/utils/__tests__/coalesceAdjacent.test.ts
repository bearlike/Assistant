import { describe, expect, it } from "vitest";
import { coalesceAdjacent } from "../coalesceAdjacent";

type Item = { id: string; kind: string; n: number };

function byKind(item: Item): string | null {
  return item.kind;
}

function sumMerge(group: Item[]): Item {
  return { id: group[0].id, kind: group[0].kind, n: group.reduce((acc, g) => acc + g.n, 0) };
}

describe("coalesceAdjacent", () => {
  it("passes a single item through unchanged (same reference)", () => {
    const items: Item[] = [{ id: "a", kind: "x", n: 1 }];
    const result = coalesceAdjacent(items, byKind, sumMerge);
    expect(result).toHaveLength(1);
    expect(result[0]).toBe(items[0]);
  });

  it("merges a run of adjacent same-key items via merge()", () => {
    const items: Item[] = [
      { id: "a", kind: "x", n: 1 },
      { id: "b", kind: "x", n: 2 },
      { id: "c", kind: "x", n: 3 },
    ];
    const result = coalesceAdjacent(items, byKind, sumMerge);
    expect(result).toHaveLength(1);
    expect(result[0]).toEqual({ id: "a", kind: "x", n: 6 });
  });

  it("keeps non-adjacent same-key runs separate when interleaved", () => {
    const items: Item[] = [
      { id: "a", kind: "x", n: 1 },
      { id: "b", kind: "y", n: 10 },
      { id: "c", kind: "x", n: 2 },
    ];
    const result = coalesceAdjacent(items, byKind, sumMerge);
    expect(result).toHaveLength(3);
    expect(result.map((r) => r.id)).toEqual(["a", "b", "c"]);
  });

  it("never merges across a null key, even for two consecutive nulls", () => {
    const items: Item[] = [
      { id: "a", kind: "x", n: 1 },
      { id: "b", kind: "x", n: 2 },
    ];
    const result = coalesceAdjacent(items, () => null, sumMerge);
    expect(result).toHaveLength(2);
    expect(result[0]).toBe(items[0]);
    expect(result[1]).toBe(items[1]);
  });

  it("preserves order across mixed runs of different lengths", () => {
    const items: Item[] = [
      { id: "a", kind: "x", n: 1 },
      { id: "b", kind: "x", n: 2 },
      { id: "c", kind: "y", n: 3 },
      { id: "d", kind: "z", n: 4 },
      { id: "e", kind: "z", n: 5 },
      { id: "f", kind: "z", n: 6 },
    ];
    const result = coalesceAdjacent(items, byKind, sumMerge);
    expect(result.map((r) => r.id)).toEqual(["a", "c", "d"]);
    expect(result.map((r) => r.n)).toEqual([3, 3, 15]);
  });

  it("returns an empty array for an empty input", () => {
    expect(coalesceAdjacent<Item>([], byKind, sumMerge)).toEqual([]);
  });
});
