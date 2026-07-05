/**
 * Tests for CollapseModel — the pure level-of-detail brain behind the 3D
 * galaxy. Deterministic fixtures, no DOM / three.js. The model is PURE over an
 * externally-held ``expanded`` set (the screen owns it as React state), so the
 * tests drive ``visibleGraph(expanded)`` directly. Concerns:
 *   1. ``initialExpanded`` = top-level (depth 1) folders only.
 *   2. Expanding / collapsing (by mutating the passed set) changes visibility.
 *   3. Edges into a collapsed folder re-point onto the supernode.
 *   4. Duplicate supernode↔supernode ties aggregate into one weighted edge
 *      that carries its constituent originals.
 */
import { describe, expect, it } from "vitest";

import { CollapseModel } from "./collapseModel";
import type { CollapsedEdge } from "./collapseModel";
import type {
  GraphEdgeKind,
  KnowledgeGraph,
  KnowledgeGraphEdge,
  KnowledgeGraphNode,
} from "./api/types";

// ── Fixture builders ──────────────────────────────────────────────────

function folder(path: string, parentId: string | null): KnowledgeGraphNode {
  return {
    data: {
      id: `folder:${path}`,
      label: path.split("/").pop() ?? path,
      kind: "Folder",
      layer: "ast",
      folderPath: path,
      parentId,
    },
  };
}

function file(id: string, parentId: string | null): KnowledgeGraphNode {
  return {
    data: { id, label: id, kind: "File", layer: "ast", parentId },
  };
}

function edge(
  id: string,
  source: string,
  target: string,
  kind: GraphEdgeKind = "IMPORTS",
): KnowledgeGraphEdge {
  return { data: { id, source, target, kind, layer: "ast" } };
}

/**
 * A two-folder repo:
 *   folder:src        (depth 1)
 *     ├ folder:src/a  (depth 2)  → a1.ts, a2.ts
 *     └ b1.ts
 *   folder:lib        (depth 1)  → l1.ts
 *
 * Relationship edges:
 *   a1 → b1     (sibling inside src)
 *   a2 → l1     (cross-folder, src/a → lib)
 *   a1 → l1     (cross-folder, src/a → lib)  ← duplicates a2→l1 once folded
 */
function makeGraph(): KnowledgeGraph {
  const nodes: KnowledgeGraphNode[] = [
    folder("src", null),
    folder("src/a", "folder:src"),
    file("a1.ts", "folder:src/a"),
    file("a2.ts", "folder:src/a"),
    file("b1.ts", "folder:src"),
    folder("lib", null),
    file("l1.ts", "folder:lib"),
  ];
  const edges: KnowledgeGraphEdge[] = [
    // CONTAINS ties fold the tree — never rendered as relationships.
    edge("c1", "folder:src", "folder:src/a", "CONTAINS"),
    edge("c2", "folder:src/a", "a1.ts", "CONTAINS"),
    edge("c3", "folder:src/a", "a2.ts", "CONTAINS"),
    edge("c4", "folder:src", "b1.ts", "CONTAINS"),
    edge("c5", "folder:lib", "l1.ts", "CONTAINS"),
    // Real relationships.
    edge("e1", "a1.ts", "b1.ts", "IMPORTS"),
    edge("e2", "a2.ts", "l1.ts", "IMPORTS"),
    edge("e3", "a1.ts", "l1.ts", "IMPORTS"),
  ];
  return {
    slug: "host/acme/repo",
    nodes,
    edges,
    stats: { nodeCount: nodes.length, edgeCount: edges.length, kinds: {}, folderCount: 3 },
  };
}

const ids = (g: { nodes: KnowledgeGraphNode[] }): string[] =>
  g.nodes.map((n) => n.data.id).sort();

describe("CollapseModel.initialExpanded", () => {
  it("expands only top-level (depth 1) folders", () => {
    const init = CollapseModel.initialExpanded(makeGraph());
    expect(init.has("folder:src")).toBe(true);
    expect(init.has("folder:lib")).toBe(true);
    expect(init.has("folder:src/a")).toBe(false);
  });

  it("shows depth-1 contents + collapsed depth-2 folder as a supernode", () => {
    const g = makeGraph();
    const m = new CollapseModel(g);
    const vis = ids(m.visibleGraph(CollapseModel.initialExpanded(g)));
    // src + lib expanded → their direct children visible; src/a collapsed →
    // src/a is a visible supernode but a1/a2 are hidden inside it.
    expect(vis).toEqual(
      ["b1.ts", "folder:lib", "folder:src", "folder:src/a", "l1.ts"].sort(),
    );
    expect(vis).not.toContain("a1.ts");
    expect(vis).not.toContain("a2.ts");
  });
});

