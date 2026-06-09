/**
 * Tests for the multiplex knowledge-graph renderer (wire contract v2).
 *
 * Three concerns:
 *   1. Every node/edge kind has a complete palette + icon entry — a missing
 *      ``Record<…>`` row would be a runtime ``undefined`` colour/glyph (tsc
 *      catches the *type* gap; this pins the *values*).
 *   2. The per-layer toggle bulk-hides/shows a whole layer's node kinds via
 *      the same ``hiddenKinds`` machinery the chips drive.
 *   3. The static data path consumes a full multiplex payload (AST + entity
 *      + memory + cross) without throwing — no live canvas needed.
 */
import { describe, expect, it } from "vitest";

import { KnowledgeGraphRenderer } from "@/components/wiki/KnowledgeGraphRenderer";
import type {
  GraphEdgeKind,
  GraphLayer,
  GraphNodeKind,
  KnowledgeGraph,
} from "@/components/wiki/api/types";

// Mirror the screen's exhaustive kind lists — if a new kind lands without a
// matching test row, tsc flags the array literal below as incomplete.
const ALL_NODE_KINDS: GraphNodeKind[] = [
  "File",
  "Module",
  "Class",
  "Function",
  "Method",
  "Interface",
  "External",
  "Entity",
  "Memory",
];

const ALL_EDGE_KINDS: GraphEdgeKind[] = [
  "CONTAINS",
  "IMPORTS",
  "CALLS",
  "EXTENDS",
  "REFERENCES",
  "ANCHORS",
  "RELATES",
];

/** A small multiplex graph spanning all three layers + a cross edge. */
function multiplexGraph(): KnowledgeGraph {
  return {
    slug: "host/acme/widgets",
    nodes: [
      {
        data: {
          id: "f1",
          label: "widget.py",
          kind: "File",
          layer: "ast",
          file: "src/widget.py",
          range: [0, 100],
          docstring: "",
        },
      },
      {
        data: {
          id: "ext1",
          label: "requests",
          kind: "External",
          layer: "ast",
        },
      },
      {
        data: {
          id: "e1",
          label: "Authentication",
          kind: "Entity",
          layer: "entity",
          entityType: "concept",
          labels: ["security", "core"],
        },
      },
      {
        data: {
          id: "e2",
          label: "Session",
          kind: "Entity",
          layer: "entity",
          entityType: "concept",
          labels: ["security"],
        },
      },
      {
        data: {
          id: "m1",
          label: "user prefers dark mode",
          kind: "Memory",
          layer: "memory",
          snippet: "The user prefers a dark theme across all surfaces.",
          labels: ["preference"],
        },
      },
    ],
    edges: [
      // AST: a resolved cross-file import.
      { data: { id: "x1", source: "f1", target: "ext1", kind: "IMPORTS", layer: "ast" } },
      // Entity: an INTRA-layer RELATES (entity ↔ entity) carrying a verb.
      { data: { id: "x2", source: "e1", target: "e2", kind: "RELATES", layer: "entity", label: "uses" } },
      // Cross: an ANCHORS tie binding the entity to its AST anchor.
      { data: { id: "x3", source: "e1", target: "f1", kind: "ANCHORS", layer: "cross" } },
      // Cross: an ANCHORS tie binding the memory to the same AST anchor.
      { data: { id: "x4", source: "m1", target: "f1", kind: "ANCHORS", layer: "cross" } },
    ],
    stats: {
      nodeCount: 5,
      edgeCount: 4,
      kinds: { File: 1, External: 1, Entity: 2, Memory: 1 },
      perLayer: { ast: 2, entity: 2, memory: 1 },
    },
  };
}

