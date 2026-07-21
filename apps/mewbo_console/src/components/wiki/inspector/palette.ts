/**
 * Inspector palette + edge grouping — the non-component shared vocabulary.
 *
 * Kept separate from `parts.tsx` (the shared component atoms) so each file has
 * one job: this module owns the edge/layer colour maps (ported from the
 * original 2D graph side panel) and the pure edge-grouping helper. The node
 * `kind` → colour map is NOT owned here — `graphTheme.ts` is the single home
 * for that (`kindDot()` below reads it directly) so the inspector panels and
 * the 3D galaxy can never disagree about what a kind's dot looks like.
 */

import { KIND_DOT } from "../graphTheme";
import type { GraphEdgeKind, GraphLayer } from "../api/types";
import type { AdjacentEdge } from "./GraphIndex";

export const EDGE_DOT: Record<GraphEdgeKind, string> = {
  CONTAINS: "bg-[hsl(var(--graph-edge-soft))]",
  IMPORTS: "bg-[hsl(var(--graph-file))]",
  CALLS: "bg-[hsl(var(--graph-function))]",
  EXTENDS: "bg-[hsl(var(--graph-class))]",
  REFERENCES: "bg-[hsl(var(--graph-edge-soft))]",
  ANCHORS: "bg-[hsl(var(--graph-edge-anchor))]",
  RELATES: "bg-[hsl(var(--graph-edge-relates))]",
};

export const LAYER_LABEL: Record<GraphLayer, string> = {
  ast: "Code",
  entity: "Entities",
  memory: "Memory",
};

/** AST node kinds that are code symbols (vs File/Module/Folder containers) —
 *  shared by the Folder/File panels to tally + list contained symbols. */
export const SYMBOL_KINDS: ReadonlySet<string> = new Set([
  "Class",
  "Function",
  "Method",
  "Interface",
  "Object",
  "Property",
]);

/**
 * The kind dot class for any node kind (falls back to the soft edge token).
 * Reads the single canonical `graphTheme.KIND_DOT` map — this module used to
 * keep its own copy of the same values, which is exactly the kind of drift
 * `graphTheme.ts` exists to prevent. The cast is required here, and only
 * here: callers pass a plain `string` (a dangling edge target's kind, or
 * other unvalidated wire data), while `KIND_DOT` itself stays keyed by the
 * closed `GraphNodeKind` union everywhere else it's read.
 */
export function kindDot(kind: string): string {
  return (KIND_DOT as Record<string, string>)[kind] ?? "bg-[hsl(var(--graph-edge-soft))]";
}

/** Bucket an adjacency list by edge kind, preserving first-seen kind order. */
export function groupEdgesByKind(
  edges: AdjacentEdge[],
): Array<{ kind: GraphEdgeKind; items: AdjacentEdge[] }> {
  const buckets = new Map<GraphEdgeKind, AdjacentEdge[]>();
  for (const e of edges) {
    const kind = e.edge.data.kind;
    const list = buckets.get(kind);
    if (list) list.push(e);
    else buckets.set(kind, [e]);
  }
  return [...buckets.entries()].map(([kind, items]) => ({ kind, items }));
}