describe("CollapseModel — expansion changes visibility", () => {
  it("expanding a folder reveals its children", () => {
    const g = makeGraph();
    const m = new CollapseModel(g);
    const expanded = new Set(["folder:src", "folder:lib", "folder:src/a"]);
    const vis = ids(m.visibleGraph(expanded));
    expect(vis).toContain("a1.ts");
    expect(vis).toContain("a2.ts");
    expect(vis).toContain("folder:src/a");
  });

  it("collapsing an ancestor hides everything beneath it (ancestor wins)", () => {
    const g = makeGraph();
    const m = new CollapseModel(g);
    // src collapsed even though src/a is in the set — ancestor collapse wins.
    const expanded = new Set(["folder:lib", "folder:src/a"]);
    const vis = ids(m.visibleGraph(expanded));
    expect(vis).not.toContain("folder:src/a");
    expect(vis).not.toContain("a1.ts");
    expect(vis).not.toContain("b1.ts");
    expect(vis).toContain("folder:src");
    expect(vis).toContain("folder:lib");
  });

  it("isFolder reports folder membership", () => {
    const m = new CollapseModel(makeGraph());
    expect(m.isFolder("folder:src/a")).toBe(true);
    expect(m.isFolder("a1.ts")).toBe(false);
  });
});

describe("CollapseModel — edge re-pointing", () => {
  it("re-points an edge whose endpoint is inside a collapsed folder", () => {
    const g = makeGraph();
    const m = new CollapseModel(g);
    // Default: src/a collapsed. e1 (a1→b1) has a1 hidden inside src/a, b1
    // visible under src → the edge re-points folder:src/a → b1.ts.
    const { edges } = m.visibleGraph(CollapseModel.initialExpanded(g));
    const e1 = edges.find(
      (e) => e.data.source === "folder:src/a" && e.data.target === "b1.ts",
    );
    expect(e1).toBeDefined();
    expect(e1?.data.kind).toBe("IMPORTS");
    expect(edges.some((e) => e.data.source === "a1.ts" || e.data.target === "a1.ts")).toBe(
      false,
    );
  });

  it("drops edges that fold into a self-loop on one supernode", () => {
    const g = makeGraph();
    // Add an intra-folder edge a1 → a2 (both inside src/a). Collapsed, both
    // resolve to folder:src/a → a self-loop, which must be dropped.
    g.edges.push(edge("e4", "a1.ts", "a2.ts", "CALLS"));
    const m = new CollapseModel(g);
    const selfLoops = m
      .visibleGraph(CollapseModel.initialExpanded(g))
      .edges.filter((e) => e.data.source === e.data.target);
    expect(selfLoops).toHaveLength(0);
  });
});

describe("CollapseModel — aggregation", () => {
  it("folds duplicate supernode ties into one weighted edge with constituents", () => {
    const g = makeGraph();
    const m = new CollapseModel(g);
    // src/a collapsed: e2 (a2→l1) and e3 (a1→l1) both re-point to
    // folder:src/a → l1.ts, same kind IMPORTS → ONE aggregated edge weight 2.
    const { edges } = m.visibleGraph(CollapseModel.initialExpanded(g));
    const agg = edges.filter(
      (e) =>
        e.data.source === "folder:src/a" &&
        e.data.target === "l1.ts" &&
        e.data.kind === "IMPORTS",
    );
    expect(agg).toHaveLength(1);
    expect(agg[0].data.weight).toBe(2);
    expect(agg[0].data.aggregated?.map((e) => e.data.id).sort()).toEqual(["e2", "e3"]);
  });

  it("keeps a singleton tie as a verbatim passthrough (no weight)", () => {
    const g = makeGraph();
    const m = new CollapseModel(g);
    const { edges } = m.visibleGraph(CollapseModel.initialExpanded(g));
    const e1 = edges.find(
      (e) => e.data.source === "folder:src/a" && e.data.target === "b1.ts",
    );
    expect(e1?.data.id).toBe("e1");
    expect(e1?.data.weight).toBeUndefined();
    expect(e1?.data.aggregated).toBeUndefined();
  });

  it("resolveVisible maps a hidden node to its collapsed ancestor", () => {
    const g = makeGraph();
    const m = new CollapseModel(g);
    const expanded = CollapseModel.initialExpanded(g);
    expect(m.resolveVisible("a1.ts", expanded)).toBe("folder:src/a");
    expect(m.resolveVisible("b1.ts", expanded)).toBe("b1.ts");
    expect(m.resolveVisible("folder:lib", expanded)).toBe("folder:lib");
  });
});