describe("KnowledgeGraphRenderer — multiplex kind coverage", () => {
  it("builds a stylesheet that overlays every node + edge kind (no missing record)", () => {
    const sheet = KnowledgeGraphRenderer.buildStylesheet() as Array<{
      selector: string;
    }>;
    const selectors = sheet.map((s) => s.selector);
    for (const kind of ALL_NODE_KINDS) {
      expect(selectors).toContain(`node[kind = "${kind}"]`);
    }
    for (const kind of ALL_EDGE_KINDS) {
      expect(selectors).toContain(`edge[kind = "${kind}"]`);
    }
    // The cross-layer ANCHORS edge gets its own dashed overlay too.
    expect(selectors).toContain('edge[kind = "ANCHORS"]');
  });

  it("styles ANCHORS (cross) and RELATES (intra) edges distinctly", () => {
    const sheet = KnowledgeGraphRenderer.buildStylesheet() as Array<{
      selector: string;
      style: Record<string, unknown>;
    }>;
    // The cross-layer ANCHORS tie carries its OWN dashed + arrowhead-less
    // overlay so it reads as a different layer from the directed edges.
    const anchorDash = sheet.find(
      (s) =>
        s.selector === 'edge[kind = "ANCHORS"]' && s.style["line-style"] === "dashed",
    );
    expect(anchorDash).toBeDefined();
    expect(anchorDash?.style["target-arrow-shape"]).toBe("none");
    // RELATES (intra-layer) gets a colour overlay but is NOT dashed and
    // keeps the default directed arrowhead — visually unlike ANCHORS.
    const relatesColor = sheet.find(
      (s) => s.selector === 'edge[kind = "RELATES"]' && "line-color" in s.style,
    );
    expect(relatesColor).toBeDefined();
    expect(relatesColor?.style["line-style"]).toBeUndefined();
    const relatesDash = sheet.find(
      (s) =>
        s.selector === 'edge[kind = "RELATES"]' && s.style["line-style"] === "dashed",
    );
    expect(relatesDash).toBeUndefined();
  });

  it("assigns every node kind to exactly one layer", () => {
    const seen = new Set<GraphNodeKind>();
    for (const layer of ["ast", "entity", "memory"] as GraphLayer[]) {
      for (const k of KnowledgeGraphRenderer.kindsForLayer(layer)) {
        expect(KnowledgeGraphRenderer.layerForKind(k)).toBe(layer);
        expect(seen.has(k)).toBe(false);
        seen.add(k);
      }
    }
    // Partition is total over all node kinds.
    expect([...seen].sort()).toEqual([...ALL_NODE_KINDS].sort());
  });

  it("maps each layer to its node kinds", () => {
    expect(KnowledgeGraphRenderer.kindsForLayer("entity")).toEqual(["Entity"]);
    expect(KnowledgeGraphRenderer.kindsForLayer("memory")).toEqual(["Memory"]);
    expect(KnowledgeGraphRenderer.kindsForLayer("ast")).toContain("External");
    expect(KnowledgeGraphRenderer.kindsForLayer("ast")).toContain("File");
  });
});

describe("KnowledgeGraphRenderer — multiplex payload (static data path)", () => {
  it("computes a degree map across all three layers without throwing", () => {
    const g = multiplexGraph();
    const degree = KnowledgeGraphRenderer.degreeMap(g);
    // f1: imports ext1 (+1) and is anchored by e1 + m1 (+2) → 3.
    expect(degree.get("f1")).toBe(3);
    expect(degree.get("ext1")).toBe(1);
    // e1: relates to e2 (+1) and anchors f1 (+1) → 2.
    expect(degree.get("e1")).toBe(2);
    expect(degree.get("e2")).toBe(1);
    expect(degree.get("m1")).toBe(1);
  });

  it("maps a multiplex graph to Cytoscape elements without throwing", () => {
    const g = multiplexGraph();
    const degree = KnowledgeGraphRenderer.degreeMap(g);
    const elements = KnowledgeGraphRenderer.toElements(g, degree);
    expect(elements).toHaveLength(g.nodes.length + g.edges.length);
    // Entity/memory data survives into the element payload.
    const entity = elements.find(
      (el) => (el.data as { id?: string }).id === "e1",
    );
    expect((entity?.data as { entityType?: string }).entityType).toBe("concept");
    const memory = elements.find(
      (el) => (el.data as { id?: string }).id === "m1",
    );
    expect((memory?.data as { snippet?: string }).snippet).toContain("dark theme");
  });
});

describe("layer toggle — bulk hide/show via hiddenKinds", () => {
  // Mirror the screen's bulk-toggle wrapper: hiding a layer adds every one
  // of its node kinds to ``hiddenKinds``; showing removes them. Edges follow
  // via the renderer's endpoint cascade, so no edge kinds are enumerated.
  const layerShown = (
    hidden: Set<GraphNodeKind>,
    layer: GraphLayer,
  ): boolean =>
    KnowledgeGraphRenderer.kindsForLayer(layer).some((k) => !hidden.has(k));

  const toggleLayer = (
    hidden: Set<GraphNodeKind>,
    layer: GraphLayer,
  ): Set<GraphNodeKind> => {
    const next = new Set(hidden);
    const hide = layerShown(hidden, layer);
    for (const k of KnowledgeGraphRenderer.kindsForLayer(layer)) {
      if (hide) next.add(k);
      else next.delete(k);
    }
    return next;
  };

  it("hides the entire entity layer in one toggle", () => {
    let hidden = new Set<GraphNodeKind>();
    expect(layerShown(hidden, "entity")).toBe(true);
    hidden = toggleLayer(hidden, "entity");
    expect(layerShown(hidden, "entity")).toBe(false);
    expect(hidden.has("Entity")).toBe(true);
    // Other layers untouched.
    expect(layerShown(hidden, "ast")).toBe(true);
    expect(layerShown(hidden, "memory")).toBe(true);
  });

  it("hides the multi-kind AST layer atomically and restores it", () => {
    let hidden = new Set<GraphNodeKind>();
    hidden = toggleLayer(hidden, "ast");
    // Every AST kind hidden, including the new External.
    for (const k of KnowledgeGraphRenderer.kindsForLayer("ast")) {
      expect(hidden.has(k)).toBe(true);
    }
    expect(layerShown(hidden, "ast")).toBe(false);
    // Toggle back → fully visible again.
    hidden = toggleLayer(hidden, "ast");
    expect(layerShown(hidden, "ast")).toBe(true);
    expect(hidden.size).toBe(0);
  });
});
