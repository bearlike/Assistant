/**
 * Inspector palette + edge grouping — the non-component shared vocabulary.
 *
 * Kept separate from `parts.tsx` (the shared component atoms) so each file has
 * one job: this module owns the kind/edge/layer colour maps (ported from the
 * original 2D graph side panel) and the pure edge-grouping helper.
 */

import type { GraphEdgeKind, GraphLayer } from "../api/types";
import type { AdjacentEdge } from "./GraphIndex";

/**
 * Kind → dot colour. Exhaustive over the node-kind union plus the scene's
 * `Folder` grouping node so every dot resolves to a themed token. Typed as
 * `Record<string, …>` since the renderer's `Folder` kind isn't an AST kind.
 */
export const KIND_DOT: Record<string, string> = {
  // Folder reuses the module token — matches `graphTheme.KIND_VAR`/`FOLDER_VAR`
  // (the scene) and the 2D screen's `KIND_DOT`, so the dot agrees everywhere.
  Folder: "bg-[hsl(var(--graph-module))]",
  File: "bg-[hsl(var(--graph-file))]",
  Module: "bg-[hsl(var(--graph-module))]",
  Class: "bg-[hsl(var(--graph-class))]",
  Function: "bg-[hsl(var(--graph-function))]",
  Method: "bg-[hsl(var(--graph-method))]",
  Interface: "bg-[hsl(var(--graph-interface))]",
  Object: "bg-[hsl(var(--graph-object))]",
  Property: "bg-[hsl(var(--graph-property))]",
  External: "bg-[hsl(var(--graph-external))]",
  Entity: "bg-[hsl(var(--graph-entity))]",
  Memory: "bg-[hsl(var(--graph-memory))]",
};

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

/** The kind dot class for any node kind (falls back to the soft edge token). */
export function kindDot(kind: string): string {
  return KIND_DOT[kind] ?? "bg-[hsl(var(--graph-edge-soft))]";
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