describe("CollapseModel — containment ties (hierarchy backbone)", () => {
  const containment = (edges: CollapsedEdge[]): CollapsedEdge[] =>
    edges.filter((e) => e.data.containment);
  const has = (edges: CollapsedEdge[], s: string, t: string): boolean =>
    edges.some((e) => e.data.source === s && e.data.target === t);

  it("emits faint containment ties for the visible hierarchy backbone", () => {
    const g = makeGraph();
    const m = new CollapseModel(g);
    const ties = containment(m.visibleGraph(CollapseModel.initialExpanded(g)).edges);
    // depth-1 folders expanded → their direct children tie back to them; the
    // collapsed src/a is shown as a tie from its (expanded) parent src.
    expect(has(ties, "folder:src", "b1.ts")).toBe(true);
    expect(has(ties, "folder:lib", "l1.ts")).toBe(true);
    expect(has(ties, "folder:src", "folder:src/a")).toBe(true);
    for (const e of ties) {
      expect(e.data.kind).toBe("CONTAINS");
      expect(e.data.containment).toBe(true);
    }
  });

  it("reveals deeper containment ties when a subfolder is expanded", () => {
    const g = makeGraph();
    const m = new CollapseModel(g);
    const expanded = new Set(["folder:src", "folder:lib", "folder:src/a"]);
    const ties = containment(m.visibleGraph(expanded).edges);
    expect(has(ties, "folder:src/a", "a1.ts")).toBe(true);
    expect(has(ties, "folder:src/a", "a2.ts")).toBe(true);
  });

  it("emits file→symbol containment ties, not only folder→file", () => {
    const nodes: KnowledgeGraphNode[] = [
      folder("src", null),
      file("a1.ts", "folder:src"),
      {
        data: {
          id: "sym:Foo",
          label: "Foo",
          kind: "Class",
          layer: "ast",
          parentId: "a1.ts",
        },
      },
    ];
    const edges: KnowledgeGraphEdge[] = [
      edge("c1", "folder:src", "a1.ts", "CONTAINS"),
      edge("c2", "a1.ts", "sym:Foo", "CONTAINS"),
    ];
    const g: KnowledgeGraph = {
      slug: "host/acme/repo",
      nodes,
      edges,
      stats: { nodeCount: 3, edgeCount: 2, kinds: {}, folderCount: 1 },
    };
    const m = new CollapseModel(g);
    const ties = containment(m.visibleGraph(new Set(["folder:src"])).edges);
    expect(has(ties, "a1.ts", "sym:Foo")).toBe(true);
  });

  it("absorbs a collapsed folder's internal containment — no self-loops, no hidden endpoints", () => {
    const g = makeGraph();
    const m = new CollapseModel(g);
    // Default: src/a collapsed → a1/a2 live inside it.
    const ties = containment(m.visibleGraph(CollapseModel.initialExpanded(g)).edges);
    expect(ties.some((e) => e.data.source === "a1.ts" || e.data.target === "a1.ts")).toBe(
      false,
    );
    expect(ties.some((e) => e.data.source === "a2.ts" || e.data.target === "a2.ts")).toBe(
      false,
    );
    expect(ties.some((e) => e.data.source === e.data.target)).toBe(false);
  });

  it("dedupes duplicate containment ties onto one edge", () => {
    const g = makeGraph();
    // A second folder:src → b1.ts CONTAINS edge (malformed/duplicate payload).
    g.edges.push(edge("c4-dup", "folder:src", "b1.ts", "CONTAINS"));
    const m = new CollapseModel(g);
    const ties = containment(m.visibleGraph(CollapseModel.initialExpanded(g)).edges);
    const matches = ties.filter(
      (e) => e.data.source === "folder:src" && e.data.target === "b1.ts",
    );
    expect(matches).toHaveLength(1);
  });

  it("tags containment apart from relationship ties and never weights them", () => {
    const g = makeGraph();
    const m = new CollapseModel(g);
    const { edges } = m.visibleGraph(CollapseModel.initialExpanded(g));
    const ties = containment(edges);
    const rels = edges.filter((e) => !e.data.containment);
    expect(ties.length).toBeGreaterThan(0);
    expect(rels.length).toBeGreaterThan(0);
    // The partition is clean: relationship ties are never CONTAINS, and
    // containment ties are never folded into a weighted aggregate.
    expect(rels.every((e) => e.data.kind !== "CONTAINS")).toBe(true);
    for (const e of ties) {
      expect(e.data.weight).toBeUndefined();
      expect(e.data.aggregated).toBeUndefined();
    }
  });
});

describe("CollapseModel.foldedCounts", () => {
  it("counts all recursive descendants per folder", () => {
    const counts = new CollapseModel(makeGraph()).foldedCounts();
    expect(counts.get("folder:src/a")).toBe(2); // a1, a2
    expect(counts.get("folder:lib")).toBe(1); // l1
    expect(counts.get("folder:src")).toBe(4); // src/a, a1, a2, b1
  });
});
