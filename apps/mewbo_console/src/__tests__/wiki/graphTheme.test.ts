/**
 * Exhaustiveness guard for the shared graph theme (``components/wiki/graphTheme.ts``).
 *
 * The 3D galaxy + the SCG workspace graph both render off these maps; a missing
 * row is a runtime ``undefined`` colour / layer / label (``tsc`` catches the
 * *type* gap on the ``Record<Kind, …>`` literals, this pins the *values* and the
 * forward/backward completeness). The hardcoded union arrays below mirror the
 * CLOSED unions in ``components/wiki/api/types.ts`` — they ARE the tripwire:
 * adding a kind to a union forces (via ``Record<…>`` tsc) a new map row, which
 * makes ``keys(map) === arrayBelow`` fail until this array is updated too, so no
 * kind ever ships without a colour/layer/label.
 */
import { describe, expect, it } from "vitest";

import {
  ALL_NODE_KINDS,
  EDGE_CONTAIN_VAR,
  EDGE_VAR,
  FOLDER_VAR,
  KIND_DOT,
  KIND_LAYER,
  KIND_VAR,
  LAYER_DOT,
  LAYER_LABEL,
  LAYER_ORDER,
  nodeSize,
} from "@/components/wiki/graphTheme";
import type {
  GraphEdgeKind,
  GraphLayer,
  GraphNodeKind,
} from "@/components/wiki/api/types";

// The CLOSED unions, enumerated. Cross-checked against the type definitions; a
// union edit that forgets one of these arrays trips the set-equality assertions.
const NODE_KINDS: GraphNodeKind[] = [
  "File",
  "Module",
  "Class",
  "Function",
  "Method",
  "Interface",
  "Object",
  "Property",
  "External",
  "Entity",
  "Memory",
  "Folder",
];

const EDGE_KINDS: GraphEdgeKind[] = [
  "CONTAINS",
  "IMPORTS",
  "CALLS",
  "EXTENDS",
  "REFERENCES",
  "ANCHORS",
  "RELATES",
];

const LAYERS: GraphLayer[] = ["ast", "entity", "memory"];

/** Set-equality helper — order-independent, both directions. */
function sameMembers(a: readonly string[], b: readonly string[]): void {
  expect(new Set(a)).toEqual(new Set(b));
}

describe("graphTheme — node-kind coverage", () => {
  it("gives every GraphNodeKind a colour, dot + layer (no missing/extra rows)", () => {
    sameMembers(Object.keys(KIND_VAR), NODE_KINDS);
    sameMembers(Object.keys(KIND_DOT), NODE_KINDS);
    sameMembers(Object.keys(KIND_LAYER), NODE_KINDS);
    for (const kind of NODE_KINDS) {
      // Fill token is a ``--graph-*`` CSS var…
      expect(KIND_VAR[kind]).toMatch(/^--graph-/);
      // …the legend dot is a Tailwind ``bg-…`` class…
      expect(KIND_DOT[kind]).toMatch(/^bg-/);
      // …and the kind belongs to a real layer.
      expect(LAYERS).toContain(KIND_LAYER[kind]);
    }
  });

  it("lists every node kind exactly once in ALL_NODE_KINDS (toolbar order)", () => {
    sameMembers(ALL_NODE_KINDS, NODE_KINDS);
    expect(ALL_NODE_KINDS).toHaveLength(NODE_KINDS.length);
  });
});

describe("graphTheme — edge-kind coverage", () => {
  it("gives every GraphEdgeKind a CSS-var line colour", () => {
    sameMembers(Object.keys(EDGE_VAR), EDGE_KINDS);
    for (const kind of EDGE_KINDS) {
      expect(EDGE_VAR[kind]).toMatch(/^--graph-/);
    }
  });
});

describe("graphTheme — layer coverage", () => {
  it("labels + dots every GraphLayer and orders them all", () => {
    sameMembers(Object.keys(LAYER_LABEL), LAYERS);
    sameMembers(Object.keys(LAYER_DOT), LAYERS);
    sameMembers(LAYER_ORDER, LAYERS);
    for (const layer of LAYERS) {
      expect(LAYER_LABEL[layer].length).toBeGreaterThan(0);
      expect(LAYER_DOT[layer]).toMatch(/^bg-/);
    }
  });

  it("exposes the hierarchy-only folder + containment tokens", () => {
    expect(FOLDER_VAR).toMatch(/^--graph-/);
    expect(EDGE_CONTAIN_VAR).toMatch(/^--graph-/);
  });
});

describe("graphTheme — nodeSize curve", () => {
  it("grows monotonically with degree", () => {
    expect(nodeSize(5)).toBeGreaterThan(nodeSize(0));
    expect(nodeSize(50)).toBeGreaterThan(nodeSize(5));
  });

  it("boosts folder supernodes above an equal-degree leaf, scaled by fold count", () => {
    // A folder reads bigger than a plain node of the same degree…
    expect(nodeSize(5, true, 10)).toBeGreaterThan(nodeSize(5, false, 0));
    // …and a folder folding more descendants is bigger still.
    expect(nodeSize(5, true, 200)).toBeGreaterThan(nodeSize(5, true, 1));
  });
});
